"""kawasemi-indexer 対話デバッガ（索引診断コンソール）

ビルド済みリソース索引に対し、臨床的コンテキスト（NL）を対話的に入力して
「データの場所」を診断するコンソール。仕様: docs/kawasemi_indexer_debug_spec.md。

設計:
- 正体は「索引の診断ツール」。NL→SQL 生成でも WebUI 再現でもない。
- 診断は2系統: embedding 検索（store.search）＋直接内省（store.units 走査）。
- テスト容易性のため I/O ループ（input/print/readline/色）と純粋な dispatch() を分離。
  dispatch(line, session) -> list[str] は TTY/Ollama 不要で pytest から叩ける。
"""

from __future__ import annotations

import json as _json
import os
import re
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import numpy as np

    from kawasemi_indexer.embedder import Embedder
    from kawasemi_indexer.ref_links import RefLinkIndex
    from kawasemi_indexer.store import ResourceIndexStore
    from kawasemi_indexer.synonyms import QueryRewriter

from kawasemi_indexer import lexical
from kawasemi_indexer.schema import Kind, ResourceHit
from kawasemi_indexer.search_pipeline import MODES, SearchOutcome, run_search

# 隣接候補とのスコア差がこの値未満なら「識別困難（曖昧）」とマークする。
AMBIGUOUS_DELTA = 0.01
# 複数行モードの終端トークン。
MULTILINE_TERMINATOR = ";;"
# readline 履歴の永続先（読み書き失敗時は黙って無効）。
HISTORY_FILE = os.path.expanduser("~/.kawasemi_indexer_history")
# :grep / :why の存在確認で列挙する最大件数。
_INTROSPECT_CAP = 50

_VALID_KINDS = Kind.ORDER  # (db, file, fhir, master) — 新 kind は自動で追従
_SPLIT_RE = re.compile(r"[\s　、。,.;:：/|()\[\]【】「」]+")

# --- ANSI 色（session.color が真のときだけ適用） ----------------------------- #
_RESET = "\x1b[0m"
_BOLD = "\x1b[1m"
_CYAN = "\x1b[36m"
_YELLOW = "\x1b[33m"
_DIM = "\x1b[2m"


@dataclass
class Session:
    """対話セッションの可変状態（dispatch が読み書きする唯一の入れ物）。"""

    store: ResourceIndexStore
    embedder: Embedder
    model: str
    top_k: int = 8
    min_score: float = 0.0
    kind_filter: str | None = None
    conn_filter: str | None = None
    color: bool = False
    # 検索パイプライン（ハイブリッド検索・クエリ書き換え＋参照リンクチャネル）。rewriter/ref_links が
    # None のモードはその施策抜きで縮退する（search_pipeline.run_search 参照）。
    mode: str = "ref"
    rewriter: QueryRewriter | None = None
    ref_links: RefLinkIndex | None = None
    last_hits: list[ResourceHit] = field(default_factory=list)
    last_query: str = ""
    last_qv: np.ndarray | None = None
    # :why が直近クエリと同じチャネル構成で順位を再現するための記録。
    last_terms: list[str] | None = None
    last_ref: list[tuple[str, str]] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# 小道具
# --------------------------------------------------------------------------- #
def _norm(s: Any) -> str:
    return unicodedata.normalize("NFKC", str(s if s is not None else "")).lower()


def _probes(text: str) -> list[str]:
    """クエリを語彙ヒット判定用の部分文字列群に分解する（2 文字以上）。"""
    return [p for p in _SPLIT_RE.split(_norm(text)) if len(p) >= 2]


def _paint(session: Session, text: str, code: str) -> str:
    return f"{code}{text}{_RESET}" if session.color else text


def _passes_filter(session: Session, unit: Any) -> bool:
    if session.kind_filter and unit.kind != session.kind_filter:
        return False
    if session.conn_filter and unit.payload.get("connection_id") != session.conn_filter:
        return False
    return True


def _renumber(session: Session, hits: list[ResourceHit]) -> list[ResourceHit]:
    """kind/conn フィルタを適用し cand_id を振り直す（ハイブリッド付随情報は保持）。"""
    filtered = [h for h in hits if _passes_filter(session, h.unit)]
    return [h.model_copy(update={"cand_id": i}) for i, h in enumerate(filtered)]


def _ranked(
    session: Session,
    qv: np.ndarray,
    min_score: float,
    terms: list[str] | None = None,
    ref_candidates: list[tuple[str, str]] | None = None,
) -> list[ResourceHit]:
    """全ユニットを検索順に並べ、kind/conn フィルタを適用して cand_id を振り直す。

    store.search は top_k で切るため、フィルタが正しく効くよう全件取得してから絞る。
    terms / ref_candidates を渡すとハイブリッド（＋参照リンク）順、無指定は cosine 順。
    """
    n = session.store.unit_count or 1
    hits = session.store.search(
        qv, top_k=n, min_score=min_score, lexical_terms=terms, ref_candidates=ref_candidates
    )
    return _renumber(session, hits)


def execute_query(session: Session, text: str) -> tuple[list[ResourceHit], SearchOutcome]:
    """検索パイプラインを session.mode で実行し、フィルタ済み全順位と中間情報を返す。

    CLI（run_query）と Web 層（webapi.build_search_response）が同じ経路を共有する。
    足切り線を「見える」ようにするため min_score=-1.0 で全件並べる（表示側で扱う）。
    """
    n = session.store.unit_count or 1
    outcome = run_search(
        session.store,
        session.embedder,
        text,
        mode=session.mode,
        top_k=n,
        min_score=-1.0,
        rewriter=session.rewriter,
        ref_links=session.ref_links,
    )
    ranked = _renumber(session, outcome.hits)
    session.last_hits = ranked[: session.top_k]
    session.last_query = text
    session.last_qv = outcome.qv
    session.last_terms = outcome.lexical_terms
    session.last_ref = outcome.ref_candidates
    return ranked, outcome


def _location(unit: Any) -> str:
    """db: conn/db_qualified/physical_name。master: conn/マスタ表/code。他は unit_id。"""
    p = unit.payload
    if unit.kind == Kind.DB:
        return f"{p.get('connection_id')} / {p.get('db_qualified')} / {p.get('physical_name')}"
    if unit.kind == Kind.MASTER:
        return f"{p.get('connection_id')} / {p.get('db_qualified')} / code={p.get('code')}"
    return f"{p.get('connection_id') or '-'} / {unit.unit_id}"


def _value_dict_hits(query_norm: str, unit: Any) -> list[str]:
    """クエリ語がコード辞書ラベルに当たった由来を `"label"=COL'code'` 形式で返す。"""
    out: list[str] = []
    phys = unit.payload.get("physical_name") or unit.unit_id
    for pair in unit.payload.get("value_dict") or []:
        label = pair.get("label")
        if label and _norm(label) in query_norm:
            out.append(f"\"{label}\"={phys}'{pair.get('code')}'")
    return out


def _lexical_hits(probes: list[str], unit: Any) -> list[str]:
    """クエリ部分文字列が候補のどのフィールドに現れるかを列挙する。"""
    fields = [
        ("logical_name", unit.payload.get("logical_name")),
        ("physical_name", unit.payload.get("physical_name")),
        ("search_text", unit.search_text),
    ]
    seen: set[tuple[str, str]] = set()
    out: list[str] = []
    for name, val in fields:
        fnorm = _norm(val)
        if not fnorm:
            continue
        for p in probes:
            if p in fnorm and (name, p) not in seen:
                seen.add((name, p))
                out.append(f"「{p}」∈ {name}")
    return out


def _lexical_match(query_norm: str, unit: Any) -> tuple[float, str | None]:
    """クエリと候補の字面一致（label/aliases/code 等）を (スコア, 段階名) で返す。

    診断表示（match_type）用。store のハイブリッド検索と同じ字面スコアラーを使う。
    query_norm は _norm 済みだが lexical.normalize で空白畳み込みも施して照合する。
    """
    return lexical.match(lexical.normalize(query_norm), lexical.surface_forms(unit))


def _quality_flags(unit: Any) -> list[str]:
    flags: list[str] = []
    if unit.payload.get("inferred"):
        flags.append("⚑inferred")
    if unit.kind == Kind.DB and not unit.payload.get("logical_name"):
        flags.append("論理名なし(物理名のみ)")
    phys = _norm(unit.payload.get("physical_name"))
    if phys and _norm(unit.search_text) == phys:
        flags.append("語彙=物理名のみ")
    return flags


def _ref_flag(hit: ResourceHit) -> str | None:
    """参照リンクチャネルで押し上げられたヒットの表示フラグ。

    人確認済みは ref✓、確認前（rule/inferred）は ⚑ 付きで確度を明示する（合意事項）。
    """
    if hit.ref_status is None:
        return None
    return "ref✓" if hit.ref_status == "confirmed" else f"⚑ref:{hit.ref_status}"


def _format_hit(
    session: Session, hit: ResourceHit, query_norm: str, probes: list[str], ambiguous: bool
) -> list[str]:
    u = hit.unit
    loc = _paint(session, _location(u), _BOLD + _CYAN)
    if hit.match_type is not None:
        # ハイブリッド検索済み: store が付けた match_type / rrf をそのまま表示する。
        mtype = hit.match_type
    else:
        # 純 embedding ビュー: 字面一致の有無を診断用に導出（意味チャネルは常に有効）。
        lscore, _lkind = _lexical_match(query_norm, u)
        mtype = lexical.classify_match_type(lscore > 0, has_semantic=True)
    head = (
        f"[{hit.cand_id}] {hit.score:.4f}  {u.kind}  {loc}  {_paint(session, f'[{mtype}]', _DIM)}"
    )
    if hit.rrf_score is not None:
        head += "  " + _paint(session, f"rrf={hit.rrf_score:.4f}", _DIM)
    rf = _ref_flag(hit)
    if rf:
        head += "  " + _paint(session, rf, _YELLOW if rf.startswith("⚑") else _CYAN)
    if ambiguous:
        head += "  " + _paint(session, "⚠ambiguous", _YELLOW)
    lines = [head]

    if u.kind == Kind.DB:
        p = u.payload
        meta = [str(p.get("logical_name") or "—"), str(p.get("type_raw") or "")]
        if p.get("pk"):
            meta.append("pk")
        if p.get("patient_key"):
            kt = p.get("key_type")
            meta.append(f"patient_key={p.get('patient_key')}" + (f"({kt})" if kt else ""))
    else:
        meta = [u.label]
    flags = _quality_flags(u)
    second = "      " + "  ".join(m for m in meta if m)
    for fl in flags:
        second += "  " + _paint(session, fl, _YELLOW)
    lines.append(second)

    for vd in _value_dict_hits(query_norm, u):
        lines.append("      " + _paint(session, f"← value_dict: {vd}", _CYAN))
    lex = _lexical_hits(probes, u)
    if lex:
        lines.append("      ↳ hit: " + " / ".join(lex))
    else:
        lines.append("      " + _paint(session, "↳ 意味的近接のみ（語彙一致なし）", _DIM))
    return lines


def hit_diagnostics(
    hit: ResourceHit, query_norm: str, probes: list[str], min_score: float, ambiguous: bool
) -> dict[str, Any]:
    """ResourceHit から診断データ（整形前）を抽出する純粋関数。

    _format_hit（CLI のテキスト整形）と Web 層（webapi）が同じ事実を共有するための
    単一ソース。診断ロジックの二重定義を避ける。store には触れないので Session 不要。
    """
    u = hit.unit
    lscore, lkind = _lexical_match(query_norm, u)
    quality_flags = _quality_flags(u)
    rf = _ref_flag(hit)
    if rf:
        quality_flags = [*quality_flags, rf]
    return {
        "location": _location(u),
        "value_dict_hits": _value_dict_hits(query_norm, u),
        "lexical_hits": _lexical_hits(probes, u),
        "quality_flags": quality_flags,
        "ambiguous": ambiguous,
        "below_min_score": hit.score < min_score,
        # 字面一致チャネル: ハイブリッド検索済みなら store の判定を優先。
        "match_type": hit.match_type or lexical.classify_match_type(lscore > 0, has_semantic=True),
        "lexical_score": hit.lexical_score if hit.lexical_score is not None else round(lscore, 4),
        "lexical_match": lkind,
        # RRF 融合と参照リンクチャネル（純 embedding 時は None）。
        "rrf_score": hit.rrf_score,
        "ref_status": hit.ref_status,
    }


# --------------------------------------------------------------------------- #
# クエリ実行
# --------------------------------------------------------------------------- #
def run_query(session: Session, text: str) -> list[str]:
    """臨床的コンテキストを検索パイプライン（session.mode）で検索し、診断付きで整形する。"""
    text = text.strip()
    if not text:
        return []
    ranked, outcome = execute_query(session, text)
    displayed = session.last_hits

    filt = _filter_desc(session)
    if not displayed:
        return [f"候補なし（索引空 or フィルタ {filt} で全除外）"]

    query_norm = _norm(text)
    probes = _probes(text)
    out = [
        f"上位 {len(displayed)} 件 / 全 {len(ranked)} 件{filt}"
        f"（mode={session.mode}, min_score={session.min_score}）"
    ]
    # パイプラインの中間情報（書き換え・参照リンクの発動）を可視化する。
    if outcome.rewrite is not None and outcome.rewrite.matched:
        srcs = ", ".join(str(p.get("label")) for p in outcome.rewrite.provenance[:3])
        out.append(
            f"↳ クエリ展開（{len(outcome.rewrite.expansions)}語 / 展開元: {srcs}）: "
            f"{outcome.rewrite.expansions}"
        )
    if outcome.ref_masters:
        desc = ", ".join(
            f"{m['connection_id']}/{m['db_qualified']}({m['via']})" for m in outcome.ref_masters
        )
        out.append(
            f"↳ 参照リンクチャネル発動: マスタ {desc} → 候補 {len(outcome.ref_candidates)} 列"
        )
    below_drawn = False
    prev_score: float | None = None
    for hit in displayed:
        # ハイブリッド順は cosine 単調でないため、足切り線は純 embedding 時のみ引く。
        if session.mode == "embedding" and not below_drawn and hit.score < session.min_score:
            out.append(f"--- min_score={session.min_score} ---")
            below_drawn = True
        ambiguous = prev_score is not None and abs(prev_score - hit.score) < AMBIGUOUS_DELTA
        out.extend(_format_hit(session, hit, query_norm, probes, ambiguous))
        prev_score = hit.score
    return out


def _filter_desc(session: Session) -> str:
    parts = []
    if session.kind_filter:
        parts.append(f"kind={session.kind_filter}")
    if session.conn_filter:
        parts.append(f"conn={session.conn_filter}")
    return f" [{', '.join(parts)}]" if parts else ""


# --------------------------------------------------------------------------- #
# メタコマンド
# --------------------------------------------------------------------------- #
def _cmd_show(session: Session, arg: str) -> list[str]:
    if not session.last_hits:
        return ["（直近の検索結果がありません。先にクエリを実行してください）"]
    try:
        n = int(arg)
        hit = session.last_hits[n]
    except (ValueError, IndexError):
        return [f"候補番号が不正です: {arg!r}（0..{len(session.last_hits) - 1}）"]
    u = hit.unit
    out = [f"[{hit.cand_id}] score={hit.score:.4f}  {u.kind}  {u.unit_id}", f"  label: {u.label}"]
    for k, v in u.payload.items():
        out.append(f"  {k}: {v}")
    out.append(f"  search_text: {u.search_text}")
    if u.snippet:
        out.append(f"  snippet: {u.snippet}")
    flags = _quality_flags(u)
    if flags:
        out.append("  flags: " + ", ".join(flags))
    return out


def _match_units(session: Session, term: str) -> list[Any]:
    """physical_name/logical_name/unit_id への部分一致（直接内省）。"""
    t = _norm(term)
    hits = []
    for u in session.store.units:
        if not _passes_filter(session, u):
            continue
        # 名前は kind により別フィールド: db=physical/logical_name、master=label/code。
        # unit_id と label を含め、code も拾う（コードでの存在確認も可に）。
        hay = " ".join(
            _norm(x)
            for x in (
                u.unit_id,
                u.label,
                u.payload.get("physical_name"),
                u.payload.get("logical_name"),
                u.payload.get("code"),
            )
        )
        if t in hay:
            hits.append(u)
    return hits


def _cmd_grep(session: Session, arg: str) -> list[str]:
    if not arg:
        return ["使い方: :grep <語>"]
    matches = _match_units(session, arg)
    if not matches:
        return [f"内省: '{arg}' に一致するユニットなし{_filter_desc(session)}"]
    out = [f"内省: {len(matches)} 件一致{_filter_desc(session)}"]
    for u in matches[:_INTROSPECT_CAP]:
        out.append(f"  {u.unit_id}  →  {_location(u)}  | {u.payload.get('logical_name') or '—'}")
    if len(matches) > _INTROSPECT_CAP:
        out.append(f"  …他 {len(matches) - _INTROSPECT_CAP} 件（:grep で絞り込み）")
    return out


def _cmd_why(session: Session, arg: str) -> list[str]:
    """対象の存在確認＋直近クエリでの順位（top-k 外も）を提示する。"""
    if not arg:
        return ["使い方: :why <自由語|カラム名|unit_id>"]
    matches = _match_units(session, arg)
    if not matches:
        return [f":why: '{arg}' は索引に見つかりません（内省一致なし）{_filter_desc(session)}"]
    out = [f":why '{arg}': 索引に {len(matches)} 件存在"]
    if session.last_qv is None:
        out.append("  （順位を出すには先にクエリを実行してください）")
        for u in matches[:_INTROSPECT_CAP]:
            out.append(f"  {u.unit_id}  →  {_location(u)}")
        return out

    # 直近クエリと同じチャネル構成（字面語群・参照リンク）で順位を再現する。
    ranked = _ranked(
        session,
        session.last_qv,
        min_score=-1.0,
        terms=session.last_terms,
        ref_candidates=session.last_ref or None,
    )
    rank_by_id = {h.unit.unit_id: (i, h.score) for i, h in enumerate(ranked)}
    out.append(f"  直近クエリ: {session.last_query!r}（全 {len(ranked)} 件中の順位）")
    for u in matches[:_INTROSPECT_CAP]:
        rk = rank_by_id.get(u.unit_id)
        if rk is None:
            out.append(f"  {u.unit_id}: フィルタ {_filter_desc(session)} で対象外")
            continue
        idx, score = rk
        in_topk = "（top-k 内）" if idx < session.top_k else f"（top-k={session.top_k} 外）"
        out.append(f"  第{idx}位 score={score:.4f} {in_topk}  {_location(u)}")
    return out


def _cmd_set_int(session: Session, arg: str, attr: str, label: str) -> list[str]:
    try:
        val = int(arg)
        if val < 1:
            raise ValueError
    except ValueError:
        return [f"{label} は正の整数で指定してください: {arg!r}"]
    setattr(session, attr, val)
    return [f"{label} = {val}"]


def _cmd_set_float(session: Session, arg: str, attr: str, label: str) -> list[str]:
    try:
        val = float(arg)
    except ValueError:
        return [f"{label} は数値で指定してください: {arg!r}"]
    setattr(session, attr, val)
    return [f"{label} = {val}"]


def _cmd_mode(session: Session, arg: str) -> list[str]:
    arg = arg.strip().lower()
    if not arg:
        return [f"現在の mode = {session.mode}（候補: {', '.join(MODES)}）"]
    if arg not in MODES:
        return [f"mode は {', '.join(MODES)} のいずれか: {arg!r}"]
    session.mode = arg
    notes = []
    if arg in ("rewrite", "ref") and session.rewriter is None:
        notes.append("（変換テーブル未ロード: 書き換えは不発）")
    if arg == "ref" and (session.ref_links is None or not len(session.ref_links)):
        notes.append("（参照リンク未配置: 第3チャネルは不発）")
    return [f"mode = {arg}" + "".join(notes)]


def _cmd_kind(session: Session, arg: str) -> list[str]:
    arg = arg.strip().lower()
    if arg in ("", "all"):
        session.kind_filter = None
        return ["kind フィルタ解除（全 kind）"]
    if arg not in _VALID_KINDS:
        return [f"kind は {', '.join(_VALID_KINDS)} / all のいずれか: {arg!r}"]
    session.kind_filter = arg
    return [f"kind フィルタ = {arg}"]


def _cmd_conn(session: Session, arg: str) -> list[str]:
    arg = arg.strip()
    if arg in ("", "all"):
        session.conn_filter = None
        return ["connection_id フィルタ解除（全 conn）"]
    session.conn_filter = arg
    return [f"connection_id フィルタ = {arg}"]


def _cmd_json(session: Session, arg: str) -> list[str]:
    if not session.last_hits:
        return ["（直近の検索結果がありません）"]
    arg = arg.strip()
    if arg in ("", "all"):
        payload: Any = [
            {"cand_id": h.cand_id, "score": h.score, "unit": h.unit.model_dump()}
            for h in session.last_hits
        ]
    else:
        try:
            h = session.last_hits[int(arg)]
        except (ValueError, IndexError):
            return [f"候補番号が不正です: {arg!r}"]
        payload = {"cand_id": h.cand_id, "score": h.score, "unit": h.unit.model_dump()}
    return _json.dumps(payload, ensure_ascii=False, indent=2).splitlines()


def _cmd_help() -> list[str]:
    return [
        "コマンド一覧:",
        "  <text>                  臨床的コンテキストとして検索（mode に従う・全 kind 横断）",
        "  :mode <m>               検索モード切替（embedding|hybrid|rewrite|ref。既定 ref）",
        "  :multi … ;;             複数行ブロックを 1 クエリとして検索",
        "  :show N                 候補 N の全 payload＋search_text＋snippet",
        "  :why <語|カラム|unit_id> 索引内の存在確認＋直近クエリでの順位（top-k 外も）",
        "  :grep <語>              physical/logical/unit_id への部分一致スキャン（内省）",
        "  :k N                    top-k 変更",
        "  :t X                    min_score 変更",
        "  :kind db|file|fhir|all  kind 絞り込み",
        "  :conn <id>|all          connection_id 絞り込み",
        "  :json N|all             候補を JSON 出力（機械可読）",
        "  :help                   この一覧",
        "  :quit                   終了（Ctrl-D でも可）",
    ]


def dispatch(line: str, session: Session) -> list[str]:
    """1 行の入力（メタコマンド or クエリ）を処理し、出力行リストを返す純粋関数。

    複数行収集（:multi）と終了（:quit）は対話ループ側が処理する。テストはここを直接叩く。
    """
    line = line.strip()
    if not line:
        return []
    if not line.startswith(":"):
        return run_query(session, line)

    parts = line[1:].split(maxsplit=1)
    cmd = parts[0].lower() if parts else ""
    arg = parts[1] if len(parts) > 1 else ""

    if cmd == "help":
        return _cmd_help()
    if cmd == "show":
        return _cmd_show(session, arg)
    if cmd == "why":
        return _cmd_why(session, arg)
    if cmd == "grep":
        return _cmd_grep(session, arg)
    if cmd == "mode":
        return _cmd_mode(session, arg)
    if cmd == "k":
        return _cmd_set_int(session, arg, "top_k", "top_k")
    if cmd == "t":
        return _cmd_set_float(session, arg, "min_score", "min_score")
    if cmd == "kind":
        return _cmd_kind(session, arg)
    if cmd == "conn":
        return _cmd_conn(session, arg)
    if cmd == "json":
        return _cmd_json(session, arg)
    return [f"未知のコマンド: :{cmd}（:help で一覧）"]


# --------------------------------------------------------------------------- #
# 起動ヘッダ
# --------------------------------------------------------------------------- #
def startup_header(session: Session) -> list[str]:
    """索引の素性（drift 認識用）を返す。"""
    bi = session.store.build_info
    out = ["=" * 60, "kawasemi-indexer 対話デバッガ（索引診断コンソール）"]
    if bi is None:
        out.append("索引: 未ロード")
        return out
    out.append(f"索引: {session.store.index_dir}")
    out.append(
        f"  embedding_model={bi.embedding_model}  schema_version={bi.schema_version}  "
        f"dim={bi.dim}  built_at={bi.built_at}"
    )
    out.append(f"  unit_count={bi.unit_count}  kind={dict(bi.kind_counts)}")
    conns: dict[str, int] = {}
    for u in session.store.units:
        cid = str(u.payload.get("connection_id"))
        conns[cid] = conns.get(cid, 0) + 1
    out.append(f"  connection_id={conns}")
    rw = session.rewriter.entry_count if session.rewriter is not None else 0
    rl = len(session.ref_links) if session.ref_links is not None else 0
    out.append(
        f"  top_k={session.top_k}  min_score={session.min_score}  mode={session.mode}"
        f"  変換テーブル={rw}エントリ  参照リンク={rl}件"
    )
    out.append(":help でコマンド一覧。:quit で終了。")
    out.append("=" * 60)
    return out


# --------------------------------------------------------------------------- #
# I/O ループ（対話時のみ。テスト対象外）
# --------------------------------------------------------------------------- #
def _load_history() -> None:
    try:
        import readline

        if os.path.exists(HISTORY_FILE):
            readline.read_history_file(HISTORY_FILE)
    except Exception:
        pass


def _save_history() -> None:
    try:
        import readline

        readline.write_history_file(HISTORY_FILE)
    except Exception:
        pass


def _read_multiline() -> str:
    lines: list[str] = []
    while True:
        try:
            ln = input("... ")
        except EOFError:
            break
        if ln.strip() == MULTILINE_TERMINATOR:
            break
        lines.append(ln)
    return "\n".join(lines)


def run(session: Session) -> int:
    """対話ループ。embedder/store はロード済み・検索可能であることを前提とする。"""
    _load_history()
    for ln in startup_header(session):
        print(ln)
    try:
        while True:
            try:
                line = input("indexer> ")
            except EOFError:
                print()
                break
            except KeyboardInterrupt:
                print("\n（入力をキャンセル。:quit で終了）")
                continue

            stripped = line.strip()
            if stripped.lower() in (":quit", ":q", ":exit"):
                break
            if stripped.lower() == ":multi":
                text = _read_multiline()
                lines = run_query(session, text)
            else:
                lines = dispatch(line, session)
            for out in lines:
                print(out)
    finally:
        _save_history()
    return 0
