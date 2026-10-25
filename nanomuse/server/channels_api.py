"""``/api/channels`` — the chat apps (Feishu, DingTalk, WeCom, Telegram) from the app.

One call wires it in: ``install_channels(app, svc, dep)`` adds the routes and hooks the
channel manager into the app's lifespan, so channels start with the server and stop with
it. The manager hangs off the service as ``svc.channels``.

Secrets are written here and never read back: a secret field comes back as
``has_value: true`` only.
"""

from __future__ import annotations

import base64
import contextlib
import io
from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from nanomuse.logger import logger

if TYPE_CHECKING:
    from nanomuse.channels.manager import ChannelManager
    from nanomuse.server.service import MuseService


class ChannelBody(BaseModel):
    enabled: bool | None = None
    settings: dict[str, Any] | None = None
    allow_from: list[str] | None = None
    group_policy: str | None = None


class TestBody(BaseModel):
    chat_id: str = ""


class DeliverBody(BaseModel):
    deliver: bool


class LoginBody(BaseModel):
    domain: str = Field("feishu", pattern="^(feishu|lark)$")


class DeliverTextBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=20_000)


def _qr_png(text: str) -> str:
    """A QR code of ``text`` as a base64 PNG, or ``""`` when qrcode/pillow are missing."""
    try:
        import qrcode

        image = qrcode.make(text, border=1)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return base64.b64encode(buffer.getvalue()).decode("ascii")
    except Exception as exc:  # noqa: BLE001
        logger.debug("channels: no QR image: {}", exc)
        return ""


def install_channels(app: FastAPI, svc: MuseService, dep: list[Any]) -> ChannelManager:
    # imported here: the manager uses the service's event helpers, and nanomuse.server
    # imports this module while it is still being set up
    from nanomuse.channels.manager import ChannelManager

    manager = ChannelManager(svc)
    svc.channels = manager  # type: ignore[attr-defined]
    first_new = len(app.router.routes)

    inner = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def lifespan(the_app: FastAPI) -> AsyncIterator[Any]:
        async with inner(the_app) as state:
            try:
                await manager.start()
            except Exception as exc:  # noqa: BLE001 — the chat apps must not keep the server down
                logger.warning("channels did not start: {}", exc)
            try:
                yield state
            finally:
                with contextlib.suppress(Exception):
                    await manager.stop()

    app.router.lifespan_context = lifespan

    def _manager_name(name: str) -> str:
        if name not in manager.types:
            raise HTTPException(status_code=404, detail=f"no channel called {name}")
        return name

    @app.get("/api/channels", dependencies=dep)
    async def channels_view() -> dict[str, Any]:
        return manager.view()

    @app.put("/api/channels/{name}", dependencies=dep)
    async def channels_update(name: str, body: ChannelBody) -> dict[str, Any]:
        _manager_name(name)
        try:
            return await manager.update(name, body.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/channels/reload", dependencies=dep)
    async def channels_reload() -> dict[str, Any]:
        return await manager.reload()

    @app.post("/api/channels/deliver", dependencies=dep)
    async def channels_deliver(body: DeliverTextBody) -> dict[str, Any]:
        """Send a line to every chat marked "deliver here" (what a check-in or a reminder
        would do); returns how many chats got it."""
        return {"sent": await manager.deliver(body.text)}

    @app.post("/api/channels/{name}/test", dependencies=dep)
    async def channels_test(name: str, body: TestBody | None = None) -> dict[str, Any]:
        _manager_name(name)
        try:
            return await manager.test(name, (body.chat_id if body else "") or "")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001 — the vendor's words
            raise HTTPException(status_code=502, detail=str(exc).splitlines()[0][:300]) from exc

    @app.post("/api/channels/pairing/{code}/approve", dependencies=dep)
    async def channels_approve(code: str) -> dict[str, Any]:
        entry = await manager.approve(code)
        if entry is None:
            raise HTTPException(
                status_code=404, detail="no pairing with that code (codes last 10 minutes)"
            )
        return {"ok": True, "paired": entry, **manager.view()}

    @app.post("/api/channels/pairing/{code}/deny", dependencies=dep)
    async def channels_deny(code: str) -> dict[str, Any]:
        info = await manager.deny(code)
        if info is None:
            raise HTTPException(status_code=404, detail="no pairing with that code")
        return {"ok": True, **manager.view()}

    @app.put("/api/channels/{name}/chats/{sender_id}", dependencies=dep)
    async def channels_chat_update(name: str, sender_id: str, body: DeliverBody) -> dict[str, Any]:
        _manager_name(name)
        entry = manager.set_deliver(name, sender_id, body.deliver)
        if entry is None:
            raise HTTPException(status_code=404, detail="that chat is not paired")
        return manager.view()

    @app.delete("/api/channels/{name}/chats/{sender_id}", dependencies=dep)
    async def channels_chat_remove(name: str, sender_id: str) -> dict[str, Any]:
        _manager_name(name)
        if not manager.remove_chat(name, sender_id):
            raise HTTPException(status_code=404, detail="that chat is not paired")
        return manager.view()

    @app.post("/api/channels/{name}/login", dependencies=dep)
    async def channels_login(name: str, body: LoginBody | None = None) -> dict[str, Any]:
        """Feishu's scan-to-create: a QR code to scan (or a link to open) and a device
        code to poll with. Nothing is stored until the poll says ``succeeded``."""
        _manager_name(name)
        try:
            session = await manager.login_begin(name, (body.domain if body else "feishu"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc).splitlines()[0][:300]) from exc
        return {**session, "qr_png": _qr_png(session["url"])}

    @app.get("/api/channels/{name}/login/{device_code}", dependencies=dep)
    async def channels_login_poll(name: str, device_code: str) -> dict[str, Any]:
        _manager_name(name)
        try:
            return await manager.login_poll(name, device_code)
        except KeyError:
            raise HTTPException(status_code=404, detail="that login is not under way") from None
        except Exception as exc:  # noqa: BLE001
            raise HTTPException(status_code=502, detail=str(exc).splitlines()[0][:300]) from exc

    # the web app's catch-all (``/{path:path}``) is registered before this call; routes match
    # in order, so ours move ahead of it
    routes = app.router.routes
    added = routes[first_new:]
    del routes[first_new:]
    routes[0:0] = added
    return manager


__all__ = ["install_channels"]
