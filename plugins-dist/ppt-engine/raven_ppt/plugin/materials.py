"""Getting source documents in, and verifying the finished deck came out.

This was the fork launcher's job first and its ACP layer's second
(fork ``raven/acp/materials.py``, ported here with its behaviours intact): read
absolute paths out of the task text, copy them into the session's
``materials/``, append a listing to the prompt, and afterwards search ``out/``
for the deck the run published. Here the seat is the plugin's turn hook -- the
``before_user_inbound`` phase stages and rewrites the model's view of the
inbound text, ``after_send`` verifies and announces -- and the working
directory the copies land under is the turn's own (``workdir.current()``),
which the host binds per session.

What it can trust, inherited from the fork move onto ACP:

* The prose scan is kept. A dispatching agent still writes paths in prose, and
  the fenced-JSON block is still the reliable channel -- a path in prose is
  delimited by whitespace, so ``/tmp/my deck.pptx`` splits in two.
* The wrap-repair in the deck search stays gone. It existed for a console that
  hard-wrapped the announced path across three lines; on this host the reply
  is model text, and nothing wraps it.
* Searching the directory rather than parsing the announcement is kept anyway.
  A build writes intermediates beside the deck, so the announcement is what
  *chooses between* candidates -- and only this turn's writes are candidates,
  because a session's directory accumulates every turn's decks.

One fork function does not board: ``deliver()`` copied the finished deck from
the job directory to the ACP client's ``cwd``. On this host the turn's working
directory IS the delegating conversation's directory -- the deck is already
where the client looks -- so the copy has nowhere left to carry anything.
"""

from __future__ import annotations

import filecmp
import json
import os
import re
import shutil
import zipfile
from collections.abc import Iterable, Iterator, Sequence
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
    same way the fork launcher refused it: the channel is gone from both entry
    points, and a deck delivered without the house file the caller named would read
    as if it had been used. A ``.pptx`` named among the materials is still staged
    like any other document, and ``ppt_template`` binds it -- that tool is the
    template channel now, on either transport. A non-path value under that key (a
    style name in an unrelated JSON block) is not a path declaration and is left
    alone.
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

    One match can hold more than one path, so the scan carries on past the file it
    just found instead of taking it as the answer for the whole match. The pattern
    ends a path on ASCII punctuation, so ``a.md, b.md`` arrives as two matches and
    ``a.md`` plus an ideographic comma plus ``b.md`` arrives as one -- and taking
    the first file in it dropped ``b.md`` without a word, which is the silence
    ``stage`` refuses for a file it cannot copy. Widening the pattern instead was
    tried and is worse: every mark added to it is a mark a filename may not
    contain, and ``/root/report (final).md`` written in full-width brackets really
    does exist.
    """
    found: list[str] = []
    for match in _PATH_RE.findall(text):
        for candidate in _files_in(match):
            if Path(candidate).suffix.lower() in MATERIAL_SUFFIXES and candidate not in found:
                found.append(candidate)
    return found


def _files_in(match: str) -> Iterator[str]:
    """Every existing file named inside one prose match, longest name first.

    Walks the match one path-start at a time: the longest prefix that is a file is
    the answer for that start, and the search resumes at the next ``/`` after it.
    A start that names nothing is not the end of the match either -- a name that
    does not exist can be followed by one that does.
    """
    rest = match
    while len(rest) > 1:
        hit = next((end for end in range(len(rest), 1, -1) if Path(rest[:end]).is_file()), None)
        if hit is not None:
            yield rest[:hit]
        following = rest.find("/", hit if hit is not None else 1)
        if following < 0:
            return
        rest = rest[following:]


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


def unstaged(sources: list[str], staged: list[tuple[str, Path]]) -> list[str]:
    """The given sources minus the ones this session already holds a copy of.

    Two ways a source is already held, and both have to be read. By real path,
    which is one file named again on a later turn of the same session. By
    content, under any of the names ``stage`` could have copied it to, which is
    that same repeat across a reopen: a pairing recovered by ``rehydrate`` records
    each copy as its own source, so the path the client declares matches nothing
    there and the file would be staged again under a collision-suffixed name --
    leaving the prompt listing one document twice, which is what the real-path
    check exists to stop.

    A family of names rather than the one exact match, because a single basename
    can hold several: two sources both named ``report.txt`` are copied to
    ``report.txt`` and ``report-2.txt``, and a repeat compared against
    ``report.txt`` alone misses the second copy and stages it a third time.
    """
    held = {os.path.realpath(source) for source, _ in staged}
    by_name = {target.name: target for _, target in staged}
    return [
        source for source in sources if os.path.realpath(source) not in held and not _copied_already(source, by_name)
    ]


def _copied_already(source: str, by_name: dict[str, Path]) -> bool:
    """Whether one of the copies is ``source``, under any name it could have got.

    Walks the names ``stage`` would have tried for this basename in that same
    order and stops at the first the directory does not hold, which is where the
    family ends: ``stage`` always takes the lowest free name, so the copies of one
    basename run from the front. A basename nothing collides with is therefore
    compared once, and ``filecmp`` opens neither file when the two sizes differ.
    """
    for name in _copy_names(Path(source).name):
        if (copy := by_name.get(name)) is None:
            break
        if _same_bytes(source, copy):
            return True
    return False


def _same_bytes(source: str, copy: Path) -> bool:
    """Whether ``source`` and ``copy`` are the same document.

    Unreadable is not the same: a source that cannot be opened has to reach
    ``stage``, which is the one place that turns it into a ``StagingError`` the
    turn reports rather than a file dropped without a word.
    """
    try:
        return filecmp.cmp(source, copy, shallow=False)
    except OSError:
        return False


def _copy_names(basename: str) -> Iterator[str]:
    """The names ``stage`` gives a copy of ``basename``, in the order it tries them.

    The collision suffix is ``stage``'s own invention, so the names one basename
    can occupy are generated in one place: ``unstaged`` has to recognise a
    recovered copy under any of them, and a pattern matching them back would be a
    second spelling of this rule, free to drift from it.
    """
    stem, suffix = Path(basename).stem, Path(basename).suffix
    yield stem + suffix
    nth = 2
    while True:
        yield f"{stem}-{nth}{suffix}"
        nth += 1


def rehydrate(materials_dir: Path) -> tuple[list[tuple[str, Path]], set[str]]:
    """The staging bookkeeping for a job whose ``materials/`` already has copies.

    ``stage`` keeps two things across a session: the (source, copy) pairs the
    prompt block is built from, and the basenames the directory has already given
    out. Neither survives the process while the directory does, so a session
    reopened on an existing job recovers both from the copies themselves -- the
    only record of a past staging still there. Without it the next prompt tells
    the agent that nothing is staged, and a new source of a name already used
    overwrites the copy holding it instead of being suffixed.

    Each copy stands as its own source. Where it was copied from is recorded
    nowhere, and what the pairing owes the agent is the file it is to read.
    """
    if not materials_dir.is_dir():
        return [], set()
    copies = sorted(path for path in materials_dir.iterdir() if path.is_file())
    return [(str(path), path) for path in copies], {path.name for path in copies}


def stage(materials_dir: Path, sources: list[str], taken: set[str]) -> list[tuple[str, Path]]:
    """Copy each source into ``materials_dir``, returning (source, copy) pairs.

    ``taken`` is the set of basenames already used in this directory and is
    updated in place, so a second turn's material cannot overwrite a first turn's.
    A colliding basename is suffixed rather than allowed to overwrite: that
    collision loses material exactly as silently as a skipped copy would --
    ``copyfile`` succeeds, and the prompt would list two entries resolving to one
    file, so the agent believes it holds two documents and grounds the deck twice
    in one of them. The names it tries come from ``_copy_names`` because the
    held-check has to recognise a copy under any of them.
    """
    materials_dir.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[str, Path]] = []
    for source in sources:
        name = next(candidate for candidate in _copy_names(Path(source).name) if candidate not in taken)
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


DESTINATION_LEAD = (
    " If the task names where the file should go, state that once as ppt_build's deliver_to: every build "
    "that publishes then writes the deck there as well, and the MEDIA line names that path."
)
"""One sentence past the fork's text, for the one request the fork could not meet.

A task that names the file's destination was met by a `cp` after the build -- a copy the
verification then called out. The engine now takes the destination itself, and the prompt
says where to put it, because the tool description is read only once the model has
reached for the tool.
"""


def describe(
    staged: list[tuple[str, Path]],
    materials_dir: Path,
    out_dir: Path,
    standing: Sequence[Path] = (),
) -> str:
    """The block appended to the prompt naming what was staged and where to build.

    Word for word what the fork's two entry points produced for the same inputs
    (its launcher's ``material_section`` and this function's ACP original),
    because the agent behind every entry point is one agent: a deck author told
    to gather its own material over one transport and refused over the other is
    two products wearing one name. ``tests/test_ppt_engine_prompt_claims.py``
    holds the text to the claims the skill and the tools make. One adaptation
    rides the D2 tool-name change: the gathering instructions name
    ``ppt_image_search`` where the fork named ``web_search(kind="images")`` and
    plain ``web_fetch`` where it named an image extract mode trunk's tool does
    not carry.

    Built from the staging pairing rather than re-derived from the source paths, so
    the agent is told where each file actually is: a collision-suffixed copy has a
    name the source does not.

    ``standing`` is the deck this folder has already published, and it is the one
    input the fork never had. Without it the block ended the same way on every turn
    of a session -- compile the deck and end the reply with a MEDIA line -- so the
    turn after a delivery, where the user says the deck is fine, was instructed to
    publish it again, and it did.

    What replaces that instruction is a statement of what is on the record: the
    published deck's path, that changing it means another build, and where the MEDIA
    line comes from. Nothing here decides whether this turn is deck work, because
    nothing here can: the model reads the user's message, and a fixed rule guessing
    at it from the staging book was wrong on the ordinary source-backed deck, whose
    first turn's material is still in the book on every later turn. The material
    listing is unchanged and composes with this: a turn that does build needs its
    sources whether or not a deck already stands.

    The gathering advice does not compose with it. "Nothing was named, so this deck's
    material has to be gathered -- web_search, ppt_image_search, web_fetch, ppt_fetch"
    is written for a deck that has none, and a deck on the publish record has already
    taken it: on that turn the text is not merely redundant but stale. So a standing
    deck with nothing staged gets the statement alone. That turns on the same fact the
    statement itself turns on, which is the record rather than a reading of the turn.
    With no deck standing, both branches keep their text word for word, because those
    are the two inputs the fork was measured on.
    """
    if standing:
        published = Path(standing[-1])
        stands = (
            f"\n\n# The deck this session published\n{published} stands as published, and it is what the user "
            "already has. A change to it is another ppt_build that publishes; the MEDIA line comes from a "
            "publish and from nothing else."
        )
        if not staged:
            return stands
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
            "read: web_search for the sources, ppt_image_search for the pictures, "
            "web_fetch on the URLs they cite -- take each image link with the words its "
            "page printed beside it -- and ppt_fetch what you will use, "
            "so the ingest reads it in. Fetch into the project rather than placing anything "
            "straight onto a page: what this deck never ingested is what its provenance "
            "checks cannot see, and on a run with no staged material that is everything. "
            "What you still cannot verify is a guess, and it is presented as one."
        )
    if standing:
        return text + stands
    return text + (
        f" Compile the deck under {out_dir}/ and end your final reply with the MEDIA line naming it.{DESTINATION_LEAD}"
    )


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


def deck_mtimes(out_dir: Path, also: Iterable[Path] = ()) -> dict[Path, float]:
    """Every pptx under ``out_dir`` with its mtime, for the before/after compare.

    ``also`` names files outside ``out_dir`` the compare has to cover as well: the
    destination the user named is delivered to on every publish, so a turn that
    published nothing must not find last week's delivery there and announce it as
    this turn's.
    """
    found: dict[Path, float] = {}
    candidates = list(out_dir.rglob("*.pptx")) if out_dir.is_dir() else []
    candidates.extend(path for path in also if path.is_file())
    for path in candidates:
        try:
            found[path] = path.stat().st_mtime
        except OSError:
            continue
    return found


def verified_deck(
    out_dir: Path,
    reply: str,
    before: dict[Path, float],
    published: set[str] | None = None,
    delivered: Iterable[Path] = (),
) -> tuple[Path | None, int]:
    """The deck this turn published, or ``(None, 0)``.

    ``published`` is the set of sha256 digests the publish step recorded; given, a
    candidate has to be one of them. A file the model copied into ``out/`` itself is a
    valid deck newer than the turn started and used to verify -- two live runs did
    exactly that after a refused build, and the reply then told the user the deck was
    delivered. Passing ``None`` keeps the older reading for a caller with no record.

    ``delivered`` names the paths outside ``out/`` the publish step itself wrote -- the
    destination the user asked for, read off the publish record. Those stand as
    candidates on the same terms as the files under ``out/``: written this turn, a deck,
    and on the record. Any other path outside ``out/`` is not looked at here; it is a
    copy the publish step never made, whatever it holds.

    Every valid deck this turn wrote or rewrote is a candidate; the announcement
    only chooses between them, and the newest wins when it names none of them. The
    whole path is matched before the bare name, because a delivery keeps the deck's
    own name unless the user chose one, and ``deck.pptx`` would then pick out/.

    Scoped to this turn rather than to the directory, which is what ``before`` is
    for: one session's job directory accumulates every turn's decks, so accepting
    any deck in it would let a turn that published nothing -- a model error, a
    render that never finished -- fall through to the newest and hand back an
    earlier turn's file as this turn's result.
    """
    candidates = [
        (path, count)
        for path, mtime in sorted(deck_mtimes(out_dir, delivered).items())
        if mtime > before.get(path, -1.0) and (count := slide_count(path))
    ]
    if published is not None:
        candidates = [(path, count) for path, count in candidates if _digest(path) in published]
    if not candidates:
        return None, 0

    marker = reply.rfind("MEDIA:")
    # The announcement first, and only then the rest of the reply. A build writes
    # intermediates next to the deck, and both names appear somewhere in the text,
    # so anything wider than the announcement picks between them by luck.
    for scope in ([reply[marker:]] if marker >= 0 else []) + [reply]:
        for named in (str, lambda path: path.name):
            for path, count in candidates:
                if named(path) in scope:
                    return path, count
    return max(candidates, key=lambda item: item[0].stat().st_mtime)


def _digest(path: Path) -> str:
    import hashlib

    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def published_decks(out_dir: Path, published: set[str]) -> list[Path]:
    """Every deck under ``out/`` the publish step recorded, oldest first.

    The turn's own before/after compare cannot answer this one: what matters here is
    not what this turn wrote but whether anything stands from an earlier one.
    """
    return [
        path for path, _ in sorted(deck_mtimes(out_dir).items(), key=lambda item: item[1]) if _digest(path) in published
    ]


def unpublished_decks(out_dir: Path, before: dict[Path, float], published: set[str]) -> list[Path]:
    """Decks this turn wrote under ``out/`` that the publish step never recorded."""
    return [
        path
        for path, mtime in sorted(deck_mtimes(out_dir).items())
        if mtime > before.get(path, -1.0) and slide_count(path) and _digest(path) not in published
    ]


__all__ = [
    "DESTINATION_LEAD",
    "MATERIAL_SUFFIXES",
    "StagingError",
    "deck_mtimes",
    "describe",
    "inputs_from_prompt",
    "materials_from_prompt",
    "rehydrate",
    "slide_count",
    "stage",
    "published_decks",
    "unpublished_decks",
    "unique_sources",
    "unstaged",
    "verified_deck",
]
