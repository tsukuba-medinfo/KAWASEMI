"""集計の**対象項目**（分母）を様式ごとに決める。

所在の確からしさの段階、典拠の付与率、取得の母集団は、どれも「その様式の対象項目のうち
N 項目」という形で出す。**分母はここの1箇所で決める**（呼び出し側で項目を数え直すと、
指標ごとに分母がずれる）。

## 項目番号

項目番号は問診票の `item.text` の先頭 `[NNN]` で表す（`item_number`）。`link_id` との
対応は問診票 JSON が持つので、対象判定は項目番号で行う。

## 設定（`site/target_items.yaml`）

どの項目を分母に入れるかは様式（とそれを使う研究）で決まるので、コードには持たず
`settings.target_items_file`（既定 `<site_dir>/target_items.yaml`）に置く。
様式は `Questionnaire.url` の**完全一致**で引く:

    questionnaires:
      "http://kawasemi.example/Questionnaire/e2e-infection":
        target_item_numbers: ["1", "2", "3"]   # 分母に入れる項目番号
        excluded_item_numbers: ["14"]          # 意図した対象外（任意・記録用）

- 設定のある様式: 項目番号が `target_item_numbers` にある項目が対象。期待件数
  （`expected`）は `target_item_numbers` の件数で、問診票に見つからなかった番号は
  `missing_numbers` に出る（分母が静かに縮まないように）。番号を持つが対象でない項目は
  `excluded`、番号を持たない項目は `extension`
- 設定の無い様式（**既定**）: 項目番号を持つ項目が1つでもあれば番号を持つ項目すべてが対象
  （番号を持たない項目は `extension`）。1つも無ければ回答できる項目すべてが対象。期待件数は
  見つかった対象の数（`missing_numbers` は常に空）
- **ファイルが無ければ全様式が既定**。デモと CI はこれで動く
- 形が正しくなければ例外にする（黙って既定に落とすと、分母が変わったことに気づけない）
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kawasemi.core.config import settings
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)

_ITEM_NUMBER = re.compile(r"^\[(\d+)\]")

# 項目の種別（分母に入るか、意図した対象外か、番号を持たない項目か）
TARGET = "target"
EXCLUDED = "excluded"
EXTENSION = "extension"


class TargetItemsError(ValueError):
    """`target_items.yaml` の形が正しくない（どこが悪いかを文言に含める）。"""


def item_number(text: str | None) -> str | None:
    """問診票の項目テキスト先頭の `[NNN]` から項目番号を取り出す（無ければ None）。"""
    if not text:
        return None
    matched = _ITEM_NUMBER.match(text)
    return matched.group(1) if matched else None


@dataclass(frozen=True)
class FormTargets:
    """1つの様式の設定（項目番号の集合）。"""

    target_numbers: frozenset[str]
    excluded_numbers: frozenset[str] = frozenset()


@dataclass(frozen=True)
class Targets:
    """問診票1つの分母。

    - `link_ids`: 対象項目の `link_id`（問診票の順）。**分母はこれの件数**
    - `expected`: 期待する対象項目数（設定があれば設定の件数、無ければ `len(link_ids)`）
    - `missing_numbers`: 設定にあるのに問診票に見つからなかった項目番号
    - `kinds`: `link_id` → 種別（`TARGET` / `EXCLUDED` / `EXTENSION`）
    """

    link_ids: list[str]
    expected: int
    missing_numbers: list[str] = field(default_factory=list)
    kinds: dict[str, str] = field(default_factory=dict)


_lock = threading.Lock()
# パス → (更新時刻, 表)。更新時刻で読み直すので、ファイルを置いたら再起動なしで効く
_cache: dict[str, tuple[float | None, dict[str, FormTargets]]] = {}


def _numbers(value: Any, where: str) -> frozenset[str]:
    # 文字列をそのまま集合にすると1文字ずつに割れるので、リストでなければ止める。
    # YAML で引用符を付け忘れた整数（`- 100`）は受けるが、先頭0の消える書き方は区別できない
    if not isinstance(value, list):
        raise TargetItemsError(f'{where} は項目番号のリストで書く（例: ["1", "2"]）: {value!r}')
    out: list[str] = []
    for v in value:
        s = str(v) if isinstance(v, int) and not isinstance(v, bool) else v
        if not isinstance(s, str) or not s.isdigit():
            raise TargetItemsError(f"{where} の項目番号は数字の文字列で書く: {v!r}")
        out.append(s)
    if len(out) != len(set(out)):
        raise TargetItemsError(f"{where} に重複がある: {value!r}")
    return frozenset(out)


def _parse(p: Path) -> dict[str, FormTargets]:
    try:
        raw: Any = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise TargetItemsError(f"{p} を YAML として読めない: {e}") from e
    if raw is None:
        return {}
    if not isinstance(raw, dict) or not isinstance(raw.get("questionnaires", {}), dict):
        raise TargetItemsError(f"{p} は `questionnaires:`（様式の url → 設定）の対応表で書く")
    table: dict[str, FormTargets] = {}
    for url, spec in (raw.get("questionnaires") or {}).items():
        where = f"{p} の questionnaires[{url!r}]"
        if not isinstance(url, str) or not url:
            raise TargetItemsError(f"{p} の様式の url は空でない文字列で書く: {url!r}")
        if not isinstance(spec, dict) or "target_item_numbers" not in spec:
            raise TargetItemsError(f"{where} は target_item_numbers を持つ対応表で書く")
        unknown = set(spec) - {"target_item_numbers", "excluded_item_numbers"}
        if unknown:
            raise TargetItemsError(f"{where} に知らないキーがある: {sorted(unknown)}")
        targets = _numbers(spec["target_item_numbers"], f"{where}.target_item_numbers")
        excluded = _numbers(
            spec.get("excluded_item_numbers") or [], f"{where}.excluded_item_numbers"
        )
        both = targets & excluded
        if both:
            raise TargetItemsError(f"{where} で対象と対象外の両方にある番号: {sorted(both)}")
        table[url] = FormTargets(target_numbers=targets, excluded_numbers=excluded)
    logger.info(f"対象項目の設定を読み込んだ: 様式{len(table)}件 ({p})")
    return table


def load_target_items(path: str | None = None) -> dict[str, FormTargets]:
    """対象項目の設定（様式の url → 設定）を読む。ファイルが無ければ空。"""
    key = str(path or settings.target_items_file)
    p = Path(key)
    with _lock:
        mtime = p.stat().st_mtime if p.is_file() else None
        cached = _cache.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        table = {} if mtime is None else _parse(p)
        _cache[key] = (mtime, table)
        return table


def resolve_targets(questionnaire_url: str | None, fields: list[dict[str, Any]]) -> Targets:
    """問診票の分母を決める。

    `fields` は `routes._editable_fields` の戻り値（`link_id` と `text` を持つ dict）。
    **対象外を落とすのはここだけ**にして、集計の分母がこの1箇所から決まるようにする。
    """
    spec = load_target_items().get(questionnaire_url) if questionnaire_url else None
    numbered = [(f, item_number(f.get("text"))) for f in fields if f.get("link_id")]
    any_numbered = any(n for _, n in numbered)

    def kind_of(n: str | None) -> str:
        if spec is None:
            return TARGET if (n or not any_numbered) else EXTENSION
        if n is None:
            return EXTENSION
        # 番号はあるが対象でないものは、excluded_item_numbers に書いていなくても対象外
        return TARGET if n in spec.target_numbers else EXCLUDED

    classified = [(str(f["link_id"]), kind_of(n)) for f, n in numbered]
    kinds = dict(classified)
    link_ids = [lid for lid, kind in classified if kind == TARGET]
    if spec is None:
        return Targets(link_ids=link_ids, expected=len(link_ids), kinds=kinds)
    found = {item_number(f.get("text")) for f in fields}
    return Targets(
        link_ids=link_ids,
        expected=len(spec.target_numbers),
        missing_numbers=sorted(spec.target_numbers - {n for n in found if n}, key=int),
        kinds=kinds,
    )
