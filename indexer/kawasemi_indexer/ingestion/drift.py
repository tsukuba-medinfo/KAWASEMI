"""drift: 前回スナップショットと比較し、変化分だけを人へ提示する（索引仕様 §7）。

確認済知識層は再適用で温存し、新規/変更/消失カラムだけを再確認対象として差分化する。
"""

from __future__ import annotations

from pathlib import Path

from kawasemi_indexer.ingestion.models import (
    DriftItem,
    DriftReport,
    SchemaSnapshot,
)
from kawasemi_indexer.logging_config import get_logger

logger = get_logger(__name__)

SNAPSHOT_FILENAME = "schema_snapshot.json"


def _norm_type(type_raw: str | None) -> str:
    """型表記を比較用に正規化（空白除去・小文字化）。repr の表記揺れ誤検知を抑える。"""
    return "".join((type_raw or "").split()).lower()


def load_prev_snapshot(out_dir: str | Path) -> SchemaSnapshot | None:
    """前回スナップショットを読む。無ければ None（初回）。"""
    path = Path(out_dir) / SNAPSHOT_FILENAME
    if not path.exists():
        return None
    return SchemaSnapshot.model_validate_json(path.read_text("utf-8"))


def save_snapshot(snapshot: SchemaSnapshot, out_dir: str | Path) -> Path:
    """今回スナップショットを次回 drift 用に永続する。"""
    path = Path(out_dir) / SNAPSHOT_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(snapshot.model_dump_json(indent=2), encoding="utf-8")
    return path


def diff_snapshots(prev: SchemaSnapshot | None, new: SchemaSnapshot) -> DriftReport:
    """前回→今回の差分（added / removed / type_changed）を返す。"""
    report = DriftReport(connection_id=new.connection_id)
    if prev is None:
        return report  # 初回は差分なし（全件が新規確認だが drift としては扱わない）

    prev_tables = {t.table_id: t for t in prev.tables}
    new_tables = {t.table_id: t for t in new.tables}

    for table_id, ntbl in new_tables.items():
        ptbl = prev_tables.get(table_id)
        prev_cols = {c.physical_name: c for c in ptbl.columns} if ptbl else {}
        new_cols = {c.physical_name: c for c in ntbl.columns}

        for phys, ncol in new_cols.items():
            pcol = prev_cols.get(phys)
            if pcol is None:
                report.items.append(
                    DriftItem(
                        table_id=table_id,
                        physical_name=phys,
                        change="added",
                        detail=f"{ncol.type_raw}" + ("（新表）" if ptbl is None else ""),
                        needs_enrichment=True,
                    )
                )
            elif _norm_type(pcol.type_raw) != _norm_type(ncol.type_raw):
                # 表記揺れ（空白・大小・型repr差）を正規化してから比較し、誤検知を抑える。
                report.items.append(
                    DriftItem(
                        table_id=table_id,
                        physical_name=phys,
                        change="type_changed",
                        detail=f"{pcol.type_raw} → {ncol.type_raw}",
                    )
                )

        for phys in prev_cols:
            if phys not in new_cols:
                report.items.append(
                    DriftItem(table_id=table_id, physical_name=phys, change="removed")
                )

    for table_id in prev_tables:
        if table_id not in new_tables:
            for c in prev_tables[table_id].columns:
                report.items.append(
                    DriftItem(
                        table_id=table_id,
                        physical_name=c.physical_name,
                        change="removed",
                        detail="（表ごと消失）",
                    )
                )

    logger.info(f"drift 差分: {len(report.items)}件（connection={new.connection_id}）")
    return report
