"""canonical 値の物理整形（physical rendering）と証跡用マスク。

canonical（resolver / key_types カタログが返す正準形）を、列ごとの格納形式
（ゼロ詰め等。DSL step.physical_format）に合わせて実行時バインド直前に整形する。
証跡には生の名寄せ値を残さず、マスク値のみ記録する（非保持）。

設計: 整形2層: canonical/physical / evidence境界
"""

from __future__ import annotations

from typing import Any


def apply_physical_format(value: str, fmt: dict[str, Any] | None) -> str:
    """canonical 値に列固有の物理整形を適用する。

    対応キー: pad(総桁数・左詰め) / padchar(既定 '0') / prefix / suffix。
    fmt が None/空ならそのまま返す。
    """
    if not fmt:
        return value
    out = value
    pad = fmt.get("pad")
    if pad:
        padchar = (str(fmt.get("padchar", "0")) or "0")[:1]
        out = out.rjust(int(pad), padchar)
    prefix = fmt.get("prefix")
    if prefix:
        out = f"{prefix}{out}"
    suffix = fmt.get("suffix")
    if suffix:
        out = f"{out}{suffix}"
    return out


def mask_value(value: str | None) -> str | None:
    """証跡用に値をマスクする。生の名寄せ値は残さない（非保持）。

    温存量を値長に比例させる（末尾 min(4, 値長の25%) 文字のみ残す）。固定の
    「末尾4文字」では桁数の少ない登録番号や患者番号で値の大半が露出し、evidence から
    再構成され得るため。例: 6桁→末尾1、10桁→末尾2、UUID 等の長い値→末尾4。
    4文字以下は全マスク。
    """
    if value is None:
        return None
    s = str(value)
    if len(s) <= 4:
        return "*" * len(s)
    keep = min(4, len(s) // 4)
    return "*" * (len(s) - keep) + s[-keep:]
