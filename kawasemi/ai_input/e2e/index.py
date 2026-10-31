"""E2E の索引を indexer（`indexer/`）の実コードで作る（`kawasemi e2e setup --build-index`）。

流れ（docs/e2e_assets.md §5 と docs/e2e_pipeline.md §2）:

1. `ingest_connection` を接続ごとに通す（dwh / billing。ingest は 1 ファイル＝1 接続）
2. 2 接続の `L2/column_index.jsonl` と `value_dictionary.jsonl` を **1 つの DB 定義書
   ディレクトリへ連結**する（`build_index` の `db_source` は 1 つしか受けない。連結しないと
   `db:billing_stays.antibiotic_days` が索引に入らず関門 (a) で落ちる）
3. `build_index`（db ＋ fhir 辞書 ＋ 典拠の文書 ＋ ファイルの所在宣言）
   db 単位の**実行用の識別子**（`payload.physical`）は indexer の `ingest` が reflection から付け、
   `build` が payload へ写す（SQLite の既定スキーマ `main`）。外部 DB の取得は定義書の
   名前では引けない前提で、`derive._derive_db` は `payload.physical` を持たない単位の SQL を組まない。
   試験側では足さない
4. 観察項目の単位（`db:vital_signs.value_num#body_temp` など）を **DB のマスタと件数表から**
   `ItemSpec` に写し、`append_item_units` で追記する

embedder は引数で受ける。決定的層は `HashingEmbedder`、実機層は Ollama（`--embedder ollama`）。
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kawasemi.ai_input.e2e import (
    BILLING_DB,
    FHIR_DICTIONARY,
    FILE_MANIFEST,
    GENERATED_DIR,
    KNOWLEDGE_BILLING,
    KNOWLEDGE_DWH,
    REFERENCES_DIR,
    WAREHOUSE_DB,
)
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)

# 索引の出力先（生成物の側。gitignore）
RESOURCE_INDEX_DIR = GENERATED_DIR / "resource_index"

# 項目コードで行を選ぶ表（観察記録）。値の列・有効行の条件・時系列の列。
# `item_counts.source_table` と同じ表名で引く。
ITEM_TABLES: dict[str, dict[str, Any]] = {
    "vital_signs": {
        "value_column": "value_num",
        "filters": {"is_deleted": "0"},
        "time_column": "measured_at",
    },
    "observations": {
        "value_column": "value_text",
        "filters": {"is_deleted": "0", "is_active": "1"},
        "time_column": "recorded_on",
    },
}
ITEM_COLUMN = "item_code"
ITEM_SOURCE = "dwh.observation_items"


@dataclass(frozen=True)
class IndexBuildResult:
    """`build_e2e_index` の結果（試験と CLI が読む）。"""

    index_dir: Path
    unit_count: int
    item_units: tuple[str, ...]  # 追記した項目単位の unit_id
    # `payload.physical` を持つ db 単位の数（indexer が付けたもの。試験側では足さない）
    physical_units: int
    notes: tuple[str, ...] = field(default_factory=tuple)


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in path.read_text("utf-8").splitlines() if ln.strip()]


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


# --------------------------------------------------------------------------- #
# 1〜2. ingest と連結
# --------------------------------------------------------------------------- #


def ingest_connections(
    work_dir: Path,
    *,
    dwh_db: Path = WAREHOUSE_DB,
    billing_db: Path = BILLING_DB,
    knowledge_dwh: Path = KNOWLEDGE_DWH,
    knowledge_billing: Path = KNOWLEDGE_BILLING,
) -> Path:
    """2 接続を `ingest_connection` で取り込み、連結した DB 定義書ディレクトリを返す。"""
    from kawasemi_indexer.ingestion.pipeline import ingest_connection

    outs: list[Path] = []
    for knowledge, db, name in (
        (knowledge_dwh, dwh_db, "dwh"),
        (knowledge_billing, billing_db, "billing"),
    ):
        out = work_dir / "ingest" / name
        result = ingest_connection(knowledge, out, dsn=f"sqlite+aiosqlite:///{db}")
        if result.discarded_doc_rows:
            # 定義書に実 DB に無い列がある＝固定資産と生成器がずれている。黙って進めない
            raise RuntimeError(
                f"ingest({name}): 定義書の {result.discarded_doc_rows} 行が実DBに無い列を指している"
            )
        outs.append(out)

    merged = work_dir / "db_definition"
    columns: list[dict[str, Any]] = []
    values: list[dict[str, Any]] = []
    for out in outs:
        columns.extend(_read_jsonl(out / "L2" / "column_index.jsonl"))
        vd = out / "value_dictionary.jsonl"
        if vd.exists():
            values.extend(_read_jsonl(vd))
    _write_jsonl(merged / "L2" / "column_index.jsonl", columns)
    _write_jsonl(merged / "value_dictionary.jsonl", values)
    logger.info(f"ingest を連結: 列 {len(columns)} / マスタ語彙 {len(values)} → {merged}")
    return merged


# --------------------------------------------------------------------------- #
# 4. 項目単位（DB のマスタ・件数表 → ItemSpec）
# --------------------------------------------------------------------------- #


def item_specs_from_dwh(dwh_db: Path = WAREHOUSE_DB) -> list[Any]:
    """`observation_items` / `measurements` / `item_counts` から 1 項目＝1 `ItemSpec` を組む。

    患者の行は読まない（件数表が患者数と数値率を持つ）。有効な行の件数だけを数える
    （`vital_signs` は `is_deleted='0'`、`observations` は `is_deleted='0'` かつ `is_active='1'`）。

    **1 項目に複数の測定（seq）が同居する項目は 1 単位にまとめる**（`weight` 体重測定）。
    `ItemSpec.item_code` が `item_code` の絞り込み値そのものなので、seq ごとに単位を分ける
    命名は indexer 側に無い。測定名は `measurements` に全部載せ、seq の絞り込みは取得プラン
    （DSL）の WHERE が担う（gold の `row_filters` の `seq='1'`）。
    """
    from kawasemi_indexer.item_units import ItemSpec, numeric_share

    conn = sqlite3.connect(str(dwh_db))
    try:
        items = {
            r[0]: (r[1], r[2])
            for r in conn.execute(
                "SELECT item_code, item_name, measure_code FROM observation_items"
            )
        }
        measures: dict[str, list[tuple[str, str, str]]] = defaultdict(list)
        for measure_code, seq, name, unit in conn.execute(
            "SELECT measure_code, seq, measure_name, unit FROM measurements"
            " ORDER BY measure_code, seq"
        ):
            measures[measure_code].append((seq, name, unit))
        counts = conn.execute(
            "SELECT source_table, item_code, is_deleted, is_active, n_rows, n_patients, n_numeric "
            "FROM item_counts"
        ).fetchall()
    finally:
        conn.close()

    agg: dict[tuple[str, str], dict[str, int]] = {}
    for table, code, is_deleted, is_active, n_rows, n_pat, n_num in counts:
        spec = ITEM_TABLES.get(table)
        if spec is None:
            continue
        wanted = spec["filters"]
        flags = {"is_deleted": is_deleted or "", "is_active": is_active or ""}
        if any(flags.get(col) != val for col, val in wanted.items()):
            continue  # 有効でない行の件数は数えない
        g = agg.setdefault((table, code), {"rows": 0, "patients": 0, "numeric": 0})
        g["rows"] += int(n_rows)
        g["patients"] = max(g["patients"], int(n_pat))  # 測定ごとの患者数は重なるので最大
        g["numeric"] += int(n_num)

    specs: list[ItemSpec] = []
    for (table, code), g in sorted(agg.items()):
        name, measure_code = items.get(code, (code, ""))
        rows = measures.get(measure_code, [])
        measurements = [m for _seq, m, _u in rows]
        units = list(dict.fromkeys(u for _seq, _m, u in rows if u))
        t = ITEM_TABLES[table]
        valid = " AND ".join(f"{c}='{v}'" for c, v in t["filters"].items())
        specs.append(
            ItemSpec(
                db_qualified=table,
                value_column=t["value_column"],
                item_code=code,
                item_name=name,
                item_column=ITEM_COLUMN,
                row_filters=dict(t["filters"]),
                order_by=((t["time_column"], "asc"),),
                measurements=tuple(measurements),
                units=tuple(units),
                patients=g["patients"],
                numeric_share=numeric_share(g["numeric"], g["rows"]),
                item_source=ITEM_SOURCE,
                # 文面は indexer の既定に頼らず E2E 側で決める
                notes=(
                    f"観察項目 {code}（{name}）の値。{table} の {ITEM_COLUMN}='{code}' で絞り、"
                    f"有効な行は {valid}。記録が無いことは「なし」を意味しない。"
                ),
            )
        )
    return specs


# --------------------------------------------------------------------------- #
# 一括
# --------------------------------------------------------------------------- #


def make_embedder(kind: str, model: str) -> Any:
    """`--embedder` の値から embedder を作る。`hashing`（決定的）か `ollama`（実機）。"""
    if kind == "hashing":
        from kawasemi.ai_input.e2e.embedder import HashingEmbedder

        return HashingEmbedder(model=model)
    if kind == "ollama":
        from kawasemi_indexer.embedder import OllamaEmbedder

        from kawasemi.core.config import settings

        return OllamaEmbedder(model=model, base_url=settings.embedding_host)
    raise ValueError(f"embedder は hashing か ollama: {kind!r}")


def build_e2e_index(
    out_dir: Path,
    embedder: Any,
    *,
    work_dir: Path,
    generated_dir: Path = GENERATED_DIR,
    fhir_source: Path = FHIR_DICTIONARY,
    file_source: Path = REFERENCES_DIR,
    file_manifest: Path = FILE_MANIFEST,
    knowledge_dwh: Path = KNOWLEDGE_DWH,
    knowledge_billing: Path = KNOWLEDGE_BILLING,
) -> IndexBuildResult:
    """生成物（`generated_dir`）と固定資産から索引を `out_dir` へ作る。

    `work_dir` には ingest の出力・連結した定義書・追記前の索引が残る（調査用）。
    `knowledge_*` は確認済みの知識層。実機層で `draft-knowledge` の下書きから通すときに
    差し替える（docs/e2e_guide.md §6）。
    """
    from kawasemi_indexer.builder import build_index
    from kawasemi_indexer.item_units import append_item_units, build_item_units, read_db_units

    dwh_db = generated_dir / WAREHOUSE_DB.name
    billing_db = generated_dir / BILLING_DB.name
    db_source = ingest_connections(
        work_dir,
        dwh_db=dwh_db,
        billing_db=billing_db,
        knowledge_dwh=knowledge_dwh,
        knowledge_billing=knowledge_billing,
    )

    base = work_dir / "index_base"
    if base.exists():
        shutil.rmtree(base)
    info = build_index(
        base,
        embedder,
        db_source=db_source,
        fhir_source=fhir_source,
        file_source=file_source,
        file_manifest=file_manifest,
    )
    physical_units = sum(
        1
        for u in _read_jsonl(base / "db_index.jsonl")
        if isinstance(u["payload"].get("physical"), dict)
    )

    specs = item_specs_from_dwh(dwh_db)
    units = build_item_units(specs, read_db_units(base))
    if out_dir.exists():
        shutil.rmtree(out_dir)
    appended = append_item_units(base, out_dir, units, embedder)
    if not appended.appended:
        # 追記するものが無ければ `append_item_units` は何も書かない。索引はそのまま複製する
        shutil.copytree(base, out_dir)
    notes = tuple(
        f"{s.unit_id}: 測定 {'・'.join(s.measurements)}（seq が同居。絞り込みは DSL 側）"
        for s in specs
        if len(s.measurements) > 1
    )
    logger.info(
        f"E2E 索引を構築: {out_dir}（{appended.unit_count}単位。項目単位 {len(appended.appended)}件、"
        f"physical {physical_units}件、embedding={info.embedding_model}）"
    )
    return IndexBuildResult(
        index_dir=out_dir,
        unit_count=appended.unit_count,
        item_units=tuple(u["unit_id"] for u in appended.appended),
        physical_units=physical_units,
        notes=notes,
    )
