"""Custom OpenAI-compatible endpoints, several at once (#294).

The four model slots (``[llm]``, ``[gui]``, ``[image]``, ``[video]``) each hold one provider,
one address and one key. Before this module the web console offered a single *Other
OpenAI-compatible endpoint* row, so a second gateway could only replace the first. Now the
app keeps a list of endpoints in ``<data_dir>/app-settings.json`` under ``endpoints``: each
one has a stable id, a name the person typed (the host name by default), its base URL, its
key as a vault reference, and the models it serves (the endpoint's own ``/models`` when it
answers, else the ids the person typed). A slot that should use one is set from it
(``PUT /api/connections/llm {"endpoint": id, "model": ...}`` and the same for ``gui``,
``image`` and ``video``), so the console never handles a vault name.

On the first start with this module the chat slot is migrated: when it points at a host the
catalogue does not list, that host becomes endpoint number one, its key copied to the
endpoint's own vault entry, and the slot keeps working as it did, without a question asked.

Nothing here is shown to the model, and no key ever leaves the vault: the list carries
``{{vault:ENDPOINT_<ID>_API_KEY}}`` references and the view says only where a key is.
"""

from __future__ import annotations

import asyncio
import re
import secrets
import time
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

from nanomuse.cloud import model_url
from nanomuse.logger import logger

if TYPE_CHECKING:
    from nanomuse.server.connections import Connections

#: the slots an endpoint can serve, in the order of the Models contract
SLOTS: tuple[str, ...] = ("chat", "hands", "image", "video")
#: how many model ids a person may type for one endpoint, and how long each may be
MAX_TYPED_MODELS = 40
MAX_MODEL_ID = 120
#: the app settings key the list lives under
DATA_KEY = "endpoints"


def key_name(endpoint_id: str) -> str:
    """The vault entry an endpoint's key is kept under: ``ENDPOINT_<ID>_API_KEY``."""
    stem = re.sub(r"[^A-Z0-9]+", "_", endpoint_id.upper()).strip("_") or "ENDPOINT"
    return f"ENDPOINT_{stem}_API_KEY"


def typed_model_ids(value: Any) -> list[str]:
    """The model ids a person typed (a comma- or newline-separated field, or a list):
    trimmed, without duplicates, at most 40 of up to 120 characters each."""
    if isinstance(value, list):
        raw = [str(v) for v in value]
    elif isinstance(value, str):
        raw = re.split(r"[,\n]", value)
    else:
        raw = []
    out: list[str] = []
    for piece in raw:
        model_id = piece.strip()
        if not model_id or len(model_id) > MAX_MODEL_ID or model_id in out:
            continue
        out.append(model_id)
        if len(out) == MAX_TYPED_MODELS:
            break
    return out


def host_label(base_url: str) -> str:
    """The name an endpoint gets when the person types none: its host, with the port when
    the address names one (two local servers are then told apart)."""
    parts = urlsplit(base_url)
    host = parts.hostname or base_url
    return f"{host}:{parts.port}" if parts.port else host


class Endpoints:
    """The list behind *Add endpoint*: owned by :class:`Connections`, which holds the
    settings file, the vault and the slot setters."""

    def __init__(self, conn: Connections):
        self.conn = conn
        # add, update and refresh_models each read the list, ask the endpoint for its
        # models (a network call of several seconds) and write the list back; two of them
        # at once (the web console sends PUT on blur) would otherwise let the second write
        # undo the first. One at a time, on the one loop the service runs on.
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------ the list
    def rows(self) -> list[dict[str, Any]]:
        rows = self.conn.data.get(DATA_KEY)
        return (
            [r for r in rows if isinstance(r, dict) and r.get("id")]
            if isinstance(rows, list)
            else []
        )

    def get(self, endpoint_id: str) -> dict[str, Any] | None:
        for row in self.rows():
            if row.get("id") == endpoint_id:
                return row
        return None

    def _write(self, rows: list[dict[str, Any]]) -> None:
        self.conn.data[DATA_KEY] = rows
        self.conn._save()

    def _new_id(self) -> str:
        taken = {r["id"] for r in self.rows()}
        while True:
            candidate = "ep-" + secrets.token_hex(4)
            if candidate not in taken:
                return candidate

    @staticmethod
    def _same(a: str | None, b: str | None) -> bool:
        return bool(a) and bool(b) and str(a).rstrip("/") == str(b).rstrip("/")

    def id_for(self, base_url: str | None) -> str:
        """The endpoint whose address a slot points at; "" for any other host."""
        for row in self.rows():
            if self._same(row.get("base_url"), base_url):
                return str(row["id"])
        return ""

    def slots_of(self, endpoint_id: str) -> list[str]:
        """The slots that use the endpoint today, by address."""
        row = self.get(endpoint_id)
        if row is None:
            return []
        s = self.conn.settings
        used: list[str] = []
        if self._same(s.llm.endpoint, row["base_url"]):
            used.append("chat")
        if s.gui.model and self._same(s.gui.endpoint, row["base_url"]):
            used.append("hands")
        for slot in ("image", "video"):
            media = getattr(s, slot)
            if media.configured and self._same(media.endpoint, row["base_url"]):
                used.append(slot)
        return used

    def view(self) -> list[dict[str, Any]]:
        """What the app shows: never a key, only where it is."""
        return [
            {
                "id": row["id"],
                "label": row.get("label") or host_label(str(row.get("base_url") or "")),
                "base_url": row.get("base_url") or "",
                "key_source": self.conn._key_source(str(row.get("api_key") or "")),
                "models": [str(m) for m in row.get("models") or []],
                "slots": self.slots_of(str(row["id"])),
            }
            for row in self.rows()
        ]

    def slot_fields(self, endpoint_id: str) -> dict[str, str]:
        """What a slot takes from an endpoint: the plain OpenAI protocol, its address, its
        key by reference (or no key)."""
        row = self.get(endpoint_id)
        if row is None:
            raise ValueError(f"no endpoint {endpoint_id!r}; add it first")
        return {
            "provider": "openai",
            "base_url": str(row.get("base_url") or ""),
            "api_key": str(row.get("api_key") or ""),
        }

    # ------------------------------------------------------------------ migration
    def migrate(self) -> None:
        """Once, on the first start with the list: the chat slot set from the app on a host
        the catalogue does not list becomes endpoint number one. Its key is copied into the
        endpoint's own vault entry and the slot is pointed at that entry, so changing the
        key on the endpoint later changes it for the slot too. Nothing is asked."""
        if DATA_KEY in self.conn.data:
            return
        rows: list[dict[str, Any]] = []
        llm = dict(self.conn.data.get("llm") or {})
        base_url = str(llm.get("base_url") or "").rstrip("/")
        if base_url and self._unlisted(base_url) and (llm.get("provider") or "openai") == "openai":
            endpoint_id = self._new_id()
            row: dict[str, Any] = {
                "id": endpoint_id,
                "label": host_label(base_url),
                "base_url": base_url,
                "api_key": "",
                "models": [str(llm["model"])] if llm.get("model") else [],
                "at": int(time.time()),
            }
            ref = str(llm.get("api_key") or "")
            if ref:
                value = self.conn.vault.resolve(ref, strict=False)
                if value and not self.conn.vault.has_placeholders(value):
                    name = key_name(endpoint_id)
                    self.conn.vault.set(name, str(value))
                    row["api_key"] = "{{vault:" + name + "}}"
                    llm["api_key"] = row["api_key"]
                    self.conn.data["llm"] = llm
                    self.conn.settings.llm.api_key = row["api_key"]
                else:
                    # the slot names a vault entry nothing answers: the endpoint keeps the
                    # same reference, so both say "missing" and setting that entry mends both
                    row["api_key"] = ref
            rows.append(row)
            logger.info("the chat model's endpoint {} is now endpoint number one", base_url)
        self._write(rows)

    def _unlisted(self, base_url: str) -> bool:
        """A host that is neither a catalogue vendor, nor a preset of the app, nor the
        account's relay: the kind the custom row used to hold."""
        from nanomuse.llm import catalogue
        from nanomuse.server.connections import PROVIDERS

        if self._same(base_url, model_url(self.conn.settings.cloud.base_url)):
            return False
        if any(self._same(base_url, str(p.get("base_url") or "")) for p in PROVIDERS.values()):
            return False
        return catalogue.load().by_base_url(base_url) is None

    # ------------------------------------------------------------------ add / change / remove
    def _check_url(self, base_url: str) -> str:
        from nanomuse.server.connections import check_base_url

        url = check_base_url(base_url)
        if not re.match(r"^https?://[^/\s]+", url):
            raise ValueError("the address must start with http:// or https://")
        if self._same(url, model_url(self.conn.settings.cloud.base_url)):
            raise ValueError("that is nanoMuse Cloud; it is the account, not an endpoint")
        return url

    def _check_unique(self, url: str, except_id: str = "") -> None:
        """Two rows with one address would both claim the slots that point at it
        (:meth:`id_for` and :meth:`slots_of` match by address), so the second is refused."""
        for row in self.rows():
            if row.get("id") != except_id and self._same(row.get("base_url"), url):
                label = row.get("label") or host_label(str(row.get("base_url") or ""))
                raise ValueError(
                    f"that address is already the endpoint '{label}'; change that one instead"
                )

    async def _models_for(self, base_url: str, key: str, typed: list[str]) -> list[str]:
        """The ids the person typed first, then what the endpoint lists; a ValueError when
        there is neither (a slot cannot pick from an empty list)."""
        from nanomuse.server.connections import fetch_models

        listed, _error = await fetch_models(base_url, key)
        models = typed + [m for m in listed if m not in typed]
        if not models:
            raise ValueError(
                "the endpoint did not list its models (GET /models); type the ids of the "
                "models it serves, separated by commas"
            )
        return models

    async def add(self, body: dict[str, Any]) -> dict[str, Any]:
        """A new endpoint: ``base_url`` (required), ``label`` (the host when empty),
        ``api_key`` (into the vault; empty for a server that wants none), ``models`` (typed
        ids; the endpoint's own list is added after them). An address another endpoint
        already has is refused."""
        async with self._lock:
            return await self._add(body)

    async def _add(self, body: dict[str, Any]) -> dict[str, Any]:
        base_url = self._check_url(str(body.get("base_url") or ""))
        self._check_unique(base_url)
        key = str(body.get("api_key") or "").strip()
        if key and re.search(r"\s", key):
            raise ValueError("a key has no spaces in it")
        typed = typed_model_ids(body.get("models"))
        models = await self._models_for(base_url, key, typed)
        endpoint_id = self._new_id()
        row: dict[str, Any] = {
            "id": endpoint_id,
            "label": str(body.get("label") or "").strip()[:80] or host_label(base_url),
            "base_url": base_url,
            "api_key": "",
            "models": models,
            "at": int(time.time()),
        }
        if key:
            name = key_name(endpoint_id)
            self.conn.vault.set(name, key)
            row["api_key"] = "{{vault:" + name + "}}"
        self._write([*self.rows(), row])
        self.conn._publish()
        return self._one(endpoint_id)

    async def update(self, endpoint_id: str, body: dict[str, Any]) -> dict[str, Any]:
        """Rename, change the key or the address, or retype the models. A key or address
        that changed asks the endpoint for its models again (typed ids stay first); the
        slots that use the endpoint follow it."""
        async with self._lock:
            return await self._update(endpoint_id, body)

    async def _update(self, endpoint_id: str, body: dict[str, Any]) -> dict[str, Any]:
        row = self.get(endpoint_id)
        if row is None:
            raise ValueError(f"no endpoint {endpoint_id!r}")
        changed = dict(row)
        if body.get("label") is not None:
            changed["label"] = str(body["label"]).strip()[:80] or host_label(changed["base_url"])
        moved = False
        if body.get("base_url") is not None:
            url = self._check_url(str(body["base_url"]))
            moved = not self._same(url, changed["base_url"])
            if moved:
                self._check_unique(url, except_id=endpoint_id)
            changed["base_url"] = url
        api_key = body.get("api_key")
        rekeyed = False
        if api_key:
            key = str(api_key).strip()
            if re.search(r"\s", key):
                raise ValueError("a key has no spaces in it")
            name = key_name(endpoint_id)
            self.conn.vault.set(name, key)
            changed["api_key"] = "{{vault:" + name + "}}"
            rekeyed = True
        elif api_key == "":
            self.conn.vault.delete(key_name(endpoint_id))
            changed["api_key"] = ""
            rekeyed = True
        typed = typed_model_ids(body.get("models")) if body.get("models") is not None else []
        if typed or moved or rekeyed:
            key = self.conn.vault.resolve(changed["api_key"] or "", strict=False)
            if self.conn.vault.has_placeholders(key):
                key = ""
            kept = typed or [str(m) for m in changed.get("models") or []]
            try:
                changed["models"] = await self._models_for(changed["base_url"], key, kept)
            except ValueError:
                if not kept:
                    raise
                changed["models"] = kept
        used = self.slots_of(endpoint_id)
        self._write([changed if r.get("id") == endpoint_id else r for r in self.rows()])
        if moved or rekeyed:
            for slot in used:
                self._follow(slot, endpoint_id)
        self.conn._publish()
        return self._one(endpoint_id)

    def _follow(self, slot: str, endpoint_id: str) -> None:
        """A slot that used the endpoint takes its new address or key."""
        if slot == "chat":
            self.conn.set_llm({"endpoint": endpoint_id})
        elif slot == "hands":
            self.conn.set_gui({"endpoint": endpoint_id})
        else:
            self.conn.set_media(slot, {"endpoint": endpoint_id})

    def remove(self, endpoint_id: str) -> bool:
        """The endpoint, its key and the slots that pointed at it (back to automatic). The
        chat slot cannot be left empty, so an endpoint it uses is refused until another
        chat model is chosen."""
        row = self.get(endpoint_id)
        if row is None:
            return False
        used = self.slots_of(endpoint_id)
        if "chat" in used:
            raise ValueError(
                "the chat model answers through this endpoint; pick another chat model first"
            )
        for slot in used:
            if slot == "hands":
                self.conn.set_gui({"provider": "", "model": "", "base_url": "", "api_key": ""})
            else:
                self.conn.set_media(
                    slot, {"provider": "", "model": "", "base_url": "", "api_key": ""}
                )
        self.conn.vault.delete(key_name(endpoint_id))
        self._write([r for r in self.rows() if r.get("id") != endpoint_id])
        self.conn._publish()
        return True

    async def refresh_models(self, endpoint_id: str) -> dict[str, Any]:
        """Ask the endpoint for its models again; the list stays when it does not answer."""
        async with self._lock:
            row = self.get(endpoint_id)
            if row is None:
                raise ValueError(f"no endpoint {endpoint_id!r}")
            from nanomuse.server.connections import fetch_models

            key = self.conn.vault.resolve(str(row.get("api_key") or ""), strict=False)
            if self.conn.vault.has_placeholders(key):
                key = ""
            listed, error = await fetch_models(str(row["base_url"]), key)
            if listed:
                # the row as it is now: a rename that landed meanwhile is kept
                current = self.get(endpoint_id)
                if current is None:
                    raise ValueError(f"no endpoint {endpoint_id!r}")
                changed = {**current, "models": listed}
                self._write([changed if r.get("id") == endpoint_id else r for r in self.rows()])
                self.conn._publish()
        return {**self._one(endpoint_id), "source": "live" if listed else "kept", "error": error}

    def _one(self, endpoint_id: str) -> dict[str, Any]:
        for entry in self.view():
            if entry["id"] == endpoint_id:
                return entry
        raise ValueError(f"no endpoint {endpoint_id!r}")


__all__ = ["DATA_KEY", "SLOTS", "Endpoints", "host_label", "key_name", "typed_model_ids"]
