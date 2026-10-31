"""実スキーマとモデル宣言の食い違いを検出する検査。

**守る対象は「CI では発火しない欠陥を、検査そのものは検出できる」ことである。**
CI もテストも DB を新規に作るので、移行漏れは本番の既存 DB でしか起きない。
そこでここでは**欠けを人工的に作って**、`inspect_schema` がそれを報告することを見る。
実際に落ちた `input_targets.needs_attention` の欠けを題材にする。
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from kawasemi.core.schema_check import SchemaDrift, inspect_schema
from kawasemi.models import Base

_REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """モデルどおりに作った、食い違いの無い SQLite ファイル。"""
    path = tmp_path / "drift.db"
    engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(engine)
    engine.dispose()
    return str(path)


def _inspect(path: str) -> SchemaDrift:
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.connect() as conn:
            return inspect_schema(conn)
    finally:
        engine.dispose()


def _execute(path: str, sql: str) -> None:
    engine = create_engine(f"sqlite:///{path}")
    try:
        with engine.begin() as conn:
            conn.execute(text(sql))
    finally:
        engine.dispose()


def test_a_schema_built_from_the_models_has_no_drift(db_path: str) -> None:
    """モデルから作った DB では、表も列も欠けない。

    ここが落ちるなら検査側の誤検出であり、以降の検査はすべて信用できない。
    """
    drift = _inspect(db_path)
    assert drift.missing_tables == ()
    assert drift.missing_columns == ()


def test_a_missing_column_is_reported(db_path: str) -> None:
    """列だけが欠けた DB を、表名と列名で報告する（実際に起きた形）。"""
    _execute(db_path, "ALTER TABLE input_targets DROP COLUMN needs_attention")

    drift = _inspect(db_path)
    assert ("input_targets", "needs_attention") in drift.missing_columns
    assert drift.missing_tables == ()
    assert bool(drift) is True
    assert "列が無い: input_targets.needs_attention" in drift.messages()


def test_a_missing_table_is_reported_once(db_path: str) -> None:
    """表ごと無いときは表として1件だけ報告し、その表の列は数えない。"""
    _execute(db_path, "DROP TABLE ai_input_executions")

    drift = _inspect(db_path)
    assert "ai_input_executions" in drift.missing_tables
    assert not [t for t, _ in drift.missing_columns if t == "ai_input_executions"]


def test_a_lying_alembic_version_is_reported(db_path: str) -> None:
    """**版の記録が head と食い違えば言う。**

    実際に起きた事故は逆（記録は head どおりで、実スキーマだけが古い）だったが、
    記録が古いまま止まっている DB も同じ画面の 500 を生む。
    """
    _execute(db_path, "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
    _execute(db_path, "INSERT INTO alembic_version (version_num) VALUES ('deadbeef1234')")

    drift = _inspect(db_path)
    assert drift.recorded_revision == "deadbeef1234"
    assert drift.head_revision is not None, "移行スクリプトの head を解決できていない"
    assert drift.revision_disagrees is True
    assert bool(drift) is True


def test_no_alembic_version_table_is_not_drift(db_path: str) -> None:
    """`create_all` で作った DB（`alembic_version` を持たない）を食い違いとしない。

    テストと開発環境がこれに当たる。ここで真になると、本当の食い違いが警告に埋もれる。
    """
    drift = _inspect(db_path)
    assert drift.recorded_revision is None
    assert drift.revision_disagrees is False
    assert bool(drift) is False


def test_the_check_loads_every_model_module() -> None:
    """**`schema_check` を import しただけで 24 表が揃うこと。**

    `kawasemi.models` だけでは 16 表しか登録されず、`ai_input_executions.batch_job_id`
    の欠けを見落とす。pytest プロセスでは他のテスト経由で
    `kawasemi.ai_input.models` が既に読み込まれてしまうため、
    **新しいプロセスで**確かめる（`tests/test_alembic_env.py` と同じ理由）。
    """
    code = (
        "import kawasemi.core.schema_check;"
        "from kawasemi.models import Base;"
        "print(len(Base.metadata.tables));"
        "print('ai_input_executions' in Base.metadata.tables)"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=_REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    count, has_ai_input = proc.stdout.split()
    assert has_ai_input == "True", (
        "schema_check を import しても ai_input 系の表が Base.metadata に入っていない。"
        "この状態の検査は ai_input 側の欠けを「差分なし」と答える"
    )
    assert int(count) >= 24, f"モデルの表が {count} 件しか登録されていない"
