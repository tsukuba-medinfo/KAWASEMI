"""構造化出力のスキーマと検証の強さを一致させる契約

**スキーマが緩いと生成物が壊れ、厳しいと生成できない program ができる。** どちらも
「テストは緑なのに保証が消えている」形で起きるので、機械で止める。

背景。DSL 生成で検証を通る項目が半数に満たなかった。
`DSLStep` は37フィールドすべて省略可で、`_REQUIRED_PLAN_KEYS` は3つだけだったため、
**`compare` が `left` 無しで通っていた**。後段の `_repair_op_args` は取得系しか救えない
（索引の payload から焼き込める）ので、`compare` / `branch` / `transform` には救済経路が無い。
プロンプトには `{op,id,left,cmp,right}` と書いてあり、それでも従わなかった。
"""

from __future__ import annotations

import json

import pytest

from kawasemi.ai_input.dsl import (
    _REQUIRED_FIELDS,
    _SCHEMA_VALUE_FIELDS,
    _VALUE_FIELDS,
    COMPARE_OPS,
    UNARY_COMPARE_OPS,
    step_schema,
    validate_program,
)
from kawasemi.ai_input.models import OpType


def _variants() -> list[dict]:
    return step_schema()["oneOf"]


def _determined() -> list[dict]:
    return [v for v in _variants() if v["properties"]["needs_input"] == {"const": False}]


def _pending() -> list[dict]:
    return [v for v in _variants() if v["properties"]["needs_input"] == {"const": True}]


# --------------------------------------------------------------------------- #
# スキーマと検証の強さが一致すること（ずれを機械で止める）
# --------------------------------------------------------------------------- #


def test_the_schema_requires_exactly_what_the_validator_requires():
    """`step_schema()` の必須が `_REQUIRED_FIELDS` と一致すること。

    **どちらかを触ったらここが落ちる。** 手でスキーマを書くと必ずずれるので、
    `step_schema()` は `_REQUIRED_FIELDS` から導いている。その導出が保たれることを固定する。
    """
    by_op: dict[str, list[set[str]]] = {}
    for v in _determined():
        op = v["properties"]["op"]["const"]
        by_op.setdefault(op, []).append(set(v["required"]) - {"op", "id", "needs_input"})

    for op in OpType.ALL:
        assert op in by_op, f"{op} の variant が無い"
        expected = set(_REQUIRED_FIELDS[op])
        if op == OpType.COMPARE:
            # 単項（right 免除）と二項（right 必須）の2つに割れている
            assert sorted(by_op[op], key=len) == [expected - {"right"}, expected], (
                f"compare の割り方が検証と食い違う: {by_op[op]}"
            )
            continue
        assert by_op[op] == [expected], f"{op}: スキーマ={by_op[op]} 検証={expected}"


def test_branch_then_is_not_required_by_the_schema():
    """検証より厳しくしない（`_REQUIRED_FIELDS[branch]` は `when` だけ）。

    厳しくすると「検証は通るのに生成できない program」ができる。
    """
    branch = next(v for v in _determined() if v["properties"]["op"]["const"] == OpType.BRANCH)
    assert "then" not in branch["required"]
    assert "else_branch" not in branch["required"]


# --------------------------------------------------------------------------- #
# op の語彙
# --------------------------------------------------------------------------- #


def test_every_variant_pins_its_op_as_a_constant():
    """`op` が `const` であること。

    **これは閉じるべき穴である。** 現行は `DSLStep.op` が `str` で
    語彙が構造化出力へ載らず、実測でモデルが `assert` や `create` を
    出した。`validate_program` が必須引数で先に落としていたので見えていなかった。
    A の設計を変えたときにこの保証が黙って消えないよう、ここで固定する。
    """
    for v in _variants():
        op = v["properties"]["op"]
        assert set(op) == {"const"}, f"op が const でない: {op}"
        assert op["const"] in OpType.ALL, f"語彙外の op: {op['const']}"
    # 12 op（compare は2つ）× needs_input の2値
    assert len(_variants()) == (len(OpType.ALL) + 1) * 2


def test_an_out_of_vocabulary_op_is_rejected_by_the_validator():
    """語彙外の op は検証でも落ちること（スキーマだけに頼らない）。"""
    for bogus in ("assert", "create", "return"):
        errors = validate_program(
            {"params": [], "steps": [{"op": bogus, "id": "a"}], "return": "$a"}
        )
        assert any("op" in e for e in errors), f"{bogus} が落ちていない: {errors}"


# --------------------------------------------------------------------------- #
# 決定不能の申告を潰さないこと
# --------------------------------------------------------------------------- #


def test_a_pending_step_does_not_require_the_op_arguments():
    """`needs_input=true` では op 固有引数を必須にしないこと。

    **必須にすると、モデルは「決められない」と申告する代わりに引数を捏造する。**
    実際の生成では `needs_input=true` が多くの項目に立っており、
    理由の大半は「索引に接続の種別が無い」だった。これは決定不能を正直に申告している
    記録であって、埋めさせると手順書から DSL を作れた割合が実態より良く出る。
    """
    for v in _pending():
        assert set(v["required"]) == {"op", "id", "needs_input"}, v["required"]
    assert len(_pending()) == len(_determined())


# --------------------------------------------------------------------------- #
# 形の取り違え（値の引数にオブジェクトを入れる）
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("field", ["left", "right", "when", "then", "else_branch", "input"])
def test_value_arguments_cannot_hold_an_object(field: str):
    """値を取る引数がスカラ（または配列）に閉じていること。

    `DSLStep` ではこれらが `Any`（JSON スキーマ上は `{}`）なので任意のオブジェクトが入る。
    実測でモデルは `left` に `branch` の step を丸ごと入れ、`when` に
    `{"not_empty": "$sql_basic"}` を入れた。**`validate_program` はこれを弾かない**ので、
    スキーマの型がこの穴の唯一の関門である。
    """
    seen = False
    for v in _variants():
        prop = v["properties"].get(field)
        if prop is None:
            continue
        seen = True
        blob = json.dumps(prop)
        assert prop != {}, f"{field} が任意の JSON になっている"
        assert '"object"' not in blob, f"{field} がオブジェクトを許している: {blob}"
    assert seen, f"{field} を持つ variant が無い"


def test_the_schema_value_fields_do_not_shadow_the_validator_ones():
    """スキーマ側と検証側の定数を混同しないこと。

    **実際にやった間違いである。** `dsl.py` の末尾へ `_VALUE_FIELDS` と同名で定義したため、
    後の定義が前を上書きし、**`right` / `source` / `match` / `guard` の前方参照検査が
    黙って外れていた**。全テストは緑のままだった（保証だけが消える型）。
    """
    assert _VALUE_FIELDS != _SCHEMA_VALUE_FIELDS
    # 検証側は9件すべてを見る（減らすと参照健全性の検査が穴になる）
    assert set(_VALUE_FIELDS) == {
        "left",
        "right",
        "when",
        "then",
        "else_branch",
        "input",
        "source",
        "match",
        "guard",
    }


# --------------------------------------------------------------------------- #
# `branch.when` は判定の結果を指すこと（測定器の強化）
# --------------------------------------------------------------------------- #


def _branch_program(when: object, *, needs_input: bool = False) -> dict:
    step: dict = {"op": OpType.BRANCH, "id": "b", "then": "$a"}
    if needs_input:
        step["needs_input"] = True
    else:
        step["when"] = when
    return {
        "params": [],
        "steps": [{"op": OpType.SQL_EXTRACT, "id": "a", "sql": "SELECT 1"}, step],
        "return": "$b",
    }


def test_a_meaningless_literal_in_when_is_rejected():
    """`when: "not_empty"` のような意味の無いリテラルを「変換成功」に数えないこと。

    **`validate_program` は「手順書から DSL を作れた割合」を数える測定器そのものである。** 必須引数の欠落は
    落ちるので見えるが、**通ってしまうものは見えない**。実測で、構造化出力の型を
    「参照かリテラル」まで閉じるとモデルはこの形へ逃げた（`branch` は `when` しか
    必須でないため）。
    """
    errors = validate_program(_branch_program("not_empty"))
    assert any("when" in e for e in errors), errors


@pytest.mark.parametrize("when", ["$a", True, False])
def test_a_reference_or_a_boolean_in_when_is_accepted(when: object):
    """`$id` 参照と真偽は正当（`guard` と同じ規則）。"""
    errors = [e for e in validate_program(_branch_program(when)) if "when" in e]
    assert errors == [], errors


def test_when_is_exempt_while_the_step_is_pending():
    """`needs_input=true` のときは免除する（まだ書いていないことは許す、の一貫）。"""
    errors = [e for e in validate_program(_branch_program(None, needs_input=True)) if "when" in e]
    assert errors == []


def test_the_unary_compare_variant_cannot_carry_a_right():
    """単項の `compare` に `right` を書けないこと（`validate_program` の検査と揃える）。

    `validate_program` は単項に `right` があると弾く（「書いたものが無視される」の通知）。
    スキーマ側でも書けないようにしておかないと、モデルが書いて検証で落ちる。
    """
    unary = [
        v
        for v in _determined()
        if v["properties"]["op"]["const"] == OpType.COMPARE
        and set(v["properties"]["cmp"]["enum"]) == set(UNARY_COMPARE_OPS)
    ]
    assert len(unary) == 1
    assert "right" not in unary[0]["properties"]
    assert unary[0]["additionalProperties"] is False
    # 二項側は right を持つ
    binary = [
        v
        for v in _determined()
        if v["properties"]["op"]["const"] == OpType.COMPARE
        and set(v["properties"]["cmp"]["enum"]) != set(UNARY_COMPARE_OPS)
    ]
    assert len(binary) == 1
    assert "right" in binary[0]["properties"]
    assert set(binary[0]["properties"]["cmp"]["enum"]) == set(COMPARE_OPS) - set(UNARY_COMPARE_OPS)
