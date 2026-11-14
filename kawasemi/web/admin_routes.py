"""管理者用ルーター"""

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi_csrf_protect import CsrfProtect
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from kawasemi.core.auth import get_password_hash
from kawasemi.core.database import get_db
from kawasemi.core.deps import require_roles
from kawasemi.core.logging_config import get_logger
from kawasemi.core.sessions import revoke_user_sessions
from kawasemi.models.auth_event import AuthEvent, AuthEventAction
from kawasemi.models.case_assignment import CaseAssignment
from kawasemi.models.input_target import InputTarget
from kawasemi.models.refresh_token import RefreshToken
from kawasemi.models.role import Role
from kawasemi.models.user import User, UserRole
from kawasemi.web.template_globals import register_template_globals

router = APIRouter(prefix="/admin")
logger = get_logger(__name__)

templates = Jinja2Templates(directory="kawasemi/web/templates")
register_template_globals(templates)

# 管理者ロールが必要な依存性
RequireAdmin = Annotated[User, Depends(require_roles("admin"))]


@router.get("/users", response_class=HTMLResponse)
async def list_users(
    request: Request,
    current_user: RequireAdmin,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    """ユーザー一覧画面"""
    result = await db.execute(
        select(User)
        .options(selectinload(User.user_roles).selectinload(UserRole.role))
        .order_by(User.id)
    )
    users = list(result.scalars().all())

    return templates.TemplateResponse(
        "admin/user_list.html",
        {"request": request, "current_user": current_user, "users": users},
    )


@router.get("/users/new", response_class=HTMLResponse)
async def new_user(
    request: Request,
    current_user: RequireAdmin,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    """ユーザー新規登録画面"""
    # ロール一覧を取得
    result = await db.execute(select(Role).order_by(Role.id))
    roles = list(result.scalars().all())

    return templates.TemplateResponse(
        "admin/user_edit.html",
        {
            "request": request,
            "current_user": current_user,
            "user": None,
            "roles": roles,
            "user_role_ids": [],
            "error": None,
        },
    )


@router.post("/users/new", response_class=HTMLResponse)
async def create_user(
    request: Request,
    current_user: RequireAdmin,
    csrf_protect: Annotated[CsrfProtect, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
    username: Annotated[str, Form()],
    email: Annotated[str, Form()],
    full_name: Annotated[str, Form()],
    password: Annotated[str, Form()],
) -> Response:
    """ユーザー新規登録処理"""
    await csrf_protect.validate_csrf(request)
    # ロール一覧を取得（エラー時の再表示用）
    role_result = await db.execute(select(Role).order_by(Role.id))
    roles = list(role_result.scalars().all())

    # フォームからロールIDを取得
    form_data = await request.form()
    role_ids = [int(str(r)) for r in form_data.getlist("roles")]

    # 重複チェック
    result = await db.execute(select(User).where(User.username == username))
    if result.scalar_one_or_none():
        return templates.TemplateResponse(
            "admin/user_edit.html",
            {
                "request": request,
                "current_user": current_user,
                "user": None,
                "roles": roles,
                "user_role_ids": role_ids,
                "error": f"ユーザー名 '{username}' は既に使用されています",
            },
        )

    result = await db.execute(select(User).where(User.email == email))
    if result.scalar_one_or_none():
        return templates.TemplateResponse(
            "admin/user_edit.html",
            {
                "request": request,
                "current_user": current_user,
                "user": None,
                "roles": roles,
                "user_role_ids": role_ids,
                "error": f"メールアドレス '{email}' は既に使用されています",
            },
        )

    # ユーザー作成
    password_hash = get_password_hash(password)
    user = User(
        username=username,
        email=email,
        password_hash=password_hash,
        full_name=full_name,
        is_active=True,
    )
    db.add(user)
    await db.flush()

    # ロールを付与
    for role_id in role_ids:
        user_role = UserRole(
            user_id=user.id,
            role_id=role_id,
            assigned_by_id=current_user.id,
        )
        db.add(user_role)

    await db.commit()
    logger.info(f"ユーザーを作成しました: id={user.id}, username={username}")

    return RedirectResponse(url="/admin/users", status_code=303)


@router.get("/users/{user_id}/edit", response_class=HTMLResponse)
async def edit_user(
    request: Request,
    user_id: int,
    current_user: RequireAdmin,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """ユーザー編集画面"""
    result = await db.execute(
        select(User)
        .where(User.id == user_id)
        .options(selectinload(User.user_roles).selectinload(UserRole.role))
    )
    user = result.scalar_one_or_none()

    if not user:
        return RedirectResponse(url="/admin/users", status_code=303)

    # ロール一覧を取得
    role_result = await db.execute(select(Role).order_by(Role.id))
    roles = list(role_result.scalars().all())

    # ユーザーの現在のロールID
    user_role_ids = [ur.role_id for ur in user.user_roles]

    return templates.TemplateResponse(
        "admin/user_edit.html",
        {
            "request": request,
            "current_user": current_user,
            "user": user,
            "roles": roles,
            "user_role_ids": user_role_ids,
            "error": None,
        },
    )


@router.post("/users/{user_id}/edit", response_class=HTMLResponse)
async def update_user(
    request: Request,
    user_id: int,
    current_user: RequireAdmin,
    csrf_protect: Annotated[CsrfProtect, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
    username: Annotated[str, Form()],
    email: Annotated[str, Form()],
    full_name: Annotated[str, Form()],
    password: Annotated[str | None, Form()] = None,
    is_active: Annotated[str | None, Form()] = None,
) -> Response:
    """ユーザー更新処理"""
    await csrf_protect.validate_csrf(request)
    result = await db.execute(
        select(User).where(User.id == user_id).options(selectinload(User.user_roles))
    )
    user = result.scalar_one_or_none()

    if not user:
        return RedirectResponse(url="/admin/users", status_code=303)

    # ロール一覧を取得（エラー時の再表示用）
    role_result = await db.execute(select(Role).order_by(Role.id))
    roles = list(role_result.scalars().all())

    # フォームからロールIDを取得
    form_data = await request.form()
    role_ids = [int(str(r)) for r in form_data.getlist("roles")]

    # 重複チェック（自分以外）
    result = await db.execute(select(User).where(User.username == username, User.id != user_id))
    if result.scalar_one_or_none():
        return templates.TemplateResponse(
            "admin/user_edit.html",
            {
                "request": request,
                "current_user": current_user,
                "user": user,
                "roles": roles,
                "user_role_ids": role_ids,
                "error": f"ユーザー名 '{username}' は既に使用されています",
            },
        )

    result = await db.execute(select(User).where(User.email == email, User.id != user_id))
    if result.scalar_one_or_none():
        return templates.TemplateResponse(
            "admin/user_edit.html",
            {
                "request": request,
                "current_user": current_user,
                "user": user,
                "roles": roles,
                "user_role_ids": role_ids,
                "error": f"メールアドレス '{email}' は既に使用されています",
            },
        )

    # 無効化・役割の変更では、発行済みのトークンを無効にする（あとで有効に戻したり役割を
    # 戻したりしても、以前のトークンが生き返らないように）。更新前の状態を控えておく
    was_active = user.is_active
    old_role_ids = {ur.role_id for ur in user.user_roles}

    # ユーザー情報を更新
    user.username = username
    user.email = email
    user.full_name = full_name
    user.is_active = is_active == "on"

    password_changed = bool(password)
    if password:
        user.password_hash = get_password_hash(password)
    # パスワードを変えた・無効にした・役割を変えたら、それまでに発行された全トークンを無効に
    # する。盗まれたリフレッシュ Cookie やアクセストークンが、再設定・無効化のあとに
    # （無効化の取り消しを含めて）生き残らないように
    if password_changed or (was_active and not user.is_active) or set(role_ids) != old_role_ids:
        await revoke_user_sessions(db, user)

    # ロールを更新（既存を削除して再作成）
    for ur in user.user_roles:
        await db.delete(ur)

    for role_id in role_ids:
        user_role = UserRole(
            user_id=user.id,
            role_id=role_id,
            assigned_by_id=current_user.id,
        )
        db.add(user_role)

    await db.commit()
    logger.info(f"ユーザーを更新しました: id={user.id}")

    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/delete")
async def delete_user(
    request: Request,
    user_id: int,
    current_user: RequireAdmin,
    csrf_protect: Annotated[CsrfProtect, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RedirectResponse:
    """ユーザー削除処理"""
    await csrf_protect.validate_csrf(request)
    # 自分自身は削除できない
    if user_id == current_user.id:
        return RedirectResponse(url="/admin/users", status_code=303)

    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user:
        # 削除の前に、そのユーザーが持つセッション・担当・リースを片付ける。
        # 外部キーの ON DELETE は DB 側で強制されない構成（SQLite の既定）があり、
        # 行が残ると削除済みユーザーのトークンが生き続ける
        await revoke_user_sessions(db, user)
        await db.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))
        await db.execute(
            update(InputTarget)
            .where(InputTarget.started_by_id == user_id)
            .values(started_by_id=None, lease_expires_at=None)
        )
        await db.execute(delete(CaseAssignment).where(CaseAssignment.assigned_to_id == user_id))
        await db.delete(user)
        await db.commit()
        logger.info(f"ユーザーを削除しました: id={user_id}")

    return RedirectResponse(url="/admin/users", status_code=303)


@router.post("/users/{user_id}/unlock")
async def unlock_user(
    request: Request,
    user_id: int,
    current_user: RequireAdmin,
    csrf_protect: Annotated[CsrfProtect, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RedirectResponse:
    """アカウントロック解除処理。失敗カウントとロックを解除し監査記録。"""
    await csrf_protect.validate_csrf(request)
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()

    if user and (user.locked_until is not None or user.failed_login_count):
        user.reset_lockout()
        db.add(
            AuthEvent(
                user_id=user.id,
                actor_id=current_user.id,
                action=AuthEventAction.UNLOCKED,
                reason="admin による解除",
            )
        )
        await db.commit()
        logger.info(f"アカウントロックを解除しました: id={user_id}, actor={current_user.id}")

    return RedirectResponse(url="/admin/users", status_code=303)
