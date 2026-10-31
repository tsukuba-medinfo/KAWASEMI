"""字面（lexical）一致チャネル。

embedding の cosine 類似は「意味的に近い」候補を拾うが、コード直打ち・正式名称・
略称（aliases 収載分）の **完全一致** は、意味的に近いだけの候補に埋もれてランク落ち
しうる（docs/kawasemi_indexer_debug_spec.md の `⚠ambiguous` / `↳意味的近接のみ`）。

本モジュールは label / aliases / code / physical_name / logical_name への
完全一致・前方一致・部分一致・トークン被覆を軽量に判定する字面スコアラーを提供する
（外部依存なし。rapidfuzz / FTS5 を使わずとも「完全一致は決定的に安定」の要件を満たす）。

store 側はこのスコアを embedding 順位と RRF（Reciprocal Rank Fusion）で融合し、
完全一致を実質最上位へ押し上げる。
"""

from __future__ import annotations

import re
import unicodedata
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kawasemi_indexer.schema import ResourceUnit

_WS_RE = re.compile(r"\s+")

# 字面一致の段階別スコア（高いほど強い一致）。RRF とは別に「完全一致最上位」の判定にも使う。
TIER_EXACT = "exact"
TIER_PREFIX = "prefix"
TIER_SUBSTRING = "substring"
TIER_TOKEN = "token"
_TIER_SCORE = {TIER_EXACT: 1.0, TIER_PREFIX: 0.6, TIER_SUBSTRING: 0.4, TIER_TOKEN: 0.3}


def normalize(value: Any) -> str:
    """NFKC 正規化 + 小文字化 + 連続空白の畳み込み（全角/半角・大小の揺れを吸収）。"""
    if value is None:
        return ""
    return _WS_RE.sub(" ", unicodedata.normalize("NFKC", str(value)).strip().lower())


def _compact(text: str) -> str:
    """空白を除去した比較用の形（「架空薬乙錠 20mg」⇔「架空薬乙錠20mg」を同一視）。"""
    return text.replace(" ", "")


def surface_forms(unit: ResourceUnit) -> list[str]:
    """ユニットの「字面としての同一性」を担うフィールド群を正規化して返す。

    label（人可読名）・code（コード）・physical_name/logical_name（DB カラム名）・
    aliases（別名/標準コード群）を対象にする。search_text は notes 等の意味的語彙も
    含むため字面チャネルでは使わない（意味は embedding の担当）。
    """
    p = unit.payload or {}
    forms: list[Any] = [unit.label, p.get("code"), p.get("physical_name"), p.get("logical_name")]
    aliases = p.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    forms.extend(aliases)

    out: list[str] = []
    seen: set[str] = set()
    for f in forms:
        n = normalize(f)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


def match(query_norm: str, forms: list[str]) -> tuple[float, str | None]:
    """正規化済みクエリと候補の字面群を照合し、(スコア, 段階名) を返す。

    query_norm は normalize() 済みであることを前提とする。段階の強い順に:
    完全一致(1.0) > 前方一致(0.6) > 部分一致(0.4) > トークン被覆(<=0.3)。
    """
    if not query_norm or not forms:
        return 0.0, None

    qc = _compact(query_norm)
    best = 0.0
    best_kind: str | None = None
    long_enough = len(query_norm) >= 2

    for f in forms:
        if not f:
            continue
        fc = _compact(f)
        if query_norm == f or qc == fc:
            return _TIER_SCORE[TIER_EXACT], TIER_EXACT  # 完全一致は即最上位
        if not long_enough:
            continue
        if f.startswith(query_norm) or fc.startswith(qc):
            if _TIER_SCORE[TIER_PREFIX] > best:
                best, best_kind = _TIER_SCORE[TIER_PREFIX], TIER_PREFIX
        elif query_norm in f or qc in fc:
            if _TIER_SCORE[TIER_SUBSTRING] > best:
                best, best_kind = _TIER_SCORE[TIER_SUBSTRING], TIER_SUBSTRING

    if best == 0.0:
        # トークン被覆: クエリを空白分割し、各語がいずれかの form に部分一致する割合。
        tokens = [t for t in query_norm.split(" ") if len(t) >= 2]
        if tokens:
            blob = " ".join(forms)
            hit = sum(1 for t in tokens if t in blob)
            if hit:
                best = _TIER_SCORE[TIER_TOKEN] * (hit / len(tokens))
                best_kind = TIER_TOKEN
    return best, best_kind


def classify_match_type(has_lexical: bool, has_semantic: bool) -> str:
    """字面/意味のどちらのチャネルで当たったかを lexical / semantic / both で表す。"""
    if has_lexical and has_semantic:
        return "both"
    if has_lexical:
        return "lexical"
    return "semantic"


class LexicalIndex:
    """索引ユニット群に対する字面スコアラー（RRF 融合用の順位リストを供給する）。

    各ユニットの surface_forms を load 時に一度だけ前計算し、クエリごとに線形走査する
    （単位数が数万でも1クエリは短時間で済む。字面チャネルは検索点検・評価のホットループではない）。
    """

    def __init__(self, units: list[ResourceUnit]) -> None:
        self._forms: list[list[str]] = [surface_forms(u) for u in units]

    def __len__(self) -> int:
        return len(self._forms)

    def scores(self, query: str | list[str]) -> list[tuple[int, float, str]]:
        """字面一致した (行index, スコア, 段階名) を返す（スコア>0 のみ）。

        query に複数語を渡すと（クエリ書き換えで展開した別名・標準コード群など）、
        各ユニットのスコアは全語の中の最良一致を採る（変換テーブルによる展開検索）。
        """
        queries = [query] if isinstance(query, str) else query
        qns = [q for q in (normalize(x) for x in queries) if q]
        out: list[tuple[int, float, str]] = []
        if not qns:
            return out
        for i, forms in enumerate(self._forms):
            best = 0.0
            best_kind: str | None = None
            for qn in qns:
                score, kind = match(qn, forms)
                if score > best:
                    best, best_kind = score, kind
            if best > 0 and best_kind is not None:
                out.append((i, best, best_kind))
        return out
