"""Excel/CSV 定義書アダプタ（意味補完の源）。

施設別マッピング（TabularSource.columns: canonicalフィールド→ヘッダ名）に従って、
任意レイアウトの定義書から (表識別子, 物理名, 論理名, メモ, 表論理名) を抽出する。
レイアウト解釈の確定はマッピング（LLM下書き→人確認）が担い、本モジュールは決定的。
"""

from __future__ import annotations

import csv
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kawasemi_indexer.ingestion.models import TabularSource
from kawasemi_indexer.logging_config import get_logger

logger = get_logger(__name__)


@dataclass
class Normalization:
    """--fix で物理識別子に適用した正規化（元値の追跡・監査用）。"""

    table: str  # その行の表識別子（どの表の値を直したか特定する。物理名修正時も表IDを保持）
    field: str  # "match_value" または "physical_name"
    original: str
    normalized: str


@dataclass
class TabularRow:
    """定義書1行から抽出したカラム記述（info_schema と突き合わせる前段）。"""

    match_value: str  # 表識別子の値（table_id または db_qualified）
    physical_name: str
    logical_name: str | None = None
    notes: str | None = None
    table_logical: str | None = None


@dataclass
class TabularData:
    """1ソースの抽出結果（join方式とともに返す）。"""

    table_match: str
    rows: list[TabularRow] = field(default_factory=list)
    source_path: str | None = None  # 検品レポートの位置情報
    dropped_empty_rows: int = 0  # 必須列（表識別子/物理名）が空で捨てた行数（検品用）
    normalizations: list[Normalization] = field(default_factory=list)  # --fix の適用記録


def normalize_identifier(value: str) -> str:
    """物理識別子（DB名・表名・カラム名）を NFKC 正規化する（全角→半角・空白畳み）。

    全角英数の混入（lint のクラスA）や全角空白を半角へ寄せ、実 DB の
    information_schema（ASCII）と突き合うようにする。値の意味は変えない。
    """
    return unicodedata.normalize("NFKC", value)


def read_tabular(source: TabularSource, *, normalize: bool = False) -> TabularData:
    """1つの定義書を読み、マッピングに従って TabularRow 群を返す。

    normalize=True（lint --fix 相当）のとき、表識別子・物理名を NFKC 正規化して
    実 DB と突き合うようにし、適用した正規化を TabularData.normalizations に記録する。
    """
    path = Path(source.path)
    if not path.exists():
        raise FileNotFoundError(f"定義書が見つかりません: {path}")
    suffix = path.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        headers, records = _read_xlsx(path, source.sheet, source.header_row)
    elif suffix == ".csv":
        headers, records = _read_csv(path, source.header_row)
    else:
        raise ValueError(f"未対応の定義書形式です: {path}（.xlsx/.csv のみ）")

    cmap = source.columns
    match_field = source.table_match  # "table_id" or "db_qualified"
    match_header = cmap.get(match_field) or cmap.get("table_id")
    if not match_header:
        raise ValueError(
            f"マッピングに表識別子（{match_field} / table_id）の列が指定されていません: {source.path}"
        )
    phys_header = cmap.get("physical_name")
    if not phys_header:
        raise ValueError(f"マッピングに physical_name の列が指定されていません: {source.path}")

    # マッピングが指すヘッダが実ファイルに存在するか検証（タイプミス・全半角差で
    # 全行が黙って欠落するのを防ぐ）。必須列(表識別子・物理名)は存在必須＝エラー、
    # 任意列(論理名・メモ・表論理名)は欠落を警告に留める。
    header_set = set(headers)
    for label, hdr in (("表識別子", match_header), ("physical_name", phys_header)):
        if hdr not in header_set:
            raise ValueError(
                f"マッピングの{label}列『{hdr}』が定義書ヘッダに存在しません: {source.path}"
                f"（ヘッダ: {sorted(header_set)}）"
            )
    for opt_field in ("logical_name", "notes", "table_logical"):
        opt_hdr = cmap.get(opt_field)
        if opt_hdr and opt_hdr not in header_set:
            logger.warning(
                f"マッピングの{opt_field}列『{opt_hdr}』が定義書ヘッダに無く補完されません: {source.path}"
            )

    rows: list[TabularRow] = []
    dropped_empty = 0
    normalizations: list[Normalization] = []
    for rec in records:
        match_value = _clean(rec.get(match_header))
        physical = _clean(rec.get(phys_header))
        if not match_value or not physical:
            dropped_empty += 1  # 表識別子・物理名が空の行は対象外（区切り・小計行 or 記載漏れ）
            continue
        if normalize:
            # 表識別子を先に正規化し、その行の表IDとして以降の記録に使う（物理名修正時も
            # 表IDで追跡できるようにする）。
            fixed_match = normalize_identifier(match_value)
            if fixed_match != match_value:
                normalizations.append(
                    Normalization(
                        table=fixed_match,
                        field="match_value",
                        original=match_value,
                        normalized=fixed_match,
                    )
                )
                match_value = fixed_match
            fixed_phys = normalize_identifier(physical)
            if fixed_phys != physical:
                normalizations.append(
                    Normalization(
                        table=match_value,  # その行の表ID（物理名でなく）
                        field="physical_name",
                        original=physical,
                        normalized=fixed_phys,
                    )
                )
                physical = fixed_phys
        rows.append(
            TabularRow(
                match_value=match_value,
                physical_name=physical,
                logical_name=_clean(rec.get(cmap.get("logical_name", ""))),
                notes=_clean(rec.get(cmap.get("notes", ""))),
                table_logical=_clean(rec.get(cmap.get("table_logical", ""))),
            )
        )
    if normalizations:
        logger.info(f"定義書の物理識別子を {len(normalizations)} 件正規化しました: {path}")
    logger.info(f"定義書を読み込み: {path}（{len(rows)}カラム, match={match_field}）")
    return TabularData(
        table_match=match_field,
        rows=rows,
        source_path=str(path),
        dropped_empty_rows=dropped_empty,
        normalizations=normalizations,
    )


def read_headers_and_records(
    path: str | Path, sheet: str | None = None, header_row: int = 1
) -> tuple[list[str], list[dict[str, str]]]:
    """定義書のヘッダ一覧と全行レコードを読む公開API（マッピング下書き等の前段で使う）。"""
    p = Path(path)
    suffix = p.suffix.lower()
    if suffix in (".xlsx", ".xlsm"):
        return _read_xlsx(p, sheet, header_row)
    if suffix == ".csv":
        return _read_csv(p, header_row)
    raise ValueError(f"未対応の定義書形式です: {path}（.xlsx/.csv のみ）")


def _clean(value: object) -> str | None:
    if value is None:
        return None
    s = str(value).strip()
    return s or None


def _read_csv(path: Path, header_row: int) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.reader(fh)
        rows = list(reader)
    return _records_from_rows(rows, header_row)


def _read_xlsx(
    path: Path, sheet: str | None, header_row: int
) -> tuple[list[str], list[dict[str, str]]]:
    from openpyxl import load_workbook  # type: ignore[import-untyped]

    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb[sheet] if sheet else wb[wb.sheetnames[0]]
        rows = [[("" if c is None else c) for c in r] for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()
    return _records_from_rows(rows, header_row)


def _records_from_rows(
    rows: list[list[Any]], header_row: int
) -> tuple[list[str], list[dict[str, str]]]:
    """ヘッダ行を見出しにして、以降の行を dict 化する。(headers, records) を返す。

    同名ヘッダは後勝ちで dict に潰れるため、重複・空ヘッダを検出して警告する。
    """
    if len(rows) < header_row:
        return [], []
    headers = [str(h).strip() for h in rows[header_row - 1]]
    non_empty = [h for h in headers if h]
    dupes = {h for h in non_empty if non_empty.count(h) > 1}
    if dupes:
        logger.warning(f"定義書に重複ヘッダがあり後勝ちで上書きされます: {sorted(dupes)}")
    records: list[dict[str, str]] = []
    for row in rows[header_row:]:
        rec = {headers[i]: row[i] for i in range(min(len(headers), len(row))) if headers[i]}
        records.append(rec)
    return [h for h in headers if h], records
