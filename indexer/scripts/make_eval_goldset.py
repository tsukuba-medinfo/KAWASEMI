#!/usr/bin/env python3
"""検索評価ゴールドセット（query → 正解 unit_id）を索引から決定的に生成する。

ビルド済み索引（`master_index.jsonl` / `db_index.jsonl`）を再編成し、以下の観点を
意図的に混ぜたゴールドセットを作る。実 embedding 不要（純テキスト処理・再現可能）。

  - name_alias : 名称別名（製品名⇔一般名・略称・院内名称）でエントリを引く
  - reading    : カナ読みでエントリを引く
  - code_direct: 標準コード（HOT/YJ/レセ電/ICD10/JLAC10）直打ちでエントリを引く
  - db_logical : DB カラムを論理名で引く
  - no_logical : 論理名なしカラムを物理名で引く（既知の弱点）

出力は `kawasemi-indexer eval --gold <out>` に渡せる jsonl（1行=GoldItem）。
生成物は施設特有の名称・コードを含むため git 管理外（site/ 配下は .gitignore 済）。

使い方:
    python scripts/make_eval_goldset.py --index-dir var/resource_index \
        --out site/eval/gold.jsonl
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from kawasemi_indexer.lexical import normalize
from kawasemi_indexer.schema import Kind
from kawasemi_indexer.synonyms import classify_term

# 観点ごとの採取上限（合計 ~80 件を目安に決定的に採る）。
_CAPS = {
    "name_alias": 25,
    "reading": 15,
    "code_direct": 25,
    "db_logical": 10,
    "no_logical": 5,
}


def _is_katakana(s: str) -> bool:
    """全角カタカナ（＋長音/中点）のみで構成される読み仮名か。"""
    if not s:
        return False
    return all("゠" <= c <= "ヿ" or c in "ー・" for c in s)


def _iter_jsonl(path: Path):
    if not path.exists():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def _gold(query: str, unit_id: str, tag: str, note: str | None = None) -> dict:
    item = {"query": query, "unit_id": unit_id, "tags": [tag]}
    if note:
        item["note"] = note
    return item


def build_goldset(index_dir: str | Path) -> list[dict]:
    index = Path(index_dir)
    items: list[dict] = []
    counts = dict.fromkeys(_CAPS, 0)
    seen_query: set[str] = set()

    def add(query: str, unit_id: str, tag: str, note: str | None = None) -> None:
        qn = normalize(query)
        if not qn or qn in seen_query or counts[tag] >= _CAPS[tag]:
            return
        seen_query.add(qn)
        counts[tag] += 1
        items.append(_gold(query, unit_id, tag, note))

    # --- master: 名称別名 / カナ / 標準コード ---
    # ファイル行順で走査しつつ stride で母集団を散らす（決定的）。
    for i, rec in enumerate(_iter_jsonl(index / Kind.INDEX_FILENAMES[Kind.MASTER])):
        uid = rec["unit_id"]
        label = rec.get("label") or ""
        label_n = normalize(label)
        aliases = rec.get("payload", {}).get("aliases") or []
        name_aliases = [
            a for a in aliases if classify_term(normalize(a)) == "name" and normalize(a) != label_n
        ]
        code_aliases = [a for a in aliases if classify_term(normalize(a)) == "code"]

        # 名称別名（stride 3 で散らす）。カナ読みは reading タグへ振り分け。
        if name_aliases and i % 3 == 0:
            alias = name_aliases[0]
            tag = "reading" if _is_katakana(alias) else "name_alias"
            add(alias, uid, tag, note=f"別名『{alias}』→『{label}』")
        # 標準コード直打ち（stride 5）。
        if code_aliases and i % 5 == 0:
            add(code_aliases[0], uid, "code_direct", note=f"code→『{label}』")

    # --- db: 論理名で引く / 論理名なしを物理名で引く ---
    for i, rec in enumerate(_iter_jsonl(index / Kind.INDEX_FILENAMES[Kind.DB])):
        uid = rec["unit_id"]
        p = rec.get("payload", {})
        logical = p.get("logical_name")
        physical = p.get("physical_name") or ""
        if logical and i % 400 == 0:
            add(str(logical), uid, "db_logical", note=f"論理名→{physical}")
        elif not logical and physical and i % 300 == 0:
            add(str(physical), uid, "no_logical", note="論理名なし（物理名で引く）")

    return items


def main() -> int:
    ap = argparse.ArgumentParser(description="検索評価ゴールドセットを索引から生成する")
    ap.add_argument("--index-dir", default="var/resource_index")
    ap.add_argument("--out", default="site/eval/gold.jsonl")
    args = ap.parse_args()

    items = build_goldset(args.index_dir)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for it in items:
            fh.write(json.dumps(it, ensure_ascii=False) + "\n")

    by_tag: dict[str, int] = {}
    for it in items:
        for t in it["tags"]:
            by_tag[t] = by_tag.get(t, 0) + 1
    print(f"ゴールドセットを書き出しました: {out}（{len(items)}件）")
    print(f"  観点別: {by_tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
