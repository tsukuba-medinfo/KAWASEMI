"""データ自動収集 機能のユーザ向け表示ラベル（単一の真実源）

UI に出す名称・番号はすべてここで定義する。内部コード（routes.py の関数名・
URL パス・DB モデル名）とは独立しており、開発コード上の「モードN」表記とは
区別する。番号はユーザ向けに 1 始まり。

内部「モードN」とのおおまかな対応（履歴）:
    ステップ1 = 旧モード2、ステップ2 = 旧モード3、
    ステップ3 = 旧モード4、ステップ4 = 旧モード5（旧モード4: 自動入力）
"""

from __future__ import annotations

# 機能全体の総称（メニュー・入口ボタン・見出し）
FEATURE_NAME = "データ自動収集"

# 4 ステップの定義。path は /ai-input/questionnaires/{id}/{path} のサフィックス。
STAGES: list[dict[str, object]] = [
    {"no": 1, "name": "データソース調査", "path": "source-survey"},
    {"no": 2, "name": "取得手順書の作成", "path": "runbook"},
    {"no": 3, "name": "取得プランの作成", "path": "plan"},
    {"no": 4, "name": "AI自動入力", "path": "autofill"},
]

# ステップ番号 → 名前 の早見表
STAGE_NAME: dict[int, str] = {s["no"]: s["name"] for s in STAGES}  # type: ignore[misc]
