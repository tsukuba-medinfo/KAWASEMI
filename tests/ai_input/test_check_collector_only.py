"""チェックモード（AIドラフトのレビュー）は収集者だけが行う

欠損を埋める人が検証者を兼ねるので、チェック・確定・要確認は収集者の役割で、閲覧・検証者は
この流れに入らない（以前あった「検証者は自分が実行した取得結果を確定できない」職務分離は、
検証者がそもそも確定できなくなったことで、より強く満たされる）。

- 収集者は、自分が有効なリースを持つ AIドラフト対象だけ開ける・確定できる。
- 閲覧・検証者はチェックの開く/確定/要確認がいずれも 403。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


async def _hold(db: AsyncSession, user_id: int, target: InputTarget) -> None:
    """`target` の有効なリースを `user_id` に持たせる（受け取り済みの状態）。"""
    target.started_by_id = user_id
    target.lease_expires_at = datetime.now(UTC) + timedelta(minutes=60)
    await db.commit()


@pytest.mark.asyncio
async def test_collector_can_open_check_mode_for_the_leased_target(
    collector_client: AsyncClient, collector_user: User, db_session: AsyncSession
) -> None:
    q = await _make_questionnaire(db_session)
    await _add_confirmed_plan(db_session, q.id)
    ds, target = await _dataset_with_ai_drafted(db_session, q.id, "p-001", executed_by_id=None)
    await _hold(db_session, collector_user.id, target)

    resp = await collector_client.get(f"/datasets/{ds.id}/check", params={"questionnaire_id": q.id})
    assert resp.status_code == 200
    assert "ドラフト太郎" in resp.text


@pytest.mark.asyncio
async def test_reviewer_cannot_open_confirm_or_flag_check(
    reviewer_client: AsyncClient,
    reviewer_user: User,
    csrf_token: str,
    db_session: AsyncSession,
) -> None:
    """閲覧・検証者はチェックに入れない。リースを持っていても同じ（役割で弾く）。"""
    q = await _make_questionnaire(db_session)
    await _add_confirmed_plan(db_session, q.id)
    ds, target = await _dataset_with_ai_drafted(
        db_session, q.id, "p-001", executed_by_id=reviewer_user.id
    )
    await _hold(db_session, reviewer_user.id, target)

    got = await reviewer_client.get(f"/datasets/{ds.id}/check", params={"questionnaire_id": q.id})
    assert got.status_code == 403
    for action in ("confirm", "flag"):
        resp = await reviewer_client.post(
            f"/datasets/{ds.id}/check/{target.id}/{action}",
            data={"name": "山田太郎"},
            headers={"X-CSRF-Token": csrf_token},
            follow_redirects=False,
        )
        assert resp.status_code == 403, action
    async with TestSessionLocal() as verify:
        refreshed = (
            await verify.execute(select(InputTarget).where(InputTarget.id == target.id))
        ).scalar_one()
        assert refreshed.status == InputTargetStatus.AI_DRAFTED
        assert not refreshed.needs_attention
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
async def test_collector_confirms_own_leased_draft(
    collector_client: AsyncClient,
    collector_user: User,
    csrf_token: str,
    db_session: AsyncSession,
) -> None:
    q = await _make_questionnaire(db_session)
    await _add_confirmed_plan(db_session, q.id)
    ds, target = await _dataset_with_ai_drafted(db_session, q.id, "p-001", executed_by_id=None)
    await _hold(db_session, collector_user.id, target)

    resp = await collector_client.post(
        f"/datasets/{ds.id}/check/{target.id}/confirm",
        data={"name": "山田太郎"},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/input-targets")
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
