"""KnowledgeDrafter（確認済知識層の LLM 下書き）のユニットテスト（Ollama 非依存）。

fake client は system プロンプトでサブタスクを判別し、定型 JSON を返す。LLM が実在しない
列/未知 key_type/捏造ヘッダを返しても弾くガードと、value_domains の名称ヒューリスティック、
定義書で論理名が付く列を補間対象から外すことを検証する。
"""

from __future__ import annotations

import json
import re
import types
from pathlib import Path

import pytest

from kawasemi_indexer.ingestion.drafter import (
    KnowledgeDrafter,
    _doc_covered_logical,
    draft_value_domains,
)
from kawasemi_indexer.ingestion.models import (
    ColumnSnapshot,
    SchemaSnapshot,
    TableSnapshot,
    TabularSource,
)
from kawasemi_indexer.key_types import KeyType, KeyTypeCatalog


def _ns_response(payload: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        message=types.SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
    )


def _routed_client(routes: dict[str, dict]):
    """system プロンプトの部分文字列 → payload で応答する fake client。"""

    async def chat(**kwargs):
        system = kwargs["messages"][0]["content"]
        for marker, payload in routes.items():
            if marker in system:
                return _ns_response(payload)
        raise AssertionError(f"no route for system prompt: {system[:30]}")

    return types.SimpleNamespace(chat=chat)


def _snap(tables: dict[str, list[tuple[str, bool]]]) -> SchemaSnapshot:
    """tables: {table_id: [(physical, pk), ...]} から SchemaSnapshot を作る。"""
    return SchemaSnapshot(
        connection_id="dwh",
        db_group="G",
        tables=[
            TableSnapshot(
                table_id=t,
                db_qualified=t,
                columns=[
                    ColumnSnapshot(physical_name=n, type_raw="TEXT", type_base="TEXT", pk=pk)
                    for n, pk in cols
                ],
            )
            for t, cols in tables.items()
        ],
    )


_CATALOG = KeyTypeCatalog({"mrn": KeyType(id="mrn", label="院内MRN")})


@pytest.mark.asyncio
async def test_draft_mapping_keeps_only_real_headers(tmp_path: Path) -> None:
    csv = tmp_path / "def.csv"
    csv.write_text("T,C,L,備考\nLAB,WBC,白血球数,x\n", encoding="utf-8")
    client = _routed_client(
        {
            "正規フィールド": {
                "table_id": "T",
                "physical_name": "C",
                "logical_name": "L",
                "notes": "存在しないヘッダ",  # 捏造 → 弾かれる
            }
        }
    )
    drafter = KnowledgeDrafter(client=client, catalog=_CATALOG)
    src = await drafter.draft_mapping(str(csv))
    assert src is not None
    assert src.columns == {"table_id": "T", "physical_name": "C", "logical_name": "L"}
    assert "notes" not in src.columns  # 実在しないヘッダは採用しない


@pytest.mark.asyncio
async def test_draft_classification_validates_master_columns() -> None:
    snap = _snap(
        {"LAB": [("PT_ID", True), ("WBC", False)], "SEX_MASTER": [("CODE", True), ("LABEL", False)]}
    )
    client = _routed_client(
        {
            "role に分類": {
                "tables": [
                    {"table_id": "LAB", "role": "patient_data"},
                    {
                        "table_id": "SEX_MASTER",
                        "role": "master",
                        "code_column": "CODE",
                        "label_column": "LABEL",
                    },
                    {
                        "table_id": "GHOST",
                        "role": "master",
                        "code_column": "X",
                        "label_column": "Y",
                    },
                ]
            }
        }
    )
    drafter = KnowledgeDrafter(client=client, catalog=_CATALOG)
    roles, masters = await drafter.draft_classification(snap)
    assert roles == {"LAB": "patient_data", "SEX_MASTER": "master"}  # GHOST は実在せず無視
    assert masters["SEX_MASTER"].code_column == "CODE"
    assert "GHOST" not in masters


@pytest.mark.asyncio
async def test_draft_patient_keys_filters_unknown_keytype_and_columns() -> None:
    snap = _snap({"LAB": [("PT_ID", True)], "OTHER": [("X", False)]})
    client = _routed_client(
        {
            "患者識別": {
                "keys": [
                    {"table_id": "LAB", "physical_name": "PT_ID", "key_type": "mrn"},
                    {
                        "table_id": "OTHER",
                        "physical_name": "NOPE",
                        "key_type": "mrn",
                    },  # 列不在→弾く
                    {
                        "table_id": "LAB",
                        "physical_name": "PT_ID",
                        "key_type": "bogus",
                    },  # 未知keytype→None
                ]
            }
        }
    )
    drafter = KnowledgeDrafter(client=client, catalog=_CATALOG)
    keys = await drafter.draft_patient_keys(snap, roles={})
    assert keys["LAB"].physical_name == "PT_ID"
    # 同テーブルが2回来る場合は後勝ち（bogus→key_type None）
    assert keys["LAB"].key_type is None
    assert "OTHER" not in keys  # 実在しない列は採用しない


@pytest.mark.asyncio
async def test_draft_patient_keys_skips_master_tables() -> None:
    snap = _snap({"SEX_MASTER": [("CODE", True)]})
    # master のみ → 患者キー対象が無く LLM を呼ばずとも空（呼ばれたら fail させる）
    client = _routed_client(
        {"患者識別": {"keys": [{"table_id": "SEX_MASTER", "physical_name": "CODE"}]}}
    )
    drafter = KnowledgeDrafter(client=client, catalog=_CATALOG)
    keys = await drafter.draft_patient_keys(snap, roles={"SEX_MASTER": "master"})
    assert keys == {}


@pytest.mark.asyncio
async def test_draft_enrichments_excludes_doc_covered(tmp_path: Path) -> None:
    # 定義書で LAB.WBC に論理名が付く → 補間対象から外れる。HGB だけ補間される。
    csv = tmp_path / "def.csv"
    csv.write_text("T,C,L\nLAB,WBC,白血球数\n", encoding="utf-8")
    source = TabularSource(
        path=str(csv), columns={"table_id": "T", "physical_name": "C", "logical_name": "L"}
    )
    snap = _snap({"LAB": [("WBC", False), ("HGB", False)]})

    async def chat(**kwargs):
        msg = kwargs["messages"][1]["content"]
        pairs = re.findall(r"db_qualified=(\S+?), physical_name=(\S+)", msg)
        items = [
            {"db_qualified": d, "physical_name": p, "logical_name": f"L_{p}"} for d, p in pairs
        ]
        return _ns_response({"items": items})

    drafter = KnowledgeDrafter(client=types.SimpleNamespace(chat=chat), catalog=_CATALOG)
    enr = await drafter.draft_enrichments(snap, [source])
    assert "LAB.HGB" in enr
    assert enr["LAB.HGB"].inferred is True and enr["LAB.HGB"].confirmed is False
    assert "LAB.WBC" not in enr  # 定義書で論理名が付くので補間しない


def test_doc_covered_logical(tmp_path: Path) -> None:
    csv = tmp_path / "def.csv"
    csv.write_text("T,C,L\nLAB,WBC,白血球数\nLAB,X,\n", encoding="utf-8")
    source = TabularSource(
        path=str(csv), columns={"table_id": "T", "physical_name": "C", "logical_name": "L"}
    )
    covered = _doc_covered_logical([source])
    assert ("LAB", "WBC") in covered
    assert ("LAB", "X") not in covered  # 論理名が空なら covered ではない


def test_draft_value_domains_heuristic() -> None:
    snap = _snap(
        {
            "LAB": [("PT_ID", True), ("SEX_CD", False), ("WBC", False)],
            "SEX_MASTER": [("CODE", True), ("LABEL", False)],
        }
    )
    roles = {"LAB": "patient_data", "SEX_MASTER": "master"}
    domains = draft_value_domains(snap, roles)
    assert domains == {"LAB.SEX_CD": "SEX_MASTER"}  # 語幹 SEX 一致のみ。WBC等は出さない


def test_draft_value_domains_excludes_key_columns() -> None:
    """PK・患者キー列は語幹一致しても値ドメイン対象外（PT_ID→PT_MASTER 誤マッチ防止）。"""
    from kawasemi_indexer.ingestion.models import PatientKeyBinding

    snap = _snap(
        {
            # PT_ID(pk) と SERIAL(患者キー宣言・非pk) はともに PT_MASTER と語幹一致しうる
            "VISIT": [("PT_ID", True), ("PT_SERIAL", False), ("DEPT_CD", False)],
            "PT_MASTER": [("CODE", True), ("NAME", False)],
            "DEPT_MASTER": [("CODE", True), ("NAME", False)],
        }
    )
    roles = {"VISIT": "patient_data", "PT_MASTER": "master", "DEPT_MASTER": "master"}
    patient_keys = {"VISIT": PatientKeyBinding(physical_name="PT_SERIAL", key_type="mrn")}
    domains = draft_value_domains(snap, roles, patient_keys)
    # PT_ID(pk) も PT_SERIAL(患者キー) も除外。DEPT_CD のみ DEPT_MASTER に対応。
    assert domains == {"VISIT.DEPT_CD": "DEPT_MASTER"}


@pytest.mark.asyncio
async def test_draft_enrichments_rejects_fabricated_columns() -> None:
    """LLM が送っていない列を返しても採用しない（捏造列ガード）。"""
    snap = _snap({"LAB": [("HGB", False)]})

    client = _routed_client(
        {
            "論理名を推定": {
                "items": [
                    {"db_qualified": "LAB", "physical_name": "HGB", "logical_name": "ヘモグロビン"},
                    {"db_qualified": "LAB", "physical_name": "GHOST", "logical_name": "幽霊列"},
                ]
            }
        }
    )
    drafter = KnowledgeDrafter(client=client, catalog=_CATALOG)
    enr = await drafter.draft_enrichments(snap, [])
    assert "LAB.HGB" in enr
    assert "LAB.GHOST" not in enr  # 送っていない列は弾く


# --------------------------------------------------------------------------- #
# 補間対象の選択（オンデマンド化・優先度付け）
# --------------------------------------------------------------------------- #


def _enrich_chat():
    """送られた列をそのまま論理名付きで返す fake chat（捏造なし）。"""

    async def chat(**kwargs):
        msg = kwargs["messages"][1]["content"]
        pairs = re.findall(r"db_qualified=(\S+?), physical_name=(\S+)", msg)
        items = [
            {"db_qualified": d, "physical_name": p, "logical_name": f"L_{p}"} for d, p in pairs
        ]
        return _ns_response({"items": items})

    return types.SimpleNamespace(chat=chat)


def test_select_targets_priority_and_limit() -> None:
    """患者キー/patient_data表を優先し、limit で上位のみに絞る。"""
    from kawasemi_indexer.ingestion.drafter import select_enrichment_targets

    snap = _snap({"MASTER_T": [("A", False), ("B", False)], "VISIT": [("C", False), ("D", False)]})
    sel = select_enrichment_targets(snap, [], priority_tables={"VISIT"}, limit=2)
    # VISIT の列（優先）が limit=2 に収まり、MASTER_T は後回し。
    assert set(sel.targets) == {("VISIT", "C"), ("VISIT", "D")}
    assert sel.eligible == 4 and sel.selected == 2 and sel.dropped_by_limit == 2


def test_select_targets_only_missing_is_idempotent() -> None:
    """already_enriched に載る列は再補間対象から外れる（冪等）。"""
    from kawasemi_indexer.ingestion.drafter import select_enrichment_targets

    snap = _snap({"VISIT": [("C", False), ("D", False)]})
    sel = select_enrichment_targets(snap, [], already_enriched={"VISIT.C"})
    assert set(sel.targets) == {("VISIT", "D")}
    assert sel.already_enriched == 1


def test_select_targets_table_filter() -> None:
    """--tables のグロブに一致する表の列だけを対象にする。"""
    from kawasemi_indexer.ingestion.drafter import select_enrichment_targets

    snap = _snap({"DWH.LAB": [("C", False)], "EHR.DIAGNOSIS": [("D", False)]})
    sel = select_enrichment_targets(snap, [], table_patterns=["dwh.*"])
    assert set(sel.targets) == {("DWH.LAB", "C")}
    assert sel.table_filtered == 1


def test_select_targets_near_terms_priority() -> None:
    """near_terms に物理名が一致する列を優先する。"""
    from kawasemi_indexer.ingestion.drafter import select_enrichment_targets

    snap = _snap({"T": [("BYOMEI_CD", False), ("MISC", False), ("ETC", False)]})
    sel = select_enrichment_targets(snap, [], near_terms=["byomei"], limit=1)
    assert sel.targets == [("T", "BYOMEI_CD")]  # 近傍語一致が最優先


@pytest.mark.asyncio
async def test_draft_enrichments_dry_run_calls_no_llm() -> None:
    """dry_run は LLM を呼ばず空を返す（対象内訳の表示のみ）。"""
    snap = _snap({"VISIT": [("C", False), ("D", False)]})

    async def chat(**kwargs):  # 呼ばれたら失敗させる
        raise AssertionError("dry_run で LLM を呼んではいけない")

    drafter = KnowledgeDrafter(client=types.SimpleNamespace(chat=chat), catalog=_CATALOG)
    enr = await drafter.draft_enrichments(snap, [], dry_run=True)
    assert enr == {}


@pytest.mark.asyncio
async def test_draft_enrichments_limit_and_only_missing() -> None:
    """limit と already_enriched が draft_enrichments 経由でも効く。"""
    snap = _snap({"VISIT": [("C", False), ("D", False), ("E", False)]})
    drafter = KnowledgeDrafter(client=_enrich_chat(), catalog=_CATALOG)
    enr = await drafter.draft_enrichments(snap, [], already_enriched={"VISIT.C"}, limit=1)
    # C は除外、残り2列から limit=1。inferred で人確認前提。
    assert len(enr) == 1
    ((key, e),) = enr.items()
    assert key in ("VISIT.D", "VISIT.E")
    assert e.inferred is True and e.confirmed is False


@pytest.mark.asyncio
async def test_draft_resume_reuses_structure_and_preserves_confirmed(tmp_path: Path) -> None:
    """existing 再利用時、構造下書きは再生成せず、確認済み補間を保持して欠落だけ追補。"""
    from kawasemi_indexer.ingestion.models import (
        Enrichment,
        KnowledgeLayer,
        PatientKeyBinding,
    )

    snap = _snap({"VISIT": [("PT_ID", True), ("DONE", False), ("TODO", False)]})
    existing = KnowledgeLayer(
        connection_id="dwh",
        db_group="G",
        table_roles={"VISIT": "patient_data"},
        patient_keys={"VISIT": PatientKeyBinding(physical_name="PT_ID", key_type="mrn")},
        # DONE は人が確認済み（confirmed=True）。再実行で上書きされてはならない。
        enrichments={
            "VISIT.DONE": Enrichment(logical_name="確定済", inferred=False, confirmed=True)
        },
    )

    # 構造下書き（分類・患者キー）を呼んだら失敗させる = 再利用の証明。
    async def chat(**kwargs):
        system = kwargs["messages"][0]["content"]
        assert "論理名を推定" in system, f"構造下書きを再実行した: {system[:20]}"
        msg = kwargs["messages"][1]["content"]
        pairs = re.findall(r"db_qualified=(\S+?), physical_name=(\S+)", msg)
        items = [
            {"db_qualified": d, "physical_name": p, "logical_name": f"L_{p}"} for d, p in pairs
        ]
        return _ns_response({"items": items})

    drafter = KnowledgeDrafter(client=types.SimpleNamespace(chat=chat), catalog=_CATALOG)
    k = await drafter.draft(
        connection_id="dwh",
        db_group="G",
        snapshot=snap,
        enrich_only_missing=True,
        existing=existing,
    )
    # 構造は existing から再利用。
    assert k.table_roles == {"VISIT": "patient_data"}
    assert k.patient_keys["VISIT"].key_type == "mrn"
    # 確認済み DONE は保持、TODO は新規補間、PT_ID も補間（未補間だったため）。
    assert k.enrichments["VISIT.DONE"].confirmed is True
    assert k.enrichments["VISIT.DONE"].logical_name == "確定済"
    assert "VISIT.TODO" in k.enrichments and k.enrichments["VISIT.TODO"].inferred is True


@pytest.mark.asyncio
async def test_draft_resume_alone_skips_already_enriched(tmp_path: Path) -> None:
    """--resume 単独（only_missing なし）でも、既補間列は LLM へ再送しない。"""
    from kawasemi_indexer.ingestion.models import (
        Enrichment,
        KnowledgeLayer,
        PatientKeyBinding,
    )

    snap = _snap({"VISIT": [("PT_ID", True), ("DONE", False), ("TODO", False)]})
    existing = KnowledgeLayer(
        connection_id="dwh",
        db_group="G",
        table_roles={"VISIT": "patient_data"},
        patient_keys={"VISIT": PatientKeyBinding(physical_name="PT_ID", key_type="mrn")},
        enrichments={"VISIT.DONE": Enrichment(logical_name="既補間", inferred=True)},
    )

    sent_cols: list[str] = []

    async def chat(**kwargs):
        msg = kwargs["messages"][1]["content"]
        pairs = re.findall(r"db_qualified=(\S+?), physical_name=(\S+)", msg)
        sent_cols.extend(p for _d, p in pairs)
        items = [
            {"db_qualified": d, "physical_name": p, "logical_name": f"L_{p}"} for d, p in pairs
        ]
        return _ns_response({"items": items})

    drafter = KnowledgeDrafter(client=types.SimpleNamespace(chat=chat), catalog=_CATALOG)
    k = await drafter.draft(
        connection_id="dwh",
        db_group="G",
        snapshot=snap,
        enrich_only_missing=False,  # --resume 単独を模す
        existing=existing,
    )
    # 既補間の DONE は LLM に送られない（コスト削減）。PT_ID / TODO のみ。
    assert "DONE" not in sent_cols
    assert set(sent_cols) == {"PT_ID", "TODO"}
    assert k.enrichments["VISIT.DONE"].logical_name == "既補間"  # 保持


@pytest.mark.asyncio
async def test_draft_orchestration_roundtrip(tmp_path: Path) -> None:
    """draft() が一貫した KnowledgeLayer を返し、YAML 保存→ロードできる。"""
    from kawasemi_indexer.ingestion.knowledge import load_knowledge, save_knowledge

    csv = tmp_path / "def.csv"
    csv.write_text("T,C,L\nLAB,SEX_CD,性別コード\n", encoding="utf-8")
    snap = _snap(
        {
            "LAB": [("PT_ID", True), ("SEX_CD", False)],
            "SEX_MASTER": [("CODE", True), ("LABEL", False)],
        }
    )
    client = _routed_client(
        {
            "正規フィールド": {"table_id": "T", "physical_name": "C", "logical_name": "L"},
            "role に分類": {
                "tables": [
                    {"table_id": "LAB", "role": "patient_data"},
                    {
                        "table_id": "SEX_MASTER",
                        "role": "master",
                        "code_column": "CODE",
                        "label_column": "LABEL",
                    },
                ]
            },
            "患者識別": {
                "keys": [{"table_id": "LAB", "physical_name": "PT_ID", "key_type": "mrn"}]
            },
            "論理名を推定": {
                "items": [
                    {"db_qualified": "LAB", "physical_name": "PT_ID", "logical_name": "患者番号"}
                ]
            },
        }
    )
    drafter = KnowledgeDrafter(client=client, catalog=_CATALOG)
    k = await drafter.draft(
        connection_id="dwh", db_group="G", snapshot=snap, tabular_paths=[str(csv)]
    )
    assert k.table_roles == {"LAB": "patient_data", "SEX_MASTER": "master"}
    assert k.patient_keys["LAB"].key_type == "mrn"
    assert k.master_tables["SEX_MASTER"].label_column == "LABEL"
    assert k.value_domains == {"LAB.SEX_CD": "SEX_MASTER"}
    assert k.tabular_sources[0].columns["physical_name"] == "C"

    path = tmp_path / "k.yaml"
    save_knowledge(k, path)
    assert load_knowledge(path).connection_id == "dwh"
