"""AI 下書き系のテンプレートが JS を抱え込んでいないこと

**インライン `<script>` は構文検査に掛けられない。** 以前、
`design_clinical.html` の JS に構文エラーを注入しても **33 passed で全件緑**になることが
実測された。この状態ではページの JS が丸ごとパースエラーになり、AI 下書きどころか
保存・確認・典拠エディタも動かない。

切り出した先（`kawasemi/web/static/js/`）は CI の「JS 構文チェック」が `node --check` で
見る。ここで守るのは**逆流**である——テンプレートへ関数を書き戻すと、その分がまた
検査の外側に出る。

**構文そのものはここでは見ない。** node のある環境でだけ検査する形は「node の無い環境では
skip ＝無検証」になり、`docs/development_flow.md` §4.3（skip は通ったではない）に触れる。
構文の関門は CI 側にある。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_TEMPLATES = _REPO / "kawasemi" / "web" / "templates" / "ai_input"
_STATIC_JS = _REPO / "kawasemi" / "web" / "static" / "js"

# 「まず AI 下書き系」と定めた2件。残りのテンプレートは後続。
_EXTRACTED = ("design_clinical", "design_system")


@pytest.mark.parametrize("name", _EXTRACTED)
def test_the_extracted_javascript_file_exists(name: str) -> None:
    """切り出し先の実ファイルがあること。"""
    path = _STATIC_JS / f"{name}.js"
    assert path.is_file(), f"{path} が無い"
    # 中身が空同然だと、下の「テンプレートに関数が無い」だけが通って検査が空になる。
    assert len(path.read_text(encoding="utf-8").splitlines()) > 50


@pytest.mark.parametrize("name", _EXTRACTED)
def test_the_template_has_no_inline_javascript_logic(name: str) -> None:
    """テンプレートの `<script>` に関数定義が残っていないこと。

    設定を渡す `type="application/json"` のブロックと、`src` を指す `<script>` だけが
    残る。関数を書き戻すと、その分が構文検査の外側へ出る。
    """
    html = (_TEMPLATES / f"{name}.html").read_text(encoding="utf-8")
    for block in re.finditer(r"<script(?P<attrs>[^>]*)>(?P<body>.*?)</script>", html, re.S):
        attrs = block.group("attrs")
        if 'type="application/json"' in attrs or "src=" in attrs:
            continue
        body = block.group("body")
        assert "function " not in body and "=>" not in body, (
            f"{name}.html にインラインの JS ロジックが残っている:\n{body[:300]}"
        )


@pytest.mark.parametrize("name", _EXTRACTED)
def test_the_template_loads_the_extracted_file(name: str) -> None:
    """切り出したファイルを読み込む `<script src>` があること（キャッシュ破棄つき）。"""
    html = (_TEMPLATES / f"{name}.html").read_text(encoding="utf-8")
    assert f"/static/js/{name}.js" in html
    # `static_v()` を外すと、ブラウザが古い JS を掴んだまま新しい画面を出す。
    assert f"static_v('js/{name}.js')" in html


@pytest.mark.parametrize("name", _EXTRACTED)
def test_the_extracted_file_has_no_template_syntax(name: str) -> None:
    """切り出したファイルに Jinja が残っていないこと。

    残っていると**そのまま文字列としてブラウザへ届く**（静的配信はテンプレートを
    評価しない）。`{{ base_path() }}` が生で残れば、サブパス配下の fetch が全部壊れる。
    """
    js = (_STATIC_JS / f"{name}.js").read_text(encoding="utf-8")
    assert "{{" not in js, f"{name}.js に Jinja の式が残っている"
    assert "{%" not in js, f"{name}.js に Jinja のタグが残っている"


@pytest.mark.parametrize("name", _EXTRACTED)
def test_the_base_path_is_still_prefixed_on_internal_urls(name: str) -> None:
    """内部 URL に `base_path` を前置していること。

    サブパス配下（`ROOT_PATH=/KAWASEMI_DEMO` 等）で動かすための前置である。
    切り出しのときに `{{ base_path() }}` を落とすと、**リバースプロキシ配下で
    fetch が全部 404 になる**（デモ環境で実際に踏んだ形）。
    """
    js = (_STATIC_JS / f"{name}.js").read_text(encoding="utf-8")
    internal = re.findall(r"`([^`]*?/ai-input/[^`]*?)`", js)
    assert internal, f"{name}.js に内部 URL が見つからない（検査が空回りする）"
    for url in internal:
        assert url.startswith("${BASE}"), f"{name}.js の内部 URL に base_path が無い: {url}"
