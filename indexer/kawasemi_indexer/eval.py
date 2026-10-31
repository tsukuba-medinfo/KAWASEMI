"""検索評価（ゴールドセットで recall@k / MRR を測る）。

施策（ハイブリッド検索、変換テーブル）の効果を主観でなく数値で比較するための
物差し。ゴールドセット（クエリ → 正解 unit_id）に対し、検索モード別に順位を求めて
recall@5 / recall@10 / MRR を算出する。デバッガの `⚠ambiguous` は個別診断で集計指標
ではないため、本モジュールが集計を担う。

検索モード（search_pipeline.MODES と共通）:
- embedding: 純 embedding cosine（施策前ベースライン）
- hybrid:    字面チャネル＋RRF
- rewrite:   変換テーブルでクエリ展開してから hybrid
- ref:       rewrite ＋ 参照リンクチャネル（RRF 第3チャネル）

embedder は Protocol（embed(texts)->ndarray）で受けるため、実 Ollama でもテスト用の
決定的スタブでも駆動できる（store 同様）。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, model_validator

from kawasemi_indexer.search_pipeline import MODES, run_search

if TYPE_CHECKING:
    from kawasemi_indexer.embedder import Embedder
    from kawasemi_indexer.ref_links import RefLinkIndex
    from kawasemi_indexer.store import ResourceIndexStore
    from kawasemi_indexer.synonyms import QueryRewriter

__all__ = [
    "MODES",
    "GoldItem",
    "MetricBlock",
    "EvalReport",
    "load_gold",
    "evaluate",
    "format_report",
]

# recall を測る順位カットと、順位検出（MRR 用）に取得する候補数。
_RECALL_KS = (5, 10)
_EVAL_TOP_K = 20


class GoldItem(BaseModel):
    """評価の 1 件（クエリ → 正解 unit_ids）。tags で観点別集計を可能にする。

    正解は複数許容（unit_ids）。「格納先カラム」型クエリは正解カラムが複数あり得るため。
    recall@k は **いずれか** の正解がヒットすれば正解、MRR は **最良順位** で算出する。
    旧形式（unit_id 単数）の jsonl もそのまま読める（後方互換）。
    """

    query: str
    unit_ids: list[str] = Field(min_length=1)
    tags: list[str] = Field(default_factory=list)
    note: str | None = None

    @model_validator(mode="before")
    @classmethod
    def _accept_legacy_unit_id(cls, data: Any) -> Any:
        """旧形式 `unit_id: str` を `unit_ids: [str]` へ吸収する。"""
        if isinstance(data, dict) and "unit_ids" not in data and data.get("unit_id"):
            data = dict(data)
            data["unit_ids"] = [data.pop("unit_id")]
        return data


class MetricBlock(BaseModel):
    """ある母集団（全体 or タグ別）の集計指標。"""

    n: int = 0
    recall_at_5: float = 0.0
    recall_at_10: float = 0.0
    mrr: float = 0.0


class EvalReport(BaseModel):
    """1 モードの評価結果（全体 + タグ別 + 取りこぼし）。"""

    mode: str
    overall: MetricBlock
    per_tag: dict[str, MetricBlock] = Field(default_factory=dict)
    misses: list[str] = Field(default_factory=list, description="top_k 外だったクエリ")


def load_gold(path: str | Path) -> list[GoldItem]:
    """ゴールドセット jsonl を読み込む（1行1件）。"""
    items: list[GoldItem] = []
    with Path(path).open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line and not line.startswith("#"):
                items.append(GoldItem(**json.loads(line)))
    return items


def _rank_of(
    store: ResourceIndexStore,
    embedder: Embedder,
    item: GoldItem,
    mode: str,
    top_k: int,
    rewriter: QueryRewriter | None,
    ref_links: RefLinkIndex | None = None,
) -> int | None:
    """正解 unit_ids のいずれかが候補の何位に出るか（0始まり・最良順位）。top_k 外は None。

    検索は search_pipeline.run_search（CLI/debug/WebUI と同一経路）で行う。
    """
    outcome = run_search(
        store,
        embedder,
        item.query,
        mode=mode,
        top_k=top_k,
        min_score=-1.0,
        rewriter=rewriter,
        ref_links=ref_links,
    )
    gold_ids = set(item.unit_ids)
    for i, h in enumerate(outcome.hits):
        if h.unit.unit_id in gold_ids:
            return i
    return None


def _aggregate(ranks: list[int | None]) -> MetricBlock:
    """順位リスト（None=取りこぼし）から recall@k / MRR を算出する。"""
    n = len(ranks)
    if n == 0:
        return MetricBlock()
    found = [r for r in ranks if r is not None]
    rec5 = sum(1 for r in found if r < 5) / n
    rec10 = sum(1 for r in found if r < 10) / n
    mrr = sum(1.0 / (r + 1) for r in found) / n
    return MetricBlock(
        n=n,
        recall_at_5=round(rec5, 4),
        recall_at_10=round(rec10, 4),
        mrr=round(mrr, 4),
    )


def evaluate(
    store: ResourceIndexStore,
    embedder: Embedder,
    gold: list[GoldItem],
    mode: str,
    top_k: int = _EVAL_TOP_K,
    rewriter: QueryRewriter | None = None,
    ref_links: RefLinkIndex | None = None,
) -> EvalReport:
    """ゴールドセットを 1 モードで評価する。"""
    if mode not in MODES:
        raise ValueError(f"mode は {MODES} のいずれか: {mode!r}")

    ranks: list[int | None] = []
    per_tag_ranks: dict[str, list[int | None]] = {}
    misses: list[str] = []
    for item in gold:
        rank = _rank_of(store, embedder, item, mode, top_k, rewriter, ref_links)
        ranks.append(rank)
        if rank is None:
            misses.append(item.query)
        for tag in item.tags:
            per_tag_ranks.setdefault(tag, []).append(rank)

    return EvalReport(
        mode=mode,
        overall=_aggregate(ranks),
        per_tag={tag: _aggregate(rs) for tag, rs in sorted(per_tag_ranks.items())},
        misses=misses,
    )


def format_report(reports: list[EvalReport]) -> list[str]:
    """複数モードの評価結果を比較テーブルとして整形する（recall@k は _RECALL_KS 準拠）。"""
    out: list[str] = []
    if not reports:
        return ["（評価結果なし）"]
    n = reports[0].overall.n
    out.append(
        f"評価件数: {n}（recall@{'/'.join(map(str, _RECALL_KS))} / MRR、top_k={_EVAL_TOP_K}）"
    )
    out.append("-" * 56)
    out.append(f"{'mode':<12} {'recall@5':>9} {'recall@10':>10} {'MRR':>8}")
    for r in reports:
        o = r.overall
        out.append(f"{r.mode:<12} {o.recall_at_5:>9.3f} {o.recall_at_10:>10.3f} {o.mrr:>8.3f}")

    # タグ別（全モード横断で観点ごとの改善を見る）。
    tags = sorted({t for r in reports for t in r.per_tag})
    if tags:
        out.append("")
        out.append("タグ別 recall@10:")
        header = f"  {'tag':<18}" + "".join(f"{r.mode:>10}" for r in reports)
        out.append(header)
        for tag in tags:
            row = f"  {tag:<18}"
            for r in reports:
                blk = r.per_tag.get(tag)
                row += f"{(blk.recall_at_10 if blk else 0.0):>10.3f}"
            n_tag = next((r.per_tag[tag].n for r in reports if tag in r.per_tag), 0)
            out.append(row + f"  (n={n_tag})")
    return out
