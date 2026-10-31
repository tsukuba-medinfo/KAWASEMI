"""取り込みオーケストレーション: 生ソース＋知識層 → column_index.jsonl 一式。

`kawasemi-indexer ingest` から呼ばれる。流れ（docs/kawasemi_indexer_spec.md §3）:
  1. 知識層 YAML をロード
  2. connections.yaml から DSN を解決し information_schema を reflection（骨格真実）
  3. Excel/CSV 定義書を読む（意味補完）
  4. merge → column_index.jsonl（契約。実行用の識別子 physical を含む）
  5. master 表のみ code→label を読み value_dictionary.jsonl
  6. 前回スナップショットと drift 差分を算出し、今回を永続
出力後は `kawasemi-indexer build` が column_index.jsonl を消費して embedding 索引を作る。
"""

from __future__ import annotations

from pathlib import Path

from kawasemi_indexer.connections import get_registry
from kawasemi_indexer.ingestion import drift as drift_mod
from kawasemi_indexer.ingestion.info_schema import (
    read_master_codes,
    reflect_catalog,
    reflect_snapshot,
)
from kawasemi_indexer.ingestion.knowledge import load_knowledge
from kawasemi_indexer.ingestion.lint import lint_tabular, merge_reports, write_reports
from kawasemi_indexer.ingestion.merge import (
    merge,
    write_column_index,
    write_value_dictionary,
)
from kawasemi_indexer.ingestion.models import IngestResult
from kawasemi_indexer.ingestion.tabular import read_tabular
from kawasemi_indexer.logging_config import get_logger
from kawasemi_indexer.physical import check_catalog, needs_catalog, read_catalog, table_schemas

logger = get_logger(__name__)


def ingest_connection(
    knowledge_path: str | Path,
    out_dir: str | Path,
    *,
    dsn: str | None = None,
    schema: str | None = None,
    connections_path: str | None = None,
    lint_fix: bool = False,
    force: bool = False,
    catalog_path: str | Path | None = None,
) -> IngestResult:
    """1接続分の定義書・スキーマを取り込み、column_index.jsonl 一式を生成する。

    Args:
        knowledge_path: 確認済知識層 YAML（connection_id/db_group/マッピング等）。
        out_dir: 出力ディレクトリ（L2/column_index.jsonl 等を書く）。
        dsn: DSN の明示指定（テスト用。未指定なら connections.yaml から解決）。
        schema: information_schema のスキーマ名（DBにより必要）。
        connections_path: connections.yaml パス（未指定で設定値）。
        lint_fix: 定義書の物理識別子を NFKC 正規化して取り込む（--fix）。
        force: 検品で error があっても続行する（既定は error で停止）。
        catalog_path: 実行先DBの表一覧（`schema_name|table_name|行数`）。知識層の
            `physical_schemas` が複数で、reflection したスキーマが候補に無いときに使う。
            無ければ DSN の各候補スキーマの表名を列挙し、列挙できなければ止まる。
    """
    knowledge = load_knowledge(knowledge_path)
    conn_id = knowledge.connection_id

    if dsn is None:
        registry = get_registry(connections_path)
        dsn = registry.dsn_of(conn_id)
        if not dsn:
            raise ValueError(
                f"接続 {conn_id} の DSN を解決できません（connections.yaml / secret_ref を確認）"
            )

    # 2. 骨格真実（information_schema）
    snapshot = reflect_snapshot(
        dsn, connection_id=conn_id, db_group=knowledge.db_group, schema=schema
    )

    # 3. 意味補完（Excel/CSV）＋検品
    tabular_sources = [read_tabular(src, normalize=lint_fix) for src in knowledge.tabular_sources]
    lint_report = merge_reports([lint_tabular(t) for t in tabular_sources])
    if tabular_sources:
        write_reports(lint_report, out_dir)
        logger.info(f"定義書検品: {lint_report.summary()}")
        if lint_report.has_errors and not force:
            raise ValueError(
                f"定義書検品で error が {lint_report.count('error')} 件あります。"
                f"qa/qa_report.md を確認し、--fix で正規化するか --force で続行してください。"
            )

    # 4. merge → 契約。実行用の識別子（payload.physical）のスキーマもここで決める
    candidates = knowledge.physical_schemas
    catalog = None
    if needs_catalog(candidates, snapshot.schema_name):
        catalog = _exec_catalog(dsn, candidates, catalog_path)
    schemas_of = table_schemas(
        [t.table_id for t in snapshot.tables],
        candidates,
        reflected=snapshot.schema_name,
        catalog=catalog,
    )
    records, stats = merge(snapshot, tabular_sources, knowledge, physical_schemas=schemas_of)
    write_column_index(records, out_dir)

    # 5. master のみ code→label
    code_entries = read_master_codes(dsn, knowledge, snapshot, schema=schema)
    write_value_dictionary(code_entries, out_dir)

    # 6. drift
    prev = drift_mod.load_prev_snapshot(out_dir)
    report = drift_mod.diff_snapshots(prev, snapshot)
    drift_mod.save_snapshot(snapshot, out_dir)

    result = IngestResult(
        connection_id=conn_id,
        column_count=len(records),
        table_count=len(snapshot.tables),
        code_entry_count=len(code_entries),
        unenriched_count=stats.unenriched,
        discarded_doc_rows=stats.discarded_doc_rows,
        drift=report,
    )
    logger.info(
        f"取り込み完了: connection={conn_id} columns={result.column_count} "
        f"tables={result.table_count} codes={result.code_entry_count} "
        f"unenriched={result.unenriched_count} drift={len(report.items)}"
    )
    return result


def _exec_catalog(
    dsn: str, candidates: list[str], catalog_path: str | Path | None
) -> dict[str, set[str]]:
    """実行先の表一覧。ファイルが渡ればそれ、無ければ DSN の各候補スキーマを列挙する。

    列挙できない（スキーマが無い・権限が無い・空）ときは止める。reflection の DSN が実行先と
    別インスタンスなら候補スキーマは見えないので、黙って全表を「決まらない」にしない。
    """
    if catalog_path is not None:
        catalog = read_catalog(catalog_path)
    else:
        try:
            catalog = reflect_catalog(dsn, candidates)
        except Exception as e:  # noqa: BLE001 - 方言ごとに例外が違う。理由を添えて止める
            raise ValueError(
                f"候補スキーマ {candidates} の表名を DSN から列挙できない（{e.__class__.__name__}: {e}）。"
                "--catalog で実行先DBの表一覧（schema_name|table_name|行数）を渡すこと"
            ) from e
    check_catalog(catalog, candidates)
    return catalog
