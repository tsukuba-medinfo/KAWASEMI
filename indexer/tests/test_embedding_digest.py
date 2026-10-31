"""embedding モデルのダイジェストを索引に焼く。

モデル名（タグ）だけでは重みの同一性を保証できない。索引ビルド時のダイジェストを
`build_info.embedding_digest` に残し、クエリ側が起動時に照合できるようにする。
"""

from __future__ import annotations

import json
import zlib
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from kawasemi_indexer.builder import build_db_index
from kawasemi_indexer.embedder import OllamaEmbedder, l2_normalize

FIXTURE_DIR = str(Path(__file__).resolve().parent / "fixtures" / "db_definition")


class HashingEmbedder:
    """語彙をハッシュして次元へ詰める決定的な埋め込み（Ollama 非依存）。"""

    def __init__(self, model: str = "test-hash", dim: int = 16) -> None:
        self._model = model
        self._dim = dim

    @property
    def model(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> np.ndarray:
        mat = np.zeros((len(texts), self._dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for ch in text:
                mat[i, zlib.crc32(ch.encode("utf-8")) % self._dim] += 1.0
        return l2_normalize(mat)


def _embedder_with_client(client: object, model: str = "bge-m3") -> OllamaEmbedder:
    emb = OllamaEmbedder(model=model, base_url="http://127.0.0.1:1")
    emb._client = client  # type: ignore[assignment]
    return emb


def _listing(*pairs: tuple[str, str]) -> SimpleNamespace:
    return SimpleNamespace(
        models=[SimpleNamespace(model=name, digest=digest) for name, digest in pairs]
    )


def test_digest_is_read_from_the_model_listing() -> None:
    client = SimpleNamespace(list=lambda: _listing(("other:latest", "aa"), ("bge-m3:latest", "bb")))
    assert _embedder_with_client(client).digest() == "bb"


def test_a_model_name_without_a_tag_means_latest() -> None:
    client = SimpleNamespace(list=lambda: _listing(("bge-m3:latest", "bb"), ("bge-m3:567m", "cc")))
    assert _embedder_with_client(client, "bge-m3").digest() == "bb"
    assert _embedder_with_client(client, "bge-m3:567m").digest() == "cc"


def test_a_missing_model_or_digest_is_none() -> None:
    assert (
        _embedder_with_client(SimpleNamespace(list=lambda: _listing(("x:latest", "aa")))).digest()
        is None
    )
    no_digest = SimpleNamespace(models=[SimpleNamespace(model="bge-m3:latest", digest=None)])
    assert _embedder_with_client(SimpleNamespace(list=lambda: no_digest)).digest() is None


def test_a_failing_listing_does_not_raise() -> None:
    """ダイジェストの取得が埋め込み・索引ビルドを壊さない。None（未検証）になる。"""

    def boom() -> None:
        raise ConnectionError("サーバに繋がらない")

    assert _embedder_with_client(SimpleNamespace(list=boom)).digest() is None


class _DigestEmbedder(HashingEmbedder):
    def __init__(self, digest: str | None) -> None:
        super().__init__()
        self._digest = digest

    def digest(self) -> str | None:
        return self._digest


class _RaisingDigestEmbedder(HashingEmbedder):
    def digest(self) -> str | None:
        raise RuntimeError("digest が取れない")


def _build(tmp_path: Path, embedder: object) -> dict:
    out = tmp_path / "idx"
    build_db_index(source=FIXTURE_DIR, out_dir=out, embedder=embedder)  # type: ignore[arg-type]
    return json.loads((out / "build_info.json").read_text("utf-8"))


def test_build_info_records_the_embedding_digest(tmp_path: Path) -> None:
    assert _build(tmp_path, _DigestEmbedder("ab" * 32))["embedding_digest"] == "ab" * 32


@pytest.mark.parametrize(
    "embedder",
    [HashingEmbedder(), _DigestEmbedder(None), _DigestEmbedder("  "), _RaisingDigestEmbedder()],
    ids=["no-digest-method", "none", "blank", "raises"],
)
def test_build_info_has_a_null_digest_when_it_cannot_be_taken(tmp_path: Path, embedder) -> None:  # noqa: ANN001
    """取れない・例外でもビルドは成功し、None（未検証）が残る。"""
    assert _build(tmp_path, embedder)["embedding_digest"] is None
