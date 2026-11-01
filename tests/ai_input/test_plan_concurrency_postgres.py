"""取得プラン保存の同時実行（PostgreSQL）。

SQLite（テストの既定）は書き込みを DB 全体で直列化し、`FOR UPDATE` を無視するので、
**行ロックが効いているかはここでしか確かめられない**。`KAWASEMI_TEST_POSTGRES_URL`
（例: `postgresql+asyncpg://user@127.0.0.1:55432/kawasemi_test`）が無ければ skip する。
その DB のテーブルは作り直す（使い捨ての DB を渡すこと）。CI では `migrations-postgres`
ジョブが、移行の検査と読み取り専用テストの後に別の pytest 起動で回す。

- 同じ版を見た2つの保存が同時に来たら、片方だけが通り、もう片方は 409 になる。
  `_lock_row` を何もしない関数にすると、両方が古い版で検査を通り、後着が先着を
  黙って上書きする（両方 200）——このテストはそれで落ちる。
- 行がまだ無い項目を2つの要求が同時に作ったら、片方は一意制約に当たる。500 ではなく
  409 で返す。
"""

from __future__ import annotations

import asyncio
import copy
import os
from collections.abc import AsyncGenerator
from typing import Any

import pytest
from fastapi_csrf_protect import CsrfProtect
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from kawasemi.ai_input import routes as routes_mod
from kawasemi.ai_input.models import InputPlan, InputRunbook
from kawasemi.ai_input.routes import plan_version
from kawasemi.api.main import app
from kawasemi.core.auth import create_access_token, get_password_hash
from kawasemi.core.config import settings
from kawasemi.core.database import get_db
from kawasemi.models.base import Base
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import Role, RoleType
from kawasemi.models.user import User, UserRole

PG_URL = os.environ.get("KAWASEMI_TEST_POSTGRES_URL")

pytestmark = pytest.mark.skipif(
    not PG_URL, reason="KAWASEMI_TEST_POSTGRES_URL が無い（PostgreSQL の同時実行テスト）"
)

_PROGRAM: dict[str, Any] = {
    "steps": [{"op": "sql_extract", "id": "s1", "sql": "SELECT v FROM t WHERE pid = :patient_id"}],
    "return": "$s1",
}

# 検査（版の比較）から書き込みまでの間に置く遅延。2つの要求の検査と書き込みが
# 確実に重なるようにする（ロックが無ければ両方が古い版で検査を通る）。
_HOLD_SECONDS = 0.5


@pytest.fixture
async def pg() -> AsyncGenerator[async_sessionmaker[AsyncSession], None]:
    assert PG_URL is not None
    engine = create_async_engine(PG_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def override_get_db() -> AsyncGenerator[AsyncSession, None]:
        # **要求ごとに別の接続**（同時実行を本物にする）
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    try:
        yield factory
    finally:
        app.dependency_overrides.clear()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
        await engine.dispose()


async def _seed(
    factory: async_sessionmaker[AsyncSession], *, with_plan: bool
) -> tuple[int, int, str | None]:
    async with factory() as s:
        role = Role(name=RoleType.DESIGNER, description=RoleType.DESCRIPTIONS[RoleType.DESIGNER])
        user = User(
            username="pg_designer",
            email="pg_designer@test.com",
            password_hash=get_password_hash("Designer123!"),
            full_name="PG 設計者",
            is_active=True,
        )
        s.add_all([role, user])
        await s.flush()
        s.add(UserRole(user_id=user.id, role_id=role.id))
        q = Questionnaire(
            fhir_id="q-pg",
            title="PG 同時実行",
            status="active",
            content={"resourceType": "Questionnaire", "id": "q-pg", "item": []},
        )
        s.add(q)
        await s.flush()
        s.add(
            InputRunbook(
                questionnaire_id=q.id,
                link_id="item1",
                item_text="項目1",
                steps=[],
                status="confirmed",
            )
        )
        plan = None
        if with_plan:
            plan = InputPlan(
                questionnaire_id=q.id, link_id="item1", program=_PROGRAM, status="draft"
            )
            s.add(plan)
        await s.commit()
        return q.id, user.id, plan_version(plan)


def _client(user_id: int) -> tuple[AsyncClient, str]:
    client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    client.cookies.set(
        settings.jwt_cookie_name,
        create_access_token(data={"sub": str(user_id), "act": RoleType.DESIGNER}),
    )
    csrf = CsrfProtect()
    raw, signed = csrf.generate_csrf_tokens()
    client.cookies.set(csrf._cookie_key, signed)
    return client, raw


def _hold_before_write(monkeypatch: pytest.MonkeyPatch) -> None:
    real = routes_mod._upsert_plan

    async def slow(*args: Any, **kwargs: Any) -> InputPlan:
        await asyncio.sleep(_HOLD_SECONDS)
        return await real(*args, **kwargs)

    monkeypatch.setattr(routes_mod, "_upsert_plan", slow)


async def _two_saves(qid: int, user_id: int, bodies: list[dict[str, Any]]) -> list[int]:
    clients = [_client(user_id) for _ in bodies]
    try:
        resps = await asyncio.gather(
            *(
                c.post(
                    f"/ai-input/questionnaires/{qid}/plan/item1",
                    json=body,
                    headers={"X-CSRF-Token": token},
                )
                for (c, token), body in zip(clients, bodies, strict=True)
            )
        )
    finally:
        for c, _ in clients:
            await c.aclose()
    return sorted(r.status_code for r in resps)


@pytest.mark.asyncio
async def test_concurrent_saves_with_the_same_version_let_only_one_through(
    pg: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    qid, user_id, version = await _seed(pg, with_plan=True)
    assert version is not None
    _hold_before_write(monkeypatch)

    bodies = []
    for sql in (
        "SELECT a FROM t WHERE pid = :patient_id",
        "SELECT b FROM t WHERE pid = :patient_id",
    ):
        program = copy.deepcopy(_PROGRAM)
        program["steps"][0]["sql"] = sql
        bodies.append({"program": program, "base_version": version})

    assert await _two_saves(qid, user_id, bodies) == [200, 409]

    async with pg() as s:
        plan = (
            await s.execute(select(InputPlan).where(InputPlan.questionnaire_id == qid))
        ).scalar_one()
    assert plan.program["steps"][0]["sql"] in {b["program"]["steps"][0]["sql"] for b in bodies}


@pytest.mark.asyncio
async def test_concurrent_first_creates_return_409_not_500(
    pg: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    qid, user_id, version = await _seed(pg, with_plan=False)
    assert version is None
    _hold_before_write(monkeypatch)

    bodies = [{"program": _PROGRAM, "base_version": None}, {"program": _PROGRAM}]
    assert await _two_saves(qid, user_id, bodies) == [200, 409]
