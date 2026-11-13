"""API スキーマ定義"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


# Questionnaire スキーマ
class QuestionnaireCreate(BaseModel):
    """Questionnaire 作成リクエスト"""

    resource: dict[str, Any] = Field(..., description="FHIR Questionnaire リソース (JSON)")


class QuestionnaireUpdate(BaseModel):
    """Questionnaire 更新リクエスト"""

    resource: dict[str, Any] = Field(..., description="FHIR Questionnaire リソース (JSON)")


class QuestionnaireResponse(BaseModel):
    """Questionnaire レスポンス"""

    id: int = Field(..., description="内部ID")
    fhir_id: str = Field(..., description="FHIR リソースID")
    title: str | None = Field(None, description="タイトル")
    status: str = Field(..., description="ステータス")
    version: str | None = Field(None, description="バージョン")
    url: str | None = Field(None, description="URL")
    created_at: datetime = Field(..., description="作成日時")
    updated_at: datetime = Field(..., description="更新日時")

    model_config = ConfigDict(from_attributes=True)


class QuestionnaireDetail(QuestionnaireResponse):
    """Questionnaire 詳細レスポンス（リソース全体を含む）"""

    content: dict[str, Any] = Field(..., description="FHIR Questionnaire リソース全体")


# QuestionnaireResponse スキーマ
class QuestionnaireResponseCreate(BaseModel):
    """QuestionnaireResponse 作成リクエスト"""

    questionnaire_id: int = Field(..., description="QuestionnaireのID")
    answers: dict[str, Any] = Field(..., description="回答データ（linkIdをキーとした回答）")
    subject: str | None = Field(None, description="患者参照（例: 'Patient/123'）")
    author: str | None = Field(
        None, description="（無視される）作成者は認証したユーザーの `User/{id}` に固定される"
    )
    input_target_id: int | None = Field(
        None, description="保存する入力対象のID（指定するなら、自分が有効なリースを持つ対象）"
    )


class QuestionnaireResponseOut(BaseModel):
    """QuestionnaireResponse レスポンス"""

    id: int = Field(..., description="内部ID")
    fhir_id: str = Field(..., description="FHIR リソースID")
    questionnaire_id: int = Field(..., description="QuestionnaireのID")
    status: str = Field(..., description="ステータス")
    subject: str | None = Field(None, description="患者参照")
    authored: str | None = Field(None, description="作成日時")
    author: str | None = Field(None, description="作成者参照")
    created_at: datetime = Field(..., description="作成日時")
    updated_at: datetime = Field(..., description="更新日時")

    model_config = ConfigDict(from_attributes=True)


class QuestionnaireResponseDetail(QuestionnaireResponseOut):
    """QuestionnaireResponse 詳細レスポンス（リソース全体を含む）"""

    content: dict[str, Any] = Field(..., description="FHIR QuestionnaireResponse リソース全体")
