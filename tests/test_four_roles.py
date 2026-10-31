"""役割を4つに畳み、管理者をシステムの運用だけにする。

完了条件を固定する。

1. 役割が4つで、**管理者が設計・収集・検証の画面に到達できない**（患者データを見ない）
2. 1様式の設計（臨床的意味→所在→手順書→プラン）が、**役割を切り替えずに設計者だけで通る**
3. 既存 DB の利用者が移行後も同じ画面に入れる（alembic の写しを実際に走らせて測る）
4. 入り口と役割は1対1（`ENTRY_ROLES` に役割1つ）
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.routes import _CANVAS_STAGES, _stage_capability
from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.core.entry import parse_entry
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import RoleType
from kawasemi.models.user import User

_REPO = Path(__file__).resolve().parents[1]
_BEFORE = "b4c5d6e7f8a9"  # 7役割のまま
_THIS = "d5e6f7a8b9c0"  # 4役割


def _act(client: AsyncClient, user: User, act: str | None = None) -> None:
    data: dict[str, str] = {"sub": str(user.id)}
    if act is not None:
        data["act"] = act
    client.cookies.set(settings.jwt_cookie_name, create_access_token(data=data))


# --------------------------------------------------------------------------- #
# 1. 管理者は設計・収集・検証の画面に到達できない
# --------------------------------------------------------------------------- #


def test_there_are_exactly_four_roles_and_no_old_names() -> None:
    assert RoleType.ALL == ["admin", "designer", "collector", "reviewer"]
    for old in (
        "collection_lead",
        "plan_designer",
        "data_manager",
        "data_entry",
        "verifier",
        "data_user",
    ):
        assert old not in RoleType.ALL
        assert not hasattr(RoleType, old.upper()), f"旧役割の定数 {old.upper()} が残っている"
    assert not hasattr(RoleType, "DESIGN_ROLES")


# 管理者に閉じる画面（設計・収集・検証）。GET で 403 になること。
_NOT_FOR_ADMIN = [
    "/ai-input/dashboard",  # 設計
    "/ai-input/questionnaires/1/canvas",
    "/ai-input/questionnaires/1/source-survey",
    "/ai-input/metrics/acquisition",
    "/valuesets",  # 様式の内容は設計者
    "/questionnaires/1/edit",
    "/datasets",  # 収集
    "/datasets/new",
    "/datasets/1",
    "/assignments",
    "/input-targets",
    "/questionnaires/1/responses",  # 検証（回答＝患者データ）
    "/responses/1",
    "/responses/1/edit",
    "/datasets/1/check",
    "/forms",  # 入力の画面（以前は認証のみ）
    "/forms/1",
    "/api/patient/P1",
    "/api/encounter/E1",
    "/ai-input/questionnaires/1/autofill",  # autofill の画面（以前は認証のみ）
    "/ai-input/questionnaires/1/autofill/executions/1/validation",  # 確認書（PHI）
    "/ai-input/questionnaires/1/source-survey/system/assessments",  # 設計の集計（以前は認証のみ）
    "/ai-input/questionnaires/1/source-survey/clinical/citation-coverage",
    "/api/v100/questionnaire-responses/",
    "/api/v100/questionnaire-responses/1",
]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", _NOT_FOR_ADMIN)
async def test_admin_cannot_reach_design_collect_or_review_pages(
    admin_client: AsyncClient, path: str
) -> None:
    resp = await admin_client.get(path, headers={"Accept": "application/json"})
    assert resp.status_code == 403, f"{path}: {resp.status_code}"
    body = resp.json()
    assert body["active_role"] == RoleType.ADMIN
    assert RoleType.ADMIN not in body["required_roles"]


@pytest.mark.asyncio
async def test_admin_403_page_offers_no_switch(admin_client: AsyncClient) -> None:
    """管理者だけの利用者には切替先が無い。「依頼してください」になる"""
    resp = await admin_client.get("/ai-input/dashboard", headers={"Accept": "text/html"})
    assert resp.status_code == 403
    assert "切り替えて続ける" not in resp.text
    assert "依頼してください" in resp.text


@pytest.mark.asyncio
async def test_admin_cannot_submit_or_edit_responses(
    admin_client: AsyncClient, csrf_token: str
) -> None:
    """回答の投入・編集は収集者の行為。管理者は直接許可されない（以前は `data_entry, admin`）"""
    for path in ("/forms/1/submit", "/responses/1/edit", "/datasets/1/batch-autofill"):
        resp = await admin_client.post(path, data={}, headers={"X-CSRF-Token": csrf_token})
        assert resp.status_code == 403, f"{path}: {resp.status_code}"
    resp = await admin_client.post(
        "/api/v100/questionnaire-responses/",
        json={"questionnaire_id": 1, "answers": {}, "subject": "Patient/1"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_admin_still_runs_the_system(admin_client: AsyncClient, csrf_token: str) -> None:
    """管理者に残るもの: アカウント・役割の付与、様式のファイル取り込み"""
    assert (await admin_client.get("/admin/users")).status_code == 200
    assert (await admin_client.get("/questionnaires")).status_code == 200
    assert (await admin_client.get("/questionnaires/new")).status_code == 200
    resp = await admin_client.post(
        "/api/v100/questionnaires/",
        json={
            "resource": {
                "resourceType": "Questionnaire",
                "status": "active",
                "title": "取り込み",
                "item": [{"linkId": "q1", "type": "string", "text": "質問"}],
            }
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 201


@pytest.mark.asyncio
async def test_admin_home_does_not_link_to_patient_data(admin_client: AsyncClient) -> None:
    resp = await admin_client.get("/")
    assert resp.status_code == 200
    assert "/admin/users" in resp.text
    assert "/datasets" not in resp.text, "管理者のホームからデータセット（患者データ）へ導線がある"


# --------------------------------------------------------------------------- #
# 2. 1様式の設計が設計者だけで通る（役割を切り替えない）
# --------------------------------------------------------------------------- #

_SQL_PROGRAM = {
    "steps": [{"op": "sql_extract", "id": "s1", "sql": "SELECT v FROM t WHERE pid = :patient_id"}],
    "return": "$s1",
}


async def _questionnaire(db_session: AsyncSession) -> Questionnaire:
    q = Questionnaire(
        fhir_id="q-four-roles",
        title="4役割テスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-four-roles",
            "item": [{"linkId": "item1", "text": "項目1", "type": "string"}],
        },
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    return q


@pytest.mark.asyncio
async def test_a_single_designer_completes_the_design_without_switching(
    client: AsyncClient, designer_user: User, db_session: AsyncSession, csrf_token: str
) -> None:
    """臨床的意味→所在→手順書→プランを、`act` を一度も変えずに同じトークンで通す。"""
    q = await _questionnaire(db_session)
    _act(client, designer_user)  # act 無し＝既定の設計者。以後トークンを触らない
    h = {"X-CSRF-Token": csrf_token}
    base = f"/ai-input/questionnaires/{q.id}"

    # 臨床的意味（旧 collection_lead）
    resp = await client.post(
        f"{base}/source-survey/clinical?confirm=true",
        json={
            "items": [
                {
                    "link_id": "item1",
                    "item_text": "項目1",
                    "annotation": "起点は手術日。",
                    "citations": [],
                }
            ]
        },
        headers=h,
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["confirmed"] == 1

    # 所在（旧 data_manager）
    resp = await client.post(
        f"{base}/source-survey/system",
        json={
            "items": [
                {
                    "link_id": "item1",
                    "item_text": "項目1",
                    "candidates": [
                        {
                            "source_category": "local_db",
                            "location": "T.item1",
                            "unit_id": "db:T.item1",
                        }
                    ],
                }
            ]
        },
        headers=h,
    )
    assert resp.status_code == 200, resp.text

    # 手順書（旧 plan_designer）
    resp = await client.post(f"{base}/runbook/item1?confirm=true", json={"steps": []}, headers=h)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "confirmed"

    # 取得プラン（旧 plan_designer + data_manager の協働）
    resp = await client.post(
        f"{base}/plan/item1?confirm=true", json={"program": _SQL_PROGRAM}, headers=h
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "confirmed"

    # キャンバスの4段階がすべて「あなたが編集」（切替も閲覧のみも無い）
    resp = await client.get(f"{base}/canvas")
    assert resp.status_code == 200
    # 凡例の1回を除き、「ロール切替で編集」「他ロール担当」の段階が無い
    assert resp.text.count("cv-owner--switch") == 1 and resp.text.count("cv-owner--other") == 1
    assert resp.text.count("cv-owner--mine") == 1 + len(_CANVAS_STAGES)
    designer_user.active_role = RoleType.DESIGNER
    assert [_stage_capability(s["roles"], designer_user) for s in _CANVAS_STAGES] == ["edit"] * 4


def test_canvas_stages_are_all_owned_by_the_designer() -> None:
    assert [s["roles"] for s in _CANVAS_STAGES] == [[RoleType.DESIGNER]] * 4


@pytest.mark.asyncio
async def test_designer_cannot_submit_responses(
    designer_client: AsyncClient, csrf_token: str
) -> None:
    """設計者が持たないもの: 回答の入力・送信"""
    resp = await designer_client.post(
        "/forms/1/submit", data={}, headers={"X-CSRF-Token": csrf_token}
    )
    assert resp.status_code == 403
    assert (await designer_client.get("/input-targets")).status_code == 403


@pytest.mark.asyncio
async def test_collector_owns_datasets_assignments_and_batch(
    collector_client: AsyncClient,
) -> None:
    """管理者から収集者へ移したもの: データセット・割り当て（画面が開く）"""
    for path in (
        "/datasets",
        "/datasets/new",
        "/assignments",
        "/assignments/new",
        "/input-targets",
    ):
        assert (await collector_client.get(path)).status_code == 200, path
    # 設計の確定は持たない
    assert (await collector_client.get("/ai-input/dashboard")).status_code == 403


@pytest.mark.asyncio
async def test_reviewer_views_and_checks_but_does_not_input(
    reviewer_client: AsyncClient, csrf_token: str
) -> None:
    for path in ("/questionnaires?mode=response", "/datasets", "/ai-input/metrics/acquisition"):
        assert (await reviewer_client.get(path)).status_code == 200, path
    assert (await reviewer_client.get("/api/v100/questionnaire-responses/")).status_code == 200
    for path in ("/input-targets", "/ai-input/dashboard", "/valuesets"):
        assert (await reviewer_client.get(path)).status_code == 403, path
    resp = await reviewer_client.post(
        "/forms/1/submit", data={}, headers={"X-CSRF-Token": csrf_token}
    )
    assert resp.status_code == 403


# --------------------------------------------------------------------------- #
# 3. 既存 DB の移行（alembic を実際に走らせる）
# --------------------------------------------------------------------------- #


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}"}
    return subprocess.run(
        ["uv", "run", "alembic", *args], cwd=_REPO, env=env, capture_output=True, text=True
    )


def _seed_seven_roles(db: Path) -> dict[str, int]:
    """7役割と、役割の組み合わせが違う利用者を入れる。戻り値は username → user_id。"""
    old = [
        "admin",
        "collection_lead",
        "plan_designer",
        "data_manager",
        "data_entry",
        "verifier",
        "data_user",
        "ai_designer",  # さらに古い旧役割。既存 DB に残っていることがある
    ]
    users = {
        "ops": ["admin"],
        "legacy_ai": ["ai_designer"],  # 旧 ai_designer だけの利用者 → designer
        "legacy_ai_and_lead": ["ai_designer", "collection_lead"],  # 畳んで1行
        "three_designers": ["collection_lead", "plan_designer", "data_manager"],  # 畳んで1行
        "lead_only": ["collection_lead"],
        "entry": ["data_entry"],
        "qa": ["verifier", "data_user"],  # 畳んで1行
        "admin_and_entry": ["admin", "data_entry"],
        "nobody": [],
    }
    ids: dict[str, int] = {}
    with sqlite3.connect(db) as conn:
        for name in old:
            conn.execute(
                "INSERT INTO roles (name, description, created_at, updated_at) "
                "VALUES (?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (name, name),
            )
        role_id = {n: i for n, i in conn.execute("SELECT name, id FROM roles")}
        for username, roles in users.items():
            conn.execute(
                "INSERT INTO users (username, email, password_hash, full_name, is_active, "
                "failed_login_count, created_at, updated_at) "
                "VALUES (?, ?, 'x', ?, 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (username, f"{username}@x", username),
            )
            uid = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()[0]
            ids[username] = uid
            for r in roles:
                conn.execute(
                    "INSERT INTO user_roles (user_id, role_id, assigned_at) "
                    "VALUES (?, ?, CURRENT_TIMESTAMP)",
                    (uid, role_id[r]),
                )
        conn.commit()
    return ids


def _held(db: Path) -> dict[str, list[str]]:
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT u.username, r.name FROM user_roles ur "
            "JOIN users u ON u.id = ur.user_id JOIN roles r ON r.id = ur.role_id "
            "ORDER BY u.username, r.name"
        ).fetchall()
        names = {u for (u,) in conn.execute("SELECT username FROM users")}
    out: dict[str, list[str]] = {u: [] for u in names}
    for u, r in rows:
        out[u].append(r)
    return out


def test_the_migration_maps_seven_roles_to_four_without_duplicates(tmp_path: Path) -> None:
    db = tmp_path / "roles.db"
    before = _alembic(db, "upgrade", _BEFORE)
    assert before.returncode == 0, before.stderr[-2000:]
    _seed_seven_roles(db)

    done = _alembic(db, "upgrade", _THIS)
    assert done.returncode == 0, done.stderr[-2000:]

    with sqlite3.connect(db) as conn:
        roles = sorted(n for (n,) in conn.execute("SELECT name FROM roles"))
    assert roles == ["admin", "collector", "designer", "reviewer"], "旧役割が残っている"

    held = _held(db)
    assert held["ops"] == ["admin"]
    assert held["three_designers"] == ["designer"], "旧3役割が designer 1行に畳まれていない"
    assert held["lead_only"] == ["designer"]
    assert held["legacy_ai"] == ["designer"], "旧 ai_designer が designer に写されていない"
    assert held["legacy_ai_and_lead"] == ["designer"]
    assert held["entry"] == ["collector"]
    assert held["qa"] == ["reviewer"], "verifier / data_user が reviewer 1行に畳まれていない"
    assert held["admin_and_entry"] == ["admin", "collector"], "管理者に収集者が付与されたままでない"
    assert held["nobody"] == []

    # user_roles に (user_id, role_id) の重複が無い
    with sqlite3.connect(db) as conn:
        dup = conn.execute(
            "SELECT user_id, role_id, COUNT(*) FROM user_roles GROUP BY user_id, role_id "
            "HAVING COUNT(*) > 1"
        ).fetchall()
    assert dup == []


def test_the_migration_can_be_rolled_back(tmp_path: Path) -> None:
    """downgrade で旧7役割へ戻せる（設計者は旧3役割すべてに戻す）。"""
    db = tmp_path / "roles.db"
    assert _alembic(db, "upgrade", _BEFORE).returncode == 0
    _seed_seven_roles(db)
    assert _alembic(db, "upgrade", _THIS).returncode == 0

    back = _alembic(db, "downgrade", _BEFORE)
    assert back.returncode == 0, back.stderr[-2000:]
    held = _held(db)
    assert held["three_designers"] == ["collection_lead", "data_manager", "plan_designer"]
    assert held["lead_only"] == ["collection_lead", "data_manager", "plan_designer"]
    assert held["entry"] == ["data_entry"]
    assert held["legacy_ai"] == ["collection_lead", "data_manager", "plan_designer"]
    assert held["qa"] == ["data_user", "verifier"]
    assert held["ops"] == ["admin"]
    with sqlite3.connect(db) as conn:
        roles = {n for (n,) in conn.execute("SELECT name FROM roles")}
    assert "designer" not in roles and "collector" not in roles and "reviewer" not in roles


def test_the_migration_creates_the_four_roles_on_an_empty_db(tmp_path: Path) -> None:
    """旧役割の保持者が居なくても（空の DB でも）4役割は揃う。"""
    db = tmp_path / "empty.db"
    assert _alembic(db, "upgrade", _THIS).returncode == 0
    with sqlite3.connect(db) as conn:
        roles = sorted(n for (n,) in conn.execute("SELECT name FROM roles"))
    assert roles == ["admin", "collector", "designer", "reviewer"]


# --------------------------------------------------------------------------- #
# 4. 入り口と役割は1対1
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("role", RoleType.ALL)
def test_each_role_is_its_own_entry(role: str) -> None:
    others = ",".join(f"{r}=https://host/{r}/" for r in RoleType.ALL if r != role)
    e = parse_entry(RoleType.display_name(role), role, others)
    assert e.roles == (role,)
    assert all(e.allows(r) is (r == role) for r in RoleType.ALL)
    assert set(e.urls) == set(RoleType.ALL) - {role}


@pytest.mark.parametrize(
    "old",
    ["collection_lead", "plan_designer", "data_manager", "data_entry", "verifier", "data_user"],
)
def test_old_role_names_are_rejected_at_the_entry(old: str) -> None:
    with pytest.raises(ValueError):
        parse_entry("", old, "")
