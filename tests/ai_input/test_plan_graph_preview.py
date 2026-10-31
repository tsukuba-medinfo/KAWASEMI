"""取得プラン編集画面のフロー図プレビューが監査図と同じ規則で描かれること

以前はプレビューだけがクライアント側（`plan.html` の `buildMermaid`）で Mermaid 定義を
組み立てていたため、**同じ program に対してプレビューと検証画面が違う図を出していた**。

- `input` を全 op から辺にしていた（`viz.py` からは取り除いてあった「幻エッジ」）
- `params` 経由の依存を描かなかった
- `csv_extract` が抽出系のノード形状（角丸）にならなかった

規則の正本を `viz.program_to_mermaid` へ1本化し、プレビューは
`POST .../plan/graph` でその出力を受け取る形にした。ここではその同一性を固定する。
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input.viz import program_to_mermaid
from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import RoleType
from kawasemi.models.user import User

_GRAPH_PATH = "/ai-input/questionnaires/{qid}/plan/graph"

# `llm_extract` が `input: "$v1"` を持つ program。**`input` を実際に読むのは transform
# だけ**で、llm 系は prompt 内の `$id` 展開でしか前段の値を受け取らない。
# 一律に input から辺を引くと、データが渡らない「幻エッジ」を監査図に描くことになる。
_PROGRAM_WITH_PHANTOM_INPUT: dict[str, Any] = {
    "params": [{"name": "tbl", "value": "$v1", "kind": "identifier"}],
    "steps": [
        {"op": "sql_extract", "id": "v1", "connection_id": "dwh", "sql": "SELECT 1"},
        # input は読まれない（幻エッジ）。prompt の $v1 は読まれる（実エッジ）。
        {"op": "llm_extract", "id": "v2", "input": "$v1", "prompt": "$v1 から病名を抜く"},
        # params 経由の依存。{{tbl}} の値が $v1 なので v1 → v3 の点線が要る。
        {"op": "sql_extract", "id": "v3", "connection_id": "dwh", "sql": "SELECT * FROM {{tbl}}"},
        # csv_extract は抽出系（角丸）。クライアント版はこれを四角で描いていた。
        {"op": "csv_extract", "id": "v4", "path": "a.csv", "column": "c"},
    ],
    "return": "$v2",
}


def _edges(mermaid: str) -> set[str]:
    """Mermaid 定義から辺だけを取り出す（ノード定義・classDef は無視する）。"""
    return {
        line.strip()
        for line in mermaid.splitlines()
        if "-->" in line or "-.->" in line or ".->" in line
    }


@pytest.fixture
def plan_editor(designer_client: AsyncClient, designer_user: User) -> AsyncClient:
    """`plan_designer` として振る舞う認証済みクライアント（`RequirePlanEditor`）。"""
    token = create_access_token(data={"sub": str(designer_user.id), "act": RoleType.DESIGNER})
    designer_client.cookies.set(settings.jwt_cookie_name, token)
    return designer_client


async def _questionnaire(db_session: AsyncSession) -> Questionnaire:
    q = Questionnaire(
        fhir_id="q-plan-graph-preview",
        title="フロー図プレビューテスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-plan-graph-preview",
            "item": [{"linkId": "item1", "text": "[1] 登録番号", "type": "string"}],
        },
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    return q


async def _preview(
    client: AsyncClient, csrf_token: str, questionnaire_id: int, program: dict[str, Any]
) -> str:
    resp = await client.post(
        _GRAPH_PATH.format(qid=questionnaire_id),
        json={"program": program},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["mermaid"]


@pytest.mark.asyncio
async def test_preview_is_the_audit_graph_verbatim(
    plan_editor: AsyncClient, csrf_token: str, db_session: AsyncSession
):
    """プレビューが返すのは検証画面と**同じ関数の同じ出力**であること。

    `routes.validation_view` は `program_to_mermaid(program, trace)` を埋め込む。
    trace の有無でノードの注記は変わるが、**規則（辺の張り方・形状）は同じ関数**である。
    """
    q = await _questionnaire(db_session)
    preview = await _preview(plan_editor, csrf_token, q.id, _PROGRAM_WITH_PHANTOM_INPUT)
    assert preview == program_to_mermaid(_PROGRAM_WITH_PHANTOM_INPUT)


@pytest.mark.asyncio
async def test_preview_and_audit_graph_draw_the_same_edges_for_phantom_input(
    plan_editor: AsyncClient, csrf_token: str, db_session: AsyncSession
):
    """`transform` 以外の step に `input: "$ref"` があるとき、
    プレビューと検証画面の図に**同じ辺しか出ない**こと。

    検証画面は実行トレース付きで描く（注記が増える）ので、辺だけを取り出して比べる。
    """
    q = await _questionnaire(db_session)
    trace = [
        {"op": "sql_extract", "id": "v1", "output": "胃癌"},
        {"op": "llm_extract", "id": "v2", "output": "C16"},
        {"op": "sql_extract", "id": "v3", "output": "1"},
        {"op": "csv_extract", "id": "v4", "output": "x"},
    ]
    preview = await _preview(plan_editor, csrf_token, q.id, _PROGRAM_WITH_PHANTOM_INPUT)
    audit = program_to_mermaid(_PROGRAM_WITH_PHANTOM_INPUT, trace)
    assert _edges(preview) == _edges(audit)


@pytest.mark.asyncio
async def test_preview_does_not_draw_the_phantom_input_edge(
    plan_editor: AsyncClient, csrf_token: str, db_session: AsyncSession
):
    """幻エッジがプレビューから出ないこと。

    **同一性テストだけでは足りない。** 両方に幻エッジが出ても同一性は成り立つので、
    「どちらの規則へ揃えたか」をここで固定する。`v1 → v2` の実線は prompt 内の `$v1`
    に由来する1本だけで、`input` は辺を作らない。
    """
    q = await _questionnaire(db_session)
    program = {
        "steps": [
            {"op": "sql_extract", "id": "v1", "connection_id": "dwh", "sql": "SELECT 1"},
            # prompt に $v1 を持たない＝ input しか参照が無い。辺は1本も出ないはず
            {"op": "llm_extract", "id": "v2", "input": "$v1", "prompt": "病名を抜く"},
        ],
        "return": "$v2",
    }
    preview = await _preview(plan_editor, csrf_token, q.id, program)
    assert "n_v1 --> n_v2" not in preview
    # transform なら同じ `input` が実エッジになる（規則が「input を無視する」ではなく
    # 「transform だけが読む」であることを示す）
    program["steps"][1] = {"op": "transform", "id": "v2", "input": "$v1", "expr": "x"}
    assert "n_v1 --> n_v2" in await _preview(plan_editor, csrf_token, q.id, program)


@pytest.mark.asyncio
async def test_preview_draws_params_dependency_edges(
    plan_editor: AsyncClient, csrf_token: str, db_session: AsyncSession
):
    """params 経由の依存が**プレビューにも出る**こと。

    「前段 step の出力が後段の表名になる」という実在のデータフローで、描かないと
    「なぜこの値が採られたか」を図から追えない（ALCOA+ 帰属性）。
    """
    q = await _questionnaire(db_session)
    preview = await _preview(plan_editor, csrf_token, q.id, _PROGRAM_WITH_PHANTOM_INPUT)
    assert 'n_v1 -. "tbl" .-> n_v3' in preview


@pytest.mark.asyncio
async def test_preview_uses_the_extract_node_shape_for_csv_extract(
    plan_editor: AsyncClient, csrf_token: str, db_session: AsyncSession
):
    """`csv_extract` が抽出系の形状（角丸）で描かれること（クライアント版の抜け）。"""
    q = await _questionnaire(db_session)
    preview = await _preview(plan_editor, csrf_token, q.id, _PROGRAM_WITH_PHANTOM_INPUT)
    assert "  n_v4([" in preview


@pytest.mark.asyncio
async def test_preview_annotates_the_design_reasoning(
    plan_editor: AsyncClient, csrf_token: str, db_session: AsyncSession
):
    """`reasoning`（📝 設計根拠）がプレビューにも注記されること（クライアント版の抜け）。

    編集中でも `reasoning` は持っているので、実値と違って「trace が無いから出ない」
    ではない。
    """
    q = await _questionnaire(db_session)
    program = {
        "steps": [{"op": "sql_extract", "id": "v1", "reasoning": "病名は確定診断を採る"}],
        "return": "$v1",
    }
    assert "📝 設計根拠: 病名は確定診断を採る" in await _preview(
        plan_editor, csrf_token, q.id, program
    )


@pytest.mark.asyncio
async def test_graph_route_is_not_swallowed_by_the_save_route(
    plan_editor: AsyncClient, csrf_token: str, db_session: AsyncSession
):
    """`/plan/graph` が `/plan/{link_id}` に食われないこと。

    `save_plan` のパスは `/plan/{link_id}` なので、登録順が逆だと `link_id="graph"`
    として保存経路が拾い、**手順書が無い 404 になる**（プレビューが黙って壊れる）。
    """
    q = await _questionnaire(db_session)
    resp = await plan_editor.post(
        _GRAPH_PATH.format(qid=q.id),
        json={"program": {"steps": [{"op": "sql_extract", "id": "v1"}], "return": "$v1"}},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    assert resp.json()["mermaid"].startswith("graph TD")


@pytest.mark.asyncio
async def test_graph_route_requires_the_designer_role(
    designer_client: AsyncClient,
    collector_user: User,
    csrf_token: str,
    db_session: AsyncSession,
):
    """プレビューも `RequireDesigner` の関門を通ること（設計内容を漏らさない）。収集者は 403。"""
    q = await _questionnaire(db_session)
    token = create_access_token(data={"sub": str(collector_user.id), "act": RoleType.COLLECTOR})
    designer_client.cookies.set(settings.jwt_cookie_name, token)
    resp = await designer_client.post(
        _GRAPH_PATH.format(qid=q.id),
        json={"program": {"steps": []}},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_plan_template_does_not_build_the_graph_itself(
    plan_editor: AsyncClient, db_session: AsyncSession
):
    """`plan.html` が図を組み立て直していないこと（二重実装の再発検知）。

    JS のテストランナーが無いので、**レンダリング結果にサーバ経路が現れ、
    クライアント生成の痕跡が現れない**ことで固定する。
    """
    q = await _questionnaire(db_session)
    resp = await plan_editor.get(f"/ai-input/questionnaires/{q.id}/plan")
    assert resp.status_code == 200
    body = resp.text
    assert "/ai-input/questionnaires/${QID}/plan/graph" in body
    # Mermaid 定義を組み立てる痕跡（クライアント側の正本化）が残っていないこと
    assert "buildMermaid" not in body
    assert "graph TD" not in body
