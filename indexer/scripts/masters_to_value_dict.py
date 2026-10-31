#!/usr/bin/env python3
"""マスタ抽出結果（CSV/TSV）→ value_dictionary.jsonl（kind=master 取り込み入力）

site/masters/ 配下の抽出ファイルを連結し、value_dictionary.jsonl を生成する。
docs/master_ingestion_agreement.md §4 のインライン版と等価で、2 経路の出力に両対応:

  - (A) .csv  … ヘッダ付き・カンマ・クォート（Python の csv.writer で書き出したもの）
  - (B) .tsv  … ヘッダ無し・タブ・前後空白除去済み（DB のコマンドラインクライアントで出力したもの）

列順は両経路とも固定: connection_id, db_qualified, code, label, aliases（SELECT 列順）。
.csv はヘッダ行を自動スキップ、.tsv は位置でマッピングする。

使い方:
    python scripts/masters_to_value_dict.py
    MASTERS_DIR=site/masters VALUE_DICT_OUT=site/db_definition/value_dictionary.jsonl \\
        python scripts/masters_to_value_dict.py

このスクリプト自体は施設固有名を含まない汎用ユーティリティ。
"""

from __future__ import annotations

import csv
import glob
import json
import os
import sys
from pathlib import Path

MASTERS_DIR = Path(os.environ.get("MASTERS_DIR", "site/masters"))
OUT = Path(os.environ.get("VALUE_DICT_OUT", "site/db_definition/value_dictionary.jsonl"))
COLS = ["connection_id", "db_qualified", "code", "label", "aliases"]


def iter_records(path: Path):
    delimiter = "\t" if path.suffix.lower() in (".tsv", ".tab") else ","
    # utf-8-sig で BOM 付き(DB の管理ツールで出力したもの等)も透過。NULL を 'NULL' と出す
    # クライアントがあるため空扱いに正規化。
    with path.open(encoding="utf-8-sig", newline="") as fh:
        rows = [r for r in csv.reader(fh, delimiter=delimiter) if r and any(c.strip() for c in r)]
    if not rows:
        return
    # ヘッダ有り(CSV)は列名で対応付け、ヘッダ無し(TSV)は SELECT 列順で位置対応。
    header = [c.strip().lower() for c in rows[0]]
    if "connection_id" in header:
        idx = {name: i for i, name in enumerate(header)}
        data, pick = rows[1:], lambda row, k: row[idx[k]] if k in idx and idx[k] < len(row) else ""
    else:
        data, pick = rows, lambda row, k: row[COLS.index(k)] if COLS.index(k) < len(row) else ""
    for row in data:
        rec = {}
        for k in COLS:
            v = pick(row, k).strip()
            if v not in ("", "NULL"):  # 空/NULL はキーごと省略（aliases 空→無 alias 扱い）
                rec[k] = v
        yield rec


def main() -> int:
    files = sorted(glob.glob(str(MASTERS_DIR / "*.csv")) + glob.glob(str(MASTERS_DIR / "*.tsv")))
    if not files:
        print(f"入力なし: {MASTERS_DIR}/*.csv|*.tsv", file=sys.stderr)
        return 1
    OUT.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    with OUT.open("w", encoding="utf-8") as out:
        for f in files:
            count = 0
            for rec in iter_records(Path(f)):
                if not rec.get("code") or not rec.get("label"):
                    continue  # code/label 必須
                out.write(json.dumps(rec, ensure_ascii=False) + "\n")
                count += 1
            total += count
            print(f"  {Path(f).name}: {count} 行")
    print(f"→ {OUT}（計 {total} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
