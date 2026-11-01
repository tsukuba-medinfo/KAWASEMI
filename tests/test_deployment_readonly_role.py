"""DEPLOYMENT.md の読み取り専用ロールの SQL が、認証・秘密情報の表を外していること。

`sql_extract` は読み取り専用ロールが読める表をすべて読める。手順書の `REVOKE` から
認証の表が漏れると、`SELECT password_hash FROM users` が通る。モデルに表が増えたときに
手順書が追従しているかをここで見る。
"""

from __future__ import annotations

import re
from pathlib import Path

from kawasemi.ai_input import models as _ai_input_models  # noqa: F401 - 全表を登録する
from kawasemi.models import Base

DEPLOYMENT = Path(__file__).resolve().parents[1] / "docs" / "DEPLOYMENT.md"

# 認証・権限を持つモデルのモジュール。表を足したらここに入る（モジュール単位で拾う）
AUTH_MODULES = {
    "kawasemi.models.user",
    "kawasemi.models.role",
    "kawasemi.models.refresh_token",
    "kawasemi.models.auth_event",
}
# 列名から秘密情報を拾う（別モジュールに置かれた場合の取りこぼし対策）
SECRET_COLUMN = re.compile(r"password|secret|token|jti|hash|api_key", re.IGNORECASE)


def revoked_tables() -> set[str]:
    text = DEPLOYMENT.read_text(encoding="utf-8")
    match = re.search(r"REVOKE ALL ON ([\w, ]+) FROM kawasemi_ro;", text)
    assert match, "DEPLOYMENT.md に REVOKE ALL ON ... FROM kawasemi_ro; が無い"
    return {name.strip() for name in match.group(1).split(",")}


def sensitive_tables() -> set[str]:
    tables = set()
    for mapper in Base.registry.mappers:
        if mapper.class_.__module__ in AUTH_MODULES:
            tables.add(mapper.local_table.name)
    for name, table in Base.metadata.tables.items():
        if any(SECRET_COLUMN.search(c.name) for c in table.columns):
            tables.add(name)
    return tables


def test_auth_and_secret_tables_are_revoked() -> None:
    missing = sensitive_tables() - revoked_tables()
    assert not missing, f"DEPLOYMENT.md の REVOKE に足りない表: {sorted(missing)}"


def test_revoked_tables_exist() -> None:
    unknown = revoked_tables() - set(Base.metadata.tables)
    assert not unknown, f"REVOKE にモデルに無い表がある（綴り違い？）: {sorted(unknown)}"


def test_the_minimal_role_does_not_use_default_privileges() -> None:
    """既定権限で新しい表を自動で読ませると、秘密の表が増えたときに漏れる。"""
    text = DEPLOYMENT.read_text(encoding="utf-8")
    block = text[text.index("CREATE ROLE kawasemi_ro") :]
    block = block[: block.index("```")]
    assert "ALTER DEFAULT PRIVILEGES" not in block
