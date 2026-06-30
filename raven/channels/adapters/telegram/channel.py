"""Telegram channel — long-polling adapter on python-telegram-bot (22.x)."""

from __future__ import annotations

import asyncio
import html
import re
import time
import unicodedata
from pathlib import Path

from loguru import logger
from telegram import BotCommand, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest

from raven.channels.base import ChannelBase
from raven.channels.transcribe import transcribe_audio
from raven.config.paths import get_media_dir
from raven.config.schema import TelegramConfig
from raven.utils.helpers import split_message

MAX_MESSAGE_LEN = 4000
_ALBUM_WINDOW_S = 0.6

# Telegram media APIs keyed by the kind we resolve from a file extension.
_EXT_KIND = {
    "jpg": "photo",
    "jpeg": "photo",
    "png": "photo",
    "gif": "photo",
    "webp": "photo",
    "ogg": "voice",
    "mp3": "audio",
    "m4a": "audio",
    "wav": "audio",
    "aac": "audio",
}
# Inbound: map a Telegram media object kind to the saved-file extension.
_INBOUND_MIME_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "audio/ogg": ".ogg",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
}
_INBOUND_KIND_EXT = {"image": ".jpg", "voice": ".ogg", "audio": ".mp3"}


def _outbound_kind(path: str) -> str:
    ext = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    return _EXT_KIND.get(ext, "document")


def _inbound_ext(kind: str, mime: str | None, filename: str | None) -> str:
    if mime and mime in _INBOUND_MIME_EXT:
        return _INBOUND_MIME_EXT[mime]
    if kind in _INBOUND_KIND_EXT:
        return _INBOUND_KIND_EXT[kind]
    return "".join(Path(filename).suffixes) if filename else ""


def _display_width(s: str) -> int:
    return sum(2 if unicodedata.east_asian_width(c) in ("W", "F") else 1 for c in s)


def _plain(s: str) -> str:
    """Drop inline markdown emphasis so table cells render cleanly."""
    for pat in (r"\*\*(.+?)\*\*", r"__(.+?)__", r"~~(.+?)~~", r"`([^`]+)`"):
        s = re.sub(pat, r"\1", s)
    return s.strip()


def _table_to_text(rows_src: list[str]) -> str | None:
    """Render a markdown pipe-table as a width-aligned monospace block.

    Returns ``None`` when the lines aren't a real table (no separator row),
    so the caller can leave them untouched.
    """
    rows: list[list[str]] = []
    saw_separator = False
    for line in rows_src:
        cells = [_plain(c) for c in line.strip().strip("|").split("|")]
        if cells and all(re.match(r"^:?-+:?$", c) for c in cells if c):
            saw_separator = True
            continue
        rows.append(cells)
    if not rows or not saw_separator:
        return None

    ncols = max(len(r) for r in rows)
    for r in rows:
        r += [""] * (ncols - len(r))
    widths = [max(_display_width(r[c]) for r in rows) for c in range(ncols)]

    def fmt(cells: list[str]) -> str:
        return "  ".join(c + " " * (w - _display_width(c)) for c, w in zip(cells, widths))

    out = [fmt(rows[0]), "  ".join("─" * w for w in widths)]
    out += [fmt(r) for r in rows[1:]]
    return "\n".join(out)


def _markdown_to_html(text: str) -> str:
    """Render a markdown subset to the HTML flavour Telegram accepts.

    Code spans/blocks and tables are pulled out first so their contents
    survive escaping and inline-emphasis passes, then re-inserted last.
    """
    if not text:
        return ""

    vault: list[str] = []

    def stash(payload: str) -> str:
        vault.append(payload)
        return f"\x00{len(vault) - 1}\x00"

    # Fenced code blocks -> <pre><code>; tables -> monospace <pre>.
    text = re.sub(
        r"```[\w]*\n?([\s\S]*?)```",
        lambda m: stash(f"<pre><code>{html.escape(m.group(1))}</code></pre>"),
        text,
    )
    lines, merged = text.split("\n"), []
    i = 0
    while i < len(lines):
        if re.match(r"^\s*\|.+\|", lines[i]):
            block = []
            while i < len(lines) and re.match(r"^\s*\|.+\|", lines[i]):
                block.append(lines[i])
                i += 1
            rendered = _table_to_text(block)
            merged.append(
                stash(f"<pre>{html.escape(rendered)}</pre>") if rendered else "\n".join(block)
            )
        else:
            merged.append(lines[i])
            i += 1
    text = "\n".join(merged)

    # Inline code -> <code>.
    text = re.sub(r"`([^`]+)`", lambda m: stash(f"<code>{html.escape(m.group(1))}</code>"), text)

    # Strip block markers Telegram has no tag for.
    text = re.sub(r"^#{1,6}\s+(.+)$", r"\1", text, flags=re.MULTILINE)
    text = re.sub(r"^>\s*(.*)$", r"\1", text, flags=re.MULTILINE)

    text = html.escape(text)

    # Links before emphasis so bracket/paren content isn't mangled.
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r'<a href="\2">\1</a>', text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    text = re.sub(r"__(.+?)__", r"<b>\1</b>", text)
    text = re.sub(r"(?<![a-zA-Z0-9])_([^_]+)_(?![a-zA-Z0-9])", r"<i>\1</i>", text)
    text = re.sub(r"~~(.+?)~~", r"<s>\1</s>", text)
    text = re.sub(r"^[-*]\s+", "• ", text, flags=re.MULTILINE)

    for idx, payload in enumerate(vault):
        text = text.replace(f"\x00{idx}\x00", payload)
    return text


class TelegramChannel(ChannelBase):
    """Telegram bot over long polling — no webhook / public IP needed."""

    config: TelegramConfig
    name = "telegram"
    display_name = "Telegram"

    BOT_COMMANDS = [
        BotCommand("start", "Start the bot"),
        BotCommand("new", "Start a new conversation"),
        BotCommand("stop", "Stop the current task"),
        BotCommand("help", "Show available commands"),
        BotCommand("restart", "Restart the bot"),
    ]

    def __init__(self, config: TelegramConfig):
        super().__init__(config)
        self._stop_event = asyncio.Event()
        self._app: Application | None = None
        self._typing: dict[str, asyncio.Task] = {}
        self._group_buffers: dict[str, dict] = {}
        self._group_tasks: dict[str, asyncio.Task] = {}
        self._thread_of: dict[tuple[str, int], int] = {}
        self._bot_id: int | None = None
        self._bot_username: str | None = None

    # ── access ────────────────────────────────────────────────────────

    def is_allowed(self, sender_id: str) -> bool:
        """Default allowlist check, plus the ``<id>|<username>`` form so an
        allow_from list of either the numeric id or the @username matches.
        Overrides ChannelBase.is_allowed; the injected Intake picks it up."""
        from raven.auth.allowlist import is_allowed as _base

        allow = getattr(self.config, "allow_from", []) or []
        if _base(self.name, sender_id, allow):
            return True
        if not allow or "*" in allow:
            return False
        sid, sep, username = str(sender_id).partition("|")
        if not sep or not sid.isdigit() or not username:
            return False
        return sid in allow or username in allow

    # ── lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        if not self.config.token:
            logger.error("Telegram bot token not configured")
            return
        self._running = True
        self._stop_event = asyncio.Event()  # fresh per start (restart-safe)

        request = HTTPXRequest(
            connection_pool_size=16,
            pool_timeout=5.0,
            connect_timeout=30.0,
            read_timeout=30.0,
            proxy=self.config.proxy or None,
        )
        self._app = (
            Application.builder()
            .token(self.config.token)
            .request(request)
            .get_updates_request(request)
            .build()
        )
        self._app.add_error_handler(self._on_error)
        self._app.add_handler(CommandHandler("start", self._on_start))
        self._app.add_handler(CommandHandler("help", self._on_help))
        for cmd in ("new", "stop", "restart"):
            self._app.add_handler(CommandHandler(cmd, self._on_command))
        self._app.add_handler(
            MessageHandler(
                (
                    filters.TEXT
                    | filters.PHOTO
                    | filters.VOICE
                    | filters.AUDIO
                    | filters.Document.ALL
                )
                & ~filters.COMMAND,
                self._on_message,
            )
        )

        logger.info("Starting Telegram bot (polling mode)...")
        await self._app.initialize()
        await self._app.start()

        me = await self._app.bot.get_me()
        self._bot_id, self._bot_username = me.id, me.username
        logger.info("Telegram bot @{} connected", me.username)
        try:
            await self._app.bot.set_my_commands(self.BOT_COMMANDS)
        except Exception as e:
            logger.warning("Failed to register bot commands: {}", e)

        await self._app.updater.start_polling(
            allowed_updates=["message"], drop_pending_updates=True
        )
        await self._stop_event.wait()

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        # Cancel AND await the helper tasks so none die unobserved after stop.
        pending = [self._typing.pop(c) for c in list(self._typing)]
        pending += list(self._group_tasks.values())
        self._group_tasks.clear()
        self._group_buffers.clear()
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)
        if self._app:
            logger.info("Stopping Telegram bot...")
            await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            self._app = None

    # ── outbound ──────────────────────────────────────────────────────

    async def send(self, chat_id: str, content: str, media: list[str] | None = None) -> None:
        if not self._app:
            logger.warning("Telegram bot not running")
            return
        self._stop_typing(chat_id)
        try:
            chat_id_int = int(chat_id)
        except ValueError:
            logger.error("Invalid chat_id: {}", chat_id)
            return

        for path in media or []:
            await self._send_media(chat_id_int, path)

        if content and content != "[empty message]":
            for chunk in split_message(content, MAX_MESSAGE_LEN):
                await self._send_streamed(chat_id_int, chunk)

    async def _send_media(self, chat_id, path) -> None:
        kind = _outbound_kind(path)
        senders = {
            "photo": self._app.bot.send_photo,
            "voice": self._app.bot.send_voice,
            "audio": self._app.bot.send_audio,
        }
        send = senders.get(kind, self._app.bot.send_document)
        arg = kind if kind in ("photo", "voice", "audio") else "document"
        try:
            with open(path, "rb") as fh:
                await send(chat_id=chat_id, **{arg: fh})
        except Exception as e:
            logger.error("Failed to send media {}: {}", path, e)
            await self._app.bot.send_message(
                chat_id=chat_id,
                text=f"[Failed to send: {path.rsplit('/', 1)[-1]}]",
            )

    async def _send_text(self, chat_id, text) -> None:
        try:
            await self._app.bot.send_message(
                chat_id=chat_id,
                text=_markdown_to_html(text),
                parse_mode="HTML",
            )
        except Exception as e:
            logger.warning("HTML send failed, retrying as plain text: {}", e)
            try:
                await self._app.bot.send_message(chat_id=chat_id, text=text)
            except Exception as e2:
                logger.error("Error sending Telegram message: {}", e2)

    async def _send_streamed(self, chat_id, text) -> None:
        """Animate the reply via the draft API, then persist the final text."""
        draft_id = int(time.time() * 1000) % (2**31)
        try:
            step = max(len(text) // 8, 40)
            for cut in range(step, len(text), step):
                await self._app.bot.send_message_draft(
                    chat_id=chat_id, draft_id=draft_id, text=text[:cut]
                )
                await asyncio.sleep(0.04)
            await self._app.bot.send_message_draft(chat_id=chat_id, draft_id=draft_id, text=text)
            await asyncio.sleep(0.15)
        except Exception:
            pass
        await self._send_text(chat_id, text)

    # ── commands ──────────────────────────────────────────────────────

    async def _on_start(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message and update.effective_user:
            await update.message.reply_text(
                f"👋 Hi {update.effective_user.first_name}! I'm raven.\n\n"
                "Send me a message and I'll respond!\n"
                "Type /help to see available commands."
            )

    async def _on_help(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if update.message:
            await update.message.reply_text(
                "🐦‍⬛ Raven commands:\n"
                "/new — Start a new conversation\n"
                "/stop — Stop the current task\n"
                "/help — Show available commands"
            )

    async def _on_command(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        """Forward /new /stop /restart through intake for AgentLoop to handle."""
        if not update.message or not update.effective_user:
            return
        self._remember_thread(update.message)
        await self.intake.publish(
            sender_id=self._sender_id(update.effective_user),
            chat_id=str(update.message.chat_id),
            content=update.message.text,
            metadata=self._metadata(update.message, update.effective_user),
            session_key=self._topic_session_key(update.message),
        )

    # ── inbound ───────────────────────────────────────────────────────

    async def _on_message(self, update: Update, _ctx: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message or not update.effective_user:
            return
        message, user = update.message, update.effective_user
        self._remember_thread(message)
        if not await self._addressed_to_bot(message):
            return
        if not self.is_allowed(self._sender_id(user)):  # reject before media download / typing
            return

        parts: list[str] = []
        if message.text:
            parts.append(message.text)
        if message.caption:
            parts.append(message.caption)

        media_paths: list[str] = []
        media_obj, kind = self._pick_media(message)
        if media_obj and self._app:
            saved = await self._download(media_obj, kind)
            if saved is None:
                parts.append(f"[{kind}: download failed]")
            else:
                media_paths.append(saved)
                if kind in ("voice", "audio"):
                    text = await transcribe_audio(
                        saved, self.transcription_api_key, channel=self.name
                    )
                    parts.append(f"[transcription: {text}]" if text else f"[{kind}: {saved}]")
                else:
                    parts.append(f"[{kind}: {saved}]")

        content = "\n".join(parts) if parts else "[empty message]"
        chat_id = str(message.chat_id)
        metadata = self._metadata(message, user)
        session_key = self._topic_session_key(message)

        group_id = getattr(message, "media_group_id", None)
        if group_id:
            self._buffer_group(group_id, chat_id, user, content, media_paths, metadata, session_key)
            return

        self._start_typing(chat_id)
        await self.intake.publish(
            sender_id=self._sender_id(user),
            chat_id=chat_id,
            content=content,
            media=media_paths,
            metadata=metadata,
            session_key=session_key,
        )

    @staticmethod
    def _pick_media(message):
        if message.photo:
            return message.photo[-1], "image"
        if message.voice:
            return message.voice, "voice"
        if message.audio:
            return message.audio, "audio"
        if message.document:
            return message.document, "file"
        return None, None

    async def _download(self, media_obj, kind: str) -> str | None:
        try:
            tg_file = await self._app.bot.get_file(media_obj.file_id)
            ext = _inbound_ext(
                kind,
                getattr(media_obj, "mime_type", None),
                getattr(media_obj, "file_name", None),
            )
            dest = get_media_dir("telegram") / f"{media_obj.file_id[:16]}{ext}"
            await tg_file.download_to_drive(str(dest))
            return str(dest)
        except Exception as e:
            logger.error("Failed to download media: {}", e)
            return None

    def _buffer_group(self, group_id, chat_id, user, content, media, metadata, session_key) -> None:
        """Telegram splits an album into one update per item; collect them
        for a short window and forward as a single turn."""
        key = f"{chat_id}:{group_id}"
        buf = self._group_buffers.get(key)
        if buf is None:
            buf = self._group_buffers[key] = {
                "sender_id": self._sender_id(user),
                "chat_id": chat_id,
                "contents": [],
                "media": [],
                "metadata": metadata,
                "session_key": session_key,
            }
            self._start_typing(chat_id)
        if content and content != "[empty message]":
            buf["contents"].append(content)
        buf["media"].extend(media)
        if key not in self._group_tasks:
            self._group_tasks[key] = asyncio.create_task(self._flush_group(key))

    async def _flush_group(self, key: str) -> None:
        await asyncio.sleep(_ALBUM_WINDOW_S)
        # Drop the task handle BEFORE draining the buffer: a straggler arriving
        # while publish() awaits must be able to schedule a fresh flush. With
        # the old order (buffer popped first, task popped in finally) the
        # straggler rebuilt the buffer, saw the stale task key, scheduled
        # nothing — and since an album's group_id never repeats, that buffer
        # leaked and its items were silently lost.
        self._group_tasks.pop(key, None)
        buf = self._group_buffers.pop(key, None)
        if not buf:
            return
        await self.intake.publish(
            sender_id=buf["sender_id"],
            chat_id=buf["chat_id"],
            content="\n".join(buf["contents"]) or "[empty message]",
            media=list(dict.fromkeys(buf["media"])),
            metadata=buf["metadata"],
            session_key=buf["session_key"],
        )

    # ── group addressing / metadata ───────────────────────────────────

    async def _addressed_to_bot(self, message) -> bool:
        """In groups, only respond when policy is open, the bot is @mentioned,
        or the message replies to one of the bot's own messages."""
        if message.chat.type == "private" or self.config.group_policy == "open":
            return True
        if self._bot_username:
            if self._mentions_bot(message.text or "", getattr(message, "entities", None)):
                return True
            if self._mentions_bot(
                message.caption or "", getattr(message, "caption_entities", None)
            ):
                return True
        replied = getattr(getattr(message, "reply_to_message", None), "from_user", None)
        return bool(self._bot_id and replied and replied.id == self._bot_id)

    def _mentions_bot(self, text: str, entities) -> bool:
        handle = f"@{self._bot_username}".lower()
        for entity in entities or []:
            etype = getattr(entity, "type", None)
            if etype == "text_mention":
                u = getattr(entity, "user", None)
                if u and self._bot_id and getattr(u, "id", None) == self._bot_id:
                    return True
            elif etype == "mention":
                off, length = getattr(entity, "offset", None), getattr(entity, "length", None)
                if (
                    off is not None
                    and length is not None
                    and text[off : off + length].lower() == handle
                ):
                    return True
        return handle in text.lower()

    def _remember_thread(self, message) -> None:
        thread_id = getattr(message, "message_thread_id", None)
        if thread_id is None:
            return
        self._thread_of[(str(message.chat_id), message.message_id)] = thread_id
        if len(self._thread_of) > 1000:
            self._thread_of.pop(next(iter(self._thread_of)))

    @staticmethod
    def _sender_id(user) -> str:
        return f"{user.id}|{user.username}" if user.username else str(user.id)

    @staticmethod
    def _topic_session_key(message) -> str | None:
        thread_id = getattr(message, "message_thread_id", None)
        if message.chat.type == "private" or thread_id is None:
            return None
        return f"telegram:{message.chat_id}:topic:{thread_id}"

    @staticmethod
    def _metadata(message, user) -> dict:
        return {
            "message_id": message.message_id,
            "user_id": user.id,
            "username": user.username,
            "first_name": user.first_name,
            "is_group": message.chat.type != "private",
            "message_thread_id": getattr(message, "message_thread_id", None),
            "is_forum": bool(getattr(message.chat, "is_forum", False)),
        }

    # ── typing indicator ──────────────────────────────────────────────

    def _start_typing(self, chat_id: str) -> None:
        self._stop_typing(chat_id)
        self._typing[chat_id] = asyncio.create_task(self._typing_loop(chat_id))

    def _stop_typing(self, chat_id: str) -> None:
        task = self._typing.pop(chat_id, None)
        if task and not task.done():
            task.cancel()

    async def _typing_loop(self, chat_id: str) -> None:
        try:
            while self._app:
                await self._app.bot.send_chat_action(chat_id=int(chat_id), action="typing")
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            logger.debug("Typing indicator stopped for {}: {}", chat_id, e)

    async def _on_error(self, _update: object, ctx: ContextTypes.DEFAULT_TYPE) -> None:
        logger.error("Telegram error: {}", ctx.error)
