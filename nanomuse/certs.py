"""Where TLS finds its trust roots.

httpx carries certifi and verifies with it; the standard library's ``ssl`` (the hub's
WebSocket, smtplib, imaplib) verifies with OpenSSL's default paths instead — which,
in a bundled runtime, point at the build machine's Python. On a Mac without that
Python the file is not there, the store is empty, and the relay's certificate cannot
be verified while the very same relay answers over httpx. ``ensure_ca_bundle`` points
OpenSSL at certifi's bundle when its own paths hold nothing, before any context is made.
"""

from __future__ import annotations

import os
import ssl
from pathlib import Path


def ensure_ca_bundle() -> str:
    """Set ``SSL_CERT_FILE`` to certifi's bundle when OpenSSL's default paths are empty and
    nothing was configured; returns the bundle in use ('' when the system's own store is)."""
    if os.environ.get("SSL_CERT_FILE") or os.environ.get("SSL_CERT_DIR"):
        return os.environ.get("SSL_CERT_FILE", "")
    if _system_store_present():
        return ""
    try:
        import certifi
    except ImportError:  # pragma: no cover — certifi comes with httpx
        return ""
    bundle = certifi.where()
    if Path(bundle).is_file():
        os.environ["SSL_CERT_FILE"] = bundle
        return bundle
    return ""


def _system_store_present() -> bool:
    # Windows and macOS system stores are read by load_default_certs() through the OS, not
    # through these paths; on Windows that works in a bundled runtime, on macOS it does not
    # (python.org builds do not link the Security framework), hence the file check there too.
    if os.name == "nt":  # not sys.platform: mypy on Windows would call the rest unreachable
        return True
    paths = ssl.get_default_verify_paths()
    for candidate in (paths.cafile, paths.capath):
        if candidate and Path(candidate).exists():
            return True
    return False


__all__ = ["ensure_ca_bundle"]
