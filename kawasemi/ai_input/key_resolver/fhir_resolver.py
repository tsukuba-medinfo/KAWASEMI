"""in-process FHIR identifier resolver。

FHIR Patient.identifier（system|value）を crosswalk として使う。anchor key_type
（登録番号）の `system|subject` で Patient を検索し、ヒット1件のときその identifier
配列から各要求 key_type（= key_types カタログの system）の value を canonical として
返す。0件=not_found、複数=ambiguous（自動選択しない＝取り違え防止）。
`fhir_patient_id` は identifier ではなく Patient.id 自身。

KAWASEMI は値を保持しない: ここで得た値は呼び出し側（executor, PR⑦）が実行時バインドに
使い捨て、evidence にはマスク＋参照のみを残す。

設計: backend=fhir / ambiguousは自動選択しない
"""

from __future__ import annotations

from typing import Any

from kawasemi.ai_input.key_resolver.schema import (
    ResolveContext,
    ResolvedKey,
    ResolveStatus,
)
from kawasemi.ai_input.resource_index.key_types import KeyTypeCatalog, get_catalog
from kawasemi.core.config import settings
from kawasemi.core.logging_config import get_logger
from kawasemi.utils.fhir_client import FHIRClient

logger = get_logger(__name__)

# Patient.id を指す特別な key_type（identifier ではなくリソース id）
FHIR_PATIENT_ID_KEY_TYPE = "fhir_patient_id"


class FhirIdentifierResolver:
    """FHIR Patient.identifier を用いた in-process resolver。"""

    def __init__(
        self,
        base_url: str | None = None,
        catalog: KeyTypeCatalog | None = None,
        client: Any | None = None,
        anchor_key_type: str | None = None,
    ) -> None:
        self._base_url = base_url or settings.fhir_server_url
        self._catalog = catalog
        self._client = client
        self._anchor = anchor_key_type or settings.key_resolver_anchor

    @property
    def catalog(self) -> KeyTypeCatalog:
        return self._catalog or get_catalog()

    def _client_for(self) -> Any:
        return self._client or FHIRClient(base_url=self._base_url)

    async def resolve(
        self, context: ResolveContext, subject: str, key_types: list[str]
    ) -> dict[str, ResolvedKey]:
        catalog = self.catalog
        anchor = catalog.get(self._anchor)
        anchor_system = anchor.system if anchor else None
        identifier_param = f"{anchor_system}|{subject}" if anchor_system else subject

        client = self._client_for()
        try:
            bundle = await client.search_patients(identifier=identifier_param)
        except Exception as e:  # noqa: BLE001 - 到達不能は呼び出し側へ明示伝播（SPoF）
            logger.error(f"resolver(FHIR)検索エラー: study={context.study_id} ({e})")
            raise

        entries = bundle.get("entry", []) if isinstance(bundle, dict) else []
        if len(entries) == 0:
            return {
                kt: ResolvedKey(key_type=kt, status=ResolveStatus.NOT_FOUND) for kt in key_types
            }
        if len(entries) > 1:
            # 自動選択しない（取り違え防止）。一意化は resolver/外部系の責務。
            return {
                kt: ResolvedKey(key_type=kt, status=ResolveStatus.AMBIGUOUS) for kt in key_types
            }

        first = entries[0]
        patient = first.get("resource", {}) if isinstance(first, dict) else {}
        by_system: dict[str | None, str] = {}
        for idn in patient.get("identifier", []) or []:
            if isinstance(idn, dict) and idn.get("value") is not None:
                by_system[idn.get("system")] = str(idn["value"])

        results: dict[str, ResolvedKey] = {}
        for kt in key_types:
            if kt == FHIR_PATIENT_ID_KEY_TYPE:
                pid = patient.get("id")
                results[kt] = (
                    ResolvedKey(key_type=kt, status=ResolveStatus.FOUND, value=str(pid))
                    if pid
                    else ResolvedKey(key_type=kt, status=ResolveStatus.NOT_FOUND)
                )
                continue
            ktdef = catalog.get(kt)
            sysuri = ktdef.system if ktdef else None
            val = by_system.get(sysuri) if sysuri else None
            results[kt] = (
                ResolvedKey(key_type=kt, status=ResolveStatus.FOUND, value=val)
                if val is not None
                else ResolvedKey(key_type=kt, status=ResolveStatus.NOT_FOUND)
            )
        return results
