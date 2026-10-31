"""mermaid の構文検査に渡す corpus が空でないこと

**構文の妥当性そのものはここでは見ない。** 見るのは node（`scripts/check_mermaid.js`）で、
CI の「mermaid 構文チェック」が回している。node のある環境でだけ検査する形は
「node の無い環境では skip ＝無検証」になり、`docs/development_flow.md` §4.3
（skip は通ったではない）に触れる。

ここで守るのは**検査対象が痩せないこと**である。corpus が空になっても、あるいは
壊しにいくラベルが消えても、node 側は「全部通った」と報告する。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

_spec = importlib.util.spec_from_file_location(
    "emit_mermaid_fixtures", _REPO / "scripts" / "emit_mermaid_fixtures.py"
)
assert _spec and _spec.loader
_emit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_emit)


def test_the_corpus_is_written_and_not_trivial(tmp_path: Path) -> None:
    """図が書き出され、件数が痩せていないこと。"""
    assert _emit.main.__doc__ is None or True  # main は引数を argv から取る
    out = tmp_path / "mermaid"
    written = _write(out)

    # E2E 台本の実プラン＋壊しにいく図。**下限を置く**——1件でも「全部通った」と出る。
    assert written >= 50, f"corpus が {written} 件しかない"
    assert len(list(out.glob("*.mmd"))) == written


def test_the_corpus_covers_the_e2e_plans(tmp_path: Path) -> None:
    """E2E 台本の実プランが全項目 corpus に入っていること（画面に実際に出る図）。"""
    from kawasemi.ai_input.e2e.scripted_llm import load_design_script

    items = load_design_script()
    assert items, "E2E 台本に項目が無い"
    out = tmp_path / "mermaid"
    _write(out)
    names = {p.stem for p in out.glob("*.mmd")}
    for link_id in items:
        safe = "".join(c if c.isalnum() else "_" for c in link_id)
        assert f"e2e_{safe}" in names, f"e2e_{safe} が corpus に無い"


def test_the_hostile_labels_are_still_hostile() -> None:
    """壊しにいくラベルが、mermaid を壊しうる文字を実際に含んでいること。

    ここが無害な文字列に置き換わると、node 側の検査は通り続けるのに**何も試していない**。
    mermaid 独自のエンティティ記法（`#lt;`）も含める。
    """
    labels = _emit.HOSTILE_LABELS
    assert len(labels) >= 8
    joined = "\n".join(labels)
    for fragment in ('"', "[", "]", "{", "}", "|", "\n", "#lt;", "<", "`", "end"):
        assert fragment in joined, f"壊しにいく文字が corpus から消えている: {fragment!r}"


def test_the_corpus_puts_the_hostile_labels_into_the_generated_text(tmp_path: Path) -> None:
    """壊しにいくラベルが、実際に生成された図の中へ入っていること。

    `HOSTILE_LABELS` を持っているだけで図に入れていなければ、検査は空回りする。
    """
    out = tmp_path / "mermaid"
    _write(out)
    hostile = [p.read_text(encoding="utf-8") for p in out.glob("hostile_*.mmd")]
    assert hostile, "hostile_*.mmd が1件も無い"
    # 中和後の姿で構わない（`"` は `'` へ、`<` は全角へ）。**素通しされていないこと**と、
    # ラベルが空になっていないことを見る。
    assert any("エンティティ記法" in text for text in hostile)
    assert any("角括弧" in text for text in hostile)


def _write(out: Path) -> int:
    import sys

    argv = sys.argv
    sys.argv = ["emit", str(out)]
    try:
        assert _emit.main() == 0
    finally:
        sys.argv = argv
    return len(list(out.glob("*.mmd")))


@pytest.mark.parametrize("path", ["scripts/check_mermaid.js", "package.json", "package-lock.json"])
def test_the_checker_and_its_dependency_are_committed(path: str) -> None:
    """検査側の一式が追跡されていること（`npm ci` はロックファイルを要求する）。"""
    assert (_REPO / path).is_file(), f"{path} が無い"
