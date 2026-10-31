"""テストが使う FHIR リソースのひな型と、それを返す httpx トランスポート

`FHIRClient` のテストが偽の FHIR サーバに求めているのは「決まった JSON を返すこと」だけで、
ASGI アプリは要らない。以前は `fhir_mock_server/`（FastAPI アプリ 344 行）を立てて
`ASGITransport` で繋いでいたが、次の2つが問題だった。

- **アプリ自身を検査するテストが生えていた。** `patient-001` が「山田 太郎」であることの
  ような、製品コードを1行も通らない検査が9件あった（削除した）。
- **リセットのために同じ JSON を2度書いていた。** `reset_patients()` が初期値の literal を
  丸ごと持つので、ひな型を直すと2箇所直す必要があった。

ここでは応答を不変のデータとして持ち、`httpx.MockTransport` で返す。データを書き換える
経路が無いので、テスト間のリセットも要らない。
"""

from __future__ import annotations

import json
from typing import Any

import httpx

# 応答のひな型。**書き換えないこと**（トランスポートは deepcopy せずそのまま返す）。
SAMPLE_PATIENTS: dict[str, dict[str, Any]] = {
    "patient-001": {
        "resourceType": "Patient",
        "id": "patient-001",
        "meta": {"versionId": "1", "lastUpdated": "2024-01-01T00:00:00Z"},
        "identifier": [
            {"system": "urn:oid:1.2.392.100495.20.3.51.11234567890", "value": "0000000001"}
        ],
        "name": [{"use": "official", "family": "山田", "given": ["太郎"], "text": "山田 太郎"}],
        "gender": "male",
        "birthDate": "1980-01-15",
    },
    "patient-002": {
        "resourceType": "Patient",
        "id": "patient-002",
        "meta": {"versionId": "1", "lastUpdated": "2024-01-01T00:00:00Z"},
        "identifier": [
            {"system": "urn:oid:1.2.392.100495.20.3.51.11234567890", "value": "0000000002"}
        ],
        "name": [{"use": "official", "family": "佐藤", "given": ["花子"], "text": "佐藤 花子"}],
        "gender": "female",
        "birthDate": "1990-05-20",
    },
    "patient-003": {
        "resourceType": "Patient",
        "id": "patient-003",
        "meta": {"versionId": "1", "lastUpdated": "2024-01-01T00:00:00Z"},
        "identifier": [
            {"system": "urn:oid:1.2.392.100495.20.3.51.11234567890", "value": "0000000003"}
        ],
        "name": [{"use": "official", "family": "鈴木", "given": ["一郎"], "text": "鈴木 一郎"}],
        "gender": "other",
        "birthDate": "1975-11-30",
    },
}

SAMPLE_ENCOUNTERS: dict[str, dict[str, Any]] = {
    "encounter-001": {
        "resourceType": "Encounter",
        "id": "encounter-001",
        "status": "finished",
        "class": {
            "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
            "code": "AMB",
            "display": "外来",
        },
        "subject": {"reference": "Patient/patient-001"},
        "period": {"start": "2024-01-15T09:00:00+09:00", "end": "2024-01-15T10:30:00+09:00"},
        "serviceProvider": {"reference": "Organization/org-001", "display": "サンプル病院"},
    },
    "encounter-002": {
        "resourceType": "Encounter",
        "id": "encounter-002",
        "status": "in-progress",
        "class": {
            "system": "http://terminology.hl7.org/CodeSystem/v3-ActCode",
            "code": "IMP",
            "display": "入院",
        },
        "subject": {"reference": "Patient/patient-002"},
        "period": {"start": "2024-01-10T14:00:00+09:00"},
        "serviceProvider": {"reference": "Organization/org-001", "display": "サンプル病院"},
    },
}

_FHIR_JSON = "application/fhir+json"


def _fhir_response(status_code: int, content: dict[str, Any]) -> httpx.Response:
    # FHIRClient は Content-Type を見ないが、実サーバと同じ media type を返しておく。
    return httpx.Response(
        status_code,
        content=json.dumps(content, ensure_ascii=False).encode(),
        headers={"content-type": _FHIR_JSON},
    )


def _not_found(reference: str) -> httpx.Response:
    return _fhir_response(
        404,
        {
            "resourceType": "OperationOutcome",
            "issue": [
                {
                    "severity": "error",
                    "code": "not-found",
                    "diagnostics": f"{reference} が見つかりません",
                }
            ],
        },
    )


def _searchset(resources: list[dict[str, Any]], resource_type: str) -> httpx.Response:
    return _fhir_response(
        200,
        {
            "resourceType": "Bundle",
            "type": "searchset",
            "total": len(resources),
            "entry": [{"resource": r, "fullUrl": f"{resource_type}/{r['id']}"} for r in resources],
        },
    )


def _handle(request: httpx.Request) -> httpx.Response:
    """`FHIRClient` が実際に叩く経路だけに応答する。

    未知のパスは 404 ではなく `AssertionError` にする。テストが想定と違う URL を
    組み立てたときに「サーバが 404 を返した」と読めてしまうのを避けるため。
    """
    path = request.url.path
    params = request.url.params

    if path == "/Patient":
        results = list(SAMPLE_PATIENTS.values())
        if identifier := params.get("identifier"):
            results = [p for p in results if any(i["value"] == identifier for i in p["identifier"])]
        if gender := params.get("gender"):
            results = [p for p in results if p["gender"] == gender]
        return _searchset(results, "Patient")

    if path.startswith("/Patient/"):
        patient_id = path[len("/Patient/") :]
        patient = SAMPLE_PATIENTS.get(patient_id)
        return _fhir_response(200, patient) if patient else _not_found(f"Patient/{patient_id}")

    if path.startswith("/Encounter/"):
        encounter_id = path[len("/Encounter/") :]
        encounter = SAMPLE_ENCOUNTERS.get(encounter_id)
        return (
            _fhir_response(200, encounter) if encounter else _not_found(f"Encounter/{encounter_id}")
        )

    raise AssertionError(f"偽の FHIR サーバが応答しない経路を叩いた: {request.url}")


def fhir_mock_transport() -> httpx.MockTransport:
    """`SAMPLE_PATIENTS` / `SAMPLE_ENCOUNTERS` を返すトランスポート。

    応答するのは `/Patient/{id}`・`/Patient?identifier=&gender=`・`/Encounter/{id}`。
    """
    return httpx.MockTransport(_handle)
