"""Tencent Yuanbao （元宝派）, over the bot platform's WebSocket long connection.

Local patch (not upstream): the protocol is the one the Yuanbao bot platform
speaks — a sign-token handshake (HMAC-SHA256 over ``nonce + timestamp + appKey +
appSecret``) for a short-lived token, then a WebSocket to
``bot-wss.yuanbao.tencent.com`` whose frames are hand-rolled protobuf
(varint + length-delimited): an ``auth-bind`` first, ``inbound_message`` pushes
in (each answered with a push-ack), ``send_c2c_message`` / ``send_group_message``
out, a ``ping`` every 30 seconds. Text only in this round; picture / file
elements are reported as placeholders on the way in.

Pictures, videos and files go out the way the platform's own client sends
them: ``/api/resource/genUploadInfo`` (signed with the same token) returns a
temporary COS slot, the bytes are PUT to Tencent COS under a hand-signed
authorization header, and the message element points at the stored resource —
so media the Muse makes lands in the chat itself.

Setup: create a bot on the Yuanbao bot platform and copy its App ID (app_key)
and App Secret here. The same bot can hold only one connection — do not run
another client for it at the same time.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import mimetypes
import secrets
import struct
import time
import uuid
from contextlib import suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx

from nanomuse.channels.base import Channel, ChannelBus, Field, InboundMessage
from nanomuse.logger import logger

API_BASE = "https://bot.yuanbao.tencent.com"
WS_URL = "wss://bot-wss.yuanbao.tencent.com/wss/connection"
TEXT_LIMIT = 4000
DEVICE_NAME = "nanomuse-yb/1.0"


class YuanbaoError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# hand-rolled protobuf (varint + length-delimited), the platform's framing
# --------------------------------------------------------------------------
def _wv(out: bytearray, value: int) -> None:
    v = int(value)
    while True:
        x = v & 0x7F
        v >>= 7
        if v:
            out.append(x | 0x80)
        else:
            out.append(x)
            return


def _wf(out: bytearray, num: int, wire: int) -> None:
    _wv(out, (num << 3) | wire)


def _wstr(out: bytearray, num: int, s: str) -> None:
    if not s:
        return
    data = s.encode("utf-8")
    _wf(out, num, 2)
    _wv(out, len(data))
    out += data


def _wbytes(out: bytearray, num: int, data: bytes) -> None:
    if not data:
        return
    _wf(out, num, 2)
    _wv(out, len(data))
    out += data


def _wvar(out: bytearray, num: int, v: int) -> None:
    _wf(out, num, 0)
    _wv(out, v)


def _pv(buf: bytes, pos: int) -> tuple[int, int]:
    r = 0
    s = 0
    while pos < len(buf):
        x = buf[pos]
        pos += 1
        r |= (x & 0x7F) << s
        s += 7
        if not (x & 0x80):
            break
    return r, pos


def parse_fields(buf: bytes) -> list[tuple[int, Any]]:
    out: list[tuple[int, Any]] = []
    p = 0
    buf = bytes(buf)
    while p < len(buf):
        tag, p = _pv(buf, p)
        fn, wt = tag >> 3, tag & 7
        if wt == 2:
            ln, p = _pv(buf, p)
            out.append((fn, buf[p : p + ln]))
            p += ln
        elif wt == 0:
            v, p = _pv(buf, p)
            out.append((fn, v))
        else:
            break
    return out


def field_str(fields: list[tuple[int, Any]], num: int) -> str:
    for fn, val in fields:
        if fn == num:
            return val.decode("utf-8", "replace") if isinstance(val, bytes) else str(val)
    return ""


def field_bytes(fields: list[tuple[int, Any]], num: int) -> bytes:
    for fn, val in fields:
        if fn == num and isinstance(val, bytes):
            return val
    return b""


class _Framer:
    """ConnMsg envelopes: field 1 = head, field 2 = data."""

    def __init__(self) -> None:
        self._seq = 0

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def msg(
        self,
        cmd_type: int,
        cmd: str,
        msg_id: str,
        module: str,
        data: bytes = b"",
        need_ack: bool = False,
    ) -> bytes:
        head = bytearray()
        _wvar(head, 1, cmd_type)
        _wstr(head, 2, cmd)
        _wvar(head, 3, self._next_seq())
        _wstr(head, 4, msg_id)
        _wstr(head, 5, module)
        if need_ack:
            _wvar(head, 6, 1)
        out = bytearray()
        _wbytes(out, 1, bytes(head))
        _wbytes(out, 2, data)
        return bytes(out)

    @staticmethod
    def parse(raw: bytes) -> tuple[dict[str, Any], bytes] | None:
        cm = parse_fields(raw)
        head_b = field_bytes(cm, 1)
        if not head_b:
            return None
        hf = parse_fields(head_b)
        head = {
            "cmd_type": int(field_str(hf, 1) or 0) if field_str(hf, 1) else 0,
            "cmd": field_str(hf, 2),
            "msg_id": field_str(hf, 4),
            "module": field_str(hf, 5),
            "need_ack": any(fn == 6 and v == 1 for fn, v in hf),
        }
        # cmd_type arrives as a varint, not a string — read it properly
        for fn, val in hf:
            if fn == 1 and isinstance(val, int):
                head["cmd_type"] = val
        return head, field_bytes(cm, 2)


def _element(msg_type: str, content: bytes) -> bytes:
    el = bytearray()
    _wstr(el, 1, msg_type)
    _wbytes(el, 2, content)
    return bytes(el)


def _text_body(text: str) -> bytes:
    mc = bytearray()
    _wstr(mc, 1, text)
    return _element("TIMTextElem", bytes(mc))


def _image_body(url: str, file_uuid: str, size: int, width: int, height: int) -> bytes:
    info = bytearray()
    _wvar(info, 1, 1)
    _wvar(info, 2, size)
    _wvar(info, 3, width)
    _wvar(info, 4, height)
    _wstr(info, 5, url)
    mc = bytearray()
    _wstr(mc, 2, file_uuid)
    _wvar(mc, 3, 255)
    _wbytes(mc, 8, bytes(info))
    return _element("TIMImageElem", bytes(mc))


def _file_body(url: str, file_uuid: str, name: str, size: int, *, video: bool = False) -> bytes:
    mc = bytearray()
    _wstr(mc, 2, file_uuid)
    _wstr(mc, 10, url)
    _wvar(mc, 11, size)
    if not video:
        _wstr(mc, 12, name)
    return _element("TIMVideoElem" if video else "TIMFileElem", bytes(mc))


def _image_size(data: bytes) -> tuple[int, int]:
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        w, h = struct.unpack(">II", data[16:24])
        return w, h
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        w, h = struct.unpack("<HH", data[6:10])
        return w, h
    if data[:2] == b"\xff\xd8":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xC0, 0xC1, 0xC2):
                h, w = struct.unpack(">HH", data[i + 5 : i + 9])
                return w, h
            length = struct.unpack(">H", data[i + 2 : i + 4])[0]
            i += 2 + length
    return 0, 0


# ---------------------------------------------------------------------------
# Tencent COS request signing (the platform hands out temporary credentials)
# ---------------------------------------------------------------------------
def _hmac_sha1_hex(key: str, msg: str) -> str:
    return hmac.new(key.encode(), msg.encode(), hashlib.sha1).hexdigest()


def _cos_authorization(
    secret_id: str,
    secret_key: str,
    pathname: str,
    headers: dict[str, str],
    start: int,
    expired: int,
) -> str:
    key_time = f"{start};{expired}"
    sign_key = _hmac_sha1_hex(secret_key, key_time)
    keys = sorted(k.lower() for k in headers)
    by_lower = {k.lower(): v for k, v in headers.items()}
    http_headers = "&".join(f"{k}={quote(str(by_lower[k]), safe='')}" for k in keys)
    http_string = f"put\n{pathname}\n\n{http_headers}\n"
    string_to_sign = f"sha1\n{key_time}\n{hashlib.sha1(http_string.encode()).hexdigest()}\n"
    signature = _hmac_sha1_hex(sign_key, string_to_sign)
    return (
        f"q-sign-algorithm=sha1&q-ak={secret_id}&q-sign-time={key_time}"
        f"&q-key-time={key_time}&q-header-list={';'.join(keys)}&q-url-param-list=&q-signature={signature}"
    )


def _parse_inbound(data: bytes) -> dict[str, Any]:
    f = parse_fields(data)
    texts: list[str] = []
    for fn, val in f:
        if fn != 13 or not isinstance(val, bytes):
            continue
        el = parse_fields(val)
        msg_type = field_str(el, 1)
        mc_buf = field_bytes(el, 2)
        if not mc_buf:
            continue
        mc = parse_fields(mc_buf)
        if msg_type == "TIMTextElem":
            t = field_str(mc, 1)
            if t:
                texts.append(t)
        elif msg_type == "TIMImageElem":
            texts.append("[图片]")
        elif msg_type == "TIMFileElem":
            texts.append(f"[文件] {field_str(mc, 12)}")
        elif msg_type == "TIMSoundElem":
            texts.append("[语音]")
    return {
        "msg_id": field_str(f, 12),
        "from": field_str(f, 2),
        "nickname": field_str(f, 4),
        "group_code": field_str(f, 6),
        "group_name": field_str(f, 7),
        "text": "\n".join(texts).strip(),
    }


class YuanbaoChannel(Channel):
    name = "yuanbao"
    label = "元宝派 (Yuanbao)"
    sdk_module = "websockets"
    extra = "channels"
    console_url = "https://yuanbao.tencent.com"
    fields = (
        Field("app_id", "App ID (app_key)", required=True, help="The bot's App ID on the Yuanbao bot platform"),
        Field("app_secret", "App Secret", "secret", required=True, help="The bot's App Secret"),
    )

    def __init__(self, settings: dict[str, Any]):
        super().__init__(settings)
        self._framer = _Framer()
        self._task: asyncio.Task[None] | None = None
        self._ws: Any = None
        self._send_lock = asyncio.Lock()
        self._bot_id = ""
        self._seen: dict[str, float] = {}

    # ------------------------------------------------------------------ sign-token
    async def _sign_token(self) -> tuple[str, str, str]:
        app_key = str(self.settings.get("app_id") or "")
        app_secret = str(self.settings.get("app_secret") or "")
        nonce = secrets.token_hex(16)
        ts = datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")
        signature = hmac.new(
            app_secret.encode(), (nonce + ts + app_key + app_secret).encode(), hashlib.sha256
        ).hexdigest()
        async with httpx.AsyncClient(timeout=httpx.Timeout(20.0, connect=15.0)) as http:
            r = await http.post(
                f"{API_BASE}/api/v5/robotLogic/sign-token",
                json={
                    "app_key": app_key,
                    "nonce": nonce,
                    "signature": signature,
                    "timestamp": ts,
                },
            )
        data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code >= 400 or data.get("code") != 0:
            raise YuanbaoError(
                f"sign-token failed: HTTP {r.status_code} code={data.get('code')} {data.get('message') or ''}"
            )
        d = data.get("data") or {}
        return str(d.get("bot_id") or ""), str(d.get("token") or ""), str(d.get("source") or "web")

    async def verify(self) -> str:
        bot_id, _token, _source = await self._sign_token()
        return f"bot {bot_id[:12]}…" if bot_id else "bot"

    # ------------------------------------------------------------------ lifecycle
    async def start(self, bus: ChannelBus) -> None:
        await super().start(bus)
        self._task = asyncio.create_task(self._run_loop(), name="yuanbao-ws")

    async def stop(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError, Exception):
                await task
        if self._ws is not None:
            with suppress(Exception):
                await self._ws.close()
            self._ws = None

    async def _run_loop(self) -> None:
        delay = 2.0
        while True:
            try:
                await self._connect_once()
                delay = 2.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning("yuanbao: connection failed: {}", exc)
                if self.bus is not None:
                    self.bus.set_status(self.name, "connecting", str(exc)[:200])
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)

    async def _connect_once(self) -> None:
        import websockets

        bot_id, token, source = await self._sign_token()
        self._bot_id = bot_id
        auth = bytearray()
        ai = bytearray()
        _wstr(ai, 1, bot_id)
        _wstr(ai, 2, source)
        _wstr(ai, 3, token)
        di = bytearray()
        _wstr(di, 1, DEVICE_NAME)
        _wstr(di, 2, "linux")
        _wstr(di, 10, "17")
        _wstr(di, 24, DEVICE_NAME)
        _wstr(auth, 1, "ybBot")
        _wbytes(auth, 2, bytes(ai))
        _wbytes(auth, 3, bytes(di))

        authed = asyncio.Event()
        async with websockets.connect(WS_URL, max_size=8 * 1024 * 1024) as ws:
            self._ws = ws
            await ws.send(self._framer.msg(0, "auth-bind", f"auth_{int(time.time() * 1000)}", "conn_access", bytes(auth)))
            ping_task = asyncio.create_task(self._ping_loop(ws))
            try:
                async for raw in ws:
                    if isinstance(raw, str):
                        raw = raw.encode()
                    parsed = _Framer.parse(raw)
                    if parsed is None:
                        continue
                    head, data = parsed
                    if head["cmd"] == "auth-bind" and head["cmd_type"] == 1:
                        authed.set()
                        if self.bus is not None:
                            self.bus.set_status(self.name, "connected", f"bot {bot_id[:12]}…")
                        continue
                    if head["cmd_type"] == 2 and head["cmd"] in ("inbound_message", "InboundMessagePush"):
                        if head["need_ack"]:
                            with suppress(Exception):
                                await ws.send(self._framer.msg(3, head["cmd"], head["msg_id"], "conn_access"))
                        self._handle_inbound(data)
            finally:
                ping_task.cancel()
                self._ws = None

    async def _ping_loop(self, ws: Any) -> None:
        while True:
            await asyncio.sleep(30.0)
            await ws.send(self._framer.msg(0, "ping", f"ping_{int(time.time() * 1000)}", "conn_access"))

    # ------------------------------------------------------------------ inbound
    def _handle_inbound(self, data: bytes) -> None:
        if self.bus is None:
            return
        try:
            m = _parse_inbound(data)
        except Exception as exc:  # noqa: BLE001
            logger.warning("yuanbao: inbound parse failed: {}", exc)
            return
        if not m["msg_id"] or not m["text"]:
            return
        now = time.time()
        self._seen = {k: v for k, v in self._seen.items() if now - v < 600}
        if m["msg_id"] in self._seen:
            return
        self._seen[m["msg_id"]] = now
        if m["from"] == self._bot_id:
            return  # our own echo
        is_group = bool(m["group_code"])
        self.bus.inbound(
            InboundMessage(
                channel=self.name,
                chat_id=f"g:{m['group_code']}" if is_group else f"u:{m['from']}",
                sender_id=m["from"],
                sender_name=m["nickname"],
                text=m["text"],
                is_group=is_group,
                mentioned=True,  # a push to the bot means it was addressed
                message_id=m["msg_id"],
                chat_name=m["group_name"],
            )
        )

    # ------------------------------------------------------------------ media upload
    async def _api_post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """The platform's business API, authenticated with the sign-token."""
        bot_id, token, source = await self._sign_token()
        headers = {
            "X-ID": bot_id,
            "X-Token": token,
            "X-Source": source or "web",
            "X-AppVersion": "2.18.3",
            "X-OperationSystem": "linux",
            "X-Instance-Id": "16",
            "X-Bot-Version": "nanomuse",
        }
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=15.0)) as http:
            r = await http.post(f"{API_BASE}{path}", json=body, headers=headers)
        data = r.json() if "json" in r.headers.get("content-type", "") else {}
        if r.status_code >= 400 or data.get("code") not in (0, None):
            raise YuanbaoError(f"{path} failed: HTTP {r.status_code} {str(data)[:200]}")
        return data.get("data") or data

    async def _upload_media(self, path: Path) -> dict[str, Any]:
        """Reserve a COS slot through the platform and PUT the bytes to it."""
        import json as _json

        data_bytes = path.read_bytes()
        if len(data_bytes) > 20 * 1024 * 1024:
            raise YuanbaoError(f"{path.name} is too large for Yuanbao (over 20 MB)")
        cfg = await self._api_post(
            "/api/resource/genUploadInfo",
            {"fileName": path.name, "fileId": secrets.token_hex(16), "docFrom": "localDoc", "docOpenId": ""},
        )
        flat: dict[str, Any] = {}
        for src in (cfg, cfg.get("encryptConfig"), cfg.get("credentials"), cfg.get("tmpCredentials")):
            if isinstance(src, dict):
                for k, v in src.items():
                    flat.setdefault(k, v)
        secret_id = flat.get("encryptTmpSecretId") or flat.get("tmpSecretId")
        secret_key = flat.get("encryptTmpSecretKey") or flat.get("tmpSecretKey")
        token = flat.get("encryptToken") or flat.get("sessionToken") or flat.get("token")
        bucket, region, location = flat.get("bucketName"), flat.get("region"), flat.get("location")
        if not (secret_id and secret_key and bucket and region and location):
            raise YuanbaoError(f"genUploadInfo returned an incomplete slot: {sorted(flat)[:14]}")
        host = f"{bucket}.cos.{region}.myqcloud.com"
        pathname = "/" + str(location).lstrip("/")
        sign_headers = {"host": host, "content-length": str(len(data_bytes))}
        if token:
            sign_headers["x-cos-security-token"] = str(token)
        auth = _cos_authorization(
            str(secret_id),
            str(secret_key),
            pathname,
            sign_headers,
            int(flat.get("startTime") or time.time()),
            int(flat.get("expiredTime") or time.time() + 1800),
        )
        mime = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        headers = {"Authorization": auth, "Content-Type": mime}
        if token:
            headers["x-cos-security-token"] = str(token)
        if mime.startswith("image/"):
            headers["Pic-Operations"] = _json.dumps(
                {"is_pic_info": 1, "rules": [{"fileid": str(location), "rule": "imageMogr2/format/jpg"}]}
            )
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=15.0)) as http:
            r = await http.put(f"https://{host}{pathname}", content=data_bytes, headers=headers)
        if r.status_code >= 300:
            raise YuanbaoError(f"COS upload failed: HTTP {r.status_code} {r.text[:160]}")
        width, height = _image_size(data_bytes) if mime.startswith("image/") else (0, 0)
        return {
            "url": str(flat.get("resourceUrl") or f"https://{host}{pathname}"),
            "uuid": hashlib.md5(data_bytes).hexdigest(),
            "size": len(data_bytes),
            "width": width,
            "height": height,
            "mime": mime,
        }

    # ------------------------------------------------------------------ outbound
    async def _send_element(self, chat_id: str, element: bytes) -> str:
        ws = self._ws
        if ws is None:
            raise YuanbaoError("Yuanbao is not connected.")
        msg_id = f"nm_{uuid.uuid4().hex[:16]}"
        body = bytearray()
        _wstr(body, 1, msg_id)
        if chat_id.startswith("g:"):
            _wstr(body, 2, chat_id[2:])
            _wstr(body, 3, self._bot_id)
            _wbytes(body, 6, element)
            frame = self._framer.msg(0, "send_group_message", msg_id, "yuanbao_openclaw_proxy", bytes(body))
        else:
            _wstr(body, 2, chat_id[2:] if chat_id.startswith("u:") else chat_id)
            _wstr(body, 3, self._bot_id)
            _wbytes(body, 5, element)
            frame = self._framer.msg(0, "send_c2c_message", msg_id, "yuanbao_openclaw_proxy", bytes(body))
        async with self._send_lock:
            await ws.send(frame)
        return msg_id

    async def send(self, chat_id: str, text: str, *, files: list[str] | None = None) -> str:
        last = ""
        if text:
            last = await self._send_element(chat_id, _text_body(text[:TEXT_LIMIT]))
        for path_str in files or []:
            path = Path(path_str)
            if not path.is_file():
                continue
            up = await self._upload_media(path)
            if up["mime"].startswith("image/"):
                element = _image_body(up["url"], up["uuid"], up["size"], up["width"], up["height"])
            elif up["mime"].startswith("video/"):
                element = _file_body(up["url"], up["uuid"], path.name, up["size"], video=True)
            else:
                element = _file_body(up["url"], up["uuid"], path.name, up["size"])
            last = await self._send_element(chat_id, element)
        if not last:
            raise YuanbaoError("nothing to send")
        return last


__all__ = ["YuanbaoChannel"]
