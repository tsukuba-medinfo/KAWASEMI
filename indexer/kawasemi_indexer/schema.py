"""リソース索引の出力契約スキーマ（indexer ↔ KAWASEMI の境界）

索引ディレクトリのレイアウト:
    <index_dir>/
        db_index.jsonl       # kind=db のユニット（1行1ユニット）
        file_index.jsonl     # kind=file のユニット（任意）
        fhir_index.jsonl     # kind=fhir のユニット（任意）
        embeddings.npy       # 全ユニットのベクトル（float32, 行=全jsonlの連結順）
        build_info.json      # スキーマ／embeddingモデル等のメタ

各 jsonl の1行は ResourceUnit。embeddings.npy の行は、kind順（Kind.ORDER）に
各 *_index.jsonl を読み込んだ連結順と1対1対応する。検索側はこの順序規約に依存する。
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# 索引スキーマのバージョン。非互換変更時に上げる。KAWASEMI 起動時に build_info の
# schema_version と照合し、非互換なら検索を縮退（候補ゼロ扱い）する。
# v2: kind=master（大規模臨床マスタを 1エントリ=1ユニットで索引化）を追加。
SCHEMA_VERSION = 2

# 索引ディレクトリ内の固定ファイル名
EMBEDDINGS_FILENAME = "embeddings.npy"
BUILD_INFO_FILENAME = "build_info.json"
# クエリ書き換え用の標準コード変換テーブル（任意・クエリ側成果物。CONTRACT §1）。
# embedding 契約に非依存な付加物のため schema_version は上げず、存在すれば使う運用。
SYNONYM_INDEX_FILENAME = "synonym_index.jsonl"
# 参照リンク（列→参照先マスタ）テーブル（任意・クエリ側成果物。RRF 第3チャネル用）。
# synonym_index と同じく embedding 契約に非依存な付加物（存在すれば使う運用）。
REF_LINKS_FILENAME = "ref_links.jsonl"


class Kind:
    """リソース種別。索引ファイル名と embeddings.npy の連結順を規定する。"""

    DB = "db"
    FILE = "file"
    FHIR = "fhir"
    MASTER = "master"  # マスタ（参照表/コード表）の 1エントリ=1ユニット（病名/検査/薬剤等）

    # embeddings.npy の行連結順（決定的にするため固定）。新 kind は末尾に足し、
    # 既存 kind の行オフセットを保つ（MASTER は v2 で末尾追加）。
    ORDER = (DB, FILE, FHIR, MASTER)

    INDEX_FILENAMES: dict[str, str] = {
        DB: "db_index.jsonl",
        FILE: "file_index.jsonl",
        FHIR: "fhir_index.jsonl",
        MASTER: "master_index.jsonl",
    }

    LABELS: dict[str, str] = {
        DB: "DB",
        FILE: "ファイル",
        FHIR: "FHIR",
        MASTER: "マスタ",
    }


class ResourceUnit(BaseModel):
    """索引の1ユニット（アドレス可能な最小単位）

    薄い共通ヘッダ + kind固有 payload。共通ヘッダで kind 横断の検索・提示を行い、
    location文字列・DSL op引数の決定的組み立ては payload から kind 別に行う。
    """

    unit_id: str = Field(description="ユニットの一意キー（例 'db:DEMO.LAB.BLAST_PCT'）")
    kind: str = Field(description="db / file / fhir")
    label: str = Field(description="人間可読の短い名称（候補リスト表示用）")
    search_text: str = Field(description="embedding 対象テキスト（検索のための語彙）")
    snippet: str = Field(default="", description="LLM/人へ見せる抜粋（妥当性判断の材料）")
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description="kind固有情報。connection_id を含み、location/op引数の素材になる",
    )


class BuildInfo(BaseModel):
    """索引のメタ情報（build_info.json）

    KAWASEMI 起動時に embedding_model / dim を設定側と照合し、不一致なら検索を
    無効化（候補ゼロ縮退）してベクトル空間のズレによる誤検索を防ぐ。
    """

    schema_version: int = SCHEMA_VERSION
    indexer_version: str = "0.0.0"
    embedding_model: str = Field(description="索引生成に使った embedding モデル名")
    # **モデル名だけでは重みの同一性を保証できない**。索引ビルド時の Ollama
    # `/api/tags` の digest。**省略可能**（既存の索引・digest を取れなかったビルドは None）。
    # None は「未検証」で、検索は無効化しない。schema_version は上げない（付加的な欄）。
    embedding_digest: str | None = Field(
        default=None, description="索引生成に使った embedding モデルのダイジェスト（未検証は None）"
    )
    dim: int = Field(description="ベクトル次元")
    normalized: bool = Field(default=True, description="ベクトルがL2正規化済みか")
    unit_count: int = Field(default=0, description="全kind合計のユニット数")
    kind_counts: dict[str, int] = Field(default_factory=dict, description="kindごとのユニット数")
    built_at: str | None = Field(default=None, description="生成時刻（ISO8601）")


class ResourceHit(BaseModel):
    """検索結果の1件（ResourceUnit + スコア + 提示用の連番）

    cand_id は LLM へ提示するときの 0 始まり連番。LLM はこの cand_id を選ぶだけで、
    location文字列・source_category・op引数は呼び出し側が unit.payload から
    決定的に導出する（カラム名等の捏造を構造的に防ぐ）。
    """

    cand_id: int
    unit: ResourceUnit
    score: float = Field(description="意味的類似度（cosine）。ハイブリッド時も表示スコアはこれ")
    # --- ハイブリッド検索（字面チャネル＋RRF）の付随情報。純 embedding 検索では None。 ---
    match_type: str | None = Field(
        default=None, description="lexical / semantic / both（字面と意味のどちらで当たったか）"
    )
    lexical_score: float | None = Field(
        default=None, description="字面一致スコア（0..1）。1.0=完全一致。字面ヒット無しは None"
    )
    rrf_score: float | None = Field(
        default=None, description="RRF 融合スコア（順位付けの実キー）。純 embedding では None"
    )
    ref_status: str | None = Field(
        default=None,
        description="参照リンクチャネルで押し上げられた場合のリンク確度"
        "（confirmed / rule / inferred）。非該当は None",
    )
