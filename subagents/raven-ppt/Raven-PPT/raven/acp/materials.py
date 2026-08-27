"""Getting source documents in, and getting the finished deck out.

This is the launcher's job moved onto the protocol. ``subagents/raven-ppt/run.py``
did it around a forked CLI: it read absolute paths out of the task text, copied
them into the job's ``materials/``, appended a listing to the prompt, and
afterwards searched the job's ``out/`` for the deck the run had published. All of
that still has to happen; what changes is *when* and *what it can trust*.

**When.** Staging moves from launch to ``session/prompt``, because that is where
the paths arrive. A session can therefore add material on a second turn, which
the launcher could not: it staged once per process.

**What it can trust.** Two of the launcher's defences are no longer needed and
one still is.

* The prose scan is kept. A dispatching agent still writes paths in prose, and
  the fenced-JSON block is still the reliable channel -- a path in prose is
  delimited by whitespace, so ``/tmp/my deck.pptx`` splits in two.
* The wrap-repair in the deck search is gone. It existed because the CLI rendered
  its reply through a console that hard-wrapped at the terminal width, so the
  announced path arrived split across three lines. On this transport the reply is
  model text in a JSON string; nothing wraps it.
* Searching the directory rather than parsing the announcement is kept anyway.
  A build writes intermediates beside the deck, so the announcement is what
  *chooses between* candidates -- and only this run's writes are candidates,
  because a session's job directory accumulates every turn's decks.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import zipfile
from pathlib import Path

MATERIAL_SUFFIXES = {
    ".pdf",
    ".md",
    ".markdown",
    ".txt",
    ".rst",
    ".docx",
    ".doc",
    ".pptx",
    ".xlsx",
    ".xls",
    ".csv",
    ".tsv",
    ".json",
    ".html",
    ".htm",
    ".png",
    ".jpg",
    ".jpeg",
    ".webp",
}
"""Document kinds the prose scan will accept.

The filter applies to the *prose* scan alone: prose names paths that are not
material, and a run that staged every file mentioned in passing would ground the
deck in its own log. A document of another kind reaches a run through the
declared block, which does not filter.
"""

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

_PATH_RE = re.compile(r"/[^\s'\"`,;()<>\[\]]+")
_INPUTS_FENCE = re.compile(r"```(?:raven-ppt|json)?\s*\n\s*(\{.*?\})\s*\n\s*```", re.DOTALL)


class StagingError(Exception):
    """A named source could not be copied into the job.

    A file that cannot be staged stops the turn rather than being skipped: a deck
    built from part of its material is wrong in a way nothing downstream can see.
    """


def inputs_from_prompt(text: str) -> list[str]:
    """What a dispatching agent declared, as a fenced JSON object: its materials.

    ``[]`` when it declared nothing, which leaves the prose scan below as the only
    reader.

    The sub-agent contract carries one content channel, the task text, and prose is
    not a reliable one. A path in prose is delimited by whitespace, so
    ``/tmp/my deck.pptx`` splits in two; a full stop in a script whose punctuation
    is not ASCII stays attached, so ``notes.md`` plus a full-width full stop matches
    no known type. Either way the file is dropped without a word, which is exactly
    the silence ``stage`` refuses to allow for a file it cannot copy. A quoted
    string has neither problem.

    A declared ``template`` path is refused rather than ignored, and refused the
    same way the launcher refuses it: the channel is gone from both entry points,
    and a deck delivered without the house file the caller named would read as if
    it had been used. A ``.pptx`` named among the materials is still staged like
    any other document, and ``ppt_template`` binds it -- that tool is the template
    channel now, on either transport. A non-path value under that key (a style name
    in an unrelated JSON block) is not a path declaration and is left alone.
    """
    for block in _INPUTS_FENCE.findall(text):
        try:
            declared = json.loads(block)
        except json.JSONDecodeError:
            continue
        if not isinstance(declared, dict) or not declared.keys() & {"materials", "template"}:
            continue
        template = declared.get("template")
        if isinstance(template, str) and template.startswith("/"):
            raise StagingError(
                "the template channel was removed. The deck is built in the file the "
                "run publishes; drop the template declaration and name the .pptx among "
                "the materials, where ppt_template can bind it."
            )
        listed = declared.get("materials")
        return [item for item in listed if isinstance(item, str)] if isinstance(listed, list) else []
    return []


def materials_from_prompt(text: str) -> list[str]:
    """Absolute paths named in the prompt that exist and look like documents.

    The tail is trimmed a character at a time until what is left is a file, rather
    than by stripping ASCII full stops: prose in any script puts its punctuation
    against the path, and a mark that is not ASCII stays attached.
    """
    found: list[str] = []
    for match in _PATH_RE.findall(text):
        for end in range(len(match), 1, -1):
            candidate = match[:end]
            if Path(candidate).is_file():
                if Path(candidate).suffix.lower() in MATERIAL_SUFFIXES and candidate not in found:
                    found.append(candidate)
                break
    return found


def unique_sources(paths: list[str]) -> list[str]:
    """The given paths in order, one entry per real file.

    Deduplicated by real path rather than by spelling: two names for one file
    would be staged twice under different names and listed as two entries, so the
    prompt would claim material the run does not have.
    """
    unique: list[str] = []
    seen: set[str] = set()
    for path in paths:
        if (key := os.path.realpath(path)) not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def stage(materials_dir: Path, sources: list[str], taken: set[str]) -> list[tuple[str, Path]]:
    """Copy each source into ``materials_dir``, returning (source, copy) pairs.

    ``taken`` is the set of basenames already used in this directory and is
    updated in place, so a second turn's material cannot overwrite a first turn's.
    A colliding basename is suffixed rather than allowed to overwrite: that
    collision loses material exactly as silently as a skipped copy would --
    ``copyfile`` succeeds, and the prompt would list two entries resolving to one
    file, so the agent believes it holds two documents and grounds the deck twice
    in one of them.
    """
    materials_dir.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[str, Path]] = []
    for source in sources:
        stem, suffix = Path(source).stem, Path(source).suffix
        name, nth = stem + suffix, 2
        while name in taken:
            name, nth = f"{stem}-{nth}{suffix}", nth + 1
        taken.add(name)
        target = materials_dir / name
        try:
            shutil.copyfile(source, target)
        except OSError as exc:
            raise StagingError(
                f"cannot stage {source}: {exc.strerror or exc}. Name a readable absolute path, "
                "or drop it from the task."
            ) from None
        staged.append((source, target))
    return staged


def describe(staged: list[tuple[str, Path]], materials_dir: Path, out_dir: Path) -> str:
    """The block appended to the prompt naming what was staged and where to build.

    Word for word what the launcher's ``material_section`` produces for the same
    inputs, plus the same compile sentence it appends at its call site, because the
    agent behind both entry points is one agent: a deck author told to gather its
    own material over one transport and refused over the other is two products
    wearing one name. ``tests/ppt/test_prompt_claims.py`` drives both and asserts
    they agree, which is the only thing that can keep them equal -- the launcher is
    standard-library-only and outside this package, so the text cannot be shared.

    Built from the staging pairing rather than re-derived from the source paths, so
    the agent is told where each file actually is: a collision-suffixed copy has a
    name the source does not.
    """
    if staged:
        listing = "\n".join(f"- {Path(source).name} (from {source}) -> {target}" for source, target in staged)
        text = (
            f"\n\n# Material staged for this run\n{listing}"
            f"\nUse only files under {materials_dir} as factual source material."
        )
    else:
        text = (
            "\n\n# No material staged for this run\n"
            "Nothing was named, so this deck's material has to be gathered rather than "
            "read: web_search for the sources and the pictures, "
            'web_fetch(extractMode="images") on the URLs they cite -- which returns each '
            "picture with the caption its author wrote -- and ppt_fetch what you will use, "
            "so the ingest reads it in. Fetch into the project rather than placing anything "
            "straight onto a page: what this deck never ingested is what its provenance "
            "checks cannot see, and on a run with no staged material that is everything. "
            "What you still cannot verify is a guess, and it is presented as one."
        )
    return text + (f" Compile the deck under {out_dir}/ and end your final reply with the MEDIA line naming it.")


def slide_count(deck: Path) -> int:
    """Slides in a pptx, or 0 if it is not one.

    A failed render leaves a truncated or empty file behind, so the check is that
    the archive opens and carries slide parts -- not that the name is right.
    """
    if not (deck.is_file() and zipfile.is_zipfile(deck)):
        return 0
    try:
        with zipfile.ZipFile(deck) as archive:
            return len(
                [name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml")]
            )
    except (OSError, zipfile.BadZipFile):
        # ``is_zipfile`` reads the central directory only; a file whose entries are
        # truncated passes it and fails here. A half-written deck is not a deck.
        return 0


def deck_mtimes(out_dir: Path) -> dict[Path, float]:
    """Every pptx under ``out_dir`` with its mtime, for the before/after compare."""
    found: dict[Path, float] = {}
    if not out_dir.is_dir():
        return found
    for path in out_dir.rglob("*.pptx"):
        try:
            found[path] = path.stat().st_mtime
        except OSError:
            continue
    return found


def verified_deck(out_dir: Path, reply: str, before: dict[Path, float]) -> tuple[Path | None, int]:
    """The deck this turn published, or ``(None, 0)``.

    Every valid deck this turn wrote or rewrote is a candidate; the announcement
    only chooses between them, and the newest wins when it names none of them.

    Scoped to this turn rather than to the directory, which is what ``before`` is
    for: one session's job directory accumulates every turn's decks, so accepting
    any deck in it would let a turn that published nothing -- a model error, a
    render that never finished -- fall through to the newest and hand back an
    earlier turn's file as this turn's result.
    """
    candidates = [
        (path, count)
        for path, mtime in sorted(deck_mtimes(out_dir).items())
        if mtime > before.get(path, -1.0) and (count := slide_count(path))
    ]
    if not candidates:
        return None, 0

    marker = reply.rfind("MEDIA:")
    # The announcement first, and only then the rest of the reply. A build writes
    # intermediates next to the deck, and both names appear somewhere in the text,
    # so anything wider than the announcement picks between them by luck.
    for scope in ([reply[marker:]] if marker >= 0 else []) + [reply]:
        for path, count in candidates:
            if path.name in scope:
                return path, count
    return max(candidates, key=lambda item: item[0].stat().st_mtime)


def deliver(deck: Path, dest_dir: str | None) -> Path | None:
    """Copy the deck next to the client, keeping the job's own copy either way.

    ``dest_dir`` is the ACP session's ``cwd``, which for a raven host is the live
    session workspace of the conversation that asked for the deck. ``None`` when
    the caller wants the job copy to be the only one.
    """
    if dest_dir is None:
        return deck
    dest = Path(dest_dir).expanduser()
    try:
        dest.mkdir(parents=True, exist_ok=True)
        if dest.samefile(deck.parent):
            return deck
        target = dest / deck.name
        shutil.copyfile(deck, target)
        return target
    except OSError:
        return None


__all__ = [
    "MATERIAL_SUFFIXES",
    "PPTX_MIME",
    "StagingError",
    "deck_mtimes",
    "deliver",
    "describe",
    "inputs_from_prompt",
    "materials_from_prompt",
    "slide_count",
    "stage",
    "unique_sources",
    "verified_deck",
]
