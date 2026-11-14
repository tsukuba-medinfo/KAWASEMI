"""F7/F8 の追加テスト: 版番号による書き込み条件、詳細画面のボタン、移行の穴埋め。

Reality Check で、SQLite（`FOR UPDATE` が効かない）では「状態が変わらない編集」が検証の
書き込みの前に割り込める、差戻しが編集の読み取りの後に割り込める、と分かった。
読んだ時点の状態と版（`edit_version`）を書き込みの条件にして、どの DB でも弾く。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import Update

from kawasemi.models.questionnaire_response import (
    QuestionnaireResponse,
    ResponseVerificationLog,
    VerificationStatus,
)
from kawasemi.models.role import Role
from kawasemi.models.user import User
from tests.conftest import TestSessionLocal, _make_user
from tests.lease_helpers import lease_patient
from tests.test_verification_workflow import _act_as, _make_response


async def _load(qr_id: int) -> QuestionnaireResponse:
    async with TestSessionLocal() as v:
        return (
            await v.execute(select(QuestionnaireResponse).where(QuestionnaireResponse.id == qr_id))
        ).scalar_one()


async def _actions(qr_id: int) -> list[str]:
    async with TestSessionLocal() as v:
        rows = await v.execute(
            select(ResponseVerificationLog.action)
            .where(ResponseVerificationLog.questionnaire_response_id == qr_id)
            .order_by(ResponseVerificationLog.id)
        )
        return [r[0] for r in rows]


async def _in_other_session(statement: Any) -> None:
    async with TestSessionLocal() as other:
        await other.execute(statement)
        await other.commit()


def _race_before_first_update(monkeypatch: pytest.MonkeyPatch, interloper: Any) -> dict[str, bool]:
    """リクエストの最初の UPDATE の直前に、別のセッションで interloper を確定させる。"""
    real_execute = AsyncSession.execute
    state = {"fired": False}

    async def execute(self: AsyncSession, statement: Any, *args: Any, **kw: Any) -> Any:
        if not state["fired"] and isinstance(statement, Update):
            state["fired"] = True
            await interloper()
        return await real_execute(self, statement, *args, **kw)

    monkeypatch.setattr(AsyncSession, "execute", execute)
    return state


def _bump(qr_id: int, **extra: Any) -> Any:
    return (
        update(QuestionnaireResponse)
        .where(QuestionnaireResponse.id == qr_id)
        .values(edit_version=QuestionnaireResponse.edit_version + 1, **extra)
    )


@pytest.mark.asyncio
async def test_verify_is_refused_when_an_edit_that_keeps_the_status_lands_first(
    client: AsyncClient,
    collector_user: User,
    reviewer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """検証の SoD 確認の後・書き込みの前に、状態を変えない編集が確定したら、検証は書かれない。"""
    _, qr = await _make_response(db_session, submitted_by_id=collector_user.id)
    qr_id = qr.id

    async def edit_elsewhere() -> None:
        await _in_other_session(_bump(qr_id, last_edited_by_id=collector_user.id))

    state = _race_before_first_update(monkeypatch, edit_elsewhere)
    _act_as(client, reviewer_user, "reviewer")
    resp = await client.post(
        f"/responses/{qr_id}/verify", headers={"X-CSRF-Token": csrf_token}, follow_redirects=False
    )
    assert state["fired"]
    assert resp.status_code == 409
    saved = await _load(qr_id)
    assert saved.verification_status == VerificationStatus.UNVERIFIED
    assert saved.verified_by_id is None


@pytest.mark.asyncio
async def test_a_dual_role_user_cannot_edit_between_the_sod_check_and_verify(
    client: AsyncClient,
    collector_user: User,
    collector_role: Role,
    reviewer_role: Role,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """兼任ユーザー自身の編集が、自分の検証の SoD 確認と書き込みの間に確定しても検証は通らない。"""
    _, qr = await _make_response(db_session, submitted_by_id=collector_user.id)
    qr_id = qr.id
    dual = await _make_user(
        db_session, "dual2", "兼任", "Passw0rd!x", [collector_role, reviewer_role]
    )

    async def own_edit_elsewhere() -> None:
        await _in_other_session(_bump(qr_id, last_edited_by_id=dual.id))

    state = _race_before_first_update(monkeypatch, own_edit_elsewhere)
    _act_as(client, dual, "reviewer")
    resp = await client.post(
        f"/responses/{qr_id}/verify", headers={"X-CSRF-Token": csrf_token}, follow_redirects=False
    )
    assert state["fired"]
    assert resp.status_code == 409
    saved = await _load(qr_id)
    assert saved.verification_status == VerificationStatus.UNVERIFIED
    assert saved.verified_by_id is None


@pytest.mark.asyncio
async def test_reject_is_refused_when_an_edit_lands_first(
    client: AsyncClient,
    collector_user: User,
    reviewer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, qr = await _make_response(db_session, submitted_by_id=collector_user.id)
    qr_id = qr.id

    async def edit_elsewhere() -> None:
        await _in_other_session(_bump(qr_id))

    state = _race_before_first_update(monkeypatch, edit_elsewhere)
    _act_as(client, reviewer_user, "reviewer")
    resp = await client.post(
        f"/responses/{qr_id}/reject",
        data={"reason": "理由"},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert state["fired"]
    assert resp.status_code == 409
    assert (await _load(qr_id)).verification_status == VerificationStatus.UNVERIFIED


@pytest.mark.asyncio
async def test_edit_is_refused_when_a_reject_lands_between_the_read_and_the_write(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """編集が「未検証」と読んだ後に差戻しが確定したら、編集は書かれない（再提出の判定がずれない）。"""
    _, qr = await _make_response(db_session, submitted_by_id=collector_user.id)
    qr_id = qr.id
    original = (await _load(qr_id)).content

    async def reject_elsewhere() -> None:
        await _in_other_session(
            update(QuestionnaireResponse)
            .where(QuestionnaireResponse.id == qr_id)
            .values(verification_status=VerificationStatus.REJECTED, rejection_reason="理由")
        )

    state = _race_before_first_update(monkeypatch, reject_elsewhere)
    resp = await collector_client.post(
        f"/responses/{qr_id}/edit",
        data={"q1": "差戻しの直前に読まれた編集"},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert state["fired"]
    assert resp.status_code == 409
    saved = await _load(qr_id)
    assert saved.verification_status == VerificationStatus.REJECTED
    assert saved.content == original
    assert await _actions(qr_id) == []


@pytest.mark.asyncio
async def test_a_second_concurrent_edit_does_not_overwrite_the_first(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, qr = await _make_response(db_session, submitted_by_id=collector_user.id)
    qr_id = qr.id

    async def other_edit() -> None:
        await _in_other_session(_bump(qr_id))

    state = _race_before_first_update(monkeypatch, other_edit)
    resp = await collector_client.post(
        f"/responses/{qr_id}/edit",
        data={"q1": "遅れた編集"},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert state["fired"]
    assert resp.status_code == 409
    assert "遅れた編集" not in str((await _load(qr_id)).content)


@pytest.mark.asyncio
async def test_each_edit_bumps_the_version(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    _, qr = await _make_response(db_session, submitted_by_id=collector_user.id)
    for _ in range(2):
        resp = await collector_client.post(
            f"/responses/{qr.id}/edit",
            data={"q1": "値"},
            headers={"X-CSRF-Token": csrf_token},
            follow_redirects=False,
        )
        assert resp.status_code == 303
    assert (await _load(qr.id)).edit_version == 2


@pytest.mark.asyncio
async def test_the_detail_page_hides_verify_for_someone_who_edited_the_response(
    client: AsyncClient,
    collector_user: User,
    reviewer_user: User,
    collector_role: Role,
    reviewer_role: Role,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """編集した兼任ユーザーには「検証済みにする」を出さない（押すと 403 になるため）。"""
    _, qr = await _make_response(db_session, submitted_by_id=collector_user.id)
    dual = await _make_user(
        db_session, "dual3", "兼任", "Passw0rd!x", [collector_role, reviewer_role]
    )
    await lease_patient(db_session, dual.id, "p-001")
    headers = {"X-CSRF-Token": csrf_token}

    _act_as(client, dual, "reviewer")
    before = await client.get(f"/responses/{qr.id}")
    assert "検証済みにする" in before.text

    _act_as(client, dual, "collector")
    await client.post(
        f"/responses/{qr.id}/edit", data={"q1": "編集"}, headers=headers, follow_redirects=False
    )
    _act_as(client, dual, "reviewer")
    after = await client.get(f"/responses/{qr.id}")
    assert after.status_code == 200
    assert "検証済みにする" not in after.text

    _act_as(client, reviewer_user, "reviewer")
    other = await client.get(f"/responses/{qr.id}")
    assert "検証済みにする" in other.text


# ----- 移行（過去の編集の穴埋め） -----


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}"}
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        capture_output=True,
        text=True,
    )


def _insert_row(conn: sqlite3.Connection, table: str, values: dict[str, Any]) -> None:
    """NOT NULL で既定値の無い列は、型に応じた値で埋めて1行入れる。"""
    cols = conn.execute(f"PRAGMA table_info({table})").fetchall()
    row = dict(values)
    for _, name, ctype, notnull, default, pk in cols:
        if name in row or not notnull or default is not None or pk:
            continue
        up = (ctype or "").upper()
        if "INT" in up or "BOOL" in up:
            row[name] = 0
        elif "DATE" in up or "TIME" in up:
            row[name] = "2026-01-01 00:00:00"
        elif "JSON" in up:
            row[name] = "{}"
        else:
            row[name] = "x"
    names = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    conn.execute(f"INSERT INTO {table} ({names}) VALUES ({marks})", list(row.values()))


def test_migration_backfills_the_last_editor_from_the_author(tmp_path: Path) -> None:
    """移行前の編集は編集者を残していない。`author` が `User/<id>` で提出者と違えば写す。"""
    db_path = tmp_path / "legacy.db"
    assert _alembic(db_path, "upgrade", "e2a4c6e8b0d1").returncode == 0
    with sqlite3.connect(db_path) as conn:
        for i in (1, 2, 3):
            conn.execute(
                "INSERT INTO users (id, username, email, password_hash, full_name, is_active, "
                "failed_login_count, token_version, created_at, updated_at) VALUES "
                "(?, ?, ?, 'x', 'n', 1, 0, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (i, f"u{i}", f"u{i}@example.com"),
            )
        _insert_row(
            conn,
            "questionnaires",
            {"id": 1, "fhir_id": "q1", "title": "t", "status": "active", "content": "{}"},
        )
        cases = {
            1: "User/2",  # 提出者は 1、author は 2: 編集者と推定
            2: "User/1",  # 提出者と同じ: 編集の痕跡なし
            3: "Practitioner/x",  # 推定できない
            4: "User/99",  # 実在しないユーザー
            5: "User/abc",  # 数字でない
        }
        for rid, author in cases.items():
            _insert_row(
                conn,
                "questionnaire_responses",
                {
                    "id": rid,
                    "fhir_id": f"r{rid}",
                    "questionnaire_id": 1,
                    "content": "{}",
                    "status": "completed",
                    "author": author,
                    "submitted_by_id": 1,
                },
            )
    after = _alembic(db_path, "upgrade", "head")
    assert after.returncode == 0, after.stderr
    with sqlite3.connect(db_path) as conn:
        got = dict(conn.execute("SELECT id, last_edited_by_id FROM questionnaire_responses"))
        versions = {r[0] for r in conn.execute("SELECT edit_version FROM questionnaire_responses")}
    assert got == {1: 2, 2: None, 3: None, 4: None, 5: None}
    assert versions == {0}
