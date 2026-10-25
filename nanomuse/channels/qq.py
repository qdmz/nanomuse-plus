"""QQ, over the official QQ Bot API (api.bot.qq.com).

Local patch (not upstream): an access token from ``/app/getAppAccessToken``, a
gateway WebSocket (Hello → Identify with the C2C + group intents → heartbeat →
dispatch), and REST sends. Direct messages arrive as ``C2C_MESSAGE_CREATE``,
group @-messages as ``GROUP_AT_MESSAGE_CREATE``; replies carry the triggering
``msg_id`` while QQ's passive-reply window is open and fall back to a plain
send after it. Files go through the ``/v2/.../files`` upload (base64) and are
delivered as rich-media messages (``msg_type`` 7).

Setup: either use the scan login (Settings → Chat apps → QQ, or the
``/api/channels/qq/login`` flow) — the platform's lite bind flow creates or
binds a bot when the owner scans the code with mobile QQ and hands back the
AppID and an encrypted AppSecret — or create a bot at <https://q.qq.com> by
hand and paste its AppID and AppSecret here. The same bot app can hold only
one gateway session — do not run another client for it at the same time.
"""

from __future__ import annotations

import asyncio
import base64
import itertools
import json
import mimetypes
import secrets
import time
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from nanomuse.channels.base import (
    Channel,
    ChannelBus,
    Field,
    InboundFile,
    InboundMessage,
    json_object,
)
from nanomuse.logger import logger

API = "https://api.bot.qq.com"
WS_URL = "wss://api.bot.qq.com/websocket/"
INTENTS = (1 << 25) | (1 << 30)  # C2C + group @ events, as the platform grants them
TEXT_LIMIT = 3000

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm"}
_AUDIO_SUFFIXES = {".mp3", ".wav", ".ogg", ".silk", ".flac", ".m4a"}


class QQError(RuntimeError):
    pass


# ---------------------------------------------------------------------------
# QR scan-to-create login (the platform's lite bind flow on q.qq.com)
# ---------------------------------------------------------------------------
BIND_HOST = "q.qq.com"


def _decrypt_bind_secret(encrypted_b64: str, bind_key_b64: str) -> str:
    """The bind result's AppSecret: AES-256-GCM under the key we sent, as
    base64(nonce 12 ‖ ciphertext ‖ tag 16)."""
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM

    key = base64.b64decode(bind_key_b64)
    raw = base64.b64decode(encrypted_b64)
    if len(key) != 32 or len(raw) <= 28:
        raise QQError("bind result credential is malformed")
    return AESGCM(key).decrypt(raw[:12], raw[12:], None).decode("utf-8")


def login_begin(muse_name: str = "") -> dict[str, Any]:
    """Create a bind task; the session is polled with :func:`login_poll`."""
    bind_key = base64.b64encode(secrets.token_bytes(32)).decode()

    async def _go() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=10.0)) as http:
            r = await http.post(
                f"https://{BIND_HOST}/lite/create_bind_task",
                json={"key": bind_key},
                headers={"Accept": "application/json"},
            )
        data = json_object(r)
        if r.status_code >= 400 or data.get("retcode") not in (0, None):
            raise QQError(f"create_bind_task failed: HTTP {r.status_code} {str(data)[:200]}")
        task_id = str((data.get("data") or {}).get("task_id") or "")
        if not task_id:
            raise QQError(f"create_bind_task returned no task_id: {str(data)[:200]}")
        url = f"https://{BIND_HOST}/qqbot/openclaw/connect.html?task_id={quote(task_id, safe='')}&_wv=2"
        return {
            "device_code": task_id,
            "task_id": task_id,
            "bind_key": bind_key,
            "url": url,
            "qrcode": url,
            "qrcode_img_content": url,
            "expires_in": 600,
        }

    return asyncio.run(_go())


def login_poll(session: dict[str, Any]) -> dict[str, Any]:
    """One poll of the bind task: pending / succeeded (with settings values) / failed."""

    async def _go() -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=10.0)) as http:
            r = await http.post(
                f"https://{BIND_HOST}/lite/poll_bind_result",
                json={"task_id": str(session.get("task_id") or "")},
                headers={"Accept": "application/json"},
            )
        data = json_object(r)
        if r.status_code >= 400 or data.get("retcode") not in (0, None):
            raise QQError(f"poll_bind_result failed: HTTP {r.status_code} {str(data)[:200]}")
        payload = data.get("data") or {}
        try:
            status = int(payload.get("status") or 0)
        except (TypeError, ValueError):
            status = 0
        if status == 2:  # completed
            appid = str(payload.get("bot_appid") or "")
            encrypted = str(payload.get("bot_encrypt_secret") or "")
            if not appid or not encrypted:
                return {"status": "failed", "detail": "scan succeeded but no credentials came back"}
            try:
                secret = _decrypt_bind_secret(encrypted, str(session.get("bind_key") or ""))
            except Exception as exc:  # noqa: BLE001
                return {"status": "failed", "detail": f"credential decrypt failed: {exc}"}
            return {"status": "succeeded", "values": {"app_id": appid, "app_secret": secret}}
        if status == 3:  # expired
            return {"status": "failed", "detail": "QR code expired"}
        return {"status": "pending"}

    return asyncio.run(_go())


def _file_type(path: Path) -> int:
    suffix = path.suffix.lower()
    if suffix in _IMAGE_SUFFIXES:
        return 1
    if suffix in _VIDEO_SUFFIXES:
        return 2
    if suffix in _AUDIO_SUFFIXES:
        return 3
    return 4


def _chunks(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    return [text[i : i + limit] for i in range(0, len(text), limit)] or [""]


class QQChannel(Channel):
    name = "qq"
    label = "QQ 机器人 (QQ Bot)"
    sdk_module = "websockets"
    extra = "channels"
    console_url = "https://q.qq.com"
    fields = (
        Field("app_id", "AppID", required=True, help="The bot's AppID from q.qq.com"),
        Field("app_secret", "AppSecret", "secret", required=True, help="The bot's AppSecret from q.qq.com"),
    )

    def __init__(self, settings: dict[str, Any]):
        super().__init__(settings)
        self._http: httpx.AsyncClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._ws: Any = None
        self._send_lock = asyncio.Lock()
        self._token = ""
        self._token_expiry = 0.0
        self._session_id = ""
        self._seq: int | None = None
        self._seq_counters: dict[str, Any] = {}
        self._last_msg: dict[str, tuple[str, float]] = {}  # chat_id -> (msg_id, when)
        self._bot_name = ""

    # ------------------------------------------------------------------ token / REST
    async def _access_token(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=15.0))
        r = await self._http.post(
            f"{API}/app/getAppAccessToken",
            json={
                "appId": str(self.settings.get("app_id") or ""),
                "clientSecret": str(self.settings.get("app_secret") or ""),
            },
        )
        data = json_object(r)
        if r.status_code >= 400 or not data.get("access_token"):
            raise QQError(
                f"getAppAccessToken failed: HTTP {r.status_code} code={data.get('code')} {data.get('message') or ''}"
            )
        self._token = str(data["access_token"])
        self._token_expiry = time.time() + float(data.get("expires_in") or 7200)
        return self._token

    async def _rest(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if self._http is None:
            self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=15.0))
        token = await self._access_token()
        r = await self._http.request(
            method, f"{API}{path}", json=body, headers={"Authorization": f"QQBot {token}"}
        )
        data = json_object(r)
        if r.status_code >= 400:
            raise QQError(f"{path} answered HTTP {r.status_code}: {str(data)[:240]}")
        return data

    async def verify(self) -> str:
        me = await self._rest("GET", "/users/@me")
        return str(me.get("username") or me.get("id") or "QQ bot")

    # ------------------------------------------------------------------ lifecycle
    async def start(self, bus: ChannelBus) -> None:
        await super().start(bus)
        self._task = asyncio.create_task(self._run_loop(), name="qq-gateway")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def _run_loop(self) -> None:
        delay = 2.0
        while True:
            try:
                await self._connect_once()
                delay = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("qq: gateway failed: {}", exc)
                if self.bus is not None:
                    self.bus.set_status(self.name, "connecting", str(exc)[:200])
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)

    async def _connect_once(self) -> None:
        import websockets

        token = await self._access_token()
        async with websockets.connect(WS_URL, max_size=16 * 1024 * 1024) as ws:
            self._ws = ws
            heartbeat: asyncio.Task[None] | None = None
            try:
                async for raw in ws:
                    payload = json.loads(raw)
                    op = payload.get("op")
                    if payload.get("s"):
                        self._seq = int(payload["s"])
                    if op == 10:  # Hello
                        interval = float((payload.get("d") or {}).get("heartbeat_interval") or 45000) / 1000
                        heartbeat = asyncio.create_task(self._heartbeat_loop(ws, interval))
                        if self._session_id and self._seq is not None:
                            await ws.send(json.dumps({
                                "op": 6,
                                "d": {"token": f"QQBot {token}", "session_id": self._session_id, "seq": self._seq},
                            }))
                        else:
                            await ws.send(json.dumps({
                                "op": 2,
                                "d": {
                                    "token": f"QQBot {token}",
                                    "intents": INTENTS,
                                    "shard": [0, 1],
                                    "properties": {"$os": "linux", "$browser": "nanomuse", "$device": "nanomuse"},
                                },
                            }))
                    elif op == 0:  # Dispatch
                        await self._dispatch(payload.get("t") or "", payload.get("d") or {})
                    elif op == 7:  # Reconnect
                        return
                    elif op == 9:  # Invalid session
                        self._session_id = ""
                        self._seq = None
                        return
            finally:
                if heartbeat is not None:
                    heartbeat.cancel()
                self._ws = None

    async def _heartbeat_loop(self, ws: Any, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            await ws.send(json.dumps({"op": 1, "d": self._seq}))

    # ------------------------------------------------------------------ inbound
    async def _dispatch(self, t: str, d: dict[str, Any]) -> None:
        if t == "READY":
            self._session_id = str((d or {}).get("session_id") or "")
            user = (d or {}).get("user") or {}
            self._bot_name = str(user.get("username") or "")
            if self.bus is not None:
                self.bus.set_status(self.name, "connected", self._bot_name)
            return
        if t == "RESUMED":
            if self.bus is not None:
                self.bus.set_status(self.name, "connected", self._bot_name)
            return
        if self.bus is None:
            return
        if t == "C2C_MESSAGE_CREATE":
            author = d.get("author") or {}
            openid = str(author.get("user_openid") or "")
            if not openid:
                return
            files = await self._attachments(d)
            self._remember(d, openid)
            self.bus.inbound(
                InboundMessage(
                    channel=self.name,
                    chat_id=openid,
                    sender_id=openid,
                    text=str(d.get("content") or "").strip(),
                    files=files,
                    message_id=str(d.get("id") or ""),
                )
            )
        elif t == "GROUP_AT_MESSAGE_CREATE":
            author = d.get("author") or {}
            # group events carry member_openid (and id), not user_openid
            openid = str(author.get("member_openid") or author.get("user_openid") or author.get("id") or "")
            group = str(d.get("group_openid") or "")
            if not group:
                return
            chat_id = f"g:{group}"
            files = await self._attachments(d)
            self._remember(d, chat_id)
            text = str(d.get("content") or "").strip()
            if text.startswith("@"):
                # group messages arrive prefixed with the @-mention of the bot
                parts = text.split(None, 1)
                text = parts[1] if len(parts) > 1 else ""
            self.bus.inbound(
                InboundMessage(
                    channel=self.name,
                    chat_id=chat_id,
                    sender_id=openid,
                    sender_name=str(author.get("username") or ""),
                    text=text,
                    files=files,
                    is_group=True,
                    mentioned=True,
                    message_id=str(d.get("id") or ""),
                )
            )

    def _remember(self, d: dict[str, Any], chat_id: str) -> None:
        if d.get("id"):
            self._last_msg[chat_id] = (str(d["id"]), time.time())

    async def _attachments(self, d: dict[str, Any]) -> list[InboundFile]:
        files: list[InboundFile] = []
        for att in (d.get("attachments") or [])[:5]:
            ctype = str(att.get("content_type") or "")
            # voice arrives as SILK plus a ready-made WAV twin — prefer the WAV
            url = str(att.get("voice_wav_url") or att.get("url") or "")
            if not url:
                continue
            if url.startswith("//"):
                url = "https:" + url
            try:
                if self._http is None:
                    self._http = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=15.0))
                r = await self._http.get(url)
                if r.status_code >= 400 or len(r.content) > 20 * 1024 * 1024:
                    continue
                name = str(att.get("filename") or "") or f"attachment{mimetypes.guess_extension(ctype) or ''}"
                kind = "image" if ctype.startswith("image/") else "audio" if ctype.startswith("voice") else "file"
                files.append(InboundFile(name=name, data=r.content, kind=kind))
            except Exception as exc:  # noqa: BLE001
                logger.warning("qq: attachment download failed: {}", exc)
        return files

    @staticmethod
    def _passive_window(chat_id: str) -> float:
        # the platform's passive-reply windows: 60 minutes for C2C, 5 for groups
        return 3300.0 if not chat_id.startswith("g:") else 280.0

    # ------------------------------------------------------------------ outbound
    def _next_seq(self, chat_id: str) -> int:
        counter = self._seq_counters.get(chat_id)
        if counter is None:
            counter = self._seq_counters[chat_id] = itertools.count(1)
        return next(counter)

    async def send(self, chat_id: str, text: str, *, files: list[str] | None = None) -> str:
        is_group = chat_id.startswith("g:")
        target = chat_id[2:] if is_group else chat_id
        base = f"/v2/groups/{target}/messages" if is_group else f"/v2/users/{target}/messages"
        window = self._passive_window(chat_id)
        paths = [Path(p) for p in files or [] if Path(p).is_file()]
        # a media message can carry the text as its content — keep them together
        # when the whole reply fits, like the other channels deliver it
        caption = text if (text and paths and len(text) <= TEXT_LIMIT) else ""
        last_id = ""
        async with self._send_lock:
            if text and not caption:
                for chunk in _chunks(text):
                    body: dict[str, Any] = {"msg_type": 0, "content": chunk, "msg_seq": self._next_seq(chat_id)}
                    msg_id, when = self._last_msg.get(chat_id, ("", 0.0))
                    if msg_id and time.time() - when < window:
                        body["msg_id"] = msg_id
                    try:
                        sent = await self._rest("POST", base, body)
                    except QQError:
                        if "msg_id" not in body:
                            raise
                        body.pop("msg_id")  # passive window closed; try a plain send
                        sent = await self._rest("POST", base, body)
                    last_id = str(sent.get("id") or last_id)
            for i, path in enumerate(paths):
                upload = await self._rest(
                    "POST",
                    base.replace("/messages", "/files"),
                    {
                        "file_type": _file_type(path),
                        "file_data": base64.b64encode(path.read_bytes()).decode(),
                        "srv_send_msg": False,
                    },
                )
                file_info = upload.get("file_info")
                if not file_info:
                    raise QQError(f"file upload returned no file_info for {path.name}")
                body = {
                    "msg_type": 7,
                    "media": {"file_info": file_info},
                    "msg_seq": self._next_seq(chat_id),
                }
                if i == 0 and caption:
                    body["content"] = caption
                msg_id, when = self._last_msg.get(chat_id, ("", 0.0))
                if msg_id and time.time() - when < window:
                    body["msg_id"] = msg_id
                try:
                    sent = await self._rest("POST", base, body)
                except QQError:
                    if "msg_id" not in body:
                        raise
                    body.pop("msg_id")
                    sent = await self._rest("POST", base, body)
                last_id = str(sent.get("id") or last_id)
        return last_id


__all__ = ["QQChannel", "login_begin", "login_poll"]
