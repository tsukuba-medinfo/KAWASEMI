"""リソース索引（ステップ1役割2「データ所在」のembedding検索基盤）

外部接続を含むリソース（DB／ファイル／FHIR）の「所在カタログ」を、kind横断で
統一スキーマ（薄い共通ヘッダ + kind固有payload）として索引し、embeddingの
cosine検索でQuestionnaire項目に関連する候補を絞り込む。

3層分離:
1. 接続レジストリ（connections.yaml）= 外部到達手段・認証（別PR）。
2. リソース索引（本パッケージが読む indexer 出力）= 各リソースの中身カタログ。
3. LLM提示 = 検索hitの統一candidate射影（location_drafter が消費）。

索引は「地図」であり患者データを載せない。索引対象は患者データを含まないメタ／
共通知識のみ（DB定義書・FHIR臨床項目辞書・参照文書本文）。実際の患者値の取得は
ステップ4の実行時のみ行う。

indexer は出力契約（本パッケージの schema / ディレクトリレイアウト / build_info）に
従う別プログラムで、KAWASEMI は参照実装CLI（`kawasemi-indexer build`。`indexer/`）を同梱する。
"""

from kawasemi.ai_input.resource_index.schema import (
    SCHEMA_VERSION,
    BuildInfo,
    Kind,
    ResourceHit,
    ResourceUnit,
)
from kawasemi.ai_input.resource_index.store import (
    ResourceIndexStore,
    get_store,
    reset_store_cache,
)

__all__ = [
    "SCHEMA_VERSION",
    "BuildInfo",
    "Kind",
    "ResourceHit",
    "ResourceUnit",
    "ResourceIndexStore",
    "get_store",
    "reset_store_cache",
]
