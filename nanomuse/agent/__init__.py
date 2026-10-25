"""The agent loop (:mod:`nanomuse.agent.core`) and the hold registry
(:mod:`nanomuse.agent.holds`). ``core`` pulls in the Sentinel and the tools, and the tools
import ``holds``; so ``core`` is loaded on first use rather than here, and
``from nanomuse.agent.holds import Holds`` works from anywhere without a cycle."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from nanomuse.agent.core import Incoming, MuseAgent

__all__ = ["Incoming", "MuseAgent"]


def __getattr__(name: str) -> Any:
    if name in __all__:
        from nanomuse.agent import core

        return getattr(core, name)
    raise AttributeError(f"module 'nanomuse.agent' has no attribute {name!r}")
