"""手書き風の画像PDF（テキスト層なし）を生成するデモ/テスト用ユーティリティ。

実臨床では紙の文書がスキャンPDFとして連携されることがある。本モジュールは
そうした「テキスト層を持たない手書き文書」を、外部依存（スキャナ・実画像）なしに再現する。

各文字をベースライン揺らぎ＋字間ゆらぎ付きで**画像へ描画**し、Pillow で PDF へ保存する。
ラスタ画像のみの PDF となるため pypdf の ``extract_text()`` は空文字を返し、executor の
``file_extract`` が Vision OCR へ自動縮退する経路を実際に通す。

揺らぎは文字コード由来の決定的擬似乱数で与える（再生成しても同一の出力になる）。
"""

from __future__ import annotations

from pathlib import Path

# 日本語グリフを持つ TTF/TTC 候補（macOS → Linux の順）。手書き系があれば優先する。
_FONT_CANDIDATES = [
    "/System/Library/Fonts/ヒラギノ丸ゴ ProN W4.ttc",
    "/System/Library/Fonts/Supplemental/Klee.ttc",
    "/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc",
    "/System/Library/Fonts/Hiragino Sans GB.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
]


def _load_font(size: int):  # type: ignore[no-untyped-def]
    """日本語可の TTF を探して読み込む。無ければ Pillow 既定フォント（Latin のみ）。"""
    from PIL import ImageFont

    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except Exception:  # noqa: BLE001
                continue
    return ImageFont.load_default()


def _jitter(ch: str, span: int) -> int:
    """文字コード由来の決定的な揺らぎ（-span..+span）。手書きの不揃いを擬似再現する。"""
    return (ord(ch) * 37 % (2 * span + 1)) - span


def make_handwriting_pdf(
    lines: list[str],
    path: Path,
    *,
    width: int = 1240,
    height: int = 1754,
    font_size: int = 48,
) -> Path:
    """手書き風テキストを画像へ描画し、テキスト層を持たない PDF として保存する。

    Args:
        lines: 各行の文字列（上から順に描画）。
        path: 出力 PDF パス（親ディレクトリは自動作成）。
        width/height: ページのピクセルサイズ（既定 A4 相当・150dpi 目安）。
        font_size: 基準フォントサイズ。

    Returns:
        書き出した PDF のパス。
    """
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (width, height), "white")
    draw = ImageDraw.Draw(image)
    font = _load_font(font_size)

    y = 150
    line_height = int(font_size * 1.9)
    for line in lines:
        x = 140
        for ch in line:
            dy = _jitter(ch, 6)
            draw.text((x, y + dy), ch, fill=(15, 20, 70), font=font)
            try:
                char_w = draw.textlength(ch, font=font)
            except Exception:  # noqa: BLE001
                char_w = font_size * 0.6
            x += int(char_w) + _jitter(ch, 3) + 4
        y += line_height

    path.parent.mkdir(parents=True, exist_ok=True)
    # 画像のみを埋め込んだ PDF（テキスト層なし）。pypdf は空を返し Vision OCR へ縮退する。
    image.save(str(path), "PDF", resolution=150.0)
    return path
