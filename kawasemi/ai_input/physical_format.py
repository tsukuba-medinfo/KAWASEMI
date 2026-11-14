"""計画の step.physical_format の検査（依存を持たない軽量モジュール）。

`dsl.validate_program`（保存時）と `key_resolver.format.apply_physical_format`（適用時）が
同じ規則を使う。key_resolver パッケージは設定を読み込むので、保存時の検査からは
このモジュールを直接読む。
"""

from __future__ import annotations

from typing import Any

# 物理整形の許容範囲。計画（デザイナーが保存する step.physical_format）はこの関数へ届くので、
# 無制限の `pad` が巨大な文字列を確保してワーカーのメモリを使い切るのを防ぐ。
PHYSICAL_FORMAT_KEYS = frozenset({"pad", "padchar", "prefix", "suffix"})
MAX_PAD = 64
MAX_AFFIX_LENGTH = 64


def physical_format_errors(fmt: Any) -> list[str]:
    """physical_format の検査結果（問題の一覧。空なら妥当）。

    許すのは `pad`（0〜64 の整数）・`padchar`（1文字）・`prefix` / `suffix`（64文字まで）だけ。
    保存時（`dsl.validate_program`）と適用時（`apply_physical_format`）の両方で使う。
    """
    if fmt is None:
        return []
    if not isinstance(fmt, dict):
        return ["physical_format が辞書ではありません"]
    errors: list[str] = []
    for key in fmt:
        if key not in PHYSICAL_FORMAT_KEYS:
            errors.append(
                f"physical_format に未知のキー: {key!r}（pad / padchar / prefix / suffix のみ）"
            )
    pad = fmt.get("pad")
    if pad is not None and (
        isinstance(pad, bool) or not isinstance(pad, int) or not 0 <= pad <= MAX_PAD
    ):
        errors.append(f"physical_format.pad は 0〜{MAX_PAD} の整数にしてください: {pad!r}")
    padchar = fmt.get("padchar")
    if padchar is not None and (not isinstance(padchar, str) or len(padchar) != 1):
        errors.append("physical_format.padchar は1文字の文字列にしてください")
    for key in ("prefix", "suffix"):
        v = fmt.get(key)
        if v is not None and (not isinstance(v, str) or len(v) > MAX_AFFIX_LENGTH):
            errors.append(
                f"physical_format.{key} は {MAX_AFFIX_LENGTH} 文字までの文字列にしてください"
            )
    return errors
