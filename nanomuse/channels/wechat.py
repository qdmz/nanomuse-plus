"""WeChat （微信）, over the official ClawBot iLink protocol (pure HTTP/JSON).

Local patch (not upstream): the bot is a ClawBot linked to a WeChat account.
``POST /ilink/bot/getupdates`` long-polls (the server holds the request ~35s);
each message carries a ``context_token`` that replies should echo in
``POST /ilink/bot/sendmessage``. Login is a QR scan: ``login_begin`` /
``login_poll`` below back the app's scan-login flow (the manager routes them
for any channel that defines them); the token they yield can equally be
pasted into the settings by hand. Media works the way the official client
does it: inbound pictures are downloaded from the WeChat CDN and AES-128-ECB
decrypted; outbound pictures, videos and files are AES-128-ECB encrypted,
uploaded to the CDN (``/ilink/bot/getuploadurl`` for a slot, then a POST of
the ciphertext), and sent as image / video / file items — so a picture the
Muse makes lands in the chat itself. Outbound text flattens markdown, the
protocol's shape for bot replies.

Setup: choose the scan login in the app (Settings → Chat apps → WeChat) and
scan the code with the WeChat account that should host the bot.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import random
import re
import secrets
import time
import uuid
from contextlib import suppress
from pathlib import Path
from typing import Any

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

DEFAULT_BASE = "https://ilinkai.weixin.qq.com"
CDN_BASE = "https://novac2c.cdn.weixin.qq.com/c2c"
CHANNEL_VERSION = "1.0.2"
TEXT_LIMIT = 4000


class WeChatError(RuntimeError):
    pass


def _client_version() -> int:
    a, b, c = (int(x) for x in CHANNEL_VERSION.split("."))
    return (a << 16) | (b << 8) | c


def _headers(token: str = "") -> dict[str, str]:
    h = {
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": base64.b64encode(str(random.getrandbits(32)).encode()).decode(),
        "iLink-App-Id": "bot",
        "iLink-App-ClientVersion": str(_client_version()),
    }
    if token:
        h["Authorization"] = f"Bearer {token}"
    return h


async def _fetch(
    base: str,
    path: str,
    *,
    token: str = "",
    method: str = "GET",
    body: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(timeout, connect=15.0)) as http:
        r = await http.request(method, f"{base}{path}", json=body, headers=_headers(token))
    data = json_object(r)
    if r.status_code >= 400:
        raise WeChatError(f"{path} answered HTTP {r.status_code}: {str(data)[:200]}")
    return data


# ---------------------------------------------------------------------------
# QR login (the manager's scan-login flow calls these for non-Feishu channels)
# ---------------------------------------------------------------------------
def login_begin(muse_name: str = "") -> dict[str, Any]:
    """Start a scan login; the session is polled with :func:`login_poll`."""
    import asyncio

    async def _go() -> dict[str, Any]:
        data = await _fetch(DEFAULT_BASE, "/ilink/bot/get_bot_qrcode?bot_type=3", timeout=15.0)
        qrcode = str(data.get("qrcode") or "")
        if not qrcode:
            raise WeChatError(f"no qrcode in response: {str(data)[:200]}")
        img = str(data.get("qrcode_img_content") or "")
        return {
            "device_code": qrcode,
            "qrcode": qrcode,
            "qrcode_img_content": img,
            "url": img or qrcode,
            "base_url": DEFAULT_BASE,
        }

    return asyncio.run(_go())


def login_poll(session: dict[str, Any]) -> dict[str, Any]:
    """One poll of the scan state: pending / succeeded (with settings values) / failed."""
    import asyncio

    async def _go() -> dict[str, Any]:
        qrcode = str(session.get("device_code") or "")
        base = str(session.get("base_url") or DEFAULT_BASE)
        data = await _fetch(
            base,
            f"/ilink/bot/get_qrcode_status?qrcode={qrcode}",
            timeout=40.0,
        )
        token = str(data.get("bot_token") or "")
        if token:
            return {
                "status": "succeeded",
                "values": {
                    "bot_token": token,
                    "base_url": str(data.get("baseurl") or base),
                },
            }
        status = str(data.get("status") or "")
        if status in ("expired", "failed", "cancel"):
            return {"status": "failed", "detail": f"scan login {status}"}
        return {"status": "pending"}

    return asyncio.run(_go())


# ---------------------------------------------------------------------------
# text + media helpers
# ---------------------------------------------------------------------------
def _md_to_plain(s: str) -> str:
    s = re.sub(r"```[\s\S]*?```", lambda m: re.sub(r"```\w*\n?", "", m.group(0)).strip(), s)
    s = re.sub(r"`([^`]+)`", r"\1", s)
    s = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", s)
    s = re.sub(r"^#{1,6}\s+", "", s, flags=re.M)
    s = re.sub(r"\*\*([^*]+)\*\*", r"\1", s)
    s = re.sub(r"__([^_]+)__", r"\1", s)
    s = re.sub(r"^\s*[-*]\s+", "• ", s, flags=re.M)
    return s.strip()


def _decrypt_image(raw: bytes, aes_key_b64: str) -> bytes:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    decoded = base64.b64decode(aes_key_b64)
    if len(decoded) == 32 and re.fullmatch(rb"[0-9a-fA-F]{32}", decoded):
        key = bytes.fromhex(decoded.decode())
    else:
        key = decoded
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    plain = decryptor.update(raw) + decryptor.finalize()
    if plain and plain[-1] <= 16:  # PKCS#7
        plain = plain[: -plain[-1]]
    return plain


def _encrypt_media(raw: bytes, key: bytes) -> bytes:
    from cryptography.hazmat.primitives import padding
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    padder = padding.PKCS7(128).padder()
    padded = padder.update(raw) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _padded_size(n: int) -> int:
    return ((n + 1 + 15) // 16) * 16


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".mkv", ".webm", ".avi"}
MEDIA_LIMIT = 30 * 1024 * 1024


class WeChatChannel(Channel):
    name = "wechat"
    label = "微信 (ClawBot)"
    sdk_module = ""
    extra = ""
    console_url = ""
    fields = (
        Field(
            "bot_token",
            "Bot token",
            "secret",
            help="From the ClawBot scan login; the app fills it in when you scan",
        ),
        Field("base_url", "Base URL", default=DEFAULT_BASE, help="iLink service address (the login returns one)"),
    )

    def __init__(self, settings: dict[str, Any]):
        super().__init__(settings)
        self._task: asyncio.Task[None] | None = None
        self._cursor = ""
        self._contexts: dict[str, str] = {}  # chat_id -> latest context_token
        self._typing_tickets: dict[str, str] = {}
        self._started_at = 0.0

    @property
    def _base(self) -> str:
        return str(self.settings.get("base_url") or DEFAULT_BASE).rstrip("/")

    @property
    def _token(self) -> str:
        return str(self.settings.get("bot_token") or "")

    # ------------------------------------------------------------------ lifecycle
    async def start(self, bus: ChannelBus) -> None:
        await super().start(bus)
        if not self._token:
            bus.set_status(self.name, "unconfigured", "scan the login code first")
            return
        self._started_at = time.time()
        self._task = asyncio.create_task(self._poll_loop(), name="wechat-poll")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task

    async def verify(self) -> str:
        if not self._token:
            raise WeChatError("no bot token yet — use the scan login")
        data = await _fetch(
            self._base,
            "/ilink/bot/getupdates",
            token=self._token,
            method="POST",
            body={"get_updates_buf": "", "base_info": {"channel_version": CHANNEL_VERSION}},
            timeout=15.0,
        )
        if data.get("errcode") not in (None, 0):
            raise WeChatError(f"iLink errcode={data.get('errcode')} {data.get('errmsg') or ''}")
        return "ClawBot connected"

    # ------------------------------------------------------------------ inbound
    async def _poll_loop(self) -> None:
        delay = 2.0
        while True:
            try:
                data = await _fetch(
                    self._base,
                    "/ilink/bot/getupdates",
                    token=self._token,
                    method="POST",
                    body={
                        "get_updates_buf": self._cursor,
                        "base_info": {"channel_version": CHANNEL_VERSION},
                    },
                    timeout=50.0,
                )
                delay = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("wechat: polling failed: {}", exc)
                if self.bus is not None:
                    self.bus.set_status(self.name, "connecting", str(exc)[:200])
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            if data.get("errcode") == -14:
                if self.bus is not None:
                    self.bus.set_status(self.name, "error", "login expired — scan the login code again")
                await asyncio.sleep(60.0)
                continue
            if data.get("get_updates_buf"):
                self._cursor = str(data["get_updates_buf"])
            if self.bus is not None:
                self.bus.set_status(self.name, "connected", "ClawBot")
            for msg in data.get("msgs") or []:
                try:
                    await self._handle(msg)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("wechat: message failed: {}", exc)

    async def _handle(self, msg: dict[str, Any]) -> None:
        if self.bus is None:
            return
        if msg.get("message_type") == 2:
            return  # the bot's own message
        sender = str(msg.get("from_user_id") or "")
        if not sender or sender.endswith("@im.bot"):
            return
        if msg.get("context_token"):
            self._contexts[sender] = str(msg["context_token"])
        parts: list[str] = []
        files: list[InboundFile] = []
        for item in msg.get("item_list") or []:
            itype = item.get("type")
            if itype == 1 and (item.get("text_item") or {}).get("text"):
                parts.append(str(item["text_item"]["text"]))
            elif itype == 3 and (item.get("voice_item") or {}).get("text"):
                parts.append(str(item["voice_item"]["text"]))
            elif itype == 2 and (item.get("image_item") or {}).get("media"):
                data = await self._download_image(item["image_item"])
                if data:
                    files.append(InboundFile(name=f"wechat-{uuid.uuid4().hex[:8]}.jpg", data=data, kind="image"))
                else:
                    parts.append("[图片]")
            elif itype == 4:
                parts.append(f"[文件] {(item.get('file_item') or {}).get('file_name') or ''}")
            elif itype == 5:
                parts.append("[视频]")
        text = "\n".join(parts).strip()
        if not text and not files:
            return
        self.bus.inbound(
            InboundMessage(
                channel=self.name,
                chat_id=sender,
                sender_id=sender,
                text=text,
                files=files,
                message_id=str(msg.get("msg_id") or msg.get("message_id") or ""),
            )
        )

    async def _download_image(self, image_item: dict[str, Any]) -> bytes | None:
        media = image_item.get("media") or {}
        url = str(media.get("full_url") or "")
        if not url and media.get("encrypt_query_param"):
            url = f"{CDN_BASE}/download?encrypted_query_param={media['encrypt_query_param']}"
        if not url:
            return None
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=15.0)) as http:
                r = await http.get(url)
            if r.status_code >= 400:
                return None
            raw = r.content
            aes_b64 = ""
            if image_item.get("aeskey"):
                aes_b64 = base64.b64encode(bytes.fromhex(str(image_item["aeskey"]))).decode()
            elif media.get("aes_key"):
                aes_b64 = str(media["aes_key"])
            return _decrypt_image(raw, aes_b64) if aes_b64 else raw
        except Exception as exc:  # noqa: BLE001
            logger.warning("wechat: image download failed: {}", exc)
            return None

    # ------------------------------------------------------------------ outbound
    async def _send_items(self, chat_id: str, items: list[dict[str, Any]]) -> str:
        client_id = f"nm-{uuid.uuid4()}"
        msg: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": chat_id,
            "client_id": client_id,
            "message_type": 2,
            "message_state": 2,
            "item_list": items,
        }
        context = self._contexts.get(chat_id)
        if context:
            msg["context_token"] = context
        data = await _fetch(
            self._base,
            "/ilink/bot/sendmessage",
            token=self._token,
            method="POST",
            body={"msg": msg, "base_info": {"channel_version": CHANNEL_VERSION}},
            timeout=20.0,
        )
        if data.get("errcode") not in (None, 0):
            raise WeChatError(f"sendmessage errcode={data.get('errcode')} {data.get('errmsg') or ''}")
        return client_id

    async def _upload_media(self, path: Path, chat_id: str) -> dict[str, Any]:
        """Encrypt one file and push it to the WeChat CDN, the official pipeline."""
        raw = path.read_bytes()
        if len(raw) > MEDIA_LIMIT:
            raise WeChatError(f"{path.name} is too large for WeChat ({len(raw)} bytes)")
        suffix = path.suffix.lower()
        media_type = 1 if suffix in _IMAGE_SUFFIXES else 2 if suffix in _VIDEO_SUFFIXES else 3
        aeskey = os.urandom(16)
        filekey = secrets.token_hex(16)
        resp = await _fetch(
            self._base,
            "/ilink/bot/getuploadurl",
            token=self._token,
            method="POST",
            body={
                "filekey": filekey,
                "media_type": media_type,
                "to_user_id": chat_id,
                "rawsize": len(raw),
                "rawfilemd5": hashlib.md5(raw).hexdigest(),
                "filesize": _padded_size(len(raw)),
                "no_need_thumb": True,
                "aeskey": aeskey.hex(),
                "base_info": {"channel_version": CHANNEL_VERSION},
            },
            timeout=20.0,
        )
        upload_url = str(resp.get("upload_full_url") or "").strip()
        if not upload_url and resp.get("upload_param"):
            from urllib.parse import quote

            upload_url = (
                f"{CDN_BASE}/upload?encrypted_query_param={quote(str(resp['upload_param']))}"
                f"&filekey={quote(filekey)}"
            )
        if not upload_url:
            raise WeChatError(f"getuploadurl returned no upload URL: {str(resp)[:200]}")
        ciphertext = _encrypt_media(raw, aeskey)
        download_param = ""
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0)) as http:
            for attempt in range(3):
                r = await http.post(
                    upload_url,
                    content=ciphertext,
                    headers={"Content-Type": "application/octet-stream"},
                )
                if 400 <= r.status_code < 500:
                    raise WeChatError(
                        f"CDN upload refused: HTTP {r.status_code} {r.headers.get('x-error-message') or ''}"
                    )
                if r.status_code == 200 and r.headers.get("x-encrypted-param"):
                    download_param = str(r.headers["x-encrypted-param"])
                    break
                if attempt == 2:
                    raise WeChatError(f"CDN upload failed: HTTP {r.status_code}")
                await asyncio.sleep(2.0)
        media = {
            "encrypt_query_param": download_param,
            "aes_key": base64.b64encode(aeskey.hex().encode()).decode(),
            "encrypt_type": 1,
        }
        cipher_size = _padded_size(len(raw))
        if media_type == 1:
            item: dict[str, Any] = {"type": 2, "image_item": {"media": media, "mid_size": cipher_size}}
        elif media_type == 2:
            item = {"type": 5, "video_item": {"media": media, "video_size": cipher_size}}
        else:
            item = {
                "type": 4,
                "file_item": {"media": media, "file_name": path.name, "len": str(len(raw))},
            }
        return item

    async def send(self, chat_id: str, text: str, *, files: list[str] | None = None) -> str:
        if not self._token:
            raise WeChatError("no bot token yet — use the scan login")
        plain = _md_to_plain(text or "")
        paths = [Path(p) for p in files or [] if Path(p).is_file()]
        last = ""
        if not paths:
            for i in range(0, len(plain), TEXT_LIMIT):
                chunk = plain[i : i + TEXT_LIMIT]
                if chunk:
                    last = await self._send_items(chat_id, [{"type": 1, "text_item": {"text": chunk}}])
            return last
        caption = plain[:TEXT_LIMIT] if plain else ""
        rest = plain[TEXT_LIMIT:] if len(plain) > TEXT_LIMIT else ""
        for chunk_start in range(0, len(rest), TEXT_LIMIT):
            chunk = rest[chunk_start : chunk_start + TEXT_LIMIT]
            if chunk:
                last = await self._send_items(chat_id, [{"type": 1, "text_item": {"text": chunk}}])
        for i, path in enumerate(paths):
            item = await self._upload_media(path, chat_id)
            items = []
            if i == 0 and caption:
                items.append({"type": 1, "text_item": {"text": caption}})
            items.append(item)
            last = await self._send_items(chat_id, items)
        return last

    async def typing(self, chat_id: str, on: bool) -> None:
        context = self._contexts.get(chat_id)
        if not context:
            return
        try:
            ticket = self._typing_tickets.get(chat_id)
            if ticket is None:
                data = await _fetch(
                    self._base,
                    "/ilink/bot/getconfig",
                    token=self._token,
                    method="POST",
                    body={
                        "ilink_user_id": chat_id,
                        "context_token": context,
                        "base_info": {"channel_version": CHANNEL_VERSION},
                    },
                    timeout=10.0,
                )
                ticket = str(data.get("typing_ticket") or "")
                if ticket:
                    self._typing_tickets[chat_id] = ticket
            if ticket:
                await _fetch(
                    self._base,
                    "/ilink/bot/sendtyping",
                    token=self._token,
                    method="POST",
                    body={
                        "ilink_user_id": chat_id,
                        "typing_ticket": ticket,
                        "status": 1 if on else 2,
                        "base_info": {"channel_version": CHANNEL_VERSION},
                    },
                    timeout=10.0,
                )
        except Exception:  # noqa: BLE001 — typing is decoration; never fail the run for it
            pass


__all__ = ["WeChatChannel", "login_begin", "login_poll"]
