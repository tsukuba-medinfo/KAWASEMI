"""実行監査証跡への設計根拠(reasoning)反映とプログラムのバージョン固定

CSV（Computerized System Validation）/ ALCOA+ の観点で検証する:
- 正常実行 step の trace/evidence にも設計根拠(reasoning)が残ること（Complete）
- 取得フロー図(Mermaid)に設計根拠が実行事実と区別して注記されること
- 実行時に使用した program のスナップショット(reasoning込み)が固定保存され、
  確認書はそれを優先表示すること（Original・Consistent。確定後編集に影響されない）
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.executor import DSLInterpreter, program_snapshot
from kawasemi.ai_input.models import InputExecution, InputPlan
from kawasemi.ai_input.viz import program_to_mermaid
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.user import User


def _plan(link_id: str, program: dict) -> InputPlan:
    return InputPlan(questionnaire_id=1, link_id=link_id, program=program, status="confirmed")


# --------------------------------------------------------------------------- #
# executor: 正常実行 step の trace に reasoning が残る                          #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_reasoning_recorded_in_normal_step_trace(db_session: AsyncSession) -> None:
    """confirmed の正常実行 step（needs_input でない）の trace entry にも reasoning が載る。"""
    interp = DSLInterpreter()
    program = {
        "steps": [
            {
                "op": "transform",
                "id": "v",
                "reasoning": "施設コード M/F を表示名へ正規化する",
                "input": "M",
                "rule": {"type": "code_map", "map": {"M": "男性"}},
            }
        ],
        "return": "$v",
    }
    prefill, evidence = await interp.execute([_plan("gender", program)], "P1", db_session)

    assert prefill["gender"] == "男性"
    entry = evidence["gender"]["trace"][0]
    assert entry["reasoning"] == "施設コード M/F を表示名へ正規化する"


@pytest.mark.asyncio
async def test_reasoning_key_is_none_when_absent(db_session: AsyncSession) -> None:
    """reasoning 未指定の step でも trace entry に reasoning キーが明示的に None で入る。

    「記録漏れ」ではなく「根拠未記録」を監査上あいまいにしないため、キー自体は常に残す。
    """
    interp = DSLInterpreter()
    program = {
        "steps": [
            {
                "op": "transform",
                "id": "v",
                "input": "M",
                "rule": {"type": "code_map", "map": {"M": "男性"}},
            }
        ],
        "return": "$v",
    }
    _, evidence = await interp.execute([_plan("g2", program)], "P1", db_session)
    assert evidence["g2"]["trace"][0]["reasoning"] is None


# --------------------------------------------------------------------------- #
# viz: Mermaid ノードへの設計根拠注記（実行事実と区別）                          #
# --------------------------------------------------------------------------- #


def test_mermaid_annotates_reasoning() -> None:
    program = {
        "steps": [
            {
                "op": "sql_extract",
                "id": "s",
                "reasoning": "最新の病理報告を1件取得",
                "sql": "SELECT 1",
            }
        ],
        "return": "$s",
    }
    mermaid = program_to_mermaid(program)
    assert "📝 設計根拠: 最新の病理報告を1件取得" in mermaid


def test_mermaid_has_no_reasoning_marker_when_absent() -> None:
    program = {"steps": [{"op": "sql_extract", "id": "s", "sql": "SELECT 1"}], "return": "$s"}
    assert "設計根拠" not in program_to_mermaid(program)


def test_mermaid_neutralizes_angle_brackets_in_reasoning() -> None:
    """reasoning 由来の生の山括弧/引用符は中和され、意図した改行用 `<br/>` のみが残る。

    LLM/ユーザ由来フリーテキストが Mermaid の htmlLabels 解釈に干渉して図を壊さないこと、
    かつ設計根拠注記の改行タグと衝突しないことを固定する。
    """
    program = {
        "steps": [
            {
                "op": "compare",
                "id": "c",
                "reasoning": '値<10 は"低値"|要確認\n次行',
                "left": "$x",
                "cmp": "<",
                "right": "10",
            }
        ],
        "return": "$c",
    }
    mermaid = program_to_mermaid(program)
    # 山括弧は全角へ中和されている
    assert "値＜10" in mermaid
    assert '値<10 は"低値"' not in mermaid
    # 唯一の `<` は設計根拠注記の改行タグだけ（生 reasoning 由来の `<` は残らない）
    assert mermaid.count("<") == 1
    assert "<br/>📝 設計根拠:" in mermaid


# --------------------------------------------------------------------------- #
# routes: 実行時 program スナップショット捕捉                                    #
# --------------------------------------------------------------------------- #


def test_program_snapshot_captures_program_and_version() -> None:
    """program_snapshot が program（reasoning込み）・版情報(plan_id/plan_updated_at)・
    承認の帰属(confirmed_by_id/confirmed_at)を固定する。"""
    plan = _plan(
        "item1",
        {
            "steps": [{"op": "sql_extract", "id": "s", "reasoning": "根拠X", "sql": "SELECT 1"}],
            "return": "$s",
        },
    )
    snap = program_snapshot([plan])
    assert set(snap["item1"]) == {
        "program",
        "plan_id",
        "plan_updated_at",
        "confirmed_by_id",
        "confirmed_at",
    }
    assert snap["item1"]["program"]["steps"][0]["reasoning"] == "根拠X"


@pytest.mark.asyncio
async def test_program_snapshot_records_version_for_persisted_plan(
    db_session: AsyncSession,
) -> None:
    """DB永続済みプランでは plan_id が int、plan_updated_at が isoformat 文字列で固定される。"""
    questionnaire = Questionnaire(
        fhir_id="q-snap-ver",
        title="版情報テスト票",
        status="active",
        content={"resourceType": "Questionnaire", "id": "q-snap-ver"},
    )
    db_session.add(questionnaire)
    await db_session.flush()
    plan = InputPlan(
        questionnaire_id=questionnaire.id,
        link_id="item1",
        program={"steps": [{"op": "sql_extract", "id": "s", "sql": "SELECT 1"}], "return": "$s"},
        status="confirmed",
    )
    db_session.add(plan)
    await db_session.commit()
    await db_session.refresh(plan)

    snap = program_snapshot([plan])
    assert snap["item1"]["plan_id"] == plan.id
    assert isinstance(snap["item1"]["plan_updated_at"], str)


# --------------------------------------------------------------------------- #
# 確認書: バージョン固定（スナップショット優先 / 未保持時フォールバック）        #
# --------------------------------------------------------------------------- #

# 実行後に「編集された」現行プラン。フォールバック時のみ確認書のフロー図に現れる。
_LIVE_PROGRAM = {
    "steps": [
        {
            "op": "sql_extract",
            "id": "s1",
            "reasoning": "編集後の根拠（現行プラン）",
            "sql": "SELECT 1",
        }
    ],
    "return": "$s1",
}
# 実行時に固定された program。確認書はこちらを優先すべき。
_SNAPSHOT_PROGRAM = {
    "steps": [
        {
            "op": "sql_extract",
            "id": "s1",
            "reasoning": "実行時固定の根拠（スナップショット）",
            "sql": "SELECT 1",
        }
    ],
    "return": "$s1",
}


async def _make_execution(
    db_session: AsyncSession,
    *,
    executed_by_id: int,
    program_snapshot: dict | None,
) -> tuple[int, int]:
    questionnaire = Questionnaire(
        fhir_id="q-repin",
        title="再現性テスト票",
        status="active",
        content={"resourceType": "Questionnaire", "id": "q-repin"},
    )
    db_session.add(questionnaire)
    await db_session.flush()

    db_session.add(
        InputPlan(
            questionnaire_id=questionnaire.id,
            link_id="item1",
            item_text="テスト項目",
            program=_LIVE_PROGRAM,
            status="confirmed",
        )
    )
    # 実行トレースは常に実行時のまま固定（evidence 永続）。設計根拠もここに残る。
    trace = [
        {
            "op": "sql_extract",
            "id": "s1",
            "reasoning": "実行時固定の根拠（スナップショット）",
            "sql": "SELECT 1",
            "output": "1",
        }
    ]
    execution = InputExecution(
        questionnaire_id=questionnaire.id,
        patient_id="PAT-001",
        executed_by_id=executed_by_id,
        prefill_data={"item1": "1"},
        evidence={"item1": {"trace": trace, "value": "1"}, "_meta": {"acted_as": "collector"}},
        program_snapshot=program_snapshot,
        status="success",
    )
    db_session.add(execution)
    await db_session.commit()
    return questionnaire.id, execution.id


def _url(qid: int, eid: int) -> str:
    return f"/ai-input/questionnaires/{qid}/autofill/executions/{eid}/validation"


@pytest.mark.asyncio
@pytest.mark.parametrize("ai_extraction", [False, True], ids=["ai_disabled", "ai_enabled"])
async def test_report_prefers_program_snapshot(
    collector_client: AsyncClient,
    db_session: AsyncSession,
    collector_user: User,
    monkeypatch: pytest.MonkeyPatch,
    ai_extraction: bool,
) -> None:
    """スナップショットがあれば確認書はそれを優先し、実行後に編集された現行プランは使わない。"""
    snapshot = {
        "item1": {
            "program": _SNAPSHOT_PROGRAM,
            "plan_id": 123,
            "plan_updated_at": "2026-07-20T00:00:00+00:00",
        }
    }
    qid, eid = await _make_execution(
        db_session, executed_by_id=collector_user.id, program_snapshot=snapshot
    )
    # 確認書は enable_ai_extraction で様式が分かれる（部品非共有の別テンプレート）。
    # フラグを明示しないと、どちらの様式を検証したかが実行環境の .env 次第になる。
    # ここで見る系譜表示は両様式に出るので、片方へ固定して
    # カバレッジを落とすのではなく、両方を回して**どちらの様式でも出る**ことを固定する。
    monkeypatch.setattr(settings, "enable_ai_extraction", ai_extraction)
    resp = await collector_client.get(_url(qid, eid))
    assert resp.status_code == 200
    assert "🔒 実行時に固定したプログラム" in resp.text
    assert "2026-07-20" in resp.text
    # フロー図(Mermaid)はスナップショットの根拠を描く
    assert "実行時固定の根拠（スナップショット）" in resp.text
    # 実行後に編集された現行プランの根拠はフロー図に出ない（＝バージョン固定できている）
    assert "編集後の根拠（現行プラン）" not in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("ai_extraction", [False, True], ids=["ai_disabled", "ai_enabled"])
async def test_report_falls_back_to_live_plan_without_snapshot(
    collector_client: AsyncClient,
    db_session: AsyncSession,
    collector_user: User,
    monkeypatch: pytest.MonkeyPatch,
    ai_extraction: bool,
) -> None:
    """スナップショット未保持の旧記録は現行プランへフォールバックし、その旨を警告表示する。"""
    qid, eid = await _make_execution(
        db_session, executed_by_id=collector_user.id, program_snapshot=None
    )
    monkeypatch.setattr(settings, "enable_ai_extraction", ai_extraction)
    resp = await collector_client.get(_url(qid, eid))
    assert resp.status_code == 200
    assert "実行時スナップショット未保持" in resp.text
    # フォールバックなのでフロー図には現行プランの根拠が出る
    assert "編集後の根拠（現行プラン）" in resp.text


@pytest.mark.asyncio
@pytest.mark.parametrize("ai_extraction", [False, True], ids=["ai_disabled", "ai_enabled"])
async def test_report_pinned_with_empty_snapshot_program(
    collector_client: AsyncClient,
    db_session: AsyncSession,
    collector_user: User,
    monkeypatch: pytest.MonkeyPatch,
    ai_extraction: bool,
) -> None:
    """空 program のスナップショットでも pinned 扱いとし、フロー図は「手順なし」を描く。

    実行時 program が空でも「実行時に固定した内容」であることを監査上明示し、
    現行プランへ誤ってフォールバックしないことを固定する。
    """
    snapshot = {"item1": {"program": {}, "plan_id": 1, "plan_updated_at": None}}
    qid, eid = await _make_execution(
        db_session, executed_by_id=collector_user.id, program_snapshot=snapshot
    )
    monkeypatch.setattr(settings, "enable_ai_extraction", ai_extraction)
    resp = await collector_client.get(_url(qid, eid))
    assert resp.status_code == 200
    assert "🔒 実行時に固定したプログラム" in resp.text
    assert "（手順なし）" in resp.text
    # 空 program なので現行プランの根拠は使われない
    assert "編集後の根拠（現行プラン）" not in resp.text
