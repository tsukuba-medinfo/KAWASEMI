"""indexer 自前の最小設定（KAWASEMI 本体 kawasemi.core.config の代替）。

indexer は `kawasemi` パッケージに依存しないので、`kawasemi.core.config` を使わずに
済むよう最小限を自前で持つ。field 名（ollama_*, embedding_model, *_dir, *_file 等）は
本体の設定とそろえてある。

WebUI 固有の秘密（SECRET_KEY/CSRF/DATABASE_URL/FHIR_SERVER_URL/ALLOWED_HOSTS）は
持ち込まない。接続の DSN 等は connections.yaml の `secret_ref: env:` で解決する。

embedding_model は **契約クリティカル**。クエリ側（WebUI）と必ず一致させること
（CONTRACT.md / build_info.json の互換チェック参照）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # ロギング
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = Field(default="INFO")

    # Ollama（embedding 生成 ＋ LLM ドラフタ）
    ollama_base_url: str = Field(default="http://localhost:11434")
    ollama_model: str = Field(default="qwen3.8:27b", description="LLM ドラフタ用モデル")
    ollama_vision_model: str = Field(
        default="",
        description=(
            "手書き/スキャンPDF読取用。空なら ollama_model を流用する"
            "（ollama_model が画像を受け付けるとは限らないので、画像入力に対応したモデルを指定する）"
        ),
    )
    ollama_timeout: int = Field(default=120)

    # embedding モデル（契約クリティカル・クエリ側と一致必須）
    embedding_model: str = Field(default="bge-m3")
    # embedding だけ別の Ollama へ向ける。空なら ollama_base_url を流用する
    embedding_base_url: str = Field(default="")

    # 入出力パス。空のままにすると `site_dir`（施設固有の情報）と `var_dir`（実行で
    # 生まれるもの）から決める（本体の設定と同じ規則）。
    site_dir: str = Field(default="site")
    var_dir: str = Field(default="var")
    db_definition_dir: str = Field(default="", description="DB定義書コーパス")
    resource_index_dir: str = Field(default="", description="索引の出力先")
    connections_file: str = Field(default="")
    key_types_file: str = Field(default="")
    indexer_rules_file: str = Field(
        default="", description="施設の索引規則（indexer_rules.yaml）。無ければ施設に依らない既定"
    )

    # search サブコマンドの足切り閾値（点検用）
    resource_index_min_score: float = Field(default=0.5)

    @model_validator(mode="after")
    def _fill_default_paths(self) -> Settings:
        """空のパスを `site_dir` / `var_dir` から決める（明示した値はそのまま使う）。"""
        site, var = Path(self.site_dir), Path(self.var_dir)
        defaults = {
            "db_definition_dir": site / "db_definition",
            "connections_file": site / "connections.yaml",
            "key_types_file": site / "key_types.yaml",
            "indexer_rules_file": site / "indexer_rules.yaml",
            "resource_index_dir": var / "resource_index",
        }
        for name, path in defaults.items():
            if not getattr(self, name):
                setattr(self, name, path.as_posix())
        return self

    @property
    def embedding_host(self) -> str:
        """embedding の接続先（`embedding_base_url` が空なら `ollama_base_url`）"""
        return self.embedding_base_url.strip() or self.ollama_base_url


settings = Settings()
