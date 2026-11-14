"""llm_json 共通ヘルパーのユニットテスト。

- chat_json: 空content / 不正JSON / API例外 を AIServiceException に整形する
- chat_json_chunked: chunk_size ごとに分割呼び出しし、結果を統合する
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import yaml
from pydantic import BaseModel

from kawasemi.ai_input.llm_json import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_OPTIONS,
    DEFAULT_THINK,
    LARGE_CONTEXT_OPTIONS,
    MAX_CONSECUTIVE_CHUNK_FAILURES,
    PartialResultException,
    chat_json,
    chat_json_chunked,
    merge_by_link_id,
    strict_response_format,
    with_model_overrides,
)
from kawasemi.ai_input.schemas import (
    LLMClinicalDraft,
    LLMClinicalDrafts,
    LLMLocationChoice,
)
from kawasemi.core.config import settings
from kawasemi.core.exceptions import AIServiceException


def _resp(content):
    """ollama の chat 応答（response.message.content）を模す"""
    return SimpleNamespace(message=SimpleNamespace(content=content))


def _client(*, return_value=None, side_effect=None):
    client = SimpleNamespace()
    client.chat = AsyncMock(return_value=return_value, side_effect=side_effect)
    return client


@pytest.mark.asyncio
async def test_chat_json_parses_dict():
    client = _client(return_value=_resp(json.dumps({"a": 1, "b": "x"})))
    out = await chat_json(
        client,
        "m",
        system_prompt="s",
        user_message="u",
        response_format={},
        label="テスト",
    )
    assert out == {"a": 1, "b": "x"}


@pytest.mark.asyncio
async def test_chat_json_passes_default_num_ctx():
    """既定 options に num_ctx が含まれ、空応答対策が効いていることを確認"""
    client = _client(return_value=_resp(json.dumps({})))
    await chat_json(
        client,
        "m",
        system_prompt="s",
        user_message="u",
        response_format={},
        label="テスト",
    )
    _, kwargs = client.chat.call_args
    assert kwargs["options"]["num_ctx"] == 8192


@pytest.mark.asyncio
async def test_chat_json_empty_content_raises():
    client = _client(return_value=_resp(""))
    with pytest.raises(AIServiceException, match="空のレスポンス"):
        await chat_json(
            client,
            "m",
            system_prompt="s",
            user_message="u",
            response_format={},
            label="テスト",
        )


@pytest.mark.asyncio
async def test_chat_json_invalid_json_raises():
    client = _client(return_value=_resp("これはJSONではない"))
    with pytest.raises(AIServiceException, match="パースに失敗"):
        await chat_json(
            client,
            "m",
            system_prompt="s",
            user_message="u",
            response_format={},
            label="テスト",
        )


@pytest.mark.asyncio
async def test_chat_json_api_error_raises():
    client = _client(side_effect=RuntimeError("接続失敗"))
    with pytest.raises(AIServiceException, match="失敗しました"):
        await chat_json(
            client,
            "m",
            system_prompt="s",
            user_message="u",
            response_format={},
            label="テスト",
        )


@pytest.mark.asyncio
async def test_chunked_splits_and_merges():
    """**6項目・既定幅4 → `[4, 2]`** で分割し、結果は6件に統合される。

    **9項目・コール数だけでは幅3と4を区別できない**（`ceil(9/4) == ceil(9/3) == 3`）。
    分割そのものを所有するこのモジュールの契約テストが、その形（定数に追従し、コール数だけを
    見る）で書かれていた。
    6項目にすると幅4 は `[4, 2]`・幅3 は `[3, 3]` で**各コールの項目数**が違う。
    """
    assert DEFAULT_CHUNK_SIZE == 4, "前提: 既定幅は4（変えたらこの期待値も変える）"
    items = list(range(6))

    async def fake_chat(*, model, messages, format, options, think=None):
        chunk = json.loads(messages[1]["content"])
        return _resp(json.dumps({"items": chunk}))

    client = SimpleNamespace()
    client.chat = AsyncMock(side_effect=fake_chat)

    result = await chat_json_chunked(
        client,
        "m",
        items,
        system_prompt="s",
        build_message=lambda chunk: json.dumps(chunk),
        response_format={},
        parse=lambda d: d["items"],
        label="テスト",
    )

    counts = [
        len(json.loads(c.kwargs["messages"][1]["content"])) for c in client.chat.await_args_list
    ]
    assert counts == [4, 2], "既定幅（4）で分割されていない"
    assert result == items


@pytest.mark.asyncio
async def test_chunked_single_call_when_small():
    items = [1, 2]
    assert len(items) <= DEFAULT_CHUNK_SIZE

    async def fake_chat(*, model, messages, format, options, think=None):
        chunk = json.loads(messages[1]["content"])
        return _resp(json.dumps({"items": chunk}))

    client = SimpleNamespace()
    client.chat = AsyncMock(side_effect=fake_chat)

    result = await chat_json_chunked(
        client,
        "m",
        items,
        system_prompt="s",
        build_message=lambda chunk: json.dumps(chunk),
        response_format={},
        parse=lambda d: d["items"],
        label="テスト",
    )

    assert client.chat.await_count == 1
    assert result == items


@pytest.mark.asyncio
async def test_chunked_empty_items_no_calls():
    client = _client(return_value=_resp(json.dumps({"items": []})))
    result = await chat_json_chunked(
        client,
        "m",
        [],
        system_prompt="s",
        build_message=lambda chunk: json.dumps(chunk),
        response_format={},
        parse=lambda d: d["items"],
        label="テスト",
    )
    assert result == []
    assert client.chat.await_count == 0


# --------------------------------------------------------------------------- #
# 来歴の記録
# --------------------------------------------------------------------------- #
#
# 記録の**中身**を assert するテストは `test_design_provenance.py` にあるが、そちらは
# 手で組んだ `LLMCallRecord` に対するものである。**値を assert していても、その値を
# 入れる結線が無検証**なら結線を外せる。ここは `chat_json` /
# `chat_json_chunked` を実際に通して、結線そのものを固定する。


def _records() -> tuple[list, object]:
    """収集した `LLMCallRecord` のリストと、それを受け取る recorder を返す。"""
    got: list = []
    return got, got.append


@pytest.mark.asyncio
async def test_recorder_captures_api_error():
    """API 例外でも来歴が1件残ること。

    **本番で最も起きやすい失敗**（Ollama のタイムアウト・接続拒否）がこの経路である。
    ルート側の失敗テストはパース失敗経路を通るので、ここを別に固定する。
    """
    got, recorder = _records()
    client = _client(side_effect=RuntimeError("connection refused"))

    with pytest.raises(AIServiceException):
        await chat_json(
            client,
            "m",
            system_prompt="s",
            user_message="u",
            response_format={},
            label="テスト",
            recorder=recorder,
        )

    assert len(got) == 1
    assert got[0].raw_output is None
    # 来歴へ残すのは構造化した要約（型名）。例外の文面は載せない（サーバのログには全文が残る）
    assert got[0].error == "RuntimeError"
    assert got[0].system_prompt == "s"
    assert got[0].user_message == "u"


@pytest.mark.asyncio
async def test_recorder_captures_empty_response():
    """空レスポンスでも来歴が1件残ること。"""
    got, recorder = _records()
    client = _client(return_value=_resp(""))

    with pytest.raises(AIServiceException):
        await chat_json(
            client,
            "m",
            system_prompt="s",
            user_message="u",
            response_format={},
            label="テスト",
            recorder=recorder,
        )

    assert len(got) == 1
    assert got[0].error == "空のレスポンス"


@pytest.mark.asyncio
async def test_recorder_carries_chunk_index_and_item_count():
    """分割呼び出しの何回目・何項目かが、実際の呼び出しから記録へ伝わること。

    どのチャンクにどの項目を渡したかが分からないと、記述と入力を突き合わせられない。
    `chat_json_chunked` → `chat_json` → `LLMCallRecord` の**両ホップ**を通して固定する。
    """
    got, recorder = _records()

    async def fake_chat(*, model, messages, format, options, think=None):
        return _resp(json.dumps({"items": json.loads(messages[1]["content"])}))

    client = SimpleNamespace()
    client.chat = AsyncMock(side_effect=fake_chat)

    await chat_json_chunked(
        client,
        "m",
        list(range(5)),  # chunk_size=4 → 4件 + 1件
        system_prompt="s",
        build_message=lambda chunk: json.dumps(chunk),
        response_format={},
        parse=lambda d: d["items"],
        label="テスト",
        recorder=recorder,
    )

    assert [(r.chunk_index, r.item_count) for r in got] == [(0, 4), (1, 1)]


@pytest.mark.asyncio
async def test_recorder_carries_the_provider_from_the_client():
    """**呼び出しに使った client** が名乗るプロバイダが記録へ伝わること。

    モデル名だけでは実装が特定できない。`OLLAMA_MODEL` は環境変数で上書きでき、同じ名前を
    別のエンジンが名乗れる。ここが結線されていないと、別のエンジンへ差し替えた測定が記録上
    Ollama と区別できず、モデル間の比較可能性を後から検証できない。

    **client 由来である**ことが要点なので、名乗らない client と名乗る client の両方を通す。
    設定値から取る実装だと後者が "ollama" のままになり、この対比で落ちる。
    """
    got, recorder = _records()
    plain = _client(return_value=_resp(json.dumps({})))
    await chat_json(
        plain,
        "m",
        system_prompt="s",
        user_message="u",
        response_format={},
        label="テスト",
        recorder=recorder,
    )

    named = _client(return_value=_resp(json.dumps({})))
    named.provider_name = "stub"
    await chat_json(
        named,
        "m",
        system_prompt="s",
        user_message="u",
        response_format={},
        label="テスト",
        recorder=recorder,
    )

    assert [r.provider for r in got] == ["ollama", "stub"]


@pytest.mark.asyncio
async def test_chunked_keeps_completed_chunks_when_the_deadline_hits(monkeypatch):
    """全体デッドラインで打ち切っても、**完了済みチャンクの結果を捨てない**。

    項目数が多い一括生成は、1コール数十秒のコールが数十回になるので、**どう実装しても既定の
    600 秒には収まらない**。捨てると最初の一歩（全項目の臨床的意味を下書きする）が何度
    やっても完走しない。
    """
    items = list(range(9))
    calls = 0

    async def fake_chat(*, model, messages, format, options, think=None):
        nonlocal calls
        calls += 1
        if calls == 3:
            await asyncio.sleep(5)  # 3回目でデッドラインを超えさせる
        chunk = json.loads(messages[1]["content"])
        return _resp(json.dumps({"items": chunk}))

    client = SimpleNamespace()
    client.chat = AsyncMock(side_effect=fake_chat)
    monkeypatch.setattr(settings, "llm_total_timeout", 1)

    with pytest.raises(PartialResultException) as excinfo:
        await chat_json_chunked(
            client,
            "m",
            items,
            system_prompt="s",
            build_message=lambda chunk: json.dumps(chunk),
            response_format={},
            parse=lambda d: d["items"],
            label="テスト",
            chunk_size=3,
        )

    e = excinfo.value
    # 完了した2チャンク（6件）が残る
    assert e.partial == [0, 1, 2, 3, 4, 5]
    # 3チャンク目は打ち切られたので「応答なし」に入る（着手の有無ではなく応答で数える）
    assert e.unanswered_items == 3
    assert e.details["completed"] == 6
    # **部分結果を扱わない呼び出し側は従来どおり失敗として扱える**
    assert isinstance(e, AIServiceException)


@pytest.mark.asyncio
async def test_a_deadline_with_no_results_is_a_plain_failure(monkeypatch):
    """**1件も結果が無いデッドラインは素の失敗**。

    以前はここだけ `PartialResultException` を無条件に投げていたので、**4経路が `e.partial` を
    採るようになった結果、以前 500 だった実行が 200 + `success: True` + 0件になった**
    （docstring の `Raises` は「結果が1件も無ければ `AIServiceException`」と書いてあった）。

    内訳は details に残す。**1チャンク目は送信済みなので `failed`**（送ったが返らなかった）で、
    残りが `unattempted`（送っていない）である——「応答なし0件」と報告すると、呼び出し側は
    「全項目を試して結果が無かった」と解釈し、
    「規約文書を引いても意味が決まらなかった項目」へ未試行が混入する。
    """
    items = list(range(6))

    async def fake_chat(*, model, messages, format, options, think=None):
        await asyncio.sleep(5)
        return _resp("{}")

    client = SimpleNamespace()
    client.chat = AsyncMock(side_effect=fake_chat)
    monkeypatch.setattr(settings, "llm_total_timeout", 1)

    with pytest.raises(AIServiceException) as excinfo:
        await chat_json_chunked(
            client,
            "m",
            items,
            system_prompt="s",
            build_message=lambda chunk: json.dumps(chunk),
            response_format={},
            parse=lambda d: d["items"],
            label="テスト",
            chunk_size=3,
        )
    e = excinfo.value
    assert not isinstance(e, PartialResultException), (
        "部分結果が空なのに「途中まで成功」を返している"
    )
    assert e.details["answered_items"] == 0
    # **送信済みの1チャンク（3項目）は `failed`、投げていない残りが `unattempted`**
    assert e.details["failed_items"] == 3
    assert e.details["unattempted_items"] == 3
    assert e.details["unanswered_items"] == 6, "1件も応答が無いので全項目が応答なし"


@pytest.mark.asyncio
async def test_chunked_keeps_completed_chunks_when_a_call_fails(monkeypatch):
    """**打ち切り以外の失敗でも完了済みチャンクを捨てない**。

    1コールの `ollama_timeout`（既定120秒）超過・空応答・JSON の崩れは `chat_json` が素の
    `AIServiceException` で投げるので、以前は `wait_for` を素通りして**全部捨てていた**。
    **1コールが数百秒規模**になりうるので、**1パス36コールのうち1本**が超えれば全滅する——
    **最も起こりやすい失敗経路**だった。
    """
    items = list(range(9))
    calls = 0

    async def fake_chat(*, model, messages, format, options, think=None):
        nonlocal calls
        calls += 1
        if calls == 3:
            return _resp("")  # 空応答（AIServiceException になる）
        chunk = json.loads(messages[1]["content"])
        return _resp(json.dumps({"items": chunk}))

    client = SimpleNamespace()
    client.chat = AsyncMock(side_effect=fake_chat)

    with pytest.raises(PartialResultException) as excinfo:
        await chat_json_chunked(
            client,
            "m",
            items,
            system_prompt="s",
            build_message=lambda chunk: json.dumps(chunk),
            response_format={},
            parse=lambda d: d["items"],
            label="テスト",
            chunk_size=3,
        )
    assert excinfo.value.partial == [0, 1, 2, 3, 4, 5]
    assert excinfo.value.unanswered_items == 3


@pytest.mark.asyncio
async def test_chunked_still_fails_plainly_when_the_first_call_fails():
    """**1件も完了していないときは素の失敗にする**（正常系の裏返し）。

    部分結果が空なのに `PartialResultException` を投げると、呼び出し側は「途中まで成功した」と
    読む。**モデルかプロンプトが壊れている**示唆なので、途中まで採る意味が無い。
    """
    client = SimpleNamespace()
    client.chat = AsyncMock(return_value=_resp(""))

    with pytest.raises(AIServiceException) as excinfo:
        await chat_json_chunked(
            client,
            "m",
            [1, 2, 3],
            system_prompt="s",
            build_message=lambda chunk: json.dumps(chunk),
            response_format={},
            parse=lambda d: d["items"],
            label="テスト",
            chunk_size=1,
        )
    assert not isinstance(excinfo.value, PartialResultException)


# --- 失敗したチャンクを飛ばして次を試す ------------------------------------------------


def _chunked_client(failing_chunks):
    """`failing_chunks`（0始まりの chunk_index の集合）で空応答を返す client。

    空応答は `chat_json` が `AIServiceException` にする経路のうち**最も起きやすいもの**
    （実測で起きたのはこれ）。渡されたチャンクの中身をそのまま返すので、**どのチャンクが
    投げられたか**を戻り値と呼び出し記録の両方から確認できる。
    """
    seen: list[list[int]] = []

    async def fake_chat(*, model, messages, format, options, think=None):
        chunk = json.loads(messages[1]["content"])
        seen.append(chunk)
        if (len(seen) - 1) in failing_chunks:
            return _resp("")
        return _resp(json.dumps({"items": chunk}))

    client = SimpleNamespace()
    client.chat = AsyncMock(side_effect=fake_chat)
    return client, seen


async def _run_chunked(client, items, *, chunk_size):
    return await chat_json_chunked(
        client,
        "m",
        items,
        system_prompt="s",
        build_message=lambda chunk: json.dumps(chunk),
        response_format={},
        parse=lambda d: d["items"],
        label="テスト",
        chunk_size=chunk_size,
    )


def _assert_accounting_closes(e, total):
    """**3状態の恒等式**。どの状態にも入らない項目が黙って消えないこと。

    件数だけを別々に足していると「失敗を未試行に付け替える」退行が通る。恒等式を
    assert しておくと、**二重計上は合計の側で必ず破れる**。

    **限界**: `_accounting()` は `unattempted` を
    `total - answered - failed` の**差で導出する**ので、恒等式は構成上必ず閉じる。
    したがって**`unattempted` への取りこぼしは原理的に検出できない**——実際に起きた
    取りこぼし（デッドラインで中断された送信済みチャンクが `unattempted` に入っていた）は、
    恒等式ではなく**内訳の意味を測るテスト**（`chunk_failures` の中身）が捕まえる。
    """
    assert e.answered_items + e.failed_items + e.unattempted_items == total
    assert e.details["total_items"] == total
    assert e.details["answered_items"] == e.answered_items
    assert e.details["failed_items"] == e.failed_items
    assert e.details["unattempted_items"] == e.unattempted_items
    # 以前からの呼び出し側との互換（応答が無かった全部）
    assert e.unanswered_items == e.failed_items + e.unattempted_items


def test_the_consecutive_failure_cap_leaves_room_to_skip():
    # ここは「上限が2以上でなければ下のテストが意味を持たない」という前提の検査で、
    # 定数を読んでいるので**値を変えてもここは落ちない**。
    """上限が2以上でなければ「飛ばして続ける」と「打ち切る」が**同じ振る舞いになる**。

    下のテストが上限を使って期待値を組み立てるので、前提を assert として書いておく
    （到達しない分岐との区別は測れない）。
    """
    assert MAX_CONSECUTIVE_CHUNK_FAILURES >= 2


@pytest.mark.asyncio
async def test_a_failed_chunk_does_not_stop_the_following_chunks():
    """**途中の1チャンクが空応答でも、後続のチャンクが試される**。

    途中の1コールが空応答になっただけで、**後続の項目の大半が未試行**になることがある。
    完了済みチャンクは残るので**生成は無駄になっていない**。無駄になるのは時間で、
    1時間を超えるパスで一部の項目しか埋まらない。
    チャンクは互いに独立なので、続ける理由がある。

    チャンクを4つ作る（2つだと「残りを試した」と「最後だったので試すものが無い」が
    区別できない）。
    """
    items = list(range(12))
    client, seen = _chunked_client({1})

    with pytest.raises(PartialResultException) as excinfo:
        await _run_chunked(client, items, chunk_size=3)

    e = excinfo.value
    # **4チャンクすべてが投げられた**（失敗の次が試されている）
    assert seen == [[0, 1, 2], [3, 4, 5], [6, 7, 8], [9, 10, 11]]
    assert client.chat.await_count == 4
    # 失敗したチャンクの項目だけが落ちる
    assert e.partial == [0, 1, 2, 6, 7, 8, 9, 10, 11]
    assert (e.answered_items, e.failed_items, e.unattempted_items) == (9, 3, 0)
    _assert_accounting_closes(e, 12)
    assert e.details["chunk_failures"] == [
        {
            "chunk_index": 1,
            "item_count": 3,
            "reason": "テスト: モデルから空のレスポンスが返されました",
        }
    ]
    assert e.details["aborted_on_consecutive_failures"] is False


@pytest.mark.asyncio
async def test_a_failed_chunk_makes_the_whole_call_partial_even_when_the_loop_finishes():
    """**最後まで回っても、失敗したチャンクがあれば成功として返さない**。

    正常に返すと、呼び出し側は落ちた項目を「試して結果が無かった」と数える。
    「規約文書を引いても意味が決まらなかった項目」へ未試行が混ざる形である。
    """
    items = list(range(12))
    client, seen = _chunked_client({3})  # **最後のチャンク**が失敗する

    with pytest.raises(PartialResultException) as excinfo:
        await _run_chunked(client, items, chunk_size=3)

    assert len(seen) == 4, "最後のチャンクまで投げている"
    e = excinfo.value
    assert e.partial == [0, 1, 2, 3, 4, 5, 6, 7, 8]
    assert (e.answered_items, e.failed_items, e.unattempted_items) == (9, 3, 0)
    _assert_accounting_closes(e, 12)


@pytest.mark.asyncio
async def test_the_consecutive_failure_cap_stops_the_remaining_chunks():
    """**連続失敗の上限で打ち切り、残りを「未試行」として数える**。

    ollama が落ちているときに36コール投げ続けるのは無駄である。打ち切った残りは
    **送っていない**ので、「送ったが返らなかった」項目とは別に数える（押し直せば
    埋まる項目と、同じ入力では埋まらない項目の区別）。
    """
    cap = MAX_CONSECUTIVE_CHUNK_FAILURES
    chunk_size = 3
    # 1チャンク成功 → 上限ぶん連続失敗 → 残りは投げられないこと（残りが2チャンク以上残る）
    total_chunks = 1 + cap + 2
    items = list(range(total_chunks * chunk_size))
    client, seen = _chunked_client(set(range(1, 1 + cap)))

    with pytest.raises(PartialResultException) as excinfo:
        await _run_chunked(client, items, chunk_size=chunk_size)

    e = excinfo.value
    assert len(seen) == 1 + cap, "打ち切ったあとのチャンクは投げていない"
    assert e.partial == [0, 1, 2]
    assert (e.answered_items, e.failed_items) == (3, cap * 3)
    assert e.unattempted_items == 2 * chunk_size
    _assert_accounting_closes(e, len(items))
    assert e.details["aborted_on_consecutive_failures"] is True
    assert e.details["max_consecutive_chunk_failures"] == cap
    assert len(e.details["chunk_failures"]) == cap
    assert "打ち切" in e.message


@pytest.mark.asyncio
async def test_a_success_resets_the_consecutive_failure_count():
    """**成功で連続失敗が 0 に戻る**。

    戻さないと、散発的な失敗が積み上がって**まだ成功しているのに打ち切られる**。
    `失敗→成功` を上限より多く繰り返しても打ち切られないことで測る。
    """
    cap = MAX_CONSECUTIVE_CHUNK_FAILURES
    chunk_size = 2
    total_chunks = 2 * (cap + 1)
    items = list(range(total_chunks * chunk_size))
    # 偶数番のチャンクだけ失敗（失敗と成功が交互）→ 連続失敗は常に1
    client, seen = _chunked_client({i for i in range(total_chunks) if i % 2 == 0})

    with pytest.raises(PartialResultException) as excinfo:
        await _run_chunked(client, items, chunk_size=chunk_size)

    e = excinfo.value
    assert len(seen) == total_chunks, "散発的な失敗では打ち切らない"
    assert e.unattempted_items == 0
    assert e.failed_items == (total_chunks // 2) * chunk_size
    assert e.details["aborted_on_consecutive_failures"] is False
    _assert_accounting_closes(e, len(items))


@pytest.mark.asyncio
async def test_every_chunk_failing_is_a_plain_failure_with_the_breakdown():
    """**1件も結果が無ければ素の失敗**、ただし**内訳は details に残す**。

    部分結果が空なのに `PartialResultException` を投げると、呼び出し側は「途中まで
    成功した」と読む。一方で内訳を捨てると、**未試行が何件あるか**が分からず、押し
    直す判断ができない。
    """
    cap = MAX_CONSECUTIVE_CHUNK_FAILURES
    chunk_size = 2
    total_chunks = cap + 3
    items = list(range(total_chunks * chunk_size))
    client, seen = _chunked_client(set(range(total_chunks)))

    with pytest.raises(AIServiceException) as excinfo:
        await _run_chunked(client, items, chunk_size=chunk_size)

    e = excinfo.value
    assert not isinstance(e, PartialResultException)
    assert len(seen) == cap, "上限で打ち切っている"
    assert e.details["failed_items"] == cap * chunk_size
    assert e.details["unattempted_items"] == (total_chunks - cap) * chunk_size
    assert e.details["answered_items"] == 0
    assert e.details["answered_items"] + e.details["failed_items"] + e.details[
        "unattempted_items"
    ] == len(items)
    assert len(e.details["chunk_failures"]) == cap


@pytest.mark.asyncio
async def test_nothing_is_reported_as_failed_or_unattempted_on_the_normal_path():
    """**正常系で「失敗」「未試行」が発火しない**こと。

    3状態の会計は「正常系検出器」になりやすい。全チャンクが応答したときは**例外を
    作らずに全項目を返す**ことを、正常系そのもので固定する。
    """
    items = list(range(12))
    client, seen = _chunked_client(set())

    got = await _run_chunked(client, items, chunk_size=3)

    assert got == items
    assert len(seen) == 4


@pytest.mark.asyncio
async def test_a_parse_error_is_a_chunk_failure_and_does_not_lose_the_rest():
    """**`parse` が何を上げてもチャンクの失敗として扱う**。

    モデルがトップレベルに配列を返すと呼び出し側の `parse`（`GeneratedPlans(**d)` 等）は
    `TypeError` を上げる。以前はそれが `wait_for` を抜けて**生のまま伝播**し、ルートの
    `except AIServiceException` にも入らないので**完了済みチャンクも来歴も残らず 500**
    になっていた。いまは (a) `chat_json` がトップレベル非 dict を失敗にし、
    (b) ループが `parse` の任意の例外をチャンクの失敗として記録する。
    """
    items = list(range(12))
    seen: list[list[int]] = []

    async def fake_chat(*, model, messages, format, options, think=None):
        chunk = json.loads(messages[1]["content"])
        seen.append(chunk)
        if len(seen) == 2:
            return _resp(json.dumps(chunk))  # **トップレベルが配列**
        return _resp(json.dumps({"items": chunk}))

    client = SimpleNamespace()
    client.chat = AsyncMock(side_effect=fake_chat)

    with pytest.raises(PartialResultException) as excinfo:
        await _run_chunked(client, items, chunk_size=3)

    e = excinfo.value
    assert len(seen) == 4, "解釈に失敗した次のチャンクが投げられていない"
    assert e.partial == [0, 1, 2, 6, 7, 8, 9, 10, 11]
    assert (e.answered_items, e.failed_items, e.unattempted_items) == (9, 3, 0)
    _assert_accounting_closes(e, 12)
    assert "JSON オブジェクト" in e.details["chunk_failures"][0]["reason"]


@pytest.mark.asyncio
async def test_a_parse_callable_that_raises_anything_is_still_a_chunk_failure():
    """**`parse` が `AIServiceException` 以外を上げても抜けない**。

    `chat_json` の dict 検査を通っても、`parse` の中で `KeyError` / `TypeError` が起きる形は
    残る（受け皿のモデルが要求するキーが無い等）。`parse` は呼び出し側が渡す関数なので
    **何を上げるかを列挙できない**——完了済みチャンクを捨てないことを優先して広く受ける。
    """
    items = list(range(9))
    client, seen = _chunked_client(set())

    def parse(d):
        chunk = d["items"]
        if 3 in chunk:
            raise KeyError("欠けたキー")
        return chunk

    with pytest.raises(PartialResultException) as excinfo:
        await chat_json_chunked(
            client,
            "m",
            items,
            system_prompt="s",
            build_message=lambda chunk: json.dumps(chunk),
            response_format={},
            parse=parse,
            label="テスト",
            chunk_size=3,
        )

    e = excinfo.value
    assert len(seen) == 3, "解釈に失敗した次のチャンクが投げられていない"
    assert e.partial == [0, 1, 2, 6, 7, 8]
    assert (e.answered_items, e.failed_items, e.unattempted_items) == (6, 3, 0)
    assert e.details["chunk_failures"][0]["reason"].endswith("（KeyError）")


@pytest.mark.asyncio
async def test_the_first_failures_details_survive_when_every_chunk_fails():
    """**全チャンク失敗の details に最初の失敗の情報が残る**こと。

    パース失敗の `raw_len` のように、**原因を名指しできる情報**は最初の失敗にしか無い
    （以後は同じ理由の繰り返しになる）。`details` の中身を assert していなかったので、
    `first_details` を落としても挙動テストは1件も落ちなかった。
    """
    # **理由の違う失敗を並べる**。同じ理由で3回落とすと first と last が
    # 区別できず、「毎回上書きする（最後が勝つ）」変異が素通りする——実測で生き残った。
    # パース失敗の details は `error` を持ち、空応答の details は `{"model": ...}` だけなので、
    # **`error` の有無で first / last が分かれる**。
    calls = 0

    async def fake_chat(*, model, messages, format, options, think=None):
        nonlocal calls
        calls += 1
        return _resp("これはJSONではない" if calls == 1 else "")

    client = SimpleNamespace()
    client.chat = AsyncMock(side_effect=fake_chat)

    with pytest.raises(AIServiceException) as excinfo:
        await _run_chunked(client, list(range(3)), chunk_size=1)

    e = excinfo.value
    assert not isinstance(e, PartialResultException)
    # **1つ目（パース失敗）の details が残っている**（最後が勝つ実装なら `error` が消える）
    assert "error" in e.details, e.details
    assert "パースに失敗" in e.message, e.message
    assert e.details["chunk_failures"][0]["chunk_index"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failing_chunk", [0, 1])
async def test_a_build_message_failure_keeps_what_was_already_answered(failing_chunk):
    """**送信前（`build_message`）の失敗**の扱い。

    2つの主張を同時に測る。

    1. **`failed`（送った）に数えない**——`build_message` は送信前なので、`client.chat` の
       呼び出し回数が 0 のチャンクを「送ったが返らなかった」に入れてはいけない（固定した
       `failed` の定義に反する）。理由文も**呼び出し側のコードの失敗**だと名指しする。
    2. **完了済みチャンクと来歴を捨てない**——`try` の外へ出しただけだと、2つ目以降のチャンクで
       落ちたときに `wait_for` もルートの `except AIServiceException` も抜けて
       **500 になり部分結果も来歴も消える**（失敗したチャンクを飛ばす処理が直した形の再発）。

    **落ちるチャンクを 0 と 1 の両方で測る**のが要点である。chunk 0 だけを測ると、
    「素のまま伝播させる」実装（1 の主張は満たすが 2 を破る）が通ってしまう
    ——それが以前の修正で実際に起きた。
    """
    client, seen = _chunked_client(set())
    calls: list[int] = []

    def build_message(chunk):
        calls.append(len(calls))
        if len(calls) - 1 == failing_chunk:
            raise AttributeError("呼び出し側のバグ")
        return json.dumps(chunk)

    with pytest.raises(AIServiceException) as excinfo:
        await chat_json_chunked(
            client,
            "m",
            list(range(12)),
            system_prompt="s",
            build_message=build_message,
            response_format={},
            parse=lambda d: d["items"],
            label="テスト",
            chunk_size=3,
        )

    e = excinfo.value
    # **モデルへは失敗したチャンクの分だけ送っていない**
    assert len(seen) == failing_chunk, seen
    # **送っていないので `failed` に数えない**（未試行に落ちる）
    assert e.details["failed_items"] == 0, e.details
    assert e.details["answered_items"] == 3 * failing_chunk
    assert e.details["unattempted_items"] == 12 - 3 * failing_chunk
    reason = e.details["chunk_failures"][-1]["reason"]
    assert "呼び出し側のコード" in reason, reason
    assert "レスポンス" not in reason, "モデルのせいだと読める文になっている"

    if failing_chunk == 0:
        # 1件も結果が無いので素の失敗（部分結果があると読ませない）
        assert not isinstance(e, PartialResultException)
    else:
        # **完了済みチャンクを捨てない**（ルートの `except AIServiceException` に入る形で上げる）
        assert isinstance(e, PartialResultException)
        assert e.partial == [0, 1, 2]


@pytest.mark.asyncio
async def test_every_failure_path_reports_whether_it_aborted(monkeypatch):
    """**4つの raise 箇所すべてに、打ち切りを読むためのキーがある**こと。

    `details["aborted_on_consecutive_failures"]` を読む呼び出し側は、キーの無い経路で
    `KeyError` になる。**全チャンク失敗は連続失敗の上限が最も効く経路**（ollama が落ちている）
    なのに、そこだけ打ち切りの記録が無かった——打ち切りを記録する要件はこの経路でこそ要る。
    以前の修正は2箇所しか見ておらず、**それを測るテストも2箇所しか通していなかった**
    （デッドライン側に届いていなかった）。

    **主張は「必要なキーが4経路すべてにある」**である——**キー集合は揃っていない**
    （`timeout` はデッドライン側だけ、`error` / `raw_len` は全失敗側だけ）。以前の docstring は
    「キー集合が揃っている」と書いていたが、それは測っていないし成り立たない。

    デッドライン側の `aborted` は**構造上つねに False** である（`aborted = True` の直後に
    `_run()` が return するので `wait_for` は正常完了する）。キーを置くのは KeyError 対策で、
    「デッドライン中に打ち切りも起きた」ケースは存在しない。
    """
    cap = MAX_CONSECUTIVE_CHUNK_FAILURES
    required = {
        "aborted_on_consecutive_failures",
        "max_consecutive_chunk_failures",
        "chunk_failures",
        "total_items",
        "answered_items",
        "failed_items",
        "unattempted_items",
        "unanswered_items",
        "model",
    }

    async def collect(failing, *, items, chunk_size):
        client, _seen = _chunked_client(failing)
        with pytest.raises(AIServiceException) as excinfo:
            await _run_chunked(client, items, chunk_size=chunk_size)
        return excinfo.value

    # 経路1: ループ完走・一部失敗（部分結果あり）
    e1 = await collect({1}, items=list(range(12)), chunk_size=3)
    # 経路2: 連続失敗の上限で打ち切り（部分結果あり）
    e2 = await collect(set(range(1, 1 + cap)), items=list(range((1 + cap + 2) * 3)), chunk_size=3)
    # 経路3: 全チャンク失敗（**素の失敗**。打ち切りも起きている）
    e3 = await collect(set(range(cap + 3)), items=list(range((cap + 3) * 3)), chunk_size=3)

    # 経路4: **全体デッドライン**（以前のテストはここへ届いていなかった）
    async def hang(*, model, messages, format, options, think=None):
        chunk = json.loads(messages[1]["content"])
        if chunk[0] >= 3:
            await asyncio.sleep(5)
        return _resp(json.dumps({"items": chunk}))

    deadline_client = SimpleNamespace()
    deadline_client.chat = AsyncMock(side_effect=hang)
    monkeypatch.setattr(settings, "llm_total_timeout", 1)
    with pytest.raises(PartialResultException) as excinfo:
        await _run_chunked(deadline_client, list(range(12)), chunk_size=3)
    e4 = excinfo.value

    for label, e, aborted in (
        ("一部失敗", e1, False),
        ("打ち切り", e2, True),
        ("全失敗", e3, True),
        ("デッドライン", e4, False),
    ):
        assert required <= set(e.details), f"{label}: 足りないキー {required - set(e.details)}"
        assert e.details["aborted_on_consecutive_failures"] is aborted, label
        assert e.details["max_consecutive_chunk_failures"] == cap, label
    assert isinstance(e3, AIServiceException) and not isinstance(e3, PartialResultException)
    assert e4.details["timeout"] == 1, "デッドライン側だけが持つキー（集合は揃っていない）"


@pytest.mark.asyncio
async def test_a_cancelled_call_is_recorded_in_the_provenance(monkeypatch):
    """**全体デッドラインで中断された呼び出しも来歴に1件残る**こと。

    `CancelledError` は `BaseException` なので `except Exception` に入らない。会計では
    `failed`（送った）に数えるのに、**来歴には証拠が無い**状態だった。
    """
    got: list = []

    async def fake_chat(*, model, messages, format, options, think=None):
        await asyncio.sleep(5)
        return _resp("{}")

    client = SimpleNamespace()
    client.chat = AsyncMock(side_effect=fake_chat)
    monkeypatch.setattr(settings, "llm_total_timeout", 1)

    with pytest.raises(AIServiceException):
        await chat_json_chunked(
            client,
            "m",
            list(range(6)),
            system_prompt="s",
            build_message=lambda chunk: json.dumps(chunk),
            response_format={},
            parse=lambda d: d["items"],
            label="テスト",
            chunk_size=3,
            recorder=got.append,
        )

    assert len(got) == 1, "中断された呼び出しの来歴が残っていない"
    assert got[0].raw_output is None
    assert "中断" in (got[0].error or ""), got[0].error


@pytest.mark.asyncio
async def test_the_abort_and_the_skip_are_logged(caplog):
    """**スキップと打ち切りがログに残る**こと。

    `details` は測れていたが**ログは無検証**で、`logger` の行を消しても挙動テストは1件も
    落ちなかった（所在側のログが無テストで `if True:` が素通りしたのと同じ型）。運用でまず読まれるのはログである。
    """
    cap = MAX_CONSECUTIVE_CHUNK_FAILURES
    chunk_size = 3
    items = list(range((1 + cap + 2) * chunk_size))
    client, _seen = _chunked_client(set(range(1, 1 + cap)))

    with caplog.at_level("WARNING"):
        with pytest.raises(PartialResultException):
            await _run_chunked(client, items, chunk_size=chunk_size)

    messages = [r.message for r in caplog.records]
    skips = [m for m in messages if "飛ばして次を試す" in m]
    assert len(skips) == cap, messages
    assert "連続失敗 1回" in skips[0] and f"連続失敗 {cap}回" in skips[-1]
    assert any(f"連続{cap}回失敗したので打ち切った" in m for m in messages), messages


@pytest.mark.asyncio
async def test_the_deadline_separates_failed_chunks_from_unattempted_ones(monkeypatch):
    """全体デッドラインでも**失敗したチャンクと未試行が別に数えられる**。

    以前は「応答なし」の1つの数しか持っていなかったので、打ち切りの残りと空応答の
    区別が付かなかった。デッドラインを跨いでも恒等式が閉じることを測る。
    """
    items = list(range(12))
    calls = 0

    async def fake_chat(*, model, messages, format, options, think=None):
        nonlocal calls
        calls += 1
        if calls == 2:
            return _resp("")  # 2チャンク目は空応答（失敗）
        if calls == 3:
            await asyncio.sleep(5)  # 3チャンク目でデッドラインを超える
        chunk = json.loads(messages[1]["content"])
        return _resp(json.dumps({"items": chunk}))

    client = SimpleNamespace()
    client.chat = AsyncMock(side_effect=fake_chat)
    monkeypatch.setattr(settings, "llm_total_timeout", 1)

    with pytest.raises(PartialResultException) as excinfo:
        await _run_chunked(client, items, chunk_size=3)

    e = excinfo.value
    assert e.partial == [0, 1, 2]
    # **2チャンク目（空応答）と3チャンク目（送信済みで中断）はどちらも `failed`**
    # （中断されたチャンクは生成時間を消費して応答が返らなかったので、
    # 「送っていない」ではない。差で導出していたので**送信済みが未試行に入っていた**）
    assert (e.answered_items, e.failed_items) == (3, 6)
    # 4チャンク目（投げていない）だけが未試行
    assert e.unattempted_items == 3
    _assert_accounting_closes(e, 12)
    reasons = [f["reason"] for f in e.details["chunk_failures"]]
    assert len(reasons) == 2, reasons
    assert "中断" in reasons[1], "中断されたチャンクが失敗として記録されていない"
    assert [f["chunk_index"] for f in e.details["chunk_failures"]] == [1, 2]


def test_strict_response_format_requires_the_named_keys():
    """既定値つきのフィールドを **structured output では必須にする**こと。

    `model_json_schema()` は既定値を持つフィールドを `required` に入れないので、Ollama の
    structured output は**モデルにキーの省略を許す**。実測では、モデルが正しい記述を書きながら
    `chosen_cand_id` だけを省略し、`clinical_drafter` が「典拠を選べなかったら記述も採らない」
    という方針で**その記述を捨てていた**。典拠の付与率はそのぶん低く出る。
    """
    plain = LLMClinicalDrafts.model_json_schema()
    assert plain["$defs"]["LLMClinicalDraft"]["required"] == ["link_id"], (
        "前提: 既定値つきのフィールドは required に入らない"
    )

    strict = strict_response_format(LLMClinicalDrafts, "chosen_cand_id", "annotation")
    assert strict["$defs"]["LLMClinicalDraft"]["required"] == [
        "link_id",
        "chosen_cand_id",
        "annotation",
    ]
    # **受けは緩いまま**（モデルがキーを省略しても ValidationError にしない。省略で
    # チャンクごと失敗すると、完了済みチャンクを捨てない部分結果の話に戻る）
    draft = LLMClinicalDraft(link_id="x")
    assert draft.chosen_cand_id is None
    # **`annotation` も必須で要求しているので、受け側の緩さは両方について測る**
    assert draft.annotation is None


def test_location_choice_stays_loose_on_the_receiving_side():
    """所在側の受け皿も**緩いまま**であること。

    臨床側だけを assert していると、`LLMLocationChoice` を必須化するミューテーションが
    **上位層（structured output が required にした応答をフィクスチャが必ず返す）に助けられて**
    生き残る（`source_category` の必須化は実測で生き残った）。
    **層ごとに測る。**
    """
    choice = LLMLocationChoice(link_id="x")
    assert choice.chosen_cand_id is None
    assert choice.source_category == "manual"


def test_strict_response_format_does_not_mutate_the_models_schema():
    """`model_json_schema()` の戻り値を**書き換えない**こと。

    pydantic 2.12 は呼ぶたびに新しい dict を返すので、コピーを外しても
    `Model.model_json_schema()` を読み直す assert は**原理的に落ちない**（変異が実測で生き残った）。
    **実装依存に賭けない**ことを測るために、`model_json_schema` が**共有の dict を返す**
    受け皿を渡して、その dict が書き換わらないことを見る（将来 pydantic がキャッシュを
    返す実装になれば、`required` の追記が他の呼び出しへ漏れる）。
    """
    shared: dict[str, object] = {
        "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
        "required": ["a"],
    }

    class _Cached:
        @staticmethod
        def model_json_schema() -> dict[str, object]:
            return shared

    strict = strict_response_format(_Cached, "b")  # type: ignore[arg-type]
    assert strict["required"] == ["a", "b"]
    assert shared["required"] == ["a"], "モデル側のスキーマを書き換えている"


def test_strict_response_format_ignores_unknown_keys():
    """指定した名前がそのスキーマに無ければ何もしないこと。

    呼び出し側の綴り誤りで**黙って必須化されない**のを防ぐわけではないが、別のスキーマへ
    同じ関数を使い回したときに壊れない（`required` に存在しないキーを入れると Ollama が
    スキーマを拒否する）。
    """
    strict = strict_response_format(LLMClinicalDrafts, "no_such_field")
    assert strict["$defs"]["LLMClinicalDraft"]["required"] == ["link_id"]
    # **トップレベルにも混ぜない**（`$defs` 側だけを見ると、片脚が測れない）
    assert "no_such_field" not in strict.get("required", [])


def test_strict_response_format_covers_the_top_level_too():
    """トップレベルのフィールドも必須にできること（入れ子でない受け皿のため）。"""

    class _Flat(BaseModel):
        a: str
        b: int | None = None

    assert _Flat.model_json_schema()["required"] == ["a"]
    strict = strict_response_format(_Flat, "b")
    assert strict["required"] == ["a", "b"]


def test_strict_response_format_refuses_an_ambiguous_field_name():
    """同名フィールドが**2つ以上の定義**にあるときは落ちること。

    名前だけではどの定義を厳しくしたいのかが決まらない。**広い方に倒すと**、受け皿へ
    モデルを1つ足した瞬間に無関係な同名フィールドまで黙って必須化される（実測で再現した）。
    **黙って広げるより落とす**（呼び出し側の3経路はテストが必ず通るので、CI で気づく）。
    """

    class _Choice(BaseModel):
        link_id: str
        chosen_cand_id: int | None = None

    class _Note(BaseModel):
        # 別の意味だが**同名**のフィールド
        chosen_cand_id: int | None = None

    class _Wrap(BaseModel):
        choices: list[_Choice]
        notes: list[_Note]

    with pytest.raises(ValueError, match="chosen_cand_id"):
        strict_response_format(_Wrap, "chosen_cand_id")

    # **単独なら通る**（正常系も測る。落ちる側だけ測ると「常に落ちる」実装が通る）
    assert strict_response_format(_Choice, "chosen_cand_id")["required"] == [
        "link_id",
        "chosen_cand_id",
    ]


# --------------------------------------------------------------------------- #
# 重複エントリの採用規則
# --------------------------------------------------------------------------- #


def _entry(link_id: str, cand: int | None) -> SimpleNamespace:
    return SimpleNamespace(link_id=link_id, chosen_cand_id=cand)


def test_merge_takes_the_first_entry_and_records_the_duplicate():
    """**同じ項目に2エントリ返る応答で、後勝ちで有効な値を捨てない**こと。

    実際の生成で起きた形: 1エントリ目が `cand_id=61` と記述あり、2エントリ目が両方 null。
    dict 内包は後勝ちなので、**有効な下書きが黙って捨てられ**、その項目が
    「規約文書を引いても意味が決まらなかった項目」へ誤って入っていた。
    """
    merged = merge_by_link_id(
        [_entry("a", 61), _entry("a", None), _entry("b", 3)],
        value=lambda e: e.chosen_cand_id,
    )
    assert merged.values == {"a": 61, "b": 3}, "後勝ちになっている"
    assert merged.duplicated_link_ids == ("a",)


def test_merge_records_each_duplicated_item_once():
    """3回以上現れても項目は1回だけ数えること（件数が水増しされない）。"""
    merged = merge_by_link_id(
        [_entry("a", 1), _entry("a", 2), _entry("a", None), _entry("b", 0)],
        value=lambda e: e.chosen_cand_id,
    )
    assert merged.values == {"a": 1, "b": 0}
    assert merged.duplicated_link_ids == ("a",)


def test_merge_does_not_flag_single_entries():
    """重複していない応答を「矛盾した応答」と数えないこと（**正常系で発火しない**）。

    ここが常に立つと、矛盾していない項目まで分子から外れて**典拠の付与率を過小に見せる**。
    """
    merged = merge_by_link_id([_entry("a", 1), _entry("b", None)], value=lambda e: e.chosen_cand_id)
    assert merged.duplicated_link_ids == ()


def test_merge_ignores_entries_without_a_link_id():
    """`link_id` を持たない要素を無視すること（モデルの崩れた応答で落ちない）。"""
    merged = merge_by_link_id(
        [SimpleNamespace(chosen_cand_id=1), _entry("a", 2)], value=lambda e: e.chosen_cand_id
    )
    assert merged.values == {"a": 2}
    assert merged.duplicated_link_ids == ()


# --------------------------------------------------------------------------- #
# thinking の固定と、所要時間・接続先の記録
# --------------------------------------------------------------------------- #


def _resp_full(content, *, thinking=None, **timings):
    """thinking とサーバ側の計測値を持つ応答を模す。"""
    return SimpleNamespace(message=SimpleNamespace(content=content, thinking=thinking), **timings)


@pytest.mark.asyncio
async def test_think_is_passed_explicitly_and_not_left_to_the_model_default():
    """**`think` を渡していることを振る舞いで固定する**。

    渡さないとモデルのテンプレートの既定に従うので、Ollama やモデルの更新で
    thinking が出たり出なかったりし、**測定が黙って動く**。`num_ctx` や温度と同じ基準で
    固定する値なので、「渡している」こと自体が退行検知の対象である。

    ソース文字列の検査では足りない——実測（`inspect.getsource` を見る検査は
    呼び出しが到達不能になっても緑のまま）と同じ理由で、実際に client へ届くことを見る。
    """
    client = _client(return_value=_resp_full('{"a": 1}'))
    await chat_json(
        client,
        "m",
        system_prompt="s",
        user_message="u",
        response_format={"type": "object"},
        label="L",
    )
    assert client.chat.await_args.kwargs["think"] is DEFAULT_THINK


@pytest.mark.asyncio
async def test_the_thinking_text_is_kept_in_the_record():
    """thinking の**中身**を来歴へ残す。

    thinking は `content` と別のフィールドで返るので JSON は汚れないが、時間と
    `num_ctx` を消費し出力に影響する。捨てると「なぜこの答えになったか」を後から
    追えない。`think` も一緒に残す——`think=True` でもモデルが何も考えないことは
    ありうるので、**`thinking` が空であることだけでは「切っていた」と区別できない**。
    """
    records = []
    client = _client(return_value=_resp_full('{"a": 1}', thinking="まず定義を確認する"))
    await chat_json(
        client,
        "m",
        system_prompt="s",
        user_message="u",
        response_format={"type": "object"},
        label="L",
        recorder=records.append,
    )
    assert records[0].thinking == "まず定義を確認する"
    assert records[0].think is DEFAULT_THINK


@pytest.mark.asyncio
async def test_the_elapsed_time_and_server_breakdown_are_recorded():
    """処理時間の評価の出所を作る。

    行は生成後に1度だけ作られるので `created_at`/`updated_at` の差からは出せない。
    **生成の瞬間にしか捕まえられない。**

    サーバ側の内訳もそのまま残す。**`load_duration` が別に取れるのが要点**で、
    初回コールのモデルロードを差し引けないと、複数のモデルを逐次で回したとき1本目だけ
    遅く見える（所要時間をモデルどうしで比べるとき、そこにロード時間が混ざると
    モデルの速度ではなく実行順を測ることになる）。
    """
    records = []
    client = _client(
        return_value=_resp_full(
            '{"a": 1}',
            total_duration=9_000_000_000,
            load_duration=4_000_000_000,
            eval_count=120,
            eval_duration=5_000_000_000,
            prompt_eval_count=700,
            prompt_eval_duration=1_000_000_000,
        )
    )
    await chat_json(
        client,
        "m",
        system_prompt="s",
        user_message="u",
        response_format={"type": "object"},
        label="L",
        recorder=records.append,
    )
    timing = records[0].timing
    assert timing["elapsed_ms"] >= 0
    assert timing["total_duration"] == 9_000_000_000
    assert timing["load_duration"] == 4_000_000_000
    assert timing["eval_count"] == 120


@pytest.mark.asyncio
async def test_the_elapsed_time_is_recorded_even_when_the_call_fails():
    """**失敗したコールでも「何秒待ったか」を残す**。

    `raw_output` を失敗時も記録するのと同じ理由である。応答が返らなかった事実と、
    そこまでに使った時間は別の情報で、どちらも記録の一部になる。サーバ側の内訳は
    応答が無いので入らない。
    """
    records = []
    client = _client(side_effect=RuntimeError("boom"))
    with pytest.raises(AIServiceException):
        await chat_json(
            client,
            "m",
            system_prompt="s",
            user_message="u",
            response_format={"type": "object"},
            label="L",
            recorder=records.append,
        )
    assert records[0].error
    assert "elapsed_ms" in records[0].timing
    assert "total_duration" not in records[0].timing


@pytest.mark.asyncio
async def test_the_endpoint_is_recorded_so_times_can_be_compared():
    """どこへ投げたかを残す。

    所要時間は**マシン依存**なので、時間だけでは比較できない。「Mac の実測は
    1.7〜2.7時間だったが、**別マシン・別モデルなので当てはまらない**」という引き継ぎが
    あったが、その 1.7〜2.7 時間がどのマシンの値かは来歴のどこにも無かった。
    """
    records = []
    client = _client(return_value=_resp_full('{"a": 1}'))
    client.base_url = "http://llm-host:11434"
    await chat_json(
        client,
        "m",
        system_prompt="s",
        user_message="u",
        response_format={"type": "object"},
        label="L",
        recorder=records.append,
    )
    assert records[0].endpoint == "http://llm-host:11434"


@pytest.mark.asyncio
async def test_an_unknown_endpoint_does_not_break_the_call():
    """接続先が取れなくても生成は続く。

    `llm_provider.provider_of` の docstring にある実測と同じ理由で、**来歴の検証が
    生成を壊してはならない**。base URL を公開していない client では
    `endpoint` が null になるだけである。
    """
    records = []
    client = _client(return_value=_resp_full('{"a": 1}'))
    out = await chat_json(
        client,
        "m",
        system_prompt="s",
        user_message="u",
        response_format={"type": "object"},
        label="L",
        recorder=records.append,
    )
    assert out == {"a": 1}
    assert records[0].endpoint is None


# --- モデルごとの options の例外 -----------------------------------------------------
#
# 一部のモデルは長い入力で推論サーバが落ちることがあり、`num_batch` を下げると回避できる。
# 例外は施設の設定（`model_options.yaml`）に置くので、架空のモデル名で1件載せたファイルを
# 作って検査する。読み込みの検査は `test_model_options.py`。

_OVERRIDDEN_MODEL = "example-model:35b"
_OTHER_MODELS = ("example-model:9b", "example-model:12b", "example-model:27b")


@pytest.fixture
def overrides(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    path = tmp_path / "model_options.yaml"
    path.write_text(
        yaml.safe_dump({"models": {_OVERRIDDEN_MODEL: {"num_batch": 512}}}), encoding="utf-8"
    )
    monkeypatch.setattr(settings, "model_options_file", str(path))


def test_a_model_without_an_override_is_untouched(overrides: None):
    """**載っていないモデルの options は1バイトも変えない。**

    ここが緩むと、1つのモデルのために足した例外が他のモデルの条件を黙って動かす。
    """
    for model in _OTHER_MODELS:
        assert with_model_overrides(model, LARGE_CONTEXT_OPTIONS) == LARGE_CONTEXT_OPTIONS
        assert with_model_overrides(model, DEFAULT_OPTIONS) == DEFAULT_OPTIONS


def test_the_override_adds_only_the_batch_width(overrides: None):
    got = with_model_overrides(_OVERRIDDEN_MODEL, LARGE_CONTEXT_OPTIONS)
    assert got == {**LARGE_CONTEXT_OPTIONS, "num_batch": 512}
    assert {k: got[k] for k in LARGE_CONTEXT_OPTIONS} == LARGE_CONTEXT_OPTIONS, (
        "固定した temperature / num_ctx / seed は動かさない"
    )


def test_the_override_never_wins_over_the_frozen_options(overrides: None):
    """**例外表が固定した値を上書きできないこと。**

    `num_ctx` を例外表に書けば、そのモデルだけ文脈長が変わったまま動くことになる。
    渡された options を後に重ねて、その形を構造的に作れなくする。
    """
    overridden = {**LARGE_CONTEXT_OPTIONS, "num_batch": 4096}
    assert with_model_overrides(_OVERRIDDEN_MODEL, overridden)["num_batch"] == 4096


@pytest.mark.asyncio
async def test_the_effective_options_reach_both_the_client_and_the_provenance(overrides: None):
    """結線。例外が**実際の要求**と**来歴**の両方に出ること。

    来歴に出ないと、あとから「どの条件で動かしたのか」を記録から言えない。
    """
    records = []
    client = _client(return_value=_resp(json.dumps({"a": 1})))
    await chat_json(
        client,
        _OVERRIDDEN_MODEL,
        system_prompt="s",
        user_message="u",
        response_format={},
        label="テスト",
        options=LARGE_CONTEXT_OPTIONS,
        recorder=records.append,
    )
    assert client.chat.await_args.kwargs["options"]["num_batch"] == 512
    assert records[0].options["num_batch"] == 512


@pytest.mark.asyncio
async def test_the_other_models_keep_the_frozen_options(overrides: None):
    """結線の裏。載っていないモデルの要求は例外表が無いときと同じであること。"""
    client = _client(return_value=_resp(json.dumps({"a": 1})))
    for model in _OTHER_MODELS:
        await chat_json(
            client,
            model,
            system_prompt="s",
            user_message="u",
            response_format={},
            label="テスト",
            options=LARGE_CONTEXT_OPTIONS,
        )
        assert client.chat.await_args.kwargs["options"] == LARGE_CONTEXT_OPTIONS


def test_without_the_site_file_no_model_is_overridden():
    """**設定ファイルが無ければ何も足さない。** 載せるのは、その環境で落ちることを確かめたモデルだけ。"""
    assert with_model_overrides(_OVERRIDDEN_MODEL, LARGE_CONTEXT_OPTIONS) == LARGE_CONTEXT_OPTIONS
