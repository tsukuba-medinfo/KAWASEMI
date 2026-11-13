"""データセット・入力対象ルーター（バッチAI自動入力・チェックモードを含む）"""

from datetime import UTC, datetime
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi_csrf_protect import CsrfProtect
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from starlette.datastructures import FormData

from kawasemi.ai_input.batch import launch_batch_job
from kawasemi.ai_input.models import InputExecution, InputPlan, OpType, PlanStatus
from kawasemi.ai_input.schemas import validate_study_id
from kawasemi.api.v100.questionnaire_responses import build_questionnaire_response_resource
from kawasemi.core.config import settings
from kawasemi.core.database import get_db
from kawasemi.core.deps import RequireAdmin, RequireCollector, require_roles
from kawasemi.core.lease import (
    aware_utc,
    claim_next_target,
    extend_lease,
    get_active_lease,
    lease_is_valid,
    release_lease,
    utcnow,
)
from kawasemi.core.logging_config import get_logger
from kawasemi.models.batch_job import BatchJob, BatchJobStatus
from kawasemi.models.dataset import Dataset, DatasetQuestionnaire
from kawasemi.models.input_target import InputTarget, InputTargetStatus
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.questionnaire_response import QuestionnaireResponse
from kawasemi.models.user import User
from kawasemi.utils.fhir_utils import (
    extract_questionnaire_response_metadata,
    generate_fhir_id,
    validate_fhir_questionnaire_response,
)
from kawasemi.web.filter_utils import parse_int_filter
from kawasemi.web.form_generator import FormGenerator
from kawasemi.web.template_globals import register_template_globals

router = APIRouter()
logger = get_logger(__name__)

templates = Jinja2Templates(directory="kawasemi/web/templates")
register_template_globals(templates)

# データセット = 研究計画で固定した対象患者リスト。登録・削除は管理者だけが行う
# （誰を収集するかは計画で決まり、収集者が患者を選ぶと逸脱になるため）。管理者が見るのは
# 患者IDの一覧だけで、患者データ（FHIR）は見ない。
# 収集者は「次の患者を受け取る」で先着の自動割り当てを受け、受け取った対象のチェック・確定まで
# 同じ人が行う（欠損を埋める人が検証者を兼ねるので、閲覧・検証者はこの流れに入らない）。
# 一覧/詳細は管理者・収集者・閲覧・検証者が閲覧できる。
RequireDatasetViewer = Annotated[User, Depends(require_roles("admin", "collector", "reviewer"))]

_LLM_OPS = {OpType.LLM_EXTRACT, OpType.LLM_JUDGE}


# =============================================================================
# データセット管理（登録・削除は admin）
# =============================================================================


@router.get("/datasets", response_class=HTMLResponse)
async def list_datasets(
    request: Request,
    current_user: RequireDatasetViewer,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    """データセット一覧画面"""
    result = await db.execute(
        select(Dataset)
        .options(
            selectinload(Dataset.dataset_questionnaires).selectinload(
                DatasetQuestionnaire.questionnaire
            ),
            selectinload(Dataset.input_targets),
            selectinload(Dataset.created_by),
        )
        .order_by(Dataset.created_at.desc())
    )
    datasets = list(result.scalars().all())

    return templates.TemplateResponse(
        "dataset_list.html",
        {
            "request": request,
            "current_user": current_user,
            "datasets": datasets,
        },
    )


@router.get("/datasets/new", response_class=HTMLResponse)
async def new_dataset(
    request: Request,
    current_user: RequireAdmin,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    """データセット作成画面"""
    q_result = await db.execute(
        select(Questionnaire)
        .where(~Questionnaire.is_deleted, Questionnaire.status == "active")
        .order_by(Questionnaire.title)
    )
    questionnaires = list(q_result.scalars().all())

    return templates.TemplateResponse(
        "dataset_new.html",
        {
            "request": request,
            "current_user": current_user,
            "questionnaires": questionnaires,
            "error": None,
        },
    )


def _form_str(form_data: FormData, key: str) -> str:
    """フォームのテキスト項目を文字列として取り出す（未指定/ファイルは空文字）。

    FormData.get() は `str | UploadFile | None` を返すため、テキストフィールド前提の
    値を安全に str へ寄せる。
    """
    value = form_data.get(key)
    return value if isinstance(value, str) else ""


def _parse_dataset_form(
    form_data: FormData,
) -> tuple[str, str, str, list[int], list[str]]:
    """データセット作成フォームをパースする（作成・確認で共用）。

    returns: (name, description, patient_ids_text, questionnaire_ids, patient_ids)
    """
    name = _form_str(form_data, "name").strip()
    description = _form_str(form_data, "description").strip()
    patient_ids_text = _form_str(form_data, "patient_ids_text")
    questionnaire_ids = [
        int(v) for v in form_data.getlist("questionnaire_ids") if isinstance(v, str) and v
    ]
    patient_ids = [pid.strip() for pid in patient_ids_text.splitlines() if pid.strip()]
    return name, description, patient_ids_text, questionnaire_ids, patient_ids


def _validate_dataset_form(
    name: str, questionnaire_ids: list[int], patient_ids: list[str]
) -> str | None:
    """サーバ側バリデーション。エラー文言を返す（無ければ None）。"""
    if not name:
        return "データセット名を入力してください。"
    if not questionnaire_ids:
        return "Questionnaireを1つ以上選択してください。"
    if not patient_ids:
        return "Patient IDを1件以上入力してください。"
    return None


async def _render_dataset_new(
    request: Request, current_user: User, db: AsyncSession, error: str | None
) -> HTMLResponse:
    q_result = await db.execute(
        select(Questionnaire)
        .where(~Questionnaire.is_deleted, Questionnaire.status == "active")
        .order_by(Questionnaire.title)
    )
    questionnaires = list(q_result.scalars().all())
    return templates.TemplateResponse(
        "dataset_new.html",
        {
            "request": request,
            "current_user": current_user,
            "questionnaires": questionnaires,
            "error": error,
        },
    )


@router.post("/datasets/new/confirm", response_class=HTMLResponse)
async def confirm_dataset(
    request: Request,
    current_user: RequireAdmin,
    csrf_protect: Annotated[CsrfProtect, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    """データセット作成の確認画面。デカルト積の件数とサンプルをプレビューする（コミットしない）。"""
    await csrf_protect.validate_csrf(request)
    form_data = await request.form()
    name, description, patient_ids_text, questionnaire_ids, patient_ids = _parse_dataset_form(
        form_data
    )
    error = _validate_dataset_form(name, questionnaire_ids, patient_ids)
    if error:
        return await _render_dataset_new(request, current_user, db, error)

    q_sel = await db.execute(select(Questionnaire).where(Questionnaire.id.in_(questionnaire_ids)))
    sel_map = {q.id: q for q in q_sel.scalars().all()}
    selected_questionnaires = [sel_map[qid] for qid in questionnaire_ids if qid in sel_map]
    return templates.TemplateResponse(
        "dataset_confirm.html",
        {
            "request": request,
            "current_user": current_user,
            "name": name,
            "description": description,
            "patient_ids_text": patient_ids_text,
            "questionnaire_ids": questionnaire_ids,
            "selected_questionnaires": selected_questionnaires,
            "patient_ids": patient_ids,
            "patient_sample": patient_ids[:10],
            "total_targets": len(questionnaire_ids) * len(patient_ids),
        },
    )


@router.post("/datasets/new")
async def create_dataset(
    request: Request,
    current_user: RequireAdmin,
    csrf_protect: Annotated[CsrfProtect, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """データセット作成処理 + InputTarget一括生成。

    導線は フォーム(dataset_new.html) → 確認(/datasets/new/confirm) → この作成 POST。
    確認は別ルートに分離してあるため、この POST の契約（直接作成して 303）は不変で、
    既存の自動化やテストが直接この POST を叩く経路も維持される。
    """
    await csrf_protect.validate_csrf(request)
    form_data = await request.form()
    name, description, patient_ids_text, questionnaire_ids, patient_ids = _parse_dataset_form(
        form_data
    )
    error = _validate_dataset_form(name, questionnaire_ids, patient_ids)
    if error:
        return await _render_dataset_new(request, current_user, db, error)

    dataset = Dataset(
        name=name,
        description=description or None,
        created_by_id=current_user.id,
    )
    db.add(dataset)
    await db.flush()  # IDを確定

    # Questionnaireリンク
    for order, qid in enumerate(questionnaire_ids):
        db.add(
            DatasetQuestionnaire(
                dataset_id=dataset.id,
                questionnaire_id=qid,
                display_order=order,
            )
        )

    # InputTargetをデカルト積で一括生成
    for qid in questionnaire_ids:
        for patient_id in patient_ids:
            db.add(
                InputTarget(
                    dataset_id=dataset.id,
                    questionnaire_id=qid,
                    patient_id=patient_id,
                    status=InputTargetStatus.PENDING,
                )
            )

    await db.commit()
    logger.info(
        f"データセットを作成しました: id={dataset.id}, "
        f"questionnaires={len(questionnaire_ids)}, patients={len(patient_ids)}, "
        f"input_targets={len(questionnaire_ids) * len(patient_ids)}"
    )
    return RedirectResponse(url=f"/datasets/{dataset.id}", status_code=303)


@router.get("/datasets/{dataset_id}", response_class=HTMLResponse)
async def show_dataset(
    request: Request,
    dataset_id: int,
    current_user: RequireDatasetViewer,
    db: Annotated[AsyncSession, Depends(get_db)],
    status: str | None = Query(None),
    questionnaire_id: str | None = Query(None),
) -> Response:
    """データセット詳細・入力対象一覧画面（バッチ起動・チェック導線を含む）

    閲覧は admin / collector / reviewer に許可する。データセットの作成・削除は admin、
    バッチ起動と入力対象の受け取りは collector。
    """
    result = await db.execute(
        select(Dataset)
        .where(Dataset.id == dataset_id)
        .options(
            selectinload(Dataset.dataset_questionnaires).selectinload(
                DatasetQuestionnaire.questionnaire
            ),
            selectinload(Dataset.created_by),
        )
    )
    dataset = result.scalar_one_or_none()
    if not dataset:
        return RedirectResponse(url="/datasets", status_code=303)

    # 入力対象一覧（フィルタ付き）
    query = (
        select(InputTarget)
        .where(InputTarget.dataset_id == dataset_id)
        .options(
            selectinload(InputTarget.questionnaire),
            selectinload(InputTarget.started_by),
        )
        .order_by(InputTarget.questionnaire_id, InputTarget.patient_id)
    )
    filter_warnings: list[str] = []
    questionnaire_id_int = parse_int_filter(questionnaire_id, "Questionnaire", filter_warnings)
    if status:
        query = query.where(InputTarget.status == status)
    if questionnaire_id_int is not None:
        query = query.where(InputTarget.questionnaire_id == questionnaire_id_int)

    targets_result = await db.execute(query)
    input_targets = list(targets_result.scalars().all())

    # Questionnaire ごとのバッチ用サマリー（状態別件数・プラン準備状況・実行中ジョブ）
    questionnaires = [dq.questionnaire for dq in dataset.dataset_questionnaires]
    q_summaries = []
    for q in questionnaires:
        counts = await _target_status_counts(db, dataset_id, q.id)
        readiness = await _plan_readiness(db, q.id)
        active_job = await _active_batch_job(db, dataset_id, q.id)
        q_summaries.append(
            {
                "questionnaire": q,
                "counts": counts,
                "readiness": readiness,
                "active_job": active_job,
            }
        )

    return templates.TemplateResponse(
        "dataset_detail.html",
        {
            "request": request,
            "current_user": current_user,
            "dataset": dataset,
            "input_targets": input_targets,
            "q_summaries": q_summaries,
            "statuses": InputTargetStatus.ALL,
            "status_labels": InputTargetStatus.LABELS,
            "filters": {
                "status": status,
                "questionnaire_id": questionnaire_id_int,
            },
            "filter_warnings": filter_warnings,
        },
    )


@router.post("/datasets/{dataset_id}/delete")
async def delete_dataset(
    request: Request,
    dataset_id: int,
    current_user: RequireAdmin,
    csrf_protect: Annotated[CsrfProtect, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RedirectResponse:
    """データセット削除処理（関連InputTargetもCASCADE削除）"""
    await csrf_protect.validate_csrf(request)
    result = await db.execute(select(Dataset).where(Dataset.id == dataset_id))
    dataset = result.scalar_one_or_none()
    if dataset:
        await db.delete(dataset)
        await db.commit()
        logger.info(f"データセットを削除しました: id={dataset_id}")
    return RedirectResponse(url="/datasets", status_code=303)


# =============================================================================
# 入力対象一覧（入力担当者向け）
# =============================================================================


@router.get("/input-targets", response_class=HTMLResponse)
async def list_input_targets(
    request: Request,
    # **手動収集に認可されたロールに限る**。以前は `RequireAuthenticated` で、
    # 閲覧・検証者や設計者でも pending な入力対象の一覧を見られた。一覧は
    # `patient_id` を含み、そのまま `/input-targets/{id}/start` の入口になる。
    current_user: RequireCollector,
    db: Annotated[AsyncSession, Depends(get_db)],
    status: str | None = Query(None),
    dataset_id: str | None = Query(None),
    questionnaire_id: str | None = Query(None),
) -> HTMLResponse:
    """入力対象一覧画面"""
    query = (
        select(InputTarget)
        .options(
            selectinload(InputTarget.questionnaire),
            selectinload(InputTarget.dataset),
            selectinload(InputTarget.started_by),
        )
        .order_by(InputTarget.dataset_id, InputTarget.questionnaire_id, InputTarget.patient_id)
    )
    filter_warnings: list[str] = []
    dataset_id_int = parse_int_filter(dataset_id, "データセット", filter_warnings)
    questionnaire_id_int = parse_int_filter(questionnaire_id, "Questionnaire", filter_warnings)
    if status:
        query = query.where(InputTarget.status == status)
    if dataset_id_int is not None:
        query = query.where(InputTarget.dataset_id == dataset_id_int)
    if questionnaire_id_int is not None:
        query = query.where(InputTarget.questionnaire_id == questionnaire_id_int)

    result = await db.execute(query)
    input_targets = list(result.scalars().all())

    # フィルタ用データ
    ds_result = await db.execute(select(Dataset).order_by(Dataset.name))
    datasets = list(ds_result.scalars().all())

    q_result = await db.execute(
        select(Questionnaire).where(~Questionnaire.is_deleted).order_by(Questionnaire.title)
    )
    questionnaires = list(q_result.scalars().all())

    # サマリー（全件）
    all_result = await db.execute(select(InputTarget))
    all_targets = list(all_result.scalars().all())

    # 自分の現在のリースと、受け取れる件数（プール）
    my_lease = await get_active_lease(db, current_user.id)
    now = utcnow()
    pool_free = [
        t
        for t in all_targets
        if t.status != InputTargetStatus.COMPLETED
        and (
            t.started_by_id is None
            or t.lease_expires_at is None
            or aware_utc(t.lease_expires_at) <= now
        )
    ]
    pool_counts = {
        "ai_drafted": sum(1 for t in pool_free if t.status == InputTargetStatus.AI_DRAFTED),
        "pending": sum(1 for t in pool_free if t.status == InputTargetStatus.PENDING),
        "in_progress": sum(1 for t in pool_free if t.status == InputTargetStatus.IN_PROGRESS),
    }

    return templates.TemplateResponse(
        "input_target_list.html",
        {
            "request": request,
            "current_user": current_user,
            "input_targets": input_targets,
            "all_targets": all_targets,
            "my_lease": my_lease,
            "my_lease_url": _open_url(my_lease) if my_lease else None,
            "pool_counts": pool_counts,
            "datasets": datasets,
            "questionnaires": questionnaires,
            "statuses": InputTargetStatus.ALL,
            "status_labels": InputTargetStatus.LABELS,
            "filters": {
                "status": status,
                "dataset_id": dataset_id_int,
                "questionnaire_id": questionnaire_id_int,
            },
            "filter_warnings": filter_warnings,
        },
    )


def _open_url(target: InputTarget) -> str:
    """受け取った対象を開く URL。AIドラフト済みはチェック画面、それ以外は入力フォーム。"""
    if target.status == InputTargetStatus.AI_DRAFTED:
        return (
            f"/datasets/{target.dataset_id}/check"
            f"?questionnaire_id={target.questionnaire_id}&target_id={target.id}"
        )
    return (
        f"/forms/{target.questionnaire_id}"
        f"?patient_id={quote(target.patient_id, safe='')}&input_target_id={target.id}"
    )


def _targets_redirect(notice: str) -> RedirectResponse:
    return RedirectResponse(url=f"/input-targets?notice={quote(notice)}", status_code=303)


@router.post("/input-targets/claim")
async def claim_input_target(
    request: Request,
    current_user: RequireCollector,
    csrf_protect: Annotated[CsrfProtect, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
    dataset_id: Annotated[str, Form()] = "",
    questionnaire_id: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """「次の患者を受け取る」: 先着で対象を1件自動割り当てし、排他リースを付けて開く。

    収集者は患者を選べない（対象リストは管理者が登録する）。既に有効なリースがあれば、それを
    そのまま開く（一度に1件）。取り合いの排他は kawasemi.core.lease の条件付き UPDATE で取る。
    """
    await csrf_protect.validate_csrf(request)
    warnings: list[str] = []
    ds_id = parse_int_filter(dataset_id, "データセット", warnings)
    q_id = parse_int_filter(questionnaire_id, "Questionnaire", warnings)
    if warnings:
        # 絞り込みの指定が壊れているときに、黙って全体から受け取らない
        return _targets_redirect(warnings[0])
    target = await claim_next_target(db, current_user.id, dataset_id=ds_id, questionnaire_id=q_id)
    if target is None:
        return _targets_redirect("受け取れる患者がありません")
    logger.info(f"入力対象を受け取りました: target_id={target.id}, user={current_user.username}")
    return RedirectResponse(url=_open_url(target), status_code=303)


# =============================================================================
# バッチAI自動入力 / チェックモード（collector。チェックは reviewer も）
# =============================================================================


def _has_llm_op(program: dict[str, Any]) -> bool:
    """program に LLM op（llm_extract / llm_judge）が含まれるか。"""
    return any(s.get("op") in _LLM_OPS for s in (program or {}).get("steps", []))


async def _target_status_counts(
    db: AsyncSession, dataset_id: int, questionnaire_id: int
) -> dict[str, int]:
    """(dataset, questionnaire) の InputTarget をステータス別に集計する。"""
    rows = await db.execute(
        select(InputTarget.status, func.count())
        .where(
            InputTarget.dataset_id == dataset_id,
            InputTarget.questionnaire_id == questionnaire_id,
        )
        .group_by(InputTarget.status)
    )
    counts = {s: 0 for s in InputTargetStatus.ALL}
    for st, c in rows.all():
        counts[st] = c
    return counts


async def _plan_readiness(db: AsyncSession, questionnaire_id: int) -> dict[str, Any]:
    """バッチ実行可否（設計完了）の判定。

    手動入力項目はそもそもプランを持たないため「全 link_id にプラン」ではなく
    「確認済みプランが 1 つ以上あり、未確認(draft)プランが 1 つも残っていない」を
    設計完了の条件とする。
    """
    rows = await db.execute(
        select(InputPlan.status, func.count())
        .where(InputPlan.questionnaire_id == questionnaire_id)
        .group_by(InputPlan.status)
    )
    confirmed = draft = 0
    for st, c in rows.all():
        if st == PlanStatus.CONFIRMED:
            confirmed = c
        elif st == PlanStatus.DRAFT:
            draft = c
    return {"confirmed": confirmed, "draft": draft, "ready": confirmed >= 1 and draft == 0}


async def _active_batch_job(
    db: AsyncSession, dataset_id: int, questionnaire_id: int
) -> BatchJob | None:
    """(dataset, questionnaire) で実行中のバッチジョブ（あれば最新の1件）。"""
    result = await db.execute(
        select(BatchJob)
        .where(
            BatchJob.dataset_id == dataset_id,
            BatchJob.questionnaire_id == questionnaire_id,
            BatchJob.status == BatchJobStatus.RUNNING,
        )
        .order_by(BatchJob.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def _latest_execution(
    db: AsyncSession, questionnaire_id: int, patient_id: str
) -> InputExecution | None:
    """(questionnaire, patient) の最新 InputExecution（AIドラフト）。"""
    result = await db.execute(
        select(InputExecution)
        .where(
            InputExecution.questionnaire_id == questionnaire_id,
            InputExecution.patient_id == patient_id,
        )
        .order_by(InputExecution.created_at.desc(), InputExecution.id.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


def _detail_redirect(dataset_id: int, questionnaire_id: int, notice: str | None = None) -> str:
    url = f"/datasets/{dataset_id}?questionnaire_id={questionnaire_id}"
    if notice:
        url += f"&notice={quote(notice)}"
    return url


@router.post("/datasets/{dataset_id}/batch-autofill")
async def start_batch_autofill(
    request: Request,
    dataset_id: int,
    # バッチ（非対話）の起動は収集の行為なので収集者（以前は admin に集約していた）。
    # 確定プランを患者集合へ一括適用し LLM サーバへ継続負荷がかかるため、対話本番と同じく
    # 収集者の責任で起動する。
    current_user: RequireCollector,
    csrf_protect: Annotated[CsrfProtect, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
    questionnaire_id: Annotated[int, Form()],
    model: Annotated[str | None, Form()] = None,
    study_id: Annotated[str, Form()] = "",
) -> RedirectResponse:
    """pending な InputTarget 群へ一括AI自動入力を起動する（バックグラウンド実行）。"""
    await csrf_protect.validate_csrf(request)
    try:
        study_id = validate_study_id(study_id)
    except ValueError as e:
        return RedirectResponse(
            url=_detail_redirect(dataset_id, questionnaire_id, str(e)),
            status_code=303,
        )
    # Questionnaire がこのデータセットに属するか
    dq = (
        await db.execute(
            select(DatasetQuestionnaire).where(
                DatasetQuestionnaire.dataset_id == dataset_id,
                DatasetQuestionnaire.questionnaire_id == questionnaire_id,
            )
        )
    ).scalar_one_or_none()
    if dq is None:
        return RedirectResponse(url="/datasets", status_code=303)

    # 二重起動防止
    if await _active_batch_job(db, dataset_id, questionnaire_id) is not None:
        return RedirectResponse(
            url=_detail_redirect(dataset_id, questionnaire_id, "既に実行中のバッチがあります"),
            status_code=303,
        )

    # 設計完了（確認済みプランが揃っている）か
    readiness = await _plan_readiness(db, questionnaire_id)
    if not readiness["ready"]:
        return RedirectResponse(
            url=_detail_redirect(
                dataset_id,
                questionnaire_id,
                "確認済みプランが揃っていません（未確認プランが残っている、または確認済みが0件）",
            ),
            status_code=303,
        )

    plans = (
        (
            await db.execute(
                select(InputPlan).where(
                    InputPlan.questionnaire_id == questionnaire_id,
                    InputPlan.status == PlanStatus.CONFIRMED,
                )
            )
        )
        .scalars()
        .all()
    )

    # 二段ガードの2段目（autofill と同じ権威ガード）
    if not settings.enable_ai_extraction:
        llm_links = [p.link_id for p in plans if _has_llm_op(p.program or {})]
        if llm_links:
            return RedirectResponse(
                url=_detail_redirect(
                    dataset_id,
                    questionnaire_id,
                    "LLMを含む確認済みプランがあるため実行できません（ENABLE_AI_EXTRACTION=false）",
                ),
                status_code=303,
            )

    pending = (
        await db.execute(
            select(func.count())
            .select_from(InputTarget)
            .where(
                InputTarget.dataset_id == dataset_id,
                InputTarget.questionnaire_id == questionnaire_id,
                InputTarget.status == InputTargetStatus.PENDING,
            )
        )
    ).scalar_one()
    if not pending:
        return RedirectResponse(
            url=_detail_redirect(dataset_id, questionnaire_id, "未着手の対象がありません"),
            status_code=303,
        )

    job = BatchJob(
        dataset_id=dataset_id,
        questionnaire_id=questionnaire_id,
        created_by_id=current_user.id,
        model=(model or "").strip() or None,
        study_id=study_id,
        status=BatchJobStatus.RUNNING,
        total=int(pending),
    )
    db.add(job)
    await db.commit()
    await db.refresh(job)

    launch_batch_job(job.id)
    logger.info(
        f"バッチAI自動入力を起動: job={job.id} dataset={dataset_id} "
        f"questionnaire={questionnaire_id} total={pending} by={current_user.username}"
    )
    return RedirectResponse(url=_detail_redirect(dataset_id, questionnaire_id), status_code=303)


@router.get("/batch-jobs/{job_id}/status")
async def batch_job_status(
    job_id: int,
    current_user: RequireCollector,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> JSONResponse:
    """バッチジョブの進捗（ポーリング用）。"""
    job = (await db.execute(select(BatchJob).where(BatchJob.id == job_id))).scalar_one_or_none()
    if job is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(
        {
            "id": job.id,
            "status": job.status,
            "status_label": job.status_label,
            "total": job.total,
            "processed": job.processed,
            "succeeded": job.succeeded,
            "partial": job.partial,
            "failed": job.failed,
            "cancel_requested": job.cancel_requested,
            "finished": job.status != BatchJobStatus.RUNNING,
        }
    )


@router.post("/batch-jobs/{job_id}/cancel")
async def cancel_batch_job(
    request: Request,
    job_id: int,
    current_user: RequireCollector,
    csrf_protect: Annotated[CsrfProtect, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RedirectResponse:
    """実行中バッチへキャンセルを要求する（ランナーが各件処理前に確認して停止）。"""
    await csrf_protect.validate_csrf(request)
    job = (await db.execute(select(BatchJob).where(BatchJob.id == job_id))).scalar_one_or_none()
    if job is None:
        return RedirectResponse(url="/datasets", status_code=303)
    if job.status == BatchJobStatus.RUNNING:
        job.cancel_requested = True
        await db.commit()
        logger.info(f"バッチジョブにキャンセル要求: id={job_id} by={current_user.username}")
    return RedirectResponse(
        url=_detail_redirect(job.dataset_id, job.questionnaire_id, "キャンセルを要求しました"),
        status_code=303,
    )


async def _held_ai_drafted(
    db: AsyncSession, user: User, dataset_id: int, target_id: int | None = None
) -> InputTarget | None:
    """呼び出し者が有効なリースを持つ、このデータセットの AIドラフト対象（無ければ None）。"""
    target = await get_active_lease(db, user.id)
    if (
        target is None
        or target.dataset_id != dataset_id
        or target.status != InputTargetStatus.AI_DRAFTED
        or (target_id is not None and target.id != target_id)
    ):
        return None
    return target


_NO_LEASE_NOTICE = "この対象を担当していません。「次の患者を受け取る」から受け取ってください"


@router.get("/datasets/{dataset_id}/check", response_class=HTMLResponse)
async def check_mode(
    request: Request,
    dataset_id: int,
    # 欠損を埋める人が検証者を兼ねるので、チェックは収集者だけが行う。
    current_user: RequireCollector,
    db: Annotated[AsyncSession, Depends(get_db)],
    questionnaire_id: int = Query(...),
    target_id: int | None = Query(None),
) -> Response:
    """チェックモード: 自分が受け取った AIドラフト済みの対象を1件レビューする画面。

    **この GET は担当を作らない**（以前は開いた人を started_by に書いており、そこが患者データ
    の認可の根拠になっていた）。表示できるのは自分が有効なリースを持つ対象だけで、`target_id`
    はリースと食い違えば無視する。開くとリースを延長する。
    """
    questionnaire = (
        await db.execute(select(Questionnaire).where(Questionnaire.id == questionnaire_id))
    ).scalar_one_or_none()
    if questionnaire is None:
        return RedirectResponse(url=f"/datasets/{dataset_id}", status_code=303)

    current = await _held_ai_drafted(db, current_user, dataset_id)
    if current is None or current.questionnaire_id != questionnaire_id:
        return _targets_redirect(_NO_LEASE_NOTICE)
    await extend_lease(db, current, current_user.id)

    execution = await _latest_execution(db, questionnaire_id, current.patient_id)
    prefill = (execution.prefill_data or {}) if execution else {}
    evidence = (execution.evidence or {}) if execution else {}

    generator = FormGenerator(questionnaire.content)
    form_fields = generator.generate_form_fields()

    return templates.TemplateResponse(
        "ai_input/check.html",
        {
            "request": request,
            "current_user": current_user,
            "dataset_id": dataset_id,
            "questionnaire": questionnaire,
            "target": current,
            "form_fields": form_fields,
            "prefill": prefill,
            "evidence": evidence,
            "execution": execution,
            "op_labels": OpType.LABELS,
        },
    )


@router.post("/datasets/{dataset_id}/check/{target_id}/confirm", response_model=None)
async def confirm_check(
    dataset_id: int,
    target_id: int,
    request: Request,
    current_user: RequireCollector,
    csrf_protect: Annotated[CsrfProtect, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """チェック1件を承認・確定する: QuestionnaireResponse を生成し completed にする。"""
    await csrf_protect.validate_csrf(request)
    target = (
        await db.execute(select(InputTarget).where(InputTarget.id == target_id))
    ).scalar_one_or_none()
    if target is None or target.dataset_id != dataset_id:
        return RedirectResponse(url=f"/datasets/{dataset_id}", status_code=303)

    # 自分が有効なリースを持つ AIドラフト対象だけ確定できる（他者の担当・期限切れ・状態違いは弾く）。
    # チェックは収集者だけが行い、ここへ来るのは収集者なので、以前あった閲覧・検証者向けの
    # 職務分離（SoD）の分岐は到達不能になり除いた。
    if target.status != InputTargetStatus.AI_DRAFTED or not lease_is_valid(target, current_user.id):
        return _targets_redirect(_NO_LEASE_NOTICE)

    questionnaire = (
        await db.execute(select(Questionnaire).where(Questionnaire.id == target.questionnaire_id))
    ).scalar_one_or_none()
    if questionnaire is None:
        return RedirectResponse(url=f"/datasets/{dataset_id}", status_code=303)

    # フォーム値 → 回答（subject/author 等の予約キーは除外し、サーバ側で整合的に決める）
    form_data = await request.form()
    reserved = {"subject", "author", "csrf_token", "input_target_id", "assignment_id"}
    answers: dict[str, Any] = {}
    for key, value in form_data.items():
        if key in reserved:
            continue
        answers[key] = True if value == "on" else value

    resource = build_questionnaire_response_resource(
        questionnaire_content=questionnaire.content,
        answers=answers,
        subject=f"Patient/{target.patient_id}",
        author=f"User/{current_user.id}",
        status="completed",
    )
    fhir_response = validate_fhir_questionnaire_response(resource)
    metadata = extract_questionnaire_response_metadata(fhir_response)
    fhir_id = generate_fhir_id()
    resource["id"] = fhir_id

    db.add(
        QuestionnaireResponse(
            fhir_id=fhir_id,
            questionnaire_id=target.questionnaire_id,
            content=resource,
            status=metadata.get("status"),
            subject=metadata.get("subject"),
            authored=metadata.get("authored"),
            author=metadata.get("author"),
            # チェック確定者が記入責任者（SoD 判定用）。検証状態は既定 unverified。
            submitted_by_id=current_user.id,
        )
    )

    target.status = InputTargetStatus.COMPLETED
    target.completed_at = datetime.now(UTC)
    target.needs_attention = False
    await db.commit()
    logger.info(
        f"チェック確定: target={target_id} patient={target.patient_id} by={current_user.username}"
    )

    # 確定でリースは認可を失う（完了済み）。次の患者は一覧から受け取る
    return _targets_redirect("確定しました。次の患者を受け取れます")


@router.post("/datasets/{dataset_id}/check/{target_id}/flag")
async def flag_check(
    request: Request,
    dataset_id: int,
    target_id: int,
    current_user: RequireCollector,
    csrf_protect: Annotated[CsrfProtect, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RedirectResponse:
    """チェック1件を「要確認」にして後回しにする（状態は ai_drafted のまま、リースは手放す）。"""
    await csrf_protect.validate_csrf(request)
    target = (
        await db.execute(select(InputTarget).where(InputTarget.id == target_id))
    ).scalar_one_or_none()
    if target is None or target.dataset_id != dataset_id:
        return RedirectResponse(url=f"/datasets/{dataset_id}", status_code=303)

    if target.status != InputTargetStatus.AI_DRAFTED or not lease_is_valid(target, current_user.id):
        return _targets_redirect(_NO_LEASE_NOTICE)

    target.needs_attention = True
    await release_lease(db, target)
    return _targets_redirect("要確認として記録し、担当を手放しました")
