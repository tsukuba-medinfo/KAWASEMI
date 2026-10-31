"""典拠の範囲：様式ごとに、典拠にしてよい規約文書を決める。

## なぜ

典拠の索引は施設に1つで、様式を区別しない。1つの領域だけを扱っていた間は問題に
ならなかったが、別の領域の様式を流したところ、
臨床的意味の AI 下書きが「患者識別番号」「診療科名」「初診日」「死亡日」に**先の領域の
定義文**を写した。索引にはその領域の規約しか無く、項目名が似ていればそれが典拠として
選ばれる。それがそのまま所在探索のクエリと手順書へ流れた。

## 規則

`site/reference_scopes.yaml` に、様式の `url`（**完全一致**）と典拠にしてよい文書の
`path`（索引の `file` 単位の `payload.path`）を列挙する。

    scopes:
      - name: 感染症登録
        questionnaires:
          - http://kawasemi.example/Questionnaire/e2e-infection
        references:
          - registry_manual.pdf

- **ファイルが無ければ範囲を設けない**（従来どおり全文書が候補）。デモと CI はこれで動く
- **ファイルがあれば、載っていない様式には典拠を出さない**（空の範囲）。典拠の無い様式は
  「典拠なし」として人が書く。別の領域の定義を写すより、何も出さない方を選ぶ
- 前方一致にしないのは、URL が領域を表さない様式があるためである（例:
  `http://example.com/fhir/Questionnaire/sample` のような汎用の URL）
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml

from kawasemi.ai_input.resource_index.schema import ResourceUnit
from kawasemi.core.config import settings
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)

_lock = threading.Lock()
# パス → (更新時刻, 表)。**更新時刻で読み直す**——サーバを起動したあとにファイルを置いたり
# 編集したりしても、再起動なしで効く（置く前の「範囲なし」を握り続けると、開いたまま通る）
_cache: dict[str, tuple[float | None, dict[str, frozenset[str]] | None]] = {}


class ReferenceScopeError(ValueError):
    """`reference_scopes.yaml` の形が正しくない（どこが悪いかを文言に含める）。"""


def _str_list(value: Any, where: str) -> list[str]:
    # 文字列をそのまま集合にすると1文字ずつに割れる（`references: registry_manual.pdf` が
    # {'2','0','1',…} になり、典拠が黙って0件になる）。リストでなければ止める
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ReferenceScopeError(
            f"{where} は文字列のリストで書く（例: [registry_manual.pdf]）: {value!r}"
        )
    return value


def _parse(p: Path) -> dict[str, frozenset[str]]:
    try:
        raw: Any = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ReferenceScopeError(f"{p} を YAML として読めない: {e}") from e
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or not isinstance(raw.get("scopes", []), list):
        raise ReferenceScopeError(f"{p} は `scopes:` のリストを持つ形で書く")
    table: dict[str, set[str]] = {}
    for n, scope in enumerate(raw.get("scopes") or [], start=1):
        if not isinstance(scope, dict):
            raise ReferenceScopeError(f"{p} の scopes の{n}件目が対応表の形でない: {scope!r}")
        refs = set(_str_list(scope.get("references", []), f"{p} の scopes[{n}].references"))
        for url in _str_list(scope.get("questionnaires", []), f"{p} の scopes[{n}].questionnaires"):
            table.setdefault(url, set()).update(refs)
    logger.info(f"典拠の範囲を読み込んだ: 様式{len(table)}件 ({p})")
    return {k: frozenset(v) for k, v in table.items()}


def _load(path: str) -> dict[str, frozenset[str]] | None:
    """`{様式の url: 典拠にしてよい path の集合}`。ファイルが無ければ None。"""
    p = Path(path)
    mtime = p.stat().st_mtime if p.is_file() else None
    cached = _cache.get(path)
    if cached is not None and cached[0] == mtime:
        return cached[1]
    if mtime is None:
        logger.warning(
            f"典拠の範囲 {p.resolve()} が無いため、様式によらず全文書を典拠候補にします。"
            "複数の領域の様式を扱うなら作成してください（雛形: site.example/reference_scopes.yaml）"
        )
        table = None
    else:
        table = _parse(p)  # 形が悪ければ例外（キャッシュしない＝直せば次で効く）
    _cache[path] = (mtime, table)
    return table


def reference_paths_for(
    questionnaire_url: str | None, path: str | None = None
) -> frozenset[str] | None:
    """様式に許す典拠の `path` の集合。**None は「範囲を設けない」**（設定ファイルが無い）。

    設定があって様式が載っていなければ空集合を返す（典拠を出さない）。`url` が無い様式も空。
    """
    key = str(path or settings.reference_scopes_file)
    with _lock:
        table = _load(key)
    if table is None:
        return None
    return table.get(questionnaire_url or "", frozenset())


def in_reference_scope(unit: ResourceUnit, paths: frozenset[str] | None) -> bool:
    """ユニットが範囲内の文書か。`paths` が None なら常に真。"""
    if paths is None:
        return True
    return str(unit.payload.get("path") or "") in paths


def reset_reference_scope_cache() -> None:
    """キャッシュをクリアする（テスト用 / 差し替え後の再ロード用）。"""
    with _lock:
        _cache.clear()
