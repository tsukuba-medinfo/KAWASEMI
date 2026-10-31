"""典拠の付与率の算出。

「典拠の付与率」を測る道具。**これまで算出コードが存在せず**、ログと下書き API の
応答フィールドしか無かったので、率を出すことができなかった。

## 定義

- **分母 = 対象項目**（`target_items.resolve_targets`）。**成果物の性質として定義する**
  ため。AI下書きの対象に限ると、一括生成（記述が未入力の項目のみ）か項目単位の再生成かという
  **運用の順序で母集団が動く**。同じ設計成果物でも測るタイミングで数字が変わる指標は、
  評価の指標として説明できない。
- **分子 = 索引に紐づく典拠を1件以上持つ、記述のある項目**。判定は
  `is_distributable_citation` を再利用する（`unit_id` が索引のアドレス形式に一致し、来歴が
  **索引由来**（`payload.file_role == "reference"`）か**配布物由来**（`payload.origin`）である）。
  到達を確認できない自由文（`unit_id` が「紙の院内マニュアル 第3版 p.7」のようなもの）を数えると
  付与率は「何か書いた率」になる。

  **この分子が測っているのは「索引に紐づく来歴を持つ典拠」であって、「人が原典に到達できるか」
  そのものではない。** 保存時に `unit_id` を索引で照合し、実在するなら payload を
  索引から導出するので、人が UI で索引の正しいアドレスを貼った典拠も分子に入る。
  **ただし既存レコードは遡及照合されない。** 保存し直したときに洗浄されるかは
  **アドレスが索引に実在するかで決まる**——**向きの正本は `citation_coverage` の docstring**
  に書いてある。要点だけ言えば、索引に無いアドレスの記録は
  規則2 が残すので洗浄されず、実在するアドレスの記録は規則1 が洗浄する。
  **保存時の照合が入る前に作られた典拠を含む記録は、保存し直した後も比較に使えない**
  （前者が残るうえ、後者は保存し直した項目だけ動くので分子が測定時点で動く）。
  洗浄には一度きりの再照合が要る。
  **索引に載っていない文書（紙の資料）を典拠にした入力は依然として分子に入らない**——
  それは定義どおりだが、「人が原典に到達できるか」を測りたいなら別の指標が要る。

- **記述（`annotation`）が空の項目は分子に入れない。** 典拠は記述の根拠なので、根拠づける対象が
  無い典拠を「付与」と数えない。Extension への配布も記述がある項目に限っているので、
  そちらと揃える。**この条件は分子の定義の一部なので、呼び出し側ではなくこの関数の中で判定する**
  （外に置くと、呼び出し側でフィルタを1行足すだけで分子が黙って変わる）。

**率は `citation_found_items / target_items`**。ここでは件数だけを返す（丸め方を規則に持ち込ま
ないため）。

## 算出の出どころ

永続化された `clinical_annotations.citations` だけから計算する。実行時のログや API 応答に
依存しないので、**いつ測っても同じ値が出る**（分母の定義とセットの要件）。
"""

from __future__ import annotations

from typing import Any

from kawasemi.utils.file_export import is_distributable_citation


def citation_coverage(
    target_ids: list[str],
    annotations_by_link_id: dict[str, dict[str, Any]],
    expected_target_items: int | None = None,
) -> dict[str, Any]:
    """典拠の付与率の分母・分子と、差を追うための内訳を返す。

    Args:
        target_ids: 対象項目の link_id（`target_items.resolve_targets(...).link_ids`）
        annotations_by_link_id: 項目ごとの記述と典拠。
            `{link_id: {"annotation": 記述, "citations": [典拠, ...]}}`。
            典拠は `ClinicalAnnotation.citation_list` の形。**記述の条件もこの関数が判定する**
            （分子の定義の一部を呼び出し側に散らさないため）。
        expected_target_items: 期待する対象項目数（`resolve_targets(...).expected`）。
            省略すると `len(target_ids)`（設定の無い様式の既定と同じ）。

    Returns:
        件数の dict。率は `citation_found_items / target_items` で求める。

    **洗浄の向きの正本はここである**（モジュールの docstring ではなく、分子を作る関数の
    そばに置く）。

    **既存レコードが洗浄されるかはアドレスで決まる。**

    - **索引に実在するアドレス**の記録は、保存し直すと規則1（`routes._verified_citation`）が
      payload を索引から導出するので**洗浄され、分子に入る**（索引を差し替えた窓では
      規則2 が記録を残すので洗浄されない）
    - **索引に無いアドレス**の記録は、規則2 が payload をそのまま残すので**洗浄されない**
      （他人の確認帰属を奪わないための規則）。洗浄には一度きりの再照合が要る

    **主語を書かずに「保存し直しても洗浄されない」と書いてはいけない。** 保存時の照合が入る前に
    作られた典拠を含む記録が比較に使えないのは、後者が残るうえ、前者が洗浄されるかが
    **どの項目を保存し直したか**に依存して**分子が測定時点で動く**からである。
    """
    found: list[str] = []
    any_citation: list[str] = []
    ai_citation = 0
    citations_total = 0
    for link_id in target_ids:
        entry = annotations_by_link_id.get(link_id)
        if not isinstance(entry, dict):
            continue
        # **記述のある項目に限る**（典拠は記述の根拠。根拠づける対象が無い典拠を「付与」と
        # 数えない）。判定は**許可リスト**（空でない文字列だけを記述と認める）で、Extension への
        # 配布側（`routes.export_clinical_extension` の `if a.annotation`）と同じ規則にする。
        # 保存 API は `_clean_text` で strip して空文字を None に落とすので、空白のみの記述は
        # この経路では作れない。
        annotation = entry.get("annotation")
        if not isinstance(annotation, str) or not annotation:
            continue
        citations = [c for c in entry.get("citations") or [] if isinstance(c, dict)]
        if not citations:
            continue
        citations_total += len(citations)
        any_citation.append(link_id)
        if any(is_distributable_citation(c) for c in citations):
            found.append(link_id)
        if any(c.get("llm_model") for c in citations):
            ai_citation += 1

    return {
        # 分母（設定の件数と、その問診票で見つかった数）
        "expected_target_items": (
            len(target_ids) if expected_target_items is None else expected_target_items
        ),
        "target_items": len(target_ids),
        # **分子**: 索引に紐づく典拠を1件以上持つ項目
        "citation_found_items": len(found),
        "citation_found_link_ids": found,
        # 内訳（分子との差が追えるように）
        "items_with_any_citation": len(any_citation),
        # 索引に紐づかない典拠しか無い項目（自由文のみ、または人が UI で貼ったもの）。
        # **「検証できない」ではなく「索引に紐づいていない」**——名前を実態に合わせている。
        "items_with_unlinked_citation_only": len(any_citation) - len(found),
        # AI が典拠を差し込んだ項目（参考値）。**「AI下書きを実行した項目数」ではない**——
        # 設計ロールの来歴（`ai_input_design_llm_calls`）はコール単位で link_id を持たないので、
        # 項目単位では復元できない。ここは「典拠に `llm_model` が付いた項目」を数える。
        "items_with_ai_citation": ai_citation,
        # 典拠の総数（項目単位ではない）
        "citations_total": citations_total,
    }
