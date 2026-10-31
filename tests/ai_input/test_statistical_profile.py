"""所在候補の統計プロファイル生成のテスト

出してはいけないもの（下に挙げる4つ）が実際に出ないことを固定する:
- 最小値・最大値
- 度数が閾値未満のセルの実数
- 自由記載の実文字列
- 日単位の日付分布
"""

from __future__ import annotations

import json
from datetime import date

import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input import db_retry
from kawasemi.ai_input import statistical_profile as sp
from kawasemi.ai_input.resource_index.connections import ConnectionRegistry

APP_DB_REGISTRY = ConnectionRegistry({"app_db": {"kind": "db", "scope": "self"}})


@pytest.fixture(autouse=True)
def _fixed_registry(monkeypatch: pytest.MonkeyPatch) -> ConnectionRegistry:
    """statistical_profile モジュールの get_registry を固定レジストリへ差し替える。"""
    monkeypatch.setattr(sp, "get_registry", lambda: APP_DB_REGISTRY)
    return APP_DB_REGISTRY


def _local_candidate(table: str, column: str, *, connection_id: str | None = "app_db") -> dict:
    return {
        "source_category": "local_db",
        "location": f"{table}.{column}",
        "unit_id": f"db:{table}.{column}",
        "connection_id": connection_id,
        "payload": {"db_qualified": table, "physical_name": column},
    }


def _json_contains(profile: dict, needle: str) -> bool:
    return needle in json.dumps(profile, default=str, ensure_ascii=False)


def _has_min_or_max_key(obj: object) -> bool:
    """出力のどこかに `min`/`max`（完全一致・大文字小文字無視）というキーが無いか再帰確認する。

    `min_cell_size` 等は語として "min" を含むが別概念（閾値の下限件数）であり誤検出になる
    ため、部分一致ではなく完全一致で見る。
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k.strip().lower() in ("min", "max"):
                return True
            if _has_min_or_max_key(v):
                return True
    elif isinstance(obj, list):
        return any(_has_min_or_max_key(v) for v in obj)
    return False


# --------------------------------------------------------------------------- #
# 純粋関数
# --------------------------------------------------------------------------- #


def test_year_of_handles_date_instance_and_iso_string():
    assert sp._year_of(date(2020, 5, 1)) == 2020
    assert sp._year_of("2020-05-01") == 2020
    assert sp._year_of("2020-05-01 12:00:00") == 2020
    assert sp._year_of("free text value") is None
    assert sp._year_of(42) is None


def test_bucket_by_year_aggregates_same_year():
    rows = [
        sp._GroupedRow(value="2020-01-01", count=3),
        sp._GroupedRow(value="2020-06-15", count=4),
        sp._GroupedRow(value="2021-01-01", count=2),
    ]
    bucketed, is_date = _bucket_result(rows)
    assert is_date is True
    by_year = {r.value: r.count for r in bucketed}
    assert by_year == {2020: 7, 2021: 2}


def _bucket_result(rows):
    return sp._bucket_by_year(rows)


def test_bucket_by_year_requires_unanimous_date_values():
    """一部だけ日付らしい混在列は日付列として扱わない（非日付行の件数消失を防ぐ）。

    以前は1件でも日付と判定されれば列全体を日付扱いし、日付でない行を黙って
    捨てていた（非NULL件数が過小報告される回帰）。
    """
    rows = [
        sp._GroupedRow(value="2020-01-01", count=5),
        sp._GroupedRow(value="自由記載テキスト", count=8),
    ]
    bucketed, is_date = _bucket_result(rows)
    assert is_date is False
    assert sum(r.count for r in bucketed) == 13  # 混在時は件数が失われない


def test_weighted_percentiles_median_of_simple_distribution():
    # 1..100 の均等分布。閾値(10)を超える度数にして「閾値以上セルのみ返す」制約を満たす。
    rows = [sp._GroupedRow(value=v, count=15) for v in range(1, 101)]
    result = sp._weighted_percentiles(rows, min_cell_size=10)
    assert result is not None
    assert result["median"] == pytest.approx(50, abs=1)
    assert result["q1"] < result["median"] < result["q3"]
    assert result["p5"] < result["q1"]
    assert result["q3"] < result["p95"]


def test_weighted_percentiles_never_returns_below_threshold_value():
    """分位点として返す値は閾値以上セルのものに限る（MIN/MAX禁止の脱法経路を塞ぐ）。

    外れ値1件（count=1、閾値未満）が分布の端に来ると、素朴な累積度数計算では
    その生値がそのまま p95 として返ってしまう回帰があった。
    """
    rows = [
        sp._GroupedRow(value=1, count=14),
        sp._GroupedRow(value=999999, count=1),  # 閾値未満の外れ値
    ]
    result = sp._weighted_percentiles(rows, min_cell_size=10)
    assert result is not None
    assert all(v != 999999 for v in result.values())
    assert set(result.values()) == {1.0}


def test_weighted_percentiles_none_when_no_cell_meets_threshold():
    rows = [sp._GroupedRow(value=v, count=1) for v in range(1, 6)]
    assert sp._weighted_percentiles(rows, min_cell_size=10) is None


def test_summarize_rows_suppresses_below_threshold_cells():
    rows = [
        sp._GroupedRow(value="A", count=15),
        sp._GroupedRow(value="B", count=12),
        sp._GroupedRow(value="C", count=3),  # 閾値未満
        sp._GroupedRow(value="D", count=1),  # 閾値未満
    ]
    result = sp._summarize_rows(rows, min_cell_size=10)
    assert sorted(result["cell_frequencies_at_or_above_threshold"]) == [12, 15]
    assert result["below_threshold_cell_count"] == 2
    assert result["distinct_count"] == 4
    assert result["non_null_count"] == 31


def test_summarize_rows_no_central_tendency_when_below_min_cell_size():
    rows = [sp._GroupedRow(value=v, count=1) for v in range(5)]  # 合計5件 < 10
    result = sp._summarize_rows(rows, min_cell_size=10)
    assert result["central_tendency"] is None


# --------------------------------------------------------------------------- #
# generate_statistical_profile（DBアクセスあり、local_db = app_db 経由）
# --------------------------------------------------------------------------- #


async def _make_numeric_table(
    db_session: AsyncSession, table: str, values: list[int | None]
) -> None:
    await db_session.execute(text(f"CREATE TABLE {table} (v INTEGER)"))
    for v in values:
        await db_session.execute(text(f"INSERT INTO {table} (v) VALUES (:v)"), {"v": v})
    await db_session.commit()


@pytest.mark.asyncio
async def test_profile_basic_counts(db_session: AsyncSession):
    values = [1] * 12 + [2] * 11 + [None] * 3
    await _make_numeric_table(db_session, "stats_basic", values)

    profile = await sp.generate_statistical_profile(
        _local_candidate("stats_basic", "v"), db_session
    )
    assert profile["applicable"] is True
    assert profile["total_count"] == len(values)
    assert profile["non_null_count"] == 23
    assert profile["non_null_rate"] == pytest.approx(23 / 26 * 100, abs=0.1)
    assert profile["distinct_count"] == 2


@pytest.mark.asyncio
async def test_profile_never_exposes_min_or_max(db_session: AsyncSession):
    """最小値・最大値を出力する経路が存在しない"""
    # 999999 は count=1（閾値未満）の外れ値。閾値以上の値は 1 と 2 のみ。
    values = [1] * 15 + [2] * 15 + [999999]
    await _make_numeric_table(db_session, "stats_extreme", values)

    profile = await sp.generate_statistical_profile(
        _local_candidate("stats_extreme", "v"), db_session
    )
    assert profile["applicable"] is True
    assert not _json_contains(profile, "999999")
    assert not _has_min_or_max_key(profile)
    # 度数が閾値未満のセルは below_threshold_cell_count のみに反映される
    assert profile["below_threshold_cell_count"] == 1
    assert sorted(profile["cell_frequencies_at_or_above_threshold"]) == [15, 15]


@pytest.mark.asyncio
async def test_profile_below_threshold_counts_not_leaked(db_session: AsyncSession):
    """度数が閾値未満のセルが実数で出力されない"""
    values = [1] * 10 + [2] * 3 + [3] * 1  # 2,3 は閾値(10)未満
    await _make_numeric_table(db_session, "stats_threshold", values)

    profile = await sp.generate_statistical_profile(
        _local_candidate("stats_threshold", "v"), db_session
    )
    # 閾値未満セルの実数（3件・1件）はどちらも度数リストに現れない
    assert profile["cell_frequencies_at_or_above_threshold"] == [10]
    assert profile["below_threshold_cell_count"] == 2


@pytest.mark.asyncio
async def test_profile_free_text_value_never_quoted(db_session: AsyncSession):
    """自由記載の実文字列が出力されない（度数が閾値以上でも値そのものは出さない）"""
    distinctive = "この患者固有の自由記載テキスト12345"
    await db_session.execute(text("CREATE TABLE stats_text (v TEXT)"))
    for _ in range(20):
        await db_session.execute(text("INSERT INTO stats_text (v) VALUES (:v)"), {"v": distinctive})
    await db_session.commit()

    profile = await sp.generate_statistical_profile(_local_candidate("stats_text", "v"), db_session)
    assert profile["applicable"] is True
    assert not _json_contains(profile, distinctive)
    # 度数(20)は出るが、値そのものは出ない
    assert profile["cell_frequencies_at_or_above_threshold"] == [20]


@pytest.mark.asyncio
async def test_profile_date_column_bucketed_to_year(db_session: AsyncSession):
    """日単位の日付分布が年単位より細かく出力されない"""
    await db_session.execute(text("CREATE TABLE stats_dates (d DATE)"))
    # 2020年に12件（日ごとに1件、日単位では全部閾値未満）、2021年に15件
    for day in range(1, 13):
        await db_session.execute(
            text("INSERT INTO stats_dates (d) VALUES (:d)"), {"d": date(2020, 1, day)}
        )
    for day in range(1, 16):
        await db_session.execute(
            text("INSERT INTO stats_dates (d) VALUES (:d)"), {"d": date(2021, 1, day)}
        )
    await db_session.commit()

    profile = await sp.generate_statistical_profile(
        _local_candidate("stats_dates", "d"), db_session
    )
    assert profile["applicable"] is True
    # 年単位でのdistinct_countは2（2020, 2021）。日単位なら27になるはず。
    assert profile["distinct_count"] == 2
    assert sorted(profile["cell_frequencies_at_or_above_threshold"]) == [12, 15]
    assert profile["central_tendency"]["kind"] == "year"
    # 日付の生文字列（"2020-01-05" など）がどこにも出ない
    assert not _json_contains(profile, "2020-01-05")
    assert not _json_contains(profile, "-01-")


@pytest.mark.asyncio
async def test_profile_rules_applied_recorded(db_session: AsyncSession):
    """丸めや除外の適用が出力に記録される"""
    await _make_numeric_table(db_session, "stats_rules", [1] * 10)
    profile = await sp.generate_statistical_profile(
        _local_candidate("stats_rules", "v"), db_session
    )
    assert profile["rules_applied"]["min_cell_size"] == 10
    assert profile["rules_applied"]["min_max_suppressed"] is True
    assert profile["rules_applied"]["date_granularity"] == "year"
    assert profile["rules_applied"]["free_text_values_suppressed"] is True


def test_default_min_cell_size_matches_the_default_disclosure_rule():
    """既定閾値は10である（安全側の既定）"""
    assert sp.DEFAULT_MIN_CELL_SIZE == 10


@pytest.mark.asyncio
async def test_profile_not_applicable_for_file_category(db_session: AsyncSession):
    candidate = {"source_category": "file", "payload": {}}
    profile = await sp.generate_statistical_profile(candidate, db_session)
    assert profile["applicable"] is False
    assert "file" not in profile.get("rules_applied", {})


@pytest.mark.asyncio
async def test_profile_not_applicable_without_payload_metadata(db_session: AsyncSession):
    candidate = {"source_category": "local_db", "connection_id": "app_db", "payload": {}}
    profile = await sp.generate_statistical_profile(candidate, db_session)
    assert profile["applicable"] is False


@pytest.mark.asyncio
async def test_profile_not_applicable_when_connection_unregistered(db_session: AsyncSession):
    """接続が未登録なら手入力の local_db 自称を信用しない（フェイルクローズ）"""
    candidate = _local_candidate("stats_basic", "v", connection_id=None)
    profile = await sp.generate_statistical_profile(candidate, db_session)
    assert profile["applicable"] is False


@pytest.mark.asyncio
async def test_profile_not_applicable_for_malformed_identifier(db_session: AsyncSession):
    candidate = _local_candidate("stats_basic; DROP TABLE users", "v")
    profile = await sp.generate_statistical_profile(candidate, db_session)
    assert profile["applicable"] is False


# --------------------------------------------------------------------------- #
# external_db 経路
# --------------------------------------------------------------------------- #


# 外部DBは `payload.physical`（実DBの識別子）から SQL を組む。`db_qualified` は
# 定義書側の名前で、実DBには無い表を指す。ここでは**わざと実在しない名前**にして、
# 使われていないことをテストで効かせる。
_EXT_PAYLOAD = {
    "db_qualified": "NOT_IN_THE_REAL_DB.ext_stats",
    "physical_name": "NOT_A_COLUMN",
    "physical": {"schema": "main", "table": "ext_stats", "column": "v"},
    "connection_id": "dwh",
}


@pytest.mark.asyncio
async def test_profile_external_db_path(
    tmp_path, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
):
    import aiosqlite  # noqa: F401  (存在確認。未導入なら明示的にskipしたいがdevで導入済み)
    from sqlalchemy.ext.asyncio import create_async_engine

    db_path = tmp_path / "external.db"
    dsn = f"sqlite+aiosqlite:///{db_path}"

    engine = create_async_engine(dsn)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE ext_stats (v INTEGER)"))
        for v in [1] * 11 + [2] * 11:
            await conn.execute(text("INSERT INTO ext_stats (v) VALUES (:v)"), {"v": v})
    await engine.dispose()

    registry = ConnectionRegistry({"dwh": {"kind": "db", "scope": "external", "secret_ref": dsn}})
    monkeypatch.setattr(sp, "get_registry", lambda: registry)

    candidate = {
        "source_category": "external_db",
        "connection_id": "dwh",
        "payload": _EXT_PAYLOAD,
    }
    profile = await sp.generate_statistical_profile(candidate, db_session)
    assert profile["applicable"] is True
    assert profile["total_count"] == 22
    assert sorted(profile["cell_frequencies_at_or_above_threshold"]) == [11, 11]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "physical",
    [
        None,
        {},
        {"schema": "main", "table": "ext_stats"},
        {"schema": "", "table": "ext_stats", "column": "v"},
        {"schema": "main", "table": "ext_stats", "column": "v; DROP TABLE x"},
    ],
    ids=["absent", "empty", "no-column", "blank-schema", "malformed-column"],
)
async def test_external_db_without_usable_physical_identifiers_is_not_applicable(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, physical
) -> None:
    """`db_qualified` しか無い外部DB候補では SQL を組まない。

    `db_qualified` は定義書上の名前で実DBには無い（「表が存在しない（invalid object name）」で落ちる）。SQL は形として
    成立するので、実行するまで誤りが見えない。**組まずに `applicable=False` で返す**。
    """
    registry = ConnectionRegistry(
        {"dwh": {"kind": "db", "scope": "external", "secret_ref": "sqlite+aiosqlite://"}}
    )
    monkeypatch.setattr(sp, "get_registry", lambda: registry)

    def _no_engine(*a, **k):  # noqa: ANN002, ANN003, ANN202
        raise AssertionError("識別子が揃わないのに接続を作った")

    monkeypatch.setattr(sp, "create_async_engine", _no_engine)

    payload = {"db_qualified": "SRC.DOCUMENT", "physical_name": "v", "connection_id": "dwh"}
    if physical is not None:
        payload["physical"] = physical
    candidate = {"source_category": "external_db", "connection_id": "dwh", "payload": payload}

    profile = await sp.generate_statistical_profile(candidate, db_session)

    assert profile["applicable"] is False
    assert profile["reason"]


@pytest.mark.asyncio
async def test_external_db_sql_is_built_from_physical_not_db_qualified(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """組まれた SQL に出てくる表は `payload.physical` の `schema.table`。"""
    registry = ConnectionRegistry(
        {"dwh": {"kind": "db", "scope": "external", "secret_ref": "sqlite+aiosqlite://"}}
    )
    monkeypatch.setattr(sp, "get_registry", lambda: registry)
    seen: list[tuple[str, str]] = []

    async def _stop(conn, table):  # noqa: ANN001, ANN202
        seen.append(("count", table))
        return 0

    async def _rows(conn, table, column):  # noqa: ANN001, ANN202
        seen.append((table, column))
        return []

    monkeypatch.setattr(sp, "_fetch_total_count", _stop)
    monkeypatch.setattr(sp, "_fetch_grouped_rows", _rows)

    candidate = {
        "source_category": "external_db",
        "connection_id": "dwh",
        "payload": {
            "db_qualified": "SRC.DOCUMENT",
            "physical_name": "DEFINITION_SIDE_NAME",
            "physical": {"schema": "ehr", "table": "DOCUMENT", "column": "REAL_COLUMN"},
            "connection_id": "dwh",
        },
    }
    await sp.generate_statistical_profile(candidate, db_session)

    assert ("count", "ehr.DOCUMENT") in seen
    assert ("ehr.DOCUMENT", "REAL_COLUMN") in seen
    assert all("SRC" not in t and c != "DEFINITION_SIDE_NAME" for t, c in seen)


# --------------------------------------------------------------------------- #
# 一時的なエラーの再試行
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_a_transient_error_is_retried_after_waiting() -> None:
    """一時的なエラーは待って投げ直す。

    外部DBの一時的なエラーは、少し待って投げ直せば通ることが多い（`db_retry` を参照）。
    待機の実時間はテストへ持ち込まず、`sleep` を差し替えて**待った長さ**だけを見る。
    """
    calls = 0
    waited: list[float] = []

    async def flaky() -> str:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise SQLAlchemyError("一時的なエラー")
        return "ok"

    async def fake_sleep(seconds: float) -> None:
        waited.append(seconds)

    value, attempts = await sp._with_retry("テスト", flaky, sleep=fake_sleep)

    assert value == "ok"
    assert attempts == 2, "2回目で成功したことが残っていない"
    assert waited == [db_retry.RETRY_WAIT_SECONDS]


@pytest.mark.asyncio
async def test_the_retry_gives_up_at_the_limit_and_raises() -> None:
    """上限まで失敗したら例外をそのまま上げる。

    **握り潰さない。** 握り潰すと「一時的なエラーが続いた」と「表が存在しない」を
    区別できなくなり、所在の確からしさの判定材料として誤って読まれる。
    """
    calls = 0
    waited: list[float] = []

    async def always_fails() -> str:
        nonlocal calls
        calls += 1
        raise SQLAlchemyError("落ちている")

    async def fake_sleep(seconds: float) -> None:
        waited.append(seconds)

    with pytest.raises(SQLAlchemyError, match="落ちている"):
        await sp._with_retry("テスト", always_fails, sleep=fake_sleep)

    assert calls == db_retry.RETRY_MAX_ATTEMPTS
    assert len(waited) == db_retry.RETRY_MAX_ATTEMPTS - 1


@pytest.mark.asyncio
async def test_a_success_on_the_first_try_does_not_wait() -> None:
    """正常時に待たない（再試行の実装が常時10秒を足していないこと）。"""
    waited: list[float] = []

    async def ok() -> int:
        return 1

    async def fake_sleep(seconds: float) -> None:
        waited.append(seconds)

    value, attempts = await sp._with_retry("テスト", ok, sleep=fake_sleep)

    assert (value, attempts) == (1, 1)
    assert waited == []


@pytest.mark.asyncio
async def test_a_broken_connection_is_retried_from_a_fresh_connection(
    tmp_path, monkeypatch: pytest.MonkeyPatch, db_session: AsyncSession
) -> None:
    """**接続が壊れる種類**の一時的エラーでも、投げ直して取れること。

    同じ `AsyncConnection` を使い回して投げ直す実装だと、ここは**必ず**失敗する。
    SQLAlchemy は壊れた接続を invalidate するので、2回目以降は `PendingRollbackError`
    「Can't reconnect until invalid transaction is rolled back」になり、上限まで数えて
    最後に上がる例外もそれに化ける——**外部DBが返した本当のエラーが、ログにも
    判定材料にも残らない**。「投げ直す」は接続からやり直す意味に取る。
    """
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.ext.asyncio import create_async_engine

    db_path = tmp_path / "flaky.db"
    dsn = f"sqlite+aiosqlite:///{db_path}"
    engine = create_async_engine(dsn)
    async with engine.begin() as conn:
        await conn.execute(text("CREATE TABLE ext_stats (v INTEGER)"))
        for v in [1] * 11 + [2] * 11:
            await conn.execute(text("INSERT INTO ext_stats (v) VALUES (:v)"), {"v": v})
    await engine.dispose()

    registry = ConnectionRegistry({"dwh": {"kind": "db", "scope": "external", "secret_ref": dsn}})
    monkeypatch.setattr(sp, "get_registry", lambda: registry)
    monkeypatch.setattr(db_retry, "RETRY_WAIT_SECONDS", 0.0)  # 待機の実時間は持ち込まない

    real_total_count = sp._fetch_total_count
    calls = {"n": 0}

    async def flaky(conn, table):  # noqa: ANN001, ANN202
        calls["n"] += 1
        if calls["n"] == 1:
            # **クエリを投げた後に落ちた状態**を作る。最初の execute で暗黙の
            # トランザクションが始まり、そこで接続が invalidate されるのが実際の姿である
            # （投げる前に落ちていたなら、同じ接続でも再接続できてしまい再現にならない）。
            await conn.execute(text("SELECT 1"))
            await conn.invalidate()
            raise OperationalError("SELECT COUNT(*)", {}, Exception("接続が切れた"))
        return await real_total_count(conn, table)

    monkeypatch.setattr(sp, "_fetch_total_count", flaky)

    candidate = {
        "source_category": "external_db",
        "connection_id": "dwh",
        "payload": _EXT_PAYLOAD,
    }
    profile = await sp.generate_statistical_profile(candidate, db_session)

    assert profile["applicable"] is True, profile.get("reason")
    assert profile["total_count"] == 22
    assert profile["attempts"] == 2, "何回目で取れたかが残っていない"
