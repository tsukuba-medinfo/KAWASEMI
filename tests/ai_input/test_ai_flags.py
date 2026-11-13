"""AI機能フラグ（ENABLE_AI_DRAFTING / ENABLE_AI_EXTRACTION）のガードテスト

docs/ai_flags_spec.md の検証:
- ルーターは常時マウント（フラグ両方 false でも手動経路は使える）
- DRAFTING=false → ドラフト生成系エンドポイントが 403
- EXTRACTION=false → LLM op 入りプランの保存 400 / 実行 403（二段ガード）
- 旧 ENABLE_AI が設定されていたら Settings が起動時エラー
"""

import pytest
from httpx import AsyncClient
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.models import InputPlan, InputRunbook
from kawasemi.core.auth import create_access_token
from kawasemi.core.config import Settings, settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import RoleType
from kawasemi.models.user import User
from tests.lease_helpers import lease_patient


def _act_as(client: AsyncClient, user: User, role: str) -> None:
    """ハード・スコープのアクティブ役割を role に切り替えた JWT を client に設定する。

    設計役割は specific role（data_manager / plan_designer など）で保護されるため、
    設計3ロール保有ユーザーでも対象ルートに合わせて active role を切り替える必要がある。
    """
    token = create_access_token(data={"sub": str(user.id), "act": role})
    client.cookies.set(settings.jwt_cookie_name, token)


LLM_PROGRAM = {
    "steps": [
        {"op": "llm_extract", "id": "s1", "prompt": "所見から有無を抽出せよ"},
    ],
    "return": "$s1",
}


async def _make_questionnaire(db_session: AsyncSession) -> Questionnaire:
    q = Questionnaire(
        fhir_id="q-ai-flags",
        title="AIフラグテスト票",
        status="active",
        content={"resourceType": "Questionnaire", "id": "q-ai-flags"},
    )
    db_session.add(q)
    await db_session.flush()
    return q


@pytest.mark.asyncio
async def test_manual_routes_available_with_all_flags_off(
    designer_client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """両フラグ false でもデータ自動収集（手動経路）は常時マウントされ利用できる"""
    monkeypatch.setattr(settings, "enable_ai_drafting", False)
    monkeypatch.setattr(settings, "enable_ai_extraction", False)
    resp = await designer_client.get("/ai-input/dashboard")
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_drafting_endpoints_403_when_drafting_off(
    designer_client: AsyncClient,
    designer_user: User,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DRAFTING=false でドラフト生成系6エンドポイントが 403 を返す

    各ドラフト生成ルートは specific な設計役割で保護される。設計3ロール保有
    ユーザーでも、対象ルートに合わせて active role を切り替えてから呼ぶ。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", False)
    headers = {"X-CSRF-Token": csrf_token}
    # (URL, そのルートが要求する設計役割)
    endpoints = [
        (
            # 臨床的意味の典拠つき下書き。step1-A なので収集責任者。
            "/ai-input/questionnaires/999999/source-survey/clinical/ai-draft",
            RoleType.DESIGNER,
        ),
        (
            "/ai-input/questionnaires/999999/source-survey/system/ai-draft",
            RoleType.DESIGNER,
        ),
        ("/ai-input/questionnaires/999999/runbook/generate", RoleType.DESIGNER),
        (
            "/ai-input/questionnaires/999999/runbook/item1/regenerate",
            RoleType.DESIGNER,
        ),
        ("/ai-input/questionnaires/999999/plan/compile", RoleType.DESIGNER),
        (
            "/ai-input/questionnaires/999999/plan/item1/recompile",
            RoleType.DESIGNER,
        ),
    ]
    for url, role in endpoints:
        _act_as(designer_client, designer_user, role)
        resp = await designer_client.post(url, json={}, headers=headers)
        assert resp.status_code == 403, url
        assert "ENABLE_AI_DRAFTING" in resp.json()["message"], url


@pytest.mark.asyncio
async def test_save_plan_rejects_llm_ops_when_extraction_off(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """二段ガード1段目: EXTRACTION=false では LLM op 入りプランの手動保存も 400"""
    # プラン保存ルートは plan_designer / data_manager で保護される
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    q = await _make_questionnaire(db_session)
    db_session.add(
        InputRunbook(
            questionnaire_id=q.id,
            link_id="item1",
            item_text="テスト項目",
            steps=[{"order": 1, "title": "抽出", "description": "x"}],
            status="confirmed",
        )
    )
    await db_session.commit()

    monkeypatch.setattr(settings, "enable_ai_extraction", False)
    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/plan/item1",
        json={"program": LLM_PROGRAM},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 400
    assert "ENABLE_AI_EXTRACTION" in resp.json()["message"]


@pytest.mark.asyncio
async def test_execute_autofill_rejects_llm_plan_when_extraction_off(
    collector_client: AsyncClient,
    db_session: AsyncSession,
    collector_user: User,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """二段ガード2段目（権威）: EXTRACTION=false では LLM op 入り確認済みプランを実行前に 403"""
    q = await _make_questionnaire(db_session)
    db_session.add(
        InputPlan(
            questionnaire_id=q.id,
            link_id="item1",
            item_text="テスト項目",
            program=LLM_PROGRAM,
            status="confirmed",
            llm_model="model-a:12b",
        )
    )
    # 本番 autofill の患者スコープ（有効なリース）を満たし、extraction ガードまで到達させる。
    await lease_patient(db_session, collector_user.id, "PAT-001", questionnaire_id=q.id)

    monkeypatch.setattr(settings, "enable_ai_extraction", False)
    resp = await collector_client.post(
        f"/ai-input/questionnaires/{q.id}/autofill",
        json={"patient_id": "PAT-001"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 403
    body = resp.json()
    assert "ENABLE_AI_EXTRACTION" in body["message"]
    assert "item1" in body["message"]


def test_legacy_enable_ai_env_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """廃止済み ENABLE_AI 環境変数が設定されていたら Settings が起動時エラーで案内する"""
    monkeypatch.setenv("ENABLE_AI", "true")
    with pytest.raises(ValidationError, match="ENABLE_AI は廃止されました"):
        Settings(  # type: ignore[call-arg]
            secret_key="x" * 32,
            csrf_secret_key="y" * 32,
        )
