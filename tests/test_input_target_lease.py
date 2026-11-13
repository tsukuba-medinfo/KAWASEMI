"""入力対象の先着自動割り当て（claim）と排他リース

背景（セキュリティ指摘 F4）: 以前は `GET /datasets/{id}/check` が、割り当ての検査なしに
`InputTarget.started_by_id = 自分` を書いた。`user_can_access_patient` は
`started_by_id == 自分` を患者データ（FHIR）の認可の根拠にしていたので、収集者は
チェック画面を開くだけで任意の患者の FHIR データを `GET /api/patient/{id}` から読めた。

設計: 誰を収集するかは研究計画で固定（管理者が対象患者リスト = Dataset を登録）し、
誰が収集するかは先着の自動割り当て + 期限付きの排他リースで決める。収集者は患者を選べない。
認可は「有効なリース（本人・期限内・未完了）」だけを根拠にする。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi_csrf_protect import CsrfProtect
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from kawasemi.api.main import app
from kawasemi.core.authz import user_can_access_patient
from kawasemi.core.config import settings
from kawasemi.core.lease import (
    claim_next_target,
    extend_lease,
    get_active_lease,
    lease_is_valid,
    release_lease,
)
from kawasemi.models.base import Base
from kawasemi.models.dataset import Dataset, DatasetQuestionnaire
from kawasemi.models.input_target import InputTarget, InputTargetStatus
from kawasemi.models.role import Role
from kawasemi.models.user import User
from tests.conftest import _login, _make_user
from tests.lease_helpers import lease_patient, make_target


async def _reload(db: AsyncSession, target_id: int) -> InputTarget:
    got = await db.execute(
        select(InputTarget)
        .where(InputTarget.id == target_id)
        .execution_options(populate_existing=True)
    )
    return got.scalar_one()


@pytest.fixture
async def collector2(db_session: AsyncSession, collector_role: Role) -> User:
    return await _make_user(
        db_session, "collector_two", "収集者2", "Collector123!", [collector_role]
    )


# --------------------------------------------------------------------------- #
# 受け取り（claim）・認可・期限
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_claim_gives_a_lease_and_authorizes_until_it_expires_or_completes(
    db_session: AsyncSession, collector_user: User
) -> None:
    await make_target(db_session, "P-1")

    assert await user_can_access_patient(db_session, collector_user, "P-1") is False

    got = await claim_next_target(db_session, collector_user.id)
    assert got is not None and got.patient_id == "P-1"
    assert got.started_by_id == collector_user.id
    assert got.status == InputTargetStatus.IN_PROGRESS  # pending は受け取ると入力中
    assert got.lease_expires_at is not None
    assert await user_can_access_patient(db_session, collector_user, "P-1") is True

    # 期限切れ → 認可を失う（掃除ジョブは要らない）
    got.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    assert await user_can_access_patient(db_session, collector_user, "P-1") is False
    assert lease_is_valid(got, collector_user.id) is False

    # 延長された有効なリース → 再び認可される。完了すると認可を失う
    got.lease_expires_at = datetime.now(UTC) + timedelta(minutes=5)
    await db_session.commit()
    assert await user_can_access_patient(db_session, collector_user, "P-1") is True
    got.status = InputTargetStatus.COMPLETED
    await db_session.commit()
    assert await user_can_access_patient(db_session, collector_user, "P-1") is False


@pytest.mark.asyncio
async def test_lease_validity_handles_naive_datetimes(
    db_session: AsyncSession, collector_user: User
) -> None:
    """SQLite は naive な日時を返す。aware と比べて落ちず、正しく判定する。"""
    t = await lease_patient(db_session, collector_user.id, "P-NAIVE")
    t.lease_expires_at = (datetime.now(UTC) + timedelta(minutes=5)).replace(tzinfo=None)
    assert lease_is_valid(t, collector_user.id) is True
    t.lease_expires_at = (datetime.now(UTC) - timedelta(minutes=5)).replace(tzinfo=None)
    assert lease_is_valid(t, collector_user.id) is False


@pytest.mark.asyncio
async def test_lease_length_follows_the_setting(
    db_session: AsyncSession, collector_user: User, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(settings, "claim_lease_minutes", 7)
    await make_target(db_session, "P-LEN")
    before = datetime.now(UTC)
    got = await claim_next_target(db_session, collector_user.id)
    assert got is not None and got.lease_expires_at is not None
    left = got.lease_expires_at
    left = left if left.tzinfo else left.replace(tzinfo=UTC)
    assert timedelta(minutes=6) < left - before <= timedelta(minutes=7, seconds=5)


@pytest.mark.asyncio
async def test_claim_again_resumes_the_held_target(
    db_session: AsyncSession, collector_user: User
) -> None:
    """一度に1件: 有効なリースがあるうちに押しても新しい患者は増えず、同じ対象が返る。"""
    await make_target(db_session, "P-A")
    await make_target(db_session, "P-B")

    first = await claim_next_target(db_session, collector_user.id)
    again = await claim_next_target(db_session, collector_user.id)

    assert first is not None and again is not None and again.id == first.id
    other = (
        await db_session.execute(select(InputTarget).where(InputTarget.patient_id == "P-B"))
    ).scalar_one()
    assert other.started_by_id is None
    assert await user_can_access_patient(db_session, collector_user, "P-B") is False


@pytest.mark.asyncio
async def test_after_expiry_a_collector_can_claim_a_new_target(
    db_session: AsyncSession, collector_user: User
) -> None:
    first = await make_target(db_session, "P-OLD")
    await make_target(db_session, "P-NEW")
    got = await claim_next_target(db_session, collector_user.id)
    assert got is not None and got.id == first.id
    got.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()

    assert await get_active_lease(db_session, collector_user.id) is None
    nxt = await claim_next_target(db_session, collector_user.id)
    # 期限切れの P-OLD（入力中）より未着手の P-NEW が先
    assert nxt is not None and nxt.patient_id == "P-NEW"


@pytest.mark.asyncio
async def test_claim_order_ai_drafted_then_pending_then_expired_then_needs_attention(
    db_session: AsyncSession, collector_user: User, collector2: User
) -> None:
    uid = collector_user.id
    # id 順に並べると逆順になるよう作る
    attention = await make_target(
        db_session, "P-5-attention", status=InputTargetStatus.AI_DRAFTED, needs_attention=True
    )
    expired = await make_target(
        db_session,
        "P-4-expired",
        status=InputTargetStatus.IN_PROGRESS,
        holder_id=collector2.id,
        lease_minutes=-5,
    )
    pending = await make_target(db_session, "P-3-pending")
    drafted = await make_target(db_session, "P-2-drafted", status=InputTargetStatus.AI_DRAFTED)
    # 他者の有効なリースは受け取れない
    await make_target(
        db_session,
        "P-1-held",
        status=InputTargetStatus.AI_DRAFTED,
        holder_id=collector2.id,
        lease_minutes=30,
    )
    # 完了済みは対象外
    await make_target(db_session, "P-0-done", status=InputTargetStatus.COMPLETED)

    order = []
    for _ in range(4):
        got = await claim_next_target(db_session, uid)
        assert got is not None
        order.append(got.id)
        got.status = InputTargetStatus.COMPLETED  # 次を受け取れるよう完了させる
        await db_session.commit()
    assert order == [drafted.id, pending.id, expired.id, attention.id]
    assert await claim_next_target(db_session, uid) is None


@pytest.mark.asyncio
async def test_ai_drafted_stays_ai_drafted_when_claimed(
    db_session: AsyncSession, collector_user: User
) -> None:
    await make_target(db_session, "P-D", status=InputTargetStatus.AI_DRAFTED)
    got = await claim_next_target(db_session, collector_user.id)
    assert got is not None and got.status == InputTargetStatus.AI_DRAFTED


@pytest.mark.asyncio
async def test_claim_can_be_narrowed_to_a_dataset_and_questionnaire(
    db_session: AsyncSession, collector_user: User
) -> None:
    await make_target(db_session, "P-DS1", dataset_id=1, questionnaire_id=1)
    t2 = await make_target(db_session, "P-DS2", dataset_id=2, questionnaire_id=9)
    got = await claim_next_target(db_session, collector_user.id, dataset_id=2, questionnaire_id=9)
    assert got is not None and got.id == t2.id
    await release_lease(db_session, got)
    assert await claim_next_target(db_session, collector_user.id, dataset_id=3) is None, (
        "該当の無い絞り込みで他から受け取ってはならない"
    )


@pytest.mark.asyncio
async def test_activity_extends_the_lease_only_for_the_holder(
    db_session: AsyncSession, collector_user: User, collector2: User
) -> None:
    t = await lease_patient(db_session, collector_user.id, "P-EXT", minutes=1)
    before = t.lease_expires_at
    assert await extend_lease(db_session, t, collector2.id) is False  # 保持者でなければ延びない
    assert await extend_lease(db_session, t, collector_user.id) is True
    t = await _reload(db_session, t.id)
    assert t.lease_expires_at is not None and before is not None
    assert t.lease_expires_at.replace(tzinfo=None) > before.replace(tzinfo=None)

    # 期限切れのリースは、保持者本人でも延長で復活させられない
    t.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()
    assert await extend_lease(db_session, t, collector_user.id) is False


# --------------------------------------------------------------------------- #
# 排他（同時に押した場合）。2 つの別セッション（別接続）で競わせる
# --------------------------------------------------------------------------- #


@pytest.fixture
async def two_sessions(tmp_path: Path):
    """同じファイル DB を別々の接続で開く 2 セッション（インメモリ DB は接続が1本で競合しない）。"""
    url = f"sqlite+aiosqlite:///{tmp_path / 'race.db'}"
    engine_a = create_async_engine(url, connect_args={"timeout": 30})
    engine_b = create_async_engine(url, connect_args={"timeout": 30})
    async with engine_a.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    maker_a = async_sessionmaker(engine_a, class_=AsyncSession, expire_on_commit=False)
    maker_b = async_sessionmaker(engine_b, class_=AsyncSession, expire_on_commit=False)
    async with maker_a() as a, maker_b() as b:
        yield a, b
    await engine_a.dispose()
    await engine_b.dispose()


@pytest.mark.asyncio
async def test_concurrent_claims_get_different_targets(two_sessions) -> None:
    a, b = two_sessions
    for pid in ("P-1", "P-2", "P-3"):
        a.add(InputTarget(dataset_id=1, questionnaire_id=1, patient_id=pid))
    await a.commit()

    got_a, got_b = await asyncio.gather(
        claim_next_target(a, user_id=101), claim_next_target(b, user_id=102)
    )

    assert got_a is not None and got_b is not None
    assert got_a.id != got_b.id
    assert got_a.started_by_id == 101 and got_b.started_by_id == 102


@pytest.mark.asyncio
async def test_with_one_target_left_only_one_collector_gets_it(two_sessions) -> None:
    a, b = two_sessions
    a.add(InputTarget(dataset_id=1, questionnaire_id=1, patient_id="P-LAST"))
    await a.commit()

    results = await asyncio.gather(
        claim_next_target(a, user_id=101), claim_next_target(b, user_id=102)
    )

    winners = [r for r in results if r is not None]
    assert len(winners) == 1, results
    assert results.count(None) == 1


@pytest.mark.asyncio
async def test_a_lost_race_retries_with_another_candidate(
    db_session: AsyncSession, collector_user: User, collector2: User, monkeypatch
) -> None:
    """候補の選択と UPDATE の間に他者が取った場合、rowcount 0 を見て別の候補で取り直す。"""
    first = await make_target(db_session, "P-RACE-1")
    second = await make_target(db_session, "P-RACE-2")
    original_execute = db_session.execute
    stolen = {"done": False}

    async def execute(stmt, *args, **kwargs):
        result = await original_execute(stmt, *args, **kwargs)
        sql = str(stmt)
        # 候補の id だけを選ぶ SELECT（get_active_lease は全列を選ぶので除く）
        if (
            not stolen["done"]
            and sql.startswith("SELECT input_targets.id \n")
            and "dataset_id" not in sql.split("FROM")[0]
        ):
            stolen["done"] = True
            await original_execute(
                InputTarget.__table__.update()
                .where(InputTarget.id == first.id)
                .values(
                    started_by_id=collector2.id,
                    lease_expires_at=datetime.now(UTC) + timedelta(minutes=30),
                    status=InputTargetStatus.IN_PROGRESS,
                )
            )
        return result

    monkeypatch.setattr(db_session, "execute", execute)
    got = await claim_next_target(db_session, collector_user.id)
    monkeypatch.setattr(db_session, "execute", original_execute)

    assert stolen["done"]
    assert got is not None and got.id == second.id
    assert (await _reload(db_session, first.id)).started_by_id == collector2.id


# --------------------------------------------------------------------------- #
# HTTP: F4 の回帰、claim ルート、役割、check/confirm/flag の所有確認
# --------------------------------------------------------------------------- #


async def _dataset_with_target(
    db: AsyncSession, patient_id: str = "P-HTTP", *, status: str = InputTargetStatus.AI_DRAFTED
) -> tuple[Dataset, InputTarget]:
    from kawasemi.models.questionnaire import Questionnaire

    q = Questionnaire(
        fhir_id="q-lease",
        title="リース検証票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-lease",
            "item": [{"linkId": "name", "type": "string", "text": "氏名"}],
        },
    )
    db.add(q)
    ds = Dataset(name="リース検証DS")
    db.add(ds)
    await db.flush()
    db.add(DatasetQuestionnaire(dataset_id=ds.id, questionnaire_id=q.id, display_order=0))
    t = InputTarget(dataset_id=ds.id, questionnaire_id=q.id, patient_id=patient_id, status=status)
    db.add(t)
    await db.commit()
    await db.refresh(t)
    return ds, t


@pytest.mark.asyncio
async def test_f4_opening_check_mode_does_not_authorize_patient_data(
    collector_client: AsyncClient, db_session: AsyncSession
) -> None:
    """F4 の回帰: 担当のない収集者がチェック画面を開いても、FHIR データへの認可は生えない。"""
    ds, t = await _dataset_with_target(db_session)

    resp = await collector_client.get(
        f"/datasets/{ds.id}/check",
        params={"questionnaire_id": t.questionnaire_id, "target_id": t.id},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/input-targets")  # 受け取りを促す

    after = await _reload(db_session, t.id)
    assert after.started_by_id is None and after.lease_expires_at is None, "GET が担当を書いた"
    api = await collector_client.get("/api/patient/P-HTTP")
    assert api.status_code == 403


@pytest.mark.asyncio
async def test_unleased_collector_gets_403_on_patient_api_and_form(
    collector_client: AsyncClient, db_session: AsyncSession
) -> None:
    await _dataset_with_target(db_session, "P-NOLEASE", status=InputTargetStatus.PENDING)
    assert (await collector_client.get("/api/patient/P-NOLEASE")).status_code == 403
    form = await collector_client.get("/forms/1", params={"patient_id": "P-NOLEASE"})
    assert form.status_code == 403


@pytest.mark.asyncio
async def test_leased_collector_passes_the_patient_gate_until_expiry(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kawasemi.web.routes as web_routes

    class _Fhir:
        async def get_patient(self, pid: str) -> dict:
            return {"resourceType": "Patient", "id": pid}

    monkeypatch.setattr(web_routes, "FHIRClient", _Fhir)
    t = await lease_patient(db_session, collector_user.id, "P-LEASED")
    assert (await collector_client.get("/api/patient/P-LEASED")).status_code != 403

    t.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()
    assert (await collector_client.get("/api/patient/P-LEASED")).status_code == 403


@pytest.mark.asyncio
async def test_claim_route_hands_out_a_target_and_opens_it(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    ds, drafted = await _dataset_with_target(db_session, "P-DRAFT")
    resp = await collector_client.post(
        "/input-targets/claim", headers={"X-CSRF-Token": csrf_token}, follow_redirects=False
    )
    assert resp.status_code == 303
    loc = resp.headers["location"]
    assert loc == (
        f"/datasets/{ds.id}/check?questionnaire_id={drafted.questionnaire_id}&target_id={drafted.id}"
    )
    after = await _reload(db_session, drafted.id)
    assert after.started_by_id == collector_user.id
    assert after.status == InputTargetStatus.AI_DRAFTED

    # 確定の前に受け取った画面（一覧）は現在の担当と「続きを開く」を出し、他者の開始ボタンは無い
    page = await collector_client.get("/input-targets")
    assert page.status_code == 200
    assert "続きを開く" in page.text and "/start" not in page.text

    # 受け取った患者は開ける（check 画面）
    ok = await collector_client.get(loc)
    assert ok.status_code == 200
    # target_id が自分のリースと違っても他の患者は出ない（ここでは存在しない id）
    other = await collector_client.get(
        f"/datasets/{ds.id}/check",
        params={"questionnaire_id": drafted.questionnaire_id, "target_id": 99999},
    )
    assert other.status_code == 200 and "P-DRAFT" in other.text


@pytest.mark.asyncio
async def test_claim_route_opens_the_form_for_a_pending_target(
    collector_client: AsyncClient, db_session: AsyncSession, csrf_token: str
) -> None:
    _, t = await _dataset_with_target(db_session, "P-PEND", status=InputTargetStatus.PENDING)
    resp = await collector_client.post(
        "/input-targets/claim", headers={"X-CSRF-Token": csrf_token}, follow_redirects=False
    )
    assert resp.headers["location"] == (
        f"/forms/{t.questionnaire_id}?patient_id=P-PEND&input_target_id={t.id}"
    )


@pytest.mark.asyncio
async def test_claim_route_says_so_when_nothing_is_left(
    collector_client: AsyncClient, csrf_token: str
) -> None:
    resp = await collector_client.post(
        "/input-targets/claim", headers={"X-CSRF-Token": csrf_token}, follow_redirects=False
    )
    assert resp.status_code == 303
    assert resp.headers["location"].startswith("/input-targets?notice=")


@pytest.mark.asyncio
async def test_claim_route_requires_csrf(collector_client: AsyncClient) -> None:
    resp = await collector_client.post("/input-targets/claim", follow_redirects=False)
    assert resp.status_code in (400, 403)


@pytest.mark.asyncio
async def test_claim_is_collector_only(
    admin_client: AsyncClient, csrf_token: str, db_session: AsyncSession
) -> None:
    _, t = await _dataset_with_target(db_session, "P-ROLE")
    resp = await admin_client.post(
        "/input-targets/claim", headers={"X-CSRF-Token": csrf_token}, follow_redirects=False
    )
    assert resp.status_code == 403
    assert (await _reload(db_session, t.id)).started_by_id is None


@pytest.mark.asyncio
async def test_reviewer_cannot_claim_but_can_view_the_dataset(
    reviewer_client: AsyncClient, csrf_token: str, db_session: AsyncSession
) -> None:
    ds, t = await _dataset_with_target(db_session, "P-REV")
    claim = await reviewer_client.post(
        "/input-targets/claim", headers={"X-CSRF-Token": csrf_token}, follow_redirects=False
    )
    assert claim.status_code == 403
    assert (await reviewer_client.get(f"/datasets/{ds.id}")).status_code == 200
    assert (await reviewer_client.get("/datasets")).status_code == 200
    detail = await reviewer_client.get(f"/datasets/{ds.id}")
    assert "/input-targets/claim" not in detail.text and "batch-autofill" not in detail.text
    assert (await _reload(db_session, t.id)).started_by_id is None


@pytest.mark.asyncio
async def test_removed_start_route_and_assignment_routes_are_gone(
    collector_client: AsyncClient, csrf_token: str
) -> None:
    for path in (
        "/assignments",
        "/assignments/new",
        "/assignments/1/edit",
        "/my-cases",
    ):
        assert (await collector_client.get(path)).status_code == 404, path
    for path in ("/assignments/new", "/assignments/1/delete", "/my-cases/1/start"):
        resp = await collector_client.post(path, headers={"X-CSRF-Token": csrf_token})
        assert resp.status_code in (404, 405), path
    resp = await collector_client.post(
        "/input-targets/1/start", headers={"X-CSRF-Token": csrf_token}
    )
    assert resp.status_code in (404, 405)


@pytest.mark.asyncio
async def test_collector_home_goes_to_input_targets(collector_client: AsyncClient) -> None:
    resp = await collector_client.get("/", follow_redirects=False)
    assert resp.headers["location"] == "/input-targets"


# ---- チェック画面の所有確認 ------------------------------------------------


async def _other_client(db: AsyncSession, user: User) -> AsyncClient:
    csrf = CsrfProtect()
    raw, signed = csrf.generate_csrf_tokens()
    c = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
    c.cookies.set(csrf._cookie_key, signed)
    c.headers["X-CSRF-Token"] = raw
    return _login(c, user)


@pytest.mark.asyncio
async def test_check_confirm_flag_require_the_callers_own_valid_lease(
    collector_client: AsyncClient,
    collector_user: User,
    collector2: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    ds, t = await _dataset_with_target(db_session, "P-OWN")
    t.started_by_id = collector2.id  # 他の収集者が有効に保持している
    t.lease_expires_at = datetime.now(UTC) + timedelta(minutes=30)
    await db_session.commit()

    page = await collector_client.get(
        f"/datasets/{ds.id}/check",
        params={"questionnaire_id": t.questionnaire_id, "target_id": t.id},
        follow_redirects=False,
    )
    assert page.status_code == 303 and page.headers["location"].startswith("/input-targets")
    for action in ("confirm", "flag"):
        resp = await collector_client.post(
            f"/datasets/{ds.id}/check/{t.id}/{action}",
            data={"name": "x"},
            headers={"X-CSRF-Token": csrf_token},
            follow_redirects=False,
        )
        assert resp.status_code == 303, action
        assert resp.headers["location"].startswith("/input-targets"), action
    after = await _reload(db_session, t.id)
    assert after.status == InputTargetStatus.AI_DRAFTED
    assert after.started_by_id == collector2.id, "他者の担当を奪った/外した"
    assert after.needs_attention is False

    # 自分のリースが期限切れでも同じ
    t.started_by_id = collector_user.id
    t.lease_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()
    for action in ("confirm", "flag"):
        await collector_client.post(
            f"/datasets/{ds.id}/check/{t.id}/{action}",
            data={"name": "x"},
            headers={"X-CSRF-Token": csrf_token},
            follow_redirects=False,
        )
    after = await _reload(db_session, t.id)
    assert after.status == InputTargetStatus.AI_DRAFTED and after.needs_attention is False


@pytest.mark.asyncio
async def test_confirm_requires_ai_drafted_status(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    ds, t = await _dataset_with_target(db_session, "P-INPROG", status=InputTargetStatus.IN_PROGRESS)
    t.started_by_id = collector_user.id
    t.lease_expires_at = datetime.now(UTC) + timedelta(minutes=30)
    await db_session.commit()
    resp = await collector_client.post(
        f"/datasets/{ds.id}/check/{t.id}/confirm",
        data={"name": "x"},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert (await _reload(db_session, t.id)).status == InputTargetStatus.IN_PROGRESS


@pytest.mark.asyncio
async def test_confirm_completes_and_revokes_authorization_then_next_can_be_claimed(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    ds, t = await _dataset_with_target(db_session, "P-FIN")
    await claim_next_target(db_session, collector_user.id)
    resp = await collector_client.post(
        f"/datasets/{ds.id}/check/{t.id}/confirm",
        data={"name": "山田"},
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303 and resp.headers["location"].startswith("/input-targets")
    assert (await _reload(db_session, t.id)).status == InputTargetStatus.COMPLETED
    assert await user_can_access_patient(db_session, collector_user, "P-FIN") is False
    assert await get_active_lease(db_session, collector_user.id) is None


@pytest.mark.asyncio
async def test_flag_releases_the_lease_and_sends_the_target_to_the_back(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    ds, t = await _dataset_with_target(db_session, "P-FLAG")
    await claim_next_target(db_session, collector_user.id)
    resp = await collector_client.post(
        f"/datasets/{ds.id}/check/{t.id}/flag",
        headers={"X-CSRF-Token": csrf_token},
        follow_redirects=False,
    )
    assert resp.status_code == 303 and resp.headers["location"].startswith("/input-targets")
    after = await _reload(db_session, t.id)
    assert after.needs_attention is True and after.started_by_id is None
    assert after.lease_expires_at is None
    assert after.status == InputTargetStatus.AI_DRAFTED
    assert await user_can_access_patient(db_session, collector_user, "P-FLAG") is False


@pytest.mark.asyncio
async def test_two_collectors_over_http_get_different_patients(
    collector_client: AsyncClient,
    collector2: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    await make_target(db_session, "P-X1")
    await make_target(db_session, "P-X2")
    second = await _other_client(db_session, collector2)
    async with second:
        r1 = await collector_client.post(
            "/input-targets/claim", headers={"X-CSRF-Token": csrf_token}, follow_redirects=False
        )
        r2 = await second.post("/input-targets/claim", follow_redirects=False)
    assert r1.status_code == r2.status_code == 303
    assert r1.headers["location"] != r2.headers["location"]
    assert "P-X1" in r1.headers["location"] and "P-X2" in r2.headers["location"]


@pytest.mark.asyncio
async def test_opening_the_form_for_the_leased_target_extends_the_lease(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import kawasemi.web.routes as web_routes

    class _Fhir:
        async def get_patient(self, pid: str) -> dict:
            return {"resourceType": "Patient", "id": pid}

    monkeypatch.setattr(web_routes, "FHIRClient", _Fhir)
    from kawasemi.models.questionnaire import Questionnaire

    q = Questionnaire(
        fhir_id="q-ext",
        title="延長",
        status="active",
        content={"resourceType": "Questionnaire", "id": "q-ext", "item": []},
    )
    db_session.add(q)
    await db_session.commit()
    t = await lease_patient(
        db_session, collector_user.id, "P-FORM", questionnaire_id=q.id, minutes=1
    )
    resp = await collector_client.get(
        f"/forms/{q.id}", params={"patient_id": "P-FORM", "input_target_id": t.id}
    )
    assert resp.status_code == 200
    t = await _reload(db_session, t.id)
    assert t.lease_expires_at is not None
    left = t.lease_expires_at
    left = left if left.tzinfo else left.replace(tzinfo=UTC)
    assert left - datetime.now(UTC) > timedelta(minutes=30)


# ---- データセット（対象患者リスト）の登録は管理者だけ ----------------------------


_NEW_DATASET = {"name": "新規リスト", "patient_ids_text": "P-1\nP-2", "questionnaire_ids": "1"}


@pytest.mark.asyncio
async def test_collector_cannot_create_or_delete_a_dataset(
    collector_client: AsyncClient, csrf_token: str, db_session: AsyncSession
) -> None:
    ds, _ = await _dataset_with_target(db_session, "P-DEL")
    assert (await collector_client.get("/datasets/new")).status_code == 403
    for path in ("/datasets/new", "/datasets/new/confirm", f"/datasets/{ds.id}/delete"):
        resp = await collector_client.post(
            path, data=_NEW_DATASET, headers={"X-CSRF-Token": csrf_token}, follow_redirects=False
        )
        assert resp.status_code == 403, path
    assert (await db_session.execute(select(Dataset).where(Dataset.id == ds.id))).first()


@pytest.mark.asyncio
async def test_admin_creates_and_deletes_a_dataset(
    admin_client: AsyncClient, csrf_token: str, db_session: AsyncSession
) -> None:
    from kawasemi.models.questionnaire import Questionnaire

    q = Questionnaire(
        fhir_id="q-admin",
        title="管理者登録",
        status="active",
        content={"resourceType": "Questionnaire"},
    )
    db_session.add(q)
    await db_session.commit()
    data = {
        "name": "管理者のリスト",
        "patient_ids_text": "A-1\nA-2",
        "questionnaire_ids": str(q.id),
    }

    assert (await admin_client.get("/datasets/new")).status_code == 200
    confirm = await admin_client.post(
        "/datasets/new/confirm", data=data, headers={"X-CSRF-Token": csrf_token}
    )
    assert confirm.status_code == 200
    created = await admin_client.post(
        "/datasets/new", data=data, headers={"X-CSRF-Token": csrf_token}, follow_redirects=False
    )
    assert created.status_code == 303
    ds = (
        await db_session.execute(select(Dataset).where(Dataset.name == "管理者のリスト"))
    ).scalar_one()
    targets = (
        (await db_session.execute(select(InputTarget).where(InputTarget.dataset_id == ds.id)))
        .scalars()
        .all()
    )
    assert sorted(t.patient_id for t in targets) == ["A-1", "A-2"]
    assert all(t.started_by_id is None for t in targets)

    # 管理者は一覧と詳細を見られる（患者IDのみ）。収集操作は出ない
    assert (await admin_client.get("/datasets")).status_code == 200
    detail = await admin_client.get(f"/datasets/{ds.id}")
    assert detail.status_code == 200 and "A-1" in detail.text
    assert "/input-targets/claim" not in detail.text

    deleted = await admin_client.post(
        f"/datasets/{ds.id}/delete", headers={"X-CSRF-Token": csrf_token}, follow_redirects=False
    )
    assert deleted.status_code == 303
    db_session.expire_all()
    assert (
        await db_session.execute(select(Dataset).where(Dataset.id == ds.id))
    ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_admin_cannot_claim_check_or_batch(
    admin_client: AsyncClient, csrf_token: str, db_session: AsyncSession
) -> None:
    ds, t = await _dataset_with_target(db_session, "P-ADM")
    got = await admin_client.get(
        f"/datasets/{ds.id}/check", params={"questionnaire_id": t.questionnaire_id}
    )
    assert got.status_code == 403
    for path in (f"/datasets/{ds.id}/batch-autofill", f"/datasets/{ds.id}/check/{t.id}/confirm"):
        resp = await admin_client.post(path, data={}, headers={"X-CSRF-Token": csrf_token})
        assert resp.status_code == 403, path


@pytest.mark.asyncio
async def test_same_user_double_claim_ends_with_exactly_one_lease(tmp_path: Path) -> None:
    """同じ収集者の二重送信（2タブ・ダブルクリック）で有効なリースが2件にならない。"""
    from kawasemi.core.lease import valid_lease_clause

    for round_no in range(8):
        url = f"sqlite+aiosqlite:///{tmp_path / f'dbl{round_no}.db'}"
        ea = create_async_engine(url, connect_args={"timeout": 30})
        eb = create_async_engine(url, connect_args={"timeout": 30})
        async with ea.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
        ma = async_sessionmaker(ea, class_=AsyncSession, expire_on_commit=False)
        mb = async_sessionmaker(eb, class_=AsyncSession, expire_on_commit=False)
        async with ma() as a, mb() as b:
            for pid in ("P-1", "P-2", "P-3"):
                a.add(InputTarget(dataset_id=1, questionnaire_id=1, patient_id=pid))
            await a.commit()
            got_a, got_b = await asyncio.gather(
                claim_next_target(a, user_id=101), claim_next_target(b, user_id=101)
            )
            assert got_a is not None and got_b is not None
            assert got_a.id == got_b.id, round_no
            held = (
                (await a.execute(select(InputTarget).where(valid_lease_clause(101))))
                .scalars()
                .all()
            )
            assert len(held) == 1 and held[0].id == got_a.id
        await ea.dispose()
        await eb.dispose()


@pytest.mark.asyncio
async def test_convergence_releases_extra_leases_keeping_the_lowest_id(
    db_session: AsyncSession, collector_user: User
) -> None:
    """バックストップ: 万一2件持っていても、最小 id だけ残して手放す。"""
    from kawasemi.core.lease import _converge_to_one_lease

    t1 = await lease_patient(db_session, collector_user.id, "P-K1")
    t2 = await lease_patient(db_session, collector_user.id, "P-K2")
    kept = await _converge_to_one_lease(db_session, collector_user.id, t2.id)
    assert kept.id == t1.id
    assert (await _reload(db_session, t2.id)).started_by_id is None
    assert await user_can_access_patient(db_session, collector_user, "P-K2") is False
    assert await user_can_access_patient(db_session, collector_user, "P-K1") is True


@pytest.mark.asyncio
async def test_submit_requires_a_lease_on_this_patient_and_questionnaire(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    from kawasemi.models.questionnaire import Questionnaire
    from kawasemi.models.questionnaire_response import QuestionnaireResponse

    qs = []
    for n in (1, 2):
        q = Questionnaire(
            fhir_id=f"q-sub{n}",
            title=f"提出{n}",
            status="active",
            content={
                "resourceType": "Questionnaire",
                "id": f"q-sub{n}",
                "item": [{"linkId": "name", "type": "string", "text": "氏名"}],
            },
        )
        db_session.add(q)
        qs.append(q)
    await db_session.commit()
    leased = await lease_patient(
        db_session, collector_user.id, "P-SUB", questionnaire_id=qs[0].id, minutes=30
    )

    async def submit(qid: int, **extra: str):
        data = {"subject": "Patient/P-SUB", "name": "x", **extra}
        return await collector_client.post(
            f"/forms/{qid}/submit", data=data, headers={"X-CSRF-Token": csrf_token}
        )

    async def saved() -> int:
        return len((await db_session.execute(select(QuestionnaireResponse))).scalars().all())

    # 別の様式（担当は qs[0] の対象）: 拒否され、何も保存されない
    assert (await submit(qs[1].id)).status_code == 403
    assert (await submit(qs[1].id, input_target_id=str(leased.id))).status_code == 403
    # 別の対象 id を名乗る: 拒否
    assert (await submit(qs[0].id, input_target_id=str(leased.id + 99))).status_code == 403
    # subject が無い: 拒否
    resp = await collector_client.post(
        f"/forms/{qs[0].id}/submit", data={"name": "x"}, headers={"X-CSRF-Token": csrf_token}
    )
    assert resp.status_code == 403
    assert await saved() == 0
    assert (await _reload(db_session, leased.id)).status != InputTargetStatus.COMPLETED

    # input_target_id なしでも、担当の対象（患者 × 様式）に一致すれば通り、対象は完了になる
    ok = await submit(qs[0].id)
    assert ok.status_code == 200
    assert await saved() == 1
    done = await _reload(db_session, leased.id)
    assert done.status == InputTargetStatus.COMPLETED
    assert await user_can_access_patient(db_session, collector_user, "P-SUB") is False


@pytest.mark.asyncio
async def test_submit_with_the_correct_input_target_id_completes_it(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    ds, t = await _dataset_with_target(db_session, "P-OK", status=InputTargetStatus.IN_PROGRESS)
    t.started_by_id = collector_user.id
    t.lease_expires_at = datetime.now(UTC) + timedelta(minutes=30)
    await db_session.commit()
    resp = await collector_client.post(
        f"/forms/{t.questionnaire_id}/submit",
        data={"subject": "Patient/P-OK", "name": "x", "input_target_id": str(t.id)},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    assert (await _reload(db_session, t.id)).status == InputTargetStatus.COMPLETED
