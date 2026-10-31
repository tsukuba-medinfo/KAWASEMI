"""標準コード変換テーブルとクエリ書き換え層の契約テスト。"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_resource_index import HashingEmbedder

from kawasemi_indexer.builder import write_index
from kawasemi_indexer.schema import Kind, ResourceUnit
from kawasemi_indexer.store import ResourceIndexStore
from kawasemi_indexer.synonyms import (
    QueryRewriter,
    build_synonym_entries,
    build_synonym_index,
    classify_term,
)


def _drug(unit_id: str, label: str, aliases: list[str], code: str) -> ResourceUnit:
    return ResourceUnit(
        unit_id=unit_id,
        kind=Kind.MASTER,
        label=label,
        search_text=" ".join([label, *aliases]),
        payload={
            "connection_id": "dwh",
            "db_qualified": "DEMO.ORDER_ITEM",
            "code": code,
            "aliases": aliases,
        },
    )


def test_classify_term():
    assert classify_term("9999999000001") == "code"  # HOT13
    assert classify_term("9999999x9999") == "code"  # YJ 形式の架空コード（正規化済み小文字）
    assert classify_term("d481") == "code"  # ICD10
    assert classify_term("架空成分甲塩酸塩") == "name"
    assert classify_term("架空薬甲錠") == "name"


def test_build_entries_splits_names_and_codes():
    # 標準コード（HOT/YJ/レセ電）は aliases 側、院内コードは payload.code（＝internal_code）。
    units = [
        _drug(
            "master:dwh:D:1",
            "架空薬甲錠 15mg",
            ["架空成分甲塩酸塩", "9999999000001", "9999999X9999", "999999999"],
            "10001",
        ),
    ]
    entries = build_synonym_entries(units)
    assert len(entries) == 1
    e = entries[0]
    assert "架空薬甲錠 15mg" in e.names
    assert "架空成分甲塩酸塩" in e.names
    assert "9999999000001" in e.codes  # HOT13
    assert "999999999" in e.codes  # レセ電
    # 院内コードは横断展開語に含めない（接続ローカルで衝突するため）。
    assert e.internal_code == "10001"
    assert "10001" not in e.codes
    assert "10001" not in e.terms


def test_entry_with_only_label_and_internal_code_is_dropped():
    # label + 院内コードのみ（横断できる別名・標準コードなし）→ 変換価値が無いので落とす。
    units = [_drug("master:dwh:D:9", "単独薬", [], "10001")]
    assert build_synonym_entries(units) == []


def test_rewriter_expands_general_name_to_product_and_codes():
    units = [
        _drug("master:dwh:D:1", "架空薬甲錠 15mg", ["架空成分甲塩酸塩", "9999999000001"], "10001"),
    ]
    rw = QueryRewriter(build_synonym_entries(units))
    # 一般名で引くと製品名・コードへ展開される
    res = rw.rewrite("架空成分甲塩酸塩")
    assert res.matched
    assert "架空薬甲錠 15mg" in res.expansions
    assert "9999999000001" in res.expansions
    assert "架空成分甲塩酸塩" not in res.expansions  # 元クエリは展開語に含めない
    assert res.query in res.all_terms
    assert "架空薬甲錠 15mg" in res.expanded_text
    assert res.provenance[0]["unit_id"] == "master:dwh:D:1"


def test_rewriter_expands_from_standard_code():
    # 標準コード（HOT）は aliases 側。それで引くと名称・他コードへ展開される。
    units = [_drug("master:dwh:D:1", "架空薬甲錠", ["架空成分甲塩酸塩", "9999999000001"], "10001")]
    rw = QueryRewriter(build_synonym_entries(units))
    res = rw.rewrite("9999999000001")
    assert "架空薬甲錠" in res.expansions
    assert "架空成分甲塩酸塩" in res.expansions


def test_rewriter_does_not_expand_from_internal_code():
    # 院内コード（payload.code）では展開しない（接続ローカルで衝突源になるため）。
    units = [_drug("master:dwh:D:1", "架空薬甲錠", ["架空成分甲塩酸塩"], "10001")]
    rw = QueryRewriter(build_synonym_entries(units))
    assert not rw.rewrite("10001").matched


def test_rewriter_no_match_is_noop():
    rw = QueryRewriter(build_synonym_entries([_drug("m:1", "薬A", ["別名A"], "1")]))
    res = rw.rewrite("まったく無関係な語")
    assert not res.matched
    assert res.expansions == []
    assert res.all_terms == ["まったく無関係な語"]
    assert res.expanded_text == "まったく無関係な語"


@pytest.fixture
def index_with_synonyms(tmp_path) -> Path:
    """薄い aliases の別エントリを含む索引（コード経由の横断展開を検証する）。"""
    units = [
        # aliases 豊富: 一般名と製品名とコードを持つ
        _drug("master:dwh:D:1", "架空薬甲錠 15mg", ["架空成分甲塩酸塩", "9999999000001"], "10001"),
        # aliases 薄い別接続の同薬: 製品名のみ（一般名で直接は引けない）
        ResourceUnit(
            unit_id="master:OTHER:D:2",
            kind=Kind.MASTER,
            label="架空薬甲錠 15mg",
            search_text="架空薬甲錠 15mg 分子標的",
            payload={
                "connection_id": "OTHER",
                "db_qualified": "OTHER.DRUG",
                "code": "9999",
                "aliases": [],
            },
        ),
        _drug("master:dwh:D:3", "無関係薬", ["解熱鎮痛"], "20002"),
    ]
    out = tmp_path / "resource_index"
    write_index({Kind.MASTER: units}, HashingEmbedder(), out)
    build_synonym_index(out)  # synonym_index.jsonl を併置
    return out


def test_synonym_index_roundtrip_and_load(index_with_synonyms):
    assert (index_with_synonyms / "synonym_index.jsonl").exists()
    rw = QueryRewriter.load(index_with_synonyms)
    # entry:2 は label＋院内コードのみ（横断語1個）で落ちる。entry:1 と entry:3 が載る。
    assert rw.entry_count == 2
    res = rw.rewrite("架空成分甲塩酸塩")
    assert "架空薬甲錠 15mg" in res.expansions


def test_rewrite_reaches_thin_alias_entry_via_expansion(index_with_synonyms):
    """一般名クエリを製品名へ展開すると、製品名しか持たない別接続エントリも字面で拾える。"""
    store = ResourceIndexStore.load(index_with_synonyms)
    rw = QueryRewriter.load(index_with_synonyms)
    q = "架空成分甲塩酸塩"
    res = rw.rewrite(q)
    qv = HashingEmbedder().embed([res.expanded_text])[0]
    hits = store.search(qv, top_k=5, query_text=q, lexical_terms=res.all_terms)
    ids = {h.unit.unit_id for h in hits}
    # 一般名を持つ entry:1 に加え、製品名のみの entry:2 も展開語の字面一致で拾える。
    assert "master:dwh:D:1" in ids
    assert "master:OTHER:D:2" in ids
