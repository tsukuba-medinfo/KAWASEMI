"""FHIRClient（FHIRサーバ通信クライアント）のユニットテスト

`tests/test_fhir_integration.py` が各種 Extractor と自動補完のシナリオを検証するのに対し、
本モジュールは `FHIRClient` 本体の HTTP 通信ロジックを検証する:

- 各リソース取得・検索メソッドのURL/クエリパラメータ組み立て（偽トランスポート経由）
- `_get_resource` のエラーハンドリング（404 / タイムアウト / HTTPステータス / 接続エラー）
- パフォーマンス（同時アクセス）
"""

import asyncio

import httpx
import pytest

import kawasemi.utils.fhir_client as fhir_client_mod
from kawasemi.core.exceptions import FHIRServerException
from kawasemi.utils.fhir_client import FHIRClient, PatientDataExtractor
from tests.fhir_resources import fhir_mock_transport


def _redirect_httpx_to_mock(monkeypatch):
    """fhir_client モジュールの httpx.AsyncClient を偽トランスポートへ向ける。

    FHIRClient は絶対URLでリクエストするが、MockTransport はホストを見ずに
    ハンドラへ渡すため、base_url は任意の値でよい。client_on_mock フィクスチャと
    便利関数テストの双方から使う共通ヘルパ。
    """
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", fhir_mock_transport())
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(fhir_client_mod.httpx, "AsyncClient", factory)


@pytest.fixture
def client_on_mock(monkeypatch):
    """内部 httpx.AsyncClient を偽トランスポートへ向けた FHIRClient"""
    _redirect_httpx_to_mock(monkeypatch)
    return FHIRClient(base_url="http://mock-fhir-server")


# --- エラーハンドリング検証用の偽 httpx クライアント ---------------------------


class _FakeResponse:
    """status_code / json / raise_for_status のみを持つ最小レスポンス"""

    def __init__(self, status_code: int, json_data: dict | None = None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self) -> dict:
        return self._json

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "http://fake-fhir/resource")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(
                f"status {self.status_code}", request=request, response=response
            )


def _fake_client_factory(*, response=None, raises=None, record=None, url_record=None):
    """get() が指定の例外を送出 or 指定レスポンスを返す偽 AsyncClient を生成。

    record（list）を渡すと、AsyncClient(...) 生成時の kwargs を記録する。
    timeout など実装が httpx へ渡す引数の伝播検証に使う。
    url_record（list）を渡すと、get() に渡された url を記録する。
    パスセグメントの組み立て（URLエンコード）の検証に使う。
    """

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            if record is not None:
                record.append(kwargs)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc_info):
            return False

        async def get(self, url, params=None, headers=None):
            if url_record is not None:
                url_record.append(url)
            if raises is not None:
                raise raises
            return response

    return _FakeAsyncClient


@pytest.fixture
def patch_async_client(monkeypatch):
    """fhir_client モジュールの httpx.AsyncClient を差し替えるヘルパ"""

    def _apply(*, response=None, raises=None, record=None, url_record=None):
        monkeypatch.setattr(
            fhir_client_mod.httpx,
            "AsyncClient",
            _fake_client_factory(
                response=response, raises=raises, record=record, url_record=url_record
            ),
        )

    return _apply


# --- FHIRClient: リソース取得・検索（偽トランスポート経由） ---------------------


class TestFHIRClientResourceFetch:
    """FHIRClient の各メソッドが偽の FHIR サーバから正しく取得・検索できること"""

    async def test_get_patient(self, client_on_mock: FHIRClient):
        patient = await client_on_mock.get_patient("patient-001")
        assert patient["resourceType"] == "Patient"
        assert patient["id"] == "patient-001"
        assert patient["gender"] == "male"

    async def test_get_patient_not_found_raises(self, client_on_mock: FHIRClient):
        # httpx のスタックを通して _get_resource の明示404分岐を検証する。
        # 偽クライアント版（TestFHIRClientErrorHandling.test_404_raises_not_found）と
        # 同じ分岐だが、こちらは Response の組み立て込みで例外型が保たれることを保証する。
        with pytest.raises(FHIRServerException) as exc_info:
            await client_on_mock.get_patient("nonexistent")
        assert exc_info.value.details["status_code"] == 404

    async def test_search_patients_all(self, client_on_mock: FHIRClient):
        bundle = await client_on_mock.search_patients()
        assert bundle["resourceType"] == "Bundle"
        assert bundle["total"] == 3

    async def test_search_patients_by_gender(self, client_on_mock: FHIRClient):
        bundle = await client_on_mock.search_patients(gender="male")
        assert bundle["total"] == 1
        assert bundle["entry"][0]["resource"]["gender"] == "male"

    async def test_search_patients_by_identifier(self, client_on_mock: FHIRClient):
        bundle = await client_on_mock.search_patients(identifier="0000000001")
        assert bundle["total"] == 1
        assert bundle["entry"][0]["resource"]["id"] == "patient-001"

    async def test_get_encounter(self, client_on_mock: FHIRClient):
        encounter = await client_on_mock.get_encounter("encounter-001")
        assert encounter["resourceType"] == "Encounter"
        assert encounter["id"] == "encounter-001"


# --- FHIRClient: 初期化・設定 ------------------------------------------------


class TestFHIRClientInit:
    """初期化時の base_url 正規化と timeout 設定"""

    def test_base_url_trailing_slash_stripped(self):
        client = FHIRClient(base_url="http://example.org/fhir/")
        assert client.base_url == "http://example.org/fhir"

    def test_custom_timeout(self):
        client = FHIRClient(base_url="http://example.org/fhir", timeout=5)
        assert client.timeout == 5

    def test_defaults_from_settings(self):
        client = FHIRClient()
        assert client.base_url  # 設定値が入る
        assert client.timeout > 0

    async def test_timeout_propagated_to_httpx_client(self, patch_async_client):
        """client.timeout が httpx.AsyncClient(timeout=...) へ実際に渡ることを検証。

        属性値の確認（test_custom_timeout）だけでは、その値が httpx に渡る経路の
        リグレッションを検出できないため、生成時 kwargs を記録して確認する。
        """
        calls: list[dict] = []
        patch_async_client(
            response=_FakeResponse(200, {"resourceType": "Patient", "id": "p"}),
            record=calls,
        )
        client = FHIRClient(base_url="http://fake-fhir", timeout=7)
        await client.get_patient("p")
        assert calls, "httpx.AsyncClient が生成されていない"
        assert calls[0].get("timeout") == 7


class TestPatientDataExtractorFallback:
    """test_fhir_integration では未カバーの extract_name フォールバック分岐"""

    def test_extract_name_without_official_use(self):
        """official use が無い場合は先頭の name を使用する"""
        patient = {"name": [{"use": "nickname", "text": "たろちゃん"}]}
        assert PatientDataExtractor.extract_name(patient) == "たろちゃん"

    def test_extract_name_empty(self):
        """name が無い場合は None"""
        assert PatientDataExtractor.extract_name({}) is None


# --- FHIRClient: エラーハンドリング ------------------------------------------


class TestFHIRClientErrorHandling:
    """_get_resource の各例外パスが FHIRServerException に変換されること"""

    async def test_404_raises_not_found(self, patch_async_client):
        patch_async_client(response=_FakeResponse(404))
        client = FHIRClient(base_url="http://fake-fhir")
        with pytest.raises(FHIRServerException) as exc_info:
            await client.get_patient("missing")
        assert exc_info.value.details["status_code"] == 404
        assert "見つかりません" in exc_info.value.message

    async def test_timeout_raises(self, patch_async_client):
        patch_async_client(raises=httpx.TimeoutException("timed out"))
        client = FHIRClient(base_url="http://fake-fhir")
        with pytest.raises(FHIRServerException) as exc_info:
            await client.get_patient("patient-001")
        assert "タイムアウト" in exc_info.value.message

    async def test_http_status_error_raises(self, patch_async_client):
        patch_async_client(response=_FakeResponse(500))
        client = FHIRClient(base_url="http://fake-fhir")
        with pytest.raises(FHIRServerException) as exc_info:
            await client.get_patient("patient-001")
        assert exc_info.value.details["status_code"] == 500

    async def test_connection_error_raises(self, patch_async_client):
        patch_async_client(raises=httpx.ConnectError("connection refused"))
        client = FHIRClient(base_url="http://fake-fhir")
        with pytest.raises(FHIRServerException) as exc_info:
            await client.get_patient("patient-001")
        assert "接続に失敗" in exc_info.value.message

    async def test_401_raises_auth_error(self, patch_async_client):
        """401 は認証・認可失敗の専用メッセージになる。"""
        patch_async_client(response=_FakeResponse(401))
        client = FHIRClient(base_url="http://fake-fhir")
        with pytest.raises(FHIRServerException) as exc_info:
            await client.get_patient("patient-001")
        assert exc_info.value.details["status_code"] == 401
        assert "認証・認可に失敗" in exc_info.value.message

    async def test_403_raises_auth_error(self, patch_async_client):
        """403 も認証・認可失敗として扱う。"""
        patch_async_client(response=_FakeResponse(403))
        client = FHIRClient(base_url="http://fake-fhir")
        with pytest.raises(FHIRServerException) as exc_info:
            await client.get_patient("patient-001")
        assert exc_info.value.details["status_code"] == 403
        assert "認証・認可に失敗" in exc_info.value.message


# --- FHIRClient: パスセグメントのURLエンコード -----------------


class TestFHIRClientPathEncoding:
    """リソース読み取りヘルパがパスセグメントをURLエンコードすること。

    executor.py の fhir_extract は先に対応済みだったが、
    FHIRClient のリソース読み取りヘルパ（get_patient/get_encounter）は
    未対応だった。デコード後のパスパラメータに `?` `#` `;` 等が含まれると、
    そのままf-string補間した実装ではバックエンドへのクエリ注入が成立しうる。
    """

    async def test_get_patient_encodes_query_injection_attempt(self, patch_async_client):
        urls: list[str] = []
        patch_async_client(
            response=_FakeResponse(200, {"resourceType": "Patient", "id": "p"}),
            url_record=urls,
        )
        client = FHIRClient(base_url="http://fake-fhir")
        await client.get_patient("abc?_count=1000")
        assert urls == ["http://fake-fhir/Patient/abc%3F_count%3D1000"]

    async def test_get_encounter_encodes_query_injection_attempt(self, patch_async_client):
        urls: list[str] = []
        patch_async_client(
            response=_FakeResponse(200, {"resourceType": "Encounter", "id": "e"}),
            url_record=urls,
        )
        client = FHIRClient(base_url="http://fake-fhir")
        await client.get_encounter("abc?_count=1000")
        assert urls == ["http://fake-fhir/Encounter/abc%3F_count%3D1000"]

    async def test_get_patient_ordinary_id_unchanged(self, patch_async_client):
        """通常のFHIR id（英数字・ハイフン）はエンコード後も見た目が変わらない。"""
        urls: list[str] = []
        patch_async_client(
            response=_FakeResponse(200, {"resourceType": "Patient", "id": "patient-001"}),
            url_record=urls,
        )
        client = FHIRClient(base_url="http://fake-fhir")
        await client.get_patient("patient-001")
        assert urls == ["http://fake-fhir/Patient/patient-001"]


class TestFHIRClientAuth:
    """FHIRサーバ認証（Bearer / Basic）のヘッダ付与"""

    def test_auth_header_none_by_default(self):
        """既定（auth_mode=none）では Authorization を付けない。"""
        client = FHIRClient(base_url="http://fake-fhir", auth_mode="none")
        assert client._build_auth_header() is None
        assert "Authorization" not in client._request_headers()

    def test_bearer_header(self):
        client = FHIRClient(base_url="http://fake-fhir", auth_mode="bearer", bearer_token="tok-123")
        assert client._request_headers()["Authorization"] == "Bearer tok-123"

    def test_basic_header(self):
        import base64

        client = FHIRClient(
            base_url="http://fake-fhir",
            auth_mode="basic",
            basic_username="user",
            basic_password="pass",
        )
        expected = "Basic " + base64.b64encode(b"user:pass").decode()
        assert client._request_headers()["Authorization"] == expected

    def test_basic_header_non_ascii(self):
        """非ASCII資格情報は UTF-8 バイト列として base64 化される（RFC 7617 準拠）。"""
        import base64

        client = FHIRClient(
            base_url="http://fake-fhir",
            auth_mode="basic",
            basic_username="ユーザ",
            basic_password="秘密",
        )
        expected = "Basic " + base64.b64encode("ユーザ:秘密".encode()).decode("ascii")
        assert client._request_headers()["Authorization"] == expected

    def test_bearer_missing_token_fails_fast(self):
        """bearer 指定でトークン未設定は fail-fast（黙って無認証送信しない）。"""
        with pytest.raises(ValueError, match="fhir_bearer_token"):
            FHIRClient(base_url="http://fake-fhir", auth_mode="bearer", bearer_token="")

    def test_basic_missing_username_fails_fast(self):
        with pytest.raises(ValueError, match="fhir_basic_username"):
            FHIRClient(base_url="http://fake-fhir", auth_mode="basic", basic_username="")

    def test_unknown_mode_fails_fast(self):
        with pytest.raises(ValueError, match="未知の fhir_auth_mode"):
            FHIRClient(base_url="http://fake-fhir", auth_mode="oauth2")

    def test_default_auth_from_settings(self, monkeypatch):
        """無引数 FHIRClient() は settings.fhir_auth_mode 等から認証を組み立てる。"""
        from kawasemi.core.config import settings

        monkeypatch.setattr(settings, "fhir_auth_mode", "bearer")
        monkeypatch.setattr(settings, "fhir_bearer_token", "from-settings")
        client = FHIRClient(base_url="http://fake-fhir")
        assert client._request_headers()["Authorization"] == "Bearer from-settings"

    async def test_auth_header_sent_on_request(self, monkeypatch):
        """実リクエストの GET に Authorization ヘッダが載ること（Bearer / Basic 両方）。"""
        import base64

        captured: dict = {}

        class _RecordingClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc):
                return False

            async def get(self, url, params=None, headers=None):
                captured["headers"] = headers
                return _FakeResponse(200, {"resourceType": "Patient", "id": "p1"})

        monkeypatch.setattr(fhir_client_mod.httpx, "AsyncClient", _RecordingClient)

        bearer = FHIRClient(base_url="http://fake-fhir", auth_mode="bearer", bearer_token="tok-xyz")
        await bearer.get_patient("p1")
        assert captured["headers"]["Authorization"] == "Bearer tok-xyz"
        assert captured["headers"]["Accept"] == "application/fhir+json"

        basic = FHIRClient(
            base_url="http://fake-fhir",
            auth_mode="basic",
            basic_username="u",
            basic_password="p",
        )
        await basic.get_patient("p1")
        expected = "Basic " + base64.b64encode(b"u:p").decode()
        assert captured["headers"]["Authorization"] == expected

    async def test_secret_not_logged(self, monkeypatch, caplog):
        """401 エラー時のログ・例外にトークン値が漏れない（redaction）。"""
        import logging

        monkeypatch.setattr(
            fhir_client_mod.httpx, "AsyncClient", _fake_client_factory(response=_FakeResponse(401))
        )
        client = FHIRClient(
            base_url="http://fake-fhir", auth_mode="bearer", bearer_token="super-secret-token"
        )
        with caplog.at_level(logging.DEBUG):
            with pytest.raises(FHIRServerException) as exc_info:
                await client.get_patient("p1")
        assert "super-secret-token" not in caplog.text
        assert "super-secret-token" not in str(exc_info.value.details)
        assert "super-secret-token" not in exc_info.value.message


# --- パフォーマンス（同時アクセス） ------------------------------------------


class TestPerformance:
    """同時アクセスに対する基本的な健全性チェック（決定論的）"""

    async def test_concurrent_patient_fetch(self, client_on_mock: FHIRClient):
        """50件の同時取得がすべて成功する"""
        patient_ids = ["patient-001", "patient-002", "patient-003"] * 17  # 51件
        results = await asyncio.gather(*(client_on_mock.get_patient(pid) for pid in patient_ids))
        assert len(results) == len(patient_ids)
        assert all(r["resourceType"] == "Patient" for r in results)

    async def test_concurrent_mixed_resources(self, client_on_mock: FHIRClient):
        """Patient / Encounter の混在同時アクセス"""
        results = await asyncio.gather(
            client_on_mock.get_patient("patient-001"),
            client_on_mock.get_encounter("encounter-001"),
        )
        types = {r["resourceType"] for r in results}
        assert types == {"Patient", "Encounter"}
