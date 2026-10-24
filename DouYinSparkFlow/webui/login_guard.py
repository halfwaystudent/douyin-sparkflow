"""Brute-force protection and password policy for the Web console login.

Two in-memory counters (the console runs as one uvicorn process):

1. Per (client IP, username): 5 failures within 15 minutes lock that pair for
   15 minutes. Stops one source from guessing.
2. Per username, across all IPs: after 10 failures within 15 minutes the
   username enters "slow mode" — at most one attempt per 30 seconds from
   anywhere. This caps an attacker rotating source IPs at ~120 guesses per
   hour, while the owner is never locked for more than 30 seconds by it.

There is deliberately no IP-wide lock and no hard per-username lock: either
would let a stranger (on the owner's NAT, or anywhere) lock the owner out.
A pair lock can still be triggered by someone sharing the owner's public IP
who knows the username; that costs the owner at most 15 minutes and is an
accepted trade-off for this console.

Memory is bounded: usernames/IPs are truncated, expired entries are swept,
and each table holds at most MAX_KEYS entries. In both tables, entries that
are actively protecting (a pair lock, a username in slow mode) are never
evicted; other entries are evicted oldest first. If a table is entirely
protected entries, a new key is simply not tracked in that table (the other
counter still applies).
A successful login clears that pair and ends slow mode for that username.
The client IP is the TCP peer; uvicorn only trusts forwarding headers from
127.0.0.1.
"""

from __future__ import annotations

import re
import threading
import time
from collections import OrderedDict

PAIR_LIMIT = 5
WINDOW_SECONDS = 15 * 60
LOCK_SECONDS = 15 * 60
USER_SLOW_AFTER = 10
USER_SLOW_INTERVAL_SECONDS = 30
MAX_USERNAME_CHARS = 64
MAX_KEYS = 4096
_SWEEP_INTERVAL_SECONDS = 60

PASSWORD_MIN_LENGTH = 10


class LoginThrottle:
    def __init__(self, *, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        # (ip, user) -> {"failures": [t, ...], "locked_until": float | None}
        self._pairs: OrderedDict[tuple, dict] = OrderedDict()
        # user -> {"failures": [t, ...], "last": float}
        self._users: OrderedDict[str, dict] = OrderedDict()
        self._last_sweep = float("-inf")

    @staticmethod
    def _user(username):
        return str(username or "").strip()[:MAX_USERNAME_CHARS].casefold()

    @classmethod
    def _pair_key(cls, ip, username):
        return (str(ip or "")[:64], cls._user(username))

    @staticmethod
    def _live_pair(entry, now):
        entry["failures"] = [t for t in entry["failures"] if now - t < WINDOW_SECONDS]
        if entry["locked_until"] is not None and entry["locked_until"] <= now:
            entry["locked_until"] = None
        return bool(entry["failures"]) or entry["locked_until"] is not None

    @staticmethod
    def _live_user(entry, now):
        entry["failures"] = [t for t in entry["failures"] if now - t < WINDOW_SECONDS]
        return bool(entry["failures"])

    def _sweep(self, now):
        if now - self._last_sweep < _SWEEP_INTERVAL_SECONDS:
            return
        self._last_sweep = now
        for key in [k for k, e in self._pairs.items() if not self._live_pair(e, now)]:
            del self._pairs[key]
        for key in [k for k, e in self._users.items() if not self._live_user(e, now)]:
            del self._users[key]

    def _pair_room(self):
        """True when a new pair entry may be added (evicting if needed)."""
        if len(self._pairs) < MAX_KEYS:
            return True
        for key in list(self._pairs):
            if self._pairs[key]["locked_until"] is None:
                del self._pairs[key]
                return True
        return False  # every entry is an active lock: never evict one

    def _user_room(self, now):
        """True when a new username entry may be added (evicting if needed).

        Entries in slow mode are never evicted: otherwise spraying fresh
        usernames would clear the cross-IP cap on the targeted one. When every
        slot is in slow mode, the new username simply is not tracked here (its
        per-pair limit still applies).
        """
        if len(self._users) < MAX_KEYS:
            return True
        for key in list(self._users):
            entry = self._users[key]
            if not self._live_user(entry, now) or len(entry["failures"]) < USER_SLOW_AFTER:
                del self._users[key]
                return True
        return False

    def _user_wait(self, user, now):
        entry = self._users.get(user)
        if entry is None or not self._live_user(entry, now):
            return 0.0
        if len(entry["failures"]) < USER_SLOW_AFTER:
            return 0.0
        return max(0.0, entry["last"] + USER_SLOW_INTERVAL_SECONDS - now)

    def retry_after(self, ip, username):
        """Seconds until this attempt may be tried, or 0 when allowed."""
        now = self._clock()
        key = self._pair_key(ip, username)
        with self._lock:
            wait = self._user_wait(key[1], now)
            entry = self._pairs.get(key)
            if entry is not None:
                if not self._live_pair(entry, now):
                    del self._pairs[key]
                elif entry["locked_until"] is not None:
                    wait = max(wait, entry["locked_until"] - now)
            return int(wait + 0.999) if wait > 0 else 0

    def record_failure(self, ip, username):
        now = self._clock()
        key = self._pair_key(ip, username)
        with self._lock:
            self._sweep(now)
            user_entry = self._users.get(key[1])
            if user_entry is None and self._user_room(now):
                user_entry = {"failures": [], "last": now}
                self._users[key[1]] = user_entry
            elif user_entry is not None:
                self._users.move_to_end(key[1])
                self._live_user(user_entry, now)
            if user_entry is not None:
                user_entry["failures"].append(now)
                user_entry["last"] = now

            entry = self._pairs.get(key)
            if entry is None:
                if not self._pair_room():
                    return
                entry = {"failures": [], "locked_until": None}
                self._pairs[key] = entry
            else:
                self._pairs.move_to_end(key)
                self._live_pair(entry, now)
            entry["failures"].append(now)
            if len(entry["failures"]) >= PAIR_LIMIT:
                entry["locked_until"] = now + LOCK_SECONDS
                entry["failures"] = []

    def record_success(self, ip, username):
        key = self._pair_key(ip, username)
        with self._lock:
            self._pairs.pop(key, None)
            self._users.pop(key[1], None)

    # Introspection for tests and diagnostics.
    def size(self):
        with self._lock:
            return len(self._pairs)

    def user_size(self):
        with self._lock:
            return len(self._users)

    def stored_chars(self):
        with self._lock:
            pair_chars = sum(len(ip) + len(user) for ip, user in self._pairs)
            return pair_chars + sum(len(user) for user in self._users)


def password_problem(password, username=""):
    """Chinese reason the password is too weak, or "" when it is acceptable.

    Applies to passwords being set now; existing passwords keep working.
    """
    value = str(password or "")
    if len(value) < PASSWORD_MIN_LENGTH:
        return f"密码至少需要 {PASSWORD_MIN_LENGTH} 位。"
    classes = sum(
        bool(re.search(pattern, value))
        for pattern in (r"[A-Za-z]", r"\d", r"[^A-Za-z\d]")
    )
    if classes < 2:
        return "密码需要同时包含字母、数字、符号中的至少两种。"
    if len(set(value)) < 4:
        return "密码过于简单，请避免重复字符。"
    name = str(username or "").strip().casefold()
    if name and name in value.casefold():
        return "密码不能包含用户名。"
    return ""
