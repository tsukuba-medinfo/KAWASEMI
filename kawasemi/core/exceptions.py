"""カスタム例外クラス定義"""

from typing import Any


class KawasemiBaseException(Exception):
    """KAWASEMI 共通基底例外クラス"""

    def __init__(self, message: str, error_code: str | None = None, details: Any = None):
        self.message = message
        self.error_code = error_code
        self.details = details
        super().__init__(self.message)


class ResourceNotFoundException(KawasemiBaseException):
    """リソースが見つからない場合の例外"""

    def __init__(self, resource_type: str, resource_id: str | int):
        message = f"{resource_type}が見つかりません: ID={resource_id}"
        super().__init__(message=message, error_code="RESOURCE_NOT_FOUND")


class ValidationException(KawasemiBaseException):
    """バリデーションエラーの例外"""

    def __init__(self, message: str, validation_errors: list[dict[str, Any]] | None = None):
        super().__init__(
            message=message,
            error_code="VALIDATION_ERROR",
            details={"errors": validation_errors or []},
        )


class FHIRValidationException(ValidationException):
    """FHIR バリデーションエラーの例外"""

    def __init__(self, message: str, validation_errors: list[dict[str, Any]] | None = None):
        super().__init__(message=message, validation_errors=validation_errors)
        self.error_code = "FHIR_VALIDATION_ERROR"


class DatabaseException(KawasemiBaseException):
    """データベース操作エラーの例外"""

    def __init__(self, message: str, original_error: Exception | None = None):
        super().__init__(
            message=message,
            error_code="DATABASE_ERROR",
            details={"original_error": str(original_error)},
        )


class FHIRServerException(KawasemiBaseException):
    """FHIR サーバ通信エラーの例外"""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message=message, error_code="FHIR_SERVER_ERROR", details=details or {})


class QuestionnaireNotFoundException(ResourceNotFoundException):
    """Questionnaire が見つからない場合の例外"""

    def __init__(self, questionnaire_id: str | int):
        super().__init__(resource_type="Questionnaire", resource_id=questionnaire_id)


class QuestionnaireResponseNotFoundException(ResourceNotFoundException):
    """QuestionnaireResponse が見つからない場合の例外"""

    def __init__(self, response_id: str | int):
        super().__init__(resource_type="QuestionnaireResponse", resource_id=response_id)


class ValueSetNotFoundException(ResourceNotFoundException):
    """ValueSet が見つからない場合の例外"""

    def __init__(self, valueset_id: str | int):
        super().__init__(resource_type="ValueSet", resource_id=valueset_id)


class AIServiceException(KawasemiBaseException):
    """AI サービス（Ollama）エラーの例外"""

    def __init__(self, message: str, details: dict[str, Any] | None = None):
        super().__init__(message=message, error_code="AI_SERVICE_ERROR", details=details or {})
