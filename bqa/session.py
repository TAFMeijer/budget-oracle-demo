"""Signed session tokens, so a page refresh does not sign the user out.

Streamlit keeps session state only for the life of one websocket connection; a refresh starts
a new one. The browser therefore holds a cookie carrying a token the app can trust without any
server-side store:

    <email, base64url> . <expiry, unix seconds> . <HMAC-SHA256 of the two, hex>

The secret behind the HMAC never leaves the server. Anyone can read the token, nobody can forge
or alter one, and a token stops working at its expiry or the moment its address leaves the
allow-list — the app re-checks the list every time it restores a session.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
import time
from pathlib import Path

COOKIE_NAME = "bqa_session"


def load_or_create_secret(configured: str, path: Path) -> bytes:
    """The HMAC key: SESSION_SECRET from the environment if set, otherwise a random key kept in
    `path` so sessions survive a restart. Regenerating it would sign everyone out, nothing worse."""
    if configured:
        return configured.encode("utf-8")
    try:
        if path.is_file():
            data = path.read_text(encoding="utf-8").strip()
            if len(data) >= 32:
                return data.encode("utf-8")
        path.parent.mkdir(parents=True, exist_ok=True)
        data = secrets.token_urlsafe(48)
        path.write_text(data, encoding="utf-8")
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return data.encode("utf-8")
    except OSError:
        # A read-only disk: sessions then last for this process only, which is still correct.
        return secrets.token_bytes(48)


def _b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode("utf-8")).decode("ascii").rstrip("=")


def _unb64(s: str) -> str:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)).decode("utf-8")


def _sign(secret: bytes, payload: str) -> str:
    return hmac.new(secret, payload.encode("ascii"), hashlib.sha256).hexdigest()


def make_token(secret: bytes, email: str, ttl_seconds: int, now: float | None = None) -> str:
    now = time.time() if now is None else now
    payload = f"{_b64(email.strip().lower())}.{int(now + ttl_seconds)}"
    return f"{payload}.{_sign(secret, payload)}"


def read_token(secret: bytes, token: str | None, allowed: set[str], now: float | None = None) -> str | None:
    """The e-mail a token vouches for, or None if it is missing, forged, expired or no longer listed."""
    if not token:
        return None
    now = time.time() if now is None else now
    parts = token.split(".")
    if len(parts) != 3:
        return None
    email_b64, exp, sig = parts
    payload = f"{email_b64}.{exp}"
    if not hmac.compare_digest(_sign(secret, payload), sig):
        return None
    try:
        if int(exp) <= now:
            return None
        email = _unb64(email_b64)
    except (ValueError, UnicodeDecodeError):
        return None
    return email if email in allowed else None
