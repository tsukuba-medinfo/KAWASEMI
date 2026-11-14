"""再スキャン 3 回目の F6: FHIR の URL に埋め込まれた資格情報（userinfo）をログに出さない。

運用者が `FHIR_SERVER_URL=https://svc:secret@host/fhir` のように書いても、接続失敗・タイムアウト・
401/403・HTTP エラー・不正 JSON のどれでも、ログと例外の details/メッセージに `svc:secret@` が
現れてはならない。
"""

from __future__ import annotations

import logging
from collections.abc import Callable

import httpx
import pytest

from kawasemi.core.exceptions import FHIRServerException
from kawasemi.core.logging_config import PHIRedactionFilter
from kawasemi.utils import fhir_client as fhir_client_module
from kawasemi.utils.fhir_client import FHIRClient, redact_url

_SECRET = "svc:s3cr3t-pw"
_BASE = f"https://{_SECRET}@fhir.example/fhir"


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


def _everything(caplog: pytest.LogCaptureFixture, exc: FHIRServerException) -> str:
    return caplog.text + exc.message + repr(exc.details) + str(exc)


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
    caplog: pytest.LogCaptureFixture,
) -> None:
    patched_httpx(lambda request: httpx.Response(status))
    caplog.set_level(logging.DEBUG)
    exc = await _fail(_client_with(lambda r: httpx.Response(status)))
    shown = _everything(caplog, exc)
    assert "s3cr3t-pw" not in shown and "svc:" not in shown
    assert "fhir.example" in str(exc.details["url"])  # 表示用の URL は残る


@pytest.mark.asyncio
async def test_connection_failure_does_not_leak_userinfo(
    patched_httpx: Callable[..., None], caplog: pytest.LogCaptureFixture
) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        # httpx の例外文字列に URL（userinfo つき）が入る場合を再現する
        raise httpx.ConnectError(f"connect failed for {_BASE}/Patient/P1", request=request)

    patched_httpx(boom)
    caplog.set_level(logging.DEBUG)
    exc = await _fail(_client_with(boom))
    assert "s3cr3t-pw" not in _everything(caplog, exc)


@pytest.mark.asyncio
async def test_timeout_does_not_leak_userinfo(
    patched_httpx: Callable[..., None], caplog: pytest.LogCaptureFixture
) -> None:
    def slow(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout(f"timed out: {_BASE}/Patient/P1", request=request)

    patched_httpx(slow)
    caplog.set_level(logging.DEBUG)
    exc = await _fail(_client_with(slow))
    assert "s3cr3t-pw" not in _everything(caplog, exc)


@pytest.mark.asyncio
async def test_bad_json_does_not_leak_userinfo(
    patched_httpx: Callable[..., None], caplog: pytest.LogCaptureFixture
) -> None:
    def bad(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"<html>not json</html>")

    patched_httpx(bad)
    caplog.set_level(logging.DEBUG)
    exc = await _fail(_client_with(bad))
    assert "s3cr3t-pw" not in _everything(caplog, exc)


def test_log_filter_masks_userinfo_in_any_log_message() -> None:
    """個別の呼び出し箇所が表示用 URL を作り忘れても、ログのフィルタが userinfo を伏せる。"""
    f = PHIRedactionFilter()
    record = logging.LogRecord(
        "x", logging.ERROR, "p", 1, f"failed: {_BASE}/Patient/P1 (retry)", (), None
    )
    f.filter(record)
    assert "s3cr3t-pw" not in record.getMessage()
    assert "fhir.example/fhir/Patient/P1" in record.getMessage()
