"""The hands on this computer: a mouse and a keyboard, driven by coordinates.

Three backends do the same eight things — click (left, right, middle, double), move, drag,
scroll, type, press keys, open an application:

* ``desktop``: the nanoMuse Desktop app's own operator (UI-TARS-desktop's nut.js operator,
  :mod:`nanomuse.computer.operator`), reached over loopback when the app started this
  runtime (``NANOMUSE_OPERATOR_URL``). Preferred whenever it is there: the app is the
  process the system grants input and screen capture to.
* ``pyautogui`` (``pip install 'nanomuse[hands]'``): every platform; its fail-safe stays on,
  so throwing the mouse into a screen corner aborts whatever the hands were doing — a stop
  button that needs no window.
* ``xdotool``: X11 only, no Python packages; also how non-ASCII text is typed on Linux when
  ``pyautogui`` cannot (it types by key codes, which have no 中文).

Which one is used is ``[hands] backend``: ``auto`` takes the desktop operator when the app
is around, then ``pyautogui`` when it is installed, then ``xdotool`` — except on macOS under
the desktop app, where it is the operator or its reason (a fallback would mean a second TCC
prompt for the runtime; :func:`nanomuse.computer.operator.operator_owns_the_screen`). Every backend moves
in its own screen space — X11 root pixels, macOS points, Windows physical pixels — and
says how big that space is (``size()``) so :class:`~nanomuse.computer.link.ComputerLink`
can map the model's picture pixels onto it (:mod:`nanomuse.computer.coords`). Wayland
sessions have neither a global cursor nor a screen to grab without a portal; the hands say
so instead of pretending.

Nothing here decides *whether* to act: :class:`~nanomuse.tools.computer.ComputerAct` carries
the words under the cursor to the Sentinel first, exactly like the phone's ``phone_act``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any, Protocol

from nanomuse.logger import logger

# Qwen / pyautogui-style key names → xdotool keysyms
_XDO_KEYS = {
    "ctrl": "ctrl",
    "control": "ctrl",
    "alt": "alt",
    "option": "alt",
    "shift": "shift",
    "cmd": "super",
    "command": "super",
    "win": "super",
    "super": "super",
    "meta": "super",
    "enter": "Return",
    "return": "Return",
    "esc": "Escape",
    "escape": "Escape",
    "backspace": "BackSpace",
    "delete": "Delete",
    "del": "Delete",
    "tab": "Tab",
    "space": "space",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "home": "Home",
    "end": "End",
    "pageup": "Page_Up",
    "page_up": "Page_Up",
    "pagedown": "Page_Down",
    "page_down": "Page_Down",
    "insert": "Insert",
    "capslock": "Caps_Lock",
    "printscreen": "Print",
}
_PYAUTO_KEYS = {
    "control": "ctrl",
    "option": "alt",
    "cmd": "command",
    "super": "win",
    "meta": "win",
    "return": "enter",
    "escape": "esc",
    "page_up": "pageup",
    "page_down": "pagedown",
    "del": "delete",
}


class HandsUnavailable(RuntimeError):
    """No way to move the mouse or type on this session (and why)."""


class HandsBackend(Protocol):
    name: str

    def click(self, x: float, y: float, button: str = "left", clicks: int = 1) -> None: ...
    def move(self, x: float, y: float) -> None: ...
    def drag(self, x: float, y: float, x2: float, y2: float) -> None: ...
    def scroll(self, x: float, y: float, dy: float) -> None: ...
    def type(self, text: str) -> None: ...
    def key(self, keys: list[str]) -> None: ...
    def open_app(self, name: str) -> str: ...


def hands_space(backend: Any) -> tuple[int, int] | None:
    """The size of the screen space a backend moves in, when it can say (``size()``);
    None for one that cannot (a fake, an older backend) — the capture's size stands in."""
    size = getattr(backend, "size", None)
    if not callable(size):
        return None
    try:
        w, h = size()
        w, h = int(w), int(h)
    except Exception:  # noqa: BLE001 — a backend that cannot measure is one without a say
        return None
    return (w, h) if w > 0 and h > 0 else None


def _normalise_key(key: str, table: dict[str, str]) -> str:
    k = key.strip()
    return table.get(k.lower(), k if len(k) > 1 else k.lower()) if k else k


def _import_pyautogui() -> Any:
    """``pyautogui``, importable without Tk. Its ``mouseinfo`` dependency *exits the
    interpreter* (``sys.exit``) when tkinter is missing on Linux — and the frozen runtime
    leaves Tk out — which took every request on the Linux desktop app down with it. Nothing
    here ever opens the MouseInfo window, so a stand-in module is registered first."""
    if "mouseinfo" not in sys.modules:
        try:
            import tkinter  # noqa: F401
        except ImportError:
            stub = types.ModuleType("mouseinfo")
            stub.MouseInfoWindow = lambda *args, **kwargs: None  # type: ignore[attr-defined]
            sys.modules["mouseinfo"] = stub
    import pyautogui

    return pyautogui


class PyAutoGUIHands:
    name = "pyautogui"

    def __init__(self) -> None:
        try:
            pyautogui = _import_pyautogui()
        except (Exception, SystemExit) as exc:  # noqa: BLE001 — import errors differ per platform
            raise HandsUnavailable(
                f"pyautogui is not available ({exc}); pip install 'nanomuse[hands]'"
            ) from exc
        pyautogui.FAILSAFE = True  # the mouse in a corner aborts: a physical Stop
        pyautogui.PAUSE = 0.05
        self._gui = pyautogui

    def size(self) -> tuple[int, int]:
        """The space ``moveTo`` takes: points on macOS (half a Retina capture), physical
        pixels on Windows (pyautogui makes the process DPI-aware), root pixels on X11."""
        w, h = self._gui.size()
        return int(w), int(h)

    def click(self, x: float, y: float, button: str = "left", clicks: int = 1) -> None:
        self._gui.click(x=x, y=y, button=button, clicks=clicks, interval=0.08)

    def move(self, x: float, y: float) -> None:
        self._gui.moveTo(x, y, duration=0.15)

    def drag(self, x: float, y: float, x2: float, y2: float) -> None:
        self._gui.moveTo(x, y, duration=0.1)
        self._gui.dragTo(x2, y2, duration=0.4, button="left")

    def scroll(self, x: float, y: float, dy: float) -> None:
        # pyautogui counts clicks, positive = up; the dialect counts pixels, positive = down
        clicks = max(1, min(30, int(abs(dy) / 40) or 1))
        self._gui.scroll(-clicks if dy > 0 else clicks, x=x, y=y)

    def type(self, text: str) -> None:
        if text.isascii():
            self._gui.write(text, interval=0.01)
            return
        # key codes have no 中文: paste through the clipboard, or xdotool on X11
        if sys.platform.startswith("linux") and shutil.which("xdotool"):
            XdotoolHands().type(text)
            return
        try:
            import pyperclip

            pyperclip.copy(text)
            self._gui.hotkey("command" if sys.platform == "darwin" else "ctrl", "v")
        except Exception as exc:  # noqa: BLE001
            raise HandsUnavailable(
                f"cannot type non-ASCII text on this system ({exc}); pip install pyperclip"
            ) from exc

    def key(self, keys: list[str]) -> None:
        names = [_normalise_key(k, _PYAUTO_KEYS) for k in keys if k.strip()]
        if not names:
            return
        if len(names) == 1:
            self._gui.press(names[0])
        else:
            self._gui.hotkey(*names)

    def open_app(self, name: str) -> str:
        return open_application(name)


class XdotoolHands:
    name = "xdotool"

    def __init__(self) -> None:
        if not shutil.which("xdotool"):
            raise HandsUnavailable("xdotool is not installed")
        if not os.environ.get("DISPLAY"):
            raise HandsUnavailable("no X11 display (DISPLAY is not set)")

    @staticmethod
    def _run(*args: str) -> None:
        subprocess.run(["xdotool", *args], check=True, timeout=15, capture_output=True)

    def size(self) -> tuple[int, int]:
        """The X root window: the pixels ``mousemove`` takes, and what ``mss`` grabs."""
        out = subprocess.run(
            ["xdotool", "getdisplaygeometry"], capture_output=True, text=True, timeout=5
        ).stdout.split()
        if len(out) != 2:
            raise RuntimeError("xdotool getdisplaygeometry said nothing")
        return int(out[0]), int(out[1])

    def click(self, x: float, y: float, button: str = "left", clicks: int = 1) -> None:
        code = {"left": "1", "middle": "2", "right": "3"}.get(button, "1")
        self._run("mousemove", str(int(x)), str(int(y)))
        time.sleep(0.05)
        args = ["click"]
        if clicks > 1:
            args += ["--repeat", str(clicks), "--delay", "80"]
        self._run(*args, code)

    def move(self, x: float, y: float) -> None:
        self._run("mousemove", str(int(x)), str(int(y)))

    def drag(self, x: float, y: float, x2: float, y2: float) -> None:
        self._run("mousemove", str(int(x)), str(int(y)))
        self._run("mousedown", "1")
        time.sleep(0.1)
        self._run("mousemove", str(int(x2)), str(int(y2)))
        time.sleep(0.1)
        self._run("mouseup", "1")

    def scroll(self, x: float, y: float, dy: float) -> None:
        self._run("mousemove", str(int(x)), str(int(y)))
        clicks = max(1, min(30, int(abs(dy) / 40) or 1))
        self._run("click", "--repeat", str(clicks), "--delay", "30", "5" if dy > 0 else "4")

    def type(self, text: str) -> None:
        self._run("type", "--delay", "15", "--", text)

    def key(self, keys: list[str]) -> None:
        names = [_normalise_key(k, _XDO_KEYS) for k in keys if k.strip()]
        if names:
            self._run("key", "--clearmodifiers", "+".join(names))

    def open_app(self, name: str) -> str:
        return open_application(name)


def _platform() -> str:
    # a function, so mypy checks every branch below on every platform
    return sys.platform


# names that are commands to the machine, not applications a person opens: `open_app` is
# for a window on the screen, and these would stop the session or worse
NOT_APPLICATIONS = frozenset(
    {
        "shutdown",
        "poweroff",
        "reboot",
        "halt",
        "init",
        "telinit",
        "systemctl",
        "launchctl",
        "logout",
        "pkill",
        "killall",
        "kill",
        "taskkill",
        "rm",
        "del",
        "format",
        "diskutil",
        "mkfs",
        "dd",
        "sh",
        "bash",
        "zsh",
        "fish",
        "cmd",
        "powershell",
        "pwsh",
        "sudo",
        "su",
        "doas",
        "runas",
    }
)


def looks_like_an_application(name: str) -> bool:
    """A bare name a person would call an application: no path, no shell characters,
    not one of the commands in :data:`NOT_APPLICATIONS`."""
    bare = name.strip()
    if not bare or len(bare) > 80:
        return False
    if any(ch in bare for ch in "/\\;&|<>`$\"'\n\r\t") or bare.startswith("-"):
        return False
    return bare.lower().removesuffix(".exe") not in NOT_APPLICATIONS


def open_application(name: str) -> str:
    """Start an application by the name a person uses for it. Returns what was started."""
    name = name.strip()
    if not name:
        raise ValueError("an application name is required")
    if not looks_like_an_application(name):
        raise ValueError(f"{name!r} is not an application to open")
    platform = _platform()
    if platform == "darwin":
        subprocess.run(["open", "-a", name], check=True, timeout=20, capture_output=True)
        return name
    if platform == "win32":
        subprocess.Popen(["cmd", "/c", "start", "", name], shell=False)  # noqa: S603
        return name
    # Linux: a command on PATH, else a .desktop entry whose Name matches
    exe = shutil.which(name) or shutil.which(name.lower())
    if exe:
        subprocess.Popen(
            [exe], start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
        )
        return exe
    desktop = _desktop_entry(name)
    if desktop is None:
        raise ValueError(f"no application called {name!r} was found")
    if shutil.which("gtk-launch"):
        subprocess.Popen(
            ["gtk-launch", desktop.stem],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return desktop.stem
    if shutil.which("gio"):
        subprocess.Popen(
            ["gio", "launch", str(desktop)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return desktop.stem
    raise ValueError(f"found {desktop.name} but no launcher (gtk-launch or gio) to start it")


def _desktop_entry(name: str) -> Path | None:
    low = name.lower()
    roots = [
        Path.home() / ".local/share/applications",
        Path("/usr/local/share/applications"),
        Path("/usr/share/applications"),
        Path("/var/lib/flatpak/exports/share/applications"),
        Path("/var/lib/snapd/desktop/applications"),
    ]
    best: tuple[int, Path] | None = None
    for root in roots:
        if not root.is_dir():
            continue
        for entry in root.glob("*.desktop"):
            try:
                text = entry.read_text("utf-8", errors="ignore")
            except OSError:
                continue
            if "NoDisplay=true" in text:
                continue
            names = [
                line.split("=", 1)[1].strip().lower()
                for line in text.splitlines()
                if line.startswith(("Name=", "Name[", "GenericName="))
            ]
            score = 0
            if low in names or entry.stem.lower() == low:
                score = 3
            elif any(n.startswith(low) for n in names):
                score = 2
            elif any(low in n for n in names):
                score = 1
            if score and (best is None or score > best[0]):
                best = (score, entry)
    return best[1] if best else None


BACKENDS = ("auto", "desktop", "pyautogui", "xdotool")

WAYLAND_TEXT = (
    "Wayland session: no global screen or cursor for a program to drive. Log in with Xorg "
    "(the session chooser on the login screen), or run the hands on another computer of "
    "the account."
)


def wayland_session(env: dict[str, str] | None = None) -> bool:
    """Linux: whether this is a Wayland session — the one case the hands cannot work in at
    all (no global pointer to move, a root grab that shows none of the native windows).
    ``XDG_SESSION_TYPE`` is what the login manager sets; a ``WAYLAND_DISPLAY`` without a
    ``DISPLAY`` is the same thing from a compositor started by hand. XWayland (both set,
    session type wayland) counts: the X11 tools would reach X windows only. The desktop
    app's operator (``harness/desktop/src/operator.ts``) draws the same line."""
    if not _platform().startswith("linux"):
        return False
    e = os.environ if env is None else env
    if (e.get("XDG_SESSION_TYPE") or "").strip().lower() == "wayland":
        return True
    return bool(e.get("WAYLAND_DISPLAY")) and not e.get("DISPLAY")


def _make_backend(name: str) -> HandsBackend:
    if name == "desktop":
        from nanomuse.computer.operator import OperatorHands

        return OperatorHands.from_env()
    if name == "pyautogui":
        return PyAutoGUIHands()
    return XdotoolHands()


def pick_backend(preference: str = "auto") -> HandsBackend:
    """The hands to use, or :class:`HandsUnavailable` saying what is missing."""
    preference = (preference or "auto").lower()
    from nanomuse.computer.operator import operator_env, operator_owns_the_screen

    # The desktop app's operator goes first — and when it is there but says no (a Wayland
    # session, a macOS permission missing), its reason is the one worth reading; the
    # Python backends would fail for the same cause with vaguer words.
    operator_reason = ""
    if preference in ("auto", "desktop") and operator_env() is not None:
        try:
            backend = _make_backend("desktop")
        except HandsUnavailable as exc:
            operator_reason = str(exc)
        else:
            logger.info("computer hands: {}", backend.name)
            return backend
    if preference == "desktop":
        raise HandsUnavailable(
            operator_reason or "the desktop operator is not running (no NANOMUSE_OPERATOR_URL)"
        )
    if operator_reason and operator_owns_the_screen():
        # macOS under the desktop app: the operator or nothing. pyautogui here would ask
        # TCC a second time, for the runtime, and the fix is the one the operator named.
        raise HandsUnavailable(operator_reason)
    if wayland_session():
        # Linux under Wayland: xdotool and pyautogui would start (XWayland is an X server)
        # and then move nothing the person can see — said plainly instead, once.
        raise HandsUnavailable(operator_reason or WAYLAND_TEXT)
    errors: list[str] = [operator_reason] if operator_reason else []
    order = {"pyautogui": ["pyautogui"], "xdotool": ["xdotool"]}.get(
        preference, ["pyautogui", "xdotool"]
    )
    for name in order:
        try:
            backend = _make_backend(name)
        except HandsUnavailable as exc:
            errors.append(str(exc))
            continue
        except Exception as exc:  # noqa: BLE001 — a backend that cannot set up is one that is not there
            errors.append(f"{name}: {exc}")
            continue
        logger.info("computer hands: {}", backend.name)
        return backend
    raise HandsUnavailable("; ".join(errors) or "no hands backend")


def describe_availability(preference: str = "auto") -> dict[str, Any]:
    """For the settings screen: which backend would be used, or why none."""
    try:
        backend = pick_backend(preference)
    except HandsUnavailable as exc:
        return {"available": False, "backend": None, "reason": str(exc)}
    return {"available": True, "backend": backend.name, "reason": ""}


__all__ = [
    "BACKENDS",
    "WAYLAND_TEXT",
    "HandsBackend",
    "HandsUnavailable",
    "PyAutoGUIHands",
    "XdotoolHands",
    "describe_availability",
    "hands_space",
    "open_application",
    "pick_backend",
    "wayland_session",
]
