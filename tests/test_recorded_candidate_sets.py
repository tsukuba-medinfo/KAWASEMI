"""測定の記録が**提示した候補集合**を持つこと

施設側の測定スクリプトは、測定時に LLM へ提示した候補集合を
**索引の再検索で作り直していた**。`cand_id` は `draft()` 全体を通した連番なので「同じ順序で
振り直せば `cand_id → unit_id` が復元できる」という前提である。

そのため次の3つを代理指標として検査していた——記録の定数（`_TOP_K` / `_MIN_SCORE` /
`_CHUNK_SIZE`）と実装の一致、索引と測定記録の一致、`embedding_model` の一致。

**しかし `cand_id` が範囲内で「別の unit へ解決した」場合は誰も検出できない。** 索引の内容が
同じでも埋め込みの数値が僅かに動けば順位は入れ替わりうる（`build_info` はチャンク長も
収録文書一覧も持たない）。この再検索に依存すると、`_TOP_K` を触って一致率が大きく
崩れても**記録の見た目に出ない**。

**提示そのものを記録すれば、代理検査は要らなくなる。**
"""

from __future__ import annotations

from kawasemi.ai_input.resource_index.schema import Kind, ResourceHit, ResourceUnit
from kawasemi.ai_input.schemas import ClinicalAnnotationDraftCandidate, ClinicalAnnotationDraftItem


def _hit(cand_id: int, unit_id: str, score: float) -> ResourceHit:
    return ResourceHit(
        cand_id=cand_id,
        unit=ResourceUnit(
            unit_id=unit_id,
            kind=Kind.FILE,
            label="入力マニュアル.pdf",
            snippet="抜粋",
            search_text="本文",
            payload={"file_role": "reference", "path": "入力マニュアル.pdf"},
        ),
        score=score,
    )


def test_the_candidate_carries_the_presented_cand_id() -> None:
    """候補が**提示したときの番号**を持つこと。

    モデルはこの番号で候補を選ぶ（`chosen_cand_id`）。番号を持たせずに再検索で振り直すと、
    埋め込みの僅かな差で順位が入れ替わっても**誰も気づけない**。
    """
    from kawasemi.ai_input.clinical_drafter import AIClinicalDrafter

    candidate = AIClinicalDrafter._to_candidate(_hit(7, "file:manual.pdf#p1:0-800", 0.91))

    assert candidate.cand_id == 7
    assert candidate.unit_id == "file:manual.pdf#p1:0-800"
    assert candidate.score == 0.91


def test_the_recorded_shape_maps_cand_id_to_unit_id() -> None:
    """記録の形が `cand_id → unit_id` を**一意に**引けること。

    これが 測定スクリプトの再検索を不要にする条件である。`cand_id` の重複があると
    「別の unit へ解決した」形を作れてしまうので、そこも見る。
    """
    item = ClinicalAnnotationDraftItem(
        link_id="item1",
        candidates=[
            ClinicalAnnotationDraftCandidate(
                cand_id=0, unit_id="file:a.pdf#p1:0-800", source="a", score=0.9
            ),
            ClinicalAnnotationDraftCandidate(
                cand_id=1, unit_id="file:b.pdf#p2:0-800", source="b", score=0.8
            ),
        ],
    )

    recorded = [
        {"cand_id": c.cand_id, "unit_id": c.unit_id, "score": c.score} for c in item.candidates
    ]

    by_cand = {c["cand_id"]: c["unit_id"] for c in recorded}
    assert len(by_cand) == len(recorded), "cand_id が重複している（解決が一意にならない）"
    assert by_cand[1] == "file:b.pdf#p2:0-800"


def test_the_candidate_without_a_cand_id_is_representable() -> None:
    """`cand_id` が無い（この列より前の）記録も表現できること。

    既定を必須にすると、古い記録の読み込みが `ValidationError` になり**過去の測定が
    読めなくなる**。**受けは緩く**する（「要求は厳しく、受けは緩く」）。
    """
    candidate = ClinicalAnnotationDraftCandidate(unit_id="file:a.pdf#p1:0-800", source="a")

    assert candidate.cand_id is None


def test_the_pass_record_shape_is_documented() -> None:
    """`pass*.json` に写す形（`candidates_by_link`）が、測定スクリプトの再検索を不要にすること。

    ここで固定するのは**形**である（実際の書き出しは施設側の測定スクリプトの側で見る）。
    `cand_id → unit_id` が引ければ、測定スクリプトは
    索引を再検索しなくてよくなり、代理検査3件（定数の一致・索引の一致・`embedding_model` の
    一致）も要らなくなる。
    """
    recorded = {
        "item1": [
            {"cand_id": 0, "unit_id": "file:a.pdf#p1:0-800", "score": 0.9},
            {"cand_id": 1, "unit_id": "file:b.pdf#p2:0-800", "score": 0.8},
        ]
    }

    # 測定時に LLM が選んだ番号（`chosen_cand_id`）から unit_id を引く
    chosen = 1
    by_cand = {c["cand_id"]: c["unit_id"] for c in recorded["item1"]}

    assert by_cand[chosen] == "file:b.pdf#p2:0-800"
    assert set(by_cand) == {0, 1}, "番号が飛んでいると解決できない範囲が生まれる"
