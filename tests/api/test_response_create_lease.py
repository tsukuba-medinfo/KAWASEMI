"""REST の回答作成も、Web の保存と同じく「自分が有効なリースを持つ入力対象」に限る（F17/F18）。

以前は `POST /api/v100/questionnaire-responses/` が、収集者ロールだけを見て、任意の患者・任意の
様式の回答を保存できた（Web の `submit_form` は担当の検査をしていた）。検査は
`kawasemi.core.lease.require_leased_target` に共通化してある。
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.models.input_target import InputTarget, InputTargetStatus
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.questionnaire_response import QuestionnaireResponse
from kawasemi.models.user import User
from tests.lease_helpers import lease_patient, make_target


async def _questionnaire(db: AsyncSession, n: int) -> Questionnaire:
    q = Questionnaire(
        fhir_id=f"q-api-lease-{n}",
        title=f"REST リース{n}",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": f"q-api-lease-{n}",
            "item": [{"linkId": "name", "type": "string", "text": "氏名"}],
        },
    )
    db.add(q)
    await db.commit()
    await db.refresh(q)
    return q


async def _count(db: AsyncSession) -> int:
    return int((await db.execute(select(func.count(QuestionnaireResponse.id)))).scalar_one())


async def _reload(db: AsyncSession, target_id: int) -> InputTarget:
    db.expire_all()
    return (await db.execute(select(InputTarget).where(InputTarget.id == target_id))).scalar_one()


def _body(qid: int, subject: str | None, **extra: object) -> dict[str, object]:
    body: dict[str, object] = {"questionnaire_id": qid, "answers": {"name": "x"}}
    if subject is not None:
        body["subject"] = subject
    body.update(extra)
    return body


@pytest.mark.asyncio
async def test_a_collector_cannot_save_for_a_patient_without_a_lease(
    collector_client: AsyncClient, db_session: AsyncSession, csrf_token: str
) -> None:
    """担当していない患者（対象が無い）への保存は 403 で、何も保存されない。"""
    q = await _questionnaire(db_session, 1)

    resp = await collector_client.post(
        "/api/v100/questionnaire-responses/",
        json=_body(q.id, "Patient/NOT-MINE", author="Practitioner/someone-else"),
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 403
    assert await _count(db_session) == 0


@pytest.mark.asyncio
async def test_another_collectors_lease_does_not_count(
    collector_client: AsyncClient,
    reviewer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    q = await _questionnaire(db_session, 2)
    await lease_patient(db_session, reviewer_user.id, "P-OTHER", questionnaire_id=q.id)

    resp = await collector_client.post(
        "/api/v100/questionnaire-responses/",
        json=_body(q.id, "Patient/P-OTHER"),
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 403
    assert await _count(db_session) == 0


@pytest.mark.asyncio
async def test_an_expired_lease_does_not_count(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    q = await _questionnaire(db_session, 3)
    await make_target(
        db_session,
        "P-EXPIRED",
        questionnaire_id=q.id,
        status=InputTargetStatus.IN_PROGRESS,
        holder_id=collector_user.id,
        lease_minutes=-5,
    )

    resp = await collector_client.post(
        "/api/v100/questionnaire-responses/",
        json=_body(q.id, "Patient/P-EXPIRED"),
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 403
    assert await _count(db_session) == 0


@pytest.mark.asyncio
async def test_the_lease_is_per_questionnaire(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """担当患者でも、別の様式の回答は保存できない。"""
    q1 = await _questionnaire(db_session, 4)
    q2 = await _questionnaire(db_session, 5)
    await lease_patient(db_session, collector_user.id, "P-Q", questionnaire_id=q1.id)

    resp = await collector_client.post(
        "/api/v100/questionnaire-responses/",
        json=_body(q2.id, "Patient/P-Q"),
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 403
    assert await _count(db_session) == 0


@pytest.mark.asyncio
async def test_a_missing_subject_is_refused(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """患者を特定できない回答は、担当の検査ができないので 403。"""
    q = await _questionnaire(db_session, 6)
    await lease_patient(db_session, collector_user.id, "P-NOSUBJ", questionnaire_id=q.id)

    resp = await collector_client.post(
        "/api/v100/questionnaire-responses/",
        json=_body(q.id, None),
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 403
    assert await _count(db_session) == 0


@pytest.mark.asyncio
async def test_a_wrong_input_target_id_is_refused(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    q = await _questionnaire(db_session, 7)
    leased = await lease_patient(db_session, collector_user.id, "P-ID", questionnaire_id=q.id)

    for wrong in (leased.id + 99, 0):
        resp = await collector_client.post(
            "/api/v100/questionnaire-responses/",
            json=_body(q.id, "Patient/P-ID", input_target_id=wrong),
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp.status_code == 403
    assert await _count(db_session) == 0
    assert (await _reload(db_session, leased.id)).status != InputTargetStatus.COMPLETED


@pytest.mark.asyncio
async def test_a_leased_save_succeeds_completes_the_target_and_keeps_the_submitter(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    q = await _questionnaire(db_session, 8)
    qid = q.id
    collector_id = collector_user.id
    leased = await lease_patient(db_session, collector_id, "P-OK", questionnaire_id=qid)
    leased_id = leased.id

    resp = await collector_client.post(
        "/api/v100/questionnaire-responses/",
        json=_body(qid, "Patient/P-OK", input_target_id=leased_id),
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 201
    saved = (await db_session.execute(select(QuestionnaireResponse))).scalars().one()
    submitted_by = saved.submitted_by_id
    assert submitted_by == collector_id
    done = await _reload(db_session, leased_id)
    assert done.status == InputTargetStatus.COMPLETED
    assert done.completed_at is not None
    # 完了した対象は、もうリースとして効かない（同じ患者を続けて保存することはできない）
    again = await collector_client.post(
        "/api/v100/questionnaire-responses/",
        json=_body(qid, "Patient/P-OK"),
        headers={"X-CSRF-Token": csrf_token},
    )
    assert again.status_code == 403
    assert await _count(db_session) == 1


@pytest.mark.asyncio
async def test_the_lease_alone_is_enough_without_input_target_id(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    q = await _questionnaire(db_session, 9)
    await make_target(
        db_session,
        "P-BARE",
        questionnaire_id=q.id,
        status=InputTargetStatus.IN_PROGRESS,
        holder_id=collector_user.id,
        lease_minutes=30,
    )

    resp = await collector_client.post(
        "/api/v100/questionnaire-responses/",
        json=_body(q.id, "Patient/P-BARE"),
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 201
