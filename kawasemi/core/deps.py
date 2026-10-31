"""依存性注入モジュール"""

from typing import Annotated, Any

from fastapi import Cookie, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from kawasemi.core.auth import decode_access_token
from kawasemi.core.database import get_db
from kawasemi.models.role import RoleType
from kawasemi.models.user import User, UserRole


async def get_current_user_optional(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    token: Annotated[str | None, Cookie(alias="kawasemi_token")] = None,
) -> User | None:
    """現在のユーザーを取得（オプション）

    認証されていない場合はNoneを返す
    """
    if not token:
        return None

    payload = decode_access_token(token)
    if not payload:
        return None

    user_id = payload.get("sub")
    if not user_id:
        return None

    try:
        user_id_int = int(user_id)
    except ValueError:
        return None

    result = await db.execute(
        select(User)
        .where(User.id == user_id_int, User.is_active.is_(True))
        .options(selectinload(User.user_roles).selectinload(UserRole.role))
    )
    user = result.scalar_one_or_none()
    if user is None:
        return None

    # アクティブ役割を決定する。JWT の `act` クレームを尊重するが、
    # 実際に切替可能な役割でない場合は既定（最高権限）にフォールバックする。
    requested = payload.get("act")
    if isinstance(requested, str) and user.can_act_as(requested):
        user.active_role = requested
    else:
        user.active_role = user.default_active_role()
    # **この入り口で使える役割が無ければ未認証と同じに扱う**。同じホストの別 port は
    # Cookie を共有するので、別の入り口でログインした利用者のトークンがここにも届く。
    # 「役割なしの認証済み」で通すと、ログイン画面の拒否（auth_routes）が素通りになる
    if user.active_role is None and user.held_role_names_outside_entry():
        return None
    return user


async def get_current_user(
    user: Annotated[User | None, Depends(get_current_user_optional)],
) -> User:
    """現在のユーザーを取得（必須）

    認証されていない場合は例外を発生
    """
    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="認証が必要です",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


class RoleRequiredError(HTTPException):
    """アクティブ役割が足りないときの 403。**どの役割なら実行できるかを持つ。**

    素の `HTTPException` だと、画面には `detail` の JSON がそのまま出る
    （`api/main.py` に HTTPException のハンドラが無いため）。利用者には
    「どの役割に切り替えればできるか」も「どこへ戻ればよいか」も分からず、
    ハード・スコープの切替コストがそのまま行き止まりになっていた。

    `detail` は従来と同じ文字列のままにしてある（API 側の応答を変えないため）。
    画面向けの案内は `api/main.py: role_required_handler` が組み立てる。
    """

    def __init__(
        self,
        required_roles: tuple[str, ...],
        active_role: str | None,
        switchable: list[str],
        full_name: str = "",
        held_outside: tuple[str, ...] | list[str] = (),
    ) -> None:
        self.required_roles = list(required_roles)
        self.active_role = active_role
        self.switchable = list(switchable)
        # この入り口では使えないが保有している役割（別の入り口への案内は、保有する役割の
        # 入り口だけを指す）
        self.held_outside = list(held_outside)
        # ORM の User をそのまま持ち回らない（ハンドラが走る時点でセッションが
        # 閉じていると属性アクセスで落ちるため）。表示に要る値だけを写す。
        self.full_name = full_name
        # 保有していて切替できる役割のうち、その操作ができるもの（空なら他人に依頼する）。
        self.switchable_to = [r for r in switchable if r in required_roles]
        super().__init__(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="この操作を行う権限がありません",
        )


def require_roles(*required_roles: str) -> Any:
    """特定の「アクティブ役割」を持つユーザーのみアクセスを許可するファクトリ

    ハード・スコープ方式: 保有役割ではなく、現在アクティブな役割
    (`user.active_role`) で判定する。例えば設計者と収集者を保有する人が収集者へ
    切り替えると設計者専用ルートは 403 になり、設計へ戻るには切り替え直す。

    Args:
        required_roles: アクセスを許可するアクティブ役割名（いずれか一つ）

    Usage:
        @router.get("/admin")
        async def admin_page(user: Annotated[User, Depends(require_roles("admin"))]):
            ...
    """

    async def role_checker(
        user: Annotated[User, Depends(get_current_user)],
    ) -> User:
        if user.active_role not in required_roles:
            raise RoleRequiredError(
                required_roles=required_roles,
                active_role=user.active_role,
                switchable=user.switchable_role_names(),
                full_name=user.full_name,
                held_outside=user.held_role_names_outside_entry(),
            )
        return user

    return role_checker


# よく使うロールチェッカー（ハード・スコープ: アクティブ役割で判定）。
# 4役割。管理者はシステムの運用だけで、設計・収集・検証の経路には入らない。
RequireAdmin = Annotated[User, Depends(require_roles(RoleType.ADMIN))]
# 設計者: 臨床的意味・所在・手順書・取得プラン・キャンバス・様式の内容（Questionnaire の項目・ValueSet）。
RequireDesigner = Annotated[User, Depends(require_roles(RoleType.DESIGNER))]
# 収集者: データセット・割り当て・自動収集のバッチ起動と本番実行・手動入力・回答の確定保存。
RequireCollector = Annotated[User, Depends(require_roles(RoleType.COLLECTOR))]
# 閲覧・検証者: 回答の閲覧・check・検証/差戻し・エクスポート。
RequireReviewer = Annotated[User, Depends(require_roles(RoleType.REVIEWER))]
# 取得困難度メトリクス: 項目別成否集計の閲覧・CSVエクスポート。
# 集計面は件数・率のみ（PHI 非含有）のため、設計者と閲覧・検証者に開く。管理者は含まない。
RequireMetricsViewer = Annotated[User, Depends(require_roles(RoleType.DESIGNER, RoleType.REVIEWER))]
# autofill 実行者: 設計者（試行・揮発）＋収集者（本番・永続）。
# 試行/本番の区別はアクティブ役割で導出する。バッチ実行（非対話）は収集者が起動する。
RequireAutofillRunner = Annotated[
    User, Depends(require_roles(RoleType.DESIGNER, RoleType.COLLECTOR))
]
RequireAnyRole = Annotated[User, Depends(get_current_user)]
