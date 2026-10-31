"""索引の受け渡し契約のゴールデン試験（CONTRACT.md §6）。

indexer（ビルド側）と KAWASEMI 本体（クエリ側）は別のネットワークで動くため、
`embedder` / `schema` を双方が別々に持つ。複製が静かにズレると検索が壊れるので、
**両方の複製**を同じ期待値で検査する。埋め込みモデルそのものの出力は再現できないので
ここでは固定しない。モデルの同一性は `build_info.json` の互換チェックが守る。
"""

from __future__ import annotations

from types import ModuleType

import kawasemi_indexer.embedder as indexer_embedder
import kawasemi_indexer.schema as indexer_schema
import numpy as np
import pytest

import kawasemi.ai_input.resource_index.embedder as main_embedder
import kawasemi.ai_input.resource_index.schema as main_schema

SIDES = pytest.mark.parametrize(
    ("schema", "embedder"),
    [(main_schema, main_embedder), (indexer_schema, indexer_embedder)],
    ids=["main", "indexer"],
)


# --- レイアウト契約（CONTRACT.md §1, §3） ---------------------------------- #


@SIDES
def test_schema_version_pinned(schema: ModuleType, embedder: ModuleType) -> None:
    # 契約を変えたら version を上げ、この期待値も更新すること（意図的な変更の関門）。
    assert schema.SCHEMA_VERSION == 2


@SIDES
def test_index_layout_constants(schema: ModuleType, embedder: ModuleType) -> None:
    assert schema.EMBEDDINGS_FILENAME == "embeddings.npy"
    assert schema.BUILD_INFO_FILENAME == "build_info.json"
    # embeddings.npy の行連結順は決定的でなければならない（検索側が依存）。
    # 新 kind は末尾に追加し、既存 kind の行オフセットを保つ。
    assert schema.Kind.ORDER == ("db", "file", "fhir", "master")
    assert schema.Kind.INDEX_FILENAMES == {
        "db": "db_index.jsonl",
        "file": "file_index.jsonl",
        "fhir": "fhir_index.jsonl",
        "master": "master_index.jsonl",
    }


# --- レコード契約（CONTRACT.md §1） ---------------------------------------- #


@SIDES
def test_resource_unit_field_set(schema: ModuleType, embedder: ModuleType) -> None:
    # ユニットの共通ヘッダのフィールド集合が契約どおりであること。
    assert set(schema.ResourceUnit.model_fields) == {
        "unit_id",
        "kind",
        "label",
        "search_text",
        "snippet",
        "payload",
    }


@SIDES
def test_build_info_field_set(schema: ModuleType, embedder: ModuleType) -> None:
    assert set(schema.BuildInfo.model_fields) == {
        "schema_version",
        "indexer_version",
        "embedding_model",
        "embedding_digest",
        "dim",
        "normalized",
        "unit_count",
        "kind_counts",
        "built_at",
    }


# --- 正規化契約（CONTRACT.md §2） ------------------------------------------ #


@SIDES
def test_l2_normalize_golden_vector(schema: ModuleType, embedder: ModuleType) -> None:
    """l2 正規化が固定入力に対し既知の出力を返すこと（正規化のズレ検知）。"""
    mat = np.array([[3.0, 4.0], [1.0, 0.0], [0.0, 0.0]], dtype=np.float32)
    out = embedder.l2_normalize(mat)
    expected = np.array([[0.6, 0.8], [1.0, 0.0], [0.0, 0.0]], dtype=np.float32)
    assert np.allclose(out, expected, atol=1e-6)
    # 正規化後の各行ノルムは 1（ゼロ行を除く）。
    norms = np.linalg.norm(out, axis=1)
    assert np.allclose(norms[:2], 1.0, atol=1e-6)
