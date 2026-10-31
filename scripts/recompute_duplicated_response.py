#!/usr/bin/env python
"""来歴から `clinical_annotations.duplicated_response` を数え直す。

## なぜ要るのか

`duplicated_response` は「その項目の下書きが**矛盾した応答**（同じ項目に複数エントリ）
由来か」という事実で、集計では通常の項目と分けて別掲する。

立てているのは**保存の瞬間**である。`routes.save_clinical` が設計ロールの来歴
（`DesignLlmCall.duplicated_link_ids`）を引いて `ClinicalAnnotation` へ刻む。したがって
**保存より後に来歴が入った場合、遡って立たない。**

典型は、LLM による生成を別サーバで実行し、人手の記述を別の DB で並行して書き、
あとで両者の DB を統合する運用である。人手の記述を保存する側の DB は
`ai_input_design_llm_calls` が **0行**なので、**その行はすべて `duplicated_response=false`
で入る**。来歴を後から統合しても false のままになり、**矛盾した応答だった項目が普通の
項目として集計に入る**。

## 何をするか

来歴（`role=clinical`）の `duplicated_link_ids` に現れる link_id を集め、同じ問診票の
`clinical_annotations` の `duplicated_response` を立てる。

**一度立ったら下げない。** `save_clinical` と同じ規則である——その下書きが矛盾した応答
由来だったことは、人が記述を直しても変わらない事実なので、来歴に現れなくなった項目
（別の DB から来た来歴だけを見ている等）を false へ戻すことはしない。

## 安全側の設計

- **下見が既定**。`--apply` を明示したときだけ書き込む
- 冪等。2回目は0件になる
- 実行結果（対象件数・立てた件数・対象 link_id）を出力する。**作業記録に残すこと**
- **当てた DB を出力する**（資格情報は伏せる。sqlite は実体のパスも出す）。件数だけでは、
  DB を統合する運用でどちらに当てたのかが後から確定できない
- **照合できなかった来歴を出す。** 統合で `questionnaire_id` の採番がずれると、症状は
  「新たに立てる項目 0件」になり**冪等な2回目と見分けが付かない**。来歴にあって記録側に
  無い組を数えて出し、`fhir_id` を併記して人が突き合わせられるようにする

## 使い方

    uv run python scripts/recompute_duplicated_response.py            # 下見（既定）
    uv run python scripts/recompute_duplicated_response.py --apply    # 実行
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.models import ClinicalAnnotation, DesignLlmCall, DesignRole
from kawasemi.core.config import settings
from kawasemi.core.database import AsyncSessionLocal
from kawasemi.core.logging_config import get_logger
from kawasemi.models.questionnaire import Questionnaire

logger = get_logger(__name__)


def database_identity() -> str:
    """当てた DB を1行で特定する文字列（**作業記録に残すため**）。

    件数だけを記録しても、DB を統合する運用（この script の存在理由そのもの）では
    **どちらの DB に当てたのかが後から確定できない。**

    2つ手当てしてある。

    - **資格情報は落とす。** URL をそのまま印字すると、作業記録や端末のログへ
      パスワードが残る（`render_as_string(hide_password=True)`）
    - **sqlite は実体のパスを併記する。** 相対パスは `settings` が
      プロジェクトルート基準の絶対パスへ正規化するが（`normalize_sqlite_database_url`）、
      symlink と `..` はそこでは解けない。統合の作業では DB をコピー・リンクして
      並べるので、実体を記録したい
    """
    url = make_url(settings.database_url)
    shown = url.render_as_string(hide_password=True)
    if url.get_backend_name() == "sqlite" and url.database and url.database != ":memory:":
        resolved = str(Path(url.database).resolve())
        # 解決結果が URL と同じなら足さない（同じ文字列を2回出しても読み手の負担が増えるだけ）
        if resolved != url.database:
            return f"{shown}（実体: {resolved}）"
    return shown


async def duplicated_links_by_questionnaire(db: AsyncSession) -> dict[int, set[str]]:
    """来歴から「矛盾した応答だった link_id」を問診票ごとに集める。

    **全ての臨床的意味の呼び出しを見る**（最新1件ではない）。分割呼び出しでは重複が
    どのチャンクで起きたか分からず、下書きを2回に分けて作った項目もある
    （`routes._duplicated_link_ids` と同じ規則）。
    """
    rows = await db.execute(
        select(DesignLlmCall.questionnaire_id, DesignLlmCall.duplicated_link_ids).where(
            DesignLlmCall.role == DesignRole.CLINICAL
        )
    )
    out: dict[int, set[str]] = {}
    for questionnaire_id, link_ids in rows.all():
        if not isinstance(link_ids, list):
            continue
        out.setdefault(questionnaire_id, set()).update(str(x) for x in link_ids)
    return out


async def questionnaire_labels(db: AsyncSession, ids: set[int]) -> dict[int, str]:
    """問診票の `id → fhir_id`（出力に併記するため）。

    **突き合わせのキーは `questionnaire_id`（採番）だが、同一性の正本は `fhir_id` である。**
    DB を統合する運用では採番が保たれている保証が無いので、人が目で確かめられるように
    出力へ併記する。照合できなかった来歴（下の `unmatched`）が出たとき、まず疑うのが
    ここのずれである。
    """
    if not ids:
        return {}
    rows = await db.execute(
        select(Questionnaire.id, Questionnaire.fhir_id).where(Questionnaire.id.in_(ids))
    )
    return {questionnaire_id: fhir_id for questionnaire_id, fhir_id in rows.all()}


async def recompute(db: AsyncSession, *, apply: bool) -> dict[str, Any]:
    """来歴に照らして `duplicated_response` を立て直す。"""
    by_questionnaire = await duplicated_links_by_questionnaire(db)
    total_marked_in_provenance = sum(len(links) for links in by_questionnaire.values())

    rows = await db.execute(select(ClinicalAnnotation))
    annotations = list(rows.scalars().all())

    raised: list[str] = []
    already: int = 0
    for annotation in annotations:
        in_provenance = annotation.link_id in by_questionnaire.get(annotation.questionnaire_id, ())
        if not in_provenance:
            continue
        if annotation.duplicated_response:
            already += 1
            continue
        raised.append(f"{annotation.questionnaire_id}:{annotation.link_id}")
        if apply:
            # **立てるだけ**。下げる枝は持たない（`save_clinical` と同じ規則）。
            annotation.duplicated_response = True

    if apply and raised:
        await db.commit()

    # **来歴にあって記録側に無い組**。ここを出さないと、統合で `questionnaire_id` の採番が
    # ずれた場合の症状が「新たに立てる項目 0件」になり、**冪等な2回目の実行と見分けが
    # 付かない**（この運用で最も起きやすい事故は「何も起きなかったように見える」ことである）。
    recorded = {(a.questionnaire_id, a.link_id) for a in annotations}
    unmatched = sorted(
        f"{questionnaire_id}:{link_id}"
        for questionnaire_id, links in by_questionnaire.items()
        for link_id in links
        if (questionnaire_id, link_id) not in recorded
    )

    return {
        "applied": apply,
        "annotations": len(annotations),
        "duplicated_in_provenance": total_marked_in_provenance,
        "already_marked": already,
        "raised": len(raised),
        "targets": sorted(raised),
        "unmatched": unmatched,
        "labels": await questionnaire_labels(db, set(by_questionnaire)),
    }


def _render(pairs: list[str], labels: dict[int, str]) -> str:
    """`4:laterality` を `4(q-example):laterality` の形にする（`fhir_id` の併記）。"""
    out = []
    for pair in pairs:
        questionnaire_id, _, link_id = pair.partition(":")
        fhir_id = labels.get(int(questionnaire_id)) if questionnaire_id.isdigit() else None
        out.append(f"{questionnaire_id}({fhir_id}):{link_id}" if fhir_id else pair)
    return ", ".join(out)


async def _main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="実際に書き込む（既定は下見）")
    parser.add_argument("--json", action="store_true", help="機械可読の要約を出す")
    args = parser.parse_args()

    async with AsyncSessionLocal() as db:
        result = await recompute(db, apply=args.apply)

    # **どの DB に当てたか**を結果と同じ場所に置く（`--json` をそのまま作業記録へ貼れる）。
    result = {"database": database_identity(), **result}

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return

    labels = result["labels"]
    print(f"対象DB                : {result['database']}")
    print(
        f"モード                : {'実行（書き込み）' if result['applied'] else '下見（--apply で実行）'}"
    )
    print(f"臨床的意味の記録      : {result['annotations']}件")
    print(f"来歴の矛盾した link_id: {result['duplicated_in_provenance']}件")
    print(f"既に立っていた項目    : {result['already_marked']}件")
    print(f"新たに立てる項目      : {result['raised']}件")
    if result["targets"]:
        print(f"対象                  : {_render(result['targets'], labels)}")
    if result["unmatched"]:
        # **黙って0件で終わらせない。** 採番のずれはこの形でしか表に出ない。
        print(f"照合できない来歴      : {len(result['unmatched'])}件 ← 確認すること")
        print(f"  {_render(result['unmatched'], labels)}")
        print("  来歴にある項目が記録側に見つからない。**統合で questionnaire_id の採番が")
        print("  ずれていないか**を fhir_id で確かめること（ずれていると、立てるべき項目に")
        print("  1件も当たらないまま「0件」で正常終了する）。")
    print()
    print("**この実行を作業記録に残すこと**（いつ・どの DB で・何件立てたか）。")


if __name__ == "__main__":
    asyncio.run(_main())
