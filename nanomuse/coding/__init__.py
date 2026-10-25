"""The coding agents on this computer (Cursor, Codex, Claude Code, DeepSeek Harness), seen
and steered from any device of the account.

``agents`` reads what the tools leave on disk (their session transcripts) and finds their
command-line entry points; ``runner`` sends a message into a session through those entry
points and streams what comes back; ``service`` ties both to the runtime (the bus, the hub)
so the phone or another computer can list sessions, read one, send a message and stop a run.
"""

from nanomuse.coding.agents import (
    AGENTS,
    AgentInfo,
    Session,
    detect,
    read_session,
    sessions,
)
from nanomuse.coding.runner import Run, RunEvent, start_run

__all__ = [
    "AGENTS",
    "AgentInfo",
    "Run",
    "RunEvent",
    "Session",
    "detect",
    "read_session",
    "sessions",
    "start_run",
]
