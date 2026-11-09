"""臨床的コンテキストの「この項目を確認」は、ページを再読込しない（#20）。

再読込すると、他項目の未保存の編集と AI 下書きが失われる。
"""

from __future__ import annotations

import re
from pathlib import Path

JS = Path(__file__).parents[1] / "kawasemi/web/static/js/design_clinical.js"


def _function_body(src: str, name: str) -> str:
    m = re.search(rf"(?:async )?function {name}\(.*?\n}}\n", src, re.S)
    assert m, name
    return m.group(0)


def test_confirm_item_does_not_reload_the_page() -> None:
    src = JS.read_text("utf-8")
    for name in ("confirmItem", "confirmOne"):
        body = _function_body(src, name)
        assert "location.reload" not in body
        assert "save(true" not in body  # save() は成功時に再読込する
    # 当該項目だけを送る
    assert "postSave(true, [item])" in _function_body(src, "confirmOne")
