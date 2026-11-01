"""ステップ3/4 DSL のユニット・統合テスト（Ollama 不要・決定的）

- dsl.validate_program / is_select_only_sql / safe_arith
- schemas.DSLProgram の return エイリアス往復
- executor.resolve_fhir_path の対応記法と限界（FHIRPath ではない）
- viz.program_to_mermaid
- executor.DSLInterpreter の各 op 実行（in-memory sqlite 使用）
- select の where / order が非実行ヒントであること
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.dsl import (
    has_needs_input,
    is_select_only_sql,
    safe_arith,
    sql_bind_names,
    validate_program,
)
from kawasemi.ai_input.executor import (
    DSLInterpreter,
    _apply_compare,
    apply_transform,
    resolve_fhir_path,
)
from kawasemi.ai_input.models import InputPlan
from kawasemi.ai_input.schemas import DSLProgram
from kawasemi.ai_input.viz import program_to_mermaid

# --------------------------------------------------------------------------- #
# SQL 安全性
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "sql,ok",
    [
        ("SELECT value FROM labs WHERE patient_id = :patient_id", True),
        ("  with x as (select 1) select * from x  ", True),
        ("DELETE FROM labs", False),
        ("SELECT 1; DROP TABLE labs", False),
        ("UPDATE labs SET v=1", False),
        ("INSERT INTO labs VALUES (1)", False),
        ("", False),
    ],
)
def test_is_select_only_sql(sql: str, ok: bool) -> None:
    assert is_select_only_sql(sql) is ok


# 文字列リテラルの中の `--` / `/*` から後ろをコメントとして消していたため、禁止語・複文・
# `U&` の検査の前に本体が見えなくなっていた（レビューの再現。PostgreSQL 17 の開発用
# フォールバックで dblink_exec が実際に書けた）
_COMMENT_IN_LITERAL_BYPASSES = [
    "SELECT '--', dblink_exec('host=x', 'INSERT INTO t VALUES (1)')",
    "SELECT '--', pg_sleep(100)",
    "SELECT '--'; DROP TABLE users",
    "SELECT x FROM t WHERE a='--' UNION SELECT password_hash FROM users",
    "SELECT '--' AS x, dblink_exec('host=x', 'INSERT ...') WHERE :patient_id = :patient_id",
    # `/*` がリテラルの中にあると、以前は後ろの `*/` までが消えた
    "SELECT '/*', pg_sleep(1) -- */",
    "SELECT '/*' AS a, dblink_exec('x', 'y') /* */",
    # ドル引用の本体の `--` はコメントではない
    "SELECT $$--$$, pg_sleep(1)",
    "SELECT $q$ -- $q$, pg_sleep(1)",
    # 識別子の中のコメント記号
    'SELECT "--", pg_sleep(1) FROM t',
    'SELECT "/*", pg_sleep(1) FROM t -- */',
    "SELECT `--`, pg_sleep(1) FROM t",
    # E 文字列の `\'` で閉じたと見なすと、後ろの `--` がコメントになる
    "SELECT E'\\'--', pg_sleep(1)",
    "SELECT E'\\'' -- ', pg_sleep(1)",
    # 引用の中の `U&`
    "SELECT '--', U&\"\\0064blink_exec\"('x', 'y')",
    # PostgreSQL は `--` コメントを `\r` でも終える（2回目のレビューの再現。PG 17 で書けた）
    "SELECT 1 --\r, dblink_exec('host=x', 'INSERT INTO t VALUES (1)')\n",
    "SELECT 'x' --\rUNION SELECT rolname FROM pg_authid\n",
    # PostgreSQL の入れ子の開き `/*/` は閉じ `*/` と重なる（同上）
    "SELECT 1 /* /*/ -- */ */ , dblink_exec('host=x', 'INSERT INTO t VALUES (1)')\n",
    "SELECT 1 /* /*/ -- */ */ , pg_sleep(2)\n",
    "SELECT 'x' /* /*/ -- */ */ UNION SELECT rolpassword FROM pg_authid\n",
]


@pytest.mark.parametrize("sql", _COMMENT_IN_LITERAL_BYPASSES)
def test_comment_markers_inside_literals_do_not_hide_the_rest(sql: str) -> None:
    assert is_select_only_sql(sql) is False
    prog = {"steps": [{"op": "sql_extract", "id": "s", "sql": sql}], "return": "$s"}
    assert any("SELECT" in e for e in validate_program(prog))
    prog["steps"][0]["op"] = "external_db_extract"
    prog["steps"][0]["connection_id"] = "c"
    assert any("SELECT" in e for e in validate_program(prog))


@pytest.mark.parametrize(
    "sql",
    [
        # 閉じていない引用・コメントは、境界が決まらないので拒否する
        "SELECT 'abc FROM t",
        'SELECT "abc FROM t',
        "SELECT $$abc FROM t",
        "SELECT 1 /* c",
        "SELECT a[1 FROM t",
        # バックスラッシュでエスケープした引用符は、方言（SQLite、
        # standard_conforming_strings=off）で終端の位置が変わる。`''` で書けるので拒否する
        "SELECT E'it\\'s' AS x",
        "SELECT 'a\\' AS x",
        # 入れ子のブロックコメント（SQLite は最初の `*/` で閉じる）
        "SELECT /* /* */ pg_sleep(1) */ 1",
        "SELECT /* /* */ 1 */ 1",
        # MySQL の実行されるコメント
        "SELECT 1 /*! , pg_sleep(1) */",
        # SQLite の [識別子] の中のコメント記号
        "SELECT [--], pg_sleep(1)",
        "SELECT [--] FROM t",
        # ドル引用の本体は SQLite ではコード。引用符・; を含むものは読みが割れる
        "SELECT $a$ ' $a$ -- ', pg_sleep(1)",
        "SELECT $a$ ; $a$",
        # コメントは引用の外では一切拒否する
        "SELECT 1; -- trailing comment",
        "SELECT 1; /* c */",
        "SELECT 1 */",
        "SELECT v -- コメント 'x\nFROM labs /* 'y */ WHERE patient_id = :patient_id",
        "SELECT 1 +--x\n 1",
        "SELECT '--'/*'*/, 1",
        # ドル引用・[…] の中のコメント記号（SQLite ではコード）
        "SELECT $$--$$ AS x",
        "SELECT $tag$ /* $tag$ AS x",
        "SELECT [a--b] FROM t",
        # SQLite は [a'] を識別子と読むが、ここで引用と読むと間の ; が隠れる
        "SELECT [a'] ; SELECT 1 ; ['] FROM t",
        # `]` を含む文字列を [ ] の中に書くと、SQLite の読みでは識別子が `[']` で閉じ、
        # 残りが閉じない文字列になる（両方の読みで通らないので拒否）
        "SELECT ARRAY[']'] AS x",
        # 引用の外の制御文字・空白類（単独の \r、\f、\v、NUL、U+2028/2029、NBSP、ゼロ幅、BOM）
        "SELECT 1\r, 2",
        "SELECT 1\f, 2",
        "SELECT 1\v, 2",
        "SELECT 1\x00, 2",
        "SELECT 1\u2028, 2",
        "SELECT 1\u2029, 2",
        "SELECT\u00a01",
        "SELECT 1\u3000, 2",
        "SELECT pg_sl\u200beep(1)",
        "\ufeffSELECT 1",
        # ESCAPE '\' は standard_conforming_strings=off で終端がずれる
        "SELECT v FROM t WHERE a LIKE 'x\\_%' ESCAPE '\\'",
        # 文末のセミコロンは1つだけ
        "SELECT 1;;",
        "SELECT 1;;;",
        "SELECT 1; ;",
        "SELECT 1; SELECT 2",
        "SELECT 1; -- c\n;",
        ";",
        "-- only a comment",
    ],
)
def test_ambiguous_or_unterminated_sql_is_rejected(sql: str) -> None:
    assert is_select_only_sql(sql) is False


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1;",
        "SELECT 1 ; ",
        "SELECT 1;\n",
        "SELECT '--' AS x FROM t WHERE patient_id = :patient_id",
        "SELECT '/*' AS x FROM t",
        "SELECT 'it''s' AS x",
        "SELECT 'a;b' AS x",
        "SELECT E'a\\nb' AS x",
        "SELECT E'a\\\\' AS x",
        'SELECT "--" FROM t',
        'SELECT "a""b" FROM t',
        "SELECT a[1] FROM t",
        "SELECT x::int FROM t WHERE c = $1",
        "SELECT a$b$ FROM t",
        "SELECT $tag$ abc $tag$ AS x",
        # Web 画面の textarea は CRLF を送る（保存された SQL に入る）
        "SELECT v\r\nFROM t WHERE pid = :patient_id",
        "SELECT v\n\tFROM t",
        # 引用の中はデータ（コメント記号・制御文字・全角空白も許す）
        "SELECT 'a\rb\u3000c' AS x",
        "SELECT v FROM t WHERE a LIKE 'x!_%' ESCAPE '!'",
        # PostgreSQL の配列の構築子・添字（SQLite の読みでは [ … ] が識別子になる）
        "SELECT v FROM t WHERE status = ANY(ARRAY['final','amended'])",
        "SELECT (ARRAY['a','b'])[1]",
        "SELECT a[1][2], b[i:j] FROM t",
        "SELECT (string_to_array(v, ','))[2] FROM t",
        # 数字を含む識別子・語の一部は禁止語ではない
        "SELECT updated_at, created_by, t1.v, x.lo FROM t1 JOIN x ON t1.id = x.id",
        "SELECT v2 FROM t WHERE c IN (1, 2)",
    ],
)
def test_legitimate_sql_still_passes(sql: str) -> None:
    assert is_select_only_sql(sql) is True


def test_bind_names_are_counted_like_sqlalchemy_text() -> None:
    """コメントは拒否するので、バインド名は SQLAlchemy の `text()` と同じく素の文字列で数える
    （コメントの中の `:name` を片方だけが拾う食い違いが無い）。"""
    sql = "SELECT v FROM t WHERE pid = :patient_id -- :hidden"
    assert sql_bind_names(sql) == {"patient_id", "hidden"} == set(text(sql).compile().params)
    assert is_select_only_sql(sql) is False


@pytest.mark.parametrize(
    "sql",
    [
        # 直前の数字が境界を消していた（PostgreSQL 14 以前は `1union` を `1` と `union` に分ける）
        "SELECT 1union SELECT password_hash FROM users",
        "SELECT 1.5union SELECT 1",
        "SELECT 1e5union SELECT 1",
        "SELECT x FROM t WHERE a = $1union SELECT 1",
        "SELECT 1u&'\\0064'",
        # 読み取り専用でも状態を変える・サーバの外へ手が届く関数
        "SELECT pg_create_physical_replication_slot('s')",
        "SELECT pg_create_logical_replication_slot('s', 'test_decoding')",
        "SELECT pg_drop_replication_slot('s')",
        "SELECT pg_logical_emit_message(true, 'p', 'm')",
        "SELECT setval('seq', 1)",
        "SELECT nextval('seq')",
        "SELECT pg_reload_conf()",
        "SELECT pg_switch_wal()",
        "SELECT pg_stat_reset()",
        "SELECT pg_stat_reset_shared('bgwriter')",
        "SELECT pg_ls_dir('.')",
        "SELECT pg_ls_waldir()",
        "SELECT pg_stat_file('postgresql.conf')",
        "SELECT pg_read_file('postgresql.conf')",
        "SELECT pg_read_binary_file('postgresql.conf')",
        "SELECT lo_import('/etc/passwd')",
        "SELECT lo_from_bytea(0, 'x')",
        "SELECT lo_unlink(1)",
        "SELECT 1nextval('seq')",
    ],
)
def test_forbidden_words_with_numeric_prefix_and_state_changing_functions(sql: str) -> None:
    assert is_select_only_sql(sql) is False


def test_forbidden_words_inside_literals_are_still_rejected() -> None:
    """リテラルの中身も禁止語の検査に含める（未知の「文字列を実行する関数」への網）。

    `WHERE status = 'delete'` は誤検知になるが、以前からの挙動で、フェイルクローズ側。
    """
    assert is_select_only_sql("SELECT x FROM t WHERE status = 'delete'") is False
    assert is_select_only_sql("SELECT some_ext_exec('INSERT INTO t VALUES (1)')") is False


# --------------------------------------------------------------------------- #
# 安全な算術評価
# --------------------------------------------------------------------------- #


def test_safe_arith_basic() -> None:
    assert safe_arith("value/18.01", 180.1) == pytest.approx(10.0)
    assert safe_arith("(value + 2) * 3", 1) == pytest.approx(9.0)


def test_safe_arith_rejects_calls() -> None:
    with pytest.raises(ValueError):
        safe_arith("__import__('os').system('x')", 1)
    with pytest.raises(ValueError):
        safe_arith("value.bit_length()", 1)


# --------------------------------------------------------------------------- #
# プログラム検証
# --------------------------------------------------------------------------- #


def _good_program() -> dict:
    return {
        "steps": [
            {
                "op": "sql_extract",
                "id": "hb",
                "sql": "SELECT v FROM labs WHERE patient_id = :patient_id",
            },
            {"op": "compare", "id": "low", "left": "$hb", "cmp": "<", "right": 8.0},
            {"op": "branch", "id": "g", "when": "$low", "then": "$hb", "else_branch": None},
        ],
        "return": "$g",
    }


def test_validate_good_program() -> None:
    assert validate_program(_good_program()) == []


def test_validate_unknown_op() -> None:
    prog = {"steps": [{"op": "evil", "id": "x"}], "return": "$x"}
    assert any("未知の op" in e for e in validate_program(prog))


def test_validate_dangling_reference() -> None:
    prog = {
        "steps": [{"op": "compare", "id": "c", "left": "$missing", "cmp": "==", "right": 1}],
        "return": "$c",
    }
    assert any("未定義" in e for e in validate_program(prog))


def test_validate_forward_reference_rejected() -> None:
    # b は後方の a を参照しているのでエラー
    prog = {
        "steps": [
            {"op": "compare", "id": "b", "left": "$a", "cmp": "==", "right": 1},
            {"op": "sql_extract", "id": "a", "sql": "SELECT 1"},
        ],
        "return": "$b",
    }
    assert any("$a" in e for e in validate_program(prog))


def test_validate_bad_sql() -> None:
    prog = {"steps": [{"op": "sql_extract", "id": "x", "sql": "DELETE FROM labs"}], "return": "$x"}
    assert any("SELECT" in e for e in validate_program(prog))


def test_validate_select_pick_allowed_values() -> None:
    # 未指定/first/last は妥当。綴り違いは黙って first に化けさせず検証で弾く
    def prog(pick: object) -> dict:
        step = {"op": "select", "id": "s", "source": "$rows"}
        if pick is not None:
            step["pick"] = pick
        return {
            "steps": [{"op": "sql_extract", "id": "rows", "multi": True, "sql": "SELECT 1"}, step],
            "return": "$s",
        }

    assert validate_program(prog(None)) == []
    assert validate_program(prog("first")) == []
    assert validate_program(prog("last")) == []
    assert any("select.pick" in e for e in validate_program(prog("latest")))


def test_validate_needs_input_allows_missing_fields() -> None:
    prog = {
        "steps": [{"op": "sql_extract", "id": "x", "needs_input": True, "reasoning": "表名不明"}],
        "return": "$x",
    }
    assert validate_program(prog) == []
    assert has_needs_input(prog) is True


# --------------------------------------------------------------------------- #
# `needs_input` が免除する範囲
#
# 方針は「**欠落は許すが、書いてある内容は妥当でなければならない**」。免除するのは
# `_REQUIRED_FIELDS` の充足検査ただ1つで、他の検査は `needs_input` に関わらず走る。
# 以前は値域と csv の構造検査も丸ごと免除しており、**旗を立てるだけで検査を回避できた**。
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "step,expected",
    [
        pytest.param(
            {"op": "compare", "id": "x", "left": "$a", "cmp": "bogus", "right": 1},
            "compare.cmp",
            id="compare.cmp",
        ),
        pytest.param(
            {"op": "select", "id": "x", "source": "$a", "pick": "latest"},
            "select.pick",
            id="select.pick",
        ),
        pytest.param(
            {"op": "aggregate", "id": "x", "source": "$a", "func": "median"},
            "aggregate.func",
            id="aggregate.func",
        ),
        pytest.param(
            {"op": "csv_extract", "id": "x", "column": "c", "delimiter": ";;"},
            "delimiter",
            id="csv.delimiter",
        ),
        pytest.param(
            {"op": "csv_extract", "id": "x", "column": {"bad": 1}},
            "列指定",
            id="csv.column-type",
        ),
        pytest.param(
            {"op": "csv_extract", "id": "x", "column": "{{tbl}}"},
            "列指定に",
            id="csv.column-placeholder",
        ),
        pytest.param(
            {"op": "csv_extract", "id": "x", "column": "c", "match": ["文字列"]},
            "match 要素は辞書",
            id="csv.match-shape",
        ),
        # 空文字の column は `None` と同じに扱う。`is None` で見ていると、空文字が
        # `col_specs` へ流れて `_is_unset` に飛ばされ、**header=false の整数index規則を
        # 素通りして確定できてしまう**（実行時に `col_index("")` が ValueError →
        # schema_mismatch＝所在同定の失敗として数えられる）。
        pytest.param(
            {
                "op": "csv_extract",
                "id": "x",
                "column": 0,
                "header": False,
                "match": [{"column": "", "value": "v"}],
            },
            "match 要素に column が必要です",
            id="csv.match-column-empty",
        ),
    ],
)
def test_needs_input_does_not_exempt_written_but_invalid_content(step, expected) -> None:
    """旗を立てても、**書いてある内容**の検査は素通りしないこと。

    綴り違いを通すと黙って別の意味になる（`pick="latest"` は executor で first に化ける）。
    `needs_input` は書き手が立てる旗であってサーバ側の保証ではないので、
    「旗を立てれば検査を回避できる」形にしてはいけない。
    """
    prog = {"steps": [{"op": "sql_extract", "id": "a"}, {**step, "needs_input": True}]}
    errors = validate_program(prog)
    assert any(expected in e for e in errors), errors
    # 同じ内容は needs_input=false でも当然弾かれる（旗の有無で答えが変わらない）
    prog_confirmed = {"steps": [{"op": "sql_extract", "id": "a", "sql": "SELECT 1"}, step]}
    assert any(expected in e for e in validate_program(prog_confirmed))


@pytest.mark.parametrize(
    "step",
    [
        pytest.param({"op": "compare", "id": "x"}, id="compare-all-missing"),
        pytest.param({"op": "select", "id": "x"}, id="select-all-missing"),
        pytest.param({"op": "aggregate", "id": "x"}, id="aggregate-all-missing"),
        pytest.param({"op": "csv_extract", "id": "x"}, id="csv-all-missing"),
        # 空文字も「まだ書いていない」として扱う（`_is_unset`）
        pytest.param({"op": "compare", "id": "x", "cmp": ""}, id="compare-empty-cmp"),
        pytest.param({"op": "aggregate", "id": "x", "func": ""}, id="aggregate-empty-func"),
    ],
)
def test_needs_input_still_exempts_missing_fields(step) -> None:
    """**欠落は許す。** 値域・構造の検査は書かれているときだけ発火する。"""
    prog = {"steps": [{**step, "needs_input": True, "reasoning": "未確定"}]}
    assert validate_program(prog) == []


@pytest.mark.parametrize(
    "step,expected",
    [
        pytest.param(
            {"op": "aggregate", "id": "x", "source": "$a", "func": 0},
            "aggregate.func",
            id="aggregate.func-zero",
        ),
        pytest.param(
            {"op": "select", "id": "x", "source": "$a", "pick": False},
            "select.pick",
            id="select.pick-false",
        ),
        pytest.param(
            {"op": "csv_extract", "id": "x", "column": False},
            "列指定",
            id="csv.column-false",
        ),
        pytest.param(
            {"op": "csv_extract", "id": "x", "column": []},
            "列指定",
            id="csv.column-empty-list",
        ),
        pytest.param(
            {"op": "csv_extract", "id": "x", "column": {}},
            "列指定",
            id="csv.column-empty-dict",
        ),
    ],
)
def test_needs_input_exempts_absence_not_falsy_values(step, expected) -> None:
    """免除するのは**未設定（キー無し・`None`）と空文字だけ**であること。

    `_is_unset` は `value is None or value == ""` であって falsy 判定ではない。
    ここを `not value` に倒すと `0` `False` `[]` `{}` も「書いていない」に入り、
    **値域と型の検査が黙って素通りする**（`func=0` や `column=False` を確定できてしまう）。
    `_is_unset` の docstring はこの区別を主張しているので、主張を測る側をここに置く。

    この検査が無いと、`_is_unset` を素朴な falsy 判定へ書き換える1行の変更が
    フルスイートを全通しで抜ける（実測: 2,334件すべて緑のまま28形が新たに通る）。
    """
    prog = {"steps": [{"op": "sql_extract", "id": "a"}, {**step, "needs_input": True}]}
    assert any(expected in e for e in validate_program(prog)), validate_program(prog)


def test_needs_input_does_not_exempt_the_parameter_declaration_net() -> None:
    """パラメータ宣言の網は免除しない（旗で検査を回避させない）。

    実行時に何を読むかを縛る検査なので、ここを免除すると `needs_input` を立てるだけで
    未宣言のパラメータ参照や `allowed` 無しの表名切替が保存できてしまう。
    """
    unknown_param = {
        "steps": [
            {
                "op": "sql_extract",
                "id": "x",
                "sql": "SELECT * FROM t WHERE c = :nope",
                "needs_input": True,
            }
        ]
    }
    assert any("nope" in e for e in validate_program(unknown_param))

    identifier_without_allowed = {
        "params": [{"name": "tbl", "kind": "identifier", "value": "T"}],
        "steps": [
            {"op": "sql_extract", "id": "x", "sql": "SELECT * FROM {{tbl}}", "needs_input": True}
        ],
    }
    assert any("allowed" in e for e in validate_program(identifier_without_allowed))


# --------------------------------------------------------------------------- #
# スキーマ往復（return エイリアス）
# --------------------------------------------------------------------------- #


def test_dslprogram_return_alias_roundtrip() -> None:
    prog = DSLProgram(**{"steps": [{"op": "transform", "id": "v"}], "return": "$v"})
    assert prog.return_ == "$v"
    dumped = prog.model_dump(by_alias=True, exclude_none=True)
    assert dumped["return"] == "$v"
    # populate_by_name でも受理
    prog2 = DSLProgram(steps=[], return_="$z")
    assert prog2.model_dump(by_alias=True)["return"] == "$z"


# --------------------------------------------------------------------------- #
# compare / transform ヘルパ
# --------------------------------------------------------------------------- #


def test_apply_compare_numeric_and_membership() -> None:
    assert _apply_compare(7.2, "<", 8.0) is True
    assert _apply_compare("10", ">=", 10) is True
    assert _apply_compare("a", "in", ["a", "b"]) is True
    assert _apply_compare("abc", "contains", "b") is True
    assert _apply_compare(3, "!=", 4) is True


def test_apply_transform_code_map_and_units() -> None:
    assert apply_transform("M", {"type": "code_map", "map": {"M": "男性"}}) == "男性"
    assert (
        apply_transform(180.1, {"type": "unit_conversion", "formula": "value/18.01", "decimals": 2})
        == 10.0
    )


# --------------------------------------------------------------------------- #
# fhir_path（FHIRPath ではなく簡易ドット記法）
# --------------------------------------------------------------------------- #


def test_resolve_fhir_path_supported_notation() -> None:
    data = {"status": "final", "name": [{"text": "山田太郎"}], "valueQuantity": {"value": 12.3}}
    # 先頭の大文字始まり要素はリソース型として読み飛ばす
    assert resolve_fhir_path(data, "Observation.valueQuantity.value") == 12.3
    assert resolve_fhir_path(data, "status") == "final"
    assert resolve_fhir_path(data, "Patient.name[0].text") == "山田太郎"


@pytest.mark.parametrize(
    "path",
    [
        "Patient.name.where(use='official').text",  # 関数
        "Observation.value.ofType(Quantity).value",  # 型フィルタ
        "Observation.value as Quantity",  # 型キャスト
        "Patient.name[0].text | Patient.name[1].text",  # union
        "Patient.name[*].text",  # ワイルドカード添字
    ],
)
def test_resolve_fhir_path_ignores_fhirpath_syntax(path: str) -> None:
    """FHIRPath 構文は解釈されず None になる（実装は簡易ドット記法）"""
    data = {"name": [{"use": "official", "text": "山田太郎"}], "value": {"value": 1}}
    assert resolve_fhir_path(data, path) is None


@pytest.mark.parametrize("path", ["Observation", "Patient | Practitioner"])
def test_resolve_fhir_path_returns_whole_resource_when_nothing_to_walk(path: str) -> None:
    """大文字始まりの要素は resourceType と照合せず捨てるため、辿る要素が残らないと
    リソース全体が返る（None ではない）。docstring に明記した驚きの挙動を固定する。
    """
    data = {"resourceType": "Observation", "status": "final"}
    assert resolve_fhir_path(data, path) == data


def test_resolve_fhir_path_is_case_and_space_sensitive() -> None:
    data = {"resourceType": "Observation", "status": "final"}
    assert resolve_fhir_path(data, "observation.status") is None
    assert resolve_fhir_path(data, " Observation.status") is None


# --------------------------------------------------------------------------- #
# Mermaid 可視化
# --------------------------------------------------------------------------- #


def test_program_to_mermaid_includes_nodes_and_trace() -> None:
    out = program_to_mermaid(
        _good_program(),
        trace=[
            {"op": "sql_extract", "id": "hb", "output": 7.2},
            {"op": "branch", "id": "g", "taken": "then", "output": "Grade"},
        ],
    )
    assert "graph TD" in out
    assert "7.2" in out  # トレース値が注記される
    assert "|then|" in out  # branch のエッジ注記
    assert "result" in out  # return ノード


# --------------------------------------------------------------------------- #
# インタプリタ統合（in-memory sqlite）
# --------------------------------------------------------------------------- #


def _plan(link_id: str, program: dict) -> InputPlan:
    return InputPlan(questionnaire_id=1, link_id=link_id, program=program, status="confirmed")


@pytest.mark.asyncio
async def test_interpreter_sql_compare_branch_transform(db_session: AsyncSession) -> None:
    await db_session.execute(text("CREATE TABLE labs (patient_id TEXT, v REAL)"))
    await db_session.execute(text("INSERT INTO labs VALUES (:p, :v)"), {"p": "P1", "v": 7.2})
    await db_session.commit()

    program = {
        "steps": [
            {
                "op": "sql_extract",
                "id": "hb",
                "sql": "SELECT v FROM labs WHERE patient_id = :patient_id",
            },
            {"op": "compare", "id": "low", "left": "$hb", "cmp": "<", "right": 8.0},
            {"op": "branch", "id": "g", "when": "$low", "then": "要対応", "else_branch": "正常"},
        ],
        "return": "$g",
    }
    interp = DSLInterpreter()
    prefill, evidence = await interp.execute([_plan("item1", program)], "P1", db_session)

    assert prefill["item1"] == "要対応"
    trace = evidence["item1"]["trace"]
    assert [t["op"] for t in trace] == ["sql_extract", "compare", "branch"]
    assert trace[0]["output"] == 7.2
    assert trace[2]["taken"] == "then"


@pytest.mark.asyncio
async def test_interpreter_select_and_aggregate(db_session: AsyncSession) -> None:
    await db_session.execute(text("CREATE TABLE vitals (patient_id TEXT, v REAL)"))
    for v in (3.0, 9.0, 5.0):
        await db_session.execute(text("INSERT INTO vitals VALUES (:p, :v)"), {"p": "P1", "v": v})
    await db_session.commit()

    program = {
        "steps": [
            {
                "op": "sql_extract",
                "id": "rows",
                "multi": True,
                "sql": "SELECT v FROM vitals WHERE patient_id = :patient_id ORDER BY v",
            },
            {"op": "aggregate", "id": "mx", "source": "$rows", "func": "max"},
            {"op": "select", "id": "first", "source": "$rows", "pick": "first"},
        ],
        "return": "$mx",
    }
    interp = DSLInterpreter()
    prefill, evidence = await interp.execute([_plan("item2", program)], "P1", db_session)

    assert prefill["item2"] == 9.0
    by_id = {t["id"]: t for t in evidence["item2"]["trace"]}
    assert by_id["first"]["output"] == 3.0


@pytest.mark.asyncio
async def test_interpreter_select_ignores_where_and_order_hints(
    db_session: AsyncSession,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """select の where / order は非実行のヒント（仕様＝実装）。

    where で絞り込まれず、order でも並び替わらず、pick が抽出時点の行順に適用される。
    無視した事実は trace.inputs.hints_ignored に残り、警告ログが出る（1インスタンス1回）。
    """
    await db_session.execute(text("CREATE TABLE vitals2 (patient_id TEXT, v REAL)"))
    for v in (3.0, 9.0, 5.0):
        await db_session.execute(text("INSERT INTO vitals2 VALUES (:p, :v)"), {"p": "P1", "v": v})
    await db_session.commit()

    program = {
        "steps": [
            {
                "op": "sql_extract",
                "id": "rows",
                "multi": True,
                "sql": "SELECT v FROM vitals2 WHERE patient_id = :patient_id",
            },
            {
                "op": "select",
                "id": "s",
                "source": "$rows",
                "pick": "first",
                "where": "v > 100",  # 絞り込まれない（全行が残る）
                "order": "v DESC",  # 並び替わらない（挿入順のまま）
            },
        ],
        "return": "$s",
    }
    interp = DSLInterpreter()
    with caplog.at_level(logging.WARNING, logger="kawasemi.ai_input.executor"):
        prefill, evidence = await interp.execute([_plan("item2b", program)], "P1", db_session)
        # 2人目（バッチ相当）でも同じ警告を繰り返さない
        await interp.execute([_plan("item2b", program)], "P1", db_session)

    # where が効けば None、order が効けば 9.0 になるはず。実装は挿入順の先頭を返す。
    assert prefill["item2b"] == 3.0
    entry = {t["id"]: t for t in evidence["item2b"]["trace"]}["s"]
    assert entry["inputs"]["count"] == 3
    assert entry["inputs"]["hints_ignored"] == {"where": "v > 100", "order": "v DESC"}
    warnings = [r for r in caplog.records if "実行されないヒント" in r.getMessage()]
    assert len(warnings) == 1


@pytest.mark.asyncio
async def test_interpreter_select_without_hints_keeps_trace_clean(
    db_session: AsyncSession,
) -> None:
    # ヒントが無ければ hints_ignored キー自体を作らない（確認書に null を並べない）
    await db_session.execute(text("CREATE TABLE vitals3 (patient_id TEXT, v REAL)"))
    await db_session.execute(text("INSERT INTO vitals3 VALUES ('P1', 4.0)"))
    await db_session.commit()

    program = {
        "steps": [
            {
                "op": "sql_extract",
                "id": "rows",
                "multi": True,
                "sql": "SELECT v FROM vitals3 WHERE patient_id = :patient_id",
            },
            {"op": "select", "id": "s", "source": "$rows"},
        ],
        "return": "$s",
    }
    interp = DSLInterpreter()
    _, evidence = await interp.execute([_plan("item2c", program)], "P1", db_session)
    entry = {t["id"]: t for t in evidence["item2c"]["trace"]}["s"]
    assert "hints_ignored" not in entry["inputs"]
    assert entry["inputs"]["pick"] == "first"


@pytest.mark.asyncio
async def test_interpreter_records_error_without_aborting(db_session: AsyncSession) -> None:
    # 解決できない外部DB接続 → エラー記録、値は None（実行は中断しない）
    program = {
        "steps": [
            {
                "op": "external_db_extract",
                "id": "x",
                "sql": "SELECT 1",
                "connection_id": "no_such_conn",
            }
        ],
        "return": "$x",
    }
    interp = DSLInterpreter()
    prefill, evidence = await interp.execute([_plan("item3", program)], "P1", db_session)

    assert "item3" not in prefill  # 値が出ない
    assert "接続が解決できません" in evidence["item3"]["trace"][0]["error"]


# --------------------------------------------------------------------------- #
# 堅牢化（Stage3）の回帰テスト
# --------------------------------------------------------------------------- #


def test_safe_arith_rejects_pow() -> None:
    """冪乗（**）は DoS 回避のため拒否する"""
    with pytest.raises(ValueError):
        safe_arith("value ** 3", 2)
    with pytest.raises(ValueError):
        safe_arith("9 ** 9 ** 9", 1)


def test_safe_arith_rejects_non_finite() -> None:
    """ゼロ除算など非有限結果は拒否する"""
    with pytest.raises((ValueError, ZeroDivisionError)):
        safe_arith("value / 0", 1)


@pytest.mark.parametrize(
    "sql,ok",
    [
        # コメントで危険句を隠した複文すり抜けを防ぐ
        ("SELECT 1 -- ; DROP TABLE labs\n; DELETE FROM labs", False),
        ("SELECT 1 /* ; DROP TABLE labs */ ; UPDATE labs SET v=1", False),
        # コメントは消さずに拒否する（方言ごとのコメントの終わり方を追わない）
        ("SELECT v FROM labs -- コメント", False),
        ("/* 先頭コメント */ SELECT v FROM labs", False),
        # 追加した禁止キーワード
        ("COPY labs TO '/tmp/x'", False),
        ("CALL do_something()", False),
        ("VACUUM", False),
        ("SELECT execute('x')", False),
    ],
)
def test_is_select_only_sql_comment_and_keywords(sql: str, ok: bool) -> None:
    assert is_select_only_sql(sql) is ok


def test_validate_program_rejects_too_many_steps() -> None:
    steps = [{"op": "sql_extract", "id": f"s{i}", "sql": "SELECT 1"} for i in range(300)]
    errors = validate_program({"steps": steps, "return": "$s0"})
    assert any("上限" in e for e in errors)


@pytest.mark.asyncio
async def test_interpreter_refuses_invalid_program(db_session: AsyncSession) -> None:
    """実行時に validate_program で弾かれる不正プログラムは実行せずエラー記録する"""
    program = {"steps": [{"op": "evil_op", "id": "x"}], "return": "$x"}
    interp = DSLInterpreter()
    prefill, evidence = await interp.execute([_plan("itemX", program)], "P1", db_session)

    assert "itemX" not in prefill
    assert "プログラム検証エラー" in evidence["itemX"]["error"]
    assert evidence["itemX"]["trace"] == []


# --------------------------------------------------------------------------- #
# csv_extract（全患者横断CSVの決定的取得）＋ aggregate sum
# --------------------------------------------------------------------------- #


def test_validate_csv_extract_required_fields() -> None:
    prog = {"steps": [{"op": "csv_extract", "id": "x", "path": "a.csv"}], "return": "$x"}
    errs = validate_program(prog)
    assert any("key_column" in e for e in errs)
    assert any("column" in e for e in errs)


def test_validate_csv_extract_headerless_requires_index() -> None:
    # header=false（ヘッダ無し）で列名(str)指定はエラー
    prog = {
        "steps": [
            {
                "op": "csv_extract",
                "id": "x",
                "connection_id": "fs",
                "path": "a.csv",
                "header": False,
                "key_column": "pid",  # str はNG
                "column": 3,
            }
        ],
        "return": "$x",
    }
    assert any("整数index" in e for e in validate_program(prog))


def test_validate_csv_extract_good() -> None:
    prog = {
        "steps": [
            {
                "op": "csv_extract",
                "id": "x",
                "connection_id": "fs",
                "path": "a.csv",
                "key_column": "patient_id",
                "column": "points",
                "match": [{"column": "adm_id", "value": "A1"}],
            }
        ],
        "return": "$x",
    }
    assert validate_program(prog) == []


def test_validate_csv_extract_match_column_required() -> None:
    prog = {
        "steps": [
            {
                "op": "csv_extract",
                "id": "x",
                "connection_id": "fs",
                "path": "a.csv",
                "key_column": "pid",
                "column": "v",
                "match": [{"value": "A1"}],  # column 欠落
            }
        ],
        "return": "$x",
    }
    assert any("match 要素に column が必要" in e for e in validate_program(prog))


def test_validate_csv_extract_bad_delimiter() -> None:
    prog = {
        "steps": [
            {
                "op": "csv_extract",
                "id": "x",
                "connection_id": "fs",
                "path": "a.csv",
                "key_column": "pid",
                "column": "v",
                "delimiter": ", ",  # 2文字はNG
            }
        ],
        "return": "$x",
    }
    assert any("delimiter は1文字" in e for e in validate_program(prog))


def test_validate_aggregate_sum_allowed() -> None:
    prog = {
        "steps": [
            {"op": "sql_extract", "id": "r", "multi": True, "sql": "SELECT 1"},
            {"op": "aggregate", "id": "s", "source": "$r", "func": "sum"},
        ],
        "return": "$s",
    }
    assert validate_program(prog) == []


@pytest.mark.asyncio
async def test_interpreter_csv_composite_where_and_sum(
    db_session: AsyncSession, fs_root: Path
) -> None:
    """ヘッダ付きCSVを複合等値WHERE(患者ID＋adm_id＋code)で絞り → aggregate sum。"""
    csv_path = fs_root / "ef.csv"
    csv_path.write_text(
        "patient_id,adm_id,code,points\n"
        "P1,A1,150,100\n"
        "P1,A1,150,200\n"
        "P1,A1,999,50\n"  # code 不一致 → 除外
        "P1,A2,150,300\n"  # adm 不一致 → 除外
        "P2,A1,150,400\n",  # 患者 不一致 → 除外
        encoding="utf-8",
    )
    program = {
        "steps": [
            {
                "op": "csv_extract",
                "id": "rows",
                "connection_id": "test_fs",
                "path": csv_path.name,
                "key_column": "patient_id",
                "column": "points",
                "match": [
                    {"column": "adm_id", "value": "A1"},
                    {"column": "code", "value": "150"},
                ],
                "multi": True,
            },
            {"op": "aggregate", "id": "total", "source": "$rows", "func": "sum"},
        ],
        "return": "$total",
    }
    interp = DSLInterpreter()
    prefill, evidence = await interp.execute([_plan("ef", program)], "P1", db_session)

    assert prefill["ef"] == 300.0
    by_id = {t["id"]: t for t in evidence["ef"]["trace"]}
    assert by_id["rows"]["output"] == ["100", "200"]
    assert by_id["rows"]["matched_count"] == 2
    # 生の患者キーは evidence に残さずマスクされる（_DATA_OPS のキー解決経路）
    assert by_id["rows"]["inputs"].get("patient_id") is None


@pytest.mark.asyncio
async def test_interpreter_csv_strict_key_match(db_session: AsyncSession, fs_root: Path) -> None:
    """患者ID突合は厳密文字列等値。"00123" は "123" と誤マッチしない。"""
    csv_path = fs_root / "z.csv"
    csv_path.write_text("patient_id,v\n00123,10\n123,20\n", encoding="utf-8")
    program = {
        "steps": [
            {
                "op": "csv_extract",
                "id": "r",
                "connection_id": "test_fs",
                "path": csv_path.name,
                "key_column": "patient_id",
                "column": "v",
                "multi": True,
            }
        ],
        "return": "$r",
    }
    interp = DSLInterpreter()
    prefill, evidence = await interp.execute([_plan("z", program)], "123", db_session)

    # "123" の行のみ。"00123" は数値的には等しいが文字列としては不一致＝除外
    assert prefill["z"] == ["20"]
    assert evidence["z"]["trace"][0]["matched_count"] == 1


@pytest.mark.asyncio
async def test_interpreter_csv_headerless_index_and_tab(
    db_session: AsyncSession, fs_root: Path
) -> None:
    """ヘッダ無し・タブ区切りCSVを列indexで指定して読む（DPCのEF/D相当）。"""
    csv_path = fs_root / "ef.tsv"
    csv_path.write_text(
        "P1\tA1\t150\t100\nP1\tA1\t150\t200\nP2\tA1\t150\t300\n",
        encoding="utf-8",
    )
    program = {
        "steps": [
            {
                "op": "csv_extract",
                "id": "rows",
                "connection_id": "test_fs",
                "path": csv_path.name,
                "header": False,
                "delimiter": "\t",
                "key_column": 0,
                "column": 3,
                "match": [{"column": 1, "value": "A1"}, {"column": 2, "value": "150"}],
                "multi": True,
            },
            {"op": "aggregate", "id": "total", "source": "$rows", "func": "sum"},
        ],
        "return": "$total",
    }
    interp = DSLInterpreter()
    prefill, evidence = await interp.execute([_plan("ef", program)], "P1", db_session)

    assert prefill["ef"] == 300.0
    assert evidence["ef"]["trace"][0]["output"] == ["100", "200"]


@pytest.mark.asyncio
async def test_interpreter_csv_bom_and_multi_false(db_session: AsyncSession, fs_root: Path) -> None:
    """BOM付きUTF-8でも列名が壊れず一致。multi=false の複数一致は先頭採用＋件数記録。"""
    csv_path = fs_root / "bom.csv"
    csv_path.write_text("patient_id,v\nP1,first\nP1,second\n", encoding="utf-8-sig")
    program = {
        "steps": [
            {
                "op": "csv_extract",
                "id": "r",
                "connection_id": "test_fs",
                "path": csv_path.name,
                "key_column": "patient_id",  # BOM が剥がれていないと列名不一致になる
                "column": "v",
            }
        ],
        "return": "$r",
    }
    interp = DSLInterpreter()
    prefill, evidence = await interp.execute([_plan("b", program)], "P1", db_session)

    assert prefill["b"] == "first"  # multi=false → 先頭
    assert evidence["b"]["trace"][0]["matched_count"] == 2
    assert "複数行" in evidence["b"]["trace"][0]["warning"]  # 絞り不足の注記


@pytest.mark.asyncio
async def test_interpreter_csv_ref_in_match_value(db_session: AsyncSession, fs_root: Path) -> None:
    """match の value が前stepの $id を参照できる（複合キーを動的に与える）。"""
    csv_path = fs_root / "m.csv"
    csv_path.write_text("patient_id,adm_id,v\nP1,A1,10\nP1,A2,20\n", encoding="utf-8")
    program = {
        "steps": [
            {
                "op": "transform",
                "id": "adm",
                "input": "raw",
                "rule": {"type": "code_map", "map": {"raw": "A2"}},
            },
            {
                "op": "csv_extract",
                "id": "r",
                "connection_id": "test_fs",
                "path": csv_path.name,
                "key_column": "patient_id",
                "column": "v",
                "match": [{"column": "adm_id", "value": "$adm"}],
            },
        ],
        "return": "$r",
    }
    interp = DSLInterpreter()
    prefill, _ = await interp.execute([_plan("m", program)], "P1", db_session)

    assert prefill["m"] == "20"


@pytest.mark.asyncio
async def test_interpreter_csv_duplicate_header_rejected(
    db_session: AsyncSession, fs_root: Path
) -> None:
    """同名ヘッダの列名指定は後勝ちで黙読みせず明示エラーにする。"""
    csv_path = fs_root / "dup.csv"
    csv_path.write_text("patient_id,v,v\nP1,10,20\n", encoding="utf-8")
    program = {
        "steps": [
            {
                "op": "csv_extract",
                "id": "r",
                "connection_id": "test_fs",
                "path": csv_path.name,
                "key_column": "patient_id",
                "column": "v",  # 重複列名
            }
        ],
        "return": "$r",
    }
    interp = DSLInterpreter()
    prefill, evidence = await interp.execute([_plan("d", program)], "P1", db_session)

    assert "d" not in prefill
    assert "重複" in evidence["d"]["trace"][0]["error"]


@pytest.mark.asyncio
async def test_interpreter_csv_missing_file_records_error(
    db_session: AsyncSession, fs_root: Path
) -> None:
    program = {
        "steps": [
            {
                "op": "csv_extract",
                "id": "r",
                "connection_id": "test_fs",
                "path": "nope.csv",
                "key_column": "patient_id",
                "column": "v",
            }
        ],
        "return": "$r",
    }
    interp = DSLInterpreter()
    prefill, evidence = await interp.execute([_plan("n", program)], "P1", db_session)

    assert "n" not in prefill
    assert "見つかりません" in evidence["n"]["trace"][0]["error"]


# --------------------------------------------------------------------------- #
# step が1つも無いプログラム
# --------------------------------------------------------------------------- #


def test_an_empty_program_is_valid_because_it_is_the_manual_fallback_shape() -> None:
    """空の `steps` は検証を通る。**意図した振る舞い**である。

    コンパイラは「manual 中心で自動化できない項目は、空の steps と return:null を返してよい」
    と指示し（`COMPILE_SYSTEM_PROMPT`）、実行側も step が無い項目を `needs_input` として扱う。
    ここで弾くと、手入力へ縮退した正当なプランを確定できなくなる。
    """
    assert validate_program({"params": [], "steps": []}) == []
    assert validate_program({"steps": [], "return": None}) == []


def test_is_empty_program_tells_the_empty_shape_apart_from_a_program_with_steps() -> None:
    """検証では区別できないので、集計の側が別に数えるための述語。"""
    from kawasemi.ai_input.dsl import is_empty_program

    assert is_empty_program({"params": [], "steps": []}) is True
    assert is_empty_program({"steps": [], "return": None}) is True
    assert is_empty_program({}) is True
    assert is_empty_program({"steps": None}) is True
    one = {"steps": [{"op": "sql_extract", "id": "a", "sql": "SELECT 1"}], "return": "$a"}
    assert is_empty_program(one) is False
    # needs_input の step も step である（空ではない。人の補完を待つ形）
    assert (
        is_empty_program({"steps": [{"op": "sql_extract", "id": "a", "needs_input": True}]})
        is False
    )
