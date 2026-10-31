"""モデルの default・unique・複合ユニーク・ORM cascade の単体テスト。

db_session は in-memory SQLite（Base.metadata.create_all）。SQLite は既定で
外部キー制約 OFF のため DB レベルの ondelete は検証しないが、ORM の
cascade="all, delete-orphan" は SQLAlchemy が発行するため検証できる。
"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from kawasemi.models.batch_job import BatchJob, BatchJobStatus
from kawasemi.models.dataset import Dataset, DatasetQuestionnaire
from kawasemi.models.input_target import InputTarget, InputTargetStatus
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.refresh_token import RefreshToken

# --- default 値 ---


@pytest.mark.asyncio
async def test_refresh_token_default_not_revoked(db_session: AsyncSession) -> None:
    rt = RefreshToken(jti="j1", user_id=1, expires_at=datetime.now(UTC) + timedelta(days=1))
    db_session.add(rt)
    await db_session.flush()
    await db_session.refresh(rt)
    assert rt.revoked is False


@pytest.mark.asyncio
async def test_questionnaire_default_not_deleted(db_session: AsyncSession) -> None:
    q = Questionnaire(fhir_id="q1", content={"resourceType": "Questionnaire"}, status="active")
    db_session.add(q)
    await db_session.flush()
    await db_session.refresh(q)
    assert q.is_deleted is False


@pytest.mark.asyncio
async def test_input_target_default_status_and_attention(db_session: AsyncSession) -> None:
    t = InputTarget(dataset_id=1, questionnaire_id=1, patient_id="P1")
    db_session.add(t)
    await db_session.flush()
    await db_session.refresh(t)
    assert t.status == InputTargetStatus.PENDING
    assert t.needs_attention is False


@pytest.mark.asyncio
async def test_batch_job_defaults(db_session: AsyncSession) -> None:
    job = BatchJob(dataset_id=1, questionnaire_id=1)
    db_session.add(job)
    await db_session.flush()
    await db_session.refresh(job)
    assert job.status == BatchJobStatus.RUNNING
    assert job.total == job.processed == job.succeeded == job.partial == job.failed == 0
    assert job.cancel_requested is False
    assert job.study_id == ""


# --- unique 制約 ---


@pytest.mark.asyncio
async def test_questionnaire_fhir_id_unique(db_session: AsyncSession) -> None:
    db_session.add(Questionnaire(fhir_id="dup", content={}, status="active"))
    db_session.add(Questionnaire(fhir_id="dup", content={}, status="active"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


# --- 複合ユニーク制約 ---


@pytest.mark.asyncio
async def test_dataset_questionnaire_composite_unique(db_session: AsyncSession) -> None:
    db_session.add(DatasetQuestionnaire(dataset_id=1, questionnaire_id=2))
    db_session.add(DatasetQuestionnaire(dataset_id=1, questionnaire_id=2))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_input_target_composite_unique(db_session: AsyncSession) -> None:
    db_session.add(InputTarget(dataset_id=1, questionnaire_id=2, patient_id="P1"))
    db_session.add(InputTarget(dataset_id=1, questionnaire_id=2, patient_id="P1"))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_input_target_composite_allows_different_patient(db_session: AsyncSession) -> None:
    db_session.add(InputTarget(dataset_id=1, questionnaire_id=2, patient_id="P1"))
    db_session.add(InputTarget(dataset_id=1, questionnaire_id=2, patient_id="P2"))
    await db_session.commit()  # patient_id が異なれば衝突しない
    result = await db_session.execute(select(InputTarget))
    assert len(result.scalars().all()) == 2


# --- ORM cascade（delete-orphan） ---


@pytest.mark.asyncio
async def test_dataset_delete_cascades_children(db_session: AsyncSession) -> None:
    ds = Dataset(name="D")
    db_session.add(ds)
    await db_session.flush()
    db_session.add(DatasetQuestionnaire(dataset_id=ds.id, questionnaire_id=1))
    db_session.add(InputTarget(dataset_id=ds.id, questionnaire_id=1, patient_id="P1"))
    await db_session.commit()

    # 子を eager ロードしてから親を削除（ORM cascade delete-orphan）
    loaded = await db_session.execute(
        select(Dataset)
        .where(Dataset.id == ds.id)
        .options(
            selectinload(Dataset.dataset_questionnaires),
            selectinload(Dataset.input_targets),
        )
    )
    ds_loaded = loaded.scalar_one()
    await db_session.delete(ds_loaded)
    await db_session.commit()

    dq = await db_session.execute(select(DatasetQuestionnaire))
    it = await db_session.execute(select(InputTarget))
    assert dq.scalars().all() == []
    assert it.scalars().all() == []
