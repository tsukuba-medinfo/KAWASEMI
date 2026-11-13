"""取得困難度メトリクスのテスト

- classify_exception / derive_item_failure_code の分類規約
- executor が evidence（step トレース・項目単位）へ failure_code を付与すること
- acquisition_summary の集計（自動充足率・失敗内訳・旧レコード unclassified・人手介入）
- 集計ページ / CSV エクスポートの権限と PHI 非含有（患者値・patient_id を出さない）
"""

from __future__ import annotations

from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.executor import (
    DSLInterpreter,
    classify_exception,
    derive_item_failure_code,
    is_empty_value,
)
from kawasemi.ai_input.metrics import (
    _DENOMINATOR_EXCLUDED_CODES,
    acquisition_summary,
    item_failure_code,
)
from kawasemi.ai_input.models import (
    ExecutionStatus,
    FailureCode,
    InputExecution,
    InputPlan,
    PlanStatus,
)
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.questionnaire_response import QuestionnaireResponse

# 集計面に漏れてはならない値（PHI 検査用の目印）
SECRET_PATIENT_ID = "PAT-SECRET-001"
SECRET_VALUE = "SECRET-VALUE-42"


# =============================================================================
# 分類ヘルパ
# =============================================================================


def test_classify_exception_timeout() -> None:
    assert classify_exception(TimeoutError()) == FailureCode.TIMEOUT
    assert classify_exception(Exception("connection timed out")) == FailureCode.TIMEOUT


def test_classify_exception_schema() -> None:
    assert classify_exception(Exception("no such table: labo")) == FailureCode.SCHEMA_MISMATCH
    assert (
        classify_exception(Exception('relation "labo" does not exist'))
        == FailureCode.SCHEMA_MISMATCH
    )


def test_classify_exception_unavailable() -> None:
    assert classify_exception(ConnectionError("refused")) == FailureCode.SOURCE_UNAVAILABLE
    assert (
        classify_exception(Exception("Connection refused by host"))
        == FailureCode.SOURCE_UNAVAILABLE
    )
    assert (
        classify_exception(Exception("password authentication failed"))
        == FailureCode.SOURCE_UNAVAILABLE
    )


def test_classify_exception_does_not_exist_is_not_schema() -> None:
    """FHIR 等の「リソースが存在しない」は schema_mismatch に誤分類しない（回帰）。

    schema 判定は表・カラムを示す語を伴う形（relation "/column " 等）に限定し、
    素の "does not exist" では発火しない。
    """
    e = Exception("Resource Patient/xyz does not exist")
    assert (
        classify_exception(e, default=FailureCode.SOURCE_UNAVAILABLE)
        == FailureCode.SOURCE_UNAVAILABLE
    )
    assert classify_exception(e) == FailureCode.UNCLASSIFIED


def test_is_empty_value() -> None:
    """不充足の空値定義: None・空文字・空リスト。0 / False は値として有効。"""
    assert is_empty_value(None)
    assert is_empty_value("")
    assert is_empty_value([])
    assert not is_empty_value(0)
    assert not is_empty_value(False)
    assert not is_empty_value("0")


def test_item_failure_code_empty_values_not_filled() -> None:
    """空文字・空リストは充足に数えない（step 分類と項目成否の整合）。"""
    assert (
        item_failure_code({"value": "", "failure_code": FailureCode.EXTRACTION_LOW_CONFIDENCE})
        == FailureCode.EXTRACTION_LOW_CONFIDENCE
    )
    assert item_failure_code({"value": []}) == FailureCode.UNCLASSIFIED
    assert item_failure_code({"value": "実値"}) is None


def test_classify_exception_unclassified_and_default() -> None:
    assert classify_exception(Exception("なにか変")) == FailureCode.UNCLASSIFIED
    assert (
        classify_exception(Exception("なにか変"), default=FailureCode.SOURCE_UNAVAILABLE)
        == FailureCode.SOURCE_UNAVAILABLE
    )


def test_derive_item_failure_code() -> None:
    # 空トレース（manual 中心: steps 空 + return:null）は needs_input
    assert derive_item_failure_code([]) == FailureCode.NEEDS_INPUT
    assert derive_item_failure_code(None) == FailureCode.NEEDS_INPUT
    # 最初に failure_code が付いた step が根本原因（失敗は後続へ連鎖するため）
    trace = [
        {"op": "fhir_extract", "failure_code": FailureCode.TIMEOUT},
        {"op": "llm_extract", "failure_code": FailureCode.EXTRACTION_LOW_CONFIDENCE},
    ]
    assert derive_item_failure_code(trace) == FailureCode.TIMEOUT
    # コード無しのトレースのみなら unclassified
    assert derive_item_failure_code([{"op": "branch", "output": None}]) == FailureCode.UNCLASSIFIED


# =============================================================================
# executor の failure_code 付与
# =============================================================================


def _plan(qid: int, link_id: str, program: dict) -> InputPlan:
    return InputPlan(
        questionnaire_id=qid,
        link_id=link_id,
        item_text=link_id,
        program=program,
        status=PlanStatus.CONFIRMED,
    )


async def test_executor_attaches_failure_codes(db_session: AsyncSession) -> None:
    q = Questionnaire(
        fhir_id="q-metrics-exec",
        title="メトリクス実行テスト",
        status="active",
        content={"resourceType": "Questionnaire", "id": "q-metrics-exec", "item": []},
    )
    db_session.add(q)
    await db_session.flush()
    # 臨床データの表（アプリ自身の表は sql_extract から読めない）。行は無い
    await db_session.execute(text("DROP TABLE IF EXISTS clinical_t"))
    await db_session.execute(text("CREATE TABLE clinical_t (pid TEXT, v TEXT)"))

    plans = [
        # 実在しない表 → 例外 "no such table" → schema_mismatch
        _plan(
            q.id,
            "bad_table",
            {
                "steps": [{"op": "sql_extract", "id": "s1", "sql": "SELECT x FROM no_such_tbl"}],
                "return": "$s1",
            },
        ),
        # 実在する表・当該患者の行なし → not_found_for_patient
        _plan(
            q.id,
            "no_row",
            {
                "steps": [
                    {
                        "op": "sql_extract",
                        "id": "s1",
                        "sql": "SELECT v FROM clinical_t WHERE pid = :patient_id",
                    }
                ],
                "return": "$s1",
            },
        ),
        # 未確定 op のスキップ → needs_input
        _plan(
            q.id,
            "pending",
            {
                "steps": [
                    {"op": "sql_extract", "id": "s1", "sql": "SELECT 1", "needs_input": True}
                ],
                "return": "$s1",
            },
        ),
        # manual 中心（steps 空・return:null）→ needs_input
        _plan(q.id, "manual_only", {"steps": [], "return": None}),
    ]
    db_session.add_all(plans)
    await db_session.commit()

    interpreter = DSLInterpreter()
    prefill, evidence = await interpreter.execute(plans, "PAT-X", db_session)

    assert prefill == {}
    assert evidence["bad_table"]["failure_code"] == FailureCode.SCHEMA_MISMATCH
    assert evidence["bad_table"]["trace"][0]["failure_code"] == FailureCode.SCHEMA_MISMATCH
    assert evidence["no_row"]["failure_code"] == FailureCode.NOT_FOUND_FOR_PATIENT
    assert evidence["pending"]["failure_code"] == FailureCode.NEEDS_INPUT
    assert evidence["manual_only"]["failure_code"] == FailureCode.NEEDS_INPUT


# =============================================================================
# 集計サービス
# =============================================================================


async def _make_questionnaire(db: AsyncSession) -> Questionnaire:
    q = Questionnaire(
        fhir_id="q-metrics",
        title="メトリクステスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-metrics",
            "item": [
                {"linkId": "item1", "type": "string", "text": "項目1"},
                {"linkId": "item2", "type": "string", "text": "項目2"},
            ],
        },
    )
    db.add(q)
    await db.flush()
    return q


async def _seed_metrics_data(db: AsyncSession) -> Questionnaire:
    """新形式1件（P1）＋ 旧形式（コード無し）1件（P2）＋ P1 の回答を投入する。"""
    q = await _make_questionnaire(db)
    db.add(
        InputPlan(
            questionnaire_id=q.id,
            link_id="item1",
            item_text="項目1",
            program={"steps": [], "return": None},
            status=PlanStatus.CONFIRMED,
        )
    )
    # 新形式: item1 は充足（ただし回答で修正される）、item2 は schema_mismatch で失敗
    db.add(
        InputExecution(
            questionnaire_id=q.id,
            patient_id=SECRET_PATIENT_ID,
            prefill_data={"item1": SECRET_VALUE},
            evidence={
                "item1": {
                    "trace": [{"op": "sql_extract", "output": SECRET_VALUE}],
                    "value": SECRET_VALUE,
                },
                "item2": {
                    "trace": [
                        {
                            "op": "sql_extract",
                            "error": "no such table: labo",
                            "failure_code": FailureCode.SCHEMA_MISMATCH,
                        }
                    ],
                    "value": None,
                    "error": "no such table: labo",
                    "failure_code": FailureCode.SCHEMA_MISMATCH,
                },
                "_meta": {"acted_as": "collector", "executed_by": "テスト"},
            },
            status=ExecutionStatus.PARTIAL,
        )
    )
    # 旧形式（FailureCode 導入前）: failure_code も value も無い evidence
    db.add(
        InputExecution(
            questionnaire_id=q.id,
            patient_id="PAT-LEGACY-002",
            prefill_data={},
            evidence={"item2": {"source_type": "fhir", "query_used": "/Observation?x=1"}},
            status=ExecutionStatus.FAILED,
        )
    )
    # P1 の回答: item1 は autofill 値から修正、item2 は人手入力
    db.add(
        QuestionnaireResponse(
            fhir_id="qr-metrics-1",
            questionnaire_id=q.id,
            content={
                "resourceType": "QuestionnaireResponse",
                "item": [
                    {"linkId": "item1", "answer": [{"valueString": "人手修正値"}]},
                    {"linkId": "item2", "answer": [{"valueString": "人手入力値"}]},
                ],
            },
            status="completed",
            subject=f"Patient/{SECRET_PATIENT_ID}",
        )
    )
    await db.commit()
    return q


async def test_acquisition_summary_aggregates(db_session: AsyncSession) -> None:
    q = await _seed_metrics_data(db_session)

    summary = await acquisition_summary(db_session, q.id)

    assert summary.total_executions == 2
    assert summary.compared_responses == 1  # P1 のみ回答あり

    by_link = {m.link_id: m for m in summary.items}
    item1, item2 = by_link["item1"], by_link["item2"]

    assert item1.executions == 1
    assert item1.filled == 1
    assert item1.fill_rate == 1.0
    assert item1.manual_corrected == 1  # SECRET_VALUE → 人手修正値
    assert item1.intervention_rate == 1.0
    assert item1.item_text == "項目1"  # InputPlan からのラベル解決

    assert item2.executions == 2
    assert item2.filled == 0
    assert item2.fill_rate == 0.0
    # 新形式は schema_mismatch、旧形式（コード無し）は unclassified として壊れず集計
    assert item2.failure_counts == {
        FailureCode.SCHEMA_MISMATCH: 1,
        FailureCode.UNCLASSIFIED: 1,
    }
    assert item2.manual_entered == 1  # autofill 空振り → 人手入力


async def test_acquisition_summary_failed_without_evidence(db_session: AsyncSession) -> None:
    """resolver 不通など evidence を残さない実行全体失敗は、消えずに別掲される。"""
    q = await _seed_metrics_data(db_session)
    db_session.add(
        InputExecution(
            questionnaire_id=q.id,
            patient_id="PAT-RESOLVER-DOWN",
            prefill_data={},
            evidence={},
            status=ExecutionStatus.FAILED,
            error_summary="resolver 到達不能",
        )
    )
    await db_session.commit()

    summary = await acquisition_summary(db_session, q.id)
    assert summary.failed_without_evidence == 1
    assert summary.total_executions == 3
    # 項目統計の分母は変わらない（evidence が無いので項目に現れない）
    by_link = {m.link_id: m for m in summary.items}
    assert by_link["item2"].executions == 2


def _item_ev(value: object, code: str | None = None) -> dict[str, object]:
    """項目 evidence（trace は集計側が見ないので最小形）。"""
    ev: dict[str, object] = {"trace": [], "value": value}
    if code is not None:
        ev["failure_code"] = code
    return ev


async def _add_execution(
    db: AsyncSession, q: Questionnaire, patient_id: str, items: dict[str, dict[str, object]]
) -> None:
    db.add(
        InputExecution(
            questionnaire_id=q.id,
            patient_id=patient_id,
            prefill_data={},
            evidence=items,
            status=ExecutionStatus.PARTIAL,
        )
    )
    await db.commit()


async def test_not_applicable_is_excluded_from_the_denominator(db_session: AsyncSession) -> None:
    """`not_applicable` を自動充足率の**分母から除く**こと。

    「該当しないので取得しなくて正しい」を分母に入れると困難度が過小に見える。**分母を
    `executions` にしたままだと 1/3、除けば 1/2** になる規模のフィクスチャで固定する
    （件数を揃えると、除いているかどうかを率から区別できない）。
    """
    q = await _seed_metrics_data(db_session)
    await _add_execution(db_session, q, "PAT-NA-1", {"item3": _item_ev("値あり")})
    await _add_execution(
        db_session, q, "PAT-NA-2", {"item3": _item_ev(None, FailureCode.SCHEMA_MISMATCH)}
    )
    await _add_execution(
        db_session, q, "PAT-NA-3", {"item3": _item_ev(None, FailureCode.NOT_APPLICABLE)}
    )

    summary = await acquisition_summary(db_session, q.id)
    item3 = {m.link_id: m for m in summary.items}["item3"]

    assert item3.executions == 3, "実行回数そのものは変わらない"
    assert item3.not_applicable == 1, "除いた件数を別掲する（黙って消さない）"
    assert item3.denominator == 2, "分母は実行回数 − 該当なし"
    assert item3.fill_rate == 0.5, "分母を executions にしていると 1/3 になる"
    # **内訳には残す**（分母から除くだけ。内訳から消すと会計が閉じない）
    assert item3.failure_counts == {
        FailureCode.SCHEMA_MISMATCH: 1,
        FailureCode.NOT_APPLICABLE: 1,
    }
    # 会計: 充足 + 失敗内訳の合計 == 実行回数、かつ 分母 == 実行回数 − 該当なし
    assert item3.filled + sum(item3.failure_counts.values()) == item3.executions
    assert item3.denominator == item3.executions - item3.not_applicable


async def test_an_item_that_never_applies_has_no_fill_rate(db_session: AsyncSession) -> None:
    """該当しない実行しか無い項目は率を**算出しない**こと。

    分母が 0 なので率は存在しない。0% として出すと「取得に失敗し続けている項目」と
    区別できなくなる。
    """
    q = await _seed_metrics_data(db_session)
    await _add_execution(
        db_session, q, "PAT-NA-4", {"item4": _item_ev(None, FailureCode.NOT_APPLICABLE)}
    )
    await _add_execution(
        db_session, q, "PAT-NA-5", {"item4": _item_ev(None, FailureCode.NOT_APPLICABLE)}
    )

    item4 = {m.link_id: m for m in (await acquisition_summary(db_session, q.id)).items}["item4"]
    assert item4.executions == 2
    assert item4.denominator == 0
    assert item4.fill_rate is None, "分母 0 で率を出してはいけない"


def test_only_not_applicable_is_excluded_from_the_denominator() -> None:
    """**分母から除く語彙は `not_applicable` の1件だけ**であること。

    集計テストで触れるのは実際に置いたコードだけなので、**残りのコードを除外語彙へ足しても
    挙動テストは通ってしまう**（実測で `needs_input` を足すと素通りした）。
    語彙そのものを固定する。
    """
    assert _DENOMINATOR_EXCLUDED_CODES == frozenset({FailureCode.NOT_APPLICABLE})
    for code in FailureCode.ALL:
        if code != FailureCode.NOT_APPLICABLE:
            assert code not in _DENOMINATOR_EXCLUDED_CODES, f"{code} を分母から除いてはいけない"


async def test_other_failure_codes_stay_in_the_denominator(db_session: AsyncSession) -> None:
    """**除くのは `not_applicable` だけ**であること。

    除外を失敗コード全般へ広げると、取得できなかった項目が分母から消えて困難度が 0 に近づく。
    """
    q = await _seed_metrics_data(db_session)
    item2 = {m.link_id: m for m in (await acquisition_summary(db_session, q.id)).items}["item2"]
    assert item2.executions == 2
    assert item2.not_applicable == 0
    assert item2.denominator == 2, "schema_mismatch と unclassified は分母に残る"
    assert item2.fill_rate == 0.0


async def test_an_execution_without_item_evidence_is_only_counted_when_it_failed(
    db_session: AsyncSession,
) -> None:
    """項目 evidence の無い実行を別掲するのは**失敗した実行だけ**であること。

    別掲（`failed_without_evidence`）は「resolver 不通など実行前提の障害」を数えるものなので、
    正常終了したのに項目 evidence が無い実行（対象0件など）を混ぜてはいけない。この形の
    フィクスチャが無く、status の条件を落とすミューテーションが素通りしていた。
    """
    q = await _seed_metrics_data(db_session)
    db_session.add(
        InputExecution(
            questionnaire_id=q.id,
            patient_id="PAT-NO-EVIDENCE-OK",
            prefill_data={},
            evidence={},
            status=ExecutionStatus.SUCCESS,
        )
    )
    await db_session.commit()

    summary = await acquisition_summary(db_session, q.id)
    assert summary.total_executions == 3
    assert summary.failed_without_evidence == 0, "失敗していない実行を別掲に数えてはいけない"


async def test_the_intervention_rate_denominator_keeps_not_applicable(
    db_session: AsyncSession,
) -> None:
    """人手介入率の分母は `not_applicable` を**除かない**こと。

    自動充足率とは分母が違う。理由はこうである:
    **該当しない項目に人が値を入れたら、それは「判定が誤っていた」ことの手がかりで介入である**
    （分子に入る）。分子に残したまま分母から除くと**率が 1 を超えうる**。分子からも除くと
    その手がかりが消える。よって分母に残し、**介入率が下振れすることは限界として受け入れる**。

    この選択を固定しておかないと、その逆（`fill_rate` と揃える）へ黙って変わっても気づけない。
    """
    q = await _seed_metrics_data(db_session)
    # item2 は P1 で人手入力されている（既存フィクスチャ）。そこへ「該当しない」実行を足す
    await _add_execution(
        db_session, q, SECRET_PATIENT_ID, {"item2": _item_ev(None, FailureCode.NOT_APPLICABLE)}
    )

    item2 = {m.link_id: m for m in (await acquisition_summary(db_session, q.id)).items}["item2"]
    assert item2.not_applicable == 1
    assert item2.denominator == 2, "自動充足率の分母からは除く"
    assert item2.responses_compared == 1, "人手介入率の分母は突合できた回数のまま（除かない）"
    assert item2.interventions <= item2.responses_compared, "率は 1 を超えない"


async def test_acquisition_summary_period_filter(db_session: AsyncSession) -> None:
    q = await _seed_metrics_data(db_session)
    future = datetime(2099, 1, 1, tzinfo=UTC)
    summary = await acquisition_summary(db_session, q.id, date_from=future)
    assert summary.total_executions == 0
    assert summary.items == []


# =============================================================================
# エンドポイント（権限・PHI 非含有）
# =============================================================================


async def test_metrics_page_allows_data_user(
    reviewer_client: AsyncClient, db_session: AsyncSession
) -> None:
    q = await _seed_metrics_data(db_session)
    res = await reviewer_client.get(f"/ai-input/metrics/acquisition?questionnaire_id={q.id}")
    assert res.status_code == 200
    assert "取得困難度メトリクス" in res.text
    # PHI 非含有: 患者値・patient_id を集計面に出さない
    assert SECRET_PATIENT_ID not in res.text
    assert SECRET_VALUE not in res.text


async def test_metrics_page_shows_the_denominator_and_the_exclusion(
    reviewer_client: AsyncClient, db_session: AsyncSession
) -> None:
    """画面が**分母**と「該当なし N件を除外」を出すこと。

    集計側で除いていても画面へ繋がっていなければ、除外が黙って起きたのと同じである
    （`metrics._DENOMINATOR_EXCLUDED_CODES` のコメントが主張している性質）。CSV にはこの結線の
    テストがあったが、**画面には1本も無かった**。

    **見出しとセルの個数が一致していること**も見る。`<th>` を1つ落とすと、以降の列が1つずれた
    見出しの下に描画される——値は正しいのに**全部別の意味で読まれる**ので、出ていないより悪い。
    """
    q = await _seed_metrics_data(db_session)
    await _add_execution(db_session, q, "PAT-PAGE-1", {"item3": _item_ev("値あり")})
    await _add_execution(
        db_session, q, "PAT-PAGE-2", {"item3": _item_ev(None, FailureCode.SCHEMA_MISMATCH)}
    )
    await _add_execution(
        db_session, q, "PAT-PAGE-3", {"item3": _item_ev(None, FailureCode.NOT_APPLICABLE)}
    )

    body = (
        await reviewer_client.get(f"/ai-input/metrics/acquisition?questionnaire_id={q.id}")
    ).text
    assert "<th>分母</th>" in body, "分母の見出しが無い"
    assert "該当なし 1件を除外" in body, "何件除いたかが画面に出ていない"
    assert FailureCode.LABELS[FailureCode.NOT_APPLICABLE] in body, "内訳のラベルが出ていない"

    # 見出しとセルの個数が一致すること（1列でもずれると全列の意味がずれる）
    header_row = body.split("<thead>", 1)[1].split("</thead>", 1)[0]
    headers = [h.split("</th>")[0].strip() for h in header_row.split("<th>")[1:]]
    rows = body.split("<tbody>", 1)[1].split("</tbody>", 1)[0].split("<tr>")[1:]
    for row in rows:
        assert len(row.split("<td>")[1:]) == len(headers) > 0

    # **「分母」の下に出ている数が `denominator` であること。** 見出しがあっても、値が
    # `executions` に差し替わっていたら誰も気づかない（実測で素通りした）
    item3_row = next(r for r in rows if "item3" in r)
    cells = dict(
        zip(headers, [c.split("</td>")[0] for c in item3_row.split("<td>")[1:]], strict=True)
    )
    assert cells["実行"].strip() == "3"
    assert cells["分母"].strip().startswith("2"), "分母の欄に executions が出ている"
    assert "該当なし 1件を除外" in cells["分母"]
    assert "50.0%" in cells["自動充足率"], "率が分母 2 で計算されていない"


async def test_metrics_page_allows_verifier(
    reviewer_client: AsyncClient, db_session: AsyncSession
) -> None:
    res = await reviewer_client.get("/ai-input/metrics/acquisition")
    assert res.status_code == 200


async def test_metrics_page_allows_collection_lead(
    designer_client: AsyncClient, db_session: AsyncSession
) -> None:
    """設計3ロール保有ユーザーの既定アクティブ役割は collection_lead（PRIORITY 先頭）。"""
    res = await designer_client.get("/ai-input/metrics/acquisition")
    assert res.status_code == 200


async def test_metrics_page_denies_data_entry(
    collector_client: AsyncClient, db_session: AsyncSession
) -> None:
    res = await collector_client.get("/ai-input/metrics/acquisition")
    assert res.status_code == 403


async def test_metrics_page_requires_auth(client: AsyncClient) -> None:
    res = await client.get("/ai-input/metrics/acquisition")
    assert res.status_code == 401


async def test_metrics_csv_export_phi_free(
    reviewer_client: AsyncClient, db_session: AsyncSession
) -> None:
    q = await _seed_metrics_data(db_session)
    res = await reviewer_client.get(f"/ai-input/metrics/acquisition.csv?questionnaire_id={q.id}")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    assert "attachment" in res.headers["content-disposition"]

    body = res.text
    assert body.startswith("\ufeff")  # Excel 用 UTF-8 BOM
    lines = [ln for ln in body.strip().splitlines() if ln]
    header = lines[0].lstrip("\ufeff").split(",")
    assert header[0] == "link_id"
    assert FailureCode.SCHEMA_MISMATCH in header
    # ヘッダ + item1 + item2 + 母集団の併記（**末尾に置く**——先頭に置くと
    # ヘッダ行が2行目になり素朴なパーサが壊れる）
    assert len(lines) == 4
    assert lines[-1].startswith("# 母集団"), lines[-1]
    assert "取得困難度は confirmed に至った項目に限った条件付きの値" in lines[-1]

    # PHI 非含有: 患者値・patient_id が CSV に含まれない
    assert SECRET_PATIENT_ID not in body
    assert "PAT-LEGACY-002" not in body
    assert SECRET_VALUE not in body


async def test_metrics_csv_carries_the_denominator_and_not_applicable(
    reviewer_client: AsyncClient, db_session: AsyncSession
) -> None:
    """CSV が**分母そのもの**と `not_applicable` 列を運ぶこと。

    率だけを出すと、分母が何だったか（何件を除いたか）を読み手が復元できない。集計側で除いても
    出力へ繋がっていなければ記録には残らないので、**結線を単独で固定する**。
    """
    q = await _seed_metrics_data(db_session)
    await _add_execution(db_session, q, "PAT-CSV-1", {"item3": _item_ev("値あり")})
    await _add_execution(
        db_session, q, "PAT-CSV-2", {"item3": _item_ev(None, FailureCode.SCHEMA_MISMATCH)}
    )
    await _add_execution(
        db_session, q, "PAT-CSV-3", {"item3": _item_ev(None, FailureCode.NOT_APPLICABLE)}
    )

    res = await reviewer_client.get(f"/ai-input/metrics/acquisition.csv?questionnaire_id={q.id}")
    assert res.status_code == 200
    lines = [ln for ln in res.text.strip().splitlines() if ln]
    header = lines[0].lstrip("﻿").split(",")
    assert "denominator" in header
    assert FailureCode.NOT_APPLICABLE in header

    row = next(ln.split(",") for ln in lines[1:] if ln.startswith("item3"))
    cells = dict(zip(header, row, strict=True))
    assert cells["executions"] == "3"
    assert cells["denominator"] == "2", "除いた後の分母が CSV に出ていない"
    assert cells[FailureCode.NOT_APPLICABLE] == "1"
    assert cells["fill_rate"] == "0.5000"


async def test_metrics_csv_leaves_fill_rate_empty_when_the_denominator_is_zero(
    reviewer_client: AsyncClient, db_session: AsyncSession
) -> None:
    """分母 0 の項目の `fill_rate` が**空セル**であること。

    分母 0 の率を算出しないのは集計側の性質だが、CSV 側の結線にテストが無いと
    `0.0000` を出すミューテーションが素通りする。0 と読まれると
    「取得に失敗し続けている項目」に見える。
    """
    q = await _seed_metrics_data(db_session)
    await _add_execution(
        db_session, q, "PAT-CSV-NA", {"item4": _item_ev(None, FailureCode.NOT_APPLICABLE)}
    )

    res = await reviewer_client.get(f"/ai-input/metrics/acquisition.csv?questionnaire_id={q.id}")
    lines = [ln for ln in res.text.strip().splitlines() if ln]
    header = lines[0].lstrip("\ufeff").split(",")
    row = next(ln.split(",") for ln in lines[1:] if ln.startswith("item4"))
    cells = dict(zip(header, row, strict=True))
    assert cells["executions"] == "1"
    assert cells["denominator"] == "0"
    assert cells["fill_rate"] == "", "分母 0 で率を出してはいけない"


async def test_metrics_csv_denies_data_entry(
    collector_client: AsyncClient, db_session: AsyncSession
) -> None:
    q = await _seed_metrics_data(db_session)
    res = await collector_client.get(f"/ai-input/metrics/acquisition.csv?questionnaire_id={q.id}")
    assert res.status_code == 403


async def test_metrics_page_rejects_bad_date(
    reviewer_client: AsyncClient, db_session: AsyncSession
) -> None:
    q = await _seed_metrics_data(db_session)
    res = await reviewer_client.get(
        f"/ai-input/metrics/acquisition?questionnaire_id={q.id}&date_from=2026/01/01"
    )
    assert res.status_code == 400


# --- 母集団の縮みの併記 --------------------------------------------------------


async def test_the_summary_carries_the_population_shrink(db_session: AsyncSession) -> None:
    """集計が**対象項目数と confirmed 項目数**を持つこと。

    取得困難度は `InputExecution` を一次データにするが、autofill を実行できるのは
    `confirmed` になったプランだけである。**プランを確定できなかった項目——所在が分からない・
    SQL を書けない・所在候補が無い項目——は母集団から落ちる。それらは本来もっとも取得困難な
    項目**なので、集計は困難度を過小に見せる。

    測定器は変えない（比較の途中で測定器を変えない原則）。代わりに**縮みを数で併記する**——
    所在の確からしさの段階の集計（`coverage`）は対象項目の全件を分母にするので、**2つの分母が違うことが
    出力から分かる形**にする。

    ここでは対象項目2件のうち1件だけ `confirmed` にして、**縮みが1と出る**ことを見る。
    """
    numbers = ["1", "2"]  # 架空の項目番号（設定の無い様式は番号を持つ項目すべてが対象）
    q = Questionnaire(
        fhir_id="q-population",
        title="母集団テスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-population",
            "item": [
                {"linkId": f"t{n}", "type": "string", "text": f"[{n}] 対象項目{n}"} for n in numbers
            ],
        },
    )
    db_session.add(q)
    await db_session.flush()
    # 2件のうち1件だけプランを確定する（残りは母集団から落ちる項目）
    db_session.add(
        InputPlan(
            questionnaire_id=q.id,
            link_id=f"t{numbers[0]}",
            item_text=f"[{numbers[0]}] 対象項目",
            program={"steps": [], "return": None},
            status=PlanStatus.CONFIRMED,
        )
    )
    await db_session.commit()

    summary = await acquisition_summary(db_session, q.id)

    assert summary.target_item_count == 2, "対象項目の分母が問診票から数えられていない"
    assert summary.confirmed_item_count == 1
    assert summary.population_shrink == 1, "母集団の縮みが出ていない"


async def test_the_population_is_counted_from_the_questionnaire_not_from_plans(
    db_session: AsyncSession,
) -> None:
    """分母は**問診票の対象項目**から数えること。

    プラン側から数えると、**プランが無い項目こそ落ちている項目**なので縮みが見えなくなる
    （分母と分子が同じ集合になり、縮みが常に 0 になる）。
    """
    numbers = ["1", "2", "3"]
    q = Questionnaire(
        fhir_id="q-population-2",
        title="母集団テスト票2",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-population-2",
            "item": [
                {"linkId": f"t{n}", "type": "string", "text": f"[{n}] 対象項目{n}"} for n in numbers
            ],
        },
    )
    db_session.add(q)
    await db_session.flush()
    await db_session.commit()

    summary = await acquisition_summary(db_session, q.id)

    # プランが1件も無い＝全項目が母集団から落ちている
    assert summary.target_item_count == 3
    assert summary.confirmed_item_count == 0
    assert summary.population_shrink == 3


async def test_the_summary_counts_confirmed_empty_programs_inside_the_population(
    db_session: AsyncSession,
) -> None:
    """母集団のうち step が空のプランを**内数として別掲する**。

    空のプランは手入力への縮退という正当な形で、確定できる。確定すると
    `confirmed_item_count` に入るので、**母集団を「取得の設計ができた項目」と読むと実態より
    広く見える**。測定器の規則は変えない（母集団の数え方はそのまま）。内数を足すだけである。
    """
    numbers = ["1", "2", "3"]
    q = Questionnaire(
        fhir_id="q-population-empty",
        title="母集団（空プラン）テスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-population-empty",
            "item": [
                {"linkId": f"t{n}", "type": "string", "text": f"[{n}] 対象項目{n}"} for n in numbers
            ],
        },
    )
    db_session.add(q)
    await db_session.flush()
    programs = [
        ({"steps": [], "return": None}, PlanStatus.CONFIRMED),  # 空・確定 → 内数に入る
        (
            {"steps": [{"op": "sql_extract", "id": "s1", "sql": "SELECT 1"}], "return": "$s1"},
            PlanStatus.CONFIRMED,
        ),  # 空ではない・確定
        ({"steps": [], "return": None}, PlanStatus.DRAFT),  # 空だが未確定 → 母集団の外
    ]
    for n, (program, status) in zip(numbers, programs, strict=True):
        db_session.add(
            InputPlan(
                questionnaire_id=q.id,
                link_id=f"t{n}",
                item_text=f"[{n}] 対象項目",
                program=program,
                status=status,
            )
        )
    await db_session.commit()

    summary = await acquisition_summary(db_session, q.id)

    assert summary.target_item_count == 3
    assert summary.confirmed_item_count == 2, "母集団の数え方を変えてはいけない"
    assert summary.confirmed_empty_program_count == 1, "空の確定プランが内数に出ていない"
    assert summary.population_shrink == 1
