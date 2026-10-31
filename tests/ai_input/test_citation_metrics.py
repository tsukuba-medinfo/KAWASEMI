"""典拠の付与率の算出のテスト。

**これまで算出コードが存在しなかった**（ログと下書き API の応答フィールドしか無く、率を出せ
なかった）。定義は次のとおり:

- **分母 = 対象項目**（成果物の性質として定義する。AI下書きの対象に限ると運用の順序で
  母集団が動く）
- **分子 = 索引に紐づく典拠を1件以上持つ、記述のある項目**（`is_distributable_citation`
  を再利用。到達を確認できない自由文を数えると「何か書いた率」に
  なる）。**測っているのは「索引に紐づく来歴」であって「人が到達できるか」ではない**。
  人が UI で正しいアドレスを貼った場合も、保存時に索引で照合し、実在すれば payload を索引から
  導出するので分子に入る。
  **ただし既存レコードのうち、索引に無いアドレスを指すものは保存し直しても洗浄されない**（規則2 が記録を残す。索引に実在するアドレスなら規則1 が発火して洗浄される）。

ここで固定するのは、この2つの定義が実装と一致していること、および**分子と内訳の差が追える**
ことである。
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.citation_metrics import citation_coverage
from kawasemi.ai_input.models import ClinicalAnnotation
from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import RoleType
from kawasemi.models.user import User

# 索引された規約文書のチャンクを指す典拠（**索引に紐づく来歴がある**＝分子に入る）
_LINKED: dict[str, Any] = {
    "unit_id": "file:manuals/登録の手引き.pdf#p12:340-780",
    "payload": {
        "file_role": "reference",
        "path": "manuals/登録の手引き.pdf",
        "page": 12,
        "snippet": "診断日は病理診断の確定日を採る",
    },
    "llm_model": "qwen3:8b",
}

# 人が手で書いた典拠（索引に無い紙資料）。保存はできるが**索引に紐づかない**＝分子に入らない
_UNLINKED: dict[str, Any] = {
    "unit_id": "紙の院内マニュアル 第3版 p.7",
    "payload": {"label": "紙の院内マニュアル 第3版 p.7", "snippet": "…"},
    "llm_model": None,
}


def _item(
    citations: list[Any], annotation: str = "診断日は病理診断の確定日を採る"
) -> dict[str, Any]:
    """1項目の入力（記述と典拠）。**記述の条件は分子の定義の一部なので算出関数が判定する。**"""
    return {"annotation": annotation, "citations": citations}


def test_the_denominator_is_the_target_items_not_the_drafted_ones() -> None:
    """分母は**対象項目**で、典拠や下書きの有無に依らないこと。

    AI下書きの対象に限ると、一括生成か項目単位の再生成かで母集団が動く。同じ成果物でも測る
    タイミングで数字が変わる指標は評価に使えない。
    """
    result = citation_coverage(
        ["q1", "q2", "q3"], {"q1": _item([_LINKED])}, expected_target_items=5
    )
    assert result["target_items"] == 3
    assert result["expected_target_items"] == 5
    assert result["citation_found_items"] == 1


def test_the_expected_count_defaults_to_the_targets_found() -> None:
    """期待件数を渡さなければ、見つかった対象項目の数になること（設定の無い様式の既定）。"""
    result = citation_coverage(["q1", "q2", "q3"], {})
    assert result["expected_target_items"] == 3


def test_only_index_linked_citations_count_in_the_numerator() -> None:
    """分子は**索引に紐づく典拠**を持つ項目だけであること。

    到達を確認できない自由文を数えると、付与率は「何か書いた率」になる。
    """
    result = citation_coverage(
        ["q1", "q2", "q3"],
        {"q1": _item([_LINKED]), "q2": _item([_UNLINKED]), "q3": _item([])},
    )
    assert result["citation_found_items"] == 1
    assert result["citation_found_link_ids"] == ["q1"]
    # 内訳で差が追える
    assert result["items_with_any_citation"] == 2
    assert result["items_with_unlinked_citation_only"] == 1
    assert result["citations_total"] == 2


def test_a_mix_of_linked_and_unlinked_counts_once() -> None:
    """索引に紐づく典拠が1件でもあれば分子に入り、項目は二重に数えないこと。"""
    result = citation_coverage(["q1"], {"q1": _item([_UNLINKED, _LINKED])})
    assert result["citation_found_items"] == 1
    assert result["items_with_any_citation"] == 1
    assert result["items_with_unlinked_citation_only"] == 0
    assert result["citations_total"] == 2


def test_items_outside_the_denominator_are_ignored() -> None:
    """対象外の項目の典拠は分子にも内訳にも入らないこと（分母の外は数えない）。"""
    result = citation_coverage(["q1"], {"q1": _item([]), "ext1": _item([_LINKED])})
    assert result["citation_found_items"] == 0
    assert result["items_with_any_citation"] == 0
    assert result["citations_total"] == 0


def test_the_accounting_is_closed() -> None:
    """内訳の会計が閉じること（`any = found + unlinked_only`）。"""
    result = citation_coverage(
        ["q1", "q2", "q3", "q4"],
        {
            "q1": _item([_LINKED]),
            "q2": _item([_UNLINKED]),
            "q3": _item([_UNLINKED, _LINKED]),
            "q4": _item([]),
        },
    )
    assert (
        result["citation_found_items"] + result["items_with_unlinked_citation_only"]
        == result["items_with_any_citation"]
    )
    assert result["items_with_any_citation"] <= result["target_items"]


def test_the_ai_citation_count_is_about_citations_not_drafting() -> None:
    """`items_with_ai_citation` は「典拠に `llm_model` が付いた項目」であること。

    「AI下書きを実行した項目数」ではない——設計ロールの来歴はコール単位で link_id を持たない
    ので、項目単位では復元できない。数えているものを名前と docstring で一致させる。
    """
    result = citation_coverage(
        ["q1", "q2"],
        {"q1": _item([_LINKED]), "q2": _item([{**_LINKED, "llm_model": None}])},
    )
    assert result["items_with_ai_citation"] == 1
    assert result["citation_found_items"] == 2, "AI 由来かどうかは分子に関係しない"


def test_a_non_reference_citation_is_not_linked() -> None:
    """患者データ由来の典拠は分子に数えないこと（`is_distributable_citation` と同じ基準）。"""
    patient_data = {
        "unit_id": "file:patient_data/病理レポート.txt#p1:0-100",
        "payload": {"file_role": "patient_data", "path": "patient_data/病理レポート.txt"},
        "llm_model": None,
    }
    result = citation_coverage(["q1"], {"q1": _item([patient_data])})
    assert result["citation_found_items"] == 0
    assert result["items_with_unlinked_citation_only"] == 1


def test_broken_citation_entries_do_not_raise() -> None:
    """壊れた典拠（dict でない要素）で例外にならないこと。"""
    result = citation_coverage(["q1"], {"q1": _item(["文字列", None, _LINKED])})
    assert result["citation_found_items"] == 1
    assert result["citations_total"] == 1


def test_two_linked_citations_count_the_item_once() -> None:
    """**索引に紐づく典拠が2件ある項目**でも、項目は1つとして数えること。

    「混在で二重に数えない」を索引由来1件＋自由文1件だけで見ていると、分子を項目単位から
    典拠単位に膨らませる退行（率が 100% を超える）を捕まえられない。
    同じ `unit_id` を2回貼れることは保存側のコメントが明記している。
    """
    result = citation_coverage(["q1", "q2"], {"q1": _item([_LINKED, dict(_LINKED)])})
    assert result["citation_found_items"] == 1
    assert result["citation_found_link_ids"] == ["q1"]
    assert result["items_with_any_citation"] == 1
    assert result["citations_total"] == 2, "典拠の総数は2件"
    # 率が分母を超えないこと（項目単位であることの帰結）
    assert result["citation_found_items"] <= result["target_items"]


def test_a_citation_without_payload_is_not_linked() -> None:
    """`payload` が無い典拠は分子に入らないこと（形式の合う `unit_id` だけでは足りない）。

    保存側は `unit_id` だけの典拠を通すので、`payload` の欠落を「索引由来」に倒す退行
    （fail-open）が作れる。**来歴が無いものを索引由来と見なさない。**
    """
    no_payload = {"unit_id": _LINKED["unit_id"], "payload": None, "llm_model": None}
    result = citation_coverage(["q1"], {"q1": _item([no_payload])})
    assert result["citation_found_items"] == 0
    assert result["items_with_unlinked_citation_only"] == 1


def test_a_self_declared_reference_role_with_free_text_path_is_not_linked() -> None:
    """`unit_id` が索引のアドレス形式でなければ、`file_role` を自己申告しても分子に入らないこと。

    保存 API は payload の値を検査しないので、`file_role: reference` は自己申告でしかない。
    **形式検査（`unit_id`）と来歴（`file_role`）の両方**が要る。
    """
    self_declared = {
        "unit_id": "紙の院内マニュアル 第3版 p.7",
        "payload": {"file_role": "reference", "path": "紙の院内マニュアル"},
        "llm_model": None,
    }
    result = citation_coverage(["q1"], {"q1": _item([self_declared])})
    assert result["citation_found_items"] == 0


def test_an_item_without_an_annotation_is_not_in_the_numerator() -> None:
    """**記述が空の項目は分子に入らない**こと。判定は**算出関数の中**で行う。

    典拠は記述の根拠なので、根拠づける対象が無い典拠を「発見」と数えない。この条件を呼び出し側
    （ルート）に置くと、**分子の定義の一部が算出関数の外**に出て、呼び出し側の1行で分子が黙って変わる。
    """
    result = citation_coverage(
        ["q1", "q2"],
        {"q1": _item([_LINKED], annotation=""), "q2": _item([_LINKED])},
    )
    assert result["citation_found_items"] == 1, "記述が無い項目が分子に入っている"
    assert result["citation_found_link_ids"] == ["q2"]
    assert result["items_with_any_citation"] == 1
    assert result["citations_total"] == 1, "記述が無い項目の典拠は内訳にも入らない"
    assert result["target_items"] == 2, "分母は記述の有無に依らない"


def test_a_missing_or_non_string_annotation_is_not_an_annotation() -> None:
    """記述が `None` や文字列以外なら分子に入らないこと（**許可リストで判定する**）。

    保存 API は記述を正規化するが、DB へ直接入った値や将来の経路のために、記述と認めるのは
    **空でない文字列だけ**にする（「記述が無い」を「記述がある」に倒す fail-open を作らない）。
    """
    result = citation_coverage(
        ["q1", "q2", "q3"],
        {
            "q1": {"citations": [_LINKED]},  # 記述キーが無い
            "q2": {"annotation": None, "citations": [_LINKED]},
            "q3": {"annotation": 12345, "citations": [_LINKED]},
        },
    )
    assert result["citation_found_items"] == 0
    assert result["items_with_any_citation"] == 0


def test_an_entry_that_is_not_a_mapping_is_ignored() -> None:
    """項目の入力が dict でなければ数えないこと（記述の有無を確かめられないので数えない）。

    典拠の配列を直に渡す古い形が黙って分子に入ると、記述の条件が抜けたことに気づけない。
    """
    result = citation_coverage(["q1"], {"q1": [_LINKED]})  # type: ignore[dict-item]
    assert result["citation_found_items"] == 0
    assert result["items_with_any_citation"] == 0
    assert result["citations_total"] == 0


# =============================================================================
# ルート越し（永続化された典拠から計算していることを見る）
# =============================================================================


# 架空の様式。設定で対象にする番号は [1]〜[4] で、問診票は [1] [2] しか持たない
_FORM_URL = "http://kawasemi.example/Questionnaire/citation-coverage"


async def _make_questionnaire(db_session: AsyncSession) -> Questionnaire:
    q = Questionnaire(
        fhir_id="q-citation-coverage",
        title="典拠の付与率テスト票",
        status="active",
        url=_FORM_URL,
        content={
            "resourceType": "Questionnaire",
            "id": "q-citation-coverage",
            "url": _FORM_URL,
            "item": [
                {"linkId": "item1", "text": "[1] 登録番号", "type": "string"},
                {"linkId": "item2", "text": "[2] 生年月日", "type": "string"},
                # 対象外（他の項目が番号を持つ問診票で、項目番号なし）
                {"linkId": "ext1", "text": "リンパ管侵襲（拡張）", "type": "string"},
            ],
        },
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    return q


@pytest.mark.asyncio
async def test_citation_coverage_route_reads_persisted_citations(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    target_items_config,
) -> None:
    """ルートが**永続化された典拠**から計算すること（実行順序に依存しない）。

    ログや API 応答フィールドから作ると、いつ測るかで値が変わる。期待件数は様式の設定
    （`target_items.yaml`）から来る。
    """
    target_items_config({_FORM_URL: {"target_item_numbers": ["1", "2", "3", "4"]}})
    q = await _make_questionnaire(db_session)
    token = create_access_token(data={"sub": str(designer_user.id), "act": RoleType.DESIGNER})
    designer_client.cookies.set(settings.jwt_cookie_name, token)

    db_session.add_all(
        [
            ClinicalAnnotation(
                questionnaire_id=q.id,
                link_id="item1",
                item_text="[1] 登録番号",
                annotation="診断確定日",
                citations=[_LINKED],
            ),
            ClinicalAnnotation(
                questionnaire_id=q.id,
                link_id="item2",
                item_text="[2] 生年月日",
                annotation="架空の臨床的意味（典拠なし）",
                citations=[_UNLINKED],
            ),
            # 対象外の項目に索引由来の典拠があっても分子に入らない
            ClinicalAnnotation(
                questionnaire_id=q.id,
                link_id="ext1",
                item_text="リンパ管侵襲",
                annotation="拡張項目",
                citations=[_LINKED],
            ),
        ]
    )
    await db_session.commit()

    resp = await designer_client.get(
        f"/ai-input/questionnaires/{q.id}/source-survey/clinical/citation-coverage"
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["expected_target_items"] == 4, "期待件数は設定の件数"
    assert body["target_items"] == 2, "対象外（番号なし）は分母に入らない"
    assert body["citation_found_items"] == 1
    assert body["citation_found_link_ids"] == ["item1"]
    assert body["items_with_any_citation"] == 2
    assert body["items_with_unlinked_citation_only"] == 1
    assert body["items_with_ai_citation"] == 1


@pytest.mark.asyncio
async def test_citation_coverage_route_ignores_items_without_an_annotation(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
) -> None:
    """**記述が空の項目の典拠は分子に入らない**こと。

    保存 API は記述を空にしても典拠を保持する。典拠は記述の根拠なので、根拠づける対象が無い
    典拠を「発見」と数えない（Extension への配布も記述がある項目に限っているので揃える）。
    """
    q = await _make_questionnaire(db_session)
    token = create_access_token(data={"sub": str(designer_user.id), "act": RoleType.DESIGNER})
    designer_client.cookies.set(settings.jwt_cookie_name, token)
    db_session.add(
        ClinicalAnnotation(
            questionnaire_id=q.id,
            link_id="item1",
            item_text="[1] 登録番号",
            annotation="",  # 記述なし・典拠あり
            citations=[_LINKED],
        )
    )
    await db_session.commit()

    body = (
        await designer_client.get(
            f"/ai-input/questionnaires/{q.id}/source-survey/clinical/citation-coverage"
        )
    ).json()
    assert body["citation_found_items"] == 0, "記述が無い項目が分子に入っている"
    assert body["items_with_any_citation"] == 0
    assert body["target_items"] == 2, "分母は記述の有無に依らない"


@pytest.mark.asyncio
async def test_citation_coverage_route_without_any_annotation(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
) -> None:
    """臨床的意味が1件も無くても**分母が出る**こと（作業開始時点）。"""
    q = await _make_questionnaire(db_session)
    token = create_access_token(data={"sub": str(designer_user.id), "act": RoleType.DESIGNER})
    designer_client.cookies.set(settings.jwt_cookie_name, token)

    body = (
        await designer_client.get(
            f"/ai-input/questionnaires/{q.id}/source-survey/clinical/citation-coverage"
        )
    ).json()
    assert body["target_items"] == 2
    assert body["citation_found_items"] == 0
    assert body["items_with_any_citation"] == 0
    assert body["citations_total"] == 0
