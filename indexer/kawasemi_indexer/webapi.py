"""索引診断 Web API の純粋ロジック層（FastAPI 非依存）

CLI デバッガ（debug_repl）の診断ロジックを再利用し、整形済みテキストではなく
構造化データ（pydantic モデル）を返す。FastAPI から切り離すことで Ollama 非依存の
単体テストを可能にする（webserver.py が薄いルーティング層としてこれを呼ぶ）。

設計（docs/kawasemi_indexer_debug_spec.md と整合）:
- 診断系統は2つ: embedding 検索（_ranked）＋直接内省（_match_units）。
- ステートレス: フィルタ/top_k/min_score は Session に溜めず、リクエスト毎に
  短命 Session を組んで debug_repl のヘルパへ渡す（store/embedder は共有インスタンス）。
- 足切り線は除外せず below_min_score フラグで表現（CLI の「--- min_score ---」を UI のバッジで再現）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

from kawasemi_indexer.debug_repl import (
    AMBIGUOUS_DELTA,
    Session,
    _location,
    _match_units,
    _norm,
    _probes,
    _quality_flags,
    execute_query,
    hit_diagnostics,
)
from kawasemi_indexer.schema import Kind, ResourceUnit
from kawasemi_indexer.search_pipeline import MODES

if TYPE_CHECKING:
    from kawasemi_indexer.embedder import Embedder
    from kawasemi_indexer.ref_links import RefLinkIndex
    from kawasemi_indexer.store import ResourceIndexStore
    from kawasemi_indexer.synonyms import QueryRewriter


# --------------------------------------------------------------------------- #
# I/O 契約（pydantic）
# --------------------------------------------------------------------------- #
class SearchRequest(BaseModel):
    query: str
    top_k: int = Field(default=8, ge=1)
    min_score: float = 0.0
    kind: str | None = None
    connection_id: str | None = None
    mode: str = Field(default="ref", description="embedding|hybrid|rewrite|ref（既定は全施策）")


class WhyRequest(BaseModel):
    term: str
    query: str | None = None  # 直近クエリ。あれば順位を算出（無ければ存在確認のみ）
    top_k: int = Field(default=8, ge=1)
    kind: str | None = None
    connection_id: str | None = None
    mode: str = Field(default="ref", description="順位算出に使う検索モード")


class GrepRequest(BaseModel):
    term: str
    kind: str | None = None
    connection_id: str | None = None


class HitOut(BaseModel):
    cand_id: int
    unit_id: str
    kind: str
    label: str
    location: str
    score: float
    payload: dict[str, Any]
    diagnostics: dict[
        str, Any
    ]  # value_dict_hits / lexical_hits / quality_flags / ambiguous / below_min_score


class SearchResponse(BaseModel):
    query: str
    total: int  # フィルタ後の全件数
    shown: int  # 表示件数（top_k 切り出し後）
    min_score: float
    mode: str
    filters: dict[str, Any]
    hits: list[HitOut]
    # パイプラインの中間情報（UI がクエリ展開・参照リンク発動を可視化するため）。
    rewrite: dict[str, Any] | None = None  # {expansions, provenance}（不発は None）
    ref_masters: list[dict[str, Any]] = Field(default_factory=list)
    ref_candidate_count: int = 0


# --------------------------------------------------------------------------- #
# 例外（webserver 側で HTTP ステータスへ変換）
# --------------------------------------------------------------------------- #
class InvalidKind(ValueError):
    """kind フィルタが Kind.ORDER にない（→ 422）。"""


class UnitNotFound(KeyError):
    """unit_id が索引に存在しない（→ 404）。"""


def _validate_kind(kind: str | None) -> str | None:
    if kind is None or kind == "" or kind == "all":
        return None
    if kind not in Kind.ORDER:
        raise InvalidKind(f"kind は {', '.join(Kind.ORDER)} / all のいずれか: {kind!r}")
    return kind


class InvalidMode(ValueError):
    """mode が MODES にない（→ 422）。"""


def _validate_mode(mode: str | None) -> str:
    if mode is None or mode == "":
        return "ref"
    if mode not in MODES:
        raise InvalidMode(f"mode は {', '.join(MODES)} のいずれか: {mode!r}")
    return mode


def _make_session(
    store: ResourceIndexStore,
    embedder: Embedder,
    model: str,
    top_k: int,
    min_score: float,
    kind: str | None,
    conn: str | None,
    mode: str = "ref",
    rewriter: QueryRewriter | None = None,
    ref_links: RefLinkIndex | None = None,
) -> Session:
    """リクエスト毎の短命 Session（共有 store/embedder/rewriter/ref_links を差し、フィルタだけ載せる）。"""
    return Session(
        store=store,
        embedder=embedder,
        model=model,
        top_k=top_k,
        min_score=min_score,
        kind_filter=_validate_kind(kind),
        conn_filter=(conn or None) if conn != "all" else None,
        mode=_validate_mode(mode),
        rewriter=rewriter,
        ref_links=ref_links,
    )


def _filters_dict(session: Session) -> dict[str, Any]:
    return {"kind": session.kind_filter, "connection_id": session.conn_filter}


# --------------------------------------------------------------------------- #
# ドメイン関数
# --------------------------------------------------------------------------- #
def build_search_response(
    store: ResourceIndexStore,
    embedder: Embedder,
    model: str,
    req: SearchRequest,
    rewriter: QueryRewriter | None = None,
    ref_links: RefLinkIndex | None = None,
) -> SearchResponse:
    """臨床的コンテキストを検索パイプライン（req.mode）で検索し、診断付き候補を構造化して返す。"""
    session = _make_session(
        store,
        embedder,
        model,
        req.top_k,
        req.min_score,
        req.kind,
        req.connection_id,
        req.mode,
        rewriter,
        ref_links,
    )
    # 足切り線を「見える」ようにするため全件並べ（execute_query 内 min_score=-1.0）、
    # below_min_score フラグで表現する。
    ranked, outcome = execute_query(session, req.query)
    displayed = ranked[: req.top_k]

    query_norm = _norm(req.query)
    probes = _probes(req.query)
    hits: list[HitOut] = []
    prev_score: float | None = None
    for h in displayed:
        ambiguous = prev_score is not None and abs(prev_score - h.score) < AMBIGUOUS_DELTA
        diag = hit_diagnostics(h, query_norm, probes, req.min_score, ambiguous)
        u = h.unit
        hits.append(
            HitOut(
                cand_id=h.cand_id,
                unit_id=u.unit_id,
                kind=u.kind,
                label=u.label,
                location=diag["location"],
                score=h.score,
                payload=u.payload,
                diagnostics=diag,
            )
        )
        prev_score = h.score

    rewrite_out = None
    if outcome.rewrite is not None and outcome.rewrite.matched:
        rewrite_out = {
            "expansions": outcome.rewrite.expansions,
            "provenance": outcome.rewrite.provenance,
        }
    return SearchResponse(
        query=req.query,
        total=len(ranked),
        shown=len(displayed),
        min_score=req.min_score,
        mode=session.mode,
        filters=_filters_dict(session),
        hits=hits,
        rewrite=rewrite_out,
        ref_masters=outcome.ref_masters,
        ref_candidate_count=len(outcome.ref_candidates),
    )


def build_show_response(unit_index: dict[str, ResourceUnit], unit_id: str) -> dict[str, Any]:
    """単一ユニットの完全 payload + search_text + snippet + 品質フラグ（:show 相当）。"""
    unit = unit_index.get(unit_id)
    if unit is None:
        raise UnitNotFound(unit_id)
    data = unit.model_dump()
    data["quality_flags"] = _quality_flags(unit)
    return data


def build_grep_response(store: ResourceIndexStore, req: GrepRequest) -> dict[str, Any]:
    """physical/logical/unit_id/label/code への部分一致スキャン（直接内省・:grep 相当）。"""
    session = _make_session(store, None, "", 8, 0.0, req.kind, req.connection_id)  # type: ignore[arg-type]
    matches = _match_units(session, req.term)
    return {
        "term": req.term,
        "filters": _filters_dict(session),
        "count": len(matches),
        "matches": [
            {
                "unit_id": u.unit_id,
                "kind": u.kind,
                "label": u.label,
                "logical_name": u.payload.get("logical_name"),
                "location": _location(u),
            }
            for u in matches
        ],
    }


def build_why_response(
    store: ResourceIndexStore,
    embedder: Embedder,
    model: str,
    req: WhyRequest,
    rewriter: QueryRewriter | None = None,
    ref_links: RefLinkIndex | None = None,
) -> dict[str, Any]:
    """対象の索引内存在確認＋（query があれば）直近クエリでの順位（top-k 外も）。"""
    session = _make_session(
        store,
        embedder,
        model,
        req.top_k,
        0.0,
        req.kind,
        req.connection_id,
        req.mode,
        rewriter,
        ref_links,
    )
    matches = _match_units(session, req.term)
    result: dict[str, Any] = {
        "term": req.term,
        "filters": _filters_dict(session),
        "exists": bool(matches),
        "count": len(matches),
        "query": req.query,
        "ranked": req.query is not None,
        "matches": [],
    }
    if not matches:
        return result

    rank_by_id: dict[str, tuple[int, float]] = {}
    total = 0
    if req.query:
        ranked, _outcome = execute_query(session, req.query)
        total = len(ranked)
        rank_by_id = {h.unit.unit_id: (i, h.score) for i, h in enumerate(ranked)}
    result["total_ranked"] = total

    for u in matches:
        entry: dict[str, Any] = {
            "unit_id": u.unit_id,
            "kind": u.kind,
            "label": u.label,
            "location": _location(u),
        }
        if req.query:
            rk = rank_by_id.get(u.unit_id)
            if rk is None:
                entry["rank"] = None  # フィルタで対象外
            else:
                idx, score = rk
                entry["rank"] = idx
                entry["score"] = score
                entry["in_topk"] = idx < req.top_k
        result["matches"].append(entry)
    return result


def build_status_response(store: ResourceIndexStore, model: str) -> dict[str, Any]:
    """索引の素性（drift 認識用）。startup_header 相当を構造化して返す。"""
    bi = store.build_info
    conns: dict[str, int] = {}
    for u in store.units:
        cid = str(u.payload.get("connection_id"))
        conns[cid] = conns.get(cid, 0) + 1
    return {
        "index_dir": str(store.index_dir) if store.index_dir else None,
        "is_searchable": store.is_searchable(model),
        "model": model,
        "build_info": bi.model_dump() if bi else None,
        "unit_count": store.unit_count,
        "kind_counts": dict(bi.kind_counts) if bi else {},
        "kind_labels": dict(Kind.LABELS),
        "connection_counts": conns,
    }
