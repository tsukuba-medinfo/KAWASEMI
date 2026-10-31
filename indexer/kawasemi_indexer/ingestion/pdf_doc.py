"""紙・スキャンPDF定義書の取り込み。

定義書が紙（スキャンPDF）しか無い施設のため、PDF から表構造（表識別子・物理名・
論理名・メモ）を復元し、既存の CSV 定義書と同じ正準列を持つ CSV へ書き出す。人が
確認・修正してから通常の `draft-knowledge` / `ingest`（--doc）へ合流させる。

抽出の2段構え:
1. テキスト層のある PDF: pypdf でテキスト抽出 → LLM で表構造復元。
2. スキャン画像 PDF: pypdfium2 でページを画像化 → Ollama vision で OCR＋表構造復元。

原則:
- OCR/LLM 由来の値はすべて低信頼。人確認（通常の ingest 前レビュー）なしに索引へ載らない
  （出力は「レビュー前の CSV 下書き」であり confirmed 化はしない）。反捏造原則の維持。
- 定義書は患者データ非含有が前提。患者データらしき値（氏名・生年月日・ID 様の並び）を
  多く含む場合は誤投入ガードとして警告・停止する。
- OCR 不能ページは黙って欠落させず、ページ別レポートに残す。

テスト容易性: LLM クライアントとページ読取は注入可能（Ollama/実PDF 非依存で駆動）。
"""

from __future__ import annotations

import base64
import csv
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from kawasemi_indexer.config import settings
from kawasemi_indexer.logging_config import get_logger

logger = get_logger(__name__)

# テキスト層ありと判定する最小文字数（これ未満のページはスキャンとみなし OCR へ）。
_TEXT_LAYER_MIN_CHARS = 40
# 定義書の正準 CSV 列（そのまま TabularSource マッピングで読める）。
DOC_CSV_HEADERS = ("table_id", "physical_name", "logical_name", "notes")


@dataclass
class PdfPage:
    """PDF の1ページ（テキスト層と、必要なら画像バイト）。"""

    page_no: int
    text: str = ""
    image_png: bytes | None = None  # スキャンページの画像（vision OCR 用）

    @property
    def has_text_layer(self) -> bool:
        return len(self.text.strip()) >= _TEXT_LAYER_MIN_CHARS


@dataclass
class DocRecord:
    """PDF から復元した1カラム記述（人確認前の下書き）。"""

    table_id: str
    physical_name: str
    logical_name: str | None = None
    notes: str | None = None
    page_no: int | None = None
    method: str = "text"  # "text" | "ocr"


@dataclass
class PageResult:
    """1ページの処理結果（OCR不能ページも欠落させず記録する）。"""

    page_no: int
    method: str  # "text" | "ocr" | "failed"
    record_count: int = 0
    note: str | None = None


@dataclass
class PdfExtraction:
    """PDF 抽出の全結果（下書きレコード・ページ別結果・誤投入ガード所見）。"""

    records: list[DocRecord] = field(default_factory=list)
    pages: list[PageResult] = field(default_factory=list)
    guard_findings: list[str] = field(default_factory=list)

    @property
    def has_patient_data_risk(self) -> bool:
        return bool(self.guard_findings)


# --------------------------------------------------------------------------- #
# LLM 応答スキーマ / プロンプト
# --------------------------------------------------------------------------- #
class _DocRow(BaseModel):
    table_id: str | None = None
    physical_name: str | None = None
    logical_name: str | None = None
    notes: str | None = None


class _DocRowsOut(BaseModel):
    rows: list[_DocRow] = []


_SYS = """あなたはDB定義書（表・カラムの一覧）を読むアシスタントです。与えられた定義書
ページから、各カラムの (table_id=表物理名, physical_name=カラム物理名, logical_name=
カラム論理名, notes=備考) を抽出してください。定義書はスキーマ定義であり、患者個人の
データ行ではありません。**患者の値（氏名・生年月日・患者番号の実値など）は抽出せず**、
カラムの定義（物理名・論理名）のみを返してください。読み取れない項目は null。存在しない
情報を作らないでください。"""


# --------------------------------------------------------------------------- #
# ページ読取（pypdf テキスト / pypdfium2 レンダリング）
# --------------------------------------------------------------------------- #
def read_pdf_pages(path: str | Path, *, render_scanned: bool = True) -> list[PdfPage]:
    """PDF を PdfPage 群へ読む。テキスト層が薄いページは画像化して OCR に回す。

    レンダリング用の pypdfium ドキュメントは全ページで1回だけ開いて使い回す
    （スキャン主体の文書でページ毎に開き直さない）。
    """
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    texts: list[str] = []
    for i, page in enumerate(reader.pages):
        try:
            texts.append(page.extract_text() or "")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"PDFテキスト抽出失敗 p{i + 1}: {e}")
            texts.append("")

    need_render = render_scanned and any(len(t.strip()) < _TEXT_LAYER_MIN_CHARS for t in texts)
    images: dict[int, bytes | None] = {}
    if need_render:
        with _open_pdfium(path) as doc:
            for i, t in enumerate(texts):
                if len(t.strip()) < _TEXT_LAYER_MIN_CHARS:
                    images[i] = _render_page_png(doc, i) if doc is not None else None

    return [PdfPage(page_no=i + 1, text=t, image_png=images.get(i)) for i, t in enumerate(texts)]


@contextmanager
def _open_pdfium(path: str | Path) -> Iterator[Any]:
    """pypdfium ドキュメントを1回開いて close する（未導入・失敗なら None を yield）。"""
    doc = None
    try:
        import pypdfium2 as pdfium  # type: ignore[import-untyped]

        doc = pdfium.PdfDocument(str(path))
    except Exception as e:  # noqa: BLE001
        logger.warning(f"PDFドキュメント（画像化）を開けません: {e}")
        doc = None
    try:
        yield doc
    finally:
        if doc is not None:
            try:
                doc.close()
            except Exception:  # noqa: BLE001
                pass


def _render_page_png(doc: Any, page_index: int) -> bytes | None:
    """開いた pypdfium ドキュメントの1ページを PNG バイトへレンダリングする（失敗時 None）。"""
    try:
        import io

        bitmap = doc[page_index].render(scale=2.0)
        buf = io.BytesIO()
        bitmap.to_pil().save(buf, format="PNG")
        return buf.getvalue()
    except Exception as e:  # noqa: BLE001
        logger.warning(f"PDFページ画像化失敗 p{page_index + 1}: {e}")
        return None


# --------------------------------------------------------------------------- #
# 患者データ誤投入ガード
# --------------------------------------------------------------------------- #
_DOB_RE = re.compile(r"\b(19|20)\d{2}[-/年.](0?[1-9]|1[0-2])[-/月.](0?[1-9]|[12]\d|3[01])")
_LONG_DIGITS_RE = re.compile(r"^\d{6,}$")


def _guard_patient_data(records: list[DocRecord]) -> list[str]:
    """抽出物が患者データ「値」らしければ警告を返す（定義書は値を含まない前提）。

    ガードは **physical_name（カラム物理名）が値に見えるか** のみで判定する。カラム定義には
    「生年月日」「氏名」等の論理名が正当に現れるため、logical_name/notes の語では判定しない
    （正当な列定義を誤検知しない）。物理名が長い数字列・日付ならデータ行の誤抽出を疑う。
    """
    findings: list[str] = []
    if not records:
        return findings
    # physical_name が「値」に見える（長い数字列・日付）割合が高い＝行データを誤抽出。
    valueish = sum(
        1
        for r in records
        if _LONG_DIGITS_RE.match(r.physical_name or "") or _DOB_RE.search(r.physical_name or "")
    )
    if valueish and valueish / len(records) >= 0.3:
        findings.append(
            f"物理名の {valueish}/{len(records)} 件が値（長い数字列・日付）に見えます。"
            "カラム定義でなく患者データ行を抽出した可能性があります（要確認）。"
        )
    return findings


# --------------------------------------------------------------------------- #
# 抽出（テキスト / vision OCR）
# --------------------------------------------------------------------------- #
async def _recover_rows_text(client: Any, model: str, page: PdfPage) -> list[_DocRow]:
    from kawasemi_indexer.llm_json import chat_json

    data = await chat_json(
        client,
        model,
        system_prompt=_SYS,
        user_message=f"次の定義書ページからカラム定義を抽出してください:\n\n{page.text}",
        response_format=_DocRowsOut.model_json_schema(),
        label=f"PDF定義書抽出 p{page.page_no}",
    )
    return _DocRowsOut.model_validate(data).rows


async def _recover_rows_ocr(client: Any, model: str, page: PdfPage) -> list[_DocRow]:
    """スキャンページ画像を vision モデルへ渡して OCR＋構造復元する。"""
    from kawasemi_indexer.llm_json import DEFAULT_OPTIONS

    assert page.image_png is not None
    b64 = base64.b64encode(page.image_png).decode("ascii")
    response = await client.chat(
        model=model,
        messages=[
            {"role": "system", "content": _SYS},
            {
                "role": "user",
                "content": "この定義書ページ画像からカラム定義を抽出してください。",
                "images": [b64],
            },
        ],
        format=_DocRowsOut.model_json_schema(),
        options=DEFAULT_OPTIONS,  # temperature=0・十分な num_ctx（text 経路と対称・空応答回避）
    )
    raw = response.message.content
    if not raw:
        # 空応答は「読めなかった」＝失敗として扱う（0件成功と区別する）。呼び出し側が failed 記録。
        raise ValueError("vision OCR が空応答を返しました（失読の可能性）")
    return _DocRowsOut.model_validate(json.loads(raw)).rows


async def extract_records(
    pages: list[PdfPage],
    *,
    model: str,
    client: Any = None,
    vision_model: str | None = None,
) -> PdfExtraction:
    """ページ群からカラム定義を復元する（テキスト層は text、スキャンは vision OCR）。

    client は注入可能（テストはスタブ）。既定は ollama.AsyncClient。
    """
    if client is None:
        import ollama

        client = ollama.AsyncClient(host=settings.ollama_base_url, timeout=settings.ollama_timeout)
    vmodel = vision_model or model

    result = PdfExtraction()
    for page in pages:
        try:
            if page.has_text_layer:
                rows = await _recover_rows_text(client, model, page)
                method = "text"
            elif page.image_png is not None:
                rows = await _recover_rows_ocr(client, vmodel, page)
                method = "ocr"
            else:
                # テキストも画像も無い＝抽出不能。黙って落とさずレポートに残す。
                result.pages.append(
                    PageResult(page_no=page.page_no, method="failed", note="テキスト層も画像も無し")
                )
                continue
        except Exception as e:  # noqa: BLE001
            logger.warning(f"PDF抽出失敗 p{page.page_no}: {e}")
            result.pages.append(
                PageResult(page_no=page.page_no, method="failed", note=str(e)[:120])
            )
            continue

        page_records = [
            DocRecord(
                table_id=(r.table_id or "").strip(),
                physical_name=(r.physical_name or "").strip(),
                logical_name=((r.logical_name or "").strip() or None),
                notes=((r.notes or "").strip() or None),
                page_no=page.page_no,
                method=method,
            )
            for r in rows
            if (r.table_id or "").strip() and (r.physical_name or "").strip()
        ]
        result.records.extend(page_records)
        result.pages.append(
            PageResult(page_no=page.page_no, method=method, record_count=len(page_records))
        )

    result.guard_findings = _guard_patient_data(result.records)
    logger.info(
        f"PDF定義書抽出完了: {len(result.records)}レコード / "
        f"ページ{len(result.pages)}（失敗{sum(1 for p in result.pages if p.method == 'failed')}）"
        f"{' / 患者データ警告あり' if result.has_patient_data_risk else ''}"
    )
    return result


# --------------------------------------------------------------------------- #
# 出力（正準 CSV ＋ページ別レポート）
# --------------------------------------------------------------------------- #
def write_doc_csv(records: list[DocRecord], out_path: str | Path) -> Path:
    """正準列（table_id/physical_name/logical_name/notes）の CSV を書き出す（人確認前提）。"""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(DOC_CSV_HEADERS)
        for r in records:
            writer.writerow([r.table_id, r.physical_name, r.logical_name or "", r.notes or ""])
    logger.info(f"PDF定義書 下書きCSVを書き出しました（要人手確認）: {path}")
    return path


def write_extraction_report(extraction: PdfExtraction, out_path: str | Path) -> Path:
    """ページ別結果・患者データ警告の JSON レポートを書き出す。"""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "record_count": len(extraction.records),
        "pages": [asdict(p) for p in extraction.pages],
        "guard_findings": extraction.guard_findings,
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info(f"PDF抽出レポートを書き出しました: {path}")
    return path
