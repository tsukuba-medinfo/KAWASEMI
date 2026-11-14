"""移行が作る DB とモデルの既定値の食い違いを検出する

**移行を実際に走らせて測る。** テストの既定は `Base.metadata.create_all` で、そちらは
モデルの `server_default` をそのまま DDL にするので、**移行側が書き忘れていても緑になる**。
同じ穴は `tests/test_stage_vocabulary_migration.py` の docstring が既に名指ししている。

見つかった実害: `ai_input_design_llm_calls.created_at` は移行が
`server_default` 無しの NOT NULL で作っていた。`TimestampMixin` は値を送らない前提なので
ORM の INSERT が必ず `IntegrityError` になり、**alembic で作った DB では設計ロールの
来歴が1行も保存できなかった**。テストは全件緑のままだった。

ここで測るのは2つ。**モデルが `server_default` を宣言している列は、移行が作る DB でも
既定値を持つ**ことと、**`String` の列長が移行と ORM で一致している**こと。
列を1つずつ列挙するのではなく `Base.metadata` から導くので、**この先に足される表と列にも
自動で効く**。

列長がずれると、モデルが許す長さの値が保存時に落ちる。設計ロールの来歴では
`save_design_llm_calls_safely` がその例外を飲むので、**そのロールの記録が丸ごと消え、
確認書からも消える**（「AI を使っていない」と「記録に失敗した」が区別できなくなる）。
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import String

# `Base.metadata` を埋めるために全モデルを読み込む（alembic/env.py と同じ理由）。
import kawasemi.ai_input.models  # noqa: F401
import kawasemi.models  # noqa: F401
from kawasemi.models.base import Base

_REPO = Path(__file__).resolve().parents[1]


def _upgrade_head(db_path: Path) -> subprocess.CompletedProcess[str]:
    """`env.py` は `settings.database_url` を読むので `DATABASE_URL` で渡す。"""
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}"}
    return subprocess.run(
        ["uv", "run", "alembic", "upgrade", "head"],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
    )


@pytest.fixture(scope="module")
def migrated_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """`alembic upgrade head` で作った DB のパス（**移行の実行はここ1回だけ**）。"""
    db_path = tmp_path_factory.mktemp("migrated") / "kawasemi.db"
    result = _upgrade_head(db_path)
    assert result.returncode == 0, f"alembic upgrade head が失敗した:\n{result.stderr}"
    return db_path


@pytest.fixture(scope="module")
def migrated_schema(migrated_db: Path) -> dict[str, dict[str, str | None]]:
    """移行が作った DB の {表: {列: 既定値}} を返す。"""
    schema: dict[str, dict[str, str | None]] = {}
    with sqlite3.connect(migrated_db) as conn:
        for table in _table_names(conn):
            schema[table] = {
                r[1]: r[4]
                for r in conn.execute(f'PRAGMA table_info("{table}")')  # name, dflt_value
            }
    return schema


@pytest.fixture(scope="module")
def migrated_types(migrated_db: Path) -> dict[str, dict[str, str]]:
    """移行が作った DB の {表: {列: 宣言型}} を返す（`VARCHAR(128)` など）。"""
    types: dict[str, dict[str, str]] = {}
    with sqlite3.connect(migrated_db) as conn:
        for table in _table_names(conn):
            types[table] = {
                r[1]: r[2]
                for r in conn.execute(f'PRAGMA table_info("{table}")')  # name, type
            }
    return types


def _table_names(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )
    ]


def test_columns_declaring_a_server_default_have_one_in_the_migrated_db(
    migrated_schema: dict[str, dict[str, str | None]],
) -> None:
    """モデルが `server_default` を宣言した列は、移行が作った DB でも既定値を持つ。

    持たないと、値を送らない ORM の INSERT が `NOT NULL constraint failed` で落ちる
    （**呼び出し側は成功として返るので気づけない**）。
    """
    missing: list[str] = []
    for table in Base.metadata.sorted_tables:
        migrated = migrated_schema.get(table.name)
        if migrated is None:
            continue  # 移行が作らない表（テスト専用など）は対象外
        for column in table.columns:
            if column.server_default is None:
                continue
            if column.name not in migrated:
                continue  # 列そのものの欠落は別の検査の担当
            if migrated[column.name] is None:
                missing.append(f"{table.name}.{column.name}")

    assert not missing, (
        "モデルは server_default を宣言しているのに、移行が作った DB に既定値が無い列がある。"
        "この列は ORM 経由の INSERT を必ず失敗させる: " + ", ".join(sorted(missing))
    )


def test_the_design_llm_calls_timestamps_are_the_regression_this_guards(
    migrated_schema: dict[str, dict[str, str | None]],
) -> None:
    """実害の出た `ai_input_design_llm_calls` の時刻列を名指しで押さえる。

    上の一般の検査は `Base.metadata` から導くので、**モデル側の宣言が消えると一緒に
    緑へ戻る**（守っている対象が静かに空になる）。実害の出た2列は値で固定しておく。
    """
    timestamps = migrated_schema["ai_input_design_llm_calls"]
    for column in ("created_at", "updated_at"):
        assert timestamps[column] is not None, (
            f"ai_input_design_llm_calls.{column} に既定値が無い。"
            "設計ロールの来歴が1行も保存できない状態である"
        )


def test_string_column_lengths_match_between_the_migration_and_the_models(
    migrated_types: dict[str, dict[str, str]],
) -> None:
    """`String(length=N)` の N が移行と ORM で一致すること。

    定数・ORM・移行の3者のうち、**定数と ORM の一致だけが検査されていた**
    （`tests/ai_input/test_llm_provider.py::test_the_clamp_lengths_match_the_columns`）。
    移行側だけ古い長さのまま残ると、`create_all` で作るテストは緑のまま、alembic で
    作った DB だけが保存時に落ちる。既定値の書き忘れと同じ形の穴である。
    """
    mismatched: list[str] = []
    checked = 0
    for table in Base.metadata.sorted_tables:
        migrated = migrated_types.get(table.name)
        if migrated is None:
            continue  # 移行が作らない表は対象外
        for column in table.columns:
            if not isinstance(column.type, String) or column.type.length is None:
                continue
            declared = migrated.get(column.name)
            if declared is None:
                continue  # 列そのものの欠落は別の検査の担当
            checked += 1
            m = re.fullmatch(r"VARCHAR\((\d+)\)", declared)
            actual = int(m.group(1)) if m else None
            if actual != column.type.length:
                mismatched.append(
                    f"{table.name}.{column.name}: モデル={column.type.length} / 移行={declared}"
                )

    assert checked, "String の列長を1つも検査できていない（フィクスチャが壊れている）"
    assert not mismatched, (
        "移行とモデルで String の列長が食い違っている。モデルが許す長さの値が"
        "保存時に落ちる: " + " / ".join(sorted(mismatched))
    )


def test_the_digest_columns_are_the_lengths_the_clamp_assumes(
    migrated_types: dict[str, dict[str, str]],
) -> None:
    """ダイジェストの2列を名指しで押さえる。

    上の一般の検査は `Base.metadata` から導くので、**モデル側の宣言が縮むと移行と
    一緒に緑へ戻る**。打ち切り長（`llm_provider` の定数）が前提にしている値で固定する。
    """
    from kawasemi.ai_input.llm_provider import DIGEST_MAX_LENGTH, DIGEST_REASON_MAX_LENGTH

    calls = migrated_types["ai_input_design_llm_calls"]
    assert calls["model_digest"] == f"VARCHAR({DIGEST_MAX_LENGTH})"
    assert calls["model_digest_unavailable"] == f"VARCHAR({DIGEST_REASON_MAX_LENGTH})"


# --------------------------------------------------------------------------- #
# ORM 宣言と物理制約の食い違い
#
# **SQLite は既存テーブルへの `ALTER ADD CONSTRAINT` / `ALTER COLUMN` を持たない。**
# そのため、後から足した外部キーと NOT NULL は物理制約にならず、参照整合性・非NULL性は
# ORM 宣言とアプリ層が担保している。各マイグレーションの docstring に理由が明記された
# **意図的な設計判断**であり、見落としではない。
#
# ここで守るのは「**既知の差分だけであること**」である。差分が常に非空なので、
# `alembic revision --autogenerate` の出力を目で見る運用は
# 「毎回出る13件」に紛れて新しい差分を見落とす。集合を固定すれば、増えたときに落ちる。
# --------------------------------------------------------------------------- #

# 物理 FK を張っていない外部キー宣言。**追加するときは、なぜ張らないのかを
# マイグレーションの docstring に書いてからここへ足すこと。**
_KNOWN_MISSING_FOREIGN_KEYS = frozenset(
    {
        "ai_input_executions.batch_job_id",
        "ai_input_executions.subject_id",
        "ai_input_plans.confirmed_by_id",
        "ai_input_runbooks.confirmed_by_id",
        "ai_input_trial_runs.executed_by_id",
        "case_assignments.subject_id",
        "clinical_annotations.confirmed_by_id",
        "input_targets.subject_id",
        "questionnaire_responses.submitted_by_id",
        "questionnaire_responses.verified_by_id",
        "questionnaire_responses.last_edited_by_id",
        # 後から増えた1件。`5476e155ca8a` が
        # 「物理FKを張らず index のみ付与する」と docstring に明記している。
        "system_locations.updated_by_id",
    }
)

# 物理 NOT NULL になっていない非NULL宣言。既定値を ORM 側が持つ。
_KNOWN_NULLABLE_IN_DB = frozenset(
    {
        "clinical_annotations.citations",
        "clinical_annotations.status",
        "system_locations.candidates",
    }
)


def _physical_foreign_keys(db_path: Path, table: str) -> set[str]:
    with sqlite3.connect(db_path) as conn:
        return {r[3] for r in conn.execute(f'PRAGMA foreign_key_list("{table}")')}


def test_the_foreign_keys_without_a_physical_constraint_are_exactly_the_known_ones(
    migrated_db: Path,
) -> None:
    """物理 FK を張っていない外部キー宣言が、既知の集合と一致すること。

    **増えたら落ちる。** 差分が常に非空なので、autogenerate の出力を目で見る運用では
    新しい差分が「毎回出る差分」に紛れる。**減っても落ちる**——物理制約化したなら
    この一覧から消すのが正しく、消し忘れると検査が空になる。
    """
    with sqlite3.connect(migrated_db) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}

    missing: set[str] = set()
    for table in Base.metadata.sorted_tables:
        if table.name not in tables:
            continue
        physical = _physical_foreign_keys(migrated_db, table.name)
        for column in table.columns:
            if column.foreign_keys and column.name not in physical:
                missing.add(f"{table.name}.{column.name}")

    assert missing == _KNOWN_MISSING_FOREIGN_KEYS, (
        "物理 FK の無い外部キー宣言が既知の集合と違う。"
        f"増えた: {sorted(missing - _KNOWN_MISSING_FOREIGN_KEYS)} / "
        f"減った: {sorted(_KNOWN_MISSING_FOREIGN_KEYS - missing)}"
    )


def test_the_not_null_columns_without_a_physical_constraint_are_exactly_the_known_ones(
    migrated_schema: dict[str, dict[str, str | None]],
    migrated_db: Path,
) -> None:
    """物理 NOT NULL になっていない非NULL宣言が、既知の集合と一致すること。"""
    nullable_in_db: set[str] = set()
    with sqlite3.connect(migrated_db) as conn:
        for table in Base.metadata.sorted_tables:
            if table.name not in migrated_schema:
                continue
            info = {r[1]: r for r in conn.execute(f'PRAGMA table_info("{table.name}")')}
            for column in table.columns:
                row = info.get(column.name)
                if row is None or column.primary_key:
                    continue
                if not column.nullable and not row[3]:  # row[3] = notnull
                    nullable_in_db.add(f"{table.name}.{column.name}")

    assert nullable_in_db == _KNOWN_NULLABLE_IN_DB, (
        "物理 NOT NULL の無い非NULL宣言が既知の集合と違う。"
        f"増えた: {sorted(nullable_in_db - _KNOWN_NULLABLE_IN_DB)} / "
        f"減った: {sorted(_KNOWN_NULLABLE_IN_DB - nullable_in_db)}"
    )


def test_the_known_differences_name_real_columns() -> None:
    """既知の一覧が実在の表・列を指していること（綴り間違いで検査が空にならない）。

    集合の一致だけを見る検査は、**一覧側の綴りが実装と一緒にずれると通り続ける**。
    片方だけの変更で落ちるよう、名前の実在をここで見る。
    """
    for entry in _KNOWN_MISSING_FOREIGN_KEYS | _KNOWN_NULLABLE_IN_DB:
        table_name, _, column_name = entry.partition(".")
        table = Base.metadata.tables.get(table_name)
        assert table is not None, f"存在しない表: {entry}"
        assert column_name in table.columns, f"存在しない列: {entry}"
