"""テスト用: 指定ユーザーに有効なリース（担当）を持たせる。

担当は本来「次の患者を受け取る」で先着に割り当たるが、認可や画面の挙動だけを見るテストでは
対象と保持者を直接置く方が速い。排他・順序そのものは tests/test_input_target_lease.py が見る。
dataset_id / questionnaire_id の親行が無ければ、最小の行をここで作る（テストDBも本番と同じく
FK を強制する）。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.models.dataset import Dataset
from kawasemi.models.input_target import InputTarget, InputTargetStatus
from kawasemi.models.questionnaire import Questionnaire


async def ensure_parents(db: AsyncSession, dataset_id: int, questionnaire_id: int) -> None:
    """入力対象の親（データセットと質問票）が無ければ、最小の行を作る。"""
    if await db.get(Dataset, dataset_id) is None:
        db.add(Dataset(id=dataset_id, name=f"stub-dataset-{dataset_id}"))
    if await db.get(Questionnaire, questionnaire_id) is None:
        db.add(
            Questionnaire(
                id=questionnaire_id,
                fhir_id=f"q-stub-{questionnaire_id}",
                title=f"stub-{questionnaire_id}",
                status="active",
                content={"resourceType": "Questionnaire", "id": f"q-stub-{questionnaire_id}"},
            )
        )
    await db.flush()


async def make_target(
    db: AsyncSession,
    patient_id: str,
    *,
    questionnaire_id: int = 1,
    dataset_id: int = 1,
    status: str = InputTargetStatus.PENDING,
    holder_id: int | None = None,
    lease_minutes: float | None = None,
    needs_attention: bool = False,
) -> InputTarget:
    """入力対象を1件作る。holder_id と lease_minutes（負なら期限切れ）でリースを付ける。"""
    expires = None
    if holder_id is not None and lease_minutes is not None:
        expires = datetime.now(UTC) + timedelta(minutes=lease_minutes)
    await ensure_parents(db, dataset_id, questionnaire_id)
    target = InputTarget(
        dataset_id=dataset_id,
        questionnaire_id=questionnaire_id,
        patient_id=patient_id,
        status=status,
        started_by_id=holder_id,
        lease_expires_at=expires,
        needs_attention=needs_attention,
    )
    db.add(target)
    await db.commit()
    await db.refresh(target)
    return target


async def lease_patient(
    db: AsyncSession,
    user_id: int,
    patient_id: str,
    *,
    questionnaire_id: int = 1,
    dataset_id: int = 1,
    status: str = InputTargetStatus.IN_PROGRESS,
    minutes: float = 60,
) -> InputTarget:
    """`user_id` が `patient_id` の有効なリースを持つ状態を作る。"""
    return await make_target(
        db,
        patient_id,
        questionnaire_id=questionnaire_id,
        dataset_id=dataset_id,
        status=status,
        holder_id=user_id,
        lease_minutes=minutes,
    )
