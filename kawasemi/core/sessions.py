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
    """発行済みのアクセストークンとリフレッシュトークンをすべて無効にする（commit は呼び出し側）。

    **ロックの順序は users 行 → refresh_tokens 行で固定する。** 世代の +1 を先に flush して
    users 行の行ロックを取り、そのあとで refresh_tokens を失効する。

    - 逆（refresh_tokens を先に UPDATE し、世代は commit の flush まで遅らせる）にすると、
      リフレッシュの更新（users 行を FOR UPDATE で取り、旧トークンを失効し、新トークンを INSERT
      して commit する）がその間に完了した場合、再設定の UPDATE のスナップショットには新トークンが
      見えず、再設定が commit されたあとにも未失効のリフレッシュトークンが残る。
    - 同時に、再設定が refresh_tokens の行を握って users 行を待ち、更新が users 行を握って
      refresh_tokens の行を待つ形の**デッドロック**になる（片方が 500 になる）。

    リフレッシュの更新・ログインも同じ順（users 行を先に）でロックを取る
    （`web/auth_routes.py: _issue_session_refresh_token`）。
    """
    user.token_version = (user.token_version or 0) + 1
    await db.flush()  # users 行の行ロックを、refresh_tokens の UPDATE より先に取る
    await db.execute(
        update(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False))
        .values(revoked=True)
    )
