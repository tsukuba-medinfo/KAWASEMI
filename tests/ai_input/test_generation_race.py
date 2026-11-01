"""AI 生成の経路で、LLM 呼び出しの間に行が確認・作成されたときの扱い。

人が承認した program と実行される program が同一であることが設計の核である。
`plan/compile`・`plan/{link_id}/recompile`・`runbook/generate`・`runbook/{link_id}/regenerate`
は行を読んでから LLM を呼び、応答の後に書く。以前は**呼び出し前に読んだオブジェクト**へ
書いていたので、読んだ時点で draft の行が呼び出し中に確認されると、draft と空の確認者の
代入が「読んだ値と同じ」として UPDATE から外れ、`SET program=?` だけが走った。行は
**確認済み（確認者は別人）のまま、誰も確認していない生成結果**を持った。

ここでは LLM クライアントを差し替え、呼び出しの途中で**別のセッション**から行を確認する。
どの経路でも、結果は次のどちらかでなければならない。

- 単一項目（recompile / regenerate）は 409、一括（compile / generate）はその項目を
  `skipped_conflict` に出して書かない。行は確認済みの内容のまま残る。
- 書く場合は draft・確認者なし・生成結果。

**確認済み＋生成結果**の組み合わせは、どの経路でも残らない。

別のセッションは `tests.conftest.TestSessionLocal`（同じインメモリ DB）を使う。要求の
セッション（`db_session`）で書くと、同じ identity map のオブジェクトが更新されて不具合が
隠れる。本物の同時実行と行ロックは PostgreSQL のテスト
（`test_plan_concurrency_postgres.py`）で見る。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input import routes as routes_mod
from kawasemi.ai_input.models import ClinicalAnnotation, InputPlan, InputRunbook
from kawasemi.ai_input.schemas import (
    DSLProgram,
    GeneratedPlanItem,
    GeneratedRunbookItem,
    RunbookStep,
)
from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import RoleType
from kawasemi.models.user import User
from tests.conftest import TestSessionLocal

_HUMAN_PROGRAM: dict[str, Any] = {
    "steps": [
        {"op": "sql_extract", "id": "s1", "sql": "SELECT human FROM t WHERE pid = :patient_id"}
    ],
    "return": "$s1",
}
_GENERATED_SQL = "SELECT generated FROM t WHERE pid = :patient_id"
_HUMAN_STEPS: list[dict[str, Any]] = [
    {
        "order": 0,
        "title": "人が確かめた手順",
        "description": "",
        "action_type": None,
        "data_hint": None,
    }
]
_GENERATED_TITLE = "AI が生成した手順"

MidCall = Callable[[], Awaitable[None]]


def _act_as(client: AsyncClient, user: User, role: str) -> None:
    client.cookies.set(
        settings.jwt_cookie_name, create_access_token(data={"sub": str(user.id), "act": role})
    )


async def _setup(
    db: AsyncSession,
    *,
    link_ids: tuple[str, ...] = ("item1",),
    runbook_status: str = "confirmed",
    plan_for: tuple[str, ...] = ("item1",),
) -> int:
    """項目・臨床的コンテキスト・手順書・draft の取得プランを作り、様式の id を返す。

    id を先に取り出しておく。競合で要求が rollback すると、同じセッションのオブジェクトは
    expire し、読むと非同期の外で読み込みが走る。
    """
    q = Questionnaire(
        fhir_id="q-gen-race",
        title="生成中の確認テスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-gen-race",
            "item": [{"linkId": lid, "text": f"項目{lid}", "type": "string"} for lid in link_ids],
        },
    )
    db.add(q)
    await db.flush()
    for lid in link_ids:
        db.add(
            ClinicalAnnotation(
                questionnaire_id=q.id, link_id=lid, item_text=f"項目{lid}", annotation="意味"
            )
        )
        db.add(
            InputRunbook(
                questionnaire_id=q.id,
                link_id=lid,
                item_text=f"項目{lid}",
                steps=_HUMAN_STEPS,
                status=runbook_status,
            )
        )
    for lid in plan_for:
        db.add(
            InputPlan(questionnaire_id=q.id, link_id=lid, program=_HUMAN_PROGRAM, status="draft")
        )
    await db.commit()
    return q.id


async def _confirm_elsewhere(
    model: type[InputPlan] | type[InputRunbook], qid: int, user_id: int
) -> None:
    """**別のセッション**で item1 を確認する（他の利用者が生成中に確認ボタンを押した）。"""
    async with TestSessionLocal() as other:
        await other.execute(
            update(model)
            .where(model.questionnaire_id == qid, model.link_id == "item1")
            .values(status="confirmed", confirmed_by_id=user_id, confirmed_at=datetime.now(UTC))
            .execution_options(synchronize_session=False)
        )
        await other.commit()


async def _create_plan_elsewhere(qid: int) -> None:
    async with TestSessionLocal() as other:
        other.add(
            InputPlan(questionnaire_id=qid, link_id="item1", program=_HUMAN_PROGRAM, status="draft")
        )
        await other.commit()


def _fake_compiler(monkeypatch: pytest.MonkeyPatch, mid_call: MidCall | None) -> None:
    class FakeCompiler:
        async def compile(self, items: list[dict[str, Any]], **_: Any) -> list[GeneratedPlanItem]:
            if mid_call is not None:
                await mid_call()
            return [
                GeneratedPlanItem(
                    link_id=item["link_id"],
                    program=DSLProgram.model_validate(
                        {
                            "steps": [{"op": "sql_extract", "id": "s1", "sql": _GENERATED_SQL}],
                            "return": "$s1",
                        }
                    ),
                )
                for item in items
            ]

    monkeypatch.setattr(routes_mod, "AIPlanCompiler", FakeCompiler)


def _fake_generator(monkeypatch: pytest.MonkeyPatch, mid_call: MidCall | None) -> None:
    class FakeGenerator:
        async def generate(
            self, items: list[dict[str, Any]], **_: Any
        ) -> list[GeneratedRunbookItem]:
            if mid_call is not None:
                await mid_call()
            return [
                GeneratedRunbookItem(
                    link_id=item["link_id"], steps=[RunbookStep(order=0, title=_GENERATED_TITLE)]
                )
                for item in items
            ]

    monkeypatch.setattr(routes_mod, "AIRunbookGenerator", FakeGenerator)


async def _row(
    model: type[InputPlan] | type[InputRunbook], qid: int, link_id: str = "item1"
) -> Any:
    """要求のセッションの identity map を通さず、DB の今の値を読む。"""
    async with TestSessionLocal() as fresh:
        return (
            await fresh.execute(
                select(model).where(model.questionnaire_id == qid, model.link_id == link_id)
            )
        ).scalar_one_or_none()


def _plan_is_generated(plan: InputPlan) -> bool:
    return plan.program["steps"][0]["sql"] == _GENERATED_SQL


def _runbook_is_generated(rb: InputRunbook) -> bool:
    return rb.steps[0]["title"] == _GENERATED_TITLE


def _assert_plan_never_confirmed_with_generated(plan: InputPlan) -> None:
    assert not (plan.status == "confirmed" and _plan_is_generated(plan)), (
        "誰も確認していない生成結果が確認済みのまま残った"
    )


def _assert_runbook_never_confirmed_with_generated(rb: InputRunbook) -> None:
    assert not (rb.status == "confirmed" and _runbook_is_generated(rb)), (
        "誰も確認していない生成結果が確認済みのまま残った"
    )


@pytest.fixture(autouse=True)
def _drafting_on(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    monkeypatch.setattr(settings, "enable_ai_extraction", True)


# ---------------------------------------------------------------------------
# 単一項目: 生成中に確認されたら 409、確認済みの内容を残す
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recompile_confirmed_during_llm_call_is_409(
    designer_client: AsyncClient,
    designer_user: User,
    admin_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qid = await _setup(db_session)
    other_id = admin_user.id
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _fake_compiler(monkeypatch, lambda: _confirm_elsewhere(InputPlan, qid, other_id))

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{qid}/plan/item1/recompile",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["conflict"] is True
    plan = await _row(InputPlan, qid)
    assert plan.status == "confirmed"
    assert plan.confirmed_by_id == other_id
    assert plan.program == _HUMAN_PROGRAM
    assert resp.json()["version"] == routes_mod.plan_version(plan)


@pytest.mark.asyncio
async def test_regenerate_confirmed_during_llm_call_is_409(
    designer_client: AsyncClient,
    designer_user: User,
    admin_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qid = await _setup(db_session, runbook_status="draft", plan_for=())
    other_id = admin_user.id
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _fake_generator(monkeypatch, lambda: _confirm_elsewhere(InputRunbook, qid, other_id))

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{qid}/runbook/item1/regenerate",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["conflict"] is True
    rb = await _row(InputRunbook, qid)
    assert rb.status == "confirmed"
    assert rb.confirmed_by_id == other_id
    assert rb.steps == _HUMAN_STEPS


# ---------------------------------------------------------------------------
# 一括: 生成中に確認された項目は skipped_conflict、他の項目は書く
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compile_skips_item_confirmed_during_llm_call(
    designer_client: AsyncClient,
    designer_user: User,
    admin_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qid = await _setup(db_session, link_ids=("item1", "item2"))
    other_id = admin_user.id
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _fake_compiler(monkeypatch, lambda: _confirm_elsewhere(InputPlan, qid, other_id))

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{qid}/plan/compile",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skipped_conflict"] == ["item1"]
    assert body["compiled_link_ids"] == ["item2"]
    assert body["compiled"] == 1
    assert "item1" in body["message"]
    assert "item1" in body["rows_not_rewritten"]

    plan1 = await _row(InputPlan, qid, "item1")
    assert plan1.status == "confirmed"
    assert plan1.confirmed_by_id == other_id
    assert plan1.program == _HUMAN_PROGRAM
    plan2 = await _row(InputPlan, qid, "item2")
    assert plan2.status == "draft"
    assert _plan_is_generated(plan2)


@pytest.mark.asyncio
async def test_generate_skips_item_confirmed_during_llm_call(
    designer_client: AsyncClient,
    designer_user: User,
    admin_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qid = await _setup(db_session, link_ids=("item1", "item2"), runbook_status="draft", plan_for=())
    other_id = admin_user.id
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _fake_generator(monkeypatch, lambda: _confirm_elsewhere(InputRunbook, qid, other_id))

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{qid}/runbook/generate",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skipped_conflict"] == ["item1"]
    assert body["generated_link_ids"] == ["item2"]
    assert body["generated"] == 1
    assert "item1" in body["message"]

    rb1 = await _row(InputRunbook, qid, "item1")
    assert rb1.status == "confirmed"
    assert rb1.confirmed_by_id == other_id
    assert rb1.steps == _HUMAN_STEPS
    rb2 = await _row(InputRunbook, qid, "item2")
    assert rb2.status == "draft"
    assert _runbook_is_generated(rb2)


# ---------------------------------------------------------------------------
# 読み直しから書き込みまでの間に確認された場合（SQLite は FOR UPDATE が効かない）:
# status・確認者を必ず UPDATE に含めるので、draft と生成結果になる
# ---------------------------------------------------------------------------


def _confirm_right_after_reread(
    monkeypatch: pytest.MonkeyPatch, model: type[InputPlan] | type[InputRunbook], user_id: int
) -> None:
    real = routes_mod._lock_rows_for_write

    async def reread_then_confirm(db: AsyncSession, m: Any, qid: int, link_ids: list[str]) -> Any:
        rows = await real(db, m, qid, link_ids)
        await _confirm_elsewhere(model, qid, user_id)
        return rows

    monkeypatch.setattr(routes_mod, "_lock_rows_for_write", reread_then_confirm)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["compile", "recompile"])
async def test_plan_confirmed_between_reread_and_write_ends_draft(
    designer_client: AsyncClient,
    designer_user: User,
    admin_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    qid = await _setup(db_session)
    other_id = admin_user.id
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _fake_compiler(monkeypatch, None)
    _confirm_right_after_reread(monkeypatch, InputPlan, other_id)
    url = {
        "compile": f"/ai-input/questionnaires/{qid}/plan/compile",
        "recompile": f"/ai-input/questionnaires/{qid}/plan/item1/recompile",
    }[route]

    resp = await designer_client.post(url, json={}, headers={"X-CSRF-Token": csrf_token})

    assert resp.status_code == 200, resp.text
    plan = await _row(InputPlan, qid)
    _assert_plan_never_confirmed_with_generated(plan)
    assert plan.status == "draft"
    assert plan.confirmed_by_id is None
    assert plan.confirmed_at is None
    assert _plan_is_generated(plan)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["generate", "regenerate"])
async def test_runbook_confirmed_between_reread_and_write_ends_draft(
    designer_client: AsyncClient,
    designer_user: User,
    admin_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
    route: str,
) -> None:
    qid = await _setup(db_session, runbook_status="draft", plan_for=())
    other_id = admin_user.id
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _fake_generator(monkeypatch, None)
    _confirm_right_after_reread(monkeypatch, InputRunbook, other_id)
    url = {
        "generate": f"/ai-input/questionnaires/{qid}/runbook/generate",
        "regenerate": f"/ai-input/questionnaires/{qid}/runbook/item1/regenerate",
    }[route]

    resp = await designer_client.post(url, json={}, headers={"X-CSRF-Token": csrf_token})

    assert resp.status_code == 200, resp.text
    rb = await _row(InputRunbook, qid)
    _assert_runbook_never_confirmed_with_generated(rb)
    assert rb.status == "draft"
    assert rb.confirmed_by_id is None
    assert rb.confirmed_at is None
    assert _runbook_is_generated(rb)


# ---------------------------------------------------------------------------
# 同時の新規作成: 生成中に別の要求が同じ項目を作った
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_compile_skips_item_created_during_llm_call(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """呼び出し中に作られた行は読み直しで見えるので、版の不一致として skip する。"""
    qid = await _setup(db_session, plan_for=())
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _fake_compiler(monkeypatch, lambda: _create_plan_elsewhere(qid))

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{qid}/plan/compile",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["skipped_conflict"] == ["item1"]
    plan = await _row(InputPlan, qid)
    assert plan.program == _HUMAN_PROGRAM


def _create_right_after_reread(monkeypatch: pytest.MonkeyPatch) -> None:
    """読み直しが「無い」と返した直後に別の要求が行を作る（一意制約に当たる形）。"""
    real = routes_mod._lock_rows_for_write
    calls = {"n": 0}

    async def reread_then_create(db: AsyncSession, m: Any, qid: int, link_ids: list[str]) -> Any:
        rows = await real(db, m, qid, link_ids)
        calls["n"] += 1
        if calls["n"] == 1:
            await _create_plan_elsewhere(qid)
        return rows

    monkeypatch.setattr(routes_mod, "_lock_rows_for_write", reread_then_create)


@pytest.mark.asyncio
async def test_compile_unique_violation_becomes_skipped_not_500(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qid = await _setup(db_session, link_ids=("item1", "item2"), plan_for=())
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _fake_compiler(monkeypatch, None)
    _create_right_after_reread(monkeypatch)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{qid}/plan/compile",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skipped_conflict"] == ["item1"]
    assert body["compiled_link_ids"] == ["item2"]
    assert (await _row(InputPlan, qid, "item1")).program == _HUMAN_PROGRAM
    assert _plan_is_generated(await _row(InputPlan, qid, "item2"))


@pytest.mark.asyncio
async def test_recompile_unique_violation_is_409_not_500(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qid = await _setup(db_session, plan_for=())
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _fake_compiler(monkeypatch, None)
    _create_right_after_reread(monkeypatch)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{qid}/plan/item1/recompile",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 409, resp.text
    assert resp.json()["conflict"] is True
    assert (await _row(InputPlan, qid)).program == _HUMAN_PROGRAM


# ---------------------------------------------------------------------------
# 競合が無ければ従来どおり draft で書く
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_recompile_without_race_writes_draft(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    qid = await _setup(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _fake_compiler(monkeypatch, None)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{qid}/plan/item1/recompile",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 200, resp.text
    plan = await _row(InputPlan, qid)
    assert plan.status == "draft"
    assert _plan_is_generated(plan)
    assert resp.json()["version"] == routes_mod.plan_version(plan)


# ---------------------------------------------------------------------------
# 画面: 409 と skipped_conflict を成功として出さない
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pages_surface_generation_conflicts(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
) -> None:
    """単一項目の 409 は結果を画面に入れずに警告し、一括の skip は緑にしない。"""
    from tests.ai_input.test_partial_result_routes import _js_function

    qid = await _setup(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    plan_page = (await designer_client.get(f"/ai-input/questionnaires/{qid}/plan")).text
    recompile = _js_function(plan_page, "recompile")
    # 409 の枝が成功の枝より先にあり、program を置き換えない
    assert recompile.index("if (data.conflict)") < recompile.index("data.success")
    assert "it.program" not in recompile.split("else if (data.success)")[0]
    assert "data.skipped_conflict" in _js_function(plan_page, "compileAll")

    rb_page = (await designer_client.get(f"/ai-input/questionnaires/{qid}/runbook")).text
    regenerate = _js_function(rb_page, "regenerate")
    assert regenerate.index("if (data.conflict)") < regenerate.index("data.success")
    assert "it.steps" not in regenerate.split("else if (data.success)")[0]
    gen = _js_function(rb_page, "generateAll")
    branch = gen[gen.index("} else if (skippedConflict) {") :].split("} else {")[0]
    assert "false)" in branch, "生成中に更新された項目があっても緑のままになっている"
