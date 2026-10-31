"""alembic/env.py の配線を固定する回帰テスト

kawasemi.models.Base.metadata は kawasemi.ai_input.models を import した時点で
初めて ai_input 系テーブル（system_locations 等）を含む。alembic CLI は単体で
起動されるため、env.py がこの import を持たないと、実際にテーブルが存在するにも
関わらず「モデルに存在しない＝削除対象」と誤検出する。

pytest プロセスでは他のテストモジュール経由で kawasemi.ai_input.models が
既に import 済みになるため、実行時の Base.metadata を見るテストではこの回帰を
検出できない（import し忘れても metadata には含まれてしまう）。そのため
env.py のソース上の import 文そのものを確認する。
"""

from __future__ import annotations

import ast
from pathlib import Path

# CWD 相対で読むと、リポジトリルート以外から pytest を起動したときに FileNotFoundError に
# なる。`alembic/env.py` は追跡ファイルなので**不在は正常な状態ではない**。
# したがって skip ではなくテストファイルからの相対で解決し、CWD 依存そのものを消す。
# 不在なら FileNotFoundError で落ちるのが正しい（この回帰テストが守る対象が消えている）。
_REPO_ROOT = Path(__file__).resolve().parents[1]
_ENV_PY = _REPO_ROOT / "alembic" / "env.py"


def _env_py_imports() -> set[str]:
    source = _ENV_PY.read_text(encoding="utf-8")
    tree = ast.parse(source)
    return {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    }


def test_env_imports_ai_input_models():
    imports = _env_py_imports()
    assert "kawasemi.ai_input" in imports or "kawasemi.ai_input.models" in imports, (
        "alembic/env.py が kawasemi.ai_input.models を import していない。"
        "target_metadata に ai_input 系テーブルが含まれず、autogenerate が"
        "既存テーブルの削除を誤検出する"
    )


def test_env_imports_models_base():
    imports = _env_py_imports()
    assert "kawasemi.models" in imports, "target_metadata の元になる Base の import が無い"
