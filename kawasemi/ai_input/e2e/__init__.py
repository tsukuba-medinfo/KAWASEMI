"""E2E 試験環境: 散らばったダミーデータを索引で探し出して取得するまでを1本で通す。

**コードだけから決定的に生成する。** 乱数の seed を固定して患者 10 名分を作るので、
CI でも同じデータができる。期待値（`examples/e2e/gold.jsonl`）も同じ乱数から作るので、
データと正解が食い違わない。

散らばり方は実環境の形を写す:

- 横持ちの表（`patients` / `admissions` / `infection_cases`）
- `item_code` で縦持ちの観察記録（`vital_signs.value_num` / `observations.value_text`。測定 seq 付き。`weight` 体重測定は
  seq 1 体重と seq 2 体脂肪率が同居する）
- マスタ結合が要る検査結果（`lab_results.test_code` → `lab_tests`）
- 第2接続の DB（医事会計 `billing.db`）
- ファイル共有のレポート（入院時サマリ txt / 手書き同意書 PDF）
- FHIR スタブ（Patient crosswalk ＋ Observation）
- CSV（DPC EF 相当）

各システムの患者キーは患者ごとに異なる（DWH=mrn〔10桁ゼロ詰め〕 / 医事=billing_id /
reports=report_key / FHIR=fhir_patient_id）。crosswalk は FHIR Patient.identifier で、
resolver が登録番号→各IDを実行時に解決する。

様式は**架空の感染症様式**（`examples/e2e/questionnaire_e2e_infection.json`）。実在の様式は写さない。
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path

__all__ = [
    "E2E_SEED",
    "E2E_STUDY_ID",
    "DATASET_NAME",
    "DATASET_DESCRIPTION",
    "E2E_DIR",
    "GENERATED_DIR",
    "WAREHOUSE_DB",
    "BILLING_DB",
    "REPORTS_DIR",
    "FHIR_STUB_DIR",
    "CSV_DIR",
    "DPC_EF_CSV",
    "QUESTIONNAIRE_JSON",
    "QUESTIONNAIRE_URL",
    "KNOWLEDGE_DWH",
    "KNOWLEDGE_BILLING",
    "FILE_MANIFEST",
    "FHIR_DICTIONARY",
    "REFERENCES_DIR",
    "REFERENCE_SCOPES",
    "GOLD_JSONL",
    "CONNECTIONS_E2E",
    "KEY_TYPES_E2E",
    "CONNECTIONS_RUNTIME",
    "KEY_TYPES_RUNTIME",
    "CONN_DWH",
    "CONN_BILLING",
    "CONN_FHIR",
    "CONN_FILES",
    "CONN_CSV",
    "KT_TRIAL",
    "KT_MRN",
    "KT_BILLING",
    "KT_REPORT",
    "KT_FHIR",
    "SYS_TRIAL",
    "SYS_MRN",
    "SYS_BILLING",
    "SYS_REPORT",
    "FHIR_STUB_PORT",
    "APP_PORT",
    "DWH_DSN",
    "BILLING_DSN",
    "E2ESubject",
    "SUBJECTS",
    "make_subjects",
]

# --- 乱数と識別 ----------------------------------------------------------------
E2E_SEED = 20261022  # 固定。変えると gold.jsonl も変わる（追跡ファイルを作り直すこと）
E2E_STUDY_ID = "E2E-INFECTION"  # Subject.study_id
DATASET_NAME = "E2E 感染症発生報告（架空）"
DATASET_DESCRIPTION = (
    "E2E 試験環境のデータセット。架空の感染症様式 14 項目を、DWH（2接続）・ファイル・FHIR・"
    "CSV に散らばったダミーデータから取得する。患者 10 名、全員が確定待ち。"
)

# --- 固定資産（追跡する）と生成物（gitignore）---------------------------------------
E2E_DIR = Path("examples/e2e")
GENERATED_DIR = Path("var/e2e")
WAREHOUSE_DB = GENERATED_DIR / "dwh.db"
BILLING_DB = GENERATED_DIR / "billing.db"
REPORTS_DIR = GENERATED_DIR / "reports"
FHIR_STUB_DIR = GENERATED_DIR / "fhir_stub"
CSV_DIR = GENERATED_DIR / "csv"
DPC_EF_CSV = CSV_DIR / "ef.csv"

QUESTIONNAIRE_JSON = E2E_DIR / "questionnaire_e2e_infection.json"
QUESTIONNAIRE_URL = "http://kawasemi.example/Questionnaire/e2e-infection"
KNOWLEDGE_DWH = E2E_DIR / "knowledge.yaml"  # ingest は1ファイル＝1接続
KNOWLEDGE_BILLING = E2E_DIR / "knowledge.billing.yaml"
FILE_MANIFEST = E2E_DIR / "file_manifest.jsonl"
FHIR_DICTIONARY = E2E_DIR / "fhir_dictionary.jsonl"
REFERENCES_DIR = E2E_DIR / "references"
REFERENCE_SCOPES = E2E_DIR / "reference_scopes.yaml"
GOLD_JSONL = E2E_DIR / "gold.jsonl"
CONNECTIONS_E2E = E2E_DIR / "connections.e2e.yaml"
KEY_TYPES_E2E = E2E_DIR / "key_types.e2e.yaml"
# 実行時に読まれる配置先（生成物の側に置くので追跡しない）
CONNECTIONS_RUNTIME = GENERATED_DIR / "connections.yaml"
KEY_TYPES_RUNTIME = GENERATED_DIR / "key_types.yaml"

# --- 接続 ID（connections.e2e.yaml と一致させる）-----------------------------------
CONN_DWH = "dwh"
CONN_BILLING = "billing"
CONN_FHIR = "hosp_fhir"
CONN_FILES = "report_fs"
CONN_CSV = "dpc_ef"

# --- key_type 名と identifier.system（key_types.e2e.yaml と一致させる）--------------
KT_TRIAL = "trial_subject_no"
KT_MRN = "mrn"
KT_BILLING = "billing_id"
KT_REPORT = "report_key"
KT_FHIR = "fhir_patient_id"
SYS_TRIAL = "urn:kawasemi:e2e:trial"
SYS_MRN = "urn:kawasemi:e2e:mrn"
SYS_BILLING = "urn:kawasemi:e2e:billing"
SYS_REPORT = "urn:kawasemi:e2e:report"

# FHIR スタブの待受ポート（`kawasemi e2e serve` が起動する）
FHIR_STUB_PORT = 8084
# `kawasemi e2e serve` のアプリの既定ポート。`kawasemi serve` の既定 8000 など、
# 開発でよく使うポートと並行起動できる値にしてある
APP_PORT = 8030

# 外部DB の DSN（`e2e setup` が環境変数 DWH_DSN / BILLING_DSN に設定する）
DWH_DSN = f"sqlite+aiosqlite:///{WAREHOUSE_DB}"
BILLING_DSN = f"sqlite+aiosqlite:///{BILLING_DB}"


@dataclass(frozen=True)
class E2ESubject:
    """E2E の1患者。各システムの値を1か所に持ち、生成物と gold の両方をここから作る。"""

    reg_no: str  # 登録番号（Hub・anchor）
    mrn_canonical: str  # DWH の患者キー（ゼロ詰め除去）
    billing_id: str  # 医事会計の患者キー
    name: str
    birth_date: date
    sex_code: str  # 1=男 / 2=女（sex_codes）
    admission_date: date
    onset_date: date  # 発症日（真の値）
    onset_in_dwh: bool  # False なら DWH の onset_date が NULL（本文からの読み取りへ落ちる）
    diagnosis: str  # 感染症診断名（入院時サマリの本文にだけある）
    pathogen: str  # 起因微生物（FHIR Observation）
    severity: str  # 重症度 1=軽症 / 2=中等症 / 3=重症（FHIR Observation）
    max_body_temp: float  # 発症後の最高体温（vital_signs.value_num の body_temp。有効行の最大）
    body_weight: float  # 入院時体重（observations.value_text, item_code=weight, seq=1, 最新）
    body_fat: float  # 体脂肪率（同じ item_code の seq 2。取り違えを検出するための同居値）
    crp_on_admission: float  # 入院時 CRP（lab_results, test_code=crp の最初の行）
    crp_day3: float
    wbc_on_admission: int
    antibiotic_days: int  # 抗菌薬投与日数（billing。0 なら投与なし）
    los_days: int  # 在院日数
    consent_date: date  # 同意取得日（手書き PDF にだけある）

    @property
    def mrn_physical(self) -> str:
        """DWH 格納形（10桁ゼロ詰め＝physical_format {pad:10}）。"""
        return self.mrn_canonical.zfill(10)

    @property
    def report_key(self) -> str:
        return f"RPT-E2E-{self.reg_no[-4:]}"

    @property
    def fhir_id(self) -> str:
        return f"fhirpat-e2e-{self.reg_no[-4:]}"

    @property
    def antibiotic_given(self) -> str:
        """抗菌薬投与の有無 1=あり / 2=なし（CSV の薬効分類 ABX の行の有無で決まる）。"""
        return "1" if self.antibiotic_days > 0 else "2"


_NAMES = [
    ("相川 一郎", "1"),
    ("井上 花子", "2"),
    ("宇野 健太", "1"),
    ("江口 美咲", "2"),
    ("大森 隆", "1"),
    ("加納 由香", "2"),
    ("木村 俊介", "1"),
    ("久保 真理", "2"),
    ("小谷 誠治", "1"),
    ("佐々木 恵", "2"),
]
_DIAGNOSES = [
    "市中肺炎",
    "急性腎盂腎炎",
    "蜂窩織炎",
    "カテーテル関連血流感染症",
    "急性胆管炎",
    "誤嚥性肺炎",
    "感染性腸炎",
    "化膿性脊椎炎",
]
_PATHOGENS = [
    "Streptococcus pneumoniae",
    "Escherichia coli",
    "Staphylococcus aureus (MSSA)",
    "Klebsiella pneumoniae",
    "Pseudomonas aeruginosa",
    "検出なし",
]


def make_subjects(n: int = 10, seed: int = E2E_SEED) -> list[E2ESubject]:
    """患者 n 名を seed 固定で作る。同じ seed なら何度呼んでも同じ列が返る。"""
    rng = random.Random(seed)
    subjects: list[E2ESubject] = []
    for i in range(n):
        idx = i + 1
        name, sex_code = _NAMES[i % len(_NAMES)]
        birth = date(1935, 1, 1) + timedelta(days=rng.randint(0, 365 * 60))
        admission = date(2026, 1, 5) + timedelta(days=rng.randint(0, 170))
        onset = admission - timedelta(days=rng.randint(0, 6))
        # 発症日が DWH に無い患者を 3 名ごとに 1 名作る（E2E-0003/0006/0009。フォールバック経路の検証用）
        onset_in_dwh = idx % 3 != 0
        max_temp = round(37.2 + rng.random() * 2.9, 1)
        weight = round(42.0 + rng.random() * 43.0, 1)
        body_fat = round(rng.uniform(15.0, 35.0), 1)
        antibiotic_days = rng.choice([0, 0, 3, 5, 7, 7, 10, 14])
        subjects.append(
            E2ESubject(
                reg_no=f"E2E-{idx:04d}",
                mrn_canonical=str(7000000 + idx * 37),
                billing_id=f"B{idx * 1013:06d}",
                name=name,
                birth_date=birth,
                sex_code=sex_code,
                admission_date=admission,
                onset_date=onset,
                onset_in_dwh=onset_in_dwh,
                diagnosis=rng.choice(_DIAGNOSES),
                pathogen=rng.choice(_PATHOGENS),
                severity=rng.choice(["1", "2", "2", "3"]),
                max_body_temp=max_temp,
                body_weight=weight,
                body_fat=body_fat,
                crp_on_admission=round(rng.uniform(0.3, 25.0), 2),
                crp_day3=round(rng.uniform(0.1, 15.0), 2),
                wbc_on_admission=rng.randint(3200, 21000),
                antibiotic_days=antibiotic_days,
                los_days=antibiotic_days + rng.randint(2, 9),
                consent_date=admission + timedelta(days=rng.randint(1, 3)),
            )
        )
    return subjects


SUBJECTS: list[E2ESubject] = make_subjects()
