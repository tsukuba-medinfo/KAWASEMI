"""Web UI ルーター"""

import json
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi_csrf_protect import CsrfProtect
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.api.v100.questionnaire_responses import build_questionnaire_response_resource
from kawasemi.core.authz import user_can_access_patient
from kawasemi.core.config import settings
from kawasemi.core.database import get_db
from kawasemi.core.deps import RequireCollector, get_current_user_optional, require_roles
from kawasemi.core.exceptions import (
    FHIRServerException,
    QuestionnaireNotFoundException,
    QuestionnaireResponseNotFoundException,
    ValueSetNotFoundException,
)
from kawasemi.core.lease import (
    extend_lease,
    mark_completed,
    require_leased_target,
    valid_lease_clause,
)
from kawasemi.core.logging_config import get_logger
from kawasemi.models.input_target import InputTarget
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.questionnaire_response import (
    QuestionnaireResponse,
    ResponseVerificationLog,
    VerificationAction,
    VerificationStatus,
)
from kawasemi.models.user import User
from kawasemi.models.valueset import ValueSet
from kawasemi.utils.fhir_client import (
    EncounterDataExtractor,
    FHIRClient,
    PatientDataExtractor,
)
from kawasemi.utils.fhir_utils import (
    extract_questionnaire_metadata,
    extract_questionnaire_response_metadata,
    extract_valueset_metadata,
    generate_fhir_id,
    validate_fhir_questionnaire,
    validate_fhir_questionnaire_response,
    validate_fhir_valueset,
)
from kawasemi.web.form_generator import FormGenerator
from kawasemi.web.template_globals import register_template_globals

router = APIRouter()
ai_web_router = APIRouter()
logger = get_logger(__name__)


def _extract_valueset_urls(items: list[dict[str, Any]]) -> set[str]:
    """Questionnaire の item ツリーから answerValueSet URL を再帰収集する"""
    urls: set[str] = set()
    for item in items:
        if "answerValueSet" in item:
            urls.add(item["answerValueSet"])
        if "item" in item:
            urls |= _extract_valueset_urls(item["item"])
    return urls


async def _build_valueset_map(
    questionnaire_content: dict[str, Any],
    db: AsyncSession,
) -> dict[str, list[dict[str, str]]]:
    """answerValueSet URL → 選択肢リスト の辞書を DB から構築する"""
    urls = _extract_valueset_urls(questionnaire_content.get("item", []))
    if not urls:
        return {}

    result = await db.execute(select(ValueSet).where(ValueSet.url.in_(urls), ~ValueSet.is_deleted))
    valueset_map: dict[str, list[dict[str, str]]] = {}
    for vs in result.scalars().all():
        if vs.url:
            valueset_map[vs.url] = _extract_valueset_options(vs.content)
    return valueset_map


def _extract_valueset_options(content: dict[str, Any]) -> list[dict[str, str]]:
    """ValueSet JSON から選択肢リストを抽出する（compose と expansion 両形式に対応）"""
    # expansion.contains[] 形式（FHIR サーバ由来）
    expansion = content.get("expansion", {})
    if expansion.get("contains"):
        return [
            {"value": c.get("code", ""), "display": c.get("display", c.get("code", ""))}
            for c in expansion["contains"]
            if c.get("code")
        ]

    # compose.include[].concept[] 形式（ローカル JSON 由来）
    options: list[dict[str, str]] = []
    for include in content.get("compose", {}).get("include", []):
        for concept in include.get("concept", []):
            if concept.get("code"):
                options.append(
                    {"value": concept["code"], "display": concept.get("display", concept["code"])}
                )
    return options


# テンプレートエンジンの設定
templates = Jinja2Templates(directory="kawasemi/web/templates")
register_template_globals(templates)


# =============================================================================
# 認可ヘルパー
# =============================================================================


async def _user_can_access_patient(db: AsyncSession, user: User, patient_id: str) -> bool:
    """患者アクセス判定（共通ロジック kawasemi.core.authz へ委譲）。

    既存呼び出し（show_form / api_patient / submit_form / _user_can_access_response 等）の
    後方互換のため薄いラッパを残す。判定本体は authz.user_can_access_patient に集約。
    """
    return await user_can_access_patient(db, user, patient_id)


async def _user_can_access_response(
    db: AsyncSession, user: User, response_obj: QuestionnaireResponse
) -> bool:
    """収集者が対象の回答へアクセスしてよいか判定する。

    「自分の提出（submitted_by_id）」または「担当患者（subject を InputTarget の有効なリースと
    突合）」の回答に限定し、response_id 総当たりで他収集者担当の回答（プリフィル値＝PHI）を
    閲覧・改ざんできないようにする。管理者の全許可は無い（患者データを見ない）。
    書き込み(update_response)と読み取り(edit_response)で共用する。
    """
    if response_obj.submitted_by_id == user.id:
        return True
    if response_obj.subject:
        subject_str = str(response_obj.subject)
        patient_ref = (
            subject_str.split("/", 1)[1] if subject_str.startswith("Patient/") else subject_str
        )
        return await _user_can_access_patient(db, user, patient_ref)
    return False


# =============================================================================
# トップページ
# =============================================================================


@router.get("/", response_class=HTMLResponse)
async def index(
    request: Request,
    current_user: Annotated[User | None, Depends(get_current_user_optional)],
) -> Response:
    """トップページ。アクティブ役割ごとに各ワークスペースのホームへ振り分ける。"""
    # 未ログインの場合はログインページにリダイレクト
    if not current_user:
        return RedirectResponse(url="/login", status_code=303)

    # 役割別ホーム（ハード・スコープ: アクティブ役割でワークスペースへ振り分ける）。
    #   収集者=入力対象（次の患者の受け取り） / 設計者=収集ダッシュボード / 閲覧・検証者=回答管理 / 管理者=メニュー
    from kawasemi.models.role import RoleType

    active = current_user.active_role
    if active == RoleType.COLLECTOR:
        return RedirectResponse(url="/input-targets", status_code=303)
    if active == RoleType.DESIGNER:
        return RedirectResponse(url="/ai-input/dashboard", status_code=303)
    if active == RoleType.REVIEWER:
        return RedirectResponse(url="/questionnaires?mode=response", status_code=303)

    return templates.TemplateResponse(
        "index.html",
        {"request": request, "current_user": current_user},
    )


# =============================================================================
# Questionnaire管理
# =============================================================================


@router.get("/questionnaires", response_class=HTMLResponse)
async def list_questionnaires(
    request: Request,
    current_user: Annotated[User, Depends(require_roles("admin", "designer", "reviewer"))],
    mode: str | None = Query(None, description="表示モード（response=回答管理モード）"),
    status: str | None = Query(None, description="ステータスフィルタ"),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Questionnaire一覧画面"""
    query = select(Questionnaire).where(~Questionnaire.is_deleted)

    if status:
        query = query.where(Questionnaire.status == status)

    query = query.order_by(Questionnaire.created_at.desc())
    result = await db.execute(query)
    questionnaires = list(result.scalars().all())

    return templates.TemplateResponse(
        "questionnaire_list.html",
        {
            "request": request,
            "current_user": current_user,
            "questionnaires": questionnaires,
            "mode": mode,
            "status_filter": status,
        },
    )


@router.get("/questionnaires/new", response_class=HTMLResponse)
async def new_questionnaire(
    request: Request,
    current_user: Annotated[User, Depends(require_roles("admin"))],
) -> HTMLResponse:
    """Questionnaire新規登録画面"""
    return templates.TemplateResponse(
        "questionnaire_edit.html",
        {"request": request, "current_user": current_user, "questionnaire": None, "error": None},
    )


@router.post("/questionnaires/new", response_class=HTMLResponse)
async def create_questionnaire(
    request: Request,
    current_user: Annotated[User, Depends(require_roles("admin"))],
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Questionnaire新規登録処理"""
    await csrf_protect.validate_csrf(request)
    form_data = await request.form()
    resource_json = form_data.get("resource_json", "")

    try:
        resource = json.loads(str(resource_json))
    except json.JSONDecodeError as e:
        return templates.TemplateResponse(
            "questionnaire_edit.html",
            {
                "request": request,
                "questionnaire": None,
                "error": f"無効なJSON形式です: {e}",
            },
        )

    try:
        # FHIRバリデーション
        fhir_questionnaire = validate_fhir_questionnaire(resource)
        metadata = extract_questionnaire_metadata(fhir_questionnaire)

        # FHIR IDの生成
        fhir_id = generate_fhir_id()
        resource["id"] = fhir_id

        # データベースに保存
        questionnaire = Questionnaire(
            fhir_id=fhir_id,
            content=resource,
            title=metadata.get("title"),
            status=metadata.get("status", "draft"),
            version=metadata.get("version"),
            url=metadata.get("url"),
        )

        db.add(questionnaire)
        await db.commit()
        await db.refresh(questionnaire)

        logger.info(f"Questionnaireを登録しました: id={questionnaire.id}")
        return RedirectResponse(
            url=f"/questionnaires/{questionnaire.id}",
            status_code=303,
        )

    except Exception as e:
        logger.error(f"Questionnaire登録エラー: {e}")
        return templates.TemplateResponse(
            "questionnaire_edit.html",
            {
                "request": request,
                "questionnaire": None,
                "error": f"登録に失敗しました: {e}",
            },
        )


@router.get("/questionnaires/{questionnaire_id}", response_class=HTMLResponse)
async def show_questionnaire(
    request: Request,
    questionnaire_id: int,
    current_user: Annotated[User, Depends(require_roles("admin", "designer"))],
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Questionnaire詳細画面"""
    result = await db.execute(
        select(Questionnaire).where(
            Questionnaire.id == questionnaire_id,
            ~Questionnaire.is_deleted,
        )
    )
    questionnaire = result.scalar_one_or_none()

    if not questionnaire:
        raise QuestionnaireNotFoundException(questionnaire_id)

    return templates.TemplateResponse(
        "questionnaire_detail.html",
        {"request": request, "questionnaire": questionnaire, "current_user": current_user},
    )


@router.get("/questionnaires/{questionnaire_id}/edit", response_class=HTMLResponse)
async def edit_questionnaire(
    request: Request,
    questionnaire_id: int,
    current_user: Annotated[User, Depends(require_roles("designer"))],
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """Questionnaire編集画面"""
    result = await db.execute(
        select(Questionnaire).where(
            Questionnaire.id == questionnaire_id,
            ~Questionnaire.is_deleted,
        )
    )
    questionnaire = result.scalar_one_or_none()

    if not questionnaire:
        raise QuestionnaireNotFoundException(questionnaire_id)

    return templates.TemplateResponse(
        "questionnaire_edit.html",
        {"request": request, "questionnaire": questionnaire, "error": None},
    )


@router.post("/questionnaires/{questionnaire_id}/edit", response_class=HTMLResponse)
async def update_questionnaire(
    request: Request,
    questionnaire_id: int,
    current_user: Annotated[User, Depends(require_roles("designer"))],
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """Questionnaire更新処理"""
    await csrf_protect.validate_csrf(request)
    result = await db.execute(
        select(Questionnaire).where(
            Questionnaire.id == questionnaire_id,
            ~Questionnaire.is_deleted,
        )
    )
    questionnaire = result.scalar_one_or_none()

    if not questionnaire:
        raise QuestionnaireNotFoundException(questionnaire_id)

    form_data = await request.form()
    resource_json = form_data.get("resource_json", "")

    try:
        resource = json.loads(str(resource_json))
    except json.JSONDecodeError as e:
        return templates.TemplateResponse(
            "questionnaire_edit.html",
            {
                "request": request,
                "questionnaire": questionnaire,
                "error": f"無効なJSON形式です: {e}",
            },
        )

    try:
        # FHIRバリデーション
        fhir_questionnaire = validate_fhir_questionnaire(resource)
        metadata = extract_questionnaire_metadata(fhir_questionnaire)

        # FHIR IDを維持
        resource["id"] = questionnaire.fhir_id

        # 更新
        questionnaire.content = resource
        questionnaire.title = metadata.get("title")
        questionnaire.status = metadata.get("status", "draft")
        questionnaire.version = metadata.get("version")
        questionnaire.url = metadata.get("url")

        await db.commit()
        await db.refresh(questionnaire)

        logger.info(f"Questionnaireを更新しました: id={questionnaire.id}")
        return RedirectResponse(
            url=f"/questionnaires/{questionnaire.id}",
            status_code=303,
        )

    except Exception as e:
        logger.error(f"Questionnaire更新エラー: {e}")
        return templates.TemplateResponse(
            "questionnaire_edit.html",
            {
                "request": request,
                "questionnaire": questionnaire,
                "error": f"更新に失敗しました: {e}",
            },
        )


@router.post("/questionnaires/{questionnaire_id}/delete")
async def delete_questionnaire(
    request: Request,
    questionnaire_id: int,
    current_user: Annotated[User, Depends(require_roles("admin"))],
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Questionnaire削除処理（論理削除）"""
    await csrf_protect.validate_csrf(request)
    result = await db.execute(
        select(Questionnaire).where(
            Questionnaire.id == questionnaire_id,
            ~Questionnaire.is_deleted,
        )
    )
    questionnaire = result.scalar_one_or_none()

    if not questionnaire:
        raise QuestionnaireNotFoundException(questionnaire_id)

    questionnaire.is_deleted = True
    await db.commit()

    logger.info(f"Questionnaireを削除しました: id={questionnaire_id}")
    return RedirectResponse(url="/questionnaires", status_code=303)


# =============================================================================
# ValueSet管理
# =============================================================================


@router.get("/valuesets", response_class=HTMLResponse)
async def list_valuesets(
    request: Request,
    current_user: Annotated[User, Depends(require_roles("designer"))],
    status: str | None = Query(None, description="ステータスフィルタ"),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """ValueSet一覧画面"""
    query = select(ValueSet).where(~ValueSet.is_deleted)

    if status:
        query = query.where(ValueSet.status == status)

    query = query.order_by(ValueSet.created_at.desc())
    result = await db.execute(query)
    valuesets = list(result.scalars().all())

    return templates.TemplateResponse(
        "valueset_list.html",
        {
            "request": request,
            "current_user": current_user,
            "valuesets": valuesets,
            "status_filter": status,
        },
    )


@router.get("/valuesets/new", response_class=HTMLResponse)
async def new_valueset(
    request: Request,
    current_user: Annotated[User, Depends(require_roles("designer"))],
) -> HTMLResponse:
    """ValueSet新規登録画面"""
    return templates.TemplateResponse(
        "valueset_edit.html",
        {"request": request, "current_user": current_user, "valueset": None, "error": None},
    )


@router.post("/valuesets/new", response_class=HTMLResponse)
async def create_valueset(
    request: Request,
    current_user: Annotated[User, Depends(require_roles("designer"))],
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """ValueSet新規登録処理"""
    await csrf_protect.validate_csrf(request)
    form_data = await request.form()
    resource_json = form_data.get("resource_json", "")

    try:
        resource = json.loads(str(resource_json))
    except json.JSONDecodeError as e:
        return templates.TemplateResponse(
            "valueset_edit.html",
            {
                "request": request,
                "valueset": None,
                "resource_json": str(resource_json),
                "error": f"無効なJSON形式です: {e}",
            },
        )

    try:
        # FHIRバリデーション
        fhir_valueset = validate_fhir_valueset(resource)
        metadata = extract_valueset_metadata(fhir_valueset)

        # FHIR IDの生成
        fhir_id = generate_fhir_id()
        resource["id"] = fhir_id

        # データベースに保存
        valueset = ValueSet(
            fhir_id=fhir_id,
            content=resource,
            title=metadata.get("title"),
            status=metadata.get("status", "draft"),
            version=metadata.get("version"),
            url=metadata.get("url"),
        )

        db.add(valueset)
        await db.commit()
        await db.refresh(valueset)

        logger.info(f"ValueSetを登録しました: id={valueset.id}")
        return RedirectResponse(
            url=f"/valuesets/{valueset.id}",
            status_code=303,
        )

    except Exception as e:
        # url 重複（unique 制約）や FHIR 検証失敗など。詳細はログに残し、
        # 画面には内部情報を出さない汎用文言を返す。
        await db.rollback()
        logger.error(f"ValueSet登録エラー: {e}")
        return templates.TemplateResponse(
            "valueset_edit.html",
            {
                "request": request,
                "valueset": None,
                "resource_json": str(resource_json),
                "error": "登録に失敗しました。入力内容（url の重複や必須項目）を確認してください。",
            },
        )


@router.get("/valuesets/{valueset_id}", response_class=HTMLResponse)
async def show_valueset(
    request: Request,
    valueset_id: int,
    current_user: Annotated[User, Depends(require_roles("designer"))],
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """ValueSet詳細画面"""
    result = await db.execute(
        select(ValueSet).where(
            ValueSet.id == valueset_id,
            ~ValueSet.is_deleted,
        )
    )
    valueset = result.scalar_one_or_none()

    if not valueset:
        raise ValueSetNotFoundException(valueset_id)

    # 選択肢プレビュー（expansion.contains / compose.include[].concept の両形式に対応）
    options = _extract_valueset_options(valueset.content)

    return templates.TemplateResponse(
        "valueset_detail.html",
        {
            "request": request,
            "valueset": valueset,
            "current_user": current_user,
            "options": options,
        },
    )


@router.get("/valuesets/{valueset_id}/edit", response_class=HTMLResponse)
async def edit_valueset(
    request: Request,
    valueset_id: int,
    current_user: Annotated[User, Depends(require_roles("designer"))],
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """ValueSet編集画面"""
    result = await db.execute(
        select(ValueSet).where(
            ValueSet.id == valueset_id,
            ~ValueSet.is_deleted,
        )
    )
    valueset = result.scalar_one_or_none()

    if not valueset:
        raise ValueSetNotFoundException(valueset_id)

    return templates.TemplateResponse(
        "valueset_edit.html",
        {"request": request, "current_user": current_user, "valueset": valueset, "error": None},
    )


@router.post("/valuesets/{valueset_id}/edit", response_class=HTMLResponse)
async def update_valueset(
    request: Request,
    valueset_id: int,
    current_user: Annotated[User, Depends(require_roles("designer"))],
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """ValueSet更新処理"""
    await csrf_protect.validate_csrf(request)
    result = await db.execute(
        select(ValueSet).where(
            ValueSet.id == valueset_id,
            ~ValueSet.is_deleted,
        )
    )
    valueset = result.scalar_one_or_none()

    if not valueset:
        raise ValueSetNotFoundException(valueset_id)

    form_data = await request.form()
    resource_json = form_data.get("resource_json", "")

    try:
        resource = json.loads(str(resource_json))
    except json.JSONDecodeError as e:
        return templates.TemplateResponse(
            "valueset_edit.html",
            {
                "request": request,
                "valueset": valueset,
                "resource_json": str(resource_json),
                "error": f"無効なJSON形式です: {e}",
            },
        )

    try:
        # FHIRバリデーション
        fhir_valueset = validate_fhir_valueset(resource)
        metadata = extract_valueset_metadata(fhir_valueset)

        # FHIR IDを維持
        resource["id"] = valueset.fhir_id

        # 更新
        valueset.content = resource
        valueset.title = metadata.get("title")
        valueset.status = metadata.get("status", "draft")
        valueset.version = metadata.get("version")
        valueset.url = metadata.get("url")

        await db.commit()
        await db.refresh(valueset)

        logger.info(f"ValueSetを更新しました: id={valueset.id}")
        return RedirectResponse(
            url=f"/valuesets/{valueset.id}",
            status_code=303,
        )

    except Exception as e:
        # rollback 後は valueset の属性が expire する。async セッションでは
        # テンプレートの遅延ロードが MissingGreenlet になり 500 化するため、
        # refresh で属性を再取得してから再描画する。入力は resource_json で保持。
        await db.rollback()
        await db.refresh(valueset)
        logger.error(f"ValueSet更新エラー: {e}")
        return templates.TemplateResponse(
            "valueset_edit.html",
            {
                "request": request,
                "valueset": valueset,
                "resource_json": str(resource_json),
                "error": "更新に失敗しました。入力内容（url の重複や必須項目）を確認してください。",
            },
        )


@router.post("/valuesets/{valueset_id}/delete")
async def delete_valueset(
    request: Request,
    valueset_id: int,
    current_user: Annotated[User, Depends(require_roles("designer"))],
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """ValueSet削除処理（論理削除）"""
    await csrf_protect.validate_csrf(request)
    result = await db.execute(
        select(ValueSet).where(
            ValueSet.id == valueset_id,
            ~ValueSet.is_deleted,
        )
    )
    valueset = result.scalar_one_or_none()

    if not valueset:
        raise ValueSetNotFoundException(valueset_id)

    valueset.is_deleted = True
    await db.commit()

    logger.info(f"ValueSetを削除しました: id={valueset_id}")
    return RedirectResponse(url="/valuesets", status_code=303)


# =============================================================================
# QuestionnaireResponse管理
# =============================================================================


@router.get("/questionnaires/{questionnaire_id}/responses", response_class=HTMLResponse)
async def list_responses(
    request: Request,
    questionnaire_id: int,
    current_user: Annotated[User, Depends(require_roles("reviewer"))],
    subject: str | None = Query(None, description="患者フィルタ"),
    status: str | None = Query(None, description="ステータスフィルタ"),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """QuestionnaireResponse一覧画面"""
    # Questionnaireの取得
    q_result = await db.execute(
        select(Questionnaire).where(
            Questionnaire.id == questionnaire_id,
            ~Questionnaire.is_deleted,
        )
    )
    questionnaire = q_result.scalar_one_or_none()

    if not questionnaire:
        raise QuestionnaireNotFoundException(questionnaire_id)

    # QuestionnaireResponseの取得
    query = select(QuestionnaireResponse).where(
        QuestionnaireResponse.questionnaire_id == questionnaire_id
    )

    if subject:
        query = query.where(QuestionnaireResponse.subject.contains(subject))
    if status:
        query = query.where(QuestionnaireResponse.status == status)

    query = query.order_by(QuestionnaireResponse.created_at.desc())
    result = await db.execute(query)
    responses = list(result.scalars().all())

    return templates.TemplateResponse(
        "response_list.html",
        {
            "request": request,
            "questionnaire": questionnaire,
            "responses": responses,
            "subject_filter": subject,
            "status_filter": status,
        },
    )


@router.get("/responses/{response_id}", response_class=HTMLResponse)
async def show_response(
    request: Request,
    response_id: int,
    current_user: Annotated[User, Depends(require_roles("reviewer"))],
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """QuestionnaireResponse詳細画面"""
    result = await db.execute(
        select(QuestionnaireResponse).where(QuestionnaireResponse.id == response_id)
    )
    response = result.scalar_one_or_none()

    if not response:
        raise QuestionnaireResponseNotFoundException(response_id)

    # Questionnaireの取得
    q_result = await db.execute(
        select(Questionnaire).where(Questionnaire.id == response.questionnaire_id)
    )
    questionnaire = q_result.scalar_one_or_none()

    # 回答内容を整形
    answers = []
    if response.content and "item" in response.content:
        # Questionnaireのitemからテキストを取得するためのマップを作成
        question_map = {}
        if questionnaire and questionnaire.content and "item" in questionnaire.content:
            for item in questionnaire.content["item"]:
                question_map[item.get("linkId")] = item.get("text", "")

        for item in response.content["item"]:
            link_id = item.get("linkId", "")
            value = ""

            if "answer" in item and item["answer"]:
                answer = item["answer"][0]
                if "valueString" in answer:
                    value = answer["valueString"]
                elif "valueBoolean" in answer:
                    value = "はい" if answer["valueBoolean"] else "いいえ"
                elif "valueInteger" in answer:
                    value = str(answer["valueInteger"])
                elif "valueDecimal" in answer:
                    value = str(answer["valueDecimal"])
                elif "valueDate" in answer:
                    value = answer["valueDate"]
                elif "valueDateTime" in answer:
                    value = answer["valueDateTime"]
                elif "valueTime" in answer:
                    value = answer["valueTime"]
                elif "valueCoding" in answer:
                    coding = answer["valueCoding"]
                    value = coding.get("display") or coding.get("code", "")

            answers.append(
                {
                    "link_id": link_id,
                    "text": question_map.get(link_id, ""),
                    "value": value,
                }
            )

    # 検証ワークフローの表示・操作可否。
    # 検証者は、未検証/差戻しの回答を verify/reject できる。ただし自分が提出または編集した
    # 回答は検証できない（SoD。verify/reject と同じ判定）。検証済は施錠（編集不可）。
    can_verify = (
        current_user.active_role == "reviewer"
        and response.verification_status != VerificationStatus.VERIFIED
        and not await _touched_response(db, response, current_user.id)
    )
    can_edit = (
        current_user.active_role == "collector"
        and response.verification_status != VerificationStatus.VERIFIED
    )

    return templates.TemplateResponse(
        "response_detail.html",
        {
            "request": request,
            "current_user": current_user,
            "response": response,
            "questionnaire": questionnaire,
            "answers": answers,
            "can_verify": can_verify,
            "can_edit": can_edit,
            "verification_labels": VerificationStatus.LABELS,
        },
    )


@router.get("/responses/{response_id}/edit", response_class=HTMLResponse)
async def edit_response(
    request: Request,
    response_id: int,
    # 差戻し後の再編集は収集者の作業。
    current_user: Annotated[User, Depends(require_roles("collector"))],
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """QuestionnaireResponse編集画面"""
    result = await db.execute(
        select(QuestionnaireResponse).where(QuestionnaireResponse.id == response_id)
    )
    response = result.scalar_one_or_none()

    if not response:
        raise QuestionnaireResponseNotFoundException(response_id)

    # IDOR 防止: 編集フォーム(GET)でも担当外患者の回答値(PHI)を
    # response_id 総当たりで閲覧させない。書き込み側(update_response)と同一ガード。
    if not await _user_can_access_response(db, current_user, response):
        raise HTTPException(
            status_code=403,
            detail="この回答を閲覧・編集する権限がありません（担当症例を確認してください）",
        )

    # 検証済は施錠（不変）。編集できるのは未検証・差戻しのみ。
    if response.verification_status == VerificationStatus.VERIFIED:
        raise HTTPException(status_code=403, detail="検証済みの回答は編集できません（施錠）")

    # Questionnaireの取得
    q_result = await db.execute(
        select(Questionnaire).where(Questionnaire.id == response.questionnaire_id)
    )
    questionnaire = q_result.scalar_one_or_none()

    # フォームフィールドを生成
    fields = []
    if questionnaire and questionnaire.content:
        valueset_map = await _build_valueset_map(questionnaire.content, db)
        generator = FormGenerator(questionnaire.content, valueset_map=valueset_map)
        form_fields = generator.generate_form_fields()

        # 現在の回答値を取得
        answer_map = {}
        if response.content and "item" in response.content:
            for item in response.content["item"]:
                link_id = item.get("linkId", "")
                if "answer" in item and item["answer"]:
                    answer = item["answer"][0]
                    if "valueString" in answer:
                        answer_map[link_id] = answer["valueString"]
                    elif "valueBoolean" in answer:
                        answer_map[link_id] = answer["valueBoolean"]
                    elif "valueInteger" in answer:
                        answer_map[link_id] = answer["valueInteger"]
                    elif "valueDecimal" in answer:
                        answer_map[link_id] = answer["valueDecimal"]
                    elif "valueDate" in answer:
                        answer_map[link_id] = answer["valueDate"]
                    elif "valueDateTime" in answer:
                        answer_map[link_id] = answer["valueDateTime"]
                    elif "valueTime" in answer:
                        answer_map[link_id] = answer["valueTime"]
                    elif "valueCoding" in answer:
                        coding = answer["valueCoding"]
                        answer_map[link_id] = coding.get("code", "")

        # フィールドに値を設定
        for field in form_fields:
            if field["type"] not in ["display", "group"]:
                field["value"] = answer_map.get(field["link_id"])
                fields.append(field)

    return templates.TemplateResponse(
        "response_edit.html",
        {
            "request": request,
            "response": response,
            "questionnaire": questionnaire,
            "fields": fields,
            "error": None,
        },
    )


async def _touched_response(
    db: AsyncSession, response_obj: QuestionnaireResponse, user_id: int
) -> bool:
    """職務分離: このユーザーが回答を提出した、または一度でも編集したか。

    提出者だけを見ると、収集者と検証者の役割を兼ねるユーザーが他人の回答を編集してから
    検証できてしまう。最後の編集者に加え、編集の監査ログに残る全ての編集者も対象にする。
    """
    if response_obj.submitted_by_id == user_id or response_obj.last_edited_by_id == user_id:
        return True
    edited = await db.execute(
        select(ResponseVerificationLog.id)
        .where(
            ResponseVerificationLog.questionnaire_response_id == response_obj.id,
            ResponseVerificationLog.actor_id == user_id,
            ResponseVerificationLog.action == VerificationAction.EDITED,
        )
        .limit(1)
    )
    return edited.first() is not None


@router.post("/responses/{response_id}/edit", response_class=HTMLResponse)
async def update_response(
    request: Request,
    response_id: int,
    current_user: Annotated[User, Depends(require_roles("collector"))],
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """QuestionnaireResponse更新処理"""
    await csrf_protect.validate_csrf(request)
    # 行ロックで読む（PostgreSQL）。検証者の検証と編集が競合しても、施錠の確認と書き込みの間に
    # 検証が割り込まない。ロックの効かない SQLite でも、書き込みは下の条件付き UPDATE が守る。
    result = await db.execute(
        select(QuestionnaireResponse)
        .where(QuestionnaireResponse.id == response_id)
        .with_for_update()
    )
    response_obj = result.scalar_one_or_none()

    if not response_obj:
        raise QuestionnaireResponseNotFoundException(response_id)

    # IDOR 防止: 担当外患者・他者提出の回答を response_id 総当たりで改ざんさせない。
    if not await _user_can_access_response(db, current_user, response_obj):
        raise HTTPException(
            status_code=403,
            detail="この回答を編集する権限がありません（担当症例を確認してください）",
        )

    # 検証済は施錠（不変）。
    if response_obj.verification_status == VerificationStatus.VERIFIED:
        raise HTTPException(status_code=403, detail="検証済みの回答は編集できません（施錠）")

    # Questionnaireの取得
    q_result = await db.execute(
        select(Questionnaire).where(Questionnaire.id == response_obj.questionnaire_id)
    )
    questionnaire = q_result.scalar_one_or_none()

    form_data = await request.form()

    try:
        # 患者（subject）は保存済みの値を保つ。クライアントが送る値は使わない:
        # 認可（`_user_can_access_response`）は保存済みの患者に対してだけなので、
        # 書き換えを許すと担当外の患者へ回答を付け替えられる。作成者（author）も
        # 認証したユーザーにサーバー側で固定する（REST の作成と同じ）。
        subject = response_obj.subject
        author = f"User/{current_user.id}"
        status = str(form_data.get("status") or "completed")

        # 回答データの構築（チェックボックスは bool 化）
        answers: dict[str, Any] = {}
        for key, value in form_data.items():
            if key in ["subject", "author", "status"]:
                continue
            answers[key] = True if value == "on" else value

        # リソースの再構築（JASPEHR 準拠の共通ビルダーへ集約）
        resource = build_questionnaire_response_resource(
            questionnaire_content=questionnaire.content if questionnaire else {},
            answers=answers,
            subject=str(subject) if subject else None,
            author=str(author) if author else None,
            status=status,
        )
        # 既存リソースの論理 ID を維持
        existing_id = response_obj.content.get("id")
        if existing_id:
            resource["id"] = existing_id

        # FHIRバリデーション
        fhir_response = validate_fhir_questionnaire_response(resource)
        metadata = extract_questionnaire_response_metadata(fhir_response)

        was_rejected = response_obj.verification_status == VerificationStatus.REJECTED

        loaded_status = response_obj.verification_status
        loaded_version = response_obj.edit_version
        values: dict[str, Any] = {
            "edit_version": QuestionnaireResponse.edit_version + 1,
            "content": resource,
            "status": metadata.get("status"),
            "subject": metadata.get("subject"),
            "author": metadata.get("author"),
            "authored": metadata.get("authored"),
            "last_edited_by_id": current_user.id,
        }
        # 差戻し後の再編集は「再提出」: 未検証へ戻し、FHIR status を amended にして
        # 検証サイクルをやり直す。監査ログに resubmitted を追記。
        if was_rejected:
            values["verification_status"] = VerificationStatus.UNVERIFIED
            values["rejection_reason"] = None
            values["status"] = "amended"

        # 読んだ時点の状態・版のままであることを書き込みの条件にする（確認と書き込みを
        # 原子的にする）。検証・差戻し・別の編集が割り込んでいれば 0 行になり、書き込まない。
        # 状態が「検証済みでない」だけだと、差戻しの割り込み（was_rejected の判定がずれる）や
        # 別の編集の上書きを許してしまう。
        written = await db.execute(
            update(QuestionnaireResponse)
            .where(
                QuestionnaireResponse.id == response_obj.id,
                QuestionnaireResponse.verification_status == loaded_status,
                QuestionnaireResponse.edit_version == loaded_version,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        if written.rowcount != 1:  # type: ignore[attr-defined]
            await db.rollback()
            raise HTTPException(
                status_code=409,
                detail="回答が検証・差戻し・編集により変わりました。再読み込みしてください",
            )
        # 編集は全て監査ログに残す（誰が・いつ編集したか）。
        db.add(
            ResponseVerificationLog(
                questionnaire_response_id=response_obj.id,
                actor_id=current_user.id,
                action=VerificationAction.EDITED,
            )
        )
        if was_rejected:
            db.add(
                ResponseVerificationLog(
                    questionnaire_response_id=response_obj.id,
                    actor_id=current_user.id,
                    action=VerificationAction.RESUBMITTED,
                )
            )

        await db.commit()
        await db.refresh(response_obj)

        logger.info(f"QuestionnaireResponseを更新しました: id={response_obj.id}")
        return RedirectResponse(
            url=f"/responses/{response_obj.id}",
            status_code=303,
        )

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"QuestionnaireResponse更新エラー: {e}")

        # フィールドを再生成
        fields = []
        if questionnaire and questionnaire.content:
            valueset_map = await _build_valueset_map(questionnaire.content, db)
            generator = FormGenerator(questionnaire.content, valueset_map=valueset_map)
            form_fields = generator.generate_form_fields()
            for field in form_fields:
                if field["type"] not in ["display", "group"]:
                    fields.append(field)

        return templates.TemplateResponse(
            "response_edit.html",
            {
                "request": request,
                "response": response_obj,
                "questionnaire": questionnaire,
                "fields": fields,
                "error": f"更新に失敗しました: {e}",
            },
        )


@router.post("/responses/{response_id}/verify", response_model=None)
async def verify_response(
    request: Request,
    response_id: int,
    current_user: Annotated[User, Depends(require_roles("reviewer"))],
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """検証者が回答を承認（検証済）する。

    SoD: 自分が提出した回答は検証できない。検証済は施錠され、以後編集不可。
    """
    await csrf_protect.validate_csrf(request)
    response_obj = (
        await db.execute(
            select(QuestionnaireResponse)
            .where(QuestionnaireResponse.id == response_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not response_obj:
        raise QuestionnaireResponseNotFoundException(response_id)

    if await _touched_response(db, response_obj, current_user.id):
        raise HTTPException(
            status_code=403,
            detail="自分が提出または編集した回答は検証できません（職務分離）",
        )
    if response_obj.verification_status == VerificationStatus.VERIFIED:
        raise HTTPException(status_code=400, detail="既に検証済みです")

    # 読んだ時点の状態・版のままであることを書き込みの条件にする（割り込む編集・検証を弾く）。
    # 状態が変わらない編集（版だけ進む）も、版の条件で弾く。
    written = await db.execute(
        update(QuestionnaireResponse)
        .where(
            QuestionnaireResponse.id == response_obj.id,
            QuestionnaireResponse.verification_status == response_obj.verification_status,
            QuestionnaireResponse.edit_version == response_obj.edit_version,
        )
        .values(
            verification_status=VerificationStatus.VERIFIED,
            verified_by_id=current_user.id,
            verified_at=datetime.now(UTC),
            rejection_reason=None,
        )
        .execution_options(synchronize_session=False)
    )
    if written.rowcount != 1:  # type: ignore[attr-defined]
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="回答の状態が変わりました。再読み込みしてください"
        )
    db.add(
        ResponseVerificationLog(
            questionnaire_response_id=response_obj.id,
            actor_id=current_user.id,
            action=VerificationAction.VERIFIED,
        )
    )
    await db.commit()
    logger.info(f"回答を検証済にしました: id={response_id} by={current_user.username}")
    return RedirectResponse(url=f"/responses/{response_id}", status_code=303)


@router.post("/responses/{response_id}/reject", response_model=None)
async def reject_response(
    request: Request,
    response_id: int,
    current_user: Annotated[User, Depends(require_roles("reviewer"))],
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """検証者が回答を差戻し（理由必須）する。

    SoD: 自分が提出した回答は検証できない。差戻し後は収集者が再編集→再提出できる。
    """
    await csrf_protect.validate_csrf(request)
    response_obj = (
        await db.execute(
            select(QuestionnaireResponse)
            .where(QuestionnaireResponse.id == response_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if not response_obj:
        raise QuestionnaireResponseNotFoundException(response_id)

    if await _touched_response(db, response_obj, current_user.id):
        raise HTTPException(
            status_code=403,
            detail="自分が提出または編集した回答は検証できません（職務分離）",
        )
    if response_obj.verification_status == VerificationStatus.VERIFIED:
        raise HTTPException(status_code=400, detail="検証済みの回答は差戻しできません")

    form_data = await request.form()
    reason = str(form_data.get("reason") or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="差戻し理由を入力してください")

    written = await db.execute(
        update(QuestionnaireResponse)
        .where(
            QuestionnaireResponse.id == response_obj.id,
            QuestionnaireResponse.verification_status == response_obj.verification_status,
            QuestionnaireResponse.edit_version == response_obj.edit_version,
        )
        .values(
            verification_status=VerificationStatus.REJECTED,
            rejection_reason=reason,
            verified_by_id=None,
            verified_at=None,
        )
        .execution_options(synchronize_session=False)
    )
    if written.rowcount != 1:  # type: ignore[attr-defined]
        await db.rollback()
        raise HTTPException(
            status_code=409, detail="回答の状態が変わりました。再読み込みしてください"
        )
    db.add(
        ResponseVerificationLog(
            questionnaire_response_id=response_obj.id,
            actor_id=current_user.id,
            action=VerificationAction.REJECTED,
            reason=reason,
        )
    )
    await db.commit()
    logger.info(f"回答を差戻しました: id={response_id} by={current_user.username}")
    return RedirectResponse(url=f"/responses/{response_id}", status_code=303)


# =============================================================================
# フォーム入力（既存機能）
# =============================================================================


@router.get("/forms", response_class=HTMLResponse)
async def list_forms(
    request: Request,
    # 入力の画面は収集者だけ。認証のみだと管理者が開けて 403 の行き止まりになる
    current_user: RequireCollector,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """フォーム一覧画面"""
    result = await db.execute(
        select(Questionnaire)
        .where(Questionnaire.status == "active")
        .order_by(Questionnaire.created_at.desc())
    )
    questionnaires = list(result.scalars().all())

    return templates.TemplateResponse(
        "form_list.html",
        {"request": request, "current_user": current_user, "questionnaires": questionnaires},
    )


@router.get("/forms/{questionnaire_id}", response_class=HTMLResponse)
async def show_form(
    request: Request,
    questionnaire_id: int,
    current_user: RequireCollector,
    patient_id: str | None = Query(None, description="FHIRサーバから取得する患者ID"),
    encounter_id: str | None = Query(None, description="FHIRサーバから取得するEncounter ID"),
    input_target_id: int | None = Query(None, description="入力対象ID"),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """フォーム表示画面（事前入力対応）"""
    result = await db.execute(select(Questionnaire).where(Questionnaire.id == questionnaire_id))
    questionnaire = result.scalar_one_or_none()

    if not questionnaire:
        raise QuestionnaireNotFoundException(questionnaire_id)

    # フォーム生成
    valueset_map = await _build_valueset_map(questionnaire.content, db)
    generator = FormGenerator(questionnaire.content, valueset_map=valueset_map)
    fields = generator.generate_form_fields()

    # 事前入力データの取得
    prefill_data = {}
    patient_info = None
    encounter_info = None
    assignment_info = None

    # 入力対象から患者情報を取得
    if input_target_id:
        # 自分が**有効なリース**を持つ入力対象に限定する（任意の input_target_id を URL に付けて
        # 他者担当患者を引き出す横取りを防ぐ。期限切れ・完了済みは対象外）。管理者の全件許可は無い
        it_stmt = select(InputTarget).where(
            InputTarget.id == input_target_id, valid_lease_clause(current_user.id)
        )
        it_result = await db.execute(it_stmt)
        input_target = it_result.scalar_one_or_none()
        if input_target:
            await extend_lease(db, input_target, current_user.id)
            assignment_info = {
                "id": input_target.id,
                "patient_id": input_target.patient_id,
                "patient_name": None,
                "is_input_target": True,
            }
            if not patient_id:
                patient_id = input_target.patient_id
            prefill_data["subject"] = f"Patient/{input_target.patient_id}"

    if patient_id or encounter_id:
        # IDOR 防止: 直接指定された patient_id は担当患者に限定する。
        # input_target 経由で解決された patient_id もこの関門を通す。
        # FHIR へ問い合わせる前に 403 で弾き、担当外患者の PHI が描画されないようにする。
        if patient_id and not await _user_can_access_patient(db, current_user, patient_id):
            raise HTTPException(
                status_code=403,
                detail="この患者データにアクセスする権限がありません（割り当てを確認してください）",
            )
        try:
            fhir_client = FHIRClient()

            # 患者情報の取得
            if patient_id:
                patient = await fhir_client.get_patient(patient_id)
                patient_info = PatientDataExtractor.extract_all(patient)
                prefill_data["subject"] = f"Patient/{patient_id}"

                # 性別フィールドの自動補完
                if patient_info.get("gender"):
                    prefill_data["gender"] = patient_info["gender"]
                if patient_info.get("birthDate"):
                    prefill_data["birthDate"] = patient_info["birth_date"]

            # Encounter情報の取得
            if encounter_id:
                encounter = await fhir_client.get_encounter(encounter_id)
                # IDOR 防止: Encounter.subject（Patient/xxx）を患者スコープで認可する。
                # subject / reference が null（キーはあるが値が None）でも AttributeError に
                # ならないよう or {} と or "" で畳む（fail-closed で 403 に落とす）。
                subject_ref = ((encounter or {}).get("subject") or {}).get("reference") or ""
                encounter_patient_id = (
                    subject_ref.split("/", 1)[1] if subject_ref.startswith("Patient/") else None
                )
                if encounter_patient_id is None or not await _user_can_access_patient(
                    db, current_user, encounter_patient_id
                ):
                    raise HTTPException(
                        status_code=403,
                        detail="このEncounterデータにアクセスする権限がありません",
                    )
                encounter_info = EncounterDataExtractor.extract_all(encounter)

        except FHIRServerException as e:
            logger.warning(f"FHIRサーバからのデータ取得に失敗: {e.message}")
            # エラーが発生しても処理を継続

    return templates.TemplateResponse(
        "form.html",
        {
            "request": request,
            "current_user": current_user,
            "questionnaire_id": questionnaire_id,
            "title": generator.get_title(),
            "description": generator.get_description(),
            "fields": fields,
            "prefill_data": prefill_data,
            "patient_info": patient_info,
            "encounter_info": encounter_info,
            "assignment_info": assignment_info,
            "input_target_id": input_target_id,
            "fhir_enabled": bool(settings.fhir_server_url),
            "enable_when_config": generator.get_enable_when_config(),
        },
    )


@router.get("/api/patient/{patient_id}")
async def get_patient_info(
    patient_id: str,
    current_user: RequireCollector,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    患者情報をFHIRサーバから取得（AJAX用エンドポイント）

    フォームでPatient IDを入力した際に、自動的に患者情報を取得してフィールドを補完するために使用

    権限: 自分に割り当てられた患者のみ（IDOR 防止。管理者の全許可は無い）
    """
    if not await _user_can_access_patient(db, current_user, patient_id):
        return JSONResponse(
            content={"success": False, "error": "この患者データにアクセスする権限がありません"},
            status_code=403,
        )
    try:
        fhir_client = FHIRClient()
        patient = await fhir_client.get_patient(patient_id)
        patient_info = PatientDataExtractor.extract_all(patient)

        return JSONResponse(
            content={
                "success": True,
                "data": patient_info,
            }
        )
    except FHIRServerException as e:
        logger.warning(f"患者情報の取得に失敗: {e.message}")
        return JSONResponse(
            content={
                "success": False,
                "error": e.message,
            },
            status_code=404,
        )


@router.get("/api/encounter/{encounter_id}")
async def get_encounter_info(
    encounter_id: str,
    current_user: RequireCollector,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """
    Encounter情報をFHIRサーバから取得（AJAX用エンドポイント）

    権限: Encounter の被験者（subject=Patient）が自分に割り当てられた患者である場合のみ
    （IDOR 防止。管理者の全許可は無い）
    """
    try:
        fhir_client = FHIRClient()
        encounter = await fhir_client.get_encounter(encounter_id)

        # Encounter.subject（Patient/xxx）を取得し、患者スコープで認可する
        subject_ref = (encounter or {}).get("subject", {}).get("reference", "")
        encounter_patient_id = (
            subject_ref.split("/", 1)[1] if subject_ref.startswith("Patient/") else None
        )
        if encounter_patient_id is None or not await _user_can_access_patient(
            db, current_user, encounter_patient_id
        ):
            return JSONResponse(
                content={
                    "success": False,
                    "error": "このEncounterデータにアクセスする権限がありません",
                },
                status_code=403,
            )

        encounter_info = EncounterDataExtractor.extract_all(encounter)

        return JSONResponse(
            content={
                "success": True,
                "data": encounter_info,
            }
        )
    except FHIRServerException as e:
        logger.warning(f"Encounter情報の取得に失敗: {e.message}")
        return JSONResponse(
            content={
                "success": False,
                "error": e.message,
            },
            status_code=404,
        )


@router.post("/forms/{questionnaire_id}/submit", response_model=None)
async def submit_form(
    questionnaire_id: int,
    request: Request,
    # 確定保存（記入責任）は収集者に限定。管理者は入らない。
    current_user: Annotated[User, Depends(require_roles("collector"))],
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """フォーム送信処理"""
    await csrf_protect.validate_csrf(request)
    logger.info(f"フォーム送信を受信: questionnaire_id={questionnaire_id}")

    # Questionnaireの取得
    result = await db.execute(select(Questionnaire).where(Questionnaire.id == questionnaire_id))
    questionnaire = result.scalar_one_or_none()

    if not questionnaire:
        raise QuestionnaireNotFoundException(questionnaire_id)

    # フォームデータの取得
    form_data = await request.form()
    # PHI 保護: 回答値そのもの（臨床値・subject）はログに出さず、受信フィールド名のみ記録する
    logger.debug(f"フォーム送信フィールド: {list(form_data.keys())}")

    # メタデータの抽出
    subject = form_data.get("subject") or None
    # 作成者はクライアントの入力を採らず、認証したユーザーに固定する（REST の作成と同じ）。
    author = f"User/{current_user.id}"
    input_target_id_str = form_data.get("input_target_id") or None

    # 責任境界: 確定保存は「自分が有効なリースを持つ入力対象（患者 × この様式）」に限定する
    # （REST の作成と同じ検査 `require_leased_target`）。
    leased = await require_leased_target(
        db,
        current_user,
        questionnaire_id,
        str(subject) if subject else None,
        str(input_target_id_str) if input_target_id_str else None,
    )

    # 回答データの構築
    answers: dict[str, Any] = {}
    for key, value in form_data.items():
        if key not in ["subject", "author", "input_target_id"]:
            # チェックボックスの場合はbooleanに変換
            if value == "on":
                answers[key] = True
            else:
                answers[key] = value

    # QuestionnaireResponse リソースの構築（JASPEHR 準拠の共通ビルダーへ集約）
    response_resource = build_questionnaire_response_resource(
        questionnaire_content=questionnaire.content,
        answers=answers,
        subject=str(subject) if subject else None,
        author=author,
    )

    # FHIR バリデーション
    fhir_response = validate_fhir_questionnaire_response(response_resource)
    metadata = extract_questionnaire_response_metadata(fhir_response)

    # FHIR IDの生成
    fhir_id = generate_fhir_id()
    response_resource["id"] = fhir_id

    # データベースに保存
    questionnaire_response = QuestionnaireResponse(
        fhir_id=fhir_id,
        questionnaire_id=questionnaire_id,
        content=response_resource,
        status=metadata.get("status"),
        subject=metadata.get("subject"),
        authored=metadata.get("authored"),
        author=metadata.get("author"),
        # 記入責任者（SoD: 検証者の自己検証禁止の判定に使う）。検証状態は既定 unverified。
        submitted_by_id=current_user.id,
    )

    db.add(questionnaire_response)

    # 入力対象を完了にする（上で検査した、自分が有効なリースを持つ対象）
    mark_completed(leased)
    logger.info(f"入力対象を完了しました: id={leased.id}")

    await db.commit()
    await db.refresh(questionnaire_response)

    logger.info(f"QuestionnaireResponseを保存しました: id={questionnaire_response.id}")

    # 成功画面を表示
    return templates.TemplateResponse(
        "success.html",
        {
            "request": request,
            "response": questionnaire_response,
            "questionnaire_id": questionnaire_id,
        },
    )
