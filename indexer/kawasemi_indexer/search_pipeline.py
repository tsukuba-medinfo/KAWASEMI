"""検索パイプライン（クエリ書き換え → 参照リンクトリガ → ハイブリッド検索）。

CLI search / eval / debug REPL / WebUI が同じ 3 施策（ハイブリッド、書き換え、
参照リンクチャネル）を同じ手順で通すための単一ソース。従来は cli / eval に同型の
3 段組みが重複し、debug/WebUI は純 embedding のまま未接続だった（配線問題）。

参照リンクチャネルの発動トリガ:
1. QueryRewriter で標準コード解決できたら、その展開元マスタエントリの表を優先。
2. できなければ、ハイブリッド検索の上位 kind=master ヒット（N=5 エントリ）の表。
特定した表を参照する列（RefLinkIndex）を RRF 第3チャネルとして store へ渡す。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kawasemi_indexer.schema import Kind, ResourceHit

if TYPE_CHECKING:
    import numpy as np

    from kawasemi_indexer.embedder import Embedder
    from kawasemi_indexer.ref_links import RefLinkIndex
    from kawasemi_indexer.store import ResourceIndexStore
    from kawasemi_indexer.synonyms import QueryRewriter, RewriteResult

# 検索モード（ベースライン → 施策の積み上げ順）。
MODES = ("embedding", "hybrid", "rewrite", "ref")

# ref トリガのフォールバックで kind=master を探す深さと、採用するマスタエントリ数。
_MASTER_SCAN_K = 50
_MASTER_TOP_N = 5


@dataclass
class SearchOutcome:
    """1 クエリの検索結果と、診断・再現に必要な中間情報。"""

    hits: list[ResourceHit]
    qv: np.ndarray  # クエリベクトル（debug の :why 再現用）
    embed_text: str  # embedding に使ったテキスト（書き換えで拡張され得る）
    lexical_terms: list[str] | None  # 字面チャネルに渡した語群（None=字面チャネル無効）
    ref_candidates: list[tuple[str, str]]  # 参照リンク候補 (unit_id, status)。順位付き
    rewrite: RewriteResult | None = None
    ref_masters: list[dict[str, Any]] = field(default_factory=list)  # 特定したマスタ表と由来
    # マスタ hit ごとの FK 逆引き（master unit_id → それを参照する患者表FK列 [(unit_id, role, status)]）。
    # 「マスタ表の場所＋code まで」に加え、どの列で WHERE を組めるかを注釈する。
    ref_columns_by_master: dict[str, list[tuple[str, str, str]]] = field(default_factory=dict)


def parse_master_unit_id(unit_id: str) -> tuple[str, str] | None:
    """master ユニット ID から (connection_id, db_qualified) を取り出す。

    unit_id は builder の規約 'master:{conn}:{dbq}:{code}'（builder.build_master_units）。
    """
    parts = unit_id.split(":", 3)
    if len(parts) == 4 and parts[0] == Kind.MASTER:
        return parts[1], parts[2]
    return None


def resolve_ref_masters(
    store: ResourceIndexStore,
    qv: np.ndarray,
    query: str,
    rewrite: RewriteResult | None,
    top_n: int = _MASTER_TOP_N,
) -> list[dict[str, Any]]:
    """参照リンクチャネルの対象マスタ表を特定する（二段展開の 1 段目）。"""
    masters: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    # 1) 標準コード解決（QueryRewriter が当てた展開元エントリ＝クエリの臨床概念）。
    if rewrite is not None and rewrite.matched:
        for p in rewrite.provenance:
            key = parse_master_unit_id(str(p.get("unit_id", "")))
            if key and key not in seen:
                seen.add(key)
                masters.append({"connection_id": key[0], "db_qualified": key[1], "via": "rewrite"})
        if masters:
            return masters

    # 2) フォールバック: ハイブリッド上位の kind=master エントリ（表で重複排除）。
    hits = store.search(qv, top_k=_MASTER_SCAN_K, min_score=-1.0, lexical_terms=[query])
    n_entries = 0
    for h in hits:
        if h.unit.kind != Kind.MASTER:
            continue
        n_entries += 1
        key = parse_master_unit_id(h.unit.unit_id)
        if key and key not in seen:
            seen.add(key)
            masters.append({"connection_id": key[0], "db_qualified": key[1], "via": "master_hit"})
        if n_entries >= top_n:
            break
    return masters


def run_search(
    store: ResourceIndexStore,
    embedder: Embedder,
    query: str,
    *,
    mode: str = "ref",
    top_k: int = 8,
    min_score: float = 0.0,
    rewriter: QueryRewriter | None = None,
    ref_links: RefLinkIndex | None = None,
) -> SearchOutcome:
    """1 クエリを指定モードで検索する（全消費者共通の入口）。

    mode:
    - embedding: 純 embedding cosine（ベースライン）
    - hybrid:    字面チャネル＋RRF
    - rewrite:   変換テーブルでクエリ展開してから hybrid
    - ref:       rewrite に加え参照リンクチャネル（RRF 第3チャネル）を発動
    rewriter / ref_links が None（または空）のモードは、その施策抜きで縮退する。
    """
    if mode not in MODES:
        raise ValueError(f"mode は {MODES} のいずれか: {mode!r}")

    embed_text = query
    lexical_terms: list[str] | None = None
    rewrite: RewriteResult | None = None

    if mode in ("hybrid", "rewrite", "ref"):
        lexical_terms = [query]
    if mode in ("rewrite", "ref") and rewriter is not None:
        rewrite = rewriter.rewrite(query)
        if rewrite.matched:
            embed_text = rewrite.expanded_text
            lexical_terms = rewrite.all_terms

    qv = embedder.embed([embed_text])[0]

    ref_candidates: list[tuple[str, str]] = []
    ref_masters: list[dict[str, Any]] = []
    if mode == "ref" and ref_links is not None and len(ref_links):
        ref_masters = resolve_ref_masters(store, qv, query, rewrite)
        if ref_masters:
            ref_candidates = ref_links.candidates_for(
                [(m["connection_id"], m["db_qualified"]) for m in ref_masters]
            )

    hits = store.search(
        qv,
        top_k=top_k,
        min_score=min_score,
        lexical_terms=lexical_terms,
        ref_candidates=ref_candidates or None,
    )

    # FK 逆引き注釈: 結果に含まれる master hit ごとに、それを参照する患者表の
    # FK 列一覧を付ける。「マスタ表の場所＋code」に加え、どの列で WHERE を組めるかを示す。
    ref_columns_by_master: dict[str, list[tuple[str, str, str]]] = {}
    if ref_links is not None and len(ref_links):
        for h in hits:
            if h.unit.kind != Kind.MASTER:
                continue
            key = parse_master_unit_id(h.unit.unit_id)
            if key is None:
                continue
            cols = ref_links.referencing_columns(key[0], key[1])
            if cols:
                ref_columns_by_master[h.unit.unit_id] = cols

    return SearchOutcome(
        hits=hits,
        qv=qv,
        embed_text=embed_text,
        lexical_terms=lexical_terms,
        ref_candidates=ref_candidates,
        rewrite=rewrite,
        ref_masters=ref_masters,
        ref_columns_by_master=ref_columns_by_master,
    )
