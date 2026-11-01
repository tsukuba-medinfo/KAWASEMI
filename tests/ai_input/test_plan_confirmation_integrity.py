"""取得プランの確認（承認）と実行の対応を保つことのテスト。

人が承認した program と実行される program が同一であることが設計の核である。

- 確認済みプランの program を変えて保存すると draft へ戻り、確認者・確認日時が消える
  （`recompile` と同じ扱い）。step・params・needs_input のどれを変えても同じ。
- 正準形（キー順・null 表記に依らない JSON）が同一の保存は確認済みを保つ。
- 手順書が確認済みでなければ取得プランを確認できない（compile / recompile と同じ前提）。
- draft へ戻ったプランは実行されない（実行経路は confirmed のみを読む）。
- 実行記録（program_snapshot）は program と承認の帰属（confirmed_by_id / confirmed_at）を固定する。
- 手順書にも同じ規則を当てる（内容が変われば draft、同一なら確認済みを保つ）。ただし
  手順書の編集で取得プランは連動させない（実行されるのはプランの program である）。
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
from kawasemi.ai_input.routes import (
    _program_canonical,
    _program_changed,
    _runbook_canonical,
    _runbook_changed,
)
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


# ---------------------------------------------------------------------------
# 手順書: 同じ規則
# ---------------------------------------------------------------------------

_RB_STEPS: list[dict[str, Any]] = [
    {
        "order": 1,
        "title": "検査値を取得",
        "description": "直近の値",
        "action_type": "fetch",
        "data_hint": None,
    }
]


def test_runbook_canonical_ignores_key_order_defaults_and_order_field() -> None:
    """キー順・既定値の欠落・order の値（保存時に振り直す）は「同一」とみなす。"""
    stored = [
        {
            "order": 7,
            "data_hint": None,
            "action_type": "fetch",
            **{"description": "直近の値", "title": "検査値を取得"},
        }
    ]
    sent = [{"title": "検査値を取得", "description": "直近の値", "action_type": "fetch"}]
    assert not _runbook_changed(stored, sent)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda s: s[0].__setitem__("title", "別の題"),
        lambda s: s[0].__setitem__("description", "別の説明"),
        lambda s: s[0].__setitem__("action_type", None),
        lambda s: s[0].__setitem__("data_hint", "ヒント"),
        lambda s: s.append({"order": 2, "title": "追加", "description": ""}),
        lambda s: s.clear(),
    ],
    ids=["title", "description", "action_type", "data_hint", "append", "clear"],
)
def test_runbook_changed_detects_edits(mutate: Any) -> None:
    edited = copy.deepcopy(_RB_STEPS)
    mutate(edited)
    assert _runbook_changed(_RB_STEPS, edited)


def test_runbook_changed_detects_reorder() -> None:
    two = [{"title": "A"}, {"title": "B"}]
    assert _runbook_changed(two, list(reversed(two)))


def test_runbook_canonical_falls_back_to_raw() -> None:
    assert _runbook_canonical("legacy") == _runbook_canonical("legacy")
    assert _runbook_changed("legacy", _RB_STEPS)


async def _confirmed_runbook(
    client: AsyncClient, user: User, db: AsyncSession, csrf: str
) -> tuple[Questionnaire, InputRunbook]:
    q = await _setup(db, runbook_status="draft")
    _act_as(client, user, RoleType.DESIGNER)
    resp = await client.post(
        f"/ai-input/questionnaires/{q.id}/runbook/item1?confirm=true",
        json={"steps": _RB_STEPS},
        headers={"X-CSRF-Token": csrf},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "confirmed"
    rb = (
        await db.execute(
            select(InputRunbook).where(
                InputRunbook.questionnaire_id == q.id, InputRunbook.link_id == "item1"
            )
        )
    ).scalar_one()
    await db.refresh(rb)
    assert rb.confirmed_by_id == user.id
    return q, rb


async def _save_runbook(client: AsyncClient, qid: int, steps: Any, csrf: str) -> Any:
    return await client.post(
        f"/ai-input/questionnaires/{qid}/runbook/item1",
        json={"steps": steps},
        headers={"X-CSRF-Token": csrf},
    )


@pytest.mark.asyncio
async def test_editing_confirmed_runbook_demotes_to_draft(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """確認済み手順書の内容を変えて保存すると draft へ戻り、確認者・確認日時が消える。"""
    q, rb = await _confirmed_runbook(designer_client, designer_user, db_session, csrf_token)

    edited = copy.deepcopy(_RB_STEPS)
    edited[0]["description"] = "初回の値"
    resp = await _save_runbook(designer_client, q.id, edited, csrf_token)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "draft"
    assert body["demoted"] is True
    assert "確認し直して" in body["warning"]

    await db_session.refresh(rb)
    assert rb.status == "draft"
    assert rb.confirmed_by_id is None
    assert rb.confirmed_at is None


@pytest.mark.asyncio
async def test_identical_runbook_save_keeps_confirmed(
    designer_client: AsyncClient,
    designer_user: User,
    multi_role_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """同一内容の保存（キー順違い・既定値の省略）は確認済みと元の確認者を保つ。"""
    q, rb = await _confirmed_runbook(designer_client, designer_user, db_session, csrf_token)
    confirmer, confirmed_at = rb.confirmed_by_id, rb.confirmed_at

    _act_as(designer_client, multi_role_user, RoleType.DESIGNER)
    # order は保存時に振り直すので値が違っても同一。data_hint は既定値なので省略。
    same = [
        {"order": 5, "action_type": "fetch", "description": "直近の値", "title": "検査値を取得"}
    ]
    resp = await _save_runbook(designer_client, q.id, same, csrf_token)
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "confirmed"
    assert "demoted" not in resp.json()

    await db_session.refresh(rb)
    assert rb.status == "confirmed"
    assert rb.confirmed_by_id == confirmer
    assert rb.confirmed_at == confirmed_at


@pytest.mark.asyncio
async def test_runbook_edit_leaves_confirmed_plan_unchanged(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """手順書を編集して draft へ戻しても、確認済みの取得プランは確認済みのまま残る。

    実行されるのはプランの program で、それはプランの確認で承認済みである。
    """
    q, _ = await _confirmed_runbook(designer_client, designer_user, db_session, csrf_token)
    resp = await _save(designer_client, q.id, _PROGRAM, csrf_token, confirm=True)
    assert resp.status_code == 200, resp.text
    plan = await _get_plan(db_session, q.id)
    confirmer, confirmed_at = plan.confirmed_by_id, plan.confirmed_at

    edited = copy.deepcopy(_RB_STEPS)
    edited[0]["title"] = "別の題"
    resp = await _save_runbook(designer_client, q.id, edited, csrf_token)
    assert resp.json()["status"] == "draft"

    plan = await _get_plan(db_session, q.id)
    assert plan.status == "confirmed"
    assert plan.confirmed_by_id == confirmer
    assert plan.confirmed_at == confirmed_at


# ---------------------------------------------------------------------------
# 手順書が draft に戻った項目のプランを画面から見失わない
# ---------------------------------------------------------------------------


def test_build_plan_items_lists_plan_of_draft_runbook_read_only() -> None:
    """手順書が未確認でもプランがある項目は載せ、読み取り専用の印を付ける。

    プランの無い未確認手順書の項目は従来どおり載せない。
    """
    from kawasemi.ai_input.routes import _build_plan_items

    runbooks = {
        "a": InputRunbook(link_id="a", item_text="A", steps=[], status="confirmed"),
        "b": InputRunbook(link_id="b", item_text="B", steps=[], status="draft"),
        "c": InputRunbook(link_id="c", item_text="C", steps=[], status="draft"),
    }
    plans = {"b": InputPlan(link_id="b", program=_PROGRAM, status="confirmed")}
    items = {it["link_id"]: it for it in _build_plan_items(runbooks, plans)}
    assert set(items) == {"a", "b"}
    assert items["a"]["runbook_confirmed"] is True
    assert items["b"]["runbook_confirmed"] is False
    assert items["b"]["status"] == "confirmed"
    assert items["b"]["version"] is not None
    assert items["a"]["version"] is None  # プランがまだ無い


@pytest.mark.asyncio
async def test_plan_page_shows_confirmed_plan_after_runbook_demotion(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """手順書を編集して draft へ戻しても、確認済みプランは画面に読み取り専用で残り、
    確認は拒否され、下書きへ戻すことはできる。"""
    q, _ = await _confirmed_runbook(designer_client, designer_user, db_session, csrf_token)
    await _save(designer_client, q.id, _PROGRAM, csrf_token, confirm=True)
    edited = copy.deepcopy(_RB_STEPS)
    edited[0]["title"] = "別の題"
    assert (await _save_runbook(designer_client, q.id, edited, csrf_token)).json()[
        "status"
    ] == "draft"

    page = await designer_client.get(f"/ai-input/questionnaires/{q.id}/plan")
    assert page.status_code == 200
    assert '"runbook_confirmed": false' in page.text
    assert '"link_id": "item1"' in page.text
    assert "下書きに戻す" in page.text

    # 確認は手順書の確認待ち
    resp = await _save(designer_client, q.id, _PROGRAM, csrf_token, confirm=True)
    assert resp.status_code == 400

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/item1/demote",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["demoted"] is True
    plan = await _get_plan(db_session, q.id)
    assert plan.status == "draft"
    assert plan.confirmed_by_id is None
    assert plan.confirmed_at is None
    assert not _program_changed(plan.program, _PROGRAM)  # program には触らない


@pytest.mark.asyncio
async def test_demote_draft_plan_is_noop_and_missing_plan_is_404(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    q = await _setup(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    url = f"/ai-input/questionnaires/{q.id}/plan/item1/demote"
    resp = await designer_client.post(url, json={}, headers={"X-CSRF-Token": csrf_token})
    assert resp.status_code == 404

    await _save(designer_client, q.id, _PROGRAM, csrf_token, confirm=False)
    resp = await designer_client.post(url, json={}, headers={"X-CSRF-Token": csrf_token})
    assert resp.status_code == 200
    assert resp.json()["demoted"] is False


# ---------------------------------------------------------------------------
# 楽観的排他（版トークン）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_plan_save_with_stale_version_is_409_and_does_not_overwrite(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """画面を開いた後に別の操作で変わった行は上書きしない（409）。"""
    q, _ = await _confirmed(designer_client, designer_user, db_session, csrf_token)
    page_version = (await _save(designer_client, q.id, _PROGRAM, csrf_token, confirm=False)).json()[
        "version"
    ]
    assert page_version

    # 別の利用者が先に編集した
    other = copy.deepcopy(_PROGRAM)
    other["steps"][0]["sql"] = "SELECT other FROM t WHERE pid = :patient_id"
    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/item1",
        json={"program": other, "base_version": page_version},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200, resp.text
    new_version = resp.json()["version"]
    assert new_version != page_version

    # 古い版のまま保存しようとすると 409
    mine = copy.deepcopy(_PROGRAM)
    mine["steps"][0]["sql"] = "SELECT mine FROM t WHERE pid = :patient_id"
    for url in (
        f"/ai-input/questionnaires/{q.id}/plan/item1",
        f"/ai-input/questionnaires/{q.id}/plan/item1?confirm=true",
    ):
        resp = await designer_client.post(
            url,
            json={"program": mine, "base_version": page_version},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp.status_code == 409
        assert resp.json()["conflict"] is True
        assert resp.json()["version"] == new_version
    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/item1/demote",
        json={"base_version": page_version},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 409

    plan = await _get_plan(db_session, q.id)
    assert plan.program["steps"][0]["sql"] == other["steps"][0]["sql"]


@pytest.mark.asyncio
async def test_plan_save_with_null_version_conflicts_when_row_exists(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """「まだ行が無い」と思っている画面（null）は、既に作られた行を上書きしない。

    版を送らない呼び出し（API 直叩き）は従来どおり検査しない。
    """
    q = await _setup(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    first = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/item1",
        json={"program": _PROGRAM, "base_version": None},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert first.status_code == 200
    edited = copy.deepcopy(_PROGRAM)
    edited["steps"][0]["sql"] = "SELECT 9"
    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/item1",
        json={"program": edited, "base_version": None},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 409
    resp = await _save(designer_client, q.id, edited, csrf_token, confirm=False)
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_runbook_save_with_stale_version_is_409(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    q, rb = await _confirmed_runbook(designer_client, designer_user, db_session, csrf_token)
    # 画面が持つ版（同一内容の保存は何も書かずに現在の版を返す）
    page_version = (await _save_runbook(designer_client, q.id, _RB_STEPS, csrf_token)).json()[
        "version"
    ]
    assert page_version

    edited = copy.deepcopy(_RB_STEPS)
    edited[0]["title"] = "先に保存した題"
    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/runbook/item1",
        json={"steps": edited, "base_version": page_version},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200, resp.text

    mine = copy.deepcopy(_RB_STEPS)
    mine[0]["title"] = "後から保存した題"
    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/runbook/item1?confirm=true",
        json={"steps": mine, "base_version": page_version},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 409
    await db_session.refresh(rb)
    assert rb.steps[0]["title"] == "先に保存した題"
    assert rb.status == "draft"


# ---------------------------------------------------------------------------
# 何も変えていない保存は何も書かない（改行・CRLF・前後空白を含む値）
# ---------------------------------------------------------------------------

_CRLF_PROGRAM: dict[str, Any] = {
    "steps": [
        {
            "op": "sql_extract",
            "id": "s1",
            "sql": "SELECT v\r\nFROM t WHERE pid = :patient_id",
            "reasoning": "\n1行目\r\n2行目\n  ",
        }
    ],
    "return": "$s1",
}


@pytest.mark.asyncio
async def test_identical_save_with_newlines_and_crlf_is_a_noop(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """改行・CRLF・前後空白を含む reasoning を同じまま送ると、確認済みのまま何も書かない。"""
    q = await _setup(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    resp = await _save(designer_client, q.id, _CRLF_PROGRAM, csrf_token, confirm=True)
    assert resp.status_code == 200, resp.text
    plan = await _get_plan(db_session, q.id)
    before = (plan.updated_at, plan.confirmed_at, plan.confirmed_by_id)

    resp = await _save(
        designer_client, q.id, copy.deepcopy(_CRLF_PROGRAM), csrf_token, confirm=False
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "confirmed"
    assert body["unchanged"] is True
    plan = await _get_plan(db_session, q.id)
    assert (plan.updated_at, plan.confirmed_at, plan.confirmed_by_id) == before
    assert plan.program["steps"][0]["reasoning"] == "\n1行目\r\n2行目\n  "


@pytest.mark.asyncio
async def test_normalised_newlines_or_trimmed_reasoning_count_as_a_change(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """サーバは文字どおりに比べる。CRLF→LF や trim は「変更」であり、だから画面は触って
    いない欄を元の値のまま送る（plan.html / design_runbook.html の collectSteps）。"""
    q = await _setup(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    for mangle in (
        lambda r: r.replace("\r\n", "\n"),
        lambda r: r.strip(),
        lambda r: r.replace("\r\n", " ").replace("\n", " "),  # 旧 <input> の改行落とし
    ):
        # 元の内容で確認し直してから、崩した内容を保存する
        resp = await _save(designer_client, q.id, _CRLF_PROGRAM, csrf_token, confirm=True)
        assert resp.json()["status"] == "confirmed"
        mangled = copy.deepcopy(_CRLF_PROGRAM)
        mangled["steps"][0]["reasoning"] = mangle(mangled["steps"][0]["reasoning"])
        resp = await _save(designer_client, q.id, mangled, csrf_token, confirm=False)
        assert resp.json()["status"] == "draft"


@pytest.mark.asyncio
async def test_identical_runbook_save_with_newlines_is_a_noop(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    q = await _setup(db_session, runbook_status="draft")
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    steps = [
        {
            "order": 1,
            "title": "  題  ",
            "description": "\n説明1\r\n説明2 ",
            "action_type": "fetch",
            "data_hint": "SELECT a\r\nFROM b",
        }
    ]
    url = f"/ai-input/questionnaires/{q.id}/runbook/item1"
    resp = await designer_client.post(
        url + "?confirm=true", json={"steps": steps}, headers={"X-CSRF-Token": csrf_token}
    )
    assert resp.status_code == 200, resp.text
    resp = await designer_client.post(
        url, json={"steps": steps}, headers={"X-CSRF-Token": csrf_token}
    )
    assert resp.json()["status"] == "confirmed"
    assert resp.json()["unchanged"] is True


# ---------------------------------------------------------------------------
# 実行記録: UTC の ISO 表記と実効モデル
# ---------------------------------------------------------------------------


def test_snapshot_writes_utc_aware_iso_for_naive_and_aware_datetimes() -> None:
    """SQLite（naive）でも PostgreSQL（aware）でも同じ UTC 表記で書く。"""
    from datetime import timedelta, timezone

    naive = datetime(2026, 1, 2, 3, 4, 5, 6)
    jst = datetime(2026, 1, 2, 12, 4, 5, 6, tzinfo=timezone(timedelta(hours=9)))
    for dt in (naive, jst):
        plan = InputPlan(link_id="i", program=_PROGRAM, confirmed_at=dt, updated_at=dt)
        snap = program_snapshot([plan])["i"]
        assert snap["confirmed_at"] == "2026-01-02T03:04:05.000006+00:00"
        assert snap["plan_updated_at"] == "2026-01-02T03:04:05.000006+00:00"


_LLM_PROGRAM: dict[str, Any] = {
    "steps": [
        {"op": "llm_extract", "id": "a", "prompt": "p", "llm_model": "approved-model"},
        {"op": "llm_judge", "id": "b", "prompt": "q"},
        {"op": "sql_extract", "id": "c", "sql": "SELECT 1"},
    ],
    "return": "$a",
}


def test_snapshot_records_effective_llm_models(monkeypatch: pytest.MonkeyPatch) -> None:
    """LLM op ごとに実効モデルと出所を残し、実行時指定で承認と違えば overridden_from を書く。"""
    monkeypatch.setattr(settings, "ollama_model", "default-model")
    plan = InputPlan(link_id="i", program=_LLM_PROGRAM)

    snap = program_snapshot([plan])["i"]["llm_models"]
    assert snap == {
        "a": {"effective": "approved-model", "source": "step", "approved": "approved-model"},
        "b": {"effective": "default-model", "source": "default", "approved": None},
    }

    snap = program_snapshot([plan], "run-model")["i"]["llm_models"]
    assert snap["a"] == {
        "effective": "run-model",
        "source": "request_override",
        "approved": "approved-model",
        "overridden_from": "approved-model",
    }
    # step にモデルの指定が無ければ「承認と違う」とは言えない
    assert "overridden_from" not in snap["b"]
    assert snap["b"]["source"] == "request_override"

    # LLM op の無いプランには llm_models を付けない
    assert "llm_models" not in program_snapshot([InputPlan(link_id="j", program=_PROGRAM)])["j"]


@pytest.mark.asyncio
async def test_llm_trace_marks_run_time_override(db_session: AsyncSession) -> None:
    """実行時指定が承認された step.llm_model に優先したことを trace だけで読めること。"""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from kawasemi.ai_input.executor import DSLInterpreter

    program = {
        "steps": [{"op": "llm_extract", "id": "v", "prompt": "p", "llm_model": "approved-model"}],
        "return": "$v",
    }
    plan = InputPlan(questionnaire_id=1, link_id="i", program=program, status="confirmed")

    for override, source, overridden in (
        ("run-model", "request_override", "approved-model"),
        (None, "step", None),
    ):
        interp = DSLInterpreter()
        interp._ollama_client.chat = AsyncMock(
            return_value=SimpleNamespace(message=SimpleNamespace(content="x"))
        )
        _, evidence = await interp.execute([plan], "P1", db_session, llm_model_override=override)
        llm = next(t for t in evidence["i"]["trace"] if t.get("op") == "llm_extract")["llm"]
        assert llm["model"] == (override or "approved-model")
        assert llm["model_source"] == source
        assert llm.get("overridden_from") == overridden
