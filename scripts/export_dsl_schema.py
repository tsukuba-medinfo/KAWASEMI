#!/usr/bin/env python3
"""DSL の step スキーマ（JSON Schema）を `docs/schemas/dsl_step.schema.json` へ書き出す。

**正本はコード**（`kawasemi.ai_input.dsl.step_schema()`）で、このファイルはその写しである。
`tests/ai_input/test_dsl_specification.py` が写しとコードの一致を検査するので、
`dsl.py` / `schemas.DSLStep` を変えたら再生成してコミットする。

使い方:
    uv run python scripts/export_dsl_schema.py          # 書き出す
    uv run python scripts/export_dsl_schema.py --check  # 差分があれば 1 で終わる（書き出さない）

`make dsl-schema` も同じ（書き出す）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from kawasemi.ai_input.dsl import step_schema  # noqa: E402

SCHEMA_PATH = _REPO / "docs" / "schemas" / "dsl_step.schema.json"

_DESCRIPTION = (
    "KAWASEMI DSL の step 1件の構造化出力スキーマ。"
    "kawasemi.ai_input.dsl.step_schema() から scripts/export_dsl_schema.py で生成した写しであり、"
    "手で編集しないこと。DSL の妥当性の定義は validate_program（docs/dsl_specification.md §9）で、"
    "このスキーマはコンパイラが LLM の生成を拘束するために使う側の定義である。"
)


def schema_document() -> dict[str, Any]:
    """書き出す JSON Schema 文書（コードの `step_schema()` に見出しを添えたもの）。"""
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "KAWASEMI DSL step",
        "description": _DESCRIPTION,
        **step_schema(),
    }


def render() -> str:
    """ファイルへ書く文字列（キー順はコードの順。末尾改行つき）。"""
    return json.dumps(schema_document(), ensure_ascii=False, indent=2) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--check", action="store_true", help="書き出さずに比較だけする（差分があれば 1）"
    )
    args = parser.parse_args(argv)
    text = render()
    if args.check:
        current = SCHEMA_PATH.read_text(encoding="utf-8") if SCHEMA_PATH.exists() else None
        if current != text:
            print(
                f"{SCHEMA_PATH.relative_to(_REPO)} がコードと一致しません。"
                "`uv run python scripts/export_dsl_schema.py` で再生成してください",
                file=sys.stderr,
            )
            return 1
        return 0
    SCHEMA_PATH.parent.mkdir(parents=True, exist_ok=True)
    SCHEMA_PATH.write_text(text, encoding="utf-8")
    print(f"書き出しました: {SCHEMA_PATH.relative_to(_REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
