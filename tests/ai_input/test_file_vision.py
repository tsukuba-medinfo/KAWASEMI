"""file_extract の Vision OCR 自動縮退（手書き/スキャン画像PDF）のテスト。

- テキスト層のない画像PDF → pypdf が空 → Vision OCR へ縮退（Ollama を mock し決定的に検証）
- .txt 等のテキストファイルは従来どおりで Vision を呼ばない
- 実 Ollama（マルチモーダル）がある環境では実モデルで手書き同意書を読む（Ollama-gated）
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from kawasemi.ai_input.e2e.handwriting import make_handwriting_pdf
from kawasemi.ai_input.executor import DSLInterpreter
from kawasemi.core.config import settings
from tests.e2e.helpers import ollama_unavailable_reason

# Vision の縮退が実際に使うモデルで判定する（`executor._vision_read_pdf` と同じ解決順で、
# `OLLAMA_VISION_MODEL` が空なら `OLLAMA_MODEL` を流用する）。**そのモデルがサーバに無いと
# `POST /api/chat` が 404 を返し、「Vision が読み取れない」という別の失敗に化ける。**
_VISION_SKIP = ollama_unavailable_reason(settings.ollama_vision_model or settings.ollama_model)


def _patient_dir(root: Path) -> Path:
    """患者ごとのディレクトリ（`path_pattern` は `{{patient_id}}` を含む必要がある）。"""
    d = root / "P1"
    d.mkdir(exist_ok=True)
    return d


def _chat_response(content: str) -> SimpleNamespace:
    """ollama の chat 応答（response.message.content）を模す。"""
    return SimpleNamespace(message=SimpleNamespace(content=content))


@pytest.mark.asyncio
async def test_image_pdf_degrades_to_vision(fs_root: Path) -> None:
    pdf = _patient_dir(fs_root) / "consent.pdf"
    make_handwriting_pdf(["研究参加 同意書", "同意日 : 2025年 3月 17日"], pdf)

    # pypdf はテキスト層を持たない画像PDFを空文字で返す（縮退条件の前提）
    assert not DSLInterpreter._extract_pdf_text(pdf).strip()

    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(  # type: ignore[method-assign]
        return_value=_chat_response("同意日 : 2025年 3月 17日")
    )

    step = {
        "op": "file_extract",
        "id": "doc",
        "connection_id": "test_fs",
        "path_pattern": f"{{{{patient_id}}}}/{pdf.name}",
    }
    output, trace = await interp._op_file(step, "P1", {"op": "file_extract", "id": "doc"})

    assert output == "同意日 : 2025年 3月 17日"
    assert trace["method"] == "vision_ocr"
    assert trace["vision"]["pages"] == 1
    assert trace["vision"]["raw_output"] == output  # 生出力を監査用に確定保存
    # Ollama へ画像（PNG bytes）が渡っていること
    interp._ollama_client.chat.assert_awaited_once()
    msg = interp._ollama_client.chat.await_args.kwargs["messages"][0]
    assert msg["images"] and isinstance(msg["images"][0], (bytes, bytearray))


@pytest.mark.asyncio
async def test_text_file_does_not_call_vision(fs_root: Path) -> None:
    txt = _patient_dir(fs_root) / "note.txt"
    txt.write_text("組織型: 類内膜癌 G2", encoding="utf-8")

    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(  # type: ignore[method-assign]
        side_effect=AssertionError("テキストファイルで Vision を呼んではならない")
    )

    step = {
        "op": "file_extract",
        "id": "doc",
        "connection_id": "test_fs",
        "path_pattern": f"{{{{patient_id}}}}/{txt.name}",
    }
    output, trace = await interp._op_file(step, "P1", {"op": "file_extract", "id": "doc"})

    assert "類内膜癌" in output
    assert trace["method"] == "text"
    assert "vision" not in trace
    interp._ollama_client.chat.assert_not_called()


@pytest.mark.skipif(_VISION_SKIP is not None, reason=_VISION_SKIP or "")
@pytest.mark.asyncio
async def test_handwritten_consent_real_vision(fs_root: Path) -> None:
    """実 Ollama（マルチモーダル）で手書き同意書PDFを読み、年が読み取れることを確認。

    手書きOCRは非決定的なため、厳密一致ではなく「縮退経路を通り・非空・年を含む」で判定。
    """
    pdf = _patient_dir(fs_root) / "consent.pdf"
    make_handwriting_pdf(
        ["研究参加 同意書", "同意日 : 2025年 3月 17日", "被験者署名 : 佐藤 良子"],
        pdf,
    )
    interp = DSLInterpreter()
    step = {
        "op": "file_extract",
        "id": "doc",
        "connection_id": "test_fs",
        "path_pattern": f"{{{{patient_id}}}}/{pdf.name}",
    }
    output, trace = await interp._op_file(step, "P1", {"op": "file_extract", "id": "doc"})

    assert trace["method"] == "vision_ocr"
    assert output.strip(), "Vision OCR が何も読み取れていない"
    assert "2025" in output


@pytest.mark.asyncio
async def test_vision_trace_records_the_provider(fs_root: Path) -> None:
    """Vision 縮退の trace にも推論プロバイダが残ること。

    **vision は将来 Ollama に残す側である**（画像を無言で捨てるエンジンもあるので、テキスト
    生成を別エンジンへ移してもここは Ollama のままになる）。テキスト側だけを測って
    いると、vision の記録を落とす変更が全テスト緑のまま通る。

    いまは同じ client を読むので値はテキスト側と一致するが、**記録されていること自体**が
    テキストと vision の推論先を2本に分けるときの受け皿になる。
    """
    pdf = _patient_dir(fs_root) / "consent.pdf"
    make_handwriting_pdf(["研究参加 同意書", "同意日 : 2025年 3月 17日"], pdf)

    interp = DSLInterpreter()
    interp._ollama_client.chat = AsyncMock(  # type: ignore[method-assign]
        return_value=_chat_response("同意日 : 2025年 3月 17日")
    )
    step = {
        "op": "file_extract",
        "id": "doc",
        "connection_id": "test_fs",
        "path_pattern": f"{{{{patient_id}}}}/{pdf.name}",
    }
    _, trace = await interp._op_file(step, "P1", {"op": "file_extract", "id": "doc"})

    assert trace["method"] == "vision_ocr"
    assert trace["vision"]["provider"] == "ollama"
