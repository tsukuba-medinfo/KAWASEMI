"""検索評価（recall@k / MRR）の契約テスト。

実 Ollama 非依存。HashingEmbedder（語彙重複 cosine）で駆動し、モード別の指標算出と、
コード直打ちでハイブリッドが純 embedding を上回ることを検証する。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_resource_index import HashingEmbedder

from kawasemi_indexer.builder import write_index
from kawasemi_indexer.eval import (
    GoldItem,
    MetricBlock,
    _aggregate,
    evaluate,
    format_report,
    load_gold,
)
from kawasemi_indexer.schema import Kind, ResourceUnit
from kawasemi_indexer.store import ResourceIndexStore
from kawasemi_indexer.synonyms import QueryRewriter, build_synonym_index


def test_aggregate_recall_and_mrr():
    # 順位: 0位, 4位, 8位, None(取りこぼし)
    blk = _aggregate([0, 4, 8, None])
    assert blk.n == 4
    assert blk.recall_at_5 == pytest.approx(2 / 4)  # <5 は 0位と4位
    assert blk.recall_at_10 == pytest.approx(3 / 4)  # <10 は 0,4,8位
    # MRR = (1/1 + 1/5 + 1/9 + 0)/4
    assert blk.mrr == pytest.approx((1.0 + 1 / 5 + 1 / 9) / 4, abs=1e-4)


def test_aggregate_empty():
    assert _aggregate([]) == MetricBlock()


def test_gold_item_multi_answer_and_legacy():
    # 旧形式 unit_id（単数）→ unit_ids へ吸収
    legacy = GoldItem(query="q", unit_id="u1")
    assert legacy.unit_ids == ["u1"]
    # 新形式 unit_ids（複数正解）
    multi = GoldItem(query="q", unit_ids=["u1", "u2"])
    assert multi.unit_ids == ["u1", "u2"]


def test_evaluate_multi_answer_uses_best_rank(eval_index):
    """複数正解はいずれかヒットで正解・MRR は最良順位。"""
    store = ResourceIndexStore.load(eval_index)
    # unit3 は「解熱鎮痛剤」クエリで最上位、unit1 は無関係（下位 or 圏外）。
    gold = [GoldItem(query="解熱鎮痛剤", unit_ids=["master:T:D.T:1", "master:T:D.T:3"])]
    rep = evaluate(store, HashingEmbedder(), gold, mode="hybrid")
    assert rep.overall.recall_at_5 == 1.0
    assert rep.overall.mrr == 1.0  # 最良順位（unit3 が 0 位）で算出


def test_load_gold_skips_blank_and_comment(tmp_path):
    p = tmp_path / "gold.jsonl"
    p.write_text(
        '# comment\n{"query":"q","unit_id":"u","tags":["t"]}\n\n',
        encoding="utf-8",
    )
    gold = load_gold(p)
    assert len(gold) == 1 and gold[0].query == "q" and gold[0].tags == ["t"]


def _master(unit_id: str, label: str, aliases: list[str], code: str, text: str) -> ResourceUnit:
    return ResourceUnit(
        unit_id=unit_id,
        kind=Kind.MASTER,
        label=label,
        search_text=text,
        payload={"connection_id": "T", "db_qualified": "D.T", "code": code, "aliases": aliases},
    )


@pytest.fixture
def eval_index(tmp_path) -> Path:
    units = [
        _master(
            "master:T:D.T:1",
            "架空薬甲錠",
            ["架空成分甲塩酸塩", "9999999000001"],
            "10001",
            "架空薬甲錠 白血病 分子標的薬",
        ),
        _master(
            "master:T:D.T:2",
            "妨害候補",
            ["架空成分甲 一般名 分子標的 白血病"],
            "999",
            "架空成分甲 一般名 分子標的 白血病 チロシンキナーゼ 類似",
        ),
        _master("master:T:D.T:3", "解熱鎮痛剤", ["架空薬丙"], "111", "解熱鎮痛剤 感冒 頭痛"),
    ]
    out = tmp_path / "resource_index"
    write_index({Kind.MASTER: units}, HashingEmbedder(), out)
    build_synonym_index(out)
    return out


def test_evaluate_hybrid_improves_mrr_over_embedding(eval_index):
    store = ResourceIndexStore.load(eval_index)
    embedder = HashingEmbedder()
    gold = [
        # 一般名クエリ: 語彙が妨害候補(unit2)と重なり、embedding は妨害を上位に置く。
        # ハイブリッドは alias 完全一致で正解(unit1)を最上位へ。
        GoldItem(query="架空成分甲塩酸塩", unit_id="master:T:D.T:1", tags=["name_alias"]),
        GoldItem(query="解熱鎮痛剤", unit_id="master:T:D.T:3", tags=["name_alias"]),
    ]
    emb = evaluate(store, embedder, gold, mode="embedding")
    hyb = evaluate(store, embedder, gold, mode="hybrid")

    assert emb.mode == "embedding" and emb.overall.n == 2
    # 順位に敏感な MRR でハイブリッドが純 embedding を上回る。
    assert hyb.overall.mrr > emb.overall.mrr
    assert hyb.per_tag["name_alias"].mrr == 1.0  # 全件を最上位で引く
    assert emb.per_tag["name_alias"].mrr < 1.0  # 妨害候補に順位を奪われる


def test_evaluate_rewrite_mode_uses_synonyms(eval_index):
    store = ResourceIndexStore.load(eval_index)
    rewriter = QueryRewriter.load(eval_index)
    # 一般名クエリ → 変換テーブルで製品名・コードへ展開して引く
    gold = [GoldItem(query="架空成分甲塩酸塩", unit_id="master:T:D.T:1", tags=["name_alias"])]
    rep = evaluate(store, HashingEmbedder(), gold, mode="rewrite", rewriter=rewriter)
    assert rep.overall.recall_at_10 == 1.0


def test_format_report_smoke(eval_index):
    store = ResourceIndexStore.load(eval_index)
    gold = [GoldItem(query="9999999000001", unit_id="master:T:D.T:1", tags=["code_direct"])]
    reports = [evaluate(store, HashingEmbedder(), gold, mode=m) for m in ("embedding", "hybrid")]
    lines = format_report(reports)
    text = "\n".join(lines)
    assert "recall@5" in text and "embedding" in text and "hybrid" in text
    assert "code_direct" in text


def test_goldset_generator(eval_index):
    """生成スクリプトが索引からゴールドセットを組めること（決定的・embedding不要）。"""
    from scripts.make_eval_goldset import build_goldset

    items = build_goldset(eval_index)
    assert items
    # 各 item は query/unit_id/tags を持ち、正解 unit_id は索引に存在する。
    store = ResourceIndexStore.load(eval_index)
    valid_ids = {u.unit_id for u in store.units}
    for it in items:
        assert it["query"] and it["unit_id"] in valid_ids and it["tags"]
    # コード直打ち観点が拾えている（HOT コードエイリアス）。
    assert any("code_direct" in it["tags"] for it in items)
