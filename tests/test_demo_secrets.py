"""デモ用の資格情報が予測できないこと

**予測できる資格情報をコミットしない。** シードに `admin` / `admin` のような
ユーザー名と同じパスワードを焼き込むと、そのデータでシードされて到達可能な
デプロイでは管理者権限を丸ごと奪われる（CWE-798）。設計ロールの資格情報も同じで、
予測可能な資格情報は **設計ロール → 任意患者の PHI 開示** という経路の入口になる。

ここで見るのは3つ。**生成される**こと、**環境から与えられれば従う**こと、そして
**固定値がコミットに残っていない**ことである。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import kawasemi.core.demo_secrets as demo_secrets
from kawasemi.core.demo_secrets import demo_password, was_generated

_REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def _clear_cache(monkeypatch: pytest.MonkeyPatch):
    """プロセス内キャッシュを毎回まっさらにする（テスト間で値が漏れない）。"""
    monkeypatch.setattr(demo_secrets, "_generated", None)
    monkeypatch.delenv("DEMO_PASSWORD", raising=False)
    yield


def test_a_generated_password_is_not_guessable() -> None:
    """与えられていなければ生成し、ユーザー名のような値にはならないこと。"""
    password = demo_password()

    assert was_generated() is True
    assert len(password) >= 32, password
    assert password not in {"admin", "ai_designer", "demo", "password"}


def test_the_same_process_gets_the_same_password() -> None:
    """同じプロセスでは同じ値を返すこと。

    シードは2箇所（`examples/init_demo_data.py` と `e2e/seed.py`）から呼ばれる。
    別々の値を作ると、**起動ログに出した値でログインできないユーザーができる**。
    """
    assert demo_password() == demo_password()


def test_an_explicit_password_is_honoured(monkeypatch: pytest.MonkeyPatch) -> None:
    """`DEMO_PASSWORD` が与えられていればそれを使うこと（実演の手順書用）。"""
    monkeypatch.setenv("DEMO_PASSWORD", "手順書に書いた値")

    assert demo_password() == "手順書に書いた値"
    assert was_generated() is False


def test_a_blank_password_falls_back_to_generation(monkeypatch: pytest.MonkeyPatch) -> None:
    """空文字・空白のみは「与えられていない」として扱うこと。

    compose は値を渡さないときに空文字を入れる。ここで空文字をそのまま採ると
    **パスワード無しのユーザー**ができる。
    """
    monkeypatch.setenv("DEMO_PASSWORD", "   ")

    assert was_generated() is True
    assert len(demo_password()) >= 32


def test_the_seeds_do_not_hardcode_a_password() -> None:
    """シードがユーザー名と同じパスワードを焼き込んでいないこと。

    `demo_password()` を経由するようにしたので、`get_password_hash("admin")` のような
    呼び出しは残っていてはいけない。
    """
    seeds = [
        _REPO / "examples" / "init_demo_data.py",
        _REPO / "kawasemi" / "ai_input" / "e2e" / "seed.py",
    ]
    for path in seeds:
        text = path.read_text(encoding="utf-8")
        calls = re.findall(r"get_password_hash\(([^)]*)\)", text)
        assert calls, f"{path.name} に get_password_hash の呼び出しが無い（検査が空回りする）"
        for arg in calls:
            # **文字列リテラルを渡していないこと。** 変数経由（`password` / `demo_password()`）
            # なら通す——`init_demo_data` は先頭で1度だけ `demo_password()` を呼んで使い回す。
            assert not re.search(r"""['"]""", arg), (
                f"{path.name} が固定パスワードを渡している: {arg}"
            )
        assert "demo_password()" in text, f"{path.name} が demo_password() を使っていない"
