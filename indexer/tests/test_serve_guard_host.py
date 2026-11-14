"""索引診断 API の認証が Host ヘッダで回避されないこと（再スキャン F3）。

`/api/*` のトークン要否を、Host ヘッダから組み立てられる `request.url.path` で決めていたため、
`Host: localhost:8000?` でパスが空になり、認証が省かれた。認証の要否はルーティングに使う
scope のパスで決め、公開するパス（`/`）以外はすべて（存在しないパスも）トークンを要する。
Host ヘッダは形を厳密に検査する。
"""

from __future__ import annotations

import asyncio

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402
from test_debug_repl import FIXTURE_DIR, HashingEmbedder  # noqa: E402

from kawasemi_indexer.builder import build_db_index  # noqa: E402
from kawasemi_indexer.store import get_store, reset_store_cache  # noqa: E402
from kawasemi_indexer.webserver import _host_name, _route_path, create_app  # noqa: E402

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


def _app(index_dir, **kw):
    kw.setdefault("token", TOKEN)
    return create_app(index_dir=index_dir, model=MODEL, embedder=HashingEmbedder(), **kw)


def _client(index_dir, **kw) -> TestClient:
    return TestClient(_app(index_dir, **kw), base_url="http://localhost:8000")


# ---------------------------------------------------------------- _host_name

_VALID = [
    ("localhost", "localhost"),
    ("LOCALHOST:8000", "localhost"),
    ("127.0.0.1:8000", "127.0.0.1"),
    ("[::1]:8000", "[::1]"),
    ("[::1]", "[::1]"),
    ("indexer.example.test:65535", "indexer.example.test"),
    ("  localhost:8000  ", "localhost"),
]


@pytest.mark.parametrize(("header", "name"), _VALID)
def test_host_name_accepts_well_formed_headers(header, name):
    assert _host_name(header) == name


_INVALID = [
    "",
    "localhost:8000?",
    "localhost:8000#",
    "localhost:8000/x",
    "localhost:8000/",
    "localhost?:8000",
    "localhost#x",
    "localhost:80?00",
    "user@localhost",
    "user:pw@localhost:8000",
    "localhost:",
    "localhost:abc",
    "localhost:8000:9000",
    "localhost:65536",
    "localhost:-1",
    "localhost:+80",
    "localhost:８０００",  # 全角数字
    "local host",
    "local\thost",
    "localhost\x00",
    "localhost\r\nX: y",
    "localhost\\x",
    "ｌｏｃａｌｈｏｓｔ",  # 全角
    "localhost。",  # 句点のような別の文字
    "[::1]x",
    "[::1",
    "[::1]:8000?",
    "[::1]:8000/x",
    "[zz]",
    ".localhost",
    "localhost..",
    "-localhost",
]


@pytest.mark.parametrize("header", _INVALID)
def test_host_name_rejects_malformed_headers(header):
    assert _host_name(header) == ""


# --------------------------------------------------------------- Host の回避

# httpx は ASCII 以外のヘッダ値を送れないので、結合の試験は ASCII の値に限る（それ以外は上の単体で見る）。
_BAD_HOSTS = [
    h for h in _INVALID if h.isascii() and h.isprintable() and "\n" not in h and h.strip() == h
]


@pytest.mark.parametrize("host", _BAD_HOSTS)
def test_malformed_host_never_reaches_api_without_a_token(index_dir, host):
    c = _client(index_dir)
    for path in ("/api/status", "/api/show/x", "/"):
        r = c.get(path, headers={"Host": host})
        assert r.status_code in (400, 401, 403), (host, path, r.status_code)


def test_the_reported_bypass_is_closed(index_dir):
    """`Host: localhost:8000?` + トークン無しで 200 になっていた。"""
    c = _client(index_dir)
    r = c.get("/api/status", headers={"Host": "localhost:8000?"})
    assert r.status_code == 400
    for host in ("localhost:8000#", "localhost:8000/x", "localhost:8000?x=1"):
        assert c.get("/api/status", headers={"Host": host}).status_code == 400


def test_auth_does_not_depend_on_the_host_built_url(index_dir):
    """Host の検査を通る値でも、認証の要否は scope のパスで決まる（URL の再構成に頼らない）。"""
    app = _app(index_dir)
    sent: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/status",
        "raw_path": b"/api/status",
        "query_string": b"",
        "root_path": "",
        "headers": [(b"host", b"localhost:8000")],
        "client": ("127.0.0.1", 1),
        "server": ("localhost", 8000),
    }
    asyncio.run(app(scope, receive, send))
    assert sent[0]["status"] == 401


def test_route_path_ignores_headers_and_strips_root_path():
    assert _route_path({"path": "/api/status", "root_path": ""}) == "/api/status"
    assert _route_path({"path": "/idx/api/status", "root_path": "/idx"}) == "/api/status"
    assert _route_path({"path": "/idx", "root_path": "/idx"}) == ""
    assert _route_path({"path": "/", "root_path": ""}) == "/"
    assert _route_path({}) == ""


# ------------------------------------------------------------ パスの回避

_API_PATHS = [
    "/api/status",
    "/api/status/",
    "//api/status",
    "///api/status",
    "/api//status",
    "/api/./status",
    "/api/../api/status",
    "/api/%2e/status",
    "/api/%2E%2E/api/status",
    "/api%2Fstatus",
    "/%61pi/status",  # 'a' のパーセントエンコード
    "/API/status",
    "/api/status.",
    "/api/status%00",
    "/api/status;x=1",
    "/api/show/../../api/status",
    "/api",
    "/api/",
    "/apix",
    "/nonexistent",
    "/docs",
    "/openapi.json",
    "/static/x",
]


@pytest.mark.parametrize("path", _API_PATHS)
@pytest.mark.parametrize("method", ["GET", "POST", "HEAD", "OPTIONS", "PUT", "DELETE"])
def test_every_path_except_root_demands_a_token(index_dir, path, method):
    c = _client(index_dir)
    r = c.request(method, path)
    assert r.status_code == 401, (method, path, r.status_code)
    assert r.headers.get("www-authenticate") == "Bearer"


def test_with_a_token_the_same_paths_reach_the_router(index_dir):
    c = _client(index_dir)
    assert c.get("/api/status", headers=AUTH).status_code == 200
    # 存在しないパスは、トークンがあってはじめてルーターの 404 に届く
    assert c.get("/nonexistent", headers=AUTH).status_code == 404
    assert c.get("//api/status", headers=AUTH).status_code in (200, 404)


def test_root_page_is_public_and_only_root(index_dir):
    c = _client(index_dir)
    assert c.get("/").status_code == 200
    assert c.head("/").status_code != 401  # 公開パス（メソッドの可否はルーターが決める）
    assert c.options("/").status_code != 401
    # `/` に見せかけたパスは公開ではない
    for path in ("//", "/?x=/api/status", "/#/api/status"):
        r = c.get(path)
        if r.status_code == 200:
            assert r.headers["content-type"].startswith("text/html")


def test_query_string_cannot_make_an_api_path_public(index_dir):
    c = _client(index_dir)
    assert c.get("/api/status?/").status_code == 401
    assert c.get("/api/status?x=/").status_code == 401


@pytest.mark.parametrize("path", ["/api/status", "/idx/api/status", "/idx//api/status"])
def test_root_path_does_not_open_the_api(index_dir, path):
    c = TestClient(_app(index_dir), base_url="http://localhost:8000", root_path="/idx")
    assert c.get(path).status_code == 401
    assert c.get(path, headers=AUTH).status_code in (200, 404)


def test_no_auth_mode_is_unchanged(index_dir):
    """--no-auth（ループバック専用）は、これまでどおりトークン無しで届く。"""
    c = _client(index_dir, token=None, insecure_no_auth=True)
    assert c.get("/api/status").status_code == 200
    # ただし Host の検査は同じように効く
    assert c.get("/api/status", headers={"Host": "localhost:8000?"}).status_code == 400
