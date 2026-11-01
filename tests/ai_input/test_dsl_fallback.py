"""フォールバック取得計画（所在候補の優先順の展開）のテスト

`SystemLocation.candidates` は「優先順のデータ所在候補の配列」として設計されているが、
DSL に fallback 専用の op は無い。**新しい op は足さず**、`compare` の単項演算子
（`is_empty` / `is_not_empty`）と `guard` の組み合わせで表現する。

```
- op: fhir_extract  id: a
- op: compare       id: has_a  left: $a       cmp: is_not_empty
- op: compare       id: no_a   left: $has_a   cmp: ==  right: false
- op: sql_extract   id: b      guard: $no_a
- op: branch        id: v      when: $has_a   then: $a  else_branch: $b
return: $v
```

固定するのは3点である。

1. **空判定が `guard` と同じ規則**であること（`is_empty_value`: None / "" / []）。
   `==` 経由の比較は `str()` に落ちるので、LLM の空出力（`""`）や0件 multi（`[]`）を
   「値あり」と誤判定して fallback が発火しない
2. **前段が成功したら後段の外部アクセスが起きない**こと（guard による遅延評価）
3. 全候補が空振りしたとき、**根本原因が最後の砦の step に帰属する**こと。先頭の失敗は
   設計どおりの空振りであって原因ではない
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.compiler import COMPILE_SYSTEM_PROMPT
from kawasemi.ai_input.dsl import UNARY_COMPARE_OPS, validate_program
from kawasemi.ai_input.executor import (
    DSLInterpreter,
    _apply_compare,
    derive_item_failure_code,
    is_empty_value,
)
from kawasemi.ai_input.models import FailureCode, InputPlan


def _plan(link_id: str, program: dict) -> InputPlan:
    return InputPlan(questionnaire_id=1, link_id=link_id, program=program, status="confirmed")


def _fallback_program(*, first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """第1候補 → 空なら第2候補、という取得計画を組み立てる。"""
    return {
        "steps": [
            first,
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            {**second, "guard": "$no_a"},
            {
                "op": "branch",
                "id": "v",
                "when": "$has_a",
                "then": "$a",
                "else_branch": "$b",
            },
        ],
        "return": "$v",
    }


_FHIR_FIRST = {
    "op": "fhir_extract",
    "id": "a",
    "fhir_query": "/Observation?patient={{patient_id}}&code=1234-5",
    "fhir_path": "valueQuantity.value",
}
_SQL_SECOND = {
    "op": "sql_extract",
    "id": "b",
    "sql": "SELECT v FROM t WHERE patient_id = :patient_id",
}


# --------------------------------------------------------------------------- #
# 空判定の規則（guard と揃っていること）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("empty", [None, "", []])
def test_is_empty_matches_guard_semantics(empty: Any) -> None:
    """`is_empty` が `guard` と同じ空の定義を使うこと。

    ここがずれると「メトリクス上は不充足なのに代替候補を引かない」という矛盾が起きる。
    `derive_item_failure_code` も `_eval_guard` も `is_empty_value` を使っている。
    """
    assert is_empty_value(empty)
    assert _apply_compare(empty, "is_empty", None) is True
    assert _apply_compare(empty, "is_not_empty", None) is False


@pytest.mark.parametrize("value", ["0", 0, False, [""], "値"])
def test_is_not_empty_accepts_falsy_but_present_values(value: Any) -> None:
    """`0` や `False` は「値がある」こと。真偽で判定すると取り違える。"""
    assert not is_empty_value(value)
    assert _apply_compare(value, "is_not_empty", None) is True
    assert _apply_compare(value, "is_empty", None) is False


@pytest.mark.parametrize("empty", ["", []])
def test_equality_comparison_cannot_express_emptiness(empty: Any) -> None:
    """`==` 経由では空を表現できないこと（`is_empty` を足した理由）。

    `_loose_eq` は `str()` に落とすので、`""` も `[]` も `None` と一致しない。
    この経路に頼ると LLM の空出力や0件 multi で fallback が発火しない。
    """
    assert _apply_compare(empty, "==", None) is False
    assert _apply_compare(empty, "!=", None) is True  # ＝「値がある」と誤判定される


# --------------------------------------------------------------------------- #
# 検証（DSL として書けること・誤記を弾くこと）
# --------------------------------------------------------------------------- #


def test_fallback_program_is_valid() -> None:
    """フォールバックの定型が検証を通ること。"""
    assert validate_program(_fallback_program(first=_FHIR_FIRST, second=_SQL_SECOND)) == []


@pytest.mark.parametrize("cmp", UNARY_COMPARE_OPS)
def test_unary_compare_does_not_require_right(cmp: str) -> None:
    """単項演算子は `right` を要求しないこと。

    `right: null` で書こうとすると、必須フィールド検査が `None` を「未設定」として
    弾くうえ、`_program_dump` の `exclude_none=True` が保存時に落とすので
    「null を書いた」と「書き忘れた」が区別できなくなる。
    """
    program = {
        "steps": [
            _FHIR_FIRST,
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": cmp},
        ],
        "return": "$has_a",
    }
    assert validate_program(program) == []


@pytest.mark.parametrize("cmp", UNARY_COMPARE_OPS)
@pytest.mark.parametrize("right", [0, "", False, "x"])
def test_unary_compare_rejects_right(cmp: str, right: Any) -> None:
    """単項演算子に `right` を書いたら弾くこと。

    黙って無視すると、書き手の意図（右辺と比べたつもり）と実際の意味がずれる。
    **falsy な値（`0` / `""` / `false`）でも弾く**。真偽で判定すると、空文字を書いた
    `is_empty` が黙って通る（`right` を書いたのに無視される）。
    """
    program = {
        "steps": [
            _FHIR_FIRST,
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": cmp, "right": right},
        ],
        "return": "$has_a",
    }
    errors = validate_program(program)
    assert errors
    assert "right を取りません" in errors[0]


def test_binary_compare_still_requires_right() -> None:
    """二項演算子の `right` 必須は変わらないこと（単項の例外が広がっていない）。"""
    program = {
        "steps": [
            _FHIR_FIRST,
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": ">"},
        ],
        "return": "$has_a",
    }
    errors = validate_program(program)
    assert errors
    assert "必須の right が未設定" in errors[0]


# --------------------------------------------------------------------------- #
# 根本原因の帰属
# --------------------------------------------------------------------------- #


def _trace(*entries: dict[str, Any]) -> list[dict[str, Any]]:
    """trace を**本番と同じ形**（JSON 往復後）で組む。

    `InputPlan.program` は JSON 列なので、実行時の guard 文字列は `json.loads` が作った
    別オブジェクトになる。テスト内のリテラルは interned されて同一オブジェクトになるため、
    候補のグループ化を `==` から `is` へ退行させても**リテラルのままでは落ちない**。
    フィクスチャが本番の入力形状を再現していないと、「候補内は最初」の規則の退行が
    そのまま素通りする。
    """
    return list(json.loads(json.dumps(list(entries))))


def test_root_cause_is_the_last_resort_not_the_first_attempt() -> None:
    """全候補が空振りしたとき、根本原因が**最後の砦**に帰属すること。

    先頭の失敗（FHIR に無い）は設計どおりの空振りで、だからこそ第2候補が走った。
    項目が埋まらなかった原因は最後に試した取得の失敗のほうにある。
    """
    trace = _trace(
        {"op": "fhir_extract", "id": "a", "failure_code": FailureCode.NOT_FOUND_FOR_PATIENT},
        {"op": "compare", "id": "has_a"},
        {"op": "compare", "id": "no_a"},
        {
            "op": "sql_extract",
            "id": "b",
            "guard": "$no_a",
            "skipped": False,
            "failure_code": FailureCode.SOURCE_UNAVAILABLE,
        },
        {"op": "branch", "id": "v"},
    )
    assert derive_item_failure_code(trace) == FailureCode.SOURCE_UNAVAILABLE


def test_root_cause_falls_back_to_the_first_failure_without_guards() -> None:
    """guard の無い直列プランでは従来どおり最初の失敗を採ること（回帰しない）。"""
    trace = _trace(
        {"op": "sql_extract", "id": "a", "failure_code": FailureCode.SOURCE_UNAVAILABLE},
        {"op": "transform", "id": "t", "failure_code": FailureCode.UNCLASSIFIED},
    )
    assert derive_item_failure_code(trace) == FailureCode.SOURCE_UNAVAILABLE


def test_skipped_second_candidate_does_not_become_the_root_cause() -> None:
    """第1候補が成功して第2候補が未実行なら、未実行を原因にしないこと。

    skip された entry に `failure_code` を持たせている。executor は現状 skip 時に
    コードを書かないが、それに依存すると「skipped を除外する」処理を外しても落ちない
    テストになる（除外の是非ではなく executor の書き方を測ることになる）。
    """
    trace = _trace(
        {"op": "fhir_extract", "id": "a"},
        {"op": "compare", "id": "has_a"},
        {"op": "compare", "id": "no_a"},
        {
            "op": "sql_extract",
            "id": "b",
            "guard": "$no_a",
            "skipped": True,
            "output": None,
            "failure_code": FailureCode.SOURCE_UNAVAILABLE,
        },
        {"op": "branch", "id": "v"},
    )
    # 実行された step にコードが無いので unclassified（従来どおり）
    assert derive_item_failure_code(trace) == FailureCode.UNCLASSIFIED


def test_conditional_step_without_failure_does_not_shadow_the_first() -> None:
    """guard 付き step が成功しているなら、先頭の失敗が原因のままであること。

    guard の有無だけで無条件に「最後」を採ると、条件付き実行で後段が成功している
    ケースまで巻き込む。**失敗した guard 付き step があるときだけ**切り替える。
    """
    trace = _trace(
        {"op": "fhir_extract", "id": "a", "failure_code": FailureCode.TIMEOUT},
        {"op": "compare", "id": "has_a"},
        {"op": "sql_extract", "id": "b", "guard": "$no_a", "skipped": False},
        {"op": "transform", "id": "t", "failure_code": FailureCode.UNCLASSIFIED},
    )
    assert derive_item_failure_code(trace) == FailureCode.TIMEOUT


def test_empty_guard_is_not_treated_as_conditional() -> None:
    """空文字の guard は「未指定」なので、条件付き試行として扱わないこと。

    `guard_of` が None と空文字を同じ「無条件実行」として扱う規則に揃える。
    """
    trace = _trace(
        {"op": "fhir_extract", "id": "a", "failure_code": FailureCode.NOT_FOUND_FOR_PATIENT},
        {"op": "sql_extract", "id": "b", "guard": "   ", "failure_code": FailureCode.TIMEOUT},
    )
    assert derive_item_failure_code(trace) == FailureCode.NOT_FOUND_FOR_PATIENT


def test_guard_true_literal_is_not_treated_as_conditional() -> None:
    """`guard: true` の step は無条件実行なので、候補の試行として扱わないこと。

    コンパイラのプロンプトは guard に真偽値リテラルを書くことを許可している。
    `guard: true` を「候補」と見なすと、直列プランで先頭の本当の原因が隠れる。
    """
    trace = _trace(
        {"op": "sql_extract", "id": "a", "failure_code": FailureCode.SOURCE_UNAVAILABLE},
        {"op": "transform", "id": "t", "guard": True, "failure_code": FailureCode.UNCLASSIFIED},
    )
    assert derive_item_failure_code(trace) == FailureCode.SOURCE_UNAVAILABLE


def test_guard_false_literal_step_does_not_change_attribution() -> None:
    """`guard: false` の step は skipped になるので、帰属の挙動が変わらないこと。"""
    trace = _trace(
        {"op": "fhir_extract", "id": "a", "failure_code": FailureCode.NOT_FOUND_FOR_PATIENT},
        {
            "op": "sql_extract",
            "id": "b",
            "guard": False,
            "skipped": True,
            "failure_code": FailureCode.TIMEOUT,
        },
    )
    assert derive_item_failure_code(trace) == FailureCode.NOT_FOUND_FOR_PATIENT


# --------------------------------------------------------------------------- #
# 実行時の挙動（前段が成功したら後段の外部アクセスが起きない）
# --------------------------------------------------------------------------- #


def _interp_with(first_value: Any, on_second: Any) -> Any:
    """第1候補が `first_value` を返し、第2候補が `on_second` で振る舞うインタプリタ。"""
    interp = DSLInterpreter()

    async def _fake_fhir(
        step: dict, bind_value: str, fhir_cache: Any, base: dict, **kwargs: Any
    ) -> tuple:
        return first_value, {**base, "output": first_value}

    interp._op_fhir = AsyncMock(side_effect=_fake_fhir)  # type: ignore[method-assign]
    interp._op_sql = on_second  # type: ignore[method-assign]
    return interp


@pytest.mark.asyncio
@pytest.mark.parametrize("empty", [None, "", []])
async def test_second_candidate_runs_only_when_the_first_is_empty(
    empty: Any, db_session: AsyncSession
) -> None:
    """第1候補が空なら第2候補が走り、その値が採られること。

    空の3形（None / 空文字 / 0件 multi）すべてで発火する。`==` 経由の比較だと
    空文字と 0件 multi が「値あり」になって発火しない。
    """

    async def _fake_sql(step: dict, db: Any, bind_value: str, base: dict, **kwargs: Any) -> tuple:
        return "院内DBの値", {**base, "output": "院内DBの値"}

    interp = _interp_with(empty, AsyncMock(side_effect=_fake_sql))
    program = _fallback_program(first=_FHIR_FIRST, second=_SQL_SECOND)

    prefill, evidence = await interp.execute([_plan("fb", program)], "P1", db_session)

    assert prefill["fb"] == "院内DBの値"
    interp._op_sql.assert_awaited_once()
    by_id = {t["id"]: t for t in evidence["fb"]["trace"]}
    assert by_id["b"]["skipped"] is False
    assert by_id["v"]["taken"] == "else"


@pytest.mark.asyncio
async def test_second_candidate_is_not_accessed_when_the_first_succeeds(
    db_session: AsyncSession,
) -> None:
    """**第1候補が値を取れたら、第2候補の外部アクセスが1回も起きないこと。**

    フォールバック計画の中核の要件。branch だけで書くと両方の取得が実行され、採用され
    なかった側の生データまで trace に残る（guard の遅延評価が解消した問題）。
    """
    interp = _interp_with(
        "FHIRの値",
        AsyncMock(side_effect=AssertionError("第1候補が成功したのに第2候補へアクセスした")),
    )
    program = _fallback_program(first=_FHIR_FIRST, second=_SQL_SECOND)

    prefill, evidence = await interp.execute([_plan("fb", program)], "P1", db_session)

    assert prefill["fb"] == "FHIRの値"
    interp._op_sql.assert_not_awaited()
    by_id = {t["id"]: t for t in evidence["fb"]["trace"]}
    assert by_id["b"]["skipped"] is True
    assert by_id["v"]["taken"] == "then"


# --------------------------------------------------------------------------- #
# 3候補以上（判定 step にも guard を伝播させる）
# --------------------------------------------------------------------------- #


def _three_candidate_program() -> dict[str, Any]:
    """3候補のフォールバック。**2段目の判定 step にも同じ guard を付ける。**

    付け忘れると、skip された step の値が None になるので `has_b` が「空だった」と出て、
    `no_b` が真になり、**第1候補が成功しているのに第3候補の患者データ取得が走る**。
    """
    return {
        "steps": [
            _FHIR_FIRST,
            {"op": "compare", "id": "has_a", "left": "$a", "cmp": "is_not_empty"},
            {"op": "compare", "id": "no_a", "left": "$has_a", "cmp": "==", "right": False},
            {**_SQL_SECOND, "guard": "$no_a"},
            {"op": "compare", "id": "has_b", "guard": "$no_a", "left": "$b", "cmp": "is_not_empty"},
            {
                "op": "compare",
                "id": "no_b",
                "guard": "$no_a",
                "left": "$has_b",
                "cmp": "==",
                "right": False,
            },
            {
                "op": "external_db_extract",
                "id": "c",
                "guard": "$no_b",
                "connection_id": "docs",
                "sql": "SELECT v FROM alt WHERE patient_id = :patient_id",
            },
            {"op": "branch", "id": "v2", "when": "$has_b", "then": "$b", "else_branch": "$c"},
            {"op": "branch", "id": "v", "when": "$has_a", "then": "$a", "else_branch": "$v2"},
        ],
        "return": "$v",
    }


def test_three_candidate_program_is_valid() -> None:
    assert validate_program(_three_candidate_program()) == []


@pytest.mark.asyncio
async def test_third_candidate_is_not_accessed_when_the_first_succeeds(
    db_session: AsyncSession,
) -> None:
    """**第1候補が成功したら、第3候補の外部アクセスも起きないこと。**

    2段目の判定 step から guard を落とすと、skip された第2候補の値（None）を「空だった」
    と判定して第3候補が走る。2候補のテストでは検出できない。
    """
    interp = _interp_with(
        "FHIRの値",
        AsyncMock(side_effect=AssertionError("第1候補が成功したのに第2候補へアクセスした")),
    )
    interp._op_external_db = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("第1候補が成功したのに第3候補へアクセスした")
    )

    prefill, evidence = await interp.execute(
        [_plan("fb3", _three_candidate_program())], "P1", db_session
    )

    assert prefill["fb3"] == "FHIRの値"
    interp._op_sql.assert_not_awaited()
    interp._op_external_db.assert_not_awaited()
    by_id = {t["id"]: t for t in evidence["fb3"]["trace"]}
    assert by_id["b"]["skipped"] is True
    assert by_id["has_b"]["skipped"] is True  # 判定 step も走らない
    assert by_id["c"]["skipped"] is True


@pytest.mark.asyncio
async def test_third_candidate_runs_when_both_earlier_ones_are_empty(
    db_session: AsyncSession,
) -> None:
    """第1・第2候補がともに空なら、第3候補まで進んでその値が採られること。"""

    async def _empty_sql(step: dict, db: Any, bind_value: str, base: dict, **kw: Any) -> tuple:
        return None, {**base, "output": None}

    async def _third(step: dict, bind_value: str, base: dict, **kw: Any) -> tuple:
        return "文書の値", {**base, "output": "文書の値"}

    interp = _interp_with(None, AsyncMock(side_effect=_empty_sql))
    interp._op_external_db = AsyncMock(side_effect=_third)  # type: ignore[method-assign]

    prefill, evidence = await interp.execute(
        [_plan("fb3", _three_candidate_program())], "P1", db_session
    )

    assert prefill["fb3"] == "文書の値"
    by_id = {t["id"]: t for t in evidence["fb3"]["trace"]}
    assert by_id["c"]["skipped"] is False
    assert by_id["v"]["taken"] == "else"
    assert by_id["v2"]["taken"] == "else"


# --------------------------------------------------------------------------- #
# 根本原因の帰属（候補間は最後・候補内は最初）
# --------------------------------------------------------------------------- #


def test_root_cause_takes_the_last_candidate_not_the_last_step() -> None:
    """候補が2つとも失敗したら、**最後の候補**のコードを採ること。

    guard 付き失敗が2件あるので、`conditional[0]` へ退行させると落ちる。
    """
    trace = _trace(
        {"op": "fhir_extract", "id": "a", "failure_code": FailureCode.NOT_FOUND_FOR_PATIENT},
        {"op": "compare", "id": "has_a"},
        {"op": "compare", "id": "no_a"},
        {
            "op": "sql_extract",
            "id": "b",
            "guard": "$no_a",
            "failure_code": FailureCode.TIMEOUT,
        },
        {"op": "compare", "id": "has_b", "guard": "$no_a"},
        {
            "op": "external_db_extract",
            "id": "c",
            "guard": "$no_b",
            "failure_code": FailureCode.SOURCE_UNAVAILABLE,
        },
    )
    assert derive_item_failure_code(trace) == FailureCode.SOURCE_UNAVAILABLE


def test_root_cause_at_the_crossing_of_both_rules() -> None:
    """**候補が3つあり、最後の候補が複数 step**という2規則の交差点。

    「候補間は最後」を固定するテストは最終候補が1 step、「候補内は最初」を固定する
    テストは guard グループが1つだけなので、両者を同時に満たす形が無いと
    「候補が複数なら全体の最後・単一候補なら最初」という**両テストと同値な誤実装**が
    素通りする（根本原因を取り違える不具合が実際にこの交差点で起きた）。
    """
    trace = _trace(
        # 先頭の無条件失敗は、期待値と**別のコード**にする（同じにすると「候補間も最初を
        # 採る」誤実装でも偶然一致してしまい、このテスト単体では測れない）。
        {"op": "fhir_extract", "id": "a", "failure_code": FailureCode.SOURCE_UNAVAILABLE},
        {"op": "compare", "id": "has_a"},
        {"op": "compare", "id": "no_a"},
        {"op": "sql_extract", "id": "b", "guard": "$no_a", "failure_code": FailureCode.TIMEOUT},
        {"op": "compare", "id": "has_b", "guard": "$no_a"},
        {"op": "compare", "id": "no_b", "guard": "$no_a"},
        {
            "op": "file_extract",
            "id": "c",
            "guard": "$no_b",
            "failure_code": FailureCode.NOT_FOUND_FOR_PATIENT,
        },
        {
            "op": "llm_extract",
            "id": "d",
            "guard": "$no_b",
            "failure_code": FailureCode.EXTRACTION_LOW_CONFIDENCE,
        },
    )
    # 最後の候補（$no_b）の、その中の最初の失敗＝取得の失敗
    assert derive_item_failure_code(trace) == FailureCode.NOT_FOUND_FOR_PATIENT


def test_root_cause_groups_by_guard_expression_not_by_adjacency() -> None:
    """候補の区切りは guard 式の同値で見る（trace 上で離れて並んでいてもよい）こと。

    同じ guard を共有する step 列は1候補の試行なので、間に別 guard の step が挟まっても
    グループは変わらない。隣接性でグループ化する実装へ替えると結果が変わる。
    """
    trace = _trace(
        {"op": "fhir_extract", "id": "a", "failure_code": FailureCode.NOT_FOUND_FOR_PATIENT},
        {"op": "file_extract", "id": "b", "guard": "$g1", "failure_code": FailureCode.TIMEOUT},
        {
            "op": "sql_extract",
            "id": "x",
            "guard": "$g2",
            "failure_code": FailureCode.SOURCE_UNAVAILABLE,
        },
        {
            "op": "llm_extract",
            "id": "c",
            "guard": "$g1",
            "failure_code": FailureCode.EXTRACTION_LOW_CONFIDENCE,
        },
    )
    # 最後の失敗は $g1 の step なので候補は $g1。その中の最初の失敗を採る。
    assert derive_item_failure_code(trace) == FailureCode.TIMEOUT


def test_root_cause_ignores_unguarded_failures_after_the_last_candidate() -> None:
    """最後の候補より後ろに guard の無い失敗が来ても、候補の特定に使わないこと。

    「最後の失敗」から候補を決める実装（`failed[-1]`）だと、guard 無しの step が
    末尾に来た時点で候補を特定できなくなる。候補は **guard を持つ失敗のうち最後**から
    決める必要がある。
    """
    trace = _trace(
        {"op": "fhir_extract", "id": "a", "failure_code": FailureCode.NOT_FOUND_FOR_PATIENT},
        {"op": "compare", "id": "has_a"},
        {"op": "compare", "id": "no_a"},
        {
            "op": "sql_extract",
            "id": "b",
            "guard": "$no_a",
            "failure_code": FailureCode.SOURCE_UNAVAILABLE,
        },
        {"op": "transform", "id": "t", "failure_code": FailureCode.UNCLASSIFIED},
    )
    assert derive_item_failure_code(trace) == FailureCode.SOURCE_UNAVAILABLE


def test_root_cause_within_one_candidate_is_the_fetch_not_the_extraction() -> None:
    """1つの候補が複数 step のとき、**取得の失敗**を後段の LLM 抽出の失敗で上書きしないこと。

    `COMPILE_SYSTEM_PROMPT` は「guard 付き step の結果を使う後続 step には同じ guard を
    付ける」と指示しており、1候補が複数 step になるのは推奨形である（叙述を取り出して
    から llm_extract で値を抽出する等）。ここを取り違えると、取得可能性の集計で
    `not_found_for_patient` と `extraction_low_confidence` が入れ替わる。
    """
    trace = _trace(
        {"op": "llm_judge", "id": "is_inpatient"},
        {
            "op": "file_extract",
            "id": "doc",
            "guard": "$is_inpatient",
            "failure_code": FailureCode.NOT_FOUND_FOR_PATIENT,
        },
        {
            "op": "llm_extract",
            "id": "v",
            "guard": "$is_inpatient",
            "failure_code": FailureCode.EXTRACTION_LOW_CONFIDENCE,
        },
    )
    assert derive_item_failure_code(trace) == FailureCode.NOT_FOUND_FOR_PATIENT


# --------------------------------------------------------------------------- #
# 検証の穴（left の必須・needs_input との関係）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("cmp", ["is_empty", "is_not_empty"])
def test_unary_compare_still_requires_left(cmp: str) -> None:
    """単項でも `left` は必須であること。

    `right` を外す例外が `left` まで広がると、`left` を書き忘れた `is_empty` が
    `is_empty_value(None) == True` で**常に「空」**になり、フォールバックが常時発火する。
    黙って誤動作するので検証で止める。
    """
    program = {
        "steps": [
            _FHIR_FIRST,
            {"op": "compare", "id": "has_a", "cmp": cmp},
        ],
        "return": "$has_a",
    }
    errors = validate_program(program)
    assert errors
    assert "必須の left が未設定" in errors[0]


@pytest.mark.parametrize("cmp", ["is_empty", "is_not_empty"])
def test_unary_compare_rejects_right_even_with_needs_input(cmp: str) -> None:
    """`needs_input=true` でも `right` の誤記は弾くこと。

    必須欠落の許容とは目的が違う。これは「書いたものが無視される」ことを知らせる検査で、
    人の補完待ちかどうかとは無関係である。
    """
    program = {
        "steps": [
            _FHIR_FIRST,
            {
                "op": "compare",
                "id": "has_a",
                "left": "$a",
                "cmp": cmp,
                "right": 5,
                "needs_input": True,
            },
        ],
        "return": "$has_a",
    }
    errors = validate_program(program)
    assert errors
    assert "right を取りません" in errors[0]


# --------------------------------------------------------------------------- #
# プロンプトが示す定型そのものの検証
#
# 3候補のときに2段目の判定 step に guard が付かなくなった不具合は、**プロンプト**の
# 定型が原因で、実装ではなかった。テストが自前の定型を持つと、プロンプト側だけが壊れても誰も気づかない
# （テストと LLM への指示が別々の写しになる）。ここでは字面を抽出して実行する。
# --------------------------------------------------------------------------- #


def _prompt_fallback_examples() -> list[list[dict[str, Any]]]:
    """`COMPILE_SYSTEM_PROMPT` のフォールバック節から、例の step 列を抽出する。"""
    section = COMPILE_SYSTEM_PROMPT.split("# フォールバック（所在候補が複数あるとき）")[1]
    section = section.split("# 厳守事項")[0]
    examples: list[list[dict[str, Any]]] = []
    for block in section.split("- 例（")[1:]:
        steps = [
            json.loads(line)
            for raw in block.splitlines()
            if (line := raw.strip()).startswith('{"op"')
        ]
        if steps:
            examples.append(steps)
    return examples


def test_quickstart_fallback_example_is_a_valid_program() -> None:
    """`docs/dsl_specification.md` のフォールバックの例も検証を通ること。

    プロンプトの例は字面を抽出して実行しているが、QUICKSTART は誰も検証していなかった。
    人が手で書くときに読むのはこちらなので、壊れていても気づけない状態だった。
    """
    # DSL の正本は docs/dsl_specification.md（§8.5 がフォールバック）
    text = (Path(__file__).resolve().parents[2] / "docs/dsl_specification.md").read_text(
        encoding="utf-8"
    )
    section = text.split("### 8.5 フォールバック（所在の候補が複数あるとき）")[1].split("\n### ")[0]
    blocks = [b.split("```")[0] for b in section.split("```json")[1:]]

    assert blocks, "フォールバックの節に JSON の例が無い"
    # 完全な program の例（"steps" を持つ）と、step 列だけの断片を分ける
    programs = [json.loads(b) for b in blocks if '"steps"' in b]
    assert programs, "完全な program の例が無い"
    for program in programs:
        assert validate_program(program) == []
    # 3候補の断片も、定型どおり判定 step に guard が付いていること
    fragment = next(b for b in blocks if '"steps"' not in b)
    steps = json.loads("[" + fragment.strip().rstrip(",") + "]")
    by_id = {s["id"]: s for s in steps}
    assert by_id["has_b"].get("guard") == "$no_a"
    assert by_id["no_b"].get("guard") == "$no_a"


def test_prompt_fallback_examples_are_valid_programs() -> None:
    """プロンプトの例が2件（2候補・3候補）あり、どちらも検証を通ること。"""
    examples = _prompt_fallback_examples()

    assert len(examples) == 2
    assert [len(steps) for steps in examples] == [5, 9]
    for steps in examples:
        assert validate_program({"steps": steps, "return": "$v"}) == []


@pytest.mark.asyncio
async def test_prompt_three_candidate_example_does_not_access_later_candidates(
    db_session: AsyncSession,
) -> None:
    """プロンプトの3候補の例を**そのまま実行**し、第1候補成功時に後段が走らないこと。

    2段目の判定 step（`has_b` / `no_b`）から guard が落ちると、`has_b` が実行されて
    「空だった」と出るので `no_b` が真になり、第3候補のファイル取得が走る。
    """
    steps = _prompt_fallback_examples()[1]
    interp = _interp_with(
        "FHIRの値",
        AsyncMock(side_effect=AssertionError("第1候補が成功したのに第2候補へアクセスした")),
    )
    interp._op_file = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("第1候補が成功したのに第3候補へアクセスした")
    )

    prefill, evidence = await interp.execute(
        [_plan("fb_prompt", {"steps": steps, "return": "$v"})], "P1", db_session
    )

    assert prefill["fb_prompt"] == "FHIRの値"
    interp._op_sql.assert_not_awaited()
    interp._op_file.assert_not_awaited()
    by_id = {t["id"]: t for t in evidence["fb_prompt"]["trace"]}
    assert by_id["has_b"]["skipped"] is True  # 判定 step にも guard が伝播している
    assert by_id["no_b"]["skipped"] is True
    assert by_id["b"]["skipped"] is True
    assert by_id["c"]["skipped"] is True
    assert by_id["v"]["taken"] == "then"


@pytest.mark.asyncio
async def test_prompt_three_candidate_example_returns_the_third_value(
    db_session: AsyncSession,
) -> None:
    """プロンプトの3候補の例で、**第1・第2が空なら第3候補の値が返ること**。

    「第1候補が成功」方向だけを実行すると、`branch` の入れ子（`else_branch: "$v2"`）を
    平坦化するような破壊が素通りする（第3候補の値がどこにも返らなくなる）。
    """
    steps = _prompt_fallback_examples()[1]

    async def _empty_sql(step: dict, db: Any, bind_value: str, base: dict, **kw: Any) -> tuple:
        return None, {**base, "output": None}

    async def _third(step: dict, bind_value: str, base: dict, **kw: Any) -> tuple:
        return "文書の値", {**base, "output": "文書の値"}

    interp = _interp_with(None, AsyncMock(side_effect=_empty_sql))
    interp._op_file = AsyncMock(side_effect=_third)  # type: ignore[method-assign]

    prefill, evidence = await interp.execute(
        [_plan("fb_prompt3", {"steps": steps, "return": "$v"})], "P1", db_session
    )

    assert prefill["fb_prompt3"] == "文書の値"
    by_id = {t["id"]: t for t in evidence["fb_prompt3"]["trace"]}
    assert by_id["c"]["skipped"] is False
    assert by_id["v2"]["taken"] == "else"
    assert by_id["v"]["taken"] == "else"


@pytest.mark.asyncio
async def test_prompt_two_candidate_example_keeps_the_refetched_value(
    db_session: AsyncSession,
) -> None:
    """プロンプトの2候補の例で、第1候補が**0件**でも第2候補の値が捨てられないこと。

    `branch.when` に取得結果そのもの（`$a`）を置くと `_truthy([])` が真になり、
    取り直した値が捨てられる（プロンプト自身が禁じている書き方）。0件で実行しないと
    `when` が `$has_a` から `$a` へ退行しても気づけない。
    """
    steps = _prompt_fallback_examples()[0]

    async def _second(step: dict, db: Any, bind_value: str, base: dict, **kw: Any) -> tuple:
        return "院内DBの値", {**base, "output": "院内DBの値"}

    interp = _interp_with([], AsyncMock(side_effect=_second))

    prefill, evidence = await interp.execute(
        [_plan("fb_prompt2", {"steps": steps, "return": "$v"})], "P1", db_session
    )

    assert prefill["fb_prompt2"] == "院内DBの値"
    by_id = {t["id"]: t for t in evidence["fb_prompt2"]["trace"]}
    assert by_id["b"]["skipped"] is False
    assert by_id["v"]["taken"] == "else"
