"""E2E 実機層の周辺（終端の判定・記録・`kawasemi e2e serve`）の検査。

実機層そのもの（`tests/e2e/test_pipeline_live.py`）は LLM サーバが要るので既定では走らない。
ここは、その結果の読み方を決める部品を Ollama 無しで固定する。
"""

from __future__ import annotations

import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from kawasemi import cli
from kawasemi.ai_input import e2e
from kawasemi.ai_input.e2e.gates import FAIL, PASS, SKIP, GateReport
from kawasemi.ai_input.e2e.pipeline import (
    EMPTY_PROGRAM,
    REJECTED_LLM,
    UNATTEMPTED,
    VISION_UNAVAILABLE,
    PipelineResult,
    compile_failure_reason,
    latest_live_run,
    run_record_markdown,
    write_run_record,
)


def _report() -> GateReport:
    report = GateReport()
    ok = report.add("birth_date", "db")
    for g in "abcde":
        ok.set(g, True)
    failed = report.add("crp_on_admission", "db")
    failed.set("a", True)
    failed.set("b", False, "db:lab_results.result_value が上位8に無い")
    vision = report.add("consent_date", "file")
    for g in "abc":
        vision.set(g, True)
    vision.skip("d", VISION_UNAVAILABLE)
    return report


def test_terminal_distinguishes_reasoned_skip_from_stalled_item() -> None:
    report = _report()
    assert report.unterminated() == []
    assert report.items["consent_date"].gates["d"] == SKIP
    assert report.items["consent_date"].gates["e"] == SKIP
    # 理由の無い skip で止まった項目は終端ではない（工程が途中で止まった）
    stalled = report.add("severity", "fhir")
    stalled.set("a", True)
    assert report.unterminated() == ["severity"]
    assert report.items["crp_on_admission"].gates["b"] == FAIL
    assert report.items["birth_date"].gates["e"] == PASS


def test_run_record_is_a_record_with_models_and_reasons(tmp_path: Path) -> None:
    result = PipelineResult(
        report=_report(),
        gold={},
        programs={},
        evidence={},
        notes={"clinical_draft": {"with_citation": ["pathogen"]}},
        timings={"index": 12.5, "compile": 30.0},
    )
    meta = {
        "started_at": "2026-10-23T01:00:00",
        "vision_available": False,
        "models": {
            "設計・実行の LLM": {"name": "m:1", "digest": "abcdef0123456789", "base_url": "u"}
        },
    }
    md = run_record_markdown(meta, result)
    assert "評価の指標ではない" in md
    assert "| 設計・実行の LLM | m:1 | abcdef012345 | u |" in md
    assert VISION_UNAVAILABLE in md
    assert "上位8に無い" in md
    assert "| 計 | 42.5 |" in md
    js, mdp = write_run_record(tmp_path / "run", {**meta, "error": "boom"}, None)
    assert js.exists() and "boom" in mdp.read_text("utf-8")


def _fake_run(root: Path, name: str) -> Path:
    run = root / name
    (run / "generated").mkdir(parents=True)
    with sqlite3.connect(run / "app.db") as con:
        con.execute("CREATE TABLE users (username TEXT, password_hash TEXT)")
        con.execute("INSERT INTO users VALUES ('e2e_designer', 'old')")
    return run


def test_latest_live_run_picks_newest_with_app_db(tmp_path: Path) -> None:
    assert latest_live_run(tmp_path / "none") is None
    _fake_run(tmp_path, "20261022-000000")
    newest = _fake_run(tmp_path, "20261023-000000")
    (tmp_path / "20261024-000000").mkdir()  # app.db の無い（途中で止まった）実行
    assert latest_live_run(tmp_path) == newest


def test_e2e_serve_points_everything_at_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = _fake_run(tmp_path, "20261023-000000")
    calls: dict[str, Any] = {}

    class _Proc:
        def terminate(self) -> None: ...

        def wait(self, timeout: float | None = None) -> int:
            return 0

    def fake_popen(argv: list[str], **kw: Any) -> _Proc:
        calls["stub"] = argv
        return _Proc()

    def fake_run(argv: list[str], **kw: Any) -> None:
        calls["app"], calls["env"] = argv, kw["env"]

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("DEMO_PASSWORD", "pw-for-test")

    cli.e2e_serve(host="127.0.0.1", port=e2e.APP_PORT, run_dir=str(run))

    env = calls["env"]
    assert env["DATABASE_URL"].endswith(str(run.resolve() / "app.db"))
    assert env["RESOURCE_INDEX_DIR"] == str(run.resolve() / "resource_index")
    assert env["FHIR_SERVER_URL"] == f"http://localhost:{e2e.FHIR_STUB_PORT}/fhir"
    assert calls["stub"][calls["stub"].index("--port") + 1] == str(e2e.FHIR_STUB_PORT)
    assert calls["app"][calls["app"].index("--host") + 1] == "127.0.0.1"
    assert calls["app"][calls["app"].index("--port") + 1] == str(e2e.APP_PORT)
    # 入り口の制限は外す（.env が入り口を絞っていても e2e の利用者が入れる）
    assert env["ENTRY_NAME"] == env["ENTRY_ROLES"] == env["ENTRY_URLS"] == ""
    conns = yaml.safe_load(Path(env["CONNECTIONS_FILE"]).read_text("utf-8"))["connections"]
    assert conns[e2e.CONN_FHIR]["base_url"] == env["FHIR_SERVER_URL"]
    assert conns[e2e.CONN_FILES]["root_path"].startswith(str(run.resolve()))
    with sqlite3.connect(run / "app.db") as con:
        (hashed,) = con.execute("SELECT password_hash FROM users").fetchone()
    assert hashed != "old"


def test_e2e_serve_defaults_bind_localhost(monkeypatch: pytest.MonkeyPatch) -> None:
    got: dict[str, Any] = {}
    monkeypatch.setattr(cli, "e2e_serve", lambda **kw: got.update(kw))
    monkeypatch.setattr(sys, "argv", ["kawasemi", "e2e", "serve"])
    cli.main()
    assert got == {"host": "127.0.0.1", "port": e2e.APP_PORT, "run_dir": None}


def test_e2e_ports_do_not_collide_with_common_dev_ports() -> None:
    """FHIR スタブとアプリのポートが、開発でよく使うポートと互いに重ならない。

    8000 は `kawasemi serve`（uvicorn）の既定、8080 / 8081 は HTTP の代替ポートとして
    FHIR サーバや開発サーバの既定に多い。3000 / 5000 / 8888 はフロントエンド・Flask・
    Jupyter の既定、11434 は Ollama の既定である。E2E を開発中のアプリと並べて起動できるよう、
    これらを避ける。
    """
    taken = {3000, 5000, 8000, 8080, 8081, 8888, 11434}
    assert e2e.FHIR_STUB_PORT not in taken
    assert e2e.APP_PORT not in taken | {e2e.FHIR_STUB_PORT}


def test_compile_reason_separates_unattempted_from_other_failures() -> None:
    """打ち切り（未試行）・棄却・空のプログラムを「プランが返らなかった」と分けて書く。"""
    compiled = {
        "unattempted_link_ids": ["max_body_temp"],
        "rejected_llm": ["diagnosis_text"],
        "empty_program_link_ids": ["sex"],
        "validation_errors": {"admission_date": ["params[0]: value は…"]},
    }
    prog = {"steps": [{"op": "external_db_extract", "id": "v"}], "return": "$v"}
    assert compile_failure_reason("max_body_temp", compiled, None) == UNATTEMPTED
    assert compile_failure_reason("diagnosis_text", compiled, None) == REJECTED_LLM
    assert compile_failure_reason("sex", compiled, {"steps": []}) == EMPTY_PROGRAM
    assert compile_failure_reason("pathogen", compiled, None) == "プランが返らなかった"
    reason = compile_failure_reason("admission_date", compiled, prog)
    assert reason is not None and reason.startswith("検証エラー")
    assert compile_failure_reason("birth_date", compiled, prog) is None
