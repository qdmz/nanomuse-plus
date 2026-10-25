"""Holds: the person takes a browser, a screen or a phone over for a while, and the agent
waits (docs/browser.md, docs/gui.md).

A *hold* belongs to one chat (``thread``) and one kind of hands (``tool``: ``browser``,
``computer`` or ``phone``). It is opened by the person ("Take over" in the app — ``by:
user``) or by the agent when a page needs them ("hand_over" with a reason: a sign-in form,
a CAPTCHA, a payment — ``by: agent``), and closed by the person's **Done**. While a hold is
on, every action of that tool for that chat waits at :meth:`Holds.wait`; it does not fail.
When the hold goes off, the waiting tool takes a fresh observation and tells the model that
the person took over for a while, so it looks again instead of trusting what it last saw.

Every change is a ``hold`` event in the chat's timeline (``status: on | off``), so the chat,
the capsule on the phone, the stage on the desktop and the browser viewer in the web app
all show one card with the same Done button. The open ones ride in the hello state as
``holds``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from nanomuse.logger import logger

TOOLS = ("browser", "computer", "phone")
# how long the agent's own hand-over waits for the person before it goes on without them
HAND_OVER_TIMEOUT_S = 600.0


def new_hold_id() -> str:
    import uuid

    return "h_" + uuid.uuid4().hex[:10]


@dataclass
class Hold:
    id: str
    thread: str
    tool: str
    by: str  # user | agent
    reason: str = ""
    status: str = "on"  # on | off
    ts: float = field(default_factory=time.time)
    done_ts: float | None = None
    # set when the hold goes off; whoever waits on it wakes up
    released: asyncio.Event = field(default_factory=asyncio.Event, repr=False)

    @property
    def open(self) -> bool:
        return self.status == "on"

    def to_event(self) -> dict[str, Any]:
        """The timeline event for this hold, as the clients read it (contract C1)."""
        out: dict[str, Any] = {
            "type": "hold",
            "id": self.id,
            "thread": self.thread,
            "tool": self.tool,
            "status": self.status,
            "by": self.by,
            "reason": self.reason,
            "ts": self.ts,
        }
        if self.done_ts is not None:
            out["done_ts"] = self.done_ts
        return out


class Holds:
    """The registry: at most one open hold per (thread, tool).

    ``emit`` is called with the event of every change (the service wires it to the chat's
    timeline). ``thread_of`` names the chat an action belongs to when the caller does not
    know it — the server sets it to the worker's context variable; the CLI has one chat.
    """

    def __init__(
        self,
        emit: Callable[[dict[str, Any]], None] | None = None,
        thread_of: Callable[[], str] | None = None,
    ):
        self.emit = emit
        self.thread_of = thread_of or (lambda: "main")
        self._holds: dict[str, Hold] = {}

    # ------------------------------------------------------------------ reading
    def get(self, hold_id: str) -> Hold | None:
        return self._holds.get(hold_id)

    def current(self, thread: str, tool: str) -> Hold | None:
        """The open hold for this chat and tool, if any."""
        for hold in self._holds.values():
            if hold.open and hold.thread == thread and hold.tool == tool:
                return hold
        return None

    def active(self, thread: str | None = None) -> list[Hold]:
        return [
            h for h in self._holds.values() if h.open and (thread is None or h.thread == thread)
        ]

    def is_held(self, thread: str, tool: str) -> bool:
        return self.current(thread, tool) is not None

    def view(self) -> list[dict[str, Any]]:
        """The open holds, for the hello state."""
        return [h.to_event() for h in self.active()]

    def thread(self) -> str:
        try:
            return str(self.thread_of() or "main")
        except Exception:  # noqa: BLE001 — a provider that fails must not stop an action
            return "main"

    # ------------------------------------------------------------------ changing
    def open(self, thread: str, tool: str, by: str = "user", reason: str = "") -> Hold:
        """Put a hold on: the person took over (``by="user"``) or the agent asked them to
        (``by="agent"``). A second call for the same chat and tool returns the open hold —
        with the new reason when one is given — rather than a second card."""
        if tool not in TOOLS:
            raise ValueError(f"unknown tool {tool!r}; one of {', '.join(TOOLS)}")
        if by not in ("user", "agent"):
            raise ValueError("by must be 'user' or 'agent'")
        reason = " ".join(str(reason or "").split())[:300]
        existing = self.current(thread, tool)
        if existing is not None:
            if reason and reason != existing.reason:
                existing.reason = reason
                self._emit(existing)
            return existing
        hold = Hold(id=new_hold_id(), thread=thread, tool=tool, by=by, reason=reason)
        self._holds[hold.id] = hold
        logger.info("hold on: {} in {} by {}: {}", tool, thread, by, reason or "(no reason)")
        self._emit(hold)
        return hold

    def done(self, hold_id: str) -> Hold | None:
        """The person is done: the hold goes off and whoever waits on it continues.
        None when no such hold; an already closed one comes back unchanged."""
        hold = self._holds.get(hold_id)
        if hold is None:
            return None
        if hold.open:
            hold.status = "off"
            hold.done_ts = time.time()
            hold.released.set()
            logger.info("hold off: {} in {}", hold.tool, hold.thread)
            self._emit(hold)
        return hold

    def release(self, thread: str, tool: str) -> Hold | None:
        """Done, by chat and tool (the phone's ``handed_back`` names no hold id)."""
        hold = self.current(thread, tool)
        return self.done(hold.id) if hold is not None else None

    def clear_thread(self, thread: str) -> None:
        """A chat was cleared or deleted: its holds go off so nothing waits for good."""
        for hold in list(self._holds.values()):
            if hold.thread == thread:
                if hold.open:
                    hold.status = "off"
                    hold.done_ts = time.time()
                    hold.released.set()
                self._holds.pop(hold.id, None)

    def forget_closed(self, keep: int = 50) -> None:
        closed = [h for h in self._holds.values() if not h.open]
        for hold in sorted(closed, key=lambda h: h.done_ts or 0)[:-keep]:
            self._holds.pop(hold.id, None)

    # ------------------------------------------------------------------ waiting
    async def wait(self, thread: str, tool: str, timeout: float | None = None) -> bool:
        """Block while a hold is on for this chat and tool. True when there was one (so the
        caller knows to look again), False when the way was clear. With a ``timeout`` the
        wait ends after that many seconds whether or not the hold is off."""
        waited = False
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            hold = self.current(thread, tool)
            if hold is None:
                return waited
            waited = True
            remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
            if remaining == 0.0:
                return waited
            try:
                await asyncio.wait_for(hold.released.wait(), remaining)
            except TimeoutError:
                return waited

    async def hand_over(
        self,
        thread: str,
        tool: str,
        reason: str,
        timeout: float = HAND_OVER_TIMEOUT_S,
    ) -> bool:
        """The agent's own hand-over: a hold goes on (``by: agent``) and this waits for the
        person's Done. True when they finished, False when they did not come back in time
        (the hold is taken off then, so the next action does not wait again)."""
        hold = self.open(thread, tool, by="agent", reason=reason)
        try:
            await asyncio.wait_for(hold.released.wait(), timeout)
        except TimeoutError:
            if hold.open:
                hold.status = "off"
                hold.done_ts = time.time()
                hold.released.set()
                self._emit(hold, timed_out=True)
            return False
        return True

    # ------------------------------------------------------------------ events
    def _emit(self, hold: Hold, timed_out: bool = False) -> None:
        if self.emit is None:
            return
        event = hold.to_event()
        if timed_out:
            event["timed_out"] = True
        try:
            self.emit(event)
        except Exception as exc:  # noqa: BLE001 — a listener must not stop the hold
            logger.warning("hold listener failed: {}", exc)


def took_over_note(tool: str, finished: bool = True) -> str:
    """What the model is told once a hold went off, before the fresh observation."""
    thing = {"browser": "the browser", "computer": "this computer", "phone": "the phone"}.get(
        tool, tool
    )
    if finished:
        return (
            f"Note: the user took over {thing} for a while and has finished. Look at it again "
            "before the next step; continue from what is there now and do not redo what they did."
        )
    return (
        f"Note: you handed {thing} to the user, but they did not come back within 10 minutes. "
        "Look at it again; if the step still needs them, say so and stop."
    )


__all__ = ["HAND_OVER_TIMEOUT_S", "TOOLS", "Hold", "Holds", "new_hold_id", "took_over_note"]
