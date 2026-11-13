"""QuestionnaireResponse API エンドポイント"""

import math
import re
import unicodedata
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse
from fastapi_csrf_protect import CsrfProtect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.api.v100.schemas import (
    QuestionnaireResponseCreate,
    QuestionnaireResponseDetail,
    QuestionnaireResponseOut,
)
from kawasemi.core.database import get_db
from kawasemi.core.deps import require_roles
from kawasemi.core.exceptions import (
    QuestionnaireNotFoundException,
    QuestionnaireResponseNotFoundException,
)
from kawasemi.core.lease import mark_completed, require_leased_target
from kawasemi.core.logging_config import get_logger
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.questionnaire_response import QuestionnaireResponse, VerificationStatus
from kawasemi.models.user import User
from kawasemi.utils.fhir_utils import (
    extract_questionnaire_response_metadata,
    generate_fhir_id,
    validate_fhir_questionnaire_response,
)
from kawasemi.utils.file_export import (
    create_bulk_export_zip,
    export_questionnaire_response_to_file,
)

router = APIRouter()
logger = get_logger(__name__)

# JASPEHR 実装ガイド v1.0.0 の QuestionnaireResponse プロファイル canonical URL。
# 注: KAWASEMI はデモ実装であり本プロファイルの一部要件（identifier の複合形式・
# author=Practitioner 等）は未充足。詳細は docs/jaspehr_compliance.md を参照。
QR_PROFILE_URL = (
    "http://www.hosp.ncgm.go.jp/JASPEHR/fhir/StructureDefinition/jaspehr-questionnaireresponse"
)


def index_questionnaire_items(
    questionnaire_content: dict[str, Any] | None,
) -> tuple[dict[str, str | None], dict[str, dict[str, dict[str, Any]]]]:
    """Questionnaire content を走査し linkId ごとの型と answerOption を索引化する。

    Returns:
        (types, options)
        - types: linkId -> item.type
        - options: linkId -> {code -> valueCoding(system/code/display のみ)}
    """
    types: dict[str, str | None] = {}
    options: dict[str, dict[str, dict[str, Any]]] = {}

    def walk(items: list[dict[str, Any]] | None) -> None:
        for it in items or []:
            link_id = it.get("linkId")
            if link_id:
                types[link_id] = it.get("type")
                opts: dict[str, dict[str, Any]] = {}
                for ao in it.get("answerOption") or []:
                    vc = ao.get("valueCoding")
                    if vc and vc.get("code") is not None:
                        opts[str(vc["code"])] = {
                            k: vc[k] for k in ("system", "code", "display") if k in vc
                        }
                if opts:
                    options[link_id] = opts
            walk(it.get("item"))

    walk((questionnaire_content or {}).get("item"))
    return types, options


# 日付の正規化。FHIR の部分日付（年・年月）はそのまま有効な date として残す
_PARTIAL_DATE_RE = re.compile(r"(\d{4})(?:-(\d{2}))?")
# 区切りつきの年月日（`2025/8/5` / `2025-08-05`）と、任意の時刻・タイムゾーン
_YMD_TIME_RE = re.compile(
    r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})"
    r"(?:[ T](\d{1,2}):(\d{2})(?::(\d{2})(?:\.\d+)?)?(Z|[+-]\d{2}:?\d{2})?)?"
)
_YMD8_RE = re.compile(r"(\d{4})(\d{2})(\d{2})")
_TRUE_TOKENS = frozenset({"true", "1", "はい", "yes", "あり"})
_FALSE_TOKENS = frozenset({"false", "0", "いいえ", "no", "なし"})


def _nfkc(value: Any) -> str:
    return unicodedata.normalize("NFKC", str(value)).strip()


def normalize_date(value: Any, *, with_time: bool = False) -> str | None:
    """値を FHIR の date（`with_time=True` なら dateTime）へ正規化する。読めなければ None。

    受ける形（デモの DB は `2025/8/5` のようにゼロ埋めしない）:

    - `date` / `datetime` オブジェクト
    - FHIR の部分日付 `YYYY` / `YYYY-MM`（月が 1〜12）→ **そのまま**
    - `YYYY/M/D` / `YYYY-M-D`（ゼロ埋め任意）→ `YYYY-MM-DD`
    - `YYYYMMDD`（8桁で、暦に在る日付）→ `YYYY-MM-DD`
    - 上の年月日に時刻（`hh:mm[:ss]`、任意のタイムゾーン）が付いたもの → date では
      `YYYY-MM-DD`、dateTime では `YYYY-MM-DDThh:mm:ss`（タイムゾーンがあれば付ける）

    暦に無い日付（`20260599`）、登録様式の番兵（`77777777` / `99999999`）、和暦などは None。
    """
    if isinstance(value, datetime):
        return value.isoformat() if with_time else value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if not isinstance(value, str):
        return None
    s = _nfkc(value)
    m = _PARTIAL_DATE_RE.fullmatch(s)
    if m:
        return s if m.group(2) is None or 1 <= int(m.group(2)) <= 12 else None
    m = _YMD_TIME_RE.fullmatch(s) or _YMD8_RE.fullmatch(s)
    if not m:
        return None
    try:
        d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None
    groups = m.groups()
    if not with_time or len(groups) == 3 or groups[3] is None:
        return d.isoformat()
    hh, mm, ss, tz = int(groups[3]), int(groups[4]), int(groups[5] or 0), groups[6]
    if hh > 23 or mm > 59 or ss > 59:
        return None
    out = f"{d.isoformat()}T{hh:02d}:{mm:02d}:{ss:02d}"
    if tz:
        out += "+00:00" if tz == "Z" else (tz if ":" in tz else f"{tz[:3]}:{tz[3:]}")
    return out


def normalize_answer(
    value: Any,
    item_type: str | None,
    coding_opts: dict[str, dict[str, Any]] | None = None,
) -> tuple[bool, Any]:
    """値を item.type（回答型）へ正規化する。`(合うか, 正規化した値)` を返す。

    autofill が prefill する前に使い、**prefill するのは正規化した値**である。`_build_answer` は
    型に合わない値を黙って `valueString` へ落とす（date 項目に LLM の断り文が入っても保存
    できてしまう）ので、その手前で弾く。型ごとの規則:

    - date / dateTime: `normalize_date`（年月日の区切りと桁、`YYYYMMDD`、時刻つきを受ける）
    - integer: `7` / `"7"` / `"7.0"` → `7`。`"7.5"` は合わない
    - decimal: `"38.5"` → `38.5`。`"38,5"` は合わない
    - boolean: `true/false/1/0/はい/いいえ/yes/no/あり/なし` → 真偽。それ以外は合わない
    - choice（answerOption あり）: コードに一致すればそのコード、**表示名に一致すれば
      そのコードへ写す**（strip と NFKC の後で完全一致）。どちらでもなければ合わない

    型の検査をしない型（string / text / open-choice / 型不明）は値をそのまま返す。リストは
    全要素が合うときだけ合う。
    """
    if isinstance(value, list):
        pairs = [normalize_answer(v, item_type, coding_opts) for v in value]
        return all(ok for ok, _ in pairs), [v for _, v in pairs]
    if item_type in ("date", "dateTime"):
        norm = normalize_date(value, with_time=item_type == "dateTime")
        return norm is not None, norm
    if item_type == "integer":
        if isinstance(value, bool):
            return False, None
        if isinstance(value, int):
            return True, value
        try:
            f = float(_nfkc(value)) if not isinstance(value, float) else value
        except ValueError:
            return False, None
        return (True, int(f)) if math.isfinite(f) and f.is_integer() else (False, None)
    if item_type == "decimal":
        if isinstance(value, bool):
            return False, None
        if isinstance(value, (int, float)):
            return math.isfinite(value), value
        try:
            f = float(_nfkc(value))
        except ValueError:
            return False, None
        return (True, f) if math.isfinite(f) else (False, None)
    if item_type == "boolean":
        if isinstance(value, bool):
            return True, value
        token = _nfkc(value).lower()
        if token in _TRUE_TOKENS:
            return True, True
        if token in _FALSE_TOKENS:
            return True, False
        return False, None
    if item_type == "choice" and coding_opts:
        if isinstance(value, bool):
            return False, None
        if str(value) in coding_opts:
            return True, str(value)
        key = _nfkc(value)
        if key in coding_opts:
            return True, key
        for code, opt in coding_opts.items():
            display = opt.get("display")
            if display is not None and _nfkc(display) == key:
                return True, code
        return False, None
    return True, value


def answer_conforms(
    value: Any,
    item_type: str | None,
    coding_opts: dict[str, dict[str, Any]] | None = None,
) -> bool:
    """値が item.type に合うか（`normalize_answer` の判定だけを返す）。"""
    return normalize_answer(value, item_type, coding_opts)[0]


def _build_answer(
    value: Any,
    item_type: str | None,
    coding_opts: dict[str, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """回答値を answerOption / item.type に従い FHIR answer 配列へ変換する。

    choice 系で answerOption のコードに一致すれば valueCoding を出力。
    一致しない場合は型に応じた値型へフォールバック（最終的に valueString）。
    """
    # choice 一致 → valueCoding
    if coding_opts and not isinstance(value, (list, bool)) and str(value) in coding_opts:
        return [{"valueCoding": dict(coding_opts[str(value)])}]

    if isinstance(value, list):
        out: list[dict[str, Any]] = []
        for v in value:
            if coding_opts and str(v) in coding_opts:
                out.append({"valueCoding": dict(coding_opts[str(v)])})
            else:
                out.append({"valueString": str(v)})
        return out

    if isinstance(value, bool) or item_type == "boolean":
        as_bool = value if isinstance(value, bool) else str(value).lower() in ("on", "true", "1")
        return [{"valueBoolean": as_bool}]

    if item_type == "integer":
        try:
            return [{"valueInteger": int(value)}]
        except (ValueError, TypeError):
            return [{"valueString": str(value)}]

    if item_type == "decimal":
        try:
            return [{"valueDecimal": float(value)}]
        except (ValueError, TypeError):
            return [{"valueString": str(value)}]

    # 型情報がない（linkId が Questionnaire に無い）場合は Python 値型で判定
    if isinstance(value, int):
        return [{"valueInteger": value}]
    if isinstance(value, float):
        return [{"valueDecimal": value}]
    if item_type is None and isinstance(value, str) and value.isdigit():
        return [{"valueInteger": int(value)}]

    return [{"valueString": str(value)}]


def build_questionnaire_response_resource(
    questionnaire_content: dict[str, Any],
    answers: dict[str, Any],
    subject: str | None,
    author: str | None,
    status: str = "completed",
) -> dict[str, Any]:
    """
    QuestionnaireResponse FHIR リソースを構築

    JASPEHR QR プロファイルに合わせ、meta.profile と canonical+version 形式の
    questionnaire 参照を付与し、choice 回答は answerOption の valueCoding を出力する。

    Args:
        questionnaire_content: 対応する Questionnaire の FHIR JSON（url/version/item を参照）
        answers: 回答データ（linkId をキーとした辞書）
        subject: 患者参照
        author: 作成者参照
        status: QuestionnaireResponse.status（completed|amended|stopped 等）

    Returns:
        dict: QuestionnaireResponse FHIRリソース
    """
    types, options = index_questionnaire_items(questionnaire_content)

    items: list[dict[str, Any]] = []
    for link_id, answer_value in answers.items():
        # 空値はスキップ（未入力の項目を空 answer として残さない）
        if answer_value is None or answer_value == "":
            continue
        items.append(
            {
                "linkId": link_id,
                "answer": _build_answer(answer_value, types.get(link_id), options.get(link_id)),
            }
        )

    # questionnaire 参照は canonical URL|version 形式（JASPEHR 準拠）
    url = (questionnaire_content or {}).get("url")
    version = (questionnaire_content or {}).get("version")
    if url and version:
        questionnaire_ref = f"{url}|{version}"
    elif url:
        questionnaire_ref = url
    else:
        # url 未設定の Questionnaire はフォールバックで論理 ID 参照
        questionnaire_ref = f"Questionnaire/{(questionnaire_content or {}).get('id', 'unknown')}"

    resource: dict[str, Any] = {
        "resourceType": "QuestionnaireResponse",
        "meta": {"profile": [QR_PROFILE_URL]},
        "status": status,
        "questionnaire": questionnaire_ref,
        "authored": datetime.now(UTC).isoformat(),
        "item": items,
    }

    if subject:
        resource["subject"] = {"reference": subject}

    if author:
        resource["author"] = {"reference": author}

    return resource


@router.post("/", response_model=QuestionnaireResponseDetail, status_code=201)
async def create_questionnaire_response(
    request: Request,
    data: QuestionnaireResponseCreate,
    current_user: Annotated[User, Depends(require_roles("collector"))],
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> QuestionnaireResponse:
    """
    QuestionnaireResponse を生成・保存

    Questionnaireに対する回答データからQuestionnaireResponseを生成する

    権限: collector。保存できるのは、自分が有効なリースを持つ入力対象（患者 × この様式）だけ
    （`input_target_id` を指定するなら、その対象）。
    """
    # CSRF検証
    await csrf_protect.validate_csrf(request)

    logger.info(
        f"QuestionnaireResponse生成リクエスト: questionnaire_id={data.questionnaire_id} "
        f"(user_id={current_user.id}, username={current_user.username})"
    )

    # Questionnaireの存在確認（論理削除済みには回答を作成させない）
    result = await db.execute(
        select(Questionnaire).where(
            Questionnaire.id == data.questionnaire_id,
            ~Questionnaire.is_deleted,
        )
    )
    questionnaire = result.scalar_one_or_none()

    if not questionnaire:
        raise QuestionnaireNotFoundException(data.questionnaire_id)

    # 責任境界: Web の保存と同じく、自分が有効なリースを持つ入力対象（患者 × この様式）に限る。
    # subject の無い作成も 403（患者を特定できない回答は、担当の検査ができない）。
    leased = await require_leased_target(
        db, current_user, data.questionnaire_id, data.subject, data.input_target_id
    )

    # QuestionnaireResponse リソースの構築
    response_resource = build_questionnaire_response_resource(
        questionnaire_content=questionnaire.content,
        answers=data.answers,
        subject=data.subject,
        author=data.author,
    )

    # FHIR バリデーション
    fhir_response = validate_fhir_questionnaire_response(response_resource)
    logger.debug("FHIRバリデーション成功")

    # メタデータ抽出
    metadata = extract_questionnaire_response_metadata(fhir_response)

    # FHIR IDの生成
    fhir_id = generate_fhir_id()
    response_resource["id"] = fhir_id

    # データベースに保存
    questionnaire_response = QuestionnaireResponse(
        fhir_id=fhir_id,
        questionnaire_id=data.questionnaire_id,
        content=response_resource,
        status=metadata.get("status"),
        subject=metadata.get("subject"),
        authored=metadata.get("authored"),
        author=metadata.get("author"),
        # 記入責任者（SoD 判定用）。検証状態は既定 unverified。
        submitted_by_id=current_user.id,
    )

    db.add(questionnaire_response)
    # 入力対象を完了にする（Web の保存と同じ）
    mark_completed(leased)
    await db.commit()
    await db.refresh(questionnaire_response)

    logger.info(
        f"QuestionnaireResponseを生成しました: id={questionnaire_response.id}, fhir_id={fhir_id}"
    )
    return questionnaire_response


@router.get("/{response_id}", response_model=QuestionnaireResponseDetail)
async def get_questionnaire_response(
    response_id: int,
    current_user: Annotated[User, Depends(require_roles("reviewer"))],
    db: AsyncSession = Depends(get_db),
) -> QuestionnaireResponse:
    """
    QuestionnaireResponse を ID で取得

    指定されたIDのQuestionnaireResponseを取得する

    権限: reviewer のみ。QuestionnaireResponse は PHI（被験者参照・回答内容）を
    含み、所有権を表す属性を持たないため、一覧（list）と同じく reviewer に限定し、
    認証済みユーザーによる ID 総当たりでの他者回答閲覧（IDOR）を防ぐ。
    """
    result = await db.execute(
        select(QuestionnaireResponse).where(QuestionnaireResponse.id == response_id)
    )
    response = result.scalar_one_or_none()

    if not response:
        raise QuestionnaireResponseNotFoundException(response_id)

    return response


@router.get("/", response_model=list[QuestionnaireResponseOut])
async def list_questionnaire_responses(
    current_user: Annotated[User, Depends(require_roles("reviewer"))],
    questionnaire_id: int | None = Query(None, description="QuestionnaireのIDでフィルタ"),
    subject: str | None = Query(None, description="患者参照でフィルタ"),
    limit: int = Query(100, ge=1, le=1000, description="取得件数"),
    offset: int = Query(0, ge=0, description="オフセット"),
    db: AsyncSession = Depends(get_db),
) -> list[QuestionnaireResponse]:
    """
    QuestionnaireResponse 一覧を取得

    ページネーションとフィルタリングをサポート

    権限: reviewer
    """
    query = select(QuestionnaireResponse).order_by(QuestionnaireResponse.created_at.desc())

    # フィルタ
    if questionnaire_id:
        query = query.where(QuestionnaireResponse.questionnaire_id == questionnaire_id)
    if subject:
        query = query.where(QuestionnaireResponse.subject == subject)

    # ページネーション
    query = query.offset(offset).limit(limit)

    result = await db.execute(query)
    responses = list(result.scalars().all())

    logger.info(f"QuestionnaireResponse一覧を取得: {len(responses)}件")
    return responses


@router.post("/{response_id}/export", response_class=FileResponse)
async def export_questionnaire_response(
    request: Request,
    response_id: int,
    current_user: Annotated[User, Depends(require_roles("reviewer"))],
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """
    QuestionnaireResponse を JSON ファイルとしてエクスポート

    指定されたIDのQuestionnaireResponseをJSONファイルとして出力し、ダウンロードを提供する

    権限: reviewer
    """
    # CSRF検証
    await csrf_protect.validate_csrf(request)

    result = await db.execute(
        select(QuestionnaireResponse).where(QuestionnaireResponse.id == response_id)
    )
    response = result.scalar_one_or_none()

    if not response:
        raise QuestionnaireResponseNotFoundException(response_id)

    # 検証ワークフロー: 既定は検証済のみエクスポート可（安全側）。
    # 未検証・差戻しの回答は外部提供しない。
    if response.verification_status != VerificationStatus.VERIFIED:
        raise HTTPException(
            status_code=403,
            detail="検証済みの回答のみエクスポートできます（この回答は未検証または差戻しです）",
        )

    # ファイルに出力
    filepath = export_questionnaire_response_to_file(
        response_id=response.id,
        fhir_id=response.fhir_id,
        content=response.content,
    )

    logger.info(f"QuestionnaireResponseをエクスポート: {filepath}")

    return FileResponse(
        path=str(filepath),
        media_type="application/fhir+json",
        filename=filepath.name,
    )


@router.post("/bulk-export", response_class=FileResponse)
async def bulk_export_questionnaire_responses(
    request: Request,
    current_user: Annotated[User, Depends(require_roles("reviewer"))],
    csrf_protect: CsrfProtect = Depends(),
    questionnaire_id: int | None = Query(None, description="QuestionnaireのIDでフィルタ"),
    subject: str | None = Query(None, description="患者参照でフィルタ"),
    limit: int = Query(1000, ge=1, le=10000, description="最大エクスポート件数"),
    include_unverified: bool = Query(
        False, description="未検証・差戻しも含める（既定は検証済のみ）"
    ),
    db: AsyncSession = Depends(get_db),
) -> FileResponse:
    """
    QuestionnaireResponse を一括でZIPファイルとしてエクスポート

    条件を指定して複数のQuestionnaireResponseをZIPアーカイブとしてダウンロードする。
    各QuestionnaireResponseは個別のJSONファイルとしてZIP内に格納される。

    権限: reviewer のみ
    """
    # CSRF検証
    await csrf_protect.validate_csrf(request)

    logger.info(
        f"一括エクスポートリクエスト: questionnaire_id={questionnaire_id}, subject={subject}"
    )

    # クエリの構築
    query = select(QuestionnaireResponse).order_by(QuestionnaireResponse.created_at.desc())

    if questionnaire_id:
        query = query.where(QuestionnaireResponse.questionnaire_id == questionnaire_id)
    if subject:
        query = query.where(QuestionnaireResponse.subject == subject)
    # 既定は検証済のみ（安全側）。include_unverified=true で全件に広げる。
    if not include_unverified:
        query = query.where(
            QuestionnaireResponse.verification_status == VerificationStatus.VERIFIED
        )

    query = query.limit(limit)

    # データ取得
    result = await db.execute(query)
    responses = list(result.scalars().all())

    if not responses:
        # 空のZIPを作成するのではなく、エラーを返す
        from kawasemi.core.exceptions import ValidationException

        raise ValidationException(message="エクスポート対象のQuestionnaireResponseが見つかりません")

    # ZIPファイルの作成
    export_data = [(r.fhir_id, r.content) for r in responses]
    zip_path = create_bulk_export_zip(export_data)

    logger.info(f"一括エクスポート完了: {len(responses)}件 -> {zip_path}")

    return FileResponse(
        path=str(zip_path),
        media_type="application/zip",
        filename=zip_path.name,
    )
