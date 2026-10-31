"""典拠の範囲（様式ごとに典拠にしてよい規約文書）。

ある領域の様式に、別の領域の登録の定義文が典拠つきで入ったことがある。
索引には別の領域の規約しか無く、項目名が似ていればそれが選ばれていた。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from httpx import AsyncClient

from kawasemi.ai_input.clinical_drafter import AIClinicalDrafter
from kawasemi.ai_input.reference_lookup import find_reference_units
from kawasemi.ai_input.reference_scope import reference_paths_for, reset_reference_scope_cache
from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import RoleType
from kawasemi.models.user import User
from tests.ai_input.test_reference_lookup import _unit, _write_index

CANCER = "http://kawasemi.example/Questionnaire/cancer-registry"
INFECTION = "https://example.org/fhir/Questionnaire/infection-sample"


@pytest.fixture
def scopes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "reference_scopes.yaml"
    path.write_text(
        f"scopes:\n  - name: がん\n    questionnaires: [{CANCER}]\n    references: [cancer.pdf]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "reference_scopes_file", str(path))
    reset_reference_scope_cache()
    return path


def _ref(path: str, page: int, text: str) -> dict[str, Any]:
    u = _unit(page=page, start=0, end=800, text=text)
    u["unit_id"] = f"file:{path}#p{page}:0-800"
    u["payload"]["path"] = path
    return u


@pytest.fixture
def two_manuals(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    path = _write_index(
        tmp_path / "resource_index",
        [
            _ref("cancer.pdf", 12, "初診日は、対象疾患について初めて受診した日"),
            _ref("infection.pdf", 3, "初診日は、発症後に初めて医療機関を受診した日"),
        ],
    )
    monkeypatch.setattr(settings, "resource_index_dir", path)
    return path


def test_without_the_file_there_is_no_scope() -> None:
    """設定ファイルが無ければ範囲を設けない（従来どおり。conftest は無い状態に固定している）。"""
    assert reference_paths_for(CANCER) is None


def test_a_listed_questionnaire_gets_its_references(scopes: Path) -> None:
    assert reference_paths_for(CANCER) == frozenset({"cancer.pdf"})


def test_an_unlisted_questionnaire_gets_no_references(scopes: Path) -> None:
    """**載っていない様式には典拠を出さない。** 別の領域の定義を写させないため。"""
    assert reference_paths_for(INFECTION) == frozenset()
    # 前方一致にしない（URL が領域を表さない様式がある）
    assert reference_paths_for(CANCER + "/flash") == frozenset()


def test_the_lookup_is_narrowed_to_the_scope(two_manuals: str) -> None:
    hits, total = find_reference_units(reference_paths=frozenset({"infection.pdf"}))
    assert total == 1 and hits[0].path == "infection.pdf"
    _, everything = find_reference_units()
    assert everything == 2


@pytest.mark.asyncio
async def test_an_empty_scope_does_not_search_or_call_the_llm(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    drafter = AIClinicalDrafter()

    def _must_not_run(*_a: Any, **_k: Any) -> Any:
        raise AssertionError("範囲が空なのに索引を引いた")

    monkeypatch.setattr("kawasemi.ai_input.clinical_drafter.get_store", _must_not_run)
    result = await drafter.draft(
        [{"link_id": "a", "item_text": "初診日"}], reference_paths=frozenset()
    )
    assert result.reference_scope_empty is True
    assert result.items == [] and result.no_hit_count == 1 and result.llm_model is None


def _act_as(client: AsyncClient, user: User, role: str) -> None:
    token = create_access_token(data={"sub": str(user.id), "act": role})
    client.cookies.set(settings.jwt_cookie_name, token)


@pytest.mark.asyncio
async def test_the_endpoint_narrows_by_the_questionnaire(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: Any,
    scopes: Path,
    two_manuals: str,
) -> None:
    q = Questionnaire(
        fhir_id="infection-sample",
        title="感染症の様式",
        status="active",
        version="1",
        url=INFECTION,
        content={"resourceType": "Questionnaire", "item": []},
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await designer_client.get("/ai-input/reference-units", params={"questionnaire_id": q.id})
    body = resp.json()
    assert resp.status_code == 200 and body["total"] == 0, "範囲の外の規約が様式に出た"
    assert body["reference_scope_empty"] is True, "範囲が空の0件を「該当なし」と区別していない"

    resp = await designer_client.get("/ai-input/reference-units")
    assert resp.json()["total"] == 2, "様式を渡さなければ従来どおり"
    assert resp.json()["reference_scope_empty"] is False


def test_the_page_range_is_narrowed_to_the_scope(two_manuals: str) -> None:
    from kawasemi.ai_input.reference_lookup import reference_page_range

    assert reference_page_range(reference_paths=frozenset({"infection.pdf"})) == (3, 3)
    assert reference_page_range() == (3, 12)


@pytest.mark.parametrize(
    "body",
    [
        "scopes:\n  - questionnaires: [http://x]\n    references: registry_manual.pdf\n",
        "scopes:\n  - questionnaires: http://x\n    references: [a.pdf]\n",
        "- just a list\n",
        "scopes: [\n",
    ],
)
def test_a_malformed_file_is_an_error_not_a_silent_scope(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, body: str
) -> None:
    """文字列を集合にすると1文字ずつに割れて典拠が黙って0件になる。形が悪ければ止める。"""
    from kawasemi.ai_input.reference_scope import ReferenceScopeError

    path = tmp_path / "reference_scopes.yaml"
    path.write_text(body, encoding="utf-8")
    monkeypatch.setattr(settings, "reference_scopes_file", str(path))
    reset_reference_scope_cache()
    with pytest.raises(ReferenceScopeError):
        reference_paths_for(CANCER)


def test_a_file_placed_after_start_takes_effect_without_restart(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """置く前の「範囲なし」を握り続けない（更新時刻で読み直す）。"""
    import os

    path = tmp_path / "reference_scopes.yaml"
    monkeypatch.setattr(settings, "reference_scopes_file", str(path))
    reset_reference_scope_cache()
    assert reference_paths_for(INFECTION) is None
    path.write_text(
        f"scopes:\n  - questionnaires: [{CANCER}]\n    references: [c.pdf]\n", encoding="utf-8"
    )
    assert reference_paths_for(INFECTION) == frozenset()
    path.write_text(
        f"scopes:\n  - questionnaires: [{INFECTION}]\n    references: [i.pdf]\n", encoding="utf-8"
    )
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 5))
    assert reference_paths_for(INFECTION) == frozenset({"i.pdf"})
