"""入力対象（InputTarget）の排他リース。

担当は管理者が登録した対象リストから先着で自動割り当てし、期限付きの排他リースで保持する。
`started_by_id` が保持者、`lease_expires_at` が期限である。

- 期限を過ぎたリースは「未保持」と同じ扱いで、患者データへの認可も失う（掃除ジョブは不要）。
- 収集者が同時に持てる有効リースは1件（有効 = 期限内かつ未完了の対象）。
- 排他は `UPDATE ... WHERE id = :id AND (未保持 OR 期限切れ)` の rowcount で取る。
  SQLite でも PostgreSQL でも、同じ行を取り合った後発は rowcount 0 になる。
- 日時は常に UTC の aware で書く。SQLite は naive で返すので、Python 側の比較は `aware_utc` を通す。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sqlalchemy import ColumnElement, and_, case, exists, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from kawasemi.core.config import settings
from kawasemi.models.input_target import InputTarget, InputTargetStatus

# 取り合いに負けたときに別の候補で取り直す回数
_CLAIM_RETRIES = 5

# 受け取り対象にしてよい状態（完了済みは対象外）
_CLAIMABLE_STATUSES = (
    InputTargetStatus.AI_DRAFTED,
    InputTargetStatus.PENDING,
    InputTargetStatus.IN_PROGRESS,
)


def utcnow() -> datetime:
    return datetime.now(UTC)


def aware_utc(dt: datetime) -> datetime:
    """SQLite が返す naive な日時を UTC の aware に揃える。"""
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def lease_duration() -> timedelta:
    return timedelta(minutes=settings.claim_lease_minutes)


def lease_is_valid(target: InputTarget, user_id: int, now: datetime | None = None) -> bool:
    """`user_id` が `target` の有効なリースを持っているか。"""
    if target.started_by_id != user_id or target.lease_expires_at is None:
        return False
    if target.status == InputTargetStatus.COMPLETED:
        return False
    return aware_utc(target.lease_expires_at) > (now or utcnow())


def valid_lease_clause(user_id: int, now: datetime | None = None) -> ColumnElement[bool]:
    """`lease_is_valid` の SQL 版（他のクエリへ and_ で足す）。"""
    return and_(
        InputTarget.started_by_id == user_id,
        InputTarget.lease_expires_at > (now or utcnow()),
        InputTarget.status != InputTargetStatus.COMPLETED,
    )


def free_clause(now: datetime) -> ColumnElement[bool]:
    """誰も有効に保持していない（未保持、または期限切れ）。"""
    return or_(
        InputTarget.started_by_id.is_(None),
        InputTarget.lease_expires_at.is_(None),
        InputTarget.lease_expires_at <= now,
    )


async def get_active_lease(db: AsyncSession, user_id: int) -> InputTarget | None:
    """`user_id` が現在保持している有効なリースの対象（無ければ None）。"""
    result = await db.execute(
        select(InputTarget)
        .options(selectinload(InputTarget.questionnaire), selectinload(InputTarget.dataset))
        .where(valid_lease_clause(user_id))
        .order_by(InputTarget.id)
        .limit(1)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def extend_lease(db: AsyncSession, target: InputTarget, user_id: int) -> bool:
    """保持者の操作でリースを now + 長さ に延ばす。有効な保持者でなければ何もしない。"""
    now = utcnow()
    result = await db.execute(
        update(InputTarget)
        .where(InputTarget.id == target.id, valid_lease_clause(user_id, now))
        .values(lease_expires_at=now + lease_duration())
        .execution_options(synchronize_session=False)
    )
    await db.commit()
    if result.rowcount:  # type: ignore[attr-defined]
        await db.refresh(target)
        return True
    return False


async def extend_leases_for_patient(db: AsyncSession, user_id: int, patient_id: str) -> None:
    """その患者についての保持者の操作（autofill など）でリースを延ばす。無ければ何もしない。"""
    now = utcnow()
    result = await db.execute(
        update(InputTarget)
        .where(InputTarget.patient_id == patient_id, valid_lease_clause(user_id, now))
        .values(lease_expires_at=now + lease_duration())
        .execution_options(synchronize_session=False)
    )
    if result.rowcount:  # type: ignore[attr-defined]
        await db.commit()


async def release_lease(db: AsyncSession, target: InputTarget) -> None:
    """リースを手放す（担当を外す）。状態は変えない。"""
    target.started_by_id = None
    target.lease_expires_at = None
    await db.commit()


async def claim_next_target(
    db: AsyncSession,
    user_id: int,
    *,
    dataset_id: int | None = None,
    questionnaire_id: int | None = None,
) -> InputTarget | None:
    """次の対象を1件受け取る。既に有効なリースがあればそれを返す（一度に1件）。

    順序: AI ドラフト済み（要確認でないもの）→ 未着手 → 期限切れの入力中 → 要確認。同順位は id 順。
    受け取れる対象が無ければ None。
    """
    held = await get_active_lease(db, user_id)
    if held is not None:
        await extend_lease(db, held, user_id)
        return held

    rank = case(
        (InputTarget.needs_attention.is_(True), 3),
        (InputTarget.status == InputTargetStatus.AI_DRAFTED, 0),
        (InputTarget.status == InputTargetStatus.PENDING, 1),
        else_=2,
    )
    for _ in range(_CLAIM_RETRIES):
        now = utcnow()
        query = (
            select(InputTarget.id)
            .where(InputTarget.status.in_(_CLAIMABLE_STATUSES), free_clause(now))
            .order_by(rank, InputTarget.id)
            .limit(1)
        )
        if dataset_id is not None:
            query = query.where(InputTarget.dataset_id == dataset_id)
        if questionnaire_id is not None:
            query = query.where(InputTarget.questionnaire_id == questionnaire_id)
        candidate_id = (await db.execute(query)).scalar_one_or_none()
        if candidate_id is None:
            await db.rollback()
            return None

        # 「この収集者が有効なリースを持たない」も UPDATE の条件に入れる。同じ人の二重送信
        # （ダブルクリック・複数タブ）が、別々の候補を取って2件持つのを防ぐ。
        mine = aliased(InputTarget)
        holds_none = ~exists().where(
            mine.started_by_id == user_id,
            mine.lease_expires_at > now,
            mine.status != InputTargetStatus.COMPLETED,
        )
        result = await db.execute(
            update(InputTarget)
            .where(
                InputTarget.id == candidate_id,
                InputTarget.status.in_(_CLAIMABLE_STATUSES),
                free_clause(now),
                holds_none,
            )
            .values(
                started_by_id=user_id,
                lease_expires_at=now + lease_duration(),
                status=case(
                    (
                        InputTarget.status == InputTargetStatus.PENDING,
                        InputTargetStatus.IN_PROGRESS,
                    ),
                    else_=InputTarget.status,
                ),
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if result.rowcount:  # type: ignore[attr-defined]
            return await _converge_to_one_lease(db, user_id, candidate_id)
        # 負けた。自分が既に持っていた（二重送信の相手が先に取った）ならそれを返す
        held = await get_active_lease(db, user_id)
        if held is not None:
            return held
        # 他者に取られた: 別の候補で取り直す
    return None


async def _converge_to_one_lease(db: AsyncSession, user_id: int, claimed_id: int) -> InputTarget:
    """有効なリースが複数あれば id 最小の1件だけ残して手放し、残した対象を返す。

    NOT EXISTS だけでは、PostgreSQL の READ COMMITTED で別の行を同時に UPDATE した
    二重送信が両方通り得る。通った呼び出しがそれぞれ同じ規則（最小 id を残す）で収束させる。
    """
    rows = (
        (
            await db.execute(
                select(InputTarget)
                .where(valid_lease_clause(user_id))
                .order_by(InputTarget.id)
                .execution_options(populate_existing=True)
            )
        )
        .scalars()
        .all()
    )
    keep = rows[0] if rows else None
    for extra in rows[1:]:
        extra.started_by_id = None
        extra.lease_expires_at = None
    if len(rows) > 1:
        await db.commit()
    if keep is None:  # 収束の相手が先に自分の分を手放した等。取り直しは呼び出し側に任せる
        keep = (
            await db.execute(
                select(InputTarget)
                .where(InputTarget.id == claimed_id)
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
    return keep
