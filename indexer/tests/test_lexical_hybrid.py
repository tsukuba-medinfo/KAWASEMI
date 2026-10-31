"""字面一致チャネル（lexical）とハイブリッド検索（RRF 融合）の契約テスト。

実 Ollama 非依存。embedding は test_resource_index の HashingEmbedder（語彙重複 cosine）を
流用する。字面チャネルが「完全一致を最上位に押し上げる」ことと match_type 付与を検証する。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_resource_index import HashingEmbedder

from kawasemi_indexer import lexical
from kawasemi_indexer.builder import write_index
from kawasemi_indexer.schema import Kind, ResourceUnit
from kawasemi_indexer.store import ResourceIndexStore


# --------------------------------------------------------------------------- #
# lexical: 正規化と段階別スコア
# --------------------------------------------------------------------------- #
def test_normalize_folds_width_case_and_space():
    assert lexical.normalize("ＡＢＣ　１２３") == "abc 123"
    assert lexical.normalize("  Foo   Bar ") == "foo bar"
    assert lexical.normalize(None) == ""


def test_match_tiers():
    forms = ["架空薬乙錠 20mg", "架空成分乙フマル酸塩", "999999999"]
    # 完全一致（全角/空白差は正規化で吸収）
    assert lexical.match(lexical.normalize("架空成分乙フマル酸塩"), forms) == (1.0, "exact")
    assert lexical.match(lexical.normalize("架空薬乙錠20mg"), forms)[1] == "exact"
    # 前方一致
    score, kind = lexical.match(lexical.normalize("架空薬乙"), forms)
    assert kind == "prefix" and 0 < score < 1.0
    # 部分一致
    assert lexical.match(lexical.normalize("フマル酸"), forms)[1] == "substring"
    # 一致なし
    assert lexical.match(lexical.normalize("存在しない語"), forms) == (0.0, None)


def test_surface_forms_includes_label_aliases_code():
    u = ResourceUnit(
        unit_id="master:X:T:1",
        kind=Kind.MASTER,
        label="架空薬甲錠",
        search_text="...",
        payload={"code": "999999999", "aliases": ["架空成分甲塩酸塩", "9999999X9999"]},
    )
    forms = lexical.surface_forms(u)
    assert "架空薬甲錠" in forms
    assert "999999999" in forms
    assert "架空成分甲塩酸塩" in forms


def test_classify_match_type():
    assert lexical.classify_match_type(True, True) == "both"
    assert lexical.classify_match_type(True, False) == "lexical"
    assert lexical.classify_match_type(False, True) == "semantic"


# --------------------------------------------------------------------------- #
# store: ハイブリッド検索
# --------------------------------------------------------------------------- #
def _master(unit_id: str, label: str, aliases: list[str], code: str, text: str) -> ResourceUnit:
    return ResourceUnit(
        unit_id=unit_id,
        kind=Kind.MASTER,
        label=label,
        search_text=text,
        payload={"connection_id": "T", "db_qualified": "D.T", "code": code, "aliases": aliases},
    )


@pytest.fixture
def hybrid_index(tmp_path) -> Path:
    """字面完全一致の対象と、意味的にだけ近い妨害候補を含む小索引。"""
    units = [
        # 完全一致させたい対象。search_text は一般名を含まず、cosine 単独では埋もれる。
        _master(
            "master:T:D.T:1",
            "架空薬甲錠",
            ["架空成分甲塩酸塩"],
            "999999999",
            "架空薬甲錠 白血病 分子標的薬 チロシンキナーゼ阻害",
        ),
        # 妨害: 語彙が「一般名」クエリと重なり、cosine では上位に来やすい。
        _master(
            "master:T:D.T:2",
            "妨害候補",
            ["架空成分甲 類似 一般名 医薬品 分子標的"],
            "999",
            "架空成分甲 一般名 医薬品 分子標的 白血病 チロシンキナーゼ 阻害 類似",
        ),
        _master("master:T:D.T:3", "無関係", ["解熱鎮痛剤"], "111", "解熱鎮痛剤 感冒 頭痛"),
    ]
    out = tmp_path / "resource_index"
    write_index({Kind.MASTER: units}, HashingEmbedder(), out)
    return out


def test_hybrid_lifts_exact_alias_match_to_top(hybrid_index):
    store = ResourceIndexStore.load(hybrid_index)
    embedder = HashingEmbedder()
    q = "架空成分甲塩酸塩"
    qv = embedder.embed([q])[0]

    # 純 embedding では完全一致エントリが先頭とは限らない（意味的妨害に負けうる）。
    semantic = store.search(qv, top_k=3)
    # ハイブリッド（query_text 指定）では完全一致（alias exact）が最上位に来る。
    hybrid = store.search(qv, top_k=3, query_text=q)
    assert hybrid[0].unit.unit_id == "master:T:D.T:1"
    assert hybrid[0].lexical_score == 1.0
    assert hybrid[0].match_type in ("both", "lexical")
    assert hybrid[0].rrf_score is not None
    # 妨害候補が純 embedding で先頭を取れていたことを確認（ハイブリッドの効果の裏取り）。
    assert semantic[0].unit.unit_id == "master:T:D.T:2"


def test_semantic_only_when_no_query_text(hybrid_index):
    """query_text を渡さなければ従来の cosine 検索（match_type 等は None）。"""
    store = ResourceIndexStore.load(hybrid_index)
    qv = HashingEmbedder().embed(["架空成分甲塩酸塩"])[0]
    hits = store.search(qv, top_k=3)
    assert all(h.match_type is None and h.rrf_score is None for h in hits)


def test_hybrid_code_direct_hit(hybrid_index):
    """コード直打ちが該当ユニットを最上位に引く。"""
    store = ResourceIndexStore.load(hybrid_index)
    qv = HashingEmbedder().embed(["999999999"])[0]
    hits = store.search(qv, top_k=3, query_text="999999999")
    assert hits[0].unit.unit_id == "master:T:D.T:1"


def test_min_score_exempts_exact_match(hybrid_index):
    """高い min_score でも完全一致（字面 exact）は cosine 足切りで落ちない。"""
    store = ResourceIndexStore.load(hybrid_index)
    q = "架空成分甲塩酸塩"
    qv = HashingEmbedder().embed([q])[0]
    # 完全一致対象の cosine が min_score 未満でも残る（RRF で上げた意図を守る）。
    hits = store.search(qv, top_k=3, query_text=q, min_score=0.99)
    ids = [h.unit.unit_id for h in hits]
    assert "master:T:D.T:1" in ids
    # 完全一致でない候補は高 min_score で落ちる（免除は完全一致・参照リンクのみ）。
    assert "master:T:D.T:2" not in ids


def test_min_score_exempts_ref_linked(hybrid_index):
    """参照リンクで押し上げた列は cosine が低くても min_score 足切りを免除される。"""
    store = ResourceIndexStore.load(hybrid_index)
    qv = HashingEmbedder().embed(["架空成分甲塩酸塩"])[0]
    # 意味的に無関係な unit 3 を参照リンク候補として渡す。
    hits = store.search(
        qv,
        top_k=3,
        query_text="架空成分甲塩酸塩",
        ref_candidates=[("master:T:D.T:3", "confirmed")],
        min_score=0.99,
    )
    ref_hit = next((h for h in hits if h.unit.unit_id == "master:T:D.T:3"), None)
    assert ref_hit is not None
    assert ref_hit.ref_status == "confirmed"
    assert hits[0].lexical_score == 1.0
