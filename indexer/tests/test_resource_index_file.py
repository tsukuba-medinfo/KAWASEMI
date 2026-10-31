"""kind=file の indexer の契約テスト

- chunk_text: 文字数ベースのオーバーラップ分割。
- build_reference_units: text/markdown を走査・チャンク化（PDFはpypdf抽出を
  monkeypatchして経路のみ検証）。payload は file_role=reference。
- build_manifest_units: patient_data の所在宣言（中身は索引しない）。
- build_index 複数kind: db+file の行整合を store 検索で確認。
"""

from __future__ import annotations

import re
import unicodedata
import zlib
from pathlib import Path

import numpy as np

from kawasemi_indexer import file_indexer
from kawasemi_indexer.builder import build_index
from kawasemi_indexer.file_indexer import (
    build_manifest_units,
    build_reference_units,
    chunk_text,
    read_manifest,
)
from kawasemi_indexer.schema import Kind
from kawasemi_indexer.store import ResourceIndexStore

FIX = Path(__file__).resolve().parents[0] / "fixtures"
DB_DIR = str(FIX / "db_definition")
REF_DIR = str(FIX / "file_reference")
MANIFEST_DIR = str(FIX / "file_manifest")
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
        from kawasemi_indexer.embedder import l2_normalize

        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)
        mat = np.zeros((len(texts), self._dim), dtype=np.float32)
        for i, t in enumerate(texts):
            for tok in _tok(t):
                mat[i, zlib.crc32(tok.encode("utf-8")) % self._dim] += 1.0
        return l2_normalize(mat)


# --------------------------------------------------------------------------- #
# chunk_text
# --------------------------------------------------------------------------- #


def test_chunk_text_overlap():
    text = "あ" * 250
    chunks = chunk_text(text, chunk_chars=100, overlap=20)
    # step = 100-20 = 80。0,80,160 で開始し、160 のチャンクが末尾(250)に達して終了
    assert [c[0] for c in chunks] == [0, 80, 160]
    assert chunks[0][1] == 100 and chunks[0][2] == "あ" * 100
    assert chunks[-1] == (160, 250, "あ" * 90)


def test_chunk_text_empty():
    assert chunk_text("") == []
    assert chunk_text("   ") == []


# --------------------------------------------------------------------------- #
# reference 走査
# --------------------------------------------------------------------------- #


def test_build_reference_units_text_and_md():
    units = build_reference_units(REF_DIR)
    assert units, "reference ユニットが返るべき"
    paths = {u.payload["path"] for u in units}
    assert "protocol.md" in paths and "notes.txt" in paths
    proto = next(u for u in units if u.payload["path"] == "protocol.md")
    assert proto.kind == Kind.FILE
    assert proto.payload["file_role"] == "reference"
    assert proto.payload["mime"] == "text/markdown"
    assert proto.payload["page"] is None
    assert "プロトコル" in proto.search_text
    # 注: source_category/location の導出（derive）は KAWASEMI 本体（kawasemi/）の責務。


def test_build_reference_units_pdf_path(tmp_path, monkeypatch):
    # 実PDF生成を避け、pypdf 抽出を monkeypatch して PDF 経路のみ検証
    monkeypatch.setattr(
        file_indexer, "_extract_pdf_pages", lambda p: ["1ページ目の本文", "2ページ目の本文"]
    )
    (tmp_path / "manual.pdf").write_bytes(b"%PDF-1.4 dummy")
    units = build_reference_units(tmp_path)
    pages = sorted(u.payload["page"] for u in units)
    assert pages == [1, 2]
    assert all(u.payload["mime"] == "application/pdf" for u in units)
    assert units[0].unit_id.startswith("file:manual.pdf#p1:")


def test_build_reference_units_skips_symlinks(tmp_path):
    """シンボリックリンク経由で reference 外のファイル本文が索引へ混入しないこと
    （索引に患者データを載せない不変条件・Stage5）"""
    secret = tmp_path / "secret.txt"
    secret.write_text("患者の生データ SECRET", encoding="utf-8")
    ref = tmp_path / "ref"
    ref.mkdir()
    (ref / "ok.txt").write_text("索引対象の reference 本文", encoding="utf-8")
    link = ref / "leak.txt"
    try:
        link.symlink_to(secret)
    except (OSError, NotImplementedError):
        import pytest

        pytest.skip("symlink を作成できない環境")

    units = build_reference_units(ref)
    texts = " ".join(u.search_text for u in units)
    assert "SECRET" not in texts  # symlink 先の本文は混入しない
    assert any("reference 本文" in u.search_text for u in units)


def test_build_reference_units_skips_oversized(tmp_path, monkeypatch):
    """サイズ上限を超える reference 文書はスキップする（OOM 防止・Stage5）"""
    monkeypatch.setattr(file_indexer, "_MAX_FILE_BYTES", 10)
    (tmp_path / "small.txt").write_text("短い", encoding="utf-8")
    (tmp_path / "big.txt").write_text("超過する長い本文" * 100, encoding="utf-8")
    units = build_reference_units(tmp_path)
    paths = {u.payload["path"] for u in units}
    assert "small.txt" in paths
    assert "big.txt" not in paths


# --------------------------------------------------------------------------- #
# patient_data マニフェスト
# --------------------------------------------------------------------------- #


def test_build_manifest_units():
    units = build_manifest_units(read_manifest(MANIFEST_DIR))
    assert len(units) == 1
    u = units[0]
    assert u.kind == Kind.FILE
    assert u.payload["file_role"] == "patient_data"
    assert u.payload["path_pattern"] == "/reports/{patient_id}/*.pdf"
    assert u.payload["extract"] == {"method": "pdf_llm"}
    # 中身（PDF本体）は読まず、所在宣言のみ
    assert "search_text" not in u.payload


# --------------------------------------------------------------------------- #
# 複数kind一括（db + file）の行整合
# --------------------------------------------------------------------------- #


def test_combined_db_and_file_row_alignment(tmp_path):
    out = tmp_path / "idx"
    info = build_index(
        out, HashingEmbedder(), db_source=DB_DIR, file_source=REF_DIR, file_manifest=MANIFEST_DIR
    )
    assert info.kind_counts["db"] == 5
    assert info.kind_counts["file"] >= 3  # protocol + notes + manifest1
    store = ResourceIndexStore.load(out)
    assert store.unit_count == info.unit_count
    # 参照文書の固有語で引くと file ユニットが上位
    qv = HashingEmbedder().embed(["プロトコル番号"])[0]
    top = store.search(qv, top_k=1)[0]
    assert top.unit.kind == Kind.FILE
