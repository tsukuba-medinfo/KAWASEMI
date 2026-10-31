#!/usr/bin/env python3
"""検査対象の mermaid を書き出す。

`scripts/check_mermaid.js` が読む `.mmd` を作る。**Python 側で図を作り、node 側で
解釈できるかを見る**という分担にしてあるのは、`program_to_mermaid()` の出力そのものを
画面と同じパーサに掛けるためである。

corpus は2種類。

1. **E2E 台本の実プラン**（`examples/e2e/script/design.yaml` の各項目の `program`）。
   台本 LLM がコンパイラの応答として返すもので、実際に画面へ出る図。外部 DB・FHIR・
   CSV・ファイル＋llm_extract・compare＋branch の分岐を含む。実行トレース付きの図も出す。
2. **壊しにきたラベル**。step id・op の自由文に mermaid を壊しうる文字を入れたもの。
   `#lt;`（mermaid 独自のエンティティ記法）・改行・`"`・`[]{}()`・`|` など。
   `_esc` は `#lt;` を中和しないことが分かっているが、**図そのものを壊せるかは未検証**
   だった。ここで実際に通す。

使い方:
    uv run python scripts/emit_mermaid_fixtures.py <出力先ディレクトリ>
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

_REPO = Path(__file__).resolve().parents[1]
if str(_REPO) not in sys.path:
    sys.path.insert(0, str(_REPO))

from kawasemi.ai_input.e2e.scripted_llm import load_design_script  # noqa: E402
from kawasemi.ai_input.viz import program_to_mermaid  # noqa: E402

# **mermaid を壊しにいく文字列**。ラベルへそのまま入ると図が描かれない／途中で切れる。
HOSTILE_LABELS: tuple[str, ...] = (
    '"二重引用符"',
    "角括弧 [] と波括弧 {} と丸括弧 ()",
    "パイプ | と矢印 --> と セミコロン;",
    "改行\nを含む",
    "mermaid のエンティティ記法 #lt; #gt; #35;",
    "生の山括弧 <b>タグ</b> と <br/>",
    "バッククォート ` と ドル ${x}",
    "全角％ ＆ と 制御っぽい \\t タブ",
    "end",  # flowchart の予約語
    "graph TD",
)


def _hostile_programs() -> list[tuple[str, dict[str, Any]]]:
    """壊しにいくラベルを、id / reasoning / trace の出力へそれぞれ入れた図。"""
    out: list[tuple[str, dict[str, Any]]] = []
    for i, label in enumerate(HOSTILE_LABELS):
        out.append(
            (
                f"hostile_{i:02d}_id",
                {
                    "steps": [
                        {"op": "file_extract", "id": label, "path_pattern": "x.txt"},
                        {"op": "transform", "id": "t", "input": "$x", "expr": label},
                    ],
                    "return": "$t",
                },
            )
        )
        out.append(
            (
                f"hostile_{i:02d}_reasoning",
                {
                    "steps": [
                        {
                            "op": "sql_extract",
                            "id": "a",
                            "sql": "SELECT 1",
                            "reasoning": label,
                            "needs_input": True,
                        },
                        {"op": "branch", "id": "v", "when": "$a", "then": "$a", "else_branch": 1},
                    ],
                    "return": "$v",
                },
            )
        )
    return out


def _hostile_traces() -> list[tuple[str, dict[str, Any], list[dict[str, Any]]]]:
    """実行トレース（実値・エラー・guard の条件式）へ入れた図。"""
    program: dict[str, Any] = {
        "steps": [
            {"op": "sql_extract", "id": "a", "sql": "SELECT 1"},
            {"op": "transform", "id": "t", "input": "$a", "expr": "x"},
        ],
        "return": "$t",
    }
    out: list[tuple[str, dict[str, Any], list[dict[str, Any]]]] = []
    for i, label in enumerate(HOSTILE_LABELS):
        out.append(
            (
                f"hostile_{i:02d}_trace",
                program,
                [
                    {"id": "a", "output": label},
                    {"id": "t", "error": label},
                ],
            )
        )
        out.append(
            (
                f"hostile_{i:02d}_skipped",
                program,
                [{"id": "a", "skipped": True, "guard": label}],
            )
        )
    return out


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    out_dir = Path(sys.argv[1])
    out_dir.mkdir(parents=True, exist_ok=True)

    written = 0

    def write(name: str, text: str) -> None:
        nonlocal written
        (out_dir / f"{name}.mmd").write_text(text, encoding="utf-8")
        written += 1

    for link_id, item in load_design_script().items():
        safe = "".join(c if c.isalnum() else "_" for c in link_id)
        program = item["program"]
        write(f"e2e_{safe}", program_to_mermaid(program))
        # 同じ図を実行トレース付きで（ステップ4の画面。実値の注記が入る形）。
        trace = [
            {"id": step["id"], "output": f"{step['id']} の値"}
            for step in program["steps"]
            if step.get("id")
        ]
        write(f"e2e_{safe}_trace", program_to_mermaid(program, trace))

    for name, program in _hostile_programs():
        write(name, program_to_mermaid(program))

    for name, program, trace in _hostile_traces():
        write(name, program_to_mermaid(program, trace))

    # 手順が無い場合の図も画面へ出る。
    write("empty", program_to_mermaid({"steps": []}))

    print(f"{written} 件の mermaid を書き出しました: {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
