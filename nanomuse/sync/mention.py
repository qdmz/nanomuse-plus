"""``@<device>`` at the start of a message: the person wants the work done on another
device of the account (contract C7, rule 8). The default is always this device; only a
mention that matches a device the hub lists is taken, and it is removed from the text."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

_BOUNDARY = re.compile(r"[\s,:;，：；]")


@dataclass(frozen=True)
class Mention:
    device_id: str
    device_name: str
    text: str  # what is left once the mention is gone


def system_note(device_name: str, device_id: str) -> str:
    """The one line put in front of the turn so the agent hands the work over."""
    return (
        f"The person addressed {device_name} (id {device_id}): run this there with `delegate` "
        "and report what it did."
    )


def parse_mention(text: str, devices: list[dict[str, Any]], self_id: str = "") -> Mention | None:
    """``@Pixel 8 open the calendar`` → the device named "Pixel 8" and "open the calendar".

    Case-insensitive. A device's whole name at the start wins (the longest when two names
    share a start); failing that, the first word after ``@`` as a prefix of exactly one
    device's name (``@pix`` → Pixel 8). ``self_id`` is never a target. None when nothing
    matches — the text is then an ordinary message, ``@`` and all."""
    s = text.lstrip()
    if not s.startswith("@") or len(s) < 2:
        return None
    rest = s[1:]
    rest_l = rest.lower()
    others = [
        d
        for d in devices
        if isinstance(d, dict) and str(d.get("id") or "") and str(d.get("id")) != self_id
    ]
    best: tuple[int, dict[str, Any]] | None = None
    for d in others:
        name = str(d.get("name") or "").strip()
        if not name:
            continue
        n = len(name)
        if rest_l.startswith(name.lower()) and (len(rest) == n or _BOUNDARY.match(rest[n])):
            if best is None or n > best[0]:
                best = (n, d)
    if best is None:
        word = _BOUNDARY.split(rest, 1)[0]
        if not word:
            return None
        hits = [
            d for d in others if str(d.get("name") or "").strip().lower().startswith(word.lower())
        ]
        if len(hits) != 1:
            # several devices share the start ("Mac", "Mac mini"): say which one
            online = [d for d in hits if d.get("online")]
            if len(online) != 1:
                return None
            hits = online
        best = (len(word), hits[0])
    n, device = best
    remainder = rest[n:].lstrip(" \t,:;，：；")
    return Mention(
        device_id=str(device.get("id")),
        device_name=str(device.get("name") or device.get("id")),
        text=remainder,
    )
