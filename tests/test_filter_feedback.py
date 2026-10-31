"""一覧フィルタ不正入力のユーザー通知のテスト。

- parse_int_filter の単体（有効/空/不正）
- 一覧ルートで不正フィルタ値が 500 でなく 200＋通知になる（潜在バグ修正含む）
- 正常フィルタでは通知が出ない
- 作成/更新で不正な due_date は未設定＋一覧で通知
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.models.case_assignment import AssignmentStatus, CaseAssignment
from kawasemi.models.dataset import Dataset
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.user import User
from kawasemi.web.filter_utils import parse_int_filter


async def _make_questionnaire(db: AsyncSession) -> Questionnaire:
    q = Questionnaire(
        fhir_id="q-filter", content={"resourceType": "Questionnaire"}, status="active"
    )
    db.add(q)
    await db.commit()
    await db.refresh(q)
    return q


def test_parse_int_filter_valid() -> None:
    warnings: list[str] = []
    assert parse_int_filter("42", "X", warnings) == 42
    assert warnings == []


def test_parse_int_filter_empty_or_none() -> None:
    warnings: list[str] = []
    assert parse_int_filter(None, "X", warnings) is None
    assert parse_int_filter("  ", "X", warnings) is None
    assert warnings == []


def test_parse_int_filter_invalid_appends_warning() -> None:
    warnings: list[str] = []
    assert parse_int_filter("abc", "Questionnaire", warnings) is None
    assert len(warnings) == 1
    assert "Questionnaire" in warnings[0] and "abc" in warnings[0]


@pytest.mark.asyncio
async def test_assignments_invalid_filter_shows_warning(collector_client: AsyncClient) -> None:
    """不正な questionnaire_id フィルタは 200＋通知（黙って無視しない）。"""
    resp = await collector_client.get("/assignments?questionnaire_id=notanumber")
    assert resp.status_code == 200
    assert "無視しました" in resp.text


@pytest.mark.asyncio
async def test_assignments_valid_filter_no_warning(collector_client: AsyncClient) -> None:
    resp = await collector_client.get("/assignments?questionnaire_id=1")
    assert resp.status_code == 200
    assert "無視しました" not in resp.text


@pytest.mark.asyncio
async def test_input_targets_invalid_filter_no_500(collector_client: AsyncClient) -> None:
    """不正な dataset_id フィルタでも 500 にならず 200＋通知（潜在バグ修正）。

    **`collector` で叩く**。入力対象の一覧は `patient_id` を含み、そのまま
    `start` の入口になるので、手動収集に認可されたロールに限定した（admin は役割を
    切り替えて入る＝ハード・スコープの既定の作法）。
    """
    resp = await collector_client.get("/input-targets?dataset_id=xxx")
    assert resp.status_code == 200
    assert "無視しました" in resp.text


@pytest.mark.asyncio
async def test_show_dataset_invalid_filter_no_500(
    collector_client: AsyncClient, db_session: AsyncSession
) -> None:
    """データセット詳細の不正フィルタでも 500 にならず 200＋通知（潜在バグ修正・issue記載経路）。"""
    ds = Dataset(name="フィルタ検証用")
    db_session.add(ds)
    await db_session.commit()
    await db_session.refresh(ds)

    resp = await collector_client.get(f"/datasets/{ds.id}?questionnaire_id=abc")
    assert resp.status_code == 200
    assert "無視しました" in resp.text


@pytest.mark.asyncio
async def test_create_assignment_invalid_due_date_notifies(
    collector_client: AsyncClient, collector_user: User, db_session: AsyncSession, csrf_token: str
) -> None:
    """不正な due_date で作成すると /assignments?warn=due_date へ遷移し一覧で通知。"""
    q = await _make_questionnaire(db_session)
    resp = await collector_client.post(
        "/assignments/new",
        data={
            "questionnaire_id": str(q.id),
            "patient_id": "P1",
            "assigned_to_id": str(collector_user.id),
            "due_date": "not-a-date",
            "csrf_token": csrf_token,
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/assignments?warn=due_date"

    listing = await collector_client.get("/assignments?warn=due_date")
    assert listing.status_code == 200
    assert "期限の日付形式が不正" in listing.text


@pytest.mark.asyncio
async def test_create_assignment_valid_due_date_no_warn(
    collector_client: AsyncClient, collector_user: User, db_session: AsyncSession, csrf_token: str
) -> None:
    """正常な due_date では warn を付けず /assignments へ遷移する（否定側）。"""
    q = await _make_questionnaire(db_session)
    resp = await collector_client.post(
        "/assignments/new",
        data={
            "questionnaire_id": str(q.id),
            "patient_id": "P2",
            "assigned_to_id": str(collector_user.id),
            "due_date": "2026-12-31",
            "csrf_token": csrf_token,
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/assignments"


@pytest.mark.asyncio
async def test_update_assignment_invalid_due_date_notifies(
    collector_client: AsyncClient, collector_user: User, db_session: AsyncSession, csrf_token: str
) -> None:
    """更新でも不正 due_date は未設定＋?warn=due_date へ遷移。"""
    q = await _make_questionnaire(db_session)
    a = CaseAssignment(
        questionnaire_id=q.id,
        patient_id="P3",
        assigned_to_id=collector_user.id,
        status=AssignmentStatus.PENDING,
    )
    db_session.add(a)
    await db_session.commit()
    await db_session.refresh(a)

    resp = await collector_client.post(
        f"/assignments/{a.id}/edit",
        data={
            "questionnaire_id": str(q.id),
            "patient_id": "P3",
            "assigned_to_id": str(collector_user.id),
            "status": AssignmentStatus.PENDING,
            "due_date": "31-12-2026",
            "csrf_token": csrf_token,
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/assignments?warn=due_date"
    await db_session.refresh(a)
    assert a.due_date is None
