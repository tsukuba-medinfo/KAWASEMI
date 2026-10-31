"""information_schema アダプタ（骨格真実の抽出。SQLAlchemy reflection）。

不変条件: 患者データ行は見ない。テーブル/カラム/型/PK の**スキーマ定義のみ**を
reflection で取得する（データ行の SELECT サンプリング禁止）。
例外: 表役割=master と人確認した表のみ、code→label をコード辞書として取り込む
（read_master_codes）。

DSN は connections.yaml の secret_ref(env) から解決した非同期DSNを、reflection 用に
同期DSNへ正規化して使う（reflection は同期APIで十分・依存も軽い）。
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

from kawasemi_indexer.ingestion.models import (
    CodeEntry,
    ColumnSnapshot,
    KnowledgeLayer,
    SchemaSnapshot,
    TableSnapshot,
)
from kawasemi_indexer.logging_config import get_logger

logger = get_logger(__name__)

# マスタのコード辞書取込で1表あたり読む上限（暴走・巨大表保護）。
MASTER_CODE_LIMIT = 5000

# 非同期ドライバ → 同期ドライバの対応（reflection用）。
_ASYNC_TO_SYNC = {
    "+aiosqlite": "",
    "+asyncpg": "+psycopg2",
    "+aiomysql": "+pymysql",
    "+asyncmy": "+pymysql",
}

_TYPE_BASE_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9_ ]*?)\s*(?:\(|$)")


def to_sync_dsn(dsn: str) -> str:
    """非同期DSNを reflection 用の同期DSNへ正規化する。"""
    for async_drv, sync_drv in _ASYNC_TO_SYNC.items():
        if async_drv in dsn:
            return dsn.replace(async_drv, sync_drv)
    return dsn


def _type_base(type_raw: str) -> str:
    """型表記の先頭識別子を type_base とする（例 'NUMBER(5,2)'→'NUMBER'）。"""
    m = _TYPE_BASE_RE.match(type_raw or "")
    return (m.group(1).strip().upper() if m else (type_raw or "")).upper()


def _qualified(schema: str | None, table: str) -> str:
    return f"{schema}.{table}" if schema else table


def reflect_snapshot(
    dsn: str,
    *,
    connection_id: str,
    db_group: str,
    schema: str | None = None,
) -> SchemaSnapshot:
    """生きDBの information_schema を reflection し、物理スナップショットを返す。

    スキーマ定義のみ（データ行は読まない）。
    """
    engine = create_engine(to_sync_dsn(dsn))
    try:
        return _reflect(engine, connection_id=connection_id, db_group=db_group, schema=schema)
    finally:
        engine.dispose()


def _reflect(
    engine: Engine, *, connection_id: str, db_group: str, schema: str | None
) -> SchemaSnapshot:
    insp = inspect(engine)
    tables: list[TableSnapshot] = []
    for table_id in sorted(insp.get_table_names(schema=schema)):
        try:
            pk_cols = set(
                insp.get_pk_constraint(table_id, schema=schema).get("constrained_columns") or []
            )
        except Exception:  # noqa: BLE001 - PK取得失敗は非致命（pk=Falseで継続）
            pk_cols = set()
        columns: list[ColumnSnapshot] = []
        for col in insp.get_columns(table_id, schema=schema):
            name = col["name"]
            type_raw = str(col.get("type"))
            columns.append(
                ColumnSnapshot(
                    physical_name=name,
                    type_raw=type_raw,
                    type_base=_type_base(type_raw),
                    pk=name in pk_cols,
                )
            )
        tables.append(
            TableSnapshot(
                table_id=table_id, db_qualified=_qualified(schema, table_id), columns=columns
            )
        )
    logger.info(
        f"information_schema reflection 完了: connection={connection_id} "
        f"tables={len(tables)} (schema={schema or '-'})"
    )
    return SchemaSnapshot(
        connection_id=connection_id,
        db_group=db_group,
        schema_name=schema or insp.default_schema_name,
        tables=tables,
    )


def reflect_catalog(dsn: str, schemas: list[str]) -> dict[str, set[str]]:
    """候補スキーマごとの表名一覧から `表名 → スキーマ集合` を作る（表名だけ。行は読まない）。"""
    out: dict[str, set[str]] = {}
    engine = create_engine(to_sync_dsn(dsn))
    try:
        insp = inspect(engine)
        for s in schemas:
            for t in insp.get_table_names(schema=s):
                out.setdefault(t, set()).add(s)
    finally:
        engine.dispose()
    return out


def read_master_codes(
    dsn: str,
    knowledge: KnowledgeLayer,
    snapshot: SchemaSnapshot,
    *,
    schema: str | None = None,
) -> list[CodeEntry]:
    """表役割=master かつ患者キー無しと確認された表のみ code→label を読む（索引仕様 §6.2）。

    patient_data 表は決して読まない。誤分類によるPHI混入を避けるため、対象は
    「role=master」「master_tables 指定あり」「patient_keys に**無い**」「reflection に存在」
    の四条件 AND を満たす表に限る。role=master と patient_keys の同時宣言は矛盾なので
    読まずに警告する（沈黙のスキップより人へ気づかせる）。
    """
    targets: list[str] = []
    for t in knowledge.master_tables:
        if knowledge.role_of(t) != "master" or snapshot.table(t) is None:
            continue
        if t in knowledge.patient_keys:
            # master と宣言されているのに患者キーも宣言＝矛盾。PHI 防御のため読まない。
            logger.warning(
                f"マスタ取込スキップ（矛盾: role=master だが patient_keys にも宣言）: {t}"
            )
            continue
        targets.append(t)
    if not targets:
        return []

    # value_domains（値ドメイン注釈）: master table_id → 参照する FK 列群。
    # マスタの code→label を、それを値域に持つ列にも紐づけて取り込む（JOINではない）。
    refs: dict[str, list[tuple[str, str]]] = {}
    for col_key, master_id in knowledge.value_domains.items():
        fk_table, _, fk_col = col_key.rpartition(".")
        fk_tbl = snapshot.table(fk_table) if fk_table else None
        if not fk_col or fk_tbl is None or fk_col not in {c.physical_name for c in fk_tbl.columns}:
            logger.warning(
                f"value_domains の参照列が解決できません（'table_id.physical_name' 形式で実在要）: {col_key}"
            )
            continue
        refs.setdefault(master_id, []).append((fk_tbl.db_qualified, fk_col))

    entries: list[CodeEntry] = []
    engine = create_engine(to_sync_dsn(dsn))
    try:
        with engine.connect() as conn:
            for table_id in targets:
                spec = knowledge.master_tables[table_id]
                tbl = snapshot.table(table_id)
                assert tbl is not None  # targets で保証済み
                cols = {c.physical_name for c in tbl.columns}
                if spec.code_column not in cols or spec.label_column not in cols:
                    logger.warning(
                        f"マスタ取込スキップ（列不在）: {table_id} "
                        f"code={spec.code_column} label={spec.label_column}"
                    )
                    continue
                rows = _select_codes(conn, tbl.db_qualified, spec.code_column, spec.label_column)
                if len(rows) >= MASTER_CODE_LIMIT:
                    logger.warning(
                        f"マスタ コード辞書が上限 {MASTER_CODE_LIMIT} 件で切り詰められました: "
                        f"{table_id}（辞書が不完全な可能性）"
                    )
                fk_refs = refs.get(table_id, [])
                for code, label in rows:
                    if code is None:
                        continue
                    code_s = str(code)
                    label_s = "" if label is None else str(label)
                    # マスタ表自身の語彙（physical_name=None）
                    entries.append(
                        CodeEntry(
                            connection_id=knowledge.connection_id,
                            db_qualified=tbl.db_qualified,
                            code=code_s,
                            label=label_s,
                        )
                    )
                    # その値域を持つ各 FK 列に紐づく語彙（physical_name 付き）
                    for fk_dbq, fk_col in fk_refs:
                        entries.append(
                            CodeEntry(
                                connection_id=knowledge.connection_id,
                                db_qualified=fk_dbq,
                                physical_name=fk_col,
                                code=code_s,
                                label=label_s,
                            )
                        )
    finally:
        engine.dispose()
    logger.info(f"マスタ コード辞書取込: {len(entries)}件（{len(targets)}表）")
    return entries


def _select_codes(
    conn: Any, db_qualified: str, code_col: str, label_col: str
) -> list[tuple[Any, Any]]:
    """マスタ表から code,label を上限付きで取得する（master 表のみ呼ばれる）。

    識別子（表名・列名）は方言のクォータで囲み、表記揺れ・特殊文字・大文字小文字を
    安全に扱う（列は呼び出し側で reflection 実在確認済み・表名も reflection 由来）。
    LIMIT は定数のみ。f-string に値由来の入力は載らない。
    """
    preparer = conn.dialect.identifier_preparer
    quoted_table = ".".join(preparer.quote(p) for p in db_qualified.split("."))
    quoted_code = preparer.quote(code_col)
    quoted_label = preparer.quote(label_col)
    sql = text(
        f"SELECT {quoted_code}, {quoted_label} FROM {quoted_table} LIMIT {int(MASTER_CODE_LIMIT)}"
    )
    return [(r[0], r[1]) for r in conn.execute(sql).fetchall()]
