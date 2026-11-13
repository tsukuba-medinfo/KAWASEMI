"""jsdom でクライアント側の振る舞いを動かす `tests/js/xss_handlers.test.js` の呼び出し口。

XSS 対策でインライン JS を `data-*` 属性＋イベント委譲に書き換えた。文字列の部分一致では
配線の生死もイベントの順序も見えない（データセット削除の確認は、リスナーを base.html の
送信ハンドラより後に登録したためキャンセルしても削除が走る退行を起こした）。

**skip の扱い**: jsdom は `npm ci` が要る（`package.json` の devDependency）。入っていない環境では
skip になるが、**skip は通ったではない**（`docs/development_flow.md` §4.3）。CI の `test` ジョブは
`npm ci` を実行しないため、現状 CI ではこのテストが skip になる。CI で走らせるには
`npm ci` と `node tests/js/xss_handlers.test.js` を CI に足す必要がある（lint ジョブは
すでに `npm ci` している）。
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]
_SCRIPT = _REPO / "tests" / "js" / "xss_handlers.test.js"


def test_client_side_handlers_behave_in_jsdom() -> None:
    node = shutil.which("node")
    if node is None:
        pytest.skip("node が無い（JS の振る舞いテストは未実行）")
    if not (_REPO / "node_modules" / "jsdom").is_dir():
        pytest.skip("jsdom が無い: `npm ci` を実行すると走る（JS の振る舞いテストは未実行）")
    result = subprocess.run(
        [node, str(_SCRIPT)], capture_output=True, text=True, cwd=_REPO, timeout=120, check=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "passed" in result.stdout
