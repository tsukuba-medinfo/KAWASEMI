"""矛盾した応答（同じ項目に複数エントリ）の永続化

「同じ項目に2エントリ返った応答は**典拠つきの分子からも「規約文書を引いても意味が決まらなかった項目」の
件数からも外して別掲する**」という集計の規則があるが、`duplicated_link_ids` は**どこにも永続化されて
いなかった**（プロセス内の戻り値・その HTTP 応答・ログの3つだけ）。

したがって**永続化された `clinical_annotations.citations` から数える経路**——
`citation_metrics.citation_coverage` と集計エンドポイント——は矛盾した応答を外せず、
「矛盾を外した分子は測定時の run の記録が手元にある間だけ再現できる」という非対称が
残っていた（指標は「いつ測っても同じ値が出る」ことを要件にしている）。

**`citation_coverage` の分子の定義は変えない。** 別掲できる材料を DB に持たせるのがこの変更である。
"""

from __future__ import annotations

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.design_provenance import DesignLlmRecorder, save_design_llm_calls
from kawasemi.ai_input.llm_json import LLMCallRecord
from kawasemi.ai_input.models import ClinicalAnnotation, DesignLlmCall, DesignRole
from kawasemi.ai_input.routes import _duplicated_link_ids


def _record(label: str = "臨床的意味AI下書き") -> LLMCallRecord:
    return LLMCallRecord(
        label=label,
        model="model-a:12b",
        provider="ollama",
        options={"temperature": 0},
        system_prompt="s",
        user_message="u",
        raw_output="{}",
        error=None,
        chunk_index=0,
        item_count=2,
    )


async def test_the_recorder_declares_duplicated_link_ids() -> None:
    """`declare_duplicated` が**空でも**記録すること。

    **空配列と null は意味が違う**——空は「マージして重複が無かった」、null は
    「記録していない」（この列が無かった窓の記録、または宣言しないロール）。
    """
    recorder = DesignLlmRecorder(DesignRole.CLINICAL)
    assert recorder.duplicated_link_ids is None

    recorder.declare_duplicated([])
    assert recorder.duplicated_link_ids == [], "空を null に丸めている"

    recorder.declare_duplicated(["item2", "item1", "item2"])
    assert recorder.duplicated_link_ids == ["item1", "item2"], "重複と順序を正規化していない"


@pytest.mark.asyncio
async def test_the_duplicated_link_ids_are_saved_to_the_provenance(
    db_session: AsyncSession,
) -> None:
    """来歴の行に矛盾した応答の link_id が残ること（**サーバ側の真実**）。"""
    recorder = DesignLlmRecorder(DesignRole.CLINICAL)
    recorder(_record())
    recorder.declare_duplicated(["item1"])

    await save_design_llm_calls(
        db_session, questionnaire_id=1, recorder=recorder, created_by_id=None
    )

    rows = await db_session.execute(select(DesignLlmCall.duplicated_link_ids))
    assert rows.scalars().all() == [["item1"]]


@pytest.mark.asyncio
async def test_the_flag_is_read_from_the_server_side_record(db_session: AsyncSession) -> None:
    """保存時の判定は**来歴から引く**こと（クライアントの申告に依存しない）。

    `origin` の自己申告と同じく、クライアントが送る値を事実として保存すると、
    後から「主張」と「観測」を区別できなくなる。
    """
    recorder = DesignLlmRecorder(DesignRole.CLINICAL)
    recorder(_record())
    recorder.declare_duplicated(["item1"])
    await save_design_llm_calls(
        db_session, questionnaire_id=7, recorder=recorder, created_by_id=None
    )

    got = await _duplicated_link_ids(db_session, 7)

    assert got == {"item1"}
    assert await _duplicated_link_ids(db_session, 999) == set(), "別の問診票の記録を拾っている"


@pytest.mark.asyncio
async def test_the_annotation_carries_the_flag(db_session: AsyncSession) -> None:
    """`clinical_annotations` の列として**項目単位に**残ること。

    測定の記録（`record.json`）を持たない環境でも `SELECT` 一発で別掲できることが目的である。
    """
    annotation = ClinicalAnnotation(
        questionnaire_id=1,
        link_id="item1",
        annotation="記述",
        citations=[],
        duplicated_response=True,
    )
    db_session.add(annotation)
    await db_session.commit()

    rows = await db_session.execute(
        select(ClinicalAnnotation.duplicated_response).where(ClinicalAnnotation.link_id == "item1")
    )
    assert rows.scalar_one() is True


@pytest.mark.asyncio
async def test_the_flag_defaults_to_false(db_session: AsyncSession) -> None:
    """既定は False（**null にしない**）。集計で三値を扱わずに済むようにする。"""
    annotation = ClinicalAnnotation(
        questionnaire_id=1, link_id="item2", annotation="記述", citations=[]
    )
    db_session.add(annotation)
    await db_session.commit()

    rows = await db_session.execute(
        select(ClinicalAnnotation.duplicated_response).where(ClinicalAnnotation.link_id == "item2")
    )
    assert rows.scalar_one() is False
