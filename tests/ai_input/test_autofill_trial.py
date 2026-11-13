"""autofill の試行(trial)/本番(production)分離テスト

設計の確定性で正しさが担保されるシステム処理として、autofill は実行コンテキストで
区別する:
- 設計クラスタ（収集責任者／取得計画設計者／データ管理者）の実行 = 試行（揮発）。
  結果は永続せず、PHI を含まない InputTrialRun（メタのみ）を残す。
- 収集者(data_entry)の実行 = 本番。InputExecution に永続する。
- それ以外のアクティブ役割（例: admin 素のまま）は autofill 実行不可（403）。
  admin は役割を data_entry 等へ切り替えて入る。
"""

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.models import InputExecution, InputPlan, InputTrialRun
from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import RoleType
from kawasemi.models.user import User
from tests.lease_helpers import lease_patient

# 非LLMの確定済みプラン（EXTRACTION フラグのガードに引っかからない）。
# 実行が成功/失敗どちらでも記録（InputExecution / InputTrialRun）は作られる。
SQL_PROGRAM = {"steps": [{"op": "sql_extract", "id": "s1", "sql": "SELECT 1"}], "return": "$s1"}


def _act_as(client: AsyncClient, user: User, role: str) -> None:
    client.cookies.set(
        settings.jwt_cookie_name, create_access_token(data={"sub": str(user.id), "act": role})
    )


async def _confirmed_plan(db: AsyncSession, fhir_id: str = "q-trial") -> Questionnaire:
    q = Questionnaire(
        fhir_id=fhir_id,
        title="試行テスト票",
        status="active",
        content={"resourceType": "Questionnaire", "id": fhir_id},
    )
    db.add(q)
    await db.flush()
    db.add(
        InputPlan(
            questionnaire_id=q.id,
            link_id="item1",
            item_text="テスト項目",
            program=SQL_PROGRAM,
            status="confirmed",
        )
    )
    await db.commit()
    return q


async def _count(db: AsyncSession, model: type) -> int:
    return (await db.execute(select(func.count()).select_from(model))).scalar_one()


@pytest.mark.asyncio
async def test_design_role_autofill_is_trial_not_persisted(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """設計役割の autofill は試行: InputExecution を作らず InputTrialRun のみ残す

    **試行にも患者スコープがある**。設計クラスタは患者割り当てを持たないので、
    試行で使えるのは**合成・ホワイトリスト患者**だけである。ここでは対象患者を許可リストへ
    入れて、従来の試行の振る舞い（永続しない）を見る。
    """
    monkeypatch.setattr(settings, "trial_run_patient_ids", "PAT-1")
    q = await _confirmed_plan(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/autofill",
        json={"patient_id": "PAT-1"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["trial"] is True
    assert body["execution_id"] is None
    assert await _count(db_session, InputExecution) == 0
    assert await _count(db_session, InputTrialRun) == 1


@pytest.mark.asyncio
async def test_data_entry_autofill_is_production_persisted(
    collector_client: AsyncClient,
    db_session: AsyncSession,
    collector_user: User,
    csrf_token: str,
) -> None:
    """収集者の autofill は本番: InputExecution に永続し InputTrialRun は作らない"""
    q = await _confirmed_plan(db_session)
    # 本番 autofill は担当（有効なリース）の患者に限定されるためリースを用意する。
    await lease_patient(db_session, collector_user.id, "PAT-1", questionnaire_id=q.id)
    resp = await collector_client.post(
        f"/ai-input/questionnaires/{q.id}/autofill",
        json={"patient_id": "PAT-1"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["trial"] is False
    assert body["execution_id"] is not None
    assert await _count(db_session, InputExecution) == 1
    assert await _count(db_session, InputTrialRun) == 0


@pytest.mark.asyncio
async def test_data_entry_autofill_blocked_for_unassigned_patient(
    collector_client: AsyncClient,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """本番 autofill の患者スコープ: 担当（有効なリース）の無い患者は 403 で実行拒否。"""
    q = await _confirmed_plan(db_session, fhir_id="q-trial-unassigned")
    resp = await collector_client.post(
        f"/ai-input/questionnaires/{q.id}/autofill",
        json={"patient_id": "PAT-UNASSIGNED"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 403, resp.text
    assert "担当" in resp.json()["message"]
    assert await _count(db_session, InputExecution) == 0


@pytest.mark.asyncio
async def test_autofill_forbidden_for_admin_active_role(
    admin_client: AsyncClient,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """素の admin（アクティブ役割 admin）は autofill 実行不可（403）。

    autofill は設計クラスタ＋収集者のみ。admin は役割切替で入る。
    """
    q = await _confirmed_plan(db_session)
    resp = await admin_client.post(
        f"/ai-input/questionnaires/{q.id}/autofill",
        json={"patient_id": "PAT-1"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_batch_autofill_is_collector_only(
    client: AsyncClient,
    admin_user: User,
    designer_user: User,
    reviewer_user: User,
    csrf_token: str,
) -> None:
    """バッチ autofill 起動は収集者限定（管理者からも外した）。管理者・設計者・閲覧検証者は 403。

    認可は依存解決時（ハンドラ本体の前）に効くため、データセットの実在は問わない。
    """
    for user in (admin_user, designer_user, reviewer_user):
        client.cookies.set(
            settings.jwt_cookie_name, create_access_token(data={"sub": str(user.id)})
        )
        resp = await client.post(
            "/datasets/1/batch-autofill",
            data={"questionnaire_id": "1"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert resp.status_code == 403, user.username


# --- 試行実行の患者スコープ ----------------------------------------------------


@pytest.mark.asyncio
async def test_trial_autofill_refuses_a_patient_outside_the_whitelist(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**設計ロールの試行でも、許可リスト外の患者は拒否する**（CWE-639）。

    以前の門は `not _is_trial_run(user) and not await user_can_access_patient(...)` で、
    設計クラスタのアクティブ役割なら**患者スコープ認可が一切評価されなかった**。
    `patient_id` は要求者が選べるので、確定プランのある Questionnaire について
    **任意患者の抽出済み臨床データと evidence** を受け取れた。

    許可リストは合成・デモ患者のためのもので、そこに無い患者は割り当てが必要になる。
    """
    monkeypatch.setattr(settings, "trial_run_patient_ids", "SYNTHETIC-1")
    q = await _confirmed_plan(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/autofill",
        json={"patient_id": "REAL-PATIENT-1"},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 403, resp.text
    assert await _count(db_session, InputTrialRun) == 0, "拒否したのに試行記録が残っている"


@pytest.mark.asyncio
async def test_trial_autofill_stream_refuses_a_patient_outside_the_whitelist(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**ストリーム側も同じ門を通ること**。

    `/autofill` と `/autofill/stream` は同一原因の別経路で、片方だけ直すと NDJSON 経由で
    同じ PHI が出る。**2つの門は同じ関数を呼ぶ**形にしてある。
    """
    monkeypatch.setattr(settings, "trial_run_patient_ids", "SYNTHETIC-1")
    q = await _confirmed_plan(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/autofill/stream",
        json={"patient_id": "REAL-PATIENT-1"},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 403, resp.text


@pytest.mark.asyncio
async def test_trial_autofill_still_works_for_an_assigned_patient(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """許可リストが空でも、**割り当てのある患者なら試行できる**。

    許可リストは追加の許しであって、割り当てを置き換えるものではない。これが無いと
    「許可リストを設定しないと試行が一切できない」運用になる。
    """
    monkeypatch.setattr(settings, "trial_run_patient_ids", "")
    q = await _confirmed_plan(db_session)
    await lease_patient(db_session, designer_user.id, "PAT-ASSIGNED", questionnaire_id=q.id)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/autofill",
        json={"patient_id": "PAT-ASSIGNED"},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 200, resp.text
    assert resp.json()["trial"] is True
