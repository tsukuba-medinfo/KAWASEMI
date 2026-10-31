"""接続レジストリ connections.yaml のロード（3層分離の(1)接続層）

connection_id → 外部到達手段・認証・scope を保持する。索引(2層)は connection_id を
参照するだけで接続詳細・秘匿情報を持たない。施設は connections.yaml と secret を
差し替えるだけで利用できる。

設計:
- secret は env/金庫参照（secret_ref）。リポジトリに生値を置かない。
- scope: self|external。db の source_category を local_db / external_db に振り分ける。
- 実際の接続確立・実行はステップ4（executor の _op_external_db / _op_file）で実装済み。
  本モジュールが提供するのは scope/kind と dsn/root_path（secret_ref 解決）の参照。

connections.yaml の形:
    connections:
      dwh:        {kind: db,   scope: external, driver: postgres, host: ..., port: 5432, secret_ref: env:DWH_DSN}
      app_db:     {kind: db,   scope: self}
      hosp_fhir:  {kind: fhir, scope: external, base_url: https://...}
      report_fs:  {kind: file, scope: external, root_path: /mnt/reports, key_type: report_key}

患者キーの宣言（コンパイラが step の key_type / physical_format へ焼き込む）:
    key_type     file / csv / fhir の接続で `{{patient_id}}` を埋める名前空間（DB は索引の
                 payload.key_type が列ごとに決めるので使わない）
    key_formats  その接続が key_type ごとに値を格納している物理形。例 `{mrn: {pad: 10}}`
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

import yaml

from kawasemi.core.config import settings
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)

SCOPE_SELF = "self"
SCOPE_EXTERNAL = "external"


def resolve_secret(ref: str | None) -> str | None:
    """secret_ref を実値へ解決する。

    - `env:VAR` → 環境変数 VAR の値（未設定なら None）
    - それ以外（生値）→ そのまま返す（非推奨だがデモ・後方互換のため許容）
    - None/空 → None
    """
    if not ref:
        return None
    if ref.startswith("env:"):
        return os.getenv(ref[len("env:") :]) or None
    return ref


class ConnectionRegistry:
    """connection_id → 接続情報(dict) のレジストリ。未配置なら空。"""

    def __init__(self, connections: dict[str, dict[str, Any]] | None = None) -> None:
        self._connections: dict[str, dict[str, Any]] = connections or {}
        self.source_path: Path | None = None

    @property
    def is_loaded(self) -> bool:
        return bool(self._connections)

    def get(self, connection_id: str | None) -> dict[str, Any] | None:
        if not connection_id:
            return None
        return self._connections.get(connection_id)

    def scope_of(self, connection_id: str | None) -> str | None:
        """接続の scope（self/external）。未登録なら None。"""
        conn = self.get(connection_id)
        if not conn:
            return None
        scope = conn.get("scope")
        return str(scope) if scope is not None else None

    def dsn_of(self, connection_id: str | None) -> str | None:
        """db 接続の DSN（SQLAlchemy URL）を secret_ref から解決する。

        設計（3層分離）どおり、DSN は connections.yaml に生値を置かず secret_ref で
        env/金庫を指す。`secret_ref: env:VAR` を解決して返す。未登録/未解決は None。
        """
        conn = self.get(connection_id)
        if not conn:
            return None
        return resolve_secret(conn.get("secret_ref"))

    def root_path_of(self, connection_id: str | None) -> str | None:
        """file 接続の root_path（外部ファイル格納のルート）を返す。未登録は None。"""
        conn = self.get(connection_id)
        if not conn:
            return None
        root = conn.get("root_path")
        return str(root) if root is not None else None

    def key_type_of(self, connection_id: str | None) -> str | None:
        """接続の既定の患者キー種別（`key_type`）。未登録・未宣言は None。

        file / csv / fhir の単位は索引に患者キー列を持たないので、どの名前空間の値で
        `{{patient_id}}` を埋めるかは接続が決める。DB は列ごとに索引の `payload.key_type`
        が決めるので、コンパイラはこの値を DB の step には使わない。
        """
        conn = self.get(connection_id)
        kt = conn.get("key_type") if conn else None
        return str(kt) if kt else None

    def key_format_of(
        self, connection_id: str | None, key_type: str | None
    ) -> dict[str, Any] | None:
        """接続がその患者キー種別を格納している物理形（`key_formats.<key_type>`）。

        例: `key_formats: {mrn: {pad: 10}}`（この接続では mrn を10桁ゼロ詰めで持つ）。
        DSL の `step.physical_format` にそのまま入る。宣言が無ければ None（正準形のまま）。
        """
        conn = self.get(connection_id)
        formats = conn.get("key_formats") if conn else None
        if not key_type or not isinstance(formats, dict):
            return None
        fmt = formats.get(key_type)
        return dict(fmt) if isinstance(fmt, dict) and fmt else None

    @classmethod
    def load(cls, path: str | Path) -> ConnectionRegistry:
        """connections.yaml を読み込む。未配置・壊れていれば空レジストリ。"""
        p = Path(path)
        registry = cls()
        registry.source_path = p
        if not p.exists():
            logger.info(
                f"接続レジストリ未配置: {p}"
                "（所在の source_category は db → local_db 既定。"
                "ただしステップ3の op 引数の決定的導出は scope を判定できないため行われず、"
                "db の取得 step はすべて needs_input になる）"
            )
            return registry
        try:
            data = yaml.safe_load(p.read_text("utf-8")) or {}
        except (OSError, yaml.YAMLError) as e:
            logger.error(f"接続レジストリ読み込みエラー: {p}: {e}")
            return cls()
        # トップレベル connections: マッピング、または直接マッピングの両方を許容
        conns = data.get("connections", data) if isinstance(data, dict) else {}
        if not isinstance(conns, dict):
            logger.error(f"接続レジストリの形式が不正です: {p}")
            return cls()
        registry._connections = {str(k): (v or {}) for k, v in conns.items()}
        logger.info(f"接続レジストリ読み込み完了: {len(registry._connections)}件 ({p})")
        return registry


# --- プロセス内シングルトン（遅延ロード） -----------------------------------

_lock = threading.Lock()
_cache: dict[str, ConnectionRegistry] = {}


def get_registry(path: str | None = None) -> ConnectionRegistry:
    """パス単位でキャッシュされた接続レジストリを返す（遅延ロード）。"""
    key = str(path or settings.connections_file)
    cached = _cache.get(key)
    if cached is not None:
        return cached
    with _lock:
        cached = _cache.get(key)
        if cached is None:
            cached = ConnectionRegistry.load(key)
            _cache[key] = cached
        return cached
