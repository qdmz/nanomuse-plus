"""Telegram, over the Bot API with plain HTTPS long polling (``httpx`` only, no SDK).

``getUpdates`` with a long timeout brings messages in; replies are ``sendMessage`` and are
edited in place with ``editMessageText`` while they stream. Approvals are two inline buttons.
Nothing needs a public URL; a proxy can be set for networks where ``api.telegram.org`` is
out of reach.

Setup: in Telegram talk to @BotFather → ``/newbot`` → copy the token. Direct messages work
at once. For groups, add the bot to the group; with BotFather's *Group Privacy* on (the
default) the bot only hears messages that @-mention it or reply to it, which matches the
``mention`` policy.
"""

from __future__ import annotations

import asyncio
import mimetypes
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

API = "https://api.telegram.org"
POLL_TIMEOUT_S = 50
TEXT_LIMIT = 4000  # Telegram allows 4096 per message
_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


class TelegramError(RuntimeError):
    pass


class TelegramChannel(Channel):
    name = "telegram"
    label = "Telegram"
    sdk_module = ""
    extra = ""
    supports_edit = True
    console_url = "https://t.me/BotFather"
    fields = (
        Field("bot_token", "Bot token", "secret", required=True, help="From @BotFather, /newbot"),
        Field(
            "proxy", "Proxy", help="Optional, e.g. socks5://127.0.0.1:1080 or http://127.0.0.1:7890"
        ),
    )

    def __init__(self, settings: dict[str, Any], transport: httpx.AsyncBaseTransport | None = None):
        super().__init__(settings)
        self._transport = transport
        self._http: httpx.AsyncClient | None = None
        self._task: asyncio.Task[None] | None = None
        self._offset = 0
        self._bot_id = 0
        self._username = ""
        self._stream_messages: dict[str, int] = {}
        self._typing: dict[str, asyncio.Task[None]] = {}

    # ------------------------------------------------------------------ lifecycle
    def _make_http(self) -> httpx.AsyncClient:
        kwargs: dict[str, Any] = {
            "base_url": f"{API}/bot{self.settings.get('bot_token') or ''}",
            "timeout": httpx.Timeout(20.0, read=POLL_TIMEOUT_S + 15),
        }
        proxy = str(self.settings.get("proxy") or "").strip()
        if self._transport is not None:
            kwargs["transport"] = self._transport
        elif proxy:
            kwargs["proxy"] = proxy
        return httpx.AsyncClient(**kwargs)

    async def start(self, bus: ChannelBus) -> None:
        await super().start(bus)
        self._http = self._make_http()
        me = await self._call("getMe")
        self._bot_id = int(me.get("id") or 0)
        self._username = str(me.get("username") or "")
        # drop whatever queued up while we were away: a reply to a day-old message helps no one
        with suppress(Exception):
            await self._call("deleteWebhook", {"drop_pending_updates": False})
        self._task = asyncio.create_task(self._poll_loop(), name="telegram-poll")
        bus.set_status(self.name, "connected", f"@{self._username}" if self._username else "")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        for typing_task in list(self._typing.values()):
            typing_task.cancel()
        self._typing.clear()
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    async def verify(self) -> str:
        own = self._http is None
        if own:
            self._http = self._make_http()
        try:
            me = await self._call("getMe")
        finally:
            if own and self._http is not None:
                await self._http.aclose()
                self._http = None
        return f"@{me.get('username') or me.get('first_name') or 'bot'}"

    # ------------------------------------------------------------------ the API
    def _redact(self, text: str) -> str:
        """httpx puts the URL — and so the token — into some of its messages."""
        token = str(self.settings.get("bot_token") or "")
        return text.replace(token, "<token>") if token else text

    async def _call(self, method: str, params: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        if self._http is None:
            raise TelegramError("Not connected.")
        try:
            response = await self._http.post(f"/{method}", json=params, **kwargs)
        except httpx.HTTPError as exc:
            raise TelegramError(self._redact(f"{type(exc).__name__}: {exc}")[:200]) from exc
        data = json_object(response)
        if not data.get("ok"):
            raise TelegramError(str(data.get("description") or f"HTTP {response.status_code}"))
        return data.get("result")

    async def _poll_loop(self) -> None:
        delay = 2.0
        while True:
            try:
                updates = await self._call(
                    "getUpdates",
                    {
                        "offset": self._offset,
                        "timeout": POLL_TIMEOUT_S,
                        "allowed_updates": ["message", "callback_query"],
                    },
                )
                delay = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("telegram: polling failed: {}", self._redact(str(exc)))
                if self.bus is not None:
                    self.bus.set_status(self.name, "connecting", self._redact(str(exc))[:200])
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
                continue
            if self.bus is not None and updates and delay == 2.0:
                self.bus.set_status(
                    self.name, "connected", f"@{self._username}" if self._username else ""
                )
            for update in updates or []:
                self._offset = max(self._offset, int(update.get("update_id", 0)) + 1)
                try:
                    await self._handle_update(update)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("telegram: update failed: {}", self._redact(str(exc)))

    # ------------------------------------------------------------------ inbound
    async def _handle_update(self, update: dict[str, Any]) -> None:
        if self.bus is None:
            return
        callback = update.get("callback_query")
        if isinstance(callback, dict):
            await self._handle_callback(callback)
            return
        message = update.get("message")
        if not isinstance(message, dict):
            return
        sender = message.get("from") or {}
        if sender.get("is_bot"):
            return
        chat = message.get("chat") or {}
        chat_id = str(chat.get("id") or "")
        is_group = chat.get("type") in ("group", "supergroup")
        text = str(message.get("text") or message.get("caption") or "")
        mentioned = not is_group or self._mentioned(message, text)
        text = self._strip_mention(text)
        files: list[InboundFile] = []
        photo = message.get("photo")
        if isinstance(photo, list) and photo:
            best = photo[-1]
            item = await self._download(best.get("file_id"), "photo.jpg", "image")
            if item:
                files.append(item)
        document = message.get("document")
        if isinstance(document, dict):
            item = await self._download(
                document.get("file_id"), str(document.get("file_name") or "file"), "file"
            )
            if item:
                files.append(item)
        voice = message.get("voice") or message.get("audio")
        if isinstance(voice, dict):
            item = await self._download(
                voice.get("file_id"), str(voice.get("file_name") or "voice.ogg"), "audio"
            )
            if item:
                files.append(item)
        if not text and not files:
            return
        name = " ".join(p for p in (sender.get("first_name"), sender.get("last_name")) if p) or str(
            sender.get("username") or ""
        )
        self.bus.inbound(
            InboundMessage(
                channel=self.name,
                chat_id=chat_id,
                sender_id=str(sender.get("id") or ""),
                sender_name=name,
                text=text,
                files=files,
                is_group=is_group,
                mentioned=mentioned,
                message_id=str(message.get("message_id") or ""),
                chat_name=str(chat.get("title") or name),
            )
        )

    def _mentioned(self, message: dict[str, Any], text: str) -> bool:
        reply = message.get("reply_to_message")
        if isinstance(reply, dict) and (reply.get("from") or {}).get("id") == self._bot_id:
            return True
        return bool(self._username) and f"@{self._username}".lower() in text.lower()

    def _strip_mention(self, text: str) -> str:
        if self._username:
            text = text.replace(f"@{self._username}", "")
        return text.strip()

    async def _handle_callback(self, callback: dict[str, Any]) -> None:
        assert self.bus is not None
        raw = str(callback.get("data") or "")
        value: dict[str, Any] = {}
        if raw.startswith("a:") and raw.count(":") >= 2:
            _, decision, approval_id = raw.split(":", 2)
            value = {"approval": approval_id, "decision": "allow" if decision == "y" else "deny"}
        message = callback.get("message") or {}
        chat_id = str((message.get("chat") or {}).get("id") or "")
        sender_id = str((callback.get("from") or {}).get("id") or "")
        with suppress(Exception):
            await self._call("answerCallbackQuery", {"callback_query_id": callback.get("id")})
        if value:
            self.bus.action(self.name, chat_id, sender_id, value)
            # take the buttons off the card
            if message.get("message_id"):
                with suppress(Exception):
                    await self._call(
                        "editMessageReplyMarkup",
                        {
                            "chat_id": chat_id,
                            "message_id": message["message_id"],
                            "reply_markup": {"inline_keyboard": []},
                        },
                    )

    async def _download(self, file_id: Any, name: str, kind: str) -> InboundFile | None:
        if not file_id or self._http is None:
            return None
        try:
            info = await self._call("getFile", {"file_id": file_id})
            path = str(info.get("file_path") or "")
            if not path:
                return None
            token = str(self.settings.get("bot_token") or "")
            response = await self._http.get(f"{API}/file/bot{token}/{path}")
            if response.status_code >= 400:
                return None
            if name in ("photo.jpg", "file") and "." in path.rsplit("/", 1)[-1]:
                name = path.rsplit("/", 1)[-1]
            return InboundFile(name=name, data=response.content, kind=kind)
        except Exception as exc:  # noqa: BLE001
            logger.warning("telegram: download failed: {}", self._redact(str(exc)))
            return None

    # ------------------------------------------------------------------ outbound
    async def send(self, chat_id: str, text: str, *, files: list[str] | None = None) -> str:
        message_id = ""
        for chunk in _chunks(text):
            result = await self._call("sendMessage", {"chat_id": chat_id, "text": chunk})
            message_id = str(result.get("message_id") or "")
        for item in files or []:
            path = Path(item)
            if not path.is_file():
                continue
            await self._send_file(chat_id, path)
        return message_id

    async def _send_file(self, chat_id: str, path: Path) -> None:
        if self._http is None:
            raise TelegramError("Not connected.")
        is_image = path.suffix.lower() in _IMAGE_SUFFIXES and path.stat().st_size < 10_000_000
        method, field_name = ("sendPhoto", "photo") if is_image else ("sendDocument", "document")
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        with path.open("rb") as fh:
            response = await self._http.post(
                f"/{method}", data={"chat_id": chat_id}, files={field_name: (path.name, fh, mime)}
            )
        data = json_object(response)
        if not data.get("ok"):
            raise TelegramError(str(data.get("description") or f"HTTP {response.status_code}"))

    async def stream(self, chat_id: str, stream_id: str, text: str, *, done: bool) -> None:
        shown = text if len(text) <= TEXT_LIMIT else text[: TEXT_LIMIT - 1] + "…"
        message_id = self._stream_messages.get(stream_id)
        if message_id is None:
            result = await self._call("sendMessage", {"chat_id": chat_id, "text": shown or "…"})
            self._stream_messages[stream_id] = int(result.get("message_id") or 0)
        else:
            try:
                await self._call(
                    "editMessageText",
                    {"chat_id": chat_id, "message_id": message_id, "text": shown or "…"},
                )
            except TelegramError as exc:
                if "not modified" not in str(exc):
                    raise
        if done:
            self._stream_messages.pop(stream_id, None)
            if len(text) > TEXT_LIMIT:
                for chunk in _chunks(text[TEXT_LIMIT - 1 :]):
                    await self._call("sendMessage", {"chat_id": chat_id, "text": chunk})

    async def ask(self, chat_id: str, approval_id: str, text: str) -> None:
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "允许 · Allow", "callback_data": f"a:y:{approval_id}"[:64]},
                    {"text": "拒绝 · Deny", "callback_data": f"a:n:{approval_id}"[:64]},
                ]
            ]
        }
        await self._call(
            "sendMessage",
            {"chat_id": chat_id, "text": text[:TEXT_LIMIT], "reply_markup": keyboard},
        )

    async def typing(self, chat_id: str, on: bool) -> None:
        current = self._typing.pop(chat_id, None)
        if current is not None:
            current.cancel()
        if on:
            self._typing[chat_id] = asyncio.create_task(self._typing_loop(chat_id))

    async def _typing_loop(self, chat_id: str) -> None:
        # Telegram shows "typing…" for ~5 s per call
        for _ in range(60):
            with suppress(Exception):
                await self._call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
            await asyncio.sleep(4.5)


def _chunks(text: str, limit: int = TEXT_LIMIT) -> list[str]:
    text = text.strip()
    if not text:
        return []
    out: list[str] = []
    while len(text) > limit:
        cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        out.append(text[:cut])
        text = text[cut:].lstrip("\n")
    if text:
        out.append(text)
    return out


__all__ = ["TelegramChannel", "TelegramError"]
