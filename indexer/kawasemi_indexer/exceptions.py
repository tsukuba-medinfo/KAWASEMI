"""例外（KAWASEMI 本体 kawasemi.core.exceptions の indexer 自前・最小版）。

llm_json が使う AIServiceException だけを提供する。
"""

from __future__ import annotations

from typing import Any


class KawasemiBaseException(Exception):
    """KAWASEMI 系例外の基底。"""

    def __init__(self, message: str, error_code: str | None = None, details: Any = None) -> None:
        self.message = message
        self.error_code = error_code
        self.details = details
        super().__init__(self.message)


class AIServiceException(KawasemiBaseException):
    """AI サービス（Ollama）エラーの例外。"""

    def __init__(self, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message=message, error_code="AI_SERVICE_ERROR", details=details or {})
