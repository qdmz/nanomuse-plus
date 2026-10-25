"""Media tools: pictures and clips from the owner's configured providers.

Local patch (not upstream): upstream v1.0.6 resolves the ``[image]`` / ``[video]``
slots for the avatar studio and the client apps, but the server-side chat agent
has no tool that uses them, so it improvises (browser, third-party websites)
when asked to draw. These two tools close that gap: the agent can call the
configured media host directly, in the OpenAI shape (``/images/generations``
for pictures, ``/videos`` create + poll for clips) that Agnes and other
OpenAI-compatible media providers speak. The destination and the key come from
the owner's settings — the model writes the prompt, never the address — and
the result is saved into the workspace, where the UI shows it as an artifact.
"""

from __future__ import annotations

import asyncio
import base64
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse

import httpx

from nanomuse.logger import logger
from nanomuse.schema import RiskLevel, ToolResult
from nanomuse.tools.base import BaseTool, CallAssessment

VIDEO_POLL_SECONDS = 5.0
VIDEO_WAIT_LIMIT = 280.0


class _MediaTool(BaseTool):
    """Endpoint resolution shared by the two media tools (the avatar studio's
    contract: the slot when it is set — its host, its key, the chat model's key
    when the host is the same — else the chat host with its named media model)."""

    slot: Any = None  # MediaSettings for this kind
    llm: Any = None  # LLMSettings (fallback host / key / media models)
    vault: Any = None  # CredentialVault
    workspace: Path = Path(".")

    kind: str = "image"

    def _key(self, raw: Any) -> str:
        if raw is None:
            return ""
        if hasattr(raw, "get_secret_value"):
            raw = raw.get_secret_value()
        raw = str(raw)
        vault = self.vault
        if vault is not None and vault.has_placeholders(raw):
            raw = vault.resolve(raw, strict=False)
            if vault.has_placeholders(raw):
                return ""
        return raw

    def _endpoint(self) -> tuple[str, str, str]:
        llm = self.llm
        chat_base = (getattr(llm, "endpoint", "") or "").rstrip("/")
        chat_key = self._key(getattr(llm, "api_key", ""))
        base, key = chat_base, chat_key
        model = (getattr(llm, f"{self.kind}_model", "") or "").strip()
        slot = self.slot
        if slot is not None and getattr(slot, "configured", False):
            base = (getattr(slot, "endpoint", None) or chat_base).rstrip("/")
            key = self._key(getattr(slot, "api_key", "")) or (
                chat_key if base == chat_base else ""
            )
            model = (getattr(slot, "model", "") or "").strip() or model
        return base, key, model

    def _assess(self, args: dict[str, Any]) -> CallAssessment:
        base, _key, model = self._endpoint()
        host = urlparse(base).hostname or ""
        prompt = str(args.get("prompt", ""))[:120]
        return CallAssessment(
            risk=RiskLevel.SAFE,
            egress=True,
            egress_target=host,
            egress_configured=True,  # the owner picked this provider; the model cannot redirect it
            summary=f"generate_{self.kind}: {model} @ {host} — {prompt}",
        )

    def _unconfigured(self) -> ToolResult | None:
        base, _key, model = self._endpoint()
        if base and model:
            return None
        return ToolResult.fail(
            f"no {self.kind} model is configured (set the [{self.kind}] slot in the "
            f"settings, or the chat model's {self.kind}_model)"
        )


class GenerateImage(_MediaTool):
    name: str = "generate_image"
    description: str = (
        "Generate a picture from a text description with the configured image model, "
        "and save it into the workspace, where it is shown to the user. Use this "
        "whenever the user asks for an image to be drawn, generated, designed or "
        "created — never use the browser or a website for that instead."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "What the picture shows, in detail.",
            },
            "size": {
                "type": "string",
                "description": "Provider size, e.g. '1K', '2K' or '1024x1024'. Omit for the provider default.",
            },
            "ratio": {
                "type": "string",
                "description": "Aspect ratio, e.g. '1:1', '16:9', '9:16'. Omit for the provider default.",
            },
        },
        "required": ["prompt"],
    }
    risk: RiskLevel = RiskLevel.SAFE
    kind: str = "image"

    def assess(self, args: dict[str, Any]) -> CallAssessment:
        return self._assess(args)

    async def execute(
        self, prompt: str = "", size: str = "", ratio: str = "", **_: Any
    ) -> ToolResult:
        if not prompt.strip():
            return ToolResult.fail("empty prompt")
        if (err := self._unconfigured()) is not None:
            return err
        base, key, model = self._endpoint()
        body: dict[str, Any] = {
            "model": model,
            "prompt": prompt,
            "n": 1,
            "response_format": "url",
        }
        if size:
            body["size"] = size
        if ratio:
            body["ratio"] = ratio
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(240.0, connect=30.0), follow_redirects=True
            ) as http:
                r = await http.post(
                    f"{base}/images/generations", json=body, headers=headers
                )
                if r.status_code >= 400:
                    return ToolResult.fail(
                        f"the image provider answered {r.status_code}: {r.text[:300]}"
                    )
                data = r.json()
                items = data.get("data") or []
                item = items[0] if items else {}
                if item.get("b64_json"):
                    raw = base64.b64decode(item["b64_json"])
                    ext = ".png"
                elif item.get("url"):
                    got = await http.get(item["url"])
                    if got.status_code >= 400:
                        return ToolResult.fail(
                            f"could not download the generated picture ({got.status_code})"
                        )
                    raw = got.content
                    ctype = got.headers.get("content-type", "")
                    ext = ".jpg" if "jpeg" in ctype or "jpg" in ctype else ".png"
                else:
                    return ToolResult.fail(
                        f"the image provider returned no picture: {r.text[:300]}"
                    )
        except Exception as exc:  # noqa: BLE001
            logger.warning("generate_image failed: {}", exc)
            return ToolResult.fail(f"image generation failed: {exc}")
        out_dir = self.workspace / "images"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}{ext}"
        path.write_bytes(raw)
        return ToolResult(
            output=(
                f"Generated an image with {model} and saved it to "
                f"images/{path.name} ({max(1, len(raw) // 1024)} KB). "
                "It is shown to the user as an artifact."
            ),
            images=[str(path)],
            system=str(path),
        )


def _find_id(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("video_id", "task_id", "id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
        for value in payload.values():
            found = _find_id(value)
            if found:
                return found
    return ""


def _find_url(payload: Any) -> str:
    if isinstance(payload, dict):
        for key in ("video_url", "url", "download_url", "output_url"):
            value = payload.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
        for value in payload.values():
            found = _find_url(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_url(value)
            if found:
                return found
    return ""


def _find_status(payload: Any) -> str:
    if isinstance(payload, dict):
        value = payload.get("status")
        if isinstance(value, str) and value:
            return value.lower()
        for nested in payload.values():
            found = _find_status(nested)
            if found:
                return found
    return ""


class GenerateVideo(_MediaTool):
    name: str = "generate_video"
    description: str = (
        "Generate a short video clip from a text description with the configured "
        "video model, and save it into the workspace, where it is shown to the "
        "user. The provider renders asynchronously, so this waits until the clip "
        "is ready — usually one to a few minutes. Use this whenever the user asks "
        "for a video to be made — never use the browser or a website for that instead."
    )
    parameters: dict[str, Any] = {
        "type": "object",
        "properties": {
            "prompt": {
                "type": "string",
                "description": "What the clip shows, in detail.",
            },
            "duration": {
                "type": "number",
                "description": "Clip length in seconds (4–12). Omit for the provider default.",
            },
            "ratio": {
                "type": "string",
                "description": "Aspect ratio: '16:9', '9:16', '1:1', '4:3', '3:4' or '21:9'. Omit for the provider default.",
            },
        },
        "required": ["prompt"],
    }
    risk: RiskLevel = RiskLevel.SAFE
    kind: str = "video"

    def assess(self, args: dict[str, Any]) -> CallAssessment:
        return self._assess(args)

    async def execute(
        self,
        prompt: str = "",
        duration: float | None = None,
        ratio: str = "",
        **_: Any,
    ) -> ToolResult:
        if not prompt.strip():
            return ToolResult.fail("empty prompt")
        if (err := self._unconfigured()) is not None:
            return err
        base, key, model = self._endpoint()
        # Agnes's dialect of the OpenAI videos shape (docs: agnes-video-2.5):
        # `mode` is required, the length is `seconds` as a string, the frame
        # shape is `aspect_ratio` — and results are polled off the host root.
        agnes = "agnes-ai.com" in base
        body: dict[str, Any] = {"model": model, "prompt": prompt, "n": 1}
        if agnes:
            body["mode"] = "text"
            if duration:
                body["seconds"] = str(int(min(12, max(4, duration))))
            if ratio:
                body["aspect_ratio"] = ratio
        else:
            if duration:
                body["duration"] = duration
            if ratio:
                body["ratio"] = ratio
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(300.0, connect=30.0), follow_redirects=True
            ) as http:
                # a full render queue (503) or a rate limit (429) is a wait, not a
                # failure: hand the task in again after a pause, a few times over
                r = await http.post(f"{base}/videos", json=body, headers=headers)
                for _ in range(4):
                    if r.status_code not in (429, 503):
                        break
                    logger.info(
                        "video provider busy ({}); submitting again in 20s",
                        r.status_code,
                    )
                    await asyncio.sleep(20.0)
                    r = await http.post(f"{base}/videos", json=body, headers=headers)
                if r.status_code >= 400:
                    return ToolResult.fail(
                        f"the video provider answered {r.status_code}: {r.text[:300]}"
                    )
                payload = r.json()
                url = _find_url(payload)
                task = _find_id(payload)
                deadline = time.monotonic() + VIDEO_WAIT_LIMIT
                while not url:
                    status = _find_status(payload)
                    if status in ("failed", "error", "cancelled", "canceled"):
                        return ToolResult.fail(
                            f"the video task failed at the provider: {r.text[:300]}"
                        )
                    if not task:
                        return ToolResult.fail(
                            f"the video provider returned no task id: {r.text[:300]}"
                        )
                    if time.monotonic() > deadline:
                        return ToolResult.fail(
                            f"the video task {task} is still '{status or 'running'}' after "
                            f"{int(VIDEO_WAIT_LIMIT)}s; it may finish at the provider later — "
                            "tell the user it is still rendering"
                        )
                    await asyncio.sleep(VIDEO_POLL_SECONDS)
                    if agnes:
                        root = base[:-3] if base.endswith("/v1") else base
                        poll_url = (
                            f"{root}/agnesapi?video_id={quote(task, safe='')}"
                            f"&model_name={quote(model, safe='')}"
                        )
                    else:
                        poll_url = f"{base}/videos/{task}"
                    poll = await http.get(poll_url, headers=headers)
                    if poll.status_code >= 400:
                        return ToolResult.fail(
                            f"polling the video task answered {poll.status_code}: {poll.text[:300]}"
                        )
                    payload = poll.json()
                    url = _find_url(payload)
                got = await http.get(url)
                if got.status_code >= 400:
                    return ToolResult.fail(
                        f"could not download the generated clip ({got.status_code})"
                    )
                raw = got.content
        except Exception as exc:  # noqa: BLE001
            logger.warning("generate_video failed: {}", exc)
            return ToolResult.fail(f"video generation failed: {exc}")
        out_dir = self.workspace / "videos"
        out_dir.mkdir(parents=True, exist_ok=True)
        path = out_dir / f"{time.strftime('%Y%m%d-%H%M%S')}.mp4"
        path.write_bytes(raw)
        return ToolResult(
            output=(
                f"Generated a video with {model} and saved it to "
                f"videos/{path.name} ({max(1, len(raw) // 1024)} KB). "
                "It is shown to the user as an artifact."
            ),
            system=str(path),
        )


__all__ = ["GenerateImage", "GenerateVideo"]
