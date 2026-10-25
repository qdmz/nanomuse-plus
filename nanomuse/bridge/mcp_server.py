"""The hands as an MCP server: ``nanomuse mcp``.

A host that is not our runtime — nanoMuse on DeepSeek Harness (docs/harness.md) — gets
this computer's screen and hands the way it gets any other capability: as an MCP server
on stdio. The two tools are the runtime's own ``computer_screen`` and ``computer_act``
(:mod:`nanomuse.tools.computer`), with their descriptions and schemas, so a model that
knows one host knows the other; ``computer_task`` stays home, because the host's model
runs the loop itself. The connectors ``config.toml`` turns on — ``read_emails`` /
``send_email``, ``calendar``, ``contacts`` — come along (:func:`connector_tools`).

What the Sentinel does in the runtime, the bridge does at the model's level: a call the
runtime would ask the person about — Enter or a submit, a heavy shortcut, a click on words
from the sensitive list (password, pay, …) — is refused with the reason, unless the call
carries ``confirmed``. With ``NANOMUSE_MCP_CONFIRM`` in the server's environment (the
nanoMuse desktop sets it: one random secret per launch, shared with its plugin), only the
host can confirm: ``confirmed`` must be the ticket :func:`ticket` computes over the call's
arguments with that secret, which the plugin produces after the person said yes on the
permission card — a ``true`` the model wrote on its own is refused. Without the variable
(``nanomuse mcp`` added to some other host by hand) ``confirmed: true`` is taken at the
model's word, as before, and the host's own approval policy is the gate.

Compatible with mcp 1.x and 2.x, like the client in :mod:`nanomuse.tools.mcp_tools`.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import inspect
import json
import os
from pathlib import Path
from typing import Any

from nanomuse.computer.link import ComputerLink
from nanomuse.config import Settings
from nanomuse.schema import STEP_KEY, RiskLevel, ToolResult
from nanomuse.tools.base import BaseTool, with_step
from nanomuse.tools.computer import ComputerAct, ComputerScreen

SERVER_NAME = "nanomuse"
CONFIRMED = "confirmed"
CONFIRM_SECRET_ENV = "NANOMUSE_MCP_CONFIRM"

_MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp"}


def hands_tools(settings: Settings, link: ComputerLink | None = None) -> list[BaseTool]:
    """The screen and the hands of this computer, as the runtime builds them."""
    link = link or ComputerLink(settings.hands, shots_dir=settings.agent.workspace / "screenshots")
    return [ComputerScreen(link=link), ComputerAct(link=link, gui=settings.gui)]


def connector_tools(settings: Settings) -> list[BaseTool]:
    """The connectors ``config.toml`` turns on — the mailbox, the calendar feeds, the
    address book — built as :class:`nanomuse.app.NanoMuseApp` builds them, so a host
    gets ``read_emails`` / ``send_email`` / ``calendar`` / ``contacts`` next to the hands
    (the desktop's Connectors page lists them as connected once they show up here)."""
    from nanomuse.calendar.feeds import CalendarFeeds
    from nanomuse.contacts.book import ContactBook
    from nanomuse.tools.calendar_tool import Calendar
    from nanomuse.tools.contacts_tool import Contacts
    from nanomuse.tools.email_tool import ReadEmails, SendEmail
    from nanomuse.vault.vault import CredentialVault

    c = settings.connectors
    if not (c.email.enabled or c.calendar.enabled or c.contacts.enabled):
        return []
    vault = CredentialVault(settings.vault_file, settings.vault_key_file)
    book = ContactBook(
        c.contacts, vault=vault, own_file=settings.contacts_file, cache_file=settings.contacts_cache
    )
    tools: list[BaseTool] = []
    if c.email.enabled:
        tools.append(ReadEmails(settings=c.email, vault=vault))
        tools.append(SendEmail(settings=c.email, vault=vault, book=book))
    if c.calendar.enabled:
        feeds = CalendarFeeds(c.calendar, vault=vault, cache_file=settings.calendar_cache)
        tools.append(Calendar(feeds=feeds, workspace=settings.agent.workspace))
    if c.contacts.enabled:
        tools.append(Contacts(book=book))
    return tools


def confirm_secret() -> str:
    """The host's confirmation secret, or ``""`` when the model's own word is accepted."""
    return os.environ.get(CONFIRM_SECRET_ENV, "").strip()


def ticket(secret: str, args: dict[str, Any], signed_step: bool = True) -> str:
    """The confirmation ticket for one exact call: HMAC of its arguments under the secret.

    The plugin (``harness/dsh-nanomuse/src/cloud.ts`` ``confirmTicket``) and this server
    compute it the same way — sorted keys, compact JSON, the ``confirmed`` field left out —
    so a ticket confirms these arguments and no others. The ``step`` words are part of the
    arguments the plugin signs; ``signed_step=False`` is the ticket without them (the
    arguments the tool itself runs on), which :func:`_confirmed` accepts as well.
    """
    skip = (CONFIRMED,) if signed_step else (CONFIRMED, STEP_KEY)
    clean = {k: v for k, v in args.items() if k not in skip}
    body = json.dumps(clean, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hmac.new(secret.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]


def _confirmed(args: dict[str, Any]) -> bool:
    """Whether this call carries a confirmation the server accepts: the host's ticket over
    the arguments as the host saw them (``step`` included — what the plugin signs) or over
    the arguments the tool runs on (``step`` left out). Before 0.1.38 only the second was
    accepted, and the plugin signs the first, so every step the person agreed to on the
    permission card came back "Not done" when the model had written a ``step``."""
    given = args.get(CONFIRMED)
    secret = confirm_secret()
    if not secret:
        return given is True
    if not isinstance(given, str):
        return False
    return hmac.compare_digest(given, ticket(secret, args)) or hmac.compare_digest(
        given, ticket(secret, args, signed_step=False)
    )


def exposed_schema(tool: BaseTool) -> dict[str, Any]:
    """The tool's schema with the bridge's ``confirmed`` field on the ones that need it."""
    schema: dict[str, Any] = json.loads(json.dumps(with_step(tool.parameters)))
    if tool.risk != RiskLevel.SAFE:
        props = schema.setdefault("properties", {})
        if confirm_secret():
            props[CONFIRMED] = {
                "type": "string",
                "description": (
                    "A confirmation ticket the host adds once the person agreed to this exact "
                    "step on the permission card. Not something to write yourself: a refused "
                    "call is answered by asking the person, and the host does the rest."
                ),
            }
        else:
            props[CONFIRMED] = {
                "type": "boolean",
                "description": (
                    "Set only after the person agreed, in the conversation, to this exact step. "
                    "Needed for Enter or a submit, heavy shortcuts, and clicks on words from "
                    "the sensitive list; a refused call says which."
                ),
            }
    return schema


def exposed_description(tool: BaseTool) -> str:
    if tool.risk == RiskLevel.SAFE:
        return tool.description
    if confirm_secret():
        return (
            tool.description + " Steps the person must agree to first are refused with the "
            "reason; the host then asks them on the permission card and runs the step if "
            "they say yes."
        )
    return (
        tool.description + " Steps the person must agree to first are refused with the reason; "
        "ask, then call again with `confirmed: true`."
    )


REFUSED = "Not done: "


def gate(tool: BaseTool, args: dict[str, Any]) -> str | None:
    """Why this call is not run without the person's word, or ``None`` to run it."""
    if tool.risk == RiskLevel.SAFE or _confirmed(args):
        return None
    assessment = tool.assess(args)
    if assessment.risk in (RiskLevel.SAFE, RiskLevel.MODERATE):
        return None
    reasons = "; ".join(assessment.warnings) or "this step acts on the person's behalf"
    if confirm_secret():
        return (
            f"{REFUSED}{assessment.summary}: {reasons}. The person has to agree to this step "
            "on their permission card first; the host asks them."
        )
    return (
        f"{REFUSED}{assessment.summary}: {reasons}. Ask the person whether to go ahead, and "
        f"call again with `{CONFIRMED}: true` once they said yes."
    )


async def call(tool: BaseTool, args: dict[str, Any]) -> ToolResult:
    refused = gate(tool, args)
    if refused is not None:
        return ToolResult.fail(refused)
    clean = {k: v for k, v in args.items() if k not in (CONFIRMED, STEP_KEY)}
    try:
        return await tool.execute(**clean)
    except Exception as exc:  # noqa: BLE001 - the host gets a message, not a dead server
        # the kind of failure and its message, with the value of every URL query parameter
        # masked (a key rides there, and httpx quotes the full URL in a 4xx)
        from nanomuse.tools.mcp_tools import _QUERY_VALUE

        return ToolResult.fail(_QUERY_VALUE.sub(r"\1\2=***", f"{type(exc).__name__}: {exc}")[:2000])


def content_blocks(result: ToolResult) -> list[dict[str, Any]]:
    """The result as MCP content: the text, then each picture that exists."""
    blocks: list[dict[str, Any]] = [{"type": "text", "text": result.for_model()}]
    for path in result.images or []:
        p = Path(path)
        mime = _MIME.get(p.suffix.lower())
        if mime is None or not p.is_file():
            continue
        blocks.append(
            {"type": "image", "data": base64.b64encode(p.read_bytes()).decode(), "mimeType": mime}
        )
    return blocks


def tool_listing(tools: list[BaseTool]) -> list[dict[str, Any]]:
    return [
        {"name": t.name, "description": exposed_description(t), "inputSchema": exposed_schema(t)}
        for t in tools
    ]


async def serve(tools: list[BaseTool]) -> None:
    """Run the MCP server on stdio until the host closes it."""
    from mcp import types
    from mcp.server.lowlevel import Server
    from mcp.server.stdio import stdio_server

    by_name = {t.name: t for t in tools}
    instructions = (
        "This computer's screen and hands, from nanoMuse. Look first (computer_screen), act "
        "in small steps (computer_act), look again. Coordinates are pixels of the last "
        "screenshot."
    )

    def listing() -> list[Any]:
        return [types.Tool.model_validate(t) for t in tool_listing(tools)]

    async def run_tool(name: str, arguments: dict[str, Any] | None) -> tuple[list[Any], bool]:
        tool = by_name.get(name)
        if tool is None:
            result = ToolResult.fail(f"no tool named {name!r}")
        else:
            result = await call(tool, dict(arguments or {}))
        blocks = [
            types.ImageContent.model_validate(b)
            if b["type"] == "image"
            else types.TextContent.model_validate(b)
            for b in content_blocks(result)
        ]
        return blocks, not result.ok

    server: Any
    if "on_call_tool" in inspect.signature(Server.__init__).parameters:  # mcp >= 2

        async def on_list_tools(_ctx: Any, _params: Any) -> Any:
            return types.ListToolsResult(tools=listing())

        async def on_call_tool(_ctx: Any, params: Any) -> Any:
            blocks, is_error = await run_tool(params.name, params.arguments)
            return types.CallToolResult.model_validate({"content": blocks, "isError": is_error})

        server = Server(
            SERVER_NAME,
            instructions=instructions,
            on_list_tools=on_list_tools,
            on_call_tool=on_call_tool,
        )
    else:  # mcp 1.x: decorators
        server = Server(SERVER_NAME, instructions=instructions)

        @server.list_tools()
        async def _list() -> list[Any]:
            return listing()

        @server.call_tool()
        async def _call(name: str, arguments: dict[str, Any] | None) -> list[Any]:
            blocks, _ = await run_tool(name, arguments)
            return blocks

    async with stdio_server() as (read, write):
        await server.run(read, write, server.create_initialization_options())


__all__ = [
    "CONFIRMED",
    "SERVER_NAME",
    "call",
    "content_blocks",
    "exposed_description",
    "exposed_schema",
    "gate",
    "hands_tools",
    "serve",
    "tool_listing",
]
