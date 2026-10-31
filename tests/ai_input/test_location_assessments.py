"""データ所在の確からしさの判定のテスト

- compute_cand_key: 候補内容から決定的キーを導出する（llm_model/payload は無視）
- POST .../assessments: 段階の固定語彙・判定不能の相互排他・round_number の採番・
  職務分離（is_self_assessment）の検出
- GET .../assessments: AI提案由来/人手入力由来の集計、全roundの出力
- _clean_candidate / candidates JSON が判定の追加によって変化しないこと
"""

from pathlib import Path

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.models import AssessmentStage, SystemLocation, compute_cand_key
from kawasemi.ai_input.routes import _is_electronic_candidate
from kawasemi.core.auth import create_access_token, get_password_hash
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import Role, RoleType
from kawasemi.models.user import User, UserRole


def _act_as(client: AsyncClient, user: User, role: str) -> None:
    token = create_access_token(data={"sub": str(user.id), "act": role})
    client.cookies.set(settings.jwt_cookie_name, token)


# 集計の分母を設定で決めるテストが使う、架空の様式の url と対象項目の設定。
# 問診票は [1]〜[3] しか持たないので、[4] [5] は「設定にあるのに問診票に無い番号」になる
_FORM_URL = "http://kawasemi.example/Questionnaire/loc-assess-item1"
_FICTIONAL_TARGETS = {_FORM_URL: {"target_item_numbers": ["1", "2", "3", "4", "5"]}}


async def _make_questionnaire(db_session: AsyncSession, link_id: str = "item1") -> Questionnaire:
    url = f"http://kawasemi.example/Questionnaire/loc-assess-{link_id}"
    q = Questionnaire(
        fhir_id=f"q-loc-assess-{link_id}",
        title="所在判定テスト票",
        status="active",
        url=url,
        content={
            "resourceType": "Questionnaire",
            "id": f"q-loc-assess-{link_id}",
            "url": url,
            # **項目テキストは実データの形にする**（`[NNN]` 付き）。集計の分母は
            # 対象項目で決まるので、番号を持つ項目がある問診票で番号の無いテキストに
            # すると対象外になって集計に現れない。
            #
            # 2項目目を常に置くのは、**分母が1件だと「未判定が落ちる」形が見えない**ため
            # （判定した1件だけで分母も分子も埋まってしまう）。
            # **3項目にするのは、判定1件のときに「未判定の件数」と「判定済みの件数」が
            # 同値にならないようにするため**（2項目だと 1 と 1 で、減算の誤りが見えない）。
            "item": [
                {"linkId": link_id, "text": "[1] 登録番号", "type": "string"},
                {"linkId": "item2", "text": "[2] 生年月日", "type": "string"},
                {"linkId": "item3", "text": "[3] 性別", "type": "string"},
            ],
        },
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    return q


async def _second_data_manager_user(db_session: AsyncSession, data_manager_role: Role) -> User:
    """自己判定検出テスト用の、別人のデータ管理者ユーザーを作る"""
    user = User(
        username="data_manager_2_test",
        email="data_manager_2@test.com",
        password_hash=get_password_hash("DataManager2!23"),
        full_name="データ管理者2テスト",
        is_active=True,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    db_session.add(UserRole(user_id=user.id, role_id=data_manager_role.id))
    await db_session.commit()
    return user


async def _save_location(
    client: AsyncClient,
    user: User,
    csrf_token: str,
    questionnaire_id: int,
    link_id: str,
    *,
    llm_model: str | None = None,
) -> None:
    resp = await client.post(
        f"/ai-input/questionnaires/{questionnaire_id}/source-survey/system",
        json={
            "items": [
                {
                    "link_id": link_id,
                    "item_text": link_id,
                    "candidates": [
                        {
                            "source_category": "local_db",
                            "location": "demoテーブルの item1 カラム",
                            "unit_id": "db:T.item1",
                            "llm_model": llm_model,
                        }
                    ],
                }
            ]
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200 and resp.json()["success"]


@pytest.mark.parametrize(
    "source_category,electronic",
    [
        ("fhir", True),
        ("local_db", True),
        ("external_db", True),
        ("file", True),
        ("llm", True),
        ("manual", False),
        ("not_available", False),
        # **許可リストに無い値は電子ではない**（拒否リストだと「manual と not_available 以外は
        # 電子」になり、種別が1つ増えた瞬間に電子として数えられる）。API 経由では未知の種別が
        # `manual` へ正規化されるので、この違いは単体でしか観測できない。
        ("paper", False),
        ("", False),
        (None, False),
    ],
)
def test_is_electronic_candidate_is_an_allowlist(
    source_category: str | None, electronic: bool
) -> None:
    """電子/非電子の判定が**許可リスト**であること。

    電子的な所在の集計は電子層別の値を見るので、ここが fail-open だと分子が上振れする。
    """
    assert _is_electronic_candidate({"source_category": source_category}) is electronic


def _assert_coverage_accounting(coverage: dict) -> None:
    """`coverage` の会計が閉じていることを検算する。

    - 項目単位: `assessed_items == Σitems_by_stage + undeterminable_items`（集約は排反）
    - 電子側: `assessed_items == Σ電子段階 + 電子判定不能 + 電子候補なし`
    - 分母: `target_items == assessed_items + unassessed_items`

    **恒等式を1シナリオでしか見ないと、段階4だけ排反にする形の退行が通る**（実測）。
    coverage を返す全テストからこれを呼ぶ。
    """
    staged = sum(coverage["items_by_stage"].values())
    assert staged + coverage["undeterminable_items"] == coverage["assessed_items"], coverage
    staged_e = sum(coverage["items_by_stage_electronic"].values())
    assert (
        staged_e
        + coverage["undeterminable_items_electronic"]
        + coverage["items_without_electronic_candidate"]
        == coverage["assessed_items"]
    ), coverage
    assert coverage["assessed_items"] + coverage["unassessed_items"] == coverage["target_items"]
    assert len(coverage["unassessed_link_ids"]) == coverage["unassessed_items"]


async def _get_location(db: AsyncSession, qid: int, link_id: str) -> SystemLocation:
    result = await db.execute(
        select(SystemLocation).where(
            SystemLocation.questionnaire_id == qid, SystemLocation.link_id == link_id
        )
    )
    return result.scalar_one()


# --------------------------------------------------------------------------- #
# compute_cand_key
# --------------------------------------------------------------------------- #


def test_compute_cand_key_deterministic():
    c = {"source_category": "local_db", "location": "T.C", "unit_id": "db:T.C"}
    assert compute_cand_key(c) == compute_cand_key(dict(c))


def test_compute_cand_key_ignores_llm_model_and_payload():
    base = {"source_category": "local_db", "location": "T.C", "unit_id": "db:T.C"}
    with_extra = {**base, "llm_model": "model-c:9b", "payload": {"snippet": "..."}}
    assert compute_cand_key(base) == compute_cand_key(with_extra)


def test_compute_cand_key_changes_with_location():
    a = {"source_category": "local_db", "location": "T.C", "unit_id": "db:T.C"}
    b = {"source_category": "local_db", "location": "T.D", "unit_id": "db:T.C"}
    assert compute_cand_key(a) != compute_cand_key(b)


def test_assessment_stage_vocabulary_is_fixed():
    """段階の語彙はコード定数。実行時に追加できない。"""
    assert AssessmentStage.ALL == [4, 3, 2, 1]
    assert AssessmentStage.LABELS[4] == "確実"
    assert AssessmentStage.LABELS[1] == "不在"


# --------------------------------------------------------------------------- #
# POST .../assessments
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_create_assessment_records_stage_and_rationale(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await _save_location(designer_client, designer_user, csrf_token, q.id, "item1")

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
        json={"link_id": "item1", "stage": 4, "rationale": "定義上必須入力の列"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["success"] is True
    assert body["round_number"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("field", ["variant_dependent_note", "cancer_type_dependent_note"])
async def test_variant_dependent_note_is_recorded_under_either_name(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    field: str,
) -> None:
    """所在の可変性は新しい名前で記録し、改名前の名前で送られても同じ列に入る。"""
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await _save_location(designer_client, designer_user, csrf_token, q.id, "item1")

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
        json={"link_id": "item1", "stage": 4, "rationale": "x", field: "variable"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    resp = await designer_client.get(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments"
    )
    assert resp.status_code == 200
    rnd = resp.json()["items"][0]["rounds"][0]
    assert rnd["variant_dependent_note"] == "variable"
    assert "cancer_type_dependent_note" not in rnd


@pytest.mark.asyncio
async def test_create_assessment_rejects_out_of_range_stage(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await _save_location(designer_client, designer_user, csrf_token, q.id, "item1")

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
        json={"link_id": "item1", "stage": 5, "rationale": "不正な段階"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_stage", [0, -1, 10])
async def test_create_assessment_rejects_other_out_of_range_stages(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    bad_stage: int,
) -> None:
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await _save_location(designer_client, designer_user, csrf_token, q.id, "item1")

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
        json={"link_id": "item1", "stage": bad_stage, "rationale": "不正な段階"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_create_assessment_forbidden_for_non_designer(
    designer_client: AsyncClient,
    designer_user: User,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """判定作成は designer ロールにゲートされる（collector では 403）"""
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await _save_location(designer_client, designer_user, csrf_token, q.id, "item1")

    _act_as(designer_client, collector_user, RoleType.COLLECTOR)
    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
        json={"link_id": "item1", "stage": 3, "rationale": "権限テスト"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_create_assessment_undeterminable_rejects_stage(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """判定不能の場合は段階を付けない（推測で段階を付けない）"""
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await _save_location(designer_client, designer_user, csrf_token, q.id, "item1")

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
        json={
            "link_id": "item1",
            "stage": 3,
            "is_undeterminable": True,
            "rationale": "判定者から回答不能と言われた",
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_repeat_assessment_increments_round_number(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """同一候補への複数回の判定を round_number で区別する（再現性の測定）"""
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await _save_location(designer_client, designer_user, csrf_token, q.id, "item1")

    payload = {"link_id": "item1", "stage": 3, "rationale": "1回目"}
    r1 = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
        json=payload,
        headers={"X-CSRF-Token": csrf_token},
    )
    assert r1.json()["round_number"] == 1

    payload2 = {"link_id": "item1", "stage": 4, "rationale": "再判定で確実に変更"}
    r2 = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
        json=payload2,
        headers={"X-CSRF-Token": csrf_token},
    )
    assert r2.json()["round_number"] == 2


@pytest.mark.asyncio
async def test_self_assessment_detected(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """判定者=SystemLocation の最終保存者なら is_self_assessment=True"""
    from kawasemi.ai_input.models import LocationCandidateAssessment

    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await _save_location(designer_client, designer_user, csrf_token, q.id, "item1")

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
        json={"link_id": "item1", "stage": 3, "rationale": "自己判定のテスト"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assessment_id = resp.json()["id"]
    row = await db_session.get(LocationCandidateAssessment, assessment_id)
    assert row is not None
    assert row.is_self_assessment is True


@pytest.mark.asyncio
async def test_self_assessment_false_for_different_user(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    designer_role: Role,
) -> None:
    """設計成果物の作成者と判定者が別人なら is_self_assessment=False"""
    from kawasemi.ai_input.models import LocationCandidateAssessment

    other_user = await _second_data_manager_user(db_session, designer_role)

    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await _save_location(designer_client, designer_user, csrf_token, q.id, "item1")

    _act_as(designer_client, other_user, RoleType.DESIGNER)
    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
        json={"link_id": "item1", "stage": 3, "rationale": "別人による判定"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assessment_id = resp.json()["id"]
    row = await db_session.get(LocationCandidateAssessment, assessment_id)
    assert row is not None
    assert row.is_self_assessment is False
    assert row.assessed_by_id == other_user.id


@pytest.mark.asyncio
async def test_assessment_does_not_alter_candidates_json(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """判定を記録しても SystemLocation.candidates（_clean_candidate の出力）は変化しない"""
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await _save_location(designer_client, designer_user, csrf_token, q.id, "item1")

    loc = await _get_location(db_session, q.id, "item1")
    before = loc.candidate_list
    assert before  # 候補が保存されている前提

    await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
        json={"link_id": "item1", "stage": 3, "rationale": "候補は変化しないはず"},
        headers={"X-CSRF-Token": csrf_token},
    )

    await db_session.refresh(loc)
    assert loc.candidate_list == before


# --------------------------------------------------------------------------- #
# GET .../assessments（集計・出力）
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_export_aggregates_ai_proposed_vs_manual(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    q = await _make_questionnaire(db_session, "item1")
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    # item1: AI提案由来（llm_model あり）、item2: 人手入力
    await _save_location(
        designer_client, designer_user, csrf_token, q.id, "item1", llm_model="model-c:9b"
    )

    # 2項目目（[2]）の所在を保存する
    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system",
        json={
            "items": [
                {
                    "link_id": "item2",
                    "candidates": [{"source_category": "manual", "location": "紹介状PDFを参照"}],
                }
            ]
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200

    for link_id, stage in (("item1", 4), ("item2", 2)):
        r = await designer_client.post(
            f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
            json={"link_id": link_id, "stage": stage, "rationale": f"{link_id}の判定"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert r.status_code == 200

    resp = await designer_client.get(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["summary"]["ai_proposed"].get("4") == 1
    assert body["summary"]["manual"].get("2") == 1

    items_by_link = {item["link_id"]: item for item in body["items"]}
    assert items_by_link["item1"]["rounds"][0]["candidate_snapshot"]["llm_model"] == "model-c:9b"
    assert items_by_link["item2"]["rounds"][0]["stage"] == 2


@pytest.mark.asyncio
async def test_export_reports_item_level_coverage(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    target_items_config,
) -> None:
    """**項目単位の分母と分子**が出力されること。

    `summary` は候補単位の票数なので、対象項目数で割ってよいのは `coverage` である。1項目だけ
    判定した状態で、未判定の項目が**分母に残る**ことを見る。判定の有無から分母を作ると、
    未判定は静かに落ちて「対象項目のうち N 項目」を出力できない。
    """
    target_items_config(_FICTIONAL_TARGETS)
    q = await _make_questionnaire(db_session, "item1")
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await _save_location(designer_client, designer_user, csrf_token, q.id, "item1")

    r = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
        json={"link_id": "item1", "stage": 4, "rationale": "判定"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert r.status_code == 200

    resp = await designer_client.get(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments"
    )
    coverage = resp.json()["coverage"]
    assert coverage["target_items"] == 3, "分母は対象項目の数（判定の有無に依らない）"
    assert coverage["assessed_items"] == 1
    assert coverage["unassessed_items"] == 2, "判定済みの件数と同値にならない規模で見る"
    assert coverage["unassessed_link_ids"] == ["item2", "item3"]
    assert coverage["candidate_votes"] == 1
    # 設定の件数（5）と、この問診票で見つかった数の食い違いが出力に現れること
    assert coverage["expected_target_items"] == 5
    assert coverage["missing_item_numbers"] == ["4", "5"]
    # 項目単位の段階分布（所在の確からしさの段階の分子）。複数候補があるときは最良の段階を採る
    assert coverage["items_by_stage"] == {"4": 1}
    assert coverage["undeterminable_items"] == 0
    _assert_coverage_accounting(coverage)


@pytest.mark.asyncio
async def test_export_coverage_before_any_assessment(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    target_items_config,
) -> None:
    """所在未保存（作業開始時点）でも**分母が出る**こと。

    `coverage` のいちばん大事な使い方は「まだ何も判定していない」を対象項目数に対して言うことで、
    その経路は所在が1件も無い早期 return を通る。ここが 0 だと分母が消える。
    """
    target_items_config(_FICTIONAL_TARGETS)
    q = await _make_questionnaire(db_session, "item1")
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    body = (
        await designer_client.get(
            f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments"
        )
    ).json()
    coverage = body["coverage"]
    assert coverage["expected_target_items"] == 5
    assert coverage["missing_item_numbers"] == ["4", "5"]
    assert coverage["target_items"] == 3
    assert coverage["assessed_items"] == 0
    assert coverage["unassessed_items"] == 3
    assert coverage["unassessed_link_ids"] == ["item1", "item2", "item3"]
    assert coverage["candidate_votes"] == 0
    assert coverage["items_by_stage"] == {}
    _assert_coverage_accounting(coverage)


@pytest.mark.asyncio
async def test_export_reports_unknown_link_ids_separately(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """**問診票に無い link_id** の判定を「対象外」と混ぜないこと。

    保存経路は link_id を問診票と突き合わせないので、typo や問診票編集後の残骸で作れる。
    それはデータ不整合であって「意図して除外した対象外」ではない。
    """
    q = await _make_questionnaire(db_session, "item1")
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await _save_location(designer_client, designer_user, csrf_token, q.id, "typo_item")
    r = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
        json={"link_id": "typo_item", "stage": 4, "rationale": "問診票に無い項目の判定"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert r.status_code == 200, r.text

    coverage = (
        await designer_client.get(
            f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments"
        )
    ).json()["coverage"]
    assert coverage["unknown_link_ids"] == ["typo_item"]
    assert coverage["out_of_scope_items_skipped"] == 0, "対象外と混ざっている"
    assert coverage["assessed_items"] == 0


@pytest.mark.asyncio
async def test_export_takes_the_best_stage_per_item(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """1項目に段階の違う候補が複数あるとき、**最良の段階**をその項目の段階とすること。

    所在の確からしさの段階は「その項目をどこかから取得できるか」を測るので、最も確実な候補が項目の
    到達可能性を決める。最悪や平均を採ると、候補を足すほど評価が下がる。
    """
    q = await _make_questionnaire(db_session, "item1")
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system",
        json={
            "items": [
                {
                    "link_id": "item1",
                    "item_text": "[1] 登録番号",
                    "candidates": [
                        {
                            "source_category": "local_db",
                            "location": "確実な候補",
                            "unit_id": "db:a",
                        },
                        {"source_category": "manual", "location": "怪しい候補", "unit_id": "db:b"},
                    ],
                }
            ]
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200, resp.text
    location = await _get_location(db_session, q.id, "item1")
    keys = [compute_cand_key(c) for c in location.candidate_list]
    for cand_key, stage in zip(keys, (2, 4), strict=True):
        r = await designer_client.post(
            f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
            json={
                "link_id": "item1",
                "cand_key": cand_key,
                "stage": stage,
                "rationale": f"段階{stage}",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert r.status_code == 200, r.text

    coverage = (
        await designer_client.get(
            f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments"
        )
    ).json()["coverage"]
    assert coverage["items_by_stage"] == {"4": 1}, "最良（4）ではなく別の段階を採っている"
    assert coverage["assessed_items"] == 1
    assert coverage["candidate_votes"] == 2
    _assert_coverage_accounting(coverage)


@pytest.mark.asyncio
async def test_export_counts_undeterminable_items_separately(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """判定不能（`is_undeterminable`）を段階分布に混ぜず、別に数えること。

    段階を持たない判定を「判定済み」の中で段階付きと同列に扱うと、充足を多めに見せる。
    """
    q = await _make_questionnaire(db_session, "item1")
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await _save_location(designer_client, designer_user, csrf_token, q.id, "item1")
    r = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
        json={
            "link_id": "item1",
            "is_undeterminable": True,
            "unreachable_reason": "接続先が不明",
            "rationale": "判定できない",
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert r.status_code == 200, r.text

    coverage = (
        await designer_client.get(
            f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments"
        )
    ).json()["coverage"]
    assert coverage["items_by_stage"] == {}, "判定不能が段階分布に入っている"
    assert coverage["undeterminable_items"] == 1
    assert coverage["assessed_items"] == 1
    _assert_coverage_accounting(coverage)


@pytest.mark.parametrize("stage", [1, 2, 3, 4])
@pytest.mark.asyncio
async def test_export_stage_counts_and_undeterminable_are_disjoint(
    stage: int,
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """段階分布と判定不能が**排反**であること（`assessed = Σstage + undeterminable`）。

    1項目に「判定不能の候補」と「段階付きの候補」が並ぶと、候補単位の分岐では両方に入って
    しまう（判定不能項目数が上振れする）。**恒等式で固定する**——これが無いと
    集約規則の言葉（排反）と実装がずれても気づけない。
    """
    q = await _make_questionnaire(db_session, "item1")
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system",
        json={
            "items": [
                {
                    "link_id": "item1",
                    "item_text": "[1] 登録番号",
                    "candidates": [
                        {"source_category": "local_db", "location": "確実", "unit_id": "db:a"},
                        {"source_category": "local_db", "location": "不明", "unit_id": "db:b"},
                    ],
                }
            ]
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200, resp.text
    location = await _get_location(db_session, q.id, "item1")
    keys = [compute_cand_key(c) for c in location.candidate_list]

    # 第1候補は段階4、第2候補は判定不能
    r = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
        json={"link_id": "item1", "cand_key": keys[0], "stage": stage, "rationale": "判定"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert r.status_code == 200, r.text
    r = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
        json={
            "link_id": "item1",
            "cand_key": keys[1],
            "is_undeterminable": True,
            "unreachable_reason": "接続先が不明",
            "rationale": "判定できない",
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert r.status_code == 200, r.text

    coverage = (
        await designer_client.get(
            f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments"
        )
    ).json()["coverage"]
    assert coverage["items_by_stage"] == {str(stage): 1}
    assert coverage["undeterminable_items"] == 0, "段階付きの候補があるのに判定不能に数えている"
    _assert_coverage_accounting(coverage)


@pytest.mark.asyncio
async def test_export_stratifies_stages_by_electronic_source(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """**紙を見て転記する候補（`manual`）が項目の段階を決めない**層別値が出ること。

    電子的に取得できる所在を数えるとき、`manual` 候補の段階4が「取得できる」に数えられるのは
    上振れ。全体の値と電子のみの値を両方出す。
    """
    q = await _make_questionnaire(db_session, "item1")
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system",
        json={
            "items": [
                {
                    "link_id": "item1",
                    "item_text": "[1] 登録番号",
                    "candidates": [
                        {"source_category": "manual", "location": "紙の紹介状を見る"},
                        {"source_category": "local_db", "location": "DBの列", "unit_id": "db:b"},
                    ],
                }
            ]
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200, resp.text
    location = await _get_location(db_session, q.id, "item1")
    keys = [compute_cand_key(c) for c in location.candidate_list]
    # 紙は「確実」、電子は「疑わしい」
    for cand_key, stage in zip(keys, (4, 2), strict=True):
        r = await designer_client.post(
            f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
            json={
                "link_id": "item1",
                "cand_key": cand_key,
                "stage": stage,
                "rationale": f"段階{stage}",
            },
            headers={"X-CSRF-Token": csrf_token},
        )
        assert r.status_code == 200, r.text

    coverage = (
        await designer_client.get(
            f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments"
        )
    ).json()["coverage"]
    assert coverage["items_by_stage"] == {"4": 1}, "全体は最良（紙の4）"
    assert coverage["items_by_stage_electronic"] == {"2": 1}, "電子のみだと 2 になるべき"
    _assert_coverage_accounting(coverage)


@pytest.mark.asyncio
async def test_export_does_not_count_non_electronic_as_electronic(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """`manual` / `not_available` の候補だけの項目は**電子の段階分布に入らない**こと。

    電子/非電子の判定は許可リストで、拒否リスト（「manual と not_available 以外は電子」）に
    すると種別が1つ増えた瞬間に電子として数えられる（実測で、紙の種別を足すと
    電子層別の分子が黙って増えた）。**電子候補が無い項目**として数えられることも見る。
    """
    q = await _make_questionnaire(db_session, "item1")
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system",
        json={
            "items": [
                {
                    "link_id": "item1",
                    "item_text": "[1] 登録番号",
                    "candidates": [{"source_category": "manual", "location": "紙の紹介状"}],
                },
                {
                    "link_id": "item2",
                    "item_text": "[2] 生年月日",
                    "candidates": [
                        {"source_category": "not_available", "location": "当院には無い"}
                    ],
                },
            ]
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200, resp.text
    for link_id in ("item1", "item2"):
        r = await designer_client.post(
            f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
            json={"link_id": link_id, "stage": 4, "rationale": "紙なら確実"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert r.status_code == 200, r.text

    coverage = (
        await designer_client.get(
            f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments"
        )
    ).json()["coverage"]
    assert coverage["items_by_stage"] == {"4": 2}, "全体では段階4が2件"
    assert coverage["items_by_stage_electronic"] == {}, "非電子が電子に数えられている"
    assert coverage["items_without_electronic_candidate"] == 2
    _assert_coverage_accounting(coverage)


@pytest.mark.asyncio
async def test_export_uses_the_latest_round_for_the_stage(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """段階は**最新 round**を現在の判定状況として採ること（再判定が反映される）。

    `history[-1]` を `history[0]` に変える退行が、挙動テストでは無検出だった（実測）。
    4週後の再判定が集計に出ないと、再現性の測定そのものが成立しない。
    """
    q = await _make_questionnaire(db_session, "item1")
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await _save_location(designer_client, designer_user, csrf_token, q.id, "item1")
    for stage in (4, 1):
        r = await designer_client.post(
            f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
            json={"link_id": "item1", "stage": stage, "rationale": f"段階{stage}"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert r.status_code == 200, r.text

    coverage = (
        await designer_client.get(
            f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments"
        )
    ).json()["coverage"]
    # 段階1（不在）を通す経路も、ここで初めて覆う
    assert coverage["items_by_stage"] == {"1": 1}, "最新 round（1）ではなく初回（4）を採っている"
    assert coverage["candidate_votes"] == 1, "round は票数を増やさない"
    _assert_coverage_accounting(coverage)


@pytest.mark.asyncio
async def test_export_counts_candidate_votes_separately_from_items(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """1項目に複数候補があるとき、**票数と項目数を区別**すること。

    `summary` の合計を項目数で割ると合わない（1項目が3候補を持てば3票入る）。区別が無いと、
    段階別の件数が「項目数」だと誤読される。
    """
    q = await _make_questionnaire(db_session, "item1")
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system",
        json={
            "items": [
                {
                    "link_id": "item1",
                    "item_text": "[1] 登録番号",
                    "candidates": [
                        {
                            "source_category": "local_db",
                            "location": "第1候補",
                            "unit_id": "db:T.a",
                        },
                        {
                            "source_category": "local_db",
                            "location": "第2候補",
                            "unit_id": "db:T.b",
                        },
                    ],
                }
            ]
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200, resp.text
    location = await _get_location(db_session, q.id, "item1")
    cand_keys = [compute_cand_key(c) for c in location.candidate_list]
    assert len(cand_keys) == 2, "候補2件のフィクスチャになっていない"

    for cand_key in cand_keys:
        r = await designer_client.post(
            f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
            json={"link_id": "item1", "cand_key": cand_key, "stage": 3, "rationale": "判定"},
            headers={"X-CSRF-Token": csrf_token},
        )
        assert r.status_code == 200, r.text

    body = (
        await designer_client.get(
            f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments"
        )
    ).json()
    # 票は2、項目は1
    assert body["summary"]["manual"].get("3") == 2
    assert body["coverage"]["candidate_votes"] == 2
    assert body["coverage"]["assessed_items"] == 1
    assert body["coverage"]["target_items"] == 3
    # 2候補が同じ段階3なら項目の段階は3（最良を採る規則）
    assert body["coverage"]["items_by_stage"] == {"3": 1}


@pytest.mark.asyncio
async def test_export_excludes_out_of_scope_items_but_counts_them(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """対象外項目（番号の無い項目）の判定を分母・分子に入れず、**件数は返す**こと。

    黙って落とすと「なぜ合計が合わないのか」を追えない。
    """
    q = await _make_questionnaire(db_session, "item1")
    # 対象外の項目を問診票に足す（他の項目が番号を持つので、番号なし = 対象外）
    content = dict(q.content)
    content["item"] = [
        *content["item"],
        {"linkId": "ext1", "text": "リンパ管侵襲（拡張）", "type": "string"},
    ]
    q.content = content
    await db_session.commit()

    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await _save_location(designer_client, designer_user, csrf_token, q.id, "ext1")
    r = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
        json={"link_id": "ext1", "stage": 4, "rationale": "対象外の判定"},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert r.status_code == 200, r.text

    body = (
        await designer_client.get(
            f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments"
        )
    ).json()
    assert body["summary"]["manual"] == {}, "対象外の判定が集計に入っている"
    assert body["coverage"]["target_items"] == 3
    assert body["coverage"]["assessed_items"] == 0
    assert body["coverage"]["out_of_scope_items_skipped"] == 1
    assert body["coverage"]["unknown_link_ids"] == []
    # **判定記録（再判定用）には残す。** 集計から除くだけで、記録から消さない
    # （判定記録は追記のみで、この endpoint が唯一の出力経路）。
    out_of_scope = [i for i in body["items"] if i["link_id"] == "ext1"]
    assert len(out_of_scope) == 1
    assert out_of_scope[0]["in_scope"] is False
    assert out_of_scope[0]["item_kind"] == "extension"
    assert out_of_scope[0]["rounds"][0]["stage"] == 4


@pytest.mark.asyncio
async def test_export_includes_full_round_history(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await _save_location(designer_client, designer_user, csrf_token, q.id, "item1")

    for stage in (2, 3):
        await designer_client.post(
            f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
            json={"link_id": "item1", "stage": stage, "rationale": f"round for stage {stage}"},
            headers={"X-CSRF-Token": csrf_token},
        )

    resp = await designer_client.get(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments"
    )
    rounds = resp.json()["items"][0]["rounds"]
    assert [r["round_number"] for r in rounds] == [1, 2]
    assert [r["stage"] for r in rounds] == [2, 3]


@pytest.mark.asyncio
async def test_duplicate_round_number_rejected_at_db_level(
    db_session: AsyncSession,
) -> None:
    """同一候補・同一round_numberの二重挿入はDBの一意制約が拒否する（同時投稿の安全網）"""
    from sqlalchemy.exc import IntegrityError

    from kawasemi.ai_input.models import LocationCandidateAssessment

    q = Questionnaire(
        fhir_id="q-dup-round",
        title="重複round_numberテスト票",
        status="active",
        content={"resourceType": "Questionnaire", "id": "q-dup-round"},
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)

    loc = SystemLocation(
        questionnaire_id=q.id,
        link_id="item1",
        candidates=[{"source_category": "local_db", "location": "T.C", "unit_id": "db:T.C"}],
    )
    db_session.add(loc)
    await db_session.commit()
    await db_session.refresh(loc)

    cand_key = compute_cand_key(loc.candidates[0])
    common = {
        "system_location_id": loc.id,
        "cand_key": cand_key,
        "candidate_snapshot": loc.candidates[0],
        "round_number": 1,
        "stage": 3,
        "is_undeterminable": False,
        "rationale": "1件目",
        "is_self_assessment": False,
        "assessed_at": loc.created_at,
    }
    db_session.add(LocationCandidateAssessment(**common))
    await db_session.commit()

    db_session.add(LocationCandidateAssessment(**{**common, "rationale": "2件目（重複）"}))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


# =========================================================================== #
# 段階の語彙を DB でも守る
#
# 保存ルートの1行だけが門番だったので、**DB 直書き・移行スクリプト・将来の別
# エンドポイント**は素通りした。集計は `str(latest.stage)` を集計キーにするので、語彙外の
# 値は `"0"` / `"None"` という**新しいキー**を作る——所在の確からしさの段階の目盛りである。
#
# **層ごとに単独で測る**（上位層が先に止めると下位層は無検証のまま残る）。
#   層1a: DB の CHECK 制約（ORM 側）  … 制約を無効化した挿入で測る
#   層1b: DB の CHECK 制約（移行側）  … `tests/test_stage_vocabulary_migration.py` が測る
#   層2:  集計（ルート）              … 制約を無効化して仕込んだ行で測る
#   層3:  集約規則（`_best_stage_per_item`）… 関数を直接呼んで測る
#
# **保存 API の 400 はここで測る層ではない**（既存の
# `test_assessment_rejects_invalid_stage` が測っている）。`is_valid` へ寄せたのは語彙の出所を
# 1つにする整理で、pydantic が型を強制するので振る舞いは `not in ALL` と同じである
# （実測。`stage=true` は改修の前後どちらでも 200 で、集計キーは `"1"`）。
# =========================================================================== #


async def _locations_for_stage_tests(
    db_session: AsyncSession, fhir_id: str
) -> list[SystemLocation]:
    """**2項目 × 2候補**の所在を作る。

    1項目・1候補だと、語彙外の行を**1件しか作れない**。すると「最初の1件だけ
    `out_of_vocabulary` に載せる」退行と「件数をハードコードする」退行が素通りする
    （実測）。このファイルは既に自分で同じ規範を書いている——
    「分母が1件だと『未判定が落ちる』形が見えない」。
    """
    q = Questionnaire(
        fhir_id=fhir_id,
        title="段階語彙テスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": fhir_id,
            "item": [
                {"linkId": "item1", "text": "[1] 登録番号", "type": "string"},
                {"linkId": "item2", "text": "[2] 生年月日", "type": "string"},
            ],
        },
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    locs = []
    for link_id in ("item1", "item2"):
        loc = SystemLocation(
            questionnaire_id=q.id,
            link_id=link_id,
            candidates=[
                {"source_category": "local_db", "location": f"T.{link_id}.A", "unit_id": "db:A"},
                {"source_category": "local_db", "location": f"T.{link_id}.B", "unit_id": "db:B"},
            ],
        )
        db_session.add(loc)
        locs.append(loc)
    await db_session.commit()
    for loc in locs:
        await db_session.refresh(loc)
    # **前提を assert として書く**（候補が2件ないと、同じ項目で語彙内と語彙外を混ぜられない）
    assert all(len(loc.candidates) == 2 for loc in locs)
    return locs


async def _location_for_stage_tests(db_session: AsyncSession, fhir_id: str) -> SystemLocation:
    return (await _locations_for_stage_tests(db_session, fhir_id))[0]


def _assessment_row(
    loc: SystemLocation, *, stage: object, undeterminable: bool = False, cand: int = 0
) -> dict:
    return {
        "system_location_id": loc.id,
        "cand_key": compute_cand_key(loc.candidates[cand]),
        "candidate_snapshot": loc.candidates[cand],
        "round_number": 1,
        "stage": stage,
        "is_undeterminable": undeterminable,
        "rationale": "語彙テスト",
        "is_self_assessment": False,
        "assessed_at": loc.created_at,
    }


def test_the_vocabulary_predicate_refuses_non_int_values() -> None:
    """語彙の判定は1箇所（`AssessmentStage.is_valid`）で、**`int` 以外を通さない**こと。

    この述語は**ORM が DB から読んだ値**にも使う。CHECK 制約が無い環境で
    `UPDATE ... SET stage = 'True'` と書かれた行は ORM が `str` のまま返し、
    `str(latest.stage)` が `"True"` という集計キーになる。**`int()` へ通してから比べると
    `"4"` のような文字列を語彙内へ昇格させる**ので、型のまま拒否する。

    **`in ALL` との差は2つ**（実測）。`None` は `is_valid` だけが許し、
    **この差は判定不能の行のために要る**（下のテストが測る）。`bool` と値の等しい非 int の数値は
    `in ALL` だけが許すが、**その差はどの既知の経路からも到達しない**——保存 API は pydantic が
    `int` へ強制し、INTEGER 親和性の列から `bool` / `float` は返らない。したがって
    **型検査は多重防御であって層ではない**（型検査だけを落とす変異は振る舞いのテストを1本も
    落とさないと実測された）。「効くのは ORM / DB 直読みの側だけ」と書いていたのは誤りである。
    """
    assert AssessmentStage.is_valid(None), "判定不能（null）は許す"
    assert all(AssessmentStage.is_valid(v) for v in AssessmentStage.ALL)
    assert not AssessmentStage.is_valid(0)
    assert not AssessmentStage.is_valid(5)
    assert not AssessmentStage.is_valid(True), "True == 1 が通っている"
    assert not AssessmentStage.is_valid(False)
    assert not AssessmentStage.is_valid("4"), "文字列の 4 が通っている"
    assert not AssessmentStage.is_valid(4.0), "int でない数値が通っている"
    # **`in ALL` との差を字面で残す**（この2つが差で、片方は必要・片方は到達しない）
    assert (None in AssessmentStage.ALL) is False, "前提: `in ALL` は None を許さない"
    assert (True in AssessmentStage.ALL) is True, "前提: `in ALL` は True を許す"


def test_the_vocabulary_expression_is_built_from_the_vocabulary() -> None:
    """**CHECK 制約の式と移行の検査は `ALL` から組む**こと。

    「語彙は `ALL` が唯一の出所」を3箇所（`check_sql` の docstring・移行の docstring・変更記録）に
    書いていたが、**どこも結線されていなかった**——数値を直書きした実装でも
    `for v in ALL: assert str(v) in check_sql()` は緑になる（同じ数値なので）。
    `ALL` を差し替えて**式が追随する**ことで測る。
    """
    import re as _re

    from kawasemi.ai_input.models import AssessmentStage as Stage

    original = list(Stage.ALL)
    try:
        Stage.ALL = [*original, 9]  # type: ignore[misc]
        assert "9" in Stage.check_sql(), "式が `ALL` から組まれていない（数値の直書き）"
        assert Stage.is_valid(9), "述語が `ALL` から判定していない"
    finally:
        Stage.ALL = original  # type: ignore[misc]
    assert not Stage.is_valid(9), "後片付けができていない"

    # **移行の検査文も同じ**（ソースに条件の書き直しが無いこと）。
    # **移行が数える行と、制約が拒否する行は同じ集合でなければならない**——違うと
    # 「検査は 0 件と言ったのに `IntegrityError` で落ちる」（あるいはその逆）が起きる。
    # 以前、この検査は `stage` 単体しか見ずに NULL 行を語彙内として通していた。
    migration = (
        Path(__file__).resolve().parents[2]
        / "alembic"
        / "versions"
        / "d8e9f0a1b2c3_add_stage_vocabulary_check.py"
    ).read_text(encoding="utf-8")
    body = _re.search(r"def _assert_existing_rows_are_in_vocabulary.*?(?=\ndef )", migration, _re.S)
    assert body, "移行の検査関数が見つからない"
    # **条件を組み立てている文そのものを見る**（関数のどこかに `check_sql` が在るだけでは
    # 足りない——エラーメッセージにも出てくるので、条件を直書きする変異が生き残ると実測された）
    where_lines = [ln for ln in body.group(0).splitlines() if ln.strip().startswith("violating =")]
    assert len(where_lines) == 1, where_lines
    assert "AssessmentStage.check_sql()" in where_lines[0], (
        f"制約の式と別に条件を書いている: {where_lines[0]}"
    )
    assert "stage" not in where_lines[0].replace("check_sql", ""), (
        f"列の条件が直書きされている: {where_lines[0]}"
    )
    assert not _re.search(r"\d", where_lines[0]), f"数値が直書きされている: {where_lines[0]}"


@pytest.mark.asyncio
async def test_an_out_of_vocabulary_stage_is_rejected_at_db_level(
    db_session: AsyncSession,
) -> None:
    """**語彙外の `stage` は DB が拒否する**。

    ORM を通さない経路（移行スクリプト・別エンドポイント・手作業）を止めるのはここだけである。
    """
    from sqlalchemy.exc import IntegrityError

    from kawasemi.ai_input.models import LocationCandidateAssessment

    loc = await _location_for_stage_tests(db_session, "q-stage-ck-ng")
    db_session.add(LocationCandidateAssessment(**_assessment_row(loc, stage=0)))
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", [*AssessmentStage.ALL, None])
async def test_the_vocabulary_constraint_accepts_every_valid_stage(
    db_session: AsyncSession, stage: int | None
) -> None:
    """**語彙内の値と null（判定不能）は通る**こと（常に拒否する制約を殺す）。

    肯定側を測らないと、`stage IN ()` のような「全部拒否」の式でもテストが緑になる。
    """
    from kawasemi.ai_input.models import LocationCandidateAssessment

    loc = await _location_for_stage_tests(db_session, f"q-stage-ck-ok-{stage}")
    db_session.add(
        LocationCandidateAssessment(
            **_assessment_row(loc, stage=stage, undeterminable=stage is None)
        )
    )
    await db_session.commit()  # 例外が出なければ通っている


@pytest.mark.asyncio
async def test_the_export_refuses_to_aggregate_out_of_vocabulary_stages(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
) -> None:
    """**集計は語彙外の値を静かに集計キーにしない**。

    CHECK 制約が入る前に保存された行、制約を持たない環境で書かれた行がここへ来る。
    `str(latest.stage)` をそのままキーにすると `"0"` が `summary` に現れ、**所在の確からしさの
    段階分布に無い段階が増える**。1件でもあれば集計を返さない（落とした値を返すと、
    落としたことが読み手に見えない）。

    **語彙外を2件・語彙内を1件混ぜる**。1件だと「最初の1件だけ載せる」
    退行が素通りする（実測）。`items` には語彙外・語彙内の**両方**が残ることも
    測る——どの行を直すべきかが分からないと運用できない。

    **層を単独で測るため CHECK 制約を無効化して仕込む**（SQLite の
    `PRAGMA ignore_check_constraints`。**SQLite 専用**なので、テストを PostgreSQL で回すように
    したらここは別の手（制約を落とした一時表など）に替える必要がある）。制約に頼ると、
    この検査を消しても緑のままになる。
    """
    from sqlalchemy import text

    from kawasemi.ai_input.models import LocationCandidateAssessment

    loc1, loc2 = await _locations_for_stage_tests(db_session, "q-stage-export-ng")
    await db_session.execute(text("PRAGMA ignore_check_constraints = ON"))
    db_session.add(LocationCandidateAssessment(**_assessment_row(loc1, stage=0, cand=0)))
    db_session.add(LocationCandidateAssessment(**_assessment_row(loc2, stage=9, cand=1)))
    # **語彙内の行も混ぜる**（語彙外だけの表では「全部落とす」実装でも緑になる）
    db_session.add(LocationCandidateAssessment(**_assessment_row(loc1, stage=3, cand=1)))
    await db_session.commit()
    await db_session.execute(text("PRAGMA ignore_check_constraints = OFF"))
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await designer_client.get(
        f"/ai-input/questionnaires/{loc1.questionnaire_id}/source-survey/system/assessments"
    )

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert "summary" not in body, "語彙外を含む集計を返している"
    assert "coverage" not in body
    # **語彙外の行を全部返す**（件数と、どの項目・どの候補か）
    reported = {(r["link_id"], r["cand_key"]) for r in body["out_of_vocabulary"]}
    assert reported == {
        ("item1", compute_cand_key(loc1.candidates[0])),
        ("item2", compute_cand_key(loc2.candidates[1])),
    }, body["out_of_vocabulary"]
    assert len(body["out_of_vocabulary"]) == 2
    assert f"{len(reported)}件" in body["message"]
    # **語彙内の行も記録として残る**（再判定用の出力）
    recorded = {(r["link_id"], r["cand_key"]) for r in body["items"]}
    assert ("item1", compute_cand_key(loc1.candidates[1])) in recorded, "語彙内の記録が落ちている"
    assert reported <= recorded, "語彙外の記録が落ちている"


@pytest.mark.asyncio
async def test_an_out_of_scope_row_also_blocks_the_aggregation(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
) -> None:
    """**対象外の項目の語彙外行でも集計を返さない**。

    対象外（設定で外した項目・番号の無い項目・問診票に無い link_id）の判定は `summary` / `coverage` に入って
    いないので、「その行の語彙外は分布を汚さないから通してよい」という設計も成り立つ。
    **通さない方を選ぶ。** 理由は、語彙外の値が同じ表にあるという事実は**書き込み経路を
    こちらが把握できていない**ことの示唆であり、その状態で段階の分布を出すのは
    「検証できないを検証に通った」に当たるからである。**どちらの意味論も無検証だと
    実測で分かった**ので、選んだ側をテストで固定する。
    """
    from sqlalchemy import text

    from kawasemi.ai_input.models import LocationCandidateAssessment

    loc1, _loc2 = await _locations_for_stage_tests(db_session, "q-stage-export-oos")
    # **問診票に無い link_id**（対象外。`unknown_link_ids` に数えられる側）
    out_of_scope = SystemLocation(
        questionnaire_id=loc1.questionnaire_id,
        link_id="item999",
        candidates=[{"source_category": "local_db", "location": "T.X", "unit_id": "db:X"}],
    )
    db_session.add(out_of_scope)
    await db_session.commit()
    await db_session.refresh(out_of_scope)

    await db_session.execute(text("PRAGMA ignore_check_constraints = ON"))
    db_session.add(LocationCandidateAssessment(**_assessment_row(out_of_scope, stage=0)))
    await db_session.commit()
    await db_session.execute(text("PRAGMA ignore_check_constraints = OFF"))
    # 対象項目の判定は語彙内（=「対象だけ見れば集計できる」状態にする）
    db_session.add(LocationCandidateAssessment(**_assessment_row(loc1, stage=4)))
    await db_session.commit()
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await designer_client.get(
        f"/ai-input/questionnaires/{loc1.questionnaire_id}/source-survey/system/assessments"
    )

    assert resp.status_code == 409, resp.text
    assert [r["link_id"] for r in resp.json()["out_of_vocabulary"]] == ["item999"]


@pytest.mark.asyncio
async def test_the_export_still_aggregates_when_every_stage_is_in_vocabulary(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
) -> None:
    """**正常系では 409 を返さない**こと（検査が「正常系検出器」になっていない）。"""
    from kawasemi.ai_input.models import LocationCandidateAssessment

    loc = await _location_for_stage_tests(db_session, "q-stage-export-ok")
    db_session.add(LocationCandidateAssessment(**_assessment_row(loc, stage=3)))
    await db_session.commit()
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await designer_client.get(
        f"/ai-input/questionnaires/{loc.questionnaire_id}/source-survey/system/assessments"
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["summary"]["manual"] == {"3": 1}
    assert "out_of_vocabulary" not in body


def test_the_aggregation_rule_refuses_an_out_of_vocabulary_stage() -> None:
    """**集約規則の側でも語彙を要求する**（層3）。

    ルートが先に 409 で止めるので、この層は「上位層に守られて無検証のまま」になりやすい。
    `_best_stage_per_item` を直接呼んで測る（`int(latest.stage)` は `0` も `7` も
    `max` に入れてしまう）。
    """
    from types import SimpleNamespace

    from kawasemi.ai_input.routes import _best_stage_per_item

    snapshot = {"source_category": "local_db", "location": "T.C"}
    ok = SimpleNamespace(stage=3, is_undeterminable=False, candidate_snapshot=snapshot)
    staged, undeterminable = _best_stage_per_item({"item1": [ok]})
    assert staged == {"item1": 3} and undeterminable == set(), "前提: 正常系は通る"

    bad = SimpleNamespace(stage=7, is_undeterminable=False, candidate_snapshot=snapshot)
    with pytest.raises(ValueError, match="語彙外"):
        _best_stage_per_item({"item1": [bad]})


def test_the_aggregation_rule_checks_the_vocabulary_before_the_electronic_filter() -> None:
    """**層別（`electronic_only=True`）でも語彙の検査が発火する**こと。

    語彙の検査を `electronic_only ... continue` の**下**に置くと、非電子の候補に語彙外があるとき
    層別の呼び出しでだけ黙って捨てられる。その形へ戻す変異が**振る舞い層を素通りした**
    （止めていたのはソースハッシュの照合だけ＝「ソースが変わった」検出器で、振る舞いは
    守っていない）。**層は呼び出しパラメータに依らず一様でなければ層と呼べない。**

    `manual`（非電子）の候補で測る。**前提を assert として書く**——同じフィクスチャで `stage` を
    語彙内にすると、その候補は `electronic_only=True` では**落ちる**。これが無いと
    「ガードが発火した」と「候補が絞り込みで捨てられた」を区別できない。
    """
    from types import SimpleNamespace

    from kawasemi.ai_input.routes import _best_stage_per_item

    manual = {"source_category": "manual", "location": "紙の台帳"}

    # 前提: 語彙内なら層別の絞り込みで落ちる（＝この候補は非電子である）
    ok = SimpleNamespace(stage=3, is_undeterminable=False, candidate_snapshot=manual)
    assert _best_stage_per_item({"item1": [ok]}) == ({"item1": 3}, set()), "前提: 既定では数える"
    assert _best_stage_per_item({"item1": [ok]}, electronic_only=True) == ({}, set()), (
        "前提: 層別では落ちる候補である"
    )

    # 語彙外なら**落とす前に**検査が発火する
    bad = SimpleNamespace(stage=0, is_undeterminable=False, candidate_snapshot=manual)
    with pytest.raises(ValueError, match="語彙外"):
        _best_stage_per_item({"item1": [bad]}, electronic_only=True)


def test_the_vocabulary_pairs_a_null_stage_with_undeterminable() -> None:
    """**語彙は `(stage, is_undeterminable)` の組で決まる**。

    `None` が語彙内なのは「判定不能」を意味するときだけである。`is_undeterminable = 0` の
    `None` は**段階が付いていないだけの行**で、`str(latest.stage)` を経て
    `summary["manual"]["None"]` になった（実測）。語彙の検査が防ぎたい
    失敗（`"0"` や `"None"` という集計キー）のうち **`"None"` の側が塞がっていなかった**。

    **`is_valid` 単体との差をそのまま書く**——この差が欠陥の正体である。
    """
    assert AssessmentStage.is_valid(None) is True, "前提: stage 単体では None は語彙内"

    assert AssessmentStage.row_is_in_vocabulary(3, False), "段階付きの行"
    assert AssessmentStage.row_is_in_vocabulary(None, True), "判定不能の行"
    assert not AssessmentStage.row_is_in_vocabulary(None, False), (
        '段階が無く判定不能でもない行が語彙内になっている（`"None"` キーの正体）'
    )
    assert not AssessmentStage.row_is_in_vocabulary(4, True), (
        "判定不能なのに段階がある行が語彙内になっている（段階の値が黙って捨てられる）"
    )
    # 段階そのものが語彙外なら、どちらの組でも語彙外
    assert not AssessmentStage.row_is_in_vocabulary(0, False)
    assert not AssessmentStage.row_is_in_vocabulary(0, True)
    # `is_undeterminable` が `bool` / `int` でなければ語彙外へ倒す（fail-closed）
    assert not AssessmentStage.row_is_in_vocabulary(None, "1")
    assert not AssessmentStage.row_is_in_vocabulary(3, None)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "undeterminable"),
    [(None, False), (4, True)],
    ids=["段階が無く判定不能でもない", "判定不能なのに段階がある"],
)
async def test_the_pair_of_stage_and_undeterminable_is_rejected_at_db_level(
    db_session: AsyncSession, stage: int | None, undeterminable: bool
) -> None:
    """**排反も DB が守る**（層1）。

    この排反はそれまで保存 API の1行（`create_location_assessment` の 400）だけが守っており、
    **「門番が API の1行だけ」という形がこの組み合わせに残っていた**。
    DB 直書き・移行・将来の別エンドポイントは素通りする。
    """
    from sqlalchemy.exc import IntegrityError

    from kawasemi.ai_input.models import LocationCandidateAssessment

    loc = await _location_for_stage_tests(db_session, f"q-stage-pair-{stage}-{undeterminable}")
    db_session.add(
        LocationCandidateAssessment(
            **_assessment_row(loc, stage=stage, undeterminable=undeterminable)
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_the_export_refuses_a_null_stage_that_is_not_undeterminable(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
) -> None:
    """**`"None"` という集計キーを作らない**（層2）。

    改修前の実測は `summary == {"ai_proposed": {}, "manual": {"None": 1}}` で 200 だった。
    あわせて `_coverage` の恒等式 `assessed_items == Σitems_by_stage + undeterminable_items`
    も破れていた（実測: `1 == 0 + 0`）。

    **語彙内の行を混ぜる**（語彙外だけの表では「全部落とす」実装でも緑になる）。
    """
    from sqlalchemy import text

    from kawasemi.ai_input.models import LocationCandidateAssessment

    loc1, loc2 = await _locations_for_stage_tests(db_session, "q-stage-export-null")
    await db_session.execute(text("PRAGMA ignore_check_constraints = ON"))
    db_session.add(
        LocationCandidateAssessment(
            **_assessment_row(loc1, stage=None, undeterminable=False, cand=0)
        )
    )
    # 鏡像（判定不能なのに段階がある行）も同じ層で止まる
    db_session.add(
        LocationCandidateAssessment(**_assessment_row(loc2, stage=4, undeterminable=True, cand=1))
    )
    await db_session.commit()
    await db_session.execute(text("PRAGMA ignore_check_constraints = OFF"))
    db_session.add(LocationCandidateAssessment(**_assessment_row(loc1, stage=3, cand=1)))
    await db_session.commit()
    _act_as(designer_client, designer_user, RoleType.DESIGNER)

    resp = await designer_client.get(
        f"/ai-input/questionnaires/{loc1.questionnaire_id}/source-survey/system/assessments"
    )

    assert resp.status_code == 409, resp.text
    body = resp.json()
    assert "summary" not in body, "語彙外を含む集計を返している"
    assert "coverage" not in body
    reported = {(r["link_id"], r["cand_key"]) for r in body["out_of_vocabulary"]}
    assert reported == {
        ("item1", compute_cand_key(loc1.candidates[0])),
        ("item2", compute_cand_key(loc2.candidates[1])),
    }, body["out_of_vocabulary"]


def test_the_aggregation_rule_refuses_a_null_stage_that_is_not_undeterminable() -> None:
    """**集約規則の側でも組を要求する**（層3）。

    この行は `staged` にも `undeterminable` にも入らないので、`_best_stage_per_item` の
    docstring が宣言する恒等式が**黙って**破れる（例外にすれば黙らない）。
    """
    from types import SimpleNamespace

    from kawasemi.ai_input.routes import _best_stage_per_item

    snapshot = {"source_category": "local_db", "location": "T.C"}
    # 前提: 判定不能の行（`stage=None, is_undeterminable=True`）は通る
    ok = SimpleNamespace(stage=None, is_undeterminable=True, candidate_snapshot=snapshot)
    assert _best_stage_per_item({"item1": [ok]}) == ({}, {"item1"}), "前提: 判定不能は通る"

    bad = SimpleNamespace(stage=None, is_undeterminable=False, candidate_snapshot=snapshot)
    with pytest.raises(ValueError, match="語彙外"):
        _best_stage_per_item({"item1": [bad]})

    mirror = SimpleNamespace(stage=4, is_undeterminable=True, candidate_snapshot=snapshot)
    with pytest.raises(ValueError, match="語彙外"):
        _best_stage_per_item({"item1": [mirror]})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage", "is_undeterminable"),
    [(4, False), (1, False), (None, True), (None, False), (4, True), (0, False)],
)
async def test_the_save_api_accepts_exactly_the_rows_the_vocabulary_allows(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    stage: int | None,
    is_undeterminable: bool,
) -> None:
    """**保存 API が受ける集合と語彙述語が一致する**こと。

    API の 400 は2つの分岐（判定不能なのに段階がある／段階が無いか語彙外）に分かれて
    いるので、**述語と同じ集合を切っているかは字面では読めない**。組ごとに実測して結ぶ。
    ここが一致していないと、API を通った行を DB の制約が拒む（またはその逆）ことが起きる。

    **400 であることまで要求する**（層を単独で測る）。「200 でないこと」だけを見ると、
    API の検査を外す変異が **DB の CHECK 制約に肩代わりされて生き残る**（実測: この形の変異が
    検出されずに残った）。人に伝える層は経路ごとに要る。
    """
    q = await _make_questionnaire(db_session)
    _act_as(designer_client, designer_user, RoleType.DESIGNER)
    await _save_location(designer_client, designer_user, csrf_token, q.id, "item1")

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/source-survey/system/assessments",
        json={
            "link_id": "item1",
            "stage": stage,
            "is_undeterminable": is_undeterminable,
            "rationale": "語彙テスト",
        },
        headers={"X-CSRF-Token": csrf_token},
    )

    allowed = AssessmentStage.row_is_in_vocabulary(stage, is_undeterminable)
    assert resp.status_code == (200 if allowed else 400), (
        stage,
        is_undeterminable,
        resp.status_code,
        resp.text,
    )
