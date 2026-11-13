"""FastAPI アプリケーションのエントリーポイント"""

import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlparse

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from fastapi_csrf_protect import CsrfProtect
from fastapi_csrf_protect.exceptions import CsrfProtectError
from pydantic import BaseModel
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded

from kawasemi.core.config import settings
from kawasemi.core.database import get_db
from kawasemi.core.deps import RoleRequiredError
from kawasemi.core.entry import current_entry
from kawasemi.core.exceptions import (
    KawasemiBaseException,
    ResourceNotFoundException,
    ValidationException,
)
from kawasemi.core.logging_config import get_logger, setup_logging
from kawasemi.core.rate_limit import limiter
from kawasemi.models.role import Role, RoleType
from kawasemi.web.template_globals import register_template_globals

# ロギングのセットアップ
setup_logging()
logger = get_logger(__name__)


# CSRF設定クラス
class CsrfSettings(BaseModel):
    # フィールド名は fastapi-csrf-protect の LoadConfig が認識するものに合わせる。
    # 旧名 cookie_name / cookie_httponly はライブラリに無視され、既定値
    # （cookie_key="fastapi-csrf-token", httponly=True）が使われてしまうため、
    # クッキー名が form.html の JS（kawasemi_csrf_token を参照）と不一致になり、
    # httponly=True で JS から読めない不具合が生じていた。
    secret_key: str = settings.csrf_secret_key
    cookie_key: str = "kawasemi_csrf_token"
    cookie_secure: bool = settings.cookie_secure
    cookie_samesite: str = "lax"
    httponly: bool = False  # JavaScriptからアクセス可能にする必要あり
    header_name: str = "X-CSRF-Token"
    token_key: str = "csrf_token"  # フォームボディからトークンを取得するキー名
    # cookie 寿命を JWT に合わせる（既定 3600s だと、ログイン1時間後に
    # cookie だけ失効し、JWT が有効なのにナビの役割切替フォームが
    # CSRF トークンを持てず CSRF_TOKEN_INVALID になる穴を塞ぐ）
    max_age: int = settings.jwt_access_token_expire_minutes * 60


@CsrfProtect.load_config  # type: ignore[arg-type]
def get_csrf_config() -> CsrfSettings:
    """CSRF設定を取得"""
    return CsrfSettings()


class UnicodeJSONResponse(JSONResponse):
    """日本語などのマルチバイト文字をエスケープせずに返すJSONレスポンス"""

    def render(self, content: object) -> bytes:
        return json.dumps(content, ensure_ascii=False).encode("utf-8")


# FastAPI アプリケーションの作成
app = FastAPI(
    title=settings.app_name,
    version=settings.app_version,
    description="JASPEHR Questionnaire/QuestionnaireResponse テンプレート入力システム",
    debug=settings.debug,
    default_response_class=UnicodeJSONResponse,
    # リバースプロキシのサブパス配下（例 /kawasemi）に対応。空ならルート直下。
    # FastAPI が scope["root_path"] を設定し、Starlette のルート照合は
    # root_path を除去後に行うため、nginx がプレフィックスを strip しても
    # しなくてもルーティングは成立する。
    root_path=settings.root_path,
)


@app.middleware("http")
async def add_root_path_to_redirects(request: Request, call_next):  # type: ignore[no-untyped-def]
    """サブパス配下のとき、内部リダイレクト先（Location）へプレフィックスを補う。

    アプリ内の RedirectResponse は "/login" のようにプレフィックス無しの
    絶対パスを返す。サブパス配下ではこのままだとプロキシ外側で
    プレフィックスが欠けたパスへ飛んでしまうため、ここで一括補正する。
    既にプレフィックス済み・外部URL（//, http）は二重付与しない。
    root_path が空（ルート直下）のときは何もしない。
    """
    response = await call_next(request)
    root = request.scope.get("root_path", "")
    if root:
        loc = response.headers.get("location")
        if (
            loc
            and loc.startswith("/")
            and not loc.startswith("//")
            and loc != root
            and not loc.startswith(f"{root}/")
        ):
            response.headers["location"] = f"{root}{loc}"
    return response


# レート制限の設定。**Limiter は `core/rate_limit.py` の1つだけ**。
# 各ルータもそこから import する。別インスタンスを作ると IP 単位のカウンタが割れ、
# ここに載っていない側の制限は 429 応答の組み立てにも参加しない。
app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)  # type: ignore[arg-type]

# 静的ファイル配信（オフライン/オンプレ前提でローカル vendor した Mermaid.js 等）
_STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"
if _STATIC_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=str(_STATIC_DIR)), name="static")

# CORS ミドルウェアの設定
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.allowed_hosts_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# グローバルエラーハンドラー
@app.exception_handler(ResourceNotFoundException)
async def resource_not_found_handler(
    request: Request, exc: ResourceNotFoundException
) -> UnicodeJSONResponse:
    """リソース未検出エラーハンドラー"""
    logger.warning(f"リソースが見つかりません: {exc.message}", extra={"error_code": exc.error_code})
    return UnicodeJSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content={
            "error_code": exc.error_code,
            "message": exc.message,
            "details": exc.details,
        },
    )


@app.exception_handler(ValidationException)
async def validation_exception_handler(
    request: Request, exc: ValidationException
) -> UnicodeJSONResponse:
    """バリデーションエラーハンドラー"""
    logger.warning(f"バリデーションエラー: {exc.message}", extra={"error_code": exc.error_code})
    return UnicodeJSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content={
            "error_code": exc.error_code,
            "message": exc.message,
            "details": exc.details,
        },
    )


@app.exception_handler(KawasemiBaseException)
async def kawasemi_exception_handler(
    request: Request, exc: KawasemiBaseException
) -> UnicodeJSONResponse:
    """KAWASEMI 共通エラーハンドラー"""
    logger.error(f"エラーが発生しました: {exc.message}", extra={"error_code": exc.error_code})
    return UnicodeJSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error_code": exc.error_code,
            "message": exc.message,
            "details": exc.details,
        },
    )


@app.exception_handler(CsrfProtectError)
async def csrf_protect_exception_handler(
    request: Request, exc: CsrfProtectError
) -> UnicodeJSONResponse:
    """CSRF保護エラーハンドラー"""
    logger.warning(f"CSRF検証エラー: {exc.message}")
    return UnicodeJSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={
            "error_code": "CSRF_TOKEN_INVALID",
            "message": "CSRFトークンが無効です",
        },
    )


# 役割不足の 403 は「切り替えればできる」ことを画面で案内する（行き止まりにしない）。
_role_templates = Jinja2Templates(directory="kawasemi/web/templates")
register_template_globals(_role_templates)


def _wants_html(request: Request) -> bool:
    """ブラウザの画面遷移か（API 呼び出しでないか）を Accept ヘッダで判定する。"""
    return "text/html" in request.headers.get("accept", "")


def _safe_next(request: Request) -> str:
    """切替後に戻る先。**外部サイトへは飛ばさない**（オープンリダイレクト対策）。

    GET はその URL 自身へ戻す。POST は再送できないので、直前に見ていた画面
    （Referer）へ戻す。どちらも自サイト内の絶対パスでなければ "/" にする。
    """
    url = request.url if request.method == "GET" else urlparse(request.headers.get("referer", ""))
    candidate = url.path + (f"?{url.query}" if url.query else "")
    # 判定規則を2つ持たない（ログイン後の next と同じもの。循環 import を避けて遅延取り込み）
    from kawasemi.web.auth_routes import _safe_redirect_path

    return _safe_redirect_path(candidate)


@app.exception_handler(RoleRequiredError)
async def role_required_handler(request: Request, exc: RoleRequiredError) -> Response:
    """役割不足の 403。画面には切替の導線を、API には従来どおり JSON を返す。"""
    logger.info(
        "役割不足で拒否: path=%s active_role=%s required=%s",
        request.url.path,
        exc.active_role,
        ",".join(exc.required_roles),
    )
    # この入り口で使えない役割の画面なら、切替ではなく別の入り口を案内する
    entry = current_entry()
    # 案内する入り口は、**利用者が保有する役割**のものだけ（持たない役割の入り口へ送ると、
    # そこでログインを拒否される）
    other_entry = (
        None
        if entry.allows_any(exc.required_roles)
        else entry.url_for([r for r in exc.required_roles if r in exc.held_outside])
    )
    if _wants_html(request):
        return _role_templates.TemplateResponse(
            "role_required.html",
            {
                "entry": entry,
                "outside_entry": not entry.allows_any(exc.required_roles),
                "other_entry": other_entry,
                "request": request,
                # ナビが見るのはこの3つだけ。ORM の User は例外ハンドラの時点で
                # セッションが閉じていて触れないので、値を写して渡す。
                "current_user": SimpleNamespace(
                    full_name=exc.full_name,
                    active_role=exc.active_role,
                    switchable_role_names=lambda: exc.switchable,
                    held_role_names_outside_entry=lambda: exc.held_outside,
                ),
                "active_role": exc.active_role,
                "required_roles": exc.required_roles,
                "switchable_to": exc.switchable_to,
                "next_url": _safe_next(request),
            },
            status_code=status.HTTP_403_FORBIDDEN,
        )
    return UnicodeJSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={
            "detail": exc.detail,
            "required_roles": exc.required_roles,
            "active_role": exc.active_role,
            "entry": entry.name,
            "outside_entry": not entry.allows_any(exc.required_roles),
            "other_entry": {"role": other_entry[0], "url": other_entry[1]} if other_entry else None,
        },
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception) -> UnicodeJSONResponse:
    """一般的な例外ハンドラー"""
    # ログには完全な情報を記録（運用者が確認可能）
    logger.exception("予期しないエラーが発生しました", exc_info=exc)

    # クライアントには最小限の情報のみ返す（デバッグモードでも詳細は非公開）
    return UnicodeJSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error_code": "INTERNAL_SERVER_ERROR",
            "message": "内部サーバエラーが発生しました。詳細はログを確認してください。",
        },
    )


# スタートアップイベント
def _warn_if_the_index_disagrees() -> None:
    """索引がクエリ側の設定で検索できない状態なら、起動時に1度だけ報告する。

    embedding モデルや `SCHEMA_VERSION` が索引側と食い違うと、検索は候補を1件も
    返さなくなる（`store.is_searchable` が偽）。**動かす前に分かる**ようにする。

    **索引が無い環境では黙る。** CI・テスト・索引を使わない運用まで警告で埋めると、
    本当に食い違っているときの1行が埋もれる。
    """
    try:
        from kawasemi.ai_input.resource_index.store import get_store

        store = get_store(settings.resource_index_dir)
        if store.build_info is None:
            return  # 索引が置かれていない（この環境では検索を使わない）
        reasons: list[str] = []
        if not store.schema_compatible:
            reasons.append(f"schema_version: 索引={store.build_info.schema_version}")
        if not store.matches_model(settings.embedding_model):
            reasons.append(
                f"embedding_model: 索引={store.build_info.embedding_model!r} / "
                f"設定={settings.embedding_model!r}"
            )
    except Exception as e:  # noqa: BLE001 - 起動を止めない（検索を使わない運用がある）
        logger.warning(f"リソース索引の照合をスキップしました: {e}")
        return

    if reasons:
        logger.error("リソース索引をこの設定では検索できません: " + " / ".join(reasons))


async def _warn_if_the_schema_disagrees() -> None:
    """実スキーマとモデル宣言の食い違いを起動時に1度だけ報告する。

    **止めずに警告する。** 評価の記録を保持する DB なので、起動を止めると記録も止まる。
    列が1つ欠けているだけなら、その列を読まない画面は使える。落とすべきかどうかは
    運用が決められるように、判断材料（どの表のどの列か）を出すところまでを担う。

    たとえば `input_targets.needs_attention` の欠けは、その列を読む画面を人が開くまで
    分からない。`alembic_version` は head を主張しており、**`alembic upgrade head`
    では直らない**（alembic は当該版を適用済みと信じて何もしない）。
    """
    try:
        from kawasemi.core.database import engine
        from kawasemi.core.schema_check import inspect_schema_async

        drift = await inspect_schema_async(engine)
    except Exception as e:  # noqa: BLE001 - 検査の失敗を理由に起動を止めない
        logger.warning(f"スキーマの照合をスキップしました: {e}")
        return

    if drift:
        logger.error(
            "実スキーマがモデル宣言と食い違っています。"
            "その列を読む画面は 500 になります: " + " / ".join(drift.messages())
        )
    else:
        logger.info("実スキーマはモデル宣言と一致しています")


@app.on_event("startup")
async def startup_event() -> None:
    """アプリケーション起動時の処理"""
    logger.info(f"{settings.app_name} v{settings.app_version} を起動しています...")
    logger.info(f"環境: {settings.environment}")
    # 入り口。役割名の誤りはここで止める（黙って全役割に倒すと制限が消える）
    entry = current_entry()
    logger.info(
        "入り口: %s 役割=%s",
        entry.name or "（区別なし）",
        ",".join(entry.roles) if entry.roles else "全役割",
    )
    if entry.name and not entry.roles:
        logger.warning("ENTRY_NAME だけが設定され ENTRY_ROLES が空。入り口の名前は出るが制限は無い")
    logger.info(
        f"候補探索AI(ENABLE_AI_DRAFTING): {'有効' if settings.enable_ai_drafting else '無効'}"
    )
    logger.info(
        f"自動構造化AI(ENABLE_AI_EXTRACTION): {'有効' if settings.enable_ai_extraction else '無効'}"
    )

    # **索引と埋め込みモデルの不一致を起動時に出す**。
    #
    # `embedding_model` が索引側と食い違うとベクトル空間が別物になり、**索引の検索そのものが
    # 無効化される**（`store` が `schema_compatible` / `matches_model` で早期 return する）。
    # 典拠の付与率は「候補が1件も出ない」形で静かに 0 へ落ちるので、**動かす前に分かる**
    # ようにする。索引が置かれていない環境（CI・テスト）では何も言わない。
    _warn_if_the_index_disagrees()

    # **移行を当て損ねた既存 DB を起動時に出す**。
    #
    # CI もテストも DB を新規に作るので、この種の欠けは発火しない。列が欠けたまま
    # 何週間も動き続けうる。止めはせず、どの表の
    # どの列が無いかを 1 行で出す。
    await _warn_if_the_schema_disagrees()

    # **プログラム SQL の読み取り専用ロールが無いことを起動時に出す**。止めはしない
    # （読み取り専用トランザクションが書き込みを止める）が、ロールの権限で閉じていない。
    from kawasemi.core.database import engine as _app_engine
    from kawasemi.core.readonly_db import readonly_route_warning

    readonly_warning = readonly_route_warning(_app_engine.dialect.name)
    if readonly_warning:
        logger.warning(readonly_warning)

    # 本番環境でのセキュリティチェック
    if settings.environment == "production":
        # DEBUG=falseの確認
        if settings.debug:
            raise ValueError("本番環境ではDEBUG=falseにしてください")

        # SECRET_KEYの強度チェック（最低32文字はFieldで保証されている）
        # 推測可能な文字列のチェック
        weak_keys = [
            "your-secret-key-here",
            "change-in-production",
            "secret",
            "password",
            "test",
            "example",
        ]
        if any(weak in settings.secret_key.lower() for weak in weak_keys):
            raise ValueError(
                "本番環境で脆弱なSECRET_KEYが検出されました。"
                "セキュリティ上安全なランダム文字列を使用してください。"
            )

        logger.info("セキュリティチェック: OK")
    else:
        # 本番ハードニングのフェイルセーフ: DEBUG / LOG_LEVEL=DEBUG の拒否と弱鍵チェックは
        # environment=="production" でのみ効く（secure Cookie は COOKIE_SECURE で環境に依らず付く）。
        # 見ているのは設定の HOST で、実際の待受アドレスは起動コマンドの --host が決める。
        # dev-on-LAN の正当運用もあるため fail-fast はしない。
        if settings.host in ("0.0.0.0", "::"):  # 全IF(IPv4/IPv6)を指す設定を検知して警告する
            logger.warning(
                "ENVIRONMENT=%s で HOST=%s（全インターフェイス）が設定されています"
                "（実際の待受アドレスは起動時の --host で決まります）。DEBUG / LOG_LEVEL=DEBUG の"
                "拒否と弱鍵チェックは production でのみ有効です。公開運用時は "
                "ENVIRONMENT=production を設定してください。",
                settings.environment,
                settings.host,
            )
        if settings.debug:
            logger.warning(
                "DEBUG=true です。SQLバインドパラメータ等が詳細ログに露出し得るため、"
                "公開運用では無効化してください。"
            )

    # 不足ロールをシード（既存ロールはスキップ）
    from sqlalchemy import select

    async for db in get_db():
        for role_name in RoleType.ALL:
            result = await db.execute(select(Role).where(Role.name == role_name))
            if not result.scalar_one_or_none():
                db.add(
                    Role(
                        name=role_name,
                        description=RoleType.DESCRIPTIONS.get(role_name, ""),
                    )
                )
        await db.commit()
    logger.info("ロールのシード完了")


# シャットダウンイベント
@app.on_event("shutdown")
async def shutdown_event() -> None:
    """アプリケーション終了時の処理"""
    logger.info(f"{settings.app_name} を終了しています...")
    from kawasemi.core.readonly_db import dispose_readonly_engine

    await dispose_readonly_engine()


@app.get("/health")
async def health() -> dict[str, str]:
    """ヘルスチェックエンドポイント"""
    return {"status": "healthy"}


# API バージョン v100 のルータを追加（循環インポート回避のためappインスタンス化後に配置）
from kawasemi.ai_input.routes import router as ai_input_router  # noqa: E402
from kawasemi.api.v100 import router as v100_router  # noqa: E402
from kawasemi.web.admin_routes import router as admin_router  # noqa: E402
from kawasemi.web.auth_routes import router as auth_router  # noqa: E402
from kawasemi.web.dataset_routes import router as dataset_router  # noqa: E402
from kawasemi.web.routes import ai_web_router  # noqa: E402
from kawasemi.web.routes import router as web_router  # noqa: E402

app.include_router(v100_router, prefix="/api/v100")
app.include_router(auth_router)  # 認証ルーター（/login, /logout）
app.include_router(admin_router)  # 管理者ルーター（/admin/*）
app.include_router(dataset_router)  # データセット・入力対象ルーター
# データ自動収集（ステップ1〜4）は常時マウント。AI関与は端点ガードで制御する
# （DRAFTING=ドラフト生成系403 / EXTRACTION=LLM opプラン拒否。docs/ai_flags_spec.md）
app.include_router(ai_input_router)
app.include_router(ai_web_router)
app.include_router(web_router)
