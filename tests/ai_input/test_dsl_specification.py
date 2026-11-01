"""DSL 仕様（`docs/dsl_specification.md`）と機械可読スキーマがコードとずれていないこと。

仕様はコードの写しなので、写しが古くなったら落ちるようにする。

- `docs/schemas/dsl_step.schema.json` が `dsl.step_schema()` の出力と一致する
- 仕様の op 表（§5.0）の op の集合と必須フィールドが `OpType.ALL` / `_REQUIRED_FIELDS` と一致する
- 仕様の作例（§12）が `validate_program` を通る（E2E の実行結果との一致は
  `tests/e2e/test_pipeline_e2e.py` が見る）
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from kawasemi.ai_input.dsl import (
    _REQUIRED_FIELDS,
    AGGREGATE_FUNCS,
    COMPARE_OPS,
    UNARY_COMPARE_OPS,
    validate_program,
)
from kawasemi.ai_input.models import OpType
from scripts.export_dsl_schema import SCHEMA_PATH, render

SPEC_PATH = Path(__file__).resolve().parents[2] / "docs" / "dsl_specification.md"


def _spec() -> str:
    return SPEC_PATH.read_text(encoding="utf-8")


def spec_example(name: str) -> dict[str, Any]:
    """`<!-- dsl-example:<name> -->` の直後の ```json ブロックを program として読む。"""
    m = re.search(
        rf"<!-- dsl-example:{re.escape(name)} -->\s*```json\n(.*?)\n```", _spec(), re.DOTALL
    )
    assert m, f"仕様に作例 {name} の印が無い"
    program: dict[str, Any] = json.loads(m.group(1))
    return program


def _op_table_rows() -> dict[str, list[str]]:
    """§5.0 の op 表を {op: [必須フィールド...]} で返す。"""
    text = _spec()
    m = re.search(r"<!-- dsl-op-table:begin -->\n(.*?)<!-- dsl-op-table:end -->", text, re.DOTALL)
    assert m, "仕様に op 表の印が無い"
    rows: dict[str, list[str]] = {}
    for line in m.group(1).splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 3 or not cells[0].startswith("`"):
            continue
        op = cells[0].strip("`")
        # 必須フィールドの列は「`a`, `b`（注記）」の形。括弧の注記は落とす
        required = re.findall(r"`([^`]+)`", cells[2].split("（")[0])
        assert op not in rows, f"op 表に {op} が重複している"
        rows[op] = required
    return rows


def test_committed_schema_matches_code() -> None:
    """コミットされたスキーマが `dsl.step_schema()` の写しであること。

    落ちたら `uv run python scripts/export_dsl_schema.py`（`make dsl-schema`）で再生成する。
    """
    assert SCHEMA_PATH.read_text(encoding="utf-8") == render()


def test_spec_op_table_matches_accepted_ops() -> None:
    """仕様の op 表の op の集合が、コードが受け付ける op の集合と一致すること。"""
    assert set(_op_table_rows()) == set(OpType.ALL)


def test_spec_op_table_required_fields_match_code() -> None:
    """op 表の必須フィールドの列が `_REQUIRED_FIELDS` と一致すること（順序も）。"""
    rows = _op_table_rows()
    for op in OpType.ALL:
        assert tuple(rows[op]) == _REQUIRED_FIELDS[op], op


def test_spec_lists_value_vocabularies() -> None:
    """`compare.cmp` と `aggregate.func` の語彙が仕様に全て書かれていること。"""
    text = _spec()
    section = text.split("### 5.8 `compare`")[1].split("\n### ")[0]
    for cmp in COMPARE_OPS:
        assert f"`{cmp}`" in section, cmp
    for cmp in UNARY_COMPARE_OPS:
        assert f"`{cmp}`" in section, cmp
    section = text.split("### 5.12 `aggregate`")[1].split("\n### ")[0]
    for func in AGGREGATE_FUNCS:
        assert f"`{func}`" in section, func


def test_spec_worked_example_validates() -> None:
    """§12 の作例が検証を通る、完全な program であること。"""
    program = spec_example("onset_date")
    assert validate_program(program) == []
    assert program["return"] == "$v"
    assert {s["op"] for s in program["steps"]} >= {
        OpType.EXTERNAL_DB_EXTRACT,
        OpType.FILE_EXTRACT,
        OpType.LLM_EXTRACT,
        OpType.COMPARE,
        OpType.BRANCH,
    }
