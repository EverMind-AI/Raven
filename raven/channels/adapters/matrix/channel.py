"""Matrix (Element) channel — matrix-nio sync loop in, message/media out."""

import asyncio
import logging
import mimetypes
from pathlib import Path
from typing import Any, TypeAlias

from loguru import logger
from nio import (
    AsyncClient,
    AsyncClientConfig,
    DownloadError,
    InviteEvent,
    JoinError,
    MatrixRoom,
    MemoryDownloadResponse,
    RoomEncryptedMedia,
    RoomMessage,
    RoomMessageMedia,
    RoomMessageText,
    RoomSendError,
    RoomTypingError,
    SyncError,
    UploadError,
)
from nio.crypto.attachments import decrypt_attachment
from nio.exceptions import EncryptionError

from raven.channels.adapters.matrix import content
from raven.channels.base import ChannelBase
from raven.channels.transcribe import transcribe_audio
from raven.config.paths import get_data_dir, get_media_dir
from raven.config.schema import MatrixConfig
from raven.utils.helpers import safe_filename

TYPING_NOTICE_TIMEOUT_MS = 30_000
# Keep below the notice timeout so the indicator never expires mid-processing.
TYPING_KEEPALIVE_INTERVAL_MS = 20_000

_ATTACH_MARKER = "[attachment: {}]"
_ATTACH_TOO_LARGE = "[attachment: {} - too large]"
_ATTACH_FAILED = "[attachment: {} - download failed]"
_ATTACH_UPLOAD_FAILED = "[attachment: {} - upload failed]"

MEDIA_EVENTS = (RoomMessageMedia, RoomEncryptedMedia)
MatrixMediaEvent: TypeAlias = RoomMessageMedia | RoomEncryptedMedia


class _NioLoguruHandler(logging.Handler):
    """Route matrix-nio's stdlib logging into Loguru."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno
        frame, depth = logging.currentframe(), 2
        while frame and frame.f_code.co_filename == logging.__file__:
            frame, depth = frame.f_back, depth + 1
        logger.opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _bridge_nio_logging() -> None:
    """Attach the Loguru bridge to the nio logger once."""
    nio_logger = logging.getLogger("nio")
    if not any(isinstance(h, _NioLoguruHandler) for h in nio_logger.handlers):
        nio_logger.handlers = [_NioLoguruHandler()]
        nio_logger.propagate = False


class MatrixChannel(ChannelBase):
    """Matrix (Element) channel driven by matrix-nio long-poll sync."""

    config: MatrixConfig
    name = "matrix"
    display_name = "Matrix"

    def __init__(self, config: Any):
        super().__init__(config)
        self.client: AsyncClient | None = None
        self._sync_task: asyncio.Task | None = None
        self._typing_tasks: dict[str, asyncio.Task] = {}
        self._upload_limit_bytes: int | None = None
        self._upload_limit_checked = False

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        self._running = True
        _bridge_nio_logging()

        store_path = get_data_dir() / "matrix-store"
        store_path.mkdir(parents=True, exist_ok=True)

        self.client = AsyncClient(
            homeserver=self.config.homeserver,
            user=self.config.user_id,
            store_path=store_path,
            config=AsyncClientConfig(store_sync_tokens=True, encryption_enabled=self.config.e2ee_enabled),
        )
        self.client.user_id = self.config.user_id
        self.client.access_token = self.config.access_token
        self.client.device_id = self.config.device_id

        self._register_callbacks()

        if not self.config.e2ee_enabled:
            logger.warning("Matrix E2EE disabled; encrypted rooms may be undecryptable.")

        if self.config.device_id:
            try:
                self.client.load_store()
            except Exception:
                logger.exception("Matrix store load failed; restart may replay recent messages.")
        else:
            logger.warning("Matrix device_id empty; restart may replay recent messages.")

        self._sync_task = asyncio.create_task(self._sync_loop())

    async def stop(self) -> None:
        self._running = False
        for room_id in list(self._typing_tasks):
            await self._stop_typing(room_id, clear=False)
        if self.client:
            self.client.stop_sync_forever()
        if self._sync_task:
            try:
                await asyncio.wait_for(asyncio.shield(self._sync_task), timeout=self.config.sync_stop_grace_seconds)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._sync_task.cancel()
                try:
                    await self._sync_task
                except asyncio.CancelledError:
                    pass
        if self.client:
            await self.client.close()

    def _register_callbacks(self) -> None:
        self.client.add_event_callback(self._on_text, RoomMessageText)
        self.client.add_event_callback(self._on_media, MEDIA_EVENTS)
        self.client.add_event_callback(self._on_invite, InviteEvent)
        self.client.add_response_callback(self._on_sync_error, SyncError)
        self.client.add_response_callback(self._on_join_error, JoinError)
        self.client.add_response_callback(self._on_send_error, RoomSendError)

    async def _sync_loop(self) -> None:
        while self._running:
            try:
                await self.client.sync_forever(timeout=30000, full_state=True)
            except asyncio.CancelledError:
                break
            except Exception:
                # A persistent failure (expired token, DNS) would otherwise be
                # an invisible 2s spin — SyncError callbacks only cover error
                # *responses*, not raised exceptions.
                logger.opt(exception=True).warning("Matrix sync loop error; retrying in 2s")
                await asyncio.sleep(2)

    def _log_error(self, label: str, response: Any) -> None:
        code = getattr(response, "status_code", None)
        fatal = code in {"M_UNKNOWN_TOKEN", "M_FORBIDDEN", "M_UNAUTHORIZED"} or getattr(response, "soft_logout", False)
        (logger.error if fatal else logger.warning)("Matrix {} failed: {}", label, response)

    async def _on_sync_error(self, response: SyncError) -> None:
        self._log_error("sync", response)

    async def _on_join_error(self, response: JoinError) -> None:
        self._log_error("join", response)

    async def _on_send_error(self, response: RoomSendError) -> None:
        self._log_error("send", response)

    # ── outbound ──────────────────────────────────────────────────────

    async def send(self, chat_id: str, content_text: str, media: list[str] | None = None) -> None:
        if not self.client:
            return
        text = content_text or ""
        candidates = content.collect_media_candidates(media or [])
        try:
            failures: list[str] = []
            if candidates:
                limit = await self._media_limit_bytes()
                for path in candidates:
                    if fail := await self._upload_attachment(chat_id, path, limit, None):
                        failures.append(fail)
            if failures:
                joined = "\n".join(failures)
                text = f"{text.rstrip()}\n{joined}" if text.strip() else joined
            if text or not candidates:
                payload = content.build_text_content(text)
                await self._room_send(chat_id, payload)
        finally:
            await self._stop_typing(chat_id, clear=True)

    def _is_encrypted_room(self, room_id: str) -> bool:
        if not self.client:
            return False
        room = getattr(self.client, "rooms", {}).get(room_id)
        return bool(getattr(room, "encrypted", False))

    async def _room_send(self, room_id: str, payload: dict[str, Any]) -> None:
        if not self.client:
            return
        kwargs: dict[str, Any] = {"room_id": room_id, "message_type": "m.room.message", "content": payload}
        if self.config.e2ee_enabled:
            kwargs["ignore_unverified_devices"] = True
        await self.client.room_send(**kwargs)

    async def _upload_limit(self) -> int | None:
        """Homeserver-advertised upload ceiling, queried once per lifecycle."""
        if self._upload_limit_checked:
            return self._upload_limit_bytes
        self._upload_limit_checked = True
        if not self.client:
            return None
        try:
            response = await self.client.content_repository_config()
        except Exception:
            return None
        size = getattr(response, "upload_size", None)
        if isinstance(size, int) and size > 0:
            self._upload_limit_bytes = size
            return size
        return None

    async def _media_limit_bytes(self) -> int:
        """min(local cap, server cap); 0 blocks all uploads."""
        local = max(int(self.config.max_media_bytes), 0)
        server = await self._upload_limit()
        if server is None:
            return local
        return min(local, server) if local else 0

    async def _upload_attachment(
        self,
        room_id: str,
        path: Path,
        limit: int,
        relates_to: dict[str, Any] | None,
    ) -> str | None:
        """Upload one local file and send it as media. Returns a failure marker or None."""
        if not self.client:
            return _ATTACH_UPLOAD_FAILED.format(path.name or content.DEFAULT_ATTACH_NAME)

        resolved = path.expanduser().resolve(strict=False)
        filename = safe_filename(resolved.name) or content.DEFAULT_ATTACH_NAME
        fail = _ATTACH_UPLOAD_FAILED.format(filename)

        if not resolved.is_file():
            return fail
        try:
            size = resolved.stat().st_size
        except OSError:
            return fail
        if limit <= 0 or size > limit:
            return _ATTACH_TOO_LARGE.format(filename)

        mime = mimetypes.guess_type(filename, strict=False)[0] or "application/octet-stream"
        try:
            with resolved.open("rb") as f:
                result = await self.client.upload(
                    f,
                    content_type=mime,
                    filename=filename,
                    encrypt=self.config.e2ee_enabled and self._is_encrypted_room(room_id),
                    filesize=size,
                )
        except Exception:
            return fail

        response = result[0] if isinstance(result, tuple) else result
        encryption_info = result[1] if isinstance(result, tuple) and isinstance(result[1], dict) else None
        if isinstance(response, UploadError):
            return fail
        mxc_url = getattr(response, "content_uri", None)
        if not isinstance(mxc_url, str) or not mxc_url.startswith("mxc://"):
            return fail

        payload = content.build_attachment_content(
            filename=filename,
            mime=mime,
            size_bytes=size,
            mxc_url=mxc_url,
            encryption_info=encryption_info,
        )
        if relates_to:
            payload["m.relates_to"] = relates_to
        try:
            await self._room_send(room_id, payload)
        except Exception:
            return fail
        return None

    # ── typing indicator ──────────────────────────────────────────────

    async def _set_typing(self, room_id: str, typing: bool) -> None:
        if not self.client:
            return
        try:
            response = await self.client.room_typing(
                room_id=room_id, typing_state=typing, timeout=TYPING_NOTICE_TIMEOUT_MS
            )
            if isinstance(response, RoomTypingError):
                logger.debug("Matrix typing failed for {}: {}", room_id, response)
        except Exception:
            pass

    async def _start_typing(self, room_id: str) -> None:
        await self._stop_typing(room_id, clear=False)
        await self._set_typing(room_id, True)
        if not self._running:
            return

        async def keepalive() -> None:
            try:
                while self._running:
                    await asyncio.sleep(TYPING_KEEPALIVE_INTERVAL_MS / 1000)
                    await self._set_typing(room_id, True)
            except asyncio.CancelledError:
                pass

        self._typing_tasks[room_id] = asyncio.create_task(keepalive())

    async def _stop_typing(self, room_id: str, *, clear: bool) -> None:
        if task := self._typing_tasks.pop(room_id, None):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        if clear:
            await self._set_typing(room_id, False)

    # ── inbound ───────────────────────────────────────────────────────

    async def _on_invite(self, room: MatrixRoom, event: InviteEvent) -> None:
        if self.is_allowed(event.sender):
            await self.client.join(room.room_id)

    def _should_process(self, room: MatrixRoom, event: RoomMessage) -> bool:
        if not self.is_allowed(event.sender):
            return False
        if content.is_direct_room(room):
            return True
        policy = self.config.group_policy
        if policy == "open":
            return True
        if policy == "allowlist":
            return room.room_id in (self.config.group_allow_from or [])
        if policy == "mention":
            return content.is_bot_mentioned(event, self.config.user_id, self.config.allow_room_mentions)
        return False

    def _base_metadata(self, room: MatrixRoom, event: RoomMessage) -> dict[str, Any]:
        meta: dict[str, Any] = {"room": getattr(room, "display_name", room.room_id)}
        if isinstance(eid := getattr(event, "event_id", None), str) and eid:
            meta["event_id"] = eid
        if thread := content.thread_metadata(event):
            meta.update(thread)
        return meta

    async def _on_text(self, room: MatrixRoom, event: RoomMessageText) -> None:
        if event.sender == self.config.user_id or not self._should_process(room, event):
            return
        await self._start_typing(room.room_id)
        try:
            await self.intake.publish(
                sender_id=event.sender,
                chat_id=room.room_id,
                content=event.body,
                metadata=self._base_metadata(room, event),
            )
        except Exception:
            await self._stop_typing(room.room_id, clear=True)
            raise

    async def _on_media(self, room: MatrixRoom, event: MatrixMediaEvent) -> None:
        if event.sender == self.config.user_id or not self._should_process(room, event):
            return
        attachment, marker = await self._fetch_attachment(event)
        parts: list[str] = []
        if isinstance(body := getattr(event, "body", None), str) and body.strip():
            parts.append(body.strip())

        if attachment and attachment.get("type") == "audio":
            if transcription := await transcribe_audio(
                attachment["path"], self.transcription_api_key, channel=self.name
            ):
                parts.append(f"[transcription: {transcription}]")
            else:
                parts.append(marker)
        elif marker:
            parts.append(marker)

        await self._start_typing(room.room_id)
        try:
            meta = self._base_metadata(room, event)
            meta["attachments"] = [attachment] if attachment else []
            await self.intake.publish(
                sender_id=event.sender,
                chat_id=room.room_id,
                content="\n".join(parts),
                media=[attachment["path"]] if attachment else [],
                metadata=meta,
            )
        except Exception:
            await self._stop_typing(room.room_id, clear=True)
            raise

    # ── media download / decrypt / persist ────────────────────────────

    async def _download_bytes(self, mxc_url: str) -> bytes | None:
        if not self.client:
            return None
        response = await self.client.download(mxc=mxc_url)
        if isinstance(response, DownloadError):
            logger.warning("Matrix download failed for {}: {}", mxc_url, response)
            return None
        body = getattr(response, "body", None)
        if isinstance(body, (bytes, bytearray)):
            return bytes(body)
        if isinstance(response, MemoryDownloadResponse):
            return bytes(response.body)
        if isinstance(body, (str, Path)):
            path = Path(body)
            if path.is_file():
                try:
                    return path.read_bytes()
                except OSError:
                    return None
        return None

    def _decrypt_bytes(self, event: MatrixMediaEvent, ciphertext: bytes) -> bytes | None:
        key_obj = getattr(event, "key", None)
        hashes = getattr(event, "hashes", None)
        iv = getattr(event, "iv", None)
        key = key_obj.get("k") if isinstance(key_obj, dict) else None
        sha256 = hashes.get("sha256") if isinstance(hashes, dict) else None
        if not all(isinstance(v, str) for v in (key, sha256, iv)):
            return None
        try:
            return decrypt_attachment(ciphertext, key, sha256, iv)
        except (EncryptionError, ValueError, TypeError):
            logger.warning("Matrix decrypt failed for event {}", getattr(event, "event_id", ""))
            return None

    async def _fetch_attachment(
        self,
        event: MatrixMediaEvent,
    ) -> tuple[dict[str, Any] | None, str]:
        """Download, decrypt if needed, and persist a Matrix media attachment."""
        kind = content.attachment_kind(event)
        mime = content.media_mime(event)
        filename = content.media_filename(event, kind)
        mxc_url = getattr(event, "url", None)
        fail = _ATTACH_FAILED.format(filename)

        if not isinstance(mxc_url, str) or not mxc_url.startswith("mxc://"):
            return None, fail

        limit = await self._media_limit_bytes()
        declared = content.declared_size_bytes(event)
        if declared is not None and declared > limit:
            return None, _ATTACH_TOO_LARGE.format(filename)

        downloaded = await self._download_bytes(mxc_url)
        if downloaded is None:
            return None, fail

        encrypted = content.is_encrypted_media(event)
        data = downloaded
        if encrypted and (data := self._decrypt_bytes(event, downloaded)) is None:
            return None, fail

        if len(data) > limit:
            return None, _ATTACH_TOO_LARGE.format(filename)

        path = content.attachment_path(get_media_dir("matrix"), event, kind, filename, mime)
        try:
            path.write_bytes(data)
        except OSError:
            return None, fail

        attachment = {
            "type": kind,
            "mime": mime,
            "filename": filename,
            "event_id": str(getattr(event, "event_id", "") or ""),
            "encrypted": encrypted,
            "size_bytes": len(data),
            "path": str(path),
            "mxc_url": mxc_url,
        }
        return attachment, _ATTACH_MARKER.format(path)
