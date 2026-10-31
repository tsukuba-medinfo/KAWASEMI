"""ai_input テスト共通のフィクスチャ"""

from __future__ import annotations

from pathlib import Path

import pytest

# Ollama 非依存の決定的な擬似 embedder。E2E の決定的層と共用するため
# `kawasemi/ai_input/e2e/embedder.py` へ移した。名前はここからも引ける（既存テストの import 先）。
from kawasemi.ai_input.e2e.embedder import HashingEmbedder  # noqa: F401
from kawasemi.ai_input.resource_index import connections as conn_mod

# file/csv extract のテストが使う接続ID（root は各テストの tmp_path）。
TEST_FS_CONNECTION = "test_fs"


@pytest.fixture
def fs_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`TEST_FS_CONNECTION` の root_path を tmp_path に向けた接続レジストリを用意する。

    `file_extract` / `csv_extract` は接続ルート配下しか読まない。
    テストも実運用と同じく、接続を登録して root 起点の相対パスで書く。
    """
    registry = conn_mod.ConnectionRegistry(
        {
            TEST_FS_CONNECTION: {
                "kind": "file",
                "scope": "external",
                "root_path": str(tmp_path),
            }
        }
    )
    monkeypatch.setattr(conn_mod, "get_registry", lambda: registry)
    monkeypatch.setattr("kawasemi.ai_input.executor.get_registry", lambda: registry)
    return tmp_path
