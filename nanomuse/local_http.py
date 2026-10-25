"""HTTP to our own server, never through the environment's proxy.

``urllib.request.urlopen`` reads ``HTTP_PROXY``, ``HTTPS_PROXY`` and ``ALL_PROXY`` (and, on
Windows, the registry), so a request meant for ``127.0.0.1`` went to whatever proxy the
machine had set. The operator's bearer token and the bridge's call token rode along with it,
a proxy that does not forward loopback broke the hands and the bridge outright, and
``nanomuse serve`` called a running nanoMuse "another program". Everything that talks to a
loopback address goes through :func:`local_open` here.

A remote host keeps the environment's proxy: that is what it is set for. Only our own server
lives in this module.

The standard library only, because ``nanomuse-device`` and its two neighbours import this
and must stay cheap on a phone.
"""

from __future__ import annotations

import urllib.request
from typing import Any

_opener: urllib.request.OpenerDirector | None = None


def local_opener() -> urllib.request.OpenerDirector:
    """An opener with an empty proxy handler: nothing from the environment is consulted."""
    global _opener
    if _opener is None:
        _opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return _opener


def local_open(url: str | urllib.request.Request, timeout: float | None = None) -> Any:
    """``urllib.request.urlopen`` for a loopback address: the same exceptions, no proxy."""
    if timeout is None:
        return local_opener().open(url)
    return local_opener().open(url, timeout=timeout)


__all__ = ["local_open", "local_opener"]
