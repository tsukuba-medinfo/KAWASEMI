"""Questionnaire API エンドポイント"""

from typing import Annotated

from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import FileResponse
from fastapi_csrf_protect import CsrfProtect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.api.v100.schemas import (
    QuestionnaireCreate,
    QuestionnaireDetail,
    QuestionnaireResponse,
    QuestionnaireUpdate,
)
from kawasemi.core.database import get_db
from kawasemi.core.deps import get_current_user, require_roles
from kawasemi.core.exceptions import QuestionnaireNotFoundException
from kawasemi.core.logging_config import get_logger
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.user import User
from kawasemi.utils.fhir_utils import (
    extract_questionnaire_metadata,
    generate_fhir_id,
    validate_fhir_questionnaire,
)
from kawasemi.utils.file_export import export_questionnaire_to_file

router = APIRouter()
logger = get_logger(__name__)


@router.post("/", response_model=QuestionnaireDetail, status_code=201)
async def create_questionnaire(
    request: Request,
    data: QuestionnaireCreate,
    current_user: Annotated[User, Depends(require_roles("admin"))],
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Questionnaire:
    """
    Questionnaire を登録

    FHIR Questionnaire リソースを受け取り、バリデーション後にデータベースに保存する

    権限: admin
    """
    # CSRF検証
    await csrf_protect.validate_csrf(request)

    logger.info(
        f"Questionnaire登録リクエストを受信 (user_id={current_user.id}, username={current_user.username})"
    )

    # FHIR バリデーション
    fhir_questionnaire = validate_fhir_questionnaire(data.resource)
    logger.debug(f"FHIRバリデーション成功: {fhir_questionnaire.id}")

    # メタデータ抽出
    metadata = extract_questionnaire_metadata(fhir_questionnaire)

    # FHIR IDの生成または使用
    fhir_id = fhir_questionnaire.id or generate_fhir_id()

    # データベースに保存
    questionnaire = Questionnaire(
        fhir_id=fhir_id,
        content=data.resource,
        title=metadata.get("title"),
        status=metadata.get("status"),
        version=metadata.get("version"),
        url=metadata.get("url"),
    )

    db.add(questionnaire)
    await db.commit()
    await db.refresh(questionnaire)

    logger.info(f"Questionnaireを登録しました: id={questionnaire.id}, fhir_id={fhir_id}")
    return questionnaire


@router.get("/{questionnaire_id}", response_model=QuestionnaireDetail)
async def get_questionnaire(
    questionnaire_id: int,
    current_user: Annotated[User, Depends(get_current_user)],
    db: AsyncSession = Depends(get_db),
) -> Questionnaire:
    """
    Questionnaire を ID で取得

    指定されたIDのQuestionnaireを取得する

    権限: 認証済みユーザー全員
    """
    result = await db.execute(
        select(Questionnaire).where(
            Questionnaire.id == questionnaire_id,
            Questionnaire.is_deleted == False,  # noqa: E712
        )
    )
    questionnaire = result.scalar_one_or_none()

    if not questionnaire:
        raise QuestionnaireNotFoundException(questionnaire_id)

    return questionnaire


@router.get("/", response_model=list[QuestionnaireResponse])
async def list_questionnaires(
    current_user: Annotated[User, Depends(get_current_user)],
    status: str | None = Query(None, description="ステータスでフィルタ"),
    limit: int = Query(100, ge=1, le=1000, description="取得件数"),
    offset: int = Query(0, ge=0, description="オフセット"),
    db: AsyncSession = Depends(get_db),
) -> list[Questionnaire]:
    """
    Questionnaire 一覧を取得

    ページネーションとフィルタリングをサポート

    権限: 認証済みユーザー全員
    """
    query = (
        select(Questionnaire)
        .where(Questionnaire.is_deleted == False)  # noqa: E712
        .order_by(Questionnaire.created_at.desc())
    )

    # ステータスフィルタ
    if status:
        query = query.where(Questionnaire.status == status)

    # ページネーション
    query = query.offset(offset).limit(limit)

    result = await db.execute(query)
    questionnaires = list(result.scalars().all())

    logger.info(f"Questionnaire一覧を取得: {len(questionnaires)}件")
    return questionnaires


@router.post("/{questionnaire_id}/export", response_class=FileResponse)
async def export_questionnaire(
    request: Request,
    questionnaire_id: int,
    current_user: Annotated[User, Depends(require_roles("reviewer"))],
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """
    Questionnaire を JSON ファイルとしてエクスポート

    指定されたIDのQuestionnaireをJSONファイルとして出力し、ダウンロードを提供する

    権限: reviewer
    """
    # CSRF検証
    await csrf_protect.validate_csrf(request)

    result = await db.execute(
        select(Questionnaire).where(
            Questionnaire.id == questionnaire_id,
            Questionnaire.is_deleted == False,  # noqa: E712
        )
    )
    questionnaire = result.scalar_one_or_none()

    if not questionnaire:
        raise QuestionnaireNotFoundException(questionnaire_id)

    # ファイルに出力
    filepath = export_questionnaire_to_file(
        questionnaire_id=questionnaire.id,
        fhir_id=questionnaire.fhir_id,
        content=questionnaire.content,
    )

    logger.info(f"Questionnaireをエクスポート: {filepath}")

    return FileResponse(
        path=str(filepath),
        media_type="application/fhir+json",
        filename=filepath.name,
        headers={"X-Content-Type-Options": "nosniff"},
    )


@router.put("/{questionnaire_id}", response_model=QuestionnaireDetail)
async def update_questionnaire(
    request: Request,
    questionnaire_id: int,
    data: QuestionnaireUpdate,
    current_user: Annotated[User, Depends(require_roles("designer"))],
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Questionnaire:
    """
    Questionnaire を更新

    指定されたIDのQuestionnaireを更新する。
    FHIR Questionnaireリソースを受け取り、バリデーション後に更新する。

    権限: designer（様式の内容の編集）
    """
    # CSRF検証
    await csrf_protect.validate_csrf(request)

    logger.info(
        f"Questionnaire更新リクエストを受信: id={questionnaire_id} (user_id={current_user.id})"
    )

    # 既存のQuestionnaireを取得
    result = await db.execute(
        select(Questionnaire).where(
            Questionnaire.id == questionnaire_id,
            Questionnaire.is_deleted == False,  # noqa: E712
        )
    )
    questionnaire = result.scalar_one_or_none()

    if not questionnaire:
        raise QuestionnaireNotFoundException(questionnaire_id)

    # FHIR バリデーション
    fhir_questionnaire = validate_fhir_questionnaire(data.resource)
    logger.debug(f"FHIRバリデーション成功: {fhir_questionnaire.id}")

    # メタデータ抽出
    metadata = extract_questionnaire_metadata(fhir_questionnaire)

    # 更新
    questionnaire.content = data.resource
    questionnaire.title = metadata.get("title")
    questionnaire.status = metadata.get("status")  # type: ignore[assignment]
    questionnaire.version = metadata.get("version")
    questionnaire.url = metadata.get("url")

    await db.commit()
    await db.refresh(questionnaire)

    logger.info(f"Questionnaireを更新しました: id={questionnaire.id}")
    return questionnaire


@router.delete("/{questionnaire_id}", status_code=204)
async def delete_questionnaire(
    request: Request,
    questionnaire_id: int,
    current_user: Annotated[User, Depends(require_roles("admin"))],
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> None:
    """
    Questionnaire を削除（論理削除）

    指定されたIDのQuestionnaireを論理削除する。
    実際にはis_deletedフラグをTrueに設定する。

    権限: admin のみ
    """
    # CSRF検証
    await csrf_protect.validate_csrf(request)

    logger.info(
        f"Questionnaire削除リクエストを受信: id={questionnaire_id} (user_id={current_user.id})"
    )

    # 既存のQuestionnaireを取得
    result = await db.execute(
        select(Questionnaire).where(
            Questionnaire.id == questionnaire_id,
            Questionnaire.is_deleted == False,  # noqa: E712
        )
    )
    questionnaire = result.scalar_one_or_none()

    if not questionnaire:
        raise QuestionnaireNotFoundException(questionnaire_id)

    # 論理削除
    questionnaire.is_deleted = True
    await db.commit()

    logger.info(f"Questionnaireを削除しました: id={questionnaire.id}")
