"""一覧フィルタ入力のパースとユーザー通知。

従来は `int()` 変換失敗を `except: pass` で握りつぶし、フィルタが黙ってスキップ
されて「効いていない」ように見えていた。ここで一元的にパースし、失敗時は
警告メッセージを収集してテンプレートで通知する。
"""

from __future__ import annotations


def parse_int_filter(raw: str | None, label: str, warnings: list[str]) -> int | None:
    """フィルタの整数値をパースする。空は None、変換失敗は None＋警告を追加。

    Args:
        raw: クエリ文字列の生値
        label: 警告文に使うフィルタ名（例: "Questionnaire"）
        warnings: 失敗時にメッセージを追記するリスト（テンプレートへ渡す）
    """
    if raw is None or not str(raw).strip():
        return None
    try:
        return int(raw)
    except ValueError:
        warnings.append(f"{label}フィルタの値「{raw}」が不正なため無視しました")
        return None
