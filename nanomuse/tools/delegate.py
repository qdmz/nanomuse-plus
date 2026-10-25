"""ask_muse — hand a heavy task to the owner's main agent （小默思）.

nanomuse is the fast front desk; some jobs belong with the main agent, who
operates the owner's servers, accounts and infrastructure (deployments,
real-system changes, deep research, media beyond these tools). This tool
posts the task to the chat bridge's /api/delegate endpoint and waits for the
main agent's answer to come back on the bridge's delegate channel.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

import httpx

from nanomuse.logger import logger
from nanomuse.schema import RiskLevel, ToolResult
from nanomuse.tools.base import BaseTool, CallAssessment


class AskMuse(BaseTool):
    name: str = "ask_muse"
    description: str = (
        "Delegate a task to 小默思， the owner's main AI agent who operates the "
        "owner's servers, accounts and infrastructure. Use it for jobs beyond "
        "your own tools: server and deployment work （运维）, changes to real "
        "systems, deep web research with browsing, or media and files you "
        "cannot produce yourself. Describe the task completely in one message, "
        "with all context included — the main agent cannot see this "
        "conversation. The call waits for the answer (up to about 4 minutes); "
        "relay the result to the user. On a timeout the task is still with "
        "the main agent — tell the user it is being worked on."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "task": {
                "type": "string",
                "description": "The full task for the main agent, with all needed context.",
            }
        },
        "required": ["task"],
    }

    slot: Any = None  # DelegateSettings
    vault: Any = None  # CredentialVault

    def _auth_value(self) -> str:
        raw = str(getattr(self.slot, "secret", "") or "")
        vault = self.vault
        if vault is not None and vault.has_placeholders(raw):
            raw = vault.resolve(raw, strict=False)
            if vault.has_placeholders(raw):
                return ""
        return raw

    def assess(self, args: dict[str, Any]) -> CallAssessment:
        endpoint = str(getattr(self.slot, "endpoint", "") or "")
        return CallAssessment(
            risk=RiskLevel.SAFE,
            egress=True,
            egress_configured=True,  # the owner configured this endpoint
            egress_target=urlparse(endpoint).hostname or "",
            summary=f"ask_muse — {str(args.get('task', ''))[:100]}",
        )

    async def execute(self, task: str = "", **_: Any) -> ToolResult:
        task = (task or "").strip()
        if not task:
            return ToolResult.fail("empty task")
        endpoint = str(getattr(self.slot, "endpoint", "") or "").strip().rstrip("/")
        if not endpoint:
            return ToolResult.fail("the delegate endpoint is not configured")
        wait_s = float(getattr(self.slot, "timeout", 240.0) or 240.0)
        auth = self._auth_value()
        headers = {"Authorization": f"Bearer {auth}"} if auth else {}
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(wait_s + 30.0, connect=15.0)
            ) as http:
                r = await http.post(
                    f"{endpoint}/api/delegate",
                    json={"task": task, "wait_s": int(wait_s)},
                    headers=headers,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("ask_muse failed: {}", exc)
            return ToolResult.fail(f"could not reach the main agent's bridge: {exc}")
        if r.status_code >= 400:
            return ToolResult.fail(f"the bridge answered {r.status_code}: {r.text[:200]}")
        try:
            data = r.json()
        except Exception:  # noqa: BLE001
            return ToolResult.fail(f"the bridge returned an unreadable answer: {r.text[:200]}")
        if data.get("ok") and data.get("answer"):
            return ToolResult(output=f"小默思回复：\n{data['answer']}")
        return ToolResult(
            output=(
                "小默思已经在处理这个任务了，但超过了等待时间还没回完。"
                "请告诉用户：任务已转交小默思，他完成后会回复；也可以让用户直接找小默思跟进。"
            )
        )
