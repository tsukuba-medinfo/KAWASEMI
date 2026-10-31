"""clinical_annotations の典拠（citations）と来歴のテスト

- 正規化ヘルパ _clean_citation / _normalize_citations
- モデルの便宜プロパティ（citation_list / is_confirmed / citation_llm_models）
- 保存API: 典拠と来歴の保存・読み出し（ラウンドトリップ）
- 確定ワークフロー: confirm=true で confirmed 昇格＋確認者記録、draft では非記録、
  確認済み維持の編集で確認者を保持、記述クリアで draft へ戻る
- Extension 読込による記述の上書きで人間確認が失効する
- 既存行（典拠・来歴なし）の移行: alembic e1f2a3b4c5d6 の upgrade
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.models import AnnotationStatus, ClinicalAnnotation
from kawasemi.ai_input.routes import _clean_citation, _normalize_citations
from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import RoleType
from kawasemi.models.user import User
from kawasemi.utils.file_export import ORIGIN_IMPORTED, is_distributable_citation

_CITATION = {
    "unit_id": "file:reference/entry_manual.pdf#p12:340-780",
    "payload": {"label": "手引き 3.2 進行期", "snippet": "手術進行期分類を用いる。"},
    "llm_model": "model-a:12b",
}


# --------------------------------------------------------------------------- #
# 正規化ヘルパ
# --------------------------------------------------------------------------- #


def test_clean_citation_keeps_unit_id_payload_and_provenance():
    out = _clean_citation(dict(_CITATION))
    assert out == {
        "unit_id": "file:reference/entry_manual.pdf#p12:340-780",
        "payload": {"label": "手引き 3.2 進行期", "snippet": "手術進行期分類を用いる。"},
        "llm_model": "model-a:12b",
    }


def test_clean_citation_accepts_payload_only():
    """索引アドレスが無くても抜粋だけの典拠（手入力）は保存する。"""
    out = _clean_citation({"payload": {"snippet": "手引き p12 の記載"}})
    assert out is not None
    assert out["unit_id"] is None
    assert out["payload"] == {"snippet": "手引き p12 の記載"}
    assert out["llm_model"] is None


def test_clean_citation_drops_empty():
    assert _clean_citation({}) is None
    assert _clean_citation({"unit_id": "   ", "payload": {}}) is None


def test_clean_citation_nulls_invalid_payload_type():
    out = _clean_citation({"unit_id": "file:x#p1:0-1", "payload": "not-a-dict"})
    assert out is not None
    assert out["payload"] is None


def test_normalize_citations_filters_invalid_elements():
    item = {
        "citations": [
            dict(_CITATION),
            "文字列は無視",
            {},  # 空要素は破棄
            {"unit_id": "file:y#p2:0-9"},
        ]
    }
    out = _normalize_citations(item)
    assert [c["unit_id"] for c in out] == [_CITATION["unit_id"], "file:y#p2:0-9"]


def test_normalize_citations_without_key_is_empty():
    assert _normalize_citations({"link_id": "item1"}) == []
    assert _normalize_citations({"citations": "not-a-list"}) == []


# --------------------------------------------------------------------------- #
# モデルの便宜プロパティ
# --------------------------------------------------------------------------- #


def test_model_helpers():
    ann = ClinicalAnnotation(
        questionnaire_id=1,
        link_id="item1",
        annotation="定義",
        citations=[dict(_CITATION), {"unit_id": "file:y#p1:0-9", "llm_model": None}],
        status=AnnotationStatus.CONFIRMED,
    )
    assert len(ann.citation_list) == 2
    assert ann.is_confirmed is True
    assert ann.citation_llm_models == ["model-a:12b"]


def test_model_helpers_normalize_none_citations():
    ann = ClinicalAnnotation(questionnaire_id=1, link_id="item1")
    ann.citations = None  # 念のため None でも [] 扱い
    assert ann.citation_list == []
    assert ann.citation_llm_models == []
    assert ann.is_confirmed is False


# --------------------------------------------------------------------------- #
# 保存API（典拠と来歴のラウンドトリップ・確定ワークフロー）
# --------------------------------------------------------------------------- #


def _act_as(client: AsyncClient, user: User, role: str) -> None:
    token = create_access_token(data={"sub": str(user.id), "act": role})
    client.cookies.set(settings.jwt_cookie_name, token)


async def _make_questionnaire(db_session: AsyncSession) -> Questionnaire:
    q = Questionnaire(
        fhir_id="q-clinical-citations",
        title="典拠テスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-clinical-citations",
            "item": [
                {"linkId": "item1", "text": "項目1", "type": "string"},
                {"linkId": "item2", "text": "項目2", "type": "string"},
            ],
        },
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    return q


async def _get_annotation(db: AsyncSession, qid: int, link_id: str) -> ClinicalAnnotation:
    result = await db.execute(
        select(ClinicalAnnotation).where(
            ClinicalAnnotation.questionnaire_id == qid,
            ClinicalAnnotation.link_id == link_id,
        )
    )
    return result.scalar_one()


def _clinical_url(qid: int, *, confirm: bool = False) -> str:
    url = f"/ai-input/questionnaires/{qid}/source-survey/clinical"
    return f"{url}?confirm=true" if confirm else url


def _items(annotation: str = "起点は手術日。時間窓は術前7日。", citations: list | None = None):
    return {
        "items": [
            {
                "link_id": "item1",
                "item_text": "項目1",
                "annotation": annotation,
                "citations": citations if citations is not None else [dict(_CITATION)],
            }
        ]
    }


@pytest.mark.asyncio
async def test_save_and_read_citations_and_provenance(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """典拠と来歴が保存され、編集画面から読み出せる。"""
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await designer_client.post(
        _clinical_url(q.id),
        json=_items(),
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    assert resp.json()["saved"] == 1

    ann = await _get_annotation(db_session, q.id, "item1")
    assert ann.citation_list == [_CITATION]
    assert ann.status == AnnotationStatus.DRAFT  # confirm 指定なしは下書き
    assert ann.citation_llm_models == ["model-a:12b"]

    # 編集画面が典拠を初期状態として返す（読み出し）
    page = await designer_client.get(_clinical_url(q.id))
    assert page.status_code == 200
    assert "file:reference/entry_manual.pdf#p12:340-780" in page.text
    assert "典拠（規約文書の該当箇所）" in page.text


@pytest.mark.asyncio
async def test_save_drops_empty_citation_rows(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """空の典拠行は保存しない（UIの未入力行が残らない）。

    **「値が全て空の payload」も落とす**。従来のフィクスチャは
    `{}` / `None` / `""` しか送っていなかったので、**`any(payload.values())` を消す変異が
    振る舞い層を素通りした**。
    `_clean_citation` の docstring 自身が「UIの未入力行」と名指しているのはこの形である。

    **この退行は指標を動かす。** 空行が1件残ると `citation_metrics.citation_coverage` の
    `citations_total` と `items_with_any_citation` が増え、**`items_with_unlinked_citation_only`
    （人が典拠を書いたが索引に紐づかない項目）が増える**——保存時の索引照合が動かす分子との差を
    説明するのに使う数である。フェイルクローズでもないので例外もログも出ない。
    """
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await designer_client.post(
        _clinical_url(q.id),
        json=_items(citations=[{"unit_id": "", "payload": None, "llm_model": None}]),
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    ann = await _get_annotation(db_session, q.id, "item1")
    assert ann.citation_list == []
    # **前提**: この項目には記述がある。上の指標の主張
    # （`items_with_unlinked_citation_only` が動く）は**記述のある項目**に限る分子なので、
    # フィクスチャの既定が変わるとこのテストは別の性質を測ることになる。
    assert ann.annotation, "前提: 記述のある項目で測っている"

    # **キーはあるが値が全て空**（UIの未入力行そのもの）。`not payload` では落ちない形
    resp = await designer_client.post(
        _clinical_url(q.id),
        json=_items(
            citations=[{"unit_id": "", "payload": {"snippet": "", "label": ""}, "llm_model": None}]
        ),
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    ann = await _get_annotation(db_session, q.id, "item1")
    assert ann.citation_list == [], "値が全て空の payload が保存されている"
    # **肯定側**（値が1つでもあれば残す。常に落とす実装を殺す）
    resp = await designer_client.post(
        _clinical_url(q.id),
        json=_items(
            citations=[
                {
                    "unit_id": "",
                    "payload": {"snippet": "紙の資料 p.7", "label": ""},
                    "llm_model": None,
                }
            ]
        ),
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    ann = await _get_annotation(db_session, q.id, "item1")
    assert [c["payload"]["snippet"] for c in ann.citation_list] == ["紙の資料 p.7"]


@pytest.mark.asyncio
async def test_confirm_records_confirmed_by(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """confirm=true で confirmed 昇格し、確認操作者・日時が記録される。"""
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await designer_client.post(
        _clinical_url(q.id, confirm=True),
        json=_items(),
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    assert resp.json()["confirmed"] == 1

    ann = await _get_annotation(db_session, q.id, "item1")
    assert ann.status == AnnotationStatus.CONFIRMED
    assert ann.confirmed_by_id == designer_user.id
    assert ann.confirmed_at is not None


@pytest.mark.asyncio
async def test_confirm_skips_empty_annotation(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """記述が空の項目は確認できない（draft のまま・確認者も付かない）。"""
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await designer_client.post(
        _clinical_url(q.id, confirm=True),
        json=_items(annotation="   ", citations=[]),
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    assert resp.json()["confirmed"] == 0

    ann = await _get_annotation(db_session, q.id, "item1")
    assert ann.annotation is None
    assert ann.status == AnnotationStatus.DRAFT
    assert ann.confirmed_by_id is None


@pytest.mark.asyncio
async def test_edit_after_confirm_preserves_confirmer(
    designer_client: AsyncClient,
    designer_user: User,
    multi_role_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """確認済みを維持する編集（confirm なし）では元の確認者・確認日時を保持する。"""
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await designer_client.post(
        _clinical_url(q.id, confirm=True),
        json=_items(),
        headers={"X-CSRF-Token": csrf_token},
    )
    ann = await _get_annotation(db_session, q.id, "item1")
    confirmer, confirmed_at = ann.confirmed_by_id, ann.confirmed_at

    # 別ユーザーが confirm なしで編集（典拠も追記）
    _act_as(designer_client, multi_role_user, RoleType.DESIGNER)
    resp = await designer_client.post(
        _clinical_url(q.id),
        json=_items(annotation="起点は手術日。時間窓は術前14日に修正。"),
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200

    await db_session.refresh(ann)
    assert ann.status == AnnotationStatus.CONFIRMED
    assert ann.confirmed_by_id == confirmer  # 編集者に書き換わらない
    assert ann.confirmed_at == confirmed_at


@pytest.mark.asyncio
async def test_clearing_annotation_reverts_to_draft(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """記述を空にしたら未確認へ戻し、確認の帰属をクリアする。"""
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await designer_client.post(
        _clinical_url(q.id, confirm=True),
        json=_items(),
        headers={"X-CSRF-Token": csrf_token},
    )
    await designer_client.post(
        _clinical_url(q.id),
        json=_items(annotation="", citations=[]),
        headers={"X-CSRF-Token": csrf_token},
    )

    ann = await _get_annotation(db_session, q.id, "item1")
    assert ann.status == AnnotationStatus.DRAFT
    assert ann.confirmed_by_id is None
    assert ann.confirmed_at is None


@pytest.mark.asyncio
async def test_import_extension_invalidates_confirmation(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """Extension からの上書きで記述が変わったら未確認へ戻す（典拠は保持）。"""
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await designer_client.post(
        _clinical_url(q.id, confirm=True),
        json=_items(),
        headers={"X-CSRF-Token": csrf_token},
    )

    # Questionnaire 側の Extension に別の記述を仕込む
    q.content = {
        "resourceType": "Questionnaire",
        "id": "q-clinical-citations",
        "item": [
            {
                "linkId": "item1",
                "text": "項目1",
                "type": "string",
                "extension": [
                    {
                        "url": (
                            "https://kawasemi.example.jp/fhir/StructureDefinition/"
                            "clinical-annotation"
                        ),
                        "valueString": "外部から持ち込まれた別の定義",
                    }
                ],
            }
        ],
    }
    await db_session.commit()

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/clinical/import-extension",
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200

    ann = await _get_annotation(db_session, q.id, "item1")
    assert ann.annotation == "外部から持ち込まれた別の定義"
    assert ann.status == AnnotationStatus.DRAFT
    assert ann.confirmed_by_id is None
    assert ann.citation_list == [_CITATION]  # 典拠は Extension に無いので保持


@pytest.mark.asyncio
async def test_canvas_shows_draft_and_confirmed_states(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """設計キャンバスの臨床セルが status（draft / confirmed）を反映する。"""
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await designer_client.post(
        _clinical_url(q.id),  # item1 を draft で保存
        json=_items(),
        headers={"X-CSRF-Token": csrf_token},
    )

    from kawasemi.ai_input.routes import _canvas_cell_states

    ann = await _get_annotation(db_session, q.id, "item1")
    assert _canvas_cell_states(ann, None, None, None)["clinical"] == "draft"

    ann.status = AnnotationStatus.CONFIRMED
    assert _canvas_cell_states(ann, None, None, None)["clinical"] == "confirmed"

    ann.annotation = None
    assert _canvas_cell_states(ann, None, None, None)["clinical"] == "empty"


@pytest.mark.asyncio
@pytest.mark.parametrize("ai_extraction", [False, True], ids=["ai_disabled", "ai_enabled"])
async def test_validation_document_discloses_clinical_provenance(
    collector_client: AsyncClient,
    db_session: AsyncSession,
    collector_user: User,
    monkeypatch: pytest.MonkeyPatch,
    ai_extraction: bool,
) -> None:
    """確認書の開示欄に臨床的意味の来歴（状態・典拠・AIモデル）が出る。"""
    from kawasemi.ai_input.models import InputExecution, InputPlan

    q = Questionnaire(
        fhir_id="q-clinical-disclosure",
        title="開示欄テスト票",
        status="active",
        content={"resourceType": "Questionnaire", "id": "q-clinical-disclosure"},
    )
    db_session.add(q)
    await db_session.flush()

    db_session.add(
        ClinicalAnnotation(
            questionnaire_id=q.id,
            link_id="item1",
            item_text="項目1",
            annotation="起点は手術日。",
            citations=[dict(_CITATION)],
            status=AnnotationStatus.CONFIRMED,
            llm_model=None,
        )
    )
    db_session.add(
        InputPlan(
            questionnaire_id=q.id,
            link_id="item1",
            item_text="項目1",
            program={
                "steps": [{"op": "sql_extract", "id": "s1", "sql": "SELECT 1"}],
                "return": "$s1",
            },
            status="confirmed",
        )
    )
    execution = InputExecution(
        questionnaire_id=q.id,
        patient_id="PAT-001",
        executed_by_id=collector_user.id,
        prefill_data={"item1": "1"},
        evidence={
            "item1": {"trace": [{"op": "sql_extract", "id": "s1", "output": "1"}], "value": "1"}
        },
        status="success",
    )
    db_session.add(execution)
    await db_session.commit()

    # 確認書は enable_ai_extraction で様式が分かれる（ai_enabled / ai_disabled は
    # 部品非共有の別テンプレート）。フラグを明示しないと、どちらの様式を検証したかが
    # 実行環境の .env 次第になる。開示欄は両様式に出るので、
    # 片方へ固定してカバレッジを落とすのではなく、両方を回して固定する。
    monkeypatch.setattr(settings, "enable_ai_extraction", ai_extraction)
    resp = await collector_client.get(
        f"/ai-input/questionnaires/{q.id}/autofill/executions/{execution.id}/validation"
    )
    assert resp.status_code == 200
    assert "① 臨床的意味" in resp.text
    # 典拠のAI由来モデルが開示され、件数とアドレスが併記される
    assert "AI下書き（model-a:12b）" in resp.text
    assert "典拠1件" in resp.text
    assert "file:reference/entry_manual.pdf#p12:340-780" in resp.text


@pytest.mark.asyncio
async def test_confirm_does_not_steal_other_items_attribution(
    designer_client: AsyncClient,
    designer_user: User,
    multi_role_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """一括保存＋confirm でも、内容が変わっていない既確認項目の帰属は奪わない。

    確認の帰属は「その内容を確認した人」を指す。A が2項目を確認したあと、B が1項目
    だけ書き換えて確認しても、触っていない項目の確認者・確認日時は A のまま残る。
    """
    q = await _make_questionnaire(db_session)

    def _payload(item2_annotation: str) -> dict:
        return {
            "items": [
                {
                    "link_id": "item1",
                    "item_text": "項目1",
                    "annotation": "項目1の定義。",
                    "citations": [dict(_CITATION)],
                },
                {
                    "link_id": "item2",
                    "item_text": "項目2",
                    "annotation": item2_annotation,
                    "citations": [],
                },
            ]
        }

    # A が2項目まとめて確認
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    resp = await designer_client.post(
        _clinical_url(q.id, confirm=True),
        json=_payload("項目2の定義。"),
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.json()["confirmed"] == 2
    ann1 = await _get_annotation(db_session, q.id, "item1")
    ann2 = await _get_annotation(db_session, q.id, "item2")
    a_confirmer, a_at = ann1.confirmed_by_id, ann1.confirmed_at
    assert a_confirmer == designer_user.id

    # B が item2 だけ書き換えて一括確認（item1 も同じ内容で送られてくる）
    _act_as(designer_client, multi_role_user, RoleType.DESIGNER)
    resp = await designer_client.post(
        _clinical_url(q.id, confirm=True),
        json=_payload("項目2の定義を修正。"),
        headers={"X-CSRF-Token": csrf_token},
    )
    # 新たに昇格したのは内容が変わった item2 のみ
    assert resp.json()["confirmed"] == 1

    await db_session.refresh(ann1)
    await db_session.refresh(ann2)
    assert ann1.confirmed_by_id == a_confirmer  # A の確認記録が残る
    assert ann1.confirmed_at == a_at
    assert ann2.confirmed_by_id == multi_role_user.id  # 内容を変えた項目は B の確認


@pytest.mark.asyncio
async def test_reconfirm_unchanged_item_counts_zero_promotions(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """同じ内容で確認し直しても「昇格件数」は増えない（ログ・UIの件数が実態と一致）。"""
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    first = await designer_client.post(
        _clinical_url(q.id, confirm=True), json=_items(), headers={"X-CSRF-Token": csrf_token}
    )
    assert first.json()["confirmed"] == 1

    second = await designer_client.post(
        _clinical_url(q.id, confirm=True), json=_items(), headers={"X-CSRF-Token": csrf_token}
    )
    assert second.json()["confirmed"] == 0


@pytest.mark.asyncio
async def test_citations_kept_when_key_omitted(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """citations キーを持たないリクエストは典拠を消さない（ロストアップデート防止）。

    典拠は本テーブルにしか存在せず（Extension に写しがない）、消えると復元できない。
    """
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await designer_client.post(
        _clinical_url(q.id), json=_items(), headers={"X-CSRF-Token": csrf_token}
    )

    # 旧クライアント相当（citations キーなし）の保存
    resp = await designer_client.post(
        _clinical_url(q.id),
        json={"items": [{"link_id": "item1", "item_text": "項目1", "annotation": "定義を修正。"}]},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200

    ann = await _get_annotation(db_session, q.id, "item1")
    assert ann.annotation == "定義を修正。"
    assert ann.citation_list == [_CITATION]  # 典拠は保持される

    # 空配列を明示した場合は消える（UIからの削除操作）
    await designer_client.post(
        _clinical_url(q.id),
        json=_items(annotation="定義を修正。", citations=[]),
        headers={"X-CSRF-Token": csrf_token},
    )
    await db_session.refresh(ann)
    assert ann.citation_list == []


@pytest.mark.asyncio
async def test_noop_save_does_not_touch_updated_at(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """内容が同じ保存で updated_at を進めない（下流の手順書を一斉 stale にしない）。"""
    from datetime import datetime

    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await designer_client.post(
        _clinical_url(q.id, confirm=True), json=_items(), headers={"X-CSRF-Token": csrf_token}
    )

    # 明示代入は onupdate より優先されるので、既知の古い値を目印として置く
    ann = await _get_annotation(db_session, q.id, "item1")
    sentinel = datetime(2020, 1, 2, 3, 4, 5)
    ann.updated_at = sentinel
    await db_session.commit()

    # 同じ内容で保存（確認つき）→ 何も変わらないので updated_at は据え置き
    await designer_client.post(
        _clinical_url(q.id, confirm=True), json=_items(), headers={"X-CSRF-Token": csrf_token}
    )
    await db_session.refresh(ann)
    assert ann.updated_at == sentinel

    # 内容を変えたら更新される
    await designer_client.post(
        _clinical_url(q.id),
        json=_items(annotation="定義を変更。"),
        headers={"X-CSRF-Token": csrf_token},
    )
    await db_session.refresh(ann)
    assert ann.updated_at != sentinel


@pytest.mark.asyncio
async def test_invalid_field_types_are_ignored_not_500(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """生JSONに型違いが混ざっても 500 にせず無視する（保存APIは request.json() 直読み）。"""
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await designer_client.post(
        _clinical_url(q.id),
        json={
            "items": [
                "文字列の項目は無視",
                {"link_id": 12345},  # link_id が文字列でない → 無視
                {
                    "link_id": "item1",
                    "item_text": 999,
                    "annotation": "定義。",
                    "citations": [
                        {"unit_id": 123, "llm_model": 5},  # 型違い → アドレス・来歴は落ちる
                        {"unit_id": "file:ok#p1:0-9", "payload": {"snippet": "抜粋"}},
                    ],
                },
            ]
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    assert resp.json()["saved"] == 1

    ann = await _get_annotation(db_session, q.id, "item1")
    assert ann.item_text is None  # 型違いは None に落とす
    assert ann.citation_list == [
        {"unit_id": "file:ok#p1:0-9", "payload": {"snippet": "抜粋"}, "llm_model": None}
    ]


# --------------------------------------------------------------------------- #
# 既存行の移行（alembic e1f2a3b4c5d6）
# --------------------------------------------------------------------------- #


def test_migration_backfills_existing_rows(tmp_path) -> None:
    """既存行は annotation を保ったまま、典拠=空配列・status=記述有無で移行される。

    移行前のスキーマ（典拠・来歴カラムなし）を素の SQLite に作り、alembic の
    upgrade() をそのコネクション上で実行する。
    """
    import importlib.util
    import json
    from pathlib import Path

    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    module_path = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "e1f2a3b4c5d6_add_citations_and_provenance_to_clinical_annotations.py"
    )
    spec = importlib.util.spec_from_file_location("migration_e1f2a3b4c5d6", module_path)
    assert spec and spec.loader
    migration = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(migration)

    engine = sa.create_engine(f"sqlite:///{tmp_path / 'legacy.db'}")
    with engine.begin() as conn:
        conn.exec_driver_sql(
            """
            CREATE TABLE clinical_annotations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                questionnaire_id INTEGER NOT NULL,
                link_id VARCHAR(255) NOT NULL,
                item_text TEXT,
                annotation TEXT,
                created_at DATETIME,
                updated_at DATETIME
            )
            """
        )
        conn.exec_driver_sql(
            "INSERT INTO clinical_annotations (questionnaire_id, link_id, item_text, annotation) "
            "VALUES (1, 'item1', '項目1', '起点は手術日。'), (1, 'item2', '項目2', NULL)"
        )

    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.upgrade()

        rows = conn.exec_driver_sql(
            "SELECT link_id, annotation, citations, status, llm_model, confirmed_by_id, "
            "confirmed_at FROM clinical_annotations ORDER BY link_id"
        ).fetchall()

    by_link = {r[0]: r for r in rows}
    # 既存の自由記述はそのまま
    assert by_link["item1"][1] == "起点は手術日。"
    assert by_link["item2"][1] is None
    # 典拠は空配列
    assert json.loads(by_link["item1"][2]) == []
    assert json.loads(by_link["item2"][2]) == []
    # 記述ありは confirmed（旧UIの表示・ゲート挙動を保つ）、記述なしは draft
    assert by_link["item1"][3] == "confirmed"
    assert by_link["item2"][3] == "draft"
    # 来歴は不明なので NULL のまま
    assert by_link["item1"][4] is None
    assert by_link["item1"][5] is None
    assert by_link["item1"][6] is None

    # downgrade で追加カラムが消える（既存カラムは残る）
    with engine.begin() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.downgrade()
        cols = {
            r[1] for r in conn.exec_driver_sql("PRAGMA table_info(clinical_annotations)").fetchall()
        }
    assert cols == {
        "id",
        "questionnaire_id",
        "link_id",
        "item_text",
        "annotation",
        "created_at",
        "updated_at",
    }


@pytest.mark.asyncio
async def test_save_keeps_the_import_provenance_already_in_the_db(
    designer_client: AsyncClient,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """**保存経路が既存レコードの取り込み来歴を引き継ぐ**こと（保存時の索引照合の結線）。

    索引の実在照合は「索引に無い `unit_id` から `origin` を落とす」——**クライアントの自己申告を
    認めない**ためである。ただし配布物から取り込んだ典拠は受け取った施設の索引に無いので、
    **DB にある事実は引き継ぐ**必要がある。引き継ぎの根拠は既存レコードなので、保存経路が
    `existing=row` を渡していないと切れる（`_normalize_citations` を直接呼ぶテストでは
    この結線が測れず、実際に**素通りした**）。
    """
    q = await _make_questionnaire(db_session)
    imported = {
        "unit_id": "file:別施設マニュアル.pdf#p3:0-100",
        "payload": {"origin": ORIGIN_IMPORTED},
        "llm_model": None,
    }
    db_session.add(
        ClinicalAnnotation(
            questionnaire_id=q.id,
            link_id="item1",
            item_text="項目1",
            annotation="取り込んだ記述",
            citations=[imported],
        )
    )
    await db_session.commit()

    resp = await designer_client.post(
        _clinical_url(q.id),
        json=_items(annotation="取り込んだ記述", citations=[dict(imported)]),
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200

    ann = await _get_annotation(db_session, q.id, "item1")
    [saved] = ann.citation_list
    assert saved["payload"]["origin"] == ORIGIN_IMPORTED, "取り込みの事実が落ちている"
    assert is_distributable_citation(saved), "再配布できなくなっている"


@pytest.mark.asyncio
async def test_save_strips_a_self_asserted_reference_role(
    designer_client: AsyncClient,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """**保存 API から `file_role: reference` を自己申告できない**こと（保存時の索引照合の結線）。

    索引が無い環境（テストの既定）では照合できないので、**フェイルクローズで主張を落とす**。
    ここが素通りすると、API を直叩きするだけで捏造したアドレスが配布物と典拠の付与率の分子に入る。
    """
    q = await _make_questionnaire(db_session)
    resp = await designer_client.post(
        _clinical_url(q.id),
        json=_items(
            citations=[
                {
                    "unit_id": "file:捏造.pdf#p1:0-1",
                    "payload": {"file_role": "reference", "path": "捏造.pdf"},
                    "llm_model": None,
                }
            ]
        ),
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200

    ann = await _get_annotation(db_session, q.id, "item1")
    [saved] = ann.citation_list
    assert saved["unit_id"] == "file:捏造.pdf#p1:0-1", "人の入力そのものは残す"
    assert (saved["payload"] or {}).get("file_role") is None, "自己申告が保存されている"
    assert not is_distributable_citation(saved)
