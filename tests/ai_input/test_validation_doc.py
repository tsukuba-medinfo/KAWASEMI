"""データ収集手続確認書（CSV = Computerized System Validation 文書）の2様式テスト

文書様式は settings.enable_ai_extraction（自動構造化AI＝実行時LLM）で分岐する
（docs/ai_flags_spec.md §5）:
- ai_enabled:  実行時LLM関与を記録する様式
- ai_disabled: 実行時LLM非関与（処理の決定性）を宣言する様式。実行トレースに
  llm_extract / llm_judge の痕跡があれば409（ただし guard が偽でスキップされた
  llm op は実際に呼ばれていないため関与に数えない）

設計時AI（LLMコンパイル済みプラン等）は様式を分けず、両様式共通の
「設計過程におけるAI利用」開示欄に記載される（409にはならない）。
テストは settings.enable_ai_extraction を monkeypatch で切り替えて検証する。
**このファイルの全テストがフラグを明示的に設定する**。環境変数
ENABLE_AI_EXTRACTION に暗黙依存すると、フラグ不在の環境で skip ではなく fail し、
docs/development_flow.md 4.3 / 4.4 の表で説明できない失敗になるため。
"""

import re

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.models import (
    DesignLlmCall,
    DesignRole,
    InputExecution,
    InputPlan,
)
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.user import User

DETERMINISTIC_PROGRAM = {
    "steps": [
        {"op": "sql_extract", "id": "s1", "sql": "SELECT 1"},
    ],
    "return": "$s1",
}

DETERMINISTIC_TRACE = [
    {"op": "sql_extract", "id": "s1", "sql": "SELECT 1", "output": "1"},
]

LLM_TRACE = [
    {"op": "sql_extract", "id": "s1", "sql": "SELECT 1", "output": "テキスト"},
    {
        "op": "llm_extract",
        "id": "s2",
        "output": "あり",
        "llm": {
            "model": "model-a:12b",
            "options": {},
            "prompt": "次の所見から有無を抽出せよ",
            "raw_output": "あり",
        },
    },
]

# guard が偽で実行されなかった llm op。op 名は残るが実際には呼ばれていない。
SKIPPED_LLM_TRACE = [
    {"op": "sql_extract", "id": "s1", "sql": "SELECT 1", "output": "テキスト"},
    {"op": "llm_extract", "id": "s2", "guard": "$s1", "output": None, "skipped": True},
]

# skipped を自己申告しつつ LLM 呼び出しの確定記録（llm ブロック）を持つ矛盾したトレース。
# executor は構造上これを生成しないため、他構成のDB持ち込み等を想定した防御の対象。
FORGED_SKIPPED_LLM_TRACE = [
    {
        "op": "llm_extract",
        "id": "s2",
        "skipped": True,
        "output": "あり",
        "llm": {
            "model": "model-a:12b",
            "options": {},
            "prompt": "次の所見から有無を抽出せよ",
            "raw_output": "あり",
        },
    },
]


async def _make_execution(
    db_session: AsyncSession,
    *,
    trace: list[dict],
    plan_llm_model: str | None = None,
    executed_by_id: int | None = None,
) -> tuple[int, int]:
    """Questionnaire + confirmed プラン + 実行記録を作り (questionnaire_id, execution_id) を返す"""
    questionnaire = Questionnaire(
        fhir_id="q-validation-doc",
        title="検証文書テスト票",
        status="active",
        content={"resourceType": "Questionnaire", "id": "q-validation-doc"},
    )
    db_session.add(questionnaire)
    await db_session.flush()

    plan = InputPlan(
        questionnaire_id=questionnaire.id,
        link_id="item1",
        item_text="テスト項目",
        program=DETERMINISTIC_PROGRAM,
        status="confirmed",
        llm_model=plan_llm_model,
    )
    db_session.add(plan)

    execution = InputExecution(
        questionnaire_id=questionnaire.id,
        patient_id="PAT-001",
        executed_by_id=executed_by_id,
        prefill_data={"item1": "1"},
        evidence={
            "item1": {"trace": trace, "value": "1"},
            "_meta": {"acted_as": "collector"},
        },
        status="success",
    )
    db_session.add(execution)
    await db_session.commit()
    return questionnaire.id, execution.id


def _validation_url(questionnaire_id: int, execution_id: int) -> str:
    return (
        f"/ai-input/questionnaires/{questionnaire_id}/autofill/executions/{execution_id}/validation"
    )


@pytest.mark.asyncio
async def test_ai_enabled_document(
    collector_client: AsyncClient,
    db_session: AsyncSession,
    collector_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ENABLE_AI_EXTRACTION=true では実行時LLM関与を記録する様式（AI利用宣言つき）が出る"""
    qid, eid = await _make_execution(
        db_session,
        trace=LLM_TRACE,
        plan_llm_model="model-a:12b",
        executed_by_id=collector_user.id,
    )
    monkeypatch.setattr(settings, "enable_ai_extraction", True)
    resp = await collector_client.get(_validation_url(qid, eid))
    assert resp.status_code == 200
    assert "データ収集手続確認書" in resp.text
    assert "AI機能有効構成" in resp.text
    assert "AI利用に関する宣言" in resp.text
    # LLM関与の記録（モデル・プロンプト・生出力）が様式に含まれる
    assert "作成モデル: model-a:12b" in resp.text
    assert "次の所見から有無を抽出せよ" in resp.text
    # 設計過程のAI利用開示欄（3層系譜）
    assert "設計過程におけるAI利用と人間レビューの記録" in resp.text
    assert "AI作成（model-a:12b）" in resp.text


@pytest.mark.asyncio
async def test_ai_disabled_document(
    collector_client: AsyncClient,
    db_session: AsyncSession,
    collector_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ENABLE_AI_EXTRACTION=false かつLLM痕跡なしなら決定性宣言つきの様式が出る"""
    qid, eid = await _make_execution(
        db_session, trace=DETERMINISTIC_TRACE, executed_by_id=collector_user.id
    )
    monkeypatch.setattr(settings, "enable_ai_extraction", False)
    resp = await collector_client.get(_validation_url(qid, eid))
    assert resp.status_code == 200
    assert "データ収集手続確認書" in resp.text
    assert "AI機能無効構成" in resp.text
    assert "処理の決定性に関する宣言" in resp.text
    assert "LLM（大規模言語モデル）は関与していません" in resp.text
    # LLM関連の実行記録要素が様式に存在しない
    assert "llm-box" not in resp.text
    assert "作成モデル" not in resp.text
    # 全層手動の場合は開示欄にその旨が出る
    assert "設計過程にAIの関与はありません" in resp.text


@pytest.mark.asyncio
async def test_ai_disabled_rejects_llm_trace(
    collector_client: AsyncClient,
    db_session: AsyncSession,
    collector_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ENABLE_AI_EXTRACTION=false で実行トレースにLLM痕跡があれば409で生成を拒否する"""
    qid, eid = await _make_execution(db_session, trace=LLM_TRACE, executed_by_id=collector_user.id)
    monkeypatch.setattr(settings, "enable_ai_extraction", False)
    resp = await collector_client.get(_validation_url(qid, eid))
    assert resp.status_code == 409
    assert "生成できません" in resp.text
    # LLMの中身（プロンプト等）は拒否ページに漏らさない
    assert "次の所見から有無を抽出せよ" not in resp.text


@pytest.mark.asyncio
async def test_ai_disabled_allows_skipped_llm_step(
    collector_client: AsyncClient,
    db_session: AsyncSession,
    collector_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """guard が偽でスキップされた llm op は実行時LLM関与に当たらないため409にしない。

    op 名だけで拒否すると、条件付き実行で実際には呼ばれなかった LLM step のせいで
    決定的な実行の確認書が出せなくなる。実行事実（skipped）で判定する。
    """
    qid, eid = await _make_execution(
        db_session, trace=SKIPPED_LLM_TRACE, executed_by_id=collector_user.id
    )
    monkeypatch.setattr(settings, "enable_ai_extraction", False)
    resp = await collector_client.get(_validation_url(qid, eid))
    assert resp.status_code == 200
    assert "LLM（大規模言語モデル）は関与していません" in resp.text
    assert "未実行" in resp.text
    # プラン構成に LLM op を含む事実は宣言欄に開示する（宣言と実行トレースの齟齬を残さない）
    assert "条件付き実行（guard）により" in resp.text
    assert "別の被験者データでは" in resp.text


@pytest.mark.asyncio
async def test_ai_disabled_rejects_skipped_llm_with_call_record(
    collector_client: AsyncClient,
    db_session: AsyncSession,
    collector_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """llm ブロック（呼び出しの確定記録）を持つ entry は skipped の自己申告では免罪しない。

    このゲートは「他構成のDB持ち込み」への防御であり evidence を信用できない入力として
    扱う。executor は skip 経路で op ハンドラを呼ばないため skipped と llm が同時に付く
    entry を生成しないので、この判定で本機能の意図は損なわれない。
    """
    qid, eid = await _make_execution(
        db_session, trace=FORGED_SKIPPED_LLM_TRACE, executed_by_id=collector_user.id
    )
    monkeypatch.setattr(settings, "enable_ai_extraction", False)
    resp = await collector_client.get(_validation_url(qid, eid))
    assert resp.status_code == 409
    assert "次の所見から有無を抽出せよ" not in resp.text


@pytest.mark.asyncio
async def test_ai_disabled_discloses_llm_compiled_plan(
    collector_client: AsyncClient,
    db_session: AsyncSession,
    collector_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LLMコンパイル済み（llm_model 非null）プランは409にせず開示欄に記載する

    設計成果物基準（docs/ai_flags_spec.md §2）: 人間レビュー済みプランの実行は
    決定的であり、決定性の宣言と矛盾しない。旧仕様の409拒否は廃止。
    """
    qid, eid = await _make_execution(
        db_session,
        trace=DETERMINISTIC_TRACE,
        plan_llm_model="model-a:12b",
        executed_by_id=collector_user.id,
    )
    monkeypatch.setattr(settings, "enable_ai_extraction", False)
    resp = await collector_client.get(_validation_url(qid, eid))
    assert resp.status_code == 200
    assert "処理の決定性に関する宣言" in resp.text
    # 設計過程のAI利用が開示される（3層系譜の③取得プラン）
    assert "設計過程におけるAI利用と人間レビューの記録" in resp.text
    assert "AI作成（model-a:12b）" in resp.text


@pytest.mark.asyncio
async def test_validation_report_idor_blocked_for_non_owner(
    collector_client: AsyncClient,
    db_session: AsyncSession,
    collector_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """IDOR 防止: 実行者でない data_entry（admin/verifier でもない）は、
    他者が実行した記録の検証文書（患者ID・抽出値・生LLM出力を含む）を閲覧できない（403）。"""
    qid, eid = await _make_execution(
        db_session,
        trace=DETERMINISTIC_TRACE,
        executed_by_id=collector_user.id + 999,  # 別ユーザーが実行した記録
    )
    # 403 は様式の分岐より前段の閲覧権限検査で返るのでフラグに依存しないが、
    # このファイルの作法（フラグは常に明示する）に揃える。
    monkeypatch.setattr(settings, "enable_ai_extraction", False)
    resp = await collector_client.get(_validation_url(qid, eid))
    assert resp.status_code == 403


# =========================================================================== #
# 設計ロールの LLM 来歴の開示
#
# 確認書のテンプレートは `{% if design_llm_provenance %}` で囲まれているため、
# ルートからテンプレートへのコンテキスト受け渡しが壊れても、来歴を参照しない
# テストは緑のまま通る。ここで**本文に現れる内容**を固定する。
# =========================================================================== #


def _design_call(questionnaire_id: int, role: str, **overrides: object) -> DesignLlmCall:
    base: dict[str, object] = {
        "questionnaire_id": questionnaire_id,
        "role": role,
        "label": "手順書生成",
        "model": "model-a:12b",
        "options": {"temperature": 0, "num_ctx": 8192},
        "system_prompt": "あなたはEDCのアシスタントです。",
        "user_message": "### linkId: item1\n  項目名: テスト項目\n",
        "raw_output": "{}",
        "content_sources": [],
    }
    base.update(overrides)
    return DesignLlmCall(**base)  # type: ignore[arg-type]


def _prov_row(text: str, label: str) -> tuple[str, ...] | None:
    """来歴テーブルから役割ラベルの行のセル（**プロバイダ**・回数・失敗・切り詰め）を取る。

    **列に記録しても読む側が無ければ意味が無い**——別エンジンで
    測った run の確認書が Ollama の run と1バイトも変わらないなら、監査者は「どのエンジンが
    この測定を出したか」に答えられない。
    """
    m = re.search(
        rf"<td>{re.escape(label)}</td>\s*<td><code>([^<]+)</code></td>"
        rf"\s*<td>(\d+)</td>\s*<td>(\d+)</td>\s*<td>(\d+)</td>",
        text,
    )
    return m.groups() if m else None


@pytest.mark.asyncio
@pytest.mark.parametrize("ai_extraction", [True, False], ids=["ai_enabled", "ai_disabled"])
async def test_design_llm_provenance_is_disclosed(
    collector_client: AsyncClient,
    db_session: AsyncSession,
    collector_user: User,
    monkeypatch: pytest.MonkeyPatch,
    ai_extraction: bool,
) -> None:
    """役割ラベル・呼び出し回数・失敗件数・切り詰め件数が確認書の本文に現れること。

    様式は2つある（`ai_enabled` / `ai_disabled`）ので両方で固定する。開示欄は
    設計時AI の記録であり、実行時AI の有無で内容が変わってはいけない。
    """
    qid, eid = await _make_execution(
        db_session,
        trace=LLM_TRACE if ai_extraction else DETERMINISTIC_TRACE,
        plan_llm_model="model-a:12b",
        executed_by_id=collector_user.id,
    )
    # 取得手順書: 3回（うち失敗1・切り詰め2） / データ所在: 1回（失敗0・切り詰め0）
    db_session.add_all(
        [
            _design_call(qid, DesignRole.RUNBOOK, chunk_index=0, truncated=True),
            _design_call(qid, DesignRole.RUNBOOK, chunk_index=1, truncated=True),
            _design_call(
                qid,
                DesignRole.RUNBOOK,
                chunk_index=2,
                raw_output="{壊れたJSON",
                error="Expecting property name",
            ),
            _design_call(qid, DesignRole.SYSTEM, label="データ所在AI案"),
        ]
    )
    await db_session.commit()

    monkeypatch.setattr(settings, "enable_ai_extraction", ai_extraction)
    resp = await collector_client.get(_validation_url(qid, eid))
    assert resp.status_code == 200, resp.text

    # 開示の趣旨（プロンプト全文は載せず、保存先を示す）が本文にある
    assert "ai_input_design_llm_calls" in resp.text
    # 役割ラベルごとの 呼び出し回数 / うち失敗 / うち切り詰め
    assert _prov_row(resp.text, DesignRole.LABELS[DesignRole.RUNBOOK]) == (
        "ollama",
        "3",
        "1",
        "2",
    ), resp.text
    assert _prov_row(resp.text, DesignRole.LABELS[DesignRole.SYSTEM]) == (
        "ollama",
        "1",
        "0",
        "0",
    ), resp.text
    # **プロバイダの見出しが本文にある**（列そのものを落とす変更を捕まえる）
    assert "推論プロバイダ" in resp.text
    # 来歴の無い役割は行を作らない（「AIを使っていない」と「記録がある」を混同させない）
    assert _prov_row(resp.text, DesignRole.LABELS[DesignRole.PLAN]) is None
    # プロンプト全文は確認書に載せない（文書が肥大するため）
    assert "あなたはEDCのアシスタントです。" not in resp.text


@pytest.mark.asyncio
async def test_no_provenance_table_without_design_llm_calls(
    collector_client: AsyncClient,
    db_session: AsyncSession,
    collector_user: User,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """来歴が1件も無ければ開示テーブルを出さないこと（空表を出して誤読させない）。"""
    qid, eid = await _make_execution(
        db_session, trace=DETERMINISTIC_TRACE, executed_by_id=collector_user.id
    )
    monkeypatch.setattr(settings, "enable_ai_extraction", False)
    resp = await collector_client.get(_validation_url(qid, eid))
    assert resp.status_code == 200
    assert "ai_input_design_llm_calls" not in resp.text
