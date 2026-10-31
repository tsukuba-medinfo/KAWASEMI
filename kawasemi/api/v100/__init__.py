"""API v100 router"""

from fastapi import APIRouter, Depends
from fastapi.responses import JSONResponse
from fastapi_csrf_protect import CsrfProtect

from kawasemi.api.v100 import questionnaire_responses, questionnaires

router = APIRouter()


@router.get("/csrf-token")
async def get_csrf_token(csrf_protect: CsrfProtect = Depends()) -> JSONResponse:
    """CSRFトークンを取得（外部システム連携用）

    外部システムがAPI v100を呼び出す際に、以下の手順で使用します:
    1. このエンドポイントでCSRFトークンを取得（署名済みトークンは Cookie に設定される）
    2. POST/PUT/DELETEリクエストのヘッダーに X-CSRF-Token を含める

    ダブルサブミット方式では検証時に署名済みトークン（Cookie）と raw トークン
    （ヘッダ）の対が必要なため、raw トークンを返すと同時に署名済みトークンを
    Cookie へ設定する。

    Returns:
        JSONResponse: CSRFトークンを含むレスポンス（署名済みトークンの Cookie 付き）
    """
    csrf_token, signed_token = csrf_protect.generate_csrf_tokens()
    response = JSONResponse(content={"csrf_token": csrf_token})
    csrf_protect.set_csrf_cookie(signed_token, response)
    return response


# サブルーターの登録
router.include_router(questionnaires.router, prefix="/questionnaires", tags=["Questionnaires"])
router.include_router(
    questionnaire_responses.router,
    prefix="/questionnaire-responses",
    tags=["QuestionnaireResponses"],
)
