"""入力対象の開始（ロック）の認可

`POST /input-targets/{id}/start` は `started_by_id = current_user.id` を書く。
`core/authz.user_can_access_patient` は **`started_by_id == user.id` を患者 PHI 認可の根拠**に
するので、このルートは**アクセス制御のゲート自体を自力でブートストラップできる書き込み**である。

以前はロール検査も割り当て検査も無く（`RequireAuthenticated` のみ）、`GET /input-targets` も
同様だったため、手動入力を行うべきでない低権限ユーザ（data_user / verifier / design 系）が
pending な対象を総当たりで start し、対応する `patient_id` の FHIR PHI（氏名・生年月日・性別）を
`GET /api/patient/{patient_id}` から読めた（CWE-639）。

ここでは「ロールで絞る」と「明示的な割り当てを要求する」の2層を固定する。
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.models.case_assignment import CaseAssignment
from kawasemi.models.input_target import InputTarget, InputTargetStatus
from kawasemi.models.user import User


async def _pending_target(db: AsyncSession, patient_id: str = "P-115") -> InputTarget:
    """pending な入力対象を1件作る（データセット・様式はダミー参照。テストDBは FK 非強制）。"""
    target = InputTarget(
        dataset_id=1,
        questionnaire_id=1,
        patient_id=patient_id,
        status=InputTargetStatus.PENDING,
    )
    db.add(target)
    await db.commit()
    await db.refresh(target)
    return target


async def _reload(db: AsyncSession, target_id: int) -> InputTarget:
    got = await db.execute(select(InputTarget).where(InputTarget.id == target_id))
    return got.scalar_one()


@pytest.mark.asyncio
async def test_data_user_cannot_list_input_targets(reviewer_client: AsyncClient) -> None:
    """データ利用者は入力対象一覧を見られない（一覧は patient_id を含み start の入口になる）。"""
    resp = await reviewer_client.get("/input-targets")
    assert resp.status_code == 403, resp.status_code


@pytest.mark.asyncio
async def test_data_user_cannot_start_an_input_target(
    reviewer_client: AsyncClient, db_session: AsyncSession, csrf_token: str
) -> None:
    """データ利用者は入力対象を開始できない（＝自分に所有権行を作れない）。"""
    target = await _pending_target(db_session, "P-115-DENY")

    resp = await reviewer_client.post(
        f"/input-targets/{target.id}/start", headers={"X-CSRF-Token": csrf_token}
    )

    assert resp.status_code == 403, resp.status_code
    after = await _reload(db_session, target.id)
    assert after.started_by_id is None, "拒否したのに所有権行が書かれている"
    assert after.status == InputTargetStatus.PENDING


@pytest.mark.asyncio
async def test_data_entry_cannot_start_an_unassigned_target(
    collector_client: AsyncClient, db_session: AsyncSession, csrf_token: str
) -> None:
    """**割り当てのない対象は data_entry でも開始できない**（自己起点の所有権を根拠にしない）。

    ロール制限だけでは data_entry 同士で任意の対象を取れる。`user_can_access_patient` が
    `started_by_id` を認可の根拠にしている以上、**認可の根拠を自分で作れる経路は残さない**。
    """
    target = await _pending_target(db_session, "P-115-UNASSIGNED")

    resp = await collector_client.post(
        f"/input-targets/{target.id}/start", headers={"X-CSRF-Token": csrf_token}
    )

    assert resp.status_code == 303
    assert "forms" not in resp.headers.get("location", ""), "入力画面へ通してしまっている"
    after = await _reload(db_session, target.id)
    assert after.started_by_id is None, "拒否したのに所有権行が書かれている"
    assert after.status == InputTargetStatus.PENDING


@pytest.mark.asyncio
async def test_data_entry_can_start_an_assigned_target(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """割り当て（`CaseAssignment`）があれば従来どおり開始できる（正常系が死んでいないこと）。"""
    target = await _pending_target(db_session, "P-115-ASSIGNED")
    db_session.add(
        CaseAssignment(
            questionnaire_id=target.questionnaire_id,
            patient_id=target.patient_id,
            assigned_to_id=collector_user.id,
        )
    )
    await db_session.commit()

    resp = await collector_client.post(
        f"/input-targets/{target.id}/start", headers={"X-CSRF-Token": csrf_token}
    )

    assert resp.status_code == 303
    assert "forms" in resp.headers.get("location", ""), resp.headers.get("location")
    after = await _reload(db_session, target.id)
    assert after.started_by_id == collector_user.id
    assert after.status == InputTargetStatus.IN_PROGRESS
