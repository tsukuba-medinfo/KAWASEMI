"""実 Ollama を使うテストの skip 条件。

**skip 条件そのものをテストする。** 決め打ちの当て先が2件のテストを永久に skip させていた
形は、テストが1本も無かったから1年近く残った——skip は緑に見えるので、誰も見に行かない。

ネットワークへは出ない。`httpx.get` を差し替えて、当て先の組み立てと理由の文言だけを見る。
"""

from pathlib import Path
from typing import Any

import httpx
import pytest

from kawasemi.core.config import settings
from tests.e2e import helpers


class _Response:
    def __init__(self, status_code: int, payload: Any = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)  # type: ignore[arg-type]

    def json(self) -> Any:
        return self._payload


def _fake_get(calls: list[str], routes: dict[str, _Response]):
    def get(url: str, timeout: float = 0.0) -> _Response:
        calls.append(url)
        for suffix, response in routes.items():
            if url.endswith(suffix):
                return response
        raise AssertionError(f"想定外の URL: {url}")

    return get


def test_the_target_comes_from_the_setting(monkeypatch: pytest.MonkeyPatch) -> None:
    """**当て先は `OLLAMA_BASE_URL` から組む。** これがこのテスト群の本体である。

    決め打ちだと、外部サーバへ向けた環境で到達可能なのに skip し続ける。
    """
    monkeypatch.setattr(settings, "ollama_base_url", "http://198.51.100.9:80")
    calls: list[str] = []
    monkeypatch.setattr(httpx, "get", _fake_get(calls, {"/api/version": _Response(200)}))

    assert helpers.ollama_unavailable_reason() is None
    assert calls == ["http://198.51.100.9:80/api/version"]


def test_a_trailing_slash_does_not_double_up(monkeypatch: pytest.MonkeyPatch) -> None:
    """末尾スラッシュつきの設定でも `//api/version` にならないこと。"""
    monkeypatch.setattr(settings, "ollama_base_url", "http://host:11434/")
    calls: list[str] = []
    monkeypatch.setattr(httpx, "get", _fake_get(calls, {"/api/version": _Response(200)}))

    helpers.ollama_unavailable_reason()
    assert calls == ["http://host:11434/api/version"]


def test_an_unreachable_server_says_where_it_looked(monkeypatch: pytest.MonkeyPatch) -> None:
    """**理由に当て先が出ること。** 「Ollama 未起動」だけだと決め打ちに気づけない。"""
    monkeypatch.setattr(settings, "ollama_base_url", "http://nowhere:11434")

    def boom(url: str, timeout: float = 0.0) -> _Response:
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", boom)

    reason = helpers.ollama_unavailable_reason()
    assert reason is not None
    assert "http://nowhere:11434" in reason
    assert "ConnectError" in reason


def test_a_non_200_version_is_also_a_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ollama_base_url", "http://host:11434")
    monkeypatch.setattr(httpx, "get", _fake_get([], {"/api/version": _Response(503)}))

    reason = helpers.ollama_unavailable_reason()
    assert reason is not None
    assert "503" in reason and "http://host:11434" in reason


def test_a_missing_model_is_a_reason_not_a_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """**設定したモデルがサーバに無いなら skip する**（実測に基づく）。

    存在しないモデルで実行すると `POST /api/chat` が 404 を返し、テストは「Vision が
    読み取れない」という別の失敗に化ける（`.env` の既定のモデルがサーバに無く、
    `trace["method"]` の KeyError で落ちた）。モデルの不足は環境の不足であって実装の欠陥
    ではないので、**何が無くて何があるのかを理由に書いて** skip する。
    """
    monkeypatch.setattr(settings, "ollama_base_url", "http://host:11434")
    monkeypatch.setattr(
        httpx,
        "get",
        _fake_get(
            [],
            {
                "/api/version": _Response(200),
                "/api/tags": _Response(200, {"models": [{"name": "model-a:12b"}]}),
            },
        ),
    )

    reason = helpers.ollama_unavailable_reason("model-c:9b")
    assert reason is not None
    assert "model-c:9b" in reason
    assert "model-a:12b" in reason, "何があるのかを書かないと、次に何をすればよいか分からない"


def test_a_present_model_is_usable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "ollama_base_url", "http://host:11434")
    monkeypatch.setattr(
        httpx,
        "get",
        _fake_get(
            [],
            {
                "/api/version": _Response(200),
                "/api/tags": _Response(200, {"models": [{"name": "model-a:12b"}]}),
            },
        ),
    )

    assert helpers.ollama_unavailable_reason("model-a:12b") is None


def test_the_model_is_not_checked_when_not_asked(monkeypatch: pytest.MonkeyPatch) -> None:
    """モデル名を渡さないときは `/api/tags` を引かない（到達性だけを見る）。

    渡されていないモデルの在庫を調べるのは無駄な往復であり、`/api/tags` を持たない
    サーバで到達性の判定まで落ちる。
    """
    monkeypatch.setattr(settings, "ollama_base_url", "http://host:11434")
    calls: list[str] = []
    monkeypatch.setattr(httpx, "get", _fake_get(calls, {"/api/version": _Response(200)}))

    assert helpers.ollama_unavailable_reason() is None
    assert calls == ["http://host:11434/api/version"]


def test_the_helper_does_not_hardcode_localhost() -> None:
    """ヘルパーに当て先が焼き込まれていないこと。

    実装を読み替える形の退行（設定を読んだ上で既定へ戻す等）を、文字列の不在で縛る。
    """
    text = Path(helpers.__file__).read_text(encoding="utf-8")
    assert "localhost" not in text
    assert "11434" not in text, "ポート番号もヘルパーに書かない（設定の既定値が正本）"


def test_entries_without_a_name_are_not_listed(monkeypatch: pytest.MonkeyPatch) -> None:
    """名前を持たないエントリを在庫の列挙に混ぜない。

    混ぜると理由が「あるのは: , model-a:12b」になり、**何が無くて何があるのか**という
    このメッセージの目的が損なわれる。
    """
    monkeypatch.setattr(settings, "ollama_base_url", "http://host:11434")
    monkeypatch.setattr(
        httpx,
        "get",
        _fake_get(
            [],
            {
                "/api/version": _Response(200),
                "/api/tags": _Response(
                    200, {"models": [{}, {"name": "  "}, {"name": "model-a:12b"}]}
                ),
            },
        ),
    )

    reason = helpers.ollama_unavailable_reason("model-c:9b")
    assert reason is not None
    assert "あるのは: model-a:12b）" in reason
