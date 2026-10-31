"""`kawasemi.ai_input.batch`（バッチAI自動入力ランナー）のユニットテスト

最小シード + DSLInterpreter/AsyncSessionLocal のモックで、ランナーのエッジパス
（launch / ジョブ不在 / partial / failed / cancel / 致命的エラー）を決定論的に検証する。
"""

from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import kawasemi.ai_input.batch as batch_mod
from kawasemi.ai_input.models import ExecutionStatus, InputExecution, InputPlan, PlanStatus
from kawasemi.models.batch_job import BatchJob, BatchJobStatus
from kawasemi.models.input_target import InputTarget, InputTargetStatus
from tests.conftest import TestSessionLocal

# FK は本テストのインメモリSQLiteで強制されないため、Dataset/Questionnaire 実体は作らず
# ダミーIDで参照する（ランナーのロジック検証に外部実体は不要）。
_QID = 9001
_DSID = 9001


async def _seed(
    db: AsyncSession,
    *,
    patients: list[str],
    plan_link_ids: list[str],
    cancel: bool = False,
    total: int | None = None,
) -> int:
    for lid in plan_link_ids:
        db.add(
            InputPlan(
                questionnaire_id=_QID,
                link_id=lid,
                program={},
                status=PlanStatus.CONFIRMED,
            )
        )
    for pid in patients:
        db.add(
            InputTarget(
                dataset_id=_DSID,
                questionnaire_id=_QID,
                patient_id=pid,
                status=InputTargetStatus.PENDING,
            )
        )
    job = BatchJob(
        dataset_id=_DSID,
        questionnaire_id=_QID,
        status=BatchJobStatus.RUNNING,
        total=len(patients) if total is None else total,
        cancel_requested=cancel,
    )
    db.add(job)
    await db.commit()
    return job.id


def _fake_interpreter(*, prefill=None, raise_exc=None, sink=None):
    """DSLInterpreter 互換のフェイクを返すファクトリ（batch は引数なしで生成する）。"""

    class _Fake:
        def __init__(self) -> None:
            self.closed = False
            if sink is not None:
                sink.append(self)

        async def execute(
            self, plans, patient_id, db, llm_model_override=None, *, study_id="", **_kw
        ):
            if raise_exc is not None:
                raise raise_exc
            if prefill is not None:
                pf = dict(prefill)
            else:
                pf = {p.link_id: f"{p.link_id}:{patient_id}" for p in plans}
            return pf, {k: {"value": v} for k, v in pf.items()}

        async def aclose(self) -> None:
            self.closed = True

    return _Fake


async def _get_job(job_id: int) -> BatchJob:
    async with TestSessionLocal() as v:
        return (await v.execute(select(BatchJob).where(BatchJob.id == job_id))).scalar_one()


async def _get_targets() -> list[InputTarget]:
    async with TestSessionLocal() as v:
        return list(
            (await v.execute(select(InputTarget).where(InputTarget.questionnaire_id == _QID)))
            .scalars()
            .all()
        )


# ---------------------------------------------------------------------------
# launch_batch_job: タスク登録と done_callback による破棄
# ---------------------------------------------------------------------------


async def test_launch_batch_job_registers_then_discards(monkeypatch: pytest.MonkeyPatch) -> None:
    ran: list[int] = []

    async def fake_run(job_id: int) -> None:
        ran.append(job_id)

    monkeypatch.setattr(batch_mod, "run_batch_job", fake_run)
    before = set(batch_mod._running_tasks)
    batch_mod.launch_batch_job(42)
    new = batch_mod._running_tasks - before
    assert len(new) == 1  # GC防止のため実行中タスクを保持する
    task = new.pop()
    await task
    await asyncio.sleep(0)  # done_callback の実行を待つ
    assert ran == [42]
    assert task not in batch_mod._running_tasks  # 完了後に破棄される


# ---------------------------------------------------------------------------
# run_batch_job: 各分岐
# ---------------------------------------------------------------------------


async def test_run_batch_job_missing_job_is_noop(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(batch_mod, "AsyncSessionLocal", TestSessionLocal)
    # 存在しないジョブIDは例外を投げず静かに return する
    await batch_mod.run_batch_job(999_999)


async def test_run_batch_job_success(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = await _seed(db_session, patients=["p1", "p2"], plan_link_ids=["a", "b"])
    monkeypatch.setattr(batch_mod, "AsyncSessionLocal", TestSessionLocal)
    monkeypatch.setattr(batch_mod, "DSLInterpreter", _fake_interpreter())

    await batch_mod.run_batch_job(job_id)

    job = await _get_job(job_id)
    assert job.status == BatchJobStatus.COMPLETED
    assert job.processed == 2
    assert job.succeeded == 2
    assert job.failed == 0
    assert all(t.status == InputTargetStatus.AI_DRAFTED for t in await _get_targets())


async def test_run_batch_job_partial(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = await _seed(db_session, patients=["p1"], plan_link_ids=["a", "b"])
    monkeypatch.setattr(batch_mod, "AsyncSessionLocal", TestSessionLocal)
    # 2プラン中1項目しか値が出ない（非空かつ < プラン数）→ PARTIAL
    monkeypatch.setattr(batch_mod, "DSLInterpreter", _fake_interpreter(prefill={"a": "x"}))

    await batch_mod.run_batch_job(job_id)

    job = await _get_job(job_id)
    assert job.status == BatchJobStatus.COMPLETED
    assert job.partial == 1
    assert job.succeeded == 0
    assert job.failed == 0  # カウンタ取り違えの検出を厚くする
    targets = await _get_targets()
    assert targets[0].status == InputTargetStatus.AI_DRAFTED


async def test_run_batch_job_failed_keeps_target_pending(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = await _seed(db_session, patients=["p1"], plan_link_ids=["a"])
    monkeypatch.setattr(batch_mod, "AsyncSessionLocal", TestSessionLocal)
    monkeypatch.setattr(
        batch_mod, "DSLInterpreter", _fake_interpreter(raise_exc=RuntimeError("exec boom"))
    )

    await batch_mod.run_batch_job(job_id)

    job = await _get_job(job_id)
    assert job.status == BatchJobStatus.COMPLETED  # ジョブ自体は完走する
    assert job.failed == 1
    assert job.processed == 1
    targets = await _get_targets()
    assert targets[0].status == InputTargetStatus.PENDING  # 失敗時は pending 維持（次回リトライ）

    async with TestSessionLocal() as v:
        ex = (
            await v.execute(select(InputExecution).where(InputExecution.batch_job_id == job_id))
        ).scalar_one()
        assert ex.status == ExecutionStatus.FAILED
        assert ex.error_summary  # 例外メッセージを記録


async def test_run_batch_job_persists_program_snapshot(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """バッチ本番実行の InputExecution にも実行時 program（reasoning込み）が固定保存される。

    確認書のバージョン固定は単発 autofill だけでなくバッチ経路でも成立する必要がある。
    """
    program = {
        "steps": [{"op": "sql_extract", "id": "s", "reasoning": "バッチ根拠", "sql": "SELECT 1"}],
        "return": "$s",
    }
    db_session.add(
        InputPlan(
            questionnaire_id=_QID, link_id="item1", program=program, status=PlanStatus.CONFIRMED
        )
    )
    db_session.add(
        InputTarget(
            dataset_id=_DSID,
            questionnaire_id=_QID,
            patient_id="p1",
            status=InputTargetStatus.PENDING,
        )
    )
    job = BatchJob(dataset_id=_DSID, questionnaire_id=_QID, status=BatchJobStatus.RUNNING, total=1)
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    monkeypatch.setattr(batch_mod, "AsyncSessionLocal", TestSessionLocal)
    monkeypatch.setattr(batch_mod, "DSLInterpreter", _fake_interpreter(prefill={"item1": "1"}))

    await batch_mod.run_batch_job(job_id)

    async with TestSessionLocal() as v:
        ex = (
            await v.execute(select(InputExecution).where(InputExecution.batch_job_id == job_id))
        ).scalar_one()
        assert ex.program_snapshot is not None
        snap = ex.program_snapshot["item1"]
        assert snap["program"]["steps"][0]["reasoning"] == "バッチ根拠"


async def test_run_batch_job_cancellation(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = await _seed(
        db_session, patients=["p1", "p2"], plan_link_ids=["a"], cancel=True, total=2
    )
    monkeypatch.setattr(batch_mod, "AsyncSessionLocal", TestSessionLocal)
    monkeypatch.setattr(batch_mod, "DSLInterpreter", _fake_interpreter())

    await batch_mod.run_batch_job(job_id)

    job = await _get_job(job_id)
    assert job.status == BatchJobStatus.CANCELED
    assert job.processed == 0  # 1件も処理せず中断
    assert all(t.status == InputTargetStatus.PENDING for t in await _get_targets())


async def test_run_batch_job_fatal_error_marks_failed(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    job_id = await _seed(db_session, patients=["p1"], plan_link_ids=["a"])
    monkeypatch.setattr(batch_mod, "AsyncSessionLocal", TestSessionLocal)
    sink: list = []
    monkeypatch.setattr(batch_mod, "DSLInterpreter", _fake_interpreter(sink=sink))

    # ループ内（インナーtryの外）で予期せぬ例外を起こし、致命的エラーハンドラを通す
    class _Boom:
        def __init__(self, **kwargs):
            raise RuntimeError("fatal boom")

    monkeypatch.setattr(batch_mod, "InputExecution", _Boom)

    await batch_mod.run_batch_job(job_id)

    job = await _get_job(job_id)
    assert job.status == BatchJobStatus.FAILED
    assert job.processed == 0  # 1件目の InputExecution 生成で破棄され processed まで到達しない
    assert job.error_summary and "fatal boom" in job.error_summary
    assert sink and sink[0].closed is True  # finally で interpreter.aclose() される


async def test_run_batch_job_rejects_values_that_do_not_fit_the_answer_type(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """バッチ経路も回答型を executor へ渡す。date 項目の「不明」は prefill しない。"""
    from kawasemi.ai_input.models import FailureCode
    from kawasemi.models.questionnaire import Questionnaire

    db_session.add(
        Questionnaire(
            id=_QID,
            fhir_id="q-batch-463",
            title="回答型",
            status="active",
            content={"resourceType": "Questionnaire", "item": [{"linkId": "d", "type": "date"}]},
        )
    )
    program = {"steps": [{"op": "sql_extract", "id": "s", "sql": "SELECT '不明'"}], "return": "$s"}
    db_session.add(
        InputPlan(questionnaire_id=_QID, link_id="d", program=program, status=PlanStatus.CONFIRMED)
    )
    db_session.add(
        InputTarget(
            dataset_id=_DSID,
            questionnaire_id=_QID,
            patient_id="p1",
            status=InputTargetStatus.PENDING,
        )
    )
    job = BatchJob(dataset_id=_DSID, questionnaire_id=_QID, status=BatchJobStatus.RUNNING, total=1)
    db_session.add(job)
    await db_session.commit()
    job_id = job.id

    monkeypatch.setattr(batch_mod, "AsyncSessionLocal", TestSessionLocal)
    await batch_mod.run_batch_job(job_id)

    async with TestSessionLocal() as v:
        ex = (
            await v.execute(select(InputExecution).where(InputExecution.batch_job_id == job_id))
        ).scalar_one()
    assert ex.prefill_data == {}
    assert ex.evidence["d"]["failure_code"] == FailureCode.ANSWER_TYPE_MISMATCH
