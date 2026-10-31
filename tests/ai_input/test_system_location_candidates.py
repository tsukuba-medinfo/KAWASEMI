"""SystemLocation の candidates 配列化のユニットテスト

- モデルの便宜プロパティ（primary / primary_source_category / primary_location /
  has_content）。
- 保存リクエスト正規化 _normalize_candidates（新形式の候補配列 / 旧単一形式の
  後方互換 / 空候補の破棄 / 不正カテゴリ補正）。
- 保存 API（`POST .../source-survey/system`）が pydantic の検査を通ること。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.models import SourceCategory, SystemLocation
from kawasemi.ai_input.routes import _clean_candidate, _normalize_candidates
from kawasemi.ai_input.schemas import MAX_LOCATION_CANDIDATES
from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import RoleType
from kawasemi.models.user import User

# --------------------------------------------------------------------------- #
# モデルの便宜プロパティ
# --------------------------------------------------------------------------- #


def test_primary_and_helpers_with_candidates():
    loc = SystemLocation(
        questionnaire_id=1,
        link_id="lk1",
        candidates=[
            {
                "source_category": "local_db",
                "location": "DB: X / 表: T / カラム: C",
                "unit_id": "db:T.C",
            },
            {"source_category": "fhir", "location": "FHIR: Observation"},
        ],
    )
    assert loc.primary["unit_id"] == "db:T.C"
    assert loc.primary_source_category == SourceCategory.LOCAL_DB
    assert loc.primary_location == "DB: X / 表: T / カラム: C"
    assert loc.has_content is True


def test_helpers_with_empty_candidates():
    loc = SystemLocation(questionnaire_id=1, link_id="lk1", candidates=[])
    assert loc.primary is None
    assert loc.primary_source_category == SourceCategory.MANUAL
    assert loc.primary_location is None
    assert loc.has_content is False


def test_has_content_manual_only_location():
    loc = SystemLocation(
        questionnaire_id=1,
        link_id="lk1",
        candidates=[{"source_category": "manual", "location": "紹介状PDFを参照"}],
    )
    assert loc.has_content is True  # location があれば manual でも内容あり


def test_candidate_list_normalizes_none():
    loc = SystemLocation(questionnaire_id=1, link_id="lk1")
    loc.candidates = None  # 念のため None でも [] 扱い
    assert loc.candidate_list == []
    assert loc.has_content is False


def test_not_available_resolved_but_no_content():
    """「該当データなし」は解決済み(is_resolved)だが取得対象外(has_content=False)。"""
    loc = SystemLocation(
        questionnaire_id=1,
        link_id="lk1",
        candidates=[{"source_category": "not_available", "location": None}],
    )
    assert loc.is_resolved is True
    assert loc.has_content is False  # 手順書・プランは作らない


def test_is_resolved_distinguishes_empty_from_decided():
    empty = SystemLocation(questionnaire_id=1, link_id="lk1", candidates=[])
    decided = SystemLocation(
        questionnaire_id=1,
        link_id="lk2",
        candidates=[{"source_category": "local_db", "location": "DB: X / 表: T / カラム: C"}],
    )
    assert empty.is_resolved is False  # 未選択は未解決
    assert decided.is_resolved is True


def test_clean_candidate_keeps_not_available_without_location():
    """not_available は location が無くても破棄しない（解決済みを保つ）。"""
    out = _clean_candidate({"source_category": "not_available", "location": ""})
    assert out is not None
    assert out["source_category"] == SourceCategory.NOT_AVAILABLE
    assert out["location"] is None


# --------------------------------------------------------------------------- #
# 保存リクエスト正規化
# --------------------------------------------------------------------------- #


def test_normalize_candidates_new_format():
    item = {
        "link_id": "lk1",
        "candidates": [
            {
                "source_category": "local_db",
                "location": "DB: X / 表: T / カラム: C",
                "unit_id": "db:T.C",
                "connection_id": "X",
                "payload": {"physical_name": "C"},
            }
        ],
    }
    out = _normalize_candidates(item)
    assert len(out) == 1
    assert out[0]["source_category"] == "local_db"
    assert out[0]["unit_id"] == "db:T.C"
    assert out[0]["payload"] == {"physical_name": "C"}


def test_normalize_candidates_legacy_single_fields():
    item = {"link_id": "lk1", "source_category": "fhir", "location": "FHIR: Observation"}
    out = _normalize_candidates(item)
    assert len(out) == 1
    assert out[0]["source_category"] == "fhir"
    assert out[0]["location"] == "FHIR: Observation"
    assert out[0]["unit_id"] is None


def test_normalize_candidates_drops_empty_manual():
    # location なし・manual の空候補は保存しない
    item = {"link_id": "lk1", "candidates": [{"source_category": "manual", "location": ""}]}
    assert _normalize_candidates(item) == []


def test_normalize_candidates_invalid_category_corrected():
    item = {"link_id": "lk1", "candidates": [{"source_category": "bogus", "location": "どこか"}]}
    out = _normalize_candidates(item)
    assert out[0]["source_category"] == SourceCategory.MANUAL


def test_normalize_candidates_invalid_payload_type_nulled():
    item = {
        "link_id": "lk1",
        "candidates": [{"source_category": "local_db", "location": "x", "payload": "not-a-dict"}],
    }
    out = _normalize_candidates(item)
    assert out[0]["payload"] is None


# --------------------------------------------------------------------------- #
# 保存 API の検査
#
# **UI が送らないことはサーバ側の保証にならない。** `data_manager` 権限と CSRF トークンが
# あれば API 直叩きで任意の形が入るので、受け口（`SystemLocationSaveRequest`）で決着させる。
# --------------------------------------------------------------------------- #

_SAVE_PATH = "/ai-input/questionnaires/{qid}/source-survey/system"


@pytest.fixture
def location_saver(designer_client: AsyncClient, designer_user: User) -> AsyncClient:
    """保存 API が要求する `data_manager` として振る舞う認証済みクライアント。"""
    token = create_access_token(data={"sub": str(designer_user.id), "act": RoleType.DESIGNER})
    designer_client.cookies.set(settings.jwt_cookie_name, token)
    return designer_client


async def _questionnaire(db_session: AsyncSession) -> Questionnaire:
    q = Questionnaire(
        fhir_id="q-loc-save-validation",
        title="所在保存検査テスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-loc-save-validation",
            "item": [{"linkId": "lk1", "text": "[1] 登録番号", "type": "string"}],
        },
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    return q


async def _post(
    client: AsyncClient, csrf_token: str, questionnaire_id: int, body: dict
) -> tuple[int, dict]:
    resp = await client.post(
        _SAVE_PATH.format(qid=questionnaire_id),
        json=body,
        headers={"X-CSRF-Token": csrf_token},
    )
    return resp.status_code, (resp.json() if resp.content else {})


async def _saved_candidates(db_session: AsyncSession, questionnaire_id: int) -> list[dict]:
    row = (
        await db_session.execute(
            select(SystemLocation).where(SystemLocation.questionnaire_id == questionnaire_id)
        )
    ).scalar_one()
    return row.candidate_list


@pytest.mark.asyncio
async def test_save_system_accepts_current_ui_shape(
    location_saver: AsyncClient, csrf_token: str, db_session: AsyncSession
):
    """現行 UI（`collectItems`）が送る形に回帰が無いこと。"""
    q = await _questionnaire(db_session)
    status, body = await _post(
        location_saver,
        csrf_token,
        q.id,
        {
            "items": [
                {
                    "link_id": "lk1",
                    "item_text": "診療録番号",
                    "candidates": [
                        {
                            "source_category": "local_db",
                            "location": "DB: X / 表: T / カラム: C",
                            "unit_id": "db:T.C",
                            "connection_id": "X",
                            "payload": {"physical_name": "C"},
                            "llm_model": "model-a:12b",
                        }
                    ],
                }
            ]
        },
    )
    assert status == 200 and body["saved"] == 1
    saved = await _saved_candidates(db_session, q.id)
    assert len(saved) == 1
    assert saved[0]["unit_id"] == "db:T.C"
    assert saved[0]["llm_model"] == "model-a:12b"


@pytest.mark.asyncio
async def test_save_system_accepts_legacy_flat_shape(
    location_saver: AsyncClient, csrf_token: str, db_session: AsyncSession
):
    """旧形式（`candidates` を持たない平坦な形）が引き続き保存できること。"""
    q = await _questionnaire(db_session)
    status, body = await _post(
        location_saver,
        csrf_token,
        q.id,
        {
            "items": [
                {
                    "link_id": "lk1",
                    "source_category": "fhir",
                    "location": "FHIR: Observation",
                    "unit_id": "fhir:Observation.value",
                }
            ]
        },
    )
    assert status == 200 and body["saved"] == 1
    saved = await _saved_candidates(db_session, q.id)
    assert len(saved) == 1
    assert saved[0]["source_category"] == "fhir"
    assert saved[0]["unit_id"] == "fhir:Observation.value"


@pytest.mark.asyncio
async def test_save_system_empty_candidates_clears_instead_of_falling_back(
    location_saver: AsyncClient, csrf_token: str, db_session: AsyncSession
):
    """**空配列は「全部消す」であって旧形式へのフォールバックではない。**

    平坦なフィールドを併記していても、`candidates: []` が明示されていればそちらが勝つ。
    """
    q = await _questionnaire(db_session)
    status, _ = await _post(
        location_saver,
        csrf_token,
        q.id,
        {"items": [{"link_id": "lk1", "candidates": [], "location": "FHIR: Observation"}]},
    )
    assert status == 200
    assert await _saved_candidates(db_session, q.id) == []


@pytest.mark.asyncio
async def test_save_system_rejects_too_many_candidates(
    location_saver: AsyncClient, csrf_token: str, db_session: AsyncSession
):
    """上限（`MAX_LOCATION_CANDIDATES`）を超える候補は 422 で拒否し、**1件も保存しない**。"""
    q = await _questionnaire(db_session)
    over = [
        {"source_category": "local_db", "location": f"表{i}"}
        for i in range(MAX_LOCATION_CANDIDATES + 1)
    ]
    status, _ = await _post(
        location_saver, csrf_token, q.id, {"items": [{"link_id": "lk1", "candidates": over}]}
    )
    assert status == 422
    assert (
        await db_session.execute(
            select(SystemLocation).where(SystemLocation.questionnaire_id == q.id)
        )
    ).first() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "items",
    [
        pytest.param(
            [{"link_id": "lk1", "candidates": {"location": "x"}}], id="candidates-not-a-list"
        ),
        pytest.param([{"link_id": "lk1", "candidates": ["文字列"]}], id="candidate-not-an-object"),
        pytest.param(
            [{"link_id": "lk1", "candidates": [{"payload": "not-a-dict"}]}], id="payload-not-a-dict"
        ),
        pytest.param(
            [{"link_id": "lk1", "candidates": [{"unit_id": 123}]}], id="unit_id-not-a-str"
        ),
        pytest.param([{"item_text": "link_id が無い"}], id="link_id-missing"),
        pytest.param("items-not-a-list", id="items-not-a-list"),
    ],
)
async def test_save_system_rejects_malformed_requests(
    location_saver: AsyncClient, csrf_token: str, db_session: AsyncSession, items: object
):
    """型の食い違いを 422 で止める（以前は `_clean_candidate` の暗黙の正規化任せだった）。"""
    q = await _questionnaire(db_session)
    status, _ = await _post(location_saver, csrf_token, q.id, {"items": items})
    assert status == 422


# --------------------------------------------------------------------------- #
# 優先順の複数候補
#
# **順序の退行検知はサーバ側（保存 API のラウンドトリップ）で固定する。** 画面側の
# `collectItems()` はテンプレートのインライン JS で、レンダリング結果への文字列 assert は
# 「存在したまま効かなくする」変更を捕まえられない。
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_save_system_keeps_the_priority_order_of_multiple_candidates(
    location_saver: AsyncClient, csrf_token: str, db_session: AsyncSession
):
    """複数候補を送った順（＝優先順）が、保存された配列順と一致すること。

    `SystemLocation.candidates` は設計上「優先順のデータ所在候補の配列」で、
    ステップ3のフォールバック取得計画はこの配列順を読む。並べ替えられて
    保存されると、**運用者が指定した優先順と違う順で取得される**。
    """
    q = await _questionnaire(db_session)
    status, body = await _post(
        location_saver,
        csrf_token,
        q.id,
        {
            "items": [
                {
                    "link_id": "lk1",
                    "item_text": "診療録番号",
                    "candidates": [
                        {"source_category": "fhir", "location": "FHIR: Observation（第1）"},
                        {"source_category": "local_db", "location": "DB: 院内（第2）"},
                        {"source_category": "external_db", "location": "DWH: 倉庫（第3）"},
                    ],
                }
            ]
        },
    )

    assert status == 200 and body["saved"] == 1
    saved = await _saved_candidates(db_session, q.id)
    assert [c["location"] for c in saved] == [
        "FHIR: Observation（第1）",
        "DB: 院内（第2）",
        "DWH: 倉庫（第3）",
    ]
    assert saved[0]["source_category"] == "fhir"


@pytest.mark.asyncio
async def test_a_reordered_save_replaces_the_previous_order(
    location_saver: AsyncClient, csrf_token: str, db_session: AsyncSession
):
    """並べ替えて保存し直すと、新しい順序で置き換わること。

    追記されると、画面で入れ替えたつもりの優先順が反映されないまま古い順が残る。
    """
    q = await _questionnaire(db_session)
    first = {"source_category": "fhir", "location": "FHIR: Observation"}
    second = {"source_category": "local_db", "location": "DB: 院内"}

    await _post(
        location_saver,
        csrf_token,
        q.id,
        {"items": [{"link_id": "lk1", "candidates": [first, second]}]},
    )
    await _post(
        location_saver,
        csrf_token,
        q.id,
        {"items": [{"link_id": "lk1", "candidates": [second, first]}]},
    )

    saved = await _saved_candidates(db_session, q.id)
    assert [c["location"] for c in saved] == ["DB: 院内", "FHIR: Observation"]


@pytest.mark.asyncio
async def test_a_single_candidate_save_is_unchanged(
    location_saver: AsyncClient, csrf_token: str, db_session: AsyncSession
):
    """1候補だけの保存（既存データの形）が壊れないこと。"""
    q = await _questionnaire(db_session)
    status, _ = await _post(
        location_saver,
        csrf_token,
        q.id,
        {
            "items": [
                {
                    "link_id": "lk1",
                    "candidates": [{"source_category": "local_db", "location": "DB: X"}],
                }
            ]
        },
    )

    assert status == 200
    saved = await _saved_candidates(db_session, q.id)
    assert len(saved) == 1
    assert saved[0]["location"] == "DB: X"
