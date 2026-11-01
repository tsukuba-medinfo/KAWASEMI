"""取得プランの確認（承認）と実行の対応を保つことのテスト。

人が承認した program と実行される program が同一であることが設計の核である。

- 確認済みプランの program を変えて保存すると draft へ戻り、確認者・確認日時が消える
  （`recompile` と同じ扱い）。step・params・needs_input のどれを変えても同じ。
- 正準形（キー順・null 表記に依らない JSON）が同一の保存は確認済みを保つ。
- 手順書が確認済みでなければ取得プランを確認できない（compile / recompile と同じ前提）。
- draft へ戻ったプランは実行されない（実行経路は confirmed のみを読む）。
- 実行記録（program_snapshot）は program と承認の帰属（confirmed_by_id / confirmed_at）を固定する。
"""

import copy
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.executor import program_snapshot
from kawasemi.ai_input.models import InputPlan, InputRunbook
from kawasemi.ai_input.routes import _program_canonical, _program_changed
from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import RoleType
from kawasemi.models.user import User

_SQL = "SELECT v FROM t WHERE pid = :patient_id"
_PROGRAM: dict[str, Any] = {
    "steps": [{"op": "sql_extract", "id": "s1", "sql": _SQL}],
    "return": "$s1",
}


def _act_as(client: AsyncClient, user: User, role: str) -> None:
    token = create_access_token(data={"sub": str(user.id), "act": role})
    client.cookies.set(settings.jwt_cookie_name, token)


async def _setup(db: AsyncSession, *, runbook_status: str = "confirmed") -> Questionnaire:
    q = Questionnaire(
        fhir_id="q-plan-integrity",
        title="承認整合テスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-plan-integrity",
            "item": [{"linkId": "item1", "text": "項目1", "type": "string"}],
        },
    )
    db.add(q)
    await db.flush()
    db.add(
        InputRunbook(
            questionnaire_id=q.id,
            link_id="item1",
            item_text="項目1",
            steps=[],
            status=runbook_status,
        )
    )
    await db.commit()
    await db.refresh(q)
    return q


async def _get_plan(db: AsyncSession, qid: int) -> InputPlan:
    result = await db.execute(
        select(InputPlan).where(InputPlan.questionnaire_id == qid, InputPlan.link_id == "item1")
    )
    plan = result.scalar_one()
    await db.refresh(plan)
    return plan


async def _save(
    client: AsyncClient, qid: int, program: dict[str, Any], csrf: str, *, confirm: bool
) -> Any:
    return await client.post(
        f"/ai-input/questionnaires/{qid}/plan/item1?confirm={'true' if confirm else 'false'}",
        json={"program": program},
        headers={"X-CSRF-Token": csrf},
    )


async def _confirmed(
    client: AsyncClient, user: User, db: AsyncSession, csrf: str
) -> tuple[Questionnaire, InputPlan]:
    q = await _setup(db)
    _act_as(client, user, RoleType.DESIGNER)
    resp = await _save(client, q.id, _PROGRAM, csrf, confirm=True)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "confirmed"
    plan = await _get_plan(db, q.id)
    assert plan.confirmed_by_id == user.id
    assert plan.confirmed_at is not None
    return q, plan


# ---------------------------------------------------------------------------
# 単体: program の同一性判定
# ---------------------------------------------------------------------------


def test_canonical_ignores_key_order_and_null_noise() -> None:
    """キー順や null のキーの有無は「同一」とみなす（保存時と同じ正規化を通す）。"""
    reordered = {
        "return": "$s1",
        "steps": [{"sql": _SQL, "id": "s1", "op": "sql_extract", "reasoning": None}],
    }
    assert not _program_changed(_PROGRAM, reordered)
    # params の既定値（空）の有無も同一
    assert not _program_changed(_PROGRAM, {**_PROGRAM, "params": []})


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p["steps"][0].__setitem__("sql", "SELECT w FROM t WHERE pid = :patient_id"),
        lambda p: p["steps"][0].__setitem__("needs_input", True),
        lambda p: p["steps"][0].__setitem__("reasoning", "設計根拠を書き換えた"),
        lambda p: p.__setitem__("params", [{"name": "code", "value": "X"}]),
        lambda p: p.__setitem__("return", None),
    ],
    ids=["sql", "needs_input", "reasoning", "params", "return"],
)
def test_program_changed_detects_edits(mutate: Any) -> None:
    """step の引数・needs_input・設計根拠・params・return のどれを変えても「変わった」。"""
    edited = copy.deepcopy(_PROGRAM)
    mutate(edited)
    assert _program_changed(_PROGRAM, edited)


def test_canonical_falls_back_to_raw_for_unparseable_program() -> None:
    """スキーマを通らない旧データは生の dict で比べ、例外にしない。"""
    legacy = {"steps": "not-a-list"}
    assert _program_canonical(legacy) == _program_canonical({"steps": "not-a-list"})
    assert _program_changed(legacy, _PROGRAM)


def test_program_snapshot_pins_approval_attribution() -> None:
    """実行記録は program と承認の帰属（確認者・確認日時）を固定する。"""
    at = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    plan = InputPlan(
        questionnaire_id=1,
        link_id="item1",
        program=_PROGRAM,
        status="confirmed",
        confirmed_by_id=42,
        confirmed_at=at,
    )
    snap = program_snapshot([plan])["item1"]
    assert snap["program"] == _PROGRAM
    assert snap["confirmed_by_id"] == 42
    assert snap["confirmed_at"] == at.isoformat()


# ---------------------------------------------------------------------------
# HTTP: 確認済みプランの編集
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_editing_confirmed_plan_demotes_to_draft(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """確認済みの program を変えて保存すると draft へ戻り、確認者・確認日時が消える。"""
    q, _ = await _confirmed(designer_client, designer_user, db_session, csrf_token)

    edited = copy.deepcopy(_PROGRAM)
    edited["steps"][0]["sql"] = "SELECT w FROM t WHERE pid = :patient_id"
    resp = await _save(designer_client, q.id, edited, csrf_token, confirm=False)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "draft"
    assert body["demoted"] is True
    assert "確認し直して" in body["warning"]

    plan = await _get_plan(db_session, q.id)
    assert plan.status == "draft"
    assert plan.confirmed_by_id is None
    assert plan.confirmed_at is None
    assert plan.program["steps"][0]["sql"] == edited["steps"][0]["sql"]


@pytest.mark.asyncio
async def test_adding_needs_input_step_demotes_to_draft(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """要入力（needs_input）の step を足した保存も draft へ戻す（確認時なら拒否される内容）。"""
    q, _ = await _confirmed(designer_client, designer_user, db_session, csrf_token)

    edited = copy.deepcopy(_PROGRAM)
    edited["steps"].append(
        {"op": "sql_extract", "id": "s2", "sql": "SELECT 1", "needs_input": True}
    )
    resp = await _save(designer_client, q.id, edited, csrf_token, confirm=False)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "draft"

    plan = await _get_plan(db_session, q.id)
    assert plan.status == "draft"
    assert plan.confirmed_by_id is None


@pytest.mark.asyncio
async def test_param_edit_demotes_to_draft(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """params の編集も program の変更であり draft へ戻す。"""
    q, _ = await _confirmed(designer_client, designer_user, db_session, csrf_token)

    edited = copy.deepcopy(_PROGRAM)
    edited["params"] = [{"name": "code", "value": "X"}]
    edited["steps"][0]["sql"] = "SELECT v FROM t WHERE pid = :patient_id AND c = :code"
    resp = await _save(designer_client, q.id, edited, csrf_token, confirm=False)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "draft"
    assert (await _get_plan(db_session, q.id)).confirmed_at is None


@pytest.mark.asyncio
async def test_identical_save_keeps_confirmed_and_confirmer(
    designer_client: AsyncClient,
    designer_user: User,
    multi_role_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """正準形が同一の保存（キー順だけ違う）は確認済みと元の確認者・日時を保つ。

    別の利用者が保存しても確認者は書き換わらない（何も承認していないため）。
    """
    q, plan = await _confirmed(designer_client, designer_user, db_session, csrf_token)
    confirmer, confirmed_at = plan.confirmed_by_id, plan.confirmed_at

    _act_as(designer_client, multi_role_user, RoleType.DESIGNER)
    same = {"return": "$s1", "steps": [{"sql": _SQL, "id": "s1", "op": "sql_extract"}]}
    resp = await _save(designer_client, q.id, same, csrf_token, confirm=False)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "confirmed"
    assert "demoted" not in body

    plan = await _get_plan(db_session, q.id)
    assert plan.status == "confirmed"
    assert plan.confirmed_by_id == confirmer
    assert plan.confirmed_at == confirmed_at


@pytest.mark.asyncio
async def test_draft_save_of_draft_plan_is_not_reported_as_demotion(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """もともと draft のプランの保存は draft のままで、demoted を立てない。"""
    q = await _setup(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    resp = await _save(designer_client, q.id, _PROGRAM, csrf_token, confirm=False)
    edited = copy.deepcopy(_PROGRAM)
    edited["steps"][0]["sql"] = "SELECT 2"
    resp = await _save(designer_client, q.id, edited, csrf_token, confirm=False)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "draft"
    assert "demoted" not in resp.json()


# ---------------------------------------------------------------------------
# HTTP: 手順書の確認を前提にする
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_confirm_refused_when_runbook_not_confirmed(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """手順書が draft のまま取得プランを確認しようとすると 400 で拒否し、何も確定しない。"""
    q = await _setup(db_session, runbook_status="draft")
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await _save(designer_client, q.id, _PROGRAM, csrf_token, confirm=True)
    assert resp.status_code == 400
    assert resp.json()["success"] is False
    assert "手順書" in resp.json()["message"]

    result = await db_session.execute(select(InputPlan).where(InputPlan.questionnaire_id == q.id))
    assert result.scalars().first() is None


@pytest.mark.asyncio
async def test_draft_save_allowed_when_runbook_not_confirmed(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """確認しない保存（draft）は手順書が未確認でも従来どおり受け付ける（実行されない）。"""
    q = await _setup(db_session, runbook_status="draft")
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await _save(designer_client, q.id, _PROGRAM, csrf_token, confirm=False)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "draft"


# ---------------------------------------------------------------------------
# HTTP: draft へ戻ったプランは実行されない
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_demoted_plan_is_not_executed(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """編集で draft へ戻ったプランは autofill の対象にならず、実行は 400 で拒否される。"""
    monkeypatch.setattr(settings, "trial_run_patient_ids", "PAT-1")
    q, _ = await _confirmed(designer_client, designer_user, db_session, csrf_token)

    async def autofill() -> Any:
        return await designer_client.post(
            f"/ai-input/questionnaires/{q.id}/autofill",
            json={"patient_id": "PAT-1"},
            headers={"X-CSRF-Token": csrf_token},
        )

    # 確認済みの間は実行できる
    assert (await autofill()).status_code == 200

    edited = copy.deepcopy(_PROGRAM)
    edited["steps"][0]["sql"] = "SELECT w FROM t WHERE pid = :patient_id"
    await _save(designer_client, q.id, edited, csrf_token, confirm=False)

    resp = await autofill()
    assert resp.status_code == 400
    assert "確認済みプラン" in resp.json()["message"]
