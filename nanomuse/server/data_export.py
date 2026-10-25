"""The agent's data as one zip: `GET /api/data/export`.

The desktop's rooms host has had *Download your agent data* since 0.1.30 (a zip written to
Downloads); this is the same file for the web client, and for anyone whose runtime runs on
another machine: the reply is the zip itself, so nothing is written to the runtime's disk.

What goes in is what the runtime keeps about the person and its own work: the profile and
the look, the rooms (feed, ideas, memory, goals, reminders, triggers), the address book,
the approvals it was given, and the chats as it holds them (`threads/`, `sessions/`).

What never goes in is a credential or a key: `vault.enc` and `vault.key` (the secrets
themselves), `server_token` (the app's own password), `push-vapid.json` (the private key
that signs notifications). The settings file is left out for the same reason: it can hold
an API key in the clear, and the person sets those in the app anyway. `logs/` and
`workspace/` are left out as well: the first is noise, the second is the person's own files.
The README in the zip says all of this.
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path

# As the desktop's export: one file this big is not worth shipping inside a zip.
MAX_FILE_BYTES = 64 * 1024 * 1024

# Files of the agent's data, straight under the data dir. A missing one is simply not in
# the zip: every one of them appears when the feature that owns it has been used.
FILES = (
    "profile.json",
    "feed_posts.json",
    "ideas.json",
    "memory.db",
    "goals.db",
    "reminders.db",
    "triggers.db",
    "approvals.json",
    "contacts.vcf",
    "contacts-cache.json",
    "calendar-cache.json",
    "firstrun.json",
    "audit.jsonl",
)

# Folders of the chats, as the runtime keeps them.
DIRS = ("threads", "sessions")

README = """Your nanoMuse data, exported {when}

profile.json        the agent's name and look
feed_posts.json     the Feed as it was written
ideas.json          the ideas kept for a new chat
memory.db           what the agent remembers
goals.db            goals and their steps
reminders.db        reminders and routines
triggers.db         standing instructions that fire on something happening
approvals.json      the approvals it was given
contacts.vcf        the agent's address book
contacts-cache.json the contacts it looked up
calendar-cache.json the calendar as last read
firstrun.json       the first-conversation state
audit.jsonl         every tool call it made, in order
threads/            the chats, one file each
sessions/           the running state of a conversation

Not in this zip, on purpose: the secrets themselves (vault.enc, vault.key), the app's own
password (server_token), the key that signs notifications (push-vapid.json), the settings
file (it can hold an API key in the clear), the logs, and workspace/ (your own files).
"""


def stamp() -> str:
    """The timestamp a file name carries: sortable, and legal on every filesystem."""
    return datetime.now(UTC).strftime("%Y%m%d-%H%M%S")


def filename() -> str:
    return f"nanomuse-data-{stamp()}.zip"


def _regular_file(path: Path) -> bool:
    """A real file inside the data tree, never a symlink to something excluded."""
    try:
        return not path.is_symlink() and path.is_file()
    except OSError:
        return False


def _add_tree(z: zipfile.ZipFile, root: Path, under: str) -> None:
    if root.is_symlink():
        return
    for path in sorted(root.rglob("*")):
        if _regular_file(path) and path.stat().st_size <= MAX_FILE_BYTES:
            z.write(path, f"{under}/{path.relative_to(root).as_posix()}")


def agent_data_zip(data_dir: Path) -> bytes:
    """The zip's bytes: every file of the agent's data that is there, and the README."""
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for name in FILES:
            path = data_dir / name
            if _regular_file(path) and path.stat().st_size <= MAX_FILE_BYTES:
                z.write(path, name)
        for folder in DIRS:
            root = data_dir / folder
            if not root.is_symlink() and root.is_dir():
                _add_tree(z, root, folder)
        z.writestr(
            "README.txt", README.format(when=datetime.now(UTC).isoformat(timespec="seconds"))
        )
    return out.getvalue()


__all__ = ["DIRS", "FILES", "MAX_FILE_BYTES", "README", "agent_data_zip", "filename"]
