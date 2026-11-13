"""患者データへのアクセス制御（ハード・スコープ）共通ロジック。

web / ai_input など複数ルータで同一の患者スコープ判定を使うため、ここに集約する
（ロジックの二重化＝drift を避ける）。IDOR 対策の権威判定。
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.core.lease import valid_lease_clause
from kawasemi.models.input_target import InputTarget
from kawasemi.models.user import User


async def user_can_access_patient(db: AsyncSession, user: User, patient_id: str) -> bool:
    """ユーザーがその患者（被験者）データへアクセスしてよいか判定する。

    ハード・スコープ方式: 役割を問わず「自分が**有効なリース**を持つ入力対象（InputTarget）の
    patient_id」に限定する。有効なリースとは started_by_id が本人で、期限内で、未完了のもの。
    リースは管理者の対象リストから先着で自動割り当てされ（kawasemi.core.lease）、収集者が
    患者を自分で選ぶことはできない。期限切れ・完了後は認可を失う。
    管理者の全許可は無い（管理者は患者データを見ない）。
    """
    result = await db.execute(
        select(InputTarget.id)
        .where(InputTarget.patient_id == patient_id, valid_lease_clause(user.id))
        .limit(1)
    )
    return result.first() is not None
