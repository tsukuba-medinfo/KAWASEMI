"""所在候補の統計プロファイル生成（所在判定の材料）

統計情報の取り扱い規則をコードに埋め込む。人が SQL を書いて
判定材料を用意する経路を使わせない（手書きは条件（対応関係の排斥）を破りやすい）。

出してよいもの: 総件数・非NULL率・値の種類数・度数が閾値以上のセルの度数・
四分位点と中央値。
出してはいけないもの（コードで禁止）: 最小値と最大値・度数が閾値未満のセルの
実数・稀な値の列挙・自由記載の実文字列・日単位の日付分布。

## 設計

対象は `local_db` / `external_db` の候補のみ（`file` は本文を読まないため対象外。
呼び出し側は `applicable=False` を見て「文書種別の存在確認は別途行う」ものとして
扱うこと）。

SQL に埋め込む表・カラム名は接続の種類で出所が違う（`derive_op_args` と同じ規則）。

- 外部DB（`external_db_extract`）: **`payload.physical` の schema / table / column**。
  `db_qualified` は定義書＝元システム側の名前で、実DBには無い表を指す
  （実行すると「表が存在しない（invalid object name）」で落ちる）。SQL は形として成立するので
  実行するまで誤りが見えない。
  3つが揃わない候補は `applicable=False` で返し、誤った SQL を組まない（fail-safe）
- 自アプリDB（`sql_extract`）: `payload.db_qualified`（表）/ `payload.physical_name`（カラム）。
  自分のスキーマなので読み替えは起きない

どちらも `resource_index.derive.SQL_IDENT` で検証したうえで SQL テンプレートへ直接埋め込む。
この正規表現は英数字・`_`・`$`・`#`・`.` のみを許可し、引用符・空白・セミコロン等の
SQL 特殊文字を構造的に排除するため、パラメータ化できない識別子位置の安全な埋め込み
として成立する（`derive.py` が DSL 側の識別子検証に使っているのと同じ規則）。

接続は `resource_index.derive.resolve_db_op` で scope を判定する。scope が
解決できない接続は None を返し（フェイルクローズ）、本モジュールは
`applicable=False` として扱う（任意の表をアプリ本体のDBセッションで読む事故を防ぐ、
`resolve_db_op` の docstring参照）。

## 個票の値を扱う範囲について

`GROUP BY {column}` で集計した (値, 件数) のペアはDBから取得するが、**値そのものは
出力に含めない**（度数のみを出力する）。日付型の値は年へ丸めたうえで再集計し、
四分位点・中央値の算出にのみ使う。集計後の (値, 件数) はこの関数のスコープを出た
時点で破棄され、ログにもキャッシュにも残さない（呼び出し元でも保存しないこと）。

行本体（個々の患者の生の値の集合）を要求するクエリは書かない。取得するのは常に
「値ごとに GROUP BY 済みの件数」であり、これは最初から集計値である。
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from kawasemi.ai_input.db_retry import RetriesExhausted, with_retry
from kawasemi.ai_input.dsl import is_select_only_sql
from kawasemi.ai_input.models import OpType, SourceCategory
from kawasemi.ai_input.resource_index.connections import get_registry
from kawasemi.ai_input.resource_index.derive import (
    SQL_IDENT,
    has_physical_identifiers,
    resolve_db_op,
)
from kawasemi.core.logging_config import get_logger
from kawasemi.core.readonly_db import (
    ProgramSqlDeniedError,
    ReadOnlySessionUnavailableError,
    assert_relations_permitted,
    readonly_program_sql,
)

logger = get_logger(__name__)

# 取り扱い規則「度数が10以上のセルの度数」の既定閾値。安全側に置く
# （小さくするほど個人を特定しうるセルが漏れやすくなるため、既定は変更しない前提）。
DEFAULT_MIN_CELL_SIZE = 10

# **外部DBは一時的なエラーを返すことがある**（`db_retry` の docstring を参照）。
# 再試行の仕組み（10秒 × 上限3回）は実行ロールと共有する（`db_retry`）。
#
# **黙って再試行しない。** 何回目で成功したかを `attempts` として結果に残す
# ——再試行を要したこと自体が外部DBについての事実で、判定材料の読み方に効く。

# 四分位点・中央値の算出に使うパーセンタイル（最小値・最大値の代替は
# 第5/第95パーセンタイルか定性判定）。中央値・四分位点自体は取り扱い規則で明示的に許可。
_PERCENTILES = ("p5", "q1", "median", "q3", "p95")
_PERCENTILE_POINTS = (5, 25, 50, 75, 95)


@dataclass(frozen=True)
class _GroupedRow:
    value: Any
    count: int


def _not_applicable(reason: str, source_category: str | None) -> dict[str, Any]:
    return {
        "applicable": False,
        "source_category": source_category,
        "reason": reason,
    }


def _rules_applied(min_cell_size: int) -> dict[str, Any]:
    """出力に適用した規則の記録。"""
    return {
        "min_cell_size": min_cell_size,
        "min_max_suppressed": True,
        "date_granularity": "year",
        "free_text_values_suppressed": True,
        "reference": "統計情報の取り扱い規則（出してよいもの・出してはいけないもの）",
    }


_ISO_DATE_PREFIX = re.compile(r"^(\d{4})-\d{2}-\d{2}")


def _year_of(value: Any) -> int | None:
    """値が日付なら年を返す。日付でなければ None。

    SQLite は DATE 型の実体を持たず、生の DBAPI 経由では ISO8601文字列
    （例: "2024-01-15"）としてそのまま返る（sqlite3標準の格納規約）。
    date/datetime インスタンスに加えてこの文字列形も日付として認識しないと、
    SQLite バックエンドで日単位の値がそのまま distinct_count・度数へ漏れる。
    """
    if isinstance(value, date | datetime):
        return value.year
    if isinstance(value, str):
        m = _ISO_DATE_PREFIX.match(value)
        if m:
            return int(m.group(1))
    return None


def _bucket_by_year(rows: list[_GroupedRow]) -> tuple[list[_GroupedRow], bool]:
    """値が日付なら年単位へ再集計する（取り扱い規則「日単位の日付分布は年単位まで丸める」）。

    値そのものは年（int）に置き換わるため、日単位の値は出力は元より内部状態にも
    残らない。

    全行が日付と判定できたときのみ日付列として扱う（`_weighted_percentiles` の
    数値判定と同じ「全部一致でなければ何もしない」方針）。一部だけ日付らしく見える
    列を日付列とみなすと、日付でない行がここで黙って捨てられ非NULL件数が過小に
    報告される。
    """
    years = [_year_of(r.value) for r in rows]
    is_date = bool(years) and all(y is not None for y in years)
    if not is_date:
        return rows, False
    year_counts: dict[int, int] = {}
    for r, year in zip(rows, years, strict=True):
        assert year is not None  # is_date=True の時点で全件 not None
        year_counts[year] = year_counts.get(year, 0) + r.count
    return [_GroupedRow(value=y, count=c) for y, c in year_counts.items()], True


def _weighted_percentiles(
    rows: list[_GroupedRow], *, min_cell_size: int
) -> dict[str, float] | None:
    """(値, 件数) の集計から重み付き分位点を計算する。

    累積度数の計算には全セルの件数を使う（分布全体の形を保つため）が、**分位点
    として返す値は必ず閾値以上のセルのものに限る**。目的の位置がちょうど閾値未満
    （個票を一意に指しうる）セルに落ちた場合は、そこから後方→前方の順で最初に
    見つかる閾値以上セルの値へスナップする。

    閾値未満セルをそのまま候補位置に使うと、外れ値1件のセルが分布の端に来た
    ときにその生値が p5/p95 としてそのまま出てしまう（MIN/MAX 禁止規則の脱法
    経路になる）。
    """
    numeric_rows = [
        r for r in rows if isinstance(r.value, int | float) and not isinstance(r.value, bool)
    ]
    if len(numeric_rows) != len(rows) or not numeric_rows:
        return None
    ordered = sorted(numeric_rows, key=lambda r: r.value)
    total = sum(r.count for r in ordered)
    if total == 0 or not any(r.count >= min_cell_size for r in ordered):
        return None

    def _at(fraction: float) -> float:
        target = fraction * total
        cumulative = 0
        landing = len(ordered) - 1
        for i, r in enumerate(ordered):
            cumulative += r.count
            if cumulative >= target:
                landing = i
                break
        # 着地点が閾値未満セルなら、後方→前方の順で最初の閾値以上セルへスナップする。
        # 全体に閾値以上セルが1つ以上あることは呼び出し前に確認済みなので必ず見つかる。
        for i in range(landing, len(ordered)):
            if ordered[i].count >= min_cell_size:
                return float(ordered[i].value)
        for i in range(landing, -1, -1):
            if ordered[i].count >= min_cell_size:
                return float(ordered[i].value)
        raise AssertionError("到達しないはず: 閾値以上セルの存在は事前に確認済み")

    return {name: _at(p / 100) for name, p in zip(_PERCENTILES, _PERCENTILE_POINTS, strict=True)}


def _summarize_rows(rows: list[_GroupedRow], *, min_cell_size: int) -> dict[str, Any]:
    rows, is_year_bucketed = _bucket_by_year(rows)
    non_null_count = sum(r.count for r in rows)
    distinct_count = len(rows)
    at_or_above = sorted((r.count for r in rows if r.count >= min_cell_size), reverse=True)
    below_threshold_cell_count = sum(1 for r in rows if r.count < min_cell_size)

    central_tendency = None
    if non_null_count >= min_cell_size:
        percentiles = _weighted_percentiles(rows, min_cell_size=min_cell_size)
        if percentiles is not None:
            central_tendency = {
                "kind": "year" if is_year_bucketed else "numeric",
                **percentiles,
            }

    return {
        "non_null_count": non_null_count,
        "distinct_count": distinct_count,
        "cell_frequencies_at_or_above_threshold": at_or_above,
        "below_threshold_cell_count": below_threshold_cell_count,
        "central_tendency": central_tendency,
    }


async def _with_retry[T](
    label: str,
    call: Callable[[], Awaitable[T]],
    *,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> tuple[T, int]:
    """`db_retry.with_retry` を、上限後に**元の例外をそのまま**上げる形で包む。

    握り潰すと「表が存在しない」と区別できなくなる。このモジュールの呼び出し側は
    `SQLAlchemyError` を直接捕まえるので、`RetriesExhausted` ではなく元の例外を渡す。
    """
    try:
        return await with_retry(label, call, sleep=sleep)
    except RetriesExhausted as e:
        raise cast(Exception, e.__cause__) from None


async def _fetch_grouped_rows(
    conn: AsyncSession | Any, table: str, column: str, *, check_relations: bool = False
) -> list[_GroupedRow]:
    sql = f"SELECT {column} AS v, COUNT(*) AS c FROM {table} WHERE {column} IS NOT NULL GROUP BY {column}"
    if not is_select_only_sql(sql):
        raise ValueError("生成した集計SQLがSELECT限定に反します")
    if check_relations:
        await assert_relations_permitted(conn, sql)
    result = await conn.execute(text(sql))
    return [_GroupedRow(value=row[0], count=row[1]) for row in result.fetchall()]


async def _fetch_total_count(
    conn: AsyncSession | Any, table: str, *, check_relations: bool = False
) -> int:
    sql = f"SELECT COUNT(*) FROM {table}"
    if not is_select_only_sql(sql):
        raise ValueError("生成した集計SQLがSELECT限定に反します")
    if check_relations:
        await assert_relations_permitted(conn, sql)
    result = await conn.execute(text(sql))
    row = result.fetchone()
    return int(row[0]) if row else 0


async def generate_statistical_profile(
    candidate: Mapping[str, Any],
    db: AsyncSession,
    *,
    min_cell_size: int = DEFAULT_MIN_CELL_SIZE,
) -> dict[str, Any]:
    """所在候補1件の統計プロファイルを生成する。

    対象は `local_db` / `external_db` の候補のみ。`file` はここでは
    `applicable=False` を返す（本文を読まないため対象外）。
    `fhir` / `manual` / `llm` / `not_available` も対象外。

    Returns:
        `applicable=False` の場合は `reason` のみを持つ。`True` の場合は
        `total_count` / `non_null_count` / `non_null_rate` / `distinct_count` /
        `cell_frequencies_at_or_above_threshold`（度数のみのリスト、値は含まない）/
        `below_threshold_cell_count` / `central_tendency`（分位点、算出不能なら
        None）/ `rules_applied`（適用した規則の記録）を持つ。
    """
    source_category = candidate.get("source_category")
    if source_category == SourceCategory.FILE:
        return _not_applicable(
            "文書系（file）は本文を読まないため対象外。文書種別の存在確認は別途行う",
            source_category,
        )
    if source_category not in (SourceCategory.LOCAL_DB, SourceCategory.EXTERNAL_DB):
        return _not_applicable(
            f"対象外の source_category（{source_category}）",
            source_category,
        )

    payload = candidate.get("payload") or {}
    registry = get_registry()
    op = resolve_db_op(candidate, registry)
    if op is None:
        return _not_applicable(
            "接続の scope が解決できません（"
            + registry.unresolved_reason(
                candidate.get("connection_id") or payload.get("connection_id")
            )
            + "）",
            source_category,
        )

    if op == OpType.EXTERNAL_DB_EXTRACT:
        # 外部DBは `db_qualified`（定義書側の名前）では引けない。
        if not has_physical_identifiers(payload):
            return _not_applicable(
                "外部DBで実行できる識別子（payload.physical の schema/table/column）が無い。"
                "db_qualified は定義書上の名前で、実DBには無い表を指すため SQL を組まない",
                source_category,
            )
        physical = payload["physical"]
        table = f"{physical['schema']}.{physical['table']}"
        column = str(physical["column"])
    else:
        table_raw = payload.get("db_qualified")
        column_raw = payload.get("physical_name")
        if not table_raw or not column_raw:
            return _not_applicable(
                "候補に表・カラムの索引メタ情報（db_qualified/physical_name）が無い",
                source_category,
            )
        table = str(table_raw)
        column = str(column_raw)
    if not (SQL_IDENT.match(table) and SQL_IDENT.match(column)):
        return _not_applicable(
            "表名・カラム名が識別子として不正な形式です",
            source_category,
        )

    try:
        if op == OpType.SQL_EXTRACT:
            # **自アプリDBは再試行しない。** 一時的なエラーへの備えは外部DBに対する
            # もので、ローカルのセッションはその対象ではない。
            # 加えて、失敗したセッションを同じトランザクションのまま投げ直すと別の失敗を招く。
            #
            # **DSL の SQL と同じ関門を通す**（`readonly_program_sql`）。SQL は識別子検査を
            # 通した表・カラム名から組むが、保証は文字列検査ではなく DB 側に置く。
            async with readonly_program_sql(db) as reader:
                total_count = await _fetch_total_count(reader, table, check_relations=True)
                rows = await _fetch_grouped_rows(reader, table, column, check_relations=True)
            attempts = 1
        else:
            connection_id = candidate.get("connection_id") or payload.get("connection_id")
            dsn = registry.dsn_of(connection_id)
            if not dsn:
                return _not_applicable(
                    f"外部DB接続が解決できません（connection_id={connection_id!r}）",
                    source_category,
                )
            engine = create_async_engine(dsn, pool_pre_ping=True, hide_parameters=True)

            async def _read() -> tuple[int, list[_GroupedRow]]:
                # **接続の取得ごと投げ直す。** 同じ `AsyncConnection` を使い回して投げ直すと、
                # **一時的なエラーが接続を壊す種類（切断・リセット）のときに必ず失敗する**
                # ——SQLAlchemy はその接続を invalidate するので、2回目以降は
                # `PendingRollbackError`「Can't reconnect until invalid transaction is rolled
                # back」になる（実測）。しかも最後に上がる例外がそれに化けるので、**施設の
                # DWH が返した本当のエラーがログにも記録にも残らない**。
                #
                # 「投げ直す」は接続からやり直す意味に取る。`pool_pre_ping=True` と合わせて、毎回生きた接続で引き直す。
                async with engine.connect() as conn:
                    return (
                        await _fetch_total_count(conn, table),
                        await _fetch_grouped_rows(conn, table, column),
                    )

            try:
                (total_count, rows), attempts = await _with_retry(
                    f"統計プロファイルの取得（{table}.{column}）", _read
                )
            finally:
                await engine.dispose()
    except ProgramSqlDeniedError as e:
        # 読ませない表（アプリ自身の表・カタログ）を指す候補は集計しない
        logger.warning(f"統計プロファイルの集計を拒否しました: {e}")
        return _not_applicable(str(e), source_category)
    except ReadOnlySessionUnavailableError as e:
        # フェイルクローズ（production で読み取り専用ロールが無い）。黙って読み書きで実行しない
        logger.error(f"統計プロファイルの集計を実行しませんでした: {e}")
        return _not_applicable(str(e), source_category)
    except SQLAlchemyError as e:
        logger.warning(f"統計プロファイル生成に失敗: table={table} column={column} error={e}")
        return _not_applicable(
            "集計クエリの実行に失敗しました（表・カラムが実在しない可能性があります）",
            source_category,
        )

    summary = _summarize_rows(rows, min_cell_size=min_cell_size)
    non_null_count = summary.pop("non_null_count")
    non_null_rate = round(non_null_count / total_count * 100, 1) if total_count else 0.0

    return {
        "applicable": True,
        "source_category": source_category,
        "total_count": total_count,
        "non_null_count": non_null_count,
        "non_null_rate": non_null_rate,
        **summary,
        # **何回目の試行で取れたかを残す**。1 なら一発、2以上なら一時的な
        # エラーの後に取れたということで、それは外部DBについての事実である。
        # 黙って再試行して隠すと、判定材料の読み手がその事実を知る手段が無くなる。
        "attempts": attempts,
        "rules_applied": _rules_applied(min_cell_size),
    }
