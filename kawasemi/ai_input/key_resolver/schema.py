"""被験者ID解決(resolver)の契約スキーマ

KAWASEMI は名寄せ値を保持も受領もしない。resolver は (study, subject) と要求
key_type 群を受け、各 key_type の canonical 値 or 不在/曖昧を返す。

**HTTP 境界の受け皿（`ResolveRequest` / `ResolveResponse`）は削除した**。
別プロセス resolverと共有するためだけの型で、使う実装が無くなったため。
名寄せ系を別プロセスへ隔離する要求が出た時点で、その境界と一緒に定義し直す。

設計: バッチ・(study,subject)キー・found/not_found/ambiguous
"""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel


class ResolveStatus(str, Enum):
    """解決結果の状態。ambiguous は自動選択せずエラー扱い（取り違え防止）。"""

    FOUND = "found"
    NOT_FOUND = "not_found"
    AMBIGUOUS = "ambiguous"


class ResolveContext(BaseModel):
    """解決コンテキスト。登録番号は試験内一意のため study を必須にする。"""

    study_id: str


class ResolvedKey(BaseModel):
    """1 key_type の解決結果。value は canonical（found 時のみ）。"""

    key_type: str
    status: ResolveStatus
    value: str | None = None
