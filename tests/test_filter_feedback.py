"""一覧フィルタ不正入力のユーザー通知のテスト。

- parse_int_filter の単体（有効/空/不正）
- 一覧ルートで不正フィルタ値が 500 でなく 200＋通知になる（潜在バグ修正含む）
- 正常フィルタでは通知が出ない
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.models.dataset import Dataset
from kawasemi.models.questionnaire import Questionnaire
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
async def test_input_targets_invalid_filter_no_500(collector_client: AsyncClient) -> None:
    """不正な dataset_id フィルタでも 500 にならず 200＋通知（潜在バグ修正）。

    **`collector` で叩く**。入力対象の一覧は `patient_id` を含み、そのまま
    受け取りの入口になるので、手動収集に認可されたロールに限定した（admin は役割を
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
