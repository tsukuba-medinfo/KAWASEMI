"""DB定義書コーパスのロードとBM25検索（ステップ1役割2「AIによる案の作成」の検索層）

施設固有のDB定義書コーパス（`settings.db_definition_dir`）の `column_index.jsonl`
（全カラムを平坦化したインデックス）を読み込み、各カラムを1ドキュメントとする
軽量BM25検索を提供する。Questionnaire項目テキスト＋役割1アノテーションをクエリに、
関連カラム候補を上位 k 件返す。

設計方針:
- 外部で作った索引ファイル（pickle 等）は読まず、本モジュールで
  自前にBM25を構築する（pickle非依存・テスト可能）。
- 検索対象は v1 では `column_index.jsonl`（→ source_category=local_db）のみ。
- メモリ常駐シングルトン（遅延ロード）。コーパス未配置なら空インデックス（候補ゼロ）。
- トークナイザは「char-bigram + ascii-token, NFKC/lower」。

候補（ColumnHit）はLLMに渡され、LLMは cand_id を選ぶだけ。所在文字列の組み立ては
location_drafter 側で決定的に行う（カラム名の捏造を構造的に防ぐ）。
"""

from __future__ import annotations

import json
import math
import re
import threading
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kawasemi.core.config import settings
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)

# BM25 ハイパーパラメータ（一般的な既定値）
_BM25_K1 = 1.5
_BM25_B = 0.75

_ASCII_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def _tokenize(text: str) -> list[str]:
    """NFKC/lower 正規化のうえ ascii-token と非ascii文字のchar-bigramに分割する。

    例: "blast 芽球" -> ["blast", "芽球"] のbigram群 + ["blast"]
    日本語論理名は語境界が曖昧なため bigram で部分一致のリコールを確保する。
    """
    if not text:
        return []
    norm = unicodedata.normalize("NFKC", text).lower()
    tokens: list[str] = []
    # ascii 連続（英数_）は丸ごと1トークン
    tokens.extend(_ASCII_TOKEN_RE.findall(norm))
    # 非ascii（日本語等）の連続を取り出して char-bigram 化
    non_ascii_runs = re.findall(r"[^\x00-\x7f]+", norm)
    for run in non_ascii_runs:
        if len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


@dataclass
class ColumnHit:
    """検索でヒットした1カラム（LLMへ渡す候補 / location組み立ての素材）"""

    cand_id: int
    db_group: str | None
    db_qualified: str | None
    table_logical: str | None
    physical_name: str | None
    logical_name: str | None
    type_raw: str | None
    pk: bool
    notes: str | None
    score: float = 0.0

    def to_candidate(self) -> dict[str, Any]:
        """LLMプロンプト用の軽量dict（cand_id付き）"""
        return {
            "cand_id": self.cand_id,
            "db_group": self.db_group,
            "db_qualified": self.db_qualified,
            "table_logical": self.table_logical,
            "physical_name": self.physical_name,
            "logical_name": self.logical_name,
            "type": self.type_raw,
            "pk": self.pk,
            "notes": self.notes,
        }

    def build_location(self) -> str:
        """データ所在の定型文を決定的に組み立てる"""
        table = self.db_qualified or "（表不明）"
        if self.table_logical:
            table = f"{table}（{self.table_logical}）"
        column = self.physical_name or "（カラム不明）"
        if self.logical_name:
            column = f"{column}（{self.logical_name}）"
        db = self.db_group or "（DB不明）"
        return f"DB: {db} / 表: {table} / カラム: {column}"


@dataclass
class _Document:
    """BM25の1ドキュメント（=1カラム）"""

    column: dict[str, Any]
    tf: Counter[str] = field(default_factory=Counter)
    length: int = 0


class DbDefinitionIndex:
    """column_index.jsonl から構築するインメモリBM25インデックス"""

    def __init__(self) -> None:
        self._docs: list[_Document] = []
        self._df: Counter[str] = Counter()
        self._idf: dict[str, float] = {}
        self._avgdl: float = 0.0
        self.source_path: Path | None = None

    @property
    def is_loaded(self) -> bool:
        return bool(self._docs)

    @property
    def column_count(self) -> int:
        return len(self._docs)

    @classmethod
    def load(cls, corpus_dir: str | Path) -> DbDefinitionIndex:
        """コーパスディレクトリから column_index.jsonl を読み込みインデックス構築する。

        ファイルが無い場合は空のインデックス（is_loaded=False）を返す。
        """
        index = cls()
        path = Path(corpus_dir) / "L2" / "column_index.jsonl"
        index.source_path = path
        if not path.exists():
            logger.info(f"DB定義書コーパス未配置: {path}（AI案作成は候補ゼロ扱い）")
            return index

        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    col = json.loads(line)
                    doc = index._make_document(col)
                    index._docs.append(doc)
                    index._df.update(doc.tf.keys())
        except (OSError, json.JSONDecodeError) as e:
            logger.error(f"DB定義書コーパス読み込みエラー: {path}: {e}")
            return cls()  # 壊れていたら空扱い

        n = len(index._docs)
        if n:
            index._avgdl = sum(d.length for d in index._docs) / n
            for term, df in index._df.items():
                # BM25 idf（非負クランプ）
                index._idf[term] = max(0.0, math.log((n - df + 0.5) / (df + 0.5) + 1.0))
        logger.info(
            f"DB定義書コーパス読み込み完了: {n}カラム, "
            f"vocab={len(index._df)}, avgdl={index._avgdl:.1f}"
        )
        return index

    @staticmethod
    def _doc_text(col: dict[str, Any]) -> str:
        parts = [
            col.get("table_logical"),
            col.get("physical_name"),
            col.get("logical_name"),
            col.get("notes"),
            col.get("db_group"),
        ]
        return " ".join(p for p in parts if p)

    def _make_document(self, col: dict[str, Any]) -> _Document:
        tokens = _tokenize(self._doc_text(col))
        return _Document(column=col, tf=Counter(tokens), length=len(tokens))

    def search(self, query: str, top_k: int = 8) -> list[ColumnHit]:
        """クエリに対しBM25上位 top_k 件の ColumnHit を返す（スコア降順）"""
        if not self._docs:
            return []
        q_terms = set(_tokenize(query))
        if not q_terms:
            return []

        scored: list[tuple[float, _Document]] = []
        for doc in self._docs:
            if doc.length == 0:
                continue
            score = 0.0
            for term in q_terms:
                tf = doc.tf.get(term)
                if not tf:
                    continue
                idf = self._idf.get(term, 0.0)
                denom = tf + _BM25_K1 * (1 - _BM25_B + _BM25_B * doc.length / (self._avgdl or 1.0))
                score += idf * (tf * (_BM25_K1 + 1)) / denom
            if score > 0:
                scored.append((score, doc))

        scored.sort(key=lambda x: x[0], reverse=True)
        hits: list[ColumnHit] = []
        for cand_id, (score, doc) in enumerate(scored[:top_k]):
            col = doc.column
            hits.append(
                ColumnHit(
                    cand_id=cand_id,
                    db_group=col.get("db_group"),
                    db_qualified=col.get("db_qualified"),
                    table_logical=col.get("table_logical"),
                    physical_name=col.get("physical_name"),
                    logical_name=col.get("logical_name"),
                    type_raw=col.get("type_raw"),
                    pk=bool(col.get("pk")),
                    notes=col.get("notes"),
                    score=round(score, 4),
                )
            )
        return hits


# --- プロセス内シングルトン（遅延ロード） -----------------------------------

_index_lock = threading.Lock()
_index_cache: dict[str, DbDefinitionIndex] = {}


def get_index(corpus_dir: str | None = None) -> DbDefinitionIndex:
    """コーパスディレクトリ単位でキャッシュされたBM25インデックスを返す（遅延ロード）"""
    key = str(corpus_dir or settings.db_definition_dir)
    cached = _index_cache.get(key)
    if cached is not None:
        return cached
    with _index_lock:
        cached = _index_cache.get(key)
        if cached is None:
            cached = DbDefinitionIndex.load(key)
            _index_cache[key] = cached
        return cached


def reset_index_cache() -> None:
    """インデックスキャッシュをクリアする（テスト用 / コーパス差し替え後の再ロード用）"""
    with _index_lock:
        _index_cache.clear()
