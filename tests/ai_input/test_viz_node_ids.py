r"""取得フロー図のノードIDが step ごとに一意であることの検証

`"n_" + re.sub(r"\W+", "_", step_id or "x")` だけでは、**別の step が同じノードIDへ
潰れる**。`a-b` と `a.b` はどちらも `n_a_b` になり、id を書かない step はどれも
`n_x` になる。mermaid はそれをエラーにせず**1つのノードとして描く**ので、図の上で
2つの step が融合する。

プラン編集画面は step id を人が手入力する場所であり、設計者が見て確定する図
（監査図を実データフローに忠実に保つために作った図）が、
実際のプログラムと違う構造を見せることになる。
"""

from __future__ import annotations

import re

from kawasemi.ai_input.viz import program_to_mermaid

# ノード宣言行（`  n_xxx["..."]` / `  n_xxx(["..."])` / `  n_xxx{"..."}`）の先頭ID
_NODE_DECL = re.compile(r"^  (n_\w+)[\[({]")


def _declared_nodes(mermaid: str) -> list[str]:
    return [m.group(1) for line in mermaid.splitlines() if (m := _NODE_DECL.match(line))]


def test_step_ids_that_sanitize_to_the_same_name_get_separate_nodes() -> None:
    """`a-b` と `a.b` は別ノードになる（どちらも `n_a_b` へ潰れていた）。"""
    program = {
        "steps": [
            {"op": "file_extract", "id": "a-b", "path_pattern": "x.txt"},
            {"op": "file_extract", "id": "a.b", "path_pattern": "y.txt"},
        ],
        "return": "$a-b",
    }
    nodes = _declared_nodes(program_to_mermaid(program))
    assert len(nodes) == 2, nodes
    assert len(set(nodes)) == 2, f"2つの step が1ノードへ融合している: {nodes}"


def test_steps_without_an_id_get_separate_nodes() -> None:
    """id を書かない step が2つあっても別ノードになる（どちらも `n_x` へ潰れていた）。"""
    program = {
        "steps": [
            {"op": "file_extract", "path_pattern": "x.txt"},
            {"op": "file_extract", "path_pattern": "y.txt"},
        ]
    }
    nodes = _declared_nodes(program_to_mermaid(program))
    assert len(nodes) == 2, nodes
    assert len(set(nodes)) == 2, f"id 無しの step が1ノードへ融合している: {nodes}"


def test_the_node_count_matches_the_step_count() -> None:
    """衝突する形を混ぜても、ノード数が step 数と一致する。"""
    steps = [
        {"op": "file_extract", "id": "a-b", "path_pattern": "1.txt"},
        {"op": "file_extract", "id": "a.b", "path_pattern": "2.txt"},
        {"op": "file_extract", "id": "a b", "path_pattern": "3.txt"},
        {"op": "file_extract", "path_pattern": "4.txt"},
        {"op": "file_extract", "id": "", "path_pattern": "5.txt"},
        {"op": "file_extract", "id": "plain", "path_pattern": "6.txt"},
    ]
    nodes = _declared_nodes(program_to_mermaid({"steps": steps}))
    assert len(nodes) == len(steps), nodes
    assert len(set(nodes)) == len(steps), f"融合しているノードがある: {nodes}"


def test_ids_are_unchanged_when_nothing_collides() -> None:
    """**衝突しないプログラムの図は従来と同じ**。

    連番の接尾辞が無条件に付くと、既存の図・テスト・画面の見た目が全部変わる。
    """
    program = {
        "steps": [
            {"op": "external_db_extract", "id": "rows", "sql": "SELECT 1", "multi": True},
            {"op": "aggregate", "id": "n", "source": "$rows", "func": "count"},
        ],
        "return": "$n",
    }
    mermaid = program_to_mermaid(program)
    assert _declared_nodes(mermaid) == ["n_rows", "n_n"]
    assert "n_rows --> n_n" in mermaid
    assert "n_n --> result" in mermaid


def test_a_reference_points_at_the_first_step_that_declares_the_id() -> None:
    """同じ id の step が2つあるとき、`$id` 参照は先頭の step へ向く。

    参照先が一意に決まらない入力なので、**どちらか一方に決める**こと自体を固定する
    （2つのノードへ同時に辺を引いたり、後から宣言した側へ黙って移ったりしない）。
    """
    program = {
        "steps": [
            {"op": "file_extract", "id": "dup", "path_pattern": "1.txt"},
            {"op": "file_extract", "id": "dup", "path_pattern": "2.txt"},
            {"op": "transform", "id": "t", "input": "$dup", "expr": "x"},
        ],
        "return": "$t",
    }
    mermaid = program_to_mermaid(program)
    nodes = _declared_nodes(mermaid)
    assert len(set(nodes)) == 3, nodes
    first, second = nodes[0], nodes[1]
    assert f"{first} --> n_t" in mermaid
    assert f"{second} --> n_t" not in mermaid


def test_a_dangling_reference_does_not_land_on_another_step() -> None:
    """どの step も宣言していない id への参照が、既存ノードへ紛れ込まない。

    `a.b` を参照しているが宣言されているのは `a-b` だけ、という下書きで、
    正規化後の名前が一致するために**別の step へ辺が伸びてしまう**のを防ぐ。
    """
    program = {
        "steps": [
            {"op": "file_extract", "id": "a-b", "path_pattern": "1.txt"},
            {"op": "transform", "id": "t", "input": "$a.b", "expr": "x"},
        ],
        "return": "$t",
    }
    mermaid = program_to_mermaid(program)
    # `a-b` のノードは `n_a_b`。そこへ辺が伸びていたら、宣言していない id の参照が
    # 別の step に化けている
    assert "n_a_b --> n_t" not in mermaid, mermaid
    # 宣言されていない参照は、既存ノードとは別の（ラベル無しの）ノードへ向く
    assert re.search(r"^  n_a_b__\d+ --> n_t$", mermaid, re.MULTILINE), mermaid
