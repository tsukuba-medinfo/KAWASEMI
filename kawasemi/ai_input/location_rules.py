"""所在の規則：データ所在の AI 案に、施設が足す規則を読む。

## なぜ

所在の候補は索引の検索で決まるが、**検索では届かない所在がある。** 経過記録のような
文書は名称に領域の語彙を持たないので、項目テキストとの類似度で上位に来ない。そうした
文書は、施設が「検索で出なくても読む場所」として名指しするしかない。どの文書種別が
それに当たるかは施設の文書体系で決まるので、コードには持たず `site/location_rules.yaml`
に置く。

## 形

    fallback_unit_ids:
      - db:DEMO.DOCUMENT.NOTE_TEXT#NOTE   # 経過記録
      - db:DEMO.DOCUMENT.NOTE_TEXT#SUMMARY

- `fallback_unit_ids`: 検索の提示枠（上位 `_TOP_K` 件）の**後ろへ**常に足す索引の
  `unit_id`。書いた順に並ぶ。索引に無い id は黙って飛ばす
- **ファイルが無ければ空の規則**（受け皿を足さない）。デモと CI はこれで動く
- 形が正しくなければ例外にする（黙って空にすると、受け皿が消えたことに気づけない）
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from kawasemi.core.config import settings
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)


class LocationRulesError(ValueError):
    """`location_rules.yaml` の形が正しくない（どこが悪いかを文言に含める）。"""


@dataclass(frozen=True)
class LocationRules:
    fallback_unit_ids: tuple[str, ...] = ()


EMPTY = LocationRules()

_lock = threading.Lock()
# パス → (更新時刻, 規則)。更新時刻で読み直すので、ファイルを置いたら再起動なしで効く
_cache: dict[str, tuple[float | None, LocationRules]] = {}


def _parse(p: Path) -> LocationRules:
    try:
        raw: Any = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise LocationRulesError(f"{p} を YAML として読めない: {e}") from e
    if raw is None:
        return EMPTY
    if not isinstance(raw, dict):
        raise LocationRulesError(f"{p} はキーと値の対応表の形で書く")
    ids = raw.get("fallback_unit_ids") or []
    if not isinstance(ids, list) or not all(isinstance(v, str) and v for v in ids):
        raise LocationRulesError(
            f"{p} の fallback_unit_ids は空でない文字列のリストで書く: {ids!r}"
        )
    if len(ids) != len(set(ids)):
        raise LocationRulesError(f"{p} の fallback_unit_ids に重複がある: {ids!r}")
    logger.info(f"所在の規則を読み込んだ: 受け皿{len(ids)}件 ({p})")
    return LocationRules(fallback_unit_ids=tuple(ids))


def load_location_rules(path: str | None = None) -> LocationRules:
    """所在の規則を読む。ファイルが無ければ空の規則。"""
    key = str(path or settings.location_rules_file)
    p = Path(key)
    with _lock:
        mtime = p.stat().st_mtime if p.is_file() else None
        cached = _cache.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        rules = EMPTY if mtime is None else _parse(p)
        _cache[key] = (mtime, rules)
        return rules
