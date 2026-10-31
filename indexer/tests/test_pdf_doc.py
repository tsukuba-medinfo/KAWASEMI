"""紙・スキャンPDF定義書の取り込みテスト（Ollama/実PDF 非依存）。

PdfPage を直接与え、fake client（text は chat / vision は images 付き chat）で
テキスト層とスキャンOCRの両経路、OCR不能ページの記録、患者データ誤投入ガード、
正準CSV/レポート出力を検証する。
"""

from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from kawasemi_indexer.ingestion.pdf_doc import (
    DOC_CSV_HEADERS,
    DocRecord,
    PdfPage,
    extract_records,
    write_doc_csv,
    write_extraction_report,
)


def _ns(payload: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        message=types.SimpleNamespace(content=json.dumps(payload, ensure_ascii=False))
    )


def _ns_empty() -> types.SimpleNamespace:
    return types.SimpleNamespace(message=types.SimpleNamespace(content=""))


def _client(rows_by_marker):
    """text/vision 両対応の fake client。images 有無で経路を切替え、rows を返す。"""

    async def chat(**kwargs):
        msgs = kwargs["messages"]
        user = msgs[-1]
        is_vision = "images" in user
        key = "vision" if is_vision else "text"
        return _ns({"rows": rows_by_marker.get(key, [])})

    return types.SimpleNamespace(chat=chat)


@pytest.mark.asyncio
async def test_text_layer_extraction() -> None:
    """テキスト層のあるページは text 経路で抽出される。"""
    pages = [
        PdfPage(
            page_no=1,
            text="表 LAB 検査結果テーブル\nWBC 白血球数\nHGB ヘモグロビン\nPLT 血小板数\nRBC 赤血球数",
        )
    ]
    client = _client(
        {"text": [{"table_id": "LAB", "physical_name": "WBC", "logical_name": "白血球数"}]}
    )
    ext = await extract_records(pages, model="m", client=client)
    assert len(ext.records) == 1
    assert ext.records[0].method == "text"
    assert ext.records[0].physical_name == "WBC"
    assert ext.pages[0].method == "text"


@pytest.mark.asyncio
async def test_scanned_page_uses_vision_ocr() -> None:
    """テキスト層が無く画像があるページは vision OCR 経路になる。"""
    pages = [PdfPage(page_no=1, text="", image_png=b"\x89PNG_fake")]
    client = _client(
        {"vision": [{"table_id": "LAB", "physical_name": "HGB", "logical_name": "ヘモグロビン"}]}
    )
    ext = await extract_records(pages, model="m", vision_model="v", client=client)
    assert len(ext.records) == 1 and ext.records[0].method == "ocr"
    assert ext.pages[0].method == "ocr"


@pytest.mark.asyncio
async def test_failed_page_is_recorded_not_dropped() -> None:
    """テキストも画像も無いページは失敗として記録され、黙って欠落しない。"""
    pages = [PdfPage(page_no=3, text="", image_png=None)]
    ext = await extract_records(pages, model="m", client=_client({}))
    assert ext.records == []
    assert len(ext.pages) == 1
    assert ext.pages[0].method == "failed" and ext.pages[0].page_no == 3


@pytest.mark.asyncio
async def test_extraction_error_is_recorded() -> None:
    """LLM 例外が出ても当該ページを failed 記録して他ページを継続。"""

    async def chat(**kwargs):
        raise RuntimeError("boom")

    pages = [PdfPage(page_no=1, text="x" * 50)]
    ext = await extract_records(pages, model="m", client=types.SimpleNamespace(chat=chat))
    assert ext.records == []
    assert ext.pages[0].method == "failed" and "boom" in (ext.pages[0].note or "")


@pytest.mark.asyncio
async def test_rejects_rows_missing_required() -> None:
    """table_id か physical_name が空の行は採用しない（捏造・不完全行ガード）。"""
    pages = [PdfPage(page_no=1, text="x" * 50)]
    client = _client(
        {
            "text": [
                {"table_id": "LAB", "physical_name": "WBC"},
                {"table_id": "", "physical_name": "GHOST"},  # 表ID空→捨てる
                {"table_id": "LAB", "physical_name": ""},  # 物理名空→捨てる
            ]
        }
    )
    ext = await extract_records(pages, model="m", client=client)
    assert [r.physical_name for r in ext.records] == ["WBC"]


@pytest.mark.asyncio
async def test_patient_data_guard_flags_valueish_physical_names() -> None:
    """物理名の多くが値（長い数字列）に見える＝患者データ誤抽出を警告。"""
    pages = [PdfPage(page_no=1, text="x" * 50)]
    client = _client(
        {
            "text": [
                {"table_id": "T", "physical_name": "0912345678"},
                {"table_id": "T", "physical_name": "0987654321"},
                {"table_id": "T", "physical_name": "WBC"},
            ]
        }
    )
    ext = await extract_records(pages, model="m", client=client)
    assert ext.has_patient_data_risk
    assert any("値" in f for f in ext.guard_findings)


@pytest.mark.asyncio
async def test_guard_does_not_flag_legit_dob_column_definition() -> None:
    """「生年月日」等の論理名は正当な列定義なので誤検知しない（値でなく定義）。"""
    pages = [PdfPage(page_no=1, text="x" * 50)]
    client = _client(
        {
            "text": [
                {"table_id": "PT", "physical_name": "BIRTH_DT", "logical_name": "生年月日"},
                {
                    "table_id": "PT",
                    "physical_name": "PT_NAME",
                    "logical_name": "氏名",
                    "notes": "様式1",
                },
            ]
        }
    )
    ext = await extract_records(pages, model="m", client=client)
    assert not ext.has_patient_data_risk  # 論理名/notes の語では発火しない


@pytest.mark.asyncio
async def test_empty_ocr_response_recorded_as_failed() -> None:
    """vision OCR の空応答は失読として failed 記録（0件成功と区別）。"""

    async def chat(**kwargs):
        return _ns_empty()

    pages = [PdfPage(page_no=1, text="", image_png=b"\x89PNG")]
    ext = await extract_records(pages, model="m", client=types.SimpleNamespace(chat=chat))
    assert ext.records == []
    assert ext.pages[0].method == "failed"


def test_write_doc_csv_and_report(tmp_path: Path) -> None:
    """正準CSV（固定ヘッダ）とレポートを書き出す。"""
    from kawasemi_indexer.ingestion.pdf_doc import PageResult, PdfExtraction

    records = [DocRecord(table_id="LAB", physical_name="WBC", logical_name="白血球数", page_no=1)]
    csv_path = write_doc_csv(records, tmp_path / "doc.csv")
    lines = csv_path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == ",".join(DOC_CSV_HEADERS)
    assert "LAB,WBC,白血球数," in lines[1]

    ext = PdfExtraction(
        records=records,
        pages=[PageResult(page_no=1, method="text", record_count=1)],
        guard_findings=["警告例"],
    )
    rep = write_extraction_report(ext, tmp_path / "doc.report.json")
    data = json.loads(rep.read_text(encoding="utf-8"))
    assert data["record_count"] == 1 and data["guard_findings"] == ["警告例"]


def test_pdf_page_has_text_layer() -> None:
    assert PdfPage(page_no=1, text="x" * 50).has_text_layer
    assert not PdfPage(page_no=1, text="short").has_text_layer


def test_doc_csv_feeds_read_tabular(tmp_path: Path) -> None:
    """出力CSVが既存 read_tabular（正準列マッピング）でそのまま読めること（合流の確認）。"""
    from kawasemi_indexer.ingestion.models import TabularSource
    from kawasemi_indexer.ingestion.tabular import read_tabular

    records = [DocRecord(table_id="LAB", physical_name="WBC", logical_name="白血球数")]
    csv_path = write_doc_csv(records, tmp_path / "doc.csv")
    source = TabularSource(
        path=str(csv_path),
        columns={
            "table_id": "table_id",
            "physical_name": "physical_name",
            "logical_name": "logical_name",
            "notes": "notes",
        },
    )
    data = read_tabular(source)
    assert data.rows[0].match_value == "LAB"
    assert data.rows[0].physical_name == "WBC"
    assert data.rows[0].logical_name == "白血球数"
