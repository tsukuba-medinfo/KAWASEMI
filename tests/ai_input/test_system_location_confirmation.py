"""所在の確認ワークフローと提示候補の保存

**「候補が選ばれている」と「人が確認した」は別物である。** AI 推奨は画面で初期選択される
ので、人が一度も触っていない項目でも `is_resolved` は真になる。この2つを混ぜると、
AI 提案の適合率も、AI が提案しなかった所在の数も、1項目あたりの工数も算出できない。

本ファイルが固定するのは次の3点である。

- `is_resolved`（決着）と `is_confirmed`（人の確認）が別々に読めること
- `draft_system_locations` が**提示した候補の全件をサーバ側で残す**こと（往復させない）
- `save_system` の `confirm` が**項目単位**に確認者と日時を記録し、内容が変われば
  未確認へ戻すこと
"""

from __future__ import annotations

import pathlib
from datetime import UTC, datetime

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.models import (
    AnnotationStatus,
    ClinicalAnnotation,
    InputPlan,
    InputRunbook,
    SystemLocation,
)
from kawasemi.ai_input.routes import _canvas_cell_states, _record_proposed_candidates
from kawasemi.ai_input.schemas import SystemLocationDraftCandidate, SystemLocationDraftItem
from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import RoleType
from kawasemi.models.user import User

_SAVE_PATH = "/ai-input/questionnaires/{qid}/source-survey/system"

_CAND = {
    "source_category": "local_db",
    "location": "DB: X / 表: T / カラム: C",
    "unit_id": "db:T.C",
    "connection_id": "X",
    "payload": {"physical_name": "C"},
    "llm_model": "model-b:27b",
}


# --------------------------------------------------------------------------- #
# モデル: 決着と確認は別の軸
# --------------------------------------------------------------------------- #


def test_resolved_does_not_imply_confirmed():
    """AI 推奨が初期選択されただけの行は、決着済みだが未確認である。"""
    loc = SystemLocation(questionnaire_id=1, link_id="lk1", candidates=[dict(_CAND)])
    # status の既定は Python 側の default が効かない（flush 前）ので明示的に確かめる
    loc.status = AnnotationStatus.DRAFT
    assert loc.is_resolved is True
    assert loc.is_confirmed is False


def test_confirmed_reads_from_status():
    loc = SystemLocation(questionnaire_id=1, link_id="lk1", candidates=[dict(_CAND)])
    loc.status = AnnotationStatus.CONFIRMED
    assert loc.is_confirmed is True


def test_proposed_candidate_list_normalizes_none():
    loc = SystemLocation(questionnaire_id=1, link_id="lk1")
    assert loc.proposed_candidate_list == []


# --------------------------------------------------------------------------- #
# 進捗表のセル: 選択済みを confirmed と読まない
# --------------------------------------------------------------------------- #


def _cells(loc: SystemLocation | None) -> dict[str, str]:
    ann = ClinicalAnnotation(questionnaire_id=1, link_id="lk1", annotation="意味")
    ann.status = AnnotationStatus.CONFIRMED
    return _canvas_cell_states(ann, loc, None, None)


def test_stage_cell_reports_draft_while_the_person_has_not_confirmed():
    """選択済み・未確認の所在は draft。

    ここを confirmed と読むと、進捗表は「人が確認した項目」を実際より多く見せる。
    """
    loc = SystemLocation(questionnaire_id=1, link_id="lk1", candidates=[dict(_CAND)])
    loc.status = AnnotationStatus.DRAFT
    assert _cells(loc)["system"] == "draft"


def test_stage_cell_reports_confirmed_only_after_a_person_confirms():
    loc = SystemLocation(questionnaire_id=1, link_id="lk1", candidates=[dict(_CAND)])
    loc.status = AnnotationStatus.CONFIRMED
    assert _cells(loc)["system"] == "confirmed"


def test_stage_cell_reports_empty_without_candidates():
    loc = SystemLocation(questionnaire_id=1, link_id="lk1", candidates=[])
    loc.status = AnnotationStatus.DRAFT
    assert _cells(loc)["system"] == "empty"
    assert _cells(None)["system"] == "empty"


# --------------------------------------------------------------------------- #
# 保存 API（確認は項目単位）
# --------------------------------------------------------------------------- #


@pytest.fixture
def location_saver(designer_client: AsyncClient, designer_user: User) -> AsyncClient:
    token = create_access_token(data={"sub": str(designer_user.id), "act": RoleType.DESIGNER})
    designer_client.cookies.set(settings.jwt_cookie_name, token)
    return designer_client


async def _questionnaire(db_session: AsyncSession, fhir_id: str) -> Questionnaire:
    q = Questionnaire(
        fhir_id=fhir_id,
        title="所在確認テスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": fhir_id,
            "item": [
                {"linkId": "lk1", "text": "[1] 登録番号", "type": "string"},
                {"linkId": "lk2", "text": "[2] 氏名", "type": "string"},
            ],
        },
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    return q


async def _post(
    client: AsyncClient, csrf_token: str, qid: int, body: dict, *, confirm: bool = False
) -> tuple[int, dict]:
    path = _SAVE_PATH.format(qid=qid) + ("?confirm=true" if confirm else "")
    resp = await client.post(path, json=body, headers={"X-CSRF-Token": csrf_token})
    return resp.status_code, (resp.json() if resp.content else {})


async def _row(db_session: AsyncSession, qid: int, link_id: str) -> SystemLocation:
    return (
        await db_session.execute(
            select(SystemLocation).where(
                SystemLocation.questionnaire_id == qid, SystemLocation.link_id == link_id
            )
        )
    ).scalar_one()


def _body(link_id: str, candidates: list[dict]) -> dict:
    return {"items": [{"link_id": link_id, "item_text": "t", "candidates": candidates}]}


@pytest.mark.asyncio
async def test_a_plain_save_does_not_confirm(
    location_saver: AsyncClient, csrf_token: str, db_session: AsyncSession
):
    """`confirm` の無い保存は確認済みにしない。

    一括保存で確定にできると、AI 推奨のまま素通りした項目が「人が確認した」に化ける。
    """
    q = await _questionnaire(db_session, "q-loc-confirm-plain")
    status, body = await _post(location_saver, csrf_token, q.id, _body("lk1", [dict(_CAND)]))
    assert status == 200 and body["confirmed"] == 0
    row = await _row(db_session, q.id, "lk1")
    await db_session.refresh(row)
    assert row.is_resolved is True
    assert row.is_confirmed is False
    assert row.confirmed_by_id is None and row.confirmed_at is None


@pytest.mark.asyncio
async def test_confirm_records_who_and_when(
    location_saver: AsyncClient,
    csrf_token: str,
    db_session: AsyncSession,
    designer_user: User,
):
    q = await _questionnaire(db_session, "q-loc-confirm-records")
    before = datetime.now(UTC)
    status, body = await _post(
        location_saver, csrf_token, q.id, _body("lk1", [dict(_CAND)]), confirm=True
    )
    assert status == 200 and body["confirmed"] == 1
    row = await _row(db_session, q.id, "lk1")
    await db_session.refresh(row)
    assert row.is_confirmed is True
    assert row.confirmed_by_id == designer_user.id
    assert row.confirmed_at is not None
    stamped = row.confirmed_at
    if stamped.tzinfo is None:
        stamped = stamped.replace(tzinfo=UTC)
    assert stamped >= before.replace(microsecond=0)


@pytest.mark.asyncio
async def test_confirm_touches_only_the_items_in_the_request(
    location_saver: AsyncClient, csrf_token: str, db_session: AsyncSession
):
    """確認は項目単位である。

    一括でしか確認できないと `confirmed_at` が全項目で揃い、1項目あたりの工数
    が後から復元できない。
    """
    q = await _questionnaire(db_session, "q-loc-confirm-per-item")
    await _post(location_saver, csrf_token, q.id, _body("lk1", [dict(_CAND)]))
    await _post(location_saver, csrf_token, q.id, _body("lk2", [dict(_CAND)]))
    await _post(location_saver, csrf_token, q.id, _body("lk1", [dict(_CAND)]), confirm=True)

    lk1 = await _row(db_session, q.id, "lk1")
    lk2 = await _row(db_session, q.id, "lk2")
    await db_session.refresh(lk1)
    await db_session.refresh(lk2)
    assert lk1.is_confirmed is True
    assert lk2.is_confirmed is False


@pytest.mark.asyncio
async def test_an_empty_selection_cannot_be_confirmed(
    location_saver: AsyncClient, csrf_token: str, db_session: AsyncSession
):
    """候補が無い項目は確定できない（確認する内容が無いため draft のまま）。"""
    q = await _questionnaire(db_session, "q-loc-confirm-empty")
    status, body = await _post(location_saver, csrf_token, q.id, _body("lk1", []), confirm=True)
    assert status == 200 and body["confirmed"] == 0
    row = await _row(db_session, q.id, "lk1")
    await db_session.refresh(row)
    assert row.is_confirmed is False


@pytest.mark.asyncio
async def test_changing_the_choice_drops_back_to_unconfirmed(
    location_saver: AsyncClient, csrf_token: str, db_session: AsyncSession
):
    """選び直した項目は未確認へ戻り、帰属も消える（確認は特定の内容に対する行為である）。"""
    q = await _questionnaire(db_session, "q-loc-confirm-rechoice")
    await _post(location_saver, csrf_token, q.id, _body("lk1", [dict(_CAND)]), confirm=True)
    other = dict(_CAND, unit_id="db:T.OTHER", location="DB: X / 表: T / カラム: OTHER")
    await _post(location_saver, csrf_token, q.id, _body("lk1", [other]))

    row = await _row(db_session, q.id, "lk1")
    await db_session.refresh(row)
    assert row.is_confirmed is False
    assert row.confirmed_by_id is None and row.confirmed_at is None


@pytest.mark.asyncio
async def test_editing_without_changing_the_choice_keeps_the_original_confirmation(
    location_saver: AsyncClient,
    csrf_token: str,
    db_session: AsyncSession,
    designer_user: User,
):
    """内容が変わらない保存は、元の確認者・日時を奪わない（`save_clinical` と同じ規則）。"""
    q = await _questionnaire(db_session, "q-loc-confirm-keep")
    await _post(location_saver, csrf_token, q.id, _body("lk1", [dict(_CAND)]), confirm=True)
    row = await _row(db_session, q.id, "lk1")
    await db_session.refresh(row)
    first_at = row.confirmed_at

    status, body = await _post(location_saver, csrf_token, q.id, _body("lk1", [dict(_CAND)]))
    assert status == 200 and body["confirmed"] == 0
    await db_session.refresh(row)
    assert row.is_confirmed is True
    assert row.confirmed_by_id == designer_user.id
    assert row.confirmed_at == first_at


# --------------------------------------------------------------------------- #
# 提示候補の記録
# --------------------------------------------------------------------------- #


def _draft(link_id: str, n: int) -> SystemLocationDraftItem:
    return SystemLocationDraftItem(
        link_id=link_id,
        candidates=[
            SystemLocationDraftCandidate(
                source_category="local_db",
                location=f"DB: X / 表: T / カラム: C{i}",
                unit_id=f"db:T.C{i}",
                stars=3 - (i % 3),
                score=0.9 - i * 0.01,
                recommended=(i == 0),
            )
            for i in range(n)
        ],
    )


@pytest.mark.asyncio
async def test_the_whole_slate_is_recorded_server_side(db_session: AsyncSession):
    """提示した候補は全件がサーバ側に残る。

    以前は候補がブラウザの JS 状態にしか無く、保存すると選ばれた1件だけが残った。
    AI 提案の適合率と、AI が提案しなかった所在の数は、どちらも「AI が提案した候補」と
    「人が選んだもの」の関係を見る指標なので、
    提示の側が消えると分母が作れない。
    """
    q = await _questionnaire(db_session, "q-loc-proposed")
    locations: dict[str, SystemLocation] = {}
    await _record_proposed_candidates(
        db_session, q.id, [_draft("lk1", 8)], locations, {"lk1": "[1] 登録番号"}
    )

    row = await _row(db_session, q.id, "lk1")
    await db_session.refresh(row)
    assert len(row.proposed_candidate_list) == 8
    assert row.proposed_candidate_list[0]["recommended"] is True
    assert row.proposed_candidate_list[7]["unit_id"] == "db:T.C7"
    # 決着は空のまま。提示は決着ではない。
    assert row.candidate_list == []
    assert row.is_resolved is False
    # AI 案の提示は人の保存操作ではないので、職務分離の帰属へ混ぜない。
    assert row.updated_by_id is None


@pytest.mark.asyncio
async def test_a_zero_candidate_item_is_recorded_as_an_empty_slate(db_session: AsyncSession):
    """候補0件も空配列で残す。「まだ出していない」（null）と別の状態である。

    `null` と `[]` が同じ状態だと、生成が全項目に届いたかを後から点検できない
    （記述が空・典拠0件・draft を1つの状態にしてしまうのと同じ型の取りこぼし）。
    """
    q = await _questionnaire(db_session, "q-loc-proposed-zero")
    await _record_proposed_candidates(
        db_session, q.id, [_draft("lk1", 0), _draft("lk2", 2)], {}, {"lk1": "t", "lk2": "t"}
    )
    zero = await _row(db_session, q.id, "lk1")
    await db_session.refresh(zero)
    assert zero.proposed_candidates == []  # 出したが候補0件
    assert zero.proposed_candidates is not None

    # 一度も対象にならなかった項目は null のまま（この問診票には他に項目が無いので、
    # 「出していない」側は行そのものが無い、という形で区別される）
    hit = await _row(db_session, q.id, "lk2")
    await db_session.refresh(hit)
    assert len(hit.proposed_candidate_list) == 2


@pytest.mark.asyncio
async def test_a_new_slate_replaces_the_previous_one_without_touching_the_decision(
    db_session: AsyncSession,
):
    """引き直しは提示だけを入れ替え、人の決着には触らない。"""
    q = await _questionnaire(db_session, "q-loc-proposed-redraw")
    row = SystemLocation(
        questionnaire_id=q.id, link_id="lk1", candidates=[dict(_CAND)], proposed_candidates=[]
    )
    db_session.add(row)
    await db_session.commit()

    await _record_proposed_candidates(
        db_session, q.id, [_draft("lk1", 3)], {"lk1": row}, {"lk1": "t"}
    )
    await db_session.refresh(row)
    assert len(row.proposed_candidate_list) == 3
    assert row.candidate_list == [dict(_CAND)]


@pytest.mark.asyncio
async def test_a_new_slate_drops_the_confirmation(db_session: AsyncSession):
    """提示が入れ替わった項目は未確認へ戻る。

    確認は「この提示を見て、この所在でよいと判断した」記録なので、提示が変われば対象が
    変わる。`save_system` の「選び直したら剥がれる」を、引き直しの経路にも適用する。
    """
    q = await _questionnaire(db_session, "q-loc-redraw-unconfirms")
    row = SystemLocation(
        questionnaire_id=q.id,
        link_id="lk1",
        candidates=[dict(_CAND)],
        proposed_candidates=[],
        status=AnnotationStatus.CONFIRMED,
        confirmed_at=datetime.now(UTC),
        confirmed_by_id=None,
    )
    db_session.add(row)
    await db_session.commit()

    await _record_proposed_candidates(
        db_session, q.id, [_draft("lk1", 3)], {"lk1": row}, {"lk1": "t"}
    )
    await db_session.refresh(row)
    assert row.is_confirmed is False
    assert row.confirmed_at is None
    # 人の決着そのものは残す（消すのは確認であって所在ではない）
    assert row.candidate_list == [dict(_CAND)]


@pytest.mark.asyncio
async def test_an_identical_slate_keeps_the_confirmation(db_session: AsyncSession):
    """提示が1件も変わらなければ確認は剥がさない（押し直しで人の記録を捨てない）。"""
    q = await _questionnaire(db_session, "q-loc-redraw-identical")
    slate = [c.model_dump() for c in _draft("lk1", 3).candidates]
    stamped = datetime.now(UTC)
    row = SystemLocation(
        questionnaire_id=q.id,
        link_id="lk1",
        candidates=[dict(_CAND)],
        proposed_candidates=slate,
        status=AnnotationStatus.CONFIRMED,
        confirmed_at=stamped,
    )
    db_session.add(row)
    await db_session.commit()

    await _record_proposed_candidates(
        db_session, q.id, [_draft("lk1", 3)], {"lk1": row}, {"lk1": "t"}
    )
    await db_session.refresh(row)
    assert row.is_confirmed is True
    assert row.confirmed_at is not None


@pytest.mark.asyncio
async def test_saving_never_writes_the_slate_from_the_client(
    location_saver: AsyncClient, csrf_token: str, db_session: AsyncSession
):
    """保存経路はクライアントの申告で提示候補を書き換えない。

    提示したのはサーバであり、往復させると画面の不具合や改変が AI 提案の適合率の分母と、
    AI が提案しなかった所在の数え方を動かす。受け口のスキーマが知らないキーなので、送られても黙って捨てられる。
    """
    q = await _questionnaire(db_session, "q-loc-proposed-not-from-client")
    await _record_proposed_candidates(db_session, q.id, [_draft("lk1", 5)], {}, {"lk1": "t"})

    body = _body("lk1", [dict(_CAND)])
    body["items"][0]["proposed_candidates"] = [{"source_category": "manual", "location": "偽"}]
    status, _ = await _post(location_saver, csrf_token, q.id, body)
    assert status == 200

    row = await _row(db_session, q.id, "lk1")
    await db_session.refresh(row)
    assert len(row.proposed_candidate_list) == 5
    assert row.proposed_candidate_list[0]["unit_id"] == "db:T.C0"


@pytest.mark.asyncio
async def test_the_edit_page_returns_the_slate_and_the_confirmation_state(
    location_saver: AsyncClient, csrf_token: str, db_session: AsyncSession
):
    """画面の初期状態に提示候補と確認状態が載ること（読み込み直しても選び直せる）。"""
    q = await _questionnaire(db_session, "q-loc-initial-state")
    await _record_proposed_candidates(db_session, q.id, [_draft("lk1", 8)], {}, {"lk1": "t"})
    await _post(location_saver, csrf_token, q.id, _body("lk1", [dict(_CAND)]), confirm=True)

    resp = await location_saver.get(f"/ai-input/questionnaires/{q.id}/source-survey/system")
    assert resp.status_code == 200
    html = resp.text
    # 8件すべてが初期状態へ載る（1件へ畳まれない）
    assert '\\"db:T.C7\\"' in html or '"db:T.C7"' in html
    assert '\\"confirmed\\": true' in html or '"confirmed": true' in html


@pytest.mark.asyncio
async def test_every_item_gets_its_own_confirm_control(
    location_saver: AsyncClient, db_session: AsyncSession
):
    """項目ごとに確認ボタンが出ること。

    確認が項目単位でなくなると `confirmed_at` が揃い、1項目あたりの工数が復元
    できなくなる。ボタンが消えても他のテストは緑のままなので、ここで固定する。
    """
    q = await _questionnaire(db_session, "q-loc-confirm-control")
    resp = await location_saver.get(f"/ai-input/questionnaires/{q.id}/source-survey/system")
    assert resp.status_code == 200
    # 問診票の2項目それぞれに、その link_id を渡す確認ボタンがある
    assert "confirmItem(this, 'lk1')" in resp.text
    assert "confirmItem(this, 'lk2')" in resp.text
    # 確認は**その項目の現在の選択を添えて**送る（`collectItems` が組み立てた形）。
    # 選択を送らないと `candidates` が空になり、サーバ側は draft へ落とす。
    #
    # **この配線は静的 JS 側にある**。ページ本文には
    # `onclick="confirmItem(...)"` だけが載る。構文エラーは CI の「JS 構文チェック」が
    # 落とすが、配線が生きているかは文字列でしか見られない。
    static_js = (
        pathlib.Path(__file__).resolve().parents[2]
        / "kawasemi"
        / "web"
        / "static"
        / "js"
        / "design_system.js"
    ).read_text(encoding="utf-8")
    assert "collectItems().find(i => i.link_id === linkId)" in static_js
    assert "system?confirm=true" in static_js


# --------------------------------------------------------------------------- #
# 他成果物との対称性
# --------------------------------------------------------------------------- #


def test_the_four_design_artefacts_all_carry_confirmation_attribution():
    """所在にだけ確認の帰属が無い、という非対称を残さない（ALCOA+）。"""
    for model in (ClinicalAnnotation, InputRunbook, InputPlan, SystemLocation):
        fields = set(model.__table__.columns.keys())
        missing = {"status", "confirmed_by_id", "confirmed_at"} - fields
        assert not missing, f"{model.__name__} に {missing} が無い"


# --------------------------------------------------------------------------- #
# 診療記録の本文への受け皿
#
# **検索では診療記録の本文に届きにくい。** 文書種別の単位の `search_text` は登録様式の
# 語彙を含まないことが多く、上位8件に来ない。そのときは受け皿だけが経路になる。
# 受け皿の `unit_id` は施設の設定（`site/location_rules.yaml`）から読む。ここでは架空の
# 文書体系（`DEMO.DOCUMENT.NOTE_TEXT` と種別 `NOTE` / `ADMIT` / `OP` / `SUMMARY`）を使う。
# --------------------------------------------------------------------------- #

_DOC_PREFIX = "db:DEMO.DOCUMENT.NOTE_TEXT#"
_FALLBACK_IDS = tuple(f"{_DOC_PREFIX}{c}" for c in ("NOTE", "ADMIT", "OP", "SUMMARY"))


@pytest.fixture
def fallback_rules(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> tuple[str, ...]:
    """架空の受け皿を `location_rules.yaml` に書いて読ませる。"""
    from kawasemi.core.config import settings

    p = tmp_path / "location_rules.yaml"
    p.write_text(
        "fallback_unit_ids:\n" + "".join(f'  - "{u}"\n' for u in _FALLBACK_IDS),
        encoding="utf-8",
    )
    monkeypatch.setattr(settings, "location_rules_file", str(p))
    return _FALLBACK_IDS


def test_the_fallback_never_displaces_a_searched_candidate(fallback_rules: tuple[str, ...]):
    """受け皿は上位8件を1件も押し出さない。

    1表あたりの上限も置いていない。候補を減らす仕組みを足さないため、押し出しが
    **構造上起きない**ことをここで固定する。
    """
    from kawasemi.ai_input.resource_drafter import _TOP_K

    searched = [
        SystemLocationDraftCandidate(source_category="local_db", location=f"c{i}", unit_id=f"u{i}")
        for i in range(_TOP_K)
    ]
    fallbacks = [
        SystemLocationDraftCandidate(
            source_category="local_db", location=u, unit_id=u, fallback=True
        )
        for u in fallback_rules
    ]
    item = SystemLocationDraftItem(link_id="lk1", candidates=[*searched, *fallbacks])

    kept = [c for c in item.candidates if not c.fallback]
    assert len(kept) == _TOP_K
    assert [c.unit_id for c in kept] == [f"u{i}" for i in range(_TOP_K)]
    # 受け皿は必ず後ろにある（前に入ると「もっと見る」の内外が入れ替わる）
    assert all(c.fallback for c in item.candidates[_TOP_K:])


def test_a_fallback_candidate_is_never_recommended(fallback_rules: tuple[str, ...]):
    """受け皿に AI推奨が付かない。

    LLM の選定が済んだ**後**に足すので、LLM は選びようがない。初期選択もされない。
    """
    for u in fallback_rules:
        c = SystemLocationDraftCandidate(
            source_category="local_db", location=u, unit_id=u, fallback=True
        )
        assert c.recommended is False
        assert c.score is None
        assert c.stars == 0


def test_the_fallback_rules_keep_order_and_reject_duplicates(
    fallback_rules: tuple[str, ...], tmp_path: pathlib.Path
):
    """受け皿は書いた順に並び、重複は設定の誤りとして止める。

    `unit_id` の形（`db:<表>#<種別>`）を保つのは、手動入力だと `unit_id` が null になり、
    ステップ3が所在を機械可読に受け取れないため（受け皿を置いた理由そのもの）。
    """
    from kawasemi.ai_input.location_rules import LocationRulesError, load_location_rules

    assert load_location_rules().fallback_unit_ids == fallback_rules
    dup = tmp_path / "dup.yaml"
    dup.write_text(f'fallback_unit_ids: ["{_FALLBACK_IDS[0]}", "{_FALLBACK_IDS[0]}"]\n')
    with pytest.raises(LocationRulesError):
        load_location_rules(str(dup))
    bad = tmp_path / "bad.yaml"
    bad.write_text("fallback_unit_ids: db:X#Y\n")
    with pytest.raises(LocationRulesError):
        load_location_rules(str(bad))


def test_the_example_rules_point_at_document_body_units():
    """雛形（`site.example/location_rules.yaml`）の受け皿は文書本文の単位を指す。"""
    from kawasemi.ai_input.location_rules import load_location_rules

    example = pathlib.Path(__file__).resolve().parents[2] / "site.example/location_rules.yaml"
    ids = load_location_rules(str(example)).fallback_unit_ids
    assert ids
    for u in ids:
        assert u.startswith("db:") and "#" in u


def test_without_rules_no_fallback_is_added():
    """施設の規則が無ければ受け皿を足さない（どの文書種別を指すかはコードに持たない）。"""
    from kawasemi.ai_input.location_rules import load_location_rules

    assert load_location_rules().fallback_unit_ids == ()
    assert _fallback_codes({}) == []


class _FallbackStore:
    """`_fallback_candidates` が触る2つ（`get_unit` と `column_fill`）だけを持つ索引。"""

    def __init__(self, reach: dict) -> None:  # noqa: ANN001
        from kawasemi.ai_input.resource_index.column_fill import ColumnFillIndex

        self._units = {
            u: _doc_unit(u.rsplit("#", 1)[1], u.rsplit("#", 1)[1]) for u in _FALLBACK_IDS
        }
        self.column_fill = ColumnFillIndex(reach=reach)

    def get_unit(self, unit_id: str):  # noqa: ANN201
        return self._units.get(unit_id)


def _fallback_codes(reach: dict) -> list[str]:  # noqa: ANN001
    from kawasemi.ai_input.resource_drafter import AIResourceDrafter
    from kawasemi.ai_input.resource_index.connections import get_registry

    cands = AIResourceDrafter()._fallback_candidates(_FallbackStore(reach), get_registry())  # type: ignore[arg-type]
    assert all(c.fallback for c in cands)
    return [str(c.unit_id).rsplit("#", 1)[1] for c in cands]


def test_the_fallback_drops_a_type_measured_to_have_no_headers(fallback_rules: tuple[str, ...]):
    """受け皿も検索の候補と同じ足切りを通る。

    受け皿は検索を経ないので、`_retrieve` の `is_acquirable` が掛からなかった。文書が
    1件も無い種別を受け皿に置くと、全項目の提示枠に出続ける。
    """
    first = fallback_rules[0]
    codes = _fallback_codes({first: _reach(header=False, body=False)})
    assert first.rsplit("#", 1)[1] not in codes
    assert len(codes) == len(fallback_rules) - 1


def test_the_fallback_keeps_a_type_whose_body_is_not_linked_and_an_unmeasured_type(
    fallback_rules: tuple[str, ...],
):
    """本文が未連携の種別と、測っていない種別は落とさない（検索の側と同じ）。"""
    first = fallback_rules[0]
    codes = _fallback_codes({first: _reach(header=True, body=False)})
    assert codes == [u.rsplit("#", 1)[1] for u in fallback_rules]


@pytest.mark.asyncio
async def test_the_slate_and_the_fallback_are_separable_after_saving(db_session: AsyncSession):
    """保存後も検索由来と受け皿由来を機械的に分けられる（AI 提案の適合率を測るため）。"""
    q = await _questionnaire(db_session, "q-loc-fallback-separable")
    searched = SystemLocationDraftCandidate(
        source_category="local_db", location="表T カラムC", unit_id="db:T.C", recommended=True
    )
    fb = SystemLocationDraftCandidate(
        source_category="local_db",
        location="文書 経過記録",
        unit_id=f"{_DOC_PREFIX}NOTE",
        fallback=True,
    )
    await _record_proposed_candidates(
        db_session,
        q.id,
        [SystemLocationDraftItem(link_id="lk1", candidates=[searched, fb])],
        {},
        {"lk1": "t"},
    )
    row = await _row(db_session, q.id, "lk1")
    await db_session.refresh(row)
    proposed = row.proposed_candidate_list
    assert [c["fallback"] for c in proposed] == [False, True]
    # 検索の性能を測るときはこの1件だけを見る
    assert len([c for c in proposed if not c["fallback"]]) == 1


# --------------------------------------------------------------------------- #
# 文書種別の所在記述
#
# 本文はすべて文書の表の同じ本文列に入っており、種別で分かれるのは文書ヘッダの表側の
# 絞り込み条件だけである。種別を書かないと `location` が全単位で同一になり、
# **別の場所を同じ文字列で表す**ことになる。
# --------------------------------------------------------------------------- #


def _doc_unit(code: str, name: str):
    from kawasemi.ai_input.resource_index.schema import Kind, ResourceUnit

    return ResourceUnit(
        unit_id=f"{_DOC_PREFIX}{code}",
        kind=Kind.DB,
        label=name,
        search_text=name,
        snippet="",
        payload={
            "connection_id": "ehr",
            "db_qualified": "DEMO.DOCUMENT",
            "table_logical": "文書",
            "physical_name": "NOTE_TEXT",
            "logical_name": "本文",
            "doc_type_code": code,
            "doc_type_name": name,
            "doc_filter_table": "DEMO.DOC_HEADER",
            "doc_filter_column": "NOTE_TYPE",
        },
    )


def test_two_document_types_do_not_share_one_location_string():
    """別の文書種別が同じ所在記述にならない。

    ここが同一だと、**手順書の生成（ステップ2）は `location` しか受け取らない**ので、
    文書を所在とする項目がすべて「文書の本文列を読め」という同じ入力になる。
    """
    from kawasemi.ai_input.resource_index.derive import build_location

    a = build_location(_doc_unit("NOTE", "経過記録"))
    b = build_location(_doc_unit("RAD", "画像診断レポート"))
    assert a != b
    assert "NOTE（経過記録）" in a
    assert "RAD（画像診断レポート）" in b


def test_the_location_carries_the_filter_condition():
    """絞り込み条件も所在記述に載る（ステップ2 が種別の列を書けるようにする）。"""
    from kawasemi.ai_input.resource_index.derive import build_location

    loc = build_location(_doc_unit("OP", "手術記録"))
    assert "DEMO.DOC_HEADER.NOTE_TYPE='OP'" in loc
    # 本文の置き場所も従来どおり残す（種別だけでは取りにいけない）
    assert "DEMO.DOCUMENT（文書）" in loc
    assert "NOTE_TEXT（本文）" in loc


def test_a_plain_column_location_is_unchanged():
    """文書種別を持たない普通のカラムは従来どおりの記述である（回帰の確認）。"""
    from kawasemi.ai_input.resource_index.derive import build_location
    from kawasemi.ai_input.resource_index.schema import Kind, ResourceUnit

    unit = ResourceUnit(
        unit_id="db:T.C",
        kind=Kind.DB,
        label="C",
        search_text="C",
        snippet="",
        payload={
            "connection_id": "X",
            "db_qualified": "S.T",
            "table_logical": "表",
            "physical_name": "C",
            "logical_name": "列",
        },
    )
    assert build_location(unit) == "DB: X / 表: S.T（表） / カラム: C（列）"
    assert "文書種別" not in build_location(unit)


# --------------------------------------------------------------------------- #
# 「患者で絞らない表」の宣言は索引の値だけを採る
# --------------------------------------------------------------------------- #


class _StubStore:
    """`get_unit` だけを持つ索引の二重。`declared` に載る unit_id だけが宣言を持つ。"""

    def __init__(self, declared: set[str], plain: set[str]) -> None:
        self._declared = declared
        self._plain = plain

    def get_unit(self, unit_id: str):  # noqa: ANN201
        from types import SimpleNamespace

        if unit_id in self._declared:
            return SimpleNamespace(payload={"patient_scoped": False})
        if unit_id in self._plain:
            return SimpleNamespace(payload={})
        return None


async def _saved_payload(
    client: AsyncClient,
    csrf_token: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    *,
    fhir_id: str,
    unit_id: str | None,
    payload: dict,
) -> dict:
    import kawasemi.ai_input.routes as routes_mod

    monkeypatch.setattr(
        routes_mod,
        "get_store",
        lambda *a, **k: _StubStore(declared={"db:MST.NAME"}, plain={"db:PAT.VAL"}),
    )
    q = await _questionnaire(db_session, fhir_id)
    cand = {**_CAND, "unit_id": unit_id, "payload": payload}
    status, _ = await _post(client, csrf_token, q.id, _body("lk1", [cand]))
    assert status == 200
    row = await _row(db_session, q.id, "lk1")
    await db_session.refresh(row)
    return row.candidate_list[0]["payload"]


@pytest.mark.asyncio
async def test_a_client_cannot_declare_a_table_unscoped_by_hand(
    location_saver: AsyncClient,
    csrf_token: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    """手入力（`unit_id` なし）や、宣言を持たない索引ユニットへの `patient_scoped: false` は落とす。

    宣言は WHERE 句を省く SQL を組ませる。**自己申告を採ると、保存権限のある人が患者データの
    表を「患者で絞らない」と偽って、全患者の行を返す SQL を組ませられる**。施設の確認を経た宣言は索引側にだけ置く。
    """
    for n, unit_id in enumerate([None, "db:PAT.VAL", "db:NOT.IN.INDEX"]):
        payload = await _saved_payload(
            location_saver,
            csrf_token,
            db_session,
            monkeypatch,
            fhir_id=f"q-scope-forged-{n}",
            unit_id=unit_id,
            payload={"physical_name": "C", "patient_scoped": False},
        )
        assert "patient_scoped" not in payload, (unit_id, payload)
        assert payload["physical_name"] == "C", "他のキーまで落としてはいけない"


@pytest.mark.asyncio
async def test_the_index_declaration_survives_the_round_trip(
    location_saver: AsyncClient,
    csrf_token: str,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
):
    """索引が宣言しているユニットは、画面から往復しても宣言が残る（索引の値が採られる）。"""
    payload = await _saved_payload(
        location_saver,
        csrf_token,
        db_session,
        monkeypatch,
        fhir_id="q-scope-declared",
        unit_id="db:MST.NAME",
        payload={"physical_name": "NAME", "patient_scoped": False},
    )
    assert payload["patient_scoped"] is False

    # 索引は宣言していないのにクライアントが `True` 等を送っても、宣言としては残らない
    other = await _saved_payload(
        location_saver,
        csrf_token,
        db_session,
        monkeypatch,
        fhir_id="q-scope-true",
        unit_id="db:PAT.VAL",
        payload={"physical_name": "VAL", "patient_scoped": True},
    )
    assert "patient_scoped" not in other


# --------------------------------------------------------------------------- #
# 「所在は特定できるが現在の取得経路では引けない」の表示
#
# ヘッダはあるが本文が取得先の DB へ未連携の文書種別（例: 病理レポート）は、候補から**落とさず**、
# 画面で他の候補と区別できるようにする。**表示だけの印**で、保存・測定の記録には載らない。
# --------------------------------------------------------------------------- #


class _ReachStore:
    """`column_fill.reach_of_id` だけを持つ索引の二重。"""

    def __init__(self, reach: dict[str, object]) -> None:
        from types import SimpleNamespace

        self.column_fill = SimpleNamespace(reach_of_id=lambda uid: reach.get(uid))


def _reach(*, header: bool, body: bool):  # noqa: ANN202
    from kawasemi.ai_input.resource_index.column_fill import DocTypeReach

    return DocTypeReach(
        has_header=header, has_body=body, header_count=None, body_count=None, measured_at=""
    )


def test_only_a_candidate_with_headers_but_no_body_gets_the_mark() -> None:
    from kawasemi.ai_input.routes import _annotate_reach

    store = _ReachStore(
        {
            "db:PATH": _reach(header=True, body=False),
            "db:NOTE": _reach(header=True, body=True),
            "db:EMPTY": _reach(header=False, body=False),
        }
    )
    cands = [{"unit_id": u} for u in ("db:PATH", "db:NOTE", "db:EMPTY", "db:UNMEASURED", None)]

    marked = _annotate_reach(cands, store)  # type: ignore[arg-type]

    assert [c.get("reach") for c in marked] == ["body_unreachable", None, None, None, None]
    assert all("reach" not in c for c in cands), "元の候補を書き換えない"


@pytest.mark.asyncio
async def test_the_display_mark_is_not_saved(
    location_saver: AsyncClient, csrf_token: str, db_session: AsyncSession
):
    """画面が往復させても、`reach` は保存に届かない（測定の記録・候補の同一性を動かさない）。"""
    q = await _questionnaire(db_session, "q-loc-reach-mark")
    cand = {**_CAND, "reach": "body_unreachable"}
    status, _ = await _post(location_saver, csrf_token, q.id, _body("lk1", [cand]))
    assert status == 200
    row = await _row(db_session, q.id, "lk1")
    await db_session.refresh(row)
    assert "reach" not in row.candidate_list[0]


def test_the_candidate_card_shows_the_mark() -> None:
    """カードに印を出す結線（JS のテストランナーが無いので文字列で固定する）。"""
    js = (
        pathlib.Path(__file__).resolve().parents[2] / "kawasemi/web/static/js/design_system.js"
    ).read_text(encoding="utf-8")
    assert "c.reach === 'body_unreachable'" in js
    assert "cand-reach-badge" in js


def test_both_routes_that_hand_candidates_to_the_page_add_the_mark() -> None:
    """結線: 画面の初期状態（`system_edit`）と AI 案の応答（`draft_system_locations`）の両方。

    片方だけだと、AI 案を出した直後は印が出て、読み込み直すと消える（または逆）。
    """
    import inspect

    from kawasemi.ai_input import routes

    assert "_annotate_reach(" in inspect.getsource(routes.system_edit)
    assert "_annotate_reach(" in inspect.getsource(routes.draft_system_locations)


@pytest.mark.asyncio
async def test_the_canvas_cell_links_open_the_pressed_item(
    location_saver: AsyncClient, designer_user: User, db_session: AsyncSession
):
    """キャンバスのセルを押したら、その項目が開く（どれを押しても①が出ていた）。

    リンクは `#item=<linkId>` を持ち、各エディタはそれを読んでその項目へ移る。linkId は
    空白や記号を含みうる（感染症の様式の `Underlying medical condition_01_01`）ので URL 符号化する。
    """
    q = Questionnaire(
        fhir_id="q-canvas-jump",
        title="キャンバスの遷移",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-canvas-jump",
            "item": [
                {"linkId": "lk1", "text": "一つ目", "type": "string"},
                {
                    "linkId": "Underlying medical condition_01_01",
                    "text": "二つ目",
                    "type": "string",
                },
            ],
        },
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)

    resp = await location_saver.get(f"/ai-input/questionnaires/{q.id}/canvas")
    assert resp.status_code == 200
    html = resp.text
    assert f"/questionnaires/{q.id}/source-survey/system#item=lk1" in html
    assert (
        f"/questionnaires/{q.id}/source-survey/system#item=Underlying%20medical%20condition_01_01"
        in html
    )
    # 段階ごとのリンクが素のパス（項目なし）のまま残っていない
    assert f'href="/ai-input/questionnaires/{q.id}/source-survey/system"' not in html

    # 受け側: 項目カードの画面は data-link-id で、ウィザードの画面は ITEMS の添字で開く
    system_js = pathlib.Path("kawasemi/web/static/js/design_system.js").read_text(encoding="utf-8")
    clinical_js = pathlib.Path("kawasemi/web/static/js/design_clinical.js").read_text(
        encoding="utf-8"
    )
    for js in (system_js, clinical_js):
        assert "#item=" in js and "jumpToItemFromHash();" in js
    for page in ("runbook", "plan"):
        token = create_access_token(data={"sub": str(designer_user.id), "act": RoleType.DESIGNER})
        location_saver.cookies.set(settings.jwt_cookie_name, token)
        resp = await location_saver.get(f"/ai-input/questionnaires/{q.id}/{page}")
        assert resp.status_code == 200, page
        assert "cur = indexFromHash();" in resp.text, page
        # 無い項目へ飛んできたときは黙って①に戻さず、そう言う
        assert "はこの画面の対象ではありません" in resp.text, page
