"""Subject（Hub）モデルと get_or_create_subject のユニットテスト。

- (study_id, registration_no) による get-or-create の冪等性
- study 違いは別被験者
- 前後空白の正規化
- 既存モデル（CaseAssignment）への subject_id 紐付け
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.models import CaseAssignment, Subject, get_or_create_subject
from kawasemi.models.user import User
from tests.lease_helpers import ensure_parents


async def test_get_or_create_is_idempotent(db_session: AsyncSession) -> None:
    s1 = await get_or_create_subject(db_session, "STUDY-A", "0000123456")
    await db_session.commit()
    s2 = await get_or_create_subject(db_session, "STUDY-A", "0000123456")
    await db_session.commit()

    assert s1.id is not None
    assert s1.id == s2.id

    rows = (await db_session.execute(select(Subject))).scalars().all()
    assert len(rows) == 1


async def test_same_registration_no_different_study_is_distinct(
    db_session: AsyncSession,
) -> None:
    a = await get_or_create_subject(db_session, "STUDY-A", "001")
    b = await get_or_create_subject(db_session, "STUDY-B", "001")
    await db_session.commit()

    assert a.id != b.id


async def test_whitespace_is_normalized(db_session: AsyncSession) -> None:
    a = await get_or_create_subject(db_session, "STUDY-A", "001")
    b = await get_or_create_subject(db_session, "  STUDY-A ", " 001  ")
    await db_session.commit()

    assert a.id == b.id


async def test_case_assignment_links_to_subject(db_session: AsyncSession) -> None:
    subject = await get_or_create_subject(db_session, "STUDY-A", "0000123456")
    await ensure_parents(db_session, 1, 1)
    holder = User(username="ca_holder", email="ca@example.com", password_hash="x", full_name="c")
    db_session.add(holder)
    await db_session.flush()
    ca = CaseAssignment(
        questionnaire_id=1,
        patient_id="0000123456",
        subject_id=subject.id,
        assigned_to_id=holder.id,
    )
    db_session.add(ca)
    await db_session.commit()
    await db_session.refresh(ca)

    assert ca.subject_id == subject.id
