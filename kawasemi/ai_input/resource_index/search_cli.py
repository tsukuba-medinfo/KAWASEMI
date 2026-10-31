"""`kawasemi index search` の検索ロジック（印字と分離した構造化層）

CLI から「このクエリで索引が何を返すか」を単体で確認するためのデバッグ補助。
本番の「データ所在AI案」(AIResourceDrafter) と同じ検索経路を再現する:

- 索引が検索可能（ロード済み・schema互換・embeddingモデル一致）なら embedding 検索。
  **取得の本体は `resource_drafter.retrieve_hits` を呼ぶ**——規約文書の
  除外、表の第1段絞り込み、長短2クエリの RRF 併合、型ヒントに
  よる加点まで本番と同じ経路を通る。以前はここに別の検索が書かれており、
  「なぜこの列が提案された／されなかった」を説明する道具が本番と別の順位を出していた。
- 検索不可なら本番同様に **DB定義書 BM25** へフォールバック（ColumnHit, Ollama不要）。

**検索ヒントは既定で本番と同じく LLM に生成させる。** `--no-hints` で切ると
拡張も加点も無い素の検索になり、`--hint-term` / `--hint-type` を渡せば LLM を呼ばずに
特定のヒントで再現できる（来歴に残った語で調べ直すため）。

`--engine` で経路を強制でき、同一クエリで embedding / bm25 を A/B 比較できる。
印字は formatter（format_text / format_json）が担い、本層は構造化結果のみ返す
（テスト容易化・mypy strict 準拠）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from kawasemi.ai_input.db_definition import ColumnHit, get_index
from kawasemi.ai_input.location_hint import EMPTY_HINT, LocationHint, generate_hints
from kawasemi.ai_input.models import SourceCategory
from kawasemi.ai_input.resource_index.connections import SCOPE_EXTERNAL, get_registry
from kawasemi.ai_input.resource_index.derive import build_location, derive_source_category
from kawasemi.ai_input.resource_index.schema import Kind, ResourceHit
from kawasemi.ai_input.resource_index.store import get_store
from kawasemi.core.config import settings
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)

ENGINE_AUTO = "auto"
ENGINE_EMBEDDING = "embedding"
ENGINE_BM25 = "bm25"
ENGINE_CHOICES = (ENGINE_AUTO, ENGINE_EMBEDDING, ENGINE_BM25)


@dataclass
class SearchHit:
    """エンジン非依存に正規化した検索ヒット1件（表示・JSON共通）"""

    rank: int
    cand_id: int
    score: float
    kind: str
    label: str
    snippet: str
    location: str
    source_category: str
    unit_id: str | None = None
    connection_id: str | None = None


@dataclass
class SearchResult:
    """検索1回分の構造化結果（診断ヘッダ＋ヒット列）"""

    query: str
    requested_engine: str
    effective_engine: str
    used_fallback: bool
    diagnostics: dict[str, Any] = field(default_factory=dict)
    hits: list[SearchHit] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _build_query(query: str, annotation: str | None) -> str:
    """本番の素のクエリ（item_text + annotation）。ヒント語は `_query_text` が足す。"""
    return " ".join(p for p in (query, annotation) if p)


def _resolve_hint(
    query: str,
    annotation: str | None,
    *,
    use_llm: bool,
    terms: tuple[str, ...],
    types: tuple[str, ...],
    model: str | None,
    diagnostics: dict[str, Any],
) -> LocationHint:
    """検索ヒントを決める。

    明示のヒントがあればそれを使い、無ければ本番と同じく LLM に生成させる。
    `use_llm` が偽なら空のヒント（＝拡張も加点も無い、この施策より前の検索）。
    **失敗しても止めない**（本番も `generate_hints` は例外を投げない）。
    """
    if terms or types:
        diagnostics["hint_source"] = "指定"
        hint = LocationHint(terms=terms, expected_types=types)
    elif not use_llm:
        diagnostics["hint_source"] = "なし（--no-hints）"
        hint = EMPTY_HINT
    else:
        import asyncio

        from kawasemi.ai_input.llm_provider import design_llm_model, make_design_llm_client

        selected = (model or design_llm_model()).strip()
        item = {"link_id": "cli", "item_text": query, "annotation": annotation}
        try:
            result = asyncio.run(generate_hints(make_design_llm_client(), selected, [item]))
        except Exception as e:  # noqa: BLE001 - 調査の道具を推論サーバの都合で止めない
            diagnostics["hint_source"] = f"生成に失敗（{type(e).__name__}）"
            return EMPTY_HINT
        diagnostics["hint_source"] = f"生成（{selected}）" if not result.failed else "生成に失敗"
        hint = result.of("cli")
    diagnostics["hint_terms"] = list(hint.terms)
    diagnostics["hint_types"] = list(hint.expected_types)
    return hint


def _source_category_for_db_group(db_group: str | None) -> str:
    """BM25(ColumnHit) の db_group(connection_id) から source_category を導出。

    derive.derive_source_category（embedding版）と同じ scope 判定で local_db /
    external_db を振り分ける（未登録は local_db 既定）。
    """
    scope = get_registry().scope_of(db_group)
    return SourceCategory.EXTERNAL_DB if scope == SCOPE_EXTERNAL else SourceCategory.LOCAL_DB


def _embedding_hits_to_search(hits: list[ResourceHit]) -> list[SearchHit]:
    registry = get_registry()
    out: list[SearchHit] = []
    for h in hits:
        out.append(
            SearchHit(
                rank=h.cand_id + 1,
                cand_id=h.cand_id,
                score=h.score,
                kind=h.unit.kind,
                label=h.unit.label,
                snippet=h.unit.snippet,
                location=build_location(h.unit),
                source_category=derive_source_category(h.unit, registry),
                unit_id=h.unit.unit_id,
                connection_id=h.unit.payload.get("connection_id"),
            )
        )
    return out


def _bm25_hits_to_search(hits: list[ColumnHit]) -> list[SearchHit]:
    out: list[SearchHit] = []
    for h in hits:
        out.append(
            SearchHit(
                rank=h.cand_id + 1,
                cand_id=h.cand_id,
                score=round(h.score, 4),
                kind=Kind.DB,
                label=" ".join(p for p in (h.physical_name, h.logical_name) if p) or "（カラム）",
                snippet=h.notes or "",
                location=h.build_location(),
                source_category=_source_category_for_db_group(h.db_group),
                unit_id=None,
                connection_id=h.db_group,
            )
        )
    return out


def _run_embedding(
    item_text: str,
    annotation: str | None,
    *,
    hint: LocationHint,
    top_k: int,
    model: str,
    index_dir: str | None,
    diagnostics: dict[str, Any],
) -> tuple[list[SearchHit], str | None]:
    """embedding 経路。**本番と同じ `retrieve_hits` を通す**。

    以前はここで `store.search` を直に呼んでおり、規約文書の除外もクエリ拡張・
    型加点も表の第1段絞り込みも RRF 併合も入っていなかった。
    写した実装を2つ置くと必ずずれる——本番だけが動き続け、差は広がっていた。

    `min_score` は受け取らない。**本番は `min_score=0.0` で引いて並べ替えで絞る**ので、
    ここで足切りすると別の順位になる（足切りは診断ヘッダに出す値としてのみ残す）。

    Returns: (hits, error)。Ollama 未起動等で失敗した場合は error を埋める。
    """
    from kawasemi.ai_input.resource_drafter import (
        _query_text,
        _short_query_text,
        retrieve_hits,
    )
    from kawasemi.ai_input.resource_index.embedder import OllamaEmbedder
    from kawasemi.ai_input.resource_index.embedding_identity import (
        compare_digests,
        embedder_digest,
    )

    store = get_store(index_dir)
    if not store.is_loaded:
        return [], "索引が未配置/未ロードです（embedding 検索不可）"

    item = {"item_text": item_text, "annotation": annotation}
    long_query = _query_text(item, hint)
    short_query = _short_query_text(item, hint)
    diagnostics["expanded_query"] = long_query
    diagnostics["short_query"] = short_query
    if not long_query:
        return [], "クエリが空です"

    try:
        embedder = OllamaEmbedder(
            model=model,
            base_url=settings.embedding_host,
            timeout=settings.ollama_timeout,
        )
        # **索引と同じ重みか照合する**。本番と同じく、違えば検索しない。診断に
        # ダイジェストと照合の結果（未検証の理由を含む）を出す。
        identity = compare_digests(store.embedding_digest, embedder_digest(embedder))
        diagnostics["embedding_host"] = settings.embedding_host
        diagnostics["index_embedding_digest"] = identity.index_digest
        diagnostics["query_embedding_digest"] = identity.query_digest
        diagnostics["embedding_identity"] = identity.reason or "一致"
        if not identity.compatible:
            return [], identity.reason
        vecs = embedder.embed([long_query, short_query or long_query])
    except Exception as e:  # Ollama 未起動・モデル未pull 等
        return [], f"クエリの埋め込みに失敗しました（Ollama要確認）: {e}"
    diagnostics["query_dim"] = int(vecs.shape[1]) if vecs.shape[0] else 0

    hits = retrieve_hits(
        store,
        hint,
        vecs[0],
        vecs[1] if short_query else None,
        top_k=top_k,
    )
    return _embedding_hits_to_search(hits), None


def _run_bm25(
    query: str,
    *,
    top_k: int,
    corpus_dir: str | None,
) -> list[SearchHit]:
    """BM25 経路（DB定義書コーパス）。Ollama 不要。"""
    index = get_index(corpus_dir)
    hits = index.search(query, top_k=top_k)
    return _bm25_hits_to_search(hits)


def run_search(
    query: str,
    *,
    annotation: str | None = None,
    engine: str = ENGINE_AUTO,
    top_k: int = 8,
    min_score: float | None = None,
    model: str | None = None,
    index_dir: str | None = None,
    corpus_dir: str | None = None,
    use_hints: bool = True,
    hint_terms: list[str] | None = None,
    hint_types: list[str] | None = None,
    hint_model: str | None = None,
) -> SearchResult:
    """1クエリの検索を実行し、診断付きの構造化結果を返す（印字はしない）。

    検索ヒントは既定で本番と同じく LLM に生成させる。`use_hints=False` で
    切ると拡張も加点も無い素の検索になり、`hint_terms` / `hint_types` を渡せば
    LLM を呼ばずに特定のヒントで再現できる。
    """
    effective_query = _build_query(query, annotation)
    eff_model = (model or settings.embedding_model).strip()
    eff_min_score = settings.resource_index_min_score if min_score is None else min_score

    store = get_store(index_dir)
    searchable = store.is_searchable(eff_model)

    diagnostics: dict[str, Any] = {
        "index_dir": str(store.index_dir) if store.index_dir else None,
        "index_loaded": store.is_loaded,
        "unit_count": store.unit_count,
        "index_model": store.embedding_model,
        "query_model": eff_model,
        "schema_compatible": store.schema_compatible,
        "searchable": searchable,
        "top_k": top_k,
        "min_score": eff_min_score,
    }
    if store.build_info is not None:
        diagnostics["index_dim"] = store.build_info.dim
        diagnostics["schema_version"] = store.build_info.schema_version

    # 検索不可の理由を切り分け（auto がなぜ BM25 に落ちたかの説明用）
    reasons: list[str] = []
    if not store.is_loaded:
        reasons.append("索引が未配置/未ロード")
    if store.is_loaded and not store.schema_compatible:
        reasons.append("schema_version 非互換（再索引が必要）")
    if store.is_loaded and store.embedding_model != eff_model:
        reasons.append(f"embeddingモデル不一致（索引={store.embedding_model} / 指定={eff_model}）")
    if reasons:
        diagnostics["not_searchable_reasons"] = reasons

    # エンジン決定（auto は本番同様 searchable で分岐）
    if engine == ENGINE_EMBEDDING:
        effective_engine = ENGINE_EMBEDDING
    elif engine == ENGINE_BM25:
        effective_engine = ENGINE_BM25
    else:  # auto
        effective_engine = ENGINE_EMBEDDING if searchable else ENGINE_BM25
    used_fallback = engine == ENGINE_AUTO and effective_engine == ENGINE_BM25

    result = SearchResult(
        query=effective_query,
        requested_engine=engine,
        effective_engine=effective_engine,
        used_fallback=used_fallback,
        diagnostics=diagnostics,
    )

    if not effective_query:
        result.error = "クエリが空です"
        return result

    if effective_engine == ENGINE_EMBEDDING:
        if engine == ENGINE_EMBEDDING and not searchable and reasons:
            # 強制 embedding だが不一致がある場合は次元ズレ等で空/誤検索の恐れを明示
            diagnostics["forced_embedding_warning"] = "; ".join(reasons)
        # **索引が読めないならヒントを生成しない**。生成は LLM の往復なので、
        # 索引が無い環境では「結局エラーになる検索」のために推論サーバの上限まで待つことに
        # なる（実測 37 秒）。本番は `is_searchable` を通ってから生成に入る。
        hint = _resolve_hint(
            query,
            annotation,
            use_llm=use_hints and store.is_loaded,
            terms=tuple(hint_terms or ()),
            types=tuple(hint_types or ()),
            model=hint_model,
            diagnostics=diagnostics,
        )
        hits, error = _run_embedding(
            query,
            annotation,
            hint=hint,
            top_k=top_k,
            model=eff_model,
            index_dir=index_dir,
            diagnostics=diagnostics,
        )
        result.hits = hits
        result.error = error
    else:
        result.hits = _run_bm25(effective_query, top_k=top_k, corpus_dir=corpus_dir)

    return result


# --- フォーマッタ -------------------------------------------------------------

_ENGINE_LABELS = {
    ENGINE_EMBEDDING: "embedding（リソース索引・cosine）",
    ENGINE_BM25: "bm25（DB定義書フォールバック）",
}


def format_text(result: SearchResult) -> str:
    """人間可読の表形式に整形する。"""
    d = result.diagnostics
    lines: list[str] = []
    lines.append("=" * 72)
    lines.append(f"クエリ: {result.query!r}")
    engine_label = _ENGINE_LABELS.get(result.effective_engine, result.effective_engine)
    suffix = "（auto→フォールバック）" if result.used_fallback else ""
    lines.append(f"経路  : {engine_label}{suffix}  [指定: {result.requested_engine}]")
    lines.append(
        f"索引  : {d.get('index_dir')} | ロード={d.get('index_loaded')} "
        f"ユニット={d.get('unit_count')} 索引model={d.get('index_model')} "
        f"検索可={d.get('searchable')}"
    )
    lines.append(
        f"検索  : query_model={d.get('query_model')} top_k={d.get('top_k')} "
        f"min_score={d.get('min_score')}"
    )
    if d.get("not_searchable_reasons"):
        lines.append("検索不可の理由: " + " / ".join(d["not_searchable_reasons"]))
    if d.get("forced_embedding_warning"):
        lines.append("⚠ 強制embedding警告: " + d["forced_embedding_warning"])
    lines.append("-" * 72)

    if result.error:
        lines.append(f"エラー: {result.error}")
        lines.append("=" * 72)
        return "\n".join(lines)

    if not result.hits:
        lines.append("ヒットなし（該当候補が見つかりませんでした）")
        lines.append("=" * 72)
        return "\n".join(lines)

    for h in result.hits:
        kind_label = Kind.LABELS.get(h.kind, h.kind)
        lines.append(f"#{h.rank:<2} score={h.score:<7} [{kind_label}] {h.label}")
        lines.append(f"     所在  : {h.location}")
        lines.append(
            f"     種別  : {h.source_category}"
            + (f" | 接続: {h.connection_id}" if h.connection_id else "")
        )
        if h.snippet:
            lines.append(f"     抜粋  : {h.snippet}")
    lines.append("=" * 72)
    return "\n".join(lines)


def format_json(result: SearchResult) -> str:
    import json

    return json.dumps(result.to_dict(), ensure_ascii=False, indent=2)
