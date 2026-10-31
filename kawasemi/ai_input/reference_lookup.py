"""規約文書の典拠候補を**人が選ぶ**ための索引引き

AI下書き（`clinical_drafter`）は embedding 検索で典拠候補を出すが、**人手で典拠を足す経路
には索引を引く手段が無かった**。画面の典拠エディタは `unit_id` の自由入力欄だけで、
`unit_id` は `file:{パス}#p{ページ}:{開始}-{終了}` というチャンクのアドレスである。
1ページが複数のチャンクに分かれ、範囲も重なる
（`file:manual.pdf#p3:0-800` と `#p3:700-1500`）。**人が手で当てられる形ではない。**

索引に無いアドレスを書くと `routes._verified_citation` が payload を
`snippet` / `label` の2キーへ絞るので、ページも見出しも構造化されずに落ちる。つまり
「AIが選んだ典拠は正しく残るが、人は典拠を1件も足せない」状態だった。

**この経路は embedding も LLM も使わない。** ページ番号と部分一致だけで引く。
人が典拠を探す操作は、比較用の自動生成版とは独立でなければならない。人が典拠を探す
たびにモデルが動く経路があると、その独立を運用の注意だけで守ることになる。
構造的に呼ばない形にしておく。

候補の母集合は **AI 経路と同じ `is_citable_reference_unit`** である（目次チャンクを
除いた規約文書のユニット）。人だけが目次を典拠にできると、人が書いた版と自動生成版の
典拠を突き合わせる比較で候補集合が食い違う。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from kawasemi.ai_input.reference_scope import in_reference_scope
from kawasemi.ai_input.resource_index.derive import (
    format_citation_source,
    is_citable_reference_unit,
)
from kawasemi.ai_input.resource_index.schema import ResourceUnit
from kawasemi.ai_input.resource_index.store import ResourceIndexStore, get_store

DEFAULT_LIMIT = 20
MAX_LIMIT = 50

# ページ番号を持たないユニットを末尾へ送る整列キー。実在のページ番号と衝突しない値にする。
_NO_PAGE = 1 << 31


@dataclass(frozen=True)
class ReferenceUnitHit:
    """人へ提示する典拠候補1件

    `source` は `format_citation_source` が payload から決定的に組み立てた出典表記で、
    画面が独自に組み立て直さないためにここへ載せる（配布物に載る表記と同じ関数を通す）。
    """

    unit_id: str
    label: str
    source: str
    snippet: str
    path: str | None
    page: int | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "label": self.label,
            "source": self.source,
            "snippet": self.snippet,
            "path": self.path,
            "page": self.page,
        }


def _int_or_none(value: object) -> int | None:
    """ページ番号として読める値だけを int にする（`bool` は弾く）。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _page_of(unit: ResourceUnit) -> int | None:
    return _int_or_none(unit.payload.get("page"))


def _char_start(unit: ResourceUnit) -> int:
    """整列用のチャンク開始位置。同一ページ内の並びを原文の順に保つ。"""
    char_range = unit.payload.get("char_range")
    if isinstance(char_range, list | tuple) and char_range:
        start = _int_or_none(char_range[0])
        if start is not None:
            return start
    return 0


def _sort_key(unit: ResourceUnit) -> tuple[int, int, str]:
    page = _page_of(unit)
    # `unit_id` まで見て**全順序**にする。同順の並びが実行ごとに変わると、画面の候補の
    # 並びが不安定になり「さっき選んだ候補」を人が追えない。
    return (page if page is not None else _NO_PAGE, _char_start(unit), unit.unit_id)


def _matches(unit: ResourceUnit, needle: str) -> bool:
    """語の部分一致（`needle` は呼び出し側で casefold 済み）

    **原文の改行はそのまま**なので、行をまたぐ語は一致しない。項目番号（`[5]` の形で
    原文に現れる）と見出しの語を引く用途に足りる範囲に留める——ここを賢くすると、
    「何が一致するか」の規則が embedding 検索と二重になる。
    """
    if not needle:
        return True
    return needle in unit.search_text.casefold() or needle in unit.label.casefold()


def _resolve_store(store: ResourceIndexStore | None) -> ResourceIndexStore:
    return store if store is not None else get_store()


def reference_lookup_available(store: ResourceIndexStore | None = None) -> bool:
    """索引を引けるか（**embedding モデルの一致は要求しない**）

    `ResourceIndexStore.is_searchable` はベクトル検索の可否なので embedding モデルの
    一致まで見るが、この経路はベクトルを使わないので答えは変わらない。`has_unit`
    （`unit_id` の実在照合）が同じ理由で `is_searchable` を見ないのと揃える——ここでモデル
    一致まで要求すると、**検索が使えない施設で人手入力の典拠だけが黙って選べなくなる**。
    """
    resolved = _resolve_store(store)
    return resolved.is_loaded and resolved.schema_compatible


def find_reference_units(
    *,
    page: int | None = None,
    query: str | None = None,
    limit: int = DEFAULT_LIMIT,
    store: ResourceIndexStore | None = None,
    reference_paths: frozenset[str] | None = None,
) -> tuple[list[ReferenceUnitHit], int]:
    """典拠候補を引く。返り値は（提示する候補, 絞る前の総数）

    総数を併せて返すのは、**画面が「これで全部」と誤解しないため**である。上限で切った
    ことが分からないと、人は「マニュアルに該当箇所が無い」と読み違える。
    """
    resolved = _resolve_store(store)
    if not reference_lookup_available(resolved):
        return [], 0

    needle = (query or "").strip().casefold()
    matched = [
        unit
        for unit in resolved.units
        if is_citable_reference_unit(unit)
        and in_reference_scope(unit, reference_paths)
        and (page is None or _page_of(unit) == page)
        and _matches(unit, needle)
    ]
    matched.sort(key=_sort_key)

    capped = max(1, min(limit, MAX_LIMIT))
    hits = [
        ReferenceUnitHit(
            unit_id=unit.unit_id,
            label=unit.label,
            source=format_citation_source(
                path=unit.payload.get("path"),
                page=unit.payload.get("page"),
                char_range=unit.payload.get("char_range"),
            ),
            snippet=unit.snippet,
            path=unit.payload.get("path"),
            page=_page_of(unit),
        )
        for unit in matched[:capped]
    ]
    return hits, len(matched)


def reference_page_range(
    store: ResourceIndexStore | None = None, reference_paths: frozenset[str] | None = None
) -> tuple[int, int] | None:
    """典拠候補が存在するページの範囲（画面の入力補助用。無ければ None）"""
    resolved = _resolve_store(store)
    if not reference_lookup_available(resolved):
        return None
    pages = [
        page
        for page in (
            _page_of(unit)
            for unit in resolved.units
            if is_citable_reference_unit(unit) and in_reference_scope(unit, reference_paths)
        )
        if page is not None
    ]
    if not pages:
        return None
    return min(pages), max(pages)
