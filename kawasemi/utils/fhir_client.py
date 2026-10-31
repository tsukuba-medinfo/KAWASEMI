"""FHIRサーバクライアント"""

import base64
import urllib.parse
from typing import Any, cast

import httpx

from kawasemi.core.config import settings
from kawasemi.core.exceptions import FHIRServerException
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)


class FHIRClient:
    """FHIRサーバへの接続クライアント"""

    def __init__(
        self,
        base_url: str | None = None,
        timeout: int | None = None,
        *,
        auth_mode: str | None = None,
        bearer_token: str | None = None,
        basic_username: str | None = None,
        basic_password: str | None = None,
    ):
        """
        FHIRClientの初期化

        Args:
            base_url: FHIRサーバのベースURL（省略時は設定値を使用）
            timeout: タイムアウト秒数（省略時は設定値を使用）
            auth_mode: 認証方式 none/bearer/basic（省略時は settings.fhir_auth_mode）
            bearer_token: Bearer トークン（省略時は settings.fhir_bearer_token）
            basic_username: Basic 認証ユーザー名（省略時は settings.fhir_basic_username）
            basic_password: Basic 認証パスワード（省略時は settings.fhir_basic_password）

        認証情報は既定で settings（= .env）から取得するため、`FHIRClient()` の
        無引数生成でも認証が有効になる。認証値はログに出力しない。
        """
        self.base_url = (base_url or settings.fhir_server_url).rstrip("/")
        self.timeout = timeout or settings.fhir_server_timeout

        self._auth_mode = auth_mode if auth_mode is not None else settings.fhir_auth_mode
        self._bearer_token = (
            bearer_token if bearer_token is not None else settings.fhir_bearer_token
        )
        self._basic_username = (
            basic_username if basic_username is not None else settings.fhir_basic_username
        )
        self._basic_password = (
            basic_password if basic_password is not None else settings.fhir_basic_password
        )
        # 認証ヘッダは一度だけ構築（値の検証・警告もここで済ませる）
        self._auth_header = self._build_auth_header()

    def _build_auth_header(self) -> str | None:
        """認証方式に応じた Authorization ヘッダ値を返す（none なら None）。

        認証を有効化（bearer/basic）したのに資格情報が欠けている場合は、黙って
        無認証で続行せず fail-fast で例外にする。医療データ経路で「認証したつもりが
        無認証送信」という設定事故を防ぐため。トークン等の秘匿値
        そのものは例外・ログに含めない。
        """
        mode = (self._auth_mode or "none").lower()
        if mode == "none":
            return None
        if mode == "bearer":
            if not self._bearer_token:
                raise ValueError(
                    "fhir_auth_mode=bearer ですが fhir_bearer_token が未設定です。"
                    "認証情報を設定するか fhir_auth_mode=none にしてください。"
                )
            return f"Bearer {self._bearer_token}"
        if mode == "basic":
            if not self._basic_username:
                raise ValueError(
                    "fhir_auth_mode=basic ですが fhir_basic_username が未設定です。"
                    "認証情報を設定するか fhir_auth_mode=none にしてください。"
                )
            raw = f"{self._basic_username}:{self._basic_password}".encode()
            return "Basic " + base64.b64encode(raw).decode("ascii")
        raise ValueError(
            f"未知の fhir_auth_mode='{self._auth_mode}' です（none/bearer/basic のいずれか）。"
        )

    def _request_headers(self) -> dict[str, str]:
        """FHIR リクエストの共通ヘッダ（Accept ＋ 任意で Authorization）。"""
        headers = {"Accept": "application/fhir+json"}
        if self._auth_header:
            headers["Authorization"] = self._auth_header
        return headers

    async def get_patient(self, patient_id: str) -> dict[str, Any]:
        """
        Patient リソースを取得

        Args:
            patient_id: 患者ID

        Returns:
            Patient リソース（dict形式）

        Raises:
            FHIRServerException: FHIRサーバエラー
        """
        url = f"{self.base_url}/Patient/{urllib.parse.quote(patient_id, safe='')}"
        return await self._get_resource(url)

    async def search_patients(
        self, identifier: str | None = None, gender: str | None = None
    ) -> dict[str, Any]:
        """
        Patient リソースを検索

        Args:
            identifier: 患者識別子
            gender: 性別

        Returns:
            Bundle リソース（検索結果）
        """
        params: dict[str, str] = {}
        if identifier:
            params["identifier"] = identifier
        if gender:
            params["gender"] = gender

        url = f"{self.base_url}/Patient"
        return await self._get_resource(url, params=params)

    async def get_encounter(self, encounter_id: str) -> dict[str, Any]:
        """
        Encounter リソースを取得

        Args:
            encounter_id: Encounter ID

        Returns:
            Encounter リソース（dict形式）
        """
        url = f"{self.base_url}/Encounter/{urllib.parse.quote(encounter_id, safe='')}"
        return await self._get_resource(url)

    async def _get_resource(self, url: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        """
        FHIRリソースをGET

        Args:
            url: リクエストURL
            params: クエリパラメータ

        Returns:
            FHIRリソース（dict形式）

        Raises:
            FHIRServerException: FHIRサーバエラー
        """
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.get(
                    url,
                    params=params,
                    headers=self._request_headers(),
                )

                if response.status_code == 404:
                    raise FHIRServerException(
                        message="リソースが見つかりません",
                        details={"url": url, "status_code": 404},
                    )

                # 認証・認可失敗。原因切り分けのため専用メッセージにする。
                if response.status_code in (401, 403):
                    logger.error(
                        f"FHIRサーバ認証エラー: {response.status_code} "
                        f"(fhir_auth_mode={self._auth_mode}) - {url}"
                    )
                    raise FHIRServerException(
                        message=(
                            f"FHIRサーバの認証・認可に失敗しました（ステータス: "
                            f"{response.status_code}）。fhir_auth_mode と認証情報を確認してください。"
                        ),
                        details={"url": url, "status_code": response.status_code},
                    )

                response.raise_for_status()
                # 不正JSON対応: 2xx でもボディが壊れている場合の
                # JSONDecodeError(ValueError) を FHIRServerException に整形する。
                try:
                    data = response.json()
                except ValueError as e:
                    logger.error(f"FHIRサーバ応答のJSONパースに失敗しました: {url}")
                    raise FHIRServerException(
                        message="FHIRサーバ応答のJSONパースに失敗しました",
                        details={"url": url, "error": str(e)},
                    )
                return cast(dict[str, Any], data)

        except httpx.TimeoutException as e:
            logger.error(f"FHIRサーバへの接続がタイムアウトしました: {url}")
            raise FHIRServerException(
                message="FHIRサーバへの接続がタイムアウトしました",
                details={"url": url, "error": str(e)},
            )
        except httpx.HTTPStatusError as e:
            logger.error(f"FHIRサーバエラー: {e.response.status_code} - {url}")
            raise FHIRServerException(
                message=f"FHIRサーバエラー（ステータス: {e.response.status_code}）",
                details={"url": url, "status_code": e.response.status_code},
            )
        except httpx.RequestError as e:
            logger.error(f"FHIRサーバへの接続に失敗しました: {url} - {e}")
            raise FHIRServerException(
                message="FHIRサーバへの接続に失敗しました",
                details={"url": url, "error": str(e)},
            )


class PatientDataExtractor:
    """Patient リソースからデータを抽出するクラス"""

    @staticmethod
    def extract_gender(patient: dict[str, Any]) -> str | None:
        """
        Patient から性別を抽出

        Args:
            patient: Patient リソース

        Returns:
            性別（male, female, other, unknown）またはNone
        """
        return patient.get("gender")

    @staticmethod
    def extract_gender_display(patient: dict[str, Any]) -> str | None:
        """
        Patient から性別の表示用文字列を抽出

        Args:
            patient: Patient リソース

        Returns:
            性別の日本語表示
        """
        gender = patient.get("gender")
        gender_map = {
            "male": "男性",
            "female": "女性",
            "other": "その他",
            "unknown": "不明",
        }
        return gender_map.get(gender) if gender else None

    @staticmethod
    def extract_birth_date(patient: dict[str, Any]) -> str | None:
        """
        Patient から生年月日を抽出

        Args:
            patient: Patient リソース

        Returns:
            生年月日（YYYY-MM-DD形式）
        """
        return patient.get("birthDate")

    @staticmethod
    def extract_name(patient: dict[str, Any]) -> str | None:
        """
        Patient から氏名を抽出

        Args:
            patient: Patient リソース

        Returns:
            氏名（text形式）
        """
        names = patient.get("name", [])
        if names:
            # official use を優先
            for name in names:
                if name.get("use") == "official":
                    return cast(str | None, name.get("text"))
            # なければ最初のものを使用
            return cast(str | None, names[0].get("text"))
        return None

    @staticmethod
    def extract_all(patient: dict[str, Any]) -> dict[str, Any]:
        """
        Patient から全ての関連データを抽出

        Args:
            patient: Patient リソース

        Returns:
            抽出されたデータのdict
        """
        return {
            "patient_id": patient.get("id"),
            "gender": PatientDataExtractor.extract_gender(patient),
            "gender_display": PatientDataExtractor.extract_gender_display(patient),
            "birth_date": PatientDataExtractor.extract_birth_date(patient),
            "name": PatientDataExtractor.extract_name(patient),
        }


class EncounterDataExtractor:
    """Encounter リソースからデータを抽出するクラス"""

    @staticmethod
    def extract_status(encounter: dict[str, Any]) -> str | None:
        """Encounter からステータスを抽出"""
        return encounter.get("status")

    @staticmethod
    def extract_class_display(encounter: dict[str, Any]) -> str | None:
        """Encounter からクラス（外来/入院など）の表示名を抽出"""
        enc_class = encounter.get("class", {})
        return cast(str | None, enc_class.get("display"))

    @staticmethod
    def extract_period(encounter: dict[str, Any]) -> dict[str, str | None]:
        """Encounter から期間を抽出"""
        period = encounter.get("period", {})
        return {
            "start": period.get("start"),
            "end": period.get("end"),
        }

    @staticmethod
    def extract_service_provider(encounter: dict[str, Any]) -> str | None:
        """Encounter からサービス提供者（医療機関）を抽出"""
        provider = encounter.get("serviceProvider", {})
        return cast(str | None, provider.get("display"))

    @staticmethod
    def extract_all(encounter: dict[str, Any]) -> dict[str, Any]:
        """
        Encounter から全ての関連データを抽出

        Args:
            encounter: Encounter リソース

        Returns:
            抽出されたデータのdict
        """
        return {
            "encounter_id": encounter.get("id"),
            "status": EncounterDataExtractor.extract_status(encounter),
            "class_display": EncounterDataExtractor.extract_class_display(encounter),
            "period": EncounterDataExtractor.extract_period(encounter),
            "service_provider": EncounterDataExtractor.extract_service_provider(encounter),
        }
