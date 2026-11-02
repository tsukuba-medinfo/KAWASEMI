"""DSL 仕様（`docs/dsl_specification.md`）と機械可読スキーマがコードとずれていないこと。

仕様はコードの写しなので、写しが古くなったら落ちるようにする。

- `docs/schemas/dsl_step.schema.json` が `dsl.step_schema()` の出力と一致する
- 仕様の op 表（§5.0）の op の集合と必須フィールドが `OpType.ALL` / `_REQUIRED_FIELDS` と一致する
- 仕様の作例（§12）が `validate_program` を通る（E2E の実行結果との一致は
  `tests/e2e/test_pipeline_e2e.py` が見る）
- 仕様の中で印（`<!-- dsl-list:... -->` など）を付けた語彙と定数が、コードの値と一致する
  （失敗コード、`compare` / `select` / `aggregate` の語彙、SQL の禁止語、step 数の上限、
  LLM の固定オプション）
- §9.1.1 のエラーメッセージの一覧が、`validate_program` / `_validate_params` が返しうる
  エラーの雛形の全量と一致する（構文木から取り出して照合する）
- §5.10 に書いた `transform` の失敗の扱いが実際の挙動と一致する
"""

from __future__ import annotations

import ast
import inspect
import json
import re
from pathlib import Path
from typing import Any

import pytest

from kawasemi.ai_input import dsl
from kawasemi.ai_input.dsl import (
    _MAX_STEPS,
    _REQUIRED_FIELDS,
    _SQL_FORBIDDEN,
    _SQL_WORD_END,
    _SQL_WORD_START,
    AGGREGATE_FUNCS,
    COMPARE_OPS,
    SELECT_PICKS,
    UNARY_COMPARE_OPS,
    validate_program,
)
from kawasemi.ai_input.executor import _LLM_OPTIONS, DSLInterpreter, apply_transform
from kawasemi.ai_input.models import FailureCode, OpType
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


# --------------------------------------------------------------------------- #
# 印を付けた語彙と定数
# --------------------------------------------------------------------------- #


def _marked(kind: str, name: str) -> list[str]:
    """`<!-- {kind}:{name} -->...<!-- /{kind}:{name} -->` の中身を出現順に返す。"""
    pattern = rf"<!-- {re.escape(kind)}:{re.escape(name)} -->(.*?)<!-- /{re.escape(kind)}:{re.escape(name)} -->"
    found = re.findall(pattern, _spec(), re.DOTALL)
    assert found, f"仕様に {kind}:{name} の印が無い"
    return found


def _marked_list(name: str) -> list[str]:
    """`dsl-list` の印の中の `...` を語の列として返す（印は仕様に1か所だけ）。"""
    (body,) = _marked("dsl-list", name)
    return re.findall(r"`([^`]+)`", body)


def test_spec_failure_codes_match_code() -> None:
    """§4.6 の失敗コードの表が `FailureCode.ALL` と一致すること。"""
    text = _spec()
    m = re.search(
        r"<!-- dsl-failure-codes:begin -->\n(.*?)<!-- dsl-failure-codes:end -->", text, re.DOTALL
    )
    assert m, "仕様に失敗コードの表の印が無い"
    codes = re.findall(r"^\| `([a-z_]+)` \|", m.group(1), re.MULTILINE)
    assert sorted(codes) == sorted(FailureCode.ALL)
    assert f"次の{len(FailureCode.ALL)}語である" in text


_PROSE_COUNTS: list[tuple[str, int]] = [
    (r"(\d+)個の op", len(OpType.ALL)),  # §1.2
    (r"§5 の(\d+)語のいずれか", len(OpType.ALL)),  # §3.2
    (r"`op` が(\d+)語に無ければ", len(OpType.ALL)),  # V-07
    (r"`compare\.cmp` が §5\.8 の(\d+)語", len(COMPARE_OPS)),  # V-11
    (r"`aggregate\.func` が §5\.12 の(\d+)語", len(AGGREGATE_FUNCS)),  # V-11
]


def test_spec_prose_counts_match_code() -> None:
    """本文に書いた語彙の数（「12個の op」「12語」など）がコードの語彙の数と一致すること。"""
    text = _spec()
    for pattern, expected in _PROSE_COUNTS:
        found = re.findall(pattern, text)
        assert found, f"仕様に {pattern} の記述が無い（文言を変えたらこのテストを直す）"
        assert all(int(n) == expected for n in found), (pattern, found, expected)


def test_spec_value_vocabularies_match_code() -> None:
    """`compare.cmp` / 単項 / `select.pick` / `aggregate.func` の語彙が、順序も含めて一致すること。"""
    assert tuple(_marked_list("compare-ops")) == COMPARE_OPS
    assert tuple(_marked_list("unary-compare-ops")) == UNARY_COMPARE_OPS
    assert tuple(_marked_list("select-picks")) == SELECT_PICKS
    assert tuple(_marked_list("aggregate-funcs")) == AGGREGATE_FUNCS


def test_spec_sql_forbidden_words_match_code() -> None:
    """§6.3 の SQL の禁止語と関数呼び出しの形が `_SQL_FORBIDDEN` と一致すること。

    `_SQL_FORBIDDEN` は「境界 + (語|語|...) + 境界」と「境界 + 関数呼び出しの形」の2つの選択肢で
    できている。語の末尾の `\\w*` は仕様では `*`、`\\w+` は `+` で書く。
    """
    pattern = _SQL_FORBIDDEN.pattern
    head = _SQL_WORD_START + "("
    tail = ")" + _SQL_WORD_END + "|" + _SQL_WORD_START
    assert pattern.startswith(head) and tail in pattern, (
        "_SQL_FORBIDDEN の形が変わった。このテストを直す"
    )
    words_part, call_form = pattern[len(head) :].split(tail, 1)
    code_words = [w.replace(r"\w*", "*").replace(r"\w+", "+") for w in words_part.split("|")]
    assert all(re.fullmatch(r"[a-z_]+[*+]?", w) for w in code_words), code_words
    spec_words = " ".join(_marked_list("sql-forbidden")).split()
    assert sorted(spec_words) == sorted(code_words)
    (spec_call,) = _marked("dsl-value", "sql-forbidden-call")
    assert spec_call.strip("`") == call_form


def test_spec_max_steps_matches_code() -> None:
    """step 数の上限（§3.1、V-02）が `_MAX_STEPS` と一致すること。"""
    values = _marked("dsl-value", "max-steps")
    assert len(values) >= 2
    assert all(v == str(_MAX_STEPS) for v in values), values


def test_spec_llm_options_match_code() -> None:
    """§5.6 の LLM の固定オプションが `executor._LLM_OPTIONS` と一致すること。"""
    options: dict[str, Any] = {}
    for item in _marked_list("llm-options"):
        key, _, value = item.partition(": ")
        assert key not in options, key
        options[key] = json.loads(value)
    assert options == _LLM_OPTIONS


# --------------------------------------------------------------------------- #
# 検証のエラーメッセージ（§9.1 の「これで全部である」の裏付け）
# --------------------------------------------------------------------------- #

_VALIDATORS = ("validate_program", "_validate_params")


def _template(node: ast.expr) -> str:
    """エラーメッセージの式を雛形にする（f-string の差し込みは `{}`）。"""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
    raise AssertionError(
        f"エラーメッセージが文字列リテラルでない（{ast.unparse(node)}）。"
        "§9.1.1 と照合できないので、雛形を文字列リテラルで書く"
    )


def _code_error_templates() -> list[str]:
    """`validate_program` / `_validate_params` が返しうるエラーの雛形を構文木から全て取り出す。

    取りこぼしが無いことを、次の形に閉じていることで確かめる。

    - `errors` への追加は `errors.append(<文字列>)` と `errors.extend(param_errors)` だけ
    - `return` は `errors`、文字列リテラルのリスト、またはそれらを先頭に持つタプルだけ
    """
    tree = ast.parse(inspect.getsource(dsl))
    funcs = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name in _VALIDATORS]
    assert {f.name for f in funcs} == set(_VALIDATORS)
    templates: list[str] = []
    for func in funcs:
        for node in ast.walk(func):
            if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
                assert node.target.id != "errors", f"{func.name}: errors への += は照合できない"
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "errors"
            ):
                if node.func.attr == "append":
                    templates.append(_template(node.args[0]))
                else:
                    assert node.func.attr == "extend", ast.unparse(node)
                    assert ast.unparse(node) == "errors.extend(param_errors)", ast.unparse(node)
            if isinstance(node, ast.Return) and node.value is not None:
                value = node.value
                if isinstance(value, ast.Tuple):
                    value = value.elts[0]
                if isinstance(value, ast.List):
                    templates.extend(_template(e) for e in value.elts)
                else:
                    assert isinstance(value, ast.Name), ast.unparse(node)
                    assert value.id in ("errors", "param_errors"), ast.unparse(node)
    return templates


def _spec_error_templates() -> list[tuple[list[str], str]]:
    """§9.1.1 の一覧を [(検査の番号の列, 雛形)] で返す。"""
    m = re.search(
        r"<!-- dsl-validation-messages:begin -->\n```text\n(.*?)\n```\n"
        r"<!-- dsl-validation-messages:end -->",
        _spec(),
        re.DOTALL,
    )
    assert m, "仕様にエラーメッセージの一覧の印が無い"
    rows: list[tuple[list[str], str]] = []
    for line in m.group(1).splitlines():
        lm = re.fullmatch(r"(V-\d\d(?:,V-\d\d)*) (.+)", line)
        assert lm, f"一覧の行の形が違う: {line!r}"
        rows.append((lm.group(1).split(","), lm.group(2)))
    return rows


def test_spec_validation_messages_match_code() -> None:
    """§9.1.1 の雛形の集合が、コードが返しうるエラーの雛形の集合と一致すること。"""
    code = _code_error_templates()
    spec = [t for _, t in _spec_error_templates()]
    assert len(set(spec)) == len(spec), "一覧に同じ雛形が2回ある"
    assert set(code) == set(spec), (
        f"コードにだけある: {sorted(set(code) - set(spec))}\n"
        f"仕様にだけある: {sorted(set(spec) - set(code))}"
    )


def test_spec_validation_message_ids_match_check_list() -> None:
    """§9.1.1 の番号が §9.1 の検査の番号と過不足なく対応すること。"""
    text = _spec()
    section = text.split("### 9.1 `validate_program` の検査")[1].split("#### 9.1.1")[0]
    defined = re.findall(r"^- \*\*(V-\d\d)\*\*", section, re.MULTILINE)
    assert len(defined) == len(set(defined)), defined
    used = {vid for ids, _ in _spec_error_templates() for vid in ids}
    assert used == set(defined)


# --------------------------------------------------------------------------- #
# transform の失敗の扱い（§5.10、付録 A）
# --------------------------------------------------------------------------- #

_TRANSFORM_PASSTHROUGH = [
    ("x", {"type": "unit_conversion", "formula": "value * 2"}),  # 数値にできない入力
    (5, {"type": "unit_conversion", "formula": "value ** 2"}),  # 冪乗
    (5, {"type": "unit_conversion", "formula": "abs(value)"}),  # 関数呼び出し
    (5, {"type": "unit_conversion", "formula": "x + value"}),  # value 以外の名前
    (5, {"type": "unit_conversion", "formula": "1e308 * 10"}),  # 非有限
    (
        5,
        {"type": "unit_conversion", "formula": "value * 2", "decimals": 1.5},
    ),  # 整数でない decimals
    (5, {"type": "unknown"}),  # 未知の type
    (5, {"type": "code_map"}),  # map の省略
    (5, {}),  # 空の rule
]

_TRANSFORM_FAILURES = [
    {"type": "unit_conversion", "formula": "value +"},  # 構文の誤り
    {"type": "unit_conversion"},  # formula の省略（空の式）
    {"type": "unit_conversion", "formula": "value / 0"},  # 0 除算
    {"type": "unit_conversion", "formula": "value % 0"},  # 0 の剰余
    {"type": "unit_conversion", "formula": 3},  # 文字列でない formula
    "abc",  # 辞書でない rule
    ["a"],  # 辞書でない rule
    {"type": "code_map", "map": ["a"]},  # 辞書でない map
]


@pytest.mark.parametrize(("value", "rule"), _TRANSFORM_PASSTHROUGH)
def test_transform_passthrough_cases_return_input(value: Any, rule: Any) -> None:
    """§5.10 の「input をそのまま返す」場合。"""
    assert apply_transform(value, rule) == value


@pytest.mark.parametrize("rule", _TRANSFORM_FAILURES)
async def test_transform_failure_cases_fail_the_step_as_unclassified(rule: Any) -> None:
    """§5.10 の「step が unclassified で失敗する」場合。検証は通る。"""
    program = {
        "steps": [{"op": "transform", "id": "a", "input": 5, "rule": rule}],
        "return": "$a",
    }
    assert validate_program(program) == []
    value, trace = await DSLInterpreter()._run_program(
        program,
        "REG-1",
        None,
        {},
        None,
        {},
        {"study_id": ""},  # type: ignore[arg-type]
    )
    assert value is None
    assert trace[0]["failure_code"] == FailureCode.UNCLASSIFIED
