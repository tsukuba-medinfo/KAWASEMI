"""DSLプログラムの共通ユーティリティ（ステップ3コンパイラ／ステップ4インタプリタ共用）

- プログラム構造の検証（op許可リスト・$id参照健全性・SQL=SELECT・needs_input検出）
- 値参照（"$id"）とリテラルの判別
- SQL安全性チェック（SELECT限定）
- unit_conversion の安全な式評価（eval 不使用）
- プログラム引数（params）の宣言解釈と、用途別サニタイズ（ParamBinder）
"""

from __future__ import annotations

import ast
import math
import operator
import re
import urllib.parse
from collections.abc import Callable, Iterable
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any, TypeGuard

from kawasemi.ai_input.models import OpType

# 各 op が「人間補完なしで実行可能」になるために最低限必要なフィールド。
# ここが欠けていて needs_input=false の場合は検証エラーにする。
_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    OpType.SQL_EXTRACT: ("sql",),
    OpType.EXTERNAL_DB_EXTRACT: ("sql",),
    # file / csv は **connection_id が無いと実行時に必ずフェイルクローズする**（
    # 接続ルートが解決できなければ読まない）。無いプランを確定できると「確定済みなのに
    # 構造的に実行不能」なプランを作れてしまい、失敗が分かるのは autofill 実行時＝バッチの
    # 途中になる。**保存時に弾く**。`needs_input` のときは免除される
    # （下の `not needs_input` 参照。まだ書いていないことは許す）。
    OpType.FILE_EXTRACT: ("path_pattern", "connection_id"),
    OpType.CSV_EXTRACT: ("path", "key_column", "column", "connection_id"),
    OpType.FHIR_EXTRACT: ("fhir_query",),
    OpType.LLM_EXTRACT: ("prompt",),
    OpType.LLM_JUDGE: ("prompt",),
    OpType.COMPARE: ("left", "cmp", "right"),
    OpType.BRANCH: ("when",),
    OpType.TRANSFORM: ("input", "rule"),
    OpType.SELECT: ("source",),
    OpType.AGGREGATE: ("source", "func"),
}

# compare で許可する演算子
#
# `is_empty` / `is_not_empty` は **`right` を取らない単項演算子**である。
# 「前段の step が値を取れたか」を書くために足した。`right: null` で表そうとすると
# 2つの理由で成立しない。(1) 必須フィールド検査が `None` を「未設定」として弾く、
# (2) `_program_dump` の `exclude_none=True` が保存時に落とすので「null を書いた」と
# 「書き忘れた」を区別できなくなる。
#
# 空の定義は `executor.is_empty_value`（None / "" / []）で、**`guard` の評価と同一の
# 規則**である。`==` 経由の比較（`_loose_eq`）は `str()` に落とすため `""` も `[]` も
# `None` と一致せず、LLM の空出力や0件 multi を「値あり」と誤判定する。
COMPARE_OPS = ("<", "<=", ">", ">=", "==", "!=", "in", "contains", "is_empty", "is_not_empty")

# `right` を取らない単項の比較演算子。
UNARY_COMPARE_OPS = ("is_empty", "is_not_empty")

# aggregate で許可する集計関数
AGGREGATE_FUNCS = ("count", "sum", "min", "max", "avg", "first")

# select で許可する選択方法（未指定は first）。executor._op_select は "last" 以外を
# すべて first として扱うため、"latest" 等の綴り違いが黙って first に化けるのを
# ここで弾く（select の where/order は非実行ヒントなので、pick だけが実行される）。
SELECT_PICKS = ("first", "last")

# 1プログラムあたりの step 数上限（巨大プログラムによるメモリ消費を防ぐ）
_MAX_STEPS = 256


# ---------------------------------------------------------------------------
# 値参照（"$id"）/ リテラルの判別
# ---------------------------------------------------------------------------


def is_ref(value: Any) -> TypeGuard[str]:
    """値が他stepへの参照（"$id"）かどうか"""
    return isinstance(value, str) and value.startswith("$") and len(value) > 1


def ref_name(value: str) -> str:
    """ "$id" → "id" """
    return value[1:]


def _collect_refs(value: Any) -> list[str]:
    """値（参照/リテラル/リスト/辞書）から参照される id を収集する"""
    refs: list[str] = []
    if is_ref(value):
        refs.append(ref_name(value))
    elif isinstance(value, list):
        for v in value:
            refs.extend(_collect_refs(v))
    elif isinstance(value, dict):
        for v in value.values():
            refs.extend(_collect_refs(v))
    return refs


# 値を取り得る step フィールド（参照健全性の検査対象）
_VALUE_FIELDS = (
    "left",
    "right",
    "when",
    "then",
    "else_branch",
    "input",
    "source",
    "match",  # csv_extract: [{column, value}] の value が "$id" 参照を取り得る
    # guard（条件付き実行の条件）も前段 step の結果を参照するため、他の値フィールドと
    # 同じ前方参照のみの規則に従わせる。後方参照を許すと「まだ実行していない step の
    # 結果で実行を決める」不能なプログラムになる。
    "guard",
)


# ---------------------------------------------------------------------------
# SQL 安全性
# ---------------------------------------------------------------------------

_SQL_FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|truncate|grant|revoke|"
    r"attach|pragma|replace|merge|copy|call|vacuum|execute|"
    # **集合演算**。索引の必須識別子を含んでいれば採用される仕組みなので、
    # `SELECT ... WHERE PATIENT_ID = :patient_id UNION SELECT password_hash FROM users` が
    # 「索引由来」として通っていた（実測）。保証は「索引の識別子を必ず含む」であって
    # 「それしか含まない」ではないため、集合演算そのものを拒否する
    r"union|except|intersect|"
    # **SELECT から到達できる書き込み・ファイル読み書き**。ブロックリストは
    # 取りこぼすので**保証は接続の権限側**（読み取り専用セッション）に置くが、多層にする
    r"into|outfile|dumpfile|pg_read_file|pg_read_binary_file|lo_import|lo_export|"
    # **読み取り専用トランザクションの外へ出る口・他の接続に作用する関数**（PostgreSQL）。
    # - `dblink*` は別セッションを開くので、READ ONLY の外で書ける（拡張が入っていれば）
    # - `pg_terminate_backend` / `pg_cancel_backend` は同じロールの他の接続を落とせる
    #   （読み取り専用でも効く。アプリのロールで動く経路ではアプリ自身の接続が対象になる）
    # - `pg_advisory*` / `pg_try_advisory*` はセッションに残り、他の処理を待たせうる
    #   （後始末で `pg_advisory_unlock_all()` も呼ぶが、取らせないのが先）
    # - `pg_sleep*` は抽出に使い道が無く、接続を握り続ける（`statement_timeout` も掛ける）
    r"dblink\w*|pg_terminate_backend|pg_cancel_backend|pg_advisory\w*|pg_try_advisory\w*|"
    r"pg_sleep\w*|"
    # **文字列で渡した SQL を実行する関数**。連結（`'select pg_' || 'sleep(1)'`）で上の
    # 語を組み立てられるので、関数ごと拒否する。PostgreSQL 17 の `pg_proc` で引数名に
    # query / sql / select / stmt / command / cursor を持つ関数を洗い出し、SQL を実行するものを
    # 採った（`ts_rewrite` は引数名が無いが、2引数形で SELECT 文の文字列を実行する）。
    # `table_to_xml` 系は SQL 文字列は取らないが、同じ一族でスキーマ丸ごとの読み出しに使える
    r"query_to_xml\w*|cursor_to_xml\w*|table_to_xml\w*|schema_to_xml\w*|database_to_xml\w*|"
    r"ts_stat|ts_rewrite)\b",
    re.IGNORECASE,
)

# **Unicode エスケープの識別子・文字列**（PostgreSQL の `U&"\0064blink_exec"`）。
# 禁止語を字面に出さずに書けるので、エスケープそのものを拒否する（抽出に使う理由が無い）。
_SQL_UNICODE_ESCAPE = re.compile(r"\bu&\s*[\"']", re.IGNORECASE)

# SQL コメント・引用の字句解析（引用を意識したコメント除去）。
#
# 以前は正規表現でコメントを消していた。引用を見ないので、文字列リテラルの中の `--` や `/*`
# から後ろが丸ごと消え、禁止語・複文・`U&` の検査の前に本体が見えなくなっていた
# （`SELECT '--', dblink_exec(...)` が通り、PostgreSQL 17 の開発用フォールバックで実際に書けた）。
#
# 方針:
#
# - **対象の方言は PostgreSQL と SQLite**（入っているドライバが asyncpg と aiosqlite だけ）。
#   PostgreSQL の字句規則で読み、2つの方言で引用・コメントの境界が食い違いうる書き方は
#   **それ自体を拒否する**（フェイルクローズ）。境界が食い違うと、こちらがコメントとして
#   消した範囲を DB が生きたコードとして実行しうるため。
# - コメントは**引用の外にあるものだけ**を空白1つへ置き換える。
# - **リテラルの中身は禁止語の検査から外さない**（下の `is_select_only_sql` を参照）。
#
# 個々の規則:
#
# - `'…'`（`''` で引用符）、`"…"`（識別子、`""` で引用符）、`` `…` ``（SQLite の識別子）。
# - `E'…'`（バックスラッシュエスケープ）。**奇数個のバックスラッシュの直後の引用符は全種で
#   拒否する**。`E'\''` は PostgreSQL では1文字の `'` だが、SQLite と
#   `standard_conforming_strings=off` の PostgreSQL では文字列の終端の位置が変わる。
#   引用符は `''` で書けるので表現力は失わない。この規則のもとでは E 文字列の終端は普通の
#   文字列と同じ位置に来るので、同じ処理で読める（`\n` などの他のエスケープは終端を動かさない）。
# - `$tag$…$tag$`（PostgreSQL のドル引用）。SQLite では本体がコードとして読まれるので、
#   本体に引用符・`[`・`;` を含むものは拒否する。本体の `--` / `/*` は消さずに残す
#   （検査側が多く見る向きの食い違いなので安全）。
# - `--` 行コメント、`/* */` ブロックコメント。**入れ子は拒否する**。PostgreSQL は入れ子を
#   数えるが SQLite は最初の `*/` で閉じるので、`/* /* */ dblink_exec(...) */` は
#   SQLite では関数呼び出しが生きる。MySQL の実行されるコメント `/*!…*/` も拒否する。
# - `[…]`（SQLite の識別子、PostgreSQL の添字）。中身に引用符・`$`・`;`・コメント記号を
#   含むものは方言で読みが割れるので拒否する。それ以外は普通のコードとして読む。
# - 閉じていない引用・コメントは拒否する（SQLite は閉じない `/*` を末尾までのコメントとして
#   受け付けるが、ここでは受け付けない）。
_SQL_IDENT_CHAR = re.compile(r"[A-Za-z0-9_$\x80-\U0010ffff]")
_SQL_DOLLAR_TAG = re.compile(r"\$(?:[A-Za-z_\x80-\U0010ffff][A-Za-z0-9_\x80-\U0010ffff]*)?\$")
_SQL_BRACKET_AMBIGUOUS = ("'", '"', "`", "$", ";", "--", "/*")
_SQL_DOLLAR_BODY_AMBIGUOUS = ("'", '"', "`", "[", ";")


class _SqlLexError(ValueError):
    """引用・コメントの境界が決まらない（閉じていない・方言で読みが割れる）SQL"""


def _end_of_quoted(sql: str, start: int) -> int:
    """`sql[start]` の引用符で始まる引用の、閉じ引用符の次の位置を返す"""
    quote = sql[start]
    i = start + 1
    n = len(sql)
    while i < n:
        ch = sql[i]
        if ch == "\\":
            run = 1
            while i + run < n and sql[i + run] == "\\":
                run += 1
            if run % 2 == 1 and sql[i + run : i + run + 1] == quote:
                raise _SqlLexError("バックスラッシュでエスケープした引用符は方言で読みが割れる")
            i += run
            continue
        if ch == quote:
            if sql[i + 1 : i + 2] == quote:
                i += 2
                continue
            return i + 1
        i += 1
    raise _SqlLexError("閉じていない引用")


def _lex_sql(sql: str) -> tuple[str, str]:
    """SQL を (コメント除去済み, コメント除去かつ引用を空白へ置換済み) の2つへ分ける。

    1つ目は禁止語・`U&` の検査に、2つ目はセミコロン・先頭語の検査に使う。

    Raises:
        _SqlLexError: 閉じていない引用・コメント、または方言で境界の読みが割れる書き方。
    """
    kept: list[str] = []
    code: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        two = sql[i : i + 2]
        if two == "--":
            end = sql.find("\n", i)
            i = n if end < 0 else end
            kept.append(" ")
            code.append(" ")
            continue
        if two == "/*":
            if sql.startswith("/*!", i):
                raise _SqlLexError("実行されるコメント（/*!）")
            end = sql.find("*/", i + 2)
            if end < 0:
                raise _SqlLexError("閉じていないブロックコメント")
            if "/*" in sql[i + 2 : end]:
                raise _SqlLexError("入れ子のブロックコメント")
            i = end + 2
            kept.append(" ")
            code.append(" ")
            continue
        if ch in "'\"`":
            end = _end_of_quoted(sql, i)
            kept.append(sql[i:end])
            code.append(" ")
            i = end
            continue
        if ch == "[":
            end = sql.find("]", i)
            if end < 0:
                raise _SqlLexError("閉じていない [")
            body = sql[i + 1 : end]
            if any(s in body for s in _SQL_BRACKET_AMBIGUOUS):
                raise _SqlLexError("[…] の中に引用・コメント記号")
            kept.append(sql[i : end + 1])
            code.append(sql[i : end + 1])
            i = end + 1
            continue
        if ch == "$" and (i == 0 or not _SQL_IDENT_CHAR.match(sql[i - 1])):
            m = _SQL_DOLLAR_TAG.match(sql, i)
            if m:
                tag = m.group()
                end = sql.find(tag, m.end())
                if end < 0:
                    raise _SqlLexError("閉じていないドル引用")
                body = sql[m.end() : end]
                if any(s in body for s in _SQL_DOLLAR_BODY_AMBIGUOUS):
                    raise _SqlLexError("ドル引用の本体に引用符・[・;")
                kept.append(sql[i : end + len(tag)])
                code.append(" ")
                i = end + len(tag)
                continue
        kept.append(ch)
        code.append(ch)
        i += 1
    return "".join(kept), "".join(code)


def _strip_sql_comments(sql: str) -> str:
    """引用の外のコメントを空白へ置き換えた SQL（字句が決まらなければ元の SQL）。

    バインド名の走査（`sql_bind_names`）用。SQLAlchemy の `text()` も引用を見ずに `:name` を
    拾うので、リテラルの中身は残す。字句が決まらない SQL は `is_select_only_sql` が拒否するので、
    ここでは消しすぎない向き（元の SQL）へ倒す。
    """
    try:
        return _lex_sql(sql)[0]
    except _SqlLexError:
        return sql


def is_select_only_sql(sql: str) -> bool:
    """SELECT（または WITH ... SELECT）のみで、書込/DDL/複文を含まないか

    - 引用を意識してコメントを除去したうえで判定する（`_lex_sql`）。閉じていない引用・
      コメント、方言で境界の読みが割れる書き方は拒否する
    - 先頭が select または with で始まる
    - 危険なキーワードを含まない
    - 引用・コメントの外のセミコロンは、文末の1つだけ許す（`SELECT 1;;` は複文として拒否）

    **禁止語はリテラルの中身も含めて探す**。リテラルを空白にしてから探せば
    `WHERE status = 'delete'` のような誤検知は消えるが、それが安全なのは「文字列を SQL として
    実行する関数が全部禁止語に入っている」ときに限る。PostgreSQL 本体の分は洗い出してある
    （下の `_SQL_FORBIDDEN`）ものの、`external_db_extract` はこの検査が唯一のコード上の関門で、
    接続先に入っている拡張（`dblink` と同類のもの）までは列挙できない。リテラルの中の禁止語は
    そうした未知の関数に渡ったときの最後の網になるので、誤検知（以前からの挙動）を取って残す。

    注: これは多層防御の一層であり、文字列パースだけに依存しない。**保証の根拠は接続の
    権限側**である——`_op_sql` はアプリDBを**読み取り専用セッション**で読み、
    external_db の接続は read-only ロールで運用して書込権限自体を与えない。

    禁止語には**集合演算**（`union` / `except` / `intersect`）も含む。索引由来 SQL の
    採用は「索引の必須識別子を含むこと」を条件にしており、**「それしか含まない」ことは
    保証していない**ため、`... UNION SELECT password_hash FROM users` が通っていた（実測）。
    """
    if not sql or not sql.strip():
        return False
    try:
        kept, code = _lex_sql(sql)
    except _SqlLexError:
        return False
    code = code.strip()
    if code.endswith(";"):
        code = code[:-1].rstrip()
    if not code:
        return False
    if ";" in code:  # 文末の1つは上で除去済み → 残れば複文
        return False
    lowered = code.lower()
    if not (lowered.startswith("select") or lowered.startswith("with")):
        return False
    if _SQL_FORBIDDEN.search(kept):
        return False
    if _SQL_UNICODE_ESCAPE.search(kept):
        return False
    return True


# ---------------------------------------------------------------------------
# プログラム引数（params）と、用途別サニタイズ
# ---------------------------------------------------------------------------

# 実行時に自動で供給される組み込みパラメータ（program.params での宣言不要）。
# patient_id は step の key_type に応じて解決済みの被験者キー。
#
# study_id は**バッチ実行でのみ供給される**（BatchJob.study_id）。単一被験者の
# autofill（画面からの実行）では未指定なので、参照した step は実行時に失敗する。
# 供給経路の無い識別子を組み込みとして増やさないこと（encounter_id は
# 「executor に参照が無い」として削除した経緯があり、供給側を用意せずに宣言だけ
# 復活させると同じ問題を再生産する）。
BUILTIN_PARAMS = ("patient_id", "study_id")

# params の kind。
# - value:      バインド値として使う（SQL は :name のバインド、FHIR/パスは字種制限）
# - identifier: SQL のテキストへ {{name}} 展開できる（表名・カラム名）。
#               バインドパラメータでは識別子を表現できないため唯一の文字列連結経路。
PARAM_KINDS = ("value", "identifier")

_PARAM_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")

# テンプレート内のプレースホルダ {{name}} / {{ name }}
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

# プロンプト専用の $name 参照。プロンプトは自然文なので、{{name}} と違い
# 「参照のつもりで書かれた $name」と「単なる字面（$USD など）」を字句だけでは
# 区別できない。したがって validate_program はこの集合を**未宣言判定には使わず**、
# 確実に誤りである自己参照・後方参照の検出にだけ使う。
# 実行時層（DSLInterpreter._render_prompt）が参照とみなす集合と同じでなければ
# 両層の答えがまた食い違うので、正規表現は必ずここ1箇所に置く。
_PROMPT_DOLLAR_RE = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)")

# SQL のバインド :name。PostgreSQL の型キャスト（`x::int`）を拾わないよう直前の
# コロン／識別子文字を除外する（SQLAlchemy の text() が見るバインドと同じ集合を狙う）。
_SQL_BIND_RE = re.compile(r"(?<![:\w]):([A-Za-z_][A-Za-z0-9_]*)")

# SQL 識別子（表名・カラム名）。**スキーマ修飾は許さない**。修飾を許すと
# `information_schema.tables` や `pg_catalog.pg_shadow` のような別スキーマの
# メタ情報へ手が届く。修飾が要る所在は allowed に完全名を列挙するのではなく、
# 索引の payload から決定的に導出する経路を使うこと。
_SQL_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

# 識別子として決して受け付けないメタ・システム表。allowed の宣言漏れに対する
# 最後の網であり、allowed 必須化（_validate_params）と併せた多層防御の一層。
_SQL_META_IDENTIFIERS = frozenset(
    {
        "sqlite_master",
        "sqlite_temp_master",
        "sqlite_sequence",
        "sqlite_schema",
        "sqlite_temp_schema",
        "information_schema",
        "pg_catalog",
        "pg_shadow",
        "pg_authid",
        "pg_user",
        "pg_roles",
        "pg_class",
        "pg_tables",
        "pg_stat_activity",
        "mysql",
        "performance_schema",
        "sys",
        "dual",
    }
)
# 接頭辞で弾くもの（PostgreSQL のシステムカタログ・MySQL の内部表）。
_SQL_META_PREFIXES = ("pg_", "sqlite_")

# FHIR 検索パラメータ値（論理ID・コード）。URLエンコード前の字種制限。
_FHIR_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")

# パス構成要素。区切り文字（/ \）・上位参照（..）・NUL を含まない1要素のみ許す。
_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class ParamError(ValueError):
    """パラメータの解決・サニタイズに失敗した（呼び出し側は当該 step を失敗させる）"""


def _assert_no_traversal(rendered: str, what: str) -> None:
    """組み立て後の文字列に上位参照・区切りの異常が現れていないか再検査する。

    個々の値はサニタイズ済みでも、隣接プレースホルダの結合で `..` が生まれうる。
    SQL が展開後に is_select_only_sql で再検査するのと同じ発想の2層目。
    """
    if ".." in rendered or "\\" in rendered or "\x00" in rendered:
        raise ParamError(f"{what}の組み立て結果に上位参照または不正な区切りが含まれます")


def placeholder_names(template: Any) -> set[str]:
    """テンプレート文字列に含まれる {{name}} の name 集合を返す"""
    if not isinstance(template, str):
        return set()
    return set(_PLACEHOLDER_RE.findall(template))


def prompt_dollar_names(template: Any) -> set[str]:
    """プロンプト文字列に含まれる $name の name 集合を返す

    検証層（validate_program）と実行時層（DSLInterpreter._render_prompt）が
    同じ字句解析を使うための共有関数。片方だけが名前を拾うと、
    「一方の層だけが解決できる参照」が生まれる。
    """
    if not isinstance(template, str):
        return set()
    return set(_PROMPT_DOLLAR_RE.findall(template))


def prompt_ref_pattern(names: Iterable[str]) -> re.Pattern[str]:
    """prompt 中の `{{name}}` / `$name` を1パスで置換するためのパターンを返す

    **字句解析（placeholder_names / prompt_dollar_names）と照合のパターンは必ず
    同じ場所に置く。** 別々に持つと「一方の層だけが参照とみなす」形が生まれる。

    `$name` の終端に `\\b` を使ってはいけない。Python の `\\w` は Unicode 対応で
    仮名・漢字を含むため、`$rawの所見` のように直後が日本語だと語境界が生じず
    **置換されない**。検証層は `$raw` を参照として受理するので、原資料を一切見て
    いないプロンプトが LLM へ渡り、その出力が抽出値として確定してしまう。
    ASCII の識別子文字だけを否定先読みで除外する。

    接頭辞衝突（`$dept` と `$dept_code`）は否定先読みが分離するが、長い名前から
    並べて交替の初手を安定させる。
    """
    if not names:
        # 空の交替（`({alt})` が空グループ）を作ると `{{}}` や単独の `$` にマッチし、
        # 空文字の名前を捕まえてしまう。呼び出し側で `if values:` を書き忘れても
        # 壊れないよう、決してマッチしないパターンを返す。
        return re.compile(r"(?!)")
    alt = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(rf"\{{\{{\s*({alt})\s*\}}\}}|\$({alt})(?![A-Za-z0-9_])")


def sql_bind_names(sql: Any) -> set[str]:
    """SQL に含まれるバインド :name の name 集合を返す（コメントは除去して走査）"""
    if not isinstance(sql, str):
        return set()
    return set(_SQL_BIND_RE.findall(_strip_sql_comments(sql)))


def param_specs(program: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """program.params を {name: spec} へ正規化する。

    構造の妥当性検査は validate_program が担う。ここは実行時用の素朴な取り出しに
    徹し、壊れた宣言は取り込まない（＝未宣言として ParamError で弾かれる）。
    """
    specs: dict[str, dict[str, Any]] = {}
    params = program.get("params")
    if not isinstance(params, list):
        return specs
    for spec in params:
        if not isinstance(spec, dict):
            continue
        name = spec.get("name")
        if isinstance(name, str) and _PARAM_NAME_RE.match(name) and name not in BUILTIN_PARAMS:
            specs.setdefault(name, spec)
    return specs


def sanitize_sql_identifier(value: Any, allowed: list[str] | None, name: str) -> str:
    """値を SQL 識別子（表名・カラム名）として安全な形に検査して返す。

    バインドパラメータでは識別子を表現できないため文字列連結せざるを得ない。
    そこで「厳格な字種（引用符・空白・セミコロン・記号を一切含まない）」と
    「許可リスト（宣言されていれば）」の二重の関門を必ず通す。
    """
    s = "" if value is None else str(value).strip()
    if not _SQL_IDENT_RE.match(s):
        raise ParamError(f"パラメータ {name} の値はSQL識別子として不正です")
    if _SQL_FORBIDDEN.search(s):
        raise ParamError(f"パラメータ {name} の値に禁止キーワードが含まれます")
    low = s.lower()
    if low in _SQL_META_IDENTIFIERS or low.startswith(_SQL_META_PREFIXES):
        raise ParamError(f"パラメータ {name} の値にメタ・システム表は指定できません")
    if allowed is None:
        # allowed は identifier では必須（_validate_params が確定前に弾く）。ここへ到達
        # するのは検証を経ていない経路なので、字種検査だけで通さずに失敗させる。
        raise ParamError(f"パラメータ {name} は識別子スロットなので allowed の宣言が必要です")
    if s not in allowed:
        raise ParamError(f"パラメータ {name} の値が allowed（許可リスト）にありません")
    return s


def sanitize_fhir_token(
    value: Any, allowed: list[str] | None, name: str, *, requires_allowlist: bool = False
) -> str:
    """値を FHIR 検索パラメータ値として安全な形に検査して返す（URLエンコード前）。

    requires_allowlist=True（値が前段 step の出力）のときは allowed を必須にする。字種検査は
    「形」しか縛らないため、実行時のデータが検索対象（別被験者のリソース等）を
    選べてしまう。確定前の検証（_validate_params）と同じ規則を実行時にも置く。
    """
    s = "" if value is None else str(value).strip()
    if not _FHIR_TOKEN_RE.match(s):
        raise ParamError(f"パラメータ {name} の値はFHIR検索値として不正です")
    if requires_allowlist and allowed is None:
        raise ParamError(f"パラメータ {name} は前段 step 由来なので allowed の宣言が必要です")
    if allowed is not None and s not in allowed:
        raise ParamError(f"パラメータ {name} の値が allowed（許可リスト）にありません")
    return s


def sanitize_path_segment(
    value: Any, allowed: list[str] | None, name: str, *, requires_allowlist: bool = False
) -> str:
    """値をパス構成要素として安全な形に検査して返す（パストラバーサル遮断）。

    区切り文字・上位参照（..）・絶対パスを許さないため、テンプレート側が示す
    ディレクトリ構造から外に出ることが原理的にできない（root 配下検査と二層）。

    requires_allowlist=True（値が前段 step の出力）のときは allowed を必須にする。**上記の
    2層はいずれも root 外への脱出しか防がない**ので、`{{dir}}/note.txt` の {{dir}} に
    別被験者のディレクトリ名が入る「root 内の横移動」はこの検査でしか止まらない。
    """
    s = "" if value is None else str(value).strip()
    if ".." in s or not _PATH_SEGMENT_RE.match(s):
        raise ParamError(f"パラメータ {name} の値はパス構成要素として不正です")
    if requires_allowlist and allowed is None:
        raise ParamError(f"パラメータ {name} は前段 step 由来なので allowed の宣言が必要です")
    if allowed is not None and s not in allowed:
        raise ParamError(f"パラメータ {name} の値が allowed（許可リスト）にありません")
    return s


class ParamBinder:
    """program.params と組み込みパラメータを、用途（スロット）別に安全な形へ解決する。

    同じ値でも差し込み先によって安全な形が違うため、経路をここで一本化して取り違えを
    防ぐ。プロンプト（自然文）への注入だけは性質が異なる（識別子ではなく、外部データを
    「指示ではなくデータ」と明示して囲む）ため、executor 側の _render_prompt が担う。

      - SQL バインド値 : sql_binds()    ドライバのバインドへ載せる（文字列連結しない）
      - SQL 識別子     : render_sql()   kind=identifier 限定＋厳格字種＋許可リスト
      - FHIR 検索値    : render_query() 字種制限＋URLエンコード
      - パス構成要素   : render_path()  区切り文字・上位参照を禁止
      - 比較値         : render_value() サニタイズなし（解釈されずそのまま比較される）
    """

    def __init__(
        self,
        specs: dict[str, dict[str, Any]] | None = None,
        request_params: dict[str, str] | None = None,
    ) -> None:
        self._specs = specs or {}
        self._request = {k: (request_params or {}).get(k) or "" for k in ("study_id",)}

    @classmethod
    def from_program(
        cls, program: dict[str, Any], request_params: dict[str, str] | None = None
    ) -> ParamBinder:
        return cls(param_specs(program), request_params)

    # ------------------------------------------------------------- 解決（生値）

    def kind_of(self, name: str) -> str:
        if name in BUILTIN_PARAMS:
            return "value"
        return str((self._specs.get(name) or {}).get("kind") or "value")

    def allowed_of(self, name: str) -> list[str] | None:
        allowed = (self._specs.get(name) or {}).get("allowed")
        if isinstance(allowed, list):
            return [str(a) for a in allowed]
        return None

    def raw(self, name: str, context: dict[str, Any], patient_id: str) -> Any:
        """パラメータ名を素の値へ解決する（サニタイズ前）。

        組み込みを常に優先する（宣言による上書き＝なりすましを許さない）。
        宣言の value は "$id"（前段 step の出力）かリテラル。
        """
        if name == "patient_id":
            return patient_id
        if name in self._request:
            builtin = self._request[name]
            if not builtin:
                raise ParamError(f"組み込みパラメータ {name} が実行リクエストで未指定です")
            return builtin
        spec = self._specs.get(name)
        if spec is None:
            raise ParamError(f"未宣言のパラメータを参照しています: {name}")
        value = spec.get("value")
        if is_ref(value):
            rid = ref_name(value)
            if rid not in context:
                # 参照先 step が実行されていない（前方参照の検証は通っていても、
                # 実行時に到達しなかった場合など）。None へ縮退させると、識別子は
                # サニタイズで失敗する一方 prompt は空文字で成功してしまうため、
                # ここで失敗させて扱いを揃える。guard で skip された step は
                # context に None が束縛されるので「実行された結果の None」と
                # 区別できる。
                raise ParamError(f"パラメータ {name} の参照先 ${rid} が解決できません")
            return context[rid]
        return value

    def is_external(self, name: str) -> bool:
        """値が外部データ由来か（前段 step の出力＝抽出結果なら True）。

        リテラル宣言は人間が確認済みのプログラム本文なので内部扱い。一方
        **study_id はリクエストボディ由来**（BatchJob.study_id）で字種制約も無いため、
        patient_id（key_resolver を通り字種検査済み）とは区別して外部扱いにする。
        「抽出値は原資料から導出されねばならず、依頼者の入力で操作できてはならない」
        というデータインテグリティの前提を守るため。
        """
        if name == "patient_id":
            return False
        if name in BUILTIN_PARAMS:
            return True
        return is_ref((self._specs.get(name) or {}).get("value"))

    def declares(self, name: str) -> bool:
        """`name` が program.params で宣言されたパラメータか（組み込みは含まない）。"""
        return name in self._specs and name not in BUILTIN_PARAMS

    def step_ref_of(self, name: str) -> str | None:
        """宣言パラメータの値が前段 step の参照（`"$id"`）なら、その step id を返す。

        LLM op の入力が使えるか（前段が失敗・空でないか）を確かめるのに使う。
        リテラル宣言と組み込みは前段 step に依存しないので None。
        """
        if not self.declares(name):
            return None
        value = (self._specs.get(name) or {}).get("value")
        return ref_name(value) if is_ref(value) else None

    def requires_allowlist(self, name: str) -> bool:
        """この値がセレクタスロット（読み取り対象を選ぶ場所）で allowed を要するか。

        `is_external`（プロンプト囲みの要否）とは**別の概念**なので述語を分ける。
        両者を兼用すると、検証（組み込みを素通り）と実行時（組み込みも外部扱い）で
        答えが食い違い、「確定は通るのに実行は必ず失敗する」プログラムを作れてしまう。

        判定は「値が前段 step の出力か」。組み込み（patient_id / study_id）は実行
        リクエスト由来で、LLM 抽出値や外部システム応答ではないため対象外とする
        （validate_program が組み込みをセレクタ検査から除外しているのと同じ規則）。
        """
        if name in BUILTIN_PARAMS:
            return False
        return is_ref((self._specs.get(name) or {}).get("value"))

    # --------------------------------------------------------------- スロット別

    def sql_binds(self, sql: str, context: dict[str, Any], patient_id: str) -> dict[str, Any]:
        """SQL の :name バインドへ渡す値の辞書を作る（識別子は含まない）。"""
        binds: dict[str, Any] = {}
        for name in sql_bind_names(sql):
            binds[name] = self.raw(name, context, patient_id)
        # 後方互換: :patient_id を書いていない SQL でも従来どおり渡す
        binds.setdefault("patient_id", patient_id)
        return binds

    def render_sql(self, sql: str, context: dict[str, Any], patient_id: str) -> str:
        """SQL の {{name}} を識別子として置換する（kind=identifier 限定）。

        ここが DSL における唯一の SQL 文字列連結経路である。呼び出し側は置換後の
        SQL を is_select_only_sql で**再検査**すること（多層防御）。
        """

        def _sub(m: re.Match[str]) -> str:
            name = m.group(1)
            if self.kind_of(name) != "identifier":
                raise ParamError(
                    f"SQLへの {{{{{name}}}}} 展開は kind=identifier のパラメータのみ許可されます"
                )
            return sanitize_sql_identifier(
                self.raw(name, context, patient_id), self.allowed_of(name), name
            )

        return _PLACEHOLDER_RE.sub(_sub, sql)

    def render_query(self, template: str, context: dict[str, Any], patient_id: str) -> str:
        """FHIR クエリの {{name}} を、字種制限＋URLエンコードして置換する。

        個々の値の検査に加え、**組み立て後の文字列も再検査する**（SQL と同じ多層防御）。
        各値が単体では安全でも、隣接プレースホルダ `{{a}}{{b}}` の結合で `..` のような
        意味を持つ並びが生まれうる（`urllib.parse.quote` はドットをエンコードしない）。
        """

        def _sub(m: re.Match[str]) -> str:
            name = m.group(1)
            token = sanitize_fhir_token(
                self.raw(name, context, patient_id),
                self.allowed_of(name),
                name,
                requires_allowlist=self.requires_allowlist(name),
            )
            return urllib.parse.quote(token, safe="")

        rendered = _PLACEHOLDER_RE.sub(_sub, template)
        _assert_no_traversal(rendered, "FHIRクエリ")
        return rendered

    def render_value(self, template: Any, context: dict[str, Any], patient_id: str) -> Any:
        """比較値の {{name}} を素の値で置換する。

        CSV の match 条件のように、**値がどこかへ解釈されず、そのまま等値比較される
        だけ**のスロット用。サニタイズしないのは、区切り文字や記号を含む値も CSV の
        セル値としては正当だからである（`sanitize_path_segment` を当てると、実在する
        セル値と一致しなくなる）。読み取り対象（表・列・パス・クエリ）を選ばないので、
        allowed も要求しない — SQL の `:name` バインドと同じ線引きである。

        **allowed 不要の実効的な根拠は「解釈されないこと」だけではない。** `_op_csv` は
        先頭の条件に必ず解決済み患者キーの等値を置き、条件を AND でしか結合しない。
        したがって match value をどう操作しても取得範囲は広がらず、当該被験者の行の中で
        狭まるだけである。`key_column` の必須化か AND 結合のどちらかを緩めると、この
        判断は成立しなくなる（そのときは allowed の要否から再検討する）。

        置換は1パスの文字列置換に統一する。呼び出し元（`_op_csv`）が比較のために
        `str()` するので値の型を保つ意味は無く、「`{{name}}` ちょうどなら型を保つ」の
        ような分岐を入れると前後の空白の扱いが記法で変わって静かな不一致を生む。

        未宣言のパラメータ参照は `raw` が ParamError にする。黙って空文字へ縮退させると
        「該当なし」と区別できない静かな誤りになるため。
        """
        if not isinstance(template, str):
            return template

        def _sub(m: re.Match[str]) -> str:
            value = self.raw(m.group(1), context, patient_id)
            return "" if value is None else str(value)

        return _PLACEHOLDER_RE.sub(_sub, template)

    def render_path(self, template: str, context: dict[str, Any], patient_id: str) -> str:
        """パステンプレートの {{name}} を、パス構成要素として安全な値で置換する。

        個々の値の検査に加え、**組み立て後のパスも再検査する**（render_query と同じ理由。
        `{{a}}{{b}}` に "." と "." が入ると結合結果が `..` になる）。
        """

        def _sub(m: re.Match[str]) -> str:
            name = m.group(1)
            return sanitize_path_segment(
                self.raw(name, context, patient_id),
                self.allowed_of(name),
                name,
                requires_allowlist=self.requires_allowlist(name),
            )

        rendered = _PLACEHOLDER_RE.sub(_sub, template)
        _assert_no_traversal(rendered, "ファイルパス")
        return rendered

    def prompt_values(
        self, context: dict[str, Any], patient_id: str, names: Iterable[str] | None = None
    ) -> dict[str, Any]:
        """プロンプト描画用に、参照されたパラメータを {name: 値} で返す。

        names には**テンプレートが実際に参照している名前**を渡す。宣言済みパラメータと
        組み込みパラメータ（patient_id / study_id）の両方を解決対象に
        する。組み込みを含めないと、コンパイラのプロンプトの案内どおり LLM が書いた
        `{{study_id}}` が生のまま LLM へ渡り、黙って誤った抽出結果になる。

        値を解決できない参照は**除外せず ParamError を送出する**（呼び出し側が当該 step
        を失敗させる）。空文字へ縮退させると、誤った値が prefill され evidence に失敗の
        痕跡が残らない。識別子スロットが失敗するのと扱いを揃える。

        names 省略時は宣言済みパラメータのみを対象にする（未参照の組み込みが未指定でも
        step を失敗させないため）。
        """
        targets = (
            list(self._specs)
            if names is None
            else [n for n in names if n in self._specs or n in BUILTIN_PARAMS]
        )
        out: dict[str, Any] = {}
        for name in targets:
            out[name] = self.raw(name, context, patient_id)
        return out


# ---------------------------------------------------------------------------
# unit_conversion の安全な式評価（eval 不使用）
# ---------------------------------------------------------------------------

# 冪乗（ast.Pow）は意図的に除外する。`9**9**9` のような式は巨大整数/浮動小数
# 演算で CPU・メモリを占有してワーカーをハングさせ得る（DoS）。単位変換に冪乗は
# ほぼ不要なため許可集合から外し、攻撃面を断つ。
_ALLOWED_BINOPS: dict[type[ast.operator], Callable[[float, float], float]] = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.Mod: operator.mod,
}
_ALLOWED_UNARYOPS: dict[type[ast.unaryop], Callable[[float], float]] = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


def safe_arith(formula: str, value: float) -> float:
    """`value` を含む算術式を安全に評価する（+,-,*,/,% と括弧・数値のみ）

    冪乗（**）は DoS 回避のため非対応。結果が inf/nan になる式も拒否する。

    Raises:
        ValueError: 許可されない構文・トークンが含まれる、または結果が非有限の場合
    """
    expr = formula.replace("value", repr(float(value)))
    node = ast.parse(expr, mode="eval")

    def _eval(n: ast.AST) -> float:
        if isinstance(n, ast.Expression):
            return _eval(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return float(n.value)
        if isinstance(n, ast.BinOp) and type(n.op) in _ALLOWED_BINOPS:
            return _ALLOWED_BINOPS[type(n.op)](_eval(n.left), _eval(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _ALLOWED_UNARYOPS:
            return _ALLOWED_UNARYOPS[type(n.op)](_eval(n.operand))
        raise ValueError(f"許可されない式要素: {ast.dump(n)}")

    result = _eval(node)
    if not math.isfinite(result):
        raise ValueError("式の評価結果が非有限（inf/nan）です")
    return result


# ---------------------------------------------------------------------------
# プログラム検証
# ---------------------------------------------------------------------------


# op ごとの「プロンプト」フィールド。自由記述だが {{name}} 参照の健全性は
# 識別子系と同じく確定前に検査する。未宣言参照を残すと、実行時に prompt_values が
# ParamError になり step が失敗する（黙って空文字へ縮退させない方針のため）。
_PROMPT_FIELDS: dict[str, tuple[str, ...]] = {
    OpType.LLM_EXTRACT: ("prompt",),
    OpType.LLM_JUDGE: ("prompt",),
}

# 「どこから読むか」を決めるスロット。ここへ前段 step の出力を差し込むときは
# allowed を必須にする（値そのものが読み取り対象の選択になるため）。
_SELECTOR_FIELDS = frozenset({"sql", "path_pattern", "path", "fhir_query"})

# op ごとの「識別子系テンプレート」フィールド。ここに現れる {{name}} は params で
# 宣言済みでなければならない。
_TEMPLATE_FIELDS: dict[str, tuple[str, ...]] = {
    OpType.SQL_EXTRACT: ("sql",),
    OpType.EXTERNAL_DB_EXTRACT: ("sql",),
    OpType.FILE_EXTRACT: ("path_pattern",),
    OpType.CSV_EXTRACT: ("path",),
    OpType.FHIR_EXTRACT: ("fhir_query",),
}


def _validate_params(program: dict[str, Any]) -> tuple[list[str], dict[str, dict[str, Any]]]:
    """program.params の宣言そのものを検証し、(エラー, 正規化した spec) を返す"""
    errors: list[str] = []
    params = program.get("params")
    if params is None:
        return errors, {}
    if not isinstance(params, list):
        return ["program.params が配列ではありません"], {}

    specs: dict[str, dict[str, Any]] = {}
    for i, spec in enumerate(params):
        loc = f"params[{i}]"
        if not isinstance(spec, dict):
            errors.append(f"{loc}: パラメータ宣言が辞書ではありません")
            continue
        name = spec.get("name")
        if not isinstance(name, str) or not _PARAM_NAME_RE.match(name):
            errors.append(
                f"{loc}: name は英字/アンダースコア始まりの識別子にしてください: {name!r}"
            )
            continue
        if name in BUILTIN_PARAMS:
            errors.append(f"{loc}: 組み込みパラメータ名は再宣言できません: {name!r}")
            continue
        if name in specs:
            errors.append(f"{loc}: パラメータ名が重複しています: {name!r}")
            continue

        kind = spec.get("kind") or "value"
        if kind not in PARAM_KINDS:
            errors.append(f"{loc}: kind は {PARAM_KINDS} のいずれかにしてください: {kind!r}")

        value = spec.get("value")
        if value is not None and not isinstance(value, (str, int, float, bool)):
            errors.append(f'{loc}: value はリテラル（文字列/数値/真偽）か "$id" 参照にしてください')

        allowed = spec.get("allowed")
        if allowed is None:
            if kind == "identifier":
                # 識別子は SQL のテキストへ連結される唯一のスロット。字種検査は識別子の
                # 「形」しか縛らず「どの表か」を縛らないため、allowed が唯一の実効的な
                # 関門になる。
                errors.append(
                    f"{loc}: kind=identifier は allowed（許可する表名/カラム名の列挙）が必須です"
                )

        elif not isinstance(allowed, list) or not allowed:
            errors.append(f"{loc}: allowed は非空の配列にしてください")
        elif any(not isinstance(a, (str, int, float)) for a in allowed):
            errors.append(f"{loc}: allowed の要素は文字列か数値にしてください")
        elif kind == "identifier":
            # allowed 自体がメタ表を含んでいたら確定前に弾く（設計者の誤りを見逃さない）。
            bad = [
                a
                for a in allowed
                if str(a).lower() in _SQL_META_IDENTIFIERS
                or str(a).lower().startswith(_SQL_META_PREFIXES)
            ]
            if bad:
                errors.append(f"{loc}: allowed にメタ・システム表は列挙できません: {bad}")

        specs[name] = spec

    return errors, specs


def has_needs_input(program: dict[str, Any]) -> bool:
    """program 内に needs_input=true の step が1つでもあるか"""
    return any(s.get("needs_input") for s in program.get("steps", []))


def is_empty_program(program: dict[str, Any]) -> bool:
    """step が1つも無いプログラムか。

    **`validate_program` は空の `steps` を弾かない。** コンパイラは「manual 中心で自動化
    できない項目は、空の steps と return:null を返してよい」と指示しており
    （`COMPILE_SYSTEM_PROMPT` の4）、実行側も step が無い項目を `needs_input` として扱う
    （`derive_item_failure_code`）。空のプログラムは**手入力へ縮退した正当な形**である。

    問題は、**手入力へ縮退したのではなく何も作れなかった**空のプログラムも、同じ形のために
    「検証を通る」「確定できる」へ数えられることである（実際に後者の例があり、
    **値を1つも取らないのに取得の設計として数えられていた**）。検証では両者を区別できない
    ので、**集計の側で別に数える**ためにこの述語を置く。
    """
    steps = program.get("steps")
    return not isinstance(steps, list) or not steps


def _is_unset(value: Any) -> bool:
    """そのフィールドが「まだ書かれていない」か。

    **`needs_input` が免除するのはこれだけである。** 未設定（キー無し・`None`）と
    空文字を「書いていない」とみなす。`0` や `False` は書かれた値なので免除しない
    （`0 == ""` も `False == ""` も偽なので、この判定で取り違えない）。
    """
    return value is None or value == ""


def validate_program(program: dict[str, Any]) -> list[str]:
    """DSLプログラムを検証し、エラーメッセージのリストを返す（空なら妥当）

    検査内容:
    - steps が配列であること
    - 各 step の op が許可リストに含まれること
    - id が空でなく重複しないこと
    - needs_input=false の step で必須フィールドが揃っていること
    - sql_extract / external_db_extract の sql が SELECT 限定であること
    - compare.cmp / aggregate.func が許可値であること
    - guard が "$id" 参照か真偽リテラルであること（自己参照は禁止）
    - 値参照 "$id"（guard を含む）が「自身より前」に定義された step を指すこと
    - return が存在する step の id を指すこと（任意。未設定は警告ではなくエラーにしない）
    - program.params の宣言が妥当であること（name/kind/value/allowed）
    - 識別子系テンプレート（sql / path_pattern / path / fhir_query）と SQL の
      バインド :name が、宣言済み params か組み込みパラメータのみを参照すること
    - SQL への {{name}} 展開が kind=identifier のパラメータに限られること
    - csv_extract の match の value が、宣言済み params か組み込みパラメータのみを
      参照すること。列指定（key_column / column / match の column）にテンプレートが
      無いこと
    - prompt の {{name}} が、宣言済み params・組み込みパラメータ・**前段 step の id**
      のいずれかを指すこと。prompt の $name は自然文中の字面と
      区別できないため未知の名前は黙認するが、**自 step 参照と後方参照はどちらの
      記法でも弾く**（実行時に解決できずプレースホルダのまま LLM へ渡るため）
    - step の id が params の name とも組み込みパラメータ名とも衝突しないこと

    `needs_input` が免除する範囲:

    **「欠落は許すが、書いてある内容は妥当でなければならない」**。免除するのは
    `_REQUIRED_FIELDS` の充足検査ただ1つで、それ以外の検査は `needs_input` に関わらず
    走る。値域の検査（`compare.cmp` / `select.pick` / `aggregate.func`）と
    `csv_extract` の構造検査は、**フィールドが書かれているときだけ**発火する
    （`_is_unset` を参照）ので、この規則と両立する。

    「書かれている」の単位はフィールドである。**`match` の要素は、要素を書いた時点で
    「書かれた」もの**なので、要素内の `column` / `value` の欠落は免除しない
    （`{"column": "c"}` だけの要素は `value` が無いとして弾く）。`delimiter` は
    空文字を1文字の区切りとして扱えないため、`""` も弾く（`_is_unset` の例外）。

    **免除を「その step は何も検査しない」に広げてはいけない。** パラメータ宣言の網
    （`_PROMPT_FIELDS` / `_TEMPLATE_FIELDS` / `_SELECTOR_FIELDS` の `allowed` 必須）は
    実行時に何を読むかを縛る検査なので、そこを免除すると **`needs_input` を立てるだけで
    検査を回避できる**。`needs_input=true` の step は確定（`confirm=true`）できず実行も
    されないが、`needs_input` は書き手（LLM または人）が立てる旗であって、
    サーバ側の保証ではない。

    したがって「未確定」と明示した step でも、**書きかけの内容そのものが妥当でなければ
    draft の保存が拒否される**。これは意図した挙動である——書いた内容が実行時に必ず
    拒否されることを、確定を待たずに知らせる。
    """
    errors: list[str] = []
    steps = program.get("steps")
    if not isinstance(steps, list):
        return ["program.steps が配列ではありません"]
    if len(steps) > _MAX_STEPS:
        return [f"step 数が上限（{_MAX_STEPS}）を超えています: {len(steps)}"]

    param_errors, specs = _validate_params(program)
    errors.extend(param_errors)

    # step id の全集合。走査中の seen_ids（自身より前の id）と突き合わせて、
    # 「未宣言の名前」と「後方の step を指す名前」を区別するために使う。
    all_step_ids: set[str] = {
        s["id"] for s in steps if isinstance(s, dict) and isinstance(s.get("id"), str)
    }

    seen_ids: set[str] = set()
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"step[{i}] が辞書ではありません")
            continue

        op = step.get("op")
        sid = step.get("id")
        loc = f"step[{i}] (id={sid!r})"

        if op not in OpType.ALL:
            errors.append(f"{loc}: 未知の op: {op!r}")
        if not sid or not isinstance(sid, str):
            errors.append(f"{loc}: id が未設定です")
        elif sid in seen_ids:
            errors.append(f"{loc}: id が重複しています: {sid!r}")
        else:
            seen_ids.add(sid)

        needs_input = bool(step.get("needs_input"))

        def _check_param_ref(name: str, field: str) -> None:
            """パラメータ参照の健全性（前方参照のみ・自己参照禁止）を検査する。

            guard が `elif field == "guard" and r == sid` で塞いでいるのと同じ穴を
            params 側にも塞ぐ。自 step の id は既に seen_ids に入っているため、
            前方参照検査だけでは自己参照を捕まえられない。
            """
            src = specs[name].get("value")
            if not is_ref(src):
                return
            rid = ref_name(src)
            if rid == sid:
                errors.append(
                    f"{loc}: {field} が参照するパラメータ {name} が、この step 自身を"
                    f"参照しています: {src}（実行時に解決できない）"
                )
            elif rid not in seen_ids:
                errors.append(
                    f"{loc}: パラメータ {name} が未定義/後方の step を参照しています: {src}"
                )

        # 必須フィールド（needs_input のときは欠落を許容）
        if op in _REQUIRED_FIELDS and not needs_input:
            required = _REQUIRED_FIELDS[op]
            # 単項の比較演算子は right を取らない。
            if op == OpType.COMPARE and step.get("cmp") in UNARY_COMPARE_OPS:
                required = tuple(f for f in required if f != "right")
            for field in required:
                if step.get(field) in (None, ""):
                    errors.append(f"{loc}: op={op} に必須の {field} が未設定です")

        # **`branch.when` は前段の判定結果への参照である**。
        #
        # `_REQUIRED_FIELDS[branch]` は `when` の**有無**しか見ていなかったので、
        # `when: "not_empty"` のような**意味の無いリテラル文字列**が通っていた。実測で、
        # 構造化出力の型を「参照かリテラル」まで閉じるとモデルはこの形へ逃げた
        # （`branch` は `when` しか必須でないため、そこへ何か入れれば済む）。
        #
        # **これは測定器の欠陥である。** `validate_program` は「手順書から
        # DSL を作れた割合」を数える測定器そのものなので、意味の無いプログラムを「成功」に数えると
        # **報告する数字が実態より良くなる**。必須引数の欠落は落ちるので見えるが、
        # 通ってしまうものは見えない——そちらのほうが質が悪い。
        #
        # 真偽リテラルは許す（`guard` と同じ。常に真の分岐は退化しているが不正ではない）。
        # `needs_input` のときは免除する（まだ書いていないことは許す、の一貫）。
        if op == OpType.BRANCH and not needs_input:
            when = step.get("when")
            if when not in (None, "") and not isinstance(when, bool) and not is_ref(when):
                errors.append(
                    f'{loc}: op=branch の when は前段 step への "$id" 参照か真偽値です'
                    f"（判定の結果を指さないと分岐になりません）: {when!r}"
                )

        # 単項なのに right を書いてあるのは書き手の誤解なので弾く（右辺が無視されて
        # 黙って別の意味になるのを防ぐ）。**needs_input の外に置く。** 必須欠落の許容とは
        # 目的が違い、これは「書いたものが無視される」ことを知らせる検査である。
        if (
            op == OpType.COMPARE
            and step.get("cmp") in UNARY_COMPARE_OPS
            and step.get("right") is not None
        ):
            errors.append(
                f"{loc}: op=compare の cmp={step.get('cmp')} は right を取りません"
                "（左辺が空かどうかだけを見ます）"
            )

        # パスは接続ルート起点の相対パスで書く。
        #
        # **needs_input の外に置く。** 必須欠落の許容とは目的が違い、これは「書いてあるものが
        # 実行時に必ず拒否される」ことを保存時に知らせる検査である（欠落は
        # 許すが、書いてある内容は妥当でなければならない）。実行時のフェイルクローズ
        # （`resolve_under_connection_root`）は残したまま、fail-fast 層を足す。
        if op in (OpType.FILE_EXTRACT, OpType.CSV_EXTRACT):
            for field in ("path_pattern", "path"):
                value = step.get(field)
                if isinstance(value, str) and (
                    PurePosixPath(value).is_absolute() or PureWindowsPath(value).is_absolute()
                ):
                    errors.append(
                        f"{loc}: op={op} の {field} に絶対パスは書けません"
                        "（接続の root_path 起点の相対パスで書きます）"
                    )

        # SQL 安全性
        if op in (OpType.SQL_EXTRACT, OpType.EXTERNAL_DB_EXTRACT):
            sql = step.get("sql")
            if sql and not is_select_only_sql(sql):
                errors.append(f"{loc}: SQLはSELECTのみ許可されます")

        # 値域の検査。**書かれているときだけ発火する**ので `needs_input` の
        # 外に置ける——未設定は上の `_REQUIRED_FIELDS` が（免除つきで）見る担当で、ここは
        # 「書いてある値が語彙に無い」を見る担当である。以前は `not needs_input` で丸ごと
        # 免除しており、`needs_input=true` を立てるだけで綴り違いが素通りした。
        #
        # 綴り違いを通すと**黙って別の意味になる**（`pick="latest"` は executor で first に
        # 化ける）ので、確定を待たずに知らせる。
        if op == OpType.COMPARE:
            cmp_value = step.get("cmp")
            if not _is_unset(cmp_value) and cmp_value not in COMPARE_OPS:
                errors.append(f"{loc}: compare.cmp は {COMPARE_OPS} のいずれかにしてください")

        # select の選択方法（未指定は first。綴り違いを黙って first にしない）
        if op == OpType.SELECT:
            pick = step.get("pick")
            if not _is_unset(pick) and pick not in SELECT_PICKS:
                errors.append(f"{loc}: select.pick は {SELECT_PICKS} のいずれかにしてください")

        # aggregate 関数
        if op == OpType.AGGREGATE:
            func = step.get("func")
            if not _is_unset(func) and func not in AGGREGATE_FUNCS:
                errors.append(
                    f"{loc}: aggregate.func は {AGGREGATE_FUNCS} のいずれかにしてください"
                )

        # csv_extract の列指定（列名=str / 列index=int）。header=false のヘッダ無し
        # CSV は列名を持たないので列index(int)でのみ指定できる。実行時失敗ではなく
        # 確定前に弾けるよう、列指定・match構造・delimiter をここで検証する。
        #
        # **`needs_input` の外に置く**。下の検査はどれも「書かれている値の
        # 形」を見るもので、未設定は素通りする（`_is_unset` で飛ばし、`match` と
        # `delimiter` は書かれていなければ走らない）。
        if op == OpType.CSV_EXTRACT:
            header = step.get("header", True)
            col_specs: list[Any] = [step.get("key_column"), step.get("column")]
            for cond in step.get("match") or []:
                if not isinstance(cond, dict):
                    errors.append(
                        f"{loc}: match 要素は辞書 {{column, value}} にしてください: {cond!r}"
                    )
                    continue
                if _is_unset(cond.get("column")):
                    # `None` と空文字を同じに扱う。ここを `is None` にすると、空文字が
                    # 下の `col_specs` へ流れて `_is_unset` に飛ばされ、**header=false の
                    # 整数index規則を素通りする**（確定できてしまい、実行時に
                    # `col_index("")` が ValueError → schema_mismatch へ落ちる）。
                    errors.append(f"{loc}: match 要素に column が必要です: {cond!r}")
                else:
                    col_specs.append(cond.get("column"))
            for c in col_specs:
                if _is_unset(c):
                    continue
                if isinstance(c, bool) or not isinstance(c, (str, int)):
                    errors.append(
                        f"{loc}: 列指定は文字列(列名)か整数(列index)にしてください: {c!r}"
                    )
                elif placeholder_names(c):
                    # 列指定は「どの列を読むか」を決めるスロットで、パラメータ展開は
                    # 効かない（実行時は列名として literal "{{name}}" を探して失敗する）。
                    # 静かな空振りではなく確定前に弾く。header=false では
                    # 列を整数indexで指定する規則とも両立しないので、展開は導入しない。
                    errors.append(
                        f"{loc}: 列指定に {{{{...}}}} は使えません（パラメータ展開は行われません）: {c!r}"
                    )
                elif header is False and isinstance(c, str):
                    errors.append(
                        f"{loc}: header=false（ヘッダ無し）では列を整数indexで指定してください: {c!r}"
                    )

            # match の value は params 展開の対象（executor が ParamBinder.render_value で
            # 解決する）。未宣言の名前を参照していたら確定前に弾く。実行時は ParamError で
            # step が失敗するので、両層の答えを一致させる（prompt の参照健全性と同じ規則）。
            for cond in step.get("match") or []:
                if not isinstance(cond, dict):
                    continue
                # value スロットも「宣言の網」に載せる。{{...}} に見えるが規則に
                # 合わない綴り（`{{form-code}}` のハイフンなど）はリテラル比較へ落ちて
                # 静かに0件になる。この静かな空振りを確定前に弾く。
                if "value" not in cond:
                    errors.append(f"{loc}: match 要素に value が必要です: {cond!r}")
                raw_value = cond.get("value")
                if raw_value is not None and not isinstance(raw_value, (str, int, float, bool)):
                    errors.append(
                        f'{loc}: match の value はリテラルか {{{{name}}}} / "$id" に'
                        f"してください: {raw_value!r}"
                    )
                if (
                    isinstance(raw_value, str)
                    and ("{{" in raw_value or "}}" in raw_value)
                    and not placeholder_names(raw_value)
                ):
                    errors.append(
                        f"{loc}: match の value のプレースホルダが {{{{name}}}} 形式では"
                        f"ありません: {raw_value!r}"
                    )
                for name in placeholder_names(cond.get("value")):
                    if name in BUILTIN_PARAMS:
                        continue
                    if name not in specs:
                        errors.append(
                            f"{loc}: match の value が未宣言のパラメータを参照しています:"
                            f" {{{{{name}}}}}（params に宣言するか、前段 step の値なら"
                            ' "$id" で参照してください）'
                        )
                        continue
                    _check_param_ref(name, "match.value")
            delim = step.get("delimiter")
            if delim is not None and (not isinstance(delim, str) or len(delim) != 1):
                errors.append(f"{loc}: delimiter は1文字にしてください: {delim!r}")

        # guard（条件付き実行）は "$id" 参照か真偽リテラルのみ許可する。
        #
        # ここを緩めて任意の文字列を許すと、`"guard": "is_inpatient"`（$ 落ち）が
        # 検証を通り、実行時は _truthy("is_inpatient")=True で**常に実行**される。
        # guard が黙って no-op になり、条件付き実行で消したはずの「不要な患者データ取得・
        # 不要な LLM 呼び出し」がエラーも痕跡もなく復活する。実行制御フィールドは
        # 「意図しない truthy」の代償が外部システムアクセスなので、値フィールドより
        # 厳しく縛る。
        if "guard" in step:
            guard = step.get("guard")
            if guard is None or (isinstance(guard, str) and not guard.strip()):
                pass  # 未指定（無条件実行）
            elif not (isinstance(guard, bool) or is_ref(guard)):
                errors.append(
                    f'{loc}: guard は "$id" 参照か真偽リテラル（true/false）に'
                    f"してください: {guard!r}"
                )

        # 参照健全性（自身より前の id のみ参照可）
        for field in _VALUE_FIELDS:
            for r in _collect_refs(step.get(field)):
                if r not in seen_ids:
                    errors.append(f"{loc}: {field} が未定義/後方の step を参照しています: ${r}")
                elif field == "guard" and r == sid:
                    # 自 step の id を指す guard は前方参照チェックをすり抜ける（自身の id は
                    # 既に seen_ids にある）。実行時は未束縛で None＝偽となり、その step が
                    # 恒久的に skip される。skip は failure_code を付けないため取得が
                    # 「静かに」消えるので、確定前にエラーにする。
                    errors.append(f"{loc}: guard が自分自身を参照しています: ${r}")

        # プロンプトの参照健全性。
        #
        # 受理する集合を DSLInterpreter._render_prompt が解決できる集合に合わせる。
        # _render_prompt は context（実行済み step の出力）を置換表へ入れるので、
        # prompt からは params / 組み込み / **前段 step の id** の3つを参照できる。
        # guard で skip された step も needs_input の step も context に None が
        # 束縛されるため、前段でありさえすれば空文字に描画される（実行時エラーには
        # ならない）。一方、自 step の id と後方の step の id は実行時点で context に
        # 無く、プレースホルダが**そのまま LLM へ渡る**ので確定前に弾く。
        #
        # 検査の強さは記法で変える。
        # - {{name}}: 参照であることが字面から確定するので、未知の名前も弾く
        # - $name:    自然文プロンプトでは「$USD」のような単なる字面と区別できない。
        #             未知の名前を弾くと既存の手書きプロンプトを壊すので黙認し、
        #             **確実に誤りである自己参照・後方参照だけ**を弾く
        for field in _PROMPT_FIELDS.get(str(op), ()):
            template = step.get(field)

            for name in placeholder_names(template):
                if name in BUILTIN_PARAMS:
                    continue
                if name in specs:
                    _check_param_ref(name, field)
                    continue
                if name == sid:
                    errors.append(
                        f"{loc}: {field} が自分自身の step を参照しています: {{{{{name}}}}}"
                        "（実行時はまだ値が無く、プレースホルダのまま LLM へ渡る）"
                    )
                elif name in all_step_ids and name not in seen_ids:
                    errors.append(
                        f"{loc}: {field} が後方の step を参照しています: {{{{{name}}}}}"
                        "（step の順序を入れ替えてください）"
                    )
                elif name not in seen_ids:
                    errors.append(
                        f"{loc}: {field} が未宣言のパラメータを参照しています: {{{{{name}}}}}"
                        "（params に宣言してください）"
                    )
                # 残りは前段 step の id。_render_prompt が context から解決する。

            for name in prompt_dollar_names(template):
                if name in BUILTIN_PARAMS:
                    continue
                if name in specs:
                    _check_param_ref(name, field)
                    continue
                if name == sid:
                    errors.append(
                        f"{loc}: {field} が自分自身の step を参照しています: ${name}"
                        "（実行時はまだ値が無く、$ 参照のまま LLM へ渡る）"
                    )
                elif name in all_step_ids and name not in seen_ids:
                    errors.append(
                        f"{loc}: {field} が後方の step を参照しています: ${name}"
                        "（step の順序を入れ替えてください）"
                    )

        # パラメータ参照健全性（識別子系テンプレートと SQL バインド）
        for field in _TEMPLATE_FIELDS.get(str(op), ()):
            template = step.get(field)
            names = placeholder_names(template)
            # セレクタ検査（allowed 必須）の対象は {{name}} 展開のみ。SQL の :name は
            # ドライバがパラメータ化するので「どの表を読むか」を選べず、選ぶのは
            # 「どの行か」だけである。prompt を対象外にしたのと同じ基準（値が読み取り
            # 対象を選ぶか）を当てると、バインドも対象外になる。
            placeholders = set(names)
            if field == "sql":
                # {{name}} は識別子展開、:name はバインド値。求める kind が違う。
                for name in names:
                    if name in BUILTIN_PARAMS:
                        # 組み込みは kind=value 相当なので SQL のテキストへは展開できない
                        # （render_sql が実行時に ParamError にする）。確定前に弾く。
                        errors.append(
                            f"{loc}: SQLへの {{{{{name}}}}} 展開はできません"
                            "（組み込みパラメータは :name バインドで参照してください）"
                        )
                    elif name in specs and (specs[name].get("kind") or "value") != "identifier":
                        errors.append(
                            f"{loc}: SQLへの {{{{{name}}}}} 展開は kind=identifier のパラメータ"
                            "のみ許可されます（値の差し込みは :name バインドを使う）"
                        )
                names = names | sql_bind_names(template)
            for name in sorted(names):
                if name in BUILTIN_PARAMS:
                    continue
                if name not in specs:
                    errors.append(
                        f"{loc}: {field} が未宣言のパラメータを参照しています: {name}"
                        "（program.params に宣言してください）"
                    )
                    continue
                # 前段 step 由来のパラメータは、その step が先に定義済みでなければならない
                _check_param_ref(name, field)
                # 「どこから読むか」を決めるスロット（識別子・パス・FHIRクエリ）へ
                # 前段 step の出力を差し込むなら allowed が必須。字種検査は「形」しか
                # 縛らないため、`path_pattern: "{{dir}}/note.txt"` の {{dir}} に別被験者の
                # ディレクトリ名が入れば、その被験者の記述を当該被験者の値として確定保存
                # してしまう（接続ルート配下なのでパストラバーサルの関門も止めない）。
                # prompt は「読む対象」ではなく「解析されるデータ」なので対象外
                # （_wrap_injected でデリミタに囲まれる）。
                if (
                    field in _SELECTOR_FIELDS
                    and name in placeholders
                    and is_ref(specs[name].get("value"))
                    and specs[name].get("allowed") is None
                ):
                    errors.append(
                        f"{loc}: {field} へ差し込む {name} は前段 step 由来なので"
                        " allowed（許可値の列挙）が必須です"
                        "（実行時のデータが読み取り対象を選ぶのを防ぐため）"
                    )

    # params と step id の名前衝突（プロンプト描画で取り違えるため禁止）
    for name in sorted(specs.keys() & seen_ids):
        errors.append(f"params の name が step の id と衝突しています: {name!r}")

    # 組み込みパラメータ名と step id の衝突も同じ理由で禁止する。
    # specs に組み込みは入らないため上の検査では捕まらない。衝突すると
    # _render_prompt が冒頭で {{patient_id}} を患者IDへ直接置換するので、
    # prompt に書いた {{patient_id}} が step の出力ではなく患者IDになる
    # （しかもデータデリミタで囲まれない）。取り違えの向きが params 衝突と同じ。
    for name in sorted(set(BUILTIN_PARAMS) & seen_ids):
        errors.append(
            f"step の id が組み込みパラメータ名と衝突しています: {name!r}"
            "（prompt では組み込みが優先され、step の出力を参照できなくなる）"
        )

    # return の参照
    ret = program.get("return") or program.get("return_")
    if ret:
        if not is_ref(ret):
            errors.append(f'return は "$id" 形式にしてください: {ret!r}')
        elif ref_name(ret) not in seen_ids:
            errors.append(f"return が未定義の step を参照しています: {ret}")

    return errors


# --------------------------------------------------------------------------- #
# 構造化出力のスキーマ
# --------------------------------------------------------------------------- #

# 全 op に共通のフィールド。どの variant にも置く。
_COMMON_STEP_FIELDS = ("op", "id", "needs_input", "reasoning", "guard")

# **値を取る引数は「`$id` 参照かリテラル」である**（`is_ref` / `ref_name` を参照）。
# `DSLStep` ではこれらが `Any`（JSON スキーマ上は `{}`）なので、**任意のオブジェクトが入る**。
# 実測で、モデルは
#   `left` に `branch` の step を丸ごと入れ、
#   `when` に `{"not_empty": "$sql_basic"}` を入れ、
#   `then` に `{"op": "return", "value": "$sql_basic"}` を入れた。
# `validate_program` はこれを弾かない（`cmp` の欠落だけを報告していた）ので、
# **スキーマで型を閉じるのがこの穴の唯一の関門**である。
#
# `right` だけは配列も許す（`in` / `contains` の右辺）。
_SCALAR = ({"type": "string"}, {"type": "number"}, {"type": "boolean"}, {"type": "null"})
_VALUE_SCHEMA: dict[str, Any] = {"anyOf": [dict(t) for t in _SCALAR]}
_VALUE_OR_LIST_SCHEMA: dict[str, Any] = {
    "anyOf": [
        *(dict(t) for t in _SCALAR),
        {"type": "array", "items": {"anyOf": [dict(t) for t in _SCALAR[:3]]}},
    ]
}
# スキーマで型を閉じる引数。**モジュール冒頭の `_VALUE_FIELDS`（参照健全性の検査対象）とは
# 別物なので名前を分ける。** 同名にすると後の定義が前を上書きし、`right` / `source` /
# `match` / `guard` の前方参照検査が黙って外れる（一度そう書いて気付いた）。
_SCHEMA_VALUE_FIELDS = ("left", "then", "else_branch", "input")

# **`branch.when` は前段の判定結果を指す `$id` 参照である。** 型を
# `anyOf: [string, ...]` まで閉じても、実測でモデルは `when: "not_empty"` という
# **意味の無いリテラル文字列**を書いた（`branch` は `when` しか必須でないので、そこへ
# 何か入れて済ませられる）。`validate_program` に「`when` は参照」の検査が無いため通る。
#
# `pattern` が GBNF へ落ちることは実測で確認した（生成で `$` が強制された）。
# 真偽リテラルは許す——`guard` と同じで、常に真の分岐は退化しているが不正ではない。
_REF_PATTERN = r"^\$[A-Za-z][A-Za-z0-9_]*$"
_WHEN_SCHEMA: dict[str, Any] = {
    "anyOf": [{"type": "string", "pattern": _REF_PATTERN}, {"type": "boolean"}]
}


def step_schema_variants() -> list[dict[str, Any]]:
    """op ごとの step スキーマ（判別可能な合併型の variant）を返す。

    **`_REQUIRED_FIELDS` から導く。** スキーマを手で書くと検証との強さが必ずずれる。
    ずれ方は2つあり、どちらも害がある。

    - スキーマが**緩い**と、今回のように必須引数が欠けた program が生成される。
      ほとんどの項目で `_repair_op_args` が走っていたが、**是正できるのは取得系だけ**で
      （索引の payload から焼き込める）、`compare` / `branch` / `transform` は
      索引に情報が無いので救済経路が無い。過半が検証で落ちた
    - スキーマが**厳しい**と、検証は通るのに生成できない program ができる。
      たとえば `branch.then` は `_REQUIRED_FIELDS` に無いので必須にしない

    **`op` は `const` にする。** 現行は `DSLStep.op` が `str` で語彙が載らないため、
    実測でモデルが `assert` や `create` を出した。どちらも
    語彙外である（`validate_program` が必須引数で先に落としていたので見えていなかった）。
    これは必須引数の欠落とは独立した穴なので、閉じたことをテストで固定する。

    `compare` だけ variant を2つに割る。`cmp` が単項（`UNARY_COMPARE_OPS`）のときは
    `right` が免除されるだけでなく、**書いてあると `validate_program` が弾く**（
    「書いたものが無視される」ことを知らせる検査）。したがって単項の variant からは
    `right` を**プロパティごと外し** `additionalProperties: False` を付ける。

    **`additionalProperties: False` は `compare` の2 variant だけに付ける。** 他の op は
    正当に使える省略可の引数を全部列挙しないと、通るはずの program を弾いてしまう。
    `compare` は実行時に `left` / `cmp` / `right` しか読まないので列挙が閉じている。
    """
    from kawasemi.ai_input.schemas import DSLStep

    base_props = DSLStep.model_json_schema().get("properties", {})

    def _typed(props: dict[str, Any]) -> dict[str, Any]:
        """値を取る引数の型を「`$id` 参照かリテラル」へ閉じる（オブジェクトを入れさせない）。"""
        for f in _SCHEMA_VALUE_FIELDS:
            if f in props:
                props[f] = dict(_VALUE_SCHEMA)
        if "right" in props:
            props["right"] = dict(_VALUE_OR_LIST_SCHEMA)
        if "when" in props:
            props["when"] = dict(_WHEN_SCHEMA)
        return props

    def _variant(op: str, required: tuple[str, ...]) -> dict[str, Any]:
        props = _typed({k: dict(v) for k, v in base_props.items()})
        props["op"] = {"const": op}
        return {
            "type": "object",
            "properties": props,
            "required": ["op", "id", *required],
        }

    def _compare_variant(cmps: tuple[str, ...], *, binary: bool) -> dict[str, Any]:
        props = {k: dict(base_props[k]) for k in _COMMON_STEP_FIELDS if k in base_props}
        props["op"] = {"const": OpType.COMPARE}
        props["left"] = dict(_VALUE_SCHEMA)
        props["cmp"] = {"enum": list(cmps)}
        required = ["op", "id", "left", "cmp"]
        if binary:
            props["right"] = dict(_VALUE_OR_LIST_SCHEMA)
            required.append("right")
        return {
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        }

    def _pending(variant: dict[str, Any]) -> dict[str, Any]:
        """`needs_input=true`（人が後で埋める）版。op 固有引数を必須にしない。

        **必須にしてはいけない。** `validate_program` は `needs_input=true` のとき必須欠落を
        許容する（「まだ書いていないことは許す」）。スキーマで無条件に必須化すると、
        モデルは「決められない」と申告する代わりに**引数を捏造する**。

        `needs_input=true` が立つ理由は「索引に接続の種別が無い」「同じ所在候補を先行 step が
        使用済み」などである。**これは決定不能を正直に申告している記録**であって、埋めさせて
        よいものではない。
        埋めさせると手順書から DSL を作れた割合が実態より良く出る。
        """
        out = {k: v for k, v in variant.items() if k != "required"}
        out["properties"] = dict(variant["properties"])
        out["properties"]["needs_input"] = {"const": True}
        out["required"] = ["op", "id", "needs_input"]
        return out

    def _determined(variant: dict[str, Any]) -> dict[str, Any]:
        out = dict(variant)
        out["properties"] = dict(variant["properties"])
        out["properties"]["needs_input"] = {"const": False}
        return out

    base: list[dict[str, Any]] = []
    for op in OpType.ALL:
        if op == OpType.COMPARE:
            binary_ops = tuple(c for c in COMPARE_OPS if c not in UNARY_COMPARE_OPS)
            base.append(_compare_variant(UNARY_COMPARE_OPS, binary=False))
            base.append(_compare_variant(binary_ops, binary=True))
            continue
        base.append(_variant(op, _REQUIRED_FIELDS.get(op, ())))

    # **`needs_input` で割る。** 割らないと「必須引数を書く」か「決定不能を申告する」の
    # どちらかしか選べなくなる。検証は両方を認めているので、スキーマも両方を認める。
    variants: list[dict[str, Any]] = []
    for v in base:
        variants.append(_determined(v))
        variants.append(_pending(v))
    return variants


def step_schema() -> dict[str, Any]:
    """step 1件のスキーマ（`oneOf` の合併型）。

    **`discriminator` は付けない。** `propertyName: "op"` では `compare` の2 variant
    （どちらも `op == "compare"`）を割れない。素の `oneOf` で honor されることは
    実測で確認してある（単項では `right` が出ず、二項では出た）。
    """
    return {"oneOf": step_schema_variants()}
