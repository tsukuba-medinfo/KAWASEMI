"""定義書（Excel/CSV）の検品（lint）。

院内の DB 定義書には機械処理を阻む不備（全角の混入・区切りの誤り・表IDのコピペ誤りなど）が
よくある。ingest の前段でこれらを類型ごとのルールにして検出し、確度別レポートを出す。
`--fix` は安全な正規化（全角→半角等）のみ適用し、適用内容を provenance として記録する。

検出ルール（初期セット。クラスは不備の類型 A〜F の記号）:
- fullwidth   : 物理識別子（DB名・表名・カラム名）への全角英数/記号の混入（クラスA）。error。
                NFKC で自動修正可。--fix 適用済みの記録は rule=fullwidth_fixed（info）で別立て。
- separator   : db_qualified 指定なのに区切り '.' が無い（クラスB。'_' 誤用等）。error。
- table_id_reuse: 同一表IDが別々の table_logical に割り当て（クラスC。コピペ誤り）。error。
- create_prefix : 表論理名・識別子に紛れた 'CREATE_' 接頭辞（クラスE）。warning。自動修正可。
- empty_required: 必須列（表識別子/物理名）が空で行が捨てられた（クラスF）。warning。
- logical_missing: 論理名が空（検索で拾えない。論理名補間の対象）。info。表単位で件数集約。

表スコープのルール（separator / 表IDの fullwidth / table_logical の create_prefix /
table_id_reuse / logical_missing）は表単位で1件に集約し、列数ぶんの重複発火を防ぐ。
物理名の fullwidth のみ列スコープ（列ごとに検出）。

索引の tabular アダプタは JOIN 句を解析しないため、
結合条件数とJOIN句数の不一致はこのレイヤの検出対象外（別レイヤの課題）。
"""

from __future__ import annotations

import json
import unicodedata
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from kawasemi_indexer.ingestion.tabular import TabularData
from kawasemi_indexer.logging_config import get_logger

logger = get_logger(__name__)

Severity = Literal["error", "warning", "info"]
_SEVERITY_ORDER: dict[Severity, int] = {"error": 0, "warning": 1, "info": 2}


@dataclass
class LintFinding:
    """検品で見つかった1件の不備。"""

    rule: str
    severity: Severity
    message: str
    source_path: str | None = None
    table: str | None = None
    column: str | None = None
    fixable: bool = False
    original: str | None = None
    fixed: str | None = None


@dataclass
class LintReport:
    """1回の検品結果。"""

    findings: list[LintFinding] = field(default_factory=list)

    def add(self, f: LintFinding) -> None:
        self.findings.append(f)

    def count(self, severity: Severity) -> int:
        return sum(1 for f in self.findings if f.severity == severity)

    @property
    def has_errors(self) -> bool:
        return self.count("error") > 0

    def summary(self) -> str:
        return (
            f"error={self.count('error')} / warning={self.count('warning')} / "
            f"info={self.count('info')}（計{len(self.findings)}件）"
        )

    def sorted_findings(self) -> list[LintFinding]:
        return sorted(
            self.findings,
            key=lambda f: (
                _SEVERITY_ORDER[f.severity],
                f.source_path or "",
                f.table or "",
                f.column or "",
            ),
        )


def _has_fullwidth(value: str) -> bool:
    """NFKC で ASCII 側へ変わる文字（全角英数・全角空白・全角記号）を含むか。"""
    return unicodedata.normalize("NFKC", value) != value


def lint_tabular(data: TabularData) -> LintReport:
    """1ソースの抽出結果を検品して LintReport を返す（純関数）。

    data が read_tabular(normalize=True) 由来なら、正規化済み識別子に対する検出になり、
    適用済みの正規化は data.normalizations から「fixed」findings として取り込む。
    """
    report = LintReport()
    src = data.source_path

    # --fix 適用済みの正規化を記録として取り込む（provenance）。検出(error)と区別するため
    # rule は fullwidth_fixed（消費側が severity 併用なしでも区別できる）。
    for norm in data.normalizations:
        report.add(
            LintFinding(
                rule="fullwidth_fixed",
                severity="info",
                message=f"物理識別子を正規化しました（{norm.field}）: '{norm.original}' → '{norm.normalized}'",
                source_path=src,
                table=norm.table,
                fixable=True,
                original=norm.original,
                fixed=norm.normalized,
            )
        )

    # クラスF: 必須列が空で捨てられた行。
    if data.dropped_empty_rows:
        report.add(
            LintFinding(
                rule="empty_required",
                severity="warning",
                message=f"必須列（表識別子/物理名）が空で {data.dropped_empty_rows} 行を除外しました（記載漏れの可能性）",
                source_path=src,
            )
        )

    # 表スコープの集約（表単位で1件に集約し、列数ぶんの重複発火を防ぐ）。
    logical_by_table: dict[str, set[str]] = {}
    logical_missing_count: dict[str, int] = {}
    table_order: list[str] = []
    for row in data.rows:
        if row.match_value not in logical_by_table:
            logical_by_table[row.match_value] = set()
            logical_missing_count[row.match_value] = 0
            table_order.append(row.match_value)
        if row.table_logical:
            logical_by_table[row.match_value].add(row.table_logical)

        # クラスA（列スコープ）: 物理名の全角混入は列ごとに検出（未 fix のときのみ）。
        if _has_fullwidth(row.physical_name):
            report.add(
                LintFinding(
                    rule="fullwidth",
                    severity="error",
                    message=f"物理名に全角文字が混入しています: '{row.physical_name}'（SQL生成に直結。--fix で NFKC 正規化可）",
                    source_path=src,
                    table=row.match_value,
                    column=row.physical_name,
                    fixable=True,
                    original=row.physical_name,
                    fixed=unicodedata.normalize("NFKC", row.physical_name),
                )
            )

        # 論理名欠落は表単位で件数集約（実DBで数千件に氾濫させない）。
        if not row.logical_name:
            logical_missing_count[row.match_value] += 1

    # 表スコープのルール（表IDごとに1回）。
    for table_id in table_order:
        # クラスA: 表識別子の全角混入。
        if _has_fullwidth(table_id):
            report.add(
                LintFinding(
                    rule="fullwidth",
                    severity="error",
                    message=f"表識別子に全角文字が混入しています: '{table_id}'（--fix で NFKC 正規化可）",
                    source_path=src,
                    table=table_id,
                    fixable=True,
                    original=table_id,
                    fixed=unicodedata.normalize("NFKC", table_id),
                )
            )
        # クラスB: db_qualified 指定なのに区切り '.' が無い（'_' 誤用等）。
        if data.table_match == "db_qualified" and "." not in table_id:
            report.add(
                LintFinding(
                    rule="separator",
                    severity="error",
                    message=f"db_qualified に区切り '.' がありません: '{table_id}'（schema.table 形式を確認）",
                    source_path=src,
                    table=table_id,
                )
            )
        # クラスE: 表識別子・表論理名の CREATE_ 接頭辞（表論理名は表内の distinct 値ごと）。
        create_targets = [("表識別子", table_id)] + [
            ("表論理名", tl) for tl in sorted(logical_by_table[table_id])
        ]
        for fld, val in create_targets:
            if val.upper().startswith("CREATE_"):
                report.add(
                    LintFinding(
                        rule="create_prefix",
                        severity="warning",
                        message=f"{fld}に 'CREATE_' 接頭辞が紛れています: '{val}'",
                        source_path=src,
                        table=table_id,
                        fixable=True,
                        original=val,
                        fixed=val[len("CREATE_") :],
                    )
                )
        # クラスC: 同一表IDが複数の異なる table_logical に割り当て（コピペ誤り）。
        if len(logical_by_table[table_id]) > 1:
            report.add(
                LintFinding(
                    rule="table_id_reuse",
                    severity="error",
                    message=f"表ID '{table_id}' が別々の表に再利用されています（論理名: {sorted(logical_by_table[table_id])}）",
                    source_path=src,
                    table=table_id,
                )
            )
        # 論理名欠落（表単位の件数集約）。
        missing = logical_missing_count[table_id]
        if missing:
            report.add(
                LintFinding(
                    rule="logical_missing",
                    severity="info",
                    message=f"論理名が空の列が {missing} 件あります（NL 検索で拾いにくい。draft-knowledge の補間対象）",
                    source_path=src,
                    table=table_id,
                )
            )

    logger.info(f"検品完了: {src or '(source)'} {report.summary()}")
    return report


def merge_reports(reports: list[LintReport]) -> LintReport:
    """複数ソースの検品結果を1つにまとめる。"""
    merged = LintReport()
    for r in reports:
        merged.findings.extend(r.findings)
    return merged


# --------------------------------------------------------------------------- #
# レポート出力
# --------------------------------------------------------------------------- #

_QA_DIR = "qa"
_QA_MD = "qa_report.md"
_QA_JSONL = "qa_report.jsonl"


def write_reports(report: LintReport, out_dir: str | Path) -> tuple[Path, Path]:
    """人向け Markdown と機械向け JSONL を out_dir/qa/ に書き出す。"""
    qa = Path(out_dir) / _QA_DIR
    qa.mkdir(parents=True, exist_ok=True)
    md_path = qa / _QA_MD
    jsonl_path = qa / _QA_JSONL

    with jsonl_path.open("w", encoding="utf-8") as fh:
        for f in report.sorted_findings():
            fh.write(json.dumps(asdict(f), ensure_ascii=False) + "\n")

    lines = ["# 定義書 検品レポート（qa_report）", "", f"要約: {report.summary()}", ""]
    for sev in ("error", "warning", "info"):
        items = [f for f in report.sorted_findings() if f.severity == sev]
        if not items:
            continue
        heading = {"error": "エラー（要修正）", "warning": "警告", "info": "情報"}[sev]
        lines.append(f"## {heading}（{len(items)}件）")
        lines.append("")
        for f in items:
            loc = " / ".join(x for x in (f.source_path, f.table, f.column) if x)
            fix = "（--fix で自動修正可）" if f.fixable else ""
            lines.append(f"- `{f.rule}` {f.message}{fix}")
            if loc:
                lines.append(f"    - 位置: {loc}")
        lines.append("")
    md_path.write_text("\n".join(lines), encoding="utf-8")

    logger.info(f"検品レポートを書き出しました: {md_path}, {jsonl_path}")
    return md_path, jsonl_path
