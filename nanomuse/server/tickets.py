"""Signed links for the bytes the app shows inline — a picture in the chat, the avatar's
face, a browser frame.

An ``<img>`` cannot send a header, and a token in the URL ends up in every access log
between the browser and the runtime (the showcase's proxy, a reverse proxy at home, the
browser's own history). So the web app signs such links itself with the token it holds::

    /api/files/<path>?exp=<unix seconds>&sig=<HMAC-SHA256(token, "<exp>\\n<path>")[:32]>

A logged link opens that one path until ``exp`` — never another path, never the API, and
the token cannot be read back from it. The client picks ``exp`` on a six-hour boundary
6–12 hours out (:func:`expiry`), so a link stays the same string for hours and the browser's
cache keeps working; the server accepts nothing further ahead than that.

The same arithmetic lives in ``web/src/ticket.ts``.
"""

from __future__ import annotations

import hashlib
import hmac
import time

BUCKET_S = 6 * 3600
"""Expiries fall on these boundaries, so a link's text is stable for hours at a time."""

MAX_AHEAD_S = 13 * 3600
"""The furthest ``exp`` the server takes: two buckets plus a little clock skew."""

SIG_HEX = 32
"""Hex characters of the MAC kept in the link: 128 bits."""


def sign(token: str, path: str, exp: int) -> str:
    """The signature of ``path`` (the decoded request path, ``/api/files/…``) until ``exp``."""
    mac = hmac.new(token.encode(), f"{exp}\n{path}".encode(), hashlib.sha256).hexdigest()
    return mac[:SIG_HEX]


def expiry(now: float | None = None) -> int:
    """The expiry a client picks now: the second six-hour boundary ahead."""
    now = time.time() if now is None else now
    return (int(now) // BUCKET_S + 2) * BUCKET_S


def check(
    token: str, path: str, exp: str | None, sig: str | None, now: float | None = None
) -> bool:
    """Whether ``?exp=&sig=`` opens ``path`` right now."""
    if not exp or not sig or not exp.isdigit() or len(sig) != SIG_HEX:
        return False
    now = time.time() if now is None else now
    when = int(exp)
    if when <= now or when > now + MAX_AHEAD_S:
        return False
    return hmac.compare_digest(sign(token, path, when).encode(), sig.encode())


__all__ = ["BUCKET_S", "MAX_AHEAD_S", "check", "expiry", "sign"]
