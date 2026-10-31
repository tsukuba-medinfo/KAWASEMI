"""リソース索引（builder / store / schema）の契約テスト

実Ollama・施設の定義書に依存しない。db入力は既存フィクスチャ
（tests/fixtures/db_definition/L2/column_index.jsonl）を流用し、embedding は
決定的なハッシュベクトル化器でスタブする（語彙重複の cosine で順位が決まる）。
"""

from __future__ import annotations

import json
import re
import unicodedata
import zlib
from pathlib import Path

import numpy as np
import pytest

from kawasemi_indexer.builder import (
    build_db_index,
    build_db_units,
    infer_patient_key,
)
from kawasemi_indexer.embedder import l2_normalize
from kawasemi_indexer.schema import SCHEMA_VERSION, Kind
from kawasemi_indexer.store import ResourceIndexStore

FIXTURE_DIR = str(Path(__file__).resolve().parents[0] / "fixtures" / "db_definition")
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    """db_definition と同方針の簡易トークナイザ（ascii-token + 非asciiのchar-bigram）。"""
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
    """語彙をハッシュして次元へ詰める決定的な埋め込み（cosine=語彙重複）。"""

    def __init__(self, model: str = "test-hash", dim: int = 64) -> None:
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
            for tok in _tokenize(text):
                mat[i, zlib.crc32(tok.encode("utf-8")) % self._dim] += 1.0
        return l2_normalize(mat)


@pytest.fixture
def built_index(tmp_path) -> Path:
    """フィクスチャのDB定義書から tmp に索引を生成して返す。"""
    out = tmp_path / "resource_index"
    build_db_index(source=FIXTURE_DIR, out_dir=out, embedder=HashingEmbedder())
    return out


# --------------------------------------------------------------------------- #
# builder: ユニット組み立てと patient_key 推定
# --------------------------------------------------------------------------- #


def _fixture_columns() -> list[dict]:
    path = Path(FIXTURE_DIR) / "L2" / "column_index.jsonl"
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def test_build_db_units_payload_shape():
    units = build_db_units(_fixture_columns())
    assert len(units) == 5
    blast = next(u for u in units if u.payload["physical_name"] == "BLAST_PCT")
    assert blast.kind == Kind.DB
    assert blast.unit_id == "db:DEMO.LAB.BLAST_PCT"
    assert blast.label == "芽球割合"
    assert blast.payload["connection_id"] == "DEMO_DB"
    assert blast.payload["db_qualified"] == "DEMO.LAB"
    assert "芽球割合" in blast.search_text


def test_infer_patient_key_pattern_and_pk():
    cols = _fixture_columns()
    patient_table = [c for c in cols if c["db_qualified"] == "DEMO.PATIENT"]
    # PATIENT_ID は patient_id パターン一致かつ PK
    assert infer_patient_key(patient_table) == "PATIENT_ID"
    # LAB は患者識別列も単一PKも無い → 推定不可
    lab_table = [c for c in cols if c["db_qualified"] == "DEMO.LAB"]
    assert infer_patient_key(lab_table) is None


def test_patient_key_attached_to_all_columns_of_table():
    units = build_db_units(_fixture_columns())
    patient_units = [u for u in units if u.payload["db_qualified"] == "DEMO.PATIENT"]
    assert patient_units and all(u.payload["patient_key"] == "PATIENT_ID" for u in patient_units)
    lab_units = [u for u in units if u.payload["db_qualified"] == "DEMO.LAB"]
    assert all(u.payload["patient_key"] is None for u in lab_units)


# --------------------------------------------------------------------------- #
# builder: 出力契約
# --------------------------------------------------------------------------- #


def test_build_writes_contract_files(built_index):
    assert (built_index / "db_index.jsonl").exists()
    assert (built_index / "embeddings.npy").exists()
    info = json.loads((built_index / "build_info.json").read_text("utf-8"))
    assert info["schema_version"] == SCHEMA_VERSION
    assert info["embedding_model"] == "test-hash"
    assert info["unit_count"] == 5
    assert info["kind_counts"] == {"db": 5}
    matrix = np.load(built_index / "embeddings.npy")
    assert matrix.shape == (5, info["dim"])


# --------------------------------------------------------------------------- #
# store: ロードと検索
# --------------------------------------------------------------------------- #


def test_store_loads_and_searches(built_index):
    store = ResourceIndexStore.load(built_index)
    assert store.is_loaded
    assert store.unit_count == 5
    assert store.is_searchable("test-hash")

    embedder = HashingEmbedder()
    qvec = embedder.embed(["芽球割合"])[0]
    hits = store.search(qvec, top_k=3)
    assert hits
    assert hits[0].unit.payload["physical_name"] == "BLAST_PCT"
    assert hits[0].cand_id == 0
    assert hits[0].score == max(h.score for h in hits)


def test_store_search_min_score_filters_and_reindexes(built_index):
    store = ResourceIndexStore.load(built_index)
    qvec = HashingEmbedder().embed(["芽球割合"])[0]
    full = store.search(qvec, top_k=5)
    assert len(full) >= 2

    # 1位と2位の中間に閾値を置くと先頭のみ残り、cand_id は振り直される。
    # （hit.score は round(_, 4) 済みのため ±1e-6 では丸め誤差に埋もれる。中点で安定させる）
    threshold = (full[0].score + full[1].score) / 2
    filtered = store.search(qvec, top_k=5, min_score=threshold)
    assert len(filtered) == 1
    assert filtered[0].cand_id == 0
    assert filtered[0].unit.unit_id == full[0].unit.unit_id

    # 到達不能な閾値（cosine上限超え）では全件足切り
    assert store.search(qvec, top_k=5, min_score=2.0) == []


def test_store_model_mismatch_not_searchable(built_index):
    store = ResourceIndexStore.load(built_index)
    assert store.is_loaded
    assert not store.is_searchable("some-other-model")


def test_store_missing_dir_is_empty(tmp_path):
    store = ResourceIndexStore.load(tmp_path / "nope")
    assert not store.is_loaded
    assert store.search(np.zeros(64, dtype=np.float32)) == []


def test_store_schema_incompatible_degrades(built_index):
    info_path = built_index / "build_info.json"
    info = json.loads(info_path.read_text("utf-8"))
    info["schema_version"] = SCHEMA_VERSION + 1
    info_path.write_text(json.dumps(info), encoding="utf-8")

    store = ResourceIndexStore.load(built_index)
    assert not store.schema_compatible
    assert not store.is_loaded
    assert store.search(np.zeros(64, dtype=np.float32)) == []


def test_store_dim_mismatch_returns_empty(built_index):
    store = ResourceIndexStore.load(built_index)
    # 索引次元(64)と異なる長さのクエリは空を返す（誤検索を避ける）
    assert store.search(np.ones(32, dtype=np.float32), top_k=3) == []
