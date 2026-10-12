"""Register finished output files for delivery on every Raven client."""

from __future__ import annotations

import hashlib
import mimetypes
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

from raven.agent import workdir
from raven.agent.tools.deliverables import DeliverableStore
from raven.agent.tools.filesystem import _with_current_root, resolve_path
from raven.contracts.tool import Tool
from raven.ops import remote_path
from raven.ops.remote_path import RemotePath

_last_stamp = 0


def _stamp() -> int:
    """Epoch milliseconds, and never the same number twice in one process.

    The page ranks deliveries against each other and had nothing to rank them by:
    the message's own stamp is naive local wall time and only exists once the turn
    is stored, so a delivery still streaming had no time at all and a stored one
    could not be compared against a browser's clock. One field written here rides
    the same manifest down both paths -- the live event and the replayed message
    carry the identical object -- so both sides read one clock.

    A clock alone is not enough: `time.time()` on an ordinary box repeats inside a
    millisecond, and fifty sequential calls through this tool were measured
    producing thirty-five distinct values. Two deliveries sharing a stamp leave
    the reader ranking them by the order the page happened to paint them, which is
    the very thing this field exists to stop. So the value only ever moves
    forward: it follows the wall clock, and steps by one when the clock has not
    moved. The drift that introduces is a millisecond per collision, against a
    number nothing measures durations from.

    Module level rather than per instance: two tools in one process are two lanes
    of one producer, and their deliveries have one order.
    """
    global _last_stamp
    now = int(time.time() * 1000)
    _last_stamp = now if now > _last_stamp else _last_stamp + 1
    return _last_stamp


@dataclass(frozen=True)
class _DeliverCtx:
    channel: str
    chat_id: str
    session_key: str


def _on_machine(raw: str) -> RemotePath | str | None:
    """The machine ``raw`` names, the sentence for a remote spelling that names none, or ``None`` for a path here."""
    try:
        return remote_path.target(raw)
    except remote_path.RemotePathError as exc:
        return str(exc)


async def _brought_back(where: RemotePath, workspace: Path | None) -> tuple[Path | None, str]:
    """``where``'s file, fetched from its machine to ``<workspace>/pulled/<machine>/<key>/<name>``; or why it was not.

    Only a file under the directories this Raven's agents work in on that
    machine is fetched here, since this tool asks nothing: a delivery is not
    a way around the read check, and what another conversation was allowed
    to read is no evidence for this one. A file elsewhere is read there. One
    over :data:`PULL_FILE_BUDGET` stays, and is named with its size. The file
    lands under a key of its path on the machine, by its name
    (:func:`_landing`), so two paths never land on each other, however this
    computer's filesystem reads their names, and no name reaches above the
    delivery directory.
    """
    pool = remote_path.nodes()
    label = remote_path._label(where._row)
    if pool is None:
        return None, f"files on machine {label} cannot be brought back by this process"
    try:
        home = await pool.home(where.machine)
        place = where.absolute(home)
        roots = remote_path.reading_roots(where)
        if not any(remote_path.within(place, remote_path.absolute(root, home)) for root in roots):
            return None, (
                f"{where.display(place)} is outside the directories this Raven's agents work in on machine "
                f"{label}; read it there instead"
            )
        fence = [remote_path.fence("read_file", where, place, home)]
        info = await pool.stat(where.machine, place, roots=fence)
        if info.get("kind") != "file":
            return None, f"not found or not a regular file on machine {label}"
        size = int(info.get("size") or 0)
        if size > PULL_FILE_BUDGET:
            return None, (
                f"{_human_size(size)} is over the {_human_size(PULL_FILE_BUDGET)} "
                f"limit for a file brought back from machine {label}; read it there instead"
            )
        try:
            local = _landing(workspace, where.machine, place)
        except ValueError:
            return None, (
                f"{where.display(place)} has a name this computer cannot keep under its delivery directory; "
                "read it there instead"
            )
        await pool.pull_file(where.machine, place, local, roots=fence)
    except remote_path.NodeCallError as exc:
        return None, str(exc)
    except OSError as exc:
        return None, f"could not write the file brought back: {exc.strerror or exc}"
    return local, ""


#: The most a file fetched from a registered machine for a delivery may weigh; a bigger one is read there.
PULL_FILE_BUDGET = 20 * 1024 * 1024

#: What a fetched file's name may not hold as a name on this computer: the
#: other platform's separator, what Windows reserves in a name,
#: a control character. A Linux file name may hold any of them, and ``..\``
#: inside one, read by a Windows host as two steps up, put a fetched file
#: above the workspace. Each is written as its
#: percent-code, the way a URL writes one -- the percent sign itself too, so
#: two names that differ only by one written out never land as one
#: (``a\b`` and ``a%5Cb``).
_NOT_A_NAME_HERE = re.compile(r'[%\\<>:"|?*\x00-\x1f]')


def _local_name(part: str) -> str:
    """A name on the machine, as one name on this computer's filesystem."""
    if part in (".", ".."):
        return part.replace(".", "%2E")
    return _NOT_A_NAME_HERE.sub(lambda m: f"%{ord(m.group(0)):02X}", part)


def _landing(workspace: Path | None, machine: str, place: str) -> Path:
    """Where ``place`` on ``machine`` lands: ``pulled/<machine>/<key>/<name>``, the key a digest of the path there.

    The key keeps apart two paths that this computer's filesystem would read
    as one name -- ``Report.txt`` and ``report.txt`` on Windows or macOS,
    ``report.txt`` and ``report.txt.`` on Windows -- as well as two files of
    one name from two directories; the name, written as one this computer
    keeps (:func:`_local_name`), stays readable beside it. Checked before
    anything is made or written: a landing that resolves outside its
    machine's delivery directory is refused, whatever the name did to get
    there.
    """
    pulled = (workspace or Path.cwd()) / "pulled" / _local_name(machine)
    key = hashlib.sha256(f"{machine}\0{place}".encode()).hexdigest()[:16]
    name = _local_name(PurePosixPath(place).name)
    local = pulled / key / name
    if not name or local.resolve() == pulled.resolve() or not local.resolve().is_relative_to(pulled.resolve()):
        raise ValueError(f"{place} would not land under {pulled}")
    return local


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
            elsewhere = _on_machine(raw)
            if isinstance(elsewhere, str):
                invalid.append({"path": raw, "reason": elsewhere})
                continue
            if elsewhere is not None:
                # A file on a registered machine is brought back first, within
                # the budget, and delivered from where it landed.
                brought, reason = await _brought_back(elsewhere, self._workspace)
                if brought is None:
                    invalid.append({"path": raw, "reason": reason})
                    continue
                resolved = brought
            else:
                try:
                    bound = workdir.current()
                    resolved = resolve_path(
                        raw, bound or self._workspace, _with_current_root(self._allowed_dirs, bound)
                    )
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
                "raven_delivery": {
                    "message": message,
                    "files": delivered,
                    "invalid": invalid,
                    # When this delivery happened, once for the manifest because
                    # its files were handed over together. See `_stamp`.
                    "delivered_at": _stamp(),
                }
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
