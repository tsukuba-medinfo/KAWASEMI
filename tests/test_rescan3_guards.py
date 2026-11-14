"""再スキャン 3 回目の F4/F6 の修正が、戻されたら落ちるようにする（Reality Check 3 回目）。

修正そのもののテストは別にある。ここは「その修正を消してもスイートが緑のまま」になっていた
箇所（ロギングの uvicorn への取り付け、エンジンの `hide_parameters`、`check_schema` の
パスワード表示、画面の失敗表示、要約関数自体の堅牢性）を押さえる。
"""

from __future__ import annotations

import ast
import json
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.core.config import settings
from kawasemi.core.exception_summary import safe_message, summarize_exception
from kawasemi.core.logging_config import PHIRedactionFilter, setup_logging
from kawasemi.models.user import User
from tests.test_verification_workflow import _make_response

_REPO = Path(__file__).resolve().parent.parent
_SECRET_DETAIL = "DETAIL:  Key (email)=(phi-leak@example.org) already exists."


# ---------------------------------------------------------------------------
# (a) setup_logging() 自体が uvicorn のハンドラに伏せ字フィルタを付ける
# ---------------------------------------------------------------------------


@pytest.fixture
def restore_logging_state() -> Iterator[None]:
    names = ("", "uvicorn", "uvicorn.error", "uvicorn.access")
    saved = {n: (list(logging.getLogger(n).handlers), logging.getLogger(n).level) for n in names}
    try:
        yield
    finally:
        for n, (handlers, level) in saved.items():
            lg = logging.getLogger(n)
            lg.handlers[:] = handlers
            lg.setLevel(level)


def test_setup_logging_itself_attaches_the_filter_to_uvicorn_handlers(
    restore_logging_state: None,
) -> None:
    """`setup_logging()` が呼ぶこと（ヘルパー単体ではなく）を確かめる。呼び出しが消えたら落ちる。"""
    dummies: dict[str, logging.Handler] = {}
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        handler = logging.StreamHandler()
        logging.getLogger(name).handlers[:] = [handler]
        dummies[name] = handler
        assert not any(isinstance(f, PHIRedactionFilter) for f in handler.filters)

    setup_logging()

    for name, handler in dummies.items():
        assert any(isinstance(f, PHIRedactionFilter) for f in handler.filters), (
            f"{name} のハンドラに伏せ字フィルタが付いていない"
        )


# ---------------------------------------------------------------------------
# (b) すべてのエンジン作成が hide_parameters=True（ソースの検査）
# ---------------------------------------------------------------------------

_ENGINE_FACTORIES = {
    "create_engine",
    "create_async_engine",
    "engine_from_config",
    "async_engine_from_config",
}
_SCAN_DIRS = ("kawasemi", "indexer/kawasemi_indexer", "alembic", "scripts")


def _call_name(node: ast.Call) -> str | None:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _module_dicts(tree: ast.Module) -> dict[str, ast.Dict]:
    found: dict[str, ast.Dict] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Dict):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = node.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.value, ast.Dict)
            and isinstance(node.target, ast.Name)
        ):
            found[node.target.id] = node.value
    return found


def _dict_has_hide_parameters(d: ast.Dict) -> bool:
    return any(
        isinstance(k, ast.Constant)
        and k.value == "hide_parameters"
        and isinstance(v, ast.Constant)
        and v.value is True
        for k, v in zip(d.keys, d.values, strict=True)
    )


def _source_files() -> Iterator[Path]:
    for rel in _SCAN_DIRS:
        for path in sorted((_REPO / rel).rglob("*.py")):
            if ".venv" in path.parts or "__pycache__" in path.parts:
                continue
            yield path


def _engine_calls_without_hide_parameters() -> list[str]:
    bad: list[str] = []
    for path in _source_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        dicts = _module_dicts(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) not in _ENGINE_FACTORIES:
                continue
            ok = False
            for kw in node.keywords:
                if kw.arg == "hide_parameters":
                    ok = isinstance(kw.value, ast.Constant) and kw.value.value is True
                elif kw.arg is None and isinstance(kw.value, ast.Name):
                    d = dicts.get(kw.value.id)
                    ok = ok or (d is not None and _dict_has_hide_parameters(d))
            if not ok:
                bad.append(f"{path.relative_to(_REPO)}:{node.lineno}")
    return bad


def test_every_engine_factory_call_hides_parameters() -> None:
    """アプリ・読み取り専用・外部 DB・統計・e2e・CLI・alembic・indexer の全エンジンを検査する。

    どれかから `hide_parameters=True` を外すと、DB エラーの文面に束縛値（ハッシュ・メール・
    患者 ID）が戻る。テストのフィクスチャ（tests/）は対象外。
    """
    assert _engine_calls_without_hide_parameters() == []


def test_the_guard_actually_sees_the_known_engine_factories() -> None:
    """検査自体が空振りしていない（既知の作成箇所を、実際に数えている）。"""
    count = 0
    for path in _source_files():
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and _call_name(node) in _ENGINE_FACTORIES:
                count += 1
    assert count >= 9


# ---------------------------------------------------------------------------
# (c) check_schema が DB のパスワードを表示しない
# ---------------------------------------------------------------------------


class _NoDrift:
    recorded_revision = "r1"
    head_revision = "r1"

    def __bool__(self) -> bool:
        return False

    def messages(self) -> list[str]:
        return []


@pytest.mark.asyncio
async def test_check_schema_does_not_print_the_database_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from kawasemi import cli
    from kawasemi.core import schema_check

    async def _fake_inspect(_engine: object) -> _NoDrift:
        return _NoDrift()

    monkeypatch.setattr(schema_check, "inspect_schema_async", _fake_inspect)
    monkeypatch.setattr(
        settings, "database_url", "postgresql+asyncpg://dbuser:PW-SECRET-77@db.example/app"
    )

    await cli.check_schema()

    out = capsys.readouterr().out
    assert "PW-SECRET-77" not in out
    assert "db.example" in out  # 接続先自体は出す


# ---------------------------------------------------------------------------
# (e) 画面の失敗表示が、例外の文面（秘密）を出さない
# ---------------------------------------------------------------------------


def _boom(*_a: object, **_k: object) -> object:
    raise RuntimeError(_SECRET_DETAIL)


@pytest.mark.asyncio
async def test_questionnaire_create_failure_page_shows_only_the_summary(
    admin_client: AsyncClient, csrf_token: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    from kawasemi.web import routes

    monkeypatch.setattr(routes, "validate_fhir_questionnaire", _boom)
    resp = await admin_client.post(
        "/questionnaires/new",
        data={
            "resource_json": json.dumps({"resourceType": "Questionnaire"}),
            "csrf_token": csrf_token,
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    assert "登録に失敗しました" in resp.text
    assert "RuntimeError" in resp.text
    assert "phi-leak@example.org" not in resp.text


@pytest.mark.asyncio
async def test_questionnaire_update_failure_page_shows_only_the_summary(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kawasemi.web import routes

    q, _qr = await _make_response(db_session, submitted_by_id=designer_user.id)
    await db_session.commit()
    monkeypatch.setattr(routes, "validate_fhir_questionnaire", _boom)
    resp = await designer_client.post(
        f"/questionnaires/{q.id}/edit",
        data={"resource_json": json.dumps(q.content), "csrf_token": csrf_token},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    assert "更新に失敗しました" in resp.text
    assert "RuntimeError" in resp.text
    assert "phi-leak@example.org" not in resp.text


@pytest.mark.asyncio
async def test_response_update_failure_page_shows_only_the_summary(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from kawasemi.web import routes

    _q, qr = await _make_response(db_session, submitted_by_id=collector_user.id)
    await db_session.commit()
    monkeypatch.setattr(routes, "build_questionnaire_response_resource", _boom)
    resp = await collector_client.post(
        f"/responses/{qr.id}/edit",
        data={"q1": "x"},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 200
    assert "更新に失敗しました" in resp.text
    assert "RuntimeError" in resp.text
    assert "phi-leak@example.org" not in resp.text


# ---------------------------------------------------------------------------
# 要約関数自体が、except 節の中で例外を投げない
# ---------------------------------------------------------------------------


class _RaisingProps(Exception):
    @property
    def diag(self) -> object:
        raise RuntimeError("diag boom")

    @property
    def orig(self) -> object:
        raise RuntimeError("orig boom")

    @property
    def sqlstate(self) -> object:
        raise RuntimeError("sqlstate boom")

    @property
    def status_code(self) -> object:
        raise RuntimeError("status boom")


class _AuthoredBadStr(Exception):
    kawasemi_authored = True

    def __str__(self) -> str:
        raise RuntimeError("str boom")


def test_summarize_exception_falls_back_to_the_type_name_when_attributes_raise() -> None:
    assert summarize_exception(_RaisingProps("DETAIL secret")) == "_RaisingProps"


def test_summarize_and_safe_message_survive_an_authored_exception_whose_str_raises() -> None:
    e = _AuthoredBadStr()
    assert summarize_exception(e) == "_AuthoredBadStr"
    assert safe_message(e) == "_AuthoredBadStr"


def test_safe_message_survives_attribute_errors_of_third_party_exceptions() -> None:
    assert safe_message(_RaisingProps("DETAIL secret")) == "_RaisingProps"


# ---------------------------------------------------------------------------
# dsl.py の設計時の ValueError は、文面を残しつつ、データの値を含まない
# ---------------------------------------------------------------------------


def test_safe_arith_errors_are_authored_and_carry_no_data_values() -> None:
    from kawasemi.ai_input.dsl import ExprError, safe_arith

    with pytest.raises(ExprError) as info:
        safe_arith("abs(value)", 123.456)  # 関数呼び出しは許可されない式要素
    assert isinstance(info.value, ValueError)  # 既存の ValueError の捕捉が変わらない
    text = summarize_exception(info.value)
    assert "ExprError" in text and "Call" in text
    assert "123.456" not in text  # 実データの数値（`value` の置換結果）は載らない

    with pytest.raises(ExprError):
        safe_arith("value * 1e308 * 10", 1.0)  # 結果が非有限
