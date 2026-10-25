"""The app-protocol fences the agent writes in a chat and the clients act on.

The same tags, keys and rules as the phones (``onboarding/FirstConversation.kt``,
``NanoMuseFirstConversation.swift``) and the desktop (``harness/dsh-nanomuse/src/fences.ts``),
so one agent behaves the same on every device. Only the first conversation's fence is read
by the runtime today::

    ```nanomuse-naming   {"user_address": "<name>" | null, "suggest": ["<a>", "<b>"]}
                         {"agent_name": "<the name>"}

Pure: parsing and stripping; ``nanomuse/server/firstrun.py`` applies the block.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

FENCE_NAMING = "nanomuse-naming"
# a name is one line, at most this long (the phone's limit)
MAX_NAME = 16

_QUOTES_HEAD = "\"'“”‘’「」『』"
_QUOTES_TAIL = _QUOTES_HEAD + "。，、！!？?."


def _fence_re(tag: str) -> re.Pattern[str]:
    return re.compile("```" + re.escape(tag) + r"[ \t]*\r?\n(.*?)```", re.DOTALL)


def find_fences(text: str, tag: str) -> list[str]:
    """Every complete fence tagged ``tag`` in ``text``, inner text as written."""
    if "```" + tag not in text:
        return []
    return [m.group(1) for m in _fence_re(tag).finditer(text)]


def strip_fences(text: str, tag: str) -> str:
    """``text`` without its ``tag`` fences: every complete one, and an unterminated one at
    the end (a reply cut off, or still streaming). Text without the tag comes back as is."""
    opener = "```" + tag
    if opener not in text:
        return text
    out = _fence_re(tag).sub("", text)
    cut = out.rfind(opener)
    if cut != -1:
        out = out[:cut]
    return out.rstrip()


def _obj(raw: str) -> dict[str, Any] | None:
    try:
        value = json.loads(raw.strip())
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def clean_name(raw: Any) -> str | None:
    """A usable name: one line, quotes and trailing punctuation gone, not absurdly long;
    None otherwise."""
    if not isinstance(raw, str):
        return None
    t = raw.strip().lstrip(_QUOTES_HEAD).rstrip(_QUOTES_TAIL).strip()
    if not t or "\n" in t or len(t) > MAX_NAME:
        return None
    return t


@dataclass
class NamingBlock:
    """What the model told the app in a ``nanomuse-naming`` block."""

    # the block had a `user_address` key (a null value = the person wants no form of address)
    address_given: bool = False
    user_address: str | None = None
    # names the model suggests for itself, cleaned, distinct, at most three
    suggestions: list[str] = field(default_factory=list)
    # the name the person gave the agent, when this block says so
    agent_name: str | None = None


def parse_naming_block(text: str | None) -> NamingBlock | None:
    """The last ``nanomuse-naming`` block in ``text``, or None when there is none or it is
    not JSON."""
    if not text:
        return None
    fences = find_fences(text, FENCE_NAMING)
    if not fences:
        return None
    o = _obj(fences[-1])
    if o is None:
        return None
    suggestions: list[str] = []
    raw_suggest = o.get("suggest")
    if isinstance(raw_suggest, list):
        for raw in raw_suggest:
            name = clean_name(raw)
            if name and name not in suggestions:
                suggestions.append(name)
            if len(suggestions) == 3:
                break
    address = o.get("user_address")
    agent = o.get("agent_name")
    return NamingBlock(
        address_given="user_address" in o,
        user_address=None if address is None else clean_name(address),
        suggestions=suggestions,
        agent_name=None if agent is None else clean_name(agent),
    )


__all__ = [
    "FENCE_NAMING",
    "MAX_NAME",
    "NamingBlock",
    "clean_name",
    "find_fences",
    "parse_naming_block",
    "strip_fences",
]
