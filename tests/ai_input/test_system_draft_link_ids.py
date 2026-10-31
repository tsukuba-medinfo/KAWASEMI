"""データ所在（役割2）のAI案で、複数の link_id をまとめて指定できること

臨床側（`test_clinical_draft_routes.py`）と**同じ形**を所在側にも置く。所在の
埋め直しで同じ問題——1コールの項目数が減ると応答を返さなくなるモデルがある——に
当たるため、対象の選び方は2経路で揃っている必要がある。

ここで固定するのは**対象の選定だけ**である。索引・BM25・来歴の配線は
`test_design_provenance_routes.py` と `test_partial_result_routes.py` が持つので、
drafter はスパイに差し替えて「誰が対象に入ったか」だけを測る。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input import routes as routes_mod
from kawasemi.ai_input.location_drafter import LocationDraftResult
from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import RoleType
from kawasemi.models.user import User


def _act_as(client: AsyncClient, user: User, role: str) -> None:
    token = create_access_token(data={"sub": str(user.id), "act": role})
    client.cookies.set(settings.jwt_cookie_name, token)


def _draft_url(qid: int) -> str:
    return f"/ai-input/questionnaires/{qid}/source-survey/system/ai-draft"


async def _make_questionnaire(db_session: AsyncSession) -> Questionnaire:
    q = Questionnaire(
        fhir_id="q-system-link-ids",
        title="所在の複数指定テスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-system-link-ids",
            "item": [
                {"linkId": "item1", "text": "診断日", "type": "string"},
                {"linkId": "item2", "text": "進行度", "type": "string"},
                {"linkId": "item3", "text": "組織型", "type": "string"},
            ],
        },
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    return q


def _spy_drafter(monkeypatch: pytest.MonkeyPatch, seen: list[list[str]]) -> None:
    """drafter へ渡された link_id を記録するだけのフェイク。"""

    class _SpyDrafter:
        async def draft(self, items, model=None, recorder=None):  # noqa: ANN001, ANN202
            seen.append([str(d["link_id"]) for d in items])
            return LocationDraftResult(items=[], no_hit_count=len(items), llm_model=None)

    monkeypatch.setattr(routes_mod, "AIResourceDrafter", lambda *a, **kw: _SpyDrafter())


@pytest.mark.asyncio
async def test_system_draft_targets_exactly_the_given_link_ids(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`link_ids` はその集合だけを、項目順で1コールへまとめること。"""
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    seen: list[list[str]] = []
    _spy_drafter(monkeypatch, seen)

    resp = await designer_client.post(
        _draft_url(q.id),
        json={"link_ids": ["item3", "item1"]},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 200, resp.text
    assert seen == [["item1", "item3"]], seen


@pytest.mark.asyncio
async def test_system_draft_rejects_unknown_link_ids(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """問診票に無い `link_id` を黙って捨てないこと（臨床側と同じ規則）。"""
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    seen: list[list[str]] = []
    _spy_drafter(monkeypatch, seen)

    resp = await designer_client.post(
        _draft_url(q.id),
        json={"link_ids": ["item1", "item404"]},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 400, resp.text
    assert "item404" in resp.json()["message"]
    assert seen == [], seen


@pytest.mark.asyncio
async def test_system_draft_does_not_treat_empty_link_ids_as_bulk(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空の `link_ids` が一括（未入力の全項目）へ落ちないこと。"""
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    seen: list[list[str]] = []
    _spy_drafter(monkeypatch, seen)

    resp = await designer_client.post(
        _draft_url(q.id),
        json={"link_ids": []},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 400, resp.text
    assert seen == [], seen


@pytest.mark.asyncio
async def test_system_draft_passes_the_parent_text_to_the_drafter(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """所在ルートは親の text を drafter へ渡す（「CRP」の下の「数値(mg/dL)」）。"""
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = Questionnaire(
        fhir_id="q-parent-text",
        title="親の text のテスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-parent-text",
            "item": [
                {
                    "linkId": "crp",
                    "type": "group",
                    "text": "CRP",
                    "item": [{"linkId": "crp_v", "type": "decimal", "text": "数値(mg/dL)"}],
                }
            ],
        },
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    received: list[dict] = []

    class _SpyDrafter:
        async def draft(self, items, model=None, recorder=None):  # noqa: ANN001, ANN202
            received.extend(items)
            return LocationDraftResult(items=[], no_hit_count=len(items), llm_model=None)

    monkeypatch.setattr(routes_mod, "AIResourceDrafter", lambda *a, **kw: _SpyDrafter())

    resp = await designer_client.post(
        _draft_url(q.id), json={"link_ids": ["crp_v"]}, headers={"X-CSRF-Token": csrf_token}
    )

    assert resp.status_code == 200, resp.text
    assert [(d["item_text"], d["parent_text"]) for d in received] == [("数値(mg/dL)", "CRP")]
