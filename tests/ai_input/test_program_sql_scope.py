"""プログラム SQL の読める表（F8・F9）と、ファイル・FHIR の患者の範囲（F7 の一部）

- F8/F9: `sql_extract` は `users.password_hash` や PHI を持つ表（回答・実行記録）を読めない。
  読み取り専用は書き込みを止めるだけなので、**読める表**を実行の側で絞る
  （SQLite は接続の authorizer、PostgreSQL は計画に出る実表）
- F7（SQL に依らない部分）: `file_extract` のパスと `fhir_extract` のクエリは、患者キー
  `{{patient_id}}` で決まること。`unit_id` は免除の根拠にならない
- SQL の患者の範囲は、アプリでは保証しない（DB 側の行レベルセキュリティの仕事。docs/DEPLOYMENT.md）

PostgreSQL の実機は無くても動くよう、計画の検査は計画 JSON を返す偽の接続でも固定する
（実機の検査は `test_program_sql_readonly_postgres.py`）。
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input import executor as executor_mod
from kawasemi.ai_input.executor import DSLInterpreter
from kawasemi.ai_input.fetch_scope import fhir_query_scopes_patient, path_scopes_patient
from kawasemi.ai_input.models import FailureCode, InputPlan
from kawasemi.ai_input.resource_index.connections import ConnectionRegistry
from kawasemi.core import readonly_db
from kawasemi.core.readonly_db import (
    ProgramSqlDeniedError,
    assert_relations_permitted,
    denied_tables,
    readonly_program_sql,
    table_read_permitted,
)


def _plan(program: dict[str, Any]) -> InputPlan:
    return InputPlan(questionnaire_id=1, link_id="q", program=program, status="confirmed")


def _sql_program(sql: str, **step: Any) -> dict[str, Any]:
    return {"steps": [{"op": "sql_extract", "id": "s", "sql": sql, **step}], "return": "$s"}


async def _run(db: AsyncSession, program: dict[str, Any], patient: str = "P1") -> dict[str, Any]:
    prefill, evidence = await DSLInterpreter().execute([_plan(program)], patient, db)
    return {"prefill": prefill, "evidence": evidence, "trace": evidence["q"]["trace"][0]}


_DROP_ALL = (
    "DROP VIEW IF EXISTS innocent",
    "DROP TABLE IF EXISTS labs",
    "DROP TABLE IF EXISTS master",
    "DROP TABLE IF EXISTS pts",
    "DROP TABLE IF EXISTS other",
)


@pytest.fixture
async def clinical(db_session: AsyncSession) -> AsyncGenerator[AsyncSession, None]:
    """臨床データの表（アプリ自身の表ではない）と、読ませてはいけないユーザー行。

    テストの DB は1接続（StaticPool）で、`Base.metadata.drop_all` はモデルに無い表を落とさない。
    ほかのテストが作った `labs` が残っていることがあるので、作る前にも落とす。
    """
    for stmt in _DROP_ALL:
        await db_session.execute(text(stmt))
    await db_session.execute(text("CREATE TABLE labs (patient_id TEXT, v TEXT)"))
    await db_session.execute(text("CREATE TABLE master (code TEXT, name TEXT)"))
    await db_session.execute(text("CREATE TABLE pts (id TEXT, patient_id TEXT)"))
    await db_session.execute(text("INSERT INTO pts VALUES ('P1', 'P1'), ('P2', 'P2')"))
    await db_session.execute(text("INSERT INTO labs VALUES ('P1', 'mine'), ('P2', 'theirs')"))
    await db_session.execute(text("INSERT INTO master VALUES ('c', 'master-row')"))
    await db_session.execute(
        text(
            "INSERT INTO users (username, email, password_hash, full_name, is_active) "
            "VALUES ('admin', 'a@example.com', 'SECRET-HASH', 'Admin', 1)"
        )
    )
    await db_session.commit()
    yield db_session
    await db_session.rollback()
    for stmt in _DROP_ALL:
        await db_session.execute(text(stmt))
    await db_session.commit()


# --------------------------------------------------------------------------- #
# F8 / F9: 読める表を絞る（SQLite の authorizer）
# --------------------------------------------------------------------------- #

DENIED_SQL = {
    "users": "SELECT password_hash FROM users WHERE username = :patient_id",
    "join": (
        "SELECT l.v FROM labs l JOIN users u ON u.username = l.patient_id "
        "WHERE l.patient_id = :patient_id"
    ),
    "subquery": (
        "SELECT v FROM labs WHERE patient_id = :patient_id "
        "AND EXISTS (SELECT 1 FROM users WHERE username = :patient_id)"
    ),
    "cte": "WITH h AS (SELECT password_hash AS x FROM users WHERE username = :patient_id) SELECT x FROM h",
    "executions": "SELECT patient_id FROM ai_input_executions WHERE patient_id = :patient_id",
    "responses": "SELECT id FROM questionnaire_responses WHERE patient_id = :patient_id",
    "schema": "SELECT sql FROM sqlite_master WHERE name = :patient_id",
    "pragma": "SELECT name FROM pragma_table_info('users') WHERE name = :patient_id",
}


@pytest.mark.asyncio
@pytest.mark.parametrize("sql", DENIED_SQL.values(), ids=DENIED_SQL.keys())
async def test_application_tables_cannot_be_read(clinical: AsyncSession, sql: str) -> None:
    """アプリ自身の表（認証・PHI・スキーマ）は、`:patient_id` を使っていても読めない。"""
    got = await _run(clinical, _sql_program(sql))

    assert got["prefill"] == {}
    assert got["trace"]["failure_code"] == FailureCode.PATH_NOT_PERMITTED
    # 患者の範囲の検査ではなく、表の検査（authorizer）で止まっていること
    assert "患者で絞られていない" not in got["trace"]["error"], got["trace"]["error"]
    assert "SECRET-HASH" not in json.dumps(got["evidence"], ensure_ascii=False)


@pytest.mark.asyncio
async def test_a_view_over_an_application_table_is_denied(clinical: AsyncSession) -> None:
    """ビュー経由でも、実際に読む表で判定される（文字列の検査ではない）。"""
    await clinical.execute(
        text("CREATE VIEW innocent AS SELECT username, password_hash FROM users")
    )
    await clinical.commit()

    got = await _run(
        clinical, _sql_program("SELECT password_hash FROM innocent WHERE username = :patient_id")
    )

    assert got["trace"]["failure_code"] == FailureCode.PATH_NOT_PERMITTED


@pytest.mark.asyncio
async def test_clinical_tables_are_still_read(clinical: AsyncSession) -> None:
    """正常系: 臨床データの表は、当該患者の行だけ読める。"""
    got = await _run(clinical, _sql_program("SELECT v FROM labs WHERE patient_id = :patient_id"))

    assert got["prefill"] == {"q": "mine"}


@pytest.mark.asyncio
async def test_the_session_is_usable_and_writable_after_a_denial(clinical: AsyncSession) -> None:
    """拒否のあとで authorizer と query_only が外れ、アプリのセッションは書けること。"""
    got = await _run(clinical, _sql_program(DENIED_SQL["users"]))
    assert got["trace"]["failure_code"] == FailureCode.PATH_NOT_PERMITTED

    await clinical.execute(text("INSERT INTO labs VALUES ('P3', 'after')"))
    assert (await clinical.execute(text("SELECT COUNT(*) FROM users"))).scalar_one() == 1


@pytest.mark.asyncio
async def test_writes_are_still_refused(clinical: AsyncSession) -> None:
    """書き込み（検査を迂回して直接投げた場合）は DB 側で止まる。"""
    async with readonly_program_sql(clinical):
        with pytest.raises(Exception) as exc:  # noqa: PT011 - 方言ごとに例外型が違う
            await clinical.execute(text("INSERT INTO labs VALUES ('P9', 'evil')"))
    assert any(w in str(exc.value).lower() for w in ("readonly", "not authorized", "prohibited"))
    count = (await clinical.execute(text("SELECT COUNT(*) FROM labs"))).scalar_one()
    assert count == 2


@pytest.mark.asyncio
async def test_allowed_tables_limit_what_can_be_read(clinical: AsyncSession) -> None:
    """`allowed_tables` を渡すと、読める表はその表だけになる（臨床の表同士でも）。"""
    async with readonly_program_sql(clinical, allowed_tables=["master"]):
        assert (
            await clinical.execute(text("SELECT name FROM master"))
        ).scalar_one() == "master-row"
        with pytest.raises(Exception) as exc:  # noqa: PT011
            await clinical.execute(text("SELECT v FROM labs"))
    assert "prohibited" in str(exc.value) or "not authorized" in str(exc.value)


def test_every_application_table_is_denied() -> None:
    """`Base.metadata` の全表が対象で、認証の表だけの列挙ではない。"""
    tables = denied_tables()
    for name in ("users", "user_roles", "refresh_tokens", "auth_events", "ai_input_executions"):
        assert name in tables
    assert not table_read_permitted("USERS")
    assert not table_read_permitted("sqlite_master")
    assert not table_read_permitted("pg_shadow")
    assert table_read_permitted("labs")
    assert not table_read_permitted("labs", frozenset({"master"}))


# --------------------------------------------------------------------------- #
# F9: PostgreSQL。計画に出る実表で検査する（偽の接続で固定する）
# --------------------------------------------------------------------------- #


class _FakePgReader:
    dialect = SimpleNamespace(name="postgresql")

    current_schema = "public"

    def __init__(self, plan: Any) -> None:
        self.plan = plan
        self.statements: list[str] = []

    async def execute(self, stmt: Any, params: Any = None) -> Any:
        self.statements.append(str(stmt))
        if "current_schema" in str(stmt):
            return SimpleNamespace(scalar_one=lambda: self.current_schema)
        return SimpleNamespace(scalar_one=lambda: self.plan)


def _plan_of(*relations: tuple[str, str]) -> list[dict[str, Any]]:
    """`EXPLAIN (FORMAT JSON)` の形（入れ子の Plans の中に Relation Name が出る）。"""
    leaves = [{"Node Type": "Seq Scan", "Schema": s, "Relation Name": r} for s, r in relations]
    return [{"Plan": {"Node Type": "Nested Loop", "Plans": leaves}}]


@pytest.mark.asyncio
async def test_postgres_plan_with_clinical_tables_only_is_permitted() -> None:
    reader = _FakePgReader(json.dumps(_plan_of(("public", "labs"), ("public", "master"))))

    await assert_relations_permitted(reader, "SELECT v FROM labs", {"patient_id": "P1"})

    assert reader.statements == ["EXPLAIN (VERBOSE, FORMAT JSON) SELECT v FROM labs"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "relations",
    [
        [("public", "users")],  # 認証の表
        [("public", "labs"), ("public", "ai_input_executions")],  # 結合の片側が PHI
        [("pg_catalog", "pg_authid")],
        [("information_schema", "tables")],
        [("public", "pg_shadow")],
    ],
)
async def test_postgres_plan_touching_application_tables_is_denied(
    relations: list[tuple[str, str]],
) -> None:
    """ビューは計画で基表まで展開されるので、ビュー越しでも基表で止まる。"""
    reader = _FakePgReader(_plan_of(*relations))

    with pytest.raises(ProgramSqlDeniedError):
        await assert_relations_permitted(reader, "SELECT 1 FROM innocent_view")


def _function_scan(schema: str, function: str) -> list[dict[str, Any]]:
    """結合の省略で `Relation Name` が消え、関数ノードだけが残った計画（`pg_stat_activity` など）。"""
    node = {
        "Node Type": "Function Scan",
        "Schema": schema,
        "Function Name": function,
        "Function Call": f"{function}(NULL::integer)",
        "Output": ["query"],
    }
    return [{"Plan": node}]


CATALOG_PLANS = {
    "pg_stat_activity": _function_scan("pg_catalog", "pg_stat_get_activity"),
    "pg_settings": _function_scan("pg_catalog", "pg_show_all_settings"),
    "pg_locks": _function_scan("pg_catalog", "pg_lock_status"),
    "pg_hba_file_rules": _function_scan("pg_catalog", "pg_hba_file_rules"),
    "pg_file_settings": _function_scan("pg_catalog", "pg_show_all_file_settings"),
    "catalog_schema_function": _function_scan("pg_catalog", "some_function"),
    "viewdef_output": [
        {"Plan": {"Node Type": "Result", "Output": ["pg_get_viewdef('vu'::regclass)"]}}
    ],
    "relation_size_output": [
        {"Plan": {"Node Type": "Result", "Output": ["pg_relation_size('users'::regclass)"]}}
    ],
    "qualified_function_output": [
        {"Plan": {"Node Type": "Result", "Output": ["pg_catalog.current_setting('x'::text)"]}}
    ],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("plan", CATALOG_PLANS.values(), ids=CATALOG_PLANS.keys())
async def test_postgres_plan_with_catalog_functions_is_denied(plan: Any) -> None:
    """表の名前が計画に出なくても、カタログの関数ノード・関数呼び出しは拒否する。"""
    with pytest.raises(ProgramSqlDeniedError):
        await assert_relations_permitted(_FakePgReader(plan), "SELECT query FROM pg_stat_activity")


@pytest.mark.asyncio
async def test_postgres_plan_with_ordinary_functions_is_permitted() -> None:
    """正常系: 集計・日付などの普通の関数は拒否しない。"""
    plan = [
        {
            "Plan": {
                "Node Type": "Aggregate",
                "Output": ["count(*)", "max(l.v)", "date_trunc('day'::text, l.t)"],
                "Plans": [{"Node Type": "Seq Scan", "Schema": "public", "Relation Name": "labs"}],
            }
        }
    ]

    await assert_relations_permitted(_FakePgReader(plan), "SELECT count(*) FROM labs")


@pytest.mark.asyncio
async def test_postgres_allowed_tables_restrict_the_plan() -> None:
    reader = _FakePgReader(_plan_of(("public", "labs")))

    await assert_relations_permitted(reader, "SELECT v FROM labs", allowed_tables=["public.labs"])
    with pytest.raises(ProgramSqlDeniedError):
        await assert_relations_permitted(reader, "SELECT v FROM labs", allowed_tables=["master"])


@pytest.mark.asyncio
async def test_non_select_statements_are_left_to_the_readonly_transaction() -> None:
    """SELECT / WITH 以外は計画を取らない（そのまま読み取り専用トランザクションが止める）。"""
    reader = _FakePgReader(_plan_of(("public", "users")))

    await assert_relations_permitted(reader, "INSERT INTO labs VALUES ('x', 'y')")

    assert reader.statements == []


@pytest.mark.asyncio
async def test_sqlite_is_checked_by_the_authorizer_not_the_plan(clinical: AsyncSession) -> None:
    """SQLite では計画の検査は何もしない（authorizer が文の準備時に見る）。"""
    await assert_relations_permitted(clinical, "SELECT password_hash FROM users")


def _use_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    reg = ConnectionRegistry({"fs": {"scope": "external", "root_path": str(root)}})
    monkeypatch.setattr(executor_mod, "get_registry", lambda path=None: reg)


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("{{patient_id}}/note.txt", True),
        ("{{patient_id}}.txt", True),
        ("{{study_id}}/{{patient_id}}.txt", True),
        ("shared.txt", False),
        ("P2/note.txt", False),
        ("{{study_id}}/note.txt", False),
        ("{{a}}{{patient_id}}/note.txt", False),  # 区間を患者だけで決めていない
    ],
)
def test_path_needs_the_patient_key_as_a_whole_segment(pattern: str, expected: bool) -> None:
    assert path_scopes_patient(pattern) is expected


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("/Observation?patient={{patient_id}}", True),
        ("/Observation?code=x&subject={{ patient_id }}&_count=5", True),
        ("/Patient/{{patient_id}}", True),
        ("/Patient/{{patient_id}}/$everything", True),
        ("/Observation?code=x", False),
        ("/Observation?patient=P2&note={{patient_id}}", False),
        ("/Observation?patient={{patient_id}},P2", False),
        ("/Observation?note={{patient_id}}", False),
        ("/Patient/P2?x={{patient_id}}", False),
        ("/Observation?Patient={{patient_id}}", False),  # 検索パラメータ名は大文字小文字を区別する
        ("/patient/{{patient_id}}", False),  # 資源型も同じ
        ("/Patient/{{patient_id}}/%2e%2e/%2e%2e/Patient", False),
        ("/Patient/%2F{{patient_id}}", False),
        ("/Observation/../Observation?patient={{patient_id}}", False),
        ("/Observation?patient={{patient_id}}&_include=Observation:subject", False),
        ("/Observation?patient={{patient_id}}&_revinclude=Provenance:target", False),
        ("/Observation?patient={{patient_id}}&_has:Group:member:_id=g", False),
        # 符号化した検索パラメータ名・二重の符号化（M2）
        ("/Observation?patient={{patient_id}}&%5Finclude=*", False),
        ("/Observation?patient={{patient_id}}&_in%63lude=*", False),
        ("/Observation?patient={{patient_id}}&%5f%68as=1", False),
        ("/Observation?patient={{patient_id}}&_%68as:Patient:link:x=1", False),
        ("/Observation?patient={{patient_id}}&%5f_include=x", False),
        ("/Patient/{{patient_id}}/%252e%252e", False),
        ("/Observation%2fx?patient={{patient_id}}", False),
        # 前後の空白は落とさない（別の名前・値）
        ("/Observation?patient ={{patient_id}}", False),
        ("/Observation?patient= {{patient_id}}", False),
        ("/Observation?patient={{patient_id}} ", False),
        ("/Observation?patient={{patient_id}}&_Include=x", False),
        # 値の `%` は普通に使う（符号化したコード体系）
        ("/Observation?code=http%3A%2F%2Floinc.org%7C1234-5&patient={{patient_id}}", True),
    ],
)
def test_fhir_query_needs_the_patient_param(query: str, expected: bool) -> None:
    assert fhir_query_scopes_patient(query) is expected


@pytest.mark.asyncio
async def test_file_extract_needs_the_patient_key_in_the_path(
    clinical: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "P1").mkdir()
    (tmp_path / "P1" / "note.txt").write_text("mine", encoding="utf-8")
    (tmp_path / "P2").mkdir()
    (tmp_path / "P2" / "note.txt").write_text("theirs", encoding="utf-8")
    _use_root(monkeypatch, tmp_path)

    def program(pattern: str, **step: Any) -> dict[str, Any]:
        s = {"op": "file_extract", "id": "f", "connection_id": "fs", "path_pattern": pattern}
        return {"steps": [{**s, **step}], "return": "$f"}

    scoped = await _run(clinical, program("{{patient_id}}/note.txt"))
    assert scoped["prefill"] == {"q": "mine"}

    literal = await _run(clinical, program("P2/note.txt"))
    assert literal["prefill"] == {}
    assert literal["trace"]["failure_code"] == FailureCode.PATH_NOT_PERMITTED


@pytest.mark.asyncio
async def test_a_unit_id_does_not_exempt_a_file_step(
    clinical: AsyncSession, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`unit_id` はプランに書かれた値で、免除の根拠にならない（どの op でも）。"""
    (tmp_path / "P2").mkdir()
    (tmp_path / "P2" / "note.txt").write_text("theirs", encoding="utf-8")
    _use_root(monkeypatch, tmp_path)
    step = {
        "op": "file_extract",
        "id": "f",
        "connection_id": "fs",
        "path_pattern": "P2/note.txt",
        "unit_id": "master-unit",
    }

    got = await _run(clinical, {"steps": [step], "return": "$f"})

    assert got["prefill"] == {}
    assert got["trace"]["failure_code"] == FailureCode.PATH_NOT_PERMITTED


async def _fhir_run(
    db: AsyncSession, query: str, **step: Any
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    interp = DSLInterpreter()
    seen: list[str] = []

    async def fake_get_resource(url: str) -> dict[str, Any]:
        seen.append(url)
        return {"resourceType": "Bundle", "entry": []}

    interp._fhir_client._get_resource = fake_get_resource  # type: ignore[method-assign]
    s = {"op": "fhir_extract", "id": "f", "fhir_query": query, "multi": True, **step}
    prefill, evidence = await interp.execute([_plan({"steps": [s], "return": "$f"})], "P1", db)
    return prefill, evidence["q"]["trace"][0], seen


@pytest.mark.asyncio
async def test_fhir_extract_needs_the_patient_param(clinical: AsyncSession) -> None:
    for query in (
        "/Observation?code=x",
        "/Observation?patient=P2&note={{patient_id}}",
    ):
        prefill, trace, seen = await _fhir_run(clinical, query)
        assert prefill == {}
        assert trace["failure_code"] == FailureCode.PATH_NOT_PERMITTED
        assert seen == []  # FHIR サーバへは出ていない

    _, _, seen = await _fhir_run(clinical, "/Observation?patient={{patient_id}}")
    assert len(seen) == 1 and "patient=P1" in seen[0]


@pytest.mark.asyncio
async def test_a_unit_id_does_not_exempt_a_fhir_step(clinical: AsyncSession) -> None:
    prefill, trace, seen = await _fhir_run(clinical, "/Observation?code=x", unit_id="master-unit")

    assert prefill == {}
    assert trace["failure_code"] == FailureCode.PATH_NOT_PERMITTED
    assert seen == []


def test_the_guard_module_documents_the_dialect_split() -> None:
    """権限の境界の説明が、モジュールの文書に残っていること（運用者が読む）。"""
    assert "set_authorizer" in (readonly_db.__doc__ or "")
    assert "EXPLAIN" in (readonly_db.__doc__ or "")


@pytest.mark.asyncio
async def test_an_unknown_dialect_fails_closed() -> None:
    """表を検査できない方言では、プログラム SQL を実行しない。"""
    reader = _FakePgReader(_plan_of(("public", "labs")))
    reader.dialect = SimpleNamespace(name="mysql")

    with pytest.raises(ProgramSqlDeniedError):
        await assert_relations_permitted(reader, "SELECT v FROM labs")
    assert reader.statements == []


@pytest.mark.asyncio
async def test_a_qualified_allowance_matches_the_plan_schema() -> None:
    """`schema.table` で許したら、同名の別スキーマの表は通さない。"""
    other_schema = _FakePgReader(_plan_of(("archive", "master")))
    same_schema = _FakePgReader(_plan_of(("app", "master")))

    await assert_relations_permitted(same_schema, "SELECT 1", allowed_tables=["app.master"])
    with pytest.raises(ProgramSqlDeniedError):
        await assert_relations_permitted(other_schema, "SELECT 1", allowed_tables=["app.master"])


@pytest.mark.asyncio
async def test_an_unqualified_allowance_resolves_to_the_current_schema() -> None:
    """スキーマ無しの許可は現在のスキーマの表。同名の別スキーマの表は通さない。"""
    here = _FakePgReader(_plan_of(("public", "master")))
    elsewhere = _FakePgReader(_plan_of(("archive", "master")))

    await assert_relations_permitted(here, "SELECT 1", allowed_tables=["master"])
    with pytest.raises(ProgramSqlDeniedError):
        await assert_relations_permitted(elsewhere, "SELECT 1", allowed_tables=["master"])


@pytest.mark.asyncio
async def test_a_plan_without_a_schema_cannot_match_an_allowance() -> None:
    """計画が表のスキーマを与えないなら、許可と照合できないので通さない（フェイルクローズ）。"""
    reader = _FakePgReader([{"Plan": {"Node Type": "Seq Scan", "Relation Name": "master"}}])

    with pytest.raises(ProgramSqlDeniedError):
        await assert_relations_permitted(reader, "SELECT 1", allowed_tables=["master"])
