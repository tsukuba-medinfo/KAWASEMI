"""QuestionnaireResponse APIのテスト

回答の作成は collector、一覧・単一取得・一括エクスポートは reviewer を要求し、いずれも
CSRF トークンを伴う。Questionnaire の登録（ファイル取り込み）は admin。
各テストは admin_client で様式を作り、収集者と閲覧・検証者を保有する `multi_role_user` に
アクティブ役割を切り替えて回答のルートへアクセスする（管理者は回答に触れない）。
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.role import RoleType
from kawasemi.models.user import User
from tests.lease_helpers import lease_patient


def _act_as(client: AsyncClient, user: User, role: str) -> None:
    """client の利用者とアクティブ役割を切り替える（ハード・スコープ）。"""
    token = create_access_token(data={"sub": str(user.id), "act": role})
    client.cookies.set(settings.jwt_cookie_name, token)


@pytest.mark.asyncio
async def test_create_questionnaire_response(
    admin_client: AsyncClient,
    multi_role_user: User,
    csrf_token: str,
    db_session: AsyncSession,
):
    """QuestionnaireResponse作成のテスト"""
    # まずQuestionnaireを作成
    questionnaire_data = {
        "resource": {
            "resourceType": "Questionnaire",
            "status": "active",
            "title": "回答テスト用",
            "item": [
                {"linkId": "name", "type": "string", "text": "氏名", "required": True},
                {"linkId": "age", "type": "integer", "text": "年齢", "required": False},
            ],
        }
    }

    q_response = await admin_client.post(
        "/api/v100/questionnaires/",
        json=questionnaire_data,
        headers={"X-CSRF-Token": csrf_token},
    )
    questionnaire_id = q_response.json()["id"]
    await lease_patient(db_session, multi_role_user.id, "123", questionnaire_id=questionnaire_id)
    _act_as(admin_client, multi_role_user, RoleType.COLLECTOR)

    # QuestionnaireResponse作成
    response_data = {
        "questionnaire_id": questionnaire_id,
        "answers": {"name": "山田太郎", "age": "30"},
        "subject": "Patient/123",
        "author": "Practitioner/456",
    }

    response = await admin_client.post(
        "/api/v100/questionnaire-responses/",
        json=response_data,
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 201
    data = response.json()
    assert data["questionnaire_id"] == questionnaire_id
    assert data["subject"] == "Patient/123"
    assert data["status"] == "completed"


@pytest.mark.asyncio
async def test_get_questionnaire_response(
    admin_client: AsyncClient,
    multi_role_user: User,
    csrf_token: str,
    db_session: AsyncSession,
):
    """QuestionnaireResponse取得のテスト"""
    # Questionnaireを作成
    q_response = await admin_client.post(
        "/api/v100/questionnaires/",
        json={
            "resource": {
                "resourceType": "Questionnaire",
                "status": "active",
                "title": "テスト",
                "item": [{"linkId": "q1", "type": "string", "text": "質問"}],
            }
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    questionnaire_id = q_response.json()["id"]
    await lease_patient(db_session, multi_role_user.id, "123", questionnaire_id=questionnaire_id)
    _act_as(admin_client, multi_role_user, RoleType.COLLECTOR)

    # QuestionnaireResponse作成
    create_response = await admin_client.post(
        "/api/v100/questionnaire-responses/",
        json={
            "questionnaire_id": questionnaire_id,
            "answers": {"q1": "回答"},
            "subject": "Patient/123",
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    response_id = create_response.json()["id"]

    # 取得
    _act_as(admin_client, multi_role_user, RoleType.REVIEWER)
    response = await admin_client.get(f"/api/v100/questionnaire-responses/{response_id}")

    assert response.status_code == 200
    data = response.json()
    assert data["id"] == response_id


@pytest.mark.asyncio
async def test_list_questionnaire_responses_with_filter(
    admin_client: AsyncClient,
    multi_role_user: User,
    csrf_token: str,
    db_session: AsyncSession,
):
    """QuestionnaireResponseフィルタ付き一覧取得のテスト

    同じ患者の回答が2件ある状況（別の様式に対する回答）で、subject の絞り込みが複数件を返す。
    """
    questionnaire_ids = []
    for title in ("フィルタテスト1", "フィルタテスト2"):
        q_response = await admin_client.post(
            "/api/v100/questionnaires/",
            json={
                "resource": {
                    "resourceType": "Questionnaire",
                    "status": "active",
                    "title": title,
                    "item": [],
                }
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        questionnaire_ids.append(q_response.json()["id"])
    q1, q2 = questionnaire_ids
    # 患者 123 は両方の様式に担当を持つ（2件）。患者 456 は片方だけ（1件）
    saves = [(q1, "123"), (q2, "123"), (q1, "456")]
    for qid, pid in saves:
        await lease_patient(db_session, multi_role_user.id, pid, questionnaire_id=qid)
    _act_as(admin_client, multi_role_user, RoleType.COLLECTOR)

    for qid, pid in saves:
        resp = await admin_client.post(
            "/api/v100/questionnaire-responses/",
            json={"questionnaire_id": qid, "answers": {}, "subject": f"Patient/{pid}"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp.status_code == 201

    # subject でフィルタ
    _act_as(admin_client, multi_role_user, RoleType.REVIEWER)
    response = await admin_client.get("/api/v100/questionnaire-responses/?subject=Patient/123")

    assert response.status_code == 200
    data = response.json()
    assert len(data) == 2
    assert all(item["subject"] == "Patient/123" for item in data)
    assert {item["questionnaire_id"] for item in data} == {q1, q2}


@pytest.mark.asyncio
async def test_bulk_export_questionnaire_responses(
    admin_client: AsyncClient,
    multi_role_user: User,
    csrf_token: str,
    db_session: AsyncSession,
):
    """QuestionnaireResponse一括エクスポートのテスト"""
    # Questionnaireを作成
    q_response = await admin_client.post(
        "/api/v100/questionnaires/",
        json={
            "resource": {
                "resourceType": "Questionnaire",
                "status": "active",
                "title": "一括エクスポートテスト",
                "item": [{"linkId": "q1", "type": "string", "text": "質問"}],
            }
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    questionnaire_id = q_response.json()["id"]
    await lease_patient(db_session, multi_role_user.id, "0", questionnaire_id=questionnaire_id)
    await lease_patient(db_session, multi_role_user.id, "1", questionnaire_id=questionnaire_id)
    await lease_patient(db_session, multi_role_user.id, "2", questionnaire_id=questionnaire_id)
    _act_as(admin_client, multi_role_user, RoleType.COLLECTOR)

    # 複数のQuestionnaireResponseを作成
    for i in range(3):
        await admin_client.post(
            "/api/v100/questionnaire-responses/",
            json={
                "questionnaire_id": questionnaire_id,
                "answers": {"q1": f"回答{i}"},
                "subject": f"Patient/{i}",
            },
            headers={"X-CSRF-Token": csrf_token},
        )

    # 一括エクスポートは reviewer の操作（ハード・スコープ）。
    # 既定は検証済のみ。本テストは未検証回答の機構確認なので include_unverified=true。
    _act_as(admin_client, multi_role_user, RoleType.REVIEWER)
    response = await admin_client.post(
        f"/api/v100/questionnaire-responses/bulk-export"
        f"?questionnaire_id={questionnaire_id}&include_unverified=true",
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "BulkExport_" in response.headers["content-disposition"]


@pytest.mark.asyncio
async def test_bulk_export_no_data(
    admin_client: AsyncClient, multi_role_user: User, csrf_token: str
):
    """一括エクスポートでデータがない場合のテスト"""
    # 一括エクスポートは reviewer の操作（ハード・スコープ）
    _act_as(admin_client, multi_role_user, RoleType.REVIEWER)
    # 存在しないquestionnaire_idでエクスポートを試みる
    response = await admin_client.post(
        "/api/v100/questionnaire-responses/bulk-export?questionnaire_id=99999",
        headers={"X-CSRF-Token": csrf_token},
    )

    assert response.status_code == 422
    data = response.json()
    assert "error_code" in data


@pytest.mark.asyncio
async def test_collector_cannot_get_single_response(
    admin_client: AsyncClient,
    multi_role_user: User,
    collector_user: User,
    csrf_token: str,
    db_session: AsyncSession,
):
    """collector は単一 QuestionnaireResponse を取得できない（PHI IDOR 防止・Stage2）

    QuestionnaireResponse は所有権属性を持たないため、一覧（reviewer 限定）と同様に
    単一取得も reviewer 限定とする。collector が ID 指定で他者回答を引き出せないこと。
    """
    from kawasemi.core.auth import create_access_token
    from kawasemi.core.config import settings

    # admin が Questionnaire を作り、収集者が QuestionnaireResponse を作成
    q_response = await admin_client.post(
        "/api/v100/questionnaires/",
        json={
            "resource": {
                "resourceType": "Questionnaire",
                "status": "active",
                "title": "IDORテスト",
                "item": [{"linkId": "q1", "type": "string", "text": "質問"}],
            }
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    questionnaire_id = q_response.json()["id"]
    await lease_patient(db_session, multi_role_user.id, "secret", questionnaire_id=questionnaire_id)
    _act_as(admin_client, multi_role_user, RoleType.COLLECTOR)
    create_response = await admin_client.post(
        "/api/v100/questionnaire-responses/",
        json={
            "questionnaire_id": questionnaire_id,
            "answers": {"q1": "秘匿回答"},
            "subject": "Patient/secret",
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    response_id = create_response.json()["id"]

    # 収集者だけの利用者に切り替えて取得を試みる → 403
    token = create_access_token(data={"sub": str(collector_user.id)})
    admin_client.cookies.set(settings.jwt_cookie_name, token)
    resp = await admin_client.get(f"/api/v100/questionnaire-responses/{response_id}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_response_rejected_for_deleted_questionnaire(
    admin_client: AsyncClient, multi_role_user: User, csrf_token: str
):
    """論理削除済み Questionnaire には回答を作成できない（404・Stage2）"""
    q_response = await admin_client.post(
        "/api/v100/questionnaires/",
        json={
            "resource": {
                "resourceType": "Questionnaire",
                "status": "active",
                "title": "削除予定",
                "item": [{"linkId": "q1", "type": "string", "text": "質問"}],
            }
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    questionnaire_id = q_response.json()["id"]

    del_resp = await admin_client.delete(
        f"/api/v100/questionnaires/{questionnaire_id}",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert del_resp.status_code == 204
    _act_as(admin_client, multi_role_user, RoleType.COLLECTOR)

    resp = await admin_client.post(
        "/api/v100/questionnaire-responses/",
        json={
            "questionnaire_id": questionnaire_id,
            "answers": {"q1": "回答"},
            "subject": "Patient/123",
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 404
