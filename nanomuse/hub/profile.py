"""The agent's name and look, the same on every device of the account.

The relay keeps one profile per account (``/v1/me/profile``: the name, which face — the
dragon, an emoji on a colour, or one drawn in the avatar studio — and, for a drawn face,
its five stills as WebP) and tells the account's other devices over the hub when it
changes (``{"type": "profile", "rev": n, "device": id}``). Here:

* **pull** on sign-in, on start and on that frame — applied when the relay's ``rev`` is
  newer than the one last seen on this device; a drawn face's stills land in the workspace
  under ``avatar/sync-<hash>/<mood>.webp`` and the profile wears that id, so the web app
  shows it through the files API like any studio face;
* **push** (a moment after the change settles) when the look is changed on this device —
  Settings, the studio, a rename in the chat. The pictures ride along only when the face
  itself changed; a rename keeps them on the relay.

A device keeps its own folder for a face it drew: when the account's pictures are the ones
it already wears (the relay says so with ``face_id``, the hash of the idle still; older
relays are checked against the downloaded still), only the name is taken, so the clips the
studio made and ``face.json`` stay. The face's description and style travel too, so the
other devices show what it is and can redraw its poses with their own image model.

The same profile carries ``connectors`` (contract C3): which device of the account connected
which remote MCP server — its name, address (without a query string, where a key might
ride), how it signs in (``oauth`` / ``key`` / ``open``), whether it is on, when, and which
device holds it. The relay keeps every device's entries side by side and replaces only the
writer's own, so this runtime PUTs what *it* holds and reads back what the others hold;
Connections lists theirs as "Connected on <device> — sign in here to use it on this device".
A stdio command is not a service another device could sign in to, so it stays private.

Never a key, never a message, never a setting that could reach the network: the fields are
``name``, ``avatar``, ``emoji``, ``color``, ``style``, ``description``, the pictures and the
connector entries above. The phone does the same from
``io.github.nanomuse.cloud.ProfileSync`` and ``connectors.SharedConnectors``.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import shutil
import time
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from nanomuse.cloud import CloudError
from nanomuse.logger import logger

if TYPE_CHECKING:
    from nanomuse.hub.service import HubService

MOODS = ("idle", "working", "waiting", "happy", "error")
# the dragon, as the profile names it; "" is the emoji
DRAGON = "dragon"
RETIRED = {"panda", "sunny", "moss", "sky", "fox", "bolt", "plum"}
# how long after the last change the push goes out (Settings saves name and colour apart)
PUSH_DELAY_S = 1.5
# what a brand-new runtime wears: nothing to seed the relay with
DEFAULT_LOOK = ("nanoMuse", DRAGON)
# the relay caps the account's connector list at 64; this device keeps its share modest
MAX_CONNECTORS = 32
# the keys of one shared connector entry, as every client reads them
CONNECTOR_FIELDS = ("id", "label", "url", "auth", "device", "device_id", "enabled", "at")


class ProfileSync:
    def __init__(self, hub: HubService):
        self.hub = hub
        self._push_task: asyncio.Task[None] | None = None
        self._pull_task: asyncio.Task[bool] | None = None
        # set while a pulled profile is being applied, so the change does not push back
        self._applying = False

    # ------------------------------------------------------------------ what this device knows
    @property
    def _cloud(self) -> dict[str, Any]:
        return dict(self.hub.data.get("cloud") or {})

    @property
    def rev(self) -> int:
        """The relay profile's rev as last applied or written here."""
        return int(self._cloud.get("profile_rev") or 0)

    @property
    def pushed_face(self) -> str:
        """The face id whose pictures the relay has from (or for) this device."""
        return str(self._cloud.get("profile_face") or "")

    def _remember(self, rev: int, face: str | None = None) -> None:
        cloud = self._cloud
        cloud["profile_rev"] = rev
        if face is not None:
            cloud["profile_face"] = face
        self.hub.data["cloud"] = cloud
        self.hub._save()

    async def stop(self) -> None:
        """The runtime is stopping: a push waiting out its delay or a pull on the wire is
        cancelled and waited for, so the relay's HTTP client can close behind it."""
        tasks = [t for t in (self._push_task, self._pull_task) if t is not None and not t.done()]
        for t in tasks:
            t.cancel()
        if tasks:
            await asyncio.wait(tasks)

    def forget(self) -> None:
        """Signed out: the next account starts from its own profile."""
        cloud = self._cloud
        cloud.pop("profile_rev", None)
        cloud.pop("profile_face", None)
        cloud.pop("shared_connectors", None)
        cloud.pop("connector_at", None)
        self.hub.data["cloud"] = cloud

    def _look(self) -> tuple[str, str]:
        p = self.hub.svc.profile
        return (p.name, p.avatar)

    # ------------------------------------------------------------------ the frame, the pull
    def on_frame(self, frame: dict[str, Any]) -> None:
        rev = int(frame.get("rev") or 0)
        if str(frame.get("device") or "") == self.hub.device_id:
            # our own write coming back
            if rev > self.rev:
                self._remember(rev)
            return
        self.pull_soon()

    def pull_soon(self) -> None:
        if self._pull_task is not None and not self._pull_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._pull_task = loop.create_task(self._pull_quietly(), name="profile-pull")

    async def _pull_quietly(self) -> bool:
        try:
            return await self.pull()
        except CloudError as exc:
            logger.debug("profile pull: {}", exc.describe())
        except Exception:  # noqa: BLE001
            logger.exception("profile pull")
        return False

    async def pull(self) -> bool:
        """Fetch the relay's profile and wear it when it is newer. True when something changed."""
        if not self.hub.signed_in:
            return False
        self.hub.cloud.api_key = self.hub._key()
        light = await self.hub.cloud.profile(with_face=False)
        rev = int(light.get("rev") or 0)
        # the other devices' connections come with every read, whatever the rev did
        self._absorb_connectors(light)
        if rev == 0:
            # the relay has nothing for this account yet: what this device wears seeds it,
            # unless it is the plain default (nothing worth telling the other devices)
            if self._look() != DEFAULT_LOOK or self.my_connectors():
                await self.push()
            return False
        if rev <= self.rev:
            return False
        avatar = str(light.get("avatar") or "")
        if not avatar and not str(light.get("name") or "").strip():
            # a device wrote only its connectors and nobody has set a look yet: nothing to wear
            self._remember(rev)
            return False
        patch: dict[str, Any] = {"name": str(light.get("name") or "").strip() or "nanoMuse"}
        face_id: str | None = None
        if avatar == DRAGON:
            patch["avatar"] = DRAGON
        elif avatar == "emoji":
            patch["avatar"] = ""
            patch["emoji"] = str(light.get("emoji") or "") or "✨"
            patch["color"] = str(light.get("color") or "") or "#0064d4"
        elif avatar == "face" and light.get("has_face"):
            worn = self._worn_face_id()
            if worn and worn[1] == str(light.get("face_id") or ""):
                # the pictures this device already wears (it drew them, or pulled them before)
                face_id = worn[0]
            else:
                full = await self.hub.cloud.profile(with_face=True)
                face_id = await asyncio.to_thread(self._store_face, full.get("face") or {}, light)
            if face_id:
                patch["avatar"] = face_id
        # the rev is remembered before the look is worn, so anyone who sees the new look
        # (the web app, a test) also sees the rev it came with; a failed apply forgets it
        before = (self.rev, self.pushed_face)
        self._remember(
            rev, face_id if face_id else (patch.get("avatar") if "avatar" in patch else None)
        )
        self._applying = True
        try:
            self.hub.svc.update_profile(patch)
        except Exception:
            self._remember(*before)
            raise
        finally:
            self._applying = False
        logger.info(
            "profile: now wearing rev {} from the account ({})",
            rev,
            patch.get("avatar", "name only"),
        )
        return True

    @staticmethod
    def face_hash(idle: bytes) -> str:
        """How a face is told apart across devices: the hash of its idle still (the relay
        reports the same as ``face_id``)."""
        return hashlib.sha1(idle).hexdigest()[:12]

    def _worn_face_id(self) -> tuple[str, str] | None:
        """(folder, hash) of the drawn face the profile wears, when its idle still is on disk."""
        face = self.hub.svc.profile.avatar
        if not face or face == DRAGON or face in RETIRED:
            return None
        path = self.hub.svc.workspace() / "avatar" / face / "idle.webp"
        try:
            return face, self.face_hash(path.read_bytes())
        except OSError:
            return None

    def _store_face(self, face: dict[str, Any], light: dict[str, Any]) -> str | None:
        """The stills of a pulled face into the workspace; the id is a hash of the idle still
        so the same face from two devices lands in one folder. The face this device already
        wears, when the pictures are the same, is kept under its own name."""
        idle = face.get("idle")
        if not isinstance(idle, str) or not idle:
            return None
        try:
            idle_bytes = base64.b64decode(idle)
        except ValueError:
            return None
        digest = self.face_hash(idle_bytes)
        worn = self._worn_face_id()
        if worn and worn[1] == digest:
            return worn[0]
        face_id = "sync-" + digest
        root = self.hub.svc.workspace() / "avatar"
        folder = root / face_id
        folder.mkdir(parents=True, exist_ok=True)
        for mood in MOODS:
            pic = face.get(mood)
            raw = idle_bytes
            if isinstance(pic, str) and pic:
                with contextlib.suppress(ValueError):
                    raw = base64.b64decode(pic)
            (folder / f"{mood}.webp").write_bytes(raw)
        # what the face is, for the studio screen here (and a redraw of its poses)
        info = {
            "description": str(light.get("description") or ""),
            "style": str(light.get("style") or ""),
            "model": "",
            "clip_model": "",
            "created": int(time.time()),
            "from": "account",
        }
        with contextlib.suppress(OSError):
            (folder / "face.json").write_text(
                json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        # older pulled faces are not worn any more
        with contextlib.suppress(OSError):
            for other in root.iterdir():
                if other.is_dir() and other.name.startswith("sync-") and other.name != face_id:
                    shutil.rmtree(other, ignore_errors=True)
        return face_id

    # ------------------------------------------------------------------ the connectors
    @staticmethod
    def public_url(url: str) -> str:
        """The address of a server as the account may see it: scheme, host, port and path.
        The user info (``user:secret@``), the query (``?key=``) and the fragment are
        where a credential rides, and none of them leaves this device."""
        try:
            parts = urlsplit(url.strip())
        except ValueError:
            return ""
        if not parts.scheme or not parts.hostname:
            return ""
        host = parts.hostname
        if ":" in host:  # an IPv6 literal keeps its brackets
            host = f"[{host}]"
        if parts.port is not None:
            host = f"{host}:{parts.port}"
        return f"{parts.scheme}://{host}{parts.path}"

    def my_connectors(self) -> list[dict[str, Any]]:
        """This device's entries for the profile body: the remote MCP servers it connected —
        a name, an address without its user info, query and fragment, how it signs in.
        Nothing that opens anything; a stdio command stays here."""
        out: list[dict[str, Any]] = []
        device = self.hub.device_name[:80]
        device_id = self.hub.device_id[:80]
        stamps = dict(self._cloud.get("connector_at") or {})
        touched = False
        for server in self.hub.svc.settings.mcp.servers[:MAX_CONNECTORS]:
            url = self.public_url(server.url or "")
            if not url:
                continue
            at = stamps.get(server.name)
            if not isinstance(at, str) or not at:
                at = datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                stamps[server.name] = at
                touched = True
            raw = (server.url or "").strip()
            parts = urlsplit(raw)
            signs_in = bool(parts.query or parts.username or parts.password or "{{vault:" in raw)
            auth = "key" if signs_in or server.env else "open"
            out.append(
                {
                    "id": server.name[:64],
                    "label": server.name[:80],
                    "url": url[:256],
                    "auth": auth,
                    "device": device,
                    "device_id": device_id,
                    "enabled": True,
                    "at": at,
                }
            )
        if touched:
            cloud = self._cloud
            cloud["connector_at"] = {
                k: v for k, v in stamps.items() if any(e["id"] == k[:64] for e in out)
            }
            self.hub.data["cloud"] = cloud
            self.hub._save()
        return out

    @property
    def shared_connectors(self) -> list[dict[str, Any]]:
        """The other devices' connections as last read from the account (kept across
        restarts); never this device's own echo."""
        raw = self._cloud.get("shared_connectors") or []
        return [dict(e) for e in raw if isinstance(e, dict)]

    def _absorb_connectors(self, profile: dict[str, Any]) -> None:
        arr = profile.get("connectors")
        if not isinstance(arr, list):
            return
        theirs: list[dict[str, Any]] = []
        for raw in arr:
            if not isinstance(raw, dict):
                continue
            entry = {k: raw.get(k) for k in CONNECTOR_FIELDS}
            entry_id = str(entry.get("id") or "").strip()
            device_id = str(entry.get("device_id") or "")
            if not entry_id or not device_id or device_id == self.hub.device_id:
                continue
            entry["id"] = entry_id
            entry["label"] = str(entry.get("label") or "") or entry_id
            entry["url"] = str(entry.get("url") or "")
            entry["auth"] = str(entry.get("auth") or "open")
            entry["device"] = str(entry.get("device") or "") or device_id
            entry["enabled"] = entry.get("enabled") is not False
            entry["at"] = str(entry.get("at") or "")
            theirs.append(entry)
        theirs.sort(key=lambda e: e["at"], reverse=True)
        if theirs == self.shared_connectors:
            return
        cloud = self._cloud
        cloud["shared_connectors"] = theirs
        self.hub.data["cloud"] = cloud
        self.hub._save()
        logger.info("profile: {} connection(s) held by the account's other devices", len(theirs))
        # the Connections page refreshes its "held elsewhere" list without a reload
        connections = getattr(self.hub.svc, "connections", None)
        if connections is not None:
            with contextlib.suppress(Exception):
                connections._publish()

    # ------------------------------------------------------------------ the push
    def changed(self) -> None:
        """The look changed on this device (Settings, the studio, a rename): push in a moment."""
        if self._applying or not self.hub.signed_in:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._push_task is not None and not self._push_task.done():
            self._push_task.cancel()
        self._push_task = loop.create_task(self._push_later(), name="profile-push")

    async def _push_later(self) -> None:
        await asyncio.sleep(PUSH_DELAY_S)
        try:
            await self.push()
        except CloudError as exc:
            logger.warning("profile push: {}", exc.describe())
        except Exception:  # noqa: BLE001
            logger.exception("profile push")

    def _body(self) -> dict[str, Any]:
        p = self.hub.svc.profile
        body: dict[str, Any] = {
            "device": self.hub.device_id,
            "name": p.name,
            # which remote servers this device connected (contract C3); the relay keeps the
            # other devices' entries and replaces only ours; never a key
            "connectors": self.my_connectors(),
        }
        if p.avatar == DRAGON or p.avatar in RETIRED:
            body["avatar"] = DRAGON
        elif not p.avatar:
            body.update(avatar="emoji", emoji=p.emoji, color=p.color)
        else:
            body["avatar"] = "face"
            info = self._face_info(p.avatar)
            body["style"] = str(info.get("style") or "")[:20]
            body["description"] = str(info.get("description") or "")[:200]
            if p.avatar != self.pushed_face:
                face = self._read_face(p.avatar)
                if face is None:
                    # a face with no pictures on disk cannot be shared: the others keep theirs
                    body["avatar"] = DRAGON
                else:
                    body["face"] = face
        return body

    def _face_info(self, face_id: str) -> dict[str, Any]:
        """``face.json`` of a drawn face (the studio writes it with the poses), or {}."""
        path = self.hub.svc.workspace() / "avatar" / face_id / "face.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _read_face(self, face_id: str) -> dict[str, str] | None:
        folder = self.hub.svc.workspace() / "avatar" / face_id
        out: dict[str, str] = {}
        for mood in MOODS:
            path = folder / f"{mood}.webp"
            if path.is_file():
                with contextlib.suppress(OSError):
                    out[mood] = base64.b64encode(path.read_bytes()).decode()
        return out if "idle" in out else None

    async def push(self) -> None:
        """This device's name and look to the relay (last writer wins)."""
        if not self.hub.signed_in:
            return
        self.hub.cloud.api_key = self.hub._key()
        body = await asyncio.to_thread(self._body)
        out = await self.hub.cloud.put_profile(body)
        face = self.hub.svc.profile.avatar if body.get("avatar") == "face" else ""
        self._remember(int(out.get("rev") or 0), face)
        logger.info("profile: shared with the account as rev {}", out.get("rev"))
