"""KeyResolver のユニットテスト。

FhirIdentifierResolver を fake FHIRClient ＋ テスト用 KeyTypeCatalog で検証する。
- found: anchor system|subject で Patient 1件 → identifier から各 key_type の value
- fhir_patient_id: Patient.id 自身
- system が identifier に無い key_type → not_found
- 0件 → all not_found / 2件 → all ambiguous（自動選択しない）

**Protocol 充足と `ResolveResponse` の JSON round-trip の検査は削除した**。
どちらも「実装が2つあり、HTTP 境界で JSON 化する」ことを前提にした検査で、
Protocol・factory・HTTP 実装・HTTP 境界の受け皿と一緒に消えた。
"""

from __future__ import annotations

from typing import Any

from kawasemi.ai_input.key_resolver import (
    ResolveContext,
    ResolveStatus,
)
from kawasemi.ai_input.key_resolver.fhir_resolver import FhirIdentifierResolver
from kawasemi.ai_input.resource_index.key_types import (
    CanonicalSpec,
    KeyType,
    KeyTypeCatalog,
)

_CATALOG = KeyTypeCatalog(
    {
        "trial_subject_no": KeyType("trial_subject_no", "登録番号", system="urn:trial"),
        "mrn": KeyType("mrn", "MRN", system="urn:mrn", canonical=CanonicalSpec("numeric_string")),
        "fhir_patient_id": KeyType("fhir_patient_id", "FHIR id"),
    }
)


class _FakeFHIRClient:
    def __init__(self, bundle: dict[str, Any]) -> None:
        self.bundle = bundle
        self.calls: list[str | None] = []

    async def search_patients(
        self, identifier: str | None = None, gender: str | None = None
    ) -> dict[str, Any]:
        self.calls.append(identifier)
        return self.bundle


def _resolver(bundle: dict[str, Any]) -> tuple[FhirIdentifierResolver, _FakeFHIRClient]:
    client = _FakeFHIRClient(bundle)
    r = FhirIdentifierResolver(catalog=_CATALOG, client=client, anchor_key_type="trial_subject_no")
    return r, client


async def test_found_resolves_identifiers() -> None:
    bundle = {
        "entry": [
            {
                "resource": {
                    "id": "p-1",
                    "identifier": [
                        {"system": "urn:trial", "value": "S-001"},
                        {"system": "urn:mrn", "value": "123456"},
                    ],
                }
            }
        ]
    }
    r, client = _resolver(bundle)
    out = await r.resolve(ResolveContext(study_id="ST"), "S-001", ["mrn", "fhir_patient_id"])

    assert client.calls == ["urn:trial|S-001"]  # anchor system|subject で検索
    assert out["mrn"].status == ResolveStatus.FOUND
    assert out["mrn"].value == "123456"
    assert out["fhir_patient_id"].status == ResolveStatus.FOUND
    assert out["fhir_patient_id"].value == "p-1"  # Patient.id 自身


async def test_missing_system_is_not_found() -> None:
    bundle = {
        "entry": [
            {"resource": {"id": "p-1", "identifier": [{"system": "urn:trial", "value": "S-001"}]}}
        ]
    }
    r, _ = _resolver(bundle)
    out = await r.resolve(ResolveContext(study_id="ST"), "S-001", ["mrn", "dwh_serial"])
    assert out["mrn"].status == ResolveStatus.NOT_FOUND  # identifier に urn:mrn 無し
    assert out["dwh_serial"].status == ResolveStatus.NOT_FOUND  # カタログにも無い


async def test_zero_entries_all_not_found() -> None:
    r, _ = _resolver({"entry": []})
    out = await r.resolve(ResolveContext(study_id="ST"), "S-999", ["mrn", "fhir_patient_id"])
    assert all(v.status == ResolveStatus.NOT_FOUND for v in out.values())


async def test_multiple_entries_all_ambiguous() -> None:
    bundle = {"entry": [{"resource": {"id": "p-1"}}, {"resource": {"id": "p-2"}}]}
    r, _ = _resolver(bundle)
    out = await r.resolve(ResolveContext(study_id="ST"), "S-001", ["mrn"])
    assert out["mrn"].status == ResolveStatus.AMBIGUOUS  # 自動選択しない
    assert out["mrn"].value is None


async def test_anchor_without_system_uses_bare_subject() -> None:
    # anchor key_type に system が無い場合は素の subject で検索する
    catalog = KeyTypeCatalog({"trial_subject_no": KeyType("trial_subject_no", "登録番号")})
    client = _FakeFHIRClient({"entry": []})
    r = FhirIdentifierResolver(catalog=catalog, client=client, anchor_key_type="trial_subject_no")
    await r.resolve(ResolveContext(study_id="ST"), "S-001", ["mrn"])
    assert client.calls == ["S-001"]
