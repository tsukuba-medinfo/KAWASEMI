"""alembic/env.py が、アプリのロギング（伏せ字フィルタ付きのハンドラ）を壊さないこと。

`logging.config.fileConfig` はルートロガーのハンドラを入れ替える。プロセス内で移行を走らせる
（`kawasemi init_db`・テスト）と、アプリが付けた `PHIRedactionFilter` 付きのハンドラが消え、
以後のログが伏せ字なしで出てしまっていた（再スキャン 3 回目の Reality Check で確認）。
アプリが設定済みなら env.py は設定しなおさない。alembic CLI の単独起動（ハンドラが無い）では
従来どおり alembic.ini で設定する。
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator
from pathlib import Path

import pytest

from kawasemi.core.config import settings
from kawasemi.core.database import _alembic_config
from kawasemi.core.logging_config import PHIRedactionFilter, setup_logging


@pytest.fixture
def restore_logging() -> Iterator[None]:
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    try:
        yield
    finally:
        root.handlers[:] = handlers
        root.setLevel(level)


def _has_redaction_handler() -> bool:
    return any(
        isinstance(f, PHIRedactionFilter)
        for handler in logging.getLogger().handlers
        for f in handler.filters
    )


def test_in_process_migration_keeps_the_apps_redaction_handler(
    restore_logging: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from alembic import command

    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{tmp_path / 'm.db'}")
    setup_logging()
    assert _has_redaction_handler()

    command.current(_alembic_config())

    assert _has_redaction_handler(), "移行を走らせたあと、伏せ字フィルタ付きのハンドラが消えた"
    # 伏せ字なしのハンドラへ入れ替わっていない: 実際にログを出して、伏せ字を確かめる
    root = logging.getLogger()
    sink = io.StringIO()
    probe = logging.StreamHandler(sink)
    probe.addFilter(PHIRedactionFilter())
    root.addHandler(probe)
    try:
        logging.getLogger("x").warning("db url https://svc:PWSECRET@h/x [parameters: ('PHI',)]")
    finally:
        root.removeHandler(probe)
    assert "PWSECRET" not in sink.getvalue() and "('PHI',)" not in sink.getvalue()


def test_without_app_logging_the_alembic_ini_logging_is_still_applied(
    restore_logging: None, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """alembic CLI の単独起動（ルートにハンドラが無い）では、従来どおり alembic.ini で設定する。"""
    from alembic import command

    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{tmp_path / 'n.db'}")
    logging.getLogger().handlers.clear()

    command.current(_alembic_config())

    assert logging.getLogger().handlers, "alembic.ini のロギング設定が適用されなかった"
