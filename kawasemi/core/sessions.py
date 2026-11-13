"""ユーザーのセッション（発行済みトークン）を一括で失効させる。

アクセストークンはステートレスな JWT なので、サーバー側から取り消す手段は
`User.token_version` の世代番号だけである（`core/deps.py` がリクエストごとに照合する）。
リフレッシュトークンは DB の行を失効させる。パスワードの再設定とログアウトは両方を行う。
"""

from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.models.refresh_token import RefreshToken
from kawasemi.models.user import User


async def revoke_user_sessions(db: AsyncSession, user: User) -> None:
    """発行済みのアクセストークンとリフレッシュトークンをすべて無効にする（commit は呼び出し側）。"""
    user.token_version = (user.token_version or 0) + 1
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False))
        .values(revoked=True)
    )
