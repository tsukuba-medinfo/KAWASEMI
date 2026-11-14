"""データ自動収集モジュールのWebルーター

ステップ1（データソース調査: 臨床的コンテキスト / データ所在の2役割）・
ステップ2（取得手順書の作成）・ステップ3（取得プランの作成）・
ステップ4（AI自動入力）の画面と API エンドポイントを提供する。

ユーザ向けの名称・番号は kawasemi/ai_input/labels.py に集約している。
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi_csrf_protect import CsrfProtect
from pydantic import ValidationError
from sqlalchemy import case, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.orm.attributes import flag_modified

from kawasemi.ai_input.citation_metrics import citation_coverage
from kawasemi.ai_input.clinical_drafter import AIClinicalDrafter
from kawasemi.ai_input.compiler import AIPlanCompiler
from kawasemi.ai_input.design_provenance import DesignLlmRecorder, save_design_llm_calls_safely
from kawasemi.ai_input.dsl import (
    MAX_STEPS,
    has_needs_input,
    is_empty_program,
    validate_program,
)
from kawasemi.ai_input.executor import DSLInterpreter, program_snapshot, utc_isoformat
from kawasemi.ai_input.labels import STAGES
from kawasemi.ai_input.llm_provider import design_llm_model
from kawasemi.ai_input.metrics import AcquisitionSummary, acquisition_summary
from kawasemi.ai_input.models import (
    AnnotationStatus,
    AssessmentStage,
    ClinicalAnnotation,
    DesignLlmCall,
    DesignRole,
    ExecutionStatus,
    FailureCode,
    InputExecution,
    InputPlan,
    InputRunbook,
    InputTrialRun,
    LocationCandidateAssessment,
    OpType,
    PlanStatus,
    RunbookStatus,
    SourceCategory,
    SystemLocation,
    compute_cand_key,
)
from kawasemi.ai_input.reference_lookup import DEFAULT_LIMIT as DEFAULT_REFERENCE_LIMIT
from kawasemi.ai_input.reference_lookup import MAX_LIMIT as MAX_REFERENCE_LIMIT
from kawasemi.ai_input.reference_lookup import (
    find_reference_units,
    reference_lookup_available,
    reference_page_range,
)
from kawasemi.ai_input.reference_scope import reference_paths_for
from kawasemi.ai_input.resource_drafter import AIResourceDrafter
from kawasemi.ai_input.resource_index.schema import ResourceUnit
from kawasemi.ai_input.resource_index.store import ResourceIndexStore, get_store
from kawasemi.ai_input.runbook import AIRunbookGenerator
from kawasemi.ai_input.schemas import (
    MAX_LOCATION_CANDIDATES,
    RUNBOOK_ACTION_TYPES,
    AutofillRequest,
    ClinicalAnnotationDraftRequest,
    DraftState,
    DSLProgram,
    LocationAssessmentIn,
    PlanCompileRequest,
    PlanDemoteRequest,
    PlanSaveRequest,
    RunbookGenerateRequest,
    RunbookSaveRequest,
    SystemLocationDraftItem,
    SystemLocationDraftRequest,
    SystemLocationSaveRequest,
)
from kawasemi.ai_input.statistical_profile import generate_statistical_profile
from kawasemi.ai_input.target_items import Targets, resolve_targets
from kawasemi.ai_input.viz import program_to_mermaid
from kawasemi.core.authz import user_can_access_patient
from kawasemi.core.config import settings
from kawasemi.core.csrf import stable_csrf_tokens
from kawasemi.core.database import get_db
from kawasemi.core.deps import (
    RequireAutofillRunner,
    RequireDesigner,
    RequireMetricsViewer,
    require_roles,
)
from kawasemi.core.exceptions import AIServiceException, ResourceNotFoundException
from kawasemi.core.lease import extend_leases_for_patient
from kawasemi.core.logging_config import get_logger
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import RoleType
from kawasemi.models.user import User
from kawasemi.utils.file_export import (
    embed_clinical_annotations,
    extract_clinical_annotations,
    is_distributable_citation,
)
from kawasemi.web.form_generator import FormGenerator
from kawasemi.web.template_globals import register_template_globals

router = APIRouter(prefix="/ai-input", tags=["AI入力支援"])

# 実行記録の確認書（PHI を含む）: 実行できる役割（設計者・収集者）と閲覧・検証者に開き、
# 閲覧・検証者以外は自分の実行記録に限る（ハンドラ内で絞る）。管理者は入らない。
RequireValidationViewer = Annotated[
    User, Depends(require_roles(RoleType.DESIGNER, RoleType.COLLECTOR, RoleType.REVIEWER))
]
logger = get_logger(__name__)
templates = Jinja2Templates(directory="kawasemi/web/templates")
register_template_globals(templates)
templates.env.globals["STAGES"] = STAGES


# AI機能ガード（docs/ai_flags_spec.md）。ルーターは常時マウントし、
# LLM関与の種類ごとに端点単位で拒否する。
_LLM_OPS = frozenset({OpType.LLM_EXTRACT, OpType.LLM_JUDGE})


def _drafting_disabled_response() -> JSONResponse:
    """候補探索AI（設計ドラフト生成系）の無効時レスポンス"""
    return JSONResponse(
        {"success": False, "message": "候補探索AIが無効です（ENABLE_AI_DRAFTING=false）"},
        status_code=403,
    )


def _llm_step_ids(program: dict[str, Any]) -> list[str]:
    """program 中の LLM op（llm_extract / llm_judge）step id を列挙する"""
    return [
        str(s.get("id") or s.get("op")) for s in program.get("steps", []) if s.get("op") in _LLM_OPS
    ]


async def _get_questionnaire_or_404(questionnaire_id: int, db: AsyncSession) -> Questionnaire:
    result = await db.execute(
        select(Questionnaire).where(
            Questionnaire.id == questionnaire_id,
            ~Questionnaire.is_deleted,
        )
    )
    q = result.scalar_one_or_none()
    if not q:
        raise ResourceNotFoundException("Questionnaire", questionnaire_id)
    return q


def _editable_fields(questionnaire: Questionnaire) -> list[dict[str, Any]]:
    generator = FormGenerator(questionnaire.content)
    return [
        f for f in generator.generate_form_fields() if f.get("type") not in ("display", "group")
    ]


def _draft_targets(link_id: str | None, link_ids: list[str] | None) -> tuple[str, ...] | None:
    """AI下書きの対象指定（`link_id` / `link_ids`）を1つの並びへ畳む。

    どちらも無ければ `None` を返す＝一括モード（未入力の項目だけが対象）。重複は落とし、
    **与えられた順を保つ**（失敗応答に出す並びに使う。対象項目そのものの順は呼び出し側が
    `_editable_fields` の順で決める）。臨床（役割1）と所在（役割2）で同じ規則を使う。
    """
    seen: list[str] = []
    for raw in [link_id, *(link_ids or [])]:
        s = (raw or "").strip()
        if s and s not in seen:
            seen.append(s)
    return tuple(seen) or None


def _reject_unknown_targets(
    targets: tuple[str, ...] | None, fields: list[dict[str, Any]]
) -> JSONResponse | None:
    """指定された link_id に対象外のものがあれば失敗応答を返す。

    **知らない link_id を黙って捨てない。** 自動生成版を固定する前の埋め直しは
    「指定した項目が全部埋まった」前提で先へ進むので、綴り違いの1件を落とすと**未試行の
    まま自動生成版を固定してしまう**。落ちた事実は成功応答のどこにも出ない（対象が1件減るだけ）。
    """
    if targets is None:
        return None
    known = {str(f["link_id"]) for f in fields}
    unknown = [t for t in targets if t not in known]
    if not unknown:
        return None
    return JSONResponse(
        {
            "success": False,
            "message": (
                "対象にできない項目が指定されています"
                f"（問診票に無い、または記述欄ではない）: {', '.join(unknown)}"
            ),
        },
        status_code=400,
    )


async def _annotation_map(questionnaire_id: int, db: AsyncSession) -> dict[str, ClinicalAnnotation]:
    result = await db.execute(
        select(ClinicalAnnotation)
        .where(ClinicalAnnotation.questionnaire_id == questionnaire_id)
        .order_by(ClinicalAnnotation.id)
    )
    return {a.link_id: a for a in result.scalars().all()}


def _clean_text(value: Any) -> str | None:
    """リクエスト由来の文字列フィールドを正規化する（型不正は破棄して None に落とす）。

    保存APIは生JSONを受けるため、数値・オブジェクト等が来ても 500 にせず無視する。
    """
    return value.strip() or None if isinstance(value, str) else None


def _nonempty_payload(payload: Any) -> dict[str, Any] | None:
    """値が1つでもある payload だけを返す（UI の未入力行・全キーが空の payload は None）。

    **破棄規則を1箇所に閉じる**。`_clean_citation` の中だけに
    書いていたので、`_verified_citation` が許可リストで払い落とした結果の空 payload には
    掛からず、**`{unit_id: None, payload: None}` という空の典拠が DB に書かれた**
    （保存が冪等でなくなり、記述もアドレスも編集していない一括確認で `content_unchanged` が
    偽になる＝他人の確認帰属を奪う）。
    """
    return payload if isinstance(payload, dict) and any(payload.values()) else None


def _clean_citation(raw: dict[str, Any]) -> dict[str, Any] | None:
    """臨床記述の典拠1件を正規化する。

    unit_id（索引側のアドレス）も payload（抜粋・索引メタ）も無い空の典拠は None（破棄）。
    llm_model は AI 由来の来歴なので、人間が unit_id を編集しても保持する
    （_clean_candidate と同じ方針）。
    """
    unit_id = _clean_text(raw.get("unit_id"))
    payload = _nonempty_payload(raw.get("payload"))
    if not unit_id and not payload:
        return None  # 空の典拠は保存しない
    return {
        "unit_id": unit_id,
        "payload": payload,
        "llm_model": _clean_text(raw.get("llm_model")),
    }


# 索引に実在する典拠で、**クライアント由来のまま残してよいキー**（許可リスト）。
#
# **索引の payload に無いキーを無制限に残してはいけない**。
# 索引が持つのは `connection_id / file_role / path / page / char_range / mime` だけなので、
# 「索引が持たないキーは残す」を無条件にすると **`origin` の自己申告が残り**、任意のキーも
# 保存される。`is_distributable_citation` の2つの根拠（`file_role` / `origin`）は**どちらも
# 索引の payload に無いキー**である、というのがこの穴の根である。
_CLIENT_PAYLOAD_KEYS = frozenset({"snippet", "label"})


def _verified_citation(
    citation: dict[str, Any],
    *,
    unit: ResourceUnit | None,
    existing_payloads: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """典拠1件を**索引の実在で裏づける**。

    `unit_id` は UI の自由入力欄から書けるので、形が索引のアドレスに一致していても索引を
    指しているとは限らない（ゼロ幅空白を挟んだ `file:患者<U+200B>山田太郎:0-0` が
    形式検査を通ることが実測された）。**配布可否と典拠の付与率の分子はこの実在を根拠にする**ので、
    保存の時点で決着させる。規則は3つである。

    1. **索引に実在するなら payload を索引から導出する。** 索引の値が必ず勝ち、クライアント由来で
       残すのは許可リストのキーだけ（`snippet` = 人が編集する抜粋 / `label`）。
       **主張のキー（`file_role` / `origin`）は先に落としてから索引をマージする**——索引に実在する
       枝で早期 return していたため `origin` の自己申告が残り、後で索引が差し替わると
       「取り込みの事実」として引き継がれて**索引に無いアドレスが配布物と分子に入る**洗浄が
       成立していた
    2. **照合できなかった `unit_id` が既存レコードにあるなら、その payload をそのまま残す。**
       索引を参照できないときも、索引が差し替わって当該アドレスが消えたときも、
       **記録を壊さない**。消すと `citations` が変わり、記述もアドレスも変えていない保存で
       `content_unchanged` が偽になり、**他人の確認帰属を奪って下流を stale 化する**
       （**AI 由来の典拠も巻き込む**）。
       **新しい主張は認めない**ので fail-closed は保たれる——保存し直しても状態は変わらないだけである
    3. **既存レコードにも無い `unit_id`（新しい主張）は、索引由来だという主張を落とす。**
       典拠そのものは残す（人の入力を消さない）。結果として `is_distributable_citation` が偽になり、
       配布物にも分子にも入らない。**ただし残る値が1つも無ければ破棄する**（`_clean_citation` を
       通った値について同じ規則——許可リストで
       払い落とした結果 `unit_id` も payload も空になった典拠を書くと、次の保存で消えて
       **保存が冪等でなくなる**。この関数を単体で呼ぶと `unit_id` の strip は掛からない
       ——`_normalize_citations` 経由では `_clean_citation` が先に正規化する）

    **「照合できない」と「照合したが無かった」を分けない**のは、どちらでも規則2 が同じ答えを
    返すからである（記録があれば残し、無ければ主張を落とす）。以前は参照不能の
    ときだけ `file_role` を復元する形にしたために**他のキーが落ち、索引の差し替えでは復元枝が
    発火しなかった**——条件を分けたことが穴になった。
    """
    out = dict(citation)
    raw = citation.get("payload")
    raw = raw if isinstance(raw, dict) else {}
    unit_id = citation.get("unit_id")
    key = unit_id.strip() if isinstance(unit_id, str) else ""
    if unit is not None:
        # **主張のキーを先に落とし**、許可リストだけ残して索引をマージする
        payload = {k: v for k, v in raw.items() if k in _CLIENT_PAYLOAD_KEYS}
        out["payload"] = {**payload, **dict(unit.payload)}
        return out
    if key and key in existing_payloads:
        # **記録を壊さない**（新しい主張は認めない＝クライアントの payload は採らない）
        out["payload"] = dict(existing_payloads[key])
        return out
    payload = {k: v for k, v in raw.items() if k in _CLIENT_PAYLOAD_KEYS}
    out["payload"] = _nonempty_payload(payload)
    if not out.get("unit_id") and not out["payload"]:
        # **空になった典拠は破棄する**（`_clean_citation` と同じ規則）。
        # 取り込み由来の典拠は `{page, path, origin}` だけを持つので、利用者が `unit_id` の欄を
        # 空にして保存すると、規則3 が許可リストで payload を払い落として**空の典拠**が残った。
        # 害は規則2 で防いでいるものと同じ
        # （保存が冪等でなくなり、確認帰属と下流の鮮度を壊す。空の典拠も `citations_total` に
        # 数えられて指標が動く）。
        return None
    return out


def _normalize_citations(
    item: dict[str, Any], *, existing: ClinicalAnnotation | None = None
) -> list[dict[str, Any]]:
    """保存リクエストの1項目から典拠配列を組み立てる（不正要素・空要素は破棄）。

    **索引で実在を照合する**。`existing` は同じ項目の既存レコードで、
    **照合できなかった `unit_id` について payload をそのまま残す**ために使う
    （規則の本体は `_verified_citation`。取り込み由来の `origin` はその一例で、
    `file_role` / `path` / `page` / `char_range` / `mime` も同じ規則で残る）。

    **復元するのは `origin` だけではない**。復元するキーを絞った形は、索引の差し替えで
    他のキーが落ちる穴になる。
    """
    raw_list = item.get("citations")
    if not isinstance(raw_list, list):
        return []
    cleaned = [c for c in (_clean_citation(r) for r in raw_list if isinstance(r, dict)) if c]
    if not cleaned:
        return []
    store = get_store()
    # **既存レコードの payload**（`unit_id` → payload）。照合できなかった典拠について
    # **記録をそのまま残す**ために使う。引き継ぐ根拠は
    # **クライアントの申告ではなく DB にある事実**である。
    # 同じ `unit_id` が2件記録されていたら**先勝ち**にする（`store.get_unit` と同じ規則。
    # dict 内包表記の後勝ちだと、緩い方の記録が勝つ組み合わせを作れる）
    existing_payloads: dict[str, dict[str, Any]] = {}
    for c in existing.citation_list if existing else []:
        if (
            isinstance(c, dict)
            and isinstance(c.get("unit_id"), str)
            and isinstance(c.get("payload"), dict)
        ):
            existing_payloads.setdefault(str(c["unit_id"]).strip(), dict(c["payload"]))
    verified = [
        _verified_citation(
            c,
            unit=store.get_unit(c["unit_id"]) if isinstance(c.get("unit_id"), str) else None,
            existing_payloads=existing_payloads,
        )
        for c in cleaned
    ]
    # **空になった典拠は落とす**（規則3 が許可リストで payload を
    # 払い落とすと、`unit_id` の無い典拠は空になる。空の典拠を書くと保存が冪等でなくなる）
    return [c for c in verified if c is not None]


def _citation_keys(citations: list[dict[str, Any]]) -> list[str | None]:
    """典拠の同一性を見るキー列（**並び順を含む**）。

    比べるのは**配布できる典拠だけ**。配布できない手元の典拠（索引に無い紙資料を人が
    書いたものなど）は Extension を往復しないので、比較に入れると読み込みのたびに
    「変わった」と判定されて確認が外れる。

    並びを含めるのは、配布側の並び（＝典拠の提示順）も人が確認した内容の一部だから。
    """
    return [
        c.get("unit_id") for c in citations if isinstance(c, dict) and is_distributable_citation(c)
    ]


def _merge_imported_citations(
    local: list[dict[str, Any]], incoming: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Extension から読んだ典拠を、手元の典拠と突き合わせて取り込む。

    **配布に載らないキー（`snippet` など）を手元から消さない。** 自分が書き出した
    Questionnaire を読み戻したときに、規約文書の抜粋が消えてしまうのを避けるため
    （書き出し→読み込みが手元では実質 no-op になる）。受け取り側の施設には手元の典拠が
    無いので、配布された部分集合がそのまま入る。

    突き合わせは `unit_id`。順序は**配布側**（Extension の並び）に合わせる。

    **配布物で表現できない手元の典拠は残す。** 索引に無い紙資料を人が典拠として書いた場合
    （`unit_id` が索引のアドレス形式でない、`file_role` が reference でない）、その典拠は
    Extension に載らない。載らないものを「配布側に無い＝消された」と読むと、**自分の
    書き出しを読み戻しただけで手元の記録が消える**。
    """
    # 同じ unit_id が手元に複数あることはある（同じチャンクを2回足せる。UI にも
    # `_clean_citation` にも重複排除は無い）。**先勝ち・後勝ちのどちらでも、2件目の
    # 手元固有の値（snippet 等）が黙って消える**ので、出現順に1件ずつ消費する。
    by_unit_id: dict[str, list[dict[str, Any]]] = {}
    for c in local:
        if isinstance(c, dict) and isinstance(c.get("unit_id"), str) and c["unit_id"]:
            by_unit_id.setdefault(c["unit_id"], []).append(c)
    merged: list[dict[str, Any]] = []
    for citation in incoming:
        incoming_unit_id = citation.get("unit_id")
        queue = by_unit_id.get(incoming_unit_id, []) if isinstance(incoming_unit_id, str) else []
        mine = queue.pop(0) if queue else None
        if mine is None:
            merged.append(citation)
            continue
        payload = {**(mine.get("payload") or {}), **(citation.get("payload") or {})}
        merged.append(
            {
                "unit_id": citation.get("unit_id"),
                "payload": payload or None,
                # 配布側に無ければ手元の来歴を残す（AI 由来の記録を落とさない）
                "llm_model": citation.get("llm_model") or mine.get("llm_model"),
            }
        )
    # 配布できなかった手元の典拠を末尾に残す（配布側の「無い」には含まれない）
    merged.extend(c for c in local if isinstance(c, dict) and not is_distributable_citation(c))
    return merged


async def _location_map(questionnaire_id: int, db: AsyncSession) -> dict[str, SystemLocation]:
    result = await db.execute(
        select(SystemLocation)
        .where(SystemLocation.questionnaire_id == questionnaire_id)
        .order_by(SystemLocation.id)
    )
    return {loc.link_id: loc for loc in result.scalars().all()}


def _clean_candidate(raw: dict[str, Any]) -> dict[str, Any] | None:
    """1候補を正規化する。source_category 不正は manual に補正。
    location も種別情報も無い空候補は None（破棄）。

    「該当データなし（not_available）」は人が明示的に決着させた回答なので、location が
    無くても破棄しない（is_resolved=True を保たせ、後段ゲートを通すため）。"""
    category = raw.get("source_category") or SourceCategory.MANUAL
    if category not in SourceCategory.ALL:
        category = SourceCategory.MANUAL
    location = (raw.get("location") or "").strip() or None
    if category == SourceCategory.NOT_AVAILABLE:
        return {  # 取得対象外。所在・索引メタは持たない
            "source_category": SourceCategory.NOT_AVAILABLE,
            "location": None,
            "unit_id": None,
            "connection_id": None,
            "payload": None,
            "llm_model": None,
        }
    if not location and category == SourceCategory.MANUAL:
        return None  # 空候補は保存しない
    payload = raw.get("payload")
    return {
        "source_category": category,
        "location": location,
        "unit_id": raw.get("unit_id") or None,
        "connection_id": raw.get("connection_id") or None,
        "payload": payload if isinstance(payload, dict) else None,
        # 来歴: AI案由来候補の生成モデル名（手入力は null）。開示欄で使う
        "llm_model": (raw.get("llm_model") or "").strip() or None,
    }


REACH_BODY_UNREACHABLE = "body_unreachable"


def _annotate_reach(
    candidates: list[dict[str, Any]], store: ResourceIndexStore
) -> list[dict[str, Any]]:
    """表示用に、候補へ到達可能性の印（`reach`）を足して返す。

    **「所在は特定できるが現在の取得経路では引けない」**（ヘッダはあるが本文が実DBへ
    未連携の文書種別。例: 別システムが持つ病理レポート）を、画面で他の候補と区別できるようにする。落とさない
    のは、所在の特定と到達可能性が別の問題だからで、後者は施設の意思決定に属する。

    **表示だけの印で、保存しない。** `_clean_candidate` は未知のキーを捨てるので、保存・
    `proposed_candidates`・`compute_cand_key` には届かない（測定の記録を動かさない）。
    測っていない候補・本文がある候補には何も足さない。
    """
    out: list[dict[str, Any]] = []
    for c in candidates:
        unit_id = c.get("unit_id")
        reach = store.column_fill.reach_of_id(unit_id) if isinstance(unit_id, str) else None
        if reach is not None and reach.body_unreachable:
            c = {**c, "reach": REACH_BODY_UNREACHABLE}
        out.append(c)
    return out


def _secure_patient_scope(
    candidates: list[dict[str, Any]], store: ResourceIndexStore
) -> list[dict[str, Any]]:
    """候補の `payload.patient_scoped`（「患者で絞らない表」の宣言）を**索引の値だけ**にする。

    宣言は WHERE 句を省く SQL を組ませる。**クライアントの自己申告を採ると、保存権限のある人が
    患者データの表を「患者で絞らない」と偽って、全患者の行を返す SQL を組ませられる**
    （施設の確認を経ない宣言）。`unit_id` が指す索引のユニットが `False` と宣言しているときだけ
    宣言を残し、それ以外（手入力・索引に無い `unit_id`・索引が未ロード）は落とす。
    臨床側の典拠（`_verified_citation`）が `payload` を索引から導出するのと同じ規則である。
    """
    out: list[dict[str, Any]] = []
    for c in candidates:
        payload = c.get("payload")
        if isinstance(payload, dict) and "patient_scoped" in payload:
            payload = {k: v for k, v in payload.items() if k != "patient_scoped"}
            unit_id = c.get("unit_id")
            unit = store.get_unit(unit_id) if isinstance(unit_id, str) else None
            if unit is not None and unit.payload.get("patient_scoped") is False:
                payload["patient_scoped"] = False
            c = {**c, "payload": payload}
        out.append(c)
    return out


def _normalize_candidates(item: dict[str, Any]) -> list[dict[str, Any]]:
    """保存リクエストの1項目から候補配列を組み立てる。

    新形式は item["candidates"] の配列。後方互換のため、candidates が無く
    単一の source_category/location を持つ旧形式も1候補として受理する。
    """
    raw_list = item.get("candidates")
    if not isinstance(raw_list, list):
        raw_list = [
            {
                "source_category": item.get("source_category"),
                "location": item.get("location"),
                "unit_id": item.get("unit_id"),
                "connection_id": item.get("connection_id"),
                "payload": item.get("payload"),
            }
        ]
    cleaned = [c for c in (_clean_candidate(r) for r in raw_list if isinstance(r, dict)) if c]
    return cleaned


async def _runbook_map(questionnaire_id: int, db: AsyncSession) -> dict[str, InputRunbook]:
    result = await db.execute(
        select(InputRunbook)
        .where(InputRunbook.questionnaire_id == questionnaire_id)
        .order_by(InputRunbook.id)
    )
    return {rb.link_id: rb for rb in result.scalars().all()}


async def _plan_map(questionnaire_id: int, db: AsyncSession) -> dict[str, InputPlan]:
    result = await db.execute(
        select(InputPlan)
        .where(InputPlan.questionnaire_id == questionnaire_id)
        .order_by(InputPlan.id)
    )
    return {p.link_id: p for p in result.scalars().all()}


async def _sqlite_write_lock(db: AsyncSession, model: type[InputRunbook] | type[InputPlan]) -> None:
    """SQLite では、読み直しの前に DB の書き込みロックを取る（`FOR UPDATE` の代わり）。

    SQLite は `FOR UPDATE` を無視する。しかもドライバ（sqlite3 / aiosqlite）は SELECT の前に
    BEGIN を出さないので、**読み直しと書き込みは同じトランザクションにならない**。読んで
    版を比べた後、書く前に別の接続が commit できる（「書き込みは DB 全体で直列化される」
    は、読んでから書くまでの間には効かない）。
    ここで行に触れない UPDATE（`WHERE 1 = 0`）を先に流すと、ドライバが BEGIN を出し、
    SQLite が書き込みロック（RESERVED）を取る。以後 commit / rollback までの間、他の接続の
    書き込みは busy_timeout まで待つ（PostgreSQL の行ロックより粗いが、比べてから書くまでを
    押さえる点は同じ）。行に触れないので `updated_at` も変わらない。PostgreSQL では何もしない。
    """
    conn = await db.connection()
    if conn.dialect.name != "sqlite":
        return
    await db.execute(text(f"UPDATE {model.__tablename__} SET id = id WHERE 1 = 0"))


async def _lock_row(db: AsyncSession, row: InputRunbook | InputPlan | None) -> None:
    """保存の読み比べ〜書き込みの間、対象行を行ロックで押さえ、最新の値で読み直す。

    楽観的排他の版比較（`_version_conflict`）と「内容が変わったか」の判定を、他の要求が
    同じ行を書き換えている途中の値で行わないためである。PostgreSQL では `FOR UPDATE`、
    SQLite では `_sqlite_write_lock` で DB の書き込みロックを先に取る。
    """
    if row is None:
        return
    model = type(row)
    await _sqlite_write_lock(db, model)
    await db.execute(
        select(model)
        .where(model.id == row.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )


def _version_token(payload: dict[str, Any]) -> str:
    text = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def plan_version(plan: InputPlan | None) -> str | None:
    """取得プランの版トークン（楽観的排他用）。行が無ければ None。

    `updated_at` ではなく内容から作る。`updated_at` は DB の `now()` で、SQLite では秒
    単位なので同じ秒の2回の保存を区別できない。版に入れるのは、上書きされると困るもの
    すべて（program・status・確認者・確認日時）である。
    """
    if plan is None:
        return None
    return _version_token(
        {
            "program": _program_canonical(plan.program),
            "status": plan.status,
            "confirmed_by_id": plan.confirmed_by_id,
            "confirmed_at": utc_isoformat(plan.confirmed_at),
        }
    )


def runbook_version(rb: InputRunbook | None) -> str | None:
    """手順書の版トークン（`plan_version` と同じ考え方）。"""
    if rb is None:
        return None
    return _version_token(
        {
            "steps": _runbook_canonical(rb.steps),
            "status": rb.status,
            "confirmed_by_id": rb.confirmed_by_id,
            "confirmed_at": utc_isoformat(rb.confirmed_at),
        }
    )


def _version_conflict(
    payload: PlanSaveRequest | PlanDemoteRequest | RunbookSaveRequest,
    current: str | None,
    *,
    required: bool,
) -> JSONResponse | None:
    """送られた版がサーバの現在の版と違えば 409 を返す。

    **確認（confirm=true）と下書きへ戻す操作では版を必須にする**（無ければ 428）。
    どちらも「人が見た内容」に対する判断なので、何を見て判断したかを版で示させる。
    明示の null は「行がまだ無い」の意味で受け付ける。confirm=false の下書き保存だけは
    省略を許し、省略時は検査しない（API・移行スクリプトの互換）。
    """
    if "base_version" not in payload.model_fields_set:
        if not required:
            return None
        return JSONResponse(
            {
                "success": False,
                "message": (
                    "確認・下書きへ戻す操作には base_version（画面で読み込んだ時点の版。"
                    "行がまだ無ければ null）が必要です"
                ),
                "version": current,
            },
            status_code=428,
        )
    if payload.base_version == current:
        return None
    return JSONResponse(
        {
            "success": False,
            "conflict": True,
            "message": (
                "この項目は画面を開いた後に別の操作で更新されています。"
                "上書きしないよう保存を止めました。再読み込みして内容を確かめてください"
            ),
            "version": current,
        },
        status_code=409,
    )


async def _commit_or_conflict(db: AsyncSession) -> JSONResponse | None:
    """commit し、同じ項目の行を同時に新規作成して一意制約に当たったら 409 にする。

    行がまだ無いときは `_lock_row` で押さえる行が無いので、2つの要求がともに「無い」と
    読んで INSERT し得る。後着は一意制約（questionnaire_id, link_id）で落ちる。500 では
    なく、版の競合と同じ 409 で返す。
    """
    try:
        await db.commit()
    except IntegrityError as e:
        await db.rollback()
        if not _is_unique_violation(e):
            raise
        return JSONResponse(
            {
                "success": False,
                "conflict": True,
                "message": (
                    "この項目は同時に別の操作で作成されました。"
                    "上書きしないよう保存を止めました。再読み込みして内容を確かめてください"
                ),
            },
            status_code=409,
        )
    return None


async def _lock_rows_for_write(
    db: AsyncSession,
    model: type[InputRunbook] | type[InputPlan],
    questionnaire_id: int,
    link_ids: list[str],
) -> dict[str, Any]:
    """AI 生成（LLM 呼び出し）の後、書き込む直前に対象行を行ロック付きで読み直す。

    生成の経路は LLM の応答を待つ間（数十秒〜数分）に、別の要求がその行を確認・編集し
    得る。**呼び出し前に読んだオブジェクトへ書いてはいけない**。読んだ時点で draft の行に
    draft と空の確認者を代入しても、ORM は「読んだ値と同じ」と見て UPDATE から外すので、
    `UPDATE ... SET program=?` だけが走り、**他人が確認した行に誰も確認していない生成
    結果が入る**（確認済みのまま）。ここで最新の値を読み直し（`populate_existing`）、
    呼び出し前に控えた版と比べてから書く。比べてから commit までは、PostgreSQL では
    `FOR UPDATE` の行ロック、SQLite では `_sqlite_write_lock` の DB 書き込みロックで押さえる。
    加えて `_upsert_*` は status・確認者・確認日時と内容の列を必ず UPDATE に含める
    （`_force_write_columns`）。
    """
    if not link_ids:
        return {}
    await _sqlite_write_lock(db, model)
    result = await db.execute(
        select(model)
        .where(model.questionnaire_id == questionnaire_id, model.link_id.in_(link_ids))
        .order_by(model.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    rows: list[Any] = list(result.scalars().all())
    return {row.link_id: row for row in rows}


def _generation_conflict_response(current: str | None) -> JSONResponse:
    """単一項目の再生成・再作成で、生成中に行が変わったときの 409。"""
    return JSONResponse(
        {
            "success": False,
            "conflict": True,
            "message": (
                "AI が生成している間に、この項目は別の操作で更新されました（確認・保存など）。"
                "上書きしないよう生成結果を保存しませんでした。再読み込みして内容を確かめてください"
            ),
            "version": current,
        },
        status_code=409,
    )


# 一括生成の書き込みで、同時の新規作成（一意制約）に当たったときに読み直してやり直す回数。
# やり直しでは相手が作った行が見えるので、その項目は版の不一致として skip に回る。
_GENERATION_WRITE_ATTEMPTS = 3


def _force_write_columns(row: InputRunbook | InputPlan) -> None:
    """内容の列（program / steps）と status・確認者・確認日時を、値が変わっていなくても
    UPDATE に含める。

    ORM は読んだ値と同じ値の代入を UPDATE から外す。すると、読んでから書くまでの間に
    別の要求が commit した値のうち、こちらが「変えていない」と見た列はそのまま残る。

    - 生成が draft を書くとき、status を外すと `SET program=?` だけが走り、間に入った
      確認が残って「確認済み＋誰も確認していない内容」になる。
    - 確認が confirmed を書くとき、内容を外すと `SET status=?, confirmed_by_id=?, ...`
      だけが走り、間に入った生成の内容が残って「確認済み（確認者は本人）＋AI の内容」になる。

    どちらの列も必ず書けば、行は**書いた側が見た内容と status の組**になる。ロック
    （`_lock_row` / `_lock_rows_for_write`）が間に入る書き込みを止めるのが本筋で、これは
    ロックが効かない経路が残っても組を崩さないための二重化である。
    """
    content = "program" if isinstance(row, InputPlan) else "steps"
    for key in (content, "status", "confirmed_by_id", "confirmed_at"):
        flag_modified(row, key)


def _is_unique_violation(exc: IntegrityError) -> bool:
    """一意制約違反か（PostgreSQL の SQLSTATE 23505 / SQLite の UNIQUE 制約）。

    同時の新規作成を 409 にするのは一意制約に当たったときだけである。NOT NULL や外部キー
    の違反は競合ではなく不具合なので、409 に変えずにそのまま上げる。
    """
    orig = exc.orig
    for source in (orig, getattr(orig, "__cause__", None)):
        if source is None:
            continue
        code = getattr(source, "pgcode", None) or getattr(source, "sqlstate", None)
        if code == "23505":
            return True
        if getattr(source, "sqlite_errorname", None) in (
            "SQLITE_CONSTRAINT_UNIQUE",
            "SQLITE_CONSTRAINT_PRIMARYKEY",
        ):
            return True
    return "UNIQUE constraint failed" in str(orig)


def _is_designed(ann: ClinicalAnnotation | None, loc: SystemLocation | None) -> bool:
    """臨床的コンテキストかデータ所在のどちらかが入力済みなら手順書の対象とする"""
    has_clinical = bool(ann and ann.annotation)
    has_system = bool(loc and loc.has_content)
    return has_clinical or has_system


def _is_resolved(ann: ClinicalAnnotation | None, loc: SystemLocation | None) -> bool:
    """役割2（データ所在）として人が決着させたか。

    決着 = 臨床的コンテキストあり、または所在を1つ確定（候補選択・手動入力・「該当
    データなし」の明示）。candidates が空のまま（AI候補を出したが未選択など）は未解決。
    後段（手順書生成）はこれが残る間は進めない（空欄の無言流出を防ぐ）。
    """
    has_clinical = bool(ann and ann.annotation)
    has_location_decision = bool(loc and loc.is_resolved)
    return has_clinical or has_location_decision


def _unresolved_link_ids(
    fields: list[dict[str, Any]],
    annotations: dict[str, ClinicalAnnotation],
    locations: dict[str, SystemLocation],
) -> list[str]:
    """未解決（候補も「該当データなし」も未指定）の項目 link_id を返す。"""
    return [
        f["link_id"]
        for f in fields
        if not _is_resolved(annotations.get(f["link_id"]), locations.get(f["link_id"]))
    ]


def _design_updated_at(ann: ClinicalAnnotation | None, loc: SystemLocation | None) -> Any:
    """2役割設計（臨床的コンテキスト／データ所在）の最終更新日時の最大値"""
    times = [
        t
        for t in (
            getattr(ann, "updated_at", None),
            getattr(loc, "updated_at", None),
        )
        if t is not None
    ]
    return max(times) if times else None


def _runbook_stale(rb: InputRunbook | None, design_at: Any) -> bool:
    """手順書が上流（臨床的コンテキスト／データ所在）より古いか。

    設計キャンバスと手順書ウィザード（_build_runbook_items）で共用し、判定の二重実装を避ける。
    """
    return bool(rb and design_at and rb.updated_at and design_at > rb.updated_at)


def _plan_stale(plan: InputPlan | None, rb: InputRunbook | None) -> bool:
    """取得プランが手順書より古いか。

    設計キャンバスとプランウィザード（_build_plan_items）で共用する。
    """
    return bool(
        plan and rb and plan.updated_at and rb.updated_at and rb.updated_at > plan.updated_at
    )


def _collection_status(
    fields: list[dict[str, Any]],
    annotations: dict[str, ClinicalAnnotation],
    locations: dict[str, SystemLocation],
    runbooks: dict[str, InputRunbook],
    plans: dict[str, InputPlan],
) -> dict[str, Any]:
    """Questionnaire のデータ自動収集 設計進捗を判定する。

    バケツ:
      - todo（未着手）: 設計済み（_is_designed）フィールドが 0
      - in_progress（進行中）: 設計済みはあるが全てに InputPlan が揃わない
      - done（完了）: 全ての設計済みフィールドに InputPlan がある
    """
    designed = [
        f
        for f in fields
        if _is_designed(annotations.get(f["link_id"]), locations.get(f["link_id"]))
    ]
    designed_count = len(designed)
    plan_done = sum(1 for f in designed if f["link_id"] in plans)
    runbook_done = sum(1 for f in designed if f["link_id"] in runbooks)

    if designed_count == 0:
        bucket = "todo"
        next_path = "source-survey"
    elif plan_done >= designed_count:
        bucket = "done"
        next_path = "plan"
    else:
        bucket = "in_progress"
        next_path = "runbook" if runbook_done < designed_count else "plan"

    return {
        "bucket": bucket,
        "designed_count": designed_count,
        "total_fields": len(fields),
        "plan_done": plan_done,
        "runbook_done": runbook_done,
        "next_path": next_path,
    }


# ============================================================
# 設計キャンバス（データ自動収集の設計を1画面に集約）
# 行 = 項目(link_id)、列 = 設計ステージ。各セル = データ状態 × 自分の権限。
# 詳細は DESIGN.md §8（セルの状態と権限の表し方は §6・§7）。
#
# NOTE: 各ステージの roles は、対応するエディタルートの require_roles() と一致させること。
#   4段階とも RequireDesigner（kawasemi/core/deps.py）。1様式の設計（臨床的意味→所在→手順書→
#   プラン）は設計者だけで、役割を切り替えずに通る。
#   ルートのガードを変えたらここも更新する（不一致は編集可セルから 403 へ落ちる）。
# ============================================================
_CANVAS_STAGES: list[dict[str, Any]] = [
    {
        "key": "clinical",
        "label": "臨床ｺﾝﾃｷｽﾄ",
        "roles": [RoleType.DESIGNER],
        "path": "source-survey/clinical",
    },
    {
        "key": "system",
        "label": "データ所在",
        "roles": [RoleType.DESIGNER],
        "path": "source-survey/system",
    },
    {"key": "runbook", "label": "手順書", "roles": [RoleType.DESIGNER], "path": "runbook"},
    {
        "key": "plan",
        "label": "取得プラン",
        "roles": [RoleType.DESIGNER],
        "path": "plan",
    },
]


def _stage_capability(stage_roles: list[str], user: User) -> str:
    """アクティブ役割を基準にした、そのステージへの権限。

    - edit: 自分のアクティブ役割が担当 → 編集可
    - switch: 担当役割を保有するが非アクティブ → ロール切替で編集可
    - readonly: 担当外 → 閲覧のみ
    """
    if user.active_role in stage_roles:
        return "edit"
    switchable = set(user.switchable_role_names())
    if switchable & set(stage_roles):
        return "switch"
    return "readonly"


def _canvas_cell_states(
    ann: ClinicalAnnotation | None,
    loc: SystemLocation | None,
    rb: InputRunbook | None,
    plan: InputPlan | None,
) -> dict[str, str]:
    """1項目(link_id)の各ステージのセル状態を {empty|draft|confirmed|stale|wait} で返す。

    状態とゲート/stale は DESIGN.md §6 と routes.py のサーバ側判定に一致させる。
    """
    design_at = _design_updated_at(ann, loc)
    # 手順書の着手可否はサーバ側ゲート（_is_resolved: 臨床 or 所在が決着）に一致させる。
    # _is_designed/_is_resolved は「臨床のみ」も設計済みと見なすため、loc 単独では判定しない。
    resolved = _is_resolved(ann, loc)

    # 1-A 臨床: 記述があれば status（draft / confirmed）に従う。
    # 記述なしは未設定。既存行は移行時に confirmed へ寄せている（人が直接書いた記述）。
    if ann and ann.annotation:
        clinical = "confirmed" if ann.is_confirmed else "draft"
    else:
        clinical = "empty"

    # 1-B データ所在: 候補が選ばれていれば status（draft / confirmed）に従う。
    # **「候補が選ばれている」を confirmed と読まない。** AI 推奨は画面で初期選択されるので、
    # 人が一度も触っていない項目でも `is_resolved` は真になる。ここで両者を混ぜると、
    # 進捗表は「人が確認した項目」を実際より多く見せる（選択済みを確認済みと読む誤り）。
    if loc and loc.is_resolved:
        system = "confirmed" if loc.is_confirmed else "draft"
    else:
        system = "empty"

    # 2 手順書: stale 優先。未作成は上流が決着済なら empty、未決着なら上流待ち
    if rb is not None:
        if _runbook_stale(rb, design_at):
            runbook = "stale"
        elif rb.status == RunbookStatus.CONFIRMED:
            runbook = "confirmed"
        else:
            runbook = "draft"
    elif resolved:
        runbook = "empty"
    else:
        runbook = "wait"

    # 3 取得プラン: 手順書が無いのにプランが残る不整合は stale 扱い（要再検証）。
    # 未作成は手順書確定済なら empty、未確定なら上流待ち
    rb_confirmed = bool(rb and rb.status == RunbookStatus.CONFIRMED)
    if plan is not None:
        if rb is None or _plan_stale(plan, rb):
            plan_state = "stale"
        elif plan.status == PlanStatus.CONFIRMED:
            plan_state = "confirmed"
        else:
            plan_state = "draft"
    elif rb_confirmed:
        plan_state = "empty"
    else:
        plan_state = "wait"

    return {"clinical": clinical, "system": system, "runbook": runbook, "plan": plan_state}


@router.get("/questionnaires/{questionnaire_id}/canvas", response_class=HTMLResponse)
async def design_canvas(
    request: Request,
    questionnaire_id: int,
    current_user: RequireDesigner,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """データ自動収集 設計キャンバス（読取専用の俯瞰ハブ）。

    全項目 × 4設計ステージのマトリクスで、各セルのデータ状態（未設定/ドラフト/
    確認済/要再確認/上流待ち）と、アクティブ役割から見た権限（編集可/閲覧のみ/
    ロール切替で編集）を一望する。編集可セルは該当ステージのエディタへ遷移する。
    """
    questionnaire = await _get_questionnaire_or_404(questionnaire_id, db)
    fields = _editable_fields(questionnaire)
    annotations = await _annotation_map(questionnaire_id, db)
    locations = await _location_map(questionnaire_id, db)
    runbooks = await _runbook_map(questionnaire_id, db)
    plans = await _plan_map(questionnaire_id, db)

    # 列メタ（担当ロール表示・権限）
    stages = [
        {
            "key": s["key"],
            "label": s["label"],
            "path": s["path"],
            "capability": _stage_capability(s["roles"], current_user),
            "owner": " / ".join(RoleType.display_name(r) for r in s["roles"]),
        }
        for s in _CANVAS_STAGES
    ]

    # 行（項目ごとのセル状態＋集中パネル用のコンテンツ概要）と進捗集計
    rows = []
    confirmed_cells = 0
    for field in fields:
        lid = field["link_id"]
        ann = annotations.get(lid)
        loc = locations.get(lid)
        rb = runbooks.get(lid)
        plan = plans.get(lid)
        cells = _canvas_cell_states(ann, loc, rb, plan)
        confirmed_cells += sum(1 for v in cells.values() if v == "confirmed")

        rb_steps = rb.steps if (rb and isinstance(rb.steps, list)) else []
        plan_steps = (
            plan.program.get("steps", []) if (plan and isinstance(plan.program, dict)) else []
        )
        clinical_text = ann.annotation if ann else None
        clinical_summary = (
            (clinical_text[:40] + "…")
            if clinical_text and len(clinical_text) > 40
            else clinical_text
        )
        # 典拠の件数を概要に添える（記述の根拠が示されているかを一覧で見せる）
        citation_count = len(ann.citation_list) if ann else 0
        if clinical_summary and citation_count:
            clinical_summary = f"{clinical_summary}（典拠{citation_count}件）"
        content = {
            "clinical": clinical_summary,
            "system": (
                None
                if not (loc and loc.is_resolved)
                else ("該当データなし" if not loc.has_content else "データ所在を確定済")
            ),
            "runbook": (f"{len(rb_steps)} 手順" if rb else None),
            "plan": (f"{len(plan_steps)} ステップ" if plan else None),
        }
        rows.append(
            {"link_id": lid, "text": field.get("text", ""), "cells": cells, "content": content}
        )

    total_cells = len(fields) * len(_CANVAS_STAGES)

    return templates.TemplateResponse(
        "ai_input/design_canvas.html",
        {
            "request": request,
            "current_user": current_user,
            "questionnaire": questionnaire,
            "current_stage": "canvas",
            "stages": stages,
            "rows": rows,
            "confirmed_cells": confirmed_cells,
            "total_cells": total_cells,
        },
    )


@router.get("/dashboard", response_class=HTMLResponse)
async def collection_dashboard(
    request: Request,
    current_user: RequireDesigner,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """データ自動収集ダッシュボード（設計者のホーム）

    全 Questionnaire を設計進捗で 未着手 / 進行中 / 完了 に分類して表示する。
    各行から該当ステップへ直行できる。
    権限: アクティブ役割が設計者。
    """
    result = await db.execute(
        select(Questionnaire)
        .where(~Questionnaire.is_deleted)
        .order_by(Questionnaire.created_at.desc())
    )
    questionnaires = list(result.scalars().all())

    buckets: dict[str, list[dict[str, Any]]] = {
        "todo": [],
        "in_progress": [],
        "done": [],
    }
    for q in questionnaires:
        fields = _editable_fields(q)
        annotations = await _annotation_map(q.id, db)
        locations = await _location_map(q.id, db)
        runbooks = await _runbook_map(q.id, db)
        plans = await _plan_map(q.id, db)
        status = _collection_status(fields, annotations, locations, runbooks, plans)
        buckets[status["bucket"]].append({"questionnaire": q, "status": status})

    return templates.TemplateResponse(
        "ai_input/dashboard.html",
        {
            "request": request,
            "current_user": current_user,
            "buckets": buckets,
        },
    )


# =============================================================================
# ステップ1: データソース調査 — 統合確認ビュー（読取専用＋編集導線）
# =============================================================================


@router.get("/questionnaires/{questionnaire_id}/source-survey", response_class=HTMLResponse)
async def source_survey_review(
    request: Request,
    questionnaire_id: int,
    current_user: RequireDesigner,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """ステップ1: データソース調査の統合確認ビュー

    項目ごとに臨床的コンテキスト・データ所在・未入力状態を一覧表示する（読取専用）。
    各役割の編集画面へのリンクを持つ（臨床=臨床定義責任者／システム=データ所在管理者）。
    権限: 設計者。
    """
    questionnaire = await _get_questionnaire_or_404(questionnaire_id, db)
    fields = _editable_fields(questionnaire)
    annotations = await _annotation_map(questionnaire_id, db)
    locations = await _location_map(questionnaire_id, db)

    unresolved = set(_unresolved_link_ids(fields, annotations, locations))

    return templates.TemplateResponse(
        "ai_input/design_review.html",
        {
            "request": request,
            "current_user": current_user,
            "questionnaire": questionnaire,
            "current_stage": 1,
            "fields": fields,
            "annotations": annotations,
            "locations": locations,
            "source_labels": SourceCategory.LABELS,
            "annotation_status_labels": AnnotationStatus.LABELS,
            "unresolved": unresolved,
            "unresolved_count": len(unresolved),
        },
    )


# =============================================================================
# ステップ1(役割1): 臨床的コンテキストアノテーション
# =============================================================================


@router.get(
    "/questionnaires/{questionnaire_id}/source-survey/clinical", response_class=HTMLResponse
)
async def clinical_edit(
    request: Request,
    questionnaire_id: int,
    current_user: RequireDesigner,
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """ステップ1(役割1): 臨床的コンテキストアノテーションの編集画面"""
    questionnaire = await _get_questionnaire_or_404(questionnaire_id, db)
    fields = _editable_fields(questionnaire)
    annotations = await _annotation_map(questionnaire_id, db)

    # フロントの初期状態: link_id -> 保存済みの典拠配列と来歴（典拠エディタが復元に使う）。
    # payload / llm_model は編集UIに出さない情報も含むため、そのまま持ち回って保存し直す
    # （人が unit_id を編集してもAI由来の事実を失わない）。
    initial = {
        lid: {
            "citations": a.citation_list,
            "status": a.status,
            "llm_model": a.llm_model,
            # **「探したが該当箇所が無い」と「まだ書いていない」を画面が区別できるようにする**。
            # どちらも記述が空・典拠0件・draft なので、この値が無いと同じに見える。
            "no_citation_reason": a.no_citation_reason,
        }
        for lid, a in annotations.items()
    }

    csrf_token, signed_token = stable_csrf_tokens(request, csrf_protect)
    response = templates.TemplateResponse(
        "ai_input/design_clinical.html",
        {
            "request": request,
            "current_user": current_user,
            "questionnaire": questionnaire,
            "current_stage": 1,
            "fields": fields,
            "annotations": annotations,
            "annotation_status_labels": AnnotationStatus.LABELS,
            "initial": initial,
            "csrf_token": csrf_token,
        },
    )
    if signed_token is not None:
        csrf_protect.set_csrf_cookie(signed_token, response)
    return response


@router.post("/questionnaires/{questionnaire_id}/source-survey/clinical/ai-draft")
async def draft_clinical_annotations(
    request: Request,
    questionnaire_id: int,
    payload: ClinicalAnnotationDraftRequest,
    current_user: RequireDesigner,
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """ステップ1(役割1): 規約文書を典拠とする臨床的意味のAI下書きを作る。

    リソース索引の規約文書（`file_role=reference`）をembedding検索し、Ollama に
    「どのチャンクを典拠にするか（cand_id）」と記述本文を書かせる。**典拠の unit_id と
    出典表記は選ばれたチャンクから決定的に組み立てる**ので、存在しない文書・ページを
    典拠に据えることが構造的にできない。

    保存はしない（フォームへ差し込むだけ）。人が確認して保存し、confirmed へ昇格させる。

    - link_id / link_ids 未指定（一括）: 記述が未入力の項目のみを対象（人の入力は保護）。
    - link_id 指定（項目単位再生成）: その項目を対象（既存入力に依らず引き直す）。
    - link_ids 指定（複数指定）: **その集合だけ**を対象（同上）。自動生成版を固定する前に一部の
      項目が未試行に終わったときに使う。単項目に落とさないことが目的なので、渡す件数は
      2件以上にする（`_CHUNK_SIZE=2` は動かさない＝プロンプトの分割は測定の前提）。
    """
    await csrf_protect.validate_csrf(request)
    if not settings.enable_ai_drafting:
        return _drafting_disabled_response()
    questionnaire = await _get_questionnaire_or_404(questionnaire_id, db)
    model = (payload.model or "").strip() or None
    targets = _draft_targets(payload.link_id, payload.link_ids)
    if payload.link_ids is not None and targets is None:
        # 空の `link_ids` を一括（＝未入力の全項目）へ落とさない。数十コールが黙って走る。
        return JSONResponse(
            {"success": False, "message": "対象の項目が指定されていません"}, status_code=400
        )

    fields = _editable_fields(questionnaire)
    annotations = await _annotation_map(questionnaire_id, db)

    rejected = _reject_unknown_targets(targets, fields)
    if rejected is not None:
        return rejected

    items: list[dict[str, str | None]] = []
    for f in fields:
        link_id = f["link_id"]
        if targets is not None:
            if link_id not in targets:
                continue
        else:
            ann = annotations.get(link_id)
            if ann and ann.annotation:
                continue  # 一括時は未入力欄のみ（人の記述を上書きしない）
        items.append(
            {"link_id": link_id, "item_text": f.get("text"), "parent_text": f.get("parent_text")}
        )

    if not items:
        msg = (
            "対象の項目が見つかりません"
            if targets is not None
            else "AI下書きの対象となる未入力項目がありません"
        )
        return JSONResponse({"success": False, "message": msg}, status_code=400)

    drafter = AIClinicalDrafter()
    recorder = DesignLlmRecorder(DesignRole.CLINICAL)
    try:
        result = await drafter.draft(
            items,
            model=model,
            recorder=recorder,
            # 様式ごとの典拠の範囲（様式と無関係な領域の文書が候補に入らないように）
            reference_paths=reference_paths_for(questionnaire.url),
        )
    except AIServiceException as e:
        # 失敗した呼び出しも来歴に残す。何を渡して何が返らなかったかは、
        # 成功時と同じくらい後から必要になる。
        await save_design_llm_calls_safely(
            db,
            questionnaire_id=questionnaire_id,
            recorder=recorder,
            created_by_id=current_user.id,
        )
        return JSONResponse({"success": False, "message": e.message}, status_code=500)
    await save_design_llm_calls_safely(
        db,
        questionnaire_id=questionnaire_id,
        recorder=recorder,
        created_by_id=current_user.id,
    )

    # 索引が検索不可なら探索そのものを実行していない。成功として返すと
    # 「典拠つき0件 / 典拠なし0件 / 候補なしN件」という自己矛盾した集計を、画面が
    # 緑の成功表示で出してしまう（LLM は1回も呼ばれていないのにモデル名まで出る）。
    if not result.index_searchable:
        return JSONResponse(
            {
                "success": False,
                "index_unsearchable": True,
                "message": (
                    "リソース索引が検索できないため、規約文書を典拠とする下書きを作成"
                    "できません（索引が未構築 / スキーマ非互換 / embedding モデル不一致）。"
                    "索引を再構築してから実行してください"
                ),
            },
            status_code=409,
        )

    if result.reference_scope_empty:
        return JSONResponse(
            {
                "success": False,
                "reference_scope_empty": True,
                "message": (
                    "この様式には典拠にしてよい規約文書が設定されていないため、AI下書きは"
                    "作成しません（別の様式の規約から定義を写さないため）。臨床的意味は"
                    "人が記述してください。典拠を足すときは site/reference_scopes.yaml に登録します"
                    "（保存すれば再起動なしで効きます）"
                ),
            },
            status_code=409,
        )

    drafts = result.items
    # 来歴の刻印: 典拠を差し込んだ生成モデル名を候補へ載せ、保存まで持ち回る
    # （所在側と同じ方式。人が記述を編集してもAI由来の事実は残る）。
    # **LLM を1回も呼んでいなければ None のままにする。** 候補が1件も無い場合に
    # モデル名を返すと、使っていないモデルを「使った」と開示することになる。
    used_model = result.llm_model
    if used_model is not None:
        for d in drafts:
            for c in d.candidates:
                c.llm_model = used_model
            if d.citation is not None:
                d.citation.llm_model = used_model

    # **三状態の判定はサーバ側が持つ**。画面は `state` から表示先を引くだけに
    # する。以前は画面の JS が `citation` / `unattempted` / それ以外を自分で分岐しており、
    # **2つの枝の中身を入れ替えるミューテーションが全テストを通った**。
    #
    # **未試行の項目を「典拠なし」と混ぜない**。全体デッドラインで打ち切られた
    # 項目と、LLM の応答に含まれなかった項目は「規約文書に定義が無い」ではない。混ぜると
    # 「規約文書を引いても意味が決まらなかった項目」へ未試行が入る。
    unattempted = set(result.unattempted_link_ids)
    # **`drafts` と同じ並びのリストで持つ**。`{d.link_id: ...}` の dict で
    # 持つと、同一 `link_id` が2件あるときに**畳み込まれて件数が減り**、しかも各下書きの
    # `state` が「同じ link_id の最後の1件」のものになる。典拠を持つ項目の画面表示が
    # 「典拠なし」に化けるので、「規約文書を引いても意味が決まらなかった項目」の
    # 分子を上振れさせる向きの誤りである。`linkId` が重複しない問診票では
    # 影響を受けないが、手書き・取り込みの問診票では起きる。
    states = [result.state_of(d) for d in drafts]
    with_citation = sum(1 for s in states if s == DraftState.WITH_CITATION)
    no_citation = sum(1 for s in states if s == DraftState.NO_CITATION)
    # **矛盾した応答（同じ項目に複数のエントリ）も別掲する**。最初のエントリを
    # 採っているが、**集計では分子からも「意味が決まらなかった項目」からも外す**。混ぜると典拠の付与率を過小に、
    # 「マニュアルに定義が無い」を過大に見せる。
    duplicated = list(result.duplicated_link_ids)
    logger.info(
        f"臨床的意味AI下書き完了: questionnaire_id={questionnaire_id}, "
        f"典拠つき{with_citation}件, 典拠なし{no_citation}件, "
        f"候補なし{result.no_hit_count}件, 未試行{len(unattempted)}件, "
        f"矛盾した応答{len(duplicated)}件"
    )
    return JSONResponse(
        {
            "success": True,
            # 各下書きに `state`（`DraftState.ALL` の3値）を載せる。
            # **`unattempted_link_ids` との突き合わせを画面にさせない。**
            "drafts": [{**d.model_dump(), "state": s} for d, s in zip(drafts, states, strict=True)],
            "llm_model": used_model,
            "with_citation": with_citation,
            "no_citation": no_citation,
            "no_hit": result.no_hit_count,
            # 未試行は別掲する。**画面はこれを「規約文書に定義が無い」と表示してはいけない。**
            # **並びは対象項目の順**（4経路で揃える。`sorted()` だと画面の並びと
            # 食い違い、しかも「並びを守る」主張がフィクスチャの自然順と一致して無検証になる）
            "unattempted": len(unattempted),
            "unattempted_link_ids": list(result.unattempted_link_ids),
            # **矛盾した応答**（同じ項目に複数のエントリ）。最初のエントリを採ったので下書きは
            # 入っているが、**集計では分子からも「意味が決まらなかった項目」からも外す**
            "duplicated": len(duplicated),
            "duplicated_link_ids": duplicated,
            # **原因を1つだけ名指ししない**。未試行には打ち切りと LLM の応答漏れの
            # 両方が入るので、「制限時間内に処理できませんでした」は応答漏れの項目には事実と違う。
            "message": (
                f"{len(unattempted)}項目は下書きが返りませんでした（未試行）。"
                "もう一度実行すると、未入力の項目だけが対象になります"
                if unattempted
                else None
            ),
        }
    )


@router.get("/reference-units")
async def list_reference_units(
    current_user: RequireDesigner,
    page: int | None = Query(default=None, ge=1, description="規約文書のページ番号"),
    q: str | None = Query(default=None, max_length=200, description="語（部分一致）"),
    limit: int = Query(default=DEFAULT_REFERENCE_LIMIT, ge=1, le=MAX_REFERENCE_LIMIT),
    questionnaire_id: int | None = Query(
        default=None, description="様式。渡すとその様式の典拠の範囲（reference_scopes.yaml）で絞る"
    ),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """ステップ1(役割1): 人が典拠を選ぶための規約文書の索引引き

    **LLM も embedding も呼ばない**（`reference_lookup` の docstring を参照）。この経路が
    モデルを動かすと、人の作業と比較用の自動生成版との独立を運用の注意だけで守ることになる。

    索引は施設に1つだが、**典拠にしてよい文書は様式ごとに違う**（`reference_scope`）。
    `questionnaire_id` を渡すとその範囲で絞る（画面は常に渡す）。権限は臨床的意味の画面と
    同じ `RequireDesigner` に揃える。
    """
    if not reference_lookup_available():
        return JSONResponse(
            {
                "success": False,
                "index_unavailable": True,
                "message": (
                    "リソース索引が読み込めないため、典拠の候補を出せません"
                    "（索引が未構築 / スキーマ非互換）。索引を配置してから実行してください"
                ),
            },
            status_code=409,
        )

    paths = None
    if questionnaire_id is not None:
        questionnaire = await _get_questionnaire_or_404(questionnaire_id, db)
        paths = reference_paths_for(questionnaire.url)
    hits, total = find_reference_units(page=page, query=q, limit=limit, reference_paths=paths)
    page_range = reference_page_range(reference_paths=paths)
    return JSONResponse(
        {
            "success": True,
            "units": [hit.as_dict() for hit in hits],
            # **絞る前の総数**。上限で切ったことが画面に出ないと、人は「マニュアルに
            # 該当箇所が無い」と読み違える
            "total": total,
            "shown": len(hits),
            "page_range": list(page_range) if page_range is not None else None,
            # **範囲が空で0件なのか、条件に合わずに0件なのかを分ける。** 混ぜると人は
            # 「マニュアルに該当箇所が無い」と読み違える
            "reference_scope_empty": paths is not None and not paths,
        }
    )


async def _duplicated_link_ids(db: AsyncSession, questionnaire_id: int) -> set[str]:
    """その問診票の下書きで**矛盾した応答**だった link_id。

    設計ロールの来歴（`DesignLlmCall.duplicated_link_ids`）から引く。**サーバ側の記録**
    なので、クライアントが送ってこなくても・送ってきても結果は変わらない。

    **全ての臨床的意味の呼び出しを見る**（最新1件ではない）。分割呼び出し
    （`chat_json_chunked`）では重複がどのチャンクで起きたか分からず、また下書きを
    2回に分けて作った項目もあるためである。
    """
    rows = await db.execute(
        select(DesignLlmCall.duplicated_link_ids).where(
            DesignLlmCall.questionnaire_id == questionnaire_id,
            DesignLlmCall.role == DesignRole.CLINICAL,
        )
    )
    out: set[str] = set()
    for (link_ids,) in rows.all():
        if isinstance(link_ids, list):
            out.update(str(x) for x in link_ids)
    return out


@router.post("/questionnaires/{questionnaire_id}/source-survey/clinical")
async def save_clinical(
    request: Request,
    questionnaire_id: int,
    current_user: RequireDesigner,
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
    confirm: bool = False,
) -> JSONResponse:
    """ステップ1(役割1): 臨床的コンテキストアノテーションを保存する（upsert）

    典拠（citations）と来歴（llm_model / status / confirmed_by_id / confirmed_at）も
    併せて保存する。リクエストに含まれる link_id だけを対象とするので、
    画面は1項目だけを送って「その項目を確認」させることもできる。

    `confirm=true` のときは保存と同時に status を confirmed へ昇格し、確認操作者・
    日時を記録する（他3成果物と同じ確定ワークフロー）。記述が空の項目は確定できない
    （確認する内容が無いため draft のまま）。

    確認の帰属（ALCOA+）は「その内容を確認した人」を指す。したがって、
    - 既に confirmed で内容（記述・典拠）も変わらない項目は、confirm 付きでも元の
      確認者・確認日時を保持する（一括確認で他人の確認記録を奪わない）
    - 内容が変わった、または未確認だった項目のみ、操作者を確認者として記録する
    - 記述を空にした項目は draft へ戻し帰属をクリアする

    typo による典拠の消失を防ぐため、citations は request が当該キーを持つ場合のみ
    更新する（llm_model も同様に、明示された場合のみ更新して来歴を保持する）。
    `no_citation_reason`（人が「規約文書に該当箇所が無い」と判断した記録）も
    同じ規則で、**キーが明示された場合のみ**更新する。
    """
    await csrf_protect.validate_csrf(request)
    await _get_questionnaire_or_404(questionnaire_id, db)

    body = await request.json()
    raw_items = body.get("items", [])

    deduped: dict[str, dict[str, Any]] = {}
    for item in raw_items:
        lid = _clean_text(item.get("link_id")) if isinstance(item, dict) else None
        if lid:
            deduped[lid] = item

    existing = await _annotation_map(questionnaire_id, db)
    now = datetime.now(UTC)
    saved = 0
    confirmed = 0
    # **矛盾した応答（同じ項目に複数エントリ）を項目へ紐づける**。
    # **クライアントの申告ではなくサーバ側の記録から引く**（`origin` の自己申告と
    # 同じ理由）。下書きを作った時点で `DesignLlmCall.duplicated_link_ids` に残してある。
    duplicated_links = await _duplicated_link_ids(db, questionnaire_id)

    for link_id, item in deduped.items():
        annotation = _clean_text(item.get("annotation"))
        row = existing.get(link_id)
        # 典拠・来歴は request が明示した場合のみ更新し、それ以外は既存値を保持する
        has_citations_key = isinstance(item.get("citations"), list)
        citations = (
            _normalize_citations(item, existing=row)
            if has_citations_key
            else (row.citation_list if row else [])
        )
        llm_model = _clean_text(item.get("llm_model"))
        # **キーが明示された場合のみ更新する**（`citations` / `llm_model` と同じ規則。
        # 送ってこない画面や項目単位保存の typo で人の記録が消えないようにする）。
        no_citation_reason = (
            _clean_text(item.get("no_citation_reason"))
            if "no_citation_reason" in item
            else (row.no_citation_reason if row else None)
        )

        content_unchanged = bool(
            row and row.annotation == annotation and row.citation_list == citations
        )
        if annotation and row and row.status == AnnotationStatus.CONFIRMED and content_unchanged:
            # 確認済みで内容も変わらない項目は元の確認者・確認日時をそのまま尊重する
            # （confirm 付き一括保存でも他人の確認記録を上書きしない）
            status = AnnotationStatus.CONFIRMED
            confirmed_by_id: int | None = row.confirmed_by_id
            confirmed_at: datetime | None = row.confirmed_at
        elif confirm and annotation:
            status = AnnotationStatus.CONFIRMED
            confirmed_by_id = current_user.id
            confirmed_at = now
            confirmed += 1
        elif annotation and row and row.status == AnnotationStatus.CONFIRMED:
            # 確認済みを維持する編集（confirm なし）は元の確認者・確認日時を保持する
            status = AnnotationStatus.CONFIRMED
            confirmed_by_id = row.confirmed_by_id
            confirmed_at = row.confirmed_at
        else:
            # 記述を空にした場合も含め、未確認へ戻して帰属をクリアする
            status = AnnotationStatus.DRAFT
            confirmed_by_id = None
            confirmed_at = None

        if row:
            # 同値の再代入でも UPDATE が飛び updated_at が進む（= 下流の手順書が一斉に
            # stale 化する）ため、実際に変わった属性だけを代入する。
            new_values: dict[str, Any] = {
                "item_text": _clean_text(item.get("item_text")),
                "annotation": annotation,
                "citations": citations,
                "status": status,
                "confirmed_by_id": confirmed_by_id,
                "confirmed_at": confirmed_at,
                # 別掲用の事実。**一度立ったら下げない**——その下書きが矛盾した
                # 応答由来だったことは、人が記述を直しても変わらない事実である。
                "duplicated_response": row.duplicated_response or (link_id in duplicated_links),
                "no_citation_reason": no_citation_reason,
            }
            if llm_model is not None:
                new_values["llm_model"] = llm_model
            for attr, value in new_values.items():
                if getattr(row, attr) != value:
                    setattr(row, attr, value)
        else:
            db.add(
                ClinicalAnnotation(
                    questionnaire_id=questionnaire_id,
                    link_id=link_id,
                    duplicated_response=link_id in duplicated_links,
                    item_text=_clean_text(item.get("item_text")),
                    annotation=annotation,
                    citations=citations,
                    status=status,
                    llm_model=llm_model,
                    no_citation_reason=no_citation_reason,
                    confirmed_by_id=confirmed_by_id,
                    confirmed_at=confirmed_at,
                )
            )
        saved += 1

    await db.commit()
    logger.info(
        f"臨床的コンテキスト保存完了: questionnaire_id={questionnaire_id}, {saved}件"
        f"（確認済みへ新たに昇格 {confirmed}件）"
    )
    return JSONResponse({"success": True, "saved": saved, "confirmed": confirmed})


@router.get("/questionnaires/{questionnaire_id}/source-survey/clinical/citation-coverage")
async def export_citation_coverage(
    questionnaire_id: int,
    current_user: RequireDesigner,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """典拠の付与率の分母・分子を出す。

    **分母は対象項目**、**分子は索引に紐づく典拠（`is_distributable_citation`）を1件以上持つ、
    記述のある項目**。定義と理由は `kawasemi/ai_input/citation_metrics.py` の docstring を参照。
    **測っているのは「索引に紐づく来歴」であって「人が原典に到達できるか」ではない。**
    人手入力の典拠は **保存時に索引照合するので分子に入る**が、
    それは **照合を入れた後に新しく書かれた典拠**についてである。既存レコードは遡及照合されない。
    **洗浄されるかはアドレスで決まる**——索引に実在するアドレスの記録は保存し直せば規則1 が
    発火して payload を索引から導出するが（索引を差し替えた窓では残る）、**索引に無い
    アドレスの記録は規則2 が payload をそのまま残すので洗浄されない**（他人の確認帰属を
    奪わないための規則で、意図した挙動である）。
    **したがって古い記録を含む項目で AI と人手を比較してはならない**——後者が残るうえ、
    前者が洗浄されるかは**どの項目を保存し直したか**に依存するので分子が測定時点で動く。

    ここは DB から読むだけで、**分子・分母の判定は一切しない**（判定規則は
    `citation_coverage` に置く。ここに絞り込みを足すと分子が黙って変わる）。

    永続化された `clinical_annotations.citations` だけから計算するので、いつ測っても同じ値が
    出る（AI下書きの実行順序に依存しない）。

    **索引を差し替えると、この値はコードも文書も変わらないまま動く**。規約文書を索引して
    いなければ分子は 0 になり、`schema_version` が非互換の索引では検索そのものが無効になる。
    値を記録に残すときはその時点の `build_info.json` を併記する。
    """
    questionnaire = await _get_questionnaire_or_404(questionnaire_id, db)
    targets = resolve_targets(questionnaire.url, _editable_fields(questionnaire))
    annotations = await _annotation_map(questionnaire_id, db)
    return JSONResponse(
        citation_coverage(
            targets.link_ids,
            {
                link_id: {"annotation": a.annotation, "citations": a.citation_list}
                for link_id, a in annotations.items()
            },
            expected_target_items=targets.expected,
        )
    )


@router.post("/questionnaires/{questionnaire_id}/source-survey/clinical/export-extension")
async def export_clinical_extension(
    request: Request,
    questionnaire_id: int,
    current_user: RequireDesigner,
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """臨床的コンテキストを Questionnaire の FHIR Extension へ書き戻す

    clinical_annotations（正）の内容を Questionnaire.content の各 item に
    clinical-annotation Extension として埋め込み、保存する。
    """
    await csrf_protect.validate_csrf(request)
    questionnaire = await _get_questionnaire_or_404(questionnaire_id, db)

    annotations = await _annotation_map(questionnaire_id, db)
    annotations_map = {link_id: a.annotation for link_id, a in annotations.items() if a.annotation}
    # 典拠も同じ Extension へ載せる。載せるのは許可キーだけで、規約文書の本文
    # （payload.snippet）と施設のインフラ識別子（connection_id）は `file_export` 側で落ちる。
    citations_map = {
        link_id: a.citation_list
        for link_id, a in annotations.items()
        if a.annotation and a.citation_list
    }

    questionnaire.content, embedded_citations = embed_clinical_annotations(
        questionnaire.content, annotations_map, citations_map
    )
    # 配布できない典拠（索引に無い手書き・patient_data 由来）は落ちるので、**実際に
    # 埋め込んだ件数**を返す。手元の件数を報告すると監査ログが実態とずれる。
    not_distributable = sum(len(v) for v in citations_map.values()) - embedded_citations
    await db.commit()
    logger.info(
        f"臨床的コンテキストをExtensionへ書き戻し: questionnaire_id={questionnaire_id}, "
        f"{len(annotations_map)}件（典拠 {embedded_citations}件"
        f"／配布できず除外 {not_distributable}件）"
    )
    return JSONResponse(
        {
            "success": True,
            "embedded": len(annotations_map),
            "embedded_citations": embedded_citations,
            # 人に見せるための数（索引に無い手書き典拠などは配布物に載せられない）
            "not_distributable_citations": not_distributable,
        }
    )


@router.post("/questionnaires/{questionnaire_id}/source-survey/clinical/import-extension")
async def import_clinical_extension(
    request: Request,
    questionnaire_id: int,
    current_user: RequireDesigner,
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """Questionnaire の FHIR Extension から臨床的コンテキストを読み込む（上書き）

    Questionnaire.content の clinical-annotation Extension を抽出し、
    clinical_annotations へ upsert する。
    """
    await csrf_protect.validate_csrf(request)
    questionnaire = await _get_questionnaire_or_404(questionnaire_id, db)

    extracted = extract_clinical_annotations(questionnaire.content)
    fields = _editable_fields(questionnaire)
    text_map = {f.get("link_id"): f.get("text") for f in fields}

    existing = await _annotation_map(questionnaire_id, db)
    imported = 0
    imported_citations = 0
    for link_id, annotation in extracted.annotations.items():
        row = existing.get(link_id)
        # None = 旧形式（典拠を運ばない）。この場合は手元の典拠に触らない。
        incoming = extracted.citations_for(link_id)
        if row:
            merged = (
                row.citation_list
                if incoming is None
                else _merge_imported_citations(row.citation_list, incoming)
            )
            # 外部（Extension）からの上書きで**記述または典拠（並びを含む）が変わった**なら、
            # 以前の人間確認はその内容に対するものではなくなるため未確認へ戻す（帰属もクリア）。
            # 典拠は確認の材料そのもので、提示順も人が見た内容の一部なので、記述と同じ扱いに
            # する。
            if row.annotation != annotation or _citation_keys(row.citation_list) != _citation_keys(
                merged
            ):
                row.status = AnnotationStatus.DRAFT
                row.confirmed_by_id = None
                row.confirmed_at = None
            row.annotation = annotation
            row.citations = merged
            if not row.item_text:
                row.item_text = text_map.get(link_id)
        else:
            db.add(
                ClinicalAnnotation(
                    questionnaire_id=questionnaire_id,
                    link_id=link_id,
                    item_text=text_map.get(link_id),
                    annotation=annotation,
                    citations=incoming or [],
                )
            )
        imported += 1
        imported_citations += len(incoming or [])

    await db.commit()
    logger.info(
        f"臨床的コンテキストをExtensionから読込: questionnaire_id={questionnaire_id}, "
        f"{imported}件（典拠 {imported_citations}件）"
    )
    return JSONResponse(
        {"success": True, "imported": imported, "imported_citations": imported_citations}
    )


# =============================================================================
# ステップ1(役割2): データ所在
# =============================================================================


@router.get("/questionnaires/{questionnaire_id}/source-survey/system", response_class=HTMLResponse)
async def system_edit(
    request: Request,
    questionnaire_id: int,
    current_user: RequireDesigner,
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """ステップ1(役割2): データ所在の編集画面"""
    questionnaire = await _get_questionnaire_or_404(questionnaire_id, db)
    fields = _editable_fields(questionnaire)
    locations = await _location_map(questionnaire_id, db)

    # フロントの初期状態（top-k選択UIが復元に使う）。
    # **提示された候補（`proposed`）も返す**。以前は保存済みの決着1件しか
    # 返していなかったので、画面を読み込み直すと残りの候補が消えて選び直せなかった。
    # `confirmed` は人が確認したかで、候補が選ばれているか（`is_resolved`）とは別である
    # （AI 推奨は初期選択されるので、選択済み＝人が見た、ではない）。
    reach_store = get_store()
    initial = {
        lid: {
            "candidates": _annotate_reach(loc.candidate_list, reach_store),
            "proposed": _annotate_reach(loc.proposed_candidate_list, reach_store),
            "confirmed": loc.is_confirmed,
        }
        for lid, loc in locations.items()
    }
    # 手動入力ドロップダウンの選択肢（「該当データなし」は専用の選択肢として別扱い）
    manual_categories = [c for c in SourceCategory.ALL if c != SourceCategory.NOT_AVAILABLE]

    csrf_token, signed_token = stable_csrf_tokens(request, csrf_protect)
    response = templates.TemplateResponse(
        "ai_input/design_system.html",
        {
            "request": request,
            "current_user": current_user,
            "questionnaire": questionnaire,
            "current_stage": 1,
            "fields": fields,
            "locations": locations,
            "initial": initial,
            "manual_categories": manual_categories,
            "source_labels": SourceCategory.LABELS,
            # 候補件数の上限を画面へ渡す。画面側で別に持つと、上限を
            # 上げたときに「押せるのに 422 になる」画面が残る。
            "max_location_candidates": MAX_LOCATION_CANDIDATES,
            "csrf_token": csrf_token,
        },
    )
    if signed_token is not None:
        csrf_protect.set_csrf_cookie(signed_token, response)
    return response


@router.post("/questionnaires/{questionnaire_id}/source-survey/system")
async def save_system(
    request: Request,
    questionnaire_id: int,
    payload: SystemLocationSaveRequest,
    current_user: RequireDesigner,
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
    confirm: bool = False,
) -> JSONResponse:
    """ステップ1(役割2): データ所在を保存する（upsert）

    **リクエストは `SystemLocationSaveRequest` の検査を通す**。以前は
    `request.json()` を直読みしていたので、`designer` 権限と CSRF トークンがあれば
    API 直叩きで任意件数・任意の形の候補が入った。UI が候補を1件しか送らないことは
    サーバ側の保証ではない——フォールバック取得計画は `candidates` の長さと
    各候補の payload を読んで DSL を生成するので、受け口で決着させる。

    値の正規化（未知の種別を `manual` へ落とす、空候補を捨てる、旧形式を1候補として
    読む）は従来どおり `_normalize_candidates` / `_clean_candidate` が行う。スキーマが
    見るのは**型と件数**だけで、両者の役割は重ならない。

    `confirm=true` のときは保存と同時に status を confirmed へ昇格し、確認操作者・日時を
    記録する（他4成果物と同じ確定ワークフロー）。**リクエストに含まれる
    link_id だけを対象とする**ので、画面は1項目だけを送って「その項目を確認」させられる。
    一括保存しか無いと `confirmed_at` が全項目で揃い、1項目あたりの工数
    が後から復元できない。候補が空の項目は確定できない（確認する内容が無いため draft）。

    確認の帰属（ALCOA+）は「その内容を確認した人」を指す。したがって、
    - 既に confirmed で候補も変わらない項目は、confirm 付きでも元の確認者・日時を保つ
      （一括確認で他人の確認記録を奪わない）
    - confirm 付きで候補のある項目のみ、操作者を確認者として記録する
    - 候補を空にした項目も、**別の候補へ選び直した項目も** draft へ戻し帰属をクリアする
      （`save_clinical` との違いは下の分岐のコメントを参照）

    **`proposed_candidates`（AI が提示した候補）はここでは触らない。** 提示したのは
    サーバなので `draft_system_locations` が書く。人が選び直しても「何が提示
    されたか」は変わらない事実であり、保存経路で往復させると画面側の不具合が
    AI 提案の適合率の分母と、AI が提案しなかった所在の数え方を動かす。
    """
    await csrf_protect.validate_csrf(request)
    await _get_questionnaire_or_404(questionnaire_id, db)

    deduped: dict[str, dict[str, Any]] = {}
    for entry in payload.items:
        lid = entry.link_id.strip()
        if lid:
            deduped[lid] = entry.model_dump()

    existing = await _location_map(questionnaire_id, db)
    now = datetime.now(UTC)
    saved = 0
    confirmed = 0
    store = get_store()
    for link_id, item in deduped.items():
        # 「患者で絞らない表」の宣言は、クライアントの自己申告を採らず索引の値だけにする
        candidates = _secure_patient_scope(_normalize_candidates(item), store)
        row = existing.get(link_id)
        content_unchanged = bool(row and row.candidate_list == candidates)

        if candidates and row and row.is_confirmed and content_unchanged:
            # 確認済みで候補も変わらない項目は元の確認者・日時をそのまま尊重する
            # （confirm 付き一括保存でも他人の確認記録を上書きしない）
            status = AnnotationStatus.CONFIRMED
            confirmed_by_id: int | None = row.confirmed_by_id
            confirmed_at: datetime | None = row.confirmed_at
        elif confirm and candidates:
            status = AnnotationStatus.CONFIRMED
            confirmed_by_id = current_user.id
            confirmed_at = now
            confirmed += 1
        else:
            # 候補を空にした場合も、**選び直した場合も**未確認へ戻して帰属をクリアする。
            #
            # **ここは `save_clinical` と分岐が1つ違う。** 臨床側には「確認済みの項目を
            # confirm なしで編集したら確認を維持する」分岐がある（記述の推敲で確認が
            # 剥がれないようにするため）。所在にそれを置くと、**別の所在を選び直した行が
            # 確認済みのまま残る**。確認は「この所在でよい」という特定の候補に対する行為で、
            # 候補が変われば対象そのものが変わる（`compute_cand_key` が候補の同一性を
            # 内容から導いているのと同じ考え方であり、所在の確からしさの判定が
            # 「提示した所在」に対して行われることとも揃う）。
            #
            # 一括保存で既存の確認が剥がれることはない——選択が変わっていない項目は
            # 上の `content_unchanged` の枝に入る。
            status = AnnotationStatus.DRAFT
            confirmed_by_id = None
            confirmed_at = None

        if row:
            row.item_text = item.get("item_text")
            row.candidates = candidates
            row.status = status
            row.confirmed_by_id = confirmed_by_id
            row.confirmed_at = confirmed_at
            row.updated_by_id = current_user.id
        else:
            db.add(
                SystemLocation(
                    questionnaire_id=questionnaire_id,
                    link_id=link_id,
                    item_text=item.get("item_text"),
                    candidates=candidates,
                    status=status,
                    confirmed_by_id=confirmed_by_id,
                    confirmed_at=confirmed_at,
                    updated_by_id=current_user.id,
                )
            )
        saved += 1

    await db.commit()
    logger.info(
        f"データ所在保存完了: questionnaire_id={questionnaire_id}, {saved}件"
        f"（確認済みへ新たに昇格 {confirmed}件）"
    )
    return JSONResponse({"success": True, "saved": saved, "confirmed": confirmed})


async def _record_proposed_candidates(
    db: AsyncSession,
    questionnaire_id: int,
    drafts: list[SystemLocationDraftItem],
    locations: dict[str, SystemLocation],
    item_texts: dict[str | None, str | None],
) -> None:
    """AI が提示した候補の全件を `SystemLocation.proposed_candidates` へ残す。

    **決着（`candidates`）には触らない。** 提示は AI の出力、決着は人の判断であり、
    別の列に分けてある。この関数が書くのは提示の側だけである。

    候補0件の項目も**空配列で残す**——「まだ AI 案を出していない」と「出したが候補が
    0件だった」は別の状態で、後者は集計の分母に入る（前者は入らない）。列が null の
    ままだと両者が区別できない。

    行が無ければ作る。所在が未決着の行ができるが、`is_resolved` は `candidates` の有無で
    決まるので後段のゲート（手順書生成の着手可否）は動かない。

    **`updated_by_id` は更新しない。** AI 案の提示は人の保存操作ではないうえ、誰が AI 案を
    走らせたかは `ai_input_design_llm_calls.created_by_id` に既に残っている。ここへも書くと
    同じ値が2箇所に載り、片方は「人が決めた所在の帰属」、もう片方は「人が実行した AI の
    帰属」という**別の意味の列に同じ値が入る**。職務分離の判定が後者を前者と読む
    余地ができるので、この列は人の保存でだけ動かす。

    **提示が入れ替わった項目は未確認へ戻す**。確認は「この提示を見て、この
    所在でよいと判断した」記録なので、提示が変われば対象が変わる。`save_system` が
    「別の候補へ選び直したら剥がれる」としているのと同じ規則を、引き直しの経路にも適用
    する。**提示が1件も変わっていなければ剥がさない**——確認が指している提示は同じなので、
    押し直しただけで人の記録を捨てる理由が無い。

    一括の AI 案（`link_id` / `link_ids` 未指定）は所在が未入力の項目だけを対象にするので、
    確認済みの項目はそもそも来ない。ここへ来るのは項目単位の引き直しであり、その項目を
    名指しで引き直すという意図的な操作である。
    """
    for d in drafts:
        proposed = [c.model_dump() for c in d.candidates]
        row = locations.get(d.link_id)
        if row is not None:
            if row.proposed_candidate_list != proposed and row.is_confirmed:
                row.status = AnnotationStatus.DRAFT
                row.confirmed_by_id = None
                row.confirmed_at = None
            row.proposed_candidates = proposed
        else:
            row = SystemLocation(
                questionnaire_id=questionnaire_id,
                link_id=d.link_id,
                item_text=item_texts.get(d.link_id),
                candidates=[],
                proposed_candidates=proposed,
            )
            db.add(row)
            locations[d.link_id] = row
    await db.commit()


@router.post("/questionnaires/{questionnaire_id}/source-survey/system/ai-draft")
async def draft_system_locations(
    request: Request,
    questionnaire_id: int,
    payload: SystemLocationDraftRequest,
    current_user: RequireDesigner,
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """ステップ1(役割2): データ所在のAI案を作成する。

    リソース索引(db/file/fhir)をembedding検索して候補を絞り、Ollamaに選定させて
    {source_category, location} の案をフォームへ差し込む（保存はしない。人が確認して保存）。
    索引未構築時はDB定義書BM25へフォールバックする。

    - link_id / link_ids 未指定（一括）: location 未入力の項目のみを対象（人の入力は保護）。
    - link_id 指定（項目単位再生成）: その項目を対象（既存入力に依らず引き直す）。
    - link_ids 指定（複数指定）: **その集合だけ**を対象（同上）。
    """
    await csrf_protect.validate_csrf(request)
    if not settings.enable_ai_drafting:
        return _drafting_disabled_response()
    questionnaire = await _get_questionnaire_or_404(questionnaire_id, db)
    model = (payload.model or "").strip() or None
    targets = _draft_targets(payload.link_id, payload.link_ids)
    if payload.link_ids is not None and targets is None:
        return JSONResponse(
            {"success": False, "message": "対象の項目が指定されていません"}, status_code=400
        )

    fields = _editable_fields(questionnaire)
    fields_text = {f.get("link_id"): f.get("text") for f in fields}
    annotations = await _annotation_map(questionnaire_id, db)
    locations = await _location_map(questionnaire_id, db)

    rejected = _reject_unknown_targets(targets, fields)
    if rejected is not None:
        return rejected

    def _is_empty_location(link_id: str) -> bool:
        loc = locations.get(link_id)
        return loc is None or not loc.has_content

    items: list[dict[str, Any]] = []
    for f in fields:
        link_id = f["link_id"]
        if targets is not None:
            if link_id not in targets:
                continue
        elif not _is_empty_location(link_id):
            continue  # 一括時は未入力欄のみ（人の入力を上書きしない）
        ann = annotations.get(link_id)
        items.append(
            {
                "link_id": link_id,
                "item_text": fields_text.get(link_id),
                "parent_text": f.get("parent_text"),
                "annotation": ann.annotation if ann else None,
            }
        )

    if not items:
        msg = (
            "対象の項目が見つかりません"
            if targets is not None
            else "AI案の対象となる未入力項目がありません"
        )
        return JSONResponse({"success": False, "message": msg}, status_code=400)

    drafter = AIResourceDrafter()
    try:
        recorder = DesignLlmRecorder(DesignRole.SYSTEM)
        result = await drafter.draft(items, model=model, recorder=recorder)
    except AIServiceException as e:
        # 失敗した呼び出しも来歴に残す。何を渡して何が返らなかったかは、
        # 成功時と同じくらい後から必要になる。
        await save_design_llm_calls_safely(
            db,
            questionnaire_id=questionnaire_id,
            recorder=recorder,
            created_by_id=current_user.id,
        )
        return JSONResponse({"success": False, "message": e.message}, status_code=500)
    await save_design_llm_calls_safely(
        db,
        questionnaire_id=questionnaire_id,
        recorder=recorder,
        created_by_id=current_user.id,
    )

    drafts = result.items
    # 来歴の刻印: AI案で提示する各候補に生成モデル名を載せ、選択→保存まで持ち回る。
    # 候補はすべてAI（索引/BM25検索＋LLM一推し）が提示したものなので、人がどれを選んでも
    # 「AI支援由来」の事実は残す。手入力に切り替えた候補のみ保存時に null になる。
    # **LLM を1回も呼んでいなければ None のままにする**。以前はここで
    # `model or settings.ollama_model` を組み立てていたので、**候補が0件で LLM を1回も
    # 呼んでいない実行でもモデル名を返していた**（臨床側で直したのと同型の誤り。
    # 使っていないモデルを「使った」と開示することになる）。
    used_model = result.llm_model
    if used_model is not None:
        for d in drafts:
            for c in d.candidates:
                if c.location:
                    c.llm_model = used_model

    # **提示した候補をサーバ側で残す**。以前は候補がブラウザの JS 状態に
    # しか無く、保存すると選ばれた1件だけが `candidates` へ入って残り7件は消えていた。
    # AI 提案の適合率と、AI が提案しなかった所在の数は、どちらも「AI が提案した候補」と
    # 「人が選んだもの」の関係を見る指標なので、
    # 提示の側が消えると分母が作れない。
    #
    # **クライアントの申告ではなくここで書く**（`origin` や矛盾した応答の記録と同じ理由）。提示したのは
    # サーバであり、往復させると画面の不具合や改変が分母を動かす。人が保存しないまま
    # 画面を読み込み直しても候補が戻るので、生成のやり直しも要らなくなる。
    #
    # 決着（`candidates`）は触らない。`updated_by_id` も更新しない——この書き込みは
    # 人の保存操作ではないので、職務分離の判定に混ぜてはいけない。
    await _record_proposed_candidates(db, questionnaire_id, drafts, locations, fields_text)

    filled = sum(1 for d in drafts if d.candidates)
    # **未試行を「候補なし」「妥当な候補なし」と混ぜない**。所在の確からしさの段階（rubric）は
    # **意味と所在の両方が裏付けられた項目を数える集計の材料**なので、押し直せば埋まる項目が「AI では所在が特定できなかった」に混ざると
    # 段階の分布が動く。
    # **並びは対象項目の順**（4経路で揃える）
    unattempted = list(result.unattempted_link_ids)
    duplicated = list(result.duplicated_link_ids)
    logger.info(
        f"データ所在AI案完了: questionnaire_id={questionnaire_id}, "
        f"候補提示{filled}件, 候補なし{result.no_hit_count}件, "
        f"未試行{len(unattempted)}件, 矛盾した応答{len(duplicated)}件"
    )
    return JSONResponse(
        {
            "success": True,
            # 候補へ到達可能性の印を足す（表示だけ。保存・測定の記録には載らない）
            "drafts": [
                {
                    **d.model_dump(),
                    "candidates": _annotate_reach(
                        [c.model_dump() for c in d.candidates], get_store()
                    ),
                }
                for d in drafts
            ],
            "llm_model": used_model,
            "filled": filled,
            "no_hit": result.no_hit_count,
            # **臨床側（`draft_clinical`）と同じキー名**で別掲する。画面は
            # これを「AI では所在が特定できなかった」と表示してはいけない。
            "unattempted": len(unattempted),
            "unattempted_link_ids": unattempted,
            # **矛盾した応答**（同じ項目に複数のエントリ。臨床側と同じ扱い）。最初の
            # エントリを採ったので一推しは付いているが、**集計では別掲する**。
            "duplicated": len(duplicated),
            "duplicated_link_ids": duplicated,
            # **原因を1つだけ名指ししない**。未試行には打ち切り・チャンクの
            # 失敗・応答漏れが入るので、「制限時間内に処理できませんでした」は事実と違う
            # 項目がある。
            "message": (
                f"{len(unattempted)}項目はAIの一推しが返りませんでした（未試行）。"
                "もう一度実行すると、未入力の項目だけが対象になります"
                if unattempted
                else None
            ),
        }
    )


def _find_candidate(loc: SystemLocation, cand_key: str | None) -> tuple[str, dict[str, Any]] | None:
    """cand_key（省略時は primary）が指す候補を (cand_key, candidate dict) で返す。無ければ None。"""
    cands = loc.candidate_list
    if not cands:
        return None
    if cand_key is None:
        primary = cands[0]
        return compute_cand_key(primary), primary
    for c in cands:
        if compute_cand_key(c) == cand_key:
            return cand_key, c
    return None


@router.post("/questionnaires/{questionnaire_id}/source-survey/system/assessments")
async def create_location_assessment(
    request: Request,
    questionnaire_id: int,
    payload: LocationAssessmentIn,
    current_user: RequireDesigner,
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """データ所在の確からしさの判定を1件記録する。

    判定は候補単位（cand_key。省略時は主たる候補）。同一候補への複数回の判定を
    round_number で区別する（1回目・再判定）。既存の判定行は変更しない
    （追記のみ。訂正は新しい round として記録する）。
    """
    await csrf_protect.validate_csrf(request)
    await _get_questionnaire_or_404(questionnaire_id, db)

    if payload.is_undeterminable:
        if payload.stage is not None:
            return JSONResponse(
                {"success": False, "message": "判定不能の場合は段階を指定できません"},
                status_code=400,
            )
    elif payload.stage is None or not AssessmentStage.is_valid(payload.stage):
        # 語彙の判定を `AssessmentStage.is_valid` に寄せた（DB の CHECK 制約・集計・
        # 集約規則が同じ規則を使う）。**この行の振る舞いは `not in AssessmentStage.ALL` と同じ**
        # である——pydantic が `stage: int | None` を強制するので、ここへ `bool` や `str` は
        # 来ない（この分岐は `stage is None` を先に見るので、`is_valid` が `None` を
        # 許す差もここでは効かない）。したがってこれは**語彙の出所を1つにする整理**で、
        # 新しい層ではない（`is_valid` の型検査は**どの経路からも到達しない多重防御**である
        # ——型検査だけを落としても振る舞いのテストは1本も落ちない）。
        return JSONResponse(
            {"success": False, "message": "段階は1〜4のいずれかを指定してください"},
            status_code=400,
        )

    locations = await _location_map(questionnaire_id, db)
    loc = locations.get(payload.link_id)
    if loc is None:
        return JSONResponse(
            {"success": False, "message": "指定された項目のデータ所在が未保存です"},
            status_code=404,
        )
    found = _find_candidate(loc, payload.cand_key)
    if found is None:
        return JSONResponse(
            {"success": False, "message": "指定された候補が見つかりません"},
            status_code=404,
        )
    cand_key, candidate = found

    result = await db.execute(
        select(LocationCandidateAssessment).where(
            LocationCandidateAssessment.system_location_id == loc.id,
            LocationCandidateAssessment.cand_key == cand_key,
        )
    )
    prior_rounds = result.scalars().all()
    round_number = len(prior_rounds) + 1

    assessment = LocationCandidateAssessment(
        system_location_id=loc.id,
        cand_key=cand_key,
        candidate_snapshot=candidate,
        round_number=round_number,
        stage=payload.stage,
        is_undeterminable=payload.is_undeterminable,
        rationale=payload.rationale,
        unreachable_reason=payload.unreachable_reason,
        variant_dependent_note=payload.variant_dependent_note,
        materials=payload.materials,
        is_self_assessment=(loc.updated_by_id is not None and loc.updated_by_id == current_user.id),
        assessed_by_id=current_user.id,
        assessed_at=datetime.now(UTC),
    )
    db.add(assessment)
    try:
        await db.commit()
    except IntegrityError:
        # 同一候補への同時投稿で round_number が競合した場合の安全網
        # （uq_location_candidate_assessment_round）。取り直しを促す。
        await db.rollback()
        return JSONResponse(
            {"success": False, "message": "判定の記録が競合しました。もう一度試してください"},
            status_code=409,
        )
    logger.info(
        f"所在の確からしさの判定を記録: questionnaire_id={questionnaire_id}, link_id={payload.link_id}, "
        f"cand_key={cand_key}, round={round_number}"
    )
    return JSONResponse({"success": True, "id": assessment.id, "round_number": round_number})


def _best_stage_per_item(
    latest_by_item: dict[str, list[Any]], *, electronic_only: bool = False
) -> tuple[dict[str, int], set[str]]:
    """項目ごとの段階と、判定不能の項目集合を返す（**段階分布の集約規則**）。

    **1項目に複数候補があるときは最良（最大）の段階**をその項目の段階とする。所在の確からしさの判定は
    「その項目をどこかから取得できるか」を測るので、最も確実な候補が到達可能性を決める。
    最悪や平均を採ると、候補を足すほど評価が下がる。

    **段階付きの候補が1つでもあれば、その項目は判定不能に数えない**（排反にする）。
    `assessed_items == Σitems_by_stage + undeterminable_items` が成り立つようにするため。

    `electronic_only=True` のときは電子的な所在の候補だけで集約する。**電子的な所在の特定を
    測るとき、`manual`（人が紙を見て転記）の候補が項目の段階を決めるのは上振れ**なので、
    層別した値も併せて出す。
    """
    staged: dict[str, int] = {}
    undeterminable: set[str] = set()
    for link_id, assessments in latest_by_item.items():
        for latest in assessments:
            # **語彙の検査は `electronic_only` の絞り込みより前に置く**。
            # あとに置くと、非電子の候補に語彙外があるとき層別の呼び出しでだけ黙って捨てられる
            # ——層は呼び出しパラメータに依らず一様でなければ、層と呼べない。
            if not AssessmentStage.row_is_in_vocabulary(latest.stage, latest.is_undeterminable):
                # **語彙外を黙って `max` に入れない**。`int(latest.stage)` は
                # `0` も `7` も通すので、段階の目盛りが黙って広がる。呼び出し側（ルート）が
                # 先に 409 で止めるが、**上位層が先に止めると下位層は無検証のまま残る**ので
                # ここでも要求する（別経路の呼び出しが増えたときに効く層）。
                #
                # **`(stage, is_undeterminable)` の組で見る**。
                # `stage IS NULL` かつ `is_undeterminable = 0` の行は、下の分岐で `staged` にも
                # `undeterminable` にも入らない——docstring が宣言する恒等式
                # `assessed_items == Σitems_by_stage + undeterminable_items` が黙って破れる。
                raise ValueError(
                    f"stage が語彙外（{AssessmentStage.ALL}）: link_id={link_id}, "
                    f"stage={latest.stage!r}, is_undeterminable={latest.is_undeterminable!r}"
                )
            if electronic_only and not _is_electronic_candidate(latest.candidate_snapshot):
                continue
            if latest.is_undeterminable:
                undeterminable.add(link_id)
            elif latest.stage is not None:
                staged[link_id] = max(staged.get(link_id, 0), int(latest.stage))
    return staged, {i for i in undeterminable if i not in staged}


# 電子的な所在とみなすソース種別（**許可リスト**）。`manual`（人が紙を見て転記）と
# `not_available`（この施設に無い）は電子ではない。
#
# **拒否リストにしない。** 「manual と not_available 以外は電子」と書くと、種別が1つ増えた
# 瞬間にそれが電子として数えられる（fail-open。紙の種別を足すと電子的な所在の
# 集計の分子が黙って増える）。候補の正規化（`_clean_candidate`）が未知の種別を `manual` へ
# 落とす向きと揃える。
_ELECTRONIC_SOURCE_CATEGORIES = (
    SourceCategory.FHIR,
    SourceCategory.LOCAL_DB,
    SourceCategory.EXTERNAL_DB,
    SourceCategory.FILE,
    SourceCategory.LLM,
)


def _is_electronic_candidate(snapshot: dict[str, Any]) -> bool:
    """その候補が電子的な所在か（許可リストに入っているものだけ）。"""
    return snapshot.get("source_category") in _ELECTRONIC_SOURCE_CATEGORIES


def _coverage(
    targets: Targets,
    assessed: set[str],
    candidate_votes: int,
    out_of_scope_skipped: int,
    unknown_link_ids: list[str],
    latest_by_item: dict[str, list[Any]],
) -> dict[str, Any]:
    """所在の確からしさの判定の**項目単位**の分母・分子。

    `summary` は候補単位の票数（1項目が3候補なら3票）。**対象項目数で割ってよいのはこちら**である。

    `unassessed_*` は1つのリストから作る（別々に数えると片方だけ壊れても気づけない）。
    `assessed_items == Σitems_by_stage + undeterminable_items` が成り立つ（集約は排反）。
    """
    unassessed = [i for i in targets.link_ids if i not in assessed]
    staged, undeterminable = _best_stage_per_item(latest_by_item)
    staged_e, undeterminable_e = _best_stage_per_item(latest_by_item, electronic_only=True)
    return {
        # 設定の件数と、その問診票で実際に見つかった数。**食い違いは missing に出る**
        "expected_target_items": targets.expected,
        "target_items": len(targets.link_ids),
        "missing_item_numbers": targets.missing_numbers,
        "assessed_items": len(assessed),
        "unassessed_items": len(unassessed),
        "unassessed_link_ids": unassessed,
        # 項目単位の段階分布（複数候補があるときは最良の段階）
        "items_by_stage": _count_stages(staged),
        "undeterminable_items": len(undeterminable),
        # **電子的な所在の候補だけで集約した値**（`manual` / `not_available` を除く）。
        # 電子的な所在の特定を見るときはこちらを見る。上の値は紙を見て転記する候補も含む。
        #
        # `items_without_electronic_candidate` を併せて出すのは、**電子側でも会計が閉じる**
        # ようにするため（`assessed_items` = Σ電子段階 + 電子判定不能 + 電子候補なし）。
        # これが無いと「電子的には取得できない項目」が未判定と区別できず、電子的な所在の集計で
        # いちばん情報量のある集合が消える。
        "items_by_stage_electronic": _count_stages(staged_e),
        "undeterminable_items_electronic": len(undeterminable_e),
        "items_without_electronic_candidate": len(assessed - set(staged_e) - undeterminable_e),
        # 候補単位の票数（`summary` の合計と一致する）
        "candidate_votes": candidate_votes,
        # 分母・分子に入れなかったもの
        "out_of_scope_items_skipped": out_of_scope_skipped,
        "unknown_link_ids": sorted(unknown_link_ids),
    }


def _count_stages(staged: dict[str, int]) -> dict[str, int]:
    """項目 → 段階 を、段階 → 項目数 に数え直す（**所在の確からしさの段階分布**）。

    段階の値をそのままキーにする（写し替えない）。ここで段階を付け替えると分布が黙って
    変わる。
    """
    counts: dict[str, int] = {}
    for stage in staged.values():
        key = str(stage)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


@router.get("/questionnaires/{questionnaire_id}/source-survey/system/assessments")
async def export_location_assessments(
    questionnaire_id: int,
    current_user: RequireDesigner,
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """所在の確からしさの判定を第三者が再判定できる形で出力する。

    項目・候補ごとに判定材料と全 round の判定結果を並べる。あわせて AI 提案由来
    （candidate_snapshot.llm_model あり）/ 人手入力由来で段階別の件数を集計する
    （最新 round を「現在の判定状況」として使う）。
    """
    questionnaire = await _get_questionnaire_or_404(questionnaire_id, db)
    # **分母は問診票の対象項目**。判定が1件も無い項目もここに含める。
    # 候補や判定の有無から分母を作ると、`SystemLocation` 行が無い項目・候補が空の項目・
    # 判定0件の候補が静かに落ち、「対象項目のうち N 項目」を出力できなくなる。
    # **設定のある様式では、分母の正本は設定の項目番号で、問診票から数えた件数ではない。**
    # 欠けたら `missing_item_numbers` に出す（「静かに落ちる」を「数えて返す」に揃える）。
    targets = resolve_targets(questionnaire.url, _editable_fields(questionnaire))
    target_ids = targets.link_ids
    item_kinds = targets.kinds
    locations = await _location_map(questionnaire_id, db)
    if not locations:
        return JSONResponse(
            {
                "items": [],
                "summary": {},
                "coverage": _coverage(targets, set(), 0, 0, [], {}),
            }
        )

    location_ids = [loc.id for loc in locations.values()]
    result = await db.execute(
        select(LocationCandidateAssessment)
        .where(LocationCandidateAssessment.system_location_id.in_(location_ids))
        .order_by(LocationCandidateAssessment.id)
    )
    by_location: dict[int, list[LocationCandidateAssessment]] = {}
    for a in result.scalars().all():
        by_location.setdefault(a.system_location_id, []).append(a)

    summary: dict[str, dict[str, int]] = {"ai_proposed": {}, "manual": {}}
    items: list[dict[str, Any]] = []
    # 判定が1件以上ある**対象**項目（項目単位の分子）。候補単位の票数とは別に数える。
    assessed_target_ids: set[str] = set()
    out_of_scope_skipped = 0
    unknown_link_ids: list[str] = []
    # **語彙外の `stage` を持つ最新判定**。1件でもあれば集計を返さない。
    out_of_vocabulary: list[dict[str, Any]] = []
    candidate_votes = 0
    # 項目単位の段階分布（**段階分布の分子**）。1項目に複数候補があるときは**最良の段階**を
    # その項目の段階とする——「その項目はどこかから取得できるか」を測るのが所在の確からしさの判定なので、
    # 候補のうち最も確実なものが項目の到達可能性を決める。判定不能（`is_undeterminable`）は
    # 段階を持たないので別に数える。**この集約規則は測定の目盛りそのものである**。
    # 項目 → その項目の候補の最新判定。集約規則は `_best_stage_per_item` に閉じる
    # （呼び出し側に散らすと、集約規則が1箇所で読めなくなる）。
    latest_by_item: dict[str, list[LocationCandidateAssessment]] = {}
    target_id_set = set(target_ids)
    for link_id, loc in locations.items():
        in_scope = link_id in target_id_set
        if not in_scope and by_location.get(loc.id):
            # 対象外の判定は集計の分母・分子に入れない。**黙って落とさず数えて返す。**
            # 「意図した対象外（設定で外した項目・番号の無い項目）」と「問診票に無い link_id（データ不整合）」は
            # 区別する（保存経路は link_id を問診票と突き合わせない）。
            if link_id in item_kinds:
                out_of_scope_skipped += 1
            else:
                unknown_link_ids.append(link_id)
        rows = by_location.get(loc.id, [])
        by_cand_key: dict[str, list[LocationCandidateAssessment]] = {}
        for a in rows:
            by_cand_key.setdefault(a.cand_key, []).append(a)
        for cand_key, history in by_cand_key.items():
            history.sort(key=lambda a: a.round_number)
            latest = history[-1]
            bucket = "ai_proposed" if latest.candidate_snapshot.get("llm_model") else "manual"
            # **語彙外の値で新しい集計キーを作らない**。`str(latest.stage)` を
            # そのままキーにすると `"0"` / `"None"` / `"True"` が段階分布の目盛りに増える。
            # DB の CHECK 制約（`AssessmentStage.CHECK_NAME`）が入る前に保存された行、
            # 制約を持たない環境で直接書かれた行がここへ来る。**集計へは入れないが、
            # `items`（再判定用の記録）には残す**——どの行を直すべきかが分からないと
            # 運用できない。
            # **`(stage, is_undeterminable)` の組で見る**。
            # `is_valid(latest.stage)` だけだと、`stage IS NULL` かつ `is_undeterminable = 0`
            # の行が語彙内と判定され、`str(None)` が `summary["manual"]["None"]` になった
            # （実測）。`None` が語彙内なのは判定不能の行だけである。
            in_vocabulary = AssessmentStage.row_is_in_vocabulary(
                latest.stage, latest.is_undeterminable
            )
            if not in_vocabulary:
                out_of_vocabulary.append(
                    {"id": latest.id, "link_id": link_id, "cand_key": cand_key}
                )
            stage_key = "undeterminable" if latest.is_undeterminable else str(latest.stage)
            # `and in_vocabulary` は**観測できる効果を持たない**（下の 409 が先に返るので
            # `summary` は外へ出ない）。多重防御として残すが、**測れる層としては数えない**
            # （ここを外しても落ちるテストは無い）。
            if in_scope and in_vocabulary:
                # **`summary` と `coverage` は対象項目だけ**を数える。一方 `items` は
                # 第三者による**再判定用の記録**なので、対象外・未知 link_id の
                # 判定も落とさない（判定記録は追記のみで、ここが唯一の出力経路）。
                summary[bucket][stage_key] = summary[bucket].get(stage_key, 0) + 1
                candidate_votes += 1
                assessed_target_ids.add(link_id)
                latest_by_item.setdefault(link_id, []).append(latest)
            items.append(
                {
                    "link_id": link_id,
                    "cand_key": cand_key,
                    # 対象外の判定は分母・分子に入っていないことを、記録の側でも分かるように
                    "in_scope": in_scope,
                    "item_kind": item_kinds.get(link_id, "unknown"),
                    "rounds": [
                        {
                            "round_number": a.round_number,
                            "candidate_snapshot": a.candidate_snapshot,
                            "stage": a.stage,
                            "stage_label": a.stage_label,
                            "is_undeterminable": a.is_undeterminable,
                            "rationale": a.rationale,
                            "unreachable_reason": a.unreachable_reason,
                            "variant_dependent_note": a.variant_dependent_note,
                            "materials": a.materials,
                            "is_self_assessment": a.is_self_assessment,
                            "assessed_by_id": a.assessed_by_id,
                            "assessed_at": a.assessed_at.isoformat(),
                        }
                        for a in history
                    ],
                }
            )

    if out_of_vocabulary:
        # **集計を返さない**。語彙外の判定が1件でもあると、段階分布は
        # 「その1件を落とした値」になる——**落としたことが読み手に見えないまま**。
        # `items`（再判定用の記録）は返す：どの行を直すべきかが分からないと運用できない。
        logger.error(
            f"所在の確からしさの判定に語彙外の stage がある: questionnaire_id={questionnaire_id}, "
            f"{len(out_of_vocabulary)}件（語彙: {AssessmentStage.ALL}）"
        )
        return JSONResponse(
            {
                "items": items,
                "message": (
                    f"{len(out_of_vocabulary)}件の判定が段階の語彙（{AssessmentStage.ALL}）の"
                    "外にあるため、集計を返しません。判定記録は追記のみなので、"
                    "該当行の扱いは記録の管理者による判断が必要です"
                ),
                "out_of_vocabulary": out_of_vocabulary,
            },
            status_code=409,
        )

    return JSONResponse(
        {
            "items": items,
            "summary": summary,
            # **項目単位の分母と分子**。`summary` は候補単位の票数なので、
            # 対象項目数で割ってよいのはこちらである。
            "coverage": _coverage(
                targets,
                assessed_target_ids,
                candidate_votes,
                out_of_scope_skipped,
                unknown_link_ids,
                latest_by_item,
            ),
        }
    )


@router.get("/questionnaires/{questionnaire_id}/source-survey/system/{link_id}/statistical-profile")
async def get_statistical_profile(
    questionnaire_id: int,
    link_id: str,
    current_user: RequireDesigner,
    db: AsyncSession = Depends(get_db),
    cand_key: str | None = None,
) -> JSONResponse:
    """所在候補の統計プロファイルを生成する。

    所在の確からしさの判定の判定材料（`LocationAssessmentIn.materials`）として使う。ここで返す
    JSON をそのまま `materials` に渡せば、判定記録に判定材料のスナップショットが
    残る（規則適用済みであることは `rules_applied` に記録されている）。
    """
    await _get_questionnaire_or_404(questionnaire_id, db)
    locations = await _location_map(questionnaire_id, db)
    loc = locations.get(link_id)
    if loc is None:
        return JSONResponse(
            {"success": False, "message": "指定された項目のデータ所在が未保存です"},
            status_code=404,
        )
    found = _find_candidate(loc, cand_key)
    if found is None:
        return JSONResponse(
            {"success": False, "message": "指定された候補が見つかりません"},
            status_code=404,
        )
    resolved_cand_key, candidate = found
    profile = await generate_statistical_profile(candidate, db)
    return JSONResponse({"cand_key": resolved_cand_key, **profile})


# =============================================================================
# ステップ2: データ取得手順書（runbook）の生成＋1項目ずつ確認
# =============================================================================


def _serialize_step(step: dict[str, Any]) -> dict[str, Any]:
    """保存された steps 要素を画面表示用に正規化する"""
    return {
        "order": step.get("order"),
        "title": step.get("title") or "",
        "description": step.get("description") or "",
        "action_type": step.get("action_type"),
        "data_hint": step.get("data_hint"),
    }


def _steps_with_known_tags(steps: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """語彙外の action_type を None にした steps を返す（保存済みデータは変更しない）。"""
    cleaned: list[dict[str, Any]] = []
    for step in steps:
        if step.get("action_type") in RUNBOOK_ACTION_TYPES:
            cleaned.append(step)
        else:
            cleaned.append({**step, "action_type": None})
    return cleaned


def _dropped_action_types(body: Any, saved: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """保存要求で与えられたが語彙外として落ちた action_type を列挙する。

    保存済み側は検証後（None）なので、**何を送ってきたか**は生のリクエスト本文にしか
    残っていない。order は採番し直しているので、突き合わせは配列の位置で行う。
    """
    if not isinstance(body, dict):
        return []
    raw_steps = body.get("steps")
    if not isinstance(raw_steps, list):
        return []
    dropped: list[dict[str, Any]] = []
    for idx, raw in enumerate(raw_steps):
        if idx >= len(saved) or not isinstance(raw, dict):
            continue
        given = raw.get("action_type")
        # 空・空白のみは「タグ無し」であって落としたのではない（検証側が strip して None に
        # するのと同じ扱いにする。ここだけ厳しくすると正常系で警告が出る）。
        blank = given is None or (isinstance(given, str) and not given.strip())
        if saved[idx].get("action_type") is None and not blank:
            dropped.append({"order": saved[idx].get("order"), "given": str(given)[:64]})
    return dropped


def _build_runbook_items(
    fields: list[dict[str, Any]],
    annotations: dict[str, ClinicalAnnotation],
    locations: dict[str, SystemLocation],
    runbooks: dict[str, InputRunbook],
) -> list[dict[str, Any]]:
    """ウィザードへ渡す項目リスト（設計済み項目のみ）を構築する"""
    items: list[dict[str, Any]] = []
    for field in fields:
        link_id = field["link_id"]
        ann = annotations.get(link_id)
        loc = locations.get(link_id)
        if not _is_designed(ann, loc):
            continue
        rb = runbooks.get(link_id)
        design_at = _design_updated_at(ann, loc)
        stale = _runbook_stale(rb, design_at)
        items.append(
            {
                "link_id": link_id,
                "item_text": field.get("text") or "",
                "annotation": (ann.annotation if ann else None),
                "source_category": (loc.primary_source_category if loc else SourceCategory.MANUAL),
                "source_label": SourceCategory.LABELS.get(
                    loc.primary_source_category if loc else SourceCategory.MANUAL, ""
                ),
                "location": (loc.primary_location if loc else None),
                "steps": [_serialize_step(s) for s in (rb.steps if rb else [])],
                "status": (rb.status if rb else RunbookStatus.DRAFT),
                "has_runbook": rb is not None,
                "stale": stale,
                "llm_model": (rb.llm_model if rb else None),
                "version": runbook_version(rb),
            }
        )
    return items


@router.get("/questionnaires/{questionnaire_id}/runbook", response_class=HTMLResponse)
async def runbook_wizard(
    request: Request,
    questionnaire_id: int,
    current_user: RequireDesigner,
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """ステップ2: 手順書の生成＋1項目ずつ確認するウィザード画面"""
    questionnaire = await _get_questionnaire_or_404(questionnaire_id, db)
    fields = _editable_fields(questionnaire)
    annotations = await _annotation_map(questionnaire_id, db)
    locations = await _location_map(questionnaire_id, db)
    runbooks = await _runbook_map(questionnaire_id, db)

    items = _build_runbook_items(fields, annotations, locations, runbooks)

    csrf_token, signed_token = stable_csrf_tokens(request, csrf_protect)
    response = templates.TemplateResponse(
        "ai_input/design_runbook.html",
        {
            "request": request,
            "current_user": current_user,
            "questionnaire": questionnaire,
            "current_stage": 2,
            "items": items,
            "status_labels": RunbookStatus.LABELS,
            "default_ollama_model": design_llm_model(),
            "csrf_token": csrf_token,
        },
    )
    if signed_token is not None:
        csrf_protect.set_csrf_cookie(signed_token, response)
    return response


async def _upsert_runbook(
    questionnaire_id: int,
    link_id: str,
    item_text: str | None,
    steps: list[dict[str, Any]],
    status: str,
    llm_model: str | None,
    existing: dict[str, InputRunbook],
    db: AsyncSession,
    confirmed_by_id: int | None = None,
    confirmed_at: datetime | None = None,
    dropped_action_types: list[dict[str, Any]] | None = None,
) -> InputRunbook:
    # **落ちたタグが無ければ null を書く**。保存のたびに上書きするので、
    # 前の保存で落ちた記録が「今の手順書に語彙外タグがある」と読まれることはない。
    dropped = dropped_action_types or None
    rb = existing.get(link_id)
    if rb:
        rb.item_text = item_text
        rb.steps = steps
        rb.status = status
        rb.confirmed_by_id = confirmed_by_id
        rb.confirmed_at = confirmed_at
        rb.dropped_action_types = dropped
        _force_write_columns(rb)
        if llm_model is not None:
            rb.llm_model = llm_model
        return rb
    rb = InputRunbook(
        questionnaire_id=questionnaire_id,
        link_id=link_id,
        item_text=item_text,
        steps=steps,
        status=status,
        llm_model=llm_model,
        confirmed_by_id=confirmed_by_id,
        confirmed_at=confirmed_at,
        dropped_action_types=dropped,
    )
    db.add(rb)
    return rb


def _combined_inputs(
    link_ids: list[str],
    fields_text: dict[str, str | None],
    annotations: dict[str, ClinicalAnnotation],
    locations: dict[str, SystemLocation],
) -> list[dict[str, Any]]:
    combined = []
    for link_id in link_ids:
        ann = annotations.get(link_id)
        loc = locations.get(link_id)
        entry: dict[str, Any] = {
            "link_id": link_id,
            "item_text": fields_text.get(link_id),
            "source_category": loc.primary_source_category if loc else SourceCategory.MANUAL,
            "location": loc.primary_location if loc else None,
            "annotation": ann.annotation if ann else None,
        }
        # **代替候補があるときだけ渡す**。所在は優先順の候補配列なので、
        # 主候補しか渡さないと「無ければ○○から」を手順書段階で書けない。
        #
        # **キーごと入れないのは、プロンプトを1バイトも変えないためである。** 候補が1件の
        # 項目（現状の全データ）では `_build_user_message` の出力が従来と完全に一致する。
        # 空配列を入れると、将来「代替なし」と書き足す変更が入ったときに測定済みの項目まで
        # プロンプトが変わる。
        alternates = [
            c.get("location")
            for c in ((loc.candidates if loc else None) or [])[1:]
            if isinstance(c, dict) and (c.get("location") or "").strip()
        ]
        if alternates:
            entry["alternate_locations"] = alternates
        combined.append(entry)
    return combined


def _join_notices(*notices: str | None) -> str | None:
    """画面に出す注記をつなぐ。1つも無ければ None（正常系の `message` を None に保つ）。"""
    return "\n".join(n for n in notices if n) or None


@router.post("/questionnaires/{questionnaire_id}/runbook/generate")
async def generate_runbooks(
    request: Request,
    questionnaire_id: int,
    payload: RunbookGenerateRequest,
    current_user: RequireDesigner,
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """ステップ2: 設計済み項目の手順書を一括生成する（draft保存）

    既に confirmed の項目は人の確認を尊重して上書きしない（draft/未生成のみ対象）。
    """
    await csrf_protect.validate_csrf(request)
    if not settings.enable_ai_drafting:
        return _drafting_disabled_response()
    questionnaire = await _get_questionnaire_or_404(questionnaire_id, db)
    model = (payload.model or "").strip() or None

    fields = _editable_fields(questionnaire)
    fields_text: dict[str, str | None] = {f["link_id"]: f.get("text") for f in fields}
    annotations = await _annotation_map(questionnaire_id, db)
    locations = await _location_map(questionnaire_id, db)
    runbooks = await _runbook_map(questionnaire_id, db)

    # 後段ゲート: データ所在が未解決（候補も「該当データなし」も未指定）の項目が残る間は
    # 手順書生成に進めない。各項目で候補を選ぶか「該当データなし」を明示させる。
    unresolved = _unresolved_link_ids(fields, annotations, locations)
    if unresolved:
        return JSONResponse(
            {
                "success": False,
                "message": (
                    f"データ所在が未解決の項目が{len(unresolved)}件あります。"
                    "各項目で候補を選択するか「該当データなし」を指定してから手順書生成に進んでください。"
                ),
                "unresolved": unresolved,
            },
            status_code=400,
        )

    # 設計済み かつ（未生成 または draft）の項目のみ対象。confirmed は上書きしない。
    # **対象から外した項目は理由ごとに応答へ出す**。出さないと、表には前回以前の
    # 行が残るので、行を数える集計が古い行を今回の結果として数える。2つの条件を混ぜない。
    target_link_ids: list[str] = []
    skipped_not_designed: list[str] = []
    skipped_confirmed: list[str] = []
    # この実行より前から表にある行。今回書き換えなかった行を後で数える（事後の検算）
    runbook_rows_before = set(runbooks)
    for f in fields:
        link_id = f["link_id"]
        if not _is_designed(annotations.get(link_id), locations.get(link_id)):
            skipped_not_designed.append(link_id)
            continue
        rb = runbooks.get(link_id)
        if rb is not None and rb.status == RunbookStatus.CONFIRMED:
            skipped_confirmed.append(link_id)
            continue
        target_link_ids.append(link_id)

    if not target_link_ids:
        return JSONResponse(
            {
                "success": False,
                "message": "生成対象の項目がありません（未設計、または全て確認済みです）",
                "skipped_not_designed": skipped_not_designed,
                "skipped_confirmed": skipped_confirmed,
            },
            status_code=400,
        )

    combined = _combined_inputs(target_link_ids, fields_text, annotations, locations)
    # **LLM を呼ぶ前の版を控える**。書き込みの直前に読み直した版と比べ、生成中に別の
    # 要求が確認・編集した項目は上書きしない（`_lock_rows_for_write`）。
    versions_before = {lid: runbook_version(runbooks.get(lid)) for lid in target_link_ids}
    user_id = current_user.id

    generator = AIRunbookGenerator()
    try:
        recorder = DesignLlmRecorder(DesignRole.RUNBOOK)
        generated = await generator.generate(combined, model=model, recorder=recorder)
    except AIServiceException as e:
        # 失敗した呼び出しも来歴に残す。何を渡して何が返らなかったかは、
        # 成功時と同じくらい後から必要になる。
        await save_design_llm_calls_safely(
            db,
            questionnaire_id=questionnaire_id,
            recorder=recorder,
            created_by_id=user_id,
        )
        return JSONResponse({"success": False, "message": e.message}, status_code=500)
    await save_design_llm_calls_safely(
        db,
        questionnaire_id=questionnaire_id,
        recorder=recorder,
        created_by_id=user_id,
    )

    used_model = model or design_llm_model()
    generated_map = {g.link_id: g for g in generated}
    to_write = {
        lid: [s.model_dump() for s in generated_map[lid].steps]
        for lid in target_link_ids
        if lid in generated_map
    }
    saved_link_ids: list[str] = []
    # 生成中に別の操作で行が変わった（確認・編集・同時の新規作成）ため書かなかった項目
    skipped_conflict: list[str] = []
    for _attempt in range(_GENERATION_WRITE_ATTEMPTS):
        current = await _lock_rows_for_write(db, InputRunbook, questionnaire_id, list(to_write))
        saved_link_ids, skipped_conflict = [], []
        for link_id, steps in to_write.items():
            if runbook_version(current.get(link_id)) != versions_before[link_id]:
                skipped_conflict.append(link_id)
                continue
            await _upsert_runbook(
                questionnaire_id,
                link_id,
                fields_text.get(link_id),
                steps,
                RunbookStatus.DRAFT,
                used_model,
                current,
                db,
            )
            saved_link_ids.append(link_id)
        try:
            await db.commit()
            break
        except IntegrityError as e:
            # 「無い」と読んだ項目を別の要求が先に作った。読み直すとその行が見えるので、
            # 次の周ではその項目が版の不一致として skip に回る。
            await db.rollback()
            if not _is_unique_violation(e):
                raise
    else:
        return _generation_conflict_response(None)
    saved = len(saved_link_ids)
    # **未試行を別掲する**。この経路の「未試行」は**手順書を生成していない**
    # （再実行すれば対象に入る）。渡した link_id と返ってきた link_id の差で数える
    # ——**順序ではなく応答の有無で定義する**（途中のチャンクが飛ぶ形も同じ扱いにする）。
    unattempted = [lid for lid in target_link_ids if lid not in generated_map]
    logger.info(
        f"手順書一括生成保存完了: questionnaire_id={questionnaire_id}, {saved}件, "
        f"未試行{len(unattempted)}件, model={used_model}"
    )
    return JSONResponse(
        {
            "success": True,
            "generated": saved,
            "model": used_model,
            # **臨床・所在と同じキー名**。「生成できなかった」ではなく
            # 「生成していない」なので、押し直せば埋まる。
            "generated_link_ids": saved_link_ids,
            "unattempted": len(unattempted),
            "unattempted_link_ids": unattempted,
            # 対象に入らなかった項目。`generated` は**対象の中の**件数で、表の行数
            # ではない。表には今回書き換えなかった行が残っている。
            "skipped_not_designed": skipped_not_designed,
            "skipped_confirmed": skipped_confirmed,
            # 生成中に別の操作で確認・編集・作成されたため、生成結果を書かなかった項目
            "skipped_conflict": skipped_conflict,
            # 表にあるが今回書き換えなかった行（事後の検算）
            "rows_not_rewritten": sorted(runbook_rows_before - set(saved_link_ids)),
            "message": _join_notices(
                (
                    f"{len(skipped_conflict)}件は生成中に別の操作で更新されたため、"
                    f"生成結果を保存していません: {', '.join(skipped_conflict)}"
                    if skipped_conflict
                    else None
                ),
                (
                    f"{len(unattempted)}項目は手順書が返りませんでした（未試行）。"
                    # **対象集合を偽って書かない**。この経路の対象は
                    # 「設計済み かつ（未生成 **または draft**）」なので、押し直すと**保存済みの
                    # draft も再生成される**（確認済みは対象外）。
                    "もう一度実行すると、未生成の項目と draft の手順書が対象になります"
                    "（確認済みは対象外）"
                    if unattempted
                    else None
                ),
                (
                    f"{len(skipped_confirmed)}件は確認済みのため作り直していません。"
                    if skipped_confirmed
                    else None
                ),
                (
                    f"{len(skipped_not_designed)}件は未設計のため対象外です。"
                    if skipped_not_designed
                    else None
                ),
            ),
        }
    )


@router.post("/questionnaires/{questionnaire_id}/runbook/{link_id}/regenerate")
async def regenerate_runbook(
    request: Request,
    questionnaire_id: int,
    link_id: str,
    payload: RunbookGenerateRequest,
    current_user: RequireDesigner,
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """ステップ2: 単一項目の手順書を再生成する（既存を上書きし draft に戻す）"""
    await csrf_protect.validate_csrf(request)
    if not settings.enable_ai_drafting:
        return _drafting_disabled_response()
    questionnaire = await _get_questionnaire_or_404(questionnaire_id, db)
    model = (payload.model or "").strip() or None

    fields = _editable_fields(questionnaire)
    fields_text: dict[str, str | None] = {f["link_id"]: f.get("text") for f in fields}
    if link_id not in fields_text:
        raise ResourceNotFoundException("QuestionnaireItem", link_id)

    annotations = await _annotation_map(questionnaire_id, db)
    locations = await _location_map(questionnaire_id, db)
    runbooks = await _runbook_map(questionnaire_id, db)

    combined = _combined_inputs([link_id], fields_text, annotations, locations)
    # LLM を呼ぶ前の版。書き込みの直前に読み直した版と違えば 409（上書きしない）
    version_before = runbook_version(runbooks.get(link_id))

    generator = AIRunbookGenerator()
    try:
        recorder = DesignLlmRecorder(DesignRole.RUNBOOK)
        generated = await generator.generate(combined, model=model, recorder=recorder)
    except AIServiceException as e:
        # 失敗した呼び出しも来歴に残す。何を渡して何が返らなかったかは、
        # 成功時と同じくらい後から必要になる。
        await save_design_llm_calls_safely(
            db,
            questionnaire_id=questionnaire_id,
            recorder=recorder,
            created_by_id=current_user.id,
        )
        return JSONResponse({"success": False, "message": e.message}, status_code=500)
    await save_design_llm_calls_safely(
        db,
        questionnaire_id=questionnaire_id,
        recorder=recorder,
        created_by_id=current_user.id,
    )

    used_model = model or design_llm_model()
    g = next((x for x in generated if x.link_id == link_id), None)
    if g is None:
        # **未試行を「空の手順書」として保存しない**。応答にこの項目が
        # 含まれなかっただけなので、生成結果ではない。以前はここで空ステップを保存して
        # **既存の手順書（人が直したものを含む）を消していた**。
        logger.warning(
            f"手順書再生成: 応答にこの項目が含まれなかった（未試行）: "
            f"questionnaire_id={questionnaire_id}, link_id={link_id}"
        )
        return JSONResponse(
            {
                "success": False,
                "message": (
                    "AIの応答にこの項目が含まれませんでした（未試行）。"
                    "既存の手順書は変更していません。もう一度実行してください"
                ),
            },
            status_code=502,
        )
    steps = [s.model_dump() for s in g.steps]

    # **生成中に確認・編集された行へ書かない**。呼び出し前に読んだオブジェクトではなく、
    # 行ロック付きで読み直した行の版を比べる（`_lock_rows_for_write`）。
    current = await _lock_rows_for_write(db, InputRunbook, questionnaire_id, [link_id])
    current_version = runbook_version(current.get(link_id))
    if current_version != version_before:
        await db.rollback()
        logger.warning(
            f"手順書再生成: 生成中に行が更新されたため保存しない: "
            f"questionnaire_id={questionnaire_id}, link_id={link_id}"
        )
        return _generation_conflict_response(current_version)

    saved = await _upsert_runbook(
        questionnaire_id,
        link_id,
        fields_text.get(link_id),
        steps,
        RunbookStatus.DRAFT,
        used_model,
        current,
        db,
    )
    conflict = await _commit_or_conflict(db)
    if conflict is not None:
        return conflict
    logger.info(
        f"手順書再生成: questionnaire_id={questionnaire_id}, link_id={link_id}, model={used_model}"
    )
    return JSONResponse(
        {
            "success": True,
            "link_id": link_id,
            "steps": [_serialize_step(s) for s in steps],
            "status": RunbookStatus.DRAFT,
            "model": used_model,
            "version": runbook_version(saved),
        }
    )


_RUNBOOK_STEP_FIELDS: tuple[tuple[str, Any], ...] = (
    ("title", ""),
    ("description", ""),
    ("action_type", None),
    ("data_hint", None),
)


def _runbook_canonical(steps: Any) -> str:
    """手順書 steps の同一性を比べるための正準形（キー順に依らない JSON 文字列）。

    保存と同じく order を 1 始まりで振り直し、`RunbookStep` の各欄を既定値で埋めて
    比べる（欠けた欄・null と既定値は同一とみなす。画面は title / description の null を
    空文字で表示するため）。`action_type` は**正規化しない**。
    語彙外のタグは保存時に None へ落ちるので、落ちた保存は内容が変わった側に倒れる。
    リストでない旧データは生のまま比べる。
    """
    if isinstance(steps, list) and all(isinstance(s, dict) for s in steps):
        steps = [
            {
                "order": idx,
                **{k: default if s.get(k) is None else s[k] for k, default in _RUNBOOK_STEP_FIELDS},
            }
            for idx, s in enumerate(steps, start=1)
        ]
    return json.dumps(steps, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _runbook_changed(before: Any, after: Any) -> bool:
    """確認済み手順書の steps が実質的に変わったか（正準 JSON の比較）。"""
    return _runbook_canonical(before) != _runbook_canonical(after)


@router.post("/questionnaires/{questionnaire_id}/runbook/{link_id}")
async def save_runbook(
    request: Request,
    questionnaire_id: int,
    link_id: str,
    payload: RunbookSaveRequest,
    current_user: RequireDesigner,
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
    confirm: bool = False,
) -> JSONResponse:
    """ステップ2: 1項目分の手順書ステップを保存する（upsert）

    `confirm=true` のときは保存と同時に status を confirmed にする（確認して次へ）。

    `confirm=false` の保存で確認済み手順書の内容が変わったら **draft へ戻し、確認者・
    確認日時を消す**（`regenerate` と同じ扱い。取得プランの `save_plan` と同じ規則）。
    正準形が同一の保存だけは確認済みを保つ。

    **取得プランは連動させない。** 実行されるのはプランの program であり、それは
    プランの確認で承認されている。手順書が後から変わったことは、プランの `stale`
    （手順書より古い）表示で設計者に知らせる。
    """
    await csrf_protect.validate_csrf(request)
    questionnaire = await _get_questionnaire_or_404(questionnaire_id, db)

    fields = _editable_fields(questionnaire)
    fields_text: dict[str, str | None] = {f["link_id"]: f.get("text") for f in fields}
    if link_id not in fields_text:
        raise ResourceNotFoundException("QuestionnaireItem", link_id)

    runbooks = await _runbook_map(questionnaire_id, db)
    existing = runbooks.get(link_id)
    await _lock_row(db, existing)
    conflict = _version_conflict(payload, runbook_version(existing), required=confirm)
    if conflict is not None:
        return conflict

    # order を 1 始まりで採番し直して保存する
    steps = []
    for idx, step in enumerate(payload.steps, start=1):
        data = step.model_dump()
        data["order"] = idx
        steps.append(data)

    # 語彙外の action_type は検証で None へ落ちている（schemas.RunbookStep）。**落ちたことを
    # 同期的に返す。** UI のセレクタは語彙しか出せないので、ここに載るのは API 直叩きや
    # 他システムからの移行データだけだが、黙って消えると気づけない（ALCOA+ の追跡可能性）。
    # 弾かない理由は schemas 側のコメントを参照（1タグで複数項目を失わせない）。
    #
    # **画面側は意図的にこれを表示していない。** ウィザードのセレクタは語彙しか出せないので
    # 人手の保存でここが空でなくなる経路が無く、常時出ない警告は読まれなくなる。読み手は
    # 応答 JSON を直接見る呼び出し元（移行スクリプト・API 直叩き）である。
    dropped_action_types = _dropped_action_types(await request.json(), steps)

    if not confirm and existing is not None and not _runbook_changed(existing.steps, steps):
        # 内容が同一の保存は何も書かない（status・確認者・更新日時を動かさない）
        return JSONResponse(
            {
                "success": True,
                "link_id": link_id,
                "status": existing.status,
                "steps": [_serialize_step(s) for s in existing.steps],
                "dropped_action_types": dropped_action_types,
                "unchanged": True,
                "version": runbook_version(existing),
            }
        )

    demoted = False
    if confirm:
        status = RunbookStatus.CONFIRMED
        # 確認操作者・日時を記録（ALCOA+ 帰属性）
        confirmed_by_id: int | None = current_user.id
        confirmed_at: datetime | None = datetime.now(UTC)
    else:
        # ここに来るのは内容が変わった保存だけ（同一の保存は上で返している）。
        # 確認済みなら draft へ戻す。人が確認したのは変更前の手順書である。
        demoted = bool(existing and existing.status == RunbookStatus.CONFIRMED)
        status = RunbookStatus.DRAFT
        confirmed_by_id = None
        confirmed_at = None

    saved = await _upsert_runbook(
        questionnaire_id,
        link_id,
        fields_text.get(link_id),
        steps,
        status,
        None,
        runbooks,
        db,
        confirmed_by_id=confirmed_by_id,
        confirmed_at=confirmed_at,
        dropped_action_types=dropped_action_types,
    )
    conflict = await _commit_or_conflict(db)
    if conflict is not None:
        return conflict
    logger.info(
        f"手順書保存: questionnaire_id={questionnaire_id}, link_id={link_id}, "
        f"status={status}, steps={len(steps)}"
        + (f", 語彙外タグ{len(dropped_action_types)}件" if dropped_action_types else "")
        + ("（内容変更により確認済みから draft へ戻した）" if demoted else "")
    )
    response: dict[str, Any] = {
        "success": True,
        "link_id": link_id,
        "status": status,
        "steps": [_serialize_step(s) for s in steps],
        "dropped_action_types": dropped_action_types,
        "version": runbook_version(saved),
    }
    if demoted:
        response["demoted"] = True
        response["warning"] = (
            "確認済みの手順書を変更したため未確認（draft）に戻しました。"
            "内容を確かめて確認し直してください（作成済みの取得プランはそのままです）"
        )
    return JSONResponse(response)


# =============================================================================
# ステップ3: 確認済み手順書 → 取得プラン（InputPlan）の作成
# =============================================================================


def _program_dump(program: Any) -> dict[str, Any]:
    """DSLProgram(pydantic) を保存用 dict に正規化する（return エイリアス維持・null除去）"""
    data: dict[str, Any] = program.model_dump(by_alias=True, exclude_none=True)
    data.setdefault("steps", [])
    return data


def _program_canonical(program: Any) -> str:
    """program の同一性を比べるための正準形（キー順に依らない JSON 文字列）。

    保存済みの program は経路（コンパイル・手保存・旧版）によって null の有無などの
    表記が揺れ得るため、**保存時と同じ `DSLProgram` → `_program_dump` を通してから**
    比べる。通らない旧データは生の dict のまま比べる（＝表記が違えば「変わった」側に
    倒れ、確認済みが不当に残ることは無い）。
    """
    if isinstance(program, dict):
        try:
            program = _program_dump(DSLProgram.model_validate(program))
        except ValidationError:
            pass
    return json.dumps(program, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _program_changed(before: Any, after: Any) -> bool:
    """確認済みプランの program が実質的に変わったか（正準 JSON の比較）。"""
    return _program_canonical(before) != _program_canonical(after)


def _build_plan_items(
    runbooks: dict[str, InputRunbook],
    plans: dict[str, InputPlan],
) -> list[dict[str, Any]]:
    """ステップ3ウィザードへ渡す項目リスト（確認済み手順書の項目）を構築する

    **手順書が未確認でも、取得プランが既にある項目は載せる**（読み取り専用）。手順書を
    編集して draft へ戻しても確認済みのプランは連動せず実行され続けるので、画面から
    消えると、実行されているのに見えず、下書きへ戻す手段も無くなる。
    """
    items: list[dict[str, Any]] = []
    for link_id, rb in runbooks.items():
        plan = plans.get(link_id)
        runbook_confirmed = rb.status == RunbookStatus.CONFIRMED
        if not runbook_confirmed and plan is None:
            continue
        stale = _plan_stale(plan, rb)
        program = plan.program if plan else {"steps": [], "return": None}
        items.append(
            {
                "link_id": link_id,
                "item_text": rb.item_text or "",
                "program": program,
                "status": plan.status if plan else PlanStatus.DRAFT,
                "has_plan": plan is not None,
                "needs_input": has_needs_input(program) if plan else False,
                # step が1つも無いプログラム。手入力への縮退か、何も作れなかったかは
                # 検証では区別できないので、画面・集計が別に数えられるようにする。
                "empty_program": is_empty_program(program) if plan else False,
                "stale": stale,
                "llm_model": plan.llm_model if plan else None,
                # False なら読み取り専用（確認・編集・再作成は手順書の確認待ち。下書きへ戻すのみ可）
                "runbook_confirmed": runbook_confirmed,
                "version": plan_version(plan),
            }
        )
    return items


def _compile_inputs(
    link_ids: list[str],
    runbooks: dict[str, InputRunbook],
    annotations: dict[str, ClinicalAnnotation],
    locations: dict[str, SystemLocation],
) -> list[dict[str, Any]]:
    """ステップ3コンパイラへ渡す項目入力を組み立てる。

    データ所在は人間可読の location だけでなく、リソース索引由来の機械可読情報
    （unit_id / connection_id / payload）と優先順の候補配列 candidates も渡す。
    コンパイラはこれらから op 引数（表・カラム・FHIRクエリ・パス）を決定的に導出し、
    LLM の推測に委ねる範囲を最小化する。
    """
    combined = []
    for link_id in link_ids:
        rb = runbooks.get(link_id)
        ann = annotations.get(link_id)
        loc = locations.get(link_id)
        candidates = loc.candidate_list if loc else []
        primary = candidates[0] if candidates else {}
        combined.append(
            {
                "link_id": link_id,
                "item_text": rb.item_text if rb else None,
                "source_category": loc.primary_source_category if loc else SourceCategory.MANUAL,
                "location": loc.primary_location if loc else None,
                # 主たる所在の機械可読情報（op 引数の決定的導出の素材）
                "unit_id": primary.get("unit_id"),
                "connection_id": primary.get("connection_id"),
                "payload": primary.get("payload"),
                # 優先順の全候補（多ソースのプログラムを step 単位で典拠づけるため）
                "candidates": candidates,
                "annotation": ann.annotation if ann else None,
                # 以前は action_type が無検証だったので、API 経由で語彙外の値が
                # 入った行が残っている可能性がある。プロンプトは「タグが付いている」前提で
                # 写し方を指示するので、未定義の値は落として渡す（自由文は残る）。
                "steps": _steps_with_known_tags(rb.steps if rb else []),
            }
        )
    return combined


async def _upsert_plan(
    questionnaire_id: int,
    link_id: str,
    item_text: str | None,
    program: dict[str, Any],
    status: str,
    llm_model: str | None,
    existing: dict[str, InputPlan],
    db: AsyncSession,
    confirmed_by_id: int | None = None,
    confirmed_at: datetime | None = None,
) -> InputPlan:
    plan = existing.get(link_id)
    if plan:
        plan.item_text = item_text
        plan.program = program
        plan.status = status
        plan.confirmed_by_id = confirmed_by_id
        plan.confirmed_at = confirmed_at
        _force_write_columns(plan)
        if llm_model is not None:
            plan.llm_model = llm_model
        return plan
    plan = InputPlan(
        questionnaire_id=questionnaire_id,
        link_id=link_id,
        item_text=item_text,
        program=program,
        status=status,
        llm_model=llm_model,
        confirmed_by_id=confirmed_by_id,
        confirmed_at=confirmed_at,
    )
    db.add(plan)
    return plan


@router.get("/questionnaires/{questionnaire_id}/plan", response_class=HTMLResponse)
async def plan_wizard(
    request: Request,
    questionnaire_id: int,
    current_user: RequireDesigner,
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """ステップ3: 手順書→取得プランの作成＋1項目ずつ確認するウィザード画面"""
    questionnaire = await _get_questionnaire_or_404(questionnaire_id, db)
    runbooks = await _runbook_map(questionnaire_id, db)
    plans = await _plan_map(questionnaire_id, db)

    items = _build_plan_items(runbooks, plans)

    csrf_token, signed_token = stable_csrf_tokens(request, csrf_protect)
    response = templates.TemplateResponse(
        "ai_input/plan.html",
        {
            "request": request,
            "current_user": current_user,
            "questionnaire": questionnaire,
            "current_stage": 3,
            "items": items,
            "status_labels": PlanStatus.LABELS,
            "op_labels": OpType.LABELS,
            "default_ollama_model": design_llm_model(),
            "csrf_token": csrf_token,
        },
    )
    if signed_token is not None:
        csrf_protect.set_csrf_cookie(signed_token, response)
    return response


@router.post("/questionnaires/{questionnaire_id}/plan/compile")
async def compile_plans(
    request: Request,
    questionnaire_id: int,
    payload: PlanCompileRequest,
    current_user: RequireDesigner,
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """ステップ3: 確認済み手順書の取得プランを一括作成する（draft保存）

    既に confirmed の plan は人の確認を尊重して上書きしない。
    """
    await csrf_protect.validate_csrf(request)
    if not settings.enable_ai_drafting:
        return _drafting_disabled_response()
    questionnaire = await _get_questionnaire_or_404(questionnaire_id, db)
    model = (payload.model or "").strip() or None

    runbooks = await _runbook_map(questionnaire_id, db)
    annotations = await _annotation_map(questionnaire_id, db)
    locations = await _location_map(questionnaire_id, db)
    plans = await _plan_map(questionnaire_id, db)

    # 確認済み手順書 かつ（未作成 または draft plan）の項目のみ対象。
    # **対象から外した項目は理由ごとに応答へ出す**。どちらも意図した振る舞い
    # （確認済みの plan は人の確認を尊重して上書きしない）だが、その事実が応答に無いと、
    # `compiled` を全体の件数と読んで数が食い違い、表に残った前回の行を今回の結果として数える。
    target_link_ids: list[str] = []
    skipped_runbook_unconfirmed: list[str] = []
    skipped_confirmed_plan: list[str] = []
    # 手順書の行そのものが無い項目（上のループは `runbooks` を走るので、ここに入らない）
    skipped_no_runbook = [
        f["link_id"] for f in _editable_fields(questionnaire) if f["link_id"] not in runbooks
    ]
    # この実行より前から表にある行。**今回書き換えなかった行**を後で数える（事後の検算）
    plan_rows_before = set(plans)
    for link_id, rb in runbooks.items():
        if rb.status != RunbookStatus.CONFIRMED:
            skipped_runbook_unconfirmed.append(link_id)
            continue
        plan = plans.get(link_id)
        if plan is not None and plan.status == PlanStatus.CONFIRMED:
            skipped_confirmed_plan.append(link_id)
            continue
        target_link_ids.append(link_id)

    if not target_link_ids:
        return JSONResponse(
            {
                "success": False,
                "message": "作成対象がありません（確認済み手順書が無い、または全プラン確認済み）",
                "skipped_no_runbook": skipped_no_runbook,
                "skipped_runbook_unconfirmed": skipped_runbook_unconfirmed,
                "skipped_confirmed_plan": skipped_confirmed_plan,
            },
            status_code=400,
        )

    combined = _compile_inputs(target_link_ids, runbooks, annotations, locations)
    # **LLM を呼ぶ前の版と書き込みに要る値を控える**。書き込みの直前に読み直した版と比べ、
    # 生成中に別の要求が確認・編集した項目は上書きしない（`_lock_rows_for_write`）。
    versions_before = {lid: plan_version(plans.get(lid)) for lid in target_link_ids}
    item_texts = {lid: runbooks[lid].item_text for lid in target_link_ids}
    user_id = current_user.id

    compiler = AIPlanCompiler()
    try:
        recorder = DesignLlmRecorder(DesignRole.PLAN)
        generated = await compiler.compile(combined, model=model, recorder=recorder)
    except AIServiceException as e:
        # 失敗した呼び出しも来歴に残す。何を渡して何が返らなかったかは、
        # 成功時と同じくらい後から必要になる。
        await save_design_llm_calls_safely(
            db,
            questionnaire_id=questionnaire_id,
            recorder=recorder,
            created_by_id=user_id,
        )
        return JSONResponse({"success": False, "message": e.message}, status_code=500)
    await save_design_llm_calls_safely(
        db,
        questionnaire_id=questionnaire_id,
        recorder=recorder,
        created_by_id=user_id,
    )

    used_model = model or design_llm_model()
    generated_map = {g.link_id: g for g in generated}
    # 二段ガードの1段目（登録時の早期拒否）: 自動構造化AIが無効の構成では
    # LLM op を含む program を draft 保存しない（実行段階で必ず拒否されるため）。
    rejected_llm: list[str] = []
    # 生成直後に検証結果を見せる。エラーがあっても draft の保存自体は
    # 続ける（修正の起点として残す方が有用）。確定（confirm=true）時の検証は save_plan
    # が従来どおり行う。
    validation_errors: dict[str, list[str]] = {}
    compiled_link_ids: list[str] = []
    # step が1つも無いプログラム。保存はするが「取得の設計ができた」とは別に数える
    empty_programs: list[str] = []
    to_write: dict[str, dict[str, Any]] = {}
    for link_id in target_link_ids:
        g = generated_map.get(link_id)
        if not g:
            continue
        program = _program_dump(g.program)
        if not settings.enable_ai_extraction and _llm_step_ids(program):
            rejected_llm.append(link_id)
            continue
        to_write[link_id] = program

    # 生成中に別の操作で行が変わった（確認・編集・同時の新規作成）ため書かなかった項目
    skipped_conflict: list[str] = []
    for _attempt in range(_GENERATION_WRITE_ATTEMPTS):
        current = await _lock_rows_for_write(db, InputPlan, questionnaire_id, list(to_write))
        compiled_link_ids, skipped_conflict = [], []
        for link_id, program in to_write.items():
            if plan_version(current.get(link_id)) != versions_before[link_id]:
                skipped_conflict.append(link_id)
                continue
            await _upsert_plan(
                questionnaire_id,
                link_id,
                item_texts[link_id],
                program,
                PlanStatus.DRAFT,
                used_model,
                current,
                db,
            )
            compiled_link_ids.append(link_id)
        try:
            await db.commit()
            break
        except IntegrityError as e:
            # 「無い」と読んだ項目を別の要求が先に作った。読み直すとその行が見えるので、
            # 次の周ではその項目が版の不一致として skip に回る。
            await db.rollback()
            if not _is_unique_violation(e):
                raise
    else:
        return _generation_conflict_response(None)
    saved = len(compiled_link_ids)
    # 検証結果と空プログラムは**保存した項目について**数える（skip した項目は保存していない）
    for link_id in compiled_link_ids:
        errors = validate_program(to_write[link_id])
        if errors:
            validation_errors[link_id] = errors
        if is_empty_program(to_write[link_id]):
            empty_programs.append(link_id)
    # **未試行を別掲する**。この経路の「未試行」は**プランを生成していない**
    # （再実行すれば対象に入る）で、「生成したが LLM op を含むので保存しなかった」
    # （`rejected_llm`）とは別である。混ぜると、押し直して埋まる項目と手順書を直さないと
    # 埋まらない項目が同じ数に見える。
    unattempted = [lid for lid in target_link_ids if lid not in generated_map]
    logger.info(
        f"取得プラン作成保存完了: questionnaire_id={questionnaire_id}, {saved}件, "
        f"LLM op拒否{len(rejected_llm)}件, 検証エラー{len(validation_errors)}件, "
        f"未試行{len(unattempted)}件, model={used_model}"
    )
    messages = []
    if validation_errors:
        messages.append(
            f"{len(validation_errors)}件は検証エラーがあります（保存はされています）: "
            f"{', '.join(sorted(validation_errors))}"
        )
    if rejected_llm:
        messages.append(
            f"{len(rejected_llm)}件は llm_extract / llm_judge を含むため保存しませんでした"
            f"（自動構造化AIが無効: ENABLE_AI_EXTRACTION=false）。"
            f"手順書を決定的な取得手順に修正して再作成してください: {', '.join(rejected_llm)}"
        )
    if unattempted:
        messages.append(
            f"{len(unattempted)}項目はプランが返りませんでした（未試行）。"
            # **対象集合を偽って書かない**。対象は「確認済み手順書 かつ
            # （未作成 **または draft**）」なので、押し直すと draft のプランも作り直される。
            "もう一度実行すると、未作成の項目と draft のプランが対象になります"
        )
    if empty_programs:
        messages.append(
            f"{len(empty_programs)}件は step が空のプログラムです（手入力への縮退、または"
            f"取得手順を作れなかった）。取得の設計としては数えないでください: "
            f"{', '.join(sorted(empty_programs))}"
        )
    if skipped_conflict:
        messages.append(
            f"{len(skipped_conflict)}件は作成中に別の操作で更新されたため、"
            f"作成結果を保存していません: {', '.join(skipped_conflict)}"
        )
    if skipped_confirmed_plan:
        messages.append(f"{len(skipped_confirmed_plan)}件は確認済みのため作り直していません。")
    if skipped_runbook_unconfirmed:
        messages.append(f"{len(skipped_runbook_unconfirmed)}件は手順書が未確認のため対象外です。")
    if skipped_no_runbook:
        messages.append(f"{len(skipped_no_runbook)}件は手順書が無いため対象外です。")
    return JSONResponse(
        {
            "success": True,
            "compiled": saved,
            "compiled_link_ids": compiled_link_ids,
            "empty_program_link_ids": sorted(empty_programs),
            "rejected_llm": rejected_llm,
            "validation_errors": validation_errors,
            "unattempted": len(unattempted),
            "unattempted_link_ids": unattempted,
            # 対象に入らなかった項目。2つの条件を混ぜない。`compiled` は**対象の
            # 中の**件数で、表の行数ではない（表には今回書き換えなかった行が残る）。
            "skipped_no_runbook": skipped_no_runbook,
            "skipped_runbook_unconfirmed": skipped_runbook_unconfirmed,
            "skipped_confirmed_plan": skipped_confirmed_plan,
            # 作成中に別の操作で確認・編集・作成されたため、作成結果を書かなかった項目
            "skipped_conflict": skipped_conflict,
            # **表にあるが今回書き換えなかった行**（事後の検算）。件数を行数で数える
            # 集計は、これを別に数えれば古い行を今回の結果に混ぜない。
            "rows_not_rewritten": sorted(plan_rows_before - set(compiled_link_ids)),
            "message": "\n".join(messages) or None,
            "model": used_model,
        }
    )


@router.post("/questionnaires/{questionnaire_id}/plan/{link_id}/recompile")
async def recompile_plan(
    request: Request,
    questionnaire_id: int,
    link_id: str,
    payload: PlanCompileRequest,
    current_user: RequireDesigner,
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """ステップ3: 単一項目の取得プランを再作成する（上書きし draft に戻す）"""
    await csrf_protect.validate_csrf(request)
    if not settings.enable_ai_drafting:
        return _drafting_disabled_response()
    await _get_questionnaire_or_404(questionnaire_id, db)
    model = (payload.model or "").strip() or None

    runbooks = await _runbook_map(questionnaire_id, db)
    rb = runbooks.get(link_id)
    if rb is None or rb.status != RunbookStatus.CONFIRMED:
        return JSONResponse(
            {"success": False, "message": "確認済み手順書がありません"},
            status_code=400,
        )

    annotations = await _annotation_map(questionnaire_id, db)
    locations = await _location_map(questionnaire_id, db)
    plans = await _plan_map(questionnaire_id, db)
    user_id = current_user.id

    combined = _compile_inputs([link_id], runbooks, annotations, locations)
    # LLM を呼ぶ前の版。書き込みの直前に読み直した版と違えば 409（上書きしない）
    version_before = plan_version(plans.get(link_id))
    item_text = rb.item_text
    compiler = AIPlanCompiler()
    try:
        recorder = DesignLlmRecorder(DesignRole.PLAN)
        generated = await compiler.compile(combined, model=model, recorder=recorder)
    except AIServiceException as e:
        # 失敗した呼び出しも来歴に残す。何を渡して何が返らなかったかは、
        # 成功時と同じくらい後から必要になる。
        await save_design_llm_calls_safely(
            db,
            questionnaire_id=questionnaire_id,
            recorder=recorder,
            created_by_id=user_id,
        )
        return JSONResponse({"success": False, "message": e.message}, status_code=500)
    await save_design_llm_calls_safely(
        db,
        questionnaire_id=questionnaire_id,
        recorder=recorder,
        created_by_id=user_id,
    )

    used_model = model or design_llm_model()
    g = next((x for x in generated if x.link_id == link_id), None)
    if g is None:
        # **未試行を「空のプログラム」として保存しない**。以前はここで
        # `{"steps": [], "return": None}` を draft 保存しており、**確認済みプランを
        # 空で上書きしていた**（生成結果ではなく応答漏れなのに、結果として記録される）。
        logger.warning(
            f"取得プラン再作成: 応答にこの項目が含まれなかった（未試行）: "
            f"questionnaire_id={questionnaire_id}, link_id={link_id}"
        )
        return JSONResponse(
            {
                "success": False,
                "message": (
                    "AIの応答にこの項目が含まれませんでした（未試行）。"
                    "既存のプランは変更していません。もう一度実行してください"
                ),
            },
            status_code=502,
        )
    program = _program_dump(g.program)

    # 二段ガードの1段目（登録時の早期拒否）
    if not settings.enable_ai_extraction and _llm_step_ids(program):
        return JSONResponse(
            {
                "success": False,
                "message": (
                    "再作成結果が llm_extract / llm_judge を含むため保存しません"
                    "（自動構造化AIが無効: ENABLE_AI_EXTRACTION=false）。"
                    "手順書を決定的な取得手順に修正してください"
                ),
            },
            status_code=400,
        )

    # 生成直後に検証結果を見せる。エラーがあっても draft の保存自体は
    # 続ける（修正の起点として残す方が有用）。確定（confirm=true）時の検証は save_plan
    # が従来どおり行う。
    errors = validate_program(program)

    # **生成中に確認・編集された行へ書かない**。呼び出し前に読んだオブジェクトではなく、
    # 行ロック付きで読み直した行の版を比べる（`_lock_rows_for_write`）。
    current = await _lock_rows_for_write(db, InputPlan, questionnaire_id, [link_id])
    current_version = plan_version(current.get(link_id))
    if current_version != version_before:
        await db.rollback()
        logger.warning(
            f"取得プラン再作成: 生成中に行が更新されたため保存しない: "
            f"questionnaire_id={questionnaire_id}, link_id={link_id}"
        )
        return _generation_conflict_response(current_version)

    saved = await _upsert_plan(
        questionnaire_id,
        link_id,
        item_text,
        program,
        PlanStatus.DRAFT,
        used_model,
        current,
        db,
    )
    conflict = await _commit_or_conflict(db)
    if conflict is not None:
        return conflict
    logger.info(
        f"取得プラン再作成: questionnaire_id={questionnaire_id}, link_id={link_id}, "
        f"検証エラー{len(errors)}件, model={used_model}"
    )
    return JSONResponse(
        {
            "success": True,
            "link_id": link_id,
            "program": program,
            "status": PlanStatus.DRAFT,
            "needs_input": has_needs_input(program),
            "errors": errors,
            "model": used_model,
            "version": plan_version(saved),
        }
    )


# **`/plan/{link_id}` より前に登録する。** 後に置くと `link_id="graph"` として
# `save_plan` が拾う（`/plan/compile` が既に同じ理由でここより前にある）。
@router.post("/questionnaires/{questionnaire_id}/plan/graph")
async def plan_graph(
    request: Request,
    questionnaire_id: int,
    payload: PlanSaveRequest,
    current_user: RequireDesigner,
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """ステップ3: 編集中の program の取得フロー図（Mermaid 定義）を返す。

    **図の規則の正本は `viz.program_to_mermaid` ただ1つである。** 以前は取得プラン編集
    画面のプレビューがクライアント側（`plan.html` の `buildMermaid`）で図を組み直して
    いたため、同じ program に対して**プレビューと検証画面が違う図を出していた**。

    - `input` を全 op から辺にしていた（`viz.py` が取り除いた幻エッジ。
      `input` を実際に読むのは `transform` だけで、他の op は prompt 内の `$id` 展開で
      前段の値を受け取る）
    - `params` 経由の依存を描かなかった
    - `csv_extract` のノード形状が抽出系（角丸）になっていなかった
    - `reasoning`（📝 設計根拠）を注記しなかった

    設計者はプレビューを見て確定し、監査者は別の規則で描かれた図を見る——という状態は
    監査図を実データフローに忠実に保つという目的を半分しか満たしていない。

    **保存と同じ `PlanSaveRequest` を通す**ので、プレビューが描くのは「いま保存したら
    保存される program」である（スキーマが落とすキーはプレビューでも落ちる）。
    **DB へは書かない**（読むのは `_get_questionnaire_or_404` の存在確認だけ）。
    通すのはロールと CSRF の関門である。
    """
    await csrf_protect.validate_csrf(request)
    # 図の組み立ては同期処理なので、step 数に上限を置く（保存時の validate_program と同じ値）。
    # 上限なしだと、巨大な program 1つの POST でワーカーのイベントループが止まる。
    if len(payload.program.steps) > MAX_STEPS:
        raise HTTPException(
            status_code=422,
            detail=f"step 数が上限（{MAX_STEPS}）を超えています: {len(payload.program.steps)}",
        )
    # `op` は図のラベルに入る。語彙は英小文字と `_` だけなので、それ以外（先頭のバッククォートなど
    # Mermaid の構文になりうる文字、空文字）は図にせず 422 にする。
    bad_ops = [
        st.op for st in payload.program.steps if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", st.op)
    ]
    if bad_ops:
        raise HTTPException(status_code=422, detail=f"op が不正です: {bad_ops[0]!r}")
    await _get_questionnaire_or_404(questionnaire_id, db)
    return JSONResponse({"mermaid": program_to_mermaid(_program_dump(payload.program))})


@router.post("/questionnaires/{questionnaire_id}/plan/{link_id}")
async def save_plan(
    request: Request,
    questionnaire_id: int,
    link_id: str,
    payload: PlanSaveRequest,
    current_user: RequireDesigner,
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
    confirm: bool = False,
) -> JSONResponse:
    """ステップ3: 1項目分の取得プランを保存する（upsert）

    `confirm=true` のときは検証を通し、program が妥当かつ needs_input が残っていなければ
    status を confirmed にする。検証エラーや needs_input 残存時は 400 で拒否する。
    **確認には手順書が確認済みであることを要する**（`compile` / `recompile` と同じ前提）。

    `confirm=false` の保存で確認済みプランの program が変わったら **draft へ戻し、確認者・
    確認日時を消す**（`recompile` と同じ扱い）。確認済みのまま残すと、誰も承認していない
    program が元の確認者の名前で実行される。正準形が同一の保存だけは確認済みを保つ。
    """
    await csrf_protect.validate_csrf(request)
    await _get_questionnaire_or_404(questionnaire_id, db)

    runbooks = await _runbook_map(questionnaire_id, db)
    rb = runbooks.get(link_id)
    if rb is None:
        raise ResourceNotFoundException("InputRunbook", link_id)

    plans = await _plan_map(questionnaire_id, db)
    existing = plans.get(link_id)
    await _lock_row(db, existing)
    conflict = _version_conflict(payload, plan_version(existing), required=confirm)
    if conflict is not None:
        return conflict

    program = _program_dump(payload.program)
    errors = validate_program(program)
    if errors:
        return JSONResponse(
            {"success": False, "message": "取得プラン検証エラー", "errors": errors},
            status_code=400,
        )

    # 二段ガードの1段目（登録時の早期拒否）: 手動保存でも LLM op は通さない
    llm_steps = _llm_step_ids(program)
    if llm_steps and not settings.enable_ai_extraction:
        return JSONResponse(
            {
                "success": False,
                "message": (
                    "llm_extract / llm_judge を含むプランは保存できません"
                    f"（自動構造化AIが無効: ENABLE_AI_EXTRACTION=false）: {', '.join(llm_steps)}"
                ),
            },
            status_code=400,
        )

    if not confirm and existing is not None and not _program_changed(existing.program, program):
        # 内容が同一の保存は何も書かない（status・確認者・更新日時を動かさない）
        return JSONResponse(
            {
                "success": True,
                "link_id": link_id,
                "status": existing.status,
                "unchanged": True,
                "version": plan_version(existing),
            }
        )

    demoted = False
    if confirm:
        # 手順書が未確認のまま取得プランだけを確定させない。compile / recompile は
        # 確認済み手順書しか扱わず、ウィザードも確認済み手順書の項目しか出さない。
        # この経路だけが前提を飛ばせた。
        if rb.status != RunbookStatus.CONFIRMED:
            return JSONResponse(
                {
                    "success": False,
                    "message": "手順書が確認済みでないため取得プランを確認できません",
                },
                status_code=400,
            )
        if has_needs_input(program):
            return JSONResponse(
                {
                    "success": False,
                    "message": "未確定（要入力）のステップが残っているため確認できません",
                },
                status_code=400,
            )
        status = PlanStatus.CONFIRMED
        # 確認操作者・日時を記録（ALCOA+ 帰属性）
        confirmed_by_id: int | None = current_user.id
        confirmed_at: datetime | None = datetime.now(UTC)
    else:
        # ここに来るのは内容が変わった保存だけ（同一の保存は上で返している）。
        # 確認済みなら draft へ戻す（recompile と同じ）。人が承認したのは変更前の program
        # であり、変更後の program ではない。
        demoted = bool(existing and existing.status == PlanStatus.CONFIRMED)
        status = PlanStatus.DRAFT
        confirmed_by_id = None
        confirmed_at = None

    saved = await _upsert_plan(
        questionnaire_id,
        link_id,
        rb.item_text,
        program,
        status,
        None,
        plans,
        db,
        confirmed_by_id=confirmed_by_id,
        confirmed_at=confirmed_at,
    )
    conflict = await _commit_or_conflict(db)
    if conflict is not None:
        return conflict
    logger.info(
        f"取得プラン保存: questionnaire_id={questionnaire_id}, link_id={link_id}, status={status}"
        + ("（内容変更により確認済みから draft へ戻した）" if demoted else "")
    )
    response: dict[str, Any] = {
        "success": True,
        "link_id": link_id,
        "status": status,
        "version": plan_version(saved),
    }
    if demoted:
        response["demoted"] = True
        response["warning"] = (
            "確認済みの取得プランを変更したため未確認（draft）に戻しました。"
            "実行するには内容を確かめて確認し直してください"
        )
    if confirm and is_empty_program(program):
        # **step が空のプランは確定できる**（手入力への縮退という正当な形）が、**人が気付かずに
        # 確定すると、取得の設計ができたかのように母集団へ入る**。止めずに告げる。
        response["empty_program"] = True
        response["warning"] = (
            "step が1つも無いプログラムを確認済みにしました。実行すると手入力（needs_input）"
            "になります。取得手順を作れなかった項目ではないか確かめてください"
        )
    return JSONResponse(response)


@router.post("/questionnaires/{questionnaire_id}/plan/{link_id}/demote")
async def demote_plan(
    request: Request,
    questionnaire_id: int,
    link_id: str,
    payload: PlanDemoteRequest,
    current_user: RequireDesigner,
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """ステップ3: 確認済みの取得プランを draft へ戻す（program は変えない）。

    手順書が draft へ戻った項目のプランは画面で読み取り専用になるが、確認済みのままでは
    実行され続ける。その項目を止める手段として、program に触らず確認だけを取り消す。
    確認者・確認日時はクリアする（`recompile` と同じ）。既に draft なら何もしない。
    """
    await csrf_protect.validate_csrf(request)
    await _get_questionnaire_or_404(questionnaire_id, db)

    plans = await _plan_map(questionnaire_id, db)
    plan = plans.get(link_id)
    if plan is None:
        raise ResourceNotFoundException("InputPlan", link_id)
    await _lock_row(db, plan)
    conflict = _version_conflict(payload, plan_version(plan), required=True)
    if conflict is not None:
        return conflict

    demoted = plan.status == PlanStatus.CONFIRMED
    if demoted:
        plan.status = PlanStatus.DRAFT
        plan.confirmed_by_id = None
        plan.confirmed_at = None
        await db.commit()
        logger.info(
            f"取得プランを下書きへ戻した: questionnaire_id={questionnaire_id}, link_id={link_id}"
        )
    return JSONResponse(
        {
            "success": True,
            "link_id": link_id,
            "status": PlanStatus.DRAFT,
            "demoted": demoted,
            "version": plan_version(plan),
        }
    )


# =============================================================================
# ステップ4: AI自動入力
# =============================================================================


@router.get("/questionnaires/{questionnaire_id}/autofill", response_class=HTMLResponse)
async def ai_autofill_mode(
    request: Request,
    questionnaire_id: int,
    # 画面も POST（試行＝設計者 / 本番＝収集者）と同じ関門
    current_user: RequireAutofillRunner,
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """ステップ4: AI自動入力画面"""
    questionnaire = await _get_questionnaire_or_404(questionnaire_id, db)

    plans = await _plan_map(questionnaire_id, db)
    confirmed = {link_id: p for link_id, p in plans.items() if p.status == PlanStatus.CONFIRMED}

    generator = FormGenerator(questionnaire.content)
    form_fields = generator.generate_form_fields()

    csrf_token, signed_token = stable_csrf_tokens(request, csrf_protect)
    response = templates.TemplateResponse(
        "ai_input/autofill.html",
        {
            "request": request,
            "current_user": current_user,
            "questionnaire": questionnaire,
            "current_stage": 4,
            "form_fields": form_fields,
            "plans_map": confirmed,
            "has_plans": len(confirmed) > 0,
            "op_labels": OpType.LABELS,
            "default_ollama_model": design_llm_model(),
            "csrf_token": csrf_token,
        },
    )
    if signed_token is not None:
        csrf_protect.set_csrf_cookie(signed_token, response)
    return response


async def _autofill_patient_allowed(db: AsyncSession, user: User, patient_id: str) -> bool:
    """autofill でその患者を対象にしてよいか。

    以前は `not _is_trial_run(user) and not await user_can_access_patient(...)` と書いてあり、
    **設計者のアクティブ役割なら患者スコープ認可が一切評価されなかった**。
    `AutofillRequest.patient_id` は攻撃者が選べるので、患者未割当のユーザが確定プランのある
    Questionnaire について**任意患者の抽出済み臨床データと evidence** を応答／NDJSON
    ストリームで受け取れた（CWE-639）。

    **単純に強制はできない**——設計者は患者割り当てを持たない設計なので、
    `user_can_access_patient` をそのまま課すと trial-run が使えなくなる。そこで
    **試行は合成・ホワイトリスト患者に限る**（`settings.trial_run_patient_ids`）。
    割り当てを持つ患者は従来どおり通るので、運用上の後退は無い。

    - 本番（`collector`）: `user_can_access_patient`（その患者の対象に有効なリースを持つこと）のみ
    - 試行（`designer`）: ホワイトリストにある患者、または有効なリースのある患者
    - `admin`: この経路に入れない（`RequireAutofillRunner` に含まれない。患者データを見ない）
    """
    if _is_trial_run(user) and patient_id in settings.trial_run_patient_id_set:
        return True
    allowed = await user_can_access_patient(db, user, patient_id)
    if allowed:
        # 保持者の操作（autofill 実行）でリースを延長する
        await extend_leases_for_patient(db, user.id, patient_id)
    return allowed


def _is_trial_run(user: User) -> bool:
    """autofill が試行(trial)実行か判定する。

    アクティブ役割が設計者なら試行＝結果を永続しない揮発プレビュー。収集者なら本番＝永続。
    実行者の同一性や割り当てでなく「実行コンテキスト＝アクティブ役割」で決まる。
    """
    return user.active_role == RoleType.DESIGNER


async def _record_autofill_run(
    db: AsyncSession,
    *,
    is_trial: bool,
    questionnaire_id: int,
    patient_id: str,
    user: User,
    prefill_data: dict[str, Any],
    evidence: dict[str, Any],
    status: str,
    error_summary: str | None,
    program_snapshot: dict[str, Any] | None = None,
) -> int | None:
    """autofill 実行の記録を永続する。

    本番(収集者): InputExecution に prefill_data / evidence ごと永続し execution_id を返す。
    試行(設計者): PHI を含まない InputTrialRun（メタのみ）を残し、None を返す
    （prefill_data / evidence は呼び出し側がレスポンスで返すのみで永続しない）。
    """
    if is_trial:
        db.add(
            InputTrialRun(
                questionnaire_id=questionnaire_id,
                patient_id=patient_id,
                executed_by_id=user.id,
                acted_as=user.active_role,
                status=status,
                filled_count=len(prefill_data),
            )
        )
        await db.commit()
        return None

    execution = InputExecution(
        questionnaire_id=questionnaire_id,
        patient_id=patient_id,
        executed_by_id=user.id,
        prefill_data=prefill_data,
        evidence=evidence,
        program_snapshot=program_snapshot,
        status=status,
        error_summary=error_summary,
    )
    db.add(execution)
    await db.commit()
    await db.refresh(execution)
    return execution.id


@router.post("/questionnaires/{questionnaire_id}/autofill")
async def execute_autofill(
    request: Request,
    questionnaire_id: int,
    payload: AutofillRequest,
    current_user: RequireAutofillRunner,
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> JSONResponse:
    """ステップ4: AI自動入力を実行する"""
    await csrf_protect.validate_csrf(request)
    questionnaire = await _get_questionnaire_or_404(questionnaire_id, db)

    patient_id = payload.patient_id.strip()
    model = (payload.model or "").strip() or None
    if not patient_id:
        return JSONResponse(
            {"success": False, "message": "患者IDを指定してください"}, status_code=400
        )

    # 患者割り当てスコープ: 本番(collector)実行は担当患者に限定する。
    # **試行(設計者)も無条件では通さない**——合成・ホワイトリスト患者か、割り当てのある
    # 患者に限る（以前は設計ロールなら患者スコープ認可が一切評価されなかった）。
    if not await _autofill_patient_allowed(db, current_user, patient_id):
        return JSONResponse(
            {
                "success": False,
                "message": "この患者は担当ではありません（「次の患者を受け取る」で受け取った患者のみ実行できます）",
            },
            status_code=403,
        )

    result = await db.execute(
        select(InputPlan).where(
            InputPlan.questionnaire_id == questionnaire_id,
            InputPlan.status == PlanStatus.CONFIRMED,
        )
    )
    plans = list(result.scalars().all())

    if not plans:
        return JSONResponse(
            {
                "success": False,
                "message": "実行可能な確認済みプランがありません。先にステップ3（手順書→DSL）でコンパイル・確認してください",
            },
            status_code=400,
        )

    # 二段ガードの2段目（実行開始時の権威ガード）: フラグ変更や他構成DBの持ち込みが
    # あっても「この構成でLLMが患者データに触れた実行は存在しない」を不変条件にする。
    if not settings.enable_ai_extraction:
        llm_plans = {p.link_id: _llm_step_ids(p.program or {}) for p in plans}
        llm_plans = {k: v for k, v in llm_plans.items() if v}
        if llm_plans:
            return JSONResponse(
                {
                    "success": False,
                    "message": (
                        "llm_extract / llm_judge を含む確認済みプランがあるため実行できません"
                        "（自動構造化AIが無効: ENABLE_AI_EXTRACTION=false）。"
                        f"対象項目: {', '.join(sorted(llm_plans))}"
                    ),
                },
                status_code=403,
            )

    interpreter = DSLInterpreter()
    status = ExecutionStatus.SUCCESS
    error_summary = None

    try:
        prefill_data, evidence = await interpreter.execute(
            plans,
            patient_id,
            db,
            llm_model_override=model,
            study_id=payload.study_id.strip(),
            questionnaire_content=questionnaire.content,
        )
    except Exception as e:
        logger.error(f"AI自動入力実行エラー: questionnaire_id={questionnaire_id}: {e}")
        prefill_data, evidence = {}, {}
        status = ExecutionStatus.FAILED
        error_summary = str(e)

    if prefill_data and len(prefill_data) < len(plans):
        status = ExecutionStatus.PARTIAL

    # 監査刻印: どの役割として実行したか（acted_as）を証跡に残す。
    # evidence は link_id をキーに持つため、予約キー _meta に格納する。
    evidence = {
        **evidence,
        "_meta": {
            "acted_as": current_user.active_role,
            "executed_by": current_user.full_name,
        },
    }

    is_trial = _is_trial_run(current_user)
    execution_id = await _record_autofill_run(
        db,
        is_trial=is_trial,
        questionnaire_id=questionnaire_id,
        patient_id=patient_id,
        user=current_user,
        prefill_data=prefill_data,
        evidence=evidence,
        status=status,
        error_summary=error_summary,
        # 試行(trial)は結果を永続しないため snapshot も不要（全プラン複製の無駄を避ける）。
        program_snapshot=None if is_trial else program_snapshot(plans, model),
    )

    used_model = model or design_llm_model()
    # PHI 保護: 患者識別子はログに出さず、非PHIの execution_id で相関する。
    logger.info(
        f"AI自動入力完了({'試行' if is_trial else '本番'}): questionnaire_id={questionnaire_id}, "
        f"execution_id={execution_id}, status={status}, "
        f"補完={len(prefill_data)}件, model={used_model}"
    )

    return JSONResponse(
        {
            "success": True,
            "trial": is_trial,
            "prefill_data": prefill_data,
            "evidence": evidence,
            "status": status,
            "execution_id": execution_id,
            "model": used_model,
        }
    )


@router.post("/questionnaires/{questionnaire_id}/autofill/stream")
async def execute_autofill_stream(
    request: Request,
    questionnaire_id: int,
    payload: AutofillRequest,
    current_user: RequireAutofillRunner,
    csrf_protect: CsrfProtect = Depends(),
    db: AsyncSession = Depends(get_db),
) -> Response:
    """ステップ4: AI自動入力を実行し、項目ごとの進捗を NDJSON でストリームする。

    execute_autofill と同じガード・記録を行うが、結果を一括返却せず、プラン完了ごとに
    1行 JSON（item_start / item / done / error）を流す。フロントは到着順にフィールドを
    埋め、根拠（DSL実行トレース）を逐次描画できる。最終的に InputExecution を1件保存する。
    """
    await csrf_protect.validate_csrf(request)
    questionnaire = await _get_questionnaire_or_404(questionnaire_id, db)

    patient_id = payload.patient_id.strip()
    model = (payload.model or "").strip() or None
    if not patient_id:
        return JSONResponse(
            {"success": False, "message": "患者IDを指定してください"}, status_code=400
        )

    # 患者割り当てスコープ: 本番(collector)実行は担当患者に限定する。
    # **試行(設計者)も無条件では通さない**——合成・ホワイトリスト患者か、割り当てのある
    # 患者に限る（以前は設計ロールなら患者スコープ認可が一切評価されなかった）。
    if not await _autofill_patient_allowed(db, current_user, patient_id):
        return JSONResponse(
            {
                "success": False,
                "message": "この患者は担当ではありません（「次の患者を受け取る」で受け取った患者のみ実行できます）",
            },
            status_code=403,
        )

    result = await db.execute(
        select(InputPlan).where(
            InputPlan.questionnaire_id == questionnaire_id,
            InputPlan.status == PlanStatus.CONFIRMED,
        )
    )
    plans = list(result.scalars().all())
    if not plans:
        return JSONResponse(
            {
                "success": False,
                "message": "実行可能な確認済みプランがありません。先にステップ3（手順書→DSL）でコンパイル・確認してください",
            },
            status_code=400,
        )

    # 二段ガードの2段目（execute_autofill と同じ権威ガード）。
    if not settings.enable_ai_extraction:
        llm_plans = {p.link_id: _llm_step_ids(p.program or {}) for p in plans}
        llm_plans = {k: v for k, v in llm_plans.items() if v}
        if llm_plans:
            return JSONResponse(
                {
                    "success": False,
                    "message": (
                        "llm_extract / llm_judge を含む確認済みプランがあるため実行できません"
                        "（自動構造化AIが無効: ENABLE_AI_EXTRACTION=false）。"
                        f"対象項目: {', '.join(sorted(llm_plans))}"
                    ),
                },
                status_code=403,
            )

    interpreter = DSLInterpreter()
    used_model = model or design_llm_model()
    study_id = payload.study_id.strip()

    async def event_gen() -> AsyncIterator[bytes]:
        def _line(obj: dict[str, Any]) -> bytes:
            return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")

        prefill_data: dict[str, Any] = {}
        evidence: dict[str, Any] = {}
        status = ExecutionStatus.SUCCESS
        error_summary: str | None = None

        try:
            async for ev in interpreter.execute_stream(
                plans,
                patient_id,
                db,
                model,
                study_id=study_id,
                questionnaire_content=questionnaire.content,
            ):
                if ev.get("type") == "item":
                    link_id = ev["link_id"]
                    if ev.get("value") is not None:
                        prefill_data[link_id] = ev["value"]
                    item_ev: dict[str, Any] = {
                        "trace": ev.get("trace", []),
                        "value": ev.get("value"),
                    }
                    if ev.get("error"):
                        item_ev["error"] = ev["error"]
                    if ev.get("failure_code"):
                        item_ev["failure_code"] = ev["failure_code"]
                    for key in ("rejected_value", "normalized_from"):
                        if key in ev:
                            item_ev[key] = ev[key]
                    evidence[link_id] = item_ev
                yield _line(ev)
        except Exception as e:  # noqa: BLE001
            logger.error(f"AI自動入力(stream)実行エラー: questionnaire_id={questionnaire_id}: {e}")
            status = ExecutionStatus.FAILED
            error_summary = str(e)
            yield _line({"type": "error", "message": str(e)})

        if status != ExecutionStatus.FAILED and prefill_data and len(prefill_data) < len(plans):
            status = ExecutionStatus.PARTIAL

        # 監査刻印（execute_autofill と同様。link_id 衝突回避のため _meta に格納）。
        evidence = {
            **evidence,
            "_meta": {
                "acted_as": current_user.active_role,
                "executed_by": current_user.full_name,
            },
        }
        is_trial = _is_trial_run(current_user)
        execution_id = await _record_autofill_run(
            db,
            is_trial=is_trial,
            questionnaire_id=questionnaire_id,
            patient_id=patient_id,
            user=current_user,
            prefill_data=prefill_data,
            evidence=evidence,
            status=status,
            error_summary=error_summary,
            # 試行(trial)は結果を永続しないため snapshot も不要（全プラン複製の無駄を避ける）。
            program_snapshot=None if is_trial else program_snapshot(plans, model),
        )
        # PHI 保護: 患者識別子はログに出さず、非PHIの execution_id で相関する。
        logger.info(
            f"AI自動入力(stream)完了({'試行' if is_trial else '本番'}): "
            f"questionnaire_id={questionnaire_id}, "
            f"execution_id={execution_id}, status={status}, "
            f"補完={len(prefill_data)}件, model={used_model}"
        )
        yield _line(
            {
                "type": "done",
                "trial": is_trial,
                "status": status,
                "execution_id": execution_id,
                "model": used_model,
                "filled": len(prefill_data),
                "total": len(plans),
            }
        )

    return StreamingResponse(
        event_gen(),
        media_type="application/x-ndjson",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# =============================================================================
# ステップ4: データ収集手続確認書（CSV = Computerized System Validation 文書）
# =============================================================================


@router.get(
    "/questionnaires/{questionnaire_id}/autofill/executions/{execution_id}/validation",
    response_class=HTMLResponse,
)
async def execution_validation_report(
    request: Request,
    questionnaire_id: int,
    execution_id: int,
    # 確認書は実行した人（設計者・収集者）と閲覧・検証者。管理者は患者データを見ない
    current_user: RequireValidationViewer,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """1回のステップ4実行について、各値の収集手続きを示す検証文書（HTML）を生成する

    誰が・いつ・何を・どこから・どう導出したかを、DSLプログラムの DAG グラフと
    step 単位の実行トレースで提示する。印刷してデータインテグリティの証跡とする。

    文書様式は settings.enable_ai_extraction で2系統に分かれる
    （フォルダレベルで独立、部品非共有。docs/ai_flags_spec.md §5）:
    - ai_enabled:  実行時LLM関与（モデル・プロンプト・生出力）を記録する様式
    - ai_disabled: 実行時LLM非関与（処理の決定性）を宣言する様式。実行トレースに
      llm_extract / llm_judge の痕跡がある場合は様式と矛盾するため生成を拒否する（409）

    設計過程のAI利用（候補探索: 所在AI案・手順書生成・プランコンパイル）は
    様式を分けず、両様式共通の開示欄に3層の設計系譜として記載する。
    人間レビュー済みの設計成果物の実行は決定的であり、決定性の宣言と矛盾しない。
    """
    questionnaire = await _get_questionnaire_or_404(questionnaire_id, db)

    result = await db.execute(
        select(InputExecution)
        .options(selectinload(InputExecution.executed_by))
        .where(
            InputExecution.id == execution_id,
            InputExecution.questionnaire_id == questionnaire_id,
        )
    )
    execution = result.scalar_one_or_none()
    if not execution:
        raise ResourceNotFoundException("InputExecution", execution_id)

    # IDOR 防止: 閲覧・検証者は全件、それ以外は自分が実行した記録のみ
    # 閲覧可。確認書は患者ID・抽出値・生LLM出力・トレース（PHI）を含むため。
    # 管理者は患者データを見ないので全件閲覧の側に入れない。
    if current_user.active_role != RoleType.REVIEWER:
        if execution.executed_by_id != current_user.id:
            raise HTTPException(
                status_code=403,
                detail="この実行記録の確認書を閲覧する権限がありません",
            )

    plans = await _plan_map(questionnaire_id, db)
    runbooks = await _runbook_map(questionnaire_id, db)
    locations = await _location_map(questionnaire_id, db)
    annotations = await _annotation_map(questionnaire_id, db)
    evidence = execution.evidence or {}
    prefill = execution.prefill_data or {}
    snapshot = execution.program_snapshot or {}
    # 監査メタ（acted_as 等）は予約キー _meta に入っているので分離する。
    audit_meta = evidence.get("_meta", {}) if isinstance(evidence, dict) else {}

    items: list[dict[str, Any]] = []
    for link_id, ev in evidence.items():
        if link_id.startswith("_"):
            continue
        plan = plans.get(link_id)
        # 監査図・トレースは「実行時に固定した program（設計根拠込み）」を優先する。
        # 実行後にプランが編集・再確認されても実行記録の再現性を保つため
        # （CSV / データインテグリティ: Original・Consistent）。スナップショット未保持の
        # 旧記録のみ現行 plan.program にフォールバックし、その旨を program_pinned=False で示す。
        snap = snapshot.get(link_id) if isinstance(snapshot, dict) else None
        if isinstance(snap, dict) and isinstance(snap.get("program"), dict):
            program = snap["program"]
            program_pinned = True
            snapshot_updated_at = snap.get("plan_updated_at")
        else:
            program = plan.program if plan else {"steps": [], "return": None}
            program_pinned = False
            snapshot_updated_at = None
        trace = ev.get("trace", []) if isinstance(ev, dict) else []
        rb = runbooks.get(link_id)
        loc = locations.get(link_id)
        ann = annotations.get(link_id)
        # 設計過程のAI利用開示（4層系譜）: 臨床的意味 → 所在候補のAI案由来
        # モデル群 → 手順書 → プラン。status=confirmed は人間確認を意味する
        # （ステップ4は確認済みプランのみ実行可）。
        location_llm_models = sorted(
            {str(c["llm_model"]) for c in (loc.candidate_list if loc else []) if c.get("llm_model")}
        )
        # 臨床的意味の来歴: 記述本体のモデル名と、典拠に付いたAI由来モデル名を合わせる
        clinical_llm_models = sorted(
            set(ann.citation_llm_models if ann else [])
            | ({ann.llm_model} if ann and ann.llm_model else set())
        )
        items.append(
            {
                "link_id": link_id,
                "item_text": plan.item_text if plan else link_id,
                "value": prefill.get(link_id, ev.get("value") if isinstance(ev, dict) else None),
                "trace": trace,
                "error": ev.get("error") if isinstance(ev, dict) else None,
                "mermaid": program_to_mermaid(program, trace),
                "program_pinned": program_pinned,
                "snapshot_updated_at": snapshot_updated_at,
                "llm_model": plan.llm_model if plan else None,
                "plan_status": plan.status if plan else None,
                "runbook_llm_model": rb.llm_model if rb else None,
                "runbook_status": rb.status if rb else None,
                "location_llm_models": location_llm_models,
                # guard が偽で実行されなかった LLM op。実行時LLM非関与の
                # 様式でも、プラン構成に LLM op を含む事実は宣言欄に開示する。
                "skipped_llm_steps": [
                    str(t.get("id") or t.get("op"))
                    for t in trace
                    if t.get("skipped") and t.get("op") in _LLM_OPS
                ],
                "clinical_llm_models": clinical_llm_models,
                "clinical_status": ann.status if ann else None,
                "clinical_citations": ann.citation_list if ann else [],
            }
        )

    # ID解決経路サマリ（key_type 別の解決状況）。生キー値は evidence に無く、
    # マスク値＋used_key_type のみ。KAWASEMI からの値入力経路は構造的に存在しない。
    id_resolution: dict[str, set[str]] = {}
    for ev in evidence.values():
        if not isinstance(ev, dict):
            continue
        for t in ev.get("trace", []):
            inp = t.get("inputs") or {}
            kt = inp.get("used_key_type")
            if kt:
                id_resolution.setdefault(str(kt), set()).add(str(inp.get("status") or "?"))
    id_resolution_sorted = {k: sorted(v) for k, v in sorted(id_resolution.items())}

    if not settings.enable_ai_extraction:
        # 自動構造化AI無効構成の様式は「実行時LLM非関与」を宣言する文書なので、
        # 実行トレースに LLM op の痕跡がある記録（他構成のDB持ち込み等）には適用
        # できない。隠して出すのではなく拒否する（三層防御の3層目）。
        # 設計時AI（LLMコンパイル済みプラン等）は実行の決定性と矛盾しないため
        # 拒否せず、開示欄に記載する。
        # guard が偽でスキップされた LLM op（skipped=true）は実際には呼び出されて
        # いないため「実行時LLM関与」には当たらない。
        # ただし llm ブロック（model / prompt / raw_output の呼び出し記録）の存在は
        # skipped の自己申告では免罪しない。このゲートは「他構成のDB持ち込み」に対する
        # 防御であり evidence を信用できない入力として扱うため、呼び出しの確定記録が
        # ある entry は常に関与とみなす（executor は skip 経路で op ハンドラを呼ばず、
        # skipped と llm が同時に付く entry を構造上生成しない）。
        has_llm_trace = any(
            any(
                t.get("llm") or (t.get("op") in _LLM_OPS and not t.get("skipped"))
                for t in it["trace"]
            )
            for it in items
        )
        if has_llm_trace:
            return HTMLResponse(
                "<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'>"
                "<title>確認書を生成できません - KAWASEMI</title></head><body>"
                "<h1>データ収集手続確認書を生成できません</h1>"
                f"<p>実行ID {execution.id} の記録には実行時LLM関与の痕跡"
                "（llm_extract / llm_judge の実行トレース）が含まれています。"
                "現在の構成は自動構造化AIが無効（ENABLE_AI_EXTRACTION=false）であり、"
                "実行時LLM非関与を宣言する様式とは整合しないため、本実行の確認書は生成しません。"
                "この実行記録の確認書が必要な場合は、実行時と同じ自動構造化AI有効構成で出力してください。</p>"
                "</body></html>",
                status_code=409,
            )

    # 設計過程AI利用のサマリ（開示欄の冒頭宣言用）: 1項目でもAI由来があれば true
    design_ai_used = any(
        it["clinical_llm_models"]
        or it["location_llm_models"]
        or it["runbook_llm_model"]
        or it["llm_model"]
        for it in items
    )

    # 設計ロールの LLM 来歴のサマリ。プロンプト全文は確認書に載せない
    # （文書が肥大する）が、**どの役割で何回呼び、切り詰めがあったか**は開示する。
    # モデル名だけでは「何を読んでその記述にしたか」を追えないため、記録の所在を示す。
    # **プロバイダも役割ごとに開示する**。ここに出さないと、別エンジンで測った run の
    # 確認書が Ollama の run と1バイトも変わらない——「どのエンジンがこの測定を出したか」に
    # 監査者が答えられないなら、列に記録しても比較可能性の役に立たない。
    provenance_rows = (
        await db.execute(
            select(
                DesignLlmCall.role,
                DesignLlmCall.provider,
                func.count(DesignLlmCall.id),
                func.sum(case((DesignLlmCall.truncated, 1), else_=0)),
                func.sum(case((DesignLlmCall.error.is_not(None), 1), else_=0)),
            )
            .where(DesignLlmCall.questionnaire_id == questionnaire.id)
            .group_by(DesignLlmCall.role, DesignLlmCall.provider)
        )
    ).all()
    design_llm_provenance = [
        {
            "role": role,
            "role_label": DesignRole.LABELS.get(role, role),
            "provider": provider,
            "calls": int(calls or 0),
            "truncated": int(truncated or 0),
            "failed": int(failed or 0),
        }
        for role, provider, calls, truncated, failed in provenance_rows
    ]

    # guard で未実行になった LLM op を持つ項目。実行事実としてはLLM非関与
    # だが「プランが決定的処理のみで構成される」わけではないため、決定性の宣言文で
    # 事実を書き分ける（宣言と実行トレースの齟齬を残さない）。
    guard_skipped_llm = {
        it["link_id"]: it["skipped_llm_steps"] for it in items if it["skipped_llm_steps"]
    }

    return templates.TemplateResponse(
        "ai_input/validation/validation.html",
        {
            "request": request,
            "current_user": current_user,
            "questionnaire": questionnaire,
            "execution": execution,
            "items": items,
            "audit_meta": audit_meta,
            "id_resolution": id_resolution_sorted,
            "status_labels": ExecutionStatus,
            "ai_enabled": settings.enable_ai_extraction,
            "design_ai_used": design_ai_used,
            "design_llm_provenance": design_llm_provenance,
            "guard_skipped_llm": guard_skipped_llm,
            "annotation_status_labels": AnnotationStatus.LABELS,
            "runbook_status_labels": RunbookStatus.LABELS,
            "plan_status_labels": PlanStatus.LABELS,
        },
    )


# =============================================================================
# 取得困難度メトリクス: 項目別成否・失敗理由の横断集計
# =============================================================================


def _parse_metrics_date(value: str | None) -> datetime | None:
    """期間指定（YYYY-MM-DD）をサーバのローカルタイムゾーンの0時として解釈する。

    施設運用（JST 等）の「日」の感覚と日境界を揃える（UTC 深夜解釈だと
    JST 00:00-09:00 の実行が前日に計上される）。不正形式は 400。
    """
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d").astimezone()
    except ValueError:
        raise HTTPException(
            status_code=400, detail="日付は YYYY-MM-DD 形式で指定してください"
        ) from None


def _csv_cell_safe(text: str) -> str:
    """CSV 式インジェクション対策: =/+/-/@/タブ始まりのセルを引用符で無害化する。

    item_text は設計者入力由来のため、Excel 等で式として解釈されないようにする。
    """
    if text and text[0] in ("=", "+", "-", "@", "\t"):
        return "'" + text
    return text


async def _metrics_summary_or_400(
    db: AsyncSession,
    questionnaire_id: int,
    date_from: str | None,
    date_to: str | None,
) -> tuple[Questionnaire, AcquisitionSummary]:
    """期間をパースして集計する（HTML/CSV 共通。to 日は含む＝半開区間 to+1日）。"""
    questionnaire = await _get_questionnaire_or_404(questionnaire_id, db)
    dt_from = _parse_metrics_date(date_from)
    dt_to = _parse_metrics_date(date_to)
    if dt_to is not None:
        dt_to += timedelta(days=1)
    summary = await acquisition_summary(db, questionnaire_id, dt_from, dt_to)
    return questionnaire, summary


@router.get("/metrics/acquisition", response_class=HTMLResponse)
async def acquisition_metrics_page(
    request: Request,
    current_user: RequireMetricsViewer,
    questionnaire_id: int | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> HTMLResponse:
    """取得困難度メトリクスの集計ページ

    Questionnaire × 期間 × 項目（link_id）で自動充足率・失敗理由内訳（FailureCode）・
    人手介入率を表示する。集計面は件数・率のみで患者値・patient_id を出さない（PHI 非含有）。
    """
    q_rows = await db.execute(
        select(Questionnaire).where(~Questionnaire.is_deleted).order_by(Questionnaire.id)
    )
    questionnaires = list(q_rows.scalars().all())

    questionnaire = None
    summary = None
    if questionnaire_id is not None:
        questionnaire, summary = await _metrics_summary_or_400(
            db, questionnaire_id, date_from, date_to
        )

    return templates.TemplateResponse(
        "ai_input/acquisition_metrics.html",
        {
            "request": request,
            "current_user": current_user,
            "questionnaires": questionnaires,
            "questionnaire": questionnaire,
            "summary": summary,
            "failure_codes": FailureCode.ALL,
            "failure_labels": FailureCode.LABELS,
            "date_from": date_from or "",
            "date_to": date_to or "",
        },
    )


@router.get("/metrics/acquisition.csv")
async def acquisition_metrics_csv(
    current_user: RequireMetricsViewer,
    questionnaire_id: int,
    date_from: str | None = None,
    date_to: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> Response:
    """取得困難度メトリクスの CSV エクスポート

    集計ページと同じ内容を項目1行の CSV で返す。件数・率のみで構成し、
    患者値・patient_id を含めない（tests/ai_input/test_acquisition_metrics.py で検査）。
    """
    _, summary = await _metrics_summary_or_400(db, questionnaire_id, date_from, date_to)

    buf = io.StringIO()
    # UTF-8 BOM: 日本語 item_text を Excel で直接開いても文字化けしないようにする
    buf.write("\ufeff")
    writer = csv.writer(buf)
    writer.writerow(
        [
            "link_id",
            "item_text",
            "executions",
            # **`fill_rate` の分母**（`executions` から「該当しない」を除いた数）。
            # 率だけを出すと、分母が何だったかを読み手が復元できない。
            "denominator",
            "filled",
            "fill_rate",
            *FailureCode.ALL,
            "responses_compared",
            "manual_entered",
            "manual_corrected",
            "intervention_rate",
        ]
    )
    for m in summary.items:
        writer.writerow(
            [
                _csv_cell_safe(m.link_id),
                _csv_cell_safe(m.item_text or ""),
                m.executions,
                m.denominator,
                m.filled,
                f"{m.fill_rate:.4f}" if m.fill_rate is not None else "",
                *[m.failure_counts.get(code, 0) for code in FailureCode.ALL],
                m.responses_compared,
                m.manual_entered,
                m.manual_corrected,
                f"{m.intervention_rate:.4f}" if m.intervention_rate is not None else "",
            ]
        )

    # **母集団の縮みを CSV にも併記する**。項目行だけを配ると、取得困難度が
    # **confirmed に至った項目に限った条件付きの値**であることが受け取り手に伝わらない。
    # **末尾に置く**のは、先頭に置くとヘッダ行が2行目になり素朴なパーサが壊れるためである。
    writer.writerow([])
    writer.writerow(
        [
            "# 母集団",
            f"対象項目={summary.target_item_count}",
            f"confirmed={summary.confirmed_item_count}",
            f"集計外={summary.population_shrink}",
            f"うちstep空={summary.confirmed_empty_program_count}",
            "取得困難度は confirmed に至った項目に限った条件付きの値。"
            "プランを確定できなかった項目はもっとも取得困難でありながら母集団から落ちるので、"
            "この値は困難度を過小に見せる（所在の確からしさの段階分布の分母は対象項目の全件）",
        ]
    )

    filename = f"acquisition_metrics_q{questionnaire_id}.csv"
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# 注: 患者キー列への key_type 付与（下書き＋確定）UI は indexer（`indexer/`）へ一本化した
# （WebUI 利用者は mrn / dwh_serial 等を判断できないため）。確定済みの key_type は
# indexer のワークフロー（draft → 人確認 → ingest）で成果物に焼き込まれ、WebUI は読むだけ。
# 旧 /resource-keys/review・/resource-keys/confirm エンドポイントと resource_key_binding
# 永続層（models.bindings_for / upsert_binding / ResourceKeyBinding）は廃止済み
# （テーブル削除は alembic e7f8a9b0c1d2）。
# ランタイムのキー解決は DSL step.physical_format を用いるため本変更の影響を受けない。
