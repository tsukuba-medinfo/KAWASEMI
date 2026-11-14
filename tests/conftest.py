"""pytest設定とフィクスチャ"""

import asyncio
import os
from collections.abc import AsyncGenerator
from pathlib import Path

# `kawasemi.api.main` の import で Settings シングルトンが生成される前に設定する。
# テストの HTTP クライアント（下の `client` fixture）は `http://test` という
# 非 HTTPS の base_url で接続するため、Cookie の既定 Secure=True だと
# httpx がクッキーを送り返さず CSRF ダブルサブミットや認証が壊れる。
# 実ネットワークではない test transport に限った緩和であり、本番の既定は変えない。
os.environ.setdefault("COOKIE_SECURE", "false")

import bcrypt
import pytest
import yaml
from fastapi_csrf_protect import CsrfProtect
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from kawasemi.api.main import app
from kawasemi.core.auth import create_access_token, get_password_hash
from kawasemi.core.config import settings
from kawasemi.core.database import get_db
from kawasemi.models.base import Base
from kawasemi.models.role import Role, RoleType
from kawasemi.models.user import User, UserRole

# --- bcrypt のコストをテストプロセス内でだけ下げる --------------------
# bcrypt は既定 cost 12 で **1ハッシュ 309ms**（実測）。下の user 系 fixture は
# **テストごとに**ユーザーを作るので、これが setup 時間の最大要因になっていた
# （約370回 ≈ 114秒。suite 全体の setup 299.6秒の 38%）。
#
# **本番のコストは据え置きである。** `kawasemi/core/auth.py` の `get_password_hash` は
# 引数なしで `bcrypt.gensalt()` を呼ぶので、ここで既定値を差し替えるとテストプロセス内
# だけが軽くなる。検証側（`bcrypt.checkpw`）はコストをハッシュ文字列から読むため、
# 下げた分だけそのまま速くなる。
#
# **テストが見ているのは「ハッシュが照合できるか」だけで、コストの強度は見ていない**
# （`$2b$12$` やコスト値を assert するテストは無く、応答時間の平準化——
# `web/auth_routes.py` のユーザー不在時も1回走らせる処理——を検査するテストも無い）。
# 強度そのものを検査するテストを足すときは、そのテストで `_bcrypt_gensalt` を使う。
_bcrypt_gensalt = bcrypt.gensalt
bcrypt.gensalt = lambda rounds=4, prefix=b"2b": _bcrypt_gensalt(rounds=rounds, prefix=prefix)

# テスト用のインメモリSQLite
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

test_engine = create_async_engine(
    TEST_DATABASE_URL,
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)


@event.listens_for(test_engine.sync_engine, "connect")
def _enable_sqlite_foreign_keys(dbapi_connection, connection_record):  # type: ignore[no-untyped-def]
    """本番（core/database.py）と同じく外部キーを強制する。宙に浮いた id を入れる fixture を許さない。"""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


TestSessionLocal = sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture(scope="function")
async def db_session() -> AsyncGenerator[AsyncSession, None]:
    """テスト用データベースセッション"""
    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with TestSessionLocal() as session:
        yield session

    async with test_engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


@pytest.fixture(scope="function")
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    """テスト用HTTPクライアント"""

    async def override_get_db():
        yield db_session

    app.dependency_overrides[get_db] = override_get_db

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()


@pytest.fixture(scope="session")
def event_loop():
    """イベントループのフィクスチャ"""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def _reset_login_rate_limit():
    """各テスト前にログインのレート制限カウンタをリセットする。

    ログインのレート制限（既定 5回/分。上限値は `settings.rate_limit_login`）は
    プロセス内で共有されるグローバルな Limiter（インメモリ storage）で実現されて
    いるため、複数テストのログインが累積して意図せず 429 を返す。テスト毎に
    クリアしてテスト間の状態漏れを防ぐ。
    （`test_login_rate_limiting` 自体はクリーンな状態から 6 回試行するため影響なし。）

    **Limiter はプロセスに1つである**。`core/rate_limit.py` が
    その1つを持ち、`api/main.py` の `app.state.limiter` も各ルータもそれを使う。
    """
    from kawasemi.core.config import settings
    from kawasemi.core.login_throttle import account_backoff, login_throttle
    from kawasemi.core.rate_limit import limiter

    limiter.reset()
    # (ユーザー名, IP) ごとの失敗カウントもプロセス内で共有される。テスト間へ持ち越さない
    login_throttle.clear()
    account_backoff.clear()
    # アカウント単位の遅延（試行が重なると秒単位で待たせる）は、既定ではテストを遅くするだけなので
    # 切る。遅延そのものを確かめるテストは、自分で設定を戻す（tests/test_login_backoff.py）。
    original = settings.login_backoff_base_seconds
    settings.login_backoff_base_seconds = 0.0
    yield
    settings.login_backoff_base_seconds = original


@pytest.fixture(autouse=True)
def _default_ai_flags_off(monkeypatch: pytest.MonkeyPatch):
    """AI機能フラグ（enable_ai_drafting / enable_ai_extraction）を既定Falseへ固定する。

    実行環境の .env が ENABLE_AI_* を立てていると、フラグに暗黙依存するテストが
    ローカルでは通り CI（.github/workflows/ci.yml が常に true 固定）でも通るが、
    フラグ OFF 構成では検出できない。フラグを明示したいテストは、この
    fixture の後に個別の monkeypatch.setattr(settings, "enable_ai_*", ...) で
    上書きすればよく、その挙動は変わらない。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", False)
    monkeypatch.setattr(settings, "enable_ai_extraction", False)
    yield


@pytest.fixture(autouse=True)
def _no_entry_restriction(monkeypatch: pytest.MonkeyPatch):
    """入り口（ENTRY_ROLES）の制限を無しに固定する。

    実行環境の .env が入り口を絞っていると、役割切替や 403 の既存テストが環境で変わる。
    入り口を試すテストは個別に settings を差し替えて `reset_entry_cache()` する。
    """
    from kawasemi.core.entry import reset_entry_cache

    monkeypatch.setattr(settings, "entry_name", "")
    monkeypatch.setattr(settings, "entry_roles", "")
    monkeypatch.setattr(settings, "entry_urls", "")
    reset_entry_cache()
    yield
    reset_entry_cache()


@pytest.fixture(autouse=True)
def _no_reference_scopes(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    """典拠の範囲（`site/reference_scopes.yaml`）を読まない状態に固定する。

    施設の設定ファイルがあると、載っていない様式（テストの問診票）には典拠が出なくなり、
    実行環境によってテストの結果が変わる。範囲を試すテストは個別にパスを差し替える。
    """
    from kawasemi.ai_input.reference_scope import reset_reference_scope_cache

    missing = tmp_path_factory.getbasetemp() / "no_reference_scopes.yaml"
    monkeypatch.setattr(settings, "reference_scopes_file", str(missing))
    reset_reference_scope_cache()
    yield
    reset_reference_scope_cache()


@pytest.fixture(autouse=True)
def _no_location_rules(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    """所在の規則（`site/location_rules.yaml`）を読まない状態に固定する。

    施設の設定ファイルがあると、所在の AI 案に受け皿の候補が足され、実行環境によって
    テストの結果が変わる。規則を試すテストは個別にパスを差し替える。
    """
    missing = tmp_path_factory.getbasetemp() / "no_location_rules.yaml"
    monkeypatch.setattr(settings, "location_rules_file", str(missing))


@pytest.fixture(autouse=True)
def _no_target_items(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    """集計の対象項目の設定（`site/target_items.yaml`）を読まない状態に固定する。

    施設・研究の設定ファイルがあると集計の分母が変わり、実行環境によってテストの結果が
    変わる。設定を試すテストは個別にパスを差し替える。
    """
    missing = tmp_path_factory.getbasetemp() / "no_target_items.yaml"
    monkeypatch.setattr(settings, "target_items_file", str(missing))


@pytest.fixture(autouse=True)
def _no_model_options(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: pytest.TempPathFactory):
    """モデルごとの options の例外（`site/model_options.yaml`）を読まない状態に固定する。

    施設の設定ファイルがあると、載っているモデルの要求に options が足され、実行環境に
    よってテストの結果が変わる。例外を試すテストは個別にパスを差し替える。
    """
    missing = tmp_path_factory.getbasetemp() / "no_model_options.yaml"
    monkeypatch.setattr(settings, "model_options_file", str(missing))


@pytest.fixture
def target_items_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """架空の対象項目の設定（`target_items.yaml`）を書いて読ませる。

    `{様式の url: {"target_item_numbers": [...], ...}}` を渡すと、その内容のファイルを
    作って `settings.target_items_file` に差し替え、パスを返す。
    """

    def write(questionnaires: dict[str, dict[str, list[str]]]) -> Path:
        path = tmp_path / "target_items.yaml"
        path.write_text(
            yaml.safe_dump({"questionnaires": questionnaires}, allow_unicode=True),
            encoding="utf-8",
        )
        monkeypatch.setattr(settings, "target_items_file", str(path))
        return path

    return write


# ===== ロールフィクスチャ（4役割） =====


async def _make_role(db_session: AsyncSession, name: str) -> Role:
    role = Role(name=name, description=RoleType.DESCRIPTIONS[name])
    db_session.add(role)
    await db_session.commit()
    await db_session.refresh(role)
    return role


async def _make_user(
    db_session: AsyncSession, username: str, full_name: str, password: str, roles: list[Role]
) -> User:
    user = User(
        username=username,
        email=f"{username}@test.com",
        password_hash=get_password_hash(password),
        full_name=full_name,
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    for role in roles:
        db_session.add(UserRole(user_id=user.id, role_id=role.id))
    await db_session.commit()
    return user


def _login(client: AsyncClient, user: User) -> AsyncClient:
    token = create_access_token(data={"sub": str(user.id)})
    client.cookies.set(settings.jwt_cookie_name, token)
    return client


@pytest.fixture
async def admin_role(db_session: AsyncSession) -> Role:
    """管理者ロール"""
    return await _make_role(db_session, RoleType.ADMIN)


@pytest.fixture
async def designer_role(db_session: AsyncSession) -> Role:
    """設計者ロール（臨床的意味・所在・手順書・プラン・様式の内容）"""
    return await _make_role(db_session, RoleType.DESIGNER)


@pytest.fixture
async def collector_role(db_session: AsyncSession) -> Role:
    """収集者ロール（対象の受け取り・自動収集の実行・手動入力・回答送信）"""
    return await _make_role(db_session, RoleType.COLLECTOR)


@pytest.fixture
async def reviewer_role(db_session: AsyncSession) -> Role:
    """閲覧・検証者ロール（回答の閲覧・チェック・差し戻し・エクスポート・メトリクス）"""
    return await _make_role(db_session, RoleType.REVIEWER)


# ===== ユーザーフィクスチャ =====


@pytest.fixture
async def admin_user(db_session: AsyncSession, admin_role: Role) -> User:
    """管理者ユーザー。**管理者だけ**を保有する（他の役割へ切り替えられない）"""
    return await _make_user(db_session, "admin_test", "管理者テスト", "AdminPass123!", [admin_role])


@pytest.fixture
async def designer_user(db_session: AsyncSession, designer_role: Role) -> User:
    """設計者ユーザー（1様式の設計を役割を切り替えずに通す）"""
    return await _make_user(
        db_session, "designer_test", "設計者テスト", "Designer123!", [designer_role]
    )


@pytest.fixture
async def collector_user(db_session: AsyncSession, collector_role: Role) -> User:
    """収集者ユーザー"""
    return await _make_user(
        db_session, "collector_test", "収集者テスト", "Collector123!", [collector_role]
    )


@pytest.fixture
async def reviewer_user(db_session: AsyncSession, reviewer_role: Role) -> User:
    """閲覧・検証者ユーザー"""
    return await _make_user(
        db_session, "reviewer_test", "閲覧検証者テスト", "Reviewer123!", [reviewer_role]
    )


@pytest.fixture
async def multi_role_user(
    db_session: AsyncSession, designer_role: Role, collector_role: Role, reviewer_role: Role
) -> User:
    """設計者・収集者・閲覧検証者の3役割を明示的に保有するユーザー（役割切替のテスト用）。

    以前は admin が全役割のスーパーセットだったので admin で切替を試していた。
    4役割では管理者は切替できないので、保有役割を明示したユーザーを使う。
    """
    return await _make_user(
        db_session,
        "multi_role_test",
        "複数役割テスト",
        "MultiRole123!",
        [designer_role, collector_role, reviewer_role],
    )


# ===== 認証済みクライアントフィクスチャ =====


@pytest.fixture
async def admin_client(client: AsyncClient, admin_user: User) -> AsyncClient:
    """管理者権限の認証済みクライアント"""
    return _login(client, admin_user)


@pytest.fixture
async def designer_client(client: AsyncClient, designer_user: User) -> AsyncClient:
    """設計者権限の認証済みクライアント"""
    return _login(client, designer_user)


@pytest.fixture
async def collector_client(client: AsyncClient, collector_user: User) -> AsyncClient:
    """収集者権限の認証済みクライアント"""
    return _login(client, collector_user)


@pytest.fixture
async def reviewer_client(client: AsyncClient, reviewer_user: User) -> AsyncClient:
    """閲覧・検証者権限の認証済みクライアント"""
    return _login(client, reviewer_user)


@pytest.fixture
async def multi_role_client(client: AsyncClient, multi_role_user: User) -> AsyncClient:
    """設計者・収集者・閲覧検証者を保有する利用者の認証済みクライアント"""
    return _login(client, multi_role_user)


# ===== CSRFトークンフィクスチャ =====


@pytest.fixture
async def csrf_token(client: AsyncClient) -> str:
    """CSRFトークンを取得し、署名済みクッキーをクライアントへ確立する

    fastapi-csrf-protect のダブルサブミット方式では、検証時に
    署名済みトークン（クッキー）と raw トークン（ヘッダ/フォーム）の対が必要。
    トークンを直接生成し、署名済みトークンをクライアントのクッキーにセットして
    返り値の raw トークンを X-CSRF-Token ヘッダや csrf_token フォーム値に使う。
    認証状態に依存せず（/login の 303 リダイレクトを避ける）安定して動作する。
    """
    csrf_protect = CsrfProtect()
    raw_token, signed_token = csrf_protect.generate_csrf_tokens()
    # ライブラリが実際に参照するクッキー名（_cookie_key）でセットする。
    # CsrfSettings.cookie_key により本番・テストとも "kawasemi_csrf_token"。
    client.cookies.set(csrf_protect._cookie_key, signed_token)
    return raw_token
