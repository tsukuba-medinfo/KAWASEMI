"""セキュリティテスト

このテストファイルは、実装されたセキュリティ機能を網羅的にテストします：
- 認証要件（Authentication）
- ロールベースアクセス制御（RBAC）
- CSRF保護
- レート制限
- エラーハンドリング（情報漏洩防止）
"""

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.user import User
from tests.e2e.helpers import extract_form_csrf_token

# ===== 認証要件テスト =====


class TestAuthentication:
    """認証要件のテスト"""

    @pytest.mark.asyncio
    async def test_api_requires_authentication_questionnaires_create(self, client: AsyncClient):
        """Questionnaire作成APIが認証を要求することを確認"""
        response = await client.post(
            "/api/v100/questionnaires/",
            json={
                "resource": {
                    "resourceType": "Questionnaire",
                    "status": "active",
                    "title": "テスト",
                    "item": [],
                }
            },
        )
        assert response.status_code == 401
        data = response.json()
        assert "detail" in data

    @pytest.mark.asyncio
    async def test_api_requires_authentication_questionnaires_get(self, client: AsyncClient):
        """Questionnaire取得APIが認証を要求することを確認"""
        response = await client.get("/api/v100/questionnaires/1")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_api_requires_authentication_questionnaires_list(self, client: AsyncClient):
        """Questionnaire一覧APIが認証を要求することを確認"""
        response = await client.get("/api/v100/questionnaires/")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_api_requires_authentication_questionnaires_update(self, client: AsyncClient):
        """Questionnaire更新APIが認証を要求することを確認"""
        response = await client.put(
            "/api/v100/questionnaires/1",
            json={
                "resource": {
                    "resourceType": "Questionnaire",
                    "status": "active",
                    "title": "更新",
                    "item": [],
                }
            },
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_api_requires_authentication_questionnaires_delete(self, client: AsyncClient):
        """Questionnaire削除APIが認証を要求することを確認"""
        response = await client.delete("/api/v100/questionnaires/1")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_api_requires_authentication_questionnaire_responses_create(
        self, client: AsyncClient
    ):
        """QuestionnaireResponse作成APIが認証を要求することを確認"""
        response = await client.post(
            "/api/v100/questionnaire-responses/",
            json={
                "questionnaire_id": 1,
                "answers": {},
                "subject": "Patient/123",
                "author": "Practitioner/456",
            },
        )
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_api_requires_authentication_questionnaire_responses_get(
        self, client: AsyncClient
    ):
        """QuestionnaireResponse取得APIが認証を要求することを確認"""
        response = await client.get("/api/v100/questionnaire-responses/1")
        assert response.status_code == 401

    @pytest.mark.asyncio
    async def test_api_requires_authentication_questionnaire_responses_list(
        self, client: AsyncClient
    ):
        """QuestionnaireResponse一覧APIが認証を要求することを確認"""
        response = await client.get("/api/v100/questionnaire-responses/")
        assert response.status_code == 401


# ===== ロールベースアクセス制御（RBAC）テスト =====


class TestRoleBasedAccessControl:
    """ロールベースアクセス制御のテスト"""

    @pytest.mark.asyncio
    async def test_collector_cannot_create_questionnaire(
        self, collector_client: AsyncClient, csrf_token: str
    ):
        """collectorロールはQuestionnaire作成できないことを確認"""
        response = await collector_client.post(
            "/api/v100/questionnaires/",
            json={
                "resource": {
                    "resourceType": "Questionnaire",
                    "status": "active",
                    "title": "テスト",
                    "item": [],
                }
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 403
        data = response.json()
        assert "detail" in data

    @pytest.mark.asyncio
    async def test_ai_designer_cannot_create_questionnaire(
        self, designer_client: AsyncClient, csrf_token: str
    ):
        """ai_designerロール（アクティブ役割）はQuestionnaire作成できないことを確認

        ハード・スコープ方式により、Questionnaire 管理は admin のアクティブ役割のみ。
        """
        response = await designer_client.post(
            "/api/v100/questionnaires/",
            json={
                "resource": {
                    "resourceType": "Questionnaire",
                    "status": "active",
                    "title": "テスト",
                    "item": [],
                }
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_admin_can_create_questionnaire(self, admin_client: AsyncClient, csrf_token: str):
        """adminロールはQuestionnaire作成できることを確認"""
        response = await admin_client.post(
            "/api/v100/questionnaires/",
            json={
                "resource": {
                    "resourceType": "Questionnaire",
                    "status": "active",
                    "title": "テスト",
                    "item": [],
                }
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_collector_cannot_delete_questionnaire(
        self, collector_client: AsyncClient, csrf_token: str
    ):
        """collectorロールはQuestionnaire削除できないことを確認"""
        response = await collector_client.delete(
            "/api/v100/questionnaires/1", headers={"X-CSRF-Token": csrf_token}
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_ai_designer_cannot_delete_questionnaire(
        self, designer_client: AsyncClient, csrf_token: str
    ):
        """ai_designerロールはQuestionnaire削除できないことを確認"""
        response = await designer_client.delete(
            "/api/v100/questionnaires/1", headers={"X-CSRF-Token": csrf_token}
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_collector_can_create_questionnaire_response(
        self, client: AsyncClient, admin_user: User, collector_user: User, csrf_token: str
    ):
        """collectorロールはQuestionnaireResponse作成できることを確認

        認証済みクライアントのフィクスチャは同一 client を共有するため、
        役割ごとに JWT クッキーを明示的に切り替える。
        """
        # まずQuestionnaireを作成（admin権限で）
        client.cookies.set(
            settings.jwt_cookie_name, create_access_token(data={"sub": str(admin_user.id)})
        )
        q_response = await client.post(
            "/api/v100/questionnaires/",
            json={
                "resource": {
                    "resourceType": "Questionnaire",
                    "status": "active",
                    "title": "テスト質問票",
                    "item": [{"linkId": "q1", "type": "string", "text": "質問1"}],
                }
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        questionnaire_id = q_response.json()["id"]

        # QuestionnaireResponse作成（collector権限で）
        client.cookies.set(
            settings.jwt_cookie_name, create_access_token(data={"sub": str(collector_user.id)})
        )
        response = await client.post(
            "/api/v100/questionnaire-responses/",
            json={
                "questionnaire_id": questionnaire_id,
                "answers": {"q1": "回答1"},
                "subject": "Patient/123",
                "author": "Practitioner/456",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_collector_cannot_list_questionnaire_responses(
        self, collector_client: AsyncClient
    ):
        """collectorロールはQuestionnaireResponse一覧を取得できないことを確認"""
        response = await collector_client.get("/api/v100/questionnaire-responses/")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_ai_designer_cannot_list_questionnaire_responses(
        self, designer_client: AsyncClient
    ):
        """ai_designerロールはQuestionnaireResponse一覧を取得できないことを確認"""
        response = await designer_client.get("/api/v100/questionnaire-responses/")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_reviewer_can_list_questionnaire_responses(self, reviewer_client: AsyncClient):
        """reviewerロールはQuestionnaireResponse一覧を取得できることを確認"""
        response = await reviewer_client.get("/api/v100/questionnaire-responses/")
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_admin_cannot_list_questionnaire_responses(self, admin_client: AsyncClient):
        """管理者は回答（患者データ）を見ない"""
        response = await admin_client.get("/api/v100/questionnaire-responses/")
        assert response.status_code == 403


# ===== CSRF保護テスト =====


class TestCSRFProtection:
    """CSRF保護のテスト"""

    @pytest.mark.asyncio
    async def test_csrf_token_endpoint_exists(self, client: AsyncClient):
        """CSRFトークン取得エンドポイントが存在することを確認"""
        response = await client.get("/api/v100/csrf-token")
        assert response.status_code == 200
        data = response.json()
        assert "csrf_token" in data
        assert len(data["csrf_token"]) > 0

    @pytest.mark.asyncio
    async def test_csrf_token_endpoint_enables_double_submit(self, admin_client: AsyncClient):
        """csrf-tokenエンドポイントが署名済みクッキーを設定し、外部連携の

        ダブルサブミット（Cookie + X-CSRF-Token ヘッダ）が成立することを確認する。
        以前は raw トークンのみ返しクッキーを設定しておらず、取得したトークンでは
        必ず検証に失敗していた（リグレッションテスト）。
        """
        # 1. エンドポイントでトークンを取得（同時に署名済みクッキーが設定される）
        token_resp = await admin_client.get("/api/v100/csrf-token")
        assert token_resp.status_code == 200
        assert "kawasemi_csrf_token" in token_resp.headers.get("set-cookie", "")
        token = token_resp.json()["csrf_token"]

        # 2. 取得したトークンを X-CSRF-Token に付けた POST が成功する
        response = await admin_client.post(
            "/api/v100/questionnaires/",
            json={
                "resource": {
                    "resourceType": "Questionnaire",
                    "status": "active",
                    "title": "外部連携テスト",
                    "item": [],
                }
            },
            headers={"X-CSRF-Token": token},
        )
        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_post_request_requires_csrf_token(self, admin_client: AsyncClient):
        """POSTリクエストがCSRFトークンを要求することを確認"""
        response = await admin_client.post(
            "/api/v100/questionnaires/",
            json={
                "resource": {
                    "resourceType": "Questionnaire",
                    "status": "active",
                    "title": "テスト",
                    "item": [],
                }
            },
            # CSRFトークンなし
        )
        assert response.status_code == 403
        data = response.json()
        assert "error_code" in data
        assert data["error_code"] == "CSRF_TOKEN_INVALID"

    @pytest.mark.asyncio
    async def test_post_request_with_valid_csrf_token(
        self, admin_client: AsyncClient, csrf_token: str
    ):
        """有効なCSRFトークンがあればPOSTリクエストが成功することを確認"""
        response = await admin_client.post(
            "/api/v100/questionnaires/",
            json={
                "resource": {
                    "resourceType": "Questionnaire",
                    "status": "active",
                    "title": "テスト",
                    "item": [],
                }
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert response.status_code == 201

    @pytest.mark.asyncio
    async def test_put_request_requires_csrf_token(
        self, admin_client: AsyncClient, csrf_token: str
    ):
        """PUTリクエストがCSRFトークンを要求することを確認"""
        # まずQuestionnaireを作成
        create_response = await admin_client.post(
            "/api/v100/questionnaires/",
            json={
                "resource": {
                    "resourceType": "Questionnaire",
                    "status": "active",
                    "title": "テスト",
                    "item": [],
                }
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        questionnaire_id = create_response.json()["id"]

        # CSRFトークンなしで更新試行
        response = await admin_client.put(
            f"/api/v100/questionnaires/{questionnaire_id}",
            json={
                "resource": {
                    "resourceType": "Questionnaire",
                    "status": "active",
                    "title": "更新後",
                    "item": [],
                }
            },
            # CSRFトークンなし
        )
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_delete_request_requires_csrf_token(
        self, admin_client: AsyncClient, csrf_token: str
    ):
        """DELETEリクエストがCSRFトークンを要求することを確認"""
        # まずQuestionnaireを作成
        create_response = await admin_client.post(
            "/api/v100/questionnaires/",
            json={
                "resource": {
                    "resourceType": "Questionnaire",
                    "status": "active",
                    "title": "テスト",
                    "item": [],
                }
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        questionnaire_id = create_response.json()["id"]

        # CSRFトークンなしで削除試行（admin権限で）
        response = await admin_client.delete(
            f"/api/v100/questionnaires/{questionnaire_id}"
            # CSRFトークンなし
        )
        assert response.status_code == 403


# ===== レート制限テスト =====


class TestRateLimiting:
    """レート制限のテスト"""

    @pytest.mark.asyncio
    async def test_login_rate_limiting(self, client: AsyncClient):
        """ログインエンドポイントがレート制限されることを確認（5回/分）

        /login はエラー応答ごとに CSRF トークンをローテーションするため、
        各試行の直前に GET /login で整合する署名済みクッキーと raw トークンを取得する。
        """
        for i in range(6):
            page = await client.get("/login")
            assert page.status_code == 200
            token = extract_form_csrf_token(page.text)

            response = await client.post(
                "/login",
                data={"username": "invalid_user", "password": "invalid_pass", "csrf_token": token},
                headers={"X-CSRF-Token": token},
            )

            if i < 5:
                # 5回目まではエラーレスポンス（認証失敗）だがステータスは200
                assert response.status_code in [200, 303]
            else:
                # 6回目はレート制限でブロック
                assert response.status_code == 429


# ===== エラーハンドリングテスト =====


class TestErrorHandling:
    """エラーハンドリングのテスト"""

    @pytest.mark.asyncio
    async def test_404_error_does_not_expose_details(self, admin_client: AsyncClient):
        """404エラーが内部情報を露出しないことを確認"""
        response = await admin_client.get("/api/v100/questionnaires/99999")
        assert response.status_code == 404
        data = response.json()

        # エラーコードとメッセージは含まれる
        assert "error_code" in data
        assert "message" in data

        # 内部情報（スタックトレース、ファイルパスなど）は含まれない
        json_str = str(data).lower()
        assert "traceback" not in json_str
        assert ".py" not in json_str
        assert "kawasemi/" not in json_str

    @pytest.mark.asyncio
    async def test_validation_error_does_not_expose_details(
        self, admin_client: AsyncClient, csrf_token: str
    ):
        """バリデーションエラーが内部情報を露出しないことを確認"""
        response = await admin_client.post(
            "/api/v100/questionnaires/",
            json={
                "resource": {
                    # 不正なリソース（statusがない）
                    "resourceType": "Questionnaire",
                    "title": "テスト",
                    # statusフィールドがない
                }
            },
            headers={"X-CSRF-Token": csrf_token},
        )

        # バリデーションエラーまたは422
        assert response.status_code in [400, 422]
        data = response.json()

        # エラー情報は含まれる
        assert "error_code" in data or "detail" in data

        # 内部情報は含まれない
        json_str = str(data).lower()
        assert "traceback" not in json_str
        assert ".py" not in json_str if "test" not in json_str else True


# ===== 統合テスト =====


class TestSecurityIntegration:
    """セキュリティ機能の統合テスト"""

    @pytest.mark.asyncio
    async def test_complete_authenticated_workflow(
        self,
        client: AsyncClient,
        admin_user: User,
        collector_user: User,
        reviewer_user: User,
        csrf_token: str,
    ):
        """認証・認可・CSRF保護が統合的に機能することを確認

        認証済みクライアントのフィクスチャは同一の `client` を共有し JWT クッキーを
        上書きし合うため、本テストでは役割ごとに JWT クッキーを明示的に切り替える。
        """

        def act_as(user: User) -> None:
            token = create_access_token(data={"sub": str(user.id)})
            client.cookies.set(settings.jwt_cookie_name, token)

        # 1. adminユーザーがQuestionnaireを作成（CSRF保護あり）
        act_as(admin_user)
        create_response = await client.post(
            "/api/v100/questionnaires/",
            json={
                "resource": {
                    "resourceType": "Questionnaire",
                    "status": "active",
                    "title": "統合テスト質問票",
                    "item": [{"linkId": "q1", "type": "string", "text": "質問1"}],
                }
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert create_response.status_code == 201
        questionnaire_id = create_response.json()["id"]

        # 2. collectorユーザーがQuestionnaireを取得（認証のみ）
        act_as(collector_user)
        get_response = await client.get(f"/api/v100/questionnaires/{questionnaire_id}")
        assert get_response.status_code == 200

        # 3. collectorユーザーがQuestionnaireResponseを作成（認証・CSRF保護あり）
        response_create = await client.post(
            "/api/v100/questionnaire-responses/",
            json={
                "questionnaire_id": questionnaire_id,
                "answers": {"q1": "統合テスト回答"},
                "subject": "Patient/999",
                "author": "Practitioner/888",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert response_create.status_code == 201

        # 4. collectorユーザーはQuestionnaireResponse一覧を取得できない（権限不足）
        list_response = await client.get("/api/v100/questionnaire-responses/")
        assert list_response.status_code == 403

        # 5. reviewerユーザーはQuestionnaireResponse一覧を取得できる（admin は見ない）
        act_as(admin_user)
        assert (await client.get("/api/v100/questionnaire-responses/")).status_code == 403
        act_as(reviewer_user)
        list_response_ok = await client.get("/api/v100/questionnaire-responses/")
        assert list_response_ok.status_code == 200
        data = list_response_ok.json()
        assert len(data) > 0


# ===== Web ルート認可テスト（Stage1: fix/web-authz の回帰防止） =====


class TestWebRouteAuthorization:
    """web/routes.py の HTML/AJAX ルートに対する認証・認可の回帰テスト。

    過去、Questionnaire/QuestionnaireResponse 管理系の状態変更・PHI 閲覧ルートに
    認証・認可が適用されておらず、未認証で患者回答の閲覧・改ざん・削除・偽投入が
    可能だった。本テスト群はその穴が再発しないことを保証する。
    """

    # --- 未認証アクセスは 401 ---

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "method,path",
        [
            ("get", "/questionnaires/1"),
            ("get", "/questionnaires/1/edit"),
            ("post", "/questionnaires/new"),
            ("post", "/questionnaires/1/edit"),
            ("post", "/questionnaires/1/delete"),
            ("get", "/valuesets"),
            ("get", "/valuesets/1"),
            ("get", "/valuesets/1/edit"),
            ("post", "/valuesets/new"),
            ("post", "/valuesets/1/edit"),
            ("post", "/valuesets/1/delete"),
            ("get", "/questionnaires/1/responses"),
            ("get", "/responses/1"),
            ("get", "/responses/1/edit"),
            ("post", "/responses/1/edit"),
            ("post", "/forms/1/submit"),
            ("get", "/api/patient/DEMO-9009"),
            ("get", "/api/encounter/enc-1"),
        ],
    )
    async def test_unauthenticated_web_routes_rejected(
        self, client: AsyncClient, method: str, path: str
    ):
        """未認証では管理系・PHI 系ルートにアクセスできない（401）"""
        response = await getattr(client, method)(path)
        assert response.status_code == 401, (
            f"{method.upper()} {path} が未認証で 401 を返していません: {response.status_code}"
        )

    # --- collector は admin 管理系ルートにアクセスできない（403） ---

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "/questionnaires/1",
            "/questionnaires/1/edit",
            "/questionnaires/1/responses",
            "/responses/1",
            # ValueSet 管理は admin 専用
            "/valuesets",
            "/valuesets/1",
            "/valuesets/1/edit",
            # 注: /responses/1/edit は collector に開放している（差戻し後の再編集）。
            # 検証済の施錠や差戻し再編集の挙動は tests/test_verification_workflow.py で検証。
        ],
    )
    async def test_collector_cannot_access_admin_web_routes(
        self, collector_client: AsyncClient, path: str
    ):
        """collector のアクティブ役割では Questionnaire/Response 管理系は 403"""
        response = await collector_client.get(path)
        assert response.status_code == 403, (
            f"GET {path} が collector に対し 403 を返していません: {response.status_code}"
        )

    # --- /api/patient の IDOR: 割り当てのない患者は 403 ---

    @pytest.mark.asyncio
    async def test_api_patient_idor_blocked_for_unassigned(self, collector_client: AsyncClient):
        """有効なリースを持たない collector は任意患者IDで 403（FHIR 到達前に遮断）"""
        response = await collector_client.get("/api/patient/DEMO-9009")
        assert response.status_code == 403
        body = response.json()
        assert body["success"] is False

    @pytest.mark.asyncio
    async def test_show_form_page_idor_blocked_for_unassigned(
        self, collector_client: AsyncClient, db_session: AsyncSession
    ):
        """フォーム表示ページ GET /forms/{id}?patient_id=... の IDOR。

        割り当て/入力対象を持たない collector が任意の patient_id を付けても、
        担当外患者の PHI が描画される前に 403 で遮断される（FHIR 到達前）。
        AJAX 版（/api/patient）だけでなくページ GET も同じ関門を通ることを固定する。
        """
        from kawasemi.models.questionnaire import Questionnaire

        q = Questionnaire(
            fhir_id="q-idor-page-test",
            content={"resourceType": "Questionnaire", "status": "active", "item": []},
            status="active",
        )
        db_session.add(q)
        await db_session.commit()
        await db_session.refresh(q)

        response = await collector_client.get(f"/forms/{q.id}?patient_id=DEMO-9009")
        assert response.status_code == 403, (
            f"未割り当て患者の /forms/{q.id} が 403 を返していません: {response.status_code}"
        )

    @pytest.mark.asyncio
    async def test_show_form_enable_when_config_is_not_stored_xss(
        self, collector_client: AsyncClient, db_session: AsyncSession
    ):
        """enableWhen の answerString 経由の格納型 XSS。

        `form.html` は `enable_when_config` を `<script>` 内へ埋め込む。管理者/インポート
        入力である Questionnaire.content の enableWhen answerString にデータフェンスを
        破る文字列を仕込んでも、`| tojson` で `<`/`>` がエスケープされ、生の
        `</script><script>` としては出力されないことを固定する。
        """
        from kawasemi.models.questionnaire import Questionnaire

        payload = "</script><script>alert(1)</script>"
        q = Questionnaire(
            fhir_id="q-xss-enable-when-test",
            content={
                "resourceType": "Questionnaire",
                "status": "active",
                "item": [
                    {"linkId": "q1", "type": "string", "text": "質問1"},
                    {
                        "linkId": "q2",
                        "type": "string",
                        "text": "質問2",
                        "enableWhen": [
                            {"question": "q1", "operator": "=", "answerString": payload}
                        ],
                    },
                ],
            },
            status="active",
        )
        db_session.add(q)
        await db_session.commit()
        await db_session.refresh(q)

        response = await collector_client.get(f"/forms/{q.id}")
        assert response.status_code == 200
        assert "</script><script>alert(1)</script>" not in response.text

    @pytest.mark.asyncio
    async def test_update_response_idor_blocked_for_non_owner(
        self, collector_client: AsyncClient, db_session: AsyncSession
    ):
        """IDOR 防止: 自分の提出でも担当患者でもない回答は collector が
        POST /responses/{id}/edit で編集できない（403）。response_id 総当たり改ざんの遮断。"""
        from kawasemi.models.questionnaire import Questionnaire
        from kawasemi.models.questionnaire_response import QuestionnaireResponse

        q = Questionnaire(
            fhir_id="q-idor-edit",
            content={"resourceType": "Questionnaire", "status": "active", "item": []},
            status="active",
        )
        db_session.add(q)
        await db_session.flush()
        resp_obj = QuestionnaireResponse(
            fhir_id="qr-idor-edit",
            questionnaire_id=q.id,
            content={"resourceType": "QuestionnaireResponse", "status": "in-progress"},
            status="in-progress",
            subject="Patient/DEMO-9009",  # collector に未割り当ての患者
            submitted_by_id=None,  # 自分の提出ではない
        )
        db_session.add(resp_obj)
        await db_session.commit()
        await db_session.refresh(resp_obj)

        response = await collector_client.post(f"/responses/{resp_obj.id}/edit")
        assert response.status_code == 403, (
            f"他者担当の回答編集が 403 を返していません: {response.status_code}"
        )

    @pytest.mark.asyncio
    async def test_edit_response_form_idor_blocked_for_non_owner(
        self, collector_client: AsyncClient, db_session: AsyncSession
    ):
        """IDOR 防止（読み取り側）: 担当外患者の回答は編集フォーム GET
        /responses/{id}/edit でも 403（プリフィル済み回答値=PHI を総当たり閲覧させない）。"""
        from kawasemi.models.questionnaire import Questionnaire
        from kawasemi.models.questionnaire_response import QuestionnaireResponse

        q = Questionnaire(
            fhir_id="q-idor-edit-get",
            content={"resourceType": "Questionnaire", "status": "active", "item": []},
            status="active",
        )
        db_session.add(q)
        await db_session.flush()
        resp_obj = QuestionnaireResponse(
            fhir_id="qr-idor-edit-get",
            questionnaire_id=q.id,
            content={"resourceType": "QuestionnaireResponse", "status": "in-progress"},
            status="in-progress",
            subject="Patient/DEMO-9009",
            submitted_by_id=None,
        )
        db_session.add(resp_obj)
        await db_session.commit()
        await db_session.refresh(resp_obj)

        response = await collector_client.get(f"/responses/{resp_obj.id}/edit")
        assert response.status_code == 403, (
            f"他者担当の回答編集フォームが 403 を返していません: {response.status_code}"
        )

    @pytest.mark.asyncio
    async def test_web_form_post_without_csrf_token_rejected(self, admin_client: AsyncClient):
        """CSRF 防御: Web フォーム POST は X-CSRF-Token 無しだと拒否される。

        admin_client は csrf_token フィクスチャを使わないため CSRF cookie/header を持たない。
        validate_csrf が最初に走り、フォーム処理へ到達する前に拒否される。
        """
        response = await admin_client.post(
            "/questionnaires/new",
            data={"resource_json": "{}"},
        )
        assert response.status_code == 403, (
            f"CSRF トークン無しの POST が拒否されていません: {response.status_code}"
        )


class TestOpenRedirect:
    """ログイン後リダイレクトのオープンリダイレクト防止（Stage1）"""

    def test_safe_redirect_path_rejects_external(self):
        from kawasemi.web.auth_routes import _safe_redirect_path

        # 外部・プロトコル相対・バックスラッシュは "/" にフォールバック
        assert _safe_redirect_path("//evil.com") == "/"
        assert _safe_redirect_path("https://evil.com") == "/"
        assert _safe_redirect_path("/\\evil.com") == "/"
        assert _safe_redirect_path("evil.com") == "/"
        assert _safe_redirect_path(None) == "/"
        assert _safe_redirect_path("") == "/"
        # 自サイト内の相対パスはそのまま許可
        assert _safe_redirect_path("/questionnaires") == "/questionnaires"
        assert _safe_redirect_path("/ai-input/dashboard") == "/ai-input/dashboard"
