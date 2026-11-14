"""再スキャン 3 回目の F6: FHIR の URL に埋め込まれた資格情報（userinfo）をログに出さない。

運用者が `FHIR_SERVER_URL=https://svc:secret@host/fhir` のように書いても、接続失敗・タイムアウト・
401/403・HTTP エラー・不正 JSON のどれでも、ログと例外の details/メッセージに `svc:secret@` が
現れてはならない。
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator

import httpx
import pytest

from kawasemi.core.exceptions import FHIRServerException
from kawasemi.core.logging_config import PHIRedactionFilter
from kawasemi.utils import fhir_client as fhir_client_module
from kawasemi.utils.fhir_client import FHIRClient, redact_url

_SECRET = "svc:s3cr3t-pw"
_BASE = f"https://{_SECRET}@fhir.example/fhir"


class _Logs:
    """PHIRedactionFilter 付きの専用ハンドラで集めたログ文面。

    `caplog.text` は、ルートのハンドラの順序（アプリのコンソールハンドラのフィルタが先に
    レコードを書き換えるか）やロギングの状態（プロセス内で動かした alembic の fileConfig が
    ハンドラを入れ替える）に結果が左右される。ここでは、フィルタを自分で付けたハンドラだけを
    見て、実際にフィルタを通ったあとの文面を確かめる。
    """

    def __init__(self) -> None:
        self.lines: list[str] = []

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


@pytest.fixture
def logs() -> Iterator[_Logs]:
    sink = _Logs()

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            sink.lines.append(self.format(record))

    handler = _Handler(logging.DEBUG)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    handler.addFilter(PHIRedactionFilter())
    root = logging.getLogger()
    old_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield sink
    finally:
        root.removeHandler(handler)
        root.setLevel(old_level)


def _client_with(handler: Callable[[httpx.Request], httpx.Response]) -> FHIRClient:
    return FHIRClient(base_url=_BASE, auth_mode="none")


@pytest.fixture
def patched_httpx(
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[[Callable[..., httpx.Response]], None]:
    """FHIRClient が作る httpx.AsyncClient を、MockTransport 付きに差し替える。"""
    real = httpx.AsyncClient

    def install(handler: Callable[..., httpx.Response]) -> None:
        def factory(*args: object, **kwargs: object) -> httpx.AsyncClient:
            kwargs["transport"] = httpx.MockTransport(handler)
            return real(*args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(fhir_client_module.httpx, "AsyncClient", factory)

    return install


def _everything(logs: _Logs, exc: FHIRServerException) -> str:
    return logs.text + exc.message + repr(exc.details) + str(exc)


async def _fail(client: FHIRClient) -> FHIRServerException:
    with pytest.raises(FHIRServerException) as info:
        await client.get_patient("P1")
    return info.value


def test_redact_url_helper() -> None:
    assert redact_url("https://a:b@h/x") == "https://h/x"
    assert redact_url("https://a:p@ss@h/x") == "https://h/x"  # パスワード中の @
    assert redact_url("see https://u:p@h/x and http://h2/y") == "see https://h/x and http://h2/y"
    assert redact_url("https://h/x") == "https://h/x"
    assert redact_url("no url here") == "no url here"


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [401, 403, 404, 500])
async def test_http_status_errors_do_not_leak_userinfo(
    status: int,
    patched_httpx: Callable[..., None],
    logs: _Logs,
) -> None:
    patched_httpx(lambda request: httpx.Response(status))
    exc = await _fail(_client_with(lambda r: httpx.Response(status)))
    shown = _everything(logs, exc)
    assert "s3cr3t-pw" not in shown and "svc:" not in shown
    assert "fhir.example" in str(exc.details["url"])  # 表示用の URL は残る


@pytest.mark.asyncio
async def test_connection_failure_does_not_leak_userinfo(
    patched_httpx: Callable[..., None], logs: _Logs
) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        # httpx の例外文字列に URL（userinfo つき）が入る場合を再現する
        raise httpx.ConnectError(f"connect failed for {_BASE}/Patient/P1", request=request)

    patched_httpx(boom)
    exc = await _fail(_client_with(boom))
    assert "s3cr3t-pw" not in _everything(logs, exc)


@pytest.mark.asyncio
async def test_timeout_does_not_leak_userinfo(
    patched_httpx: Callable[..., None], logs: _Logs
) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timed out: {_BASE}/Patient/P1", request=request)

    patched_httpx(slow)
    exc = await _fail(_client_with(slow))
    assert "s3cr3t-pw" not in _everything(logs, exc)


@pytest.mark.asyncio
async def test_bad_json_does_not_leak_userinfo(
    patched_httpx: Callable[..., None], logs: _Logs
) -> None:
    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    patched_httpx(bad)
    exc = await _fail(_client_with(bad))
    assert "s3cr3t-pw" not in _everything(logs, exc)


def test_log_filter_masks_userinfo_in_any_log_message() -> None:
    """個別の呼び出し箇所が表示用 URL を作り忘れても、ログのフィルタが userinfo を伏せる。"""
    f = PHIRedactionFilter()
    record = logging.LogRecord(
        "x", logging.ERROR, "p", 1, f"failed: {_BASE}/Patient/P1 (retry)", (), None
    )
    f.filter(record)
    assert "s3cr3t-pw" not in record.getMessage()
    assert "fhir.example/fhir/Patient/P1" in record.getMessage()


def test_redact_url_masks_query_secrets_and_scheme_less_userinfo() -> None:
    assert redact_url("https://h.example/fhir/Patient/1?access_token=TKN") == (
        "https://h.example/fhir/Patient/1?access_token=***REDACTED***"
    )
    assert redact_url("Patient/1?api_key=K&_count=2") == "Patient/1?api_key=***REDACTED***&_count=2"
    assert redact_url("user:pw@h.example/fhir") == "h.example/fhir"
    assert redact_url("//user:pw@h.example/fhir") == "h.example/fhir"
    url = "https://example.com:8080?email=a@b.com"
    assert redact_url(url) == url  # userinfo の無い URL は崩さない


@pytest.mark.asyncio
async def test_query_token_in_the_base_url_does_not_reach_the_api_details(
    patched_httpx: Callable[..., None], logs: _Logs
) -> None:
    """例外の details は API の応答に載る。クエリの秘匿値（`?access_token=`）も出さない。"""
    patched_httpx(lambda request: httpx.Response(500))
    client = FHIRClient(base_url="https://fhir.example/fhir?access_token=QTOKEN", auth_mode="none")
    exc = await _fail(client)
    assert "QTOKEN" not in repr(exc.details) + exc.message + logs.text
    assert "fhir.example" in str(exc.details["url"])
