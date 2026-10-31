"""来歴からの `duplicated_response` の数え直し

`save_clinical` は**保存の瞬間**に来歴（`DesignLlmCall.duplicated_link_ids`）を引いて
このフラグを立てる。**保存より後に来歴が入った場合、遡って立たない。**

自動生成を別サーバで実行し、人の入力を別の DB で行ってから統合する運用では、
人の入力を保存する側に来歴が1行も無い。すべて false で入り、来歴を統合しても false のまま
残るので、**矛盾した応答だった項目が普通の項目として集計に入る**（矛盾した応答の別掲が効かない）。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.models import (
    AnnotationStatus,
    ClinicalAnnotation,
    DesignLlmCall,
    DesignRole,
)
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from scripts.recompute_duplicated_response import _render, database_identity, recompute


async def _questionnaire(db_session: AsyncSession, fhir_id: str = "q-recompute") -> Questionnaire:
    q = Questionnaire(
        fhir_id=fhir_id,
        title="数え直しのテスト票",
        status="active",
        content={"resourceType": "Questionnaire", "id": fhir_id, "item": []},
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    return q


def _call(questionnaire_id: int, link_ids: list[str]) -> DesignLlmCall:
    """一括生成1周ぶんの来歴（`duplicated_link_ids` だけが本題）。"""
    return DesignLlmCall(
        questionnaire_id=questionnaire_id,
        role=DesignRole.CLINICAL,
        label="臨床的意味の下書き",
        model="model-a:12b",
        options={"seed": 0, "num_ctx": 32768, "temperature": 0},
        system_prompt="(略)",
        user_message="(略)",
        raw_output="{}",
        chunk_index=0,
        item_count=2,
        truncated=False,
        content_sources=[],
        duplicated_link_ids=link_ids,
    )


def _annotation(
    questionnaire_id: int, link_id: str, *, duplicated: bool = False
) -> ClinicalAnnotation:
    return ClinicalAnnotation(
        questionnaire_id=questionnaire_id,
        link_id=link_id,
        annotation="人手で書いた記述",
        citations=[],
        status=AnnotationStatus.DRAFT,
        duplicated_response=duplicated,
    )


@pytest.mark.asyncio
async def test_it_raises_the_flag_for_links_in_the_provenance(db_session: AsyncSession) -> None:
    """**来歴を後から入れた DB で立つこと**（このテストの眼目）。"""
    q = await _questionnaire(db_session)
    db_session.add_all([_annotation(q.id, "item1"), _annotation(q.id, "item2")])
    db_session.add(_call(q.id, ["item1"]))
    await db_session.commit()

    result = await recompute(db_session, apply=True)

    assert result["raised"] == 1
    assert result["targets"] == [f"{q.id}:item1"]
    rows = (await db_session.execute(select(ClinicalAnnotation))).scalars().all()
    assert {a.link_id: a.duplicated_response for a in rows} == {"item1": True, "item2": False}


@pytest.mark.asyncio
async def test_the_dry_run_writes_nothing(db_session: AsyncSession) -> None:
    """既定は下見。**1行も書き込まない。**"""
    q = await _questionnaire(db_session)
    annotation = _annotation(q.id, "item1")
    db_session.add(annotation)
    db_session.add(_call(q.id, ["item1"]))
    await db_session.commit()

    result = await recompute(db_session, apply=False)

    assert result["applied"] is False
    assert result["raised"] == 1, "下見でも「立てる対象」は数える"
    await db_session.refresh(annotation)
    assert annotation.duplicated_response is False


@pytest.mark.asyncio
async def test_it_is_idempotent(db_session: AsyncSession) -> None:
    """2回目は0件（統合のたびに回しても結果が変わらない）。"""
    q = await _questionnaire(db_session)
    db_session.add(_annotation(q.id, "item1"))
    db_session.add(_call(q.id, ["item1"]))
    await db_session.commit()

    first = await recompute(db_session, apply=True)
    second = await recompute(db_session, apply=True)

    assert first["raised"] == 1
    assert second["raised"] == 0
    assert second["already_marked"] == 1


@pytest.mark.asyncio
async def test_it_never_lowers_a_raised_flag(db_session: AsyncSession) -> None:
    """**一度立ったら下げない**（`save_clinical` と同じ規則）。

    別の DB から来た来歴だけを見ている場合、そこに現れない項目がある。それを false へ
    戻すと、統合の順序によって値が変わる（記録が実行順に依存する）。
    """
    q = await _questionnaire(db_session)
    db_session.add(_annotation(q.id, "item1", duplicated=True))
    db_session.add(_call(q.id, []))  # 来歴には1件も現れない
    await db_session.commit()

    result = await recompute(db_session, apply=True)

    assert result["raised"] == 0
    row = (await db_session.execute(select(ClinicalAnnotation))).scalar_one()
    assert row.duplicated_response is True


@pytest.mark.asyncio
async def test_it_does_not_cross_questionnaires(db_session: AsyncSession) -> None:
    """同じ link_id でも別の問診票へは波及しないこと。"""
    q1 = await _questionnaire(db_session, "q-recompute-1")
    q2 = await _questionnaire(db_session, "q-recompute-2")
    db_session.add_all([_annotation(q1.id, "item1"), _annotation(q2.id, "item1")])
    db_session.add(_call(q1.id, ["item1"]))
    await db_session.commit()

    result = await recompute(db_session, apply=True)

    assert result["targets"] == [f"{q1.id}:item1"]


# =============================================================================
# 失敗を「0件」に化けさせない（統合の採番ずれ）
# =============================================================================


@pytest.mark.asyncio
async def test_it_reports_provenance_that_matches_no_record(db_session: AsyncSession) -> None:
    """**来歴にあって記録側に無い組を出すこと**（この script の最も危険な失敗）。

    DB を統合する運用が前提なのに、突き合わせのキーは `questionnaire_id`（採番）である。
    統合で採番がずれると、立てるべき項目に1件も当たらないまま **「新たに立てる項目 0件」**
    で正常終了し、**冪等な2回目の実行と見分けが付かない**。照合できなかった来歴を数えて
    出すことで、その状態が表に出る。
    """
    q = await _questionnaire(db_session)
    db_session.add(_annotation(q.id, "item1"))
    # 来歴は「別の DB での採番」を指している（item1 は q.id + 1 側にある、という食い違い）
    db_session.add(_call(q.id + 1, ["item1", "item2"]))
    await db_session.commit()

    result = await recompute(db_session, apply=True)

    assert result["raised"] == 0, "前提: 立つ項目は無い（0件で正常終了して見える）"
    assert result["unmatched"] == [f"{q.id + 1}:item1", f"{q.id + 1}:item2"]


@pytest.mark.asyncio
async def test_the_matched_links_are_not_reported_as_unmatched(db_session: AsyncSession) -> None:
    """正常な実行では `unmatched` が空であること（警告が常時出ると意味を失う）。"""
    q = await _questionnaire(db_session)
    db_session.add(_annotation(q.id, "item1"))
    db_session.add(_call(q.id, ["item1"]))
    await db_session.commit()

    result = await recompute(db_session, apply=True)

    assert result["raised"] == 1
    assert result["unmatched"] == []


@pytest.mark.asyncio
async def test_the_labels_carry_the_fhir_id(db_session: AsyncSession) -> None:
    """`fhir_id` を併記すること（採番ずれを人が突き合わせる手掛かり）。

    `questionnaire_id` は採番であって同一性ではない。正本は `fhir_id` である。
    """
    q = await _questionnaire(db_session, "q-recompute-labels")
    db_session.add(_annotation(q.id, "item1"))
    db_session.add(_call(q.id, ["item1"]))
    await db_session.commit()

    result = await recompute(db_session, apply=False)

    assert result["labels"] == {q.id: "q-recompute-labels"}
    assert _render(result["targets"], result["labels"]) == f"{q.id}(q-recompute-labels):item1"


def test_the_render_falls_back_to_the_raw_pair() -> None:
    """`fhir_id` が引けない問診票（統合で片方にしか無い等）でも組を落とさないこと。"""
    assert _render(["7:item1"], {}) == "7:item1"


# =============================================================================
# どの DB に当てたかを記録できること
# =============================================================================


def test_the_database_identity_names_the_sqlite_file(monkeypatch: pytest.MonkeyPatch) -> None:
    """sqlite は**実体のパス**まで出すこと。

    件数だけを記録しても、統合の運用ではどちらの DB に当てたのかが後から確定できない。
    symlink や `..` は `settings` の正規化では解けないので、ここで解いて記録する。
    """
    monkeypatch.setattr(settings, "database_url", "sqlite+aiosqlite:///./kawasemi.db")

    identity = database_identity()

    assert identity.startswith("sqlite+aiosqlite:///./kawasemi.db")
    assert str(Path("./kawasemi.db").resolve()) in identity


def test_the_database_identity_does_not_repeat_a_resolved_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """既に解決済みのパスなら**実体を二重に出さない**（読み手の負担を増やさない）。"""
    db_path = tmp_path / "kawasemi.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite+aiosqlite:///{db_path}")

    assert database_identity() == f"sqlite+aiosqlite:///{db_path}"


def test_the_database_identity_hides_the_password(monkeypatch: pytest.MonkeyPatch) -> None:
    """**資格情報を記録へ落とさないこと。**

    この出力は「記録に残すこと」と書いて人に貼らせるものなので、URL をそのまま
    印字すると、パスワードが チケットのコメントや実験ノートへ残る。
    """
    monkeypatch.setattr(
        settings, "database_url", "postgresql+asyncpg://kawasemi:s3cret@db.example.jp/kawasemi"
    )

    identity = database_identity()

    assert "s3cret" not in identity
    assert "db.example.jp/kawasemi" in identity
