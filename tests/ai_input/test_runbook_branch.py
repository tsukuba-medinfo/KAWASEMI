"""手順書ステップの種別タグ（action_type）に条件分岐の語彙を持たせたことのテスト。

`condition`（判定）と `branch`（判定に応じた取得元・値の選択）を語彙に加えた。狙いは、
取得元が患者の文脈で変わる項目や候補の優先順がある項目を、手順書の段階で分解して書けるように
すること（ステップ3はそれを guard / branch へ写す）。

ここで固定するのは次の3点:

1. **語彙の正本が1つである**こと。`schemas.RUNBOOK_ACTION_TYPES` に足したタグが、生成
   プロンプト・UI のセレクタ・利用者向け文書に届いていない（あるいはその逆）状態を落とす。
   タグは「UI に選択肢が出る」「プロンプトが提示する」の両方が揃って初めて使われるので、
   片側だけ増えるドリフトは静かに機能を殺す。
2. **語彙外のタグは弾かずに落ちる**こと。人手の保存でも LLM 生成でも同じ扱いで、綴り違い
   1つでチャンク全体が失敗しない。
3. **タグがステップ3のプロンプトへ届く**こと。届かないなら語彙を増やしても写像は起きない。
"""

from __future__ import annotations

import ast
import inspect
import json
import re
import types
from pathlib import Path
from typing import Any, get_args

import pytest
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input import schemas as schemas_module
from kawasemi.ai_input.compiler import COMPILE_SYSTEM_PROMPT, AIPlanCompiler
from kawasemi.ai_input.e2e.scripted_llm import load_design_script
from kawasemi.ai_input.llm_json import LARGE_CONTEXT_OPTIONS
from kawasemi.ai_input.models import InputRunbook
from kawasemi.ai_input.routes import _steps_with_known_tags
from kawasemi.ai_input.runbook import (
    RUNBOOK_CHUNK_SIZE,
    RUNBOOK_SYSTEM_PROMPT,
    AIRunbookGenerator,
)
from kawasemi.ai_input.schemas import (
    RUNBOOK_ACTION_TYPES,
    GeneratedRunbooks,
    RunbookActionType,
    RunbookSaveRequest,
    RunbookStep,
)
from kawasemi.core.auth import create_access_token
from kawasemi.core.config import settings
from kawasemi.models.questionnaire import Questionnaire
from kawasemi.models.role import RoleType
from kawasemi.models.user import User

_REPO_ROOT = Path(__file__).resolve().parents[2]
_UI_TEMPLATE = _REPO_ROOT / "kawasemi/web/templates/ai_input/design_runbook.html"
_QUICKSTART = _REPO_ROOT / "docs/ai_mode_guide.md"


# =============================================================================
# 1. 語彙の正本が1つであること（正本 ↔ プロンプト ↔ UI ↔ 文書 ↔ デモデータ）
# =============================================================================


def test_the_new_branching_tags_are_in_the_vocabulary() -> None:
    """判定と分岐が語彙にあること。"""
    assert "condition" in RUNBOOK_ACTION_TYPES
    assert "branch" in RUNBOOK_ACTION_TYPES


def test_the_tuple_is_derived_from_the_literal() -> None:
    """`RUNBOOK_ACTION_TYPES` が `RunbookActionType` から導出されていること。

    **値の一致では導出を見られない。** 書き写したハードコードでも今は値が一致するので
    `== get_args(...)` は通ってしまう（実測済み）。正本を1つにする意図が消えると、次に
    語彙を足すときに2箇所へ書く形へ戻り、Literal 側だけ増えて enum と tuple がずれる。

    文字列照合（`"..." in source`）も通り抜ける穴がある：宣言文をそのまま残し、直後の行で
    `RUNBOOK_ACTION_TYPES = (...)` のようにハードコードへ上書きしても、宣言文自体は
    ソースに存在するので文字列照合は通る（実測済み）。導出であることを見るには、
    モジュールを AST として解析し、`RUNBOOK_ACTION_TYPES` への代入が1つだけで、その
    右辺が `get_args(RunbookActionType)` の呼び出しであることを確認する必要がある。
    """
    assert RUNBOOK_ACTION_TYPES == get_args(RunbookActionType)

    source = inspect.getsource(schemas_module)
    tree = ast.parse(source)

    def _targets_the_tuple(node: ast.AST) -> bool:
        if isinstance(node, ast.AnnAssign):
            return isinstance(node.target, ast.Name) and node.target.id == "RUNBOOK_ACTION_TYPES"
        if isinstance(node, ast.Assign):
            return any(
                isinstance(t, ast.Name) and t.id == "RUNBOOK_ACTION_TYPES" for t in node.targets
            )
        return False

    assignments = [node for node in ast.walk(tree) if _targets_the_tuple(node)]
    assert len(assignments) == 1, (
        "RUNBOOK_ACTION_TYPES への代入は1つだけであるべき（上書き代入が紛れていない）"
    )
    (assignment,) = assignments
    rhs = assignment.value
    assert isinstance(rhs, ast.Call), "右辺は関数呼び出しであるべき"
    assert isinstance(rhs.func, ast.Name) and rhs.func.id == "get_args"
    assert len(rhs.args) == 1
    (arg,) = rhs.args
    assert isinstance(arg, ast.Name) and arg.id == "RunbookActionType"


def test_the_pre_existing_tags_are_not_lost() -> None:
    """既存5タグの回帰（語彙の入れ替えではなく追加であること）。"""
    for tag in ("fetch", "select", "transform", "llm", "manual"):
        assert tag in RUNBOOK_ACTION_TYPES


def _prompt_glosses() -> dict[str, str]:
    """生成プロンプトの action_type 節が提示しているタグと**その語釈**を読み取る。

    節の範囲を `- action_type:` 〜 `- data_hint:` に限るのは、プロンプト本文の他の場所に
    出る同じ語（例: 手順の説明文中の「fetch のステップ」）を拾わないため。節の中の
    `fetch（データ取得）` という書式から、タグと括弧内の語釈を対で取る。

    語だけを取って集合比較すると、`condition（人手確認）` のように**語釈を入れ替えた**
    退行が通る（実測済み）。語釈まで読む。
    """
    section = RUNBOOK_SYSTEM_PROMPT.split("- action_type:", 1)[1].split("- data_hint:", 1)[0]
    return {tag: gloss for tag, gloss in re.findall(r"([a-z_]+)（([^）]*)）", section)}


def test_generation_prompt_presents_exactly_the_vocabulary() -> None:
    """生成プロンプトが提示するタグ集合が正本と一致すること（両方向のドリフトを落とす）。"""
    assert set(_prompt_glosses()) == set(RUNBOOK_ACTION_TYPES)


def test_generation_prompt_glosses_say_what_each_new_tag_means() -> None:
    """新タグの語釈が、判定と分岐という中身を指していること。

    タグ名が列挙されていても語釈が別物なら（`branch（単位換算）`）、LLM はそのタグを
    分岐に使わない。集合の一致だけでは見えない。
    """
    glosses = _prompt_glosses()
    assert "判定" in glosses["condition"], glosses["condition"]
    assert "分岐" in glosses["branch"], glosses["branch"]
    # `select` との境界（所在候補の選択は select ではない）を語釈に持たせる
    assert "選" in glosses["select"] and "所在候補" in glosses["select"], glosses["select"]


def test_generation_prompt_tells_how_to_write_a_branch() -> None:
    """語彙を出すだけでなく、分岐の書き方（判定と分岐を分ける）を指示していること。

    タグ名だけ足しても LLM は使い方を決められない。「condition で何を見るか、branch で
    どちらから取るか」の指示と、コードを書かせない歯止めが本文にあることを見る。
    """
    section = RUNBOOK_SYSTEM_PROMPT.split("条件分岐の書き方:", 1)
    assert len(section) == 2, "条件分岐の書き方の節が無い"
    body = section[1]
    assert "condition" in body and "branch" in body

    # **語が在るかではなく、指示の形が在るかを見る。** 分岐の書き方として最低限必要な形
    # ＝「2つの場合分け」「①判定→②分岐の分解（と役割の向き）」「取得は別ステップ」を要求する。
    #
    # 限界を正確に書く: ここで assert している語をすべて含んだ**語の羅列**（箇条書きの体裁
    # だけ整えたもの）は依然として通る。文章として読めるかは固定できない。守れているのは
    # 「場合分けと分解の骨格が消えていないこと」までである。
    assert "取得元が患者の文脈で変わる" in body  # 場合1
    assert "優先順の候補が複数ある" in body  # 場合2（フォールバック取得計画）
    # 分解の指示。**役割の方向まで見る**（①②の在否だけだと、condition と branch の
    # 説明を入れ替えた退行が通る）。
    i_cond = body.index("①condition")
    i_judge = body.index("何を見てどちらと判定するか")
    i_branch = body.index("②branch")
    i_pick = body.index("判定ごとにどちらから取るか")
    assert i_cond < i_judge < i_branch < i_pick, {
        "①condition": i_cond,
        "判定の説明": i_judge,
        "②branch": i_branch,
        "取得元の説明": i_pick,
    }
    assert "取得そのものは fetch" in body  # 判定・分岐と取得を混ぜない
    # タグは手順の意味であって op の予告ではない。これが消えると
    # 「SQL 1本で済むなら transform」という学習できない区別がまた入り込む。
    assert "有無の判定も condition + branch" in RUNBOOK_SYSTEM_PROMPT
    assert "SQL 1本になるか" in RUNBOOK_SYSTEM_PROMPT
    assert len([ln for ln in body.splitlines() if ln.startswith("- ")]) >= 4, body
    # 判定の根拠は臨床的コンテキストから採る（データ所在に無い条件を捏造させない）
    assert "臨床的コンテキスト" in body
    assert "推測で作らない" in body
    # 捏造させない代わりの逃げ道（材料が無いなら manual にする）を必ず示す。
    # 歯止めだけ置いて代替を書かないと、LLM は条件を作って埋めに来る。
    assert "manual" in body
    # 手順書は自由文であってコードではない。**指示そのものを固定する**（`"SQL" in body` だけ
    # では、別の箇条の「SQL 1本になるか」で満たされてしまい、この指示を丸ごと消しても通る）。
    assert "title と description にコードや SQL を書かない" in body
    assert "クエリ例を添えるなら data_hint に置く" in body


def _ui_action_types() -> list[tuple[str, str]]:
    """UI セレクタの ACTION_TYPES を (値, ラベル) の並びで読み取る（空の選択肢も含む）。"""
    html = _UI_TEMPLATE.read_text(encoding="utf-8")
    block = html.split("const ACTION_TYPES = [", 1)[1].split("];", 1)[0]
    return re.findall(r'\["([a-z_]*)", "([^"]*)"\]', block)


def test_ui_selector_offers_exactly_the_vocabulary() -> None:
    """UI の選択肢が正本と一致すること。

    ここが遅れると、生成された `condition` / `branch` を画面で編集すると別の値へ
    書き換わる（選択肢に無い値はセレクタが保持できない）。
    """
    values = [v for v, _ in _ui_action_types() if v]
    assert sorted(values) == sorted(RUNBOOK_ACTION_TYPES)
    assert len(values) == len(set(values)), f"UI の選択肢に重複がある: {values}"


def test_ui_selector_keeps_the_empty_choice() -> None:
    """「種別なし」の選択肢が残っていること。

    これが消えると**タグを外せなくなる**（未タグのステップを開いて保存すると先頭の
    選択肢＝`fetch` に化ける）。値の集合だけを見る検査では気づけない。
    """
    assert _ui_action_types()[0][0] == "", _ui_action_types()[:2]
    assert "種別なし" in _ui_action_types()[0][1]


def test_ui_labels_say_what_the_new_tags_mean() -> None:
    """UI のラベルが判定・分岐という中身を指していること（ラベルの入れ替えを落とす）。"""
    labels = dict(_ui_action_types())
    assert "判定" in labels["condition"], labels["condition"]
    assert "分岐" in labels["branch"], labels["branch"]
    # 分岐は取得元だけでなく**値**の選択でもある（デモ2の2件はどちらも値の選択）
    assert "値" in labels["branch"], labels["branch"]


def test_quickstart_enumerates_exactly_the_vocabulary() -> None:
    """利用者向け文書の**語彙列挙**が正本と一致すること（件数まで）。

    「本文のどこかにその語が出る」では足りない。列挙から1つ落としても、後段の説明文が
    同じ語に触れていれば通ってしまう。列挙の区間だけを読み、件数も突き合わせる。
    """
    text = _QUICKSTART.read_text(encoding="utf-8")
    section = text.split("`action_type` は手順書ステップの種別タグ", 1)
    assert len(section) == 2, "ai_mode_guide.md に action_type の語彙の説明が無い"
    body = section[1]
    m = re.search(r"語彙は(.+?)の(\d+)種です", body, re.DOTALL)
    assert m is not None, "語彙の列挙が見つからない"
    listed = set(re.findall(r"`([a-z_]+)`", m.group(1)))
    assert listed == set(RUNBOOK_ACTION_TYPES)
    assert int(m.group(2)) == len(RUNBOOK_ACTION_TYPES)
    # 語彙外を「弾く」ではなく「落とす」ことも書く（挙動の説明が実装と食い違わないように）
    assert "null" in body[: m.end() + 400]
    # タグは op の予告ではない、という境界も文書側に置く（実装・デモ・文書の3箇所で守る）
    assert "op で実装するかの予告ではありません" in body
    assert "所在候補の選択は `select` ではなく" in body


# E2E 台本（`examples/e2e/script/design.yaml`）は項目ごとに手順書とプランを対で持つ。
# 公開している唯一の通しの作例なので、タグとプランの対応をここで見る。
_E2E_ITEMS: dict[str, dict[str, Any]] = load_design_script()
_E2E_RUNBOOKS: dict[str, list[dict[str, Any]]] = {
    link_id: item.get("runbook") or [] for link_id, item in _E2E_ITEMS.items()
}


def _e2e_program(link_id: str) -> dict[str, Any] | None:
    item = _E2E_ITEMS.get(link_id)
    return item.get("program") if item else None


def test_e2e_runbook_tags_are_all_in_the_vocabulary() -> None:
    """E2E 台本の手順書が語彙外のタグを持たないこと。

    台本の手順書は LLM の応答として `RunbookStep` の検証を通り、語彙外のタグは**静かに
    None へ落ちる**。台本の側で綴りを誤っても試験は通り続けるので、ここで見る。
    """
    used = {s.get("action_type") for steps in _E2E_RUNBOOKS.values() for s in steps}
    assert used - {None}, "E2E 台本の手順書にタグが1つも無い（前提が崩れている）"
    unknown = {t for t in used if t is not None} - set(RUNBOOK_ACTION_TYPES)
    assert not unknown, f"E2E 台本の手順書に語彙外のタグ: {unknown}"


def test_e2e_writes_the_judgement_before_the_branch() -> None:
    """判定と分岐の両方を持つ項目で、判定が分岐より前に来ること。

    「判定 → 分岐」の順序は、ステップ3が guard/branch へ写すときの前提でもある。
    タグの個数が合っていても順序が逆なら手順書として読めない。
    """
    both = [
        link_id
        for link_id, steps in _E2E_RUNBOOKS.items()
        if {"condition", "branch"} <= {s.get("action_type") for s in steps}
    ]
    assert both, "判定と分岐の両方を持つ E2E 項目が無い（前提が崩れている）"
    for link_id in both:
        tags = [s.get("action_type") for s in _E2E_RUNBOOKS[link_id]]
        assert tags.index("condition") < len(tags) - 1 - tags[::-1].index("branch"), (
            f"{link_id}: 判定が分岐より後にある: {tags}"
        )


def _plan_makes_a_conditional_value(program: dict[str, Any]) -> bool:
    """プランが値を条件で選んでいるか（`branch` op か、SQL 側の CASE か）。

    タグは手順の意味であって op の予告ではないので、**実装が SQL の CASE 1本でも
    「分岐している」と見なす**。ここを `branch` op に限ると、同じ日本語の手順に別の
    タグを付けることを強制してしまう。

    一方、同一の取得結果の中でのカラム代替（SQL の `COALESCE(a, b)`）は**意図的に
    数えない**。あれは「どこから取るか」ではなく「取れた1件の中でどの列を使うか」なので
    `select` に留める（`schemas.py` の語彙コメントにも書いた）。ここで数えると `select` と
    `branch` の境界が消える。
    """
    for step in program.get("steps", []):
        if step.get("op") == "branch":
            return True
        text = " ".join(str(v) for v in step.values())
        if "CASE WHEN" in text.upper():
            return True
    return False


def test_plan_makes_a_conditional_value_counts_case_but_not_coalesce() -> None:
    """判定ヘルパの境界: `branch` op と SQL の CASE は分岐、COALESCE と素の取得は分岐でない。"""
    assert _plan_makes_a_conditional_value(
        {"steps": [{"op": "branch", "id": "v", "when": "$a", "then": "1", "else_branch": "2"}]}
    )
    assert _plan_makes_a_conditional_value(
        {"steps": [{"op": "sql_extract", "id": "v", "sql": "SELECT case when x then 1 end"}]}
    )
    assert not _plan_makes_a_conditional_value(
        {"steps": [{"op": "sql_extract", "id": "v", "sql": "SELECT COALESCE(a, b) FROM t"}]}
    )
    assert not _plan_makes_a_conditional_value({"steps": [{"op": "sql_extract", "id": "v"}]})


_CONDITION_JUDGEMENT_MARKERS = ("判定", "かどうか")


def test_e2e_condition_steps_describe_a_judgement() -> None:
    """`condition` ステップの自由文が判定を語っていること。

    台本は利用者が読む通しの作例なので、タグと自由文が食い違うと書き方の手本にならない。
    判定を述べる語のいずれかが含まれているかだけを見る（言い回しを丸暗記しない）。
    """
    seen = 0
    for link_id, steps in _E2E_RUNBOOKS.items():
        for step in steps:
            if step.get("action_type") != "condition":
                continue
            seen += 1
            text = f"{step.get('title', '')} {step.get('description', '')}"
            assert any(marker in text for marker in _CONDITION_JUDGEMENT_MARKERS), (
                f"{link_id}: condition ステップの自由文が判定を語っていない: {text!r}"
            )
    assert seen, "condition ステップを持つ E2E 項目が無い"


def test_e2e_branch_steps_match_a_plan_that_really_branches() -> None:
    """`branch` と書いた項目のプランが、実際に値を条件で選んでいること。

    タグを説明文として足しただけで、プランが条件によらず1つの値を返すなら、その手順書は
    嘘になる。
    """
    tagged = {
        link_id
        for link_id, steps in _E2E_RUNBOOKS.items()
        if any(s.get("action_type") == "branch" for s in steps)
    }
    assert tagged, "branch タグを持つ E2E 項目が無い"
    for link_id in tagged:
        program = _e2e_program(link_id)
        assert program is not None, f"{link_id} のプランが無い"
        assert _plan_makes_a_conditional_value(program), (
            f"{link_id} の手順書は branch と書くがプランは条件で値を選んでいない: {program}"
        )


def test_e2e_condition_steps_match_a_plan_that_really_judges() -> None:
    """`condition` と書いた項目のプランに、実際に判定があること（condition 側の逆方向）。

    分岐していない項目への誤付与は、`branch` 側の検査だけでは見えない（実測済み）。
    判定は `compare` / `llm_judge` op、または SQL 側の CASE のいずれかで現れる。
    """
    tagged = {
        link_id
        for link_id, steps in _E2E_RUNBOOKS.items()
        if any(s.get("action_type") == "condition" for s in steps)
    }
    assert tagged, "condition タグを持つ E2E 項目が無い"
    for link_id in tagged:
        program = _e2e_program(link_id)
        assert program is not None, f"{link_id} のプランが無い"
        ops = {s.get("op") for s in program.get("steps", [])}
        judged = bool(ops & {"compare", "llm_judge"}) or _plan_makes_a_conditional_value(program)
        assert judged, f"{link_id} の手順書は condition と書くがプランは判定していない: {ops}"


# =============================================================================
# 2. 語彙外のタグは弾かずに落ちる（人手の保存でも LLM 生成でも同じ）
# =============================================================================


@pytest.mark.parametrize("tag", sorted(RUNBOOK_ACTION_TYPES))
def test_every_vocabulary_tag_survives_validation(tag: str) -> None:
    """語彙のタグはそのまま保持されること（語彙を増やしたら自動で対象が増える）。"""
    assert RunbookStep(order=1, title="t", action_type=tag).action_type == tag


@pytest.mark.parametrize(
    "given,expected",
    [
        ("BRANCH", "branch"),
        (" branch ", "branch"),
        ("Condition", "condition"),
        ("分岐", None),  # 和文のラベルは値ではない
        ("if", None),  # 近いが語彙外
        ("branches", None),  # 綴り違い
        ("", None),
        (None, None),
        # 非文字列。LLM は `["condition"]` のような形も吐く。`mode="after"` にすると
        # ここで ValidationError になり、**チャンク全体が落ちる**（この検証の目的が消える）。
        (["condition"], None),
        (5, None),
        ({"a": 1}, None),
    ],
)
def test_action_type_is_normalized_or_dropped(given: Any, expected: str | None) -> None:
    """大文字・空白は吸収し、語彙外は None へ落とすこと（例外にしない）。"""
    assert RunbookStep(order=1, title="t", action_type=given).action_type == expected


def test_unknown_tag_does_not_fail_the_save_request() -> None:
    """保存リクエスト中の1ステップの綴り違いが、他のステップを落とさないこと。"""
    payload = RunbookSaveRequest(
        steps=[
            {"order": 1, "title": "判定", "action_type": "condition"},
            {"order": 2, "title": "謎", "action_type": "ぶんき"},
            {"order": 3, "title": "分岐", "action_type": "branch"},
        ]  # type: ignore[list-item]
    )
    assert [s.action_type for s in payload.steps] == ["condition", None, "branch"]


def test_unknown_tag_does_not_fail_the_generated_chunk() -> None:
    """LLM 出力中の綴り違いが、そのチャンク（複数項目）の解析を落とさないこと。

    生成は項目をまとめて1回のコールで行うので、1タグの綴り違いで例外にすると同じチャンクの
    他項目まで失われる。落とす設計を、structured output の受け皿の側でも固定する。
    """
    parsed = GeneratedRunbooks(
        **{
            "runbooks": [
                {
                    "link_id": "item1",
                    "steps": [
                        {"order": 1, "title": "判定", "action_type": "condition"},
                        {"order": 2, "title": "謎", "action_type": "decide"},
                        # 非文字列（型が違う形も同じく落ちるだけであること）
                        {"order": 3, "title": "配列", "action_type": ["branch"]},
                    ],
                },
                {"link_id": "item2", "steps": [{"order": 1, "title": "取得"}]},
            ]
        }
    )
    assert [g.link_id for g in parsed.runbooks] == ["item1", "item2"]
    assert [s.action_type for s in parsed.runbooks[0].steps] == ["condition", None, None]


def test_vocabulary_reaches_the_structured_output_schema() -> None:
    """語彙が structured output の JSON スキーマの enum に載ること。

    正本を1箇所に集めても、それが `response_format` へ届かなければ LLM は何を吐いても
    よく、外れた綴りは黙って None へ落ちる（＝分岐が手順書に書かれない）。制約デコード
    へ届いているかは、生成の受け皿のスキーマを見れば分かる。
    """
    schema = GeneratedRunbooks.model_json_schema()
    action_type = schema["$defs"]["RunbookStep"]["properties"]["action_type"]
    enums = [
        branch["enum"]
        for branch in action_type.get("anyOf", [])
        if isinstance(branch.get("enum"), list)
    ]
    assert enums, f"action_type に enum が無い: {action_type}"
    assert sorted(enums[0]) == sorted(RUNBOOK_ACTION_TYPES)


@pytest.mark.asyncio
async def test_generator_keeps_branch_tags_and_drops_unknown_ones() -> None:
    """生成器の出口で、語彙のタグが残り語彙外が落ちること（LLM 応答からの経路）。"""
    gen = AIRunbookGenerator()

    async def fake_chat(**kwargs: Any) -> Any:
        # 生成プロンプトが実際のコールへ載っていること。ここを見ないと、
        # 「条件分岐の書き方」を書いても送っていない、という結線切れが通る。
        system = kwargs["messages"][0]["content"]
        assert "条件分岐の書き方" in system
        assert "condition" in system and "branch" in system
        # 語彙が **制約デコードへ届く**こと。Literal を正本にした理由そのものなので、
        # スキーマに enum が載っているだけでなく、コールの format に載ることを見る。
        fmt = json.dumps(kwargs["format"], ensure_ascii=False)
        for tag in RUNBOOK_ACTION_TYPES:
            assert f'"{tag}"' in fmt, tag
        payload = {
            "runbooks": [
                {
                    "link_id": "item1",
                    "steps": [
                        {"order": 1, "title": "第1候補が空か判定", "action_type": "condition"},
                        {"order": 2, "title": "空なら第2候補", "action_type": "branch"},
                        {"order": 3, "title": "謎", "action_type": "fallback"},
                    ],
                }
            ]
        }
        return types.SimpleNamespace(
            message=types.SimpleNamespace(content=json.dumps(payload)),
        )

    gen._client = types.SimpleNamespace(chat=fake_chat)  # type: ignore[assignment]
    generated = await gen.generate(
        [
            {
                "link_id": "item1",
                "item_text": "診断日",
                "source_category": "ehr",
                "annotation": None,
                "location": None,
            }
        ]
    )
    assert [s.action_type for s in generated[0].steps] == ["condition", "branch", None]


async def _make_questionnaire(db_session: AsyncSession) -> Questionnaire:
    q = Questionnaire(
        fhir_id="q-runbook-branch",
        title="分岐語彙テスト票",
        status="active",
        content={
            "resourceType": "Questionnaire",
            "id": "q-runbook-branch",
            "item": [{"linkId": "item1", "text": "項目1", "type": "string"}],
        },
    )
    db_session.add(q)
    await db_session.commit()
    await db_session.refresh(q)
    return q


@pytest.mark.asyncio
async def test_saved_branch_steps_round_trip_through_the_route(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """保存ルートで condition / branch が保存され、語彙外は None で保存されること。

    正本・プロンプト・UI が揃っても、保存経路が値を落とすなら分岐の手順書は残らない。
    ルート越しに DB の中身まで見る。
    """
    q = await _make_questionnaire(db_session)
    token = create_access_token(data={"sub": str(designer_user.id), "act": RoleType.DESIGNER})
    designer_client.cookies.set(settings.jwt_cookie_name, token)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/runbook/item1",
        json={
            # **order は配列位置とずらす**（10/20/30/40）。突き合わせを「リクエストの order」で
            # 行う実装に変えても、位置と一致したフィクスチャでは違いが出ない。保存側は order を
            # 1始まりへ採番し直すので、報告の order は採番後の値になる。
            "steps": [
                {"order": 10, "title": "FHIR を照会", "action_type": "fetch"},
                {
                    "order": 20,
                    "title": "空振りか判定",
                    "description": "第1候補が空かどうかを見る",
                    "action_type": "CONDITION",
                    "data_hint": "is_empty($fhir)",
                },
                {"order": 30, "title": "空なら院内DB", "action_type": "branch"},
                {"order": 40, "title": "謎", "action_type": "その他"},
            ]
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200

    result = await db_session.execute(
        select(InputRunbook).where(
            InputRunbook.questionnaire_id == q.id, InputRunbook.link_id == "item1"
        )
    )
    rb = result.scalar_one()
    assert [s["action_type"] for s in rb.steps] == ["fetch", "condition", "branch", None]
    # 判定ステップの自由文（分岐の根拠）が失われないこと
    assert rb.steps[1]["description"] == "第1候補が空かどうかを見る"
    assert rb.steps[1]["data_hint"] == "is_empty($fhir)"

    # **落としたことを応答で返す。** UI のセレクタは語彙しか出せないので、ここに載るのは
    # API 直叩きや移行データだけだが、黙って消えると気づけない（ALCOA+ の追跡可能性）。
    body = resp.json()
    assert body["dropped_action_types"] == [{"order": 4, "given": "その他"}]
    # 採番し直した order（4）で報告される。リクエストの order（40）ではない。
    assert [s["order"] for s in body["steps"]] == [1, 2, 3, 4]
    assert [s["action_type"] for s in body["steps"]] == ["fetch", "condition", "branch", None]


@pytest.mark.asyncio
async def test_save_response_reports_no_drop_for_the_normal_path(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """語彙内だけで保存したときに「落とした」報告が出ないこと。

    落ちた報告が正常系でも出るなら、それは検出器ではなく飾りになる（画面に常時警告が
    出れば誰も読まなくなる）。空タグ（種別なし）も落としたとは呼ばない。
    """
    q = await _make_questionnaire(db_session)
    token = create_access_token(data={"sub": str(designer_user.id), "act": RoleType.DESIGNER})
    designer_client.cookies.set(settings.jwt_cookie_name, token)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/runbook/item1?confirm=true",
        json={
            "steps": [
                {"order": 1, "title": "判定", "action_type": "condition"},
                {"order": 2, "title": "分岐", "action_type": "branch"},
                {"order": 3, "title": "種別なし", "action_type": None},
                {"order": 4, "title": "キー欠落"},
                # 空文字。**「落とした」と呼ばない条件を4通り（None / キー欠落 / 空文字 /
                # 空白のみ）すべて置く。** 1つでも欠けると、判定条件を緩めた退行が通る。
                # 空白のみは検証側が strip して「タグ無し」と扱うので、報告側も揃える。
                {"order": 5, "title": "空文字タグ", "action_type": ""},
                {"order": 6, "title": "空白のみのタグ", "action_type": "   "},
            ]
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["dropped_action_types"] == []
    # 受け入れ条件「分岐を含む手順書を保存・確認できる」（confirm 昇格まで）
    assert body["status"] == "confirmed"
    result = await db_session.execute(
        select(InputRunbook).where(
            InputRunbook.questionnaire_id == q.id, InputRunbook.link_id == "item1"
        )
    )
    rb = result.scalar_one()
    assert rb.status == "confirmed"
    assert [s["action_type"] for s in rb.steps] == [
        "condition",
        "branch",
        None,
        None,
        None,
        None,
    ]


# =============================================================================
# 3. タグがステップ3（コンパイル）のプロンプトへ届く
# =============================================================================


def test_action_type_tags_reach_the_compile_prompt() -> None:
    """確認済み手順書のタグが、コンパイルの user メッセージに `[tag]` として現れること。

    ここが切れると、手順書で判定と分岐を分けて書いても DSL の guard / branch へ写らない。
    """
    msg = AIPlanCompiler()._build_user_message(
        [
            {
                "link_id": "item1",
                "item_text": "診断日",
                "source_category": "ehr",
                "annotation": "入院症例は部門システム、外来症例は電子カルテを見る",
                "candidates": [],
                "steps": [
                    {"order": 1, "title": "入院か外来かを判定", "action_type": "condition"},
                    {"order": 2, "title": "判定に応じて取得元を選ぶ", "action_type": "branch"},
                ],
            }
        ]
    )
    assert "[condition]" in msg
    assert "[branch]" in msg
    assert "入院か外来かを判定" in msg


def test_compile_prompt_maps_the_runbook_tags_to_ops() -> None:
    """コンパイルの system プロンプトが、タグから op への写し方を指示していること。

    タグ名が本文に出るだけでは足りない（それは「タグがある」という説明でしかない）。
    `[condition]` を判定 op へ、`[branch]` を guard の付与と branch へ写す、という
    対応の指示が残っていることを見る。
    """
    # **語の在否ではなく対応の方向を見る。** 「窓の中に語がある」だけの検査は、写し方を
    # 反転させた退行（`[branch]`→判定 op、`[condition]`→guard）でも通る（実測済み）。
    # 順序で書けば、どちらのタグがどちらの op に対応しているかが固定される。
    i_condition = COMPILE_SYSTEM_PROMPT.index("`[condition]` は")
    i_judge_ops = COMPILE_SYSTEM_PROMPT.index("llm_judge / compare")
    i_branch = COMPILE_SYSTEM_PROMPT.index("`[branch]` は")
    i_guard = COMPILE_SYSTEM_PROMPT.index("guard の付与と branch")
    assert i_condition < i_judge_ops < i_branch < i_guard, {
        "condition": i_condition,
        "judge_ops": i_judge_ops,
        "branch": i_branch,
        "guard": i_guard,
    }

    # `[branch]` の2つの用途を書き分けさせる。どちらも「分岐」だが写す定型が違うので、
    # 言い分けが消えると空振りフォールバックが条件付き実行として書かれる（逆も同じ）。
    # ここも「どちらの記述がどちらの定型か」を順序で固定する。
    i_empty_next = COMPILE_SYSTEM_PROMPT.index("「空なら次の候補」")
    i_fallback_form = COMPILE_SYSTEM_PROMPT.index("フォールバックの定型", i_empty_next)
    i_by_context = COMPILE_SYSTEM_PROMPT.index("「文脈でどちらか」")
    i_guarded_form = COMPILE_SYSTEM_PROMPT.index("条件付き実行の定型", i_by_context)
    assert i_empty_next < i_fallback_form < i_by_context < i_guarded_form

    # 参照方向（定型の節は指示より前にある）。ここが「下記」だと在処を間違って指す。
    assert COMPILE_SYSTEM_PROMPT.index("# フォールバック") < i_fallback_form
    assert COMPILE_SYSTEM_PROMPT.index("# 条件付き実行") < i_guarded_form
    assert "（下記）" not in COMPILE_SYSTEM_PROMPT[i_empty_next:i_guarded_form]


def test_legacy_unknown_tags_are_filtered_before_the_compile_prompt() -> None:
    """DB に残る語彙外タグが、コンパイル入力の段で None へ落ちること（自由文は残す）。

    かつては `action_type` が無検証だったので、API 経由で語彙外の値が入った行が
    残っている可能性がある。コンパイルの system プロンプトは「タグが付いている」前提で
    写し方を指示するので、未定義のタグを混ぜると指示と入力が食い違う。
    """
    filtered = _steps_with_known_tags(
        [
            {"order": 1, "title": "取得", "action_type": "fetch"},
            {"order": 2, "title": "判定", "action_type": "condition", "data_hint": "空か"},
            {"order": 3, "title": "旧タグ", "action_type": "decide", "description": "説明は残る"},
        ]
    )
    assert [s["action_type"] for s in filtered] == ["fetch", "condition", None]
    # 自由文は落とさない（タグは手掛かりであって値ではない）
    assert filtered[2]["description"] == "説明は残る"
    assert filtered[1]["data_hint"] == "空か"


def test_filtered_tags_do_not_reach_the_compile_prompt() -> None:
    """濾したタグがプロンプトに `[decide]` として現れないこと（結線の確認）。"""
    msg = AIPlanCompiler()._build_user_message(
        [
            {
                "link_id": "item1",
                "item_text": "診断日",
                "source_category": "ehr",
                "candidates": [],
                "steps": _steps_with_known_tags(
                    [{"order": 1, "title": "旧タグ", "action_type": "decide"}]
                ),
            }
        ]
    )
    assert "[decide]" not in msg
    assert "旧タグ" in msg


# 1項目あたりの出力（steps の JSON）に予約する文字数。手順書を JSON にした長さは、
# 典型的な項目で 700 字前後、ステップの多い項目では 2,000 字ほどになる（いずれも概数）。
# 900 は典型値の約1.3倍で、**最大級の項目は覆っていない**。
#
# 最大級で3項目ぶんを予約する（2,000×3）と刻み幅を1まで落とすことになり、N 項目の一括生成が
# N コールになる。そこまで払わない判断の根拠は、**溢れても検知でき、項目単位で回復できる**
# ことである（`runbook.py` の `RUNBOOK_CHUNK_SIZE` のコメント参照。空 content なら
# `AIServiceException` で特定的に失敗し、項目が落ちた場合は未生成のまま画面に空欄として
# 並んで「この項目だけ再生成」で戻せる）。**「黙って壊れないから安全」ではない**——静かな
# 方の症状を減らすために刻み幅は下げてある。ここで守るのは、その判断の前提である
# **固定部（プロンプト・スキーマ）の膨張に気づくこと**である。
_OUTPUT_RESERVE_CHARS = 900

# 入力側の最悪構成の代用。臨床的意味の自由文は、かつて同梱していたデモの確認済み
# 手順書の最長値（298 字）を下回らない長さにする（その実データはもう同梱していないので、
# **長さだけ**を固定する）。所在の自由文も同様に旧実データの長さ（45 字）以上にする。
_WORST_CASE_ANNOTATION_CHARS = 298
_WORST_CASE_ANNOTATION = (
    "感染症発生報告の拡張サマリ項目。DWH の入院歴と FHIR のコード化 Observation から取得し、"
    "DSL の純粋op（集計・比較・分岐）で論理和に統合して導出する（1=あり / 2=なし / 9=不明）。" * 4
)[:_WORST_CASE_ANNOTATION_CHARS]
_WORST_CASE_LOCATION = "DWH: PATIENT.BIRTH_DATE（10桁ゼロ詰めの患者番号 PT_ID で解決）"


def test_the_worst_case_inputs_keep_their_length() -> None:
    """予算見積もりの代用入力が、固定した長さを下回っていないこと（見積もりを痩せさせない）。"""
    assert len(_WORST_CASE_ANNOTATION) == _WORST_CASE_ANNOTATION_CHARS
    assert len(_WORST_CASE_LOCATION) >= 45


def _budget_chars(chunk_size: int) -> int:
    """`chunk_size` 項目を1コールに詰めたときの文字数見積もり（出力予約を含む）。"""
    longest_annotation = _WORST_CASE_ANNOTATION
    items = [
        {
            "link_id": f"item-{i:04d}",
            "item_text": "項目名" * 10,
            "source_category": "external_db",
            "annotation": longest_annotation,
            "location": _WORST_CASE_LOCATION * 3,
        }
        for i in range(chunk_size)
    ]
    user_msg = AIRunbookGenerator()._build_user_message(items)
    schema = json.dumps(GeneratedRunbooks.model_json_schema(), ensure_ascii=False)
    return (
        len(RUNBOOK_SYSTEM_PROMPT)
        + len(schema)
        + len(user_msg)
        + _OUTPUT_RESERVE_CHARS * chunk_size
    )


# 手順書生成の実プロンプトで想定する thinking の最大長（thinking の長いモデルを
# 刻み幅を変えて回したときの最大を基にした値）。
# **臨床側の値は使えない。** 経路が違えば thinking の長さも桁で違う。
_THINKING_OBSERVED_MAX_CHARS = 21026


def test_the_chunk_size_leaves_room_for_thinking() -> None:
    """刻み幅が**thinking の余地を残して**予算に収まること。

    以前は「予算に収まる**最大値**」を縛っていた（`_budget_chars(n+1) > num_ctx`）。
    予算を 32768 へ上げるとその最大値は 20 になり、**どの項目が同じコールに乗るかが
    総入れ替え**になる。刻み幅は 3 のまま据え置くので、上限の縛りを外す。

    **下げる退行が見えなくなるわけではない。** 刻み幅 3 は二重に固定してある。

    - `tests/ai_input/test_runbook.py` の `assert RUNBOOK_CHUNK_SIZE == 3`
    - 同ファイルの `assert counts == [3, 3, 3, 1]`（10項目の分割の逐語期待値）

    代わりにここが縛るのは「**thinking が入る余地があるか**」である。`think=True` は
    固定されており thinking は必ず出るので、入力＋出力予約だけで予算を使い切る
    構成は成立しない。8192 のとき余地は約 1,021 字しかなく、thinking の長いモデルでは
    空 content か壊れた JSON が返る。
    """
    num_ctx = int(LARGE_CONTEXT_OPTIONS["num_ctx"])
    used = _budget_chars(RUNBOOK_CHUNK_SIZE)
    headroom = num_ctx - used
    assert used <= num_ctx, {"used": used, "num_ctx": num_ctx}
    # **想定する最大長そのものを下限にする。** 臨床側は「想定の2倍」を要求しているが、
    # 同じ 2 倍をここへ当てると必要な num_ctx は 48,373 になり 32768 では通らない。
    # **実際の余裕は 1.24 倍である**（32,768 − 7,171 = 25,597 に対し想定 21,026）。
    # 「2倍の安全率がある」とは書かない——無い余裕を主張すると、次に thinking が伸びた
    # ときに理由の分からない失敗として現れる。
    assert headroom >= _THINKING_OBSERVED_MAX_CHARS, {
        "headroom": headroom,
        "thinking_observed_max": _THINKING_OBSERVED_MAX_CHARS,
        "ratio": round(headroom / _THINKING_OBSERVED_MAX_CHARS, 2),
    }


def test_generation_prompt_fits_the_num_ctx_budget() -> None:
    """**条件分岐の書き方を足したプロンプト**が num_ctx の予算に収まること。

    条件分岐の書き方を足して system プロンプトは 870 → 1599 文字（約1.8倍）になった。

    **単位は文字であってトークンではない。** 1文字＝1トークンは上界ではなく（BPE の byte
    fallback で語彙外の漢字は UTF-8 の3バイト＝3トークンに分解されうる。逆に頻出語は複数
    文字で1トークン）、この入力は非 ASCII が大半で UTF-8 バイト数は文字数の約2倍になる。
    したがってこれは「絶対に溢れない」証明ではなく、**固定部やチャンクサイズを増やしたときに
    気づくための回帰テスト**である。

    入力側の長さは臨床的意味とデータ所在の自由文で決まり上限が無いので、旧デモの実データの
    最長値と同じ長さの文（`_WORST_CASE_ANNOTATION`）を最悪構成の代用にする。出力側の予約は `_OUTPUT_RESERVE_CHARS`（最悪値ではない。
    理由は同定数のコメント）。
    """
    longest_annotation = _WORST_CASE_ANNOTATION
    items = [
        {
            "link_id": f"item-{i:04d}",
            "item_text": "項目名" * 10,
            "source_category": "external_db",
            "annotation": longest_annotation,
            "location": _WORST_CASE_LOCATION * 3,
        }
        for i in range(RUNBOOK_CHUNK_SIZE)
    ]
    user_msg = AIRunbookGenerator()._build_user_message(items)
    schema = json.dumps(GeneratedRunbooks.model_json_schema(), ensure_ascii=False)
    # 出力ぶん（各項目の steps）を明示的に予約する
    output_reserve = _OUTPUT_RESERVE_CHARS * RUNBOOK_CHUNK_SIZE
    total = len(RUNBOOK_SYSTEM_PROMPT) + len(schema) + len(user_msg) + output_reserve
    # **`runbook` が実際に渡す options を読む**。`DEFAULT_OPTIONS` を読むと、
    # 本番だけ 32768 へ移してもテストは 8192 を測り続ける静かなドリフトになる。
    num_ctx = int(LARGE_CONTEXT_OPTIONS["num_ctx"])
    assert total <= num_ctx, {
        "system": len(RUNBOOK_SYSTEM_PROMPT),
        "schema": len(schema),
        "user": len(user_msg),
        "output_reserve": output_reserve,
        "total": total,
        "num_ctx": num_ctx,
    }


@pytest.mark.asyncio
async def test_regenerated_branch_steps_come_back_through_the_route(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """再生成ルートの応答（画面が読む形）にタグが残ること。

    保存できても読み出しで落ちるなら画面の選択肢は空になる。応答は `_serialize_step` を
    通るので、整形で action_type を落とす退行をここで捕まえる。
    """
    monkeypatch.setattr(settings, "enable_ai_drafting", True)
    q = await _make_questionnaire(db_session)
    token = create_access_token(data={"sub": str(designer_user.id), "act": RoleType.DESIGNER})
    designer_client.cookies.set(settings.jwt_cookie_name, token)

    class _FakeGenerator:
        async def generate(self, items: list[dict[str, Any]], **kwargs: Any) -> Any:
            from kawasemi.ai_input.schemas import GeneratedRunbookItem

            return [
                GeneratedRunbookItem(
                    link_id="item1",
                    steps=[
                        RunbookStep(order=1, title="空振りか判定", action_type="condition"),
                        RunbookStep(order=2, title="空なら第2候補", action_type="branch"),
                    ],
                )
            ]

    monkeypatch.setattr("kawasemi.ai_input.routes.AIRunbookGenerator", _FakeGenerator)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/runbook/item1/regenerate",
        json={},
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200, resp.text
    assert [s["action_type"] for s in resp.json()["steps"]] == ["condition", "branch"]


@pytest.mark.asyncio
async def test_the_dropped_action_types_are_recorded_on_the_runbook_row(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """落とした種別タグが**保存後も追える**こと。

    応答 JSON だけに載せると、読めるのはその場で応答を見た呼び出し元に限られる。
    保存済みの `steps` には「何を送ってきたか」が残らない（検証が `None` にする）ので、
    後から追うには行に記録が要る。

    記録先を手順書の行にしたのは、これが**人の保存**であって LLM 呼び出しではないから
    である（`ai_input_design_llm_calls` の行は LLM 呼び出しの記録である）。
    判断: 画面には出さず来歴に残す。
    """
    q = await _make_questionnaire(db_session)
    token = create_access_token(data={"sub": str(designer_user.id), "act": RoleType.DESIGNER})
    designer_client.cookies.set(settings.jwt_cookie_name, token)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/runbook/item1",
        json={
            "steps": [
                {"order": 1, "title": "FHIR を照会", "action_type": "fetch"},
                {"order": 2, "title": "謎", "action_type": "その他"},
            ]
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200

    result = await db_session.execute(
        select(InputRunbook).where(
            InputRunbook.questionnaire_id == q.id, InputRunbook.link_id == "item1"
        )
    )
    rb = result.scalar_one()
    assert rb.dropped_action_types == [{"order": 2, "given": "その他"}]
    # 応答と行の内容が食い違わないこと（人が読むのはその両方である）
    assert resp.json()["dropped_action_types"] == rb.dropped_action_types


@pytest.mark.asyncio
async def test_a_clean_save_records_nothing(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """語彙内だけの保存では来歴にも何も残らないこと。

    空配列を残すと、「落ちたタグがある手順書」を列で絞れなくなる（`is not null` が
    全件を返す）。正常系で発火する記録は検出器ではなく飾りである。
    """
    q = await _make_questionnaire(db_session)
    token = create_access_token(data={"sub": str(designer_user.id), "act": RoleType.DESIGNER})
    designer_client.cookies.set(settings.jwt_cookie_name, token)

    resp = await designer_client.post(
        f"/ai-input/questionnaires/{q.id}/runbook/item1",
        json={
            "steps": [
                {"order": 1, "title": "判定", "action_type": "condition"},
                {"order": 2, "title": "種別なし", "action_type": None},
            ]
        },
        headers={"X-CSRF-Token": csrf_token},
    )
    assert resp.status_code == 200

    result = await db_session.execute(
        select(InputRunbook).where(
            InputRunbook.questionnaire_id == q.id, InputRunbook.link_id == "item1"
        )
    )
    assert result.scalar_one().dropped_action_types is None


@pytest.mark.asyncio
async def test_a_later_clean_save_clears_the_record(
    designer_client: AsyncClient,
    designer_user: User,
    db_session: AsyncSession,
    csrf_token: str,
) -> None:
    """語彙外タグを直した保存で、記録が消えること。

    残したままだと、**いま語彙外タグを持っていない手順書が持っているように読める**。
    来歴は「この保存で何が落ちたか」であって、過去の保存の履歴ではない。
    """
    q = await _make_questionnaire(db_session)
    token = create_access_token(data={"sub": str(designer_user.id), "act": RoleType.DESIGNER})
    designer_client.cookies.set(settings.jwt_cookie_name, token)
    url = f"/ai-input/questionnaires/{q.id}/runbook/item1"

    await designer_client.post(
        url,
        json={"steps": [{"order": 1, "title": "謎", "action_type": "その他"}]},
        headers={"X-CSRF-Token": csrf_token},
    )
    await designer_client.post(
        url,
        json={"steps": [{"order": 1, "title": "照会", "action_type": "fetch"}]},
        headers={"X-CSRF-Token": csrf_token},
    )

    result = await db_session.execute(
        select(InputRunbook).where(
            InputRunbook.questionnaire_id == q.id, InputRunbook.link_id == "item1"
        )
    )
    assert result.scalar_one().dropped_action_types is None


def test_the_runbook_prompt_carries_the_alternate_locations() -> None:
    """代替候補が手順書生成のプロンプトへ優先順つきで載ること。

    載らないと、所在が「FHIR 優先・無ければ院内DB」でも手順書は第1候補しか書けない
    ——`RUNBOOK_SYSTEM_PROMPT` が条件分岐の書き方として指示している形を、入力側が
    表現できない。
    """
    from kawasemi.ai_input.runbook import AIRunbookGenerator

    message = AIRunbookGenerator._build_user_message(
        object.__new__(AIRunbookGenerator),
        [
            {
                "link_id": "item1",
                "item_text": "主病名",
                "source_category": "fhir",
                "location": "FHIR: Condition.code",
                "alternate_locations": ["DWH: hospitalization_disease.disease_name"],
            }
        ],
    )

    assert "代替のデータ所在" in message
    assert "第2候補: DWH: hospitalization_disease.disease_name" in message


def test_the_prompt_is_byte_identical_without_alternates() -> None:
    """代替候補が無い項目のプロンプトが1バイトも変わらないこと。

    手順書は**測定済みの成果物**である。候補が1件の項目
    （現状の全データ）でプロンプトが変われば、再生成したときに前の測定と比べられなくなる。
    `alternate_locations` のキーごと入れない設計にしているのはこのためで、
    空配列を渡しても同じであることまで見る。
    """
    from kawasemi.ai_input.runbook import AIRunbookGenerator

    build = AIRunbookGenerator._build_user_message
    base = {
        "link_id": "item1",
        "item_text": "主病名",
        "source_category": "fhir",
        "annotation": "確定診断名",
        "location": "FHIR: Condition.code",
    }
    gen = object.__new__(AIRunbookGenerator)

    without_key = build(gen, [base])
    with_empty = build(gen, [{**base, "alternate_locations": []}])

    assert without_key == with_empty
    assert "代替のデータ所在" not in without_key
