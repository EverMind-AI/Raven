"""How a turn's attachments travel to a sub-agent.

A sub-agent never receives bytes: the host already holds every attachment as a
file, so what crosses is a path the agent's own tools can open. Two spellings,
one per transport. An ACP agent takes ``resource_link`` blocks beside the text,
the block an editor sends for an @-mentioned file, which the agent renders as a
line naming the path; a text-only transport (an in-process loop, an OpenAI-style
endpoint, a CLI) gets the same facts appended to the task as a note.

Absolute paths on purpose. A direct chat's text carries the page's own note with
a path relative to the *host's* agent home (``uploads/x.pptx``), which resolves to
nothing from the sub-agent's working directory: a deck agent handed that spelling
reported the file as not there and fell back to a bundled template. Handed the
absolute path beside it, the same agent still tried the note's spelling first, so
the note is rewritten too: each of its bullets that names an attachment relative
to the host's home is replaced by the absolute path (``retarget_note``), and a
link is named by that same relative spelling so the two are visibly one file.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from raven.spine.message import Media

ATTACHMENTS_NOTE = "[attachments, by absolute path]"
UNDELIVERABLE_NOTE = "[attachments were sent but not handed over: this agent cannot read local files]"


def _label(path: Path, root: Path | None) -> str:
    """How the host's own note spells this file: relative to the agent home when it is under it."""
    if root is not None:
        try:
            return path.resolve().relative_to(Path(root).resolve()).as_posix()
        except ValueError:
            pass
    return path.name


def attachment_blocks(media: Iterable[Media], *, root: Path | None = None) -> list[dict[str, Any]]:
    """ACP prompt blocks for the attachments that are files on disk.

    ``root`` is the host's agent home, under which the page deposits uploads; a
    link is named by the path relative to it so it reads as the note's bullet.
    """
    blocks: list[dict[str, Any]] = []
    for item in media:
        path = Path(item.path)
        if not path.is_file():
            continue
        block: dict[str, Any] = {"type": "resource_link", "uri": path.resolve().as_uri(), "name": _label(path, root)}
        if item.mime and item.mime != "application/octet-stream":
            block["mimeType"] = item.mime
        blocks.append(block)
    return blocks


def with_attachment_note(text: str, media: Iterable[Media]) -> str:
    """The task with its attachments named at the end, for a transport that takes text only."""
    lines = [f"- {Path(item.path).resolve()}" for item in media if Path(item.path).is_file()]
    if not lines:
        return text
    return f"{text}\n\n{ATTACHMENTS_NOTE}\n" + "\n".join(lines)


def with_undeliverable_note(text: str, media: Iterable[Media]) -> str:
    """The task told that its attachments stayed behind, by name, so nobody guesses."""
    names = [Path(item.path).name for item in media]
    if not names:
        return text
    return f"{text}\n\n{UNDELIVERABLE_NOTE}\n" + "\n".join(f"- {name}" for name in names)


def retarget_note(text: str, media: Iterable[Media], root: Path | None) -> str:
    """The text with each note bullet that names an attachment relative to ``root``
    replaced by the absolute path.

    Only a whole bullet line that is exactly the relative spelling is touched, so
    the user's own words stay theirs; the bullets are the page's, written for a
    reader whose working directory is the host's, which a sub-agent's is not.
    """
    if root is None:
        return text
    lines = text.split("\n")
    for item in media:
        path = Path(item.path)
        if not path.is_file():
            continue
        label = _label(path, root)
        if label == path.name:
            continue
        lines = [f"- {path.resolve()}" if line.strip() == f"- {label}" else line for line in lines]
    return "\n".join(lines)
