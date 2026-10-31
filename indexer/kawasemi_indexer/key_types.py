"""キー種別カタログ key_types.yaml のロード（被験者ID解決のレベル1カタログ）

key_type（名前空間。例 mrn / dwh_serial / fhir_patient_id / trial_subject_no）の
定義を保持する。各 key_type は canonical（正準形）の仕様を持ち、resolver は
この正準形で値を返す。列ごとの物理整形（ゼロ詰め等）は索引unit側（レベル2＝
resource_key_binding）の責務で、本カタログには含めない。

設計:
- スポーク＝key_type 名前空間。crosswalk は Subject→{key_type:値} のスパース写像。
- key_type の id は将来 FHIR Patient.identifier.system URI と揃う形にしておく
  （backend=fhir が自然に後付けできる）。
- 値（名寄せ値）は本カタログにも KAWASEMI にも保持しない（非保持・非受領）。
  本ファイルは「どんな名前空間が存在するか」の定義のみ。

key_types.yaml の形:
    key_types:
      trial_subject_no: {label: 登録番号, canonical: {kind: string}}
      mrn:  {label: 院内MRN, system: "urn:oid:...",
             canonical: {kind: numeric_string, description: "ゼロ詰めを除いた数値文字列"}}
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from kawasemi_indexer.config import settings
from kawasemi_indexer.logging_config import get_logger

logger = get_logger(__name__)

# canonical.kind の語彙
CANONICAL_STRING = "string"
CANONICAL_NUMERIC_STRING = "numeric_string"
_CANONICAL_KINDS = {CANONICAL_STRING, CANONICAL_NUMERIC_STRING}


@dataclass(frozen=True)
class CanonicalSpec:
    """key_type の正準形（resolver が返す標準表現）の仕様。"""

    kind: str = CANONICAL_STRING
    description: str = ""

    @classmethod
    def from_raw(cls, raw: Any) -> CanonicalSpec:
        if not isinstance(raw, dict):
            return cls()
        kind = str(raw.get("kind", CANONICAL_STRING))
        if kind not in _CANONICAL_KINDS:
            logger.warning(f"未知の canonical.kind '{kind}' を string として扱います")
            kind = CANONICAL_STRING
        return cls(kind=kind, description=str(raw.get("description", "")))


@dataclass(frozen=True)
class KeyType:
    """キー種別（名前空間）の定義。"""

    id: str
    label: str
    system: str | None = None  # 将来 FHIR Patient.identifier.system URI と揃える
    canonical: CanonicalSpec = field(default_factory=CanonicalSpec)

    @classmethod
    def from_raw(cls, key_type_id: str, raw: Any) -> KeyType:
        if not isinstance(raw, dict):
            raw = {}
        return cls(
            id=key_type_id,
            label=str(raw.get("label", key_type_id)),
            system=(str(raw["system"]) if raw.get("system") else None),
            canonical=CanonicalSpec.from_raw(raw.get("canonical")),
        )


class KeyTypeCatalog:
    """key_type id → KeyType のカタログ。未配置なら空。"""

    def __init__(self, key_types: dict[str, KeyType] | None = None) -> None:
        self._key_types: dict[str, KeyType] = key_types or {}
        self.source_path: Path | None = None

    @property
    def is_loaded(self) -> bool:
        return bool(self._key_types)

    def get(self, key_type_id: str | None) -> KeyType | None:
        if not key_type_id:
            return None
        return self._key_types.get(key_type_id)

    def all(self) -> list[KeyType]:
        """全 key_type（KeyTypeDrafter の候補列挙用）。"""
        return list(self._key_types.values())

    @classmethod
    def load(cls, path: str | Path) -> KeyTypeCatalog:
        """key_types.yaml を読み込む。未配置・壊れていれば空カタログ。"""
        p = Path(path)
        catalog = cls()
        catalog.source_path = p
        if not p.exists():
            logger.info(f"キー種別カタログ未配置: {p}（key_type 解決なし）")
            return catalog
        try:
            data = yaml.safe_load(p.read_text("utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            logger.error(f"キー種別カタログ読み込みエラー: {p}: {e}")
            return cls()
        # トップレベル key_types: マッピング、または直接マッピングの両方を許容
        types = data.get("key_types", data) if isinstance(data, dict) else {}
        if not isinstance(types, dict):
            logger.error(f"キー種別カタログの形式が不正です: {p}")
            return cls()
        catalog._key_types = {str(k): KeyType.from_raw(str(k), v) for k, v in types.items()}
        logger.info(f"キー種別カタログ読み込み完了: {len(catalog._key_types)}件 ({p})")
        return catalog


# --- プロセス内シングルトン（遅延ロード） -----------------------------------
_lock = threading.Lock()
_cache: dict[str, KeyTypeCatalog] = {}


def get_catalog(path: str | None = None) -> KeyTypeCatalog:
    """パス単位でキャッシュされたキー種別カタログを返す（遅延ロード）。"""
    key = str(path or settings.key_types_file)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    with _lock:
        cached = _cache.get(key)
        if cached is None:
            cached = KeyTypeCatalog.load(key)
            _cache[key] = cached
        return cached


def reset_catalog_cache() -> None:
    """カタログキャッシュをクリアする（テスト用 / 差し替え後の再ロード用）。"""
    with _lock:
        _cache.clear()
