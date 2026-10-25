"""Fire-and-forget work on the event loop, kept alive and heard when it fails.

The event loop holds only weak references to tasks: one created and dropped in the same
breath can be collected half-way, and an exception nobody awaits surfaces as a "Task
exception was never retrieved" line long after the fact, if at all. :func:`keep_task`
holds the reference; :func:`spawn` does that for a coroutine and logs what went wrong,
naming the work, so a card that never moves on has a line in the log that says why.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from typing import Any

from nanomuse.logger import logger

_BACKGROUND: set[asyncio.Task[Any]] = set()


def keep_task(task: asyncio.Task[Any]) -> asyncio.Task[Any]:
    """Hold a reference to a fire-and-forget task until it is done."""
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)
    return task


def spawn(coro: Coroutine[Any, Any, Any], what: str) -> asyncio.Task[Any]:
    """Run ``coro`` in the background; a failure is logged as ``what`` and not lost.

    Needs a running loop, like :func:`asyncio.create_task`; without one the coroutine
    is closed and :class:`RuntimeError` raised, so nothing is left half-made.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        coro.close()
        raise
    task = loop.create_task(coro, name=what)

    def _done(t: asyncio.Task[Any]) -> None:
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            logger.warning("{} failed: {}: {}", what, type(exc).__name__, exc)

    task.add_done_callback(_done)
    return keep_task(task)


__all__ = ["keep_task", "spawn"]
