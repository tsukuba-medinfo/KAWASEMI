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


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", "http://127.0.0.1:8000/"),
        ("localhost", "http://localhost:8000/"),
        ("0.0.0.0", "http://localhost:8000/"),
        ("::", "http://localhost:8000/"),
        ("::1", "http://[::1]:8000/"),
        ("diag.example.org", "http://diag.example.org:8000/"),
    ],
)
def test_browser_url_is_always_openable(host, expected):
    assert cli._browser_url(host, 8000) == expected


def test_wildcard_bind_prints_a_usable_url_and_keeps_host_allowlist(monkeypatch, capsys):
    pytest.importorskip("fastapi")
    uvicorn = pytest.importorskip("uvicorn")

    class _Store:
        def is_searchable(self, _m):
            return True

    import kawasemi_indexer.store as store_mod

    monkeypatch.setattr(store_mod, "get_store", lambda _d: _Store())
    captured = {}

    def _create_app(**kw):
        captured.update(kw)
        return object()

    import kawasemi_indexer.webserver as ws

    monkeypatch.setattr(ws, "create_app", _create_app)
    monkeypatch.setattr(uvicorn, "run", lambda *a, **k: None)
    rc = cli.cmd_serve(_args(host="0.0.0.0", token_file=None))
    out = capsys.readouterr().out
    assert rc == 0
    assert "http://localhost:8000/#token=" in out
    assert "http://0.0.0.0" not in out
    assert "--allowed-host" in out
    # Host の許可リストは緩めない（bind 先の名前と、明示した名前だけ）
    assert captured["allowed_hosts"] == ["0.0.0.0"]
