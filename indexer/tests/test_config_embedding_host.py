"""`Settings.embedding_host` のフォールバック（embedding だけ別の Ollama へ向ける）"""

import ast
from pathlib import Path

from kawasemi_indexer.config import Settings


def test_embedding_host_falls_back_to_ollama_base_url() -> None:
    s = Settings(_env_file=None, ollama_base_url="http://llm:80", embedding_base_url="  ")
    assert s.embedding_host == "http://llm:80"


def test_embedding_host_prefers_embedding_base_url() -> None:
    s = Settings(
        _env_file=None, ollama_base_url="http://llm:80", embedding_base_url="http://localhost:11434"
    )
    assert s.embedding_host == "http://localhost:11434"
    assert s.ollama_base_url == "http://llm:80"


def test_every_embedder_construction_uses_embedding_host() -> None:
    """`OllamaEmbedder(...)` の `base_url` は全箇所 `settings.embedding_host`（AST で完全一致）"""
    repo = Path(__file__).resolve().parents[1]
    found: list[str] = []
    wrong: list[str] = []
    for path in [*(repo / "kawasemi_indexer").rglob("*.py"), *(repo / "scripts").rglob("*.py")]:
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not (isinstance(node, ast.Call) and ast.unparse(node.func) == "OllamaEmbedder"):
                continue
            where = f"{path.relative_to(repo)}:{node.lineno}"
            found.append(where)
            base_url = [ast.unparse(k.value) for k in node.keywords if k.arg == "base_url"]
            if node.args or base_url != ["settings.embedding_host"]:
                wrong.append(f"{where} base_url={base_url} args={len(node.args)}")
    assert len(found) >= 5, found  # 検査が空にならないこと
    assert not wrong, wrong
