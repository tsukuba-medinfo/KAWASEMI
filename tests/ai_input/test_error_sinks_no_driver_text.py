"""失敗の要約が、永続・返却される先（InputExecution・SSE の error 行・evidence・DB の job）へ
ドライバの文面（行・束縛の値）を持ち込まないこと。

実行中に起きた例外は、`error_summary`（`InputExecution` に永続）と、`/autofill/stream` の
NDJSON の `error` 行（クライアントへ返る）へ出る。ここへ `str(e)` を入れていた頃は、DB のドライバの
`DETAIL: Key (email)=(...)` のような文面が、そのまま保存・返却された。今は構造化した要約
（型名＋SQLSTATE＋制約名・列名）だけを出す。完全な例外はサーバのログに残る。
"""

from __future__ import annotations

import json

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input import executor as executor_mod
from kawasemi.ai_input.models import InputExecution, InputPlan
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.user import User
from tests.lease_helpers import lease_patient

NEEDLE = "PHI-NEEDLE-9137"
SQL_PROGRAM = {"steps": [{"op": "sql_extract", "id": "s1", "sql": "SELECT 1"}], "return": "$s1"}


class _Orig(Exception):
    """asyncpg の例外のように、構造化した属性を持つ（文面には行の値が入っている）。"""

    sqlstate = "23505"
    constraint_name = "users_email_key"


def _driver_error() -> IntegrityError:
    orig = _Orig(f"DETAIL:  Key (email)=({NEEDLE}@example.org) already exists.")
    return IntegrityError("INSERT ...", {"email": f"{NEEDLE}@example.org"}, orig)


async def _plan_q(db: AsyncSession) -> Questionnaire:
    q = Questionnaire(
        fhir_id="q-sink",
        title="シンクのテスト",
        status="active",
        content={"resourceType": "Questionnaire", "id": "q-sink"},
    )
    db.add(q)
    await db.flush()
    db.add(
        InputPlan(
            questionnaire_id=q.id,
            link_id="item1",
            item_text="項目",
            program=SQL_PROGRAM,
            status="confirmed",
        )
    )
    await db.commit()
    return q


@pytest.fixture
def failing_interpreter(monkeypatch: pytest.MonkeyPatch) -> None:
    async def boom(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        raise _driver_error()

    async def boom_stream(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        raise _driver_error()
        yield  # 非同期ジェネレータにする

    monkeypatch.setattr(executor_mod.DSLInterpreter, "execute", boom)
    monkeypatch.setattr(executor_mod.DSLInterpreter, "execute_stream", boom_stream)


@pytest.mark.asyncio
async def test_autofill_failure_persists_a_structural_summary_only(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    failing_interpreter: None,
) -> None:
    q = await _plan_q(db_session)
    await lease_patient(db_session, collector_user.id, "PAT-1", questionnaire_id=q.id)

    resp = await collector_client.post(
        f"/ai-input/questionnaires/{q.id}/autofill",
        json={"patient_id": "PAT-1"},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert NEEDLE not in resp.text
    rows = (await db_session.execute(select(InputExecution))).scalars().all()
    assert len(rows) == 1
    summary = rows[0].error_summary or ""
    assert NEEDLE not in summary, summary
    assert "IntegrityError" in summary and "sqlstate=23505" in summary
    assert "constraint=users_email_key" in summary


@pytest.mark.asyncio
async def test_autofill_stream_error_line_and_persisted_summary_carry_no_driver_text(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    failing_interpreter: None,
) -> None:
    q = await _plan_q(db_session)
    await lease_patient(db_session, collector_user.id, "PAT-1", questionnaire_id=q.id)

    resp = await collector_client.post(
        f"/ai-input/questionnaires/{q.id}/autofill/stream",
        json={"patient_id": "PAT-1"},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert NEEDLE not in resp.text
    lines = [json.loads(line) for line in resp.text.splitlines() if line.strip()]
    errors = [ln for ln in lines if ln.get("type") == "error"]
    assert errors, resp.text
    assert "sqlstate=23505" in errors[0]["message"]
    rows = (await db_session.execute(select(InputExecution))).scalars().all()
    assert rows and NEEDLE not in (rows[0].error_summary or "")


@pytest.mark.asyncio
async def test_a_plain_runtime_error_with_a_detail_line_shows_only_its_type(
    collector_client: AsyncClient,
    collector_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom_stream(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003, ANN202
        raise RuntimeError(f"DETAIL: Key (email)=({NEEDLE}@example.org) already exists.")
        yield  # 非同期ジェネレータにする

    monkeypatch.setattr(executor_mod.DSLInterpreter, "execute_stream", boom_stream)
    q = await _plan_q(db_session)
    await lease_patient(db_session, collector_user.id, "PAT-1", questionnaire_id=q.id)

    await collector_client.post(
        f"/ai-input/questionnaires/{q.id}/autofill/stream",
        json={"patient_id": "PAT-1"},
        headers={"X-CSRF-Token": csrf_token},
    )

    rows = (await db_session.execute(select(InputExecution))).scalars().all()
    assert rows and rows[0].error_summary == "RuntimeError"


@pytest.mark.asyncio
async def test_executor_step_error_in_evidence_has_no_driver_text() -> None:
    """step の例外は、evidence の error（API の応答・DB に出る）に要約だけを入れる。"""
    assert NEEDLE not in executor_mod._exc_msg(_driver_error())
    assert executor_mod._exc_msg(_driver_error()).startswith("IntegrityError")
