"""merge: information_schema（骨格真実）＋ Excel/CSV（意味補完）＋ 知識層 → 契約。

方針（docs/kawasemi_indexer_spec.md §4, §6）:
- 物理構造は information_schema を正とする。
- Excel の logical_name/notes/table_logical を physical_name（＋表識別子）で結合して補完。
- info_schema に無い定義書行は棄却（古い定義）。
- Excel 補完の無いカラムは知識層 enrichment（inferred印・人確認）で補える。無ければ
  logical_name=None（検索品質低下として計上）。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from kawasemi_indexer.ingestion.models import (
    CodeEntry,
    ColumnRecord,
    KnowledgeLayer,
    SchemaSnapshot,
)
from kawasemi_indexer.ingestion.tabular import TabularData, TabularRow
from kawasemi_indexer.logging_config import get_logger
from kawasemi_indexer.physical import physical_of

logger = get_logger(__name__)

COLUMN_INDEX_FILENAME = "L2/column_index.jsonl"
VALUE_DICT_FILENAME = "value_dictionary.jsonl"


class _TabularLookup:
    """定義書行を (match_field, match_value, physical_name) で引けるようにする。"""

    def __init__(self, sources: list[TabularData]) -> None:
        # (match_field, match_value) -> {physical_name: TabularRow}
        self._cols: dict[tuple[str, str], dict[str, TabularRow]] = {}
        # (match_field, match_value) -> table_logical
        self._table_logical: dict[tuple[str, str], str] = {}
        self._matched: set[tuple[str, str, str]] = set()
        # 棄却数は「重複排除した定義書カラムキー」基準で数える（複数ソースが同一
        # (field,value,physical) を持っても二重計上しない）。
        self._keys: set[tuple[str, str, str]] = set()
        for src in sources:
            for row in src.rows:
                self._keys.add((src.table_match, row.match_value, row.physical_name))
                key = (src.table_match, row.match_value)
                self._cols.setdefault(key, {})[row.physical_name] = row
                if row.table_logical and key not in self._table_logical:
                    self._table_logical[key] = row.table_logical

    def find(self, table_id: str, db_qualified: str, physical_name: str) -> TabularRow | None:
        for key in (("table_id", table_id), ("db_qualified", db_qualified)):
            row = self._cols.get(key, {}).get(physical_name)
            if row is not None:
                self._matched.add((key[0], key[1], physical_name))
                return row
        return None

    def table_logical(self, table_id: str, db_qualified: str) -> str | None:
        for key in (("table_id", table_id), ("db_qualified", db_qualified)):
            if key in self._table_logical:
                return self._table_logical[key]
        return None

    @property
    def discarded_count(self) -> int:
        """info_schema に突き合わなかった（棄却された）定義書カラム数（重複排除済み）。"""
        return len(self._keys) - len(self._matched)


@dataclass
class MergeStats:
    unenriched: int = 0  # 論理名なしカラム数（検索品質に影響）
    discarded_doc_rows: int = 0  # info_schema に無く棄却した定義書行数


def merge(
    snapshot: SchemaSnapshot,
    tabular_sources: list[TabularData],
    knowledge: KnowledgeLayer,
    physical_schemas: Mapping[str, str | None] | None = None,
) -> tuple[list[ColumnRecord], MergeStats]:
    """スナップショットを骨格に、定義書・知識層で補完した ColumnRecord 群を返す。

    `physical_schemas` は table_id → 実行用のスキーマ（`physical.table_schemas`）。与えた表の列に
    `physical`（schema / table / column）を付ける。表名・列名は reflection の名前（実DBの名前）。

    Returns: (records, stats)
    """
    lookup = _TabularLookup(tabular_sources)
    records: list[ColumnRecord] = []
    unenriched = 0

    for tbl in snapshot.tables:
        role = knowledge.role_of(tbl.table_id)
        pk_binding = knowledge.patient_keys.get(tbl.table_id)
        # 「患者で絞らない表」の宣言。患者キーの宣言と同時なら矛盾なので採らない
        # （マスタ取込の矛盾の扱いと同じ。患者で絞る側へ倒す）。
        patient_scoped: bool | None = None
        if tbl.table_id in knowledge.patient_unscoped_tables:
            if pk_binding is not None:
                logger.warning(
                    "患者で絞らない表の宣言を採らない（矛盾: patient_keys にも宣言）: "
                    f"{tbl.table_id}"
                )
            else:
                patient_scoped = False
        table_logical = lookup.table_logical(tbl.table_id, tbl.db_qualified)

        for col in tbl.columns:
            doc = lookup.find(tbl.table_id, tbl.db_qualified, col.physical_name)
            logical_name = doc.logical_name if doc else None
            notes = doc.notes if doc else None
            inferred = False
            confirmed = True

            if logical_name is None:
                enr = knowledge.enrichments.get(f"{tbl.db_qualified}.{col.physical_name}")
                if enr is not None:
                    logical_name = enr.logical_name
                    notes = notes or enr.notes
                    inferred = enr.inferred
                    confirmed = enr.confirmed

            if not logical_name:
                unenriched += 1

            is_pk_col = bool(pk_binding and pk_binding.physical_name == col.physical_name)
            records.append(
                ColumnRecord(
                    table_id=tbl.table_id,
                    db_qualified=tbl.db_qualified,
                    table_logical=table_logical,
                    db_group=knowledge.db_group,
                    physical_name=col.physical_name,
                    logical_name=logical_name,
                    type_raw=col.type_raw,
                    type_base=col.type_base,
                    pk=col.pk,
                    notes=notes,
                    is_patient_key=is_pk_col,
                    key_type=pk_binding.key_type if pk_binding and is_pk_col else None,
                    patient_scoped=patient_scoped,
                    physical=physical_of(
                        (physical_schemas or {}).get(tbl.table_id), tbl.table_id, col.physical_name
                    ),
                    table_role=role,
                    logical_name_inferred=inferred,
                    enrichment_confirmed=confirmed,
                )
            )

    stats = MergeStats(unenriched=unenriched, discarded_doc_rows=lookup.discarded_count)
    logger.info(
        f"merge 完了: {len(records)}カラム, 補完欠落={stats.unenriched}, "
        f"棄却定義書行={stats.discarded_doc_rows}"
    )
    return records, stats


def write_column_index(records: list[ColumnRecord], out_dir: str | Path) -> Path:
    """column_index.jsonl（公式契約）を書き出す。builder の入力になる。"""
    path = Path(out_dir) / COLUMN_INDEX_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec.dump_row(), ensure_ascii=False) + "\n")
    logger.info(f"column_index.jsonl を書き出し: {path}（{len(records)}行）")
    return path


def write_value_dictionary(entries: list[CodeEntry], out_dir: str | Path) -> Path:
    """value_dictionary.jsonl（マスタの code→label）を書き出す。"""
    path = Path(out_dir) / VALUE_DICT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for e in entries:
            fh.write(json.dumps(e.model_dump(), ensure_ascii=False) + "\n")
    logger.info(f"value_dictionary.jsonl を書き出し: {path}（{len(entries)}行）")
    return path
