"""FHIR サーバ連携のテスト

シナリオ: Patient ID を指定して、性別を自動的に補完する

取得は `FHIRClient` を通す（以前は生の `httpx.AsyncClient` で偽サーバを直接
叩いていたので、シナリオの名前に反して製品の取得経路を通っていなかった）。
"""

import httpx
import pytest

import kawasemi.utils.fhir_client as fhir_client_mod
from kawasemi.utils.fhir_client import (
    EncounterDataExtractor,
    FHIRClient,
    PatientDataExtractor,
)
from tests.fhir_resources import fhir_mock_transport


@pytest.fixture
def client(monkeypatch) -> FHIRClient:
    """内部 httpx.AsyncClient を偽トランスポートへ向けた FHIRClient"""
    real_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs.setdefault("transport", fhir_mock_transport())
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(fhir_client_mod.httpx, "AsyncClient", factory)
    return FHIRClient(base_url="http://mock-fhir-server")


class TestPatientDataExtractor:
    """PatientDataExtractorのテスト"""

    def test_extract_gender_male(self):
        """男性の性別抽出"""
        patient = {"gender": "male"}
        assert PatientDataExtractor.extract_gender(patient) == "male"

    def test_extract_gender_female(self):
        """女性の性別抽出"""
        patient = {"gender": "female"}
        assert PatientDataExtractor.extract_gender(patient) == "female"

    def test_extract_gender_display_male(self):
        """男性の性別表示抽出"""
        patient = {"gender": "male"}
        assert PatientDataExtractor.extract_gender_display(patient) == "男性"

    def test_extract_gender_display_female(self):
        """女性の性別表示抽出"""
        patient = {"gender": "female"}
        assert PatientDataExtractor.extract_gender_display(patient) == "女性"

    def test_extract_name(self):
        """氏名の抽出"""
        patient = {
            "name": [
                {"use": "official", "text": "山田 太郎"},
            ]
        }
        assert PatientDataExtractor.extract_name(patient) == "山田 太郎"

    def test_extract_birth_date(self):
        """生年月日の抽出"""
        patient = {"birthDate": "1980-01-15"}
        assert PatientDataExtractor.extract_birth_date(patient) == "1980-01-15"

    def test_extract_all(self):
        """全データの抽出"""
        patient = {
            "id": "patient-001",
            "gender": "male",
            "birthDate": "1980-01-15",
            "name": [{"use": "official", "text": "山田 太郎"}],
        }
        result = PatientDataExtractor.extract_all(patient)

        assert result["patient_id"] == "patient-001"
        assert result["gender"] == "male"
        assert result["gender_display"] == "男性"
        assert result["birth_date"] == "1980-01-15"
        assert result["name"] == "山田 太郎"


class TestFHIRClientIntegration:
    """FHIRClient で取得した Patient から補完データを組み立てる経路"""

    async def test_get_patient_gender(self, client: FHIRClient):
        """
        シナリオ: Patient IDを指定して性別を自動補完

        このテストは、KAWASEMIがFHIRサーバから患者情報を取得し、
        性別を自動的に補完できることを検証します。
        """
        patient = await client.get_patient("patient-001")

        gender = PatientDataExtractor.extract_gender(patient)
        gender_display = PatientDataExtractor.extract_gender_display(patient)

        # 検証: Patient ID = patient-001 は男性
        assert gender == "male"
        assert gender_display == "男性"

    async def test_autocomplete_gender_scenario(self, client: FHIRClient):
        """
        シナリオ: フォームに患者IDを入力すると性別が自動補完される

        1. ユーザーがフォームに患者ID（patient-002）を入力
        2. システムがFHIRサーバからPatient情報を取得
        3. 性別フィールドに「female」が自動補完される
        """
        # Step 1 & 2: FHIRサーバからPatient情報を取得
        patient = await client.get_patient("patient-002")

        # Step 3: 性別を抽出してフォームに補完するデータを生成
        autocomplete_data = PatientDataExtractor.extract_all(patient)

        # 検証
        assert autocomplete_data["patient_id"] == "patient-002"
        assert autocomplete_data["gender"] == "female"
        assert autocomplete_data["gender_display"] == "女性"
        assert autocomplete_data["name"] == "佐藤 花子"
        assert autocomplete_data["birth_date"] == "1990-05-20"

    @pytest.mark.parametrize(
        ("patient_id", "gender", "display"),
        [
            ("patient-001", "male", "男性"),
            ("patient-002", "female", "女性"),
            ("patient-003", "other", "その他"),
        ],
    )
    async def test_autocomplete_with_different_genders(
        self, client: FHIRClient, patient_id: str, gender: str, display: str
    ):
        """シナリオ: 異なる性別の患者で自動補完が正しく動作する"""
        patient = await client.get_patient(patient_id)

        assert PatientDataExtractor.extract_gender(patient) == gender
        assert PatientDataExtractor.extract_gender_display(patient) == display


class TestFormPrefill:
    """フォーム事前入力に渡すデータの組み立て"""

    async def test_patient_lookup(self, client: FHIRClient):
        """患者情報の取得と抽出"""
        patient = await client.get_patient("patient-001")
        patient_info = PatientDataExtractor.extract_all(patient)

        assert patient_info["patient_id"] == "patient-001"
        assert patient_info["gender"] == "male"
        assert patient_info["gender_display"] == "男性"
        assert patient_info["name"] == "山田 太郎"

    async def test_patient_lookup_with_encounter(self, client: FHIRClient):
        """患者情報とEncounter情報の同時取得"""
        patient_info = PatientDataExtractor.extract_all(await client.get_patient("patient-001"))
        encounter_info = EncounterDataExtractor.extract_all(
            await client.get_encounter("encounter-001")
        )

        assert patient_info["patient_id"] == "patient-001"
        assert encounter_info["encounter_id"] == "encounter-001"
        assert encounter_info["class_display"] == "外来"


class TestEncounterDataExtractor:
    """EncounterDataExtractorのテスト"""

    def test_extract_status(self):
        """ステータスの抽出"""
        encounter = {"status": "finished"}
        assert EncounterDataExtractor.extract_status(encounter) == "finished"

    def test_extract_class_display(self):
        """クラス表示名の抽出"""
        encounter = {"class": {"code": "AMB", "display": "外来"}}
        assert EncounterDataExtractor.extract_class_display(encounter) == "外来"

    def test_extract_period(self):
        """期間の抽出"""
        encounter = {
            "period": {
                "start": "2024-01-15T09:00:00+09:00",
                "end": "2024-01-15T10:30:00+09:00",
            }
        }
        period = EncounterDataExtractor.extract_period(encounter)

        assert period["start"] == "2024-01-15T09:00:00+09:00"
        assert period["end"] == "2024-01-15T10:30:00+09:00"

    def test_extract_service_provider(self):
        """サービス提供者の抽出"""
        encounter = {
            "serviceProvider": {"reference": "Organization/org-001", "display": "サンプル病院"}
        }
        assert EncounterDataExtractor.extract_service_provider(encounter) == "サンプル病院"

    def test_extract_all(self):
        """全データの抽出"""
        encounter = {
            "id": "encounter-001",
            "status": "finished",
            "class": {"code": "AMB", "display": "外来"},
            "period": {
                "start": "2024-01-15T09:00:00+09:00",
                "end": "2024-01-15T10:30:00+09:00",
            },
            "serviceProvider": {"display": "サンプル病院"},
        }
        result = EncounterDataExtractor.extract_all(encounter)

        assert result["encounter_id"] == "encounter-001"
        assert result["status"] == "finished"
        assert result["class_display"] == "外来"
        assert result["period"]["start"] == "2024-01-15T09:00:00+09:00"
        assert result["service_provider"] == "サンプル病院"
