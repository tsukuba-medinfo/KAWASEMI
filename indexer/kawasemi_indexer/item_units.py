"""項目単位（`db:<表>.<列>#<項目コード>`）の生成と、既存の索引への追記。

何の値かが列名ではなく同じ表の項目コード列の値で決まる表（経過表など縦持ちの表）は、
列の単位だけでは「体温」で検索しても届かない。1項目を1単位にし、列の単位の payload を
写して項目コードと絞り込みを足す（CONTRACT.md「項目コードで行を選ぶ単位」）。

施設の表名・項目コード列・項目マスタの名前はここには無い（既定値も持たない）。呼び出し側が
施設の設定や回答から読んで `ItemSpec` に写す。embedder は引数で受け、
テストや E2E では決定的な embedder を注入する（`builder` / `store` と同じ流儀）。

追記は文書種別の単位を足すときと同じ要領で、**既存のベクトルを動かさない**。新しい単位の
ぶんだけ埋め込んで `embeddings.npy` の db ブロックの末尾へ挿し込む（`Kind.ORDER` は
`(DB, FILE, FHIR, MASTER)` なので db の後ろ、他 kind の前）。
"""

from __future__ import annotations

import json
import shutil
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from kawasemi_indexer.embedder import Embedder
from kawasemi_indexer.schema import BUILD_INFO_FILENAME, EMBEDDINGS_FILENAME, Kind


@dataclass(frozen=True)
class ItemSpec:
    """索引へ入れる1項目。列の単位（`db:<db_qualified>.<value_column>`）に項目コードを足す。

    - `db_qualified` / `value_column`: 値が入っている表（スキーマ修飾）と列。
      この2つで列の単位 `base_unit_id` が決まり、payload の表・列・型はそこから写す
    - `item_column` / `item_code` / `item_name`: 項目で絞る列と、その値・名称（必須）
    - `item_source`: 項目コードの出どころ（項目マスタ。`<接続>.<表>`。必須・キーワード指定）
    - `row_filters`: 有効な行の条件（`{"IS_DELETED": "0", "IS_VALID": "1"}`）。並び順も出力に写る
    - `order_by`: 時系列に並べる列（`(("DTTM", "asc"),)`）
    - `measurements` / `units`: 測定名と単位（検索文に入る。`item_name` と同じ測定名は除く）
    - `patients` / `numeric_share`: 患者数と、数値として読める行の割合（検索文には入れない）
    - `notes`: payload の `notes`。None なら既定の文（項目・絞り込み・欠測の意味）を組む
    """

    db_qualified: str
    value_column: str
    item_code: str
    item_name: str
    item_column: str
    row_filters: Mapping[str, str] = field(default_factory=dict)
    order_by: Sequence[tuple[str, str]] = ()
    measurements: Sequence[str] = ()
    units: Sequence[str] = ()
    patients: int = 0
    numeric_share: float | None = None
    item_source: str = field(kw_only=True)
    notes: str | None = None

    @property
    def base_unit_id(self) -> str:
        return f"db:{self.db_qualified}.{self.value_column}"

    @property
    def unit_id(self) -> str:
        return f"{self.base_unit_id}#{self.item_code}"


def numeric_share(numeric_rows: int, rows: int) -> float | None:
    """数値として読める行の割合（小数3桁）。行が無ければ None。"""
    return round(numeric_rows / rows, 3) if rows > 0 else None


def build_item_units(
    specs: Iterable[ItemSpec], base_units: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """1項目＝1ユニット。payload は列の単位を写し、項目コードと絞り込みを足す。

    `base_units` は列の単位（`db_index.jsonl` の行）を `unit_id` で引ける辞書。
    spec が指す列の単位が無ければ KeyError（索引に無い列へは項目を付けられない）。
    """
    units: list[dict[str, Any]] = []
    for spec in specs:
        try:
            base = base_units[spec.base_unit_id]
        except KeyError:
            raise KeyError(
                f"列の単位が索引に無い: {spec.base_unit_id}（項目 {spec.item_code}）"
            ) from None
        bp = base["payload"]
        code, name = spec.item_code, spec.item_name.strip()
        measurements = [m for m in spec.measurements if m and m != name]
        unit_words = list(spec.units)
        filters = list(spec.row_filters.items())

        # **検索文は項目の語を主にする。** 列の単位と同じく表・列・DB 名まで並べると、
        # 全単位に共通の語が文の大半を占め、項目名が埋もれる（項目名で引いても、
        # 項目名を持たない別の表の列のほうが近くなる）。表の論理名だけは残す。
        search_text = " ".join(
            w for w in [name, *measurements, *unit_words, bp["table_logical"]] if w
        )
        cond = f"{bp['db_qualified']}.{spec.item_column}='{code}'"
        flags = " AND ".join(f"{c}='{v}'" for c, v in filters)
        snippet = (
            f"DB={bp['connection_id']} 表={bp['db_qualified']} （{bp['table_logical']}） "
            f"型={bp['type_raw']} 項目={code} {name}"
            + (f" 測定={'・'.join(measurements)}" if measurements else "")
            + (f" 単位={'・'.join(unit_words)}" if unit_words else "")
            + f" 絞り込み={cond} 患者数={spec.patients}"
        )
        notes = spec.notes
        if notes is None:
            notes = (
                f"経過表の項目 {code}（{name}）の{bp['logical_name']}。"
                f"{bp['db_qualified']} の {spec.item_column}='{code}' で絞る。有効な行は {flags} と"
                f"読んでいる（施設の定義で確かめること）。"
                f"記録が無いことは「なし」を意味しない（観察して入力したときだけ行ができる）。"
            )
        payload = {
            **bp,
            "notes": notes,
            "item_code": code,
            "item_name": name,
            "item_filter_column": spec.item_column,
            # 有効な行の条件。KAWASEMI は所在の文字列に書き、DSL の照合で列名を必須にする
            "item_row_filters": [{"column": c, "equals": v} for c, v in filters],
            "item_source": spec.item_source,
            "item_measurements": measurements,
            "item_units": unit_words,
            "item_patients": spec.patients,
            "item_numeric_share": spec.numeric_share,
            "access_path": {
                "filters": [
                    {"column": spec.item_column, "equals": code},
                    *({"column": c, "equals": v} for c, v in filters),
                ],
                "order_by": [{"column": c, "dir": d} for c, d in spec.order_by],
            },
        }
        units.append(
            {
                "unit_id": f"{base['unit_id']}#{code}",
                "kind": Kind.DB,
                "label": name,
                "search_text": search_text,
                "snippet": snippet,
                "payload": payload,
            }
        )
    return units


def read_db_units(index_dir: str | Path) -> dict[str, dict[str, Any]]:
    """索引の `db_index.jsonl` を `unit_id` で引ける辞書にして返す。"""
    path = Path(index_dir) / Kind.INDEX_FILENAMES[Kind.DB]
    units: dict[str, dict[str, Any]] = {}
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.strip():
            u = json.loads(ln)
            units[u["unit_id"]] = u
    return units


@dataclass(frozen=True)
class AppendResult:
    """追記の結果。`appended` が空なら `out_dir` には何も書いていない。"""

    appended: list[dict[str, Any]]
    skipped: list[dict[str, Any]]
    n_db_before: int
    unit_count: int  # 追記後の全 kind の単位数

    @property
    def n_db_after(self) -> int:
        return self.n_db_before + len(self.appended)


def append_item_units(
    index_dir: str | Path,
    out_dir: str | Path,
    units: Sequence[Mapping[str, Any]],
    embedder: Embedder,
) -> AppendResult:
    """`units`（kind=db）を `index_dir` の索引へ足して `out_dir` へ書き出す。

    - 既に索引に在る `unit_id` は飛ばす（同じ入力でもう一度流しても何も足さない）
    - 追記するものが無ければ `out_dir` には何も書かない
    - 既存のベクトルは動かさない。新しい単位のベクトルを db ブロックの末尾へ挿し込む
    - `build_info.json` は `kind_counts.db` / `unit_count` / `built_at` だけ更新し、
      embedding のモデル名・ダイジェストはそのまま（embedder は索引と同じものを渡すこと）
    - `embeddings.npy` 以外のファイル（各 `*_index.jsonl`、synonym_index 等の付加物）は
      そのまま複製する

    行数が `build_info` と合わない・次元が合わないときは ValueError。
    """
    base = Path(index_dir)
    out = Path(out_dir)
    existing = read_db_units(base)
    skipped = [dict(u) for u in units if u["unit_id"] in existing]
    new_units = [dict(u) for u in units if u["unit_id"] not in existing]

    info = json.loads((base / BUILD_INFO_FILENAME).read_text(encoding="utf-8"))
    n_db = int(info["kind_counts"][Kind.DB])
    n_total = sum(int(v) for v in info["kind_counts"].values())
    if not new_units:
        return AppendResult(appended=[], skipped=skipped, n_db_before=n_db, unit_count=n_total)

    base_vecs = np.load(base / EMBEDDINGS_FILENAME)
    if base_vecs.shape[0] != n_total:
        raise ValueError(f"行数が build_info と合わない: {base_vecs.shape[0]} != {n_total}")

    new_vecs = embedder.embed([u["search_text"] for u in new_units])
    if new_vecs.shape[1] != base_vecs.shape[1]:
        raise ValueError(f"次元が合わない: {new_vecs.shape[1]} != {base_vecs.shape[1]}")
    merged = np.vstack([base_vecs[:n_db], new_vecs, base_vecs[n_db:]]).astype(np.float32)

    out.mkdir(parents=True, exist_ok=True)
    for p in base.iterdir():
        if p.is_file() and p.name not in (EMBEDDINGS_FILENAME, BUILD_INFO_FILENAME):
            shutil.copy2(p, out / p.name)
    with (out / Kind.INDEX_FILENAMES[Kind.DB]).open("a", encoding="utf-8") as f:
        for u in new_units:
            f.write(json.dumps(u, ensure_ascii=False) + "\n")
    np.save(out / EMBEDDINGS_FILENAME, merged)

    info["kind_counts"][Kind.DB] = n_db + len(new_units)
    info["unit_count"] = sum(int(v) for v in info["kind_counts"].values())
    info["built_at"] = datetime.now(UTC).isoformat()
    (out / BUILD_INFO_FILENAME).write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return AppendResult(
        appended=new_units, skipped=skipped, n_db_before=n_db, unit_count=int(info["unit_count"])
    )
