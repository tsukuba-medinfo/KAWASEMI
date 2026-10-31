"""段階の語彙の CHECK 制約を足す移行のテスト

**移行を実際に走らせて測る。** 制約そのものは ORM のメタデータにもあるので、
`Base.metadata.create_all` の環境（テストの既定）では移行を1行も書かなくても
`tests/ai_input/test_location_assessments.py` の DB 層のテストが緑になる——
**移行が無い／壊れていることを検出できない**。

ここで測るのは2つ。
1. **既存行に語彙外があれば移行を中止する**（黙って `IntegrityError` で落ちるのではなく、
   何件が語彙外かを先に報告する）。**これは機構であって記録ではない**——既存 DB に
   語彙外の行が無かったことの確認は、このテストでは担保しない
2. **移行後の DB が語彙外の挿入を拒否する**（ORM を通らない経路が止まる）

alembic は別プロセスで走らせる（`env.py` は `settings.database_url` を読むので、
`DATABASE_URL` を環境変数で渡す）。
"""

from __future__ import annotations

import os
import sqlite3
import subprocess
from pathlib import Path

import pytest

from kawasemi.ai_input.models import AssessmentStage

# この移行の1つ前（CHECK 制約が無い状態）
_BEFORE = "c7d8e9f0a1b2"
_THIS = "d8e9f0a1b2c3"

_REPO = Path(__file__).resolve().parents[1]

_ASSESSMENTS = "location_candidate_assessments"


def _alembic(db_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}"}
    return subprocess.run(
        ["uv", "run", "alembic", *args],
        cwd=_REPO,
        env=env,
        capture_output=True,
        text=True,
    )


def _insert_assessment(
    db_path: Path, *, stage: object, cand_key: str = "k", undeterminable: int = 0
) -> None:
    """`location_candidate_assessments` へ1行入れる（所在は無ければ作る）。

    生 SQL で書くのは、**このテストが塞ごうとしている経路（ORM を通らない書き込み）**
    そのものだからである。`cand_key` を変えれば同じ所在に複数行入る（一意制約は
    `(system_location_id, cand_key, round_number)`）。
    """
    with sqlite3.connect(db_path) as conn:
        if conn.execute("SELECT COUNT(*) FROM system_locations").fetchone()[0]:
            loc_id = conn.execute("SELECT id FROM system_locations").fetchone()[0]
            conn.execute(
                "INSERT INTO location_candidate_assessments (system_location_id, cand_key, "
                "candidate_snapshot, round_number, stage, is_undeterminable, rationale, "
                "is_self_assessment, assessed_at, created_at, updated_at) "
                "VALUES (?, ?, '{}', 1, ?, ?, '移行テスト', 0, CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                (loc_id, cand_key, stage, undeterminable),
            )
            conn.commit()
            return
        conn.execute(
            "INSERT INTO questionnaires (fhir_id, title, status, content, version, "
            "is_deleted, created_at, updated_at) VALUES ('q-mig', '移行テスト', "
            "'active', '{}', 1, 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        )
        qid = conn.execute("SELECT id FROM questionnaires").fetchone()[0]
        conn.execute(
            "INSERT INTO system_locations (questionnaire_id, link_id, candidates, "
            "created_at, updated_at) VALUES (?, 'item1', '[]', CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP)",
            (qid,),
        )
        loc_id = conn.execute("SELECT id FROM system_locations").fetchone()[0]
        conn.execute(
            "INSERT INTO location_candidate_assessments (system_location_id, cand_key, "
            "candidate_snapshot, round_number, stage, is_undeterminable, rationale, "
            "is_self_assessment, assessed_at, created_at, updated_at) "
            "VALUES (?, ?, '{}', 1, ?, ?, '移行テスト', 0, CURRENT_TIMESTAMP, "
            "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
            (loc_id, cand_key, stage, undeterminable),
        )
        conn.commit()


def test_the_migration_stops_when_an_existing_row_is_out_of_vocabulary(tmp_path: Path) -> None:
    """**語彙外の既存行があれば移行を中止し、件数を報告する**こと。

    判定記録は追記のみなので、値を直すか行を消すかは**記録の管理者による判断**が要る。
    移行が黙って行を書き換えたり、制約だけ諦めて成功したりしてはいけない。
    """
    db = tmp_path / "mig.db"
    before = _alembic(db, "upgrade", _BEFORE)
    assert before.returncode == 0, before.stderr[-2000:]

    # **語彙外を3件・語彙内を2件**入れる。1件だと「件数をハードコードする」
    # 退行が素通りする。語彙内を混ぜるのは、検査が全行を語彙外と
    # 報告する実装でも緑にならないようにするため。
    #
    # **`stage IS NULL` かつ `is_undeterminable = 0` も語彙外**である。
    # `stage` 単体を見る検査はこの行を通し、**制約は拒否する**——
    # 「検査は 0 件と言ったのに `IntegrityError` で落ちる」状態になる。
    _insert_assessment(db, stage=0, cand_key="a")
    _insert_assessment(db, stage=9, cand_key="b")
    _insert_assessment(db, stage=None, cand_key="d", undeterminable=0)
    _insert_assessment(db, stage=3, cand_key="c")
    _insert_assessment(db, stage=None, cand_key="e", undeterminable=1)  # 判定不能（語彙内）

    failed = _alembic(db, "upgrade", _THIS)
    assert failed.returncode != 0, "語彙外の行があるのに移行が成功している"
    combined = failed.stdout + failed.stderr
    assert "語彙外" in combined and "3 件" in combined, combined[-2000:]
    assert "5 件" not in combined, "語彙内の行まで語彙外として数えている"
    # **制約は作られていない**（中止したので）
    with sqlite3.connect(db) as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'location_candidate_assessments'"
        ).fetchone()[0]
    assert AssessmentStage.CHECK_NAME not in sql

    # 行を直せば移行は通る（前へ進める道が残っている）
    with sqlite3.connect(db) as conn:
        conn.execute(
            "UPDATE location_candidate_assessments SET stage = 3, is_undeterminable = 0 "
            "WHERE stage IS NULL AND is_undeterminable = 0"
        )
        conn.execute(
            "UPDATE location_candidate_assessments SET stage = 3 WHERE stage NOT IN (4, 3, 2, 1)"
        )
        conn.commit()
    fixed = _alembic(db, "upgrade", _THIS)
    assert fixed.returncode == 0, (fixed.stdout + fixed.stderr)[-2000:]


def test_the_migrated_db_rejects_an_out_of_vocabulary_stage(tmp_path: Path) -> None:
    """**移行後の DB は語彙外を拒否し、語彙内と null は通す**こと。

    肯定側も測る（`stage IN ()` のような「全部拒否」でも否定側だけなら緑になる）。
    """
    db = tmp_path / "mig_ok.db"
    up = _alembic(db, "upgrade", "head")
    assert up.returncode == 0, up.stderr[-2000:]
    with sqlite3.connect(db) as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'location_candidate_assessments'"
        ).fetchone()[0]
    assert AssessmentStage.CHECK_NAME in sql, "移行が CHECK 制約を作っていない"

    with pytest.raises(sqlite3.IntegrityError):
        _insert_assessment(db, stage=0)
    # **排反も DB が守る**。移行が作る制約と ORM の制約は同じ式
    # （`AssessmentStage.check_sql()`）だが、**移行を走らせた DB で測らないと移行側は無検証**である
    with pytest.raises(sqlite3.IntegrityError):
        _insert_assessment(db, stage=None, cand_key="null-not-und", undeterminable=0)
    with pytest.raises(sqlite3.IntegrityError):
        _insert_assessment(db, stage=4, cand_key="und-with-stage", undeterminable=1)

    for stage in (*AssessmentStage.ALL, None):
        db_ok = tmp_path / f"mig_ok_{stage}.db"
        assert _alembic(db_ok, "upgrade", "head").returncode == 0
        # 判定不能の行は `stage IS NULL` と対で入る（語彙はこの組で決まる）
        _insert_assessment(db_ok, stage=stage, undeterminable=1 if stage is None else 0)


def test_the_migration_can_be_rolled_back(tmp_path: Path) -> None:
    """downgrade で制約が落ちること（前後に動ける形になっている）。"""
    db = tmp_path / "mig_down.db"
    assert _alembic(db, "upgrade", "head").returncode == 0
    down = _alembic(db, "downgrade", _BEFORE)
    assert down.returncode == 0, (down.stdout + down.stderr)[-2000:]
    with sqlite3.connect(db) as conn:
        sql = conn.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'location_candidate_assessments'"
        ).fetchone()[0]
    assert AssessmentStage.CHECK_NAME not in sql


def _table_shape(db_path: Path) -> tuple[list[tuple], set[str], set[tuple], set[tuple]]:
    """判定表の行・索引名・外部キー・NOT NULL を読む（表の作り直しで失われていないかを測る）。

    **`notnull` も見る**。`check_sql()` は `stage` 側の三値論理を
    `stage IS NOT NULL` で明示的に塞いだが、**`is_undeterminable` 側の同型の穴を塞いでいるのは
    式ではなく列の `nullable=False`** である（実測: 列を nullable にすると
    `(None, None)` と `(4, None)` が CHECK を通る）。表を作り直す移行でその制約が落ちれば穴が
    開くので、**実際に塞いでいるものを測る側に置く**。
    """
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, system_location_id, cand_key, round_number, stage, is_undeterminable, "
            f"rationale FROM {_ASSESSMENTS} ORDER BY id"
        ).fetchall()
        # `sqlite_autoindex%` を除くので、**一意制約由来の暗黙索引はこの集合に入らない**。
        # 一意性そのものは下の重複 INSERT で測る
        indexes = {
            r[0]
            for r in conn.execute(
                f"SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='{_ASSESSMENTS}' "
                "AND name NOT LIKE 'sqlite_autoindex%'"
            ).fetchall()
        }
        fks = {
            (r[2], r[3], r[4], r[6])  # 参照先テーブル / 子列 / 親列 / ON DELETE
            for r in conn.execute(f"PRAGMA foreign_key_list({_ASSESSMENTS})").fetchall()
        }
        notnull = {
            (r[1], r[3])  # 列名 / notnull
            for r in conn.execute(f"PRAGMA table_info({_ASSESSMENTS})").fetchall()
        }
    return rows, indexes, fks, notnull


def test_the_migration_preserves_rows_indexes_and_foreign_keys(tmp_path: Path) -> None:
    """**表の作り直しで既存行・索引・外部キーが失われない**こと。

    SQLite は `ALTER TABLE ... ADD CONSTRAINT` を持たないので `batch_alter_table` は
    **表を作り直す**（新表を作り、コピーし、旧表を落として rename する）。判定記録は
    追記のみの来歴なので、ここで行が落ちるのは最悪の失敗である。**それをテストが
    一度も測っていなかった**（制約の有無しか見ていなかった）。

    往復（upgrade → downgrade）で測るので、`downgrade` 側の作り直しも同時に測る。
    """
    db = tmp_path / "mig_shape.db"
    assert _alembic(db, "upgrade", _BEFORE).returncode == 0
    # 語彙内の3形（段階あり・最小・判定不能）を入れる。**判定不能を入れないと、
    # NULL を運ぶ列のコピーが測れない**
    _insert_assessment(db, stage=4, cand_key="a")
    _insert_assessment(db, stage=1, cand_key="b")
    _insert_assessment(db, stage=None, cand_key="c", undeterminable=1)

    before = _table_shape(db)
    # 前提: 測る対象が空でない（索引も外部キーも実在する）
    assert len(before[0]) == 3 and before[1] and before[2], before
    # 前提: `is_undeterminable` は NOT NULL である（`check_sql()` の三値論理の穴を塞いでいるのは
    # 式ではなくこの制約なので、前提として書く）
    assert ("is_undeterminable", 1) in before[3], before[3]

    assert _alembic(db, "upgrade", _THIS).returncode == 0
    after_up = _table_shape(db)
    assert after_up == before, f"upgrade で行・索引・外部キーが変わった: {before} → {after_up}"

    down = _alembic(db, "downgrade", _BEFORE)
    assert down.returncode == 0, (down.stdout + down.stderr)[-2000:]
    after_down = _table_shape(db)
    assert after_down == before, f"downgrade で失われた: {before} → {after_down}"

    # 外部キーが**効いている**ことも見る（列定義に残っていても働かなければ意味がない）
    with sqlite3.connect(db) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                f"INSERT INTO {_ASSESSMENTS} (system_location_id, cand_key, candidate_snapshot, "
                "round_number, stage, is_undeterminable, rationale, is_self_assessment, "
                "assessed_at, created_at, updated_at) VALUES (99999, 'fk', '{}', 1, 3, 0, "
                "'外部キー', 0, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
            )
    # 一意制約も保たれている（同じ (所在, cand_key, round) は二度入らない）
    with pytest.raises(sqlite3.IntegrityError):
        _insert_assessment(db, stage=4, cand_key="a")


def test_the_only_write_path_for_assessments_is_the_api() -> None:
    """**判定行を作る経路が API の1箇所だけ**であることをソースで固定する。

    「保存経路が API 以外に無い」ことを、確認した時点の事実として残す。経路が増えたときは**語彙の検査も足したか**を見る必要があるので、黙って増えない形に
    しておく（DB の CHECK 制約が最後の砦だが、400 を返して人に伝える層は経路ごとに要る）。
    """
    import ast

    def _called_name(node: ast.Call) -> str | None:
        # **属性形式（`models.LocationCandidateAssessment(...)`）も拾う**。
        # `ast.Name` だけを見ると、`import models as _m` 経由の第2の構築点を足す変異が
        # 生き残る（この repo では `alembic/env.py` が属性形式を使う）。
        if isinstance(node.func, ast.Name):
            return node.func.id
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        return None

    sites: list[str] = []
    for path in sorted((_REPO / "kawasemi").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _called_name(node) == "LocationCandidateAssessment":
                sites.append(f"{path.relative_to(_REPO)}:{node.lineno}")
    assert len(sites) == 1, f"判定行を作る経路が増えている（語彙の検査も足したか）: {sites}"
    assert sites[0].startswith("kawasemi/ai_input/routes.py:"), sites
    # **限界**: `insert(LocationCandidateAssessment).values(...)` / `add_all` / 生 SQL は
    # この検査では見えない。最後の砦は DB の CHECK 制約で、この検査が守るのは
    # 「400 を返して人に伝える層を足し忘れないこと」である


def test_the_migration_refuses_to_run_in_offline_mode(tmp_path: Path) -> None:
    """**`--sql`（オフライン）では既存行を検査できないので中止する**こと。

    接続が無いので `SELECT` が返らない。検査を飛ばして制約を作りに行くと、語彙外の行がある DB
    では適用時に `IntegrityError` で落ち、**「何件が語彙外かを先に報告する」という設計が失われる**。

    改修前の挙動は「動く」ではなく `TypeError: 'NoneType' object is not iterable` だった
    （`bind.execute()` が None を返す）。つまりこの枝が奪った機能は無く、**不可解な TypeError を
    実行可能な指示に変えた**——中止のときに**走らせるべき SQL を出す**ことも測る。

    **指示が実行可能であることまで測る**。以前は「0 件を確認してから
    `--sql` の出力を当てること」と案内していたが、この枝は無条件に中止するので
    **`--sql` の出力は永久に得られない**——実行できない指示だった（テストは returncode と
    SQL の字面しか見ていなかったので通っていた）。示す道は「接続のある環境で
    `alembic upgrade`」であり、それが**実際に通る**ことをここで確かめる。

    示す SQL は**制約と同じ述語**でなければならない（`stage` 単体の条件を案内すると、
    0 件を確認したのに適用が `IntegrityError` で落ちる）。
    """
    db = tmp_path / "offline.db"
    assert _alembic(db, "upgrade", _BEFORE).returncode == 0
    result = _alembic(db, "upgrade", f"{_BEFORE}:{_THIS}", "--sql")

    assert result.returncode != 0, "オフラインモードで既存行を検査せずに走っている"
    combined = result.stdout + result.stderr
    assert "SELECT COUNT(*)" in combined, "手で走らせる SQL を示していない"
    assert _ASSESSMENTS in combined
    # **得られない出力を案内しない**
    assert "--sql の出力を当てる" not in combined, "この枝では永久に得られない手順を案内している"
    assert "alembic upgrade" in combined, "実行できる道を示していない"
    # 案内した SQL が**そのまま走り、制約と同じ集合を数える**こと
    where = combined.split("WHERE", 1)[1].split(";", 1)[0].strip()
    _insert_assessment(db, stage=None, cand_key="null-not-und", undeterminable=0)
    _insert_assessment(db, stage=3, cand_key="ok")
    with sqlite3.connect(db) as conn:
        count = conn.execute(f"SELECT COUNT(*) FROM {_ASSESSMENTS} WHERE {where}").fetchone()[0]
    assert count == 1, f"案内した SQL が語彙外を数えられていない（{where}）"
    # 案内どおり接続のある環境で走らせれば、同じ行を同じ件数で報告して止まる
    online = _alembic(db, "upgrade", _THIS)
    assert online.returncode != 0
    assert "1 件" in (online.stdout + online.stderr)
