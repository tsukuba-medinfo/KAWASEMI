"""Ollama 非依存の決定的な擬似 embedder（E2E の決定的層と索引経路のテスト用）。

実 Ollama を要さずに「索引を作って検索する」経路をそのまま通すためのもの。
`kawasemi_indexer.builder.build_index` / `item_units.append_item_units` と KAWASEMI 側の
`ResourceIndexStore` の双方が要求する Embedder プロトコル（`model` / `dim` / `embed`）を満たす。

語をハッシュして固定次元へ数え上げる（bag of n-grams）。**意味は捉えない**。短いクエリと
長いチャンクの cosine は低く出る（典拠候補の足切り `clinical_drafter._MIN_SCORE=0.5` には
届かない）。所在探索（`resource_drafter.retrieve_hits`）は足切りが 0.0 で、字面の一致を
順位に反映できれば足りるので、決定的層ではこちらで関門 (b) を見る。

`digest()` を持たないので `check_embedding_identity` は「未検証」として通す。
以前は `tests/ai_input/conftest.py` にあった（E2E の生成器と試験の両方から使うので移した）。
"""

from __future__ import annotations

import re
import unicodedata
import zlib

import numpy as np

from kawasemi.ai_input.resource_index.embedder import l2_normalize

_ASCII_TOKEN_RE = re.compile(r"[a-z0-9_]+")

DEFAULT_MODEL = "bge-m3"
DEFAULT_DIM = 64


def tokenize(text: str) -> list[str]:
    """ASCII の語と、非 ASCII の連続の 2-gram（1文字なら 1-gram）へ分ける。"""
    if not text:
        return []
    norm = unicodedata.normalize("NFKC", text).lower()
    tokens = list(_ASCII_TOKEN_RE.findall(norm))
    for run in re.findall(r"[^\x00-\x7f]+", norm):
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


class HashingEmbedder:
    """決定的な擬似 embedder（モデル名は索引と照合されるので、設定値に合わせて作る）。"""

    def __init__(self, model: str = DEFAULT_MODEL, dim: int = DEFAULT_DIM) -> None:
        self._model = model
        self._dim = dim

    @property
    def model(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return self._dim

    def embed(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)
        mat = np.zeros((len(texts), self._dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for tok in tokenize(text):
                mat[i, zlib.crc32(tok.encode("utf-8")) % self._dim] += 1.0
        return l2_normalize(mat)
