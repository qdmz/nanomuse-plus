"""What every chat channel looks like from the inside.

A *channel* puts your nanoMuse inside a chat app you already use — Feishu, DingTalk, WeCom,
Telegram. The vendor's long connection (or long polling) brings messages in; nothing here
needs a public URL. A channel knows how to connect, how to turn a vendor event into an
:class:`InboundMessage`, and how to send text and files back. Everything else — who may talk
to the bot, which chat maps to which conversation, streaming the reply — is the manager's job
(:mod:`nanomuse.channels.manager`), the same for every vendor.

Channels import their SDK lazily, inside ``start()``: the package imports without any of them
installed, and a missing SDK shows up as a one-line "install with …" rather than a crash.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from typing import Any, Protocol

FieldKind = str  # "string" | "secret" | "bool" | "choice"


def json_object(response: Any) -> dict[str, Any]:
    """The JSON object in an HTTP response, or ``{}`` when the body is not one.

    A vendor's gateway answers an outage with an HTML page or an empty body; the caller
    then reports the status code instead of crashing on the parse.
    """
    try:
        data = response.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


@dataclass(frozen=True)
class Field:
    """One setting a channel needs, as the app and the CLI show it."""

    key: str
    label: str
    kind: FieldKind = "string"
    required: bool = False
    help: str = ""
    choices: tuple[str, ...] = ()
    default: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "kind": self.kind,
            "required": self.required,
            "help": self.help,
            "choices": list(self.choices),
            "default": self.default,
        }


@dataclass
class InboundFile:
    """A file someone sent in the chat: its bytes, saved to the workspace by the manager."""

    name: str
    data: bytes
    kind: str = "file"  # "image" | "file" | "audio"


@dataclass
class InboundMessage:
    channel: str
    chat_id: str
    sender_id: str
    sender_name: str = ""
    text: str = ""
    files: list[InboundFile] = field(default_factory=list)
    is_group: bool = False
    # in a group: was the bot addressed (an @, or a reply to it)? Direct chats: always
    mentioned: bool = True
    # the vendor's id of this message (reactions, replies)
    message_id: str = ""
    # a name for the chat, where the vendor gives one (group title, the person's name)
    chat_name: str = ""


@dataclass
class OutboundMessage:
    chat_id: str
    text: str = ""
    files: list[str] = field(default_factory=list)


@dataclass
class ChannelStatus:
    """Where a channel is: ``off``, ``unconfigured``, ``missing_sdk``, ``connecting``,
    ``connected`` or ``error`` (with the vendor's words in ``detail``)."""

    state: str = "off"
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "detail": self.detail}


class ChannelBus(Protocol):
    """What a channel calls back into — the manager implements it."""

    def inbound(self, msg: InboundMessage) -> None:
        """A message arrived. Safe from any thread."""

    def set_status(self, channel: str, state: str, detail: str = "") -> None:
        """The connection changed state. Safe from any thread."""

    def action(self, channel: str, chat_id: str, sender_id: str, value: dict[str, Any]) -> None:
        """A button on a card was pressed (``value`` is what the card carried). Safe from any
        thread."""


class Channel:
    """Base class of a channel. Subclasses set the class attributes and implement
    ``start`` / ``stop`` / ``send``; the rest has workable defaults."""

    name: str = "base"
    label: str = "Base"
    # the module the vendor SDK is imported as, "" when none is needed (Telegram)
    sdk_module: str = ""
    # the extra that installs it: pip install nanomuse[<extra>]
    extra: str = ""
    fields: tuple[Field, ...] = ()
    # can a message be edited after it was sent (the reply streams in place)?
    supports_edit: bool = False
    # the vendor's console, for the setup walkthrough
    console_url: str = ""

    def __init__(self, settings: dict[str, Any]):
        self.settings = settings
        self.bus: ChannelBus | None = None

    # ------------------------------------------------------------------ availability
    @classmethod
    def sdk_available(cls) -> bool:
        if not cls.sdk_module:
            return True
        try:
            return importlib.util.find_spec(cls.sdk_module) is not None
        except (ImportError, ValueError):
            return False

    @classmethod
    def install_hint(cls) -> str:
        return f"pip install 'nanomuse[{cls.extra}]'" if cls.extra else ""

    @classmethod
    def missing(cls, settings: dict[str, Any]) -> list[str]:
        """The required fields that are empty."""
        return [f.key for f in cls.fields if f.required and not str(settings.get(f.key) or "")]

    # ------------------------------------------------------------------ lifecycle
    async def start(self, bus: ChannelBus) -> None:
        """Connect and start delivering messages to ``bus``. Returns once the connection is
        under way; report ``connected`` / ``error`` through ``bus.set_status``."""
        self.bus = bus

    async def stop(self) -> None:
        """Close the connection. Must be safe to call twice."""

    # ------------------------------------------------------------------ outbound
    async def send(self, chat_id: str, text: str, *, files: list[str] | None = None) -> str:
        """Send text (and files, by path) to a chat; returns the vendor's message id or ``""``.
        Raise on failure — the manager logs it and tells the person in the app."""
        raise NotImplementedError

    async def stream(self, chat_id: str, stream_id: str, text: str, *, done: bool) -> None:
        """A reply that grows: the first call sends a message, later calls update it in place,
        ``done`` is the last. Channels that cannot edit send once, when done."""
        if done and text.strip():
            await self.send(chat_id, text)

    async def typing(self, chat_id: str, on: bool) -> None:
        """Show that the Muse is working on it (a typing indicator, a reaction). Optional."""

    async def ask(self, chat_id: str, approval_id: str, text: str) -> None:
        """An approval the run is waiting for. ``text`` already explains the reply words
        (允许 / 拒绝); channels with buttons add two of them carrying
        ``{"approval": approval_id, "decision": "allow" | "deny"}``."""
        await self.send(chat_id, text)

    async def verify(self) -> str:
        """Check the credentials without connecting for good; returns a short description of
        the bot (its name) or raises with the vendor's message."""
        return ""


__all__ = [
    "Channel",
    "ChannelBus",
    "ChannelStatus",
    "Field",
    "InboundFile",
    "InboundMessage",
    "OutboundMessage",
]
