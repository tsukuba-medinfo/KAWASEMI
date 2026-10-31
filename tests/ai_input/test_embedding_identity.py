"""索引とクエリの embedding が同じ重みか。

名前（`embedding_model`）の一致だけでは、`EMBEDDING_BASE_URL` で別の重みのサーバへ向けても
「検索可能」と判定され、再現率が黙って落ちる。ダイジェストを照合する。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from kawasemi.ai_input.resource_index import embedding_identity as ei
from kawasemi.ai_input.resource_index.embedding_identity import (
    check_embedding_identity,
    compare_digests,
    query_embedding_digest,
)
from kawasemi.ai_input.resource_index.schema import BuildInfo
from kawasemi.ai_input.resource_index.store import ResourceIndexStore


def test_the_same_digest_is_verified() -> None:
    identity = compare_digests("aa" * 32, "aa" * 32)
    assert identity.compatible and identity.verified and identity.reason is None


def test_a_different_digest_makes_the_index_unsearchable_with_a_reason() -> None:
    identity = compare_digests("aa" * 32, "bb" * 32)
    assert identity.compatible is False and identity.verified is False
    assert identity.reason and "重み" in identity.reason and "無効" in identity.reason


@pytest.mark.parametrize(
    ("index_digest", "query_digest"),
    [(None, "bb" * 32), ("aa" * 32, None), (None, None)],
    ids=["index-has-none", "query-has-none", "both-none"],
)
def test_an_unverifiable_digest_does_not_disable_the_search(
    index_digest: str | None, query_digest: str | None
) -> None:
    """片方でも取れなければ「違う」の証拠が無い。無効化せず、未検証の理由を残す。

    無効化すると、この欄が増える前に作った索引（過去の測定に使った索引を含む）が一斉に使えなくなる。
    """
    identity = compare_digests(index_digest, query_digest)
    assert identity.compatible is True and identity.verified is False
    assert identity.reason and "未検証" in identity.reason


class _Embedder:
    def __init__(self, behaviour: object) -> None:
        self._behaviour = behaviour

    def digest(self) -> str | None:
        if isinstance(self._behaviour, BaseException):
            raise self._behaviour
        if callable(self._behaviour):
            return self._behaviour()
        return self._behaviour  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_query_digest_is_none_without_raising() -> None:
    assert await query_embedding_digest(SimpleNamespace()) is None  # digest を持たない
    assert await query_embedding_digest(_Embedder(RuntimeError("落ちた"))) is None
    assert await query_embedding_digest(_Embedder("  ")) is None
    assert await query_embedding_digest(_Embedder(" cd ")) == "cd"


@pytest.mark.asyncio
async def test_a_hanging_lookup_returns_after_the_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    """固まる形でも上限時間で戻る（照合が生成を止めない）。"""
    import threading

    release = threading.Event()
    monkeypatch.setattr(ei, "DIGEST_TIMEOUT_SECONDS", 0.05)
    try:
        started = asyncio.get_running_loop().time()
        result = await query_embedding_digest(_Embedder(lambda: release.wait(5) and "late"))
        elapsed = asyncio.get_running_loop().time() - started
    finally:
        release.set()
    assert result is None
    assert elapsed < 2


def _store(index_digest: str | None) -> ResourceIndexStore:
    store = ResourceIndexStore()
    store._build_info = BuildInfo(embedding_model="bge-m3", dim=4, embedding_digest=index_digest)
    return store


def test_the_store_exposes_the_index_digest() -> None:
    assert _store("aa" * 32).embedding_digest == "aa" * 32
    assert _store(None).embedding_digest is None
    assert ResourceIndexStore().embedding_digest is None  # 未ロード


@pytest.mark.asyncio
async def test_check_combines_the_store_and_the_embedder(caplog: pytest.LogCaptureFixture) -> None:
    ok = await check_embedding_identity(_store("aa" * 32), _Embedder("aa" * 32))
    assert ok.compatible and ok.verified

    with caplog.at_level("WARNING"):
        bad = await check_embedding_identity(_store("aa" * 32), _Embedder("bb" * 32))
    assert bad.compatible is False
    assert any("embedding 照合" in r.getMessage() for r in caplog.records), "不一致がログに出ない"


# --- OllamaEmbedder.digest()（クエリ側の vendored コピー。indexer 側にも同じテストがある）-------


def _ollama_embedder(client: object, model: str = "bge-m3"):  # noqa: ANN202
    from kawasemi.ai_input.resource_index.embedder import OllamaEmbedder

    emb = OllamaEmbedder(model=model, base_url="http://127.0.0.1:1")
    emb._client = client  # type: ignore[assignment]
    return emb


def _listing(*pairs: tuple[str, str | None]) -> SimpleNamespace:
    return SimpleNamespace(
        models=[SimpleNamespace(model=name, digest=digest) for name, digest in pairs]
    )


def test_the_query_side_embedder_reads_the_digest_from_the_listing() -> None:
    client = SimpleNamespace(list=lambda: _listing(("other:latest", "aa"), ("bge-m3:latest", "bb")))
    assert _ollama_embedder(client).digest() == "bb"


def test_a_model_name_without_a_tag_means_latest_not_another_tag() -> None:
    """タグを省いた指定は `:latest`。別のタグ（`:567m`）と取り違えない。"""
    client = SimpleNamespace(list=lambda: _listing(("bge-m3:567m", "cc"), ("bge-m3:latest", "bb")))
    assert _ollama_embedder(client, "bge-m3").digest() == "bb"
    assert _ollama_embedder(client, "bge-m3:567m").digest() == "cc"
    only_other_tag = SimpleNamespace(list=lambda: _listing(("bge-m3:567m", "cc")))
    assert _ollama_embedder(only_other_tag, "bge-m3").digest() is None


def test_a_failing_listing_or_missing_digest_is_none_not_an_error() -> None:
    def boom() -> None:
        raise ConnectionError("落ちている")

    assert _ollama_embedder(SimpleNamespace(list=boom)).digest() is None
    assert (
        _ollama_embedder(SimpleNamespace(list=lambda: _listing(("bge-m3:latest", None)))).digest()
        is None
    )
