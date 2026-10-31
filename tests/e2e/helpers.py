"""テスト共通ユーティリティ（ログインフォームの CSRF 抽出・実 Ollama の skip 条件）"""

import re

import httpx

from kawasemi.core.config import settings


def extract_form_csrf_token(html: str) -> str:
    """ログインページの hidden input から csrf_token を抽出する"""
    # name="csrf_token" value="TOKEN" または逆順
    m = re.search(r'name=["\']csrf_token["\'][^>]*value=["\']([^"\']+)["\']', html)
    if not m:
        m = re.search(r'value=["\']([^"\']+)["\'][^>]*name=["\']csrf_token["\']', html)
    if not m:
        raise ValueError("CSRF token (form input) not found in HTML")
    return m.group(1)


def ollama_base_url() -> str:
    """実 Ollama を使うテストが見に行く先。

    **決め打ちしない。** 以前はループバックの既定 URL を直接書いていたので、
    `OLLAMA_BASE_URL` が外部サーバを指していると**到達可能なのに skip していた**。
    開発環境が GPU 無しのホストへ移り生成が外部サーバへ向いた時点で、実 Ollama を使う
    テストはどのマシンでも永久に skip される状態になっていた——手書き同意書の
    Vision OCR がモデル非対応で常時失敗する不具合の**検証手段が黙って消えていた**。
    """
    return settings.ollama_base_url.rstrip("/")


def ollama_unavailable_reason(model: str | None = None) -> str | None:
    """実 Ollama を使えない理由を返す（使えるなら `None`）。

    **理由には必ず当て先を入れる。** 「Ollama 未起動」とだけ出していたのが、決め打ちに
    気づけなかった原因である。どこを見に行って失敗したのかが読めれば、設定と実体の
    食い違いはその場で分かる。

    `model` を渡すと、**そのモデルがサーバに存在するか**まで見る。存在しないモデルを指した
    まま実行すると `POST /api/chat` が 404 を返し、テストは「Vision が読み取れない」という
    **別の失敗に化ける**（実測: `.env` の既定のモデルがサーバに存在せず、
    `test_handwritten_consent_real_vision` が `trace["method"]` の KeyError で落ちた）。
    モデルが無いのは環境の不足であって実装の欠陥ではないので、**理由を明示して skip する**。
    """
    base = ollama_base_url()
    try:
        response = httpx.get(f"{base}/api/version", timeout=2.0)
    except Exception as exc:
        return f"Ollama へ到達できません（{base} / {type(exc).__name__}）"
    if response.status_code != 200:
        return f"Ollama が応答しません（{base} / HTTP {response.status_code}）"
    if model is None:
        return None

    try:
        tags = httpx.get(f"{base}/api/tags", timeout=5.0)
        tags.raise_for_status()
        names = (str(m.get("name", "")).strip() for m in tags.json().get("models", []))
        available = sorted(n for n in names if n)
    except Exception as exc:
        return f"Ollama のモデル一覧を取得できません（{base}/api/tags / {type(exc).__name__}）"
    if model not in available:
        return (
            f"モデル {model!r} が {base} にありません"
            f"（あるのは: {', '.join(available) if available else 'なし'}）"
        )
    return None
