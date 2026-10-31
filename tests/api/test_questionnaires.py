"""Questionnaire APIのテスト

Questionnaire の作成（ファイル取り込み）・削除は admin、内容の更新は designer。
いずれも CSRF トークンを要求する。取得・一覧は認証済みユーザーであれば可能。
各テストは admin_client で作り、更新では設計者に切り替える。
"""

import pytest
from httpx import AsyncClient

from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.role import RoleType
from kawasemi.models.user import User


def _act_as(client: AsyncClient, user: User, role: str) -> None:
    token = create_access_token(data={"sub": str(user.id), "act": role})
    client.cookies.set(settings.jwt_cookie_name, token)


@pytest.mark.asyncio
async def test_create_questionnaire(admin_client: AsyncClient, csrf_token: str):
    """Questionnaire作成のテスト"""
    questionnaire_data = {
        "resource": {
            "resourceType": "Questionnaire",
            "status": "active",
            "title": "テスト用質問票",
            "item": [{"linkId": "q1", "type": "string", "text": "質問1", "required": True}],
        }
    }

    response = await admin_client.post(
        "/api/v100/questionnaires/",
        json=questionnaire_data,
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 201
    data = response.json()
    assert data["title"] == "テスト用質問票"
    assert data["status"] == "active"
    assert "id" in data
    assert "fhir_id" in data


@pytest.mark.asyncio
async def test_get_questionnaire(admin_client: AsyncClient, csrf_token: str):
    """Questionnaire取得のテスト"""
    # まずQuestionnaireを作成
    create_data = {
        "resource": {
            "resourceType": "Questionnaire",
            "status": "active",
            "title": "取得テスト用",
            "item": [],
        }
    }

    create_response = await admin_client.post(
        "/api/v100/questionnaires/",
        json=create_data,
        headers={"X-CSRF-Token": csrf_token},
    )
    created_id = create_response.json()["id"]

    # 取得
    response = await admin_client.get(f"/api/v100/questionnaires/{created_id}")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == created_id
    assert data["title"] == "取得テスト用"


@pytest.mark.asyncio
async def test_list_questionnaires(admin_client: AsyncClient, csrf_token: str):
    """Questionnaire一覧取得のテスト"""
    # 複数作成
    for i in range(3):
        await admin_client.post(
            "/api/v100/questionnaires/",
            json={
                "resource": {
                    "resourceType": "Questionnaire",
                    "status": "active",
                    "title": f"質問票{i}",
                    "item": [],
                }
            },
            headers={"X-CSRF-Token": csrf_token},
        )

    # 一覧取得
    response = await admin_client.get("/api/v100/questionnaires/")

    assert response.status_code == 200
    data = response.json()
    assert len(data) >= 3


@pytest.mark.asyncio
async def test_get_nonexistent_questionnaire(admin_client: AsyncClient):
    """存在しないQuestionnaireの取得テスト"""
    response = await admin_client.get("/api/v100/questionnaires/99999")

    assert response.status_code == 404
    data = response.json()
    assert "error_code" in data
    assert data["error_code"] == "RESOURCE_NOT_FOUND"


@pytest.mark.asyncio
async def test_update_questionnaire(
    admin_client: AsyncClient, designer_user: User, csrf_token: str
):
    """Questionnaire更新のテスト"""
    # まずQuestionnaireを作成
    create_data = {
        "resource": {
            "resourceType": "Questionnaire",
            "status": "draft",
            "title": "更新前タイトル",
            "item": [],
        }
    }

    create_response = await admin_client.post(
        "/api/v100/questionnaires/",
        json=create_data,
        headers={"X-CSRF-Token": csrf_token},
    )
    created_id = create_response.json()["id"]

    # 更新（様式の内容の編集は設計者）
    _act_as(admin_client, designer_user, RoleType.DESIGNER)
    update_data = {
        "resource": {
            "resourceType": "Questionnaire",
            "status": "active",
            "title": "更新後タイトル",
            "item": [{"linkId": "q1", "type": "string", "text": "新しい質問", "required": True}],
        }
    }

    response = await admin_client.put(
        f"/api/v100/questionnaires/{created_id}",
        json=update_data,
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == created_id
    assert data["title"] == "更新後タイトル"
    assert data["status"] == "active"


@pytest.mark.asyncio
async def test_delete_questionnaire(admin_client: AsyncClient, csrf_token: str):
    """Questionnaire削除（論理削除）のテスト"""
    # まずQuestionnaireを作成
    create_data = {
        "resource": {
            "resourceType": "Questionnaire",
            "status": "active",
            "title": "削除テスト用",
            "item": [],
        }
    }

    create_response = await admin_client.post(
        "/api/v100/questionnaires/",
        json=create_data,
        headers={"X-CSRF-Token": csrf_token},
    )
    created_id = create_response.json()["id"]

    # 削除
    delete_response = await admin_client.delete(
        f"/api/v100/questionnaires/{created_id}",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert delete_response.status_code == 204

    # 削除後は取得できないことを確認
    get_response = await admin_client.get(f"/api/v100/questionnaires/{created_id}")
    assert get_response.status_code == 404


@pytest.mark.asyncio
async def test_update_nonexistent_questionnaire(
    admin_client: AsyncClient, designer_user: User, csrf_token: str
):
    """存在しないQuestionnaireの更新テスト"""
    _act_as(admin_client, designer_user, RoleType.DESIGNER)
    update_data = {
        "resource": {
            "resourceType": "Questionnaire",
            "status": "active",
            "title": "存在しない",
            "item": [],
        }
    }

    response = await admin_client.put(
        "/api/v100/questionnaires/99999",
        json=update_data,
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 404
    data = response.json()
    assert data["error_code"] == "RESOURCE_NOT_FOUND"


@pytest.mark.asyncio
async def test_delete_nonexistent_questionnaire(admin_client: AsyncClient, csrf_token: str):
    """存在しないQuestionnaireの削除テスト"""
    response = await admin_client.delete(
        "/api/v100/questionnaires/99999",
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 404
    data = response.json()
    assert data["error_code"] == "RESOURCE_NOT_FOUND"
