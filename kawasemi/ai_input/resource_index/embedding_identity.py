"""索引とクエリの embedding が**同じ重み**で作られたかの照合。

`build_info.embedding_model` と `ResourceIndexStore.is_searchable` が照合するのは**モデル名**
だけである。クエリ側の接続先は LLM と分けられる（`EMBEDDING_BASE_URL`）ようになり、
**索引を作り直さずにサーバだけ差し替えられる**。同名タグで重みが違うサーバへ向けても
「検索可能」と判定され、所在探索の再現率が**黙って**落ちる。そこで、索引ビルド時のダイジェスト
（`build_info.embedding_digest`）と、クエリに使う `embedding_host` が今返すダイジェストを照合する。

方針:

- **違えば検索を無効化して理由を出す。** 黙って劣化させない
- **取れなければ無効化しない（未検証として通す）。** 対象は (a) 索引にダイジェストが無い
  （この欄が増える前に作った索引、取得できなかったビルド）、(b) クエリ側のダイジェストが
  取れない（サーバが落ちている・一覧に無い・固まる）。どちらも「違う」の証拠ではなく、
  無効化すると既存の索引が一斉に使えなくなる。**未検証であることは理由として残す**
- **例外を投げず、固まらない。** 照合が生成そのものを壊してはならない。上限時間で戻る
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from kawasemi.ai_input.resource_index.store import ResourceIndexStore
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)

# クエリ側のダイジェストを取る上限時間（秒）。ローカルの一覧読み出しなので数秒で足りる。
# 超えたら理由つきの未検証にして生成へ進む（`llm_provider.DIGEST_TIMEOUT_SECONDS` と同じ考え方）。
DIGEST_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class EmbeddingIdentity:
    """照合結果。`compatible` が False のときだけ検索を無効化する。"""

    compatible: bool
    verified: bool  # 両側のダイジェストが取れて、一致を確かめた
    index_digest: str | None
    query_digest: str | None
    reason: str | None  # 不一致、または未検証の理由。一致を確かめたときは None


def compare_digests(index_digest: str | None, query_digest: str | None) -> EmbeddingIdentity:
    """ダイジェスト2つを比べる（純粋関数）。"""
    if index_digest is None:
        return EmbeddingIdentity(
            True,
            False,
            None,
            query_digest,
            "未検証: 索引が embedding のダイジェストを持たない"
            "（build_info.embedding_digest が無い。索引の作り直しで記録される）",
        )
    if query_digest is None:
        return EmbeddingIdentity(
            True,
            False,
            index_digest,
            None,
            "未検証: クエリ側（embedding_host）の embedding モデルのダイジェストを取得できない",
        )
    if index_digest != query_digest:
        return EmbeddingIdentity(
            False,
            False,
            index_digest,
            query_digest,
            "embedding モデルの重みが索引と違う"
            f"（索引 {_short(index_digest)} / クエリ側 {_short(query_digest)}）。"
            "同名タグでも別の重みでは検索の再現率が黙って落ちるため、検索を無効化する。"
            "索引を作り直すか、embedding_host を索引を作ったサーバへ戻すこと",
        )
    return EmbeddingIdentity(True, True, index_digest, query_digest, None)


def _short(digest: str) -> str:
    return digest[:12] + "…" if len(digest) > 12 else digest


def embedder_digest(embedder: Any) -> str | None:
    """embedder のダイジェスト（同期。CLI・測定スクリプト用）。無い・取れないときは None。

    `OllamaEmbedder.digest()` は例外を投げないが、`digest` を持たない embedder（テストの二重など）
    でも落ちないようにする。固まる形への上限時間は掛けない（呼び出し側は同期の道具で、
    `OllamaEmbedder` のクライアントのタイムアウトが効く）。
    """
    digest = getattr(embedder, "digest", None)
    if not callable(digest):
        return None
    try:
        value = digest()
    except Exception as e:  # noqa: BLE001 - 照合が道具を壊さない
        logger.warning(f"embedding のダイジェストを取得できない: {type(e).__name__}")
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


async def query_embedding_digest(embedder: Any) -> str | None:
    """クエリ側 embedder のダイジェスト。無い・取れない・固まるときは None。例外は投げない。"""
    digest = getattr(embedder, "digest", None)
    if not callable(digest):
        return None
    try:
        value = await asyncio.wait_for(asyncio.to_thread(digest), timeout=DIGEST_TIMEOUT_SECONDS)
    except Exception as e:  # noqa: BLE001 - 照合が生成を壊さない（TimeoutError を含む）
        logger.warning(f"embedding のダイジェストを取得できない: {type(e).__name__}")
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


async def check_embedding_identity(store: ResourceIndexStore, embedder: Any) -> EmbeddingIdentity:
    """索引とクエリの embedding が同じ重みか照合する。

    `store` は読み込み済みであること。未検証・不一致の理由はログへ出す（不一致は warning、
    未検証は info。未検証は既存の索引で常に起きるので、warning にするとログが埋まる）。
    """
    identity = compare_digests(store.embedding_digest, await query_embedding_digest(embedder))
    if not identity.compatible:
        logger.warning(f"リソース索引の embedding 照合: {identity.reason}")
    elif identity.reason:
        logger.info(f"リソース索引の embedding 照合: {identity.reason}")
    return identity
