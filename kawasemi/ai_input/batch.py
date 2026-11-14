"""バッチAI自動入力ランナー

(dataset, questionnaire) スコープの BatchJob を受け取り、実行時点で status=pending の
InputTarget を 1 件ずつ逐次処理する。各件について確認済みプランを DSL インタプリタで
実行し、AIドラフトとして InputExecution を保存、InputTarget の状態を遷移させる:

  - success / partial → ai_drafted（人のレビュー待ち）
  - failed            → pending 維持（次回バッチで自動リトライ）

ローカル Ollama への負荷集中を避けるため逐次実行（並列化しない）。1 件ごとにコミットして
進捗カウンタを更新するため、画面はジョブをポーリングして途中経過を表示できる。ジョブは
バックグラウンドの asyncio タスクとして走るため、画面を閉じても継続する。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from kawasemi.ai_input.executor import DSLInterpreter, program_snapshot
from kawasemi.ai_input.models import ExecutionStatus, InputExecution, InputPlan, PlanStatus
from kawasemi.core.database import AsyncSessionLocal
from kawasemi.core.exception_summary import summarize_exception
from kawasemi.core.lease import free_clause, utcnow
from kawasemi.core.logging_config import get_logger
from kawasemi.models.batch_job import BatchJob, BatchJobStatus
from kawasemi.models.input_target import InputTarget, InputTargetStatus
from kawasemi.models.questionnaire import Questionnaire

logger = get_logger(__name__)

# 起動中タスクの参照を保持して GC による中断を防ぐ（完了時に自動破棄）。
_running_tasks: set[asyncio.Task[None]] = set()


def launch_batch_job(job_id: int) -> None:
    """バッチジョブをバックグラウンドの asyncio タスクとして起動する。

    リクエスト/レスポンスのライフサイクルから切り離して走らせるため、画面を閉じても
    ジョブは継続する。画面側は BatchJob をポーリングして進捗を取得する。
    """
    task = asyncio.create_task(run_batch_job(job_id))
    _running_tasks.add(task)
    task.add_done_callback(_running_tasks.discard)


async def _confirmed_plans(db: AsyncSession, questionnaire_id: int) -> list[InputPlan]:
    result = await db.execute(
        select(InputPlan).where(
            InputPlan.questionnaire_id == questionnaire_id,
            InputPlan.status == PlanStatus.CONFIRMED,
        )
    )
    return list(result.scalars().all())


async def _pending_targets(
    db: AsyncSession, dataset_id: int, questionnaire_id: int
) -> list[InputTarget]:
    result = await db.execute(
        select(InputTarget)
        .where(
            InputTarget.dataset_id == dataset_id,
            InputTarget.questionnaire_id == questionnaire_id,
            InputTarget.status == InputTargetStatus.PENDING,
        )
        .order_by(InputTarget.patient_id)
    )
    return list(result.scalars().all())


async def run_batch_job(job_id: int) -> None:
    """1 バッチジョブを実行する。専用セッションを開き、1 件ごとにコミットする。"""
    async with AsyncSessionLocal() as db:
        job = (
            await db.execute(
                select(BatchJob)
                .options(selectinload(BatchJob.created_by))
                .where(BatchJob.id == job_id)
            )
        ).scalar_one_or_none()
        if job is None:
            logger.warning(f"バッチジョブが見つかりません: id={job_id}")
            return

        job.status = BatchJobStatus.RUNNING
        job.started_at = datetime.now(UTC)
        await db.commit()

        executed_by_id = job.created_by_id
        executed_by_name = job.created_by.full_name if job.created_by else None
        model = job.model or None
        study_id = job.study_id or ""

        plans = await _confirmed_plans(db, job.questionnaire_id)
        # 回答型の検査に使う（型に合わない値を prefill しない）
        questionnaire = await db.get(Questionnaire, job.questionnaire_id)
        questionnaire_content = questionnaire.content if questionnaire is not None else None
        targets = await _pending_targets(db, job.dataset_id, job.questionnaire_id)

        interpreter = DSLInterpreter()
        try:
            for target in targets:
                # キャンセル要求を各件の処理前に確認する（DBから最新値を読む）。
                await db.refresh(job, ["cancel_requested"])
                if job.cancel_requested:
                    job.status = BatchJobStatus.CANCELED
                    logger.info(f"バッチジョブをキャンセルしました: id={job_id}")
                    break

                status = ExecutionStatus.SUCCESS
                error_summary: str | None = None
                try:
                    prefill_data, evidence = await interpreter.execute(
                        plans,
                        target.patient_id,
                        db,
                        llm_model_override=model,
                        study_id=study_id,
                        questionnaire_content=questionnaire_content,
                    )
                except Exception as e:  # noqa: BLE001
                    logger.warning(
                        f"バッチ1件の実行失敗: job={job_id} patient={target.patient_id}",
                        exc_info=e,
                    )
                    prefill_data, evidence = {}, {}
                    status = ExecutionStatus.FAILED
                    error_summary = summarize_exception(e)

                # autofill（execute_autofill）と同じ判定にする: 例外のときだけ FAILED とし、
                # 値が 0 件でも（決定的に「該当なし」のことがある）失敗とはしない。空ドラフトを
                # 失敗扱いで pending 維持すると、再バッチで同じ対象を無限に再処理してしまうため。
                if (
                    status != ExecutionStatus.FAILED
                    and prefill_data
                    and len(prefill_data) < len(plans)
                ):
                    status = ExecutionStatus.PARTIAL

                evidence = {
                    **evidence,
                    "_meta": {
                        "executed_by": executed_by_name,
                        "via_batch_job": job_id,
                    },
                }

                # 状態遷移: success/partial → ai_drafted、failed → pending 維持。
                # 状態の書き込みは「まだ pending で誰のリースも無い」ときだけ通す条件付き UPDATE。
                # 実行中に収集者が受け取った（in_progress）・完了した対象を、スナップショットの
                # 古い値で ai_drafted へ巻き戻さない。取り損ねた対象は記録も作らずに飛ばす。
                skipped = False
                if status != ExecutionStatus.FAILED:
                    res = await db.execute(
                        update(InputTarget)
                        .where(
                            InputTarget.id == target.id,
                            InputTarget.status == InputTargetStatus.PENDING,
                            free_clause(utcnow()),
                        )
                        .values(status=InputTargetStatus.AI_DRAFTED)
                        .execution_options(synchronize_session=False)
                    )
                    skipped = not res.rowcount  # type: ignore[attr-defined]
                if skipped:
                    logger.info(
                        f"バッチ: 受け取り済み/完了のため飛ばしました: "
                        f"job={job_id} target={target.id}"
                    )
                else:
                    execution = InputExecution(
                        questionnaire_id=job.questionnaire_id,
                        patient_id=target.patient_id,
                        subject_id=target.subject_id,
                        executed_by_id=executed_by_id,
                        batch_job_id=job_id,
                        prefill_data=prefill_data,
                        evidence=evidence,
                        # バッチも本番実行なので実行時 program を固定保存する（版固定を全経路で成立）。
                        program_snapshot=program_snapshot(plans, model),
                        status=status,
                        error_summary=error_summary,
                    )
                    db.add(execution)
                    if status == ExecutionStatus.FAILED:
                        job.failed += 1
                    elif status == ExecutionStatus.PARTIAL:
                        job.partial += 1
                    else:
                        job.succeeded += 1

                job.processed += 1
                await db.commit()

            else:
                # break しなかった（キャンセルされなかった）場合のみ完了扱い。
                job.status = BatchJobStatus.COMPLETED

        except Exception as e:  # noqa: BLE001
            logger.error(f"バッチジョブが致命的エラーで停止: id={job_id}", exc_info=e)
            job.status = BatchJobStatus.FAILED
            job.error_summary = summarize_exception(e)
        finally:
            await interpreter.aclose()

        job.finished_at = datetime.now(UTC)
        await db.commit()
        logger.info(
            f"バッチジョブ終了: id={job_id} status={job.status} "
            f"processed={job.processed}/{job.total} "
            f"(success={job.succeeded} partial={job.partial} failed={job.failed})"
        )
