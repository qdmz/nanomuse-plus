"""Where channel settings and pairings live.

Two small JSON files under ``<data_dir>/channels/``:

* ``settings.json`` — what was set in the app or by ``nanomuse channels login``: enabled,
  the fields, the allowlist, the group policy. Secrets are not in here: they go to the vault
  and the file keeps a ``{{vault:CHANNEL_FEISHU_APP_SECRET}}`` placeholder. ``[channels.<name>]``
  in ``config.toml`` is read as well; the app's values win per field.
* ``pairing.json`` — pending pairing codes and the people who were approved, per channel,
  with the "deliver here" switch of each.

Both are re-read when their file changed on disk, so ``nanomuse channels approve`` from a
terminal is seen by the running server without a restart. They sit in the data directory,
not the workspace: the agent's files tool can write the workspace, and who may talk to the
bot is not the agent's to decide.
"""

from __future__ import annotations

import json
import secrets
import time
import tomllib
from pathlib import Path
from typing import Any

from nanomuse.channels.base import Channel, Field
from nanomuse.logger import logger
from nanomuse.vault.vault import CredentialVault

GROUP_POLICIES = ("mention", "open")
PAIRING_TTL_S = 600
# no 0/O/1/I: a code is read out loud or typed from a phone screen
CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6


def new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def vault_name(channel: str, key: str) -> str:
    return f"CHANNEL_{channel}_{key}".upper()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text("utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("could not read {}: {}", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), "utf-8")
    try:
        tmp.chmod(0o600)
    except OSError:  # pragma: no cover - windows
        pass
    tmp.replace(path)


class _Watched:
    """A JSON file re-read when it changed on disk."""

    def __init__(self, path: Path):
        self.path = path
        self._stamp: float | None = None
        self._data: dict[str, Any] = {}

    def load(self) -> dict[str, Any]:
        try:
            stamp = self.path.stat().st_mtime
        except OSError:
            stamp = None
        if stamp != self._stamp or (stamp is None and self._data):
            self._data = _read_json(self.path) if stamp is not None else {}
            self._stamp = stamp
        return self._data

    def save(self, data: dict[str, Any]) -> None:
        _write_json(self.path, data)
        self._data = data
        try:
            self._stamp = self.path.stat().st_mtime
        except OSError:  # pragma: no cover
            self._stamp = None


# ----------------------------------------------------------------------------- settings
class ChannelConfig:
    """One channel's effective settings (config.toml layered with the app's file)."""

    def __init__(self, name: str) -> None:
        self.name = name
        self.enabled = False
        self.settings: dict[str, Any] = {}
        self.allow_from: list[str] = []
        self.group_policy = "mention"

    def apply(self, raw: dict[str, Any]) -> None:
        if "enabled" in raw and raw["enabled"] is not None:
            self.enabled = bool(raw["enabled"])
        if isinstance(raw.get("allow_from"), list):
            self.allow_from = [str(x).strip() for x in raw["allow_from"] if str(x).strip()]
        if raw.get("group_policy") in GROUP_POLICIES:
            self.group_policy = str(raw["group_policy"])
        nested = raw.get("settings")
        values: dict[str, Any] = nested if isinstance(nested, dict) else raw
        for key, value in values.items():
            if key in ("enabled", "allow_from", "group_policy", "settings"):
                continue
            if value is None:
                continue
            self.settings[key] = value if isinstance(value, bool) else str(value)


class ChannelSettingsStore:
    def __init__(self, data_dir: Path, vault: CredentialVault, config_file: str | None = None):
        self.dir = data_dir / "channels"
        self.vault = vault
        self.config_file = config_file
        self._file = _Watched(self.dir / "settings.json")

    # ------------------------------------------------------------------ reading
    def _from_toml(self) -> dict[str, Any]:
        """``[channels.<name>]`` tables of config.toml, ``${VAR}`` expanded like the rest."""
        path = Path(self.config_file) if self.config_file else None
        if path is None or not path.is_file():
            return {}
        try:
            with path.open("rb") as fh:
                raw = tomllib.load(fh)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            logger.warning("channels: could not read {}: {}", path, exc)
            return {}
        from nanomuse.config import _expand_env

        table = raw.get("channels")
        if not isinstance(table, dict):
            return {}
        return {str(k): _expand_env(v) for k, v in table.items() if isinstance(v, dict)}

    def config(self, name: str) -> ChannelConfig:
        cfg = ChannelConfig(name)
        toml = self._from_toml().get(name)
        if isinstance(toml, dict):
            cfg.apply(toml)
        app = self._file.load().get(name)
        if isinstance(app, dict):
            cfg.apply(app)
        return cfg

    def resolved(self, cfg: ChannelConfig) -> dict[str, Any]:
        """The settings with vault placeholders replaced — for the channel, never for a view."""
        return {k: self.vault.resolve(v, strict=False) for k, v in cfg.settings.items()}

    # ------------------------------------------------------------------ writing
    def update(
        self,
        channel: type[Channel],
        *,
        enabled: bool | None = None,
        values: dict[str, Any] | None = None,
        allow_from: list[str] | None = None,
        group_policy: str | None = None,
    ) -> ChannelConfig:
        """Save what changed. A secret field's value goes to the vault; ``""`` removes it;
        a field left out keeps its value."""
        data = dict(self._file.load())
        entry = dict(data.get(channel.name) or {})
        settings = dict(entry.get("settings") or {})
        fields: dict[str, Field] = {f.key: f for f in channel.fields}
        for key, value in (values or {}).items():
            spec = fields.get(key)
            if spec is None or value is None:
                continue
            if spec.kind == "secret":
                vname = vault_name(channel.name, key)
                text = str(value)
                if text == "":
                    self.vault.delete(vname)
                    settings.pop(key, None)
                else:
                    self.vault.set(vname, text)
                    settings[key] = "{{vault:" + vname + "}}"
            elif spec.kind == "bool":
                settings[key] = (
                    bool(value)
                    if not isinstance(value, str)
                    else value.lower()
                    in (
                        "1",
                        "true",
                        "yes",
                        "on",
                    )
                )
            elif spec.kind == "choice":
                text = str(value).strip()
                if text and text not in spec.choices:
                    raise ValueError(f"{key} must be one of {', '.join(spec.choices)}")
                settings[key] = text or spec.default
            else:
                settings[key] = str(value).strip()
        entry["settings"] = settings
        if enabled is not None:
            entry["enabled"] = bool(enabled)
        if allow_from is not None:
            entry["allow_from"] = [str(x).strip() for x in allow_from if str(x).strip()][:200]
        if group_policy is not None:
            if group_policy not in GROUP_POLICIES:
                raise ValueError(f"group_policy must be one of {', '.join(GROUP_POLICIES)}")
            entry["group_policy"] = group_policy
        data[channel.name] = entry
        self._file.save(data)
        return self.config(channel.name)

    def has_secret(self, cfg: ChannelConfig, key: str) -> bool:
        value = cfg.settings.get(key)
        if not value:
            return False
        if self.vault.has_placeholders(value):
            return bool(
                self.vault.resolve(value, strict=False)
            ) and not self.vault.has_placeholders(self.vault.resolve(value, strict=False))
        return True


# ----------------------------------------------------------------------------- pairing
class PairingStore:
    """Pending codes and approved people, per channel."""

    def __init__(self, data_dir: Path):
        self._file = _Watched(data_dir / "channels" / "pairing.json")

    def _data(self) -> dict[str, Any]:
        data = dict(self._file.load())
        approved = data.get("approved")
        pending = data.get("pending")
        data["approved"] = approved if isinstance(approved, dict) else {}
        data["pending"] = pending if isinstance(pending, dict) else {}
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self._file.save(data)

    @staticmethod
    def _gc(data: dict[str, Any]) -> None:
        now = time.time()
        pending = data["pending"]
        for code in [
            c for c, p in pending.items() if not isinstance(p, dict) or p.get("expires_at", 0) < now
        ]:
            pending.pop(code, None)

    # ------------------------------------------------------------------ approved
    def approved(self, channel: str) -> dict[str, dict[str, Any]]:
        entries = self._data()["approved"].get(channel)
        return dict(entries) if isinstance(entries, dict) else {}

    def is_approved(self, channel: str, sender_id: str) -> bool:
        return sender_id in self.approved(channel)

    def entry(self, channel: str, sender_id: str) -> dict[str, Any] | None:
        return self.approved(channel).get(sender_id)

    def all_approved(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for channel, entries in self._data()["approved"].items():
            if not isinstance(entries, dict):
                continue
            for sender_id, entry in entries.items():
                if isinstance(entry, dict):
                    out.append({"channel": channel, "sender_id": sender_id, **entry})
        return out

    def add(
        self, channel: str, sender_id: str, *, sender_name: str = "", chat_id: str = ""
    ) -> dict[str, Any]:
        data = self._data()
        entries = data["approved"].setdefault(channel, {})
        entry = dict(entries.get(sender_id) or {})
        entry.update(
            {
                "sender_name": sender_name or entry.get("sender_name", ""),
                "chat_id": chat_id or entry.get("chat_id", sender_id),
                "approved_at": entry.get("approved_at") or time.time(),
                "deliver": bool(entry.get("deliver", False)),
            }
        )
        entries[sender_id] = entry
        self._save(data)
        return entry

    def set_deliver(self, channel: str, sender_id: str, on: bool) -> dict[str, Any] | None:
        data = self._data()
        entry = (data["approved"].get(channel) or {}).get(sender_id)
        if not isinstance(entry, dict):
            return None
        entry["deliver"] = bool(on)
        self._save(data)
        return entry

    def touch(
        self, channel: str, sender_id: str, *, sender_name: str = "", chat_id: str = ""
    ) -> None:
        """Keep the name and the direct chat of an approved person current."""
        data = self._data()
        entry = (data["approved"].get(channel) or {}).get(sender_id)
        if not isinstance(entry, dict):
            return
        changed = False
        if sender_name and entry.get("sender_name") != sender_name:
            entry["sender_name"] = sender_name
            changed = True
        if chat_id and entry.get("chat_id") != chat_id:
            entry["chat_id"] = chat_id
            changed = True
        if changed:
            self._save(data)

    def remove(self, channel: str, sender_id: str) -> bool:
        data = self._data()
        entries = data["approved"].get(channel)
        if not isinstance(entries, dict) or sender_id not in entries:
            return False
        entries.pop(sender_id)
        self._save(data)
        return True

    def deliver_targets(self) -> list[tuple[str, str]]:
        """``(channel, chat_id)`` of every chat marked "deliver here"."""
        return [
            (e["channel"], str(e.get("chat_id") or e["sender_id"]))
            for e in self.all_approved()
            if e.get("deliver")
        ]

    # ------------------------------------------------------------------ pending
    def pending(self) -> list[dict[str, Any]]:
        data = self._data()
        self._gc(data)
        return [
            {"code": code, **info}
            for code, info in sorted(
                data["pending"].items(), key=lambda kv: kv[1].get("created_at", 0)
            )
        ]

    def request(
        self, channel: str, sender_id: str, *, sender_name: str = "", chat_id: str = ""
    ) -> str:
        """A code for this person — the same one while the last is still good."""
        data = self._data()
        self._gc(data)
        for code, info in data["pending"].items():
            if info.get("channel") == channel and info.get("sender_id") == sender_id:
                return code
        code = new_code()
        while code in data["pending"]:
            code = new_code()
        now = time.time()
        data["pending"][code] = {
            "channel": channel,
            "sender_id": sender_id,
            "sender_name": sender_name,
            "chat_id": chat_id or sender_id,
            "created_at": now,
            "expires_at": now + PAIRING_TTL_S,
        }
        self._save(data)
        return code

    def approve(self, code: str) -> dict[str, Any] | None:
        data = self._data()
        self._gc(data)
        info = data["pending"].pop(code.strip().upper(), None)
        if not isinstance(info, dict):
            return None
        self._save(data)
        entry = self.add(
            info["channel"],
            info["sender_id"],
            sender_name=str(info.get("sender_name") or ""),
            chat_id=str(info.get("chat_id") or ""),
        )
        return {"channel": info["channel"], "sender_id": info["sender_id"], **entry}

    def deny(self, code: str) -> dict[str, Any] | None:
        data = self._data()
        self._gc(data)
        info = data["pending"].pop(code.strip().upper(), None)
        if not isinstance(info, dict):
            return None
        self._save(data)
        return info


__all__ = [
    "CODE_ALPHABET",
    "CODE_LENGTH",
    "GROUP_POLICIES",
    "PAIRING_TTL_S",
    "ChannelConfig",
    "ChannelSettingsStore",
    "PairingStore",
    "new_code",
    "vault_name",
]
