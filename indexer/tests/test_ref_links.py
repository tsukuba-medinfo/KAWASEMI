"""参照リンクチャネル（列→マスタ、RRF 第3チャネル）の契約テスト。

実 Ollama 非依存。HashingEmbedder とスタブ LLM クライアントで駆動する。
- リンクの下書き（ルール / LLM）と確度順の候補列挙
- store の第3チャネル融合（低 cosine のリンク先列が押し上がる・exact 最優先は不変）
- パイプラインの発動トリガ（rewrite 由来 / kind=master 上位フォールバック）
- eval の mode=ref
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest
from test_resource_index import HashingEmbedder

from kawasemi_indexer.builder import write_index
from kawasemi_indexer.eval import GoldItem, evaluate
from kawasemi_indexer.ref_links import (
    RefLink,
    RefLinkIndex,
    draft_llm_links,
    draft_rule_links,
    llm_candidates,
    merge_links,
    rules_for,
    write_ref_links,
)
from kawasemi_indexer.schema import REF_LINKS_FILENAME, Kind, ResourceUnit
from kawasemi_indexer.search_pipeline import parse_master_unit_id, run_search
from kawasemi_indexer.site_rules import IndexerRules, MasterRef, load_indexer_rules
from kawasemi_indexer.store import ResourceIndexStore
from kawasemi_indexer.synonyms import QueryRewriter, build_synonym_index

# 施設の索引規則（架空）。リンク先のマスタ表はコードに無く、ここから来る。
MASTERS = {
    "byomei": MasterRef(connection_id="ehr", db_qualified="MASTER.DISEASE"),
    "exam": MasterRef(connection_id="dwh", db_qualified="MASTER.LAB_ITEM"),
}
EXAMPLE_RULES = Path(__file__).resolve().parents[2] / "site.example" / "indexer_rules.yaml"


def _db(unit_id: str, logical: str, physical: str, text: str = "") -> ResourceUnit:
    return ResourceUnit(
        unit_id=unit_id,
        kind=Kind.DB,
        label=logical or physical,
        search_text=text or f"{logical} {physical}",
        payload={
            "connection_id": "ehr",
            "db_qualified": unit_id.split(":")[1].rsplit(".", 1)[0],
            "physical_name": physical,
            "logical_name": logical,
        },
    )


def _master(code: str, label: str, aliases: list[str], text: str = "") -> ResourceUnit:
    return ResourceUnit(
        unit_id=f"master:ehr:MASTER.DISEASE:{code}",
        kind=Kind.MASTER,
        label=label,
        search_text=text or " ".join([label, *aliases]),
        payload={
            "connection_id": "ehr",
            "db_qualified": "MASTER.DISEASE",
            "code": code,
            "label": label,
            "aliases": aliases,
        },
    )


# --------------------------------------------------------------------------- #
# リンクの下書きと候補列挙
# --------------------------------------------------------------------------- #
def test_draft_rule_links_matches_code_and_name_columns():
    units = [
        _db("db:DEMO.CLAIM.DX_CODE", "病名コード", "DX_CODE"),
        _db("db:DEMO.SUMMARY.DX_NAME", "病名", "DX_NAME"),
        _db("db:MASTER.DISEASE.DX_CATEGORY", "病名区分", "DX_CATEGORY"),  # 除外パターン
        _db("db:DEMO.LAB_ORDER.TEST_CD", "検査項目コード", "TEST_CD"),
        _db("db:DEMO.LAB_RESULT.JLAC10_CODE", "JLAC10コード", "JLAC10_CODE"),
        _db("db:X.Y.OTHER", "患者ID", "PT_ID"),
    ]
    links = draft_rule_links(units, rules_for(MASTERS))
    by_id = {ln.column_unit_id: ln for ln in links}

    assert by_id["db:DEMO.CLAIM.DX_CODE"].role == "code"
    assert by_id["db:DEMO.CLAIM.DX_CODE"].master_db_qualified == "MASTER.DISEASE"
    assert by_id["db:DEMO.SUMMARY.DX_NAME"].role == "name"
    assert by_id["db:DEMO.LAB_ORDER.TEST_CD"].master_db_qualified == "MASTER.LAB_ITEM"
    assert by_id["db:DEMO.LAB_RESULT.JLAC10_CODE"].master_db_qualified == "MASTER.LAB_ITEM"
    assert "db:MASTER.DISEASE.DX_CATEGORY" not in by_id  # 区分は除外
    assert "db:X.Y.OTHER" not in by_id
    assert all(ln.status == "rule" for ln in links)


def test_candidates_for_orders_by_status_and_dedups(tmp_path):
    links = [
        RefLink(
            column_unit_id="db:A",
            master_connection_id="ehr",
            master_db_qualified="MASTER.DISEASE",
            status="inferred",
        ),
        RefLink(
            column_unit_id="db:B",
            master_connection_id="ehr",
            master_db_qualified="MASTER.DISEASE",
            status="confirmed",
        ),
        RefLink(
            column_unit_id="db:C",
            master_connection_id="ehr",
            master_db_qualified="MASTER.DISEASE",
            status="rule",
        ),
        # 同一列に弱いリンクが重複 → 最良 status を採る
        RefLink(
            column_unit_id="db:B",
            master_connection_id="ehr",
            master_db_qualified="MASTER.DISEASE",
            status="inferred",
        ),
        # 対象外マスタ
        RefLink(
            column_unit_id="db:D",
            master_connection_id="dwh",
            master_db_qualified="MASTER.LAB_ITEM",
            status="confirmed",
        ),
    ]
    write_ref_links(links, tmp_path)
    idx = RefLinkIndex.load(tmp_path)
    got = idx.candidates_for([("ehr", "MASTER.DISEASE")])
    assert got == [("db:B", "confirmed"), ("db:C", "rule"), ("db:A", "inferred")]


def test_ref_link_index_load_missing_and_comments(tmp_path):
    assert len(RefLinkIndex.load(tmp_path)) == 0  # 未配置 → 空で no-op
    p = tmp_path / REF_LINKS_FILENAME
    p.write_text(
        "# 人手確認: status を confirmed に書き換える\n"
        + json.dumps(
            {
                "column_unit_id": "db:A",
                "master_connection_id": "C",
                "master_db_qualified": "S.T",
                "status": "rule",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    assert len(RefLinkIndex.load(tmp_path)) == 1


def test_draft_llm_links_rejects_unpresented_ids():
    class StubClient:
        async def chat(self, model, messages, format, options):  # noqa: A002
            content = json.dumps(
                {
                    "judgements": [
                        {
                            "unit_id": "db:DEMO.VISIT.CHIEF_COMPLAINT",
                            "master": "byomei",
                            "role": "name",
                        },
                        {"unit_id": "db:HALLUCINATED.X.Y", "master": "byomei", "role": "code"},
                        {"unit_id": "db:DEMO.VISIT.MEMO", "master": "none"},
                    ]
                }
            )

            class Msg:
                pass

            class Resp:
                pass

            r = Resp()
            r.message = Msg()
            r.message.content = content
            return r

    units = [
        _db("db:DEMO.VISIT.CHIEF_COMPLAINT", "主訴・病名", "CHIEF_COMPLAINT"),
        _db("db:DEMO.VISIT.MEMO", "診断メモ", "MEMO"),
    ]
    cands = llm_candidates(units, exclude_ids=set())
    assert len(cands) == 2  # 概念語（病名/診断）を含む列のみ
    links = asyncio.run(draft_llm_links(cands, model="stub", client=StubClient(), masters=MASTERS))
    assert [ln.column_unit_id for ln in links] == ["db:DEMO.VISIT.CHIEF_COMPLAINT"]
    assert links[0].status == "inferred" and links[0].role == "name"
    assert (links[0].master_connection_id, links[0].master_db_qualified) == (
        "ehr",
        "MASTER.DISEASE",
    )


# --------------------------------------------------------------------------- #
# 施設の索引規則（indexer_rules.yaml）が無いときは施設に依らない振る舞い
# --------------------------------------------------------------------------- #
def test_no_site_rules_drafts_no_links(tmp_path, monkeypatch):
    """規則ファイルが無ければ、ルールも LLM もリンクを下書きしない（LLM は呼ばない）。"""
    from kawasemi_indexer.config import settings

    monkeypatch.setattr(settings, "indexer_rules_file", str(tmp_path / "missing.yaml"))
    units = [
        _db("db:DEMO.CLAIM.DX_CODE", "病名コード", "DX_CODE"),
        _db("db:DEMO.LAB_ORDER.TEST_CD", "検査項目コード", "TEST_CD"),
    ]
    assert draft_rule_links(units) == []

    class NeverCalled:
        async def chat(self, *a, **k):  # pragma: no cover - 呼ばれたら失敗
            raise AssertionError("マスタが無いのに LLM を呼んだ")

    cands = llm_candidates(units, exclude_ids=set())
    assert cands  # 候補はあるが、リンク先が無いので判定しない
    assert asyncio.run(draft_llm_links(cands, model="stub", client=NeverCalled())) == []


def test_rules_for_drops_concepts_without_a_master():
    """書かれた概念のルールだけを作る（病名だけなら検査のルールは出ない）。"""
    rules = rules_for({"byomei": MASTERS["byomei"]})
    assert [r.name for r in rules] == ["byomei_code", "byomei_name"]
    assert {(r.master_connection_id, r.master_db_qualified) for r in rules} == {
        ("ehr", "MASTER.DISEASE")
    }
    assert rules_for({}) == []


def test_draft_rule_links_reads_the_site_rules_file(tmp_path, monkeypatch):
    """`rules` 省略時は設定の `indexer_rules_file` を読む（例示ファイルで確かめる）。"""
    from kawasemi_indexer.config import settings

    monkeypatch.setattr(settings, "indexer_rules_file", str(EXAMPLE_RULES))
    (link,) = draft_rule_links([_db("db:DEMO.CLAIM.DX_CODE", "病名コード", "DX_CODE")])
    assert (link.master_connection_id, link.master_db_qualified) == ("ehr", "MASTER.DISEASE")
    assert link.source == "rule:byomei_code"


def test_load_indexer_rules_missing_example_and_malformed(tmp_path):
    assert load_indexer_rules(tmp_path / "missing.yaml") == IndexerRules()
    example = load_indexer_rules(EXAMPLE_RULES)
    assert set(example.ref_link_masters) == {"byomei", "exam"}
    bad = tmp_path / "bad.yaml"
    bad.write_text("ref_link_masters:\n  byomei: {connection_id: x}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="形式が不正"):
        load_indexer_rules(bad)
    typo = tmp_path / "typo.yaml"
    typo.write_text("ref_link_master: {}\n", encoding="utf-8")
    with pytest.raises(ValueError, match="形式が不正"):
        load_indexer_rules(typo)


def test_merge_links_rule_wins_over_llm():
    rule = [
        RefLink(
            column_unit_id="db:A",
            master_connection_id="C",
            master_db_qualified="S.T",
            status="rule",
        )
    ]
    llm = [
        RefLink(
            column_unit_id="db:A",
            master_connection_id="C",
            master_db_qualified="S.T",
            status="inferred",
        ),
        RefLink(
            column_unit_id="db:B",
            master_connection_id="C",
            master_db_qualified="S.T",
            status="inferred",
        ),
    ]
    merged = merge_links(rule, llm)
    assert len(merged) == 2
    assert {(ln.column_unit_id, ln.status) for ln in merged} == {
        ("db:A", "rule"),
        ("db:B", "inferred"),
    }


# --------------------------------------------------------------------------- #
# store の第3チャネルとパイプライン
# --------------------------------------------------------------------------- #
@pytest.fixture
def ref_index(tmp_path) -> Path:
    """病名マスタ＋格納先列＋妨害列の小さな索引と参照リンクを組む。"""
    units_master = [
        _master("2500", "糖尿病", ["トウニョウビョウ", "E14"]),
        _master("100", "高血圧症", ["コウケツアツショウ", "I10"]),
    ]
    units_db = [
        # 格納先列（語彙がクエリと重ならない＝純 embedding では拾えない想定）
        _db("db:DEMO.CLAIM.DX_CODE", "病名コード", "DX_CODE", text="レセプト 請求 番号"),
        _db("db:DEMO.SUMMARY.DX_NAME", "病名", "DX_NAME", text="診療 記録 文字列"),
        # 妨害列（意味的にクエリと重なるが字面一致もリンクもない）
        _db("db:X.Y.EDU", "教育入院プログラム", "EDU_PROG", text="糖尿病 教育 入院 プログラム"),
        _db("db:X.Y.MEAL", "給食メニュー", "MEAL_MENU", text="糖尿病 食事 メニュー 給食"),
    ]
    out = tmp_path / "resource_index"
    write_index({Kind.DB: units_db, Kind.MASTER: units_master}, HashingEmbedder(), out)
    build_synonym_index(out)
    links = [
        RefLink(
            column_unit_id="db:DEMO.CLAIM.DX_CODE",
            master_connection_id="ehr",
            master_db_qualified="MASTER.DISEASE",
            status="confirmed",
            role="code",
        ),
        RefLink(
            column_unit_id="db:DEMO.SUMMARY.DX_NAME",
            master_connection_id="ehr",
            master_db_qualified="MASTER.DISEASE",
            status="inferred",
            role="name",
        ),
    ]
    write_ref_links(links, out)
    return out


def test_store_ref_channel_boosts_linked_columns(ref_index):
    store = ResourceIndexStore.load(ref_index)
    emb = HashingEmbedder()
    qv = emb.embed(["糖尿病"])[0]
    ref_candidates = RefLinkIndex.load(ref_index).candidates_for([("ehr", "MASTER.DISEASE")])

    plain = store.search(qv, top_k=5, min_score=-1.0, lexical_terms=["糖尿病"])
    boosted = store.search(
        qv, top_k=5, min_score=-1.0, lexical_terms=["糖尿病"], ref_candidates=ref_candidates
    )

    def rank(hits, uid):
        return next((i for i, h in enumerate(hits) if h.unit.unit_id == uid), None)

    # 素のハイブリッドでは意味的に重なる妨害列が格納先列より上に来るが、
    # 第3チャネルで格納先列（リンク済み）が妨害列を追い越す。
    assert rank(plain, "db:X.Y.EDU") < rank(plain, "db:DEMO.CLAIM.DX_CODE")
    r_code = rank(boosted, "db:DEMO.CLAIM.DX_CODE")
    r_name = rank(boosted, "db:DEMO.SUMMARY.DX_NAME")
    assert r_code < rank(boosted, "db:X.Y.EDU")
    assert r_name < rank(boosted, "db:X.Y.EDU")
    # チャネル内順位: confirmed（コード列）が inferred（名称列）より上。
    assert r_code < r_name
    # ヒットに ref_status が付く（⚑ 表示の根拠）。
    assert boosted[r_code].ref_status == "confirmed"
    assert boosted[r_name].ref_status == "inferred"
    # 完全一致の最優先（sort key (is_exact, rrf)）は不変: 「糖尿病」exact のマスタが最上位。
    assert boosted[0].unit.unit_id == "master:ehr:MASTER.DISEASE:2500"
    assert boosted[0].match_type is not None


def test_parse_master_unit_id():
    assert parse_master_unit_id("master:ehr:MASTER.DISEASE:2500") == ("ehr", "MASTER.DISEASE")
    assert parse_master_unit_id("db:S.T.COL") is None


def test_run_search_ref_trigger_via_rewrite(ref_index):
    """標準コード解決（ICD10 直打ち）→ 展開元マスタの表 → リンク列がブーストされる。"""
    store = ResourceIndexStore.load(ref_index)
    outcome = run_search(
        store,
        HashingEmbedder(),
        "E14",
        mode="ref",
        top_k=5,
        min_score=-1.0,
        rewriter=QueryRewriter.load(ref_index),
        ref_links=RefLinkIndex.load(ref_index),
    )
    assert outcome.rewrite is not None and outcome.rewrite.matched
    assert outcome.ref_masters and outcome.ref_masters[0]["via"] == "rewrite"
    assert outcome.ref_masters[0]["db_qualified"] == "MASTER.DISEASE"
    ids = [h.unit.unit_id for h in outcome.hits]
    assert "db:DEMO.CLAIM.DX_CODE" in ids


def test_run_search_ref_trigger_via_master_hits(ref_index):
    """書き換え不発（表記が完全一致しない）→ 上位 kind=master ヒットから表を特定する。"""
    store = ResourceIndexStore.load(ref_index)
    outcome = run_search(
        store,
        HashingEmbedder(),
        "糖尿病 の 格納先",
        mode="ref",
        top_k=5,
        min_score=-1.0,
        rewriter=QueryRewriter.load(ref_index),
        ref_links=RefLinkIndex.load(ref_index),
    )
    assert outcome.rewrite is None or not outcome.rewrite.matched
    assert outcome.ref_masters and outcome.ref_masters[0]["via"] == "master_hit"
    ids = [h.unit.unit_id for h in outcome.hits]
    assert "db:DEMO.CLAIM.DX_CODE" in ids


def test_referencing_columns_single_master(ref_index):
    """FK逆引き: 1マスタ表を参照する患者表FK列を (unit_id, role, status) で確度順に返す。"""
    idx = RefLinkIndex.load(ref_index)
    cols = idx.referencing_columns("ehr", "MASTER.DISEASE")
    assert [c[0] for c in cols] == [
        "db:DEMO.CLAIM.DX_CODE",  # confirmed が先
        "db:DEMO.SUMMARY.DX_NAME",  # inferred が後
    ]
    assert cols[0][1] == "code" and cols[0][2] == "confirmed"
    assert cols[1][1] == "name" and cols[1][2] == "inferred"
    # 未知マスタは空。
    assert idx.referencing_columns("ehr", "NO.SUCH") == []


def test_run_search_annotates_master_hits_with_fk_columns(ref_index):
    """master hit が「それを参照する患者表FK列」で注釈される。"""
    store = ResourceIndexStore.load(ref_index)
    outcome = run_search(
        store,
        HashingEmbedder(),
        "糖尿病",
        mode="ref",
        top_k=5,
        min_score=-1.0,
        rewriter=QueryRewriter.load(ref_index),
        ref_links=RefLinkIndex.load(ref_index),
    )
    master_uid = "master:ehr:MASTER.DISEASE:2500"
    assert master_uid in outcome.ref_columns_by_master
    cols = outcome.ref_columns_by_master[master_uid]
    assert [c[0] for c in cols] == [
        "db:DEMO.CLAIM.DX_CODE",
        "db:DEMO.SUMMARY.DX_NAME",
    ]
    # code 列が role=code で先頭（利用側が WHERE を組む材料）。
    assert cols[0][1] == "code"


def test_run_search_no_annotation_without_links(ref_index):
    """ref_links 未配置なら FK逆引き注釈は空（縮退・エラーにしない）。"""
    store = ResourceIndexStore.load(ref_index)
    outcome = run_search(
        store,
        HashingEmbedder(),
        "糖尿病",
        mode="ref",
        top_k=5,
        min_score=-1.0,
        rewriter=QueryRewriter.load(ref_index),
        ref_links=None,
    )
    assert outcome.ref_columns_by_master == {}


def test_run_search_ref_degrades_without_links(ref_index):
    """ref_links 未配置なら ref モードは rewrite 相当へ縮退する（エラーにしない）。"""
    store = ResourceIndexStore.load(ref_index)
    outcome = run_search(
        store,
        HashingEmbedder(),
        "糖尿病",
        mode="ref",
        top_k=5,
        min_score=-1.0,
        rewriter=QueryRewriter.load(ref_index),
        ref_links=None,
    )
    assert outcome.ref_masters == [] and outcome.ref_candidates == []
    assert outcome.hits  # 検索自体は成立


def test_evaluate_ref_mode_beats_rewrite_on_storage_column(ref_index):
    """storage_column 型ゴールド（複数正解）で ref モードが rewrite を上回る。"""
    store = ResourceIndexStore.load(ref_index)
    rewriter = QueryRewriter.load(ref_index)
    ref_links = RefLinkIndex.load(ref_index)
    gold = [
        GoldItem(
            query="糖尿病",
            unit_ids=["db:DEMO.CLAIM.DX_CODE", "db:DEMO.SUMMARY.DX_NAME"],
            tags=["storage_column"],
        ),
    ]
    rew = evaluate(
        store, HashingEmbedder(), gold, mode="rewrite", rewriter=rewriter, ref_links=ref_links
    )
    ref = evaluate(
        store, HashingEmbedder(), gold, mode="ref", rewriter=rewriter, ref_links=ref_links
    )
    assert ref.overall.recall_at_10 == 1.0
    assert ref.overall.recall_at_10 >= rew.overall.recall_at_10
    assert ref.overall.mrr > rew.overall.mrr
