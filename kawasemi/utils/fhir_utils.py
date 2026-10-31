"""FHIR ユーティリティ関数"""

import uuid
from typing import Any

from fhir.resources.questionnaire import Questionnaire as FHIRQuestionnaire
from fhir.resources.questionnaireresponse import (
    QuestionnaireResponse as FHIRQuestionnaireResponse,
)
from fhir.resources.valueset import ValueSet as FHIRValueSet
from pydantic import ValidationError as PydanticValidationError

from kawasemi.core.exceptions import FHIRValidationException


def validate_fhir_questionnaire(data: dict[str, Any]) -> FHIRQuestionnaire:
    """
    FHIR Questionnaire リソースのバリデーション

    Args:
        data: QuestionnaireのJSONデータ

    Returns:
        FHIRQuestionnaire: 検証済みのQuestionnaireオブジェクト

    Raises:
        FHIRValidationException: バリデーションエラー
    """
    try:
        questionnaire = FHIRQuestionnaire.model_validate(data)
        return questionnaire
    except PydanticValidationError as e:
        errors = [{"field": err["loc"], "message": err["msg"]} for err in e.errors()]
        raise FHIRValidationException(
            message="Questionnaireのバリデーションに失敗しました", validation_errors=errors
        )


def validate_fhir_questionnaire_response(
    data: dict[str, Any],
) -> FHIRQuestionnaireResponse:
    """
    FHIR QuestionnaireResponse リソースのバリデーション

    Args:
        data: QuestionnaireResponseのJSONデータ

    Returns:
        FHIRQuestionnaireResponse: 検証済みのQuestionnaireResponseオブジェクト

    Raises:
        FHIRValidationException: バリデーションエラー
    """
    try:
        response = FHIRQuestionnaireResponse.model_validate(data)
        return response
    except PydanticValidationError as e:
        errors = [{"field": err["loc"], "message": err["msg"]} for err in e.errors()]
        raise FHIRValidationException(
            message="QuestionnaireResponseのバリデーションに失敗しました",
            validation_errors=errors,
        )


def validate_fhir_valueset(data: dict[str, Any]) -> FHIRValueSet:
    """
    FHIR ValueSet リソースのバリデーション

    Args:
        data: ValueSetのJSONデータ

    Returns:
        FHIRValueSet: 検証済みのValueSetオブジェクト

    Raises:
        FHIRValidationException: バリデーションエラー
    """
    try:
        valueset = FHIRValueSet.model_validate(data)
        return valueset
    except PydanticValidationError as e:
        errors = [{"field": err["loc"], "message": err["msg"]} for err in e.errors()]
        raise FHIRValidationException(
            message="ValueSetのバリデーションに失敗しました", validation_errors=errors
        )


def generate_fhir_id() -> str:
    """
    FHIR リソースID を生成

    Returns:
        str: UUID形式のリソースID
    """
    return str(uuid.uuid4())


def extract_questionnaire_metadata(questionnaire: FHIRQuestionnaire) -> dict[str, Any]:
    """
    Questionnaire からメタデータを抽出

    Args:
        questionnaire: FHIRQuestionnaireオブジェクト

    Returns:
        dict: 抽出されたメタデータ
    """
    return {
        "title": questionnaire.title,
        "status": questionnaire.status,
        "version": questionnaire.version,
        "url": questionnaire.url,
    }


def extract_valueset_metadata(valueset: FHIRValueSet) -> dict[str, Any]:
    """
    ValueSet からメタデータを抽出

    Args:
        valueset: FHIRValueSetオブジェクト

    Returns:
        dict: 抽出されたメタデータ（title/status/version/url）
    """
    return {
        "title": valueset.title,
        "status": valueset.status,
        "version": valueset.version,
        "url": valueset.url,
    }


def extract_questionnaire_response_metadata(
    response: FHIRQuestionnaireResponse,
) -> dict[str, Any]:
    """
    QuestionnaireResponse からメタデータを抽出

    Args:
        response: FHIRQuestionnaireResponseオブジェクト

    Returns:
        dict: 抽出されたメタデータ
    """
    return {
        "status": response.status,
        "subject": response.subject.reference if response.subject else None,
        "authored": response.authored.isoformat() if response.authored else None,
        "author": response.author.reference if response.author else None,
    }
