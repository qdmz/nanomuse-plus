"""The desktop app's operator as this runtime's hands.

nanoMuse Desktop (Electron, ``harness/desktop``) runs the mouse, the keyboard and the
screenshot itself — UI-TARS-desktop's nut.js operator, ported — and offers them to the
runtime it starts over loopback HTTP: ``NANOMUSE_OPERATOR_URL`` and
``NANOMUSE_OPERATOR_TOKEN`` in the runtime's environment. The brain stays here (the agent
loop, the Sentinel, the tools); only the hands move over there. Why: the app is the process
macOS grants Accessibility and Screen Recording to, Electron knows the display's real
geometry, and the operator has been run on many desks already.

The contract (``harness/desktop/src/operator-server.ts``), JSON both ways, bearer token:

* ``GET /info`` → ``{available, reason, platform, display: {width, height, scaleFactor}}`` —
  ``display.width/height`` is the space the hands move in (X11 root pixels on Linux,
  points on macOS, physical pixels on Windows).
* ``POST /screenshot {width?, height?, format?, quality?, max_pixels?}`` → ``{base64, mime,
  width, height, screen: {width, height}, scaleFactor, display: {id, bounds}}`` — the
  primary display, scaled to the requested size (the picture the model sees; without one
  the screen's size under ``max_pixels``, 2 Mpx by default), ``screen`` the hands' space.
  On Linux under Wayland the operator refuses (503) with the same reason ``/info`` gives.
* ``POST /execute {action, x, y, x2, y2, dy, text, submit, clear, keys, seconds}`` →
  ``{ok, note}``; coordinates in the hands' space. Errors are 4xx/5xx with ``{error}``.
* macOS, with the app's helper *nanoMuse Computer Use* (``/info`` says
  ``helper.present``): ``GET /windows`` → ``{windows: [{id, pid, app, bundle_id, title,
  bounds: [x, y, w, h], layer, on_screen}]}`` and ``POST /window {id, max_pixels?,
  format?, quality?}`` → ``{base64, mime, width, height, window: {id, x, y, width,
  height}, scale}`` — what window mode (:mod:`nanomuse.computer.mac_window`) lists and
  captures through, so the runtime never captures the screen itself on a Mac under the app
  (the helper takes the picture with ScreenCaptureKit on macOS 14 and later).

Without the two variables (the CLI, the web app, Docker) nothing here is used and the
Python backends in :mod:`nanomuse.computer.hands` do the work as before.
"""

from __future__ import annotations

import json
import os
import platform
import sys
import urllib.error
import urllib.request
from typing import Any

from nanomuse.computer.coords import PICTURE_MAX_PIXELS, picture_size
from nanomuse.computer.hands import HandsUnavailable, open_application
from nanomuse.logger import logger

URL_ENV = "NANOMUSE_OPERATOR_URL"
TOKEN_ENV = "NANOMUSE_OPERATOR_TOKEN"  # noqa: S105 — the name of a variable, not a secret
MAX_BODY = 32 * 1024 * 1024  # a 4K PNG is a few MB; anything bigger is not a screenshot


class OperatorError(RuntimeError):
    """The operator refused or failed an action (its own words). ``status`` is the HTTP
    status it answered with (0 when it did not answer at all): 403 is a permission the
    person has to switch on, 404 a window that is gone, 503 the operator or its helper
    not being there."""

    def __init__(self, message: str, status: int = 0) -> None:
        super().__init__(message)
        self.status = status


def operator_env(env: dict[str, str] | None = None) -> tuple[str, str] | None:
    """``(url, token)`` from the environment, or None when the desktop app is not around."""
    e = os.environ if env is None else env
    url, token = e.get(URL_ENV, "").strip().rstrip("/"), e.get(TOKEN_ENV, "").strip()
    if not url or not token:
        return None
    return url, token


def _platform() -> str:
    # a function, so tests can stand on another platform and mypy checks every branch
    return sys.platform


def operator_owns_the_screen(env: dict[str, str] | None = None) -> bool:
    """Whether every screenshot and every move must go through the operator, with no
    Python fallback: macOS under the desktop app. The app bundle is the process macOS
    grants Screen Recording and Accessibility to; a fallback to ``mss`` / ``pyautogui`` in
    the runtime would mean a second TCC prompt (for a process the person never sees in the
    pane), a second error text, and a black picture handed on as if it were the screen.
    Linux and Windows keep the fallbacks (no such gate there), and so does every run
    without the app (no operator variables)."""
    return _platform() == "darwin" and operator_env(env) is not None


class OperatorClient:
    """Three calls over loopback, stdlib only (the frozen runtime carries no httpx here)."""

    def __init__(self, url: str, token: str, timeout: float = 20.0) -> None:
        self.url = url.rstrip("/")
        self._token = token
        self.timeout = timeout

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(body or {}).encode() if method == "POST" else None
        req = urllib.request.Request(
            self.url + path,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self._token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 — loopback, our own server
                raw = resp.read(MAX_BODY)
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = str(json.loads(exc.read(65536).decode() or "{}").get("error") or "")
            except Exception:  # noqa: BLE001 — not JSON; the status is all there is
                detail = ""
            raise OperatorError(detail or f"the operator answered {exc.code}", exc.code) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise OperatorError(f"the desktop operator is not reachable: {exc}") from exc
        try:
            parsed = json.loads(raw.decode())
        except ValueError as exc:
            raise OperatorError("the operator answered something that is not JSON") from exc
        if not isinstance(parsed, dict):
            raise OperatorError("the operator answered something that is not an object")
        return parsed

    def info(self) -> dict[str, Any]:
        return self._call("GET", "/info")

    def screenshot(
        self,
        width: int = 0,
        height: int = 0,
        fmt: str = "jpeg",
        quality: int = 80,
        max_pixels: int = PICTURE_MAX_PIXELS,
    ) -> dict[str, Any]:
        """The picture: ``width``×``height`` when both are given, else the screen's size
        under ``max_pixels`` (0 = the screen as it is), as ``fmt`` (``jpeg`` or ``png``)."""
        body: dict[str, Any] = {"format": fmt, "quality": quality, "max_pixels": int(max_pixels)}
        if width and height:
            body["width"], body["height"] = int(width), int(height)
        return self._call("POST", "/screenshot", body)

    def execute(self, action: dict[str, Any]) -> str:
        out = self._call("POST", "/execute", action)
        if not out.get("ok", False):
            raise OperatorError(str(out.get("error") or out.get("note") or "the action failed"))
        return str(out.get("note") or "")

    def windows(self) -> list[dict[str, Any]]:
        """The windows on screen, front to back (macOS with the helper; 503 elsewhere)."""
        out = self._call("GET", "/windows")
        found = out.get("windows")
        return [w for w in found if isinstance(w, dict)] if isinstance(found, list) else []

    def window(
        self, window_id: int, max_pixels: int = PICTURE_MAX_PIXELS, fmt: str = "png"
    ) -> dict[str, Any]:
        """One window's own pixels (``base64``, ``mime``, ``width``, ``height``) and its
        frame in points (``window: {id, x, y, width, height}``); 404 when it is gone."""
        body: dict[str, Any] = {"id": int(window_id), "max_pixels": int(max_pixels), "format": fmt}
        return self._call("POST", "/window", body)


def _size_of(obj: Any, default: tuple[int, int] = (0, 0)) -> tuple[int, int]:
    """``(width, height)`` out of an object such as ``{"width": 1920, "height": 1080}``."""
    if not isinstance(obj, dict):
        return default
    try:
        w, h = int(obj.get("width") or 0), int(obj.get("height") or 0)
    except (TypeError, ValueError):
        return default
    return (w, h) if w > 0 and h > 0 else default


def display_size(info: dict[str, Any]) -> tuple[int, int]:
    """The hands' screen space as ``/info`` states it (``display.width/height``)."""
    return _size_of(info.get("display"))


class OperatorHands:
    """The desktop app's hands, behind the :class:`~nanomuse.computer.hands.HandsBackend`
    verbs. Coordinates are the operator's screen pixels — :class:`ComputerLink` maps the
    model's picture pixels to them before calling."""

    name = "desktop"

    def __init__(self, client: OperatorClient) -> None:
        self.client = client
        try:
            info = client.info()
        except OperatorError as exc:
            raise HandsUnavailable(str(exc)) from exc
        if not info.get("available", False):
            raise HandsUnavailable(
                str(info.get("reason") or "the desktop operator is not available here")
            )
        self.platform = str(info.get("platform") or platform.system().lower())
        self._size = display_size(info)
        helper = info.get("helper")
        # macOS: the app's helper bundle is there — window mode lists and captures through it
        self.helper_present = isinstance(helper, dict) and helper.get("present") is True

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> OperatorHands:
        found = operator_env(env)
        if found is None:
            raise HandsUnavailable("the desktop operator is not running (no NANOMUSE_OPERATOR_URL)")
        return cls(OperatorClient(*found))

    def size(self) -> tuple[int, int]:
        """The hands' screen space (``display.width/height`` of ``/info``)."""
        return self._size

    # ------------------------------------------------------------------ the verbs
    def _run(self, action: str, **params: Any) -> None:
        try:
            self.client.execute({"action": action, **params})
        except OperatorError as exc:
            raise RuntimeError(str(exc)) from exc

    def click(self, x: float, y: float, button: str = "left", clicks: int = 1) -> None:
        action = {"left": "click", "right": "right_click", "middle": "middle_click"}.get(
            button, "click"
        )
        if button == "left" and clicks >= 2:
            action = "double_click"
        self._run(action, x=x, y=y)

    def move(self, x: float, y: float) -> None:
        self._run("move", x=x, y=y)

    def drag(self, x: float, y: float, x2: float, y2: float) -> None:
        self._run("drag", x=x, y=y, x2=x2, y2=y2)

    def scroll(self, x: float, y: float, dy: float) -> None:
        self._run("scroll", x=x, y=y, dy=dy)

    def type(self, text: str) -> None:
        self._run("type", text=text)

    def key(self, keys: list[str]) -> None:
        self._run("key", keys=list(keys))

    def open_app(self, name: str) -> str:
        # starting a program is the runtime's job on every platform (a .desktop lookup,
        # `open -a`, `start`); the operator only moves and types
        return open_application(name)


def operator_capture(
    client: OperatorClient, max_width: int = 0, fmt: str = "jpeg"
) -> dict[str, Any] | None:
    """One observation through the operator in the ``screen`` shape
    :meth:`nanomuse.phone.screen.Screen.from_device` takes: ``width``/``height`` are the
    picture's, ``screen_w``/``screen_h`` the hands' space. None when the operator has no
    picture (then the caller may try the Python capture)."""
    info = client.info()
    if not info.get("available", False):
        raise OperatorError(str(info.get("reason") or "the desktop operator is not available"))
    sw, sh = display_size(info)
    pw, ph = picture_size(sw, sh, max_width)
    shot = client.screenshot(pw, ph, fmt=fmt)
    data = str(shot.get("base64") or "")
    if not data:
        return None
    width, height = _size_of(shot, (pw, ph))
    sw, sh = _size_of(shot.get("screen"), (sw, sh))
    logger.debug("operator screenshot {}x{} of a {}x{} screen", width, height, sw, sh)
    return {
        "width": width,
        "height": height,
        "screen_w": sw,
        "screen_h": sh,
        "keyboard": False,
        "screenshot": data,
        "mime": str(shot.get("mime") or "image/jpeg"),
    }


__all__ = [
    "TOKEN_ENV",
    "URL_ENV",
    "OperatorClient",
    "OperatorError",
    "OperatorHands",
    "operator_capture",
    "operator_env",
    "operator_owns_the_screen",
]
