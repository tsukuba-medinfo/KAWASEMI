"""KeyTypeDrafter: 索引の患者キー列に key_type 候補を提案する（レベル2支援, PR④）。

builder が付与した patient_key（テーブルの患者キー列）の unit を集め、key_types
カタログの各 key_type との名称類似で候補を提示する。確定（resource_key_binding へ）は
人間レビューを経る（source-survey 同型）。

重要: 列名・論理名だけでは「mrn か dwh_serial か」は原理的に決まらないため、ここは
候補提示までを担う（確定は人手）。--from-live はフォーマット指紋
（key_fingerprint）で候補を補強する: 名称類似＋指紋適合＋既に key_type が付いた既知
患者キー列との値集合重なり率を合成する。数万列規模を手作業で貼るのは非現実的なので、
本ドラフタが下書きを与える。

設計: カタログ化支援・候補→人手確定・一括付与
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from kawasemi_indexer.key_types import KeyType, KeyTypeCatalog, get_catalog
from kawasemi_indexer.logging_config import get_logger
from kawasemi_indexer.store import ResourceIndexStore, get_store

logger = get_logger(__name__)

_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokens(*texts: str | None) -> set[str]:
    """英数トークン＋小文字化した素文字列（日本語の部分一致用）の集合。"""
    out: set[str] = set()
    for t in texts:
        if not t:
            continue
        low = t.lower()
        out.update(_TOKEN_RE.findall(low))
        out.add(low)
    return out


def _score(unit_tokens: set[str], kt: KeyType) -> float:
    """患者キー列のトークンと key_type(id/label) の名称類似スコア（0..1）。"""
    kt_tokens = _tokens(kt.id, kt.label)
    if not kt_tokens:
        return 0.0
    exact = unit_tokens & kt_tokens
    score = len(exact) / len(kt_tokens)
    # 部分一致（key_type id/label が列名・論理名の部分文字列、またはその逆）
    for u in unit_tokens:
        for k in (kt.id.lower(), kt.label.lower()):
            if k and k != u and (k in u or u in k):
                score = max(score, 0.4)
    return score


# --from-live 時の合成重み（名称類似 : フォーマット指紋 : 既知キー列との値集合重なり率）。
# overlap 参照が無い列では重みを (名称, 指紋) = (0.6, 0.4) に再正規化する。
_NAME_WEIGHT = 0.5
_FP_WEIGHT = 0.3
_OVERLAP_WEIGHT = 0.2


@dataclass(frozen=True)
class KeyTypeCandidate:
    key_type: str
    label: str
    score: float
    name_score: float | None = None  # --from-live 時の内訳（名称類似）
    fp_score: float | None = None  # --from-live 時の内訳（フォーマット指紋）
    overlap_score: float | None = None  # --from-live 時の内訳（既知キー列との値集合重なり率）


@dataclass(frozen=True)
class KeyTypeDraft:
    """患者キー列1つに対する key_type 候補（人間レビュー前の下書き）。"""

    unit_id: str
    label: str
    physical_name: str | None
    table: str | None
    candidates: list[KeyTypeCandidate] = field(default_factory=list)
    fingerprint: str | None = None  # --from-live 時の指紋サマリ（統計量のみ・生値なし）


class KeyTypeDrafter:
    """索引の患者キー列に key_type 候補を提案する（embedding 高度化は後続）。"""

    def __init__(
        self,
        index_dir: str | None = None,
        catalog: KeyTypeCatalog | None = None,
        store: ResourceIndexStore | None = None,
    ) -> None:
        self._index_dir = index_dir
        self._catalog = catalog
        self._store = store

    def _resolve_store(self) -> ResourceIndexStore:
        return self._store or get_store(self._index_dir)

    def draft(
        self,
        top_n: int = 3,
        min_score: float = 0.0,
        *,
        from_live: bool = False,
        dsn: str | None = None,
        sample_limit: int | None = None,
    ) -> list[KeyTypeDraft]:
        """患者キー列ごとに key_type 候補（スコア降順 top_n）を返す。

        from_live=True のとき、読み取り専用接続で各患者キー列を上限付きサンプリングして
        フォーマット指紋を計算し、名称類似スコアと合成する（列名が無意味でも値の形で候補を
        補強）。既に key_type が付いた列は「既知患者キー列」の参照指紋になり、
        値集合の重なり率でも候補を補強する。生値は保持せず統計量のみ。dsn は明示指定
        （無指定で connections.yaml）。
        """
        store = self._resolve_store()
        catalog = self._catalog or get_catalog()
        key_types = catalog.all()
        resolved_dsn = self._resolve_dsn(dsn) if from_live else None

        # 患者キー列を集め、from_live なら各列を1回だけサンプリング（列ごと指紋）。
        pk_units = [
            u
            for u in store.units
            if u.kind == "db"
            and (u.payload or {}).get("patient_key")
            and (u.payload or {}).get("physical_name") == (u.payload or {}).get("patient_key")
        ]
        fps: dict[str, Any] = {}
        if from_live and resolved_dsn:
            for u in pk_units:
                dbq = (u.payload or {}).get("db_qualified")
                phys = (u.payload or {}).get("physical_name")
                if dbq and phys:
                    fp = self._fingerprint(resolved_dsn, dbq, phys, sample_limit)
                    if fp is not None and fp.sample_count:
                        fps[u.unit_id] = fp

        # 既に key_type が付いた列を「既知患者キー列」の参照指紋として集める（重なり率補強用）。
        references: dict[str, list[Any]] = {}
        for u in pk_units:
            kt_id = (u.payload or {}).get("key_type")
            if kt_id and u.unit_id in fps:
                references.setdefault(kt_id, []).append((u.unit_id, fps[u.unit_id]))

        drafts: list[KeyTypeDraft] = []
        for unit in pk_units:
            payload = unit.payload or {}
            phys = payload["physical_name"]
            utoks = _tokens(phys, payload.get("logical_name"), unit.label)
            fp = fps.get(unit.unit_id)
            fp_summary = None
            if fp is not None:
                fp_summary = (
                    f"n={fp.sample_count} numeric={fp.numeric_rate:.2f} "
                    f"zero_pad={fp.zero_pad_rate:.2f} distinct={fp.distinct_rate:.2f} "
                    f"modal_len={fp.modal_length}"
                )

            scored = sorted(
                (self._candidate(utoks, kt, fp, references, unit.unit_id) for kt in key_types),
                key=lambda c: c.score,
                reverse=True,
            )
            cands = [c for c in scored if c.score >= min_score][:top_n]
            drafts.append(
                KeyTypeDraft(
                    unit_id=unit.unit_id,
                    label=unit.label,
                    physical_name=phys,
                    table=payload.get("db_qualified"),
                    candidates=cands,
                    fingerprint=fp_summary,
                )
            )
        logger.info(
            f"KeyTypeDrafter: 患者キー列 {len(drafts)}件に候補を提案"
            f"{'（--from-live 指紋あり）' if from_live else ''}"
            f"{f'／参照キー列 {sum(len(v) for v in references.values())} 件' if references else ''}"
        )
        return drafts

    def _candidate(
        self,
        utoks: set[str],
        kt: KeyType,
        fp: Any,
        references: dict[str, list[Any]] | None = None,
        self_unit_id: str | None = None,
    ) -> KeyTypeCandidate:
        """名称類似（＋指紋があれば指紋適合・既知キー列との重なり率）で候補スコアを作る。"""
        name = _score(utoks, kt)
        if fp is None or not fp.sample_count:
            return KeyTypeCandidate(kt.id, kt.label, name)
        from kawasemi_indexer.key_fingerprint import fingerprint_score

        fps = fingerprint_score(fp, kt)

        # 既知患者キー列（同 key_type）との値集合重なり率（自分自身は除外）。
        refs = [f for uid, f in (references or {}).get(kt.id, []) if uid != self_unit_id]
        overlap = max((fp.overlap(rf) for rf in refs), default=None)

        if overlap is None:
            # 参照が無い列は (名称, 指紋) を 0.6:0.4 に再正規化（overlap 分を割り振らない）。
            combined = 0.6 * name + 0.4 * fps
            return KeyTypeCandidate(kt.id, kt.label, combined, name_score=name, fp_score=fps)
        combined = _NAME_WEIGHT * name + _FP_WEIGHT * fps + _OVERLAP_WEIGHT * overlap
        return KeyTypeCandidate(
            kt.id, kt.label, combined, name_score=name, fp_score=fps, overlap_score=overlap
        )

    def _fingerprint(
        self, dsn: str, db_qualified: str, physical_name: str, sample_limit: int | None
    ) -> Any:
        from kawasemi_indexer.key_fingerprint import SAMPLE_LIMIT, sample_column

        try:
            return sample_column(dsn, db_qualified, physical_name, sample_limit or SAMPLE_LIMIT)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"指紋サンプリング失敗 {db_qualified}.{physical_name}: {e}")
            return None

    def _resolve_dsn(self, dsn: str | None) -> str | None:
        if dsn:
            return dsn
        try:
            from kawasemi_indexer.connections import get_registry

            store = self._resolve_store()
            conn_id = None
            for unit in store.units:
                if unit.kind == "db" and (unit.payload or {}).get("connection_id"):
                    conn_id = unit.payload["connection_id"]
                    break
            if conn_id:
                return get_registry().dsn_of(conn_id)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"DSN 解決に失敗（--dsn を指定してください）: {e}")
        return None
