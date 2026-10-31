"""E2E 試験環境の生成器と固定資産の決定的検証（Ollama 不要）。

- 生成は決定的（2 回生成して全ファイルが一致する）
- 追跡している gold.jsonl は、いま生成したものと一致する（データと正解が食い違わない）
- 固定資産（様式・マニフェスト・FHIR 辞書・典拠の範囲）が gold の期待単位を裏付ける
- indexer の `ingest_connection` が knowledge.yaml と生成した DB から
  column_index.jsonl を作り、gold が指す表がすべて載る
- フォールバック項目は一部の患者だけ DWH が空、2 つの seq が同居する項目がある
- seed がアプリDBへ様式・利用者・データセットを投入できる
"""

from __future__ import annotations

import csv
import json
import re
import sqlite3
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kawasemi.ai_input import e2e
from kawasemi.ai_input.e2e import SUBJECTS, E2ESubject, make_subjects
from kawasemi.ai_input.e2e import sources as src
from kawasemi.models import Dataset, InputTarget, Role, User, UserRole
from kawasemi.models.questionnaire import Questionnaire
from tests.conftest import TestSessionLocal

# Pillow の PDF 書き出しは生成時刻を埋める。中身（画像）は決定的なので、日付だけ落として比べる。
_PDF_DATE = re.compile(rb"/(?:Creation|Mod)Date \([^)]*\)")


def _snapshot(root: Path) -> dict[str, bytes]:
    out: dict[str, bytes] = {}
    for p in sorted(root.rglob("*")):
        if p.is_file():
            data = p.read_bytes()
            if p.suffix == ".pdf":
                data = _PDF_DATE.sub(b"", data)
            out[p.relative_to(root).as_posix()] = data
    return out


@pytest.fixture(scope="module")
def generated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """モジュール内で 1 回だけ生成する（手書き PDF の描画が重い）。"""
    root = tmp_path_factory.mktemp("e2e_generated")
    src.generate_all(root, gold_path=root / "gold.jsonl")
    return root


def _by_link(rows: list[dict]) -> dict[str, dict]:
    return {r["link_id"]: r for r in rows}


# --------------------------------------------------------------------------- #
# 決定性と gold
# --------------------------------------------------------------------------- #


def test_subjects_are_deterministic() -> None:
    assert make_subjects() == SUBJECTS
    assert len(SUBJECTS) == 10
    # 各システムの患者キーは互いに違う体系
    s = SUBJECTS[0]
    assert len({s.reg_no, s.mrn_physical, s.billing_id, s.report_key, s.fhir_id}) == 5
    assert s.mrn_physical == s.mrn_canonical.zfill(10) and len(s.mrn_physical) == 10


def test_generation_is_deterministic(generated: Path, tmp_path: Path) -> None:
    second = tmp_path / "second"
    src.generate_all(second, gold_path=second / "gold.jsonl")
    a, b = _snapshot(generated), _snapshot(second)
    assert a.keys() == b.keys()
    diff = [k for k in a if a[k] != b[k]]
    assert diff == [], f"再生成で変わったファイル: {diff}"


def test_tracked_gold_matches_fresh_generation() -> None:
    """追跡している examples/e2e/gold.jsonl は、いま生成した正解と一致する。

    ずれたら `kawasemi e2e setup`（または `write_gold()`）で作り直してコミットする。
    """
    assert e2e.GOLD_JSONL.exists(), f"{e2e.GOLD_JSONL} が無い。`kawasemi e2e setup` で生成する"
    assert src.read_gold(e2e.GOLD_JSONL) == src.build_gold()


def test_gold_covers_every_auto_item_with_all_patients() -> None:
    content = json.loads(e2e.QUESTIONNAIRE_JSON.read_text("utf-8"))
    assert content["url"] == e2e.QUESTIONNAIRE_URL
    link_ids = [it["linkId"] for it in content["item"]]
    assert 10 <= len(link_ids) <= 15
    gold = _by_link(src.build_gold())
    # 登録番号（Hub）だけは取得の起点なので gold に無い。他の全項目に期待単位と期待値がある
    assert set(gold) == set(link_ids) - {"patient_id"}
    reg_nos = {s.reg_no for s in SUBJECTS}
    for link_id, row in gold.items():
        assert row["unit_id"] and row["route"] and row["connection_id"], link_id
        assert set(row["expected"]) == reg_nos, link_id
        assert all(v not in (None, "") for v in row["expected"].values()), link_id
    # 7 つの経路（列・item_code・LLM・ファイル・FHIR・CSV・フォールバック）が各 1〜2 項目ある
    routes = [r["route"] for r in gold.values()]
    assert {
        src.ROUTE_DB,
        src.ROUTE_LLM,
        src.ROUTE_FILE,
        src.ROUTE_FHIR,
        src.ROUTE_CSV,
        src.ROUTE_FALLBACK,
    } <= set(routes)
    assert (
        sum(1 for r in gold.values() if r["unit_id"].startswith("db:") and "#" in r["unit_id"]) == 2
    )


def test_fixed_assets_back_the_gold_units() -> None:
    """gold の file / fhir 単位はマニフェストと FHIR 辞書に宣言されている。典拠の範囲も様式を指す。"""
    gold = _by_link(src.build_gold())
    manifest_labels = {
        json.loads(ln)["label"]
        for ln in e2e.FILE_MANIFEST.read_text("utf-8").splitlines()
        if ln.strip()
    }
    fhir_ids = {
        f"fhir:{d['resource_type']}:{d['code']}"
        for d in (
            json.loads(ln)
            for ln in e2e.FHIR_DICTIONARY.read_text("utf-8").splitlines()
            if ln.strip()
        )
    }
    for row in gold.values():
        unit = row["unit_id"]
        if unit.startswith("file:patient_data:"):
            assert unit.removeprefix("file:patient_data:") in manifest_labels, unit
        if unit.startswith("fhir:"):
            assert unit in fhir_ids, unit
    fb = gold["onset_date"]
    assert fb["fallback_unit_id"].removeprefix("file:patient_data:") in manifest_labels

    scopes = yaml.safe_load(e2e.REFERENCE_SCOPES.read_text("utf-8"))["scopes"]
    scope = next(s for s in scopes if e2e.QUESTIONNAIRE_URL in s["questionnaires"])
    for ref in scope["references"]:
        assert (e2e.REFERENCES_DIR / ref).is_file(), ref
    manual = (e2e.REFERENCES_DIR / scope["references"][0]).read_text("utf-8")
    assert "腫瘍" not in manual  # 別領域の規約を写していない


# --------------------------------------------------------------------------- #
# 生成物の中身
# --------------------------------------------------------------------------- #


def test_fallback_item_has_empty_dwh_for_some_patients(generated: Path) -> None:
    gold = _by_link(src.build_gold())["onset_date"]
    conn = sqlite3.connect(generated / "dwh.db")
    rows = dict(conn.execute("SELECT patient_no, onset_date FROM infection_cases").fetchall())
    conn.close()
    empty = {s.reg_no for s in SUBJECTS if rows[s.mrn_physical] is None}
    assert 0 < len(empty) < len(SUBJECTS)
    assert empty == {k for k, v in gold["expected_source"].items() if v == "text"}
    for s in SUBJECTS:
        if s.reg_no in empty:
            # 本文には発症日がある（LLM 読みの素）
            text = (generated / "reports" / s.report_key / "admission_summary.txt").read_text(
                "utf-8"
            )
            assert f"{s.onset_date.year}年{s.onset_date.month}月{s.onset_date.day}日より" in text
        else:
            assert rows[s.mrn_physical] == gold["expected"][s.reg_no]


def test_two_seq_item_and_deleted_rows(generated: Path) -> None:
    """体重測定（weight）は seq 1 と 2 が同居し、削除行は有効行より大きい値を持つ。"""
    gold = _by_link(src.build_gold())
    conn = sqlite3.connect(generated / "dwh.db")
    seqs = conn.execute(
        "SELECT seq FROM measurements WHERE measure_code = (SELECT measure_code FROM observation_items WHERE item_code = ?)",
        (src.ITEM_WEIGHT,),
    ).fetchall()
    assert sorted(s[0] for s in seqs) == ["1", "2"]
    for s in SUBJECTS:
        pid = s.mrn_physical
        latest_valid = conn.execute(
            "SELECT value_text FROM observations WHERE patient_no=? AND item_code=? AND seq='1' AND is_deleted='0' "
            "AND is_active='1' ORDER BY recorded_on DESC LIMIT 1",
            (pid, src.ITEM_WEIGHT),
        ).fetchone()[0]
        assert float(latest_valid) == gold["body_weight"]["expected"][s.reg_no]
        # seq で絞らないと体脂肪率の行が混ざる
        mixed = {
            r[0]
            for r in conn.execute(
                "SELECT value_text FROM observations WHERE patient_no=? AND item_code=? AND is_deleted='0'",
                (pid, src.ITEM_WEIGHT),
            )
        }
        assert str(s.body_fat) in mixed
        # 削除行を除かないと最高体温が変わる
        valid_max = conn.execute(
            "SELECT MAX(value_num) FROM vital_signs WHERE patient_no=? AND item_code=? AND is_deleted='0'",
            (pid, src.ITEM_BODY_TEMP),
        ).fetchone()[0]
        all_max = conn.execute(
            "SELECT MAX(value_num) FROM vital_signs WHERE patient_no=? AND item_code=?",
            (pid, src.ITEM_BODY_TEMP),
        ).fetchone()[0]
        assert valid_max == gold["max_body_temp"]["expected"][s.reg_no] < all_max
    # 件数表は生成した行と整合する（患者の行を読まずに項目単位を作れる）
    n = conn.execute(
        "SELECT n_patients FROM item_counts WHERE source_table='observations' AND item_code=? AND seq='2'",
        (src.ITEM_WEIGHT,),
    ).fetchone()[0]
    assert n == len(SUBJECTS)
    conn.close()


def test_billing_csv_fhir_match_gold(generated: Path) -> None:
    gold = _by_link(src.build_gold())
    conn = sqlite3.connect(generated / "billing.db")
    days = dict(conn.execute("SELECT account_no, antibiotic_days FROM billing_stays").fetchall())
    conn.close()
    with (generated / "csv" / "ef.csv").open(encoding="utf-8", newline="") as fh:
        ef = list(csv.DictReader(fh))
    antibiotic_pids = {r["患者ID"] for r in ef if r["薬効分類"] == src.DRUG_CLASS_ANTIBIOTIC}
    for s in SUBJECTS:
        assert days[s.billing_id] == gold["antibiotic_days"]["expected"][s.reg_no]
        given = "1" if s.mrn_physical in antibiotic_pids else "2"
        assert given == gold["antibiotic_given"]["expected"][s.reg_no]
        obs = json.loads(
            (generated / "fhir_stub" / s.fhir_id / "Observation-pathogen.json").read_text("utf-8")
        )
        assert obs["entry"][0]["resource"]["valueString"] == gold["pathogen"]["expected"][s.reg_no]
        crosswalk = json.loads(
            (generated / "fhir_stub" / "_patient_index" / f"{s.reg_no}.json").read_text("utf-8")
        )
        ids = {i["system"]: i["value"] for i in crosswalk["entry"][0]["resource"]["identifier"]}
        assert ids == {
            e2e.SYS_TRIAL: s.reg_no,
            e2e.SYS_MRN: s.mrn_canonical,
            e2e.SYS_BILLING: s.billing_id,
            e2e.SYS_REPORT: s.report_key,
        }
    assert sum(1 for s in SUBJECTS if s.antibiotic_days == 0) >= 1  # 「なし」の患者もいる


# --------------------------------------------------------------------------- #
# indexer の ingest
# --------------------------------------------------------------------------- #


def _ingest(knowledge: Path, db: Path, out: Path) -> list[dict]:
    from kawasemi_indexer.ingestion.pipeline import ingest_connection

    result = ingest_connection(knowledge, out, dsn=f"sqlite+aiosqlite:///{db}")
    assert result.discarded_doc_rows == 0  # 定義書に実DBに無い列は無い
    rows = (out / "L2" / "column_index.jsonl").read_text("utf-8").splitlines()
    return [json.loads(ln) for ln in rows if ln.strip()]


def test_ingest_builds_column_index_for_every_gold_table(generated: Path, tmp_path: Path) -> None:
    dwh_rows = _ingest(e2e.KNOWLEDGE_DWH, generated / "dwh.db", tmp_path / "dwh")
    billing_rows = _ingest(e2e.KNOWLEDGE_BILLING, generated / "billing.db", tmp_path / "billing")
    by_conn = {e2e.CONN_DWH: dwh_rows, e2e.CONN_BILLING: billing_rows}

    for row in src.build_gold():
        unit = row["unit_id"]
        if not unit.startswith("db:"):
            continue
        table, column = unit.removeprefix("db:").split("#")[0].split(".")
        hits = [r for r in by_conn[row["connection_id"]] if r["table_id"] == table]
        assert hits, f"{unit}: 表 {table} が column_index に無い"
        col = next((r for r in hits if r["physical_name"] == column), None)
        assert col is not None, f"{unit}: 列 {column} が無い"
        assert col["logical_name"], f"{unit}: 論理名が無い（定義書で補完すること）"
        assert col["table_logical"], f"{unit}: 表論理名が無い"
        assert col["db_group"] == row["connection_id"]  # builder が connection_id に流用する
        assert any(r["is_patient_key"] for r in hits), f"{table}: 患者キーの宣言が無い"

    key = next(
        r for r in dwh_rows if r["table_id"] == "vital_signs" and r["physical_name"] == "patient_no"
    )
    assert key["is_patient_key"] and key["key_type"] == e2e.KT_MRN
    bkey = next(r for r in billing_rows if r["table_id"] == "billing_stays" and r["is_patient_key"])
    assert bkey["physical_name"] == "account_no" and bkey["key_type"] == e2e.KT_BILLING
    # マスタと件数表は「患者で絞らない」宣言を持ち、マスタの語彙は value_dictionary に入る
    for t in ("sex_codes", "lab_tests", "observation_items", "measurements", "item_counts"):
        assert all(r["patient_scoped"] is False for r in dwh_rows if r["table_id"] == t), t
    vd = [
        json.loads(ln)
        for ln in (tmp_path / "dwh" / "value_dictionary.jsonl").read_text("utf-8").splitlines()
        if ln.strip()
    ]
    labels = {(e["db_qualified"], e["physical_name"], e["code"]): e["label"] for e in vd}
    assert labels[("sex_codes", None, "1")] == "男"
    assert (
        labels[("lab_results", "test_code", src.TEST_CRP)] == "CRP"
    )  # value_domains で列へ畳み込み
    assert labels[("vital_signs", "item_code", src.ITEM_BODY_TEMP)] == "体温"


# --------------------------------------------------------------------------- #
# seed
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_seed_registers_questionnaire_users_and_dataset(db_session: AsyncSession) -> None:
    from kawasemi.ai_input.e2e import seed

    qid = await seed.seed_all(TestSessionLocal)
    assert qid == await seed.seed_all(TestSessionLocal)  # 冪等

    async with TestSessionLocal() as db:
        q = (await db.execute(select(Questionnaire).where(Questionnaire.id == qid))).scalar_one()
        assert q.url == e2e.QUESTIONNAIRE_URL
        users = {u.username: u for u in (await db.execute(select(User))).scalars()}
        assert set(seed.seed_users()) <= set(users)
        for username, (_name, roles) in seed.seed_users().items():
            role_names = {
                r.name
                for r in (
                    await db.execute(
                        select(Role).join(UserRole).where(UserRole.user_id == users[username].id)
                    )
                ).scalars()
            }
            assert role_names == set(roles)
        datasets = (
            (await db.execute(select(Dataset).where(Dataset.name == e2e.DATASET_NAME)))
            .scalars()
            .all()
        )
        assert len(datasets) == 1
        targets = (
            (await db.execute(select(InputTarget).where(InputTarget.dataset_id == datasets[0].id)))
            .scalars()
            .all()
        )
        assert {t.patient_id for t in targets} == {s.reg_no for s in SUBJECTS}
        assert all(t.status == "pending" for t in targets)


def test_subject_type_is_frozen() -> None:
    with pytest.raises(AttributeError):
        E2ESubject.__setattr__(SUBJECTS[0], "name", "x")  # type: ignore[misc]
