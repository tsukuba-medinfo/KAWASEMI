"""対話デバッガ（debug_repl）のテスト

実 Ollama・施設の定義書に依存しない。db 入力は既存フィクスチャを流用し、embedding は
決定的ハッシュ化器（HashingEmbedder, test_resource_index と同方針）でスタブする。
I/O ループは対象外で、純粋関数 dispatch()/run_query() とフォーマッタを直接叩く。
"""

from __future__ import annotations

import json
import re
import unicodedata
import zlib
from pathlib import Path

import numpy as np
import pytest

from kawasemi_indexer import debug_repl as dr
from kawasemi_indexer.builder import build_db_index
from kawasemi_indexer.embedder import l2_normalize
from kawasemi_indexer.schema import Kind, ResourceHit, ResourceUnit
from kawasemi_indexer.store import ResourceIndexStore

FIXTURE_DIR = str(Path(__file__).resolve().parents[0] / "fixtures" / "db_definition")
_ASCII_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _tokenize(text: str) -> list[str]:
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
        mat = np.zeros((len(texts), self._dim), dtype=np.float32)
        for i, text in enumerate(texts):
            for tok in _tokenize(text):
                mat[i, zlib.crc32(tok.encode("utf-8")) % self._dim] += 1.0
        return l2_normalize(mat)


@pytest.fixture
def session(tmp_path) -> dr.Session:
    out = tmp_path / "resource_index"
    build_db_index(source=FIXTURE_DIR, out_dir=out, embedder=HashingEmbedder())
    store = ResourceIndexStore.load(out)
    assert store.is_loaded
    return dr.Session(store=store, embedder=HashingEmbedder(), model="test-hash")


# --------------------------------------------------------------------------- #
# クエリ → データの場所
# --------------------------------------------------------------------------- #
def test_query_returns_location(session):
    out = dr.run_query(session, "芽球割合")
    text = "\n".join(out)
    assert "DEMO_DB / DEMO.LAB / BLAST_PCT" in text
    assert "芽球割合" in text
    assert session.last_hits  # 直近候補が記録される
    assert session.last_qv is not None


def test_query_lexical_hit_annotation(session):
    out = dr.run_query(session, "芽球割合")
    text = "\n".join(out)
    assert "↳ hit:" in text  # 語彙一致が示される


def test_query_empty_after_filter(session):
    session.kind_filter = Kind.FILE  # file ユニットは無い
    out = dr.run_query(session, "芽球割合")
    assert any("全除外" in ln or "候補なし" in ln for ln in out)


# --------------------------------------------------------------------------- #
# メタコマンド
# --------------------------------------------------------------------------- #
def test_show_full_payload(session):
    dr.run_query(session, "患者ID")
    out = dr.dispatch(":show 0", session)
    text = "\n".join(out)
    assert "physical_name" in text
    assert "search_text" in text


def test_show_without_results(session):
    out = dr.dispatch(":show 0", session)
    assert any("直近の検索結果がありません" in ln for ln in out)


def test_show_bad_index(session):
    dr.run_query(session, "患者ID")
    out = dr.dispatch(":show 999", session)
    assert any("候補番号が不正" in ln for ln in out)


def test_grep_introspection(session):
    out = dr.dispatch(":grep PATIENT_ID", session)
    text = "\n".join(out)
    assert "db:DEMO.PATIENT.PATIENT_ID" in text


def test_grep_no_match(session):
    out = dr.dispatch(":grep ZZZ_NOPE", session)
    assert any("一致するユニットなし" in ln for ln in out)


def test_why_existence_and_rank(session):
    session.top_k = 1
    dr.run_query(session, "芽球割合")
    out = dr.dispatch(":why PATIENT_ID", session)
    text = "\n".join(out)
    assert "索引に" in text and "存在" in text
    assert "第" in text  # top-k=1 外でも順位が出る


def test_why_without_query(session):
    out = dr.dispatch(":why PATIENT_ID", session)
    assert any("先にクエリを実行" in ln for ln in out)


def test_why_not_found(session):
    out = dr.dispatch(":why ZZZ_NOPE", session)
    assert any("見つかりません" in ln for ln in out)


def test_set_topk_applies(session):
    dr.dispatch(":k 2", session)
    assert session.top_k == 2
    out = dr.run_query(session, "日時")
    # ヘッダ行を除いた候補ブロックは top_k=2 まで（[n] 始まり行が 2 本以下）
    heads = [ln for ln in out if re.match(r"^\[\d+\]", ln)]
    assert len(heads) <= 2


def test_set_min_score(session):
    out = dr.dispatch(":t 0.3", session)
    assert session.min_score == 0.3
    assert any("min_score = 0.3" in ln for ln in out)


def test_set_topk_invalid(session):
    out = dr.dispatch(":k zero", session)
    assert any("正の整数" in ln for ln in out)
    assert session.top_k == 8


def test_kind_filter_command(session):
    dr.dispatch(":kind db", session)
    assert session.kind_filter == "db"
    dr.dispatch(":kind all", session)
    assert session.kind_filter is None
    bad = dr.dispatch(":kind bogus", session)
    assert any("kind は" in ln for ln in bad)


def test_conn_filter_command(session):
    dr.dispatch(":conn DEMO_DB", session)
    assert session.conn_filter == "DEMO_DB"
    dr.dispatch(":conn all", session)
    assert session.conn_filter is None


def test_json_output_parseable(session):
    dr.run_query(session, "患者ID")
    out = dr.dispatch(":json 0", session)
    obj = json.loads("\n".join(out))
    assert obj["unit"]["unit_id"].startswith("db:")


def test_unknown_command(session):
    out = dr.dispatch(":bogus", session)
    assert any("未知のコマンド" in ln for ln in out)


def test_help(session):
    out = dr.dispatch(":help", session)
    text = "\n".join(out)
    assert ":show" in text and ":why" in text


def test_startup_header(session):
    out = dr.startup_header(session)
    text = "\n".join(out)
    assert "embedding_model=test-hash" in text
    assert "connection_id=" in text


# --------------------------------------------------------------------------- #
# フォーマッタ単体（value_dict 由来・僅差・語彙なし）
# --------------------------------------------------------------------------- #
def _fmt_session() -> dr.Session:
    return dr.Session(store=None, embedder=None, model="x")  # type: ignore[arg-type]


def _vd_unit() -> ResourceUnit:
    return ResourceUnit(
        unit_id="db:DEMO.PATIENT.SEX",
        kind=Kind.DB,
        label="性別",
        search_text="sex 性別 男性 女性",
        payload={
            "connection_id": "DEMO_DB",
            "db_qualified": "DEMO.PATIENT",
            "physical_name": "SEX",
            "logical_name": "性別",
            "type_raw": "CHAR(1)",
            "value_dict": [{"code": "1", "label": "男性"}, {"code": "2", "label": "女性"}],
        },
    )


def test_value_dict_attribution():
    hits = dr._value_dict_hits(dr._norm("男性の患者"), _vd_unit())
    assert hits == ["\"男性\"=SEX'1'"]


def test_format_hit_shows_value_dict():
    hit = ResourceHit(cand_id=0, unit=_vd_unit(), score=0.5)
    lines = dr._format_hit(_fmt_session(), hit, dr._norm("男性"), dr._probes("男性"), False)
    text = "\n".join(lines)
    assert "← value_dict: \"男性\"=SEX'1'" in text


def test_format_hit_semantic_only():
    hit = ResourceHit(cand_id=0, unit=_vd_unit(), score=0.5)
    lines = dr._format_hit(_fmt_session(), hit, dr._norm("xyz"), dr._probes("xyz"), False)
    assert any("意味的近接のみ" in ln for ln in lines)


def test_format_hit_ambiguous_marker():
    hit = ResourceHit(cand_id=1, unit=_vd_unit(), score=0.5)
    lines = dr._format_hit(_fmt_session(), hit, dr._norm("男性"), dr._probes("男性"), True)
    assert "⚠ambiguous" in lines[0]


# --------------------------------------------------------------------------- #
# kind=master の場所表示
# --------------------------------------------------------------------------- #
def _master_unit() -> ResourceUnit:
    return ResourceUnit(
        unit_id="master:dpc:DPC_DX_MASTER:C920",
        kind=Kind.MASTER,
        label="急性骨髄性白血病",
        search_text="急性骨髄性白血病",
        snippet="マスタ DPC_DX_MASTER 急性骨髄性白血病（code=C920）",
        payload={
            "connection_id": "dpc",
            "db_qualified": "DPC_DX_MASTER",
            "code": "C920",
            "label": "急性骨髄性白血病",
        },
    )


def test_master_location_shows_code():
    assert dr._location(_master_unit()) == "dpc / DPC_DX_MASTER / code=C920"


def test_build_and_search_master_units(tmp_path):
    """value_dictionary のマスタ表自身エントリ → master_index.jsonl → debugで surface。"""
    src = tmp_path / "db_definition"
    (src / "L2").mkdir(parents=True)
    (src / "L2" / "column_index.jsonl").write_text(
        json.dumps(
            {
                "db_group": "HOSP",
                "db_qualified": "DM.DISEASE",
                "physical_name": "BYOMEI_CD",
                "logical_name": "病名コード",
                "type_raw": "CHAR(7)",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    # マスタ表自身（physical_name なし）の code→label
    (src / "value_dictionary.jsonl").write_text(
        "\n".join(
            json.dumps(
                {"connection_id": "HOSP", "db_qualified": "DM.BYOMEI_MST", "code": c, "label": ll},
                ensure_ascii=False,
            )
            for c, ll in [("C920", "急性骨髄性白血病"), ("C910", "急性リンパ性白血病")]
        )
        + "\n",
        encoding="utf-8",
    )

    out = tmp_path / "resource_index"
    build_db_index(source=src, out_dir=out, embedder=HashingEmbedder())
    assert (out / "master_index.jsonl").exists()

    store = ResourceIndexStore.load(out)
    assert store.is_loaded
    master_units = [u for u in store.units if u.kind == Kind.MASTER]
    assert {u.label for u in master_units} == {"急性骨髄性白血病", "急性リンパ性白血病"}

    session = dr.Session(store=store, embedder=HashingEmbedder(), model="test-hash")
    out_lines = dr.run_query(session, "急性骨髄性白血病")
    text = "\n".join(out_lines)
    assert "HOSP / DM.BYOMEI_MST / code=C920" in text

    # :grep は master を label で拾える（physical/logical を持たない kind）
    grep = "\n".join(dr.dispatch(":grep 急性リンパ", session))
    assert "master:HOSP:DM.BYOMEI_MST:C910" in grep
    # :grep はコードでも拾える
    assert "C920" in "\n".join(dr.dispatch(":grep C920", session))
    # :why は master を label で見つけ、直近クエリでの順位を返す
    why = "\n".join(dr.dispatch(":why 急性骨髄性白血病", session))
    assert "索引に" in why and "第" in why


def test_build_master_units_aliases():
    """別名は search_text と payload に取り込まれる（';'区切り文字列・配列の両方）。"""
    from kawasemi_indexer.builder import build_master_units

    entries = [
        {
            "connection_id": "C",
            "db_qualified": "M",
            "code": "C920",
            "label": "急性骨髄性白血病",
            "aliases": "AML;急性骨髄芽球性白血病",
        },
        {
            "connection_id": "C",
            "db_qualified": "M",
            "code": "E11",
            "label": "2型糖尿病",
            "aliases": ["DM", "糖尿病(2型)"],
        },
    ]
    units = build_master_units(entries)
    aml = next(u for u in units if u.payload["code"] == "C920")
    assert "AML" in aml.search_text and "急性骨髄芽球性白血病" in aml.search_text
    assert aml.payload["aliases"] == ["AML", "急性骨髄芽球性白血病"]
    dm = next(u for u in units if u.payload["code"] == "E11")
    assert dm.payload["aliases"] == ["DM", "糖尿病(2型)"]


def test_format_hit_master_kind():
    hit = ResourceHit(cand_id=0, unit=_master_unit(), score=0.7)
    lines = dr._format_hit(_fmt_session(), hit, dr._norm("白血病"), dr._probes("白血病"), False)
    text = "\n".join(lines)
    assert "code=C920" in text and "急性骨髄性白血病" in text
