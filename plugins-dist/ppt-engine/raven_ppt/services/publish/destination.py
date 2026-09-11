"""Where the user asked for the deck, stated once and kept for the deck's life.

A request names the place as often as it names the subject: "put the .pptx in
~/deliverables/ as intro.pptx and tell me the path". The publish step writes to
`out/` under the workspace and nowhere else, so the only way that request was
ever met was a `cp` the model ran after the build -- a copy no record knew of,
which the turn's verification then called out as not the deliverable. The user
asked for a path and the engine contradicted the one reply that gave it.

So the place is stated to the engine, the way a palette is: once, through a tool
argument, and kept on disk under the deck's state rather than re-said per build.
Every build that publishes after that copies the same bytes there (see
`deliver.deliver`), and a revision the user asks for a week later lands on top of
the first delivery without the author remembering anything. Stating it again
replaces it; nothing here ever deletes a file the user has.

What is checked is what is not a judgement: the path is absolute, it names a
`.pptx` (or a directory, which takes the deck's own file name), its directory can
be made, and it is not one of the engine's own -- `deck/` holds builds no gate has
passed and `out/` is written by the publish step already, so a destination inside
either would either deliver an unchecked file or deliver the same file twice.

The PDF preview that rides beside the deck is a second path, and it is derived
here rather than after the checks: it was once taken from the destination once
that had passed, so it went out through none of them and replaced whatever stood
at that name. Both paths come out of this module together
(`sidecar_for` -> `Destination`), and the sidecar carries the one further rule the
deliverable does not need -- `free`, which is false when anything is already at
that name. The deliverable is
what the user asked for and replacing it is the request; the preview is this
engine's convenience, and there is nothing on the record to prove an existing
`intro.pdf` was written by us rather than by the user, so where the name is taken
the preview is not written at all and the reply says where it stayed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

DESTINATION_FILE = "delivery.json"


class DestinationError(ValueError):
    """A destination the publish step cannot promise to write."""


@dataclass(frozen=True)
class Destination:
    """The deliverable's path, and the sidecar's path with whether it may be written.

    `sidecar` is always the `.pdf` beside `deck`, so a caller can name it in a
    message whether or not it may write it; `free` is what says which. Checked
    here, in the one place that resolves the destination, because a path derived
    after the checks is a path that had none.
    """

    deck: Path
    sidecar: Path
    free: bool


def destination_path(project) -> Path:
    return project.state_dir / DESTINATION_FILE


def as_destination(given: object, project, *, default_name: str) -> Path:
    """`given` as the file the deck is to be delivered to, or DestinationError.

    A directory -- one that exists, or one written with a trailing slash -- takes
    `default_name`, which is the name the publish step gives the deck under `out/`.
    Anything else has to name the `.pptx` itself: a path with no suffix that does not
    exist could be either, and guessing would deliver a file called `deliverables`
    or a directory called `intro.pptx`.
    """
    text = str(given or "").strip() if not isinstance(given, Path) else str(given)
    if not text:
        raise DestinationError("the destination is empty; give the absolute path of the .pptx to deliver")
    path = Path(text)
    if not path.is_absolute():
        raise DestinationError(
            f"{text} is not an absolute path. Write the destination in full, from the root: "
            "/home/user/decks/intro.pptx, or a directory ending in / to keep the deck's own name"
        )
    if text.endswith(("/", "\\")) or path.is_dir():
        path = path / default_name
    if path.suffix.lower() != ".pptx":
        raise DestinationError(
            f"{text} is not a .pptx path. Name the file with its .pptx suffix, or end a directory with / "
            f"and the deck is delivered there as {default_name}"
        )
    resolved = path.parent.resolve() / path.name
    for fence, why in (
        (project.root, "holds builds no gate has passed"),
        (project.exports_dir, "is where the publish step already writes"),
    ):
        fenced = fence.resolve()
        if resolved == fenced or fenced in resolved.parents:
            raise DestinationError(
                f"{text} is inside the engine's own {fence.name}/, which {why}. Name a directory of the "
                "user's -- the one the task asked for -- and the deck is written there beside out/"
            )
    try:
        resolved.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DestinationError(f"the directory for {text} cannot be created: {exc.strerror or exc}") from None
    if resolved.is_dir():
        raise DestinationError(f"{text} is a directory, not a file; name the .pptx to write inside it")
    return resolved


def sidecar_for(deck: Path) -> Destination:
    """Both paths of one delivery and whether the preview's name is free.

    Asked here rather than at the write, and asked of the name rather than of the
    bytes: a `.pdf` that looks like one of ours is still one we cannot prove we
    wrote, and reading it to guess would be the judgement this module refuses. The
    deliverable is the file the user named and replacing it is the request; the
    preview is this engine's convenience, so where its name is taken it is not
    written and the caller says where it stayed.

    Taken at delivery time, not when the destination is stated: a name free a week
    ago is not a promise about now.
    """
    sidecar = deck.with_suffix(".pdf")
    return Destination(deck=deck, sidecar=sidecar, free=not sidecar.exists())


def read_destination(project) -> Path | None:
    """The destination stated for this deck, or None where none was.

    Read as stated rather than re-validated: the checks ran when it was written, and
    a directory removed since is made again when the deck is delivered.
    """
    target = destination_path(project)
    try:
        stored = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(stored, dict):
        return None
    text = str(stored.get("path") or "").strip()
    if not text or not Path(text).is_absolute():
        return None
    return Path(text)


def write_destination(project, destination: Path) -> Path:
    """Keep `destination` for this deck, replacing whatever it stated before."""
    target = destination_path(project)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"path": str(destination)}, ensure_ascii=False, indent=1), encoding="utf-8")
    return destination
