"""バッチAI自動入力 / チェックモードのテスト

外部サービス（Ollama / FHIR / DWH）に触れないよう DSLInterpreter をフェイクに差し替え、
バッチランナーの状態遷移・進捗集計と、チェックモードの画面/確定/フラグ動線を検証する。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import kawasemi.ai_input.batch as batch_mod
import kawasemi.web.dataset_routes as dataset_routes
from kawasemi.ai_input.models import ExecutionStatus, InputExecution, InputPlan, PlanStatus
from kawasemi.models.batch_job import BatchJob, BatchJobStatus
from kawasemi.models.dataset import Dataset, DatasetQuestionnaire
from kawasemi.models.input_target import InputTarget, InputTargetStatus
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.questionnaire_response import QuestionnaireResponse
from kawasemi.models.user import User
from tests.conftest import TestSessionLocal

QUESTIONNAIRE_CONTENT = {
    "resourceType": "Questionnaire",
    "id": "q-batch",
    "status": "active",
    "item": [
        {"linkId": "name", "text": "氏名", "type": "string"},
        {
            "linkId": "gender",
            "text": "性別",
            "type": "choice",
            "answerOption": [
                {"valueCoding": {"code": "1", "display": "男"}},
                {"valueCoding": {"code": "2", "display": "女"}},
            ],
        },
        {"linkId": "memo", "text": "メモ（手動）", "type": "string"},
    ],
}

_SQL_PROGRAM = {"steps": [{"op": "sql_extract", "id": "s1", "sql": "SELECT 1"}], "return": "$s1"}


class _FakeInterpreter:
    """確認済みプランの link_id ごとに決定的な値を返すフェイク。"""

    def __init__(self) -> None:
        self.closed = False

    async def execute(self, plans, patient_id, db, llm_model_override=None, *, study_id="", **_kw):
        prefill = {p.link_id: f"{p.link_id}-{patient_id}" for p in plans}
        evidence = {
            p.link_id: {
                "trace": [{"op": "sql_extract", "id": "s1", "output": prefill[p.link_id]}],
                "value": prefill[p.link_id],
            }
            for p in plans
        }
        return prefill, evidence

    async def aclose(self) -> None:
        self.closed = True


async def _make_questionnaire(db: AsyncSession) -> Questionnaire:
    q = Questionnaire(
        fhir_id="q-batch",
        title="バッチテスト票",
        status="active",
        content=QUESTIONNAIRE_CONTENT,
    )
    db.add(q)
    await db.flush()
    return q


async def _add_confirmed_plans(db: AsyncSession, qid: int) -> None:
    for link_id in ("name", "gender"):
        db.add(
            InputPlan(
                questionnaire_id=qid,
                link_id=link_id,
                item_text=link_id,
                program=_SQL_PROGRAM,
                status=PlanStatus.CONFIRMED,
            )
        )


async def _make_dataset_with_targets(
    db: AsyncSession, qid: int, patient_ids: list[str], status: str = InputTargetStatus.PENDING
) -> Dataset:
    ds = Dataset(name="バッチDS", description=None, created_by_id=None)
    db.add(ds)
    await db.flush()
    db.add(DatasetQuestionnaire(dataset_id=ds.id, questionnaire_id=qid, display_order=0))
    for pid in patient_ids:
        db.add(InputTarget(dataset_id=ds.id, questionnaire_id=qid, patient_id=pid, status=status))
    await db.flush()
    return ds


# --------------------------------------------------------------------------- runner


@pytest.mark.asyncio
async def test_run_batch_job_drafts_pending_targets(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ランナーは pending を逐次処理し ai_drafted へ遷移、InputExecution と進捗を残す。"""
    monkeypatch.setattr(batch_mod, "DSLInterpreter", _FakeInterpreter)
    monkeypatch.setattr(batch_mod, "AsyncSessionLocal", TestSessionLocal)

    q = await _make_questionnaire(db_session)
    await _add_confirmed_plans(db_session, q.id)
    ds = await _make_dataset_with_targets(db_session, q.id, ["p-001", "p-002", "p-003"])
    job = BatchJob(dataset_id=ds.id, questionnaire_id=q.id, status=BatchJobStatus.RUNNING, total=3)
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    await batch_mod.run_batch_job(job_id)

    # ランナーは別セッションでコミットするため、検証も新しいセッションで読む
    async with TestSessionLocal() as verify:
        targets = (
            (await verify.execute(select(InputTarget).where(InputTarget.dataset_id == ds.id)))
            .scalars()
            .all()
        )
        assert {t.status for t in targets} == {InputTargetStatus.AI_DRAFTED}

        execs = (
            (
                await verify.execute(
                    select(InputExecution).where(InputExecution.batch_job_id == job_id)
                )
            )
            .scalars()
            .all()
        )
        assert len(execs) == 3
        assert all(e.status == ExecutionStatus.SUCCESS for e in execs)
        assert all(e.prefill_data and "name" in e.prefill_data for e in execs)

        refreshed = (
            await verify.execute(select(BatchJob).where(BatchJob.id == job_id))
        ).scalar_one()
        assert refreshed.status == BatchJobStatus.COMPLETED
        assert refreshed.processed == 3
        assert refreshed.succeeded == 3
        assert refreshed.failed == 0


@pytest.mark.asyncio
async def test_run_batch_job_empty_result_is_drafted_not_failed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """値が0件でも例外でなければ ai_drafted に進める（空ドラフトの無限リトライを防ぐ）。"""

    class _EmptyInterpreter(_FakeInterpreter):
        async def execute(
            self, plans, patient_id, db, llm_model_override=None, *, study_id="", **_kw
        ):
            return {}, {}

    monkeypatch.setattr(batch_mod, "DSLInterpreter", _EmptyInterpreter)
    monkeypatch.setattr(batch_mod, "AsyncSessionLocal", TestSessionLocal)

    q = await _make_questionnaire(db_session)
    await _add_confirmed_plans(db_session, q.id)
    ds = await _make_dataset_with_targets(db_session, q.id, ["p-001"])
    job = BatchJob(dataset_id=ds.id, questionnaire_id=q.id, status=BatchJobStatus.RUNNING, total=1)
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    await batch_mod.run_batch_job(job_id)

    async with TestSessionLocal() as verify:
        target = (
            await verify.execute(select(InputTarget).where(InputTarget.dataset_id == ds.id))
        ).scalar_one()
        assert target.status == InputTargetStatus.AI_DRAFTED
        refreshed = (
            await verify.execute(select(BatchJob).where(BatchJob.id == job_id))
        ).scalar_one()
        assert refreshed.failed == 0
        assert refreshed.succeeded == 1


@pytest.mark.asyncio
async def test_run_batch_job_failed_target_stays_pending(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """実行が例外/空のときは failed 集計、対象は pending 維持（次回リトライ可能）。"""

    class _FailingInterpreter(_FakeInterpreter):
        async def execute(
            self, plans, patient_id, db, llm_model_override=None, *, study_id="", **_kw
        ):
            raise RuntimeError("resolver unreachable")

    monkeypatch.setattr(batch_mod, "DSLInterpreter", _FailingInterpreter)
    monkeypatch.setattr(batch_mod, "AsyncSessionLocal", TestSessionLocal)

    q = await _make_questionnaire(db_session)
    await _add_confirmed_plans(db_session, q.id)
    ds = await _make_dataset_with_targets(db_session, q.id, ["p-001"])
    job = BatchJob(dataset_id=ds.id, questionnaire_id=q.id, status=BatchJobStatus.RUNNING, total=1)
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    await batch_mod.run_batch_job(job_id)

    async with TestSessionLocal() as verify:
        target = (
            await verify.execute(select(InputTarget).where(InputTarget.dataset_id == ds.id))
        ).scalar_one()
        assert target.status == InputTargetStatus.PENDING  # 失敗は pending 維持
        refreshed = (
            await verify.execute(select(BatchJob).where(BatchJob.id == job_id))
        ).scalar_one()
        assert refreshed.failed == 1
        assert refreshed.status == BatchJobStatus.COMPLETED


# --------------------------------------------------------------------------- launch guards


@pytest.mark.asyncio
async def test_batch_launch_creates_job(
    collector_client: AsyncClient,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """設計完了の Questionnaire はバッチジョブを作成する（pending 件数を total に）。"""
    launched: list[int] = []
    monkeypatch.setattr(dataset_routes, "launch_batch_job", lambda jid: launched.append(jid))

    q = await _make_questionnaire(db_session)
    await _add_confirmed_plans(db_session, q.id)
    ds = await _make_dataset_with_targets(db_session, q.id, ["p-001", "p-002"])
    await db_session.commit()

    resp = await collector_client.post(
        f"/datasets/{ds.id}/batch-autofill",
        data={"questionnaire_id": str(q.id)},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    job = (
        await db_session.execute(select(BatchJob).where(BatchJob.dataset_id == ds.id))
    ).scalar_one()
    assert job.total == 2
    assert launched == [job.id]


@pytest.mark.asyncio
async def test_batch_launch_rejects_malformed_study_id(
    collector_client: AsyncClient,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """study_id が字種・長さの制約に違反していれば、ジョブを作らずに通知へリダイレクトする。"""
    launched: list[int] = []
    monkeypatch.setattr(dataset_routes, "launch_batch_job", lambda jid: launched.append(jid))

    q = await _make_questionnaire(db_session)
    await _add_confirmed_plans(db_session, q.id)
    ds = await _make_dataset_with_targets(db_session, q.id, ["p-001"])
    await db_session.commit()

    resp = await collector_client.post(
        f"/datasets/{ds.id}/batch-autofill",
        data={"questionnaire_id": str(q.id), "study_id": "study/../etc"},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "notice=" in resp.headers["location"]

    jobs = (
        (await db_session.execute(select(BatchJob).where(BatchJob.dataset_id == ds.id)))
        .scalars()
        .all()
    )
    assert jobs == []
    assert launched == []


@pytest.mark.asyncio
async def test_batch_launch_blocked_when_design_incomplete(
    collector_client: AsyncClient,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未確認(draft)プランが残っていればバッチは起動せず通知へリダイレクトする。"""
    launched: list[int] = []
    monkeypatch.setattr(dataset_routes, "launch_batch_job", lambda jid: launched.append(jid))

    q = await _make_questionnaire(db_session)
    await _add_confirmed_plans(db_session, q.id)
    # 未確認プランを1つ追加 → ready=False
    db_session.add(
        InputPlan(
            questionnaire_id=q.id,
            link_id="memo",
            program=_SQL_PROGRAM,
            status=PlanStatus.DRAFT,
        )
    )
    ds = await _make_dataset_with_targets(db_session, q.id, ["p-001"])
    await db_session.commit()

    resp = await collector_client.post(
        f"/datasets/{ds.id}/batch-autofill",
        data={"questionnaire_id": str(q.id)},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "notice=" in resp.headers["location"]

    jobs = (
        (await db_session.execute(select(BatchJob).where(BatchJob.dataset_id == ds.id)))
        .scalars()
        .all()
    )
    assert jobs == []
    assert launched == []


# --------------------------------------------------------------------------- check mode


async def _seed_ai_drafted(
    db: AsyncSession, qid: int, ds: Dataset, patient_id: str, holder_id: int | None = None
) -> InputTarget:
    """1 件を ai_drafted 状態にし、対応する InputExecution（ドラフト）を作る。

    holder_id を渡すと、その収集者が有効なリースを持つ（「次の患者を受け取る」後）状態にする。
    """
    target = (
        await db.execute(
            select(InputTarget).where(
                InputTarget.dataset_id == ds.id, InputTarget.patient_id == patient_id
            )
        )
    ).scalar_one()
    target.status = InputTargetStatus.AI_DRAFTED
    if holder_id is not None:
        target.started_by_id = holder_id
        target.lease_expires_at = datetime.now(UTC) + timedelta(minutes=60)
    db.add(
        InputExecution(
            questionnaire_id=qid,
            patient_id=patient_id,
            prefill_data={"name": f"name-{patient_id}", "gender": "1"},
            evidence={
                "name": {"trace": [{"op": "sql_extract", "id": "s1", "output": "x"}], "value": "x"},
                "_meta": {"executed_by": "batch"},
            },
            status=ExecutionStatus.SUCCESS,
        )
    )
    await db.commit()
    return target


@pytest.mark.asyncio
async def test_check_mode_renders_prefilled(
    collector_client: AsyncClient, collector_user: User, db_session: AsyncSession
) -> None:
    """チェック画面は AIドラフト値を事前入力して表示する。"""
    q = await _make_questionnaire(db_session)
    await _add_confirmed_plans(db_session, q.id)
    ds = await _make_dataset_with_targets(db_session, q.id, ["p-001"])
    await db_session.commit()
    await _seed_ai_drafted(db_session, q.id, ds, "p-001", holder_id=collector_user.id)

    resp = await collector_client.get(f"/datasets/{ds.id}/check", params={"questionnaire_id": q.id})
    assert resp.status_code == 200
    body = resp.text
    assert "name-p-001" in body  # AI が入れた値
    assert "p-001" in body  # patient id


@pytest.mark.asyncio
async def test_check_confirm_creates_qr_and_completes(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """承認確定で QuestionnaireResponse を生成し InputTarget を completed にする。"""
    q = await _make_questionnaire(db_session)
    await _add_confirmed_plans(db_session, q.id)
    ds = await _make_dataset_with_targets(db_session, q.id, ["p-001"])
    await db_session.commit()
    # 収集者が「次の患者を受け取る」で担当（有効なリース）になっている
    target = await _seed_ai_drafted(db_session, q.id, ds, "p-001", holder_id=collector_user.id)
    await collector_client.get(f"/datasets/{ds.id}/check", params={"questionnaire_id": q.id})

    resp = await collector_client.post(
        f"/datasets/{ds.id}/check/{target.id}/confirm",
        data={"name": "山田太郎", "gender": "1", "memo": "確認済"},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    async with TestSessionLocal() as verify:
        refreshed = (
            await verify.execute(select(InputTarget).where(InputTarget.id == target.id))
        ).scalar_one()
        assert refreshed.status == InputTargetStatus.COMPLETED
        assert refreshed.completed_at is not None

        qrs = (
            (
                await verify.execute(
                    select(QuestionnaireResponse).where(
                        QuestionnaireResponse.questionnaire_id == q.id
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(qrs) == 1
        assert qrs[0].subject == "Patient/p-001"


@pytest.mark.asyncio
async def test_check_flag_sets_needs_attention(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """後回しは needs_attention を立て、状態は ai_drafted のまま保ち、リースを手放す。"""
    q = await _make_questionnaire(db_session)
    await _add_confirmed_plans(db_session, q.id)
    ds = await _make_dataset_with_targets(db_session, q.id, ["p-001"])
    await db_session.commit()
    target = await _seed_ai_drafted(db_session, q.id, ds, "p-001", holder_id=collector_user.id)

    resp = await collector_client.post(
        f"/datasets/{ds.id}/check/{target.id}/flag",
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    async with TestSessionLocal() as verify:
        refreshed = (
            await verify.execute(select(InputTarget).where(InputTarget.id == target.id))
        ).scalar_one()
        assert refreshed.needs_attention is True
        assert refreshed.status == InputTargetStatus.AI_DRAFTED
        assert refreshed.started_by_id is None
        assert refreshed.lease_expires_at is None
