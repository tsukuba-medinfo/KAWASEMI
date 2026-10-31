"""入り口：使う人ごとに画面と権限の範囲を分ける。

**画面と権限は使う人ごとに分ける。コードとデータは分けない。** 同じコードを `ENTRY_ROLES` を
変えて複数のプロセスで起動し、nginx で URL や port を振り分ける。各プロセスは同じ DB を使う。

入り口が決めるのは次の3つだけで、どれも `User.switchable_role_names()` を通して効く。

- その入り口で**アクティブになれる役割**（ヘッダの役割切替・既定の役割・`can_act_as`）
- 使えない役割の画面へのナビ項目を、別の入り口への案内に変える（`template_globals.nav_items`）
- 役割不足の 403 で、「別の入り口へ」を案内する（`api/main.py: role_required_handler`）

`ENTRY_ROLES` が空なら入り口の区別は無く、従来どおり全役割を切り替えて使える。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from kawasemi.core.config import settings


@dataclass(frozen=True)
class Entry:
    name: str
    roles: tuple[str, ...]  # 空＝全役割
    urls: dict[str, str]  # 役割 → その役割の入り口の URL

    @property
    def restricted(self) -> bool:
        return bool(self.roles)

    def allows(self, role: str | None) -> bool:
        return not self.roles or (role is not None and role in self.roles)

    def allows_any(self, roles: tuple[str, ...] | list[str]) -> bool:
        return not self.roles or any(r in self.roles for r in roles)

    def filter_roles(self, roles: list[str]) -> list[str]:
        return [r for r in roles if self.allows(r)]

    def url_for(self, roles: tuple[str, ...] | list[str]) -> tuple[str, str] | None:
        """使えない役割の画面へ誘導する先（役割, URL）。URL が設定されている最初の役割。"""
        for r in roles:
            if r in self.urls:
                return r, self.urls[r]
        return None


def parse_entry(name: str, roles_raw: str, urls_raw: str) -> Entry:
    """設定文字列から入り口を組み立てる。役割名の誤りは起動時に止める（黙って全役割にしない）。"""
    from kawasemi.models.role import RoleType

    roles = tuple(r.strip() for r in roles_raw.split(",") if r.strip())
    unknown = [r for r in roles if r not in RoleType.ALL]
    if unknown:
        raise ValueError(
            f"ENTRY_ROLES に無い役割名がある: {unknown}（使える値: {list(RoleType.ALL)}）"
        )
    urls: dict[str, str] = {}
    for part in (p.strip() for p in urls_raw.split(",") if p.strip()):
        role, sep, url = part.partition("=")
        role, url = role.strip(), url.strip()
        if not sep or not url or role not in RoleType.ALL:
            raise ValueError(
                f"ENTRY_URLS は `役割=URL` の形で書く（役割は RoleType.ALL）: {part!r}"
            )
        urls[role] = url
    return Entry(name=name.strip(), roles=roles, urls=urls)


_lock = threading.Lock()
_cache: Entry | None = None


def current_entry() -> Entry:
    """この起動の入り口（設定から1回だけ組み立てる）。"""
    global _cache
    if _cache is None:
        with _lock:
            if _cache is None:
                _cache = parse_entry(settings.entry_name, settings.entry_roles, settings.entry_urls)
    return _cache


def reset_entry_cache() -> None:
    """テスト用：設定を差し替えたあとに読み直す。"""
    global _cache
    with _lock:
        _cache = None
