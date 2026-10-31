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
from collections.abc import Callable
from pathlib import Path

import numpy as np

from kawasemi.ai_input.resource_index.column_fill import (
    ColumnFillIndex,
)
from kawasemi.ai_input.resource_index.derive import (
    is_citable_reference_unit,
    is_reference_unit,
)
from kawasemi.ai_input.resource_index.embedder import l2_normalize
from kawasemi.ai_input.resource_index.schema import (
    BUILD_INFO_FILENAME,
    EMBEDDINGS_FILENAME,
    SCHEMA_VERSION,
    BuildInfo,
    Kind,
    ResourceHit,
    ResourceUnit,
)
from kawasemi.core.config import settings
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)


class ResourceIndexStore:
    """索引ディレクトリから構築するインメモリのベクトル検索ストア"""

    def __init__(self) -> None:
        self._units: list[ResourceUnit] = []
        self._matrix: np.ndarray | None = None  # (N, dim) 正規化済み
        self._build_info: BuildInfo | None = None
        # **記録済みの build_info との照合はパース後ではなく生の内容と比べる**。`BuildInfo` は
        # 既定値を持ち、pydantic の既定は `extra="ignore"` なので、パース後を見ると
        # フィールドの欠落も増加も消える。
        self._raw_build_info: dict[str, object] | None = None
        self._reference_units: int = 0
        # 表のベクトル（列ベクトルの重心）。遅延構築なので None は「未構築」。
        self._table_keys: list[tuple[str, str]] = []
        self._table_matrix: np.ndarray | None = None
        self._citable_units: int = 0
        # `unit_id` の実在照合用。**遅延構築**で、ロード後は不変。
        self._by_unit_id: dict[str, ResourceUnit] | None = None
        # 列の充填状況（任意成果物）。未配置なら空で、足切りをしない。
        self.column_fill: ColumnFillIndex = ColumnFillIndex()
        # label ごとの出現回数。**遅延構築**で、ロード後は不変。
        self._label_counts: dict[str, int] | None = None
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

    def get_unit(self, unit_id: str) -> ResourceUnit | None:
        """`unit_id` が指すユニットを返す（**無ければ None**）。

        保存経路はこのユニットの `payload` を**そのまま採る**（クライアントの自己申告を採らない）。
        AI 経路は索引由来の payload を持っているので、人手入力を同じ位置で同じ規則に揃えるための
        入口である。判定の条件は :meth:`has_unit` と同じ（そちらの docstring を参照）。
        """
        if not self.is_loaded or not self.schema_compatible:
            return None
        if not isinstance(unit_id, str):
            return None
        key = unit_id.strip()
        if not key:
            return None
        if self._by_unit_id is None:
            # 数万ユニットを毎回走査しないよう1度だけ辞書を作る（ロード後は不変）。
            # **先に現れたものを採る**（同じ `unit_id` が2件ある索引は壊れているが、
            # そのとき黙って後のものへ入れ替わらないようにする）。
            index: dict[str, ResourceUnit] = {}
            for unit in self._units:
                index.setdefault(unit.unit_id, unit)
            self._by_unit_id = index
        return self._by_unit_id.get(key)

    def has_unit(self, unit_id: str) -> bool:
        """`unit_id` が**この索引に実在する**か（**フェイルクローズ**）。

        典拠の `unit_id` は UI の自由入力欄から書けるので、形が索引のアドレスに一致していても
        索引を指しているとは限らない（`file:患者 山田太郎:0-0` のような捏造が書ける）。
        配布可否と典拠の付与率の分子はこの実在を根拠にする。

        **索引がロードできていない／schema が非互換のときは偽を返す**（「照合できない」を
        「実在する」に倒さない。`is_searchable` と同じ向きだが、**embedding モデルの一致は
        要求しない**——実在の照合はベクトルを使わないので、モデルが違っても答えは同じである。
        ここでモデル一致まで要求すると、検索が使えない施設で**人手入力の典拠だけが黙って
        配布不可になる**）。

        引数は前後の空白を落として比較する（保存経路が strip して持つのと揃える）。
        """
        return self.get_unit(unit_id) is not None

    @property
    def build_info(self) -> BuildInfo | None:
        return self._build_info

    @property
    def embedding_model(self) -> str | None:
        return self._build_info.embedding_model if self._build_info else None

    @property
    def reference_unit_count(self) -> int:
        """規約文書のユニット数（`is_reference_unit` が真のもの）。

        `build_info.json` の `kind_counts.file` は `file_role` の内訳を持たないので、
        規約文書が患者データ所在の宣言に入れ替わっても件数では気づけない。
        """
        return self._reference_units

    @property
    def citable_unit_count(self) -> int:
        """**典拠候補として提示される**ユニット数（`is_citable_reference_unit` が真のもの）。

        目次・項目一覧を除いた本文である。規約文書の件数が合っていても、
        目次の除外が効かなくなれば典拠の付与率の分子は動く。
        """
        return self._citable_units

    @property
    def embedding_digest(self) -> str | None:
        """索引ビルド時の embedding モデルのダイジェスト（未検証・未ロードは None）。"""
        return self._build_info.embedding_digest if self._build_info else None

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
        # 任意成果物。embedding 契約に依存しないので、索引が読めなくても読んでおく
        # （無ければ空で、所在候補の足切りをしないだけ）。
        store.column_fill = ColumnFillIndex.load(base)

        info_path = base / BUILD_INFO_FILENAME
        emb_path = base / EMBEDDINGS_FILENAME
        if not info_path.exists() or not emb_path.exists():
            logger.info(f"リソース索引未配置: {base}（検索は候補ゼロ扱い）")
            return store

        try:
            raw = json.loads(info_path.read_text("utf-8"))
            store._build_info = BuildInfo(**raw)
            # 記録済みの build_info との照合は生の内容と比べる。dict でなければ照合不能。
            # **`TypeError` を捕まえるのは、dict でない JSON で `**raw` の展開が失敗するため。**
            # 索引の異常はすべて候補ゼロへの縮退で扱う設計なのに、ここだけ呼び出し元へ抜けていた。
            # なお失敗時は `cls()` を返すので、**この代入の位置に意味は無い**。
            store._raw_build_info = raw if isinstance(raw, dict) else None
        except (OSError, TypeError, ValueError) as e:
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
        # 典拠候補になりうるユニットを数える（`kind_counts` は `file_role` の
        # 内訳を持たないので、ここで述語そのものに通した実数を持つ）
        store._reference_units = sum(1 for u in units if is_reference_unit(u))
        # 典拠候補として提示される件数（目次・項目一覧を除いた本文）
        store._citable_units = sum(1 for u in units if is_citable_reference_unit(u))
        logger.info(
            f"リソース索引ロード完了: {len(units)}ユニット"
            f"（うち規約文書{store._reference_units}・典拠候補{store._citable_units}）, "
            f"model={store._build_info.embedding_model}, dim={store._build_info.dim}"
        )
        return store

    # --- 表のベクトル ---------------------------------------

    @staticmethod
    def table_key(unit: ResourceUnit) -> tuple[str, str] | None:
        """ユニットが属する表のキー。表を持たない種別は None を返す。

        表を持つのは `kind=db`（カラム）だけである。`file` / `fhir` / `master` は
        表の2段階検索の対象外で、第1段で落とさない（所在の候補としては残す）。
        """
        if unit.kind != Kind.DB:
            return None
        conn = unit.payload.get("connection_id")
        tbl = unit.payload.get("db_qualified")
        if not conn or not tbl:
            return None
        return (str(conn), str(tbl))

    def label_count(self, label: str | None) -> int:
        """同じ `label` を持つユニットが索引にいくつあるか。

        **定型列を見分けるための数字である。** 施設コードや診療科コードのような列は、
        多くの表に同じ物理名と同じ論理名で入っており、索引に数百回出る。一方、固有の列の
        label の多くは1回しか出ない。

        呼び出し側はこの値で候補を下げる（`resource_drafter._DF_PENALTY`）。**索引に
        持たせるのは数え方だけ**で、どれだけ下げるかは探索側が決める。

        遅延構築で、ロード後は不変。
        """
        if self._label_counts is None:
            counts: dict[str, int] = {}
            for unit in self._units:
                key = unit.label or ""
                counts[key] = counts.get(key, 0) + 1
            self._label_counts = counts
        return self._label_counts.get(label or "", 1) or 1

    def _ensure_table_matrix(self) -> None:
        """表のベクトル（列ベクトルの重心）を作る。**索引は作り直さない**。

        `embeddings.npy` は L2 正規化済みなので、表に属する列ベクトルの平均を取って
        再正規化すれば「その表が何を記録する表か」の表現になる。**既存の索引から
        決定的に導出される**ので、索引の同一性も `build_info` も動かない。

        列の名前ではなく中身の重心なので、名前だけ似ている表（例: 名前に領域語を含むが
        列は年・月・日しかない採番管理の表）と、中身が合う表が分かれる。
        """
        if self._table_matrix is not None or self._matrix is None:
            return
        buckets: dict[tuple[str, str], list[int]] = {}
        for i, unit in enumerate(self._units):
            key = self.table_key(unit)
            if key is not None:
                buckets.setdefault(key, []).append(i)
        if not buckets:
            self._table_keys = []
            self._table_matrix = np.zeros((0, self._matrix.shape[1]), dtype=np.float32)
            return
        keys = sorted(buckets)
        mat = np.vstack([self._matrix[buckets[k]].mean(axis=0) for k in keys])
        # 平均は正規化を壊すので取り直す（cosine を行列積で計算する前提を保つ）。
        norms = np.linalg.norm(mat, axis=1, keepdims=True)
        norms[norms == 0] = 1.0
        self._table_keys = keys
        self._table_matrix = (mat / norms).astype(np.float32)
        logger.info(f"表のベクトルを構築しました: {len(keys)}表（列ベクトルの重心）")

    def search_tables(self, query_vector: np.ndarray, top_n: int) -> list[tuple[str, str]]:
        """クエリに近い表を上位 ``top_n`` 件返す（第1段）。

        索引が読めていない・表が1つも無いときは空を返す。**呼び出し側は空を
        「絞り込まない」と解釈すること**（第1段が効かないときに列が到達不能に
        なってはいけない）。
        """
        if not self.is_loaded or self._matrix is None or top_n <= 0:
            return []
        self._ensure_table_matrix()
        if self._table_matrix is None or self._table_matrix.shape[0] == 0:
            return []
        q = l2_normalize(query_vector).reshape(-1)
        if q.shape[0] != self._table_matrix.shape[1]:
            return []
        scores = self._table_matrix @ q
        n = min(top_n, scores.shape[0])
        idx = np.argpartition(-scores, n - 1)[:n]
        idx = idx[np.argsort(-scores[idx])]
        return [self._table_keys[int(i)] for i in idx]

    def search(
        self,
        query_vector: np.ndarray,
        top_k: int = 8,
        min_score: float = 0.0,
        predicate: Callable[[ResourceUnit], bool] | None = None,
    ) -> list[ResourceHit]:
        """正規化済みクエリベクトルに対し cosine 上位 top_k を返す（kind横断）。

        min_score 未満の候補は除外する（該当リソースが無い項目に低類似のノイズを
        LLM へ渡さないための足切り）。除外後に cand_id を 0 から振り直す。

        predicate を与えると、それが False を返すユニットを除外する。
        臨床的意味の典拠探索は規約文書（file_role=reference）だけを対象にするので、
        呼び出し側が対象集合を絞れるようにしてある。**既定は None で、所在探索の
        既存挙動（kind横断・全ユニット）は変わらない。**
        絞り込みは cand_id を振る前に行うため、LLM へ渡す cand_id は常に
        「提示した候補の連番」になる。
        """
        if not self.is_loaded or self._matrix is None:
            return []
        q = l2_normalize(query_vector).reshape(-1)
        if q.shape[0] != self._matrix.shape[1]:
            logger.warning(f"クエリ次元 {q.shape[0]} が索引次元 {self._matrix.shape[1]} と不一致")
            return []
        scores = self._matrix @ q
        # predicate があるときは、絞り込み後に top_k 件を確保できるよう全件を降順で見る
        # （部分ソートで上位kだけ取ると、対象種別のユニットが k 件に届かないことがある）。
        if predicate is None:
            k = min(top_k, scores.shape[0])
            # 上位kだけ部分ソート → 降順整列
            top_idx = np.argpartition(-scores, k - 1)[:k]
            top_idx = top_idx[np.argsort(-scores[top_idx])]
        else:
            top_idx = np.argsort(-scores)
        hits: list[ResourceHit] = []
        for idx in top_idx:
            if len(hits) >= top_k:
                break
            score = float(scores[idx])
            if score < min_score:
                # 足切り。**降順で見ているので以降も該当しない。** predicate 経路は
                # 全ユニットを走査しうるので、ここで打ち切らないと足切り済みの
                # 全件に対して predicate を呼ぶことになる。
                break
            if predicate is not None and not predicate(self._units[int(idx)]):
                continue
            hits.append(
                ResourceHit(
                    cand_id=len(hits),
                    unit=self._units[int(idx)],
                    score=round(score, 4),
                )
            )
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
