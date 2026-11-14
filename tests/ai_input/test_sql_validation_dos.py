"""計画の SQL 検査が、長い・病的な入力で処理時間を伸ばさないこと（再スキャン F3）。

`validate_program` はイベントループのスレッドで同期的に走るので、検査が入力長に対して
二次時間になると、デザイナーが保存した SQL 1件で応答が止まる。
"""

import time

import pytest
from pydantic import ValidationError

from kawasemi.ai_input.dsl import is_select_only_sql
from kawasemi.ai_input.schemas import MAX_SQL_LENGTH, DSLStep

# 上限以内で、旧実装なら二次時間になった大きさ（二次なら数秒〜数十秒）。
# 余裕を見て 0.3 秒未満で終わることだけを確かめる（線形なら数十ミリ秒）。
_BUDGET_SECONDS = 0.3


def _timed(sql: str) -> tuple[bool, float]:
    start = time.perf_counter()
    result = is_select_only_sql(sql)
    return result, time.perf_counter() - start


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT " + "1lo_" * 4900,  # `lo_\w+` の各開始位置が文末まで読む
        "SELECT " + "1dblink" * 2800 + "$",  # 接頭辞の族（`\w*` の読み直し）
        "SELECT " + "1pg_ls_" * 2800 + "$",
        "SELECT " + "1pg_sleep" * 2000 + "$",
        "SELECT " + ":" * 19000,  # SQLite の `:name(` 変数の読み直し
        "SELECT " + ":a" * 9000,
        "SELECT " + "$" * 19000,
        "SELECT " + "u& " * 6000,
        # 単語の連なりの上限（255 文字）ぎりぎりの連なりを並べる。上限で拒否されないので、
        # 正規表現が 1 か所あたり 255 文字ずつ読んでも全体が線形で済むことを見る
        "SELECT " + ("1lo_" * 63 + " ") * 77,
        "SELECT " + ("1dblink" * 36 + "$ ") * 77,
    ],
    ids=[
        "lo_",
        "dblink",
        "pg_ls_",
        "pg_sleep",
        "colons",
        "colon_a",
        "dollars",
        "unicode_escape",
        "lo_runs_at_the_limit",
        "dblink_runs_at_the_limit",
    ],
)
def test_adversarial_input_is_bounded_in_time(sql):
    assert len(sql) <= MAX_SQL_LENGTH
    _, elapsed = _timed(sql)
    assert elapsed < _BUDGET_SECONDS


def test_oversized_sql_is_rejected_without_scanning():
    result, elapsed = _timed("SELECT " + "1lo_" * 250_000)
    assert result is False
    assert elapsed < 0.1


def test_length_cap_is_the_documented_limit():
    assert is_select_only_sql("SELECT 1" + " " * 5)
    assert not is_select_only_sql("SELECT " + " " * MAX_SQL_LENGTH)


def test_a_very_long_word_is_rejected():
    """識別子は 63 バイトで切られる。連なりが 256 文字以上の語は受け付けない。"""
    assert is_select_only_sql("SELECT " + "a" * 255)
    assert not is_select_only_sql("SELECT " + "a" * 256)
    assert not is_select_only_sql("SELECT 'x' || '" + "a" * 300 + "'")


def test_step_schema_rejects_oversized_sql():
    with pytest.raises(ValidationError):
        DSLStep(id="a", op="sql_extract", sql="SELECT " + "a" * MAX_SQL_LENGTH)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT result_value FROM lab_results WHERE patient_id = :patient_id",
        "SELECT lo_value FROM t WHERE x = :patient_id",  # カラム名に lo_ を含む
        "SELECT a FROM t WHERE x = :patient_id ORDER BY y DESC LIMIT 1",
        "WITH c AS (SELECT 1 AS a) SELECT a FROM c",
        "SELECT '$a(b)' AS v",
    ],
)
def test_legitimate_sql_still_passes(sql):
    assert is_select_only_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT lo_import('/etc/passwd')",
        "SELECT \"lo_export\"(1, '/tmp/x')",
        "SELECT dblink_exec('x')",
        "SELECT pg_advisory_lock(1)",
        "SELECT pg_try_advisory_lock(1)",
        "SELECT pg_sleep(10)",
        "SELECT pg_ls_dir('.')",
        "SELECT pg_stat_reset()",
        "SELECT query_to_xml('select 1', true, true, '')",
        "SELECT table_to_xml('t', true, true, '')",
        "SELECT 1union SELECT 2",
    ],
)
def test_forbidden_forms_are_still_rejected(sql):
    assert not is_select_only_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT $a([) , $b$ ; SELECT 2 $b$ ]",  # TCL 変数の中の `;` の隠し方
        "SELECT :a(b c)",  # 変数名の中に空白
        "SELECT :a(",  # 閉じない
    ],
)
def test_sqlite_tcl_variable_reading_is_unchanged(sql):
    assert not is_select_only_sql(sql)


def test_tcl_variable_scan_agrees_with_the_regex_it_replaced():
    """線形にした走査が、置き換えた正規表現と同じ結果を返す（短い文字列を全列挙して照合）。"""
    import itertools
    import re

    from kawasemi.ai_input.dsl import _tcl_variable_name_end

    old = re.compile(r"[$@:#](?:[A-Za-z0-9_$\x80-\U0010ffff]|::)+\(")
    alphabet = ":a$(@ "
    for length in range(1, 8):
        for chars in itertools.product(alphabet, repeat=length):
            s = "".join(chars)
            memo: dict[int, int] = {}
            for i, ch in enumerate(s):
                if ch not in "$@:#":
                    continue
                m = old.match(s, i)
                got = _tcl_variable_name_end(s, i, memo)
                assert got == (m.end() if m else None), (s, i)
