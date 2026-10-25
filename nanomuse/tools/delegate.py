"""ask_muse — hand a heavy task to the owner's main agent （小默思）.

nanomuse is the fast front desk; some jobs belong with the main agent, who
operates the owner's servers, accounts and infrastructure (deployments,
real-system changes, deep research, media beyond these tools). This tool
posts the task to the chat bridge's /api/delegate endpoint and waits for the
main agent's answer to come back on the bridge's delegate channel.
"""

from __future__ import annotations

import re
from pathlib import Path
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
        "relay the result to the user. If the task produces a file (a video, "
        "an image, a document), ask the main agent to attach it to the reply: "
        "returned files are downloaded automatically and their local paths "
        "are listed in the result — send those files to the user. On a "
        "timeout the task is still with the main agent — tell the user it "
        "is being worked on."
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
    workspace: Path = Path(".")

    _MEDIA_URL = re.compile(
        r"https?://[^\s\"'）)]+\.(?:mp4|mov|webm|mkv|png|jpe?g|gif|mp3|wav|pdf|zip)",
        re.IGNORECASE,
    )

    async def _save_files(
        self, endpoint: str, headers: dict[str, str], data: dict[str, Any]
    ) -> list[str]:
        """Bring the main agent's files home: bridge attachments by fid, plus
        any direct media links in the answer text. Best-effort per file."""
        jobs: list[tuple[str, str]] = []  # (download url, filename)
        for att in data.get("attachments") or []:
            fid = str(att.get("fid") or "")
            name = str(att.get("name") or "file")
            if fid:
                jobs.append((f"{endpoint}/api/agent/attachment?fid={fid}", name))
        for url in self._MEDIA_URL.findall(str(data.get("answer") or ""))[:3]:
            jobs.append((url, urlparse(url).path.rsplit("/", 1)[-1] or "file"))
        if not jobs:
            return []
        out_dir = self.workspace / "delegate-files"
        out_dir.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=15.0), follow_redirects=True
        ) as http:
            for url, name in jobs[:3]:
                safe = re.sub(r"[^A-Za-z0-9._\-]", "_", name)[:80] or "file"
                try:
                    r = await http.get(url, headers=headers)
                    if r.status_code >= 400 or not r.content:
                        continue
                    dest = out_dir / safe
                    dest.write_bytes(r.content)
                    saved.append(str(dest))
                    logger.info("ask_muse: saved returned file {}", dest)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("ask_muse: file download failed ({}): {}", url, exc)
        return saved

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
            out = f"小默思回复：\n{data['answer']}"
            saved = await self._save_files(endpoint, headers, data)
            if saved:
                out += (
                    "\n\n小默思回传的文件已下载到本地（请把这些文件发给用户）:\n"
                    + "\n".join(saved)
                )
            return ToolResult(output=out)
        return ToolResult(
            output=(
                "小默思已经在处理这个任务了，但超过了等待时间还没回完。"
                "请告诉用户：任务已转交小默思，他完成后会回复；也可以让用户直接找小默思跟进。"
            )
        )
