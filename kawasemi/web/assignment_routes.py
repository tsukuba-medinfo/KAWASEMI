"""症例割り当てルーター"""

from datetime import UTC, date
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi_csrf_protect import CsrfProtect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from kawasemi.core.database import get_db
from kawasemi.core.deps import require_roles
from kawasemi.core.logging_config import get_logger
from kawasemi.models.case_assignment import AssignmentStatus, CaseAssignment
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import Role, RoleType
from kawasemi.models.user import User, UserRole
from kawasemi.web.filter_utils import parse_int_filter
from kawasemi.web.template_globals import register_template_globals

router = APIRouter()
logger = get_logger(__name__)

templates = Jinja2Templates(directory="kawasemi/web/templates")
register_template_globals(templates)

# 症例割り当ては収集の行為なので収集者（管理者から移した）
RequireCaseAssignment = Annotated[User, Depends(require_roles("collector"))]


@router.get("/assignments", response_class=HTMLResponse)
async def list_assignments(
    request: Request,
    current_user: RequireCaseAssignment,
    db: Annotated[AsyncSession, Depends(get_db)],
    questionnaire_id: str | None = Query(default=None),
    status: str | None = Query(default=None),
    assigned_to_id: str | None = Query(default=None),
    warn: str | None = Query(default=None),
) -> HTMLResponse:
    """症例割り当て一覧画面"""
    # フィルタ値のパース（不正値は無視しユーザーへ通知）
    filter_warnings: list[str] = []
    questionnaire_id_int = parse_int_filter(questionnaire_id, "Questionnaire", filter_warnings)
    assigned_to_id_int = parse_int_filter(assigned_to_id, "担当者", filter_warnings)
    # 作成/更新で期限（due_date）の形式が不正だった場合の通知（redirect 経由）
    if warn == "due_date":
        filter_warnings.append("期限の日付形式が不正だったため、期限を未設定で保存しました")

    query = (
        select(CaseAssignment)
        .options(
            selectinload(CaseAssignment.questionnaire),
            selectinload(CaseAssignment.assigned_to),
            selectinload(CaseAssignment.assigned_by),
        )
        .order_by(CaseAssignment.created_at.desc())
    )

    if questionnaire_id_int:
        query = query.where(CaseAssignment.questionnaire_id == questionnaire_id_int)
    if status:
        query = query.where(CaseAssignment.status == status)
    if assigned_to_id_int:
        query = query.where(CaseAssignment.assigned_to_id == assigned_to_id_int)

    result = await db.execute(query)
    assignments = list(result.scalars().all())

    # フィルター用のデータを取得
    q_result = await db.execute(
        select(Questionnaire).where(~Questionnaire.is_deleted).order_by(Questionnaire.title)
    )
    questionnaires = list(q_result.scalars().all())

    # 収集者ロールを持つユーザーを取得
    u_result = await db.execute(
        select(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(UserRole.role)
        .where(User.is_active, Role.name == RoleType.COLLECTOR)
        .distinct()
    )
    users = list(u_result.scalars().all())

    return templates.TemplateResponse(
        "assignment_list.html",
        {
            "request": request,
            "current_user": current_user,
            "assignments": assignments,
            "questionnaires": questionnaires,
            "users": users,
            "statuses": AssignmentStatus.ALL,
            "status_labels": AssignmentStatus.LABELS,
            "filters": {
                "questionnaire_id": questionnaire_id_int,
                "status": status,
                "assigned_to_id": assigned_to_id_int,
            },
            "filter_warnings": filter_warnings,
        },
    )


@router.get("/assignments/new", response_class=HTMLResponse)
async def new_assignment(
    request: Request,
    current_user: RequireCaseAssignment,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> HTMLResponse:
    """症例割り当て新規作成画面"""
    # Questionnaireを取得
    q_result = await db.execute(
        select(Questionnaire)
        .where(~Questionnaire.is_deleted, Questionnaire.status == "active")
        .order_by(Questionnaire.title)
    )
    questionnaires = list(q_result.scalars().all())

    # 収集者ロールを持つユーザーを取得
    u_result = await db.execute(
        select(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(UserRole.role)
        .where(User.is_active, Role.name == RoleType.COLLECTOR)
        .distinct()
    )
    users = list(u_result.scalars().all())

    return templates.TemplateResponse(
        "assignment_edit.html",
        {
            "request": request,
            "current_user": current_user,
            "assignment": None,
            "questionnaires": questionnaires,
            "users": users,
            "statuses": AssignmentStatus.ALL,
            "status_labels": AssignmentStatus.LABELS,
            "error": None,
        },
    )


@router.post("/assignments/new", response_class=HTMLResponse)
async def create_assignment(
    request: Request,
    current_user: RequireCaseAssignment,
    csrf_protect: Annotated[CsrfProtect, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
    questionnaire_id: Annotated[int, Form()],
    patient_id: Annotated[str, Form()],
    assigned_to_id: Annotated[int, Form()],
    patient_name: Annotated[str | None, Form()] = None,
    due_date: Annotated[str | None, Form()] = None,
    notes: Annotated[str | None, Form()] = None,
) -> Response:
    """症例割り当て作成処理"""
    await csrf_protect.validate_csrf(request)
    # due_dateをdate型に変換（不正形式は未設定とし、一覧で通知する）
    due_date_obj = None
    due_date_invalid = False
    if due_date and due_date.strip():
        try:
            due_date_obj = date.fromisoformat(due_date)
        except ValueError:
            due_date_invalid = True

    assignment = CaseAssignment(
        questionnaire_id=questionnaire_id,
        patient_id=patient_id,
        patient_name=patient_name or None,
        assigned_to_id=assigned_to_id,
        assigned_by_id=current_user.id,
        status=AssignmentStatus.PENDING,
        due_date=due_date_obj,
        notes=notes or None,
    )

    db.add(assignment)
    await db.commit()

    logger.info(f"症例割り当てを作成しました: id={assignment.id}")
    url = "/assignments?warn=due_date" if due_date_invalid else "/assignments"
    return RedirectResponse(url=url, status_code=303)


@router.get("/assignments/{assignment_id}/edit", response_class=HTMLResponse)
async def edit_assignment(
    request: Request,
    assignment_id: int,
    current_user: RequireCaseAssignment,
    db: Annotated[AsyncSession, Depends(get_db)],
) -> Response:
    """症例割り当て編集画面"""
    result = await db.execute(
        select(CaseAssignment)
        .where(CaseAssignment.id == assignment_id)
        .options(selectinload(CaseAssignment.questionnaire))
    )
    assignment = result.scalar_one_or_none()

    if not assignment:
        return RedirectResponse(url="/assignments", status_code=303)

    # Questionnaireを取得
    q_result = await db.execute(
        select(Questionnaire).where(~Questionnaire.is_deleted).order_by(Questionnaire.title)
    )
    questionnaires = list(q_result.scalars().all())

    # ユーザーを取得
    u_result = await db.execute(
        select(User)
        .join(UserRole, UserRole.user_id == User.id)
        .join(UserRole.role)
        .where(User.is_active, Role.name == RoleType.COLLECTOR)
        .distinct()
    )
    users = list(u_result.scalars().all())

    return templates.TemplateResponse(
        "assignment_edit.html",
        {
            "request": request,
            "current_user": current_user,
            "assignment": assignment,
            "questionnaires": questionnaires,
            "users": users,
            "statuses": AssignmentStatus.ALL,
            "status_labels": AssignmentStatus.LABELS,
            "error": None,
        },
    )


@router.post("/assignments/{assignment_id}/edit", response_class=HTMLResponse)
async def update_assignment(
    request: Request,
    assignment_id: int,
    current_user: RequireCaseAssignment,
    csrf_protect: Annotated[CsrfProtect, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
    questionnaire_id: Annotated[int, Form()],
    patient_id: Annotated[str, Form()],
    assigned_to_id: Annotated[int, Form()],
    status: Annotated[str, Form()],
    patient_name: Annotated[str | None, Form()] = None,
    due_date: Annotated[str | None, Form()] = None,
    notes: Annotated[str | None, Form()] = None,
) -> Response:
    """症例割り当て更新処理"""
    await csrf_protect.validate_csrf(request)
    result = await db.execute(select(CaseAssignment).where(CaseAssignment.id == assignment_id))
    assignment = result.scalar_one_or_none()

    if not assignment:
        return RedirectResponse(url="/assignments", status_code=303)

    # due_dateをdate型に変換（不正形式は未設定とし、一覧で通知する）
    due_date_obj = None
    due_date_invalid = False
    if due_date and due_date.strip():
        try:
            due_date_obj = date.fromisoformat(due_date)
        except ValueError:
            due_date_invalid = True

    assignment.questionnaire_id = questionnaire_id
    assignment.patient_id = patient_id
    assignment.patient_name = patient_name or None
    assignment.assigned_to_id = assigned_to_id
    assignment.status = status
    assignment.due_date = due_date_obj
    assignment.notes = notes or None

    # 完了の場合は完了日時を設定
    if status == AssignmentStatus.COMPLETED and not assignment.completed_at:
        from datetime import datetime

        assignment.completed_at = datetime.now(UTC)

    await db.commit()

    logger.info(f"症例割り当てを更新しました: id={assignment_id}")
    url = "/assignments?warn=due_date" if due_date_invalid else "/assignments"
    return RedirectResponse(url=url, status_code=303)


@router.post("/assignments/{assignment_id}/delete")
async def delete_assignment(
    request: Request,
    assignment_id: int,
    current_user: RequireCaseAssignment,
    csrf_protect: Annotated[CsrfProtect, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RedirectResponse:
    """症例割り当て削除処理"""
    await csrf_protect.validate_csrf(request)
    result = await db.execute(select(CaseAssignment).where(CaseAssignment.id == assignment_id))
    assignment = result.scalar_one_or_none()

    if assignment:
        await db.delete(assignment)
        await db.commit()
        logger.info(f"症例割り当てを削除しました: id={assignment_id}")

    return RedirectResponse(url="/assignments", status_code=303)


# =============================================================================
# 担当症例（入力者向け）
# =============================================================================

RequireCollector = Annotated[User, Depends(require_roles("collector"))]


@router.get("/my-cases")
async def my_cases_redirect() -> RedirectResponse:
    """/my-cases は /input-targets にリダイレクト"""
    return RedirectResponse(url="/input-targets", status_code=301)


@router.post("/my-cases/{assignment_id}/start")
async def start_case(
    request: Request,
    assignment_id: int,
    # 症例入力の開始も収集者だけ（以前は認証のみで、本人の割り当てに限っていた）
    current_user: RequireCollector,
    csrf_protect: Annotated[CsrfProtect, Depends()],
    db: Annotated[AsyncSession, Depends(get_db)],
) -> RedirectResponse:
    """症例入力を開始"""
    await csrf_protect.validate_csrf(request)
    result = await db.execute(
        select(CaseAssignment).where(
            CaseAssignment.id == assignment_id,
            CaseAssignment.assigned_to_id == current_user.id,
        )
    )
    assignment = result.scalar_one_or_none()

    if assignment and assignment.status == AssignmentStatus.PENDING:
        assignment.status = AssignmentStatus.IN_PROGRESS
        await db.commit()
        # フォーム入力画面にリダイレクト（患者IDと割り当てIDを渡す）
        return RedirectResponse(
            url=f"/forms/{assignment.questionnaire_id}?patient_id={assignment.patient_id}&assignment_id={assignment.id}",
            status_code=303,
        )

    return RedirectResponse(url="/my-cases", status_code=303)
