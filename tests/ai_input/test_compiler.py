"""ステップ3 DSLコンパイル（AIPlanCompiler）のユニットテスト。

Ollama をモックし、共通ヘルパー経由の chunk 分割・結果統合を検証する。
多項目を1コールに詰めると num_ctx 超過で空応答になる不具合の再発防止を兼ねる。
"""

from __future__ import annotations

import inspect
import json
import re
import types

import pytest

from kawasemi.ai_input import compiler as compiler_module
from kawasemi.ai_input.compiler import (
    _MAX_ALT_CANDIDATES,
    COMPILE_CHUNK_SIZE,
    COMPILE_SYSTEM_PROMPT,
    AIPlanCompiler,
)
from kawasemi.ai_input.llm_json import (
    DEFAULT_CHUNK_SIZE,
    DEFAULT_OPTIONS,
    LARGE_CONTEXT_OPTIONS,
)
from kawasemi.ai_input.schemas import GeneratedPlans
from kawasemi.core.exceptions import AIServiceException


def _resp(content):
    return types.SimpleNamespace(message=types.SimpleNamespace(content=content))


def _plans_resp_from_message(user_message: str):
    """user メッセージに含まれる linkId ごとに DSLプログラム1件を返す応答を作る"""
    link_ids = re.findall(r"### linkId: (\S+)", user_message)
    payload = {
        "plans": [{"link_id": lid, "program": {"steps": [], "return": None}} for lid in link_ids]
    }
    return _resp(json.dumps(payload))


def _patch_client(compiler: AIPlanCompiler, calls: list, response_fn=None):
    async def fake_chat(**kwargs):
        calls.append(kwargs)
        if response_fn is not None:
            return response_fn(kwargs["messages"][1]["content"])
        return _plans_resp_from_message(kwargs["messages"][1]["content"])

    compiler._client = types.SimpleNamespace(chat=fake_chat)


def _items(n: int):
    return [
        {
            "link_id": f"q{i}",
            "item_text": f"項目{i}",
            "source_category": "manual",
            "annotation": None,
            "location": None,
            "steps": [],
        }
        for i in range(n)
    ]


@pytest.mark.asyncio
async def test_compile_chunks_and_merges():
    """**5項目・コンパイラ幅2 → `[2, 2, 1]`**（コール数だけでは幅を特定できない）。結果は5件"""
    compiler = AIPlanCompiler()
    calls: list = []
    _patch_client(compiler, calls)

    result = await compiler.compile(_items(5))

    # **項目数と期待値を逐語で書く**。コール数だけの assert は刻み幅を
    # 特定できない（`ceil(5/2) == ceil(5/3) == 3`）ので、**各コールの項目数**を見る。
    # 幅2 は `[2, 2, 1]`、幅3 は `[3, 2]` で分布が違う。
    #
    # コンパイラは共有の `DEFAULT_CHUNK_SIZE` ではなく自前の `COMPILE_CHUNK_SIZE` を使う
    # （固定部 15,393 文字＋1項目 3,058 文字で、幅4 は 32768 にも収まらない）。
    assert COMPILE_CHUNK_SIZE == 2, "前提: コンパイラの幅は2（変えたらこの期待値も変える）"
    counts = [c["messages"][1]["content"].count("### linkId:") for c in calls]
    assert counts == [2, 2, 1], counts
    assert len(result) == 5
    assert {p.link_id for p in result} == {f"q{i}" for i in range(5)}


@pytest.mark.asyncio
async def test_compile_passes_num_ctx():
    """コンパイラは**大きい方の num_ctx**（`LARGE_CONTEXT_OPTIONS`）で呼ぶ。

    既定の 8192 では**固定部（system 8,858 + schema 6,535）だけで超える**ので、項目0件でも
    収まらない。ここは「値が何であるか」ではなく**共有の既定ではなく大きい方を渡している**
    ことを見る（予算そのものは `test_compile_prompt_fits_the_num_ctx_budget` が測る）。
    """
    compiler = AIPlanCompiler()
    calls: list = []
    _patch_client(compiler, calls)

    await compiler.compile(_items(1))

    assert calls[0]["options"]["num_ctx"] == LARGE_CONTEXT_OPTIONS["num_ctx"]
    assert calls[0]["options"]["num_ctx"] > int(DEFAULT_OPTIONS["num_ctx"])


@pytest.mark.asyncio
async def test_compile_empty_items_no_call():
    compiler = AIPlanCompiler()
    calls: list = []
    _patch_client(compiler, calls)

    result = await compiler.compile([])

    assert result == []
    assert calls == []


@pytest.mark.asyncio
async def test_compile_keeps_the_plans_of_the_chunks_that_answered():
    """**DSL コンパイルも部分結果を扱う**。

    12項目・コンパイラ幅2 で6チャンク。2番目だけ空応答にすると、以前は
    `chat_json_chunked` の失敗がそのまま上がり、**1・3番目のプランも捨てて 500 を返して
    いた**。この経路の「未試行」は**プランを生成していない**（再実行すれば対象に入る）で、
    「生成したが op 引数の是正で落ちた」とは別である。

    件数ではなく**返ってきた link_id**で表す（押し直す対象が分かる形にする。ルートは
    渡した link_id との差を未試行として出す）。
    """
    compiler = AIPlanCompiler()
    calls: list = []

    def response_fn(user_message: str):
        if len(calls) == 2:  # 2番目のチャンク（calls は呼び出し後に積まれている）
            return _resp("")
        return _plans_resp_from_message(user_message)

    _patch_client(compiler, calls, response_fn=response_fn)

    result = await compiler.compile(_items(12))

    counts = [c["messages"][1]["content"].count("### linkId:") for c in calls]
    assert counts == [2] * 6, "失敗の次のチャンクが投げられていない"
    assert {p.link_id for p in result} == {f"q{i}" for i in range(12)} - {"q2", "q3"}


@pytest.mark.asyncio
async def test_compile_empty_content_raises():
    compiler = AIPlanCompiler()
    _patch_client(compiler, [], response_fn=lambda _msg: _resp(""))

    with pytest.raises(AIServiceException, match="空のレスポンス"):
        await compiler.compile(_items(1))


# --- プロンプト予算 -------------------------------------------------

# 出力ぶんの予約（1項目あたり）。**典型値ではなく長いプラン側に合わせる**——
# 分岐を含むプランは長く、そこが溢れると「分岐のある項目だけ壊れる」という
# 最も追いにくい形で出るので、**長いプラン側に合わせて 3,000 を予約する**。
_OUTPUT_RESERVE_CHARS = 3000


def _budget_items(chunk_size: int) -> list[dict]:
    """1コールに詰める最大構成の項目（所在候補3件・payload 満載・手順書5ステップ）。"""
    notes = "この列は 2024-04 以降のみ入る。それ以前は別表 LEGACY_LAB を見る必要がある。" * 3
    items = []
    for i in range(chunk_size):
        candidates = [
            {
                "location": f"DWH: DEMO{j}.PATIENT_INFOMATION.BLAST_PCT（10桁ゼロ詰めの MRN で解決）",
                "unit_id": f"db:DEMO{j}.PATIENT_INFOMATION_LONG_TABLE_NAME.BLAST_PCT_COLUMN_{j}",
                "connection_id": "dwh_oracle",
                "payload": {
                    "connection_id": "dwh_oracle",
                    "db_qualified": f"DEMO{j}.PATIENT_INFOMATION_LONG_TABLE_NAME",
                    "table_logical": "患者基本情報テーブル（がん登録用）",
                    "physical_name": f"BLAST_PCT_COLUMN_{j}",
                    "logical_name": "末梢血白血球中の芽球割合（％）",
                    "type_raw": "NUMBER(5,2)",
                    "patient_key": "PATIENT_ID",
                    "key_type": "mrn8",
                    "notes": notes,
                },
            }
            # 提示されるのは主候補＋`_MAX_ALT_CANDIDATES` 件。それが最大構成である。
            for j in range(1 + _MAX_ALT_CANDIDATES)
        ]
        items.append(
            {
                "link_id": f"item-{i:04d}",
                "item_text": "項目名" * 10,
                "source_category": "external_db",
                "annotation": "この項目の臨床的な意味と分岐の根拠。" * 12,
                "candidates": candidates,
                "steps": [
                    {
                        "order": k,
                        "action_type": "db_query",
                        "title": "DWH から芽球割合を取得する",
                        "description": (
                            "患者IDを10桁ゼロ詰めに変換し、DEMO.LAB から採取日が"
                            "最も新しい行の BLAST_PCT を1件取得する。" * 2
                        ),
                        "data_hint": "採取日は SAMPLING_DATE。複数行あるときは降順で先頭。",
                    }
                    for k in range(1, 6)
                ],
            }
        )
    return items


def _budget_chars(chunk_size: int) -> int:
    """`chunk_size` 項目を1コールに詰めたときの文字数見積もり（出力予約を含む）。"""
    compiler = AIPlanCompiler()
    user_msg = compiler._build_user_message(_budget_items(chunk_size))
    schema = json.dumps(GeneratedPlans.model_json_schema(by_alias=True), ensure_ascii=False)
    return (
        len(COMPILE_SYSTEM_PROMPT)
        + len(schema)
        + len(user_msg)
        + _OUTPUT_RESERVE_CHARS * chunk_size
    )


def test_compile_prompt_fits_the_num_ctx_budget() -> None:
    """**実際に組み立てたプロンプト**が num_ctx の予算に収まること。

    溢れると **プロンプトの末尾から落ちる**。コンパイラでは末尾がチャンク後半の項目の
    所在情報なので、症状は「特定の項目だけ needs_input や捏造 SQL が増える」という形で出て
    原因を追いにくい。しかもコンパイラには**実 Ollama を使うテストが1本も無い**ので、
    溢れが起きても実行では気づけない。

    **単位は文字であってトークンではない。** 1文字＝1トークンは上界ではなく（BPE の byte
    fallback で語彙外の漢字は UTF-8 の3バイト＝3トークンに分解されうる）、これは「絶対に
    溢れない」証明ではなく、**固定部・刻み幅・候補件数を増やしたときに気づくための
    回帰テスト**である。会計は `clinical_drafter` の予算テストと揃えてある。

    見積もると、**固定部（system 8,858 + schema 6,535）だけで 15,393 文字**あり、
    既定の `num_ctx=8192` は**項目0件でも超えていた**。刻み幅を下げても収まらないので、
    この経路は `LARGE_CONTEXT_OPTIONS`（32768）で呼ぶ。
    """
    total = _budget_chars(COMPILE_CHUNK_SIZE)
    num_ctx = int(LARGE_CONTEXT_OPTIONS["num_ctx"])
    assert total <= num_ctx, {"total": total, "num_ctx": num_ctx}


def test_the_compile_chunk_size_is_the_largest_that_fits() -> None:
    """刻み幅が**予算に収まる最大値**であること（下限も縛る）。

    上の予算テストは上限しか見ないので、刻み幅を1に下げても通る。それは N 項目の一括生成が
    N コールになる退行で、LLM 呼び出しと来歴行が倍増する。「収まる最大値」として固定すれば、
    上げる退行（予算超過）と下げる退行（無駄なコール）の両方が見える。
    """
    num_ctx = int(LARGE_CONTEXT_OPTIONS["num_ctx"])
    assert _budget_chars(COMPILE_CHUNK_SIZE) <= num_ctx
    assert _budget_chars(COMPILE_CHUNK_SIZE + 1) > num_ctx, {
        "chunk": COMPILE_CHUNK_SIZE + 1,
        "total": _budget_chars(COMPILE_CHUNK_SIZE + 1),
        "num_ctx": num_ctx,
    }


def test_the_compiler_does_not_use_the_shared_default_chunk_size() -> None:
    """コンパイラが**自前の刻み幅と num_ctx で呼ぶ**こと（配線の固定）。

    定数だけを直しても、呼び出し側が `chat_json_chunked` の既定（`DEFAULT_CHUNK_SIZE` と
    `DEFAULT_OPTIONS`）のままなら何も変わらない。**配線が外れたことに気づけない**ので、
    実際に渡している値を見る。
    """
    assert COMPILE_CHUNK_SIZE != DEFAULT_CHUNK_SIZE
    src = inspect.getsource(compiler_module.AIPlanCompiler.compile)
    assert "chunk_size=COMPILE_CHUNK_SIZE" in src
    assert "options=LARGE_CONTEXT_OPTIONS" in src
