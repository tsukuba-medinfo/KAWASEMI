"""再スキャン（2026-11-14）F5・F6・F7: Mermaid の無害化、図の計算量、physical_format の上限。"""

from __future__ import annotations

import time
from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.dsl import MAX_STEPS, validate_program
from kawasemi.ai_input.key_resolver.format import apply_physical_format
from kawasemi.ai_input.physical_format import physical_format_errors
from kawasemi.ai_input.viz import _esc, program_to_mermaid
from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import RoleType
from kawasemi.models.user import User

_DIRECTIVE = "%%{init:{'themeCSS':'x{background:url(//evil.example/p)}'}}%%"


# --- F5: Mermaid のディレクティブ・エンティティ ------------------------------------


def test_esc_neutralizes_directive_comment_and_entity_syntax() -> None:
    out = _esc(_DIRECTIVE, limit=500)

    assert "%" not in out
    assert "{" not in out and "}" not in out
    assert "#" not in out
    assert "init" in out  # 字面は読める


def test_esc_keeps_ordinary_labels_readable() -> None:
    assert _esc("WBC 50% 以上") == "WBC 50％ 以上"
    assert _esc("患者#1") == "患者＃1"
    assert _esc("a < b") == "a ＜ b"


def test_a_directive_in_reasoning_step_id_and_trace_cannot_reach_the_diagram_source() -> None:
    program: dict[str, Any] = {
        "steps": [
            {
                "op": "sql_extract",
                "id": _DIRECTIVE,
                "reasoning": _DIRECTIVE,
                "connection_id": "dwh",
                "sql": "SELECT 1",
            },
            {"op": "llm_extract", "id": "v2", "prompt": "$v1"},
        ],
        "return": "$v2",
    }
    trace = [
        {"id": _DIRECTIVE, "output": _DIRECTIVE},
        {"id": "v2", "error": _DIRECTIVE},
    ]

    for diagram in (program_to_mermaid(program), program_to_mermaid(program, trace)):
        # ディレクティブは `%%{...}%%`。`%` が一つも残らなければ、構文として働かない。
        assert "%" not in diagram
        # 字面はラベルの文字として残るが、構文（`%%`・`{` `}`・`#`）は全角になっている
        assert "％％｛init" in diagram
        assert "{'" not in diagram and "'}" not in diagram


# --- F6: 計算量 ------------------------------------------------------------------


def test_node_id_allocation_is_not_quadratic_for_identical_step_ids() -> None:
    steps = [{"op": "sql_extract", "id": ""} for _ in range(50_000)]

    started = time.perf_counter()
    diagram = program_to_mermaid({"steps": steps})
    elapsed = time.perf_counter() - started

    assert elapsed < 5.0
    assert diagram.count("\n") >= 50_000  # 50,000 個のノードがすべて描かれる


def test_node_ids_stay_unique_and_stable_when_an_explicit_name_collides() -> None:
    steps = [
        {"op": "sql_extract", "id": "a"},
        {"op": "sql_extract", "id": "a"},
        {"op": "sql_extract", "id": "a__2"},  # 連番で割り当てる名前を明示
        {"op": "sql_extract", "id": "a"},
    ]
    diagram = program_to_mermaid({"steps": steps})
    ids = [line.split("[", 1)[0].split("(", 1)[0].strip() for line in diagram.splitlines()[1:]]

    assert len(ids) == len(set(ids)) == 4
    assert ids[0] == "n_a"
    assert ids[1] == "n_a__2"


@pytest.fixture
def plan_editor(designer_client: AsyncClient, designer_user: User) -> AsyncClient:
    token = create_access_token(data={"sub": str(designer_user.id), "act": RoleType.DESIGNER})
    designer_client.cookies.set(settings.jwt_cookie_name, token)
    return designer_client


@pytest.mark.asyncio
async def test_plan_graph_refuses_a_program_over_the_step_cap(
    plan_editor: AsyncClient, csrf_token: str, db_session: AsyncSession
) -> None:
    q = Questionnaire(
        fhir_id="q-rescan2-graph",
        title="step 上限",
        status="active",
        content={"resourceType": "Questionnaire", "id": "q-rescan2-graph", "item": []},
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    url = f"/ai-input/questionnaires/{q.id}/plan/graph"

    over = {"steps": [{"op": "sql_extract", "id": f"s{i}"} for i in range(MAX_STEPS + 1)]}
    resp = await plan_editor.post(url, json={"program": over}, headers={"X-CSRF-Token": csrf_token})
    assert resp.status_code == 422

    ok = {"steps": [{"op": "sql_extract", "id": f"s{i}"} for i in range(MAX_STEPS)]}
    resp = await plan_editor.post(url, json={"program": ok}, headers={"X-CSRF-Token": csrf_token})
    assert resp.status_code == 200


@pytest.mark.asyncio
@pytest.mark.parametrize("op", ["`", "`x", "", "a b", "A", "x\n%%{init:{}}%%", "a" * 65])
async def test_plan_graph_refuses_an_op_that_is_not_plain_vocabulary(
    plan_editor: AsyncClient, csrf_token: str, db_session: AsyncSession, op: str
) -> None:
    q = Questionnaire(
        fhir_id="q-rescan2-op",
        title="op の検証",
        status="active",
        content={"resourceType": "Questionnaire", "id": "q-rescan2-op", "item": []},
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    url = f"/ai-input/questionnaires/{q.id}/plan/graph"

    resp = await plan_editor.post(
        url,
        json={"program": {"steps": [{"op": op, "id": "s"}]}},
        headers={"X-CSRF-Token": csrf_token},
    )

    assert resp.status_code == 422


def test_esc_neutralizes_a_leading_backtick() -> None:
    assert not _esc("`markdown string`").startswith("`")
    assert "`" not in _esc("a`b")


# --- F7: physical_format の上限 -----------------------------------------------------


def test_a_huge_pad_is_refused_before_any_allocation() -> None:
    with pytest.raises(ValueError):
        apply_physical_format("123", {"pad": 8_000_000_000})


@pytest.mark.parametrize(
    "fmt",
    [
        {"pad": 65},
        {"pad": -1},
        {"pad": "10"},
        {"pad": 10.5},
        {"pad": True},
        {"pad": [10]},
        {"padchar": "ab"},
        {"padchar": ""},
        {"padchar": 0},
        {"prefix": "x" * 65},
        {"suffix": 5},
        {"unknown": 1},
        "pad=10",
        [1],
    ],
)
def test_out_of_range_physical_format_is_rejected(fmt: Any) -> None:
    assert physical_format_errors(fmt)
    with pytest.raises(ValueError):
        apply_physical_format("123", fmt)


@pytest.mark.parametrize(
    ("fmt", "expected"),
    [
        (None, "123"),
        ({}, "123"),
        ({"pad": 10}, "0000000123"),
        ({"pad": 64}, "123".rjust(64, "0")),
        ({"pad": 0}, "123"),
        ({"pad": 6, "padchar": "x"}, "xxx123"),
        ({"prefix": "P-", "suffix": "-X"}, "P-123-X"),
    ],
)
def test_legitimate_physical_format_still_works(fmt: Any, expected: str) -> None:
    assert physical_format_errors(fmt) == []
    assert apply_physical_format("123", fmt) == expected


def test_save_time_validation_rejects_a_bad_physical_format() -> None:
    step: dict[str, Any] = {
        "op": "sql_extract",
        "id": "v1",
        "connection_id": "dwh",
        "sql": "SELECT 1",
        "key_type": "mrn",
        "physical_format": {"pad": 8_000_000_000},
    }
    program = {"steps": [step], "return": "$v1"}

    errors = validate_program(program)

    assert any("physical_format.pad" in e for e in errors)
    step["physical_format"] = {"pad": 10}
    assert not any("physical_format" in e for e in validate_program(program))
