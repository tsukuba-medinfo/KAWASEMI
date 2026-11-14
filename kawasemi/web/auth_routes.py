"""認証関連ルーター"""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi_csrf_protect import CsrfProtect
from slowapi.util import get_remote_address
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from kawasemi.core.auth import (
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
    verify_password,
)
from kawasemi.core.config import settings
from kawasemi.core.database import get_db
from kawasemi.core.deps import get_current_user, get_current_user_optional
from kawasemi.core.entry import current_entry
from kawasemi.core.logging_config import get_logger
from kawasemi.core.login_throttle import login_throttle
from kawasemi.core.login_throttle import throttle_key as make_throttle_key
from kawasemi.core.rate_limit import limiter, login_rate_limit
from kawasemi.core.sessions import revoke_user_sessions
from kawasemi.models.auth_event import AuthEvent, AuthEventAction
from kawasemi.models.refresh_token import RefreshToken
from kawasemi.models.role import RoleType
from kawasemi.models.user import User, UserRole
from kawasemi.web.template_globals import register_template_globals

router = APIRouter()
logger = get_logger(__name__)

# ユーザー不在/非アクティブ時も bcrypt を1回走らせて応答時間を平準化し、
# 処理時間差による有効ユーザー名の列挙（timing oracle）を防ぐためのダミーハッシュ。
# 実在のパスワードではなく、検証は常に False になる。
_DUMMY_PASSWORD_HASH = "$2b$12$sn/2lq0dq92EJCQ2fOSDeutgWHBKFvpE4efccvhWfrMK8SjUqUgZy"

templates = Jinja2Templates(directory="kawasemi/web/templates")
register_template_globals(templates)


def _safe_redirect_path(next_url: str | None) -> str:
    """ログイン後のリダイレクト先を自サイト内の相対パスに限定する。

    `next` にユーザー制御の値が入るため、`//evil.com` や `https://evil.com`
    のような絶対/プロトコル相対 URL を弾き、オープンリダイレクトを防ぐ。
    安全でなければトップ（"/"）にフォールバックする。
    """
    if not next_url:
        return "/"
    # 先頭が単一の "/" で始まる相対パスのみ許可（"//" や "/\" は外部扱い）
    if not next_url.startswith("/") or next_url.startswith(("//", "/\\")):
        return "/"
    return next_url


def _set_access_cookie(response: Response, access_token: str) -> None:
    response.set_cookie(
        key=settings.jwt_cookie_name,
        value=access_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.jwt_access_token_expire_minutes * 60,
    )


def _set_refresh_cookie(response: Response, refresh_token: str) -> None:
    response.set_cookie(
        key=settings.jwt_refresh_cookie_name,
        value=refresh_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.jwt_refresh_token_expire_days * 86400,
    )


async def _issue_refresh_token(user_id: int, db: AsyncSession) -> str:
    """リフレッシュトークンを1件発行し DB に保存して返す。"""
    jti = uuid.uuid4().hex
    expires_at = datetime.now(UTC) + timedelta(days=settings.jwt_refresh_token_expire_days)
    db.add(RefreshToken(jti=jti, user_id=user_id, expires_at=expires_at))
    await db.commit()
    return create_refresh_token(user_id, jti)


async def _issue_session_refresh_token(user: User, db: AsyncSession) -> str | None:
    """リフレッシュトークンを発行し、**発行の最中に失効が入っていないか**を確かめる。

    管理者のパスワード再設定やログアウトは、`token_version` を +1 してその時点の未失効の
    リフレッシュトークンを全件失効する。そのコミットが、こちらの発行（INSERT）と前後すると、
    再設定のあとに新しいリフレッシュトークンが生き残りうる。そこで発行のあとに世代と有効フラグを
    読み直し、変わっていれば今発行した分も含めて失効して None を返す。
    """
    version = user.token_version
    # users 行を**先に**ロックして読み直す（PostgreSQL の FOR UPDATE。SQLite では無視される）。
    # 再設定・ログアウト（`core/sessions.py`）と同じ順（users → refresh_tokens）なので、
    # どちらかが先にコミットするまでもう一方は待ち、デッドロックにならない。ロックを取った
    # 時点で世代が変わっていれば、トークンを発行せずに断る
    await db.refresh(user, ["token_version", "is_active"], with_for_update=True)
    if user.token_version != version or not user.is_active:
        return None
    token = await _issue_refresh_token(user.id, db)
    # 発行のあとにもう一度確かめる（ロックが効かない SQLite で、発行の最中に再設定が入った場合）
    await db.refresh(user, ["token_version", "is_active"])
    if user.token_version != version or not user.is_active:
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False))
            .values(revoked=True)
        )
        await db.commit()
        return None
    return token


@router.get("/login", response_class=HTMLResponse)
async def login_page(
    request: Request,
    user: Annotated[User | None, Depends(get_current_user_optional)],
    csrf_protect: CsrfProtect = Depends(),
    next: str | None = None,
) -> Response:
    """ログインページ"""
    # すでにログイン済みの場合はトップページへリダイレクト
    if user:
        return RedirectResponse(url="/", status_code=303)

    # CSRFトークンを生成
    csrf_token, signed_token = csrf_protect.generate_csrf_tokens()

    response = templates.TemplateResponse(
        "login.html",
        {"request": request, "error": None, "next": next, "csrf_token": csrf_token},
    )

    # 署名済みトークンをCookieに設定
    csrf_protect.set_csrf_cookie(signed_token, response)

    return response


@router.post("/login", response_class=HTMLResponse)
@limiter.limit(login_rate_limit)
async def login(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    username: Annotated[str, Form()],
    password: Annotated[str, Form()],
    csrf_protect: CsrfProtect = Depends(),
    next: Annotated[str | None, Form()] = None,
) -> Response:
    """ログイン処理

    レート制限: 5回/分（ブルートフォース攻撃対策）
    CSRF保護: 有効
    """
    # CSRF検証
    await csrf_protect.validate_csrf(request)

    def _error_response(message: str) -> Response:
        # エラー時も新しいCSRFトークンを生成して返す
        csrf_token, signed_token = csrf_protect.generate_csrf_tokens()
        resp: Response = templates.TemplateResponse(
            "login.html",
            {"request": request, "error": message, "next": next, "csrf_token": csrf_token},
        )
        csrf_protect.set_csrf_cookie(signed_token, resp)
        return resp

    # 失敗時の応答は**常にこの1種類**。ユーザー不在・非アクティブ・ブロック中・パスワード違い
    # のどれでも同じ文面・ほぼ同じ処理（どれも bcrypt を1回）にして、応答から有効なユーザー名や
    # ブロックの有無が分からないようにする（ユーザー名の列挙・締め出しの確認を防ぐ）。
    failed_msg = "ユーザー名またはパスワードが正しくありません"

    # スロットルは (ユーザー名, クライアント IP) の組。存在しないユーザー名も同じく数える。
    throttle_key = make_throttle_key(username, get_remote_address(request))
    blocked = login_throttle.is_blocked(throttle_key)

    # ユーザーを検索（is_active では絞らず取得する）。NUL バイトを含む名前は PostgreSQL の
    # text に入らず、クエリが例外（500）になる。実在し得ない名前なので、クエリせず「不在」と
    # 同じに扱う（応答は他の失敗と同じ）
    if "\x00" in username:
        user = None
    else:
        result = await db.execute(
            select(User)
            .where(User.username == username)
            .options(selectinload(User.user_roles).selectinload(UserRole.role))
        )
        user = result.scalar_one_or_none()

    # 旧方式のアカウントロック（locked_until）が残っている行: 期限切れなら自動解除（監査に記録）。
    # 期限内のものは下で「ブロック中」と同じ扱いにする（応答では区別しない）。
    if user is not None and user.locked_until is not None and not user.is_locked():
        user.reset_lockout()
        db.add(AuthEvent(user_id=user.id, action=AuthEventAction.UNLOCKED, reason="時間経過"))
        await db.commit()

    # 認証チェック。検証できない場合（ユーザー不在/非アクティブ/旧ロック中/ブロック中）も
    # ダミーハッシュで bcrypt を実行し、応答時間でも区別できないようにする。
    # ブロック中は正しいパスワードでもここで必ず失敗にする（試行は止まる）。
    if user is not None and user.is_active and not user.is_locked() and not blocked:
        password_ok = verify_password(password, user.password_hash)
    else:
        verify_password(password, _DUMMY_PASSWORD_HASH)
        password_ok = False

    if not password_ok:
        logger.warning(f"ログイン失敗: username={username[:64]!r}")
        if not blocked:
            tripped = login_throttle.register_failure(
                throttle_key,
                max_attempts=settings.lockout_max_attempts,
                window_seconds=settings.lockout_duration_minutes * 60,
            )
            if tripped:
                logger.warning(
                    f"ログイン失敗が続いたため受け付けを止めました: username={username[:64]!r}, "
                    f"ip={throttle_key[1]}, minutes={settings.lockout_duration_minutes}"
                )
                # 管理者向けの証跡（応答には出さない）。実在するユーザーのときだけ残す
                if user is not None:
                    db.add(
                        AuthEvent(
                            user_id=user.id,
                            action=AuthEventAction.LOCKED,
                            reason=(
                                f"連続ログイン失敗 {settings.lockout_max_attempts} 回"
                                f"（IP {throttle_key[1]} からの試行を "
                                f"{settings.lockout_duration_minutes} 分間受け付けない）"
                            ),
                            failed_count=settings.lockout_max_attempts,
                        )
                    )
                    await db.commit()
        return _error_response(failed_msg)

    # password_ok=True は user が存在し有効な場合のみ（型の絞り込み）
    assert user is not None

    # 認証成功 → この (ユーザー名, IP) の失敗を忘れる。旧方式の失敗カウントも戻す
    login_throttle.reset(throttle_key)
    if user.failed_login_count or user.locked_until is not None:
        user.reset_lockout()
        await db.commit()

    # **この入り口で使える役割が無ければ入れない**。黙って「役割なし」で通すと、
    # ホームだけ見えて何も押せない画面になる。どの入り口へ行けばよいかを言う
    outside = user.held_role_names_outside_entry()
    if user.default_active_role() is None and outside:
        entry = current_entry()
        hint = entry.url_for(outside)
        where = f"（{hint[1]}）" if hint else ""
        logger.info(
            f"入り口外の役割によるログイン拒否: user_id={user.id} entry={entry.name!r} roles={outside}"
        )
        return _error_response(
            f"この入り口{('「' + entry.name + '」') if entry.name else ''}では、"
            f"あなたの役割（{'・'.join(RoleType.display_name(r) for r in outside)}）は使えません。"
            f"{RoleType.display_name(outside[0])}の入り口{where}からログインしてください"
        )

    # JWTトークン生成（既定アクティブ役割=最高権限を act クレームに載せる）
    access_token = create_access_token(
        data={
            "sub": str(user.id),
            "act": user.default_active_role(),
            "tv": user.token_version,
        }
    )

    logger.info(
        f"ログイン成功: user_id={user.id}, username={user.username}, "
        f"active_role={user.default_active_role()}"
    )

    # リダイレクト先はトップ（/）に任せ、/ 側でアクティブ役割ごとに振り分ける。
    # next は自サイト内の相対パスに限定（オープンリダイレクト防止）。
    redirect_url = _safe_redirect_path(next)
    response = RedirectResponse(url=redirect_url, status_code=303)

    # アクセストークン＋リフレッシュトークンを Cookie に設定
    refresh_token = await _issue_session_refresh_token(user, db)
    if refresh_token is None:  # 認証の直後にパスワード再設定などが入った。失敗と同じに返す
        return _error_response(failed_msg)
    _set_access_cookie(response, access_token)
    _set_refresh_cookie(response, refresh_token)

    return response


@router.post("/switch-role")
async def switch_role(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
    role: Annotated[str, Form()],
    csrf_protect: CsrfProtect = Depends(),
    next_url: Annotated[str | None, Form(alias="next")] = None,
) -> Response:
    """アクティブ役割を切り替える（ハード・スコープ）

    保有（=切替可能な）役割でない場合は何もせずトップへ戻す。
    JWT を再発行し、新しい act クレームで Cookie を上書きする。
    `next` があれば切替後にその画面へ戻す（役割不足の 403 からの復帰導線）。
    """
    await csrf_protect.validate_csrf(request)

    target = _safe_redirect_path(next_url)

    if not current_user.can_act_as(role):
        logger.warning(f"不正な役割切替: user_id={current_user.id}, role={role}")
        return RedirectResponse(url="/", status_code=303)

    # **有効期限は延ばさない。** 元のトークンの残り時間だけを引き継ぐ。切替のたびに新しい
    # 期限を発行すると、盗まれたアクセストークンを切替で更新し続けられてしまう。
    # `get_current_user` を通っているので、署名・期限・世代（tv）は検証済み。
    old_payload = decode_access_token(request.cookies.get(settings.jwt_cookie_name, ""))
    exp = old_payload.get("exp") if old_payload else None
    if not isinstance(exp, int | float):
        return RedirectResponse(url="/login", status_code=303)
    remaining = datetime.fromtimestamp(exp, UTC) - datetime.now(UTC)
    if remaining <= timedelta(0):
        return RedirectResponse(url="/login", status_code=303)

    access_token = create_access_token(
        data={"sub": str(current_user.id), "act": role, "tv": current_user.token_version},
        expires_delta=remaining,
    )
    logger.info(f"役割切替: user_id={current_user.id} -> active_role={role} next={target}")

    response = RedirectResponse(url=target, status_code=303)
    response.set_cookie(
        key=settings.jwt_cookie_name,
        value=access_token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=max(1, int(remaining.total_seconds())),
    )
    return response


@router.get("/logout")
async def logout_get() -> RedirectResponse:
    """GET では**何も変更しない**（ログイン画面へ戻すだけ）。

    以前はここでログアウトしていたので、外部ページからの遷移（`<img src="/logout">` や
    リンク）で利用者を勝手にログアウトさせられた。ログアウトは POST（CSRF 検証あり）。
    """
    return RedirectResponse(url="/login", status_code=303)


@router.post("/logout")
async def logout(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    csrf_protect: CsrfProtect = Depends(),
) -> RedirectResponse:
    """ログアウト処理（CSRF 検証あり）。

    このユーザーの**すべての**発行済みトークンを無効にする: リフレッシュトークンは全件失効、
    アクセストークンは世代番号を +1 して期限前でも使えなくする。盗まれたトークンがログアウト後も
    生き残らないためで、他の端末のセッションも終わる。Cookie は両方削除する。
    """
    await csrf_protect.validate_csrf(request)

    # 持ち主の特定は、署名を検証した Cookie だけから行う（他人のセッションは触らない）
    user_id: int | None = None
    access_payload = decode_access_token(request.cookies.get(settings.jwt_cookie_name, ""))
    if access_payload and str(access_payload.get("sub", "")).isdigit():
        user_id = int(access_payload["sub"])
    refresh_cookie = request.cookies.get(settings.jwt_refresh_cookie_name)
    refresh_payload = decode_refresh_token(refresh_cookie) if refresh_cookie else None
    if user_id is None and refresh_payload:
        rt_result = await db.execute(
            select(RefreshToken).where(RefreshToken.jti == refresh_payload["jti"])
        )
        rt = rt_result.scalar_one_or_none()
        user_id = rt.user_id if rt else None

    if user_id is not None:
        user_row = (await db.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
        if user_row is not None:
            await revoke_user_sessions(db, user_row)
            await db.commit()

    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(key=settings.jwt_cookie_name)
    response.delete_cookie(key=settings.jwt_refresh_cookie_name)
    return response


@router.post("/token/refresh")
async def refresh_access_token(
    request: Request,
    db: Annotated[AsyncSession, Depends(get_db)],
    csrf_protect: CsrfProtect = Depends(),
) -> Response:
    """リフレッシュトークンで新しいアクセストークンを発行する。

    ローテーション式: 旧リフレッシュトークンを revoke し新トークンへ差し替える。
    失効済み/期限切れ/不明な jti は 401。CSRF 保護あり。
    """
    await csrf_protect.validate_csrf(request)

    token = request.cookies.get(settings.jwt_refresh_cookie_name)
    payload = decode_refresh_token(token) if token else None
    if not payload:
        return JSONResponse({"success": False, "message": "リフレッシュトークンが無効です"}, 401)

    rt_result = await db.execute(select(RefreshToken).where(RefreshToken.jti == payload["jti"]))
    rt = rt_result.scalar_one_or_none()

    if rt is not None and rt.revoked:
        # 失効済み jti の再提示 = ローテ済みトークンの再利用（盗用の疑い）。
        # 当該ユーザーの全リフレッシュトークンを失効させる（家系失効, RFC 6819）。
        await db.execute(
            update(RefreshToken)
            .where(RefreshToken.user_id == rt.user_id, RefreshToken.revoked.is_(False))
            .values(revoked=True)
        )
        await db.commit()
        logger.warning(f"リフレッシュトークン再利用を検知: 家系失効 user_id={rt.user_id}")
        return JSONResponse({"success": False, "message": "リフレッシュトークンが無効です"}, 401)

    if rt is not None and rt.user_id != int(payload["sub"]):
        # jti の持ち主と sub が食い違う（持ち主を取り違えさせる形）。拒否する
        logger.warning("リフレッシュトークン拒否: jti の持ち主と sub が不一致")
        return JSONResponse({"success": False, "message": "リフレッシュトークンが無効です"}, 401)

    now = datetime.now(UTC)
    expires_at = rt.expires_at if rt else None
    if expires_at is not None and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if rt is None or (expires_at is not None and expires_at <= now):
        # DB に無い jti（改ざん）/ 期限切れ → 拒否
        logger.warning("リフレッシュトークン拒否: jti 無効/期限切れ")
        return JSONResponse({"success": False, "message": "リフレッシュトークンが無効です"}, 401)

    # ユーザー行を**ロックして**読む（PostgreSQL の FOR UPDATE。SQLite では無視される）。
    # 管理者の再設定・ログアウトも users 行を更新するので、どちらかが先にコミットするまで
    # もう一方は待つ。待ったあとでトークンの失効を読み直し、再設定の最中にローテーションが
    # 走って新しいトークンが生き残るのを防ぐ。
    user_result = await db.execute(
        select(User)
        .where(User.id == int(payload["sub"]), User.is_active.is_(True))
        .options(selectinload(User.user_roles).selectinload(UserRole.role))
        .with_for_update(of=User)
        .execution_options(populate_existing=True)
    )
    user = user_result.scalar_one_or_none()
    if user is None:
        return JSONResponse({"success": False, "message": "ユーザーが無効です"}, 401)
    await db.refresh(rt)
    if rt.revoked:
        return JSONResponse({"success": False, "message": "リフレッシュトークンが無効です"}, 401)
    if user.default_active_role() is None and user.held_role_names_outside_entry():
        # この入り口で使える役割が無い。役割なしのトークンを更新し続けない
        return JSONResponse(
            {"success": False, "message": "この入り口で使える役割がありません"}, 401
        )

    # ローテーション: 旧トークンを失効し、新アクセス＋新リフレッシュを発行
    rt.revoked = True
    access_token = create_access_token(
        data={
            "sub": str(user.id),
            "act": user.default_active_role(),
            "tv": user.token_version,
        }
    )
    refresh_token = await _issue_session_refresh_token(user, db)
    if refresh_token is None:  # 発行の最中に再設定・ログアウトが入った
        return JSONResponse({"success": False, "message": "リフレッシュトークンが無効です"}, 401)

    response: Response = JSONResponse({"success": True})
    _set_access_cookie(response, access_token)
    _set_refresh_cookie(response, refresh_token)
    logger.info(f"アクセストークン更新: user_id={user.id}")
    return response
