"""関門 (a)〜(e) の判定と、項目×関門の表（docs/e2e_pipeline.md §1）。

- (a) 索引に `gold.unit_id` がある（観察項目の単位は前方一致）
- (b) 所在探索の上位 k（既定 8）に `gold.unit_id` が入る
- (c) `needs_input` 無しでコンパイルでき、確認済みにできる
- (d) 実行値が期待値と一致する（患者ごと）。フォールバック項目は経路も見る
- (e) 回答（QuestionnaireResponse）として保存され、保存値が期待値と一致する

**落ちた段階の後ろは判定しない**（`skip`）。(b) で落ちた項目は所在が決まらないので、
(c) 以降を「失敗」と数えると原因が5つあるように見える。表には最初に落ちた関門だけが
`FAIL` で出る。
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

GATES = ("a", "b", "c", "d", "e")
GATE_NAMES = {
    "a": "索引に期待単位がある",
    "b": "所在探索の上位kに入る",
    "c": "needs_input無しでコンパイル",
    "d": "実行値が期待値と一致",
    "e": "回答として保存",
}
PASS = "PASS"
FAIL = "FAIL"
SKIP = "skip"
XFAIL = "XFAIL"  # 既知の理由で落ちると宣言した関門（理由を必ず添える）


def unit_matches(gold_unit: str, unit_id: str) -> bool:
    """期待単位と実際の単位が一致するか。観察項目の単位（`#` 付き）は前方一致で見る。

    `db:observations.value_text#weight` の gold は seq の絞り込みを `row_filters` に持つので、
    将来 seq 付きの命名（`#weight.1` 等）になっても一致とみなす（docs/e2e_assets.md §4）。
    """
    if "#" in gold_unit:
        return unit_id == gold_unit or unit_id.startswith(gold_unit)
    return unit_id == gold_unit


def values_equal(expected: Any, actual: Any) -> bool:
    """期待値と実行値を比べる。数値の期待値は数値として（DB の文字列 `"44.1"` も受ける）。"""
    if actual is None:
        return False
    if isinstance(expected, bool) or not isinstance(expected, (int, float)):
        return str(actual).strip() == str(expected)
    try:
        return math.isclose(float(actual), float(expected), rel_tol=0, abs_tol=1e-9)
    except (TypeError, ValueError):
        return False


def answer_value(item: dict[str, Any]) -> Any:
    """QuestionnaireResponse.item の最初の answer から値を取り出す。"""
    answers = item.get("answer") or []
    if not answers:
        return None
    a = answers[0]
    if "valueCoding" in a:
        return a["valueCoding"].get("code")
    for key in ("valueDate", "valueDecimal", "valueInteger", "valueString", "valueBoolean"):
        if key in a:
            return a[key]
    return None


@dataclass
class ItemGates:
    """1項目の関門の結果。`detail` に失敗の理由（患者ごとの食い違い等）を書く。"""

    link_id: str
    route: str
    gates: dict[str, str] = field(default_factory=lambda: dict.fromkeys(GATES, SKIP))
    detail: dict[str, list[str]] = field(default_factory=dict)

    def set(self, gate: str, ok: bool, *reasons: str) -> None:
        self.gates[gate] = PASS if ok else FAIL
        if reasons:
            self.detail.setdefault(gate, []).extend(reasons)

    def xfail(self, gate: str, reason: str) -> None:
        self.gates[gate] = XFAIL
        self.detail.setdefault(gate, []).append(reason)

    def skip(self, gate: str, reason: str) -> None:
        """判定できない関門を理由付きで skip にする（実機層で Vision が無い等）。

        理由の無い `skip` は「前の関門で落ちたので判定しない」の意味で、こちらは「判定の
        材料が無い」。表では同じ `skip` だが、`detail` に理由が残るので区別できる。
        """
        self.gates[gate] = SKIP
        self.detail.setdefault(gate, []).append(reason)

    @property
    def terminal(self) -> bool:
        """終端に着いたか: 全関門 PASS か、最初の非 PASS が FAIL / XFAIL / 理由付き skip。

        理由の無い skip で止まっている項目は、工程が途中で止まって判定されなかったもの。
        """
        first = next((g for g in GATES if self.gates[g] != PASS), None)
        if first is None:
            return True
        return self.gates[first] in (FAIL, XFAIL) or bool(self.detail.get(first))

    def open_at(self, gate: str) -> bool:
        """`gate` を判定してよいか（それより前の関門がすべて PASS）。"""
        before = GATES[: GATES.index(gate)]
        return all(self.gates[g] == PASS for g in before)

    @property
    def first_failure(self) -> str | None:
        return next((g for g in GATES if self.gates[g] in (FAIL, XFAIL)), None)


@dataclass
class GateReport:
    items: dict[str, ItemGates] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def add(self, link_id: str, route: str) -> ItemGates:
        self.items[link_id] = ItemGates(link_id, route)
        return self.items[link_id]

    def failures(self, gate: str | None = None) -> list[ItemGates]:
        """FAIL（XFAIL は含めない）の項目。`gate` を渡せばその関門だけ。"""
        return [
            it
            for it in self.items.values()
            if (gate is None and FAIL in it.gates.values()) or (gate and it.gates[gate] == FAIL)
        ]

    def unterminated(self) -> list[str]:
        """終端に着かなかった項目の link_id（`ItemGates.terminal`）。"""
        return [lid for lid, it in self.items.items() if not it.terminal]

    def table(self) -> str:
        """項目×関門の表（Markdown）。失敗した項目は理由を下に並べる。"""
        head = "| 項目 | 経路 | " + " | ".join(f"({g})" for g in GATES) + " |"
        sep = "|---|---|" + "---|" * len(GATES)
        rows = [
            f"| {it.link_id} | {it.route} | " + " | ".join(it.gates[g] for g in GATES) + " |"
            for it in self.items.values()
        ]
        lines = [
            head,
            sep,
            *rows,
            "",
            "関門: " + " / ".join(f"({g}) {n}" for g, n in GATE_NAMES.items()),
        ]
        for it in self.items.values():
            for g in GATES:
                for reason in it.detail.get(g, []):
                    lines.append(f"- {it.link_id} ({g}) {it.gates[g]}: {reason}")
        lines.extend(f"- 注: {n}" for n in self.notes)
        return "\n".join(lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "gates": GATE_NAMES,
            "items": {
                lid: {
                    "route": it.route,
                    "gates": it.gates,
                    "first_failure": it.first_failure,
                    "detail": it.detail,
                }
                for lid, it in self.items.items()
            },
            "notes": self.notes,
        }

    def write(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_json(), ensure_ascii=False, indent=2), "utf-8")
