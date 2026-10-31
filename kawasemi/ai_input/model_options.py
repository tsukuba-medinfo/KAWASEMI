"""モデルごとの options の例外（`site/model_options.yaml`）を読む。

一部のモデルは、長い入力を既定のバッチ幅で処理させると推論サーバが落ちることがある
（`CUDA error: an illegal memory access` で HTTP 500）。落ちるかどうかはプロンプトの中身
ではなく長さで決まり、`num_batch` を下げると回避できる。どのモデルが落ちるかは使う
モデルと推論サーバの組み合わせで決まるので、コードには持たず施設の設定に置く。

## 設定（`site/model_options.yaml`）

`settings.model_options_file`（既定 `<site_dir>/model_options.yaml`）に、モデル名
（推論サーバに渡す名前の**完全一致**）→ 足す options を書く:

    models:
      "example-model:7b":
        num_batch: 512

- **ファイルが無ければ空**（どのモデルにも何も足さない）。デモと CI はこれで動く
- 形が正しくなければ例外にする（黙って空に落とすと、例外が効いていないことに気づけない）
- 足した options は呼び出し側が渡した options を**上書きしない**
  （`llm_json.with_model_overrides`）
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import yaml

from kawasemi.core.config import settings
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)


class ModelOptionsError(ValueError):
    """`model_options.yaml` の形が正しくない（どこが悪いかを文言に含める）。"""


_lock = threading.Lock()
# パス → (更新時刻, 表)。更新時刻で読み直すので、ファイルを置いたら再起動なしで効く
_cache: dict[str, tuple[float | None, dict[str, dict[str, Any]]]] = {}


def _parse(p: Path) -> dict[str, dict[str, Any]]:
    try:
        raw: Any = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise ModelOptionsError(f"{p} を YAML として読めない: {e}") from e
    if raw is None:
        return {}
    if not isinstance(raw, dict) or set(raw) - {"models"}:
        raise ModelOptionsError(f"{p} は `models:`（モデル名 → 足す options）の対応表で書く")
    models = raw.get("models") or {}
    if not isinstance(models, dict):
        raise ModelOptionsError(f"{p} の models はモデル名 → options の対応表で書く")
    table: dict[str, dict[str, Any]] = {}
    for model, options in models.items():
        if not isinstance(model, str) or not model:
            raise ModelOptionsError(f"{p} のモデル名は空でない文字列で書く: {model!r}")
        where = f"{p} の models[{model!r}]"
        if not isinstance(options, dict) or not options:
            raise ModelOptionsError(f"{where} は options 名 → 値の空でない対応表で書く")
        for key, value in options.items():
            if not isinstance(key, str) or not key:
                raise ModelOptionsError(f"{where} の options 名は空でない文字列で書く: {key!r}")
            if not isinstance(value, (bool, int, float, str)):
                raise ModelOptionsError(
                    f"{where}.{key} の値は数値・文字列・真偽値で書く: {value!r}"
                )
        table[model] = dict(options)
    logger.info(f"モデルごとの options の例外を読み込んだ: {len(table)}件 ({p})")
    return table


def load_model_options(path: str | None = None) -> dict[str, dict[str, Any]]:
    """モデルごとの options の例外（モデル名 → 足す options）を読む。ファイルが無ければ空。"""
    key = str(path or settings.model_options_file)
    p = Path(key)
    with _lock:
        mtime = p.stat().st_mtime if p.is_file() else None
        cached = _cache.get(key)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        table = {} if mtime is None else _parse(p)
        _cache[key] = (mtime, table)
        return table
