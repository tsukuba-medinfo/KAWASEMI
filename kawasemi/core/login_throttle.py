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
- 件数にもバイト数にも上限を持つ。キーのユーザー名は SHA-256 に縮め（何文字の入力でも
  1 件 64 文字）、件数は上限までで、役目を終えた組は一定回数ごとに掃除する
  （ユーザー名を変えながらの大量の試行や巨大なユーザー名でメモリを食わせない）。

**IPv6 の送信元は /64 にまとめて数える**（`aggregate_ip`）。アドレスを変えるだけで数え直しに
なるのを防ぐ。ただし (ユーザー名, IP) の組の数え方は、送信元を大量に回せる攻撃者には効かない。
その対策は**ユーザー名だけ**で試行を直列に並べる `AccountBackoff`（ロックではなく段階的な遅延）である。

クライアント IP は `request.client.host`。リバースプロキシ配下では uvicorn の
`--proxy-headers` と `FORWARDED_ALLOW_IPS` が必要（docs/reverse_proxy.md）。無いと全員が
プロキシの IP になり、一人の失敗が全員のブロックになる。
"""

from __future__ import annotations

import hashlib
import ipaddress
import threading
import time
from dataclasses import dataclass

_MAX_ENTRIES = 10_000
# 役目を終えた組を掃除する間隔（register_failure の呼び出し回数）
_PURGE_EVERY = 256


def aggregate_ip(client_ip: str) -> str:
    """数える単位の IP。**IPv6 は /64 にまとめる**。

    IPv6 の利用者や攻撃者は、/64（1 つのサブネット）の中のアドレスを事実上いくらでも
    使える。アドレスごとに数えると、アドレスを変えるだけで失敗の数え直しと IP 単位の
    レート制限の回避ができる。IPv4（と IPv4 射影アドレス）はそのまま、解釈できない値も
    そのまま（長さだけ切る）返す。
    """
    try:
        ip = ipaddress.ip_address(client_ip.split("%", 1)[0])
    except ValueError:
        return client_ip[:64]
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped is not None:
            return str(ip.ipv4_mapped)
        return f"{ipaddress.ip_network(f'{ip}/64', strict=False).network_address}/64"
    return str(ip)


def username_digest(username: str) -> str:
    """ユーザー名の SHA-256（16進 64 文字）。入力の長さに依らず一定の大きさにする。"""
    return hashlib.sha256(username.encode("utf-8", "surrogatepass")).hexdigest()


def throttle_key(username: str, client_ip: str) -> tuple[str, str]:
    """スロットルのキー。**入力の長さに依らず一定の大きさ**にする。

    ユーザー名は認証前の入力そのもので、何万文字でも送れる。そのまま保存すると1件ごとに
    その長さのメモリを食うので、SHA-256 の16進（64文字）に縮める。IP は `aggregate_ip`
    （IPv6 は /64）にまとめ、長さも切る（通常は 45 文字以内の文字列だが、プロキシ設定の
    誤りで長い値が来ても有界にする）。
    """
    return (username_digest(username), aggregate_ip(client_ip)[:64])


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
        self._calls = 0

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
            # 定期的に、役目を終えた組を捨てる（上限に達するのを待たない）
            self._calls += 1
            if self._calls % _PURGE_EVERY == 0:
                self._purge_expired(now, window_seconds)
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

    def _purge_expired(self, now: float, window_seconds: float) -> None:
        """ブロックが明け、失敗を数える窓も過ぎた組を捨てる。"""
        expired = [
            k
            for k, e in self._entries.items()
            if e.blocked_until <= now and now - e.window_started > window_seconds
        ]
        for k in expired:
            del self._entries[k]

    def _make_room(self, now: float, window_seconds: float) -> None:
        """件数が上限に達したら、役目を終えた組を捨て、それでも足りなければ最古を捨てる。"""
        if len(self._entries) < self._max_entries:
            return
        self._purge_expired(now, window_seconds)
        if len(self._entries) >= self._max_entries:
            oldest = min(self._entries, key=lambda k: self._entries[k].window_started)
            del self._entries[oldest]


@dataclass
class _Backoff:
    attempts: int
    next_slot: float
    last: float


class AccountBackoff:
    """ユーザー名ごとの**段階的な遅延**（IP に依らない。ロックしない）。

    `LoginThrottle` は (ユーザー名, IP) の組で数えるので、送信元を回せる攻撃者には
    効かない。ここでは**ユーザー名だけ**で、試行を直列の「枠」に並べる。最初の `free`
    回は待たせず、以降は 1 回ごとに枠の間隔を `base` 秒から倍々に伸ばし、`cap` 秒で
    頭打ちにする。並列に送っても、枠は 1 つずつしか空かないので、1 アカウントへの推測の
    速さが IP の数で伸びない。

    - **試行を受けた時点で数える**（成否を待たない）。並列の試行が、同じ低い値を読んで
      すり抜けない。認証に成功したらそのユーザー名の記録を消す。
    - **存在しないユーザー名も同じに数える**ので、遅延から有効なユーザー名は分からない。
    - 待ち行列が `max_wait` 秒を超えるときは、`None` を返す（呼び出し側は、パスワードを調べず
      同じ失敗応答にする）。際限なく待たせてワーカーの接続を使い切らないための上限。
    - 最後の試行から `window` 秒経った記録は忘れる。件数にも上限がある（古いものから捨てる）。
    """

    def __init__(self, max_entries: int = _MAX_ENTRIES, window_seconds: float = 900.0) -> None:
        self._entries: dict[str, _Backoff] = {}
        self._max_entries = max_entries
        self._window = window_seconds
        self._lock = threading.Lock()
        self._calls = 0

    def reserve(
        self,
        key: str,
        *,
        free: int,
        base: float,
        cap: float,
        max_wait: float,
        now: float | None = None,
    ) -> float | None:
        """試行の枠を取る。待つべき秒数（0 なら待たない）、待ち行列が長すぎるなら None。"""
        if base <= 0:
            return 0.0
        now = time.monotonic() if now is None else now
        with self._lock:
            self._calls += 1
            if self._calls % _PURGE_EVERY == 0:
                self._purge(now)
            entry = self._entries.get(key)
            if entry is None or now - entry.last > self._window:
                self._make_room(now)
                entry = _Backoff(attempts=0, next_slot=now, last=now)
                self._entries[key] = entry
            slot = max(now, entry.next_slot)
            wait = slot - now
            if wait > max_wait:
                return None
            interval = (
                0.0 if entry.attempts < free else min(cap, base * 2 ** (entry.attempts - free))
            )
            entry.attempts += 1
            entry.last = now
            entry.next_slot = slot + interval
            return wait

    def reset(self, key: str) -> None:
        """認証に成功したら、そのユーザー名の記録を消す。"""
        with self._lock:
            self._entries.pop(key, None)

    def clear(self) -> None:
        """全部忘れる（テスト用）。"""
        with self._lock:
            self._entries.clear()

    def _purge(self, now: float) -> None:
        for k in [k for k, e in self._entries.items() if now - e.last > self._window]:
            del self._entries[k]

    def _make_room(self, now: float) -> None:
        if len(self._entries) < self._max_entries:
            return
        self._purge(now)
        if len(self._entries) >= self._max_entries:
            oldest = min(self._entries, key=lambda k: self._entries[k].last)
            del self._entries[oldest]


login_throttle = LoginThrottle()
account_backoff = AccountBackoff()
