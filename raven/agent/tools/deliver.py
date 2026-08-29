"""Register finished output files for delivery on every Raven client."""

from __future__ import annotations

import mimetypes
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from raven.agent import workdir
from raven.agent.tools._deliverables import DeliverableStore
from raven.agent.tools.filesystem import _resolve_path, _with_current_root
from raven.contracts.tool import Tool


@dataclass(frozen=True)
class _DeliverCtx:
    channel: str
    chat_id: str
    session_key: str


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


class DeliverFilesTool(Tool):
    """Register output files as user-downloadable deliverables.

    The manifest travels back to the turn stream via ``take_metadata`` rather
    than the return value, which the model reads: bytes and UI detail must not
    enter the model's context.
    """

    def __init__(
        self,
        store: DeliverableStore,
        *,
        workspace: Path | None = None,
        allowed_dirs: tuple[Path, ...] = (),
    ) -> None:
        self._store = store
        self._workspace = workspace
        self._allowed_dirs = allowed_dirs
        self._ctx: ContextVar[_DeliverCtx | None] = ContextVar("deliver_files_ctx", default=None)
        self._default = _DeliverCtx(channel="cli", chat_id="direct", session_key="cli:direct")
        # Written by execute (which may run in a child context) and popped by
        # take_metadata in the loop's own task, so the handoff cannot rely on a
        # ContextVar write propagating upward. Keyed by session so concurrent
        # turns never read each other's manifest.
        self._pending: dict[str, dict[str, Any]] = {}

    def _cur(self) -> _DeliverCtx:
        return self._ctx.get() or self._default

    def set_context(self, channel: str, chat_id: str, session_key: str) -> None:
        """Set this turn's routing context (turn-local)."""
        self._ctx.set(_DeliverCtx(channel=channel, chat_id=chat_id, session_key=session_key))

    def take_metadata(self) -> dict[str, Any] | None:
        return self._pending.pop(self._cur().session_key, None)

    @property
    def name(self) -> str:
        return "deliver_files"

    @property
    def description(self) -> str:
        # Stated as the only route because the observed failure is a reply that
        # hands over a path or a self-composed URL instead of calling the tool.
        # Neither resolves to anything the user can open.
        return (
            "Deliver finished output files to the user -- the only way to hand one over. "
            "Never write a path or a link instead; neither reaches the user. Call this once "
            "the file is on disk, for final artifacts only, not scratch files. Raven renders "
            "the resulting delivery in the form supported by the current client or channel."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "minItems": 1,
                    "description": "The files to deliver.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Path of the file to deliver, relative to the workspace or absolute.",
                            },
                            "title": {
                                "type": "string",
                                "description": "Optional human-friendly title shown in the UI.",
                            },
                            "description": {
                                "type": "string",
                                "description": "Optional one-line note about the file.",
                            },
                        },
                        "required": ["path"],
                    },
                },
                "message": {
                    "type": "string",
                    "description": "Optional note accompanying the whole delivery.",
                },
            },
            "required": ["files"],
        }

    async def execute(
        self, files: list[dict[str, Any]] | None = None, message: str | None = None, **kwargs: Any
    ) -> str:
        ctx = self._cur()
        if not files:
            return "Error: deliver_files needs at least one file."

        delivered: list[dict[str, Any]] = []
        invalid: list[dict[str, str]] = []
        seen: set[str] = set()

        for entry in files:
            entry = entry if isinstance(entry, dict) else {}
            raw = str(entry.get("path") or "")
            if not raw:
                invalid.append({"path": raw, "reason": "empty path"})
                continue
            try:
                bound = workdir.current()
                resolved = _resolve_path(raw, bound or self._workspace, _with_current_root(self._allowed_dirs, bound))
            except PermissionError as exc:
                invalid.append({"path": raw, "reason": str(exc)})
                continue
            except (OSError, ValueError, RuntimeError) as exc:
                invalid.append({"path": raw, "reason": f"cannot resolve path: {exc}"})
                continue

            key = str(resolved)
            if key in seen:
                continue
            seen.add(key)

            if not resolved.is_file():
                invalid.append({"path": raw, "reason": "not found or not a regular file"})
                continue

            record = self._store.register(
                path=key,
                name=resolved.name,
                media_type=mimetypes.guess_type(resolved.name)[0] or "application/octet-stream",
                size=resolved.stat().st_size,
                conversation=ctx.session_key,
                # Stored, not only sent: the manifest reaches a client once, and
                # the registry is what answers for this delivery afterwards.
                title=str(entry.get("title") or ""),
                description=str(entry.get("description") or ""),
            )
            delivered.append(
                {
                    "path": key,
                    "name": record.name,
                    "title": entry.get("title"),
                    "description": entry.get("description"),
                    "size": record.size,
                    "media_type": record.media_type,
                    "token": record.token,
                    "download_path": f"/files/download?token={quote(record.token)}",
                }
            )

        if delivered:
            self._pending[ctx.session_key] = {
                "raven_delivery": {"message": message, "files": delivered, "invalid": invalid}
            }
        else:
            # A manifest nobody collected (the loop skipped take_metadata) must not
            # outlive its call: leaving it here would let this failed delivery pop
            # the earlier one and report another call's files as its own.
            self._pending.pop(ctx.session_key, None)

        return self._summary(delivered, invalid)

    @staticmethod
    def _summary(delivered: list[dict[str, Any]], invalid: list[dict[str, str]]) -> str:
        failures = ", ".join(f"{item['path']} ({item['reason']})" for item in invalid)
        if not delivered:
            return f"Error: delivered no files. Could not deliver: {failures}."
        listing = ", ".join(f"{item['name']} ({_human_size(item['size'])})" for item in delivered)
        text = f"Delivered {len(delivered)} file(s): {listing}."
        if invalid:
            text += f" Could not deliver: {failures}."
        return text
