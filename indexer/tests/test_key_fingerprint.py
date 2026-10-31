"""患者キー フォーマット指紋（--from-live）のテスト。

指紋統計・PHI非保持・key_type 適合スコア・重なり率、および KeyTypeDrafter の
--from-live 合成（列名が無意味でも数値/ゼロ詰め/ユニーク性で候補を補強）を検証する。
実DBは一時 sqlite（Ollama 非依存）。
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from types import SimpleNamespace

from kawasemi_indexer.key_fingerprint import (
    ColumnFingerprint,
    compute_fingerprint,
    fingerprint_score,
    sample_column,
)
from kawasemi_indexer.key_type_drafter import KeyTypeDrafter
from kawasemi_indexer.key_types import CanonicalSpec, KeyType, KeyTypeCatalog
from kawasemi_indexer.schema import ResourceUnit

_NUMERIC = CanonicalSpec(kind="numeric_string")
_CATALOG = KeyTypeCatalog(
    {
        "mrn": KeyType("mrn", "院内MRN", canonical=_NUMERIC),
        "fhir_patient_id": KeyType("fhir_patient_id", "FHIR Patient.id"),  # string
    }
)


# --------------------------------------------------------------------------- #
# 指紋の計算・PHI 非保持
# --------------------------------------------------------------------------- #
def test_compute_fingerprint_stats() -> None:
    fp = compute_fingerprint(["00123", "00456", "00789", "01234"])
    assert fp.sample_count == 4
    assert fp.numeric_rate == 1.0
    assert fp.zero_pad_rate == 1.0
    assert fp.distinct_rate == 1.0
    assert fp.modal_length == 5 and fp.fixed_length_rate == 1.0


def test_compute_fingerprint_empty() -> None:
    fp = compute_fingerprint([None, "", "  "])  # type: ignore[list-item]
    assert fp.sample_count == 0
    assert fingerprint_score(fp, _CATALOG.all()[0]) == 0.0


def test_fingerprint_holds_no_raw_values() -> None:
    """生値は保持されない（統計量とハッシュのみ。repr にハッシュも出さない）。"""
    fp = compute_fingerprint(["12345678", "87654321"])
    text = repr(fp)
    assert "12345678" not in text and "87654321" not in text
    # ハッシュ集合はあるが生値ではない（16桁hex）。
    assert all(len(h) == 16 and "12345678" not in h for h in fp.hashes)


def test_fingerprint_score_numeric_vs_string() -> None:
    """数値・ゼロ詰め・高ユニークな列は numeric_string key_type に高適合。"""
    fp = compute_fingerprint(["0000000123", "0000000456", "0000000789", "0000001011"])
    mrn = _CATALOG.get("mrn")
    fpid = _CATALOG.get("fhir_patient_id")
    assert fingerprint_score(fp, mrn) > fingerprint_score(fp, fpid)


def test_overlap_jaccard() -> None:
    a = compute_fingerprint(["1", "2", "3", "4"])
    b = compute_fingerprint(["3", "4", "5", "6"])
    assert 0.0 < a.overlap(b) < 1.0
    assert a.overlap(a) == 1.0
    assert a.overlap(ColumnFingerprint()) == 0.0


# --------------------------------------------------------------------------- #
# 実DBサンプリング（一時 sqlite）
# --------------------------------------------------------------------------- #
def _live_db(tmp_path: Path) -> str:
    db = tmp_path / "live.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE PATIENT (COL_017 TEXT, MEMO TEXT);
        INSERT INTO PATIENT VALUES ('0000012345','x'),('0000023456','y'),('0000034567','z');
        """
    )
    conn.commit()
    conn.close()
    return f"sqlite:///{db}"


def test_sample_column_reads_readonly(tmp_path: Path) -> None:
    dsn = _live_db(tmp_path)
    fp = sample_column(dsn, "PATIENT", "COL_017")
    assert fp.sample_count == 3
    assert fp.numeric_rate == 1.0 and fp.zero_pad_rate == 1.0


def test_from_live_boosts_meaningless_column_name(tmp_path: Path) -> None:
    """列名が無意味（COL_017）でも、指紋で mrn 候補を上位提示できる。"""
    dsn = _live_db(tmp_path)
    unit = ResourceUnit(
        unit_id="db:PATIENT.COL_017",
        kind="db",
        label="COL_017",
        search_text="COL_017",
        snippet="COL_017",
        payload={
            "physical_name": "COL_017",
            "logical_name": "COL_017",
            "patient_key": "COL_017",
            "db_qualified": "PATIENT",
        },
    )
    drafter = KeyTypeDrafter(catalog=_CATALOG, store=SimpleNamespace(units=[unit]))  # type: ignore[arg-type]

    # 名称のみ（従来）: COL_017 は mrn とトークン一致せず候補スコアはほぼ0。
    name_only = drafter.draft()[0]
    assert name_only.candidates[0].score == 0.0 or name_only.candidates[0].key_type != "mrn"

    # --from-live: 指紋（数値・ゼロ詰め・ユニーク）で mrn が上位に。
    live = drafter.draft(from_live=True, dsn=dsn)[0]
    assert live.candidates[0].key_type == "mrn"
    assert live.candidates[0].score > 0
    assert live.fingerprint is not None and "numeric=1.00" in live.fingerprint
    # 生値は fingerprint サマリに出ない。
    assert "0000012345" not in live.fingerprint


def _overlap_db(tmp_path: Path) -> str:
    """既知キー列 KNOWN_MRN と、値空間が重なる無名列 COL_X を持つ sqlite。"""
    db = tmp_path / "overlap.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE T_KNOWN (KNOWN_MRN TEXT);
        CREATE TABLE T_CAND  (COL_X TEXT);
        INSERT INTO T_KNOWN VALUES ('00001'),('00002'),('00003'),('00004');
        INSERT INTO T_CAND  VALUES ('00002'),('00003'),('00004'),('00005');
        """
    )
    conn.commit()
    conn.close()
    return f"sqlite:///{db}"


def test_overlap_with_known_key_column_boosts_matching_key_type(tmp_path: Path) -> None:
    """既に mrn と分かっている列と値集合が重なる無名列は、重なり率で mrn が補強される。"""
    dsn = _overlap_db(tmp_path)

    def _u(unit_id, table, phys, key_type=None):
        return ResourceUnit(
            unit_id=unit_id,
            kind="db",
            label=phys,
            search_text=phys,
            snippet=phys,
            payload={
                "physical_name": phys,
                "logical_name": phys,
                "patient_key": phys,
                "db_qualified": table,
                "key_type": key_type,
            },
        )

    units = [
        _u("db:T_KNOWN.KNOWN_MRN", "T_KNOWN", "KNOWN_MRN", key_type="mrn"),  # 既知参照
        _u("db:T_CAND.COL_X", "T_CAND", "COL_X"),  # 無名候補（値が KNOWN_MRN と重なる）
    ]
    drafter = KeyTypeDrafter(catalog=_CATALOG, store=SimpleNamespace(units=units))  # type: ignore[arg-type]
    drafts = {d.unit_id: d for d in drafter.draft(from_live=True, dsn=dsn)}

    cand = drafts["db:T_CAND.COL_X"]
    mrn = next(c for c in cand.candidates if c.key_type == "mrn")
    assert mrn.overlap_score is not None and mrn.overlap_score > 0  # 重なり率が寄与
    assert cand.candidates[0].key_type == "mrn"  # 無名列でも mrn が最上位


def test_from_live_missing_dsn_falls_back_to_name(tmp_path: Path) -> None:
    """--from-live でも DSN 解決不能なら例外を投げず名称のみに縮退。"""
    unit = ResourceUnit(
        unit_id="db:PATIENT.mrn",
        kind="db",
        label="mrn",
        search_text="mrn",
        snippet="mrn",
        payload={
            "physical_name": "mrn",
            "logical_name": "mrn",
            "patient_key": "mrn",
            "db_qualified": "PATIENT",
        },
    )
    drafter = KeyTypeDrafter(catalog=_CATALOG, store=SimpleNamespace(units=[unit]))  # type: ignore[arg-type]
    # dsn 未指定・connections も無い → 指紋なしで名称ベースの候補が返る（クラッシュしない）。
    drafts = drafter.draft(from_live=True, dsn="sqlite:////nonexistent/x.db")
    assert drafts[0].candidates  # 候補は返る（指紋失敗は握りつぶす）
