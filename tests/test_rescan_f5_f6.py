"""再スキャンの指摘 F5 / F6 のテスト（一般的な修正のみ。施設固有の内容は含まない）。

- F5: ユーザーを削除しても、そのユーザーのセッション（リフレッシュトークン）が残り、SQLite で
  id が再利用されると、削除済みユーザーの Cookie が次に作ったアカウントのものとして通った。
- F6: 回答の編集で、クライアントが送る `subject` を採ると、担当外の患者へ付け替えられた。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.api.main import app
from kawasemi.core.auth import create_refresh_token
from kawasemi.core.config import settings
from kawasemi.models.dataset import Dataset
from kawasemi.models.input_target import InputTarget
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.questionnaire_response import QuestionnaireResponse
from kawasemi.models.refresh_token import RefreshToken
from kawasemi.models.role import Role
from kawasemi.models.user import User
from kawasemi.web.auth_routes import limiter
from tests.conftest import TestSessionLocal, _make_user
from tests.e2e.helpers import extract_form_csrf_token
from tests.test_verification_workflow import _make_response

_PAGE = "/datasets"


def _new_client() -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


async def _real_login(client: AsyncClient, username: str, password: str) -> str:
    limiter.reset()
    page = await client.get("/login")
    token = extract_form_csrf_token(page.text)
    resp = await client.post(
        "/login",
        data={"username": username, "password": password, "csrf_token": token},
        headers={"X-CSRF-Token": token},
    )
    assert resp.status_code == 303
    return token


async def _can_open(client: AsyncClient) -> bool:
    return (await client.get(_PAGE, follow_redirects=False)).status_code == 200


# ===== F5 =====


@pytest.mark.asyncio
async def test_deleted_users_session_does_not_pass_to_the_next_account(
    admin_client: AsyncClient,
    admin_user: User,
    collector_role: Role,
    admin_role: Role,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """最大の id のユーザーを消して別のユーザーを作っても、元の Cookie は新アカウントで通らない。"""
    victim = await _make_user(db_session, "last_user", "最後の人", "Victim123!", [collector_role])
    old_id = victim.id

    async with _new_client() as ex_user:
        csrf = await _real_login(ex_user, "last_user", "Victim123!")
        assert await _can_open(ex_user)

        resp = await admin_client.post(
            f"/admin/users/{old_id}/delete",
            headers={"X-CSRF-Token": csrf_token},
            data={"csrf_token": csrf_token},
            follow_redirects=False,
        )
        assert resp.status_code == 303

        # 管理者が続けて新しいユーザー（最高位の役割）を作る
        replacement = await _make_user(
            db_session, "replacement", "後任", "Replace123!", [admin_role]
        )
        assert replacement.id != old_id, "削除済みの id が再利用された"

        # 元ユーザーの Cookie は、アクセスもリフレッシュも通らない
        assert not await _can_open(ex_user)
        refreshed = await ex_user.post("/token/refresh", headers={"X-CSRF-Token": csrf})
        assert refreshed.status_code == 401

    # リフレッシュトークンの行も残らない
    async with TestSessionLocal() as v:
        rows = (
            (await v.execute(select(RefreshToken).where(RefreshToken.user_id == old_id)))
            .scalars()
            .all()
        )
        assert rows == []


@pytest.mark.asyncio
async def test_delete_user_releases_the_leased_target(
    admin_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """削除するユーザーが持っていたリースは外れる（外部キーが強制されない DB でも）。"""
    q = Questionnaire(
        fhir_id="q-del",
        title="削除テスト票",
        status="active",
        content={"resourceType": "Questionnaire"},
    )
    db_session.add(q)
    await db_session.flush()
    dataset = Dataset(name="削除テスト用")
    db_session.add(dataset)
    await db_session.flush()
    target = InputTarget(
        dataset_id=dataset.id,
        questionnaire_id=q.id,
        patient_id="p-del-1",
        started_by_id=collector_user.id,
        lease_expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    db_session.add(target)
    await db_session.commit()
    target_id = target.id

    resp = await admin_client.post(
        f"/admin/users/{collector_user.id}/delete",
        headers={"X-CSRF-Token": csrf_token},
        data={"csrf_token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    async with TestSessionLocal() as v:
        t = (await v.execute(select(InputTarget).where(InputTarget.id == target_id))).scalar_one()
        assert t.started_by_id is None
        assert t.lease_expires_at is None


@pytest.mark.asyncio
async def test_refresh_requires_the_token_owner_to_match_sub(
    client: AsyncClient,
    collector_user: User,
    admin_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """jti の持ち主と sub が食い違うリフレッシュトークンは受け付けない。"""
    # admin の jti 行を、collector の sub で提示する（持ち主の取り違え）
    db_session.add(
        RefreshToken(
            jti="owner-mismatch-1",
            user_id=admin_user.id,
            expires_at=datetime.now(UTC) + timedelta(days=1),
            revoked=False,
        )
    )
    await db_session.commit()
    client.cookies.set(
        settings.jwt_refresh_cookie_name,
        create_refresh_token(collector_user.id, "owner-mismatch-1"),
    )
    resp = await client.post("/token/refresh", headers={"X-CSRF-Token": csrf_token})
    assert resp.status_code == 401
    # 失効もローテーションも起きていない（行は未失効のまま）
    async with TestSessionLocal() as v:
        row = (
            await v.execute(select(RefreshToken).where(RefreshToken.jti == "owner-mismatch-1"))
        ).scalar_one()
        assert row.revoked is False


def test_users_table_never_reuses_ids_in_sqlite() -> None:
    """SQLite の users は AUTOINCREMENT（最大の行を消しても id が戻らない）。"""
    from sqlalchemy.dialects import sqlite
    from sqlalchemy.schema import CreateTable

    ddl = str(CreateTable(User.__table__).compile(dialect=sqlite.dialect()))
    assert "AUTOINCREMENT" in ddl


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}"}
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
    )


_INSERT_USER = (
    "INSERT INTO users (id, username, email, password_hash, full_name, is_active, "
    "failed_login_count, token_version, created_at, updated_at) "
    "VALUES (?, ?, ?, 'x', 'n', 1, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
)


def test_migration_makes_users_autoincrement_and_keeps_rows(tmp_path: Path) -> None:
    """既存の DB（AUTOINCREMENT なし）へ移行しても行・子表の行は残り、id は再利用されない。"""
    db_path = tmp_path / "existing.db"
    before = _alembic(db_path, "upgrade", "d1f3b5a7c9e2")
    assert before.returncode == 0, before.stderr
    with sqlite3.connect(db_path) as conn:
        users_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='users'").fetchone()[0]
        assert "AUTOINCREMENT" not in users_sql
        for i in (1, 2):
            conn.execute(_INSERT_USER, (i, f"u{i}", f"u{i}@example.com"))
        conn.execute(
            "INSERT INTO refresh_tokens (jti, user_id, expires_at, revoked, created_at, "
            "updated_at) VALUES ('j1', 2, '2099-01-01', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )

    after = _alembic(db_path, "upgrade", "head")
    assert after.returncode == 0, after.stderr
    with sqlite3.connect(db_path) as conn:
        users_sql = conn.execute("SELECT sql FROM sqlite_master WHERE name='users'").fetchone()[0]
        assert "AUTOINCREMENT" in users_sql
        assert [r[0] for r in conn.execute("SELECT id FROM users ORDER BY id")] == [1, 2]
        # 子表の行は消えていない
        kept = conn.execute("SELECT count(*) FROM refresh_tokens WHERE user_id = 2").fetchone()[0]
        assert kept == 1
        # 最大の id を消して作り直しても、id は戻らない
        conn.execute("DELETE FROM users WHERE id = 2")
        conn.execute(
            "INSERT INTO users (username, email, password_hash, full_name, is_active, "
            "failed_login_count, token_version, created_at, updated_at) VALUES "
            "('u3', 'u3@example.com', 'x', 'n', 1, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        assert conn.execute("SELECT id FROM users WHERE username='u3'").fetchone()[0] == 3


# ===== F6 =====


@pytest.mark.asyncio
async def test_edit_cannot_repoint_the_response_to_another_patient(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """編集で subject を別の患者に変えて送っても、保存済みの患者のまま。author はサーバーで固定。"""
    _, qr = await _make_response(db_session, submitted_by_id=collector_user.id)
    assert qr.subject == "Patient/p-001"

    resp = await collector_client.post(
        f"/responses/{qr.id}/edit",
        data={
            "subject": "Patient/not-my-patient",
            "author": "Practitioner/spoofed",
            "q1": "修正値",
        },
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    async with TestSessionLocal() as v:
        saved = (
            await v.execute(select(QuestionnaireResponse).where(QuestionnaireResponse.id == qr.id))
        ).scalar_one()
        assert saved.subject == "Patient/p-001"
        assert saved.content["subject"]["reference"] == "Patient/p-001"
        assert saved.author == f"User/{collector_user.id}"
        assert saved.content["author"]["reference"] == f"User/{collector_user.id}"
        # 回答自体は更新される
        assert "修正値" in str(saved.content)
