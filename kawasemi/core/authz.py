"""患者データへのアクセス制御（ハード・スコープ）共通ロジック。

web / ai_input など複数ルータで同一の患者スコープ判定を使うため、ここに集約する
（ロジックの二重化＝drift を避ける）。IDOR 対策の権威判定。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.models.case_assignment import CaseAssignment
from kawasemi.models.input_target import InputTarget
from kawasemi.models.user import User


async def user_can_access_patient(db: AsyncSession, user: User, patient_id: str) -> bool:
    """ユーザーがその患者（被験者）データへアクセスしてよいか判定する。

    ハード・スコープ方式: 役割を問わず「自分にロック/割り当てられた入力対象（InputTarget）
    または症例割り当て（CaseAssignment）の patient_id」に限定する。任意の patient_id を
    指定して他者の患者データを引き出す IDOR を防ぐ。
    管理者の全許可は無い（管理者は患者データを見ない）。
    """
    it = await db.execute(
        select(InputTarget.id)
        .where(
            InputTarget.patient_id == patient_id,
            InputTarget.started_by_id == user.id,
        )
        .limit(1)
    )
    if it.first() is not None:
        return True

    ca = await db.execute(
        select(CaseAssignment.id)
        .where(
            CaseAssignment.patient_id == patient_id,
            CaseAssignment.assigned_to_id == user.id,
        )
        .limit(1)
    )
    return ca.first() is not None
