"""リソース索引のロードと cosine 検索（KAWASEMI 読み取り側）

indexer が出力した <index_dir>（*_index.jsonl + embeddings.npy + build_info.json）を
読み込み、クエリベクトルとの cosine 類似で kind 横断 top-k を返す。

設計方針:
- ベクトルは正規化済み前提。cosine = 行列積（M @ q）で一括算出（faiss 非依存）。
- jsonl は Kind.ORDER 順に連結し、その行順が embeddings.npy の行に1対1対応する。
- build_info の schema_version 非互換／embedding_model 不一致は「検索無効化」
  （is_searchable=False → 候補ゼロ縮退）でベクトル空間ズレによる誤検索を防ぐ。
- 索引未配置でもエラーにせず空インデックスへ縮退する（AI案作成は候補ゼロ扱い）。
- プロセス内シングルトン（遅延ロード）。差し替え後は reset_store_cache()。
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import numpy as np

from kawasemi_indexer import lexical
from kawasemi_indexer.config import settings
from kawasemi_indexer.embedder import l2_normalize
from kawasemi_indexer.lexical import LexicalIndex
from kawasemi_indexer.logging_config import get_logger
from kawasemi_indexer.schema import (
    BUILD_INFO_FILENAME,
    EMBEDDINGS_FILENAME,
    SCHEMA_VERSION,
    BuildInfo,
    Kind,
    ResourceHit,
    ResourceUnit,
)

logger = get_logger(__name__)

# RRF（Reciprocal Rank Fusion）の平滑化定数。順位 r の寄与は 1/(K+r)。慣例値 60。
_RRF_K = 60
# ハイブリッド融合で意味順位から拾う候補プールの下限。字面ヒットは別途全件加える。
_HYBRID_POOL = 200


class ResourceIndexStore:
    """索引ディレクトリから構築するインメモリのベクトル検索ストア"""

    def __init__(self) -> None:
        self._units: list[ResourceUnit] = []
        self._matrix: np.ndarray | None = None  # (N, dim) 正規化済み
        self._lexical: LexicalIndex | None = None  # 字面一致チャネル（load 時に前計算）
        self._id_to_idx: dict[str, int] = {}  # unit_id → 行 index（参照リンクチャネル用）
        self._build_info: BuildInfo | None = None
        self.index_dir: Path | None = None
        self.schema_compatible: bool = True

    @property
    def is_loaded(self) -> bool:
        return bool(self._units) and self._matrix is not None

    @property
    def unit_count(self) -> int:
        return len(self._units)

    @property
    def units(self) -> list[ResourceUnit]:
        """全ユニット（KeyTypeDrafter 等の列挙用）。"""
        return list(self._units)

    @property
    def build_info(self) -> BuildInfo | None:
        return self._build_info

    @property
    def embedding_model(self) -> str | None:
        return self._build_info.embedding_model if self._build_info else None

    def matches_model(self, model: str) -> bool:
        """クエリ側 embedding モデルが索引生成時のモデルと一致するか。"""
        return self._build_info is not None and self._build_info.embedding_model == model

    def is_searchable(self, model: str) -> bool:
        """ロード済み・schema互換・モデル一致のときのみ検索可能。"""
        return self.is_loaded and self.schema_compatible and self.matches_model(model)

    @classmethod
    def load(cls, index_dir: str | Path) -> ResourceIndexStore:
        """索引ディレクトリをロードする。未配置・壊れていれば空ストアを返す。"""
        store = cls()
        base = Path(index_dir)
        store.index_dir = base

        info_path = base / BUILD_INFO_FILENAME
        emb_path = base / EMBEDDINGS_FILENAME
        if not info_path.exists() or not emb_path.exists():
            logger.info(f"リソース索引未配置: {base}（検索は候補ゼロ扱い）")
            return store

        try:
            store._build_info = BuildInfo(**json.loads(info_path.read_text("utf-8")))
        except (OSError, ValueError) as e:
            logger.error(f"build_info 読み込みエラー: {info_path}: {e}")
            return cls()

        if store._build_info.schema_version != SCHEMA_VERSION:
            logger.warning(
                f"リソース索引 schema_version 非互換: "
                f"索引={store._build_info.schema_version} / 期待={SCHEMA_VERSION}。"
                "検索を無効化します（再索引が必要）。"
            )
            store.schema_compatible = False
            return store

        units: list[ResourceUnit] = []
        for kind in Kind.ORDER:
            path = base / Kind.INDEX_FILENAMES[kind]
            if not path.exists():
                continue
            try:
                with path.open("r", encoding="utf-8") as fh:
                    for line in fh:
                        line = line.strip()
                        if line:
                            units.append(ResourceUnit(**json.loads(line)))
            except (OSError, ValueError) as e:
                logger.error(f"索引ファイル読み込みエラー: {path}: {e}")
                return cls()

        try:
            matrix = np.load(emb_path)
        except (OSError, ValueError) as e:
            logger.error(f"embeddings.npy 読み込みエラー: {emb_path}: {e}")
            return cls()

        if matrix.shape[0] != len(units):
            logger.error(
                f"索引整合性エラー: ユニット数={len(units)} と "
                f"ベクトル行数={matrix.shape[0]} が不一致。検索を無効化します。"
            )
            return cls()

        # 正規化済み前提だが安全のため再正規化（normalized=False の indexer も許容）
        store._matrix = l2_normalize(matrix) if len(units) else None
        store._units = units
        store._lexical = LexicalIndex(units)  # 字面チャネルの前計算（surface_forms）
        store._id_to_idx = {u.unit_id: i for i, u in enumerate(units)}
        logger.info(
            f"リソース索引ロード完了: {len(units)}ユニット, "
            f"model={store._build_info.embedding_model}, dim={store._build_info.dim}"
        )
        return store

    def search(
        self,
        query_vector: np.ndarray,
        top_k: int = 8,
        min_score: float = 0.0,
        query_text: str | None = None,
        lexical_terms: list[str] | None = None,
        ref_candidates: list[tuple[str, str]] | None = None,
    ) -> list[ResourceHit]:
        """クエリに対し上位 top_k を返す（kind横断）。

        query_text を渡すと **ハイブリッド検索**（字面一致チャネル ＋ embedding を
        RRF で融合）になり、完全一致が実質最上位へ来る。渡さなければ従来どおり
        embedding cosine のみ（後方互換）。

        lexical_terms を渡すと字面チャネルはその語群で照合する（クエリ書き換えで
        展開した別名・標準コード群）。未指定なら [query_text] を使う。

        ref_candidates を渡すと **参照リンクチャネル**（RRF 第3チャネル）が有効になる。
        (unit_id, status) の並び順がチャネル内順位（confirmed > rule > inferred は
        呼び出し側=RefLinkIndex.candidates_for が保証）で、順位 r の候補に 1/(K+r) を
        加算する。sort key (is_exact, rrf) は不変（完全一致の最優先は崩さない）。

        min_score 未満（cosine 基準）の候補は除外する（該当リソースが無い項目に低類似の
        ノイズを渡さない足切り）。除外後に cand_id を 0 から振り直す。
        """
        if not self.is_loaded or self._matrix is None:
            return []
        q = l2_normalize(query_vector).reshape(-1)
        if q.shape[0] != self._matrix.shape[1]:
            logger.warning(f"クエリ次元 {q.shape[0]} が索引次元 {self._matrix.shape[1]} と不一致")
            return []
        scores = self._matrix @ q
        terms = lexical_terms if lexical_terms else ([query_text] if query_text else [])
        terms = [t for t in terms if t]
        if (terms or ref_candidates) and self._lexical is not None and len(self._lexical):
            return self._search_hybrid(scores, terms, top_k, min_score, ref_candidates)
        return self._search_semantic(scores, top_k, min_score)

    def _search_semantic(
        self, scores: np.ndarray, top_k: int, min_score: float
    ) -> list[ResourceHit]:
        """embedding cosine 上位 top_k（従来動作）。match_type 等は付与しない。"""
        k = min(top_k, scores.shape[0])
        # 上位kだけ部分ソート → 降順整列
        top_idx = np.argpartition(-scores, k - 1)[:k]
        top_idx = top_idx[np.argsort(-scores[top_idx])]
        hits: list[ResourceHit] = []
        for idx in top_idx:
            score = float(scores[idx])
            if score < min_score:
                continue  # 足切り（降順なので以降も該当しないが明示的に継続）
            hits.append(
                ResourceHit(cand_id=len(hits), unit=self._units[int(idx)], score=round(score, 4))
            )
        return hits

    def _search_hybrid(
        self,
        scores: np.ndarray,
        terms: list[str],
        top_k: int,
        min_score: float,
        ref_candidates: list[tuple[str, str]] | None = None,
    ) -> list[ResourceHit]:
        """字面一致順位と embedding 順位（＋参照リンク順位）を RRF で融合する。

        融合スコア rrf = 1/(K+意味順位) + 1/(K+字面順位) + 1/(K+参照リンク順位)。
        字面/参照リンクのヒットが無い候補は該当項なし。完全一致（exact）は RRF に
        依らず最優先で並べる（sort key (is_exact, rrf) は不変）。
        terms は字面照合する語群（複数語ならユニット毎に最良一致を採る）。
        ref_candidates は (unit_id, status) の順位付きリスト（参照リンクチャネル）。
        """
        assert self._lexical is not None
        n = scores.shape[0]
        # 意味順位（全件降順）。sem_rank[i] = ユニット i の cosine 順位（0 始まり）。
        order = np.argsort(-scores)
        sem_rank = np.empty(n, dtype=np.int64)
        sem_rank[order] = np.arange(n)

        # 字面順位。スコア降順に並べ、同点は元の行順（安定ソート）。
        lex = sorted(self._lexical.scores(terms), key=lambda t: -t[1]) if terms else []
        lex_info: dict[int, tuple[int, float, str]] = {
            idx: (rank, lscore, kind) for rank, (idx, lscore, kind) in enumerate(lex)
        }

        # 参照リンク順位（並び順=チャネル内順位。confirmed > rule > inferred）。
        ref_info: dict[int, tuple[int, str]] = {}
        for rank, (uid, status) in enumerate(ref_candidates or []):
            idx = self._id_to_idx.get(uid)
            if idx is not None and idx not in ref_info:
                ref_info[idx] = (rank, status)

        # 候補プール = 意味上位 + 全字面ヒット + 全参照リンク候補
        # （低 cosine でも完全一致・リンク先の列を拾えるように）。
        pool_size = min(n, max(top_k * 5, _HYBRID_POOL))
        pool: set[int] = {int(i) for i in order[:pool_size]}
        pool.update(lex_info.keys())
        pool.update(ref_info.keys())

        ranked: list[tuple[int, float, bool, float | None, int, str | None]] = []
        for idx in pool:
            sr = int(sem_rank[idx])
            rrf = 1.0 / (_RRF_K + sr)
            lscore: float | None = None
            is_exact = False
            li = lex_info.get(idx)
            if li is not None:
                lrank, lscore, kind = li
                rrf += 1.0 / (_RRF_K + lrank)
                is_exact = kind == lexical.TIER_EXACT
            ref_status: str | None = None
            ri = ref_info.get(idx)
            if ri is not None:
                rrank, ref_status = ri
                rrf += 1.0 / (_RRF_K + rrank)
            ranked.append((idx, rrf, is_exact, lscore, sr, ref_status))

        # 完全一致を最上位に、その後 RRF 降順。
        ranked.sort(key=lambda t: (t[2], t[1]), reverse=True)

        hits: list[ResourceHit] = []
        for idx, rrf, _is_exact, lscore, sr, ref_status in ranked:
            cos = float(scores[idx])
            # 足切りは cosine 基準（純 embedding 時と同じ意味）。ただし完全一致と
            # 参照リンクで押し上げた列は免除する: これらは RRF で意図的に上位へ置いた
            # 候補であり（完全一致=最優先、参照リンク=意味的には近くない
            # 「値の格納先」列を出す施策）、cosine 足切りと目的が正面衝突するため。
            if cos < min_score and not _is_exact and ref_status is None:
                continue
            hits.append(
                ResourceHit(
                    cand_id=len(hits),
                    unit=self._units[idx],
                    score=round(cos, 4),
                    match_type=lexical.classify_match_type(lscore is not None, sr < top_k),
                    lexical_score=round(lscore, 4) if lscore is not None else None,
                    rrf_score=round(rrf, 6),
                    ref_status=ref_status,
                )
            )
            if len(hits) >= top_k:
                break
        return hits


# --- プロセス内シングルトン（遅延ロード） -----------------------------------

_store_lock = threading.Lock()
_store_cache: dict[str, ResourceIndexStore] = {}


def get_store(index_dir: str | None = None) -> ResourceIndexStore:
    """索引ディレクトリ単位でキャッシュされたストアを返す（遅延ロード）。"""
    key = str(index_dir or settings.resource_index_dir)
    cached = _store_cache.get(key)
    if cached is not None:
        return cached
    with _store_lock:
        cached = _store_cache.get(key)
        if cached is None:
            cached = ResourceIndexStore.load(key)
            _store_cache[key] = cached
        return cached


def reset_store_cache() -> None:
    """ストアキャッシュをクリアする（テスト用 / 索引差し替え後の再ロード用）。"""
    with _store_lock:
        _store_cache.clear()
