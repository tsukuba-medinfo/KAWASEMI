"""OllamaEmbedder のバッチ埋め込み（perf/embedder-batch）のユニットテスト

実 Ollama 非依存。client.embed をフェイクに差し替え、batch_size ごとの分割呼び出しと
ベクトル連結・正規化・dim 確定を検証する。
"""

from __future__ import annotations

import numpy as np

from kawasemi.ai_input.resource_index.embedder import OllamaEmbedder


class _FakeClient:
    """input(list) に対し各テキスト長を1次元目に入れた3次元ベクトルを返す。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def embed(self, model: str, input: list[str]):  # noqa: A002
        self.calls.append(list(input))
        return {"embeddings": [[float(len(t)), 1.0, 0.0] for t in input]}


def _make_embedder(batch_size: int) -> tuple[OllamaEmbedder, _FakeClient]:
    emb = OllamaEmbedder.__new__(OllamaEmbedder)  # __init__ をバイパス（ollama不要）
    emb._model = "bge-m3"
    emb._dim = 0
    emb._batch_size = batch_size
    fake = _FakeClient()
    emb._client = fake
    return emb, fake


def test_embed_batches_and_concatenates():
    emb, fake = _make_embedder(batch_size=2)
    mat = emb.embed(["a", "bb", "ccc", "dddd", "eeeee"])  # 5件 → 2+2+1 の3バッチ
    assert mat.shape == (5, 3)
    assert emb.dim == 3
    assert [len(c) for c in fake.calls] == [2, 2, 1]
    # 行は L2 正規化済み
    norms = np.linalg.norm(mat, axis=1)
    assert np.allclose(norms, 1.0)


def test_embed_empty():
    emb, fake = _make_embedder(batch_size=64)
    mat = emb.embed([])
    assert mat.shape[0] == 0
    assert fake.calls == []


def test_embed_none_text_coerced():
    emb, fake = _make_embedder(batch_size=64)
    emb.embed(["x", None])  # None は "" に矯正されて送られる
    assert fake.calls[0] == ["x", ""]


def test_every_embedder_construction_uses_embedding_host() -> None:
    """`OllamaEmbedder(...)` の接続先は全箇所 `settings.embedding_host`。

    `ollama_base_url` を直接渡す箇所が1つでも残ると、`EMBEDDING_BASE_URL` を設定しても
    その経路だけ LLM 側のサーバで埋め込み、索引とクエリが別サーバのモデルで作られる。
    正規表現では1行の呼び出しや条件式を取りこぼすので、AST で `base_url` を完全一致で見る。
    """
    import ast
    from pathlib import Path

    repo = Path(__file__).resolve().parents[2]
    found: list[str] = []
    wrong: list[str] = []
    for path in [*(repo / "kawasemi").rglob("*.py"), *(repo / "scripts").rglob("*.py")]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and ast.unparse(node.func) == "OllamaEmbedder"):
                continue
            where = f"{path.relative_to(repo)}:{node.lineno}"
            found.append(where)
            base_url = [ast.unparse(k.value) for k in node.keywords if k.arg == "base_url"]
            if node.args or base_url != ["settings.embedding_host"]:
                wrong.append(f"{where} base_url={base_url} args={len(node.args)}")
    assert len(found) >= 4, found  # 検査が空にならないこと
    assert not wrong, wrong
