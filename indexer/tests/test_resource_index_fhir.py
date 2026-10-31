"""kind=fhir の indexer と複数kind一括索引の契約テスト

- build_fhir_units: 臨床項目辞書 → fhir ユニット（payload に query_template/fhir_path）。
- build_index 複数kind: db+fhir を1回で索引化し、embeddings.npy の行が
  Kind.ORDER(db→fhir) 連結順とそろうことを store のロード/検索で検証。

注: location/source_category の導出（derive）は KAWASEMI 本体（kawasemi/）の責務のため、
そのテストは本体側（tests/）にある。
"""

from __future__ import annotations

import json
import re
import unicodedata
import zlib
from pathlib import Path

import numpy as np

from kawasemi_indexer.builder import (
    build_fhir_index,
    build_fhir_units,
    build_index,
)
from kawasemi_indexer.embedder import l2_normalize
from kawasemi_indexer.schema import Kind
from kawasemi_indexer.store import ResourceIndexStore

FIX = Path(__file__).resolve().parents[0] / "fixtures"
DB_DIR = str(FIX / "db_definition")
FHIR_DIR = str(FIX / "fhir_dictionary")
_ASCII = re.compile(r"[a-z0-9_]+")


def _tok(text: str) -> list[str]:
    if not text:
        return []
    norm = unicodedata.normalize("NFKC", text).lower()
    toks = list(_ASCII.findall(norm))
    for run in re.findall(r"[^\x00-\x7f]+", norm):
        if len(run) > 1:
            toks.extend(run[i : i + 2] for i in range(len(run) - 1))
        else:
            toks.append(run)
    return toks


class HashingEmbedder:
    def __init__(self, model: str = "bge-m3", dim: int = 64) -> None:
        self._model, self._dim = model, dim

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
        for i, t in enumerate(texts):
            for tok in _tok(t):
                mat[i, zlib.crc32(tok.encode("utf-8")) % self._dim] += 1.0
        return l2_normalize(mat)


def _fhir_items() -> list[dict]:
    path = Path(FHIR_DIR) / "fhir_dictionary.jsonl"
    return [json.loads(line) for line in path.read_text("utf-8").splitlines() if line.strip()]


def test_build_fhir_units_payload():
    units = build_fhir_units(_fhir_items())
    assert len(units) == 2
    wbc = next(u for u in units if u.label == "白血球数")
    assert wbc.kind == Kind.FHIR
    assert wbc.unit_id == "fhir:Observation:LOINC|6690-2"
    assert wbc.payload["query_template"].startswith("Observation?code=")
    assert wbc.payload["fhir_path"] == "valueQuantity.value"
    assert "WBC" in wbc.search_text


def test_fhir_only_index_roundtrip(tmp_path):
    out = tmp_path / "idx"
    info = build_fhir_index(source=FHIR_DIR, out_dir=out, embedder=HashingEmbedder())
    assert info.kind_counts == {"fhir": 2}
    store = ResourceIndexStore.load(out)
    assert store.is_loaded and store.unit_count == 2
    qv = HashingEmbedder().embed(["白血球数"])[0]
    hits = store.search(qv, top_k=2)
    assert hits[0].unit.kind == Kind.FHIR
    assert hits[0].unit.label == "白血球数"


def test_combined_db_and_fhir_row_alignment(tmp_path):
    """db+fhir を一括索引化し、行整合（npy行=Kind.ORDER連結）を検索で確認。"""
    out = tmp_path / "idx"
    info = build_index(out, HashingEmbedder(), db_source=DB_DIR, fhir_source=FHIR_DIR)
    assert info.kind_counts == {"db": 5, "fhir": 2}
    assert info.unit_count == 7

    store = ResourceIndexStore.load(out)
    assert store.unit_count == 7
    # db のカラム名で引くと db ユニットが、臨床概念で引くと fhir ユニットが上位に来る
    qv_db = HashingEmbedder().embed(["芽球割合"])[0]
    assert store.search(qv_db, top_k=1)[0].unit.payload["physical_name"] == "BLAST_PCT"
    qv_fhir = HashingEmbedder().embed(["生年月日"])[0]
    top = store.search(qv_fhir, top_k=1)[0]
    assert top.unit.kind == Kind.FHIR and top.unit.label == "生年月日"
