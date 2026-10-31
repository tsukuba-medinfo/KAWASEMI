"""索引診断 WebUI/JSON API のテスト

実 Ollama 非依存。test_debug_repl の HashingEmbedder + db フィクスチャを流用し、
webapi の純粋ロジックと webserver のルーティング/バリデーションを検証する。
test_debug_repl.py は一切変更しない。
"""

from __future__ import annotations

import pytest

# test_debug_repl と同一ディレクトリ → pytest の prepend import で解決可能
from test_debug_repl import FIXTURE_DIR, HashingEmbedder, _vd_unit

from kawasemi_indexer import debug_repl as dr
from kawasemi_indexer.builder import build_db_index
from kawasemi_indexer.schema import Kind, ResourceHit
from kawasemi_indexer.store import ResourceIndexStore, get_store, reset_store_cache
from kawasemi_indexer.webapi import (
    GrepRequest,
    InvalidKind,
    SearchRequest,
    UnitNotFound,
    WhyRequest,
    build_grep_response,
    build_search_response,
    build_show_response,
    build_status_response,
    build_why_response,
)

MODEL = "test-hash"


@pytest.fixture
def store(tmp_path) -> ResourceIndexStore:
    out = tmp_path / "resource_index"
    build_db_index(source=FIXTURE_DIR, out_dir=out, embedder=HashingEmbedder())
    st = ResourceIndexStore.load(out)
    assert st.is_loaded
    return st


@pytest.fixture
def embedder() -> HashingEmbedder:
    return HashingEmbedder()


# --------------------------------------------------------------------------- #
# hit_diagnostics（純粋関数の各分岐）
# --------------------------------------------------------------------------- #
def test_hit_diagnostics_value_dict_and_below():
    hit = ResourceHit(cand_id=0, unit=_vd_unit(), score=0.2)
    diag = dr.hit_diagnostics(
        hit, dr._norm("男性"), dr._probes("男性"), min_score=0.3, ambiguous=False
    )
    assert diag["value_dict_hits"] == ["\"男性\"=SEX'1'"]
    assert diag["below_min_score"] is True  # 0.2 < 0.3
    assert diag["location"] == "DEMO_DB / DEMO.PATIENT / SEX"
    assert diag["ambiguous"] is False


def test_hit_diagnostics_semantic_only():
    hit = ResourceHit(cand_id=0, unit=_vd_unit(), score=0.9)
    diag = dr.hit_diagnostics(
        hit, dr._norm("xyz"), dr._probes("xyz"), min_score=0.0, ambiguous=True
    )
    assert diag["lexical_hits"] == []
    assert diag["value_dict_hits"] == []
    assert diag["ambiguous"] is True
    assert diag["below_min_score"] is False


# --------------------------------------------------------------------------- #
# build_search_response
# --------------------------------------------------------------------------- #
def test_search_returns_location(store, embedder):
    res = build_search_response(store, embedder, MODEL, SearchRequest(query="芽球割合"))
    assert res.shown == len(res.hits)
    assert res.total >= res.shown
    locations = [h.location for h in res.hits]
    assert "DEMO_DB / DEMO.LAB / BLAST_PCT" in locations
    top = res.hits[0]
    assert top.unit_id.startswith("db:")
    assert "diagnostics" in top.model_dump()


def test_search_topk_limits_hits(store, embedder):
    res = build_search_response(store, embedder, MODEL, SearchRequest(query="日時", top_k=2))
    assert len(res.hits) <= 2
    assert res.shown == len(res.hits)


def test_search_kind_filter_excludes_all(store, embedder):
    # file ユニットは無い → total 0
    res = build_search_response(
        store, embedder, MODEL, SearchRequest(query="芽球割合", kind=Kind.FILE)
    )
    assert res.total == 0
    assert res.hits == []
    assert res.filters["kind"] == Kind.FILE


def test_search_invalid_kind_raises(store, embedder):
    with pytest.raises(InvalidKind):
        build_search_response(store, embedder, MODEL, SearchRequest(query="x", kind="bogus"))


def test_search_below_min_score_flag(store, embedder):
    res = build_search_response(
        store, embedder, MODEL, SearchRequest(query="芽球割合", min_score=0.99)
    )
    # 0.99 を超える候補はほぼ無いはず → 何らかの below フラグが立つ
    assert any(h.diagnostics["below_min_score"] for h in res.hits)


# --------------------------------------------------------------------------- #
# build_show_response
# --------------------------------------------------------------------------- #
def test_show_existing_unit(store):
    unit_index = {u.unit_id: u for u in store.units}
    uid = next(u.unit_id for u in store.units if u.kind == Kind.DB)
    data = build_show_response(unit_index, uid)
    assert data["unit_id"] == uid
    assert "search_text" in data
    assert "quality_flags" in data
    assert "payload" in data


def test_show_missing_unit_raises(store):
    unit_index = {u.unit_id: u for u in store.units}
    with pytest.raises(UnitNotFound):
        build_show_response(unit_index, "db:NOPE.NOPE.NOPE")


# --------------------------------------------------------------------------- #
# build_grep_response / build_why_response
# --------------------------------------------------------------------------- #
def test_grep_finds_unit(store):
    r = build_grep_response(store, GrepRequest(term="PATIENT_ID"))
    assert r["count"] >= 1
    assert any("db:DEMO.PATIENT.PATIENT_ID" == m["unit_id"] for m in r["matches"])


def test_grep_no_match(store):
    r = build_grep_response(store, GrepRequest(term="ZZZ_NOPE"))
    assert r["count"] == 0
    assert r["matches"] == []


def test_why_without_query_existence_only(store, embedder):
    r = build_why_response(store, embedder, MODEL, WhyRequest(term="PATIENT_ID"))
    assert r["exists"] is True
    assert r["ranked"] is False
    assert all("rank" not in m for m in r["matches"])


def test_why_with_query_gives_rank(store, embedder):
    r = build_why_response(
        store, embedder, MODEL, WhyRequest(term="PATIENT_ID", query="芽球割合", top_k=1)
    )
    assert r["exists"] is True
    assert r["ranked"] is True
    assert r["total_ranked"] >= 1
    assert any("rank" in m and m["rank"] is not None for m in r["matches"])


def test_why_not_found(store, embedder):
    r = build_why_response(store, embedder, MODEL, WhyRequest(term="ZZZ_NOPE"))
    assert r["exists"] is False
    assert r["count"] == 0


# --------------------------------------------------------------------------- #
# build_status_response
# --------------------------------------------------------------------------- #
def test_status_reports_index(store):
    s = build_status_response(store, MODEL)
    assert s["is_searchable"] is True
    assert s["model"] == MODEL
    assert s["unit_count"] == store.unit_count
    assert s["build_info"]["embedding_model"] == MODEL
    assert Kind.DB in s["kind_counts"]
    assert s["connection_counts"]  # connection_id 分布が出る


# --------------------------------------------------------------------------- #
# API 層（FastAPI TestClient）
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from kawasemi_indexer.webserver import create_app

    out = tmp_path / "resource_index"
    build_db_index(source=FIXTURE_DIR, out_dir=out, embedder=HashingEmbedder())
    reset_store_cache()  # tmp_path 索引を確実にロードさせる
    get_store(str(out))  # キャッシュへ載せる
    app = create_app(index_dir=str(out), model=MODEL, embedder=HashingEmbedder())
    with TestClient(app) as c:
        yield c
    reset_store_cache()


def test_api_status(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    body = r.json()
    assert body["is_searchable"] is True
    assert body["model"] == MODEL


def test_api_search_ok(client):
    r = client.post("/api/search", json={"query": "芽球割合", "top_k": 8})
    assert r.status_code == 200
    body = r.json()
    assert body["query"] == "芽球割合"
    assert "DEMO_DB / DEMO.LAB / BLAST_PCT" in [h["location"] for h in body["hits"]]


def test_api_search_bad_kind_422(client):
    r = client.post("/api/search", json={"query": "x", "kind": "bogus"})
    assert r.status_code == 422


def test_api_show_roundtrip(client):
    found = client.post("/api/search", json={"query": "患者ID", "top_k": 3}).json()
    uid = found["hits"][0]["unit_id"]
    r = client.get("/api/show/" + uid)
    assert r.status_code == 200
    assert r.json()["unit_id"] == uid


def test_api_show_404(client):
    r = client.get("/api/show/db:NOPE.NOPE.NOPE")
    assert r.status_code == 404


def test_api_index_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "索引診断" in r.text
