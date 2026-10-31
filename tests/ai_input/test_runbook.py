"""ステップ2 手順書生成（AIRunbookGenerator）のユニットテスト。

Ollama をモックし、共通ヘルパー経由の chunk 分割・結果統合・order 正規化を検証する。
多項目を1コールに詰めると num_ctx 超過で空応答になる不具合の再発防止を兼ねる。
"""

from __future__ import annotations

import json
import re
import types

import pytest

from kawasemi.ai_input.llm_json import DEFAULT_OPTIONS, LARGE_CONTEXT_OPTIONS
from kawasemi.ai_input.runbook import RUNBOOK_CHUNK_SIZE, AIRunbookGenerator
from kawasemi.core.exceptions import AIServiceException


def _resp(content):
    return types.SimpleNamespace(message=types.SimpleNamespace(content=content))


def _runbooks_resp_from_message(user_message: str):
    """user メッセージに含まれる linkId ごとに手順書1件を返す応答を作る"""
    link_ids = re.findall(r"### linkId: (\S+)", user_message)
    payload = {
        "runbooks": [
            {
                "link_id": lid,
                # order は意図的に 2 始まりにし、正規化で 1 始まりになることを確認
                "steps": [{"order": 2, "title": "起点", "description": "説明"}],
            }
            for lid in link_ids
        ]
    }
    return _resp(json.dumps(payload))


def _patch_client(gen: AIRunbookGenerator, calls: list, response_fn=None):
    async def fake_chat(**kwargs):
        calls.append(kwargs)
        if response_fn is not None:
            return response_fn(kwargs["messages"][1]["content"])
        return _runbooks_resp_from_message(kwargs["messages"][1]["content"])

    gen._client = types.SimpleNamespace(chat=fake_chat)


def _items(n: int):
    return [
        {
            "link_id": f"q{i}",
            "item_text": f"項目{i}",
            "source_category": "manual",
            "annotation": None,
            "location": None,
        }
        for i in range(n)
    ]


def _chunk_item_counts(calls: list) -> list[int]:
    """各コールの user メッセージに何項目載ったかを数える。"""
    return [len(re.findall(r"### linkId: ", c["messages"][1]["content"])) for c in calls]


@pytest.mark.asyncio
async def test_generate_chunks_and_merges():
    """10項目を `RUNBOOK_CHUNK_SIZE` ずつに分けて呼び、結果は10件に統合される。

    **項目数を 10 にしているのは 9 では刻み幅 3 と 4 を区別できないため**
    （`ceil(9/3) == ceil(9/4) == 3`）。刻み幅は num_ctx の予算から決めているので
    （`runbook.py` の `RUNBOOK_CHUNK_SIZE`）、`chunk_size=` の指定が呼び出しから
    外れて既定の4へ戻る退行を、ここで落とせる規模にしておく。

    コール数だけでなく**各コールに載った項目数**も見る。分割数は端数の丸めで一致して
    しまうことがあるが、1コールの項目数が上限を超えていないことは直接の性質である。
    """
    gen = AIRunbookGenerator()
    calls: list = []
    _patch_client(gen, calls)

    result = await gen.generate(_items(10))

    # **期待値は逐語で書く**。`ceil(10 / RUNBOOK_CHUNK_SIZE)` と
    # `max(counts) == RUNBOOK_CHUNK_SIZE` は**定数追従**なので、定数を変えると両辺が一緒に動いて
    # 通る（定数追従に書き換えたテストは、その定数の変化を映さなくなる）。
    # 10項目・幅3 なら `[3, 3, 3, 1]` で、幅 2 / 4 / 5 のどれとも違う。
    assert RUNBOOK_CHUNK_SIZE == 3, "前提: 刻み幅は3（変えたらこの期待値も変える）"
    counts = _chunk_item_counts(calls)
    assert counts == [3, 3, 3, 1], counts
    assert len(result) == 10
    assert {r.link_id for r in result} == {f"q{i}" for i in range(10)}


@pytest.mark.asyncio
async def test_generate_passes_num_ctx():
    """空応答対策として num_ctx が options に渡ること"""
    gen = AIRunbookGenerator()
    calls: list = []
    _patch_client(gen, calls)

    await gen.generate(_items(1))

    # **`runbook` が実際に渡す options を読む**。逐語の 8192 だと、
    # 本番だけ 32768 へ移してもテストは緑のまま＝静かなドリフトになる。
    assert calls[0]["options"] == LARGE_CONTEXT_OPTIONS


@pytest.mark.asyncio
async def test_generate_passes_the_large_num_ctx_not_the_default() -> None:
    """**既定（8192）へ戻る退行を止める**。

    `think=True` は固定されており thinking は必ず出る。8192 では入力と出力予約で
    予算が尽き、thinking の長いモデルでは空 content か壊れた JSON が返る。
    `options` を渡し忘れると `chat_json` の `options or DEFAULT_OPTIONS` で静かに 8192 へ
    落ちるので、**渡していること自体**を振る舞いで固定する。
    """
    gen = AIRunbookGenerator()
    calls: list = []
    _patch_client(gen, calls)

    await gen.generate(_items(1))

    assert calls[0]["options"]["num_ctx"] == LARGE_CONTEXT_OPTIONS["num_ctx"]
    assert calls[0]["options"]["num_ctx"] != DEFAULT_OPTIONS["num_ctx"]


@pytest.mark.asyncio
async def test_generate_normalizes_order():
    gen = AIRunbookGenerator()
    _patch_client(gen, [])

    result = await gen.generate(_items(1))

    assert result[0].steps[0].order == 1  # 2 → 1 に正規化


@pytest.mark.asyncio
async def test_generate_empty_items_no_call():
    gen = AIRunbookGenerator()
    calls: list = []
    _patch_client(gen, calls)

    result = await gen.generate([])

    assert result == []
    assert calls == []


@pytest.mark.asyncio
async def test_generate_keeps_the_runbooks_of_the_chunks_that_answered():
    """**手順書生成も部分結果を扱う**。

    12項目・幅3 で4チャンク。2番目だけ空応答にしても、残りのチャンクは試され、
    応答のあったチャンクの手順書は残る。この経路の「未試行」は**手順書を生成していない**
    という意味で、ルートは渡した link_id との差として出す。
    """
    gen = AIRunbookGenerator()
    calls: list = []

    def response_fn(user_message: str):
        if len(calls) == 2:
            return _resp("")
        return _runbooks_resp_from_message(user_message)

    _patch_client(gen, calls, response_fn=response_fn)

    result = await gen.generate(_items(12))

    assert _chunk_item_counts(calls) == [3, 3, 3, 3], "失敗の次のチャンクが投げられていない"
    assert {r.link_id for r in result} == {f"q{i}" for i in range(12)} - {"q3", "q4", "q5"}
    # **応答のあったチャンクでは order の正規化も効いている**（部分結果でも後処理を飛ばさない）
    assert all(r.steps[0].order == 1 for r in result)


@pytest.mark.asyncio
async def test_generate_empty_content_raises():
    gen = AIRunbookGenerator()
    _patch_client(gen, [], response_fn=lambda _msg: _resp(""))

    with pytest.raises(AIServiceException, match="空のレスポンス"):
        await gen.generate(_items(1))
