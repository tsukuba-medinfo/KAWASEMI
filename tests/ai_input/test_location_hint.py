"""所在検索のクエリ拡張と型加点のユニットテスト

測るのは3つ。

1. 型の照合が、索引側の**表記ゆれ**（`CHAR(8)` と `CHAR(08)`）を吸収すること。
2. 生成した語がクエリ文へ入り、型が並べ替えの加点になること（**結線**）。
3. 生成が失敗したときに**施策抜きで縮退する**こと（所在提案を止めない）。

3 が最も重要である。ヒントは検索を良くするための施策であって、所在提案の前提条件では
ない。生成が落ちたときに提案ごと落ちると、施策を入れたことで測定が取れなくなる。
"""

from __future__ import annotations

import json
import types

import pytest

from kawasemi.ai_input.location_hint import (
    EMPTY_HINT,
    LocationHint,
    generate_hints,
    normalize_type,
    type_matches,
)
from kawasemi.ai_input.resource_drafter import _query_text, _rerank_by_type
from kawasemi.ai_input.resource_index.schema import Kind, ResourceHit, ResourceUnit


def _hit(cand_id: int, score: float, type_raw: str | None) -> ResourceHit:
    return ResourceHit(
        cand_id=cand_id,
        score=score,
        unit=ResourceUnit(
            unit_id=f"db:T.C{cand_id}",
            kind=Kind.DB,
            label=f"列{cand_id}",
            search_text="x",
            payload={"physical_name": f"C{cand_id}", "type_raw": type_raw},
        ),
    )


# --------------------------------------------------------------------------- #
# 型の正規化と照合
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("CHAR(8)", ("CHAR", 8)),
        # **索引は同じ型を違う字面で持つ**（`CHAR(8)` と `CHAR(08)` が混在しうる）。
        # 文字列一致で照合すると、ゼロ埋めのほうに一切当たらない。
        ("CHAR(08)", ("CHAR", 8)),
        ("char (8)", ("CHAR", 8)),
        ("VARCHAR", ("VARCHAR", None)),
        # 小数点以下つきは整数部の桁だけを見る（桁の意味が文字数ではなくなるため）
        ("NUMERIC(06,02)", ("NUMERIC", 6)),
        ("", None),
        (None, None),
        ("なんだかわからない型", None),
    ],
)
def test_type_normalization_absorbs_the_index_spelling(raw, expected):
    assert normalize_type(raw) == expected


def test_type_match_ignores_the_length_when_either_side_omits_it():
    """桁は片方が省略されていれば無視する。

    桁まで一致を要求すると、索引側が桁を持たない列（桁なしの `VARCHAR` など）に一切
    当たらなくなる。「型は合っているが桁が書かれていない」を落とさない。
    """
    assert type_matches(LocationHint(expected_types=("CHAR(8)",)), "CHAR(08)")
    assert type_matches(LocationHint(expected_types=("CHAR",)), "CHAR(8)")
    assert type_matches(LocationHint(expected_types=("VARCHAR",)), "VARCHAR")
    # 基本型が違えば当たらない
    assert not type_matches(LocationHint(expected_types=("CHAR(8)",)), "NUMERIC(8)")
    # 桁が両方あって違えば当たらない
    assert not type_matches(LocationHint(expected_types=("CHAR(8)",)), "CHAR(1)")
    # ヒントが無ければ当たらない（加点は起きない）
    assert not type_matches(EMPTY_HINT, "CHAR(8)")


# --------------------------------------------------------------------------- #
# クエリ拡張と加点（結線）
# --------------------------------------------------------------------------- #


def test_the_generated_terms_go_into_the_query_after_the_item_itself():
    item = {"item_text": "感染症の発症日", "annotation": "症状が最初に出現した日"}
    hint = LocationHint(terms=("経過表", "ONSET_DATE"))
    q = _query_text(item, hint)
    assert q == "感染症の発症日 症状が最初に出現した日 経過表 ONSET_DATE"
    # ヒントが無ければ従来と同じ文字列（施策抜きで縮退する）
    assert _query_text(item, EMPTY_HINT) == "感染症の発症日 症状が最初に出現した日"


def test_a_matching_type_lifts_a_candidate_above_a_slightly_better_cosine():
    """型が合う候補が、わずかに cosine の高い候補を追い越すこと。

    **スコアは実際の索引で観測される程度の差を使う**（`top_k=40`）。プール40件は狭い幅に
    収まり、隣接順位の差は千分の1程度なので、0.02 のような大きな差で測ると加点の強さを
    検証できない。
    """
    pool = [_hit(0, 0.581, "NUMERIC"), _hit(1, 0.58, "CHAR(08)")]
    hint = LocationHint(expected_types=("CHAR(8)",))
    out = _rerank_by_type(pool, hint, top_k=2)
    assert [h.unit.unit_id for h in out] == ["db:T.C1", "db:T.C0"]
    # **score は素の cosine のまま**（★の入力なので加点で動かさない）
    assert out[0].score == 0.58
    # **cand_id は振り直す**（0 始まりの連番という契約を保つ）
    assert [h.cand_id for h in out] == [0, 1]


def test_the_boost_does_not_reorder_the_whole_pool():
    """加点がプール全体を並べ替えないこと（**この版で最初に間違えた点**）。

    最初に置いた `_TYPE_BOOST=0.05` はプール幅（0.05 を大きく下回る）を超えるので、型が一致した
    候補が一致しなかった候補を全部飛び越えていた（ほぼ全項目で40位から8枠へ届く）。
    それは加点ではなくプール内のハードフィルタであり、「外したときに正しい列が到達不能に
    なるのを避けるため加点にする」という方針と矛盾する。

    プール幅ぶん離れた候補を型だけで引き上げないことを測る。
    """
    # 1位 0.62（型が合わない）と、プール最下位に近い 0.58（型が合う）。加点 `_TYPE_BOOST`
    # よりずっと広い 0.04 の開きがある。
    pool = [_hit(0, 0.62, "NUMERIC"), _hit(1, 0.58, "CHAR(08)")]
    out = _rerank_by_type(pool, LocationHint(expected_types=("CHAR(8)",)), top_k=2)
    assert [h.unit.unit_id for h in out] == ["db:T.C0", "db:T.C1"]


def test_without_a_type_hint_the_index_order_is_kept():
    pool = [_hit(0, 0.581, "NUMERIC"), _hit(1, 0.58, "CHAR(08)")]
    out = _rerank_by_type(pool, EMPTY_HINT, top_k=2)
    assert [h.unit.unit_id for h in out] == ["db:T.C0", "db:T.C1"]


def test_the_pool_is_narrowed_to_top_k_after_the_boost():
    pool = [_hit(i, 0.62 - i * 0.0006, "VARCHAR") for i in range(40)]
    out = _rerank_by_type(pool, EMPTY_HINT, top_k=8)
    assert len(out) == 8
    assert [h.cand_id for h in out] == list(range(8))


# --------------------------------------------------------------------------- #
# 縮退
# --------------------------------------------------------------------------- #


def _client(content: str):
    async def chat(**kwargs):
        return types.SimpleNamespace(message=types.SimpleNamespace(content=content))

    return types.SimpleNamespace(chat=chat)


@pytest.mark.asyncio
async def test_hints_are_parsed_and_capped():
    payload = {
        "hints": [
            {
                "link_id": "q1",
                "terms": [f"t{i}" for i in range(12)],
                "expected_types": ["CHAR(8)", "NUMERIC", "VARCHAR", "INT"],
            }
        ]
    }
    res = await generate_hints(_client(json.dumps(payload)), "m", [{"link_id": "q1"}])
    assert res.failed is False
    assert len(res.of("q1").terms) == 8, "語の数に上限がある（元の項目の意味を薄めない）"
    assert len(res.of("q1").expected_types) == 3


@pytest.mark.asyncio
async def test_a_failed_generation_degrades_instead_of_raising():
    """生成が壊れても例外を投げない（所在提案そのものを止めない）。

    ヒントは検索を良くするための施策であって、提案の前提条件ではない。ここで投げると
    施策を入れたことで測定が取れなくなる。
    """
    res = await generate_hints(_client("これはJSONではない"), "m", [{"link_id": "q1"}])
    assert res.failed is True
    assert res.of("q1") == EMPTY_HINT


@pytest.mark.asyncio
async def test_no_items_makes_no_call():
    called: list = []

    async def chat(**kwargs):
        called.append(kwargs)
        raise AssertionError("呼ばれてはいけない")

    res = await generate_hints(types.SimpleNamespace(chat=chat), "m", [])
    assert res.hints == {} and not called


@pytest.mark.asyncio
async def test_empty_hints_are_not_counted_as_effective():
    """**空のヒントを「効いた」と数えないこと**。

    プロンプトは「思いつかなければ空配列でよい」と言っているので、全項目に空を返す
    モデルがありうる。応答が返った項目を数えると、検索が素の版と1バイトも変わらないのに
    「全項目でヒントあり」と記録され、**施策抜きで走ったことを見逃す検出器になる**
    """
    payload = {
        "hints": [
            {"link_id": "q1", "terms": [], "expected_types": []},
            {"link_id": "q2", "terms": ["病理検査"], "expected_types": []},
            {"link_id": "q3", "terms": [], "expected_types": ["CHAR(8)"]},
        ]
    }
    items = [{"link_id": f"q{i}"} for i in (1, 2, 3)]
    res = await generate_hints(_client(json.dumps(payload)), "m", items)
    assert len(res.hints) == 3, "応答は3件返っている"
    assert res.effective_count == 2, "検索を変えたのは2件だけ"


@pytest.mark.asyncio
async def test_blank_terms_do_not_consume_the_cap():
    """空白の語が上限の枠を食わないこと。

    先に切ってから空白を除くと、モデルが空文字を混ぜたときに使える語が落ち、
    固定した上限（`_MAX_TERMS`）が実効値と一致しなくなる。
    """
    terms = ["", " ", "病理検査", "組織診", "細胞診", "  ", "内視鏡", "手術記録", "画像"]
    payload = {"hints": [{"link_id": "q1", "terms": terms, "expected_types": []}]}
    res = await generate_hints(_client(json.dumps(payload)), "m", [{"link_id": "q1"}])
    got = res.of("q1").terms
    assert "" not in got and " " not in got
    assert len(got) == 6, "空白を除いた6語がすべて残る（枠を食われない）"
