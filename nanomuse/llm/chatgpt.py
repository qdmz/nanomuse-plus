"""The ChatGPT sign-in: the Codex OAuth flow (PKCE), the token store and refresh.

A person with a ChatGPT plan can let nanoMuse use it for chat and for the hands, the way
OpenAI's own Codex CLI does: the same client id, authorize and token URLs, scopes and
redirect; the tokens live in ``<data_dir>/chatgpt.json`` (mode 0600) and are refreshed
before they expire. The Codex backend has no image or video endpoints, so this covers chat
and vision only (contract C11). An honest word belongs next to it, printed on every login:

    OpenAI's terms cover using a ChatGPT plan inside OpenAI's own Codex; other apps have had
    this access cut off before (OpenCode, January 2026). If it stops working, an API key does.

The wire contract (CLI events, routes, the store's shape) is in the runtime team's
``CONTRACT-chatgpt.md``; the user-facing page is ``docs/configuration.md``.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import IO, Any
from urllib.parse import parse_qs, urlencode, urlparse

import httpx
from loguru import logger

# --------------------------------------------------------------------------- constants
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"
AUTHORIZE_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
REDIRECT_PORT = 1455
REDIRECT_PATH = "/auth/callback"
REDIRECT_URI = f"http://localhost:{REDIRECT_PORT}{REDIRECT_PATH}"
SCOPE = "openid profile email offline_access"
ORIGINATOR = "nanomuse"
CLAIM_PATH = "https://api.openai.com/auth"
ACCOUNT_CLAIM = "chatgpt_account_id"
PLAN_CLAIM = "chatgpt_plan_type"

RESPONSES_URL = "https://chatgpt.com/backend-api/codex/responses"
MODELS_URL = "https://chatgpt.com/backend-api/codex/models"
#: the plan's usage windows (what Codex's `/status` shows), when OpenAI answers it
USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
MODELS_CLIENT_VERSION = "99.99.99"
#: the host the plan lives on — what a "cannot be reached" card names
HOST = "chatgpt.com"

DEFAULT_MODEL = "gpt-5.6-sol"
BUILTIN_MODELS: tuple[str, ...] = (
    "gpt-6.1-sol",
    "gpt-6-sol",
    "gpt-6-luna",
    "gpt-6-astra",
    "gpt-5.6-sol",
    "gpt-5.4",
    "gpt-5.4-mini",
)
#: what the sign-in covers (the Codex backend has no image or video endpoints)
CAPABILITIES: tuple[str, ...] = ("chat", "vision")

STORE_FILE = "chatgpt.json"
#: a token this close to expiry is refreshed before it is used
REFRESH_MARGIN_S = 60
LOGIN_TIMEOUT_S = 600

HONESTY_LINE = (
    "OpenAI's terms cover using a ChatGPT plan inside OpenAI's own Codex; other apps have had "
    "this access cut off before (OpenCode, January 2026). If it stops working, an API key does."
)
HONESTY_LINE_ZH = (
    "OpenAI 的条款只允许在它自己的 Codex 里使用 ChatGPT 订阅；其他应用的这条路曾被切断过"
    "（OpenCode，2026 年 1 月）。如果哪天不能用了，API key 还能用。"
)

_PLAN_LABELS = {"plus": "ChatGPT Plus", "pro": "ChatGPT Pro", "team": "ChatGPT Team"}


def _platform() -> str:
    return sys.platform


def plan_label(plan: str) -> str:
    """``plus`` → "ChatGPT Plus"; an unknown or missing plan is just "ChatGPT"."""
    p = (plan or "").strip().lower()
    if p in _PLAN_LABELS:
        return _PLAN_LABELS[p]
    if p in ("", "free", "unknown"):
        return "ChatGPT"
    return f"ChatGPT {p.capitalize()}"


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def claims(access: str) -> dict[str, Any]:
    """The JWT's payload, decoded without verification (we only read our own claims)."""
    parts = access.split(".")
    if len(parts) < 2 or not parts[1]:
        return {}
    try:
        raw = base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4))
        data = json.loads(raw)
    except (ValueError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def account_from(access: str) -> tuple[str, str]:
    """``(account_id, plan)`` from the access token's auth claim; empty when absent."""
    auth = claims(access).get(CLAIM_PATH)
    if not isinstance(auth, dict):
        return "", ""
    return str(auth.get(ACCOUNT_CLAIM) or ""), str(auth.get(PLAN_CLAIM) or "").lower()


class ChatGPTError(Exception):
    """A failure with a stable ``code`` (the CLI's ``error`` event carries it).

    The codes a client may want to draw differently: ``not_signed_in`` (sign in again),
    ``unreachable`` (chatgpt.com does not answer from this network — DNS, connect, TLS),
    ``region_blocked`` (OpenAI refuses the region: 403 ``unsupported_country_region_territory``
    or an HTML page where JSON was due), ``quota`` (the plan has nothing left for now;
    ``retry_after`` when OpenAI said how long), ``rate_limited`` (too many requests at once),
    ``upstream`` (any other HTTP failure, ``status`` set)."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 0,
        retry_after: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.retry_after = retry_after

    def public(self) -> dict[str, Any]:
        """The error as an API body: the code, the sentence, the status, the wait."""
        out: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.status:
            out["status"] = self.status
        if self.retry_after is not None:
            out["retry_after"] = self.retry_after
        return out


# --------------------------------------------------------------------------- failures
REGION_CODE = "unsupported_country_region_territory"
#: the codes and words OpenAI uses for a plan with nothing left (not a burst limit)
_QUOTA_MARKERS = (
    "usage_limit_reached",
    "usage_not_included",
    "insufficient_quota",
    "quota_exceeded",
    "plan_limit",
    "exceeded your current quota",
    "reached your usage limit",
    "usage limit",
)

#: what the person reads for each code, in English and in Chinese; the phones have their own
FAILURE_LINES: dict[str, tuple[str, str]] = {
    "unreachable": (
        f"{HOST} cannot be reached from this network. A VPN on this device, a proxy in the "
        "app's Network setting, or another network helps.",
        f"这个网络连不上 {HOST}。在这台设备上开 VPN、在应用的「网络」里填代理，或者换个网络。",
    ),
    "region_blocked": (
        "OpenAI does not serve the ChatGPT plan in this region. Connect from a region it "
        "serves (a VPN or a proxy), or use an API key instead.",
        "OpenAI 不在这个地区提供 ChatGPT 套餐。从它服务的地区连接（VPN 或代理），或者改用 API key。",
    ),
    "not_signed_in": (
        "The ChatGPT sign-in is no longer valid. Sign in again.",
        "ChatGPT 的登录已失效，请重新登录。",
    ),
    "quota": (
        "The ChatGPT plan has nothing left for now. Wait for the window to reset, or use "
        "another provider meanwhile.",
        "ChatGPT 套餐这段时间的额度已用完。等窗口重置，或者先用别的服务商。",
    ),
    "rate_limited": (
        "ChatGPT is taking too many requests at once. Try again in a moment.",
        "ChatGPT 同时收到的请求太多了，稍后再试。",
    ),
}


def failure_line(code: str, chinese: bool = False) -> str:
    """The sentence for a classified failure, or empty for a code that has none."""
    pair = FAILURE_LINES.get(code)
    if pair is None:
        return ""
    return pair[1] if chinese else pair[0]


def _error_fields(text: str) -> tuple[str, str, str]:
    """``(code, type, message)`` from an OpenAI-shaped error body; empty when it is not one."""
    try:
        data = json.loads(text)
    except (ValueError, TypeError):
        return "", "", ""
    if not isinstance(data, dict):
        return "", "", ""
    err = data.get("error")
    err = err if isinstance(err, dict) else data
    detail = data.get("detail")
    message = err.get("message") or (detail if isinstance(detail, str) else "") or ""
    return str(err.get("code") or ""), str(err.get("type") or ""), str(message)


def looks_like_html(text: str) -> bool:
    head = text.lstrip()[:64].lower()
    return head.startswith("<!doctype html") or head.startswith("<html")


def _retry_after(headers: Mapping[str, str] | None) -> int | None:
    if not headers:
        return None
    raw = headers.get("retry-after") or headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0, int(float(raw)))
    except ValueError:
        return None


def classify_http(status: int, body: str, headers: Mapping[str, str] | None = None) -> ChatGPTError:
    """The error for a non-200 answer of the Codex backend: a stable code, a plain sentence.

    OpenAI's own message is kept where it says more than the code (a quota message names the
    window); the socket-level texts are never shown."""
    code, kind, message = _error_fields(body)
    low = f"{code} {kind} {message} {body[:400]}".lower()
    retry_after = _retry_after(headers)
    if status == 401:
        return ChatGPTError("not_signed_in", failure_line("not_signed_in"), status=status)
    if status == 403 and (REGION_CODE in low or looks_like_html(body)):
        return ChatGPTError("region_blocked", failure_line("region_blocked"), status=status)
    if status in (403, 404, 503) and looks_like_html(body):
        # an interception page (a captive portal, a firewall) where JSON was due
        return ChatGPTError("unreachable", failure_line("unreachable"), status=status)
    if status == 429:
        if any(marker in low for marker in _QUOTA_MARKERS):
            line = failure_line("quota")
            if message and len(message) <= 300:
                line = f"{line} OpenAI says: {message}"
            return ChatGPTError("quota", line, status=status, retry_after=retry_after)
        return ChatGPTError(
            "rate_limited", failure_line("rate_limited"), status=status, retry_after=retry_after
        )
    if status == 403:
        text = (
            message[:300] if message else f"the Codex endpoint refused the request (HTTP {status})"
        )
        return ChatGPTError("upstream", text, status=status)
    if message:
        return ChatGPTError("upstream", message[:500], status=status, retry_after=retry_after)
    return ChatGPTError(
        "upstream",
        f"the Codex endpoint answered HTTP {status}",
        status=status,
        retry_after=retry_after,
    )


def classify_transport(exc: Exception) -> ChatGPTError:
    """The error for a request that never got an answer: DNS, connect, TLS, a timeout."""
    if isinstance(exc, httpx.TimeoutException):
        return ChatGPTError("unreachable", failure_line("unreachable") + " (timed out)")
    if isinstance(exc, httpx.TransportError):
        return ChatGPTError("unreachable", failure_line("unreachable"))
    return ChatGPTError("network", f"could not reach {HOST}: {exc}")


# --------------------------------------------------------------------------- usage
@dataclass
class Window:
    """One of the plan's two usage windows (5 hours and a week, on OpenAI's side)."""

    used_percent: int
    window_minutes: int
    resets_in_s: int

    def public(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Limits:
    """What OpenAI reports about the plan's use: the response headers carry it after every
    request (``x-codex-primary-used-percent``, …), ``GET …/wham/usage`` on demand."""

    primary: Window | None = None
    secondary: Window | None = None
    plan: str = ""
    limit_reached: bool = False
    at: int = field(default_factory=lambda: int(time.time()))

    @property
    def empty(self) -> bool:
        return self.primary is None and self.secondary is None

    @property
    def left_percent(self) -> int | None:
        """What is left of the tighter window, 0–100, or None when nothing was reported."""
        used = [w.used_percent for w in (self.primary, self.secondary) if w is not None]
        return max(0, 100 - max(used)) if used else None

    def public(self) -> dict[str, Any]:
        return {
            "primary": self.primary.public() if self.primary else None,
            "secondary": self.secondary.public() if self.secondary else None,
            "plan": self.plan,
            "limit_reached": self.limit_reached,
            "left_percent": self.left_percent,
            "at": self.at,
        }

    @classmethod
    def from_headers(cls, headers: Mapping[str, str]) -> Limits | None:
        """The ``x-codex-*`` headers of a Codex answer; None when there are none."""

        def window(name: str) -> Window | None:
            used = headers.get(f"x-codex-{name}-used-percent")
            if used is None:
                return None
            return Window(
                used_percent=_int(used),
                window_minutes=_int(headers.get(f"x-codex-{name}-window-minutes")),
                resets_in_s=_int(headers.get(f"x-codex-{name}-reset-after-seconds")),
            )

        primary, secondary = window("primary"), window("secondary")
        if primary is None and secondary is None:
            return None
        return cls(primary=primary, secondary=secondary)

    @classmethod
    def from_usage(cls, data: Any) -> Limits:
        """The body of ``GET …/wham/usage``; a shape we do not know gives empty limits."""
        if not isinstance(data, dict):
            return cls()
        rate = data.get("rate_limit") or data.get("rate_limits") or {}
        rate = rate if isinstance(rate, dict) else {}

        def window(obj: Any) -> Window | None:
            if not isinstance(obj, dict):
                return None
            seconds = obj.get("limit_window_seconds")
            minutes = obj.get("window_minutes")
            return Window(
                used_percent=_int(obj.get("used_percent")),
                window_minutes=_int(minutes) if minutes is not None else _int(seconds) // 60,
                resets_in_s=_int(obj.get("reset_after_seconds") or obj.get("resets_in_seconds")),
            )

        return cls(
            primary=window(rate.get("primary_window") or rate.get("primary")),
            secondary=window(rate.get("secondary_window") or rate.get("secondary")),
            plan=str(data.get("plan_type") or "").lower(),
            limit_reached=bool(rate.get("limit_reached")),
        )


def _int(value: Any) -> int:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def http_client(timeout: httpx.Timeout, proxy: str | None = None) -> httpx.AsyncClient:
    """An HTTP client for the plan's hosts. With ``proxy`` (``http://host:port``,
    ``http://user:pass@host:port`` or ``socks5://…``) every request goes through it and the
    environment's ``HTTPS_PROXY`` is ignored; without, the environment decides as usual."""
    if proxy:
        return httpx.AsyncClient(timeout=timeout, proxy=proxy, trust_env=False)
    return httpx.AsyncClient(timeout=timeout)


def usage_headers(token: Token) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {token.access}",
        "chatgpt-account-id": token.account_id,
        "originator": ORIGINATOR,
        "accept": "application/json",
    }


async def fetch_usage(
    token: Token, http: httpx.AsyncClient, url: str = USAGE_URL, timeout: float = 8.0
) -> Limits:
    """The plan's usage windows from OpenAI, or :class:`ChatGPTError` (classified)."""
    try:
        r = await http.get(url, headers=usage_headers(token), timeout=timeout)
    except httpx.HTTPError as exc:
        raise classify_transport(exc) from exc
    if r.status_code != 200:
        raise classify_http(r.status_code, r.text, r.headers)
    try:
        data = r.json()
    except ValueError:
        return Limits()
    return Limits.from_usage(data)


# --------------------------------------------------------------------------- the store
@dataclass
class Token:
    access: str
    refresh: str
    #: Unix seconds
    expires_at: int
    account_id: str
    plan: str = ""
    label: str = "ChatGPT"
    obtained_at: int = 0

    def expires_within(self, seconds: int) -> bool:
        return self.expires_at - int(time.time()) <= seconds

    @property
    def expires_in(self) -> int:
        return max(0, self.expires_at - int(time.time()))

    def to_dict(self) -> dict[str, Any]:
        return {"version": 1, **asdict(self)}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Token | None:
        try:
            access = str(data["access"])
            refresh = str(data.get("refresh") or "")
            expires_at = int(data.get("expires_at") or 0)
        except (KeyError, TypeError, ValueError):
            return None
        if not access:
            return None
        plan = str(data.get("plan") or "")
        return cls(
            access=access,
            refresh=refresh,
            expires_at=expires_at,
            account_id=str(data.get("account_id") or ""),
            plan=plan,
            label=str(data.get("label") or plan_label(plan)),
            obtained_at=int(data.get("obtained_at") or 0),
        )

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> Token:
        """A token endpoint's answer (exchange or refresh) as a stored token."""
        access = str(payload.get("access_token") or "")
        if not access:
            raise ChatGPTError("exchange_failed", "the token response carries no access token")
        account_id, plan = account_from(access)
        if not account_id:
            raise ChatGPTError(
                "no_account", "the token carries no ChatGPT account; is a plan on this account?"
            )
        try:
            expires_in = int(payload.get("expires_in") or 3600)
        except (TypeError, ValueError):
            expires_in = 3600
        now = int(time.time())
        return cls(
            access=access,
            refresh=str(payload.get("refresh_token") or ""),
            expires_at=now + expires_in,
            account_id=account_id,
            plan=plan,
            label=plan_label(plan),
            obtained_at=now,
        )

    def public(self) -> dict[str, Any]:
        """What may be shown: never the tokens."""
        return {
            "label": self.label,
            "plan": self.plan,
            "account_id": self.account_id,
            "expires_at": self.expires_at,
            "expires_in": self.expires_in,
        }


class TokenStore:
    """``<data_dir>/chatgpt.json``, written 0600 on POSIX; a lock file beside it so the proxy
    and the runtime never refresh at the same time."""

    def __init__(self, path: Path):
        self.path = Path(path)

    @classmethod
    def in_dir(cls, data_dir: Path) -> TokenStore:
        return cls(Path(data_dir) / STORE_FILE)

    def load(self) -> Token | None:
        try:
            data = json.loads(self.path.read_text("utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("could not read {}: {}", self.path, exc)
            return None
        return Token.from_dict(data) if isinstance(data, dict) else None

    def save(self, token: Token) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(token.to_dict(), fh, ensure_ascii=False, indent=1)
        if _platform() != "win32":
            os.chmod(tmp, 0o600)
        os.replace(tmp, self.path)

    def clear(self) -> bool:
        """Remove the store; True when there was one."""
        try:
            self.path.unlink()
        except FileNotFoundError:
            return False
        for extra in (self.path.with_suffix(".tmp"), self.path.with_suffix(".lock")):
            try:
                extra.unlink()
            except OSError:
                pass
        return True

    @contextmanager
    def lock(self) -> Any:
        """An exclusive lock on ``chatgpt.lock`` (``fcntl`` on POSIX; a best effort elsewhere)."""
        fh = self._open_lock()
        try:
            self._flock(fh)
            yield
        finally:
            self._unlock(fh)

    @asynccontextmanager
    async def alock(self) -> AsyncIterator[None]:
        """:meth:`lock` for a coroutine: the wait for another process is spent in a worker
        thread, so the event loop keeps serving while a sibling refreshes the token."""
        fh = self._open_lock()
        try:
            await asyncio.to_thread(self._flock, fh)
            yield
        finally:
            self._unlock(fh)

    def _open_lock(self) -> IO[str]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return open(self.path.with_suffix(".lock"), "a+", encoding="utf-8")

    @staticmethod
    def _flock(fh: IO[str]) -> None:
        # `sys.platform` itself, so mypy on Windows drops the fcntl branch
        if sys.platform != "win32":
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)

    @staticmethod
    def _unlock(fh: IO[str]) -> None:
        try:
            if sys.platform != "win32":
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


# --------------------------------------------------------------------------- refresh
class Auth:
    """The access token for a request: the stored one, refreshed under the lock when it is
    about to expire. A refresh the server refuses (400/401) clears the store."""

    def __init__(
        self,
        store: TokenStore,
        http: httpx.AsyncClient | None = None,
        token_url: str = TOKEN_URL,
        margin_s: int = REFRESH_MARGIN_S,
        proxy: str | None = None,
    ):
        self.store = store
        self._http = http
        self.token_url = token_url
        self.margin_s = margin_s
        self.proxy = proxy or None

    @property
    def http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = http_client(httpx.Timeout(30.0, connect=10.0), self.proxy)
        return self._http

    async def close(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def signed_in(self) -> bool:
        return self.store.load() is not None

    async def token(self, force_refresh: bool = False) -> Token:
        """A usable token, or :class:`ChatGPTError` ``not_signed_in``."""
        token = self.store.load()
        if token is None:
            raise ChatGPTError("not_signed_in", "not signed in; run `nanomuse chatgpt login`")
        if not force_refresh and not token.expires_within(self.margin_s):
            return token
        return await self.refresh(token)

    async def refresh(self, current: Token | None = None) -> Token:
        current = current or self.store.load()
        if current is None:
            raise ChatGPTError("not_signed_in", "not signed in; run `nanomuse chatgpt login`")
        async with self.store.alock():
            # another process may have refreshed while we waited for the lock
            latest = self.store.load()
            if latest is not None and latest.access != current.access:
                if not latest.expires_within(self.margin_s):
                    return latest
                current = latest
            if not current.refresh:
                self.store.clear()
                raise ChatGPTError("not_signed_in", "the sign-in cannot be renewed; sign in again")
            data = {
                "grant_type": "refresh_token",
                "refresh_token": current.refresh,
                "client_id": CLIENT_ID,
            }
            try:
                r = await self.http.post(
                    self.token_url,
                    data=data,
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                )
            except httpx.HTTPError as exc:
                raise classify_transport(exc) from exc
            if r.status_code in (400, 401):
                self.store.clear()
                logger.warning("ChatGPT refresh refused ({}); signed out", r.status_code)
                raise ChatGPTError(
                    "not_signed_in", "ChatGPT sign-in no longer valid; run nanomuse chatgpt login"
                )
            if r.status_code != 200:
                raise ChatGPTError(
                    "refresh_failed", f"token refresh failed with HTTP {r.status_code}"
                )
            try:
                payload = r.json()
            except ValueError:
                raise ChatGPTError(
                    "refresh_failed", "token refresh answered with something that is not JSON"
                ) from None
            token = Token.from_payload(payload if isinstance(payload, dict) else {})
            if not token.refresh:
                token.refresh = current.refresh
            self.store.save(token)
            logger.info("ChatGPT token refreshed ({})", token.label)
            return token


# --------------------------------------------------------------------------- login
_CALLBACK_PAGE = (
    "<!doctype html><meta charset=utf-8><title>nanoMuse</title>"
    "<body style='font-family:system-ui;margin:3em'><h2>{title}</h2><p>{text}</p></body>"
)


@dataclass
class CallbackResult:
    code: str = ""
    state: str = ""
    error: str = ""


class LoginFlow:
    """One PKCE authorization-code sign-in.

    ``url`` is what the person opens; :meth:`wait` listens on the loopback port for the
    callback (and, when a ``paste`` reader is given, reads the pasted callback URL from it,
    whichever comes first); :meth:`finish` exchanges the code and stores the token. The
    URLs and the port are parameters so tests run the whole flow against a local fake."""

    def __init__(
        self,
        store: TokenStore,
        *,
        authorize_url: str = AUTHORIZE_URL,
        token_url: str = TOKEN_URL,
        redirect_uri: str = REDIRECT_URI,
        port: int = REDIRECT_PORT,
        originator: str = ORIGINATOR,
        http: httpx.AsyncClient | None = None,
    ):
        self.store = store
        self.authorize_url = authorize_url
        self.token_url = token_url
        self.redirect_uri = redirect_uri
        self.port = port
        self.originator = originator
        self._http = http
        self.verifier = _b64url(os.urandom(32))
        self.challenge = _b64url(hashlib.sha256(self.verifier.encode("ascii")).digest())
        self.state = _b64url(secrets.token_bytes(16))
        self._server: asyncio.base_events.Server | None = None
        self._result: asyncio.Future[CallbackResult] | None = None

    @property
    def url(self) -> str:
        params = {
            "response_type": "code",
            "client_id": CLIENT_ID,
            "redirect_uri": self.redirect_uri,
            "scope": SCOPE,
            "code_challenge": self.challenge,
            "code_challenge_method": "S256",
            "state": self.state,
            "id_token_add_organizations": "true",
            "codex_cli_simplified_flow": "true",
            "originator": self.originator,
        }
        return f"{self.authorize_url}?{urlencode(params)}"

    @property
    def callback_url(self) -> str:
        return f"http://localhost:{self.port}{REDIRECT_PATH}"

    # -- the loopback listener
    async def listen(self) -> bool:
        """Bind the callback port; False when it is taken (the paste fallback remains)."""
        loop = asyncio.get_running_loop()
        self._result = loop.create_future()
        try:
            self._server = await asyncio.start_server(self._serve, "127.0.0.1", self.port)
        except OSError:
            self._server = None
            return False
        return True

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), 10)
            for _ in range(200):  # a browser's redirect carries a few dozen header lines
                line = await asyncio.wait_for(reader.readline(), 10)
                if line in (b"\r\n", b"\n", b""):
                    break
            parts = request_line.decode("latin-1").split()
            target = parts[1] if len(parts) >= 2 else "/"
            status, body = self._handle(target)
            payload = body.encode("utf-8")
            writer.write(
                f"HTTP/1.1 {status}\r\nContent-Type: text/html; charset=utf-8\r\n"
                f"Content-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode("latin-1")
                + payload
            )
            await writer.drain()
        except (TimeoutError, OSError, UnicodeDecodeError, IndexError):
            pass
        finally:
            writer.close()

    def _handle(self, target: str) -> tuple[str, str]:
        parsed = urlparse(target)
        if parsed.path != REDIRECT_PATH:
            return "404 Not Found", _CALLBACK_PAGE.format(title="Not here", text="")
        result = self._parse_callback(target)
        if self._result is not None and not self._result.done():
            self._result.set_result(result)
        if result.error:
            return "400 Bad Request", _CALLBACK_PAGE.format(
                title="Sign-in did not finish", text=result.error
            )
        if result.state != self.state:
            return "400 Bad Request", _CALLBACK_PAGE.format(
                title="Sign-in did not finish", text="The request did not match; try again."
            )
        return "200 OK", _CALLBACK_PAGE.format(
            title="Signed in", text="You can close this tab and go back to nanoMuse."
        )

    @staticmethod
    def _parse_callback(url: str) -> CallbackResult:
        qs = parse_qs(urlparse(url.strip()).query)
        return CallbackResult(
            code=(qs.get("code") or [""])[0],
            state=(qs.get("state") or [""])[0],
            error=(qs.get("error_description") or qs.get("error") or [""])[0],
        )

    async def wait(
        self,
        timeout: float = LOGIN_TIMEOUT_S,
        paste: Callable[[], Awaitable[str | None]] | None = None,
    ) -> CallbackResult:
        """The callback's code and state, from the port or from ``paste`` (an async reader
        returning one pasted line, or None when it has nothing), whichever comes first."""
        waits: set[asyncio.Future[Any]] = set()
        if self._result is not None and self._server is not None:
            waits.add(asyncio.ensure_future(self._result))
        if paste is not None:
            waits.add(asyncio.ensure_future(paste()))
        if not waits:
            raise ChatGPTError("port_busy", f"port {self.port} is in use and nothing to read from")
        deadline = time.monotonic() + timeout
        try:
            while waits:
                left = deadline - time.monotonic()
                if left <= 0:
                    break
                done, waits = await asyncio.wait(
                    waits, timeout=left, return_when=asyncio.FIRST_COMPLETED
                )
                for task in done:
                    value = task.result()
                    if isinstance(value, CallbackResult):
                        return value
                    if value:  # a pasted callback URL
                        return self._parse_callback(str(value))
                    # the paste reader had nothing (stdin closed): keep waiting on the rest
            raise ChatGPTError("timeout", "no sign-in arrived in time")
        finally:
            for task in waits:
                task.cancel()
            await self.close()

    async def close(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # pragma: no cover
                pass
            self._server = None

    # -- the exchange
    async def finish(self, result: CallbackResult) -> Token:
        """Check the state, exchange the code, store the token."""
        if result.error:
            raise ChatGPTError("cancelled", result.error)
        if not result.code:
            raise ChatGPTError("cancelled", "the callback carried no code")
        if result.state != self.state:
            raise ChatGPTError("state_mismatch", "the callback's state does not match this login")
        data = {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": result.code,
            "code_verifier": self.verifier,
            "redirect_uri": self.redirect_uri,
        }
        http = self._http or httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0))
        try:
            r = await http.post(
                self.token_url,
                data=data,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )
        except httpx.HTTPError as exc:
            raise ChatGPTError(
                "exchange_failed", f"could not reach the token endpoint: {exc}"
            ) from exc
        finally:
            if self._http is None:
                await http.aclose()
        if r.status_code != 200:
            raise ChatGPTError(
                "exchange_failed", f"token exchange failed with HTTP {r.status_code}"
            )
        try:
            payload = r.json()
        except ValueError:
            raise ChatGPTError(
                "exchange_failed", "token exchange answered with something that is not JSON"
            ) from None
        token = Token.from_payload(payload if isinstance(payload, dict) else {})
        self.store.save(token)
        logger.info("ChatGPT sign-in stored ({})", token.label)
        return token


__all__ = [
    "AUTHORIZE_URL",
    "BUILTIN_MODELS",
    "CAPABILITIES",
    "CLIENT_ID",
    "DEFAULT_MODEL",
    "FAILURE_LINES",
    "HONESTY_LINE",
    "HONESTY_LINE_ZH",
    "HOST",
    "MODELS_URL",
    "REDIRECT_PORT",
    "REDIRECT_URI",
    "REGION_CODE",
    "RESPONSES_URL",
    "STORE_FILE",
    "TOKEN_URL",
    "USAGE_URL",
    "Auth",
    "CallbackResult",
    "ChatGPTError",
    "Limits",
    "LoginFlow",
    "Token",
    "TokenStore",
    "Window",
    "account_from",
    "claims",
    "classify_http",
    "classify_transport",
    "failure_line",
    "fetch_usage",
    "http_client",
    "looks_like_html",
    "plan_label",
]
