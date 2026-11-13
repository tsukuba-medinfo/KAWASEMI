"""`serve` の起動引数（トークンの出どころと、認証無しの条件）のテスト。"""

from __future__ import annotations

import argparse
import os

import pytest

from kawasemi_indexer import cli


def _args(**kw) -> argparse.Namespace:
    base = {
        "index_dir": "x",
        "model": None,
        "host": "127.0.0.1",
        "port": 8000,
        "allowed_host": [],
        "token_file": None,
        "no_auth": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def test_parser_defaults_are_safe():
    ns = cli.build_parser().parse_args(["serve"])
    assert ns.host == "127.0.0.1"
    assert ns.no_auth is False
    assert ns.token_file is None
    assert ns.allowed_host == []


def test_token_file_must_be_private(tmp_path):
    f = tmp_path / "tok"
    f.write_text("a" * 32)
    os.chmod(f, 0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        cli._serve_token(str(f))
    os.chmod(f, 0o600)
    assert cli._serve_token(str(f)) == "a" * 32


def test_token_file_rejects_short_and_missing(tmp_path):
    f = tmp_path / "tok"
    f.write_text("short")
    os.chmod(f, 0o600)
    with pytest.raises(ValueError, match="短すぎ"):
        cli._serve_token(str(f))
    with pytest.raises(ValueError, match="読めません"):
        cli._serve_token(str(tmp_path / "missing"))


def test_token_from_settings_and_default_empty(monkeypatch):
    monkeypatch.setattr(cli.settings, "indexer_serve_token", "")
    assert cli._serve_token(None) == ""
    monkeypatch.setattr(cli.settings, "indexer_serve_token", "b" * 20)
    assert cli._serve_token(None) == "b" * 20
    monkeypatch.setattr(cli.settings, "indexer_serve_token", "tiny")
    with pytest.raises(ValueError):
        cli._serve_token(None)


def test_no_auth_is_refused_off_loopback(monkeypatch, capsys):
    pytest.importorskip("fastapi")
    pytest.importorskip("uvicorn")

    class _Store:
        def is_searchable(self, _m):
            return True

    import kawasemi_indexer.store as store_mod

    monkeypatch.setattr(store_mod, "get_store", lambda _d: _Store())
    rc = cli.cmd_serve(_args(host="0.0.0.0", no_auth=True))
    assert rc == 1
    assert "--no-auth" in capsys.readouterr().out
