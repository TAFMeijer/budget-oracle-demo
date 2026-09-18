"""Brute-force protection for the sign-in form.

The sign-in is an e-mail on an allow-list plus a shared access code. Both are short secrets, so
the only real defence is making guessing slow. Three limits work together, all in memory of the
single app process:

  * per client IP        - failures from one address lock that address out, for longer each time
  * per e-mail address   - the same, so a distributed attacker cannot hammer one account
  * global circuit break - too many failures from everyone at once locks the form for everybody

A lockout doubles on every repeat (base -> 2x -> 4x ...) up to a cap, and failure counts are
forgotten after a quiet window. Successful sign-in clears the counters for that IP and e-mail.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    failures: list[float] = field(default_factory=list)   # timestamps of recent failures
    locked_until: float = 0.0
    lockouts: int = 0                                      # consecutive lockouts -> backoff exponent


@dataclass
class LoginLimiter:
    max_failures: int = 5          # failures within `window` before a lockout
    window: float = 600.0          # seconds a failure stays counted
    lockout: float = 60.0          # first lockout length; doubles each time
    max_lockout: float = 3600.0    # cap on the doubling
    global_max_failures: int = 30  # failures from everyone within `window` -> everyone locked
    global_lockout: float = 300.0

    _buckets: dict[str, _Bucket] = field(default_factory=dict)
    _global: _Bucket = field(default_factory=_Bucket)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # ---- queries --------------------------------------------------------------------------
    def retry_after(self, ip: str, email: str, now: float | None = None) -> float:
        """Seconds until a sign-in attempt from this ip/e-mail will be considered; 0 = now."""
        now = time.time() if now is None else now
        with self._lock:
            waits = [self._global.locked_until - now]
            for key in self._keys(ip, email):
                b = self._buckets.get(key)
                if b is not None:
                    waits.append(b.locked_until - now)
        return max(0.0, max(waits))

    # ---- updates --------------------------------------------------------------------------
    def failure(self, ip: str, email: str, now: float | None = None) -> float:
        """Record a failed attempt. Returns the lockout now in force (seconds), 0 if none yet."""
        now = time.time() if now is None else now
        with self._lock:
            wait = 0.0
            for key in self._keys(ip, email):
                b = self._buckets.setdefault(key, _Bucket())
                wait = max(wait, self._count(b, now, self.max_failures, self.lockout, self.max_lockout))
            wait = max(wait, self._count(self._global, now, self.global_max_failures,
                                         self.global_lockout, self.global_lockout))
            self._prune(now)
            return wait

    def success(self, ip: str, email: str) -> None:
        with self._lock:
            for key in self._keys(ip, email):
                self._buckets.pop(key, None)

    # ---- internals ------------------------------------------------------------------------
    @staticmethod
    def _keys(ip: str, email: str) -> list[str]:
        keys = []
        if ip:
            keys.append(f"ip:{ip}")
        if email:
            keys.append(f"email:{email.strip().lower()}")
        return keys or ["ip:unknown"]

    def _count(self, b: _Bucket, now: float, limit: int, base: float, cap: float) -> float:
        b.failures = [t for t in b.failures if now - t < self.window]
        b.failures.append(now)
        if len(b.failures) < limit:
            return max(0.0, b.locked_until - now)
        # Over the limit: lock, and lock for longer than last time.
        b.lockouts += 1
        b.locked_until = now + min(cap, base * (2 ** (b.lockouts - 1)))
        b.failures = []
        return b.locked_until - now

    def _prune(self, now: float) -> None:
        """Forget buckets that carry nothing: keeps memory bounded under a long attack."""
        dead = [k for k, b in self._buckets.items()
                if b.locked_until <= now and not any(now - t < self.window for t in b.failures)
                and (b.lockouts == 0 or now - b.locked_until > self.max_lockout)]
        for k in dead:
            del self._buckets[k]


@dataclass
class QuestionLimiter:
    """How much of the model a visitor, and everyone together, may use — for a deployment with
    no sign-in, where anyone on the internet can ask.

      * per client IP   - `per_ip` questions per `window`; the address is Cloudflare's
                          CF-Connecting-IP behind the tunnel, which a visitor cannot forge
      * everyone        - `global_max` questions per `window`, so a botnet cannot do with many
                          addresses what one address may not
      * at once         - `concurrent` questions being answered at the same time; a thinking
                          answer holds the model for tens of seconds, and a queue of them would
                          make the app useless for everyone else

    0 switches a limit off. `acquire` records the question and takes a slot only when it returns
    no refusal; the caller must `release` exactly then. All in memory of the single app process.
    """
    per_ip: int = 0
    global_max: int = 0
    window: float = 3600.0
    concurrent: int = 0

    _hits: dict[str, list[float]] = field(default_factory=dict)
    _all: list[float] = field(default_factory=list)
    _running: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def acquire(self, ip: str, now: float | None = None) -> str:
        """'' when the question may go ahead (slot taken, question counted), else the refusal."""
        now = time.time() if now is None else now
        ip = ip or "unknown"
        with self._lock:
            self._all = [t for t in self._all if now - t < self.window]
            mine = [t for t in self._hits.get(ip, []) if now - t < self.window]
            if self.per_ip and len(mine) >= self.per_ip:
                wait = self.window - (now - mine[0])
                return (f"This demo allows {self.per_ip} questions per {_span(self.window)} from one connection, "
                        f"and you have used them. You can ask again in {_span(wait)}.")
            if self.global_max and len(self._all) >= self.global_max:
                wait = self.window - (now - self._all[0])
                return f"The demo has reached its limit for everyone for now. Please try again in {_span(wait)}."
            if self.concurrent and self._running >= self.concurrent:
                return "The assistant is busy answering other questions. Please try again in a moment."
            mine.append(now)
            self._hits[ip] = mine
            self._all.append(now)
            self._running += 1
            if len(self._hits) > 10_000:             # bounded memory under a long spray of addresses
                self._hits = {k: v for k, v in self._hits.items() if any(now - t < self.window for t in v)}
            return ""

    def release(self) -> None:
        with self._lock:
            self._running = max(0, self._running - 1)


def _span(seconds: float) -> str:
    m = int(round(seconds / 60))
    if m >= 120:
        return f"{round(m / 60)} hours"
    if m >= 60:
        return "an hour"
    return f"{max(1, m)} minute{'s' if m > 1 else ''}"


def client_ip(headers, fallback: str = "") -> str:
    """The visitor's address as seen through the proxies in front of the app.

    Cloudflare sets CF-Connecting-IP on every request it forwards; that is the most trustworthy
    value when the app is only reachable through the tunnel. X-Forwarded-For is the generic
    equivalent for any other proxy. Without either, the socket peer is all there is.
    """
    try:
        for name in ("Cf-Connecting-Ip", "CF-Connecting-IP", "X-Forwarded-For"):
            v = headers.get(name) if headers is not None else None
            if v:
                return str(v).split(",")[0].strip()
    except Exception:  # noqa: BLE001 - headers may be unavailable outside a request
        pass
    return fallback or "unknown"
