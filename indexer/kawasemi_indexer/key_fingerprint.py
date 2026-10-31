"""患者キー列のフォーマット指紋（--from-live）。

読み取り専用接続で候補列の値を上限付きサンプリングし、フォーマット指紋
（桁数分布・ゼロ詰め率・文字クラス・既知患者キー列との値集合重なり率）を計算する。
名称類似スコア（KeyTypeDrafter）と合成して患者キー / key_type 候補の確度を上げる。

「列名だけでは mrn か dwh_serial か決まらない」ため、値のフォーマットで候補を補強する
（`docs/kawasemi_indexer_spec.md` §6.4）。最終確定は引き続き人手（自動 confirm しない）。

PHI 保護（不変条件）:
- 生値は保存しない。指紋計算後は統計量（分布・率）とハッシュ集合のみ保持する。
- 値集合の重なり率はソルト付きハッシュ集合で計算する（生値も復元可能な平文も残さない）。
- レポート・ログ・成果物のいずれにも生値を出さない（サンプリング直後に破棄）。
"""

from __future__ import annotations

import hashlib
from collections import Counter
from dataclasses import dataclass, field

from kawasemi_indexer.key_types import CANONICAL_NUMERIC_STRING, KeyType
from kawasemi_indexer.logging_config import get_logger

logger = get_logger(__name__)

SAMPLE_LIMIT = 1000  # 1列あたりのサンプリング上限行数
_SALT = b"kawasemi-key-fingerprint-v1"  # 重なり率用ハッシュのソルト（生値復元を防ぐ）


def _hash(value: str) -> str:
    return hashlib.sha256(_SALT + value.encode("utf-8")).hexdigest()[:16]


def _quote_ident(name: str) -> str:
    """SQL 識別子を二重引用符でエスケープ（インジェクション防止）。"""
    return '"' + name.replace('"', '""') + '"'


@dataclass
class ColumnFingerprint:
    """1列のフォーマット指紋（統計量とハッシュ集合のみ。生値は保持しない）。"""

    sample_count: int = 0
    numeric_rate: float = 0.0  # 数字のみの値の割合
    alnum_rate: float = 0.0  # 英数のみの値の割合
    zero_pad_rate: float = 0.0  # 先頭ゼロを持つ値の割合（固定長ゼロ詰めの手掛かり）
    modal_length: int = 0  # 最頻の文字列長
    fixed_length_rate: float = 0.0  # 最頻長に一致する値の割合（固定長らしさ）
    distinct_rate: float = 0.0  # ユニーク率（患者キーは高い）
    hashes: frozenset[str] = field(default_factory=frozenset, repr=False)  # 重なり率用（生値なし）

    def overlap(self, other: ColumnFingerprint) -> float:
        """別の列（既知患者キー等）とのハッシュ集合 Jaccard 係数（0..1）。"""
        if not self.hashes or not other.hashes:
            return 0.0
        inter = len(self.hashes & other.hashes)
        union = len(self.hashes | other.hashes)
        return inter / union if union else 0.0


def compute_fingerprint(values: list[str]) -> ColumnFingerprint:
    """サンプリングした生値リストから指紋を計算する（呼び出し側は直後に生値を破棄する）。"""
    vals = [v for v in (str(x).strip() for x in values if x is not None) if v]
    n = len(vals)
    if n == 0:
        return ColumnFingerprint()
    numeric = sum(1 for v in vals if v.isdigit())
    alnum = sum(1 for v in vals if v.isalnum())
    zero_pad = sum(1 for v in vals if len(v) > 1 and v[0] == "0")
    lengths = Counter(len(v) for v in vals)
    modal_len, modal_cnt = lengths.most_common(1)[0]
    distinct = len(set(vals))
    return ColumnFingerprint(
        sample_count=n,
        numeric_rate=numeric / n,
        alnum_rate=alnum / n,
        zero_pad_rate=zero_pad / n,
        modal_length=modal_len,
        fixed_length_rate=modal_cnt / n,
        distinct_rate=distinct / n,
        hashes=frozenset(_hash(v) for v in vals),
    )


def sample_column(
    dsn: str, db_qualified: str, physical_name: str, limit: int = SAMPLE_LIMIT
) -> ColumnFingerprint:
    """読み取り専用で1列を上限付きサンプリングし、指紋を返す（生値は返さず破棄する）。"""
    from sqlalchemy import create_engine, text

    from kawasemi_indexer.ingestion.info_schema import to_sync_dsn

    engine = create_engine(to_sync_dsn(dsn))
    try:
        with engine.connect() as conn:
            col = _quote_ident(physical_name)
            tbl = ".".join(_quote_ident(p) for p in db_qualified.split("."))
            # 識別子は quote、LIMIT は int 定数のみ（インジェクション防止）。
            sql = text(f"SELECT {col} FROM {tbl} WHERE {col} IS NOT NULL LIMIT {int(limit)}")
            rows = [r[0] for r in conn.execute(sql).fetchall()]
    finally:
        engine.dispose()
    fp = compute_fingerprint(rows)
    rows.clear()  # 生値を明示破棄
    return fp


def fingerprint_score(fp: ColumnFingerprint, kt: KeyType) -> float:
    """指紋と key_type canonical 仕様の適合度（0..1）。名称スコアと合成する材料。"""
    if fp.sample_count == 0:
        return 0.0
    if kt.canonical.kind == CANONICAL_NUMERIC_STRING:
        score = 0.6 * fp.numeric_rate  # 数値文字列 key_type は数字率が高いほど適合
        score += 0.2 * fp.zero_pad_rate  # ゼロ詰めは固定長IDの手掛かり
    else:
        score = 0.4 * fp.alnum_rate
    score += 0.2 * fp.distinct_rate  # 患者キーはユニーク率が高い
    return min(1.0, score)
