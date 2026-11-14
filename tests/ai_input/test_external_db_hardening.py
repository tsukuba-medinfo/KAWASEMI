"""external_db_extract の硬化: 使える接続の種類と、読み取り専用トランザクション。

患者での絞り込みは接続先 DB 側の責任（docs/DEPLOYMENT.md）。アプリ側でできる範囲として、
- 計画の作者が選べるのは kind: db かつ scope: external の接続だけ
- 抽出は読み取り専用トランザクションの中で走る（SQL の文字列検査をすり抜けても書けない）
を確かめる。
"""

from __future__ import annotations

import sqlite3

import pytest

import kawasemi.ai_input.executor as executor_mod
from kawasemi.ai_input.executor import DSLInterpreter
from kawasemi.ai_input.resource_index.connections import ConnectionRegistry

BASE = {"op": "external_db_extract", "id": "v"}


def _make_db(tmp_path) -> str:
    db_file = tmp_path / "ext.db"
    conn = sqlite3.connect(db_file)
    conn.execute("CREATE TABLE labs (pid TEXT, val TEXT)")
    conn.execute("INSERT INTO labs (pid, val) VALUES ('REG-1', 'x')")
    conn.commit()
    conn.close()
    return f"sqlite+aiosqlite:///{db_file}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "entry",
    [
        {"kind": "fhir", "scope": "external", "secret_ref": "{dsn}"},
        {"kind": "file", "scope": "external", "secret_ref": "{dsn}"},
        {"kind": "db", "scope": "self", "secret_ref": "{dsn}"},
        {"scope": "external", "secret_ref": "{dsn}"},
    ],
)
async def test_only_external_db_connections_are_usable(tmp_path, monkeypatch, entry) -> None:
    dsn = _make_db(tmp_path)
    entry = {k: (dsn if v == "{dsn}" else v) for k, v in entry.items()}
    monkeypatch.setattr(executor_mod, "get_registry", lambda: ConnectionRegistry({"c": entry}))
    interp = DSLInterpreter()
    try:
        output, trace = await interp._op_external_db(
            {**BASE, "connection_id": "c", "sql": "SELECT val FROM labs"}, "REG-1", dict(BASE)
        )
    finally:
        await interp.aclose()
    assert output is None
    assert "kind: db かつ scope: external" in trace["error"]
    assert trace["failure_code"] == executor_mod.FailureCode.PATH_NOT_PERMITTED


@pytest.mark.asyncio
async def test_extraction_runs_in_a_readonly_transaction(tmp_path, monkeypatch) -> None:
    """文字列検査を（仮に）すり抜けた書き込みでも、読み取り専用トランザクションが止める。"""
    dsn = _make_db(tmp_path)
    registry = ConnectionRegistry({"c": {"kind": "db", "scope": "external", "secret_ref": dsn}})
    monkeypatch.setattr(executor_mod, "get_registry", lambda: registry)
    # 検査を素通しにして、データベース側の防御だけを見る
    monkeypatch.setattr(executor_mod, "is_select_only_sql", lambda sql: True)
    interp = DSLInterpreter()
    try:
        output, trace = await interp._op_external_db(
            {
                **BASE,
                "connection_id": "c",
                "sql": "INSERT INTO labs (pid, val) VALUES ('REG-2', 'y')",
            },
            "REG-1",
            dict(BASE),
        )
    finally:
        await interp.aclose()
    assert output is None
    assert "error" in trace and trace["error"]
    con = sqlite3.connect(tmp_path / "ext.db")
    try:
        assert con.execute("SELECT COUNT(*) FROM labs").fetchone()[0] == 1
    finally:
        con.close()


@pytest.mark.asyncio
async def test_select_still_works_and_pooled_connection_is_writable_again(
    tmp_path, monkeypatch
) -> None:
    """読み取り専用は接続の使用中だけ。プールへ戻る接続に query_only を残さない。"""
    dsn = _make_db(tmp_path)
    registry = ConnectionRegistry({"c": {"kind": "db", "scope": "external", "secret_ref": dsn}})
    monkeypatch.setattr(executor_mod, "get_registry", lambda: registry)
    interp = DSLInterpreter()
    try:
        output, _ = await interp._op_external_db(
            {**BASE, "connection_id": "c", "sql": "SELECT val FROM labs WHERE pid = :patient_id"},
            "REG-1",
            dict(BASE),
        )
        assert output == "x"
        engine = interp._external_engine(dsn)
        async with engine.begin() as conn:
            from sqlalchemy import text

            await conn.execute(text("INSERT INTO labs (pid, val) VALUES ('REG-3', 'z')"))
    finally:
        await interp.aclose()


# --- 接続レジストリ: kind を省いた external は読み込み時に見える ---------------------------


def test_registry_reports_kindless_external_connection(tmp_path, caplog) -> None:
    import logging

    cfg = tmp_path / "connections.yaml"
    cfg.write_text(
        "connections:\n"
        "  dwh_ok:   {kind: db, scope: external, secret_ref: env:X}\n"
        "  dwh_bad:  {scope: external, secret_ref: env:X}\n"
        "  odd:      {kind: sql, scope: external}\n"
        "  fhir_ext: {kind: fhir, scope: external, secret_ref: env:Y}\n",
        encoding="utf-8",
    )
    with caplog.at_level(logging.WARNING):
        registry = ConnectionRegistry.load(cfg)
    problems = registry.problems()
    assert any("'dwh_bad'" in p and "kind" in p for p in problems)
    assert any("'odd'" in p for p in problems)
    assert any("'fhir_ext'" in p and "external_db_extract" in p for p in problems)
    assert not any("'dwh_ok'" in p for p in problems)
    assert "接続レジストリの不備" in caplog.text and "dwh_bad" in caplog.text
    assert registry.is_external_db("dwh_ok") is True
    for cid in ("dwh_bad", "odd", "fhir_ext", "missing", None):
        assert registry.is_external_db(cid) is False


def test_compiler_does_not_emit_external_db_extract_for_kindless_connection() -> None:
    from kawasemi.ai_input.models import OpType
    from kawasemi.ai_input.resource_index.derive import resolve_db_op

    registry = ConnectionRegistry(
        {
            "ok": {"kind": "db", "scope": "external", "secret_ref": "x"},
            "bad": {"scope": "external", "secret_ref": "x"},
        }
    )
    assert resolve_db_op({"connection_id": "ok"}, registry) == OpType.EXTERNAL_DB_EXTRACT
    # 実行時に拒否される op を作らない（不足＝人の補完へ回す）
    assert resolve_db_op({"connection_id": "bad"}, registry) is None
