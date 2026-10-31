"""E2E のダミー外部システムを、コードだけから決定的に生成する。

生成物（`var/e2e/`。gitignore）:

- `dwh.db`（接続 `dwh`）: 横持ちの `patients` / `admissions` / `infection_cases`、
  `item_code` 縦持ちの `vital_signs`（`value_num`）/ `observations`（`value_text`・`seq`）、`lab_results`（`test_code` →
  `lab_tests`）、マスタ（`sex_codes` / `lab_tests` / `observation_items` / `measurements`）と件数表 `item_counts`
- `billing.db`（接続 `billing`）: `billing_stays` / `billing_drugs`
- `reports/{report_key}/`: `admission_summary.txt`（入院時サマリ）と `consent.pdf`（手書き同意書）
- `fhir_stub/`: `_patient_index/{登録番号}.json`（crosswalk）と `{fhir_id}/Observation-*.json`
- `csv/ef.csv`: DPC EF 相当（全患者横断・患者ID列は mrn の10桁ゼロ詰め）

正解（`examples/e2e/gold.jsonl`。追跡する）も同じ `SUBJECTS` から `build_gold` が作る。
生成器と gold が同じ乱数を見ているので、データと期待値は構造的に食い違わない。

**DWH の行は「有効な行」だけが真の値である。** `vital_signs` と `observations` には `is_deleted='1'`
の行（削除済みの誤入力）を混ぜてあり、その値は真の値より大きい。絞り込みを落とすと
期待値と合わなくなるので、関門 (d) が「有効行の条件を踏んだか」を値で検出できる。
"""

from __future__ import annotations

import csv
import json
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from kawasemi.ai_input.e2e import (
    BILLING_DB,
    CONN_BILLING,
    CONN_CSV,
    CONN_DWH,
    CONN_FHIR,
    CONN_FILES,
    CSV_DIR,
    DPC_EF_CSV,
    E2E_STUDY_ID,
    FHIR_STUB_DIR,
    GENERATED_DIR,
    GOLD_JSONL,
    REPORTS_DIR,
    SUBJECTS,
    SYS_BILLING,
    SYS_MRN,
    SYS_REPORT,
    SYS_TRIAL,
    WAREHOUSE_DB,
    E2ESubject,
)
from kawasemi.ai_input.e2e.fhir_bundle import bundle
from kawasemi.ai_input.e2e.handwriting import make_handwriting_pdf
from kawasemi.core.logging_config import get_logger

logger = get_logger(__name__)

# --- 観察項目（observation_items / measurements）----------------------------------------------
# (item_code, 項目名, 測定コード)。vital_signs は value_num に数値、observations は value_text に文字列で入る。
ITEM_BODY_TEMP = "body_temp"
ITEM_PULSE = "pulse"
ITEM_SPO2 = "spo2"
ITEM_WEIGHT = "weight"  # 体重測定。seq 1 体重 kg と seq 2 体脂肪率 % が同居する
ITEM_COUGH = "cough"
_VITAL_ITEMS: list[tuple[str, str, str]] = [
    (ITEM_BODY_TEMP, "体温", "m_body_temp"),
    (ITEM_PULSE, "脈拍", "m_pulse"),
    (ITEM_SPO2, "SpO2", "m_spo2"),
]
_OBS_ITEMS: list[tuple[str, str, str]] = [
    (ITEM_WEIGHT, "体重測定", "m_weight"),
    (ITEM_COUGH, "咳嗽", "m_cough"),
]
# (測定コード, seq, 測定名, 単位)
_MEASUREMENTS: list[tuple[str, str, str, str]] = [
    ("m_body_temp", "1", "体温", "℃"),
    ("m_pulse", "1", "脈拍", "回/分"),
    ("m_spo2", "1", "SpO2", "%"),
    ("m_weight", "1", "体重", "kg"),
    ("m_weight", "2", "体脂肪率", "%"),
    ("m_cough", "1", "咳嗽", ""),
]
# 検査マスタ (test_code, 検査名, 単位)
TEST_CRP = "crp"
TEST_WBC = "wbc"
_LAB_MASTER: list[tuple[str, str, str]] = [
    (TEST_CRP, "CRP", "mg/dL"),
    (TEST_WBC, "白血球数", "/μL"),
    ("pct", "プロカルシトニン", "ng/mL"),
]

# EF の薬効分類（架空の分類コード。実在の分類は写さない）。ABX=抗菌薬、APY=解熱鎮痛薬
DRUG_CLASS_ANTIBIOTIC = "ABX"
DRUG_CLASS_ANTIPYRETIC = "APY"
_EF_HEADER = ["患者ID", "入院ID", "薬剤コード", "薬効分類", "実施回数", "点数"]


def _iso(d: date) -> str:
    return d.isoformat()


def _jp(d: date) -> str:
    return f"{d.year}年{d.month}月{d.day}日"


# =============================================================================
# dwh.db
# =============================================================================

_DWH_SCHEMA = """
CREATE TABLE patients (
    patient_no      TEXT PRIMARY KEY,
    full_name    TEXT,
    birth_date TEXT,
    sex_code     TEXT
);
CREATE TABLE admissions (
    patient_no      TEXT,
    admission_no    INTEGER,
    admitted_on   TEXT,
    discharged_on TEXT,
    ward_code    TEXT,
    PRIMARY KEY (patient_no, admission_no)
);
CREATE TABLE infection_cases (
    patient_no         TEXT,
    case_no      INTEGER,
    onset_date    TEXT,
    specimen_date TEXT,
    is_reportable    TEXT,
    PRIMARY KEY (patient_no, case_no)
);
CREATE TABLE vital_signs (
    patient_no   TEXT,
    item_code   TEXT,
    measured_at    TEXT,
    value_num     REAL,
    is_deleted TEXT
);
CREATE TABLE observations (
    patient_no        TEXT,
    item_code        TEXT,
    measure_code       TEXT,
    seq      TEXT,
    recorded_on TEXT,
    value_text          TEXT,
    is_deleted      TEXT,
    is_active   TEXT
);
CREATE TABLE lab_results (
    patient_no      TEXT,
    test_code    TEXT,
    tested_on  TEXT,
    result_value REAL
);
CREATE TABLE sex_codes (
    sex_code   TEXT PRIMARY KEY,
    label TEXT
);
CREATE TABLE lab_tests (
    test_code   TEXT PRIMARY KEY,
    test_name TEXT,
    unit      TEXT
);
CREATE TABLE observation_items (
    item_code      TEXT PRIMARY KEY,
    item_name TEXT,
    measure_code     TEXT
);
CREATE TABLE measurements (
    measure_code   TEXT,
    seq  TEXT,
    measure_name TEXT,
    unit     TEXT,
    PRIMARY KEY (measure_code, seq)
);
CREATE TABLE item_counts (
    source_table TEXT,
    item_code      TEXT,
    measure_code     TEXT,
    seq    TEXT,
    is_deleted    TEXT,
    is_active TEXT,
    n_rows     INTEGER,
    n_patients INTEGER,
    n_numeric  INTEGER
);
"""


def _vital_rows(s: E2ESubject) -> list[tuple[str, str, str, float, str]]:
    """1患者分の vital_signs。体温は有効行の最大が真の値で、削除行にはそれより高い値を置く。"""
    pid = s.mrn_physical
    base = s.admission_date
    rows: list[tuple[str, str, str, float, str]] = []
    # 体温: 3 測定。最大値は入院翌日の朝に置く（並び順に依存しない「最大」が真の値）
    temps = [round(s.max_body_temp - 0.8, 1), s.max_body_temp, round(s.max_body_temp - 1.3, 1)]
    for k, t in enumerate(temps):
        dttm = f"{_iso(base + timedelta(days=k))} 06:00:00"
        rows.append((pid, ITEM_BODY_TEMP, dttm, t, "0"))
    # 削除済みの誤入力（真の値より高い）。is_deleted='0' で絞らないと最高体温がこれになる
    rows.append(
        (pid, ITEM_BODY_TEMP, f"{_iso(base)} 06:05:00", round(s.max_body_temp + 1.5, 1), "1")
    )
    # 脈拍・SpO2（様式には無い。索引と検索の雑音として置く）
    pulse = 70 + (int(s.mrn_canonical) % 40)
    rows.append((pid, ITEM_PULSE, f"{_iso(base)} 06:00:00", float(pulse), "0"))
    rows.append((pid, ITEM_SPO2, f"{_iso(base)} 06:00:00", float(93 + int(s.reg_no[-1]) % 6), "0"))
    return rows


def _obs_rows(s: E2ESubject) -> list[tuple[str, str, str, str, str, str, str, str]]:
    """1患者分の observations。体重（seq 1）は最新が真の値、体脂肪率（seq 2）は同じ item_code に同居する。"""
    pid = s.mrn_physical
    base = s.admission_date
    prev_weight = round(s.body_weight + 1.2, 1)
    rows = [
        # 前回測定（入院2日前。最新ではない）
        (
            pid,
            ITEM_WEIGHT,
            "m_weight",
            "1",
            _iso(base - timedelta(days=2)),
            f"{prev_weight}",
            "0",
            "1",
        ),
        # 入院時測定（最新＝真の値）
        (pid, ITEM_WEIGHT, "m_weight", "1", _iso(base), f"{s.body_weight}", "0", "1"),
        # 体脂肪率（seq 2）。item_code だけで絞ると体重の欄にこれが混ざる。**記録日を入院翌日に
        # 置く**。入院日と同じ日付だと「最新」の並びで同順位になり、seq の
        # 絞り込みを落としても行の挿入順で体重が採れてしまい、関門 (d) が欠陥を見逃した
        (
            pid,
            ITEM_WEIGHT,
            "m_weight",
            "2",
            _iso(base + timedelta(days=1)),
            f"{s.body_fat}",
            "0",
            "1",
        ),
        # 削除済みの誤入力（桁違い）
        (
            pid,
            ITEM_WEIGHT,
            "m_weight",
            "1",
            _iso(base),
            f"{round(s.body_weight * 10, 1)}",
            "1",
            "1",
        ),
        # 咳嗽（観察項目。文字列）
        (
            pid,
            ITEM_COUGH,
            "m_cough",
            "1",
            _iso(base),
            "あり" if s.severity != "1" else "なし",
            "0",
            "1",
        ),
    ]
    return rows


def _lab_rows(s: E2ESubject) -> list[tuple[str, str, str, float]]:
    """1患者分の lab_results。CRP は入院日と3日目の2行（真の値は最初の行）。"""
    pid = s.mrn_physical
    base = s.admission_date
    return [
        (pid, TEST_CRP, _iso(base), s.crp_on_admission),
        (pid, TEST_CRP, _iso(base + timedelta(days=3)), s.crp_day3),
        (pid, TEST_WBC, _iso(base), float(s.wbc_on_admission)),
    ]


def _item_count_rows(
    vital: list[tuple[str, str, str, float, str]],
    obs: list[tuple[str, str, str, str, str, str, str, str]],
) -> list[tuple[str, str, str, str, str, str, int, int, int]]:
    """項目×測定×フラグ別の件数表を、生成した行から数える。

    患者データの行を読まずに項目単位を作れるようにするためのもの（患者数・数値率）。
    """
    agg: dict[tuple[str, str, str, str, str, str], dict[str, Any]] = {}
    for pid, icode, _dttm, num, del_flg in vital:
        key = ("vital_signs", icode, "", "", del_flg, "")
        g = agg.setdefault(key, {"rows": 0, "pts": set(), "num": 0})
        g["rows"] += 1
        g["pts"].add(pid)
        g["num"] += 1 if num is not None else 0
    for pid, icode, sok_cd, sok_seq, _d, kek, del_flg, active in obs:
        key = ("observations", icode, sok_cd, sok_seq, del_flg, active)
        g = agg.setdefault(key, {"rows": 0, "pts": set(), "num": 0})
        g["rows"] += 1
        g["pts"].add(pid)
        try:
            float(kek)
            g["num"] += 1
        except ValueError:
            pass
    return [
        (t, i, sc, ss, d, a, g["rows"], len(g["pts"]), g["num"])
        for (t, i, sc, ss, d, a), g in sorted(agg.items())
    ]


def build_dwh(subjects: list[E2ESubject] = SUBJECTS, out_db: Path = WAREHOUSE_DB) -> None:
    """dwh.db を生成する（既存は作り直す）。"""
    out_db.parent.mkdir(parents=True, exist_ok=True)
    if out_db.exists():
        out_db.unlink()
    conn = sqlite3.connect(str(out_db))
    try:
        conn.executescript(_DWH_SCHEMA)
        conn.executemany("INSERT INTO sex_codes VALUES (?, ?)", [("1", "男"), ("2", "女")])
        conn.executemany("INSERT INTO lab_tests VALUES (?, ?, ?)", _LAB_MASTER)
        conn.executemany(
            "INSERT INTO observation_items VALUES (?, ?, ?)", _VITAL_ITEMS + _OBS_ITEMS
        )
        conn.executemany("INSERT INTO measurements VALUES (?, ?, ?, ?)", _MEASUREMENTS)

        vital_all: list[tuple[str, str, str, float, str]] = []
        obs_all: list[tuple[str, str, str, str, str, str, str, str]] = []
        for s in subjects:
            pid = s.mrn_physical
            conn.execute(
                "INSERT INTO patients VALUES (?, ?, ?, ?)",
                (pid, s.name, _iso(s.birth_date), s.sex_code),
            )
            conn.execute(
                "INSERT INTO admissions VALUES (?, ?, ?, ?, ?)",
                (
                    pid,
                    1,
                    _iso(s.admission_date),
                    _iso(s.admission_date + timedelta(days=s.los_days)),
                    f"W{int(s.reg_no[-1]) % 3 + 1}",
                ),
            )
            conn.execute(
                "INSERT INTO infection_cases VALUES (?, ?, ?, ?, ?)",
                (
                    pid,
                    1,
                    _iso(s.onset_date) if s.onset_in_dwh else None,
                    _iso(s.admission_date),
                    "1",
                ),
            )
            vital_all.extend(_vital_rows(s))
            obs_all.extend(_obs_rows(s))
            conn.executemany("INSERT INTO lab_results VALUES (?, ?, ?, ?)", _lab_rows(s))
        conn.executemany("INSERT INTO vital_signs VALUES (?, ?, ?, ?, ?)", vital_all)
        conn.executemany("INSERT INTO observations VALUES (?, ?, ?, ?, ?, ?, ?, ?)", obs_all)
        conn.executemany(
            "INSERT INTO item_counts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _item_count_rows(vital_all, obs_all),
        )
        conn.commit()
    finally:
        conn.close()
    logger.info(f"dwh.db を生成: {out_db}（{len(subjects)}患者）")


# =============================================================================
# billing.db（第2接続）
# =============================================================================

_BILLING_SCHEMA = """
CREATE TABLE billing_stays (
    account_no      TEXT,
    admission_no         INTEGER,
    admitted_on        TEXT,
    length_of_stay        INTEGER,
    antibiotic_days INTEGER,
    total_points    INTEGER,
    PRIMARY KEY (account_no, admission_no)
);
CREATE TABLE billing_drugs (
    account_no TEXT,
    admission_no    INTEGER,
    drug_code    TEXT,
    drug_name  TEXT,
    drug_class   TEXT,
    days       INTEGER,
    points     INTEGER
);
"""


def _drug_rows(s: E2ESubject) -> list[tuple[str, int, str, str, str, int, int]]:
    """1患者分の薬剤行。抗菌薬（薬効分類 ABX）は投与日数 > 0 の患者だけが持つ。"""
    rows: list[tuple[str, int, str, str, str, int, int]] = []
    if s.antibiotic_days > 0:
        rows.append(
            (
                s.billing_id,
                1,
                "D0001",
                "セフトリアキソン注",
                DRUG_CLASS_ANTIBIOTIC,
                s.antibiotic_days,
                s.antibiotic_days * 180,
            )
        )
    rows.append((s.billing_id, 1, "D0002", "アセトアミノフェン錠", DRUG_CLASS_ANTIPYRETIC, 3, 60))
    return rows


def build_billing(subjects: list[E2ESubject] = SUBJECTS, out_db: Path = BILLING_DB) -> None:
    """billing.db を生成する（既存は作り直す）。"""
    out_db.parent.mkdir(parents=True, exist_ok=True)
    if out_db.exists():
        out_db.unlink()
    conn = sqlite3.connect(str(out_db))
    try:
        conn.executescript(_BILLING_SCHEMA)
        for s in subjects:
            drugs = _drug_rows(s)
            conn.execute(
                "INSERT INTO billing_stays VALUES (?, ?, ?, ?, ?, ?)",
                (
                    s.billing_id,
                    1,
                    _iso(s.admission_date),
                    s.los_days,
                    s.antibiotic_days,
                    s.los_days * 2500 + sum(r[6] for r in drugs),
                ),
            )
            conn.executemany("INSERT INTO billing_drugs VALUES (?, ?, ?, ?, ?, ?, ?)", drugs)
        conn.commit()
    finally:
        conn.close()
    logger.info(f"billing.db を生成: {out_db}")


# =============================================================================
# reports/（入院時サマリ txt ＋ 手書き同意書 PDF）
# =============================================================================


def admission_summary_text(s: E2ESubject) -> str:
    """入院時サマリの本文。診断名と発症日は**ここにしかない**（LLM 読みとフォールバックの素）。"""
    return (
        "【入院時サマリ】\n"
        f"氏名: {s.name}\n"
        f"入院日: {_jp(s.admission_date)}\n"
        "主訴: 発熱\n"
        f"現病歴: {_jp(s.onset_date)}より発熱が出現し、改善しないため当院を受診した。\n"
        f"診断: {s.diagnosis}\n"
        "方針: 培養検体を採取のうえ経験的抗菌薬治療を検討する。\n"
    )


def consent_lines(s: E2ESubject) -> list[str]:
    """手書き同意書の各行（PDF に描く文字そのもの）。

    台本 LLM の Vision 応答（文字起こし）もここから作る。描いた文字と
    読み取った文字が別の手で書かれないようにする。
    """
    d = s.consent_date
    return [
        "研究参加 同意書",
        "",
        f"試験名 : 感染症発生報告研究 ({E2E_STUDY_ID})",
        f"登録番号 : {s.reg_no}",
        f"被験者氏名 : {s.name}",
        "",
        "私は本研究について十分な説明を受け、内容を",
        "理解した上で、自由意思により参加に同意します。",
        "",
        f"同意日 : {d.year}年 {d.month}月 {d.day}日",
        f"被験者署名 : {s.name}",
        "説明医師署名 : 川瀬 太郎",
    ]


def build_reports(subjects: list[E2ESubject] = SUBJECTS, out_dir: Path = REPORTS_DIR) -> None:
    """全患者分の入院時サマリと手書き同意書を reports/{report_key}/ に書き出す。"""
    for s in subjects:
        target = out_dir / s.report_key
        target.mkdir(parents=True, exist_ok=True)
        (target / "admission_summary.txt").write_text(admission_summary_text(s), encoding="utf-8")
        make_handwriting_pdf(consent_lines(s), target / "consent.pdf")
    logger.info(f"reports: {len(subjects)}患者分の入院時サマリと同意書を書き出し: {out_dir}")


# =============================================================================
# fhir_stub/（Patient crosswalk ＋ Observation）
# =============================================================================

FHIR_CODE_PATHOGEN = "pathogen"
FHIR_CODE_SEVERITY = "severity"
FHIR_CODE_SPECIMEN_DATE = "specimen-date"


def _observation(s: E2ESubject, code: str, label: str, value: str) -> dict[str, Any]:
    return {
        "resourceType": "Observation",
        "id": f"obs-{code}-{s.fhir_id}",
        "status": "final",
        "subject": {"reference": f"Patient/{s.fhir_id}"},
        "code": {"text": label, "coding": [{"code": code}]},
        "valueString": value,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def build_fhir_stub(subjects: list[E2ESubject] = SUBJECTS, out_dir: Path = FHIR_STUB_DIR) -> None:
    """crosswalk（Patient.identifier）と Observation を書き出す。

    crosswalk は登録番号→mrn（canonical）/ billing_id / report_key を載せ、`Patient.id` が
    fhir_patient_id になる。名寄せの値はここ（外部システム側）にだけある。
    """
    idx_dir = out_dir / "_patient_index"
    idx_dir.mkdir(parents=True, exist_ok=True)
    for s in subjects:
        patient = {
            "resourceType": "Patient",
            "id": s.fhir_id,
            "identifier": [
                {"system": SYS_TRIAL, "value": s.reg_no},
                {"system": SYS_MRN, "value": s.mrn_canonical},
                {"system": SYS_BILLING, "value": s.billing_id},
                {"system": SYS_REPORT, "value": s.report_key},
            ],
        }
        _write_json(idx_dir / f"{s.reg_no}.json", bundle([patient]))
        target = out_dir / s.fhir_id
        target.mkdir(parents=True, exist_ok=True)
        obs = [
            (FHIR_CODE_PATHOGEN, "起因微生物", s.pathogen),
            (FHIR_CODE_SEVERITY, "重症度分類", s.severity),
            (FHIR_CODE_SPECIMEN_DATE, "検体採取日", _iso(s.admission_date)),
        ]
        for code, label, value in obs:
            _write_json(
                target / f"Observation-{code}.json", bundle([_observation(s, code, label, value)])
            )
    logger.info(
        f"fhir_stub: {len(subjects)}患者分の crosswalk と Observation を書き出し: {out_dir}"
    )


# =============================================================================
# csv/ef.csv（DPC EF 相当）
# =============================================================================


def build_dpc_ef_csv(subjects: list[E2ESubject] = SUBJECTS, out_csv: Path = DPC_EF_CSV) -> None:
    """全患者横断の EF ファイルを生成する。抗菌薬（薬効分類 ABX）の行の有無が投与の有無になる。"""
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    rows: list[tuple[str, str, str, str, str, str]] = []
    for s in subjects:
        pid = s.mrn_physical
        adm = f"AD{s.reg_no[-4:]}"
        if s.antibiotic_days > 0:
            rows.append((pid, adm, "D0001", DRUG_CLASS_ANTIBIOTIC, str(s.antibiotic_days), "180"))
        rows.append((pid, adm, "D0002", DRUG_CLASS_ANTIPYRETIC, "3", "20"))
    with out_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(_EF_HEADER)
        writer.writerows(rows)
    logger.info(f"dpc_ef: {out_csv} に EF {len(rows)}行を生成")


# =============================================================================
# gold.jsonl（項目ごとの期待単位と期待値）
# =============================================================================

ROUTE_DB = "db"
ROUTE_FILE = "file"
ROUTE_FHIR = "fhir"
ROUTE_CSV = "csv"
ROUTE_LLM = "llm"
ROUTE_FALLBACK = "fallback"

UNIT_ADMISSION_SUMMARY = "file:patient_data:入院時サマリ"
UNIT_CONSENT_PDF = "file:patient_data:研究参加同意書（手書き）"
UNIT_DPC_EF = "file:patient_data:DPC出来高EFファイル"


def _per_patient(subjects: list[E2ESubject], fn: Any) -> dict[str, Any]:
    return {s.reg_no: fn(s) for s in subjects}


def build_gold(subjects: list[E2ESubject] = SUBJECTS) -> list[dict[str, Any]]:
    """項目ごとの期待単位・経路・患者ごとの期待値を組む（gold.jsonl の1行＝1項目）。

    `unit_id` は indexer の builder が付ける形（`db:<表>.<列>` / `file:patient_data:<label>` /
    `fhir:<型>:<code>`）。観察項目の単位は `db:<表>.<列>#<item_code>`（`kawasemi_indexer.item_units` の形）。
    seq で分かれる単位の命名は indexer 側で決めるので、ここは `row_filters` に `seq` を書き、
    判定側は `unit_id` の前方一致で見る。

    値の型は様式の item.type に従う（date → ISO 文字列、decimal → 数値、choice → コード文字列）。
    """
    return [
        {
            "link_id": "birth_date",
            "route": ROUTE_DB,
            "connection_id": CONN_DWH,
            "unit_id": "db:patients.birth_date",
            "expected": _per_patient(subjects, lambda s: _iso(s.birth_date)),
        },
        {
            "link_id": "sex",
            "route": ROUTE_DB,
            "connection_id": CONN_DWH,
            "unit_id": "db:patients.sex_code",
            "alt_unit_ids": [f"master:{CONN_DWH}:sex_codes:1", f"master:{CONN_DWH}:sex_codes:2"],
            "expected": _per_patient(subjects, lambda s: s.sex_code),
        },
        {
            "link_id": "admission_date",
            "route": ROUTE_DB,
            "connection_id": CONN_DWH,
            "unit_id": "db:admissions.admitted_on",
            "expected": _per_patient(subjects, lambda s: _iso(s.admission_date)),
        },
        {
            "link_id": "max_body_temp",
            "route": ROUTE_DB,
            "connection_id": CONN_DWH,
            "unit_id": f"db:vital_signs.value_num#{ITEM_BODY_TEMP}",
            "row_filters": [
                {"column": "item_code", "equals": ITEM_BODY_TEMP},
                {"column": "is_deleted", "equals": "0"},
            ],
            "aggregate": "max",
            "expected": _per_patient(subjects, lambda s: s.max_body_temp),
        },
        {
            "link_id": "body_weight",
            "route": ROUTE_DB,
            "connection_id": CONN_DWH,
            "unit_id": f"db:observations.value_text#{ITEM_WEIGHT}",
            "row_filters": [
                {"column": "item_code", "equals": ITEM_WEIGHT},
                {"column": "seq", "equals": "1"},
                {"column": "is_deleted", "equals": "0"},
                {"column": "is_active", "equals": "1"},
            ],
            "select": "latest",
            "order_column": "recorded_on",
            "expected": _per_patient(subjects, lambda s: s.body_weight),
        },
        {
            "link_id": "crp_on_admission",
            "route": ROUTE_DB,
            "connection_id": CONN_DWH,
            "unit_id": "db:lab_results.result_value",
            "alt_unit_ids": [f"master:{CONN_DWH}:lab_tests:{TEST_CRP}"],
            "row_filters": [{"column": "test_code", "equals": TEST_CRP}],
            "select": "earliest",
            "order_column": "tested_on",
            "expected": _per_patient(subjects, lambda s: s.crp_on_admission),
        },
        {
            "link_id": "antibiotic_days",
            "route": ROUTE_DB,
            "connection_id": CONN_BILLING,
            "unit_id": "db:billing_stays.antibiotic_days",
            "expected": _per_patient(subjects, lambda s: s.antibiotic_days),
        },
        {
            "link_id": "diagnosis_text",
            "route": ROUTE_LLM,
            "connection_id": CONN_FILES,
            "unit_id": UNIT_ADMISSION_SUMMARY,
            "expected": _per_patient(subjects, lambda s: s.diagnosis),
        },
        {
            "link_id": "onset_date",
            "route": ROUTE_FALLBACK,
            "connection_id": CONN_DWH,
            "unit_id": "db:infection_cases.onset_date",
            "fallback_unit_id": UNIT_ADMISSION_SUMMARY,
            "fallback_connection_id": CONN_FILES,
            "expected": _per_patient(subjects, lambda s: _iso(s.onset_date)),
            # 患者ごとに、どちらの経路で値が決まるか（db=DWH の列 / text=本文の LLM 読み）
            "expected_source": _per_patient(subjects, lambda s: "db" if s.onset_in_dwh else "text"),
        },
        {
            "link_id": "consent_date",
            "route": ROUTE_FILE,
            "connection_id": CONN_FILES,
            "unit_id": UNIT_CONSENT_PDF,
            "expected": _per_patient(subjects, lambda s: _iso(s.consent_date)),
        },
        {
            "link_id": "pathogen",
            "route": ROUTE_FHIR,
            "connection_id": CONN_FHIR,
            "unit_id": f"fhir:Observation:{FHIR_CODE_PATHOGEN}",
            "expected": _per_patient(subjects, lambda s: s.pathogen),
        },
        {
            "link_id": "severity",
            "route": ROUTE_FHIR,
            "connection_id": CONN_FHIR,
            "unit_id": f"fhir:Observation:{FHIR_CODE_SEVERITY}",
            "expected": _per_patient(subjects, lambda s: s.severity),
        },
        {
            "link_id": "antibiotic_given",
            "route": ROUTE_CSV,
            "connection_id": CONN_CSV,
            "unit_id": UNIT_DPC_EF,
            "csv": {
                "key_column": "患者ID",
                "match": [{"column": "薬効分類", "value": DRUG_CLASS_ANTIBIOTIC}],
            },
            "expected": _per_patient(subjects, lambda s: s.antibiotic_given),
        },
    ]


def write_gold(subjects: list[E2ESubject] = SUBJECTS, out_path: Path = GOLD_JSONL) -> None:
    """gold.jsonl を書き出す（1行1項目。キー順は build_gold の記述順）。"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as fh:
        for row in build_gold(subjects):
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    logger.info(f"gold: {out_path} に {len(build_gold(subjects))}項目を書き出し")


def read_gold(path: Path = GOLD_JSONL) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


# =============================================================================
# 一括生成
# =============================================================================


def generate_all(
    out_root: Path = GENERATED_DIR,
    *,
    subjects: list[E2ESubject] = SUBJECTS,
    gold_path: Path | None = GOLD_JSONL,
) -> None:
    """全生成物を out_root 配下へ作る。gold_path を渡せば gold.jsonl も書き直す。"""
    build_dwh(subjects, out_root / WAREHOUSE_DB.name)
    build_billing(subjects, out_root / BILLING_DB.name)
    build_reports(subjects, out_root / REPORTS_DIR.name)
    build_fhir_stub(subjects, out_root / FHIR_STUB_DIR.name)
    build_dpc_ef_csv(subjects, out_root / CSV_DIR.name / DPC_EF_CSV.name)
    if gold_path is not None:
        write_gold(subjects, gold_path)


if __name__ == "__main__":
    generate_all()
