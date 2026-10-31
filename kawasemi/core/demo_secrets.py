"""デモ用の資格情報を決める。

**予測できる資格情報をコミットしない。** シードに `admin` / `admin` のような
ユーザー名と同じパスワードを焼き込むと、そのデータでシードされて到達可能な
デプロイでは管理者権限を丸ごと奪われる（CWE-798）。設計ロールの資格情報も同じで、
予測可能な資格情報は **設計ロール → 任意患者の PHI 開示** という経路の入口になる。

使うのはデモデータ（`examples/init_demo_data.py`）と E2E（`kawasemi.ai_input.e2e`）の
シードである。決め方は2つだけである。

- `DEMO_PASSWORD` が与えられていればそれを使う（実演の手順書に書いておきたい場合）
- 与えられていなければ**プロセスごとに1つ生成する**。シード側が生成した値を表示する
  （起動した人がすぐ見つけられること）

**デモユーザー間で同じ値を使う。** 使い捨てのデモ環境で危険なのは「コミット済みで
予測できる」ことであって、ユーザーごとに違うことではない。役割を切り替えて見せる
のがデモの中身なので、役割ごとに別々のパスワードを配ると実演が成り立たない。
"""

from __future__ import annotations

import os
import secrets

_ENV_VAR = "DEMO_PASSWORD"

_generated: str | None = None


def demo_password() -> str:
    """デモユーザーへ設定するパスワードを返す。

    同じプロセスの中では**同じ値**を返す（複数のシードが別々の値を作ると、
    起動ログに出した値でログインできないユーザーができる）。
    """
    global _generated
    given = (os.environ.get(_ENV_VAR) or "").strip()
    if given:
        return given
    if _generated is None:
        _generated = secrets.token_urlsafe(32)
    return _generated


def was_generated() -> bool:
    """パスワードを環境から受け取ったのではなく、こちらで生成したか。"""
    return not (os.environ.get(_ENV_VAR) or "").strip()
