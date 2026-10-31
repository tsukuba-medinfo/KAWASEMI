"""embedding 計算の抽象（indexer 生成側 / KAWASEMI クエリ側で共用）

索引側とクエリ側で同一の embedding モデルを使わないとベクトル空間がズレて検索が
無意味になる。そのため Embedder は「モデル名」と「次元」を公開し、build_info に
焼いて起動時照合できるようにする。

本番は Ollama の embeddings API を使う OllamaEmbedder。テストは Ollama 非依存の
決定的なダミー Embedder を注入する（builder / store は Embedder を引数で受ける）。
"""

from __future__ import annotations

from typing import Protocol

import numpy as np

from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)


def l2_normalize(matrix: np.ndarray) -> np.ndarray:
    """行ごとに L2 正規化する（ゼロ行は据え置き）。float32 で返す。"""
    arr = np.asarray(matrix, dtype=np.float32)
    if arr.ndim == 1:
        arr = arr.reshape(1, -1)
    norms = np.linalg.norm(arr, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    normalized: np.ndarray = (arr / norms).astype(np.float32)
    return normalized


class Embedder(Protocol):
    """テキスト列をベクトル行列へ変換する。返値は (N, dim) の float32。"""

    @property
    def model(self) -> str: ...

    @property
    def dim(self) -> int: ...

    def embed(self, texts: list[str]) -> np.ndarray: ...


class OllamaEmbedder:
    """Ollama の embeddings API で埋め込む本番用 Embedder

    dim は最初の埋め込み結果から確定する（モデル既知次元に依存しない）。
    """

    def __init__(self, model: str, base_url: str, timeout: int = 120, batch_size: int = 64) -> None:
        import ollama

        self._model = model
        self._client = ollama.Client(host=base_url, timeout=timeout)
        self._dim = 0
        self._batch_size = max(1, batch_size)

    @property
    def model(self) -> str:
        return self._model

    @property
    def dim(self) -> int:
        return self._dim

    def digest(self) -> str | None:
        """埋め込みモデルのダイジェスト（Ollama の `/api/tags` の `digest`）。取れなければ None。

        **モデル名（タグ）だけでは重みの同一性を保証できない**（`ollama pull` で上流が更新
        されても、別のサーバが同名タグで違う重みを持っていても、名前は同じ）。索引ビルド時に
        `build_info.embedding_digest` へ焼き、クエリ側が起動時に照合する。

        **例外を投げない。** ダイジェストの取得が埋め込みそのものを壊してはならない。
        取れなかったことは None で表し、呼び出し側が「未検証」として扱う。
        """
        wanted = self._model.strip()
        if not wanted:
            return None
        if ":" not in wanted:
            wanted = f"{wanted}:latest"
        try:
            response = self._client.list()
            entries = getattr(response, "models", None)
            if entries is None and isinstance(response, dict):
                entries = response.get("models")
            for entry in entries or []:
                name = getattr(entry, "model", None) or getattr(entry, "name", None)
                if name is None and isinstance(entry, dict):
                    name = entry.get("model") or entry.get("name")
                if not isinstance(name, str):
                    continue
                if name.strip() != wanted:
                    continue
                digest = getattr(entry, "digest", None)
                if digest is None and isinstance(entry, dict):
                    digest = entry.get("digest")
                if isinstance(digest, str) and digest.strip():
                    return digest.strip()
                return None
        except Exception as e:  # noqa: BLE001 - 取得の失敗は埋め込みを壊さない
            logger.warning(f"embedding モデルのダイジェストを取得できない: {type(e).__name__}")
        return None

    def embed(self, texts: list[str]) -> np.ndarray:
        """テキスト列をまとめて埋め込む。

        Ollama の /api/embed（バッチ対応）を使い batch_size 件ずつ送る。1件ずつの
        embeddings() に比べ、列数の多い索引では桁違いに速い。大規模索引でも現実的な
        時間で生成するために必須。
        """
        if not texts:
            return np.zeros((0, self._dim or 1), dtype=np.float32)
        vectors: list[list[float]] = []
        total = len(texts)
        for start in range(0, total, self._batch_size):
            batch = [t or "" for t in texts[start : start + self._batch_size]]
            resp = self._client.embed(model=self._model, input=batch)
            vectors.extend(resp["embeddings"])
            done = min(start + self._batch_size, total)
            if total > self._batch_size:
                logger.info(f"embedding 進捗: {done}/{total}")
        matrix = np.asarray(vectors, dtype=np.float32)
        self._dim = matrix.shape[1]
        return l2_normalize(matrix)
