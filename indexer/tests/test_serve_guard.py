"""索引診断 API（serve）の Host / Origin / トークン検査のテスト（Claude Security F27・F28）

DNS rebinding（攻撃者のドメインがループバックを指す）と、ループバック以外への bind での
無認証読み出しを塞ぐ。実 Ollama 非依存（HashingEmbedder）。
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from test_debug_repl import FIXTURE_DIR, HashingEmbedder  # noqa: E402

from kawasemi_indexer.builder import build_db_index  # noqa: E402
from kawasemi_indexer.store import get_store, reset_store_cache  # noqa: E402
from kawasemi_indexer.webserver import (  # noqa: E402
    allowed_host_names,
    create_app,
    is_loopback_bind,
)

MODEL = "test-hash"
TOKEN = "guard-token-0123456789abcdef"
AUTH = {"Authorization": f"Bearer {TOKEN}"}


@pytest.fixture
def index_dir(tmp_path):
    out = tmp_path / "resource_index"
    build_db_index(source=FIXTURE_DIR, out_dir=out, embedder=HashingEmbedder())
    reset_store_cache()
    get_store(str(out))
    yield str(out)
    reset_store_cache()


def _client(index_dir, *, base_url="http://localhost:8000", **kw) -> TestClient:
    kw.setdefault("token", TOKEN)
    app = create_app(index_dir=index_dir, model=MODEL, embedder=HashingEmbedder(), **kw)
    return TestClient(app, base_url=base_url)


def test_refuses_to_build_without_token_unless_explicit(index_dir):
    with pytest.raises(ValueError):
        create_app(index_dir=index_dir, model=MODEL, embedder=HashingEmbedder())
    # 明示すれば認証無しで作れる
    _client(index_dir, token=None, insecure_no_auth=True)


def test_api_requires_a_token(index_dir):
    c = _client(index_dir)
    assert c.get("/api/status").status_code == 401
    assert c.get("/api/status").headers["www-authenticate"] == "Bearer"
    assert c.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert c.get("/api/status", headers={"Authorization": TOKEN}).status_code == 401
    assert c.get("/api/status", headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
    assert c.get("/api/status", headers=AUTH).status_code == 200


@pytest.mark.parametrize(
    "method,path,body",
    [
        ("get", "/api/status", None),
        ("get", "/api/show/db:X.Y.Z", None),
        ("post", "/api/search", {"query": "x"}),
        ("post", "/api/grep", {"term": "x"}),
        ("post", "/api/why", {"query": "x", "unit_id": "db:X.Y.Z"}),
    ],
)
def test_every_api_route_is_guarded(index_dir, method, path, body):
    c = _client(index_dir)
    r = getattr(c, method)(path, **({"json": body} if body else {}))
    assert r.status_code == 401


def test_no_docs_or_openapi(index_dir):
    c = _client(index_dir)
    for path in ("/docs", "/openapi.json", "/redoc"):
        assert c.get(path, headers=AUTH).status_code == 404


@pytest.mark.parametrize(
    "host",
    ["evil.example", "evil.example:8000", "localhost.evil.example", "127.0.0.1.evil.example", ""],
)
def test_rebound_host_is_rejected_even_with_the_token(index_dir, host):
    """DNS rebinding: ブラウザは攻撃者のドメインを Host に付けて送る。"""
    c = _client(index_dir)
    r = c.get("/api/status", headers={**AUTH, "Host": host})
    assert r.status_code == 400
    assert c.get("/", headers={"Host": host}).status_code == 400  # UI の HTML も同じ


@pytest.mark.parametrize("host", ["localhost", "localhost:9999", "127.0.0.1:8000", "[::1]:8000"])
def test_loopback_hosts_pass(index_dir, host):
    c = _client(index_dir)
    assert c.get("/api/status", headers={**AUTH, "Host": host}).status_code == 200


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "http://evil.example"},
        {"Origin": "http://localhost:8001"},  # 別ポートも別オリジン
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
        {"Sec-Fetch-Site": "same-site"},
    ],
)
def test_cross_origin_requests_are_rejected_even_with_the_token(index_dir, headers):
    c = _client(index_dir)
    r = c.get("/api/status", headers={**AUTH, **headers})
    assert r.status_code == 403
    assert "access-control-allow-origin" not in r.headers


def test_cross_origin_preflight_gets_no_cors_grant(index_dir):
    c = _client(index_dir)
    r = c.options(
        "/api/search",
        headers={
            "Origin": "http://evil.example",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization",
        },
    )
    assert r.status_code in (400, 403, 405)
    assert "access-control-allow-origin" not in r.headers


def test_same_origin_browser_requests_pass(index_dir):
    c = _client(index_dir)
    r = c.post(
        "/api/search",
        json={"query": "患者ID"},
        headers={
            **AUTH,
            "Origin": "http://localhost:8000",
            "Sec-Fetch-Site": "same-origin",
        },
    )
    assert r.status_code == 200
    assert c.get("/", headers={"Sec-Fetch-Site": "none"}).status_code == 200


def test_the_page_itself_carries_no_index_data_or_token(index_dir):
    c = _client(index_dir)
    r = c.get("/")
    assert r.status_code == 200
    assert TOKEN not in r.text
    assert "DEMO.LAB" not in r.text


def test_extra_allowed_host(index_dir):
    c = _client(
        index_dir,
        base_url="http://indexer.hospital.lan:8000",
        allowed_hosts=["indexer.hospital.lan"],
    )
    assert c.get("/api/status", headers=AUTH).status_code == 200
    assert c.get("/api/status", headers={**AUTH, "Host": "other.lan"}).status_code == 400


def test_no_auth_mode_still_checks_host_and_origin(index_dir):
    c = _client(index_dir, token=None, insecure_no_auth=True)
    assert c.get("/api/status").status_code == 200
    assert c.get("/api/status", headers={"Host": "evil.example"}).status_code == 400
    assert c.get("/api/status", headers={"Origin": "http://evil.example"}).status_code == 403


def test_the_ui_sends_the_token_and_strips_it_from_the_url(index_dir):
    html = _client(index_dir).get("/").text
    assert "authHeaders()" in html
    assert "history.replaceState" in html


@pytest.mark.parametrize(
    "host,expected",
    [
        ("127.0.0.1", True),
        ("::1", True),
        ("[::1]", True),
        ("localhost", True),
        ("0.0.0.0", False),
        ("::", False),
        ("192.168.1.5", False),
        ("indexer.lan", False),
        ("127.0.0.1.evil.example", False),
    ],
)
def test_is_loopback_bind(host, expected):
    assert is_loopback_bind(host) is expected


def test_allowed_host_names_never_admit_a_wildcard_bind():
    assert "0.0.0.0" not in allowed_host_names("0.0.0.0")
    assert "[::]" not in allowed_host_names("::")
    names = allowed_host_names("192.168.1.5", ["Indexer.Lan"])
    assert {"localhost", "127.0.0.1", "[::1]", "192.168.1.5", "indexer.lan"} <= names
