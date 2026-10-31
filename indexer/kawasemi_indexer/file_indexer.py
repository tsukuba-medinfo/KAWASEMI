"""kind=file の indexer（reference 文書のチャンク化 / patient_data の所在宣言）

設計:
- 索引=地図、実行=患者値。索引時に読むのは患者データを含まない reference 文書のみ。
- file_role=reference: root_path を走査し text/markdown/PDF を抽出・チャンク化して索引。
- file_role=patient_data: 中身は索引しない。所在パターン(path_pattern)＋抽出方法
  (extract.method) を宣言したマニフェスト(JSONL)からユニットを作る。実際の読み取りは
  ステップ4の file_extract（v1未実装）で行う。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from kawasemi_indexer.logging_config import get_logger
from kawasemi_indexer.schema import Kind, ResourceUnit

logger = get_logger(__name__)

# reference 走査で対象にする拡張子と MIME
_MIME_BY_EXT: dict[str, str] = {
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".pdf": "application/pdf",
}

# チャンク化の既定（文字数ベース・オーバーラップ付き）
DEFAULT_CHUNK_CHARS = 800
DEFAULT_OVERLAP = 100

# 1ファイルの最大サイズ（これを超える reference 文書はスキップ）。
# 巨大ファイルによる OOM を防ぐ。
_MAX_FILE_BYTES = 20 * 1024 * 1024  # 20 MiB


def _is_within(base: Path, target: Path) -> bool:
    """target が base 配下（解決後）に収まるか。シンボリックリンクで外部へ
    抜けるのを検出するため、双方を resolve() して比較する。"""
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except (ValueError, OSError):
        return False


def chunk_text(
    text: str, chunk_chars: int = DEFAULT_CHUNK_CHARS, overlap: int = DEFAULT_OVERLAP
) -> list[tuple[int, int, str]]:
    """テキストを (start, end, chunk) のリストへ分割する（オーバーラップ付き）。"""
    text = text or ""
    if not text.strip():
        return []
    step = max(1, chunk_chars - overlap)
    chunks: list[tuple[int, int, str]] = []
    start = 0
    n = len(text)
    while start < n:
        end = min(start + chunk_chars, n)
        piece = text[start:end].strip()
        if piece:
            chunks.append((start, end, piece))
        if end >= n:
            break
        start += step
    return chunks


def _extract_pdf_pages(path: Path) -> list[str]:
    """PDF を1ページ1文字列で抽出する（pypdf）。失敗ページは空文字。"""
    from pypdf import PdfReader

    reader = PdfReader(str(path))
    pages: list[str] = []
    for page in reader.pages:
        try:
            pages.append(page.extract_text() or "")
        except Exception as e:  # noqa: BLE001
            logger.warning(f"PDFページ抽出失敗: {path}: {e}")
            pages.append("")
    return pages


def _extract_pages(path: Path) -> list[tuple[int | None, str]]:
    """ファイルを (page, text) のリストへ抽出する。text/md はページなし(None)。"""
    ext = path.suffix.lower()
    if ext == ".pdf":
        return [(i + 1, t) for i, t in enumerate(_extract_pdf_pages(path))]
    return [(None, path.read_text("utf-8", errors="replace"))]


def build_reference_units(
    root: str | Path,
    connection_id: str | None = None,
    chunk_chars: int = DEFAULT_CHUNK_CHARS,
    overlap: int = DEFAULT_OVERLAP,
) -> list[ResourceUnit]:
    """reference 文書のディレクトリを走査し、チャンク単位の file ユニットを作る。"""
    base = Path(root)
    if not base.exists():
        raise FileNotFoundError(f"reference 文書ディレクトリが見つかりません: {base}")

    units: list[ResourceUnit] = []
    for path in sorted(base.rglob("*")):
        # シンボリックリンクは追従しない。reference 配下に /etc や患者データ
        # ディレクトリへの symlink が置かれると索引外のはずのファイル本文が
        # 索引へ混入し、「索引に患者データを載せない」不変条件を破るため。
        if path.is_symlink():
            logger.warning(f"シンボリックリンクをスキップ: {path}")
            continue
        if not path.is_file():
            continue
        # 解決後パスが base 配下に収まることを再確認（symlink 経由の遡上防止）
        if not _is_within(base, path):
            logger.warning(f"reference ディレクトリ外を指すパスをスキップ: {path}")
            continue
        mime = _MIME_BY_EXT.get(path.suffix.lower())
        if mime is None:
            continue  # 非対応拡張子はスキップ
        try:
            if path.stat().st_size > _MAX_FILE_BYTES:
                logger.warning(f"サイズ上限({_MAX_FILE_BYTES}B)超過につきスキップ: {path}")
                continue
        except OSError as e:
            logger.warning(f"ファイルサイズ取得失敗につきスキップ: {path}: {e}")
            continue
        rel = path.relative_to(base).as_posix()
        for page, text in _extract_pages(path):
            for start, end, piece in chunk_text(text, chunk_chars, overlap):
                page_tag = f"#p{page}" if page is not None else ""
                units.append(
                    ResourceUnit(
                        unit_id=f"file:{rel}{page_tag}:{start}-{end}",
                        kind=Kind.FILE,
                        label=rel + (f"（p{page}）" if page is not None else ""),
                        search_text=piece,
                        snippet=piece[:200],
                        payload={
                            "connection_id": connection_id,
                            "file_role": "reference",
                            "path": rel,
                            "page": page,
                            "char_range": [start, end],
                            "mime": mime,
                        },
                    )
                )
    logger.info(f"reference 文書から {len(units)} チャンクユニットを構築: {base}")
    return units


def build_manifest_units(items: list[dict[str, Any]]) -> list[ResourceUnit]:
    """patient_data の所在宣言（マニフェスト）から file ユニットを作る。

    各 item: {label, path_pattern, extract:{method}, connection_id, notes}
    中身は索引せず、所在パターンと抽出方法のみを payload に持つ。
    """
    units: list[ResourceUnit] = []
    for item in items:
        label = item.get("label")
        pattern = item.get("path_pattern")
        if not label or not pattern:
            continue  # label と path_pattern は必須
        notes = item.get("notes")
        search_text = " ".join(str(p) for p in (label, item.get("aliases"), notes) if p)
        units.append(
            ResourceUnit(
                unit_id=f"file:patient_data:{label}",
                kind=Kind.FILE,
                label=label,
                search_text=search_text,
                snippet=f"{pattern}" + (f" — {notes}" if notes else ""),
                payload={
                    "connection_id": item.get("connection_id"),
                    "file_role": "patient_data",
                    "path_pattern": pattern,
                    "extract": item.get("extract") or {"method": "pdf_llm"},
                    "mime": item.get("mime"),
                },
            )
        )
    logger.info(f"patient_data マニフェストから {len(units)} ユニットを構築")
    return units


def read_manifest(source: str | Path) -> list[dict[str, Any]]:
    """patient_data マニフェストを読む。ディレクトリなら file_manifest.jsonl。"""
    path = Path(source)
    if path.is_dir():
        path = path / "file_manifest.jsonl"
    if not path.exists():
        raise FileNotFoundError(f"file マニフェストが見つかりません: {path}")
    items: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                items.append(json.loads(line))
    return items
