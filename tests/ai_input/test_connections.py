"""接続レジストリ（connections.yaml）と scope 依存の source_category 導出

- ConnectionRegistry: YAML ロード / scope_of / 未配置縮退。
- derive_source_category: 接続 scope と kind から local_db/external_db/fhir/file/manual。
"""

from __future__ import annotations

import pytest

from kawasemi.ai_input.models import SourceCategory
from kawasemi.ai_input.resource_index.connections import ConnectionRegistry
from kawasemi.ai_input.resource_index.derive import derive_source_category
from kawasemi.ai_input.resource_index.schema import Kind, ResourceUnit


def _db_unit(connection_id: str | None) -> ResourceUnit:
    return ResourceUnit(
        unit_id="db:T.C",
        kind=Kind.DB,
        label="x",
        search_text="x",
        payload={"connection_id": connection_id, "db_qualified": "T", "physical_name": "C"},
    )


def _file_unit(role: str) -> ResourceUnit:
    return ResourceUnit(
        unit_id="file:x",
        kind=Kind.FILE,
        label="x",
        search_text="x",
        payload={"file_role": role, "path_pattern": "/p/*.pdf"},
    )


# --------------------------------------------------------------------------- #
# ConnectionRegistry
# --------------------------------------------------------------------------- #


def test_registry_load_yaml(tmp_path):
    p = tmp_path / "connections.yaml"
    p.write_text(
        "connections:\n"
        "  dwh: {kind: db, scope: external, host: h, secret_ref: 'env:DWH'}\n"
        "  app_db: {kind: db, scope: self}\n"
        "  hosp_fhir: {kind: fhir, scope: external, base_url: 'https://x'}\n",
        encoding="utf-8",
    )
    reg = ConnectionRegistry.load(p)
    assert reg.is_loaded
    assert reg.scope_of("dwh") == "external"
    assert reg.scope_of("app_db") == "self"
    assert reg.get("hosp_fhir")["base_url"] == "https://x"
    assert reg.scope_of("unknown") is None


def test_registry_missing_is_empty(tmp_path):
    reg = ConnectionRegistry.load(tmp_path / "nope.yaml")
    assert not reg.is_loaded
    assert reg.scope_of("dwh") is None
    assert reg.get(None) is None


def test_registry_accepts_top_level_mapping(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text("dwh: {kind: db, scope: external}\n", encoding="utf-8")
    reg = ConnectionRegistry.load(p)
    assert reg.scope_of("dwh") == "external"


# --------------------------------------------------------------------------- #
# scope 依存の source_category 導出
# --------------------------------------------------------------------------- #


@pytest.fixture
def registry() -> ConnectionRegistry:
    return ConnectionRegistry(
        {
            "dwh": {"kind": "db", "scope": "external"},
            "app_db": {"kind": "db", "scope": "self"},
        }
    )


def test_derive_db_external(registry):
    assert derive_source_category(_db_unit("dwh"), registry) == SourceCategory.EXTERNAL_DB


def test_derive_db_self(registry):
    assert derive_source_category(_db_unit("app_db"), registry) == SourceCategory.LOCAL_DB


def test_derive_db_unregistered_defaults_local(registry):
    assert derive_source_category(_db_unit("ghost"), registry) == SourceCategory.LOCAL_DB
    assert derive_source_category(_db_unit(None), registry) == SourceCategory.LOCAL_DB


def test_derive_fhir(registry):
    u = ResourceUnit(unit_id="fhir:x", kind=Kind.FHIR, label="x", search_text="x", payload={})
    assert derive_source_category(u, registry) == SourceCategory.FHIR


def test_derive_file_roles(registry):
    assert derive_source_category(_file_unit("patient_data"), registry) == SourceCategory.FILE
    assert derive_source_category(_file_unit("reference"), registry) == SourceCategory.MANUAL


# --- dsn_of / root_path_of（external_db_extract / file_extract の接続解決） ---


def test_dsn_of_resolves_env_secret_ref(monkeypatch):
    """dsn_of は secret_ref=env:VAR を環境変数から解決する（生DSNをyamlに置かない）。"""
    monkeypatch.setenv("TEST_EXT_DSN", "postgresql+asyncpg://u:p@h/db")
    reg = ConnectionRegistry({"dwh": {"scope": "external", "secret_ref": "env:TEST_EXT_DSN"}})
    assert reg.dsn_of("dwh") == "postgresql+asyncpg://u:p@h/db"


def test_dsn_of_unregistered_or_missing_is_none(monkeypatch):
    reg = ConnectionRegistry({"dwh": {"scope": "external", "secret_ref": "env:UNSET_DSN_VAR"}})
    monkeypatch.delenv("UNSET_DSN_VAR", raising=False)
    assert reg.dsn_of("dwh") is None  # env 未設定
    assert reg.dsn_of("ghost") is None  # 未登録
    assert reg.dsn_of(None) is None


def test_root_path_of():
    reg = ConnectionRegistry({"report_fs": {"scope": "external", "root_path": "/data/reports"}})
    assert reg.root_path_of("report_fs") == "/data/reports"
    assert reg.root_path_of("ghost") is None
    assert reg.root_path_of(None) is None
