"""実行用の識別子（db payload の `physical`）と文書種別の到達経路（`access_path`）を決める。

索引の `db_qualified` は**定義書（元システム側）の名前**で、実行先DBの名前とは限らない
（定義書の `APP.ORDERS` が実DBでは `ehr.ORDERS` にある、スキーマ無しの `PATIENTS` が別名のスキーマにある、など）。
KAWASEMI は外部DBの単位について、`payload.physical` の `schema` / `table` / `column` が揃うものに
しか SQL を組まない。ここはその3つを決める工程で、2つの入口がある。

1. **ingest**（`ingestion/pipeline.py`）: reflection した表へ付ける（`table_schemas`）。
2. **既存の索引への追記**（`enrich_physical`、CLI `enrich-physical`）: 定義書から作った索引に、
   実行先DBの表一覧（カタログ）と知識層の `physical_schemas` を突き合わせて足す。文書種別の単位
   （`doc_type_code` を持つ）には、知識層の `doc_header` から `access_path`（本文を種別と
   有効行で絞るための結合経路）も足す（スキーマの対応表とヘッダ表は施設ごとに知識層から読む）。

**一意に決まらなければ付けない。** 同名の表が複数の候補スキーマにあるとき、どちらを選ぶ根拠も
索引に無い。推測で付けると動いてしまうぶんだけ悪質なので、付けずに残し、KAWASEMI の
fail-safe（`has_physical_identifiers`）に不足として拾わせる。

**文書種別の単位は経路が無ければ `physical` を付けない。** 本文の表が種別の列を持たない構成では、
経路なしで実行できると「その患者の全文書（削除済みを含む）」を返す SQL ができる。

**識別子は引用符で囲まずに出す。** KAWASEMI は `schema.table` をそのまま SQL に埋める。大文字小文字を
区別して作られた表（PostgreSQL で `"MyTable"` のように作ったもの）は、引用符なしでは小文字に畳まれて
見つからない。そういう接続は対象外である（CONTRACT.md の `physical`）。
"""

from __future__ import annotations

import collections
import hashlib
import json
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from kawasemi_indexer.logging_config import get_logger
from kawasemi_indexer.schema import EMBEDDINGS_FILENAME, Kind

if TYPE_CHECKING:
    from kawasemi_indexer.ingestion.models import DocHeaderSpec

logger = get_logger(__name__)

PHYSICAL_KEYS = ("schema", "table", "column")
# 文書種別の単位であることを示す payload のキー（施設側のスクリプトが付ける）
DOC_UNIT_KEYS = ("doc_type_code", "doc_filter_table", "doc_filter_column")


def physical_of(schema: str | None, table: str, column: str) -> dict[str, str] | None:
    """3つが揃えば `{"schema", "table", "column"}`、1つでも空なら None（付けない）。"""
    if not (schema and table and column):
        return None
    return {"schema": str(schema), "table": str(table), "column": str(column)}


def is_doc_unit(payload: Mapping[str, Any]) -> bool:
    """文書種別の単位か（本文の表を種別で絞る必要がある単位）。"""
    return any(payload.get(k) for k in DOC_UNIT_KEYS)


def resolve_schema(
    table: str, candidates: Iterable[str], catalog: Mapping[str, set[str]]
) -> str | None:
    """候補スキーマのうち、カタログ上 `table` を持つものが**ちょうど1つ**ならそれを返す。"""
    found = set(catalog.get(table, set())) & set(candidates)
    return next(iter(found)) if len(found) == 1 else None


def needs_catalog(candidates: Sequence[str], reflected: str | None) -> bool:
    """ingest で実行先の表一覧が要るか。候補が複数で、reflection したスキーマが候補に無いとき。"""
    return len(candidates) > 1 and reflected not in candidates


def table_schemas(
    tables: Iterable[str],
    candidates: Sequence[str],
    *,
    reflected: str | None,
    catalog: Mapping[str, set[str]] | None = None,
) -> dict[str, str | None]:
    """ingest で表ごとの実行用スキーマを決める。

    - 候補が無い: reflection したスキーマ（DSN が実行先と同じ論理スキーマを持つ前提。CONTRACT §4）
    - 候補が1つ: それ（知識層の明示。reflection 用のレプリカとスキーマ名が違うときに書く）
    - 候補が複数で、reflection したスキーマが候補に在る: それ（表はそこに実在する。推測ではない）
    - 候補が複数で、在らない: `catalog`（実行先の表一覧）で一意に決まるものだけ
    """
    out: dict[str, str | None] = {}
    for t in tables:
        if not candidates:
            out[t] = reflected
        elif len(candidates) == 1:
            out[t] = candidates[0]
        elif reflected in candidates:
            out[t] = reflected
        else:
            out[t] = resolve_schema(t, candidates, catalog or {})
    unresolved = sorted(t for t, s in out.items() if s is None)
    if unresolved:
        logger.warning(
            f"実行用のスキーマが一意に決まらない表（physical を付けない）: {len(unresolved)}表 "
            f"{unresolved[:10]}"
        )
    return out


def check_catalog(catalog: Mapping[str, set[str]], candidates: Sequence[str]) -> None:
    """候補スキーマのどれかの表が1つも列挙できなければ止める（黙って全表を落とさない）。"""
    seen = set().union(*catalog.values()) if catalog else set()
    missing = [s for s in candidates if s not in seen]
    if missing:
        raise ValueError(
            f"実行先の表一覧に候補スキーマ {missing} の表が1つも無い。reflection の DSN から列挙できない"
            "（権限・別インスタンス）なら、ingest の --catalog で実行先DBの表一覧"
            "（schema_name|table_name|行数）を渡すこと"
        )


def doc_patient_key(spec: DocHeaderSpec) -> dict[str, str]:
    """文書種別の単位の `patient_key`（と宣言されていれば `key_type`）。宣言が無ければ空。"""
    out: dict[str, str] = {}
    if spec.body_patient_key:
        out["patient_key"] = spec.body_patient_key
        if spec.body_key_type:
            out["key_type"] = spec.body_key_type
    return out


def doc_access_path(code: str, spec: DocHeaderSpec) -> dict[str, Any]:
    """文書種別 `code` の `access_path`（ヘッダ表を結合し、種別と有効行で絞る）。

    足すのは**絞り込みのための結合**だけで、値は本文の表から取る（別表の値を持ってくる結合は
    しない。1ユニット＝1つの場所を保つため）。並び順は既定値で、どれを採るか（最新/初回/全部）は
    索引ではなく DSL（`multi` と `select`）が決める。
    """
    via = {"schema": spec.header_schema, "table": spec.header_table, "on": list(spec.join_on)}
    filters: list[dict[str, Any]] = [{"via": via, "column": spec.type_column, "equals": str(code)}]
    filters += [{"via": via, "column": f.column, "equals": f.equals} for f in spec.filters]
    order_by = [
        {**({"via": via} if o.on_header else {}), "column": o.column, "dir": o.dir}
        for o in spec.order_by
    ]
    return {"filters": filters, "order_by": order_by}


# --------------------------------------------------------------------------- #
# 既存の索引への追記
# --------------------------------------------------------------------------- #


def read_catalog(path: str | Path) -> dict[str, set[str]]:
    """実行先DBの表一覧から `表名 → スキーマ集合` を読む。

    形式は `schema_name|table_name|行数` の1行1表（DB のクライアントで `|` 区切りにした出力。`---` の区切り行と
    見出し行は読み飛ばす）。`schema_name|view_name` の見出し以降はビューの節として読まない。
    行数の欄が整数でない行も読まない。
    """
    out: dict[str, set[str]] = collections.defaultdict(set)
    in_views = False
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("schema_name|view_name"):
            in_views = True
            continue
        if in_views or line.startswith("schema_name|") or line.startswith("---"):
            continue
        p = line.split("|")
        if len(p) == 3 and p[2].strip().lstrip("-").isdigit():
            out[p[1]].add(p[0])
    return dict(out)


@dataclass(frozen=True)
class GroupConfig:
    """知識層から読む、接続（db_group）ごとの追記の材料。"""

    schemas: dict[str, list[str]]
    doc_headers: dict[str, DocHeaderSpec]


def load_groups(knowledge_paths: Iterable[str | Path]) -> GroupConfig:
    """知識層 YAML 群から `db_group → physical_schemas / doc_header` を作る。

    payload の `connection_id` は db_group（builder が流用する）なので db_group で引く。
    """
    from kawasemi_indexer.ingestion.knowledge import load_knowledge

    schemas: dict[str, list[str]] = {}
    headers: dict[str, DocHeaderSpec] = {}
    for p in knowledge_paths:
        k = load_knowledge(p)
        if k.physical_schemas:
            schemas[k.db_group] = list(k.physical_schemas)
        if k.doc_header is not None:
            headers[k.db_group] = k.doc_header
    return GroupConfig(schemas=schemas, doc_headers=headers)


def physical_for_unit(
    payload: Mapping[str, Any],
    schemas: Mapping[str, Sequence[str]],
    catalog: Mapping[str, set[str]],
) -> dict[str, str] | None:
    """既存の db 単位の `physical`。表名は `db_qualified` の末尾、列は `physical_name`。

    文書種別の単位で `access_path` が無ければ付けない（経路なしで実行できる形にしない）。
    """
    dq, column = payload.get("db_qualified"), payload.get("physical_name")
    if not dq or not column:
        return None
    if is_doc_unit(payload) and not payload.get("access_path"):
        return None
    table = str(dq).split(".")[-1]
    schema = resolve_schema(table, schemas.get(str(payload.get("connection_id")), ()), catalog)
    return physical_of(schema, table, str(column))


@dataclass(frozen=True)
class EnrichResult:
    units: int
    added: int  # physical を付けた（付け直した）単位
    unresolved: int  # 対象の接続の単位で、一意に決まらず physical を持たない単位
    removed: int  # 一意に決まらなくなり、古い physical を外した単位
    access_paths: int  # 文書種別の単位に access_path を付けた数


def enrich_physical(
    base: str | Path,
    out: str | Path,
    groups: GroupConfig,
    catalog: Mapping[str, set[str]],
) -> EnrichResult:
    """既存の索引 `base` の db 単位へ `physical`（と文書種別の `access_path`）を足して `out` へ書く。

    **`search_text` を1文字も変えない**（足すのは payload のキーだけ）。`db_index.jsonl` 以外の
    ファイルはそのまま複製し、`embeddings.npy` の md5 が前後で一致することを確かめる（違えば例外）。

    知識層に `physical_schemas` がある接続の単位は付け直す。一意に決まらない単位が古い `physical` を
    持っていれば外す（残すと、決まらないはずの識別子で実行できてしまう）。知識層に無い接続の単位は
    触らない。`access_path` は既に持っていれば触らない。
    """
    base, out = Path(base), Path(out)
    if base.resolve() == out.resolve():
        raise ValueError("base と out は別のディレクトリにする（元の索引を残す）")
    out.mkdir(parents=True, exist_ok=True)
    for src in base.iterdir():
        if src.is_file() and src.name != Kind.INDEX_FILENAMES[Kind.DB]:
            shutil.copy2(src, out / src.name)

    n = added = unresolved = removed = paths = 0
    lines: list[str] = []
    for line in (base / Kind.INDEX_FILENAMES[Kind.DB]).read_text("utf-8").splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        n += 1
        before = rec.get("search_text")
        p = rec.get("payload") or {}
        group = str(p.get("connection_id"))
        spec = groups.doc_headers.get(group)
        code = p.get("doc_type_code")
        if spec is not None and code and not p.get("access_path"):
            p["access_path"] = doc_access_path(str(code), spec)
            paths += 1
        if spec is not None and code and not p.get("patient_key"):
            # 患者キーは既にあれば触らない（ingest 由来の宣言を上書きしない）
            p.update(doc_patient_key(spec))
        if group in groups.schemas:
            ph = physical_for_unit(p, groups.schemas, catalog)
            if ph:
                p["physical"] = ph
                added += 1
            elif p.get("db_qualified"):
                unresolved += 1
                if "physical" in p:
                    logger.warning(
                        f"一意に決まらないので古い physical を外す: {rec.get('unit_id')}"
                    )
                    del p["physical"]
                    removed += 1
        rec["payload"] = p
        if rec.get("search_text") != before:  # pragma: no cover - 防御
            raise AssertionError(f"search_text が変わった: {rec.get('unit_id')}")
        lines.append(json.dumps(rec, ensure_ascii=False))
    (out / Kind.INDEX_FILENAMES[Kind.DB]).write_text("\n".join(lines) + "\n", encoding="utf-8")

    emb_in, emb_out = base / EMBEDDINGS_FILENAME, out / EMBEDDINGS_FILENAME
    if emb_in.exists():
        h_in = hashlib.md5(emb_in.read_bytes()).hexdigest()
        h_out = hashlib.md5(emb_out.read_bytes()).hexdigest()
        if h_in != h_out:
            raise RuntimeError(f"{EMBEDDINGS_FILENAME} が変わった（{h_in} → {h_out}）")
    result = EnrichResult(
        units=n, added=added, unresolved=unresolved, removed=removed, access_paths=paths
    )
    logger.info(f"physical を追記: {out}（{result}）")
    return result
