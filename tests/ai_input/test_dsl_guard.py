"""DSL の条件付き実行（step.guard）の検証

guard は「実行するか否か」を決める。branch（値の選択）と役割が異なり、guard が偽の step は
**実行されない**（外部システムアクセスも LLM 呼び出しも起きない）。ここでは以下を固定する:

- guard が偽の step が実行されず、trace に skipped=True で記録される
- skip は失敗ではなく、項目の failure_code を汚染しない
- 条件分岐プランで採用されなかった側の外部アクセス・LLM 呼び出しが発生しない
- validate_program が guard の参照健全性（前方参照のみ）を検査する
- 取得フロー図が skip 経路と guard 依存を描く
"""

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.dsl import validate_program
from kawasemi.ai_input.executor import (
    DSLInterpreter,
    _all_data_steps_skipped,
    _skips_are_judged,
    derive_item_failure_code,
    guard_of,
)
from kawasemi.ai_input.models import FailureCode, InputPlan
from kawasemi.ai_input.schemas import DSLStep
from kawasemi.ai_input.viz import program_to_mermaid


def _plan(link_id: str, program: dict) -> InputPlan:
    return InputPlan(questionnaire_id=1, link_id=link_id, program=program, status="confirmed")


def _chat_response(content: str) -> SimpleNamespace:
    """ollama AsyncClient.chat の戻り値形（参照されるのは message.content のみ）。"""
    return SimpleNamespace(message=SimpleNamespace(content=content))


# --------------------------------------------------------------- schema / 検証


def test_dslstep_accepts_guard_and_defaults_to_none() -> None:
    """DSLStep は guard を任意フィールドとして受け、未指定なら None（無条件実行）。"""
    assert DSLStep(op="sql_extract", id="x", sql="SELECT 1").guard is None
    assert DSLStep(op="sql_extract", id="x", sql="SELECT 1", guard="$cond").guard == "$cond"


def test_validate_guard_forward_reference_ok() -> None:
    """自身より前の step を指す guard は妥当。"""
    program = {
        "steps": [
            {"op": "compare", "id": "cond", "left": 1, "cmp": "==", "right": 1},
            {"op": "sql_extract", "id": "x", "guard": "$cond", "sql": "SELECT 1"},
        ],
        "return": "$x",
    }
    assert validate_program(program) == []


def test_validate_guard_backward_reference_rejected() -> None:
    """後方（まだ実行していない step）を指す guard は検証エラー。実行可否を決められない。"""
    program = {
        "steps": [
            {"op": "sql_extract", "id": "x", "guard": "$cond", "sql": "SELECT 1"},
            {"op": "compare", "id": "cond", "left": 1, "cmp": "==", "right": 1},
        ],
        "return": "$x",
    }
    errors = validate_program(program)
    assert any("guard" in e and "$cond" in e for e in errors)


def test_validate_guard_undefined_reference_rejected() -> None:
    """存在しない step を指す guard は検証エラー。"""
    errors = validate_program(
        {"steps": [{"op": "sql_extract", "id": "x", "guard": "$nope", "sql": "SELECT 1"}]}
    )
    assert any("guard" in e for e in errors)


@pytest.mark.parametrize(
    "guard",
    [
        # $ を落とした参照。実行時は _truthy("is_inpatient")=True で**常に実行**され、
        # guard が黙って no-op になる（guard で消したい無条件実行が痕跡なく復活する）。
        # プランは LLM 生成の JSON なので $ 落ちは現実的な誤りであり、必ず弾く。
        "is_inpatient",
        "$",  # is_ref が偽（"$" のみ）
        0,  # 恒久 skip になる数値リテラル
        1,
        "false",  # 真偽トークン風の文字列も参照でないので拒否
        {"a": 1},  # 真偽に正規化できない
        ["a"],
    ],
)
def test_validate_guard_rejects_non_ref_non_bool(guard: object) -> None:
    """guard は "$id" 参照か真偽リテラルのみ。それ以外は確定前に検証エラーにする。"""
    errors = validate_program(
        {"steps": [{"op": "sql_extract", "id": "x", "guard": guard, "sql": "SELECT 1"}]}
    )
    assert any("guard" in e for e in errors), errors


@pytest.mark.parametrize("guard", [True, False])
def test_validate_guard_accepts_bool_literal(guard: bool) -> None:
    """真偽リテラルは妥当（false はその step を恒久的に無効化する書き方）。"""
    assert (
        validate_program(
            {
                "steps": [{"op": "sql_extract", "id": "x", "guard": guard, "sql": "SELECT 1"}],
                "return": "$x",
            }
        )
        == []
    )


def test_validate_guard_self_reference_rejected() -> None:
    """自己参照 guard は検証エラー。実行時は未束縛＝偽で恒久 skip となり取得が静かに消える。

    自 step の id は seen_ids に入っているため前方参照チェックだけでは通ってしまう。
    """
    errors = validate_program(
        {"steps": [{"op": "sql_extract", "id": "x", "guard": "$x", "sql": "SELECT 1"}]}
    )
    assert any("自分自身" in e for e in errors), errors


def test_dslstep_accepts_bool_guard() -> None:
    """プラン編集 UI で手書きされた `"guard": false` が 422 にならない（型の3層整合）。"""
    assert DSLStep(op="sql_extract", id="x", sql="SELECT 1", guard=False).guard is False


# --------------------------------------------------------- failure_code の扱い


def test_derive_item_failure_code_ignores_skipped_steps() -> None:
    """skip は失敗ではないため、skipped の step が根本原因に採用されない。"""
    trace = [
        {"op": "compare", "id": "cond", "output": False},
        # 万一 skip 側にコードが載っていても根本原因に採用しない（誤認防止）
        {"op": "sql_extract", "id": "x", "skipped": True, "failure_code": "schema_mismatch"},
        {"op": "llm_extract", "id": "v", "failure_code": FailureCode.EXTRACTION_LOW_CONFIDENCE},
    ]
    assert derive_item_failure_code(trace) == FailureCode.EXTRACTION_LOW_CONFIDENCE


def test_derive_item_failure_code_all_skipped_is_needs_input() -> None:
    """実行された step が1つも無い（全 step が guard 偽）なら needs_input。

    **not_applicable との境界**: 取得系 step が全て skip でも、判定 step が
    実行されていなければ「該当しないと判定された」わけではない。自動取得が試みられていない
    のだから人の入力を待つ（`needs_input`）。
    """
    trace = [
        {"op": "sql_extract", "id": "a", "skipped": True},
        {"op": "fhir_extract", "id": "b", "skipped": True},
    ]
    assert derive_item_failure_code(trace) == FailureCode.NEEDS_INPUT


def test_derive_item_failure_code_judged_then_all_data_steps_skipped_is_not_applicable() -> None:
    """判定が実行され、取得系 step が全て skip なら not_applicable。

    「該当しないので取得しなくて正しい」であって取得の失敗ではない。`unclassified` に落ちると
    取得困難度の分母に失敗として入り、困難度を過大に見せる。
    """
    trace = [
        {"op": "compare", "id": "cond", "output": False},
        {"op": "sql_extract", "id": "a", "guard": "$cond", "skipped": True},
        {"op": "fhir_extract", "id": "b", "guard": "$cond", "skipped": True},
        {"op": "branch", "id": "v", "output": None},
    ]
    assert derive_item_failure_code(trace) == FailureCode.NOT_APPLICABLE


def test_a_plan_where_no_judgment_ran_is_not_not_applicable() -> None:
    """**判定 op が実行されていなければ not_applicable にしない**。

    `transform` / `select` / `aggregate` / `branch` は**空を返しても failure_code を付けない**
    （`_op_transform` / `_op_select` ほか）。そのため「実行された step がある」だけを条件に
    すると、判定が動いていないプランでも後段が空へ潰れた瞬間に「該当しない」に見える。

    **この誤りは項目を分母から余分に外すので、困難度を過小に見せる**。
    """
    trace = [
        # 判定 op は1つも実行されていない。後処理が空を返しただけ
        {"op": "transform", "id": "t", "output": None},
        # guard は同じトレースの `transform` を指す（`validate_program` が通る実在の形）
        {"op": "sql_extract", "id": "a", "guard": "$t", "skipped": True},
    ]
    assert derive_item_failure_code(trace) == FailureCode.UNCLASSIFIED


def test_a_skipped_judgment_op_does_not_count_as_judged() -> None:
    """判定 op が**トレースに在るだけ**では足りず、**実行されている**ことを要求する。

    「どこかで判定 op が走ったか」で見ると、skip された判定 op でも通ってしまう。実測で
    確かめた形（`_any_judgment_executed(executed)` を `(trace)` に緩めると素通りした）。
    """
    trace = [
        {"op": "compare", "id": "outer", "output": False},
        # 判定 op がトレースに在るが skip されている
        {"op": "compare", "id": "cond", "guard": "$outer", "skipped": True},
        {"op": "sql_extract", "id": "a", "guard": "$cond", "skipped": True},
    ]
    assert derive_item_failure_code(trace) == FailureCode.UNCLASSIFIED


def test_a_boolean_literal_guard_is_not_a_judgment() -> None:
    """guard が真偽リテラルなら `not_applicable` にしない。

    無関係な判定 op が1つ走っていれば通ってしまう形。`guard: false` は
    コンパイラのプロンプトが明示的に許可している書き方なので、実在するプラン空間の中の形である。
    **判定を経ずに分母から外れると、困難度を過小に見せる。**
    """
    trace = [
        # 無関係な判定 op（この skip の原因ではない）
        {"op": "compare", "id": "unrelated", "output": True},
        {"op": "sql_extract", "id": "a", "guard": False, "skipped": True},
    ]
    assert derive_item_failure_code(trace) == FailureCode.UNCLASSIFIED


def test_a_guard_pointing_at_a_non_judgment_step_is_not_judged() -> None:
    """guard が判定 op 以外（`select` など）を指しているなら `not_applicable` にしない。

    判定を経ずに分母から外れる典型の形: `select` が空へ潰れ、その値を見る guard で取得系が skip
    される。**取得できなかったのであって「該当しない」のではない。**
    """
    trace = [
        # **判定 op の id を含む id** を非判定 step に付ける。一致を部分一致に緩める退行
        # （`j in ref_name(guard)`）を捕まえるため（LLM が付ける id は
        # 接頭辞を共有しがちなので、実際に起こりうる形である）
        {"op": "compare", "id": "cond", "output": True},
        {"op": "select", "id": "cond_rows", "output": None},
        {"op": "sql_extract", "id": "a", "guard": "$cond_rows", "skipped": True},
    ]
    assert derive_item_failure_code(trace) == FailureCode.UNCLASSIFIED


def test_every_skipped_data_step_must_be_judged() -> None:
    """**すべての**skip がその判定に紐づいていること（1つでも紐づかなければ対象外）。

    候補が2つあり、片方だけが判定に紐づく形。`all` を `any` に緩めると、判定を経ていない skip が
    混ざったまま分母から外れる。
    """
    trace = [
        {"op": "compare", "id": "cond", "output": False},
        {"op": "sql_extract", "id": "a", "guard": "$cond", "skipped": True},
        {"op": "fhir_extract", "id": "b", "guard": False, "skipped": True},
    ]
    assert derive_item_failure_code(trace) == FailureCode.UNCLASSIFIED


def test_an_llm_judge_also_counts_as_a_judgment() -> None:
    """判定は `compare` だけでなく `llm_judge` でも成立すること。

    推奨形は規約文書からの `llm_judge` で分岐することがあるので、`compare` だけを判定と
    見なすと、その形が `unclassified` に残る。
    """
    trace = [
        {"op": "llm_judge", "id": "is_post_op", "output": False},
        {"op": "file_extract", "id": "a", "guard": "$is_post_op", "skipped": True},
    ]
    assert derive_item_failure_code(trace) == FailureCode.NOT_APPLICABLE


def test_an_undeterminable_judgement_does_not_leave_the_denominator() -> None:
    """**判定不能で取得系が全 skip になった項目を `not_applicable` にしない**。

    判定 op の「判定できない応答」には `extraction_low_confidence` が付く。そのため
    **同じ trace の形が2つの分類の競合になる**——判定が動いて取得系が全 skip なので
    `not_applicable` の条件を満たすが、判定 op 自身が失敗している。

    **`not_applicable` になってはいけない。** それは「患者の文脈上そもそも該当しない」＝取得
    しなくて正しい、という意味で**自動充足率の分母から外れる**（`_DENOMINATOR_EXCLUDED_CODES`）。
    判定できなかった項目が分母から外れると、判定不能にコードを付けて防いだ向き（困難度を**過小**に見せる）の
    バイアスが**別の経路で戻る**。実装では `if failed:` が `not_applicable` の判定より前に
    あるのでそうなっているが、**順序を入れ替える変更が黙って通らないように**ここで固定する。

    前提（競合が成立していること）を assert として置く。前提が壊れたら本命より先に落ちる。
    """
    trace = [
        # 判定不能（**実行されており skipped ではない**）
        {
            "op": "llm_judge",
            "id": "is_inpatient",
            "output": None,
            "failure_code": FailureCode.EXTRACTION_LOW_CONFIDENCE,
        },
        # 判定不能を `== false` で言い換えた側も偽になるので、**両方の候補が skip される**
        {"op": "compare", "id": "is_outpatient", "output": False},
        {"op": "sql_extract", "id": "a", "guard": "$is_inpatient", "skipped": True},
        {"op": "fhir_extract", "id": "b", "guard": "$is_outpatient", "skipped": True},
    ]
    # 前提1: 取得系 step が1つ以上あり、すべて skip されている（`not_applicable` の条件の前半）
    data_steps = [e for e in trace if e["op"] in {"sql_extract", "fhir_extract"}]
    assert data_steps and all(e.get("skipped") for e in data_steps)
    # 前提2: 判定 op が実行されている（`not_applicable` の条件の後半。結線も成立している）
    assert not trace[0].get("skipped") and not trace[1].get("skipped")
    assert {e["guard"] for e in data_steps} == {"$is_inpatient", "$is_outpatient"}

    assert derive_item_failure_code(trace) == FailureCode.EXTRACTION_LOW_CONFIDENCE

    # 対照: 判定が**できた**（失敗コードが無い）同じ形は `not_applicable` である。
    # 2つの分類を分けているのは「判定 op が失敗しているか」だけであることを示す。
    determined = [dict(e) for e in trace]
    determined[0] = {"op": "llm_judge", "id": "is_inpatient", "output": False}
    assert derive_item_failure_code(determined) == FailureCode.NOT_APPLICABLE


def test_a_plan_without_any_data_op_is_not_not_applicable() -> None:
    """取得系 op を1つも持たないプランは not_applicable にしない。

    判定だけ・`manual` だけのプランは「該当しない」ではなく別の状態である。`_DATA_OPS` を
    基準にするのは、**取得を試みたかどうか**が「該当しない」の前提だから。
    """
    trace = [
        {"op": "compare", "id": "cond", "output": False},
        {"op": "llm_judge", "id": "j", "guard": "$cond", "skipped": True},
        {"op": "branch", "id": "v", "output": None},
    ]
    assert derive_item_failure_code(trace) == FailureCode.UNCLASSIFIED


def test_one_executed_data_step_prevents_not_applicable() -> None:
    """取得系 step が1つでも実行されていれば not_applicable にしない。

    取得を試みて値が得られなかったのだから「該当しない」ではない。**全て skip** が条件である
    ことを固定する（`any` へ緩めると、取得を試みた項目まで分母から消える）。

    **実行された取得 step にも判定の guard を付ける。** guard 無しにすると「結線が無いから
    対象外」という別の理由でも通ってしまい、`all` → `any` の退行を捕まえられない
    （結線を入れたあとに実測したところ、実際に素通りした）。
    """
    trace = [
        # 2つの判定（片方が真・片方が偽）。同じ guard で skipped が True と False に分かれる
        # トレースは実際には作れないので、判定を2つにして整合させる
        {"op": "compare", "id": "has_a", "output": True},
        {"op": "compare", "id": "has_b", "output": False},
        # 判定を見て**実行された**取得 step（値は空だがコードは付いていない）
        {"op": "sql_extract", "id": "a", "guard": "$has_a", "skipped": False, "output": None},
        {"op": "fhir_extract", "id": "b", "guard": "$has_b", "skipped": True},
    ]
    assert derive_item_failure_code(trace) == FailureCode.UNCLASSIFIED


def test_a_legacy_trace_without_skipped_key_is_not_not_applicable() -> None:
    """`skipped` キーが無い旧レコードは「実行された」と読む。

    判定できないものを新しい分類へ倒さない（fail-closed）。旧レコードが黙って分母から
    消えると、`not_applicable` 導入の前後の集計値が比較できるように見えてしまう。
    """
    trace = [
        {"op": "compare", "id": "cond", "output": True},
        # 旧レコード: skipped キーが無い。**判定の guard は付いている**（付けないと「結線が
        # 無いから対象外」で通ってしまい、キー無しの読み方の退行を捕まえられない）
        {"op": "sql_extract", "id": "a", "guard": "$cond"},
    ]
    assert derive_item_failure_code(trace) == FailureCode.UNCLASSIFIED


def test_a_failure_code_wins_over_not_applicable() -> None:
    """コード付き step があれば、そのコードが not_applicable より優先される。

    実際に失敗した step があるなら、その失敗コードの方が情報量が多い。

    **競合が実際に成立する形にする。** コード付き step を**取得系**にすると
    `_all_data_steps_skipped` が偽になり、`not_applicable` の分岐にそもそも到達しないので、
    優先順位を入れ替える退行を捕まえられない（実測で素通りした）。
    ここでは**非取得系**（`llm_extract`）にコードを付け、取得系は判定で全 skip にする。
    退行の向きは「**実際に失敗した項目が分母から外れる**」＝困難度を過小に見せる側。
    """
    trace = [
        {"op": "compare", "id": "cond", "output": False},
        {"op": "sql_extract", "id": "a", "guard": "$cond", "skipped": True},
        # 非取得系の失敗（guard 無しで実行され、空出力でコードが付いた）
        {"op": "llm_extract", "id": "e", "failure_code": FailureCode.EXTRACTION_LOW_CONFIDENCE},
    ]
    # **前提**: not_applicable の条件は満たしている（この形でのみ優先順位の競合が起きる）
    assert _all_data_steps_skipped(trace)
    assert _skips_are_judged(trace, [e for e in trace if not e.get("skipped")])
    assert derive_item_failure_code(trace) == FailureCode.EXTRACTION_LOW_CONFIDENCE


# ------------------------------------------------------------- 実行（executor）


@pytest.mark.asyncio
async def test_guard_false_skips_step_without_executing(db_session: AsyncSession) -> None:
    """guard が偽の step は実行されない。存在しない表を引く SQL でもエラーにならない。

    実行されていれば「no such table」で failure_code=schema_mismatch が付くため、
    エラーが無いこと自体が「実行されなかった」ことの証拠になる。
    """
    program = {
        "steps": [
            {"op": "compare", "id": "cond", "left": 1, "cmp": "==", "right": 2},  # False
            {
                "op": "sql_extract",
                "id": "x",
                "guard": "$cond",
                "sql": "SELECT v FROM table_that_does_not_exist WHERE patient_id = :patient_id",
            },
        ],
        "return": "$x",
    }
    interp = DSLInterpreter()
    prefill, evidence = await interp.execute([_plan("skipped_item", program)], "P1", db_session)

    assert "skipped_item" not in prefill
    trace = evidence["skipped_item"]["trace"]
    by_id = {t["id"]: t for t in trace}
    assert by_id["x"]["skipped"] is True
    assert by_id["x"]["output"] is None
    # 監査証跡には guard の「式」を残す（解決後の値は参照先 step の output に既にある）
    assert by_id["x"]["guard"] == "$cond"
    # skip は失敗ではないので step にも failure_code / error を付けない
    assert "failure_code" not in by_id["x"]
    assert "error" not in by_id["x"]
    # 実行されていれば所在同定の誤り（schema_mismatch）になるはずのプラン
    assert evidence["skipped_item"].get("failure_code") != FailureCode.SCHEMA_MISMATCH
    # 実行済み step には skip 印を付けない
    assert not by_id["cond"].get("skipped")


@pytest.mark.asyncio
async def test_guard_true_executes_step_and_records_guard_value(
    db_session: AsyncSession,
) -> None:
    """guard が真なら通常どおり実行され、判定に使った値が監査証跡に残る。"""
    await db_session.execute(text("CREATE TABLE guard_labs (patient_id TEXT, v REAL)"))
    await db_session.execute(text("INSERT INTO guard_labs VALUES (:p, :v)"), {"p": "P1", "v": 7.2})
    await db_session.commit()

    program = {
        "steps": [
            {"op": "compare", "id": "cond", "left": 1, "cmp": "==", "right": 1},  # True
            {
                "op": "sql_extract",
                "id": "x",
                "guard": "$cond",
                "sql": "SELECT v FROM guard_labs WHERE patient_id = :patient_id",
            },
        ],
        "return": "$x",
    }
    interp = DSLInterpreter()
    prefill, evidence = await interp.execute([_plan("guarded", program)], "P1", db_session)

    assert prefill["guarded"] == 7.2
    by_id = {t["id"]: t for t in evidence["guarded"]["trace"]}
    assert by_id["x"]["skipped"] is False
    assert by_id["x"]["guard"] == "$cond"
    assert by_id["x"]["output"] == 7.2


@pytest.mark.asyncio
async def test_skip_propagates_as_none_to_dependent_steps(db_session: AsyncSession) -> None:
    """skip された step を guard に使う後続 step も（None は偽なので）skip される。"""
    program = {
        "steps": [
            {"op": "compare", "id": "cond", "left": 1, "cmp": "==", "right": 2},  # False
            {"op": "llm_judge", "id": "j", "guard": "$cond", "prompt": "所見あり？"},
            {"op": "sql_extract", "id": "x", "guard": "$j", "sql": "SELECT 1"},
        ],
        "return": "$x",
    }
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response("はい"))
    _, evidence = await interp.execute([_plan("chain", program)], "P1", db_session)

    by_id = {t["id"]: t for t in evidence["chain"]["trace"]}
    assert by_id["j"]["skipped"] is True
    assert by_id["x"]["skipped"] is True
    # skip された llm_judge は LLM を呼んでいない
    interp._ollama_client.chat.assert_not_awaited()


@pytest.mark.asyncio
async def test_guard_prevents_access_on_unchosen_source(db_session: AsyncSession) -> None:
    """条件で取得元が変わるプラン: 採用されない側の外部アクセスと LLM 呼び出しが起きない。

    「入院中なら病院DB、外来なら FHIR」を判定 → guard 付き取得 → branch で値選択の順に
    書いたとき、外来側（FHIR）には一切アクセスしない。branch だけで書いた従来の書き方では
    両方の取得が実行されてしまう。
    """
    interp = DSLInterpreter()
    # llm_judge は「はい」→ 入院中（True）。LLM 呼び出しはこの判定の1回だけであるべき。
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response("はい"))

    async def _fake_external(step: dict, bind_value: str, base: dict, **kwargs: Any) -> tuple:
        # context / binder がキーワード引数として渡るので受け取る。
        return "病院DBの値", {**base, "output": "病院DBの値"}

    interp._op_external_db = AsyncMock(side_effect=_fake_external)  # type: ignore[method-assign]
    interp._op_fhir = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("採用されない側の FHIR アクセスが発生した")
    )

    program = {
        "steps": [
            {"op": "llm_judge", "id": "is_inpatient", "prompt": "患者 {{patient_id}} は入院中か"},
            {
                "op": "external_db_extract",
                "id": "from_hosp",
                "guard": "$is_inpatient",
                "connection_id": "hospital",
                "sql": "SELECT v FROM t WHERE id = :patient_id",
            },
            {
                "op": "compare",
                "id": "is_outpatient",
                "left": "$is_inpatient",
                "cmp": "==",
                "right": False,
            },
            {
                "op": "fhir_extract",
                "id": "from_fhir",
                "guard": "$is_outpatient",
                "fhir_query": "/Observation?patient={{patient_id}}",
            },
            {
                "op": "branch",
                "id": "value",
                "when": "$is_inpatient",
                "then": "$from_hosp",
                "else_branch": "$from_fhir",
            },
        ],
        "return": "$value",
    }
    prefill, evidence = await interp.execute([_plan("source_switch", program)], "P1", db_session)

    assert prefill["source_switch"] == "病院DBの値"
    # 採用された側だけが実行された
    assert interp._op_external_db.await_count == 1
    assert interp._op_fhir.await_count == 0
    # LLM 呼び出しは判定の1回だけ（不要な LLM 呼び出しが起きていない）
    assert interp._ollama_client.chat.await_count == 1

    by_id = {t["id"]: t for t in evidence["source_switch"]["trace"]}
    assert by_id["from_fhir"]["skipped"] is True
    assert by_id["from_hosp"]["skipped"] is False
    assert by_id["value"]["taken"] == "then"
    # 採用されなかった側の生データはトレースに残らない
    assert by_id["from_fhir"]["output"] is None
    # 値が得られた項目なので failure_code は付かない
    assert "failure_code" not in evidence["source_switch"]


@pytest.mark.asyncio
async def test_all_steps_skipped_is_needs_input(db_session: AsyncSession) -> None:
    """全 step が guard 偽なら自動取得を試みていないので項目は needs_input。"""
    program = {
        "steps": [{"op": "sql_extract", "id": "x", "guard": False, "sql": "SELECT 1"}],
        "return": "$x",
    }
    interp = DSLInterpreter()
    _, evidence = await interp.execute([_plan("nothing", program)], "P1", db_session)

    assert evidence["nothing"]["failure_code"] == FailureCode.NEEDS_INPUT
    assert evidence["nothing"]["trace"][0]["skipped"] is True


@pytest.mark.asyncio
async def test_program_without_guard_is_unchanged(db_session: AsyncSession) -> None:
    """guard を持たない既存プランには skip 印を付けない（既存プランへの回帰防止）。"""
    program = {
        "steps": [{"op": "compare", "id": "c", "left": 2, "cmp": ">", "right": 1}],
        "return": "$c",
    }
    interp = DSLInterpreter()
    prefill, evidence = await interp.execute([_plan("plain", program)], "P1", db_session)

    assert prefill["plain"] is True
    entry = evidence["plain"]["trace"][0]
    assert "skipped" not in entry
    assert "guard" not in entry


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_guard_of_treats_blank_as_unset(blank: str | None) -> None:
    """空文字・空白のみの guard は「未指定＝無条件実行」として扱う。

    コンパイラの生成物が `"guard": ""` になったとき、条件を意図していない step が
    黙って全件スキップされ（取得が静かに止まり）ないようにするための安全側の既定。
    """
    assert guard_of({"op": "sql_extract", "id": "x", "guard": blank}) is None


@pytest.mark.asyncio
async def test_blank_guard_executes_unconditionally(db_session: AsyncSession) -> None:
    """guard が空文字の step は無条件実行され、skip 印も付かない。"""
    program = {
        "steps": [{"op": "compare", "id": "c", "guard": "", "left": 2, "cmp": ">", "right": 1}],
        "return": "$c",
    }
    interp = DSLInterpreter()
    prefill, evidence = await interp.execute([_plan("blank_guard", program)], "P1", db_session)

    assert prefill["blank_guard"] is True
    assert "skipped" not in evidence["blank_guard"]["trace"][0]


@pytest.mark.asyncio
async def test_bool_literal_guard_executes(db_session: AsyncSession) -> None:
    """真偽リテラル true を guard に書いた step は実行される。"""
    program = {
        "steps": [{"op": "compare", "id": "c", "guard": True, "left": 2, "cmp": ">", "right": 1}],
        "return": "$c",
    }
    interp = DSLInterpreter()
    prefill, evidence = await interp.execute([_plan("literal_guard", program)], "P1", db_session)

    assert prefill["literal_guard"] is True
    assert evidence["literal_guard"]["trace"][0]["skipped"] is False


@pytest.mark.asyncio
async def test_empty_multi_result_guard_is_false(db_session: AsyncSession) -> None:
    """0 件の multi 抽出結果（空リスト）を guard にすると偽＝後続は実行されない。

    `_truthy([])` は `str([]) == "[]"` 経由で True になる fail-open な既定なので、
    guard では空値（None / "" / 空リスト）を先に偽と判定する。これが無いと
    「行が取れたら LLM 抽出する」という自然な書き方で 0 件でも LLM が呼ばれ、
    guard で消したい「不要な LLM 呼び出し」がそのまま残る。
    """
    await db_session.execute(text("CREATE TABLE guard_rows (patient_id TEXT, v REAL)"))
    await db_session.commit()

    program = {
        "steps": [
            {
                "op": "sql_extract",
                "id": "rows",
                "multi": True,
                "sql": "SELECT v FROM guard_rows WHERE patient_id = :patient_id",
            },
            {"op": "llm_extract", "id": "v", "guard": "$rows", "prompt": "$rows を要約せよ"},
        ],
        "return": "$v",
    }
    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(return_value=_chat_response("要約"))
    _, evidence = await interp.execute([_plan("empty_rows", program)], "P1", db_session)

    by_id = {t["id"]: t for t in evidence["empty_rows"]["trace"]}
    assert by_id["rows"]["output"] == []
    assert by_id["v"]["skipped"] is True
    interp._ollama_client.chat.assert_not_awaited()


def test_legacy_trace_without_skipped_key_is_treated_as_executed() -> None:
    """`skipped` を持たない旧 evidence は従来どおり「実行済み」として解釈される（互換性）。

    evidence は JSON カラムでありフィールド追加に migration が無いため、旧レコードの
    解釈が変わらないことを明示的に固定する。
    """
    legacy = [{"op": "fhir_extract", "id": "a", "failure_code": FailureCode.TIMEOUT}]
    assert derive_item_failure_code(legacy) == FailureCode.TIMEOUT
    assert derive_item_failure_code([{"op": "branch", "output": None}]) == FailureCode.UNCLASSIFIED


@pytest.mark.asyncio
async def test_recommended_shape_with_all_sources_skipped(db_session: AsyncSession) -> None:
    """推奨形（判定 → guard 付き取得 → branch）で取得側が全 skip のときの分類を固定する。

    判定・分岐の step は実行され、取得系 step は全て skip される。これは取得の失敗ではなく
    **「患者の文脈上そもそも該当しない」**なので `not_applicable`。
    `not_applicable` を導入する前は `unclassified` に落ちており、取得困難度の分母に失敗として入っていた。

    **`needs_input` との境界は「判定が動いたか」**である（実行 step が皆無の形は
    `test_derive_item_failure_code_all_skipped_is_needs_input` /
    `test_all_steps_skipped_is_needs_input` が `needs_input` のまま固定している）。
    """
    program = {
        "steps": [
            {"op": "compare", "id": "is_a", "left": 1, "cmp": "==", "right": 2},  # False
            {"op": "sql_extract", "id": "from_a", "guard": "$is_a", "sql": "SELECT 1"},
            {"op": "compare", "id": "is_b", "left": 1, "cmp": "==", "right": 3},  # False
            {"op": "sql_extract", "id": "from_b", "guard": "$is_b", "sql": "SELECT 2"},
            {
                "op": "branch",
                "id": "value",
                "when": "$is_a",
                "then": "$from_a",
                "else_branch": "$from_b",
            },
        ],
        "return": "$value",
    }
    interp = DSLInterpreter()
    _, evidence = await interp.execute([_plan("none_apply", program)], "P1", db_session)

    by_id = {t["id"]: t for t in evidence["none_apply"]["trace"]}
    assert by_id["from_a"]["skipped"] is True
    assert by_id["from_b"]["skipped"] is True
    assert evidence["none_apply"]["value"] is None
    assert evidence["none_apply"]["failure_code"] == FailureCode.NOT_APPLICABLE
    # **判定 step は実行されている**（この形が needs_input でないことの根拠）
    assert "skipped" not in by_id["is_a"] and "skipped" not in by_id["is_b"]


# ------------------------------------------------------------------- 可視化


def test_mermaid_marks_skipped_node_and_guard_edge() -> None:
    """取得フロー図: skip 経路は破線グレーの「未実行」ノード、guard 依存は点線エッジ。"""
    program = {
        "steps": [
            {"op": "llm_judge", "id": "cond", "prompt": "入院中か"},
            {"op": "sql_extract", "id": "x", "guard": "$cond", "sql": "SELECT 1"},
        ],
        "return": "$x",
    }
    trace = [
        {"op": "llm_judge", "id": "cond", "output": False},
        {"op": "sql_extract", "id": "x", "guard": False, "output": None, "skipped": True},
    ]
    mermaid = program_to_mermaid(program, trace)

    assert "未実行" in mermaid
    assert 'n_cond -. "guard" .-> n_x' in mermaid
    assert "classDef skipped" in mermaid
    assert "class n_x skipped" in mermaid
    # guard は値が流れる依存ではないので実線のデータ依存エッジは描かない
    assert "n_cond --> n_x" not in mermaid


def test_mermaid_without_trace_has_no_skipped_class() -> None:
    """トレース未指定（設計時プレビュー）では skip 装飾を付けない。"""
    program = {
        "steps": [
            {"op": "llm_judge", "id": "cond", "prompt": "入院中か"},
            {"op": "sql_extract", "id": "x", "guard": "$cond", "sql": "SELECT 1"},
        ],
        "return": "$x",
    }
    mermaid = program_to_mermaid(program)
    assert "classDef skipped" not in mermaid
    assert 'n_cond -. "guard" .-> n_x' in mermaid
