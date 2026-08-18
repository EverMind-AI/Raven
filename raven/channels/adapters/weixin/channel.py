"""Personal WeChat adapter over Tencent's iLink bot gateway.

HTTP long-polling against ilinkai.weixin.qq.com — no local WeChat client or
public IP. Auth is a QR-code login that yields a bot token (persisted to
disk). The iLink wire protocol lives in :mod:`.protocol`; AES media crypto in
:mod:`.crypto`.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import time
import uuid
from collections import OrderedDict
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import quote

import httpx
from loguru import logger

from raven.channels.adapters.weixin import crypto
from raven.channels.adapters.weixin import protocol as p
from raven.channels.adapters.weixin.typing_state import TypingIndicator
from raven.channels.base import ChannelBase
from raven.channels.contract import Capabilities
from raven.channels.errors import retryable_http
from raven.channels.media import save_media_bytes
from raven.channels.transcribe import transcribe_audio
from raven.config.paths import get_runtime_subdir
from raven.config.schema import WeixinConfig
from raven.utils.helpers import split_message

_DEDUP_CAP = 1000


class WeixinChannel(ChannelBase):
    """Personal WeChat channel using the iLink HTTP long-poll API."""

    config: WeixinConfig
    name = "weixin"
    display_name = "WeChat"
    capabilities = Capabilities(interactive_login=True)  # QR pairing via iLink

    def __init__(self, config: WeixinConfig):
        super().__init__(config)
        self._client: httpx.AsyncClient | None = None
        self._token: str = ""
        self._updates_buf: str = ""
        self._context_tokens: dict[str, str] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._state_dir: Path | None = None
        self._poll_timeout_s: int = p.DEFAULT_LONG_POLL_TIMEOUT_S
        self._session_pause_until: float = 0.0
        # The lambda resolves _post at call time (don't capture the bound
        # method: tests replace ch._post and the indicator must follow).
        self._typing = TypingIndicator(post=lambda *a, **kw: self._post(*a, **kw))

    # ── state persistence ─────────────────────────────────────────────

    def _dir(self) -> Path:
        if self._state_dir:
            return self._state_dir
        d = Path(self.config.state_dir).expanduser() if self.config.state_dir else get_runtime_subdir("weixin")
        d.mkdir(parents=True, exist_ok=True)
        self._state_dir = d
        return d

    def _load_state(self) -> bool:
        """Restore a saved token + cursor. Returns True if a token was found."""
        state_file = self._dir() / "account.json"
        if not state_file.exists():
            return False
        try:
            data = json.loads(state_file.read_text())
        except Exception:
            return False
        self._token = data.get("token", "")
        self._updates_buf = data.get("get_updates_buf", "")
        ctx = data.get("context_tokens", {})
        self._context_tokens = (
            {str(u): str(t) for u, t in ctx.items() if str(u).strip() and str(t).strip()}
            if isinstance(ctx, dict)
            else {}
        )
        tickets = data.get("typing_tickets", {})
        self._typing.restore(
            {str(u): t for u, t in tickets.items() if str(u).strip() and isinstance(t, dict)}
            if isinstance(tickets, dict)
            else {}
        )
        if data.get("base_url"):
            self.config.base_url = data["base_url"]
        return bool(self._token)

    def _save_state(self) -> None:
        try:
            (self._dir() / "account.json").write_text(
                json.dumps(
                    {
                        "token": self._token,
                        "get_updates_buf": self._updates_buf,
                        "context_tokens": self._context_tokens,
                        "typing_tickets": self._typing.snapshot(),
                        "base_url": self.config.base_url,
                    },
                    ensure_ascii=False,
                )
            )
        except Exception as e:
            # A silent failure here means the auth token never hits disk and
            # the next start demands a fresh QR scan with no clue why.
            logger.warning("Failed to persist weixin state: {}", e)

    # ── HTTP ──────────────────────────────────────────────────────────

    def _headers(self, *, auth: bool = True) -> dict[str, str]:
        return p.build_headers(self._token if auth else "", self.config.route_tag)

    async def _get(
        self, endpoint: str, params: dict | None = None, *, base_url: str | None = None, auth: bool = True
    ) -> dict:
        assert self._client is not None
        url = f"{(base_url or self.config.base_url).rstrip('/')}/{endpoint}"
        resp = await self._client.get(url, params=params, headers=self._headers(auth=auth))
        resp.raise_for_status()
        return resp.json()

    async def _post(self, endpoint: str, body: dict | None = None, *, auth: bool = True) -> dict:
        assert self._client is not None
        payload = dict(body or {})
        payload.setdefault("base_info", p.BASE_INFO)
        resp = await self._client.post(
            f"{self.config.base_url}/{endpoint}", json=payload, headers=self._headers(auth=auth)
        )
        resp.raise_for_status()
        return resp.json()

    # ── QR login ──────────────────────────────────────────────────────

    async def _fetch_qr(self) -> tuple[str, str]:
        data = await self._get("ilink/bot/get_bot_qrcode", params={"bot_type": "3"}, auth=False)
        qrcode_id = data.get("qrcode", "")
        if not qrcode_id:
            raise RuntimeError(f"Failed to get QR code: {data}")
        return qrcode_id, (data.get("qrcode_img_content") or qrcode_id)

    @staticmethod
    def _print_qr(url: str) -> None:
        try:
            import qrcode as qr_lib

            qr = qr_lib.QRCode(border=1)
            qr.add_data(url)
            qr.make(fit=True)
            qr.print_ascii(invert=True)
        except ImportError:
            print(f"\nLogin URL: {url}\n")

    async def _qr_login(self) -> bool:
        try:
            qrcode_id, scan_url = await self._fetch_qr()
            self._print_qr(scan_url)
            poll_base = self.config.base_url
            refreshes = 0

            while self._running:
                try:
                    status_data = await self._get(
                        "ilink/bot/get_qrcode_status",
                        params={"qrcode": qrcode_id},
                        base_url=poll_base,
                        auth=False,
                    )
                except Exception as e:
                    if retryable_http(e):
                        await asyncio.sleep(1)
                        continue
                    raise
                if not isinstance(status_data, dict):
                    await asyncio.sleep(1)
                    continue

                status = status_data.get("status", "")
                if status == "confirmed":
                    token = status_data.get("bot_token", "")
                    if not token:
                        logger.error("Login confirmed but no bot_token in response")
                        return False
                    self._token = token
                    if status_data.get("baseurl"):
                        self.config.base_url = status_data["baseurl"]
                    self._save_state()
                    logger.info(
                        "login successful (bot_id={} user_id={})",
                        status_data.get("ilink_bot_id", ""),
                        status_data.get("ilink_user_id", ""),
                    )
                    return True
                if status == "scaned_but_redirect":
                    host = str(status_data.get("redirect_host", "") or "").strip()
                    if host:
                        redirected = host if host.startswith(("http://", "https://")) else f"https://{host}"
                        if redirected != poll_base:
                            poll_base = redirected
                elif status == "expired":
                    refreshes += 1
                    if refreshes > p.MAX_QR_REFRESH_COUNT:
                        logger.warning("QR code expired too many times, giving up")
                        return False
                    qrcode_id, scan_url = await self._fetch_qr()
                    poll_base = self.config.base_url
                    self._print_qr(scan_url)
                    continue
                await asyncio.sleep(1)
        except Exception:
            logger.exception("QR login failed")
        return False

    async def login(self, force: bool = False) -> bool:
        if force:
            self._token = ""
            self._updates_buf = ""
            with suppress(FileNotFoundError):
                (self._dir() / "account.json").unlink()
        if self._token or self._load_state():
            return True
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60, connect=30), follow_redirects=True)
        self._running = True
        try:
            return await self._qr_login()
        finally:
            self._running = False
            await self._close_client()

    # ── lifecycle ─────────────────────────────────────────────────────

    async def _authenticate(self) -> bool:
        """Restore persisted state, then resolve the auth token.

        State is loaded unconditionally — a configured token must not skip it,
        or the get_updates cursor, per-chat context_tokens, and typing tickets
        are lost on every start (replays, and send() raising "context_token
        missing" until each chat speaks again). The configured token then wins
        over the persisted one.
        """
        loaded = self._load_state()
        if self.config.token:
            self._token = self.config.token
            return True
        return loaded or await self._qr_login()

    async def start(self) -> None:
        self._running = True
        self._poll_timeout_s = self.config.poll_timeout
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(self._poll_timeout_s + 10, connect=30), follow_redirects=True
        )
        if not await self._authenticate():
            logger.error("login failed. Run 'raven channels login weixin' to authenticate.")
            self._running = False
            return

        logger.info("channel starting with long-poll...")
        failures = 0
        while self._running:
            try:
                await self._poll_once()
                failures = 0
            except httpx.TimeoutException:
                continue  # normal for long-poll
            except Exception:
                if not self._running:
                    break
                logger.opt(exception=True).warning("weixin poll error")
                failures += 1
                if failures >= p.MAX_CONSECUTIVE_FAILURES:
                    failures = 0
                    await asyncio.sleep(p.BACKOFF_DELAY_S)
                else:
                    await asyncio.sleep(p.RETRY_DELAY_S)

    async def stop(self) -> None:
        self._running = False
        await self._typing.stop_all()
        await self._close_client()
        self._save_state()

    async def _close_client(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    # ── session pause ─────────────────────────────────────────────────

    def _session_remaining_s(self) -> int:
        remaining = int(self._session_pause_until - time.time())
        if remaining <= 0:
            self._session_pause_until = 0.0
            return 0
        return remaining

    def _assert_session_active(self) -> None:
        remaining = self._session_remaining_s()
        if remaining > 0:
            raise RuntimeError(
                f"WeChat session paused, {max((remaining + 59) // 60, 1)} min remaining "
                f"(errcode {p.ERRCODE_SESSION_EXPIRED})"
            )

    # ── poll ──────────────────────────────────────────────────────────

    async def _poll_once(self) -> None:
        if (remaining := self._session_remaining_s()) > 0:
            await asyncio.sleep(remaining)
            return

        assert self._client is not None
        self._client.timeout = httpx.Timeout(self._poll_timeout_s + 10, connect=30)
        data = await self._post("ilink/bot/getupdates", {"get_updates_buf": self._updates_buf})

        ret, errcode = data.get("ret", 0), data.get("errcode", 0)
        if (ret and ret != 0) or (errcode and errcode != 0):
            if p.ERRCODE_SESSION_EXPIRED in (ret, errcode):
                self._session_pause_until = time.time() + p.SESSION_PAUSE_DURATION_S
                logger.warning(
                    "session expired (errcode {}). Pausing {} min.",
                    errcode,
                    max((self._session_remaining_s() + 59) // 60, 1),
                )
                return
            raise RuntimeError(f"getUpdates failed: ret={ret} errcode={errcode} errmsg={data.get('errmsg', '')}")

        if (server_ms := data.get("longpolling_timeout_ms")) and server_ms > 0:
            self._poll_timeout_s = max(server_ms // 1000, 5)
        if new_buf := data.get("get_updates_buf", ""):
            self._updates_buf = new_buf
            self._save_state()

        for msg in data.get("msgs", []) or []:
            try:
                await self._process_message(msg)
            except Exception:
                logger.opt(exception=True).warning("Error processing weixin message")

    # ── inbound ───────────────────────────────────────────────────────

    async def _process_message(self, msg: dict) -> None:
        if msg.get("message_type") == p.MESSAGE_TYPE_BOT:
            return
        from_user = msg.get("from_user_id", "") or ""
        if not from_user or not self.is_allowed(from_user):
            return

        msg_id = str(msg.get("message_id", "") or msg.get("seq", "")) or f"{from_user}_{msg.get('create_time_ms', '')}"
        if msg_id in self._seen:
            return
        self._seen[msg_id] = None
        while len(self._seen) > _DEDUP_CAP:
            self._seen.popitem(last=False)

        if ctx := msg.get("context_token", ""):
            self._context_tokens[from_user] = ctx
            self._save_state()

        items: list[dict] = msg.get("item_list") or []
        parts: list[str] = []
        media: list[str] = []
        had_locator = False

        for item in items:
            kind = item.get("type", 0)
            if kind == p.ITEM_TEXT:
                parts.extend(self._render_text_item(item))
            elif kind in (p.ITEM_IMAGE, p.ITEM_VOICE, p.ITEM_FILE, p.ITEM_VIDEO):
                typed = self._typed_item(item, kind)
                if p.has_downloadable_media_locator(typed.get("media")):
                    had_locator = True
                await self._render_media_item(typed, kind, parts, media)

        # Fallback: pull media from a quoted message when the main items had none.
        if not media and not had_locator:
            quoted = self._first_quoted_media(items)
            if quoted:
                await self._render_media_item(quoted[1], quoted[0], parts, media)

        content = "\n".join(parts)
        if not content:
            return

        await self._start_typing(from_user, msg.get("context_token", ""))
        await self.intake.publish(
            sender_id=from_user,
            chat_id=from_user,
            content=content,
            media=media or None,
            metadata={"message_id": msg_id},
        )

    @staticmethod
    def _typed_item(item: dict, kind: int) -> dict:
        key = {
            p.ITEM_IMAGE: "image_item",
            p.ITEM_VOICE: "voice_item",
            p.ITEM_FILE: "file_item",
            p.ITEM_VIDEO: "video_item",
        }[kind]
        return item.get(key) or {}

    @staticmethod
    def _render_text_item(item: dict) -> list[str]:
        text = (item.get("text_item") or {}).get("text", "")
        if not text:
            return []
        ref = item.get("ref_msg")
        if not ref:
            return [text]
        ref_item = ref.get("message_item")
        if ref_item and ref_item.get("type", 0) in (p.ITEM_IMAGE, p.ITEM_VOICE, p.ITEM_FILE, p.ITEM_VIDEO):
            return [text]  # quoted media: just the reply text
        quoted = []
        if ref.get("title"):
            quoted.append(ref["title"])
        if ref_item and (rt := (ref_item.get("text_item") or {}).get("text", "")):
            quoted.append(rt)
        return [f"[引用: {' | '.join(quoted)}]\n{text}" if quoted else text]

    @staticmethod
    def _first_quoted_media(items: list[dict]) -> tuple[int, dict] | None:
        for item in items:
            if item.get("type", 0) != p.ITEM_TEXT:
                continue
            cand = (item.get("ref_msg") or {}).get("message_item") or {}
            ckind = cand.get("type", 0)
            if ckind in (p.ITEM_IMAGE, p.ITEM_VOICE, p.ITEM_FILE, p.ITEM_VIDEO):
                key = {
                    p.ITEM_IMAGE: "image_item",
                    p.ITEM_VOICE: "voice_item",
                    p.ITEM_FILE: "file_item",
                    p.ITEM_VIDEO: "video_item",
                }[ckind]
                return ckind, (cand.get(key) or {})
        return None

    async def _render_media_item(self, typed: dict, kind: int, parts: list[str], media: list[str]) -> None:
        if kind == p.ITEM_IMAGE:
            path = await self._download_media(typed, "image")
            if path:
                parts.append(f"[image]\n[Image: source: {path}]")
                media.append(path)
            else:
                parts.append("[image]")
        elif kind == p.ITEM_VOICE:
            if voice_text := typed.get("text", ""):  # WeChat server-side transcription
                parts.append(f"[voice] {voice_text}")
                return
            path = await self._download_media(typed, "voice")
            if not path:
                parts.append("[voice]")
                return
            media.append(path)
            transcription = await transcribe_audio(path, self.transcription_api_key, channel=self.name)
            parts.append(f"[voice] {transcription}" if transcription else f"[voice]\n[Audio: source: {path}]")
        elif kind == p.ITEM_FILE:
            name = typed.get("file_name", "unknown")
            path = await self._download_media(typed, "file", name)
            if path:
                parts.append(f"[file: {name}]\n[File: source: {path}]")
                media.append(path)
            else:
                parts.append(f"[file: {name}]")
        elif kind == p.ITEM_VIDEO:
            path = await self._download_media(typed, "video")
            if path:
                parts.append(f"[video]\n[Video: source: {path}]")
                media.append(path)
            else:
                parts.append("[video]")

    async def _download_media(self, typed: dict, media_type: str, filename: str | None = None) -> str | None:
        """Download + AES-decrypt one media item; persist via the shared helper."""
        try:
            media = typed.get("media") or {}
            encrypt_param = str(media.get("encrypt_query_param", "") or "")
            full_url = str(media.get("full_url", "") or "").strip()
            if not encrypt_param and not full_url:
                return None

            # image_item.aeskey is raw hex (→ base64); media.aes_key is already base64.
            if raw_hex := typed.get("aeskey", ""):
                aes_key_b64 = base64.b64encode(bytes.fromhex(raw_hex)).decode()
            else:
                aes_key_b64 = media.get("aes_key", "")
            if media_type != "image" and not aes_key_b64:
                return None  # voice/file/video require a key

            candidates: list[tuple[str, str]] = []
            if full_url:
                candidates.append(("full_url", full_url))
            fallback = (
                f"{self.config.cdn_base_url}/download?encrypted_query_param={quote(encrypt_param)}"
                if encrypt_param
                else ""
            )
            if fallback and fallback != full_url:
                candidates.append(("encrypt_query_param", fallback))

            assert self._client is not None
            data = b""
            for idx, (source, url) in enumerate(candidates):
                try:
                    resp = await self._client.get(url)
                    resp.raise_for_status()
                    data = resp.content
                    break
                except Exception as e:
                    if source == "full_url" and idx + 1 < len(candidates) and retryable_http(e):
                        logger.warning("media download via full_url failed, trying fallback: {}", e)
                        continue
                    raise
            if not data:
                return None
            if aes_key_b64:
                data = crypto.decrypt(data, aes_key_b64)
            if not data:
                return None
            name = filename or f"{media_type}{p.ext_for_type(media_type)}"
            return str(save_media_bytes("weixin", data, name))
        except Exception:
            logger.exception("Error downloading media")
            return None

    # ── typing ────────────────────────────────────────────────────────

    async def _start_typing(self, chat_id: str, context_token: str = "") -> None:
        if not self._client or not self._token:
            return
        await self._typing.start(chat_id, context_token)

    async def _stop_typing(self, chat_id: str, *, clear_remote: bool) -> None:
        await self._typing.stop(chat_id, clear_remote=clear_remote)

    # ── outbound ──────────────────────────────────────────────────────

    async def send(self, chat_id: str, content: str, media: list[str] | None = None) -> None:
        if not self._client or not self._token:
            raise RuntimeError("WeChat client not initialized or not authenticated")
        self._assert_session_active()

        await self._stop_typing(chat_id, clear_remote=True)

        ctx_token = self._context_tokens.get(chat_id, "")
        if not ctx_token:
            raise RuntimeError(f"WeChat context_token missing for chat_id={chat_id}, cannot send")

        # Show typing while we send; the finally tears it down and clears the
        # remote indicator.
        await self._typing.start(chat_id, ctx_token)
        try:
            for path in media or []:
                await self._send_one_media(chat_id, path, ctx_token)
            text = content.strip()
            if text:
                for chunk in split_message(text, p.MAX_MESSAGE_LEN):
                    await self._send_text(chat_id, chunk, ctx_token)
        except Exception:
            logger.exception("Error sending message")
            raise
        finally:
            await self._typing.stop(chat_id, clear_remote=True)

    async def _send_one_media(self, chat_id: str, path: str, ctx_token: str) -> None:
        """Send one media file, falling back to a text notice on non-retryable errors."""
        try:
            await self._send_media_file(chat_id, path, ctx_token)
        except (httpx.TimeoutException, httpx.TransportError):
            logger.opt(exception=True).warning("Network error sending media {}", path)
            raise  # let ChannelManager retry
        except httpx.HTTPStatusError as e:
            if e.response is not None and e.response.status_code >= 500:
                logger.exception("Server error sending media {}", path)
                raise
            logger.exception("Failed to send media {}", path)
            await self._send_text(chat_id, f"[Failed to send: {Path(path).name}]", ctx_token)
        except Exception:
            logger.exception("Failed to send media {}", path)
            await self._send_text(chat_id, f"[Failed to send: {Path(path).name}]", ctx_token)

    def _bot_msg(self, to_user: str, ctx_token: str, item_list: list[dict] | None = None) -> dict:
        msg: dict[str, Any] = {
            "from_user_id": "",
            "to_user_id": to_user,
            "client_id": f"raven-{uuid.uuid4().hex[:12]}",
            "message_type": p.MESSAGE_TYPE_BOT,
            "message_state": p.MESSAGE_STATE_FINISH,
        }
        if item_list:
            msg["item_list"] = item_list
        if ctx_token:
            msg["context_token"] = ctx_token
        return msg

    async def _send_text(self, to_user: str, text: str, ctx_token: str) -> None:
        item_list = [{"type": p.ITEM_TEXT, "text_item": {"text": text}}] if text else []
        data = await self._post("ilink/bot/sendmessage", {"msg": self._bot_msg(to_user, ctx_token, item_list)})
        if errcode := data.get("errcode", 0):
            raise RuntimeError(f"WeChat send text error (code {errcode}): {data.get('errmsg', '')}")

    async def _send_media_file(self, to_user: str, media_path: str, ctx_token: str) -> None:
        """Upload to the iLink CDN (AES-encrypted) and send it as a media message."""
        path = Path(media_path)
        if not path.is_file():
            raise FileNotFoundError(f"Media file not found: {media_path}")
        raw = path.read_bytes()

        ext = path.suffix.lower()
        if ext in p._IMAGE_EXTS:
            upload_type, item_type, item_key = p.UPLOAD_IMAGE, p.ITEM_IMAGE, "image_item"
        elif ext in p._VIDEO_EXTS:
            upload_type, item_type, item_key = p.UPLOAD_VIDEO, p.ITEM_VIDEO, "video_item"
        elif ext in p._VOICE_EXTS:
            upload_type, item_type, item_key = p.UPLOAD_VOICE, p.ITEM_VOICE, "voice_item"
        else:
            upload_type, item_type, item_key = p.UPLOAD_FILE, p.ITEM_FILE, "file_item"

        aes_key_raw = os.urandom(16)
        aes_key_hex = aes_key_raw.hex()
        padded_size = ((len(raw) + 1 + 15) // 16) * 16  # aesEcbPaddedSize
        file_key = os.urandom(16).hex()

        upload_resp = await self._post(
            "ilink/bot/getuploadurl",
            {
                "filekey": file_key,
                "media_type": upload_type,
                "to_user_id": to_user,
                "rawsize": len(raw),
                "rawfilemd5": hashlib.md5(raw).hexdigest(),
                "filesize": padded_size,
                "no_need_thumb": True,
                "aeskey": aes_key_hex,
            },
        )
        upload_full_url = str(upload_resp.get("upload_full_url", "") or "").strip()
        upload_param = str(upload_resp.get("upload_param", "") or "")
        if not upload_full_url and not upload_param:
            raise RuntimeError(f"getuploadurl returned no upload URL: {upload_resp}")

        encrypted = crypto.encrypt(raw, base64.b64encode(aes_key_raw).decode())
        cdn_url = upload_full_url or (
            f"{self.config.cdn_base_url}/upload?encrypted_query_param={quote(upload_param)}&filekey={quote(file_key)}"
        )
        assert self._client is not None
        cdn_resp = await self._client.post(
            cdn_url, content=encrypted, headers={"Content-Type": "application/octet-stream"}
        )
        cdn_resp.raise_for_status()
        download_param = cdn_resp.headers.get("x-encrypted-param", "")
        if not download_param:
            raise RuntimeError(f"CDN upload missing x-encrypted-param header (status={cdn_resp.status_code})")

        media_item: dict[str, Any] = {
            "media": {
                "encrypt_query_param": download_param,
                "aes_key": base64.b64encode(aes_key_hex.encode()).decode(),
                "encrypt_type": 1,
            },
        }
        if item_type == p.ITEM_IMAGE:
            media_item["mid_size"] = padded_size
        elif item_type == p.ITEM_VIDEO:
            media_item["video_size"] = padded_size
        elif item_type == p.ITEM_FILE:
            media_item["file_name"] = path.name
            media_item["len"] = str(len(raw))

        item_list = [{"type": item_type, item_key: media_item}]
        data = await self._post("ilink/bot/sendmessage", {"msg": self._bot_msg(to_user, ctx_token, item_list)})
        if errcode := data.get("errcode", 0):
            raise RuntimeError(f"WeChat send media error (code {errcode}): {data.get('errmsg', '')}")
