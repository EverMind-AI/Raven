"""The template a project is building in.

Two entry points converge here: `ppt_fetch` downloads a `.pptx` from a URL, and
the user hands one over by path. Both land in the same slot, so everything
downstream asks one question -- is there a template? -- rather than knowing where
it came from.

Binding is copy, prepare, read, in that order, and it happens once when the
template arrives rather than on every build. An author iterates on a deck twenty
times; preparing the template inside the build would redo the work twenty times
and, worse, would have nowhere to report what it found. The user's file is copied
first and never touched again, because it is theirs and a later run wants to
prepare it again from clean.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from raven_ppt.services.template.bands import bands_path
from raven_ppt.services.template.inventory import (
    PREPARED_FILE,
    TEMPLATE_FILE,
    TemplateInventory,
    inspect_template,
    template_dir,
    template_path,
)
from raven_ppt.services.template.palette import palette_path, read_palette
from raven_ppt.services.template.prepare import prepare, prepared_path, strip_hidden


@dataclass(frozen=True)
class BoundTemplate:
    """The template this deck is built in, and what an author needs to know of it."""

    source: Path
    """The user's file, as copied into the project. Read for its example pages."""
    prepared: Path
    """The same file with the example pages removed. The deck is built in this."""
    inventory: TemplateInventory
    example_pages: int
    """How many pages the original ships. These are the reference, not the deck."""
    palette: dict[str, object] = field(default_factory=dict)
    """What the author read off those pages, in whole or in part, or {}.

    Bound to the deck rather than passed along because that is what it is: a reading
    of this template, taken once, standing for the deck's whole life. Empty until an
    author states one, and empty is the derivation's cue rather than a gap to fill.
    """


def bind(source: Path, project) -> BoundTemplate | None:
    """Take this file as the deck's template, or None when it is not one.

    None rather than an exception: "that .pptx will not open" is something to
    tell the user in a sentence, not a stack trace, and the deck can still be
    built without a template.
    """
    if not source.is_file():
        return None
    folder = template_dir(project)
    folder.mkdir(parents=True, exist_ok=True)
    # Under its own name, except for the one name the slot already uses for
    # something else -- a user's file called prepared.pptx would otherwise be
    # copied over the copy made from it.
    destination = folder / (TEMPLATE_FILE if source.name == PREPARED_FILE else source.name)
    if source.resolve() != destination.resolve():
        shutil.copy2(source, destination)
    # One template per deck: a second bind replaces the first rather than leaving
    # two files in the slot for the lookup to choose between.
    for stale in folder.glob("*.pptx"):
        if stale.resolve() != destination.resolve():
            stale.unlink()
    # And the palette goes with them. It is a reading of the pages of one file, so
    # kept across a rebind it would put the last template's colours in this one --
    # which `bound` would then read back as this template's own.
    palette_path(project).unlink(missing_ok=True)
    # The band grid is the same kind of reading and lives in the same slot: kept
    # across a rebind, `_bands` reads it back first and every band check judges the
    # new deck against the previous template's rows.
    bands_path(project).unlink(missing_ok=True)
    # On the copy, before anything counts its pages: a hidden slide is absent from
    # the render, so leaving it in makes the file's page numbers disagree with the
    # render's for every page after it.
    strip_hidden(destination)

    prepared = prepare(destination, prepared_path(project))
    if prepared is None:
        destination.unlink(missing_ok=True)
        return None
    return BoundTemplate(
        source=destination,
        prepared=prepared.path,
        inventory=prepared.inventory,
        example_pages=prepared.removed_slides,
    )


def bound(project) -> BoundTemplate | None:
    """The template already bound to this deck, if there is one.

    Asked on every build, so it is a pair of `is_file` checks and one read of the
    prepared copy -- and it is the only thing that decides whether a build gets
    `PPT_TEMPLATE`. A template whose prepared copy has gone reads as no template
    rather than as an error, so a deleted project directory degrades to the
    ordinary route instead of failing every build.
    """
    prepared, source = prepared_path(project), template_path(project)
    if not prepared.is_file():
        return None
    inventory = inspect_template(prepared)
    if inventory is None:
        return None
    original = inspect_template(source) if source.is_file() else None
    return BoundTemplate(
        source=source,
        prepared=prepared,
        inventory=inventory,
        example_pages=original.example_slides if original else 0,
        palette=read_palette(project),
    )
