"""定義書検品（lint）のテスト。

定義書の既知の不備の類型（全角混入A・区切りB・表ID重複C・CREATE_接頭E・
必須空欄F）を検出し、--fix 相当（read_tabular normalize）の冪等性・provenance を検証する。
"""

from __future__ import annotations

from pathlib import Path

from kawasemi_indexer.ingestion.lint import lint_tabular, write_reports
from kawasemi_indexer.ingestion.models import TabularSource
from kawasemi_indexer.ingestion.tabular import (
    TabularData,
    TabularRow,
    normalize_identifier,
    read_tabular,
)


def _data(rows, table_match="table_id", **kw):
    return TabularData(
        table_match=table_match,
        rows=[TabularRow(*r) if isinstance(r, tuple) else r for r in rows],
        source_path="def.csv",
        **kw,
    )


# --------------------------------------------------------------------------- #
# 検出ルール
# --------------------------------------------------------------------------- #
def test_detects_fullwidth_in_identifiers() -> None:
    """クラスA: 物理名・表識別子の全角混入を error 検出（NFKC 修正案付き）。"""
    data = _data([("T_LAB", "ＢＬＡＳＴ_PCT", "検査結果")])  # 物理名に全角
    report = lint_tabular(data)
    fw = [f for f in report.findings if f.rule == "fullwidth" and f.severity == "error"]
    assert len(fw) == 1
    assert fw[0].fixable and fw[0].fixed == "BLAST_PCT"
    assert report.has_errors


def test_detects_separator_anomaly() -> None:
    """クラスB: db_qualified 指定なのに '.' が無い（'_' 誤用）を error 検出。"""
    data = _data([("APP_T_FORM", "COL", "名")], table_match="db_qualified")
    report = lint_tabular(data)
    assert any(f.rule == "separator" and f.severity == "error" for f in report.findings)


def test_detects_table_id_reuse() -> None:
    """クラスC: 同一表IDが別々の table_logical に割り当てられていれば error。"""
    rows = [
        TabularRow(match_value="T_LAB_HIST", physical_name="A", table_logical="検査履歴マスタDB"),
        TabularRow(match_value="T_LAB_HIST", physical_name="B", table_logical="検査履歴マスタ"),
    ]
    report = lint_tabular(_data(rows))
    reuse = [f for f in report.findings if f.rule == "table_id_reuse"]
    assert len(reuse) == 1 and reuse[0].severity == "error"


def test_detects_create_prefix() -> None:
    """クラスE: CREATE_ 接頭辞の紛れ込みを warning 検出（修正案付き）。"""
    rows = [
        TabularRow(match_value="DEMO_VIEW", physical_name="C", table_logical="CREATE_DEMO_VIEW")
    ]
    report = lint_tabular(_data(rows))
    cp = [f for f in report.findings if f.rule == "create_prefix"]
    assert len(cp) == 1 and cp[0].fixed == "DEMO_VIEW"


def test_detects_empty_required_and_logical_missing() -> None:
    """クラスF（必須空欄で行除外）を warning、論理名欠落を info 検出。"""
    data = _data([("T", "COL", None)], dropped_empty_rows=3)
    report = lint_tabular(data)
    assert any(f.rule == "empty_required" and f.severity == "warning" for f in report.findings)
    assert any(f.rule == "logical_missing" and f.severity == "info" for f in report.findings)


def test_table_scoped_rules_dedupe_across_columns() -> None:
    """表スコープのルール（separator）は列数ぶん重複せず表単位で1件（レポート肥大防止）。"""
    rows = [
        TabularRow(match_value="APP_T_FORM", physical_name="C1", logical_name="a"),
        TabularRow(match_value="APP_T_FORM", physical_name="C2", logical_name="b"),
        TabularRow(match_value="APP_T_FORM", physical_name="C3", logical_name="c"),
    ]
    report = lint_tabular(_data(rows, table_match="db_qualified"))
    sep = [f for f in report.findings if f.rule == "separator"]
    assert len(sep) == 1  # 3列でも1件


def test_logical_missing_aggregated_per_table() -> None:
    """論理名欠落は列ごとでなく表単位で件数集約される。"""
    rows = [
        TabularRow(match_value="T", physical_name="C1", logical_name=None),
        TabularRow(match_value="T", physical_name="C2", logical_name=None),
    ]
    report = lint_tabular(_data(rows))
    lm = [f for f in report.findings if f.rule == "logical_missing"]
    assert len(lm) == 1 and "2 件" in lm[0].message


def test_clean_data_has_no_errors() -> None:
    """不備の無い定義書はエラーゼロ。"""
    data = _data([("T_LAB", "BLAST_PCT", "芽球割合")])
    report = lint_tabular(data)
    assert not report.has_errors
    assert report.count("error") == 0


# --------------------------------------------------------------------------- #
# --fix（正規化）の冪等性・provenance
# --------------------------------------------------------------------------- #
def test_normalize_identifier_idempotent() -> None:
    once = normalize_identifier("ＡPP.T_LAB")
    assert once == "APP.T_LAB"
    assert normalize_identifier(once) == once  # 2回適用しても不変


def test_fix_via_read_tabular_records_provenance(tmp_path: Path) -> None:
    """read_tabular(normalize=True) が全角を正規化し、原値を追跡できる記録を残す。"""
    csv = tmp_path / "def.csv"
    # 物理名 ＰＣＴ に全角。
    csv.write_text("T,C,L\nT_LAB,ＰＣＴ,芽球割合\n", encoding="utf-8")
    source = TabularSource(
        path=str(csv), columns={"table_id": "T", "physical_name": "C", "logical_name": "L"}
    )
    fixed = read_tabular(source, normalize=True)
    assert fixed.rows[0].physical_name == "PCT"  # 正規化された
    assert len(fixed.normalizations) == 1
    assert fixed.normalizations[0].original == "ＰＣＴ"

    # fix 済みデータの検品は error でなく fullwidth_fixed（info・記録）として扱う。
    report = lint_tabular(fixed)
    assert not report.has_errors
    fixedf = [f for f in report.findings if f.rule == "fullwidth_fixed" and f.severity == "info"]
    assert len(fixedf) == 1
    assert fixedf[0].table == "T_LAB"  # 物理名修正でも表IDで追跡できる（provenance）
    assert fixedf[0].original == "ＰＣＴ" and fixedf[0].fixed == "PCT"

    # 冪等: もう一度読み直しても正規化対象は残らない（既に半角）。
    again = read_tabular(source, normalize=True)
    assert again.normalizations  # ソース自体は全角のままなので毎回同じ正規化を出す
    assert lint_tabular(again).count("error") == 0


def test_write_reports_emits_md_and_jsonl(tmp_path: Path) -> None:
    data = _data([("T_LAB", "ＢＬＡＳＴ_PCT", None)])
    report = lint_tabular(data)
    md, jsonl = write_reports(report, tmp_path)
    assert md.exists() and jsonl.exists()
    assert "検品レポート" in md.read_text(encoding="utf-8")
    assert jsonl.read_text(encoding="utf-8").strip()  # 1行以上
