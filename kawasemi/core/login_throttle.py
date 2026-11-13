"""ログイン失敗のスロットル（(ユーザー名, クライアント IP) 単位）。

**アカウント単位のロックは使わない。** 認証前の誰もが任意のユーザー名に対して失敗を
積めるので、アカウント単位でロックすると (1) 第三者が既知のアカウント（admin を含む）を
締め出し続けられ、(2) ロック済みを示す応答の違いで有効なユーザー名が分かる。
ここでは失敗を **(ユーザー名, IP)** の組で数え、閾値に達した組だけを一定時間受け付けない。
攻撃者が締め出せるのは自分の IP からの試行だけで、本人の別 IP には影響しない。
存在しないユーザー名も同じように数えるので、数え方に有効なユーザー名の手掛かりが出ない。

- ブロック中は失敗を積み増さない（延長しない＝締め出しが際限なく伸びない）。
- 状態はプロセス内メモリである。複数ワーカーではワーカーごとに数えるので、実効の上限は
  ワーカー数倍になる（`core/rate_limit.py` の slowapi と同じ制約）。再起動で消える。
- 件数の上限を持つ（ユーザー名を変えながらの大量の試行でメモリを食わせない）。

クライアント IP は `request.client.host`。リバースプロキシ配下では uvicorn の
`--proxy-headers` と `FORWARDED_ALLOW_IPS` が必要（docs/reverse_proxy.md）。無いと全員が
プロキシの IP になり、一人の失敗が全員のブロックになる。
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

_MAX_ENTRIES = 10_000


@dataclass
class _Entry:
    failures: int
    window_started: float
    blocked_until: float = 0.0


class LoginThrottle:
    """(ユーザー名, IP) ごとの失敗回数とブロック期限。"""

    def __init__(self, max_entries: int = _MAX_ENTRIES) -> None:
        self._entries: dict[tuple[str, str], _Entry] = {}
        self._max_entries = max_entries
        self._lock = threading.Lock()

    def is_blocked(self, key: tuple[str, str], now: float | None = None) -> bool:
        """この組が今ブロック中か。"""
        now = time.monotonic() if now is None else now
        with self._lock:
            entry = self._entries.get(key)
            return entry is not None and entry.blocked_until > now

    def register_failure(
        self,
        key: tuple[str, str],
        *,
        max_attempts: int,
        window_seconds: float,
        now: float | None = None,
    ) -> bool:
        """失敗を1回数える。**この失敗でブロックに入ったとき**だけ True。

        ブロック中の失敗は数えない（ブロックを延長しない）。窓（`window_seconds`）を
        過ぎた古い失敗は忘れる。ブロックの長さも `window_seconds` と同じである。
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry.blocked_until > now:
                return False
            if entry is None or now - entry.window_started > window_seconds:
                self._make_room(now, window_seconds)
                entry = _Entry(failures=0, window_started=now)
                self._entries[key] = entry
            entry.failures += 1
            if entry.failures >= max_attempts:
                entry.blocked_until = now + window_seconds
                # 次の窓は、ブロックが明けたあとの最初の失敗から数え直す
                entry.failures = 0
                entry.window_started = entry.blocked_until
                return True
            return False

    def reset(self, key: tuple[str, str]) -> None:
        """認証に成功したら、その組の失敗を忘れる。"""
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        """全部忘れる（テスト用）。"""
        with self._lock:
            self._entries.clear()

    def _make_room(self, now: float, window_seconds: float) -> None:
        """件数が上限に達したら、役目を終えた組を捨て、それでも足りなければ最古を捨てる。"""
        if len(self._entries) < self._max_entries:
            return
        expired = [
            k
            for k, e in self._entries.items()
            if e.blocked_until <= now and now - e.window_started > window_seconds
        ]
        for k in expired:
            del self._entries[k]
        if len(self._entries) >= self._max_entries:
            oldest = min(self._entries, key=lambda k: self._entries[k].window_started)
            del self._entries[oldest]


login_throttle = LoginThrottle()
