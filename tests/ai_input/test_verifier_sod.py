"""検証者(verifier)の v1 機能テスト

- 検証者はチェックモード（AIドラフトのレビュー）にアクセスできる。
- 職務分離(SoD): 検証者は「自分が生成した取得結果（最新 InputExecution の実行者が本人）」
  を検証(confirm)できない。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.models import ExecutionStatus, InputExecution, InputPlan, PlanStatus
from kawasemi.models.dataset import Dataset, DatasetQuestionnaire
from kawasemi.models.input_target import InputTarget, InputTargetStatus
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.questionnaire_response import QuestionnaireResponse
from kawasemi.models.user import User
from tests.conftest import TestSessionLocal


async def _make_questionnaire(db: AsyncSession) -> Questionnaire:
    q = Questionnaire(
        fhir_id="q-verifier",
        title="検証テスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-verifier",
            "item": [{"linkId": "name", "type": "string", "text": "氏名"}],
        },
    )
    db.add(q)
    await db.flush()
    return q


async def _add_confirmed_plan(db: AsyncSession, qid: int) -> None:
    db.add(
        InputPlan(
            questionnaire_id=qid,
            link_id="name",
            item_text="氏名",
            program={
                "steps": [{"op": "sql_extract", "id": "s1", "sql": "SELECT 1"}],
                "return": "$s1",
            },
            status=PlanStatus.CONFIRMED,
        )
    )


async def _dataset_with_ai_drafted(
    db: AsyncSession, qid: int, patient_id: str, executed_by_id: int | None
) -> tuple[Dataset, InputTarget]:
    ds = Dataset(name="検証DS", description=None, created_by_id=None)
    db.add(ds)
    await db.flush()
    db.add(DatasetQuestionnaire(dataset_id=ds.id, questionnaire_id=qid, display_order=0))
    target = InputTarget(
        dataset_id=ds.id,
        questionnaire_id=qid,
        patient_id=patient_id,
        status=InputTargetStatus.AI_DRAFTED,
    )
    db.add(target)
    # ドラフト元の InputExecution（実行者 = executed_by_id）
    db.add(
        InputExecution(
            questionnaire_id=qid,
            patient_id=patient_id,
            executed_by_id=executed_by_id,
            prefill_data={"name": "ドラフト太郎"},
            evidence={"name": {"trace": [], "value": "ドラフト太郎"}},
            status=ExecutionStatus.SUCCESS,
        )
    )
    await db.commit()
    await db.refresh(target)
    return ds, target


@pytest.mark.asyncio
async def test_verifier_can_open_check_mode(
    reviewer_client: AsyncClient,
    db_session: AsyncSession,
) -> None:
    """検証者はチェックモード画面にアクセスできる（AIドラフトのレビュー）。"""
    q = await _make_questionnaire(db_session)
    await _add_confirmed_plan(db_session, q.id)
    ds, _ = await _dataset_with_ai_drafted(db_session, q.id, "p-001", executed_by_id=None)

    resp = await reviewer_client.get(f"/datasets/{ds.id}/check", params={"questionnaire_id": q.id})
    assert resp.status_code == 200
    assert "ドラフト太郎" in resp.text


@pytest.mark.asyncio
async def test_verifier_cannot_confirm_self_executed(
    reviewer_client: AsyncClient,
    reviewer_user: User,
    csrf_token: str,
    db_session: AsyncSession,
) -> None:
    """SoD: 自分が実行(executed_by_id=自分)したドラフトは検証(confirm)できない。"""
    q = await _make_questionnaire(db_session)
    await _add_confirmed_plan(db_session, q.id)
    ds, target = await _dataset_with_ai_drafted(
        db_session, q.id, "p-001", executed_by_id=reviewer_user.id
    )
    # 検証者がレビューを開く（ソフトロック）
    await reviewer_client.get(f"/datasets/{ds.id}/check", params={"questionnaire_id": q.id})

    resp = await reviewer_client.post(
        f"/datasets/{ds.id}/check/{target.id}/confirm",
        data={"name": "山田太郎"},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    # 確定されず、QuestionnaireResponse も作られない
    async with TestSessionLocal() as verify:
        refreshed = (
            await verify.execute(select(InputTarget).where(InputTarget.id == target.id))
        ).scalar_one()
        assert refreshed.status == InputTargetStatus.AI_DRAFTED
        qrs = (
            (
                await verify.execute(
                    select(QuestionnaireResponse).where(
                        QuestionnaireResponse.questionnaire_id == q.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(qrs) == 0


@pytest.mark.asyncio
async def test_verifier_can_confirm_other_executed(
    reviewer_client: AsyncClient,
    csrf_token: str,
    db_session: AsyncSession,
) -> None:
    """SoD: 他者（ここではバッチ実行=executed_by None）のドラフトは検証者が確定できる。"""
    q = await _make_questionnaire(db_session)
    await _add_confirmed_plan(db_session, q.id)
    # バッチ実行のドラフト（executed_by_id=None）は検証者本人の生成ではないので確定可。
    ds, target = await _dataset_with_ai_drafted(db_session, q.id, "p-001", executed_by_id=None)
    await reviewer_client.get(f"/datasets/{ds.id}/check", params={"questionnaire_id": q.id})

    resp = await reviewer_client.post(
        f"/datasets/{ds.id}/check/{target.id}/confirm",
        data={"name": "山田太郎"},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    async with TestSessionLocal() as verify:
        refreshed = (
            await verify.execute(select(InputTarget).where(InputTarget.id == target.id))
        ).scalar_one()
        assert refreshed.status == InputTargetStatus.COMPLETED
        qrs = (
            (
                await verify.execute(
                    select(QuestionnaireResponse).where(
                        QuestionnaireResponse.questionnaire_id == q.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(qrs) == 1
