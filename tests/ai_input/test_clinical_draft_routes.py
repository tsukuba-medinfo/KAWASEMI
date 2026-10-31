"""臨床的意味のAI下書きエンドポイントと、記述の来歴の刻印

`AIClinicalDrafter` 自体のテストは `test_clinical_drafter.py` にある。ここで固定するのは
**ルートと画面の配線**である。次の2つを検証する。

1. **索引が使えなかったのか候補が無かったのかを、API が区別して返すこと。**
   区別せずに成功で返すと「典拠つき0件 / 典拠なし0件 / 候補なしN件」という自己矛盾した
   集計を画面が緑の成功表示で出し、LLM を1回も呼んでいないのにモデル名まで表示する
2. **AI が書いた記述であることが `clinical_annotations.llm_model` に残ること。**
   典拠行にだけ来歴を持たせると、人が典拠行を削除したときに **LLM が書いた記述が
   「手動」として確認書に開示される**
"""

from __future__ import annotations

import pathlib
import re
from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input import routes as routes_mod
from kawasemi.ai_input.clinical_drafter import ClinicalDraftResult
from kawasemi.ai_input.models import ClinicalAnnotation
from kawasemi.ai_input.schemas import (
    ClinicalAnnotationDraftCandidate,
    ClinicalAnnotationDraftItem,
    DraftState,
)
from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import RoleType
from kawasemi.models.user import User

# **画面の JS は静的ファイルへ切り出した**。以前はテンプレートのインライン
# `<script>` だったので、配線の検査はレンダリング結果（`body`）への文字列 assert で
# 行っていた。切り出し後は JS がレスポンスに載らないので、**同じ検査をファイルに対して
# 行う**。文字列 assert である点は変わらない（配線が生きているかは `node --check` では
# 分からない）が、**構文エラーは CI の「JS 構文チェック」が落とすようになった**——
# 以前は JS が丸ごとパースエラーでも全テストが緑だった。
_STATIC_JS = pathlib.Path(__file__).resolve().parents[2] / "kawasemi" / "web" / "static" / "js"
_CLINICAL_JS = (_STATIC_JS / "design_clinical.js").read_text(encoding="utf-8")

_MODEL = "model-a:12b"


def _act_as(client: AsyncClient, user: User, role: str) -> None:
    token = create_access_token(data={"sub": str(user.id), "act": role})
    client.cookies.set(settings.jwt_cookie_name, token)


async def _make_questionnaire(db_session: AsyncSession) -> Questionnaire:
    q = Questionnaire(
        fhir_id="q-clinical-draft",
        title="AI下書きテスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-clinical-draft",
            "item": [
                {"linkId": "item1", "text": "診断日", "type": "string"},
                {"linkId": "item2", "text": "進行度", "type": "string"},
            ],
        },
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    return q


def _draft_url(qid: int) -> str:
    return f"/ai-input/questionnaires/{qid}/source-survey/clinical/ai-draft"


def _clinical_url(qid: int) -> str:
    return f"/ai-input/questionnaires/{qid}/source-survey/clinical"


# ルートが drafter へ渡した典拠の範囲（`reference_scope`）。テストごとに `_patch_drafter` が空にする
seen_reference_paths: list[frozenset[str] | None] = []


def _patch_drafter(monkeypatch: pytest.MonkeyPatch, result: ClinicalDraftResult) -> None:
    """ルートが使う drafter を、決まった結果を返すフェイクに差し替える。"""
    seen_reference_paths.clear()

    class _FakeDrafter:
        async def draft(self, items, model=None, recorder=None, reference_paths=None):  # noqa: ANN001, ANN202
            seen_reference_paths.append(reference_paths)
            return result

    monkeypatch.setattr(routes_mod, "AIClinicalDrafter", lambda *a, **kw: _FakeDrafter())


def _js_function(body: str, name: str) -> str:
    """レンダリング結果から関数 `name` の本体を切り出す。

    JS のテストランナーが無いのでレンダリング結果への検査で代替しているが、
    「その文字列がページのどこかに在る」だけを見ると、同じ文字列を持つ別の関数に
    助けられて防御の脱落を見逃す。関数単位で切り出して個別に固定する。

    テンプレートの関数はすべて列0で始まり、列0の `}` で閉じる。
    """
    m = re.search(rf"^(?:async )?function {re.escape(name)}\(.*?^\}}$", body, re.M | re.S)
    assert m, f"{name} が見つからない"
    return m.group(0)


def _citation() -> ClinicalAnnotationDraftCandidate:
    return ClinicalAnnotationDraftCandidate(
        unit_id="file:manual.txt#p1:0-800",
        source="規約文書: manual.txt p.1（0-800）",
        snippet="診断日は確定日とする。",
        payload={"file_role": "reference", "path": "manual.txt", "char_range": [0, 800]},
        stars=3,
    )


# --------------------------------------------------------------------------- #
# 索引が使えないことを、候補が無いことと区別して返す
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_ai_draft_reports_unsearchable_index_as_failure(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """索引が検索不可なら、成功ではなく専用の失敗として返すこと。

    ここを success で返すと、画面は緑の成功表示で「典拠つき0件 / 典拠なし0件」を出す。
    索引の故障が「規約文書に規定が無い項目」として集計へ流れ込む。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _patch_drafter(
        monkeypatch,
        ClinicalDraftResult(items=[], no_hit_count=2, index_searchable=False, llm_model=None),
    )

    resp = await designer_client.post(
        _draft_url(q.id), json={}, headers={"X-CSRF-Token": csrf_token}
    )

    assert resp.status_code == 409
    body = resp.json()
    assert body["success"] is False
    assert body["index_unsearchable"] is True
    assert "索引" in body["message"]
    # 呼んでいないモデル名を名乗らない
    assert "llm_model" not in body


@pytest.mark.asyncio
async def test_ai_draft_reports_no_hit_without_claiming_a_model(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """候補ゼロ（索引は正常）は成功で返し、`no_hit` を出す。モデル名は名乗らない。"""
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _patch_drafter(
        monkeypatch,
        ClinicalDraftResult(
            items=[ClinicalAnnotationDraftItem(link_id="item1", no_citation=True)],
            no_hit_count=1,
            index_searchable=True,
            llm_model=None,  # 候補が無いので LLM を呼んでいない
        ),
    )

    resp = await designer_client.post(
        _draft_url(q.id), json={}, headers={"X-CSRF-Token": csrf_token}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["no_hit"] == 1
    assert body["with_citation"] == 0
    assert body["no_citation"] == 1
    assert body["llm_model"] is None


@pytest.mark.asyncio
async def test_ai_draft_reports_unattempted_items_apart_from_no_citation(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**未試行の項目を「典拠なし」に混ぜないこと**。

    全体デッドラインで打ち切られた項目と、LLM の応答に含まれなかった項目は
    「規約文書に定義が無い」ではない。混ぜると「規約文書を引いても意味が決まらなかった項目」の
    件数が水増しになる——目次チャンクを典拠に採るのとまったく同じ形の汚染である。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _patch_drafter(
        monkeypatch,
        ClinicalDraftResult(
            items=[
                # 候補はあったが LLM が「該当なし」と判断した項目（本物の典拠なし）
                ClinicalAnnotationDraftItem(link_id="item1", no_citation=True),
                # 打ち切られた項目（典拠なしではない）
                ClinicalAnnotationDraftItem(link_id="item2", no_citation=True),
            ],
            no_hit_count=0,
            index_searchable=True,
            llm_model="m",
            unattempted_link_ids=("item2",),
        ),
    )

    resp = await designer_client.post(
        _draft_url(q.id), json={}, headers={"X-CSRF-Token": csrf_token}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    # **典拠なしは1件**（未試行の item2 を含めない）
    assert body["no_citation"] == 1, "未試行が「典拠なし」に混ざっている"
    assert body["unattempted"] == 1
    assert body["unattempted_link_ids"] == ["item2"]
    assert body["message"], "再実行の案内を返すこと"


@pytest.mark.asyncio
async def test_ai_draft_returns_the_three_state_per_item(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**三状態の判定が API から返ること**。

    以前はこの分岐が画面の JavaScript にあり、レンダリング結果への文字列検査でしか
    測れず、**2つの枝の中身を入れ替える変更が全テストを通っていた**。ここが構造的な直しで、**枝の入れ替えはこのテストで落ちる**。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    citation = ClinicalAnnotationDraftCandidate(cand_id=1, unit_id="u1", source="規約 p.1")
    _patch_drafter(
        monkeypatch,
        ClinicalDraftResult(
            items=[
                ClinicalAnnotationDraftItem(link_id="item1", annotation="あ", citation=citation),
                # 候補はあったが LLM が「該当なし」と判断した（本物の典拠なし）
                ClinicalAnnotationDraftItem(link_id="item2", no_citation=True),
                # 打ち切り／応答漏れ（典拠なしではない）
                ClinicalAnnotationDraftItem(link_id="item3", no_citation=True),
            ],
            no_hit_count=0,
            index_searchable=True,
            llm_model="m",
            unattempted_link_ids=("item3",),
        ),
    )

    resp = await designer_client.post(
        _draft_url(q.id), json={}, headers={"X-CSRF-Token": csrf_token}
    )

    assert resp.status_code == 200
    body = resp.json()
    states = {d["link_id"]: d["state"] for d in body["drafts"]}
    assert states == {
        "item1": "with_citation",
        "item2": "no_citation",
        "item3": "unattempted",
    }
    # 語彙は `DraftState.ALL` に閉じている（画面の対応表が引ける値しか返らない）
    assert set(states.values()) <= set(DraftState.ALL)
    # **件数の内訳と食い違わないこと。** 内訳を別の規則で数えると、画面の表示と
    # 集計が食い違う（人が読むのはその両方である）
    assert body["with_citation"] == 1
    assert body["no_citation"] == 1
    assert body["unattempted"] == 1


def test_state_of_prefers_the_citation_over_the_unattempted_list() -> None:
    """典拠を持つ項目は、未試行リストに載っていても「未試行」と表示しない。

    両方成り立つ入力は本来来ないが、来たときに「典拠が入っているのに未試行と表示する」
    側へ倒さないことを固定する。
    """
    citation = ClinicalAnnotationDraftCandidate(cand_id=1, unit_id="u1", source="規約 p.1")
    item = ClinicalAnnotationDraftItem(link_id="item1", citation=citation)
    result = ClinicalDraftResult(
        items=[item],
        no_hit_count=0,
        index_searchable=True,
        llm_model="m",
        unattempted_link_ids=("item1",),
    )
    assert result.state_of(item) == DraftState.WITH_CITATION


@pytest.mark.asyncio
async def test_the_unattempted_ids_follow_the_item_order_not_the_dictionary_order(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`unattempted_link_ids` の並びが**対象項目の順**であること。

    4経路で並びを揃えた（画面の並びと一致させる）が、**この経路のフィクスチャは1要素だった**ので
    `sorted()` へ戻す変異が素通りしていた。**辞書順と項目順が食い違う link_id** を使って測る
    （`item10` は辞書順では `item9` より前に来る）。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = Questionnaire(
        fhir_id="q-clinical-order",
        title="並びテスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-clinical-order",
            "item": [
                {"linkId": "item9", "text": "進行度", "type": "string"},
                {"linkId": "item10", "text": "組織型", "type": "string"},
            ],
        },
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _patch_drafter(
        monkeypatch,
        ClinicalDraftResult(
            items=[
                ClinicalAnnotationDraftItem(link_id="item9", no_citation=True),
                ClinicalAnnotationDraftItem(link_id="item10", no_citation=True),
            ],
            no_hit_count=0,
            index_searchable=True,
            llm_model="m",
            unattempted_link_ids=("item9", "item10"),
        ),
    )

    resp = await designer_client.post(
        _draft_url(q.id), json={}, headers={"X-CSRF-Token": csrf_token}
    )

    assert resp.status_code == 200, resp.text
    # **項目順**（`sorted()` なら `["item10", "item9"]` になる）
    assert resp.json()["unattempted_link_ids"] == ["item9", "item10"]


@pytest.mark.asyncio
async def test_ai_draft_reports_duplicated_responses_apart(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """**矛盾した応答（同じ項目に複数のエントリ）を API が別掲する**こと。

    実際の生成で起きた形: 1エントリ目が有効・2エントリ目が両方 null。最初のエントリを採るので
    下書きは入るが、**矛盾した応答である**ことを集計へ伝えなければ、その項目が分子に
    そのまま入る。**採用したことと、矛盾していたことは別に出す。**
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _patch_drafter(
        monkeypatch,
        ClinicalDraftResult(
            items=[ClinicalAnnotationDraftItem(link_id="item1", annotation="あ")],
            no_hit_count=0,
            index_searchable=True,
            llm_model="m",
            duplicated_link_ids=("item1",),
        ),
    )

    resp = await designer_client.post(
        _draft_url(q.id), json={}, headers={"X-CSRF-Token": csrf_token}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["duplicated"] == 1
    assert body["duplicated_link_ids"] == ["item1"]
    # 未試行とは別の枠である（混ぜると原因が追えない）
    assert body["unattempted"] == 0


@pytest.mark.asyncio
async def test_ai_draft_reports_no_duplicates_for_normal_responses(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """重複が無いときは0件で返すこと（**正常系で発火しない**）。"""
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _patch_drafter(
        monkeypatch,
        ClinicalDraftResult(
            items=[ClinicalAnnotationDraftItem(link_id="item1", annotation="あ")],
            no_hit_count=0,
            index_searchable=True,
            llm_model="m",
        ),
    )

    resp = await designer_client.post(
        _draft_url(q.id), json={}, headers={"X-CSRF-Token": csrf_token}
    )

    body = resp.json()
    assert body["duplicated"] == 0
    assert body["duplicated_link_ids"] == []


@pytest.mark.asyncio
async def test_ai_draft_says_nothing_about_unattempted_when_everything_was_tried(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """全項目を試したときは未試行を0で返し、案内も出さないこと（正常系）。

    「常に未試行あり」と報告する実装は、脅威側のテストだけでは通ってしまう。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _patch_drafter(
        monkeypatch,
        ClinicalDraftResult(
            items=[ClinicalAnnotationDraftItem(link_id="item1", no_citation=True)],
            no_hit_count=0,
            index_searchable=True,
            llm_model="m",
        ),
    )

    resp = await designer_client.post(
        _draft_url(q.id), json={}, headers={"X-CSRF-Token": csrf_token}
    )
    body = resp.json()
    assert body["no_citation"] == 1
    assert body["unattempted"] == 0
    assert body["unattempted_link_ids"] == []
    assert body["message"] is None


@pytest.mark.asyncio
async def test_ai_draft_stamps_model_on_citation_and_candidates(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """典拠つきで返るとき、候補と典拠に生成モデル名が刻まれること。"""
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    # **典拠と候補は別インスタンスにする。** 同じオブジェクトを両方へ渡すと、
    # `routes.py` のどちらか一方の刻印を消しても、もう一方が同じオブジェクトを刻む
    # ので両方の assert が通ってしまう。
    _patch_drafter(
        monkeypatch,
        ClinicalDraftResult(
            items=[
                ClinicalAnnotationDraftItem(
                    link_id="item1",
                    annotation="組織学的に悪性と確定した日を記載する。",
                    citation=_citation(),
                    candidates=[_citation()],
                    no_citation=False,
                )
            ],
            no_hit_count=0,
            index_searchable=True,
            llm_model=_MODEL,
        ),
    )

    resp = await designer_client.post(
        _draft_url(q.id), json={}, headers={"X-CSRF-Token": csrf_token}
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["llm_model"] == _MODEL
    assert body["with_citation"] == 1
    draft = body["drafts"][0]
    assert draft["citation"]["llm_model"] == _MODEL
    assert draft["candidates"][0]["llm_model"] == _MODEL
    # 出典表記は索引から決定的に組み立てたもの（リスト表記を出さない）
    assert draft["citation"]["source"].startswith("規約文書:")
    assert "[" not in draft["citation"]["source"]


# --------------------------------------------------------------------------- #
# 対象項目の選定（人の記述を上書きしない）
# --------------------------------------------------------------------------- #


def _spy_drafter(monkeypatch: pytest.MonkeyPatch, seen: list[list[str]]) -> None:
    """drafter へ渡された link_id を記録するだけのフェイク。"""

    class _SpyDrafter:
        async def draft(self, items, model=None, recorder=None, reference_paths=None):  # noqa: ANN001, ANN202
            # **項目名まで記録する。** `item_text` の引き渡しを潰すと drafter は
            # 空クエリを候補ゼロに落とすので、**全項目が「規約文書に候補が
            # 見つかりませんでした」になり、正当な結果と区別がつかない**。
            # 「規約文書を引いても意味が決まらなかった項目」の件数を 100% に水増しする。
            seen.append([(str(d["link_id"]), d["item_text"]) for d in items])
            return ClinicalDraftResult(
                items=[], no_hit_count=len(items), index_searchable=True, llm_model=None
            )

    monkeypatch.setattr(routes_mod, "AIClinicalDrafter", lambda *a, **kw: _SpyDrafter())


@pytest.mark.asyncio
async def test_ai_draft_targets_only_unfilled_items_in_bulk(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一括は未入力項目だけを対象にし、`link_id` 指定はその1件だけを対象にすること。

    ここが外れると、一括AI下書きが**人が書いた（確認済みを含む）臨床的意味を
    フォーム上で上書きする**。項目単位の引き直しも全項目を対象にし、LLM 呼び出しが
    項目数ぶん走る。明示的に約束している挙動なので、ここで確かめる。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    # item1 に人の記述を保存しておく（item2 は未入力）
    await designer_client.post(
        _clinical_url(q.id),
        json={
            "items": [
                {
                    "link_id": "item1",
                    "item_text": "診断日",
                    "annotation": "人が書いた記述。",
                    "citations": [],
                }
            ]
        },
        headers={"X-CSRF-Token": csrf_token},
    )

    seen: list[list[str]] = []
    _spy_drafter(monkeypatch, seen)

    # 一括: 未入力の item2 だけが対象
    await designer_client.post(_draft_url(q.id), json={}, headers={"X-CSRF-Token": csrf_token})
    assert seen == [[("item2", "進行度")]], seen

    # 項目単位: 記述済みでも指定した1件だけを対象にする（引き直しは想定操作）
    seen.clear()
    await designer_client.post(
        _draft_url(q.id), json={"link_id": "item1"}, headers={"X-CSRF-Token": csrf_token}
    )
    assert seen == [[("item1", "診断日")]], seen


# --------------------------------------------------------------------------- #
# 複数項目の指定（未試行の項目の埋め直し）
# --------------------------------------------------------------------------- #


async def _make_three_item_questionnaire(db_session: AsyncSession) -> Questionnaire:
    q = Questionnaire(
        fhir_id="q-clinical-link-ids",
        title="複数指定テスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-clinical-link-ids",
            "item": [
                {"linkId": "item1", "text": "診断日", "type": "string"},
                {"linkId": "item2", "text": "進行度", "type": "string"},
                {"linkId": "item3", "text": "組織型", "type": "string"},
            ],
        },
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    return q


@pytest.mark.asyncio
async def test_ai_draft_targets_exactly_the_given_link_ids(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`link_ids` は**その集合だけ**を、既存入力の有無に依らず1コールへまとめること。

    一括生成で未試行に終わった項目を埋め直すための経路である。ここが
    外れると次のどちらかになる。**一括へ落ちれば既に生成済みの項目まで引き直す**（同じ
    項目に2回目の生成が来歴へ並び、どちらが採用の版か分からなくなる）。**1件ずつに割れれば
    応答を返さないモデルがある**——つまり埋め直しが物理的に進まない。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_three_item_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    # item1 は記述済み。それでも指定されたら対象に入る（1件指定と同じ扱い）
    await designer_client.post(
        _clinical_url(q.id),
        json={
            "items": [
                {
                    "link_id": "item1",
                    "item_text": "診断日",
                    "annotation": "人が書いた記述。",
                    "citations": [],
                }
            ]
        },
        headers={"X-CSRF-Token": csrf_token},
    )

    seen: list[list[str]] = []
    _spy_drafter(monkeypatch, seen)

    resp = await designer_client.post(
        _draft_url(q.id),
        # **問診票の順と食い違う並びで渡す。** 対象の順は `_editable_fields` が決める。
        json={"link_ids": ["item3", "item1", "item3"]},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 200, resp.text
    # item2（未入力）は入らない＝一括へ落ちていない。重複 item3 は1回だけ。並びは項目順。
    assert seen == [[("item1", "診断日"), ("item3", "組織型")]], seen


@pytest.mark.asyncio
async def test_ai_draft_rejects_link_ids_that_are_not_in_the_questionnaire(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """問診票に無い `link_id` は、黙って捨てずに失敗として返すこと。

    埋め直しは「指定した項目が全部埋まった」前提で先へ進む。綴り違いの1件を黙って落とすと、
    **未試行のまま一括生成を終える**。落ちた件数は成功応答のどこにも出ない（対象が減るだけ）。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_three_item_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    seen: list[list[str]] = []
    _spy_drafter(monkeypatch, seen)

    resp = await designer_client.post(
        _draft_url(q.id),
        json={"link_ids": ["item1", "item404"]},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 400, resp.text
    assert "item404" in resp.json()["message"]
    # **LLM を1回も呼ばない**（既知の分だけ走らせて「成功」と返さない）
    assert seen == [], seen


@pytest.mark.asyncio
async def test_ai_draft_does_not_treat_empty_link_ids_as_bulk(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """空の `link_ids` を一括へ落とさないこと。

    落とすと、対象を指定したつもりの呼び出しが**未入力の全項目**を
    引き直す。1コール数十秒なので数十分ぶんの生成が黙って走り、来歴もその分だけ増える。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_three_item_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    seen: list[list[str]] = []
    _spy_drafter(monkeypatch, seen)

    resp = await designer_client.post(
        _draft_url(q.id),
        json={"link_ids": []},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 400, resp.text
    assert seen == [], seen


# --------------------------------------------------------------------------- #
# ガード（役割 / CSRF / フラグ）を1つずつ
# --------------------------------------------------------------------------- #
#
# `test_ai_flags.py` が見るのは「フラグ off で 403」だけで、あのテストは
# `RequireCollectionLead` を外してもフラグ由来の 403 で通ってしまう。層ごとに
# 単独で落とせるようにする。


@pytest.mark.asyncio
async def test_ai_draft_requires_designer(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """フラグが有効でも、設計者以外のアクティブ役割（収集者）では拒否すること。"""
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_questionnaire(db_session)
    _act_as(collector_client, collector_user, RoleType.COLLECTOR)

    resp = await collector_client.post(
        _draft_url(q.id), json={}, headers={"X-CSRF-Token": csrf_token}
    )

    assert resp.status_code == 403
    # フラグ由来の 403 と取り違えないこと（フラグは有効にしてある）
    assert "ENABLE_AI_DRAFTING" not in resp.text


@pytest.mark.asyncio
async def test_ai_draft_requires_csrf_token(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CSRF トークンが無ければ実行しないこと。"""
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await designer_client.post(_draft_url(q.id), json={})

    # CSRF 由来の拒否であることまで見る。ここを「4xx なら合格」にすると、CSRF 検証を
    # 外しても後段（索引が検索不可の 409）で 4xx になるので、ガードの脱落を見逃す。
    assert resp.status_code == 403
    assert resp.json()["error_code"] == "CSRF_TOKEN_INVALID"


# --------------------------------------------------------------------------- #
# 記述そのものの来歴（確認書の開示に効く）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_saved_annotation_llm_model_survives_citation_removal(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """記述の来歴は典拠行と独立に保存され、典拠を消しても残ること。

    典拠行の `llm_model` だけに依存していると、人が典拠行を削除した瞬間に
    「LLM が書いた記述」が手動として確認書に開示される。
    """
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await designer_client.post(
        _clinical_url(q.id),
        json={
            "items": [
                {
                    "link_id": "item1",
                    "item_text": "診断日",
                    "annotation": "組織学的に悪性と確定した日を記載する。",
                    "citations": [],  # 典拠行を消した状態
                    "llm_model": _MODEL,
                }
            ]
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200

    ann = (
        await db_session.execute(
            select(ClinicalAnnotation).where(
                ClinicalAnnotation.questionnaire_id == q.id,
                ClinicalAnnotation.link_id == "item1",
            )
        )
    ).scalar_one()
    assert ann.llm_model == _MODEL
    assert ann.citation_list == []


@pytest.mark.asyncio
async def test_clinical_page_wires_annotation_provenance(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """画面が記述の来歴を項目カードに載せ、保存時にそれを送る配線を持つこと。

    JS のテストランナーは無いので、レンダリング結果に対する検査で代替する。
    `applyDraft` が刻み `collectItem` が読む経路をどちらか一方でも外すと、AI が
    書いた記述が「手動」として保存される。
    """
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await designer_client.post(
        _clinical_url(q.id),
        json={
            "items": [
                {
                    "link_id": "item1",
                    "item_text": "診断日",
                    "annotation": "組織学的に悪性と確定した日を記載する。",
                    "citations": [],
                    "llm_model": _MODEL,
                }
            ]
        },
        headers={"X-CSRF-Token": csrf_token},
    )

    page = await designer_client.get(_clinical_url(q.id))
    assert page.status_code == 200
    # **JS は静的ファイルへ切り出したので、ページの本文には載らない**。
    # ブラウザが受け取るのはこの2つの合計なので、検査の対象も合計にする。
    body = page.text + _CLINICAL_JS

    # 保存済みの来歴が項目カードへ載る（読み込み → 再保存で失われない）
    assert f'data-llm-model="{_MODEL}"' in body
    # 保存時に項目カードの来歴を送る。**前方一致では足りない。** `card.dataset.llmModelX`
    # のような dataset キーの綴り誤りは JS では黙って undefined になり、AI が書いた記述が
    # 「手動」として保存される。
    assert "llm_model: card.dataset.llmModel || null," in _CLINICAL_JS
    # 読み出し（collectItem）と書き込み（applyDraft）の2か所だけであること。
    assert _CLINICAL_JS.count("card.dataset.llmModel") == 2
    # AI下書きの適用時に来歴を刻む。**代入の右辺まで固定する。** 左辺だけを見ると
    # `card.dataset.llmModel = '';` に変えても文字列が一致して通り、`collectItem` は
    # null を送る。AI が書いた記述が「手動」として保存され確認書に開示される。
    assert (
        "card.dataset.llmModel = (draft.citation && draft.citation.llm_model) || llmModel" in body
    )
    # **記述の差し替えが `if (draft.annotation)` の枝の中にあること。** 枝の外へ出すと、
    # 引き直しで典拠なしが返ったときに記述だけが空になり、除去は典拠側の枝の中なので
    # 前回のAI典拠行が残る。`save_clinical` は記述が空でも典拠を保存するので、drafter 層が
    # 明示的に禁じた「記述の無い典拠」が DB に入る。
    ann_branch = body.split("if (draft.annotation) {", 1)[1].split("}", 1)[0]
    assert ".annotation-input').value = draft.annotation" in ann_branch
    # **AI下書きの結果を画面へ適用する結線。** これが切れると、AI下書きが画面上で
    # 何もしない状態が全件緑で通る。
    # **応答フィールド名まで固定する。** 一括側で `data.drafts` を取り違えると
    # `(undefined || []).forEach` で1件も差し込まれないのに、`data.with_citation` は
    # 生きているので画面は「✓ 典拠つき1件を差し込みました」と**緑の成功表示**を出す。
    # 索引不可なのに緑で成功と出すのと同じ「偽の成功表示」になる。
    assert "(data.drafts || []).forEach(d => applyDraft(d, data.llm_model));" in _js_function(
        body, "aiDraft"
    )
    assert "applyDraft(draft, data.llm_model);" in _js_function(body, "aiDraftItem")
    # AI が挿した典拠行に生成モデル名を渡す（人が足した典拠と区別できなくなる）
    assert "llm_model: draft.citation.llm_model," in body


@pytest.mark.asyncio
async def test_clinical_page_shows_unattempted_apart_from_no_citation(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """画面が**未試行を「典拠なし」と表示しない**こと。

    **「規約文書を引いても意味が決まらなかった項目」の件数を数えるのは人であり、その人が
    読むのはこの画面である。** API の内訳だけ正しくしても、ここで混ざれば分子は汚染される。
    しかも打ち切りは従来 500 エラーだったので、部分結果を返す変更は**「緑の成功 + 典拠なし」へ変える
    退行を作りうる**。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    resp = await designer_client.get(_clinical_url(q.id))
    # **JS は静的ファイルへ切り出したので、ページの本文には載らない**。
    # ブラウザが受け取るのはこの2つの合計なので、検査の対象も合計にする。
    body = resp.text + _CLINICAL_JS

    # **未試行の div の中身を固定する。** 「その文字列がページのどこかに在る」だけを見ると、
    # 項目単位のステータス文（同じ文言を持つ）に助けられて div の文言を差し替えても通る
    # （実測でこのミューテーションが生存した）。
    nocite = re.search(r'<div class="cite-nocite"[^>]*>(.*?)</div>', body, re.S)
    pending = re.search(r'<div class="cite-unattempted"[^>]*>(.*?)</div>', body, re.S)
    assert nocite and pending, "典拠なし／未試行の表示欄が両方あること"
    assert "下書きが返りませんでした" in pending.group(1), pending.group(1)
    assert "規約文書に該当箇所が無いという結果ではありません" in pending.group(1)
    # **2つの文言が同じでないこと**（同じにすると画面上では区別が消える）
    assert nocite.group(1).strip() != pending.group(1).strip()
    assert "規約文書に該当箇所が見つかりませんでした" not in pending.group(1), (
        "未試行の欄が「典拠なし」と同じことを言っている"
    )

    # **判定は画面に無い**。ここに残っているのは対応表だけで、
    # 「どの状態か」はサーバの `draft.state` が決める。
    # **枝を入れ替えるミューテーションはルート層のテストが殺す**
    # （`test_ai_draft_returns_the_three_state_per_item`）——枝の本体を文字列で見る方式は
    # 「その文がある」ことしか見ておらず、末尾に代入を足す形も条件を `false &&` で潰す形も
    # 通ることが実測された。`hidden` の切り替えは実行時の挙動なので、
    # レンダリング結果への検査では原理的に届かない。
    apply_fn = _js_function(body, "applyDraft")
    assert "draft.state" in apply_fn, "画面がサーバの判定を読んでいない"
    assert "unattempted_link_ids" not in apply_fn, (
        "画面が未試行リストと突き合わせて自分で判定している（判定はサーバ側が正本）"
    )
    # 対応表そのもの。**2つの行を入れ替える変更はここで落ちる。**
    assert re.search(
        r"NOTICE_BY_STATE\s*=\s*\{\s*"
        r"with_citation:\s*null,\s*"
        r"no_citation:\s*'\.cite-nocite',\s*"
        r"unattempted:\s*'\.cite-unattempted',\s*\}",
        body,
    ), "state → 表示先の対応表が期待どおりでない"

    bulk = _js_function(body, "aiDraft")
    # 一括のステータスに未試行を必ず出し、**成功として表示しない**
    assert "data.unattempted" in bulk
    assert "未試行" in bulk
    # **表示する「典拠なし」の件数に未試行を足していないこと。** ここを
    # `data.no_citation + pendingCount` にすると、⚠ の併記は残るのに**人が「意味が決まらなかった
    # 項目」として書き写す数**が汚染される。
    assert bulk.count("典拠なし${data.no_citation}件") == 2, (
        "未試行がある枝と無い枝の両方で、典拠なしの件数がそのまま出ること"
    )
    assert "setStatus(msg, pendingCount === 0);" in bulk, "未試行があるのに成功（緑）で表示している"
    # 再実行の案内（部分結果を積み上げれば全項目が埋まる）
    assert "もう一度「AI下書き」を実行すると未入力の項目だけが対象になります" in bulk

    # **項目単位の経路も同じ区別をする。** 一括だけを測ると、こちらの枝を丸ごと削っても
    # 全テストが通る。ここも判定はサーバの `state` を引くだけで、
    # **未試行だけが成功（緑）にならない**ことを対応表の第2要素で固定する。
    per_item = _js_function(body, "aiDraftItem")
    assert "MESSAGE_BY_STATE[draft.state]" in per_item
    assert "unattempted_link_ids" not in per_item, "項目単位の経路が自分で判定している"
    assert re.search(r"unattempted:\s*\[\s*\n?\s*'[^']*未試行[^']*',\s*\n?\s*false,", per_item), (
        "項目単位でも未試行を成功として表示しないこと"
    )
    assert re.search(r"no_citation:\s*\['[^']*典拠なし[^']*',\s*false\]", per_item)
    assert re.search(r"with_citation:\s*\[`[^`]*`,\s*true\]", per_item)


@pytest.mark.asyncio
async def test_clinical_page_replaces_previous_ai_citation(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AI下書きを引き直したとき、前回のAI典拠行を残さない配線を持つこと。

    記述は上書きなのに典拠だけ追加していくと、2回目の下書きで「記述 Y に前回の典拠 A も
    紐付いている」という虚偽の対応が保存される。無関係なページを典拠に据えた記述は
    典拠なしより悪い。人が追加した典拠と保存済みの典拠は印を持たないので残る。

    JS のテストランナーが無いため、レンダリング結果に対する検査で代替する。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    # JS は静的ファイルへ切り出してある。ブラウザが受け取る合計を見る。
    body = (await designer_client.get(_clinical_url(q.id))).text + _CLINICAL_JS

    # AI が挿した行に印を付ける。**条件まで固定する。** `opts.aiDraftX` のような綴り誤りは
    # JS では黙って undefined になり、印が一度も付かない。
    assert "if (opts && opts.aiDraft) row.dataset.aiDraft = '1';" in _CLINICAL_JS
    # 印の綴りが1つに保たれていること。等価な別セレクタ（`[data-ai-draft]` など）で
    # 枝の外に無条件の除去を足されると、下の count が数えられない。
    assert body.count("data-ai-draft") == 1
    # 印を **触る** のは2か所だけである——付ける側（上）と、索引から典拠を選び直したときに
    # 落とす側（`applyPickedUnit`）。選び直した行に印が残ると、その行は次の
    # 引き直しで「前回のAI下書きが挿した行」として消され、**人が選んだ典拠だけが黙って
    # 消える**。件数を2に緩めるだけだと落とす側が消えても通るので、逐語で固定する。
    assert _CLINICAL_JS.count("dataset.aiDraft") == 2
    assert "delete row.dataset.aiDraft;" in _CLINICAL_JS
    # 次の下書きの前に、印の付いた行だけを取り除く
    assert '.cite-row[data-ai-draft="1"]' in _CLINICAL_JS
    assert "r.remove()" in _CLINICAL_JS
    # **印を付ける側の結線そのもの。** 「印を付けるコード」と「印を消すコード」が
    # どこかに在ることしか見ないと、`citationRow(..., {})` のように呼び出し側の
    # 引数を外した状態を検出できない。`data-ai-draft` が一度も付かず、除去処理は
    # 永久に0件を返す。
    assert "}, { aiDraft: true }));" in _CLINICAL_JS
    # 除去は「新しい典拠を挿す枝の中」に**1か所だけ**置く。無条件に消すと、引き直しで
    # 典拠なしが返ったときに前回のAI典拠行だけが消え、前回の記述だけが典拠を失って残る
    # （記述の差し替えは `if (draft.annotation)` の中だけなので残る）。件数まで見ないと、
    # 枝の中の除去を残したまま枝の外へもう1つ足した状態を見逃す。
    assert _CLINICAL_JS.count('.cite-row[data-ai-draft="1"]') == 1
    citation_branch = body.split("if (draft.citation) {", 1)[1].split("} else if", 1)[0]
    assert '.cite-row[data-ai-draft="1"]' in citation_branch
    assert citation_branch.index('.cite-row[data-ai-draft="1"]') < citation_branch.index(
        "list.prepend("
    )

    # 二度押しの防止は**関数ごと**に固定する。片方だけを見ると、数分かかる
    # 当の一括 `aiDraft` から外しても、項目単位 `aiDraftItem` が同じ
    # 文字列を持つので通ってしまう。
    bulk = _js_function(body, "aiDraft")
    per_item = _js_function(body, "aiDraftItem")
    assert "aiDraftItem" not in bulk  # 切り出しが隣の関数まで飲み込んでいないこと
    for name, fn in (("aiDraft", bulk), ("aiDraftItem", per_item)):
        assert "btn.disabled = true" in fn, name
        assert "btn.disabled = false" in fn, name


@pytest.mark.asyncio
async def test_clinical_page_shows_no_hit_count(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """画面が `no_hit`（候補が見つからなかった件数）を表示する配線を持つこと。

    `design_system.html` は表示しているのに clinical 側は参照していなかった。
    表示しないと「規約文書に該当が無い」と「索引が引けていない」を画面で切り分け
    られない。

    件数の表示（一括下書きの結果メッセージ）に加えて、**項目単位の「典拠なし」表示**も
    固定する。「典拠が無い」ことの画面側の担保はこの2つで、項目単位の側は無検証だった
    （要素も切替えも消して全件 green だった）。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    page = await designer_client.get(_clinical_url(q.id))
    assert page.status_code == 200
    # **JS は静的ファイルへ切り出したので、ページの本文には載らない**。
    # ブラウザが受け取るのはこの2つの合計なので、検査の対象も合計にする。
    body = page.text + _CLINICAL_JS
    # 一括下書きの結果に「候補が見つからなかった件数」を出す。**表示する文まで固定
    # する。** `data.no_hit` が在ることだけを見ると、`if (data.no_hit)` の条件を潰して
    # 一度も追記しない状態でも、テンプレートリテラル側の `${data.no_hit}` が残るので
    # 通ってしまう。
    assert (
        "if (data.no_hit) msg += `（うち${data.no_hit}件は規約文書に候補が見つかりませんでした）`;"
        in body
    )

    # 項目カードの「典拠なし」表示。既定は hidden で、項目ごとに切り替える
    assert '<div class="cite-nocite" hidden>規約文書に該当箇所が見つかりませんでした' in body
    apply_draft = _js_function(body, "applyDraft")
    # **出す／隠すの切替は `state` の対応表から引く**。
    # 「典拠なしのときだけ出る」ことは `NOTICE_BY_STATE` と、`state` を返す API 側の
    # テスト（`test_ai_draft_returns_the_three_state_per_item`）で担保する。
    # **走査する集合を丸ごと固定する。** `'.cite-nocite'` だけを見ていると、ループの
    # 配列から `'.cite-unattempted'` を落とす変更が素通りする（未試行の注意書きが
    # 既定の hidden のまま永久に出なくなるが、全スイートが緑になる。実測）。
    # 未試行の表示は未試行と典拠なしを区別するために存在する状態なので、片側だけ固定しない。
    assert "for (const sel of ['.cite-nocite', '.cite-unattempted'])" in apply_draft
    assert "el.hidden = (sel !== shown);" in apply_draft
    assert "NOTICE_BY_STATE[draft.state]" in apply_draft


async def test_duplicate_link_ids_are_counted_and_labelled_individually(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    csrf_token: str,
) -> None:
    """**同一 `link_id` が2件あっても畳み込まない**。

    集計を `{d.link_id: state}` の dict で持つと、2件目が1件目を上書きして

    - `with_citation` が件数分だけ数えられない（典拠つき1件が 0 になる）
    - 各下書きの `state` が「同じ link_id の最後の1件」のものになるので、
      **典拠を持つ項目の画面表示が「典拠なし」に化ける**

    という2つの誤りが同時に出る。どちらも「規約文書を引いても意味が
    決まらなかった項目」の件数を上振れさせる向きである。生成器が作る問診票は
    `linkId` 重複0件だが、手書き・取り込みの問診票では起きる。

    **`state_of` は変えず、集計側の走査で直している。**
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    citation = ClinicalAnnotationDraftCandidate(cand_id=1, unit_id="u1", source="規約 p.1")
    _patch_drafter(
        monkeypatch,
        ClinicalDraftResult(
            items=[
                # 同じ link_id の2件。**先が典拠つき、後が典拠なし**（dict で持つと
                # 後の「典拠なし」が前の「典拠つき」を消す並びにしてある）
                ClinicalAnnotationDraftItem(link_id="item1", annotation="あ", citation=citation),
                ClinicalAnnotationDraftItem(link_id="item1", no_citation=True),
                ClinicalAnnotationDraftItem(link_id="item2", no_citation=True),
            ],
            no_hit_count=0,
            index_searchable=True,
            llm_model=_MODEL,
        ),
    )

    resp = await designer_client.post(
        _draft_url(q.id), json={}, headers={"X-CSRF-Token": csrf_token}
    )

    assert resp.status_code == 200
    body = resp.json()

    # 件数が件数分だけ数えられる
    assert body["with_citation"] == 1, body
    assert body["no_citation"] == 2, body

    # 各 draft の state が自分自身の citation の有無から決まる
    assert len(body["drafts"]) == 3
    assert [d["state"] for d in body["drafts"]] == [
        DraftState.WITH_CITATION,
        DraftState.NO_CITATION,
        DraftState.NO_CITATION,
    ], [d["state"] for d in body["drafts"]]
    # 並びも `drafts` のまま（画面はこの順で表示する）
    assert [d["link_id"] for d in body["drafts"]] == ["item1", "item1", "item2"]


@pytest.mark.asyncio
async def test_ai_draft_passes_the_reference_scope_of_the_questionnaire(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """様式の url で典拠の範囲を引いて drafter へ渡す。載っていない様式は空の範囲になる。"""
    from kawasemi.ai_input.reference_scope import reset_reference_scope_cache

    scopes = tmp_path / "reference_scopes.yaml"
    scopes.write_text(
        "scopes:\n  - questionnaires: [http://example.org/cancer]\n    references: [c.pdf]\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "reference_scopes_file", str(scopes))
    reset_reference_scope_cache()
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_questionnaire(db_session)  # url を持たない＝範囲に載っていない
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    _patch_drafter(
        monkeypatch,
        ClinicalDraftResult(
            items=[],
            no_hit_count=2,
            index_searchable=True,
            llm_model=None,
            reference_scope_empty=True,
        ),
    )

    resp = await designer_client.post(
        _draft_url(q.id), json={}, headers={"X-CSRF-Token": csrf_token}
    )

    assert seen_reference_paths == [frozenset()], "載っていない様式に空の範囲が渡っていない"
    assert resp.status_code == 409
    body = resp.json()
    assert body["success"] is False and body["reference_scope_empty"] is True
    assert "典拠にしてよい規約文書が設定されていない" in body["message"]


@pytest.mark.asyncio
async def test_ai_draft_passes_the_parent_text_to_the_drafter(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """臨床的意味のルートも親の text を drafter へ渡す（「CRP」の下の「数値(mg/dL)」）。"""
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = Questionnaire(
        fhir_id="q-clinical-parent",
        title="親の text のテスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-clinical-parent",
            "item": [
                {
                    "linkId": "crp",
                    "type": "group",
                    "text": "CRP",
                    "item": [{"linkId": "crp_v", "type": "decimal", "text": "数値(mg/dL)"}],
                }
            ],
        },
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    received: list[dict] = []

    class _SpyDrafter:
        async def draft(self, items, model=None, recorder=None, reference_paths=None):  # noqa: ANN001, ANN202
            received.extend(items)
            return ClinicalDraftResult(
                items=[], no_hit_count=len(items), index_searchable=True, llm_model=None
            )

    monkeypatch.setattr(routes_mod, "AIClinicalDrafter", lambda *a, **kw: _SpyDrafter())

    await designer_client.post(
        _draft_url(q.id), json={"link_ids": ["crp_v"]}, headers={"X-CSRF-Token": csrf_token}
    )

    assert [(d["item_text"], d.get("parent_text")) for d in received] == [("数値(mg/dL)", "CRP")]
