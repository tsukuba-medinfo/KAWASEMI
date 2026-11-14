"""executor のキー解決のユニットテスト。

- physical 整形（ゼロ詰め等）と証跡マスク
- _key_for_step: anchor フォールバック / found+physical / not_found・ambiguous は skip
- _collect_key_types: プランから key_type 収集
- _mask_key_inputs: 生 patient_id 除去＋path 内の生値マスク（evidence 境界）
"""

from __future__ import annotations

import sqlite3

import pytest

import kawasemi.ai_input.executor as executor_mod
from kawasemi.ai_input.executor import DSLInterpreter
from kawasemi.ai_input.key_resolver import ResolvedKey, ResolveStatus
from kawasemi.ai_input.key_resolver.format import apply_physical_format, mask_value
from kawasemi.ai_input.models import InputPlan
from kawasemi.ai_input.resource_index.connections import ConnectionRegistry


def test_apply_physical_format() -> None:
    assert apply_physical_format("123456", {"pad": 10}) == "0000123456"
    assert apply_physical_format("123456", {"pad": 10, "padchar": "0"}) == "0000123456"
    assert apply_physical_format("12", None) == "12"
    assert apply_physical_format("12", {"prefix": "P-", "suffix": "/X"}) == "P-12/X"


def test_mask_value() -> None:
    # 長さ比例マスク（末尾 min(4, 値長の25%) 文字のみ温存）
    assert mask_value("0000123456") == "********56"  # 10桁 → 末尾2
    assert mask_value("UC-0009") == "******9"  # 7桁 → 末尾1（短い登録番号の露出抑制）
    uuid = "550e8400-e29b-41d4-a716-446655440000"
    assert mask_value(uuid) == "*" * (len(uuid) - 4) + uuid[-4:]  # 長い値 → 末尾4
    assert mask_value("abc") == "***"  # 4文字以下は全マスク
    assert mask_value(None) is None


def test_key_for_step_anchor_fallback() -> None:
    i = DSLInterpreter()
    bind, meta, skip = i._key_for_step({"op": "sql_extract"}, "S-001", {})
    assert not skip
    assert bind == "S-001"  # key_type 未指定 → 登録番号そのもの
    assert meta["status"] == "anchor"
    assert meta["value_masked"] == mask_value("S-001")


def test_key_for_step_found_with_physical_format() -> None:
    i = DSLInterpreter()
    resolved = {"mrn": ResolvedKey(key_type="mrn", status=ResolveStatus.FOUND, value="123456")}
    bind, meta, skip = i._key_for_step(
        {"key_type": "mrn", "physical_format": {"pad": 10}}, "S-001", resolved
    )
    assert not skip
    assert bind == "0000123456"  # canonical 123456 → physical ゼロ詰め10桁
    assert meta == {"used_key_type": "mrn", "value_masked": "********56", "status": "found"}


def test_key_for_step_not_found_skips() -> None:
    i = DSLInterpreter()
    resolved = {"mrn": ResolvedKey(key_type="mrn", status=ResolveStatus.NOT_FOUND)}
    bind, meta, skip = i._key_for_step({"key_type": "mrn"}, "S-001", resolved)
    assert skip and bind is None
    assert meta["status"] == "not_found"


def test_key_for_step_ambiguous_skips() -> None:
    i = DSLInterpreter()
    resolved = {"mrn": ResolvedKey(key_type="mrn", status=ResolveStatus.AMBIGUOUS)}
    bind, meta, skip = i._key_for_step({"key_type": "mrn"}, "S-001", resolved)
    assert skip and bind is None
    assert meta["status"] == "ambiguous"


def test_collect_key_types() -> None:
    p1 = InputPlan(
        link_id="a",
        program={"steps": [{"op": "sql_extract", "key_type": "mrn"}, {"op": "sql_extract"}]},
    )
    p2 = InputPlan(
        link_id="b",
        program={"steps": [{"op": "fhir_extract", "key_type": "fhir_patient_id"}]},
    )
    assert DSLInterpreter._collect_key_types([p1, p2]) == {"mrn", "fhir_patient_id"}


def test_mask_key_inputs_strips_raw_and_masks_path() -> None:
    entry = {
        "op": "file_extract",
        "inputs": {
            "connection_id": "report_fs",
            "path": "/reports/0000123456/r.txt",
            "patient_id": "0000123456",
        },
    }
    out = DSLInterpreter._mask_key_inputs(
        entry,
        {"used_key_type": "mrn", "value_masked": "********56", "status": "found"},
        "0000123456",
    )
    assert "patient_id" not in out["inputs"]  # 生キー値は残さない
    assert out["inputs"]["path"] == "/reports/********56/r.txt"  # path 内もマスク
    assert out["inputs"]["used_key_type"] == "mrn"
    assert out["inputs"]["connection_id"] == "report_fs"


# --- external_db_extract 成功経路（実装が実際に値を返すことの担保） ---


@pytest.mark.asyncio
async def test_op_external_db_success_returns_value(tmp_path, monkeypatch) -> None:
    """connections 経由で外部DB（一時SQLite）へ SELECT し、値が補完される。

    従来は失敗経路（接続解決不可）のみ検証されており、成功経路が未カバーだった。
    ドキュメントの「実装済み」記述を実挙動で裏付ける。
    """
    db_file = tmp_path / "ext.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE labs (pid TEXT, val TEXT)")
    conn.execute("INSERT INTO labs (pid, val) VALUES ('REG-1', '陽性')")
    conn.commit()
    conn.close()

    dsn = f"sqlite+aiosqlite:///{db_file}"
    registry = ConnectionRegistry(
        {"labs_db": {"kind": "db", "scope": "external", "secret_ref": dsn}}
    )
    monkeypatch.setattr(executor_mod, "get_registry", lambda: registry)

    interp = DSLInterpreter()
    try:
        step = {
            "op": "external_db_extract",
            "id": "v",
            "connection_id": "labs_db",
            "sql": "SELECT val FROM labs WHERE pid = :patient_id",
        }
        output, trace = await interp._op_external_db(
            step, "REG-1", {"op": "external_db_extract", "id": "v"}
        )
    finally:
        await interp.aclose()

    assert output == "陽性"
    assert trace["output"] == "陽性"
    assert "error" not in trace


@pytest.mark.asyncio
async def test_op_external_db_rejects_non_select(monkeypatch) -> None:
    """SELECT 以外の SQL は接続解決前に拒否される（多層防御）。"""
    interp = DSLInterpreter()
    try:
        step = {
            "op": "external_db_extract",
            "id": "v",
            "connection_id": "labs_db",
            "sql": "DELETE FROM labs",
        }
        output, trace = await interp._op_external_db(
            step, "REG-1", {"op": "external_db_extract", "id": "v"}
        )
    finally:
        await interp.aclose()
    assert output is None
    assert "SELECT限定" in trace["error"]
