"""Signed student-link tokens.

A student reaches their personal peer-evaluation form through a link that carries
a compact, tamper-proof token: `?t=<body>.<signature>`, where the body is a
URL-safe base64 JSON payload `{"s": slug, "t": team, "p": position}` and the
signature is an HMAC-SHA256 over the body using a server-side secret.

No login, nothing to remember, and no student can forge a link for someone else
or edit the URL to reach another team — a bad signature is simply rejected.
Changing the secret invalidates every link already sent.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Optional


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(body: str, secret: str) -> str:
    digest = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).digest()
    return _b64e(digest)


def make_token(payload: dict, secret: str) -> str:
    """Serialize + sign a payload into a URL-safe token."""
    raw = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    body = _b64e(raw)
    return f"{body}.{_sign(body, secret)}"


def read_token(token: str, secret: str) -> Optional[dict]:
    """Verify a token and return its payload, or None if invalid/tampered."""
    if not token or "." not in token:
        return None
    body, _, sig = token.partition(".")
    if not hmac.compare_digest(sig, _sign(body, secret)):
        return None
    try:
        return json.loads(_b64d(body).decode("utf-8"))
    except Exception:
        return None
