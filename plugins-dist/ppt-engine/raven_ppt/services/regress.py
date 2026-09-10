"""Whether the build that just ran made a page worse than the build before it.

Nothing noticed. A page carrying an overset line is edited, the edit spills a word
past its card, and the reply that comes back says only that the page has a word
collision -- with no trace of the fact that the collision arrived *with the fix*.
The author reads a fresh blocking finding, fixes that, and the loop can run for
rounds with each round answering the damage of the last. What is missing is not a
measurement; every one of those findings was measured. It is the comparison
between two builds.

So this keeps, per page, the blocking finding kinds that page carried when it was
last built, and reports the kinds a rebuild added. Kinds rather than counts: a
page that went from two collisions to five has the same problem it had, while a
page that gained `card_overflow` has a new one, and only the second is worth
interrupting the author for.

It reports and does not act. The reference design for this keeps a copy of the
`.pptx` and rolls back to it, which cannot be right here: the source of truth is
the author's program, so restoring the generated file would leave the program
disagreeing with the deck it supposedly built, and the author's edit -- possibly
a good edit with one bad consequence -- would be gone with no record of what it
was. The author decides; this makes the decision visible.

Page identity is borrowed from `seen`, whose fingerprint of "the code that drew
this page" is already the notion the renders and the design pass are matched by.
It is a separate file rather than a second table in `seen.json` because the two
records are written for different sets of pages -- `seen` records the handful the
reply hands over, this records every page that was measured -- and cleared for
different reasons.

Page numbers are only stable while the deck's length is: insert a page and every
banner below it renumbers, so page 7's recorded findings would be compared
against what is now a different page. So a build whose page count differs from the
recorded one re-baselines and reports nothing, which under-reports on the build
that changes the length and never invents a regression that did not happen.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

from raven_ppt.contracts import Finding, Severity
from raven_ppt.services import seen

BLOCKING_FILE = "blocking.json"
SCHEMA = "raven_ppt.regress.v1"


def blocking_path(project) -> Path:
    return project.state_dir / BLOCKING_FILE


@dataclass(frozen=True)
class Regression:
    """One page that came back from a rebuild worse than it went in."""

    page: int
    gained: tuple[str, ...]
    """Blocking finding kinds this page did not carry in the previous build."""
    had: tuple[str, ...]
    """What it did carry then -- empty when the page had nothing blocking at all,
    which is the strongest form of this report and the one worth saying plainly."""
    recoded: bool
    """Whether the block that draws this page changed since that build.

    The author needs to tell "my edit did this" from "something shared did this":
    a page that gained a collision without its own code changing was changed by the
    prelude, by the design pass, or by a helper it calls."""


@dataclass(frozen=True)
class _Snapshot:
    pages: int
    by_page: dict[int, tuple[str, tuple[str, ...]]]
    """page -> (code fingerprint, blocking kinds)."""


def blocking_kinds_by_page(
    findings: Iterable[Finding], blocking_kinds: frozenset[str] | Iterable[str]
) -> dict[int, tuple[str, ...]]:
    """Per page, the sorted blocking finding kinds it carries.

    The same rule the reply refuses on -- severity, or a kind this route declares
    fatal -- so a page cannot be reported as regressed on something that would not
    have stopped the deck. Deck-wide findings are left out: they have no page, and
    the three a draft is not held to (`page_budget`, `page_mapping`, `unseen_page`)
    are all of that shape, so a draft and a finished build stay comparable here.
    """
    fatal = frozenset(blocking_kinds)
    found: dict[int, set[str]] = {}
    for finding in findings:
        if finding.page is None:
            continue
        if finding.severity is Severity.BLOCKING or finding.kind in fatal:
            found.setdefault(finding.page, set()).add(finding.kind)
    return {page: tuple(sorted(kinds)) for page, kinds in found.items()}


def compare(
    project,
    *,
    script: str,
    sources: Sequence | None,
    findings: Iterable[Finding],
    blocking_kinds: frozenset[str] | Iterable[str],
    pages: int,
) -> tuple[Regression, ...]:
    """What this build added to pages that were already built once.

    Read-only. `record` is the separate call that moves the baseline forward, so a
    caller can compare and report without the two being one irreversible step.
    """
    previous = _load(project)
    if previous is None or previous.pages != pages:
        return ()
    blocks = seen.blocks_of(script, sources)
    now = blocking_kinds_by_page(findings, blocking_kinds)
    regressions: list[Regression] = []
    for page, (digest, had) in sorted(previous.by_page.items()):
        gained = tuple(kind for kind in now.get(page, ()) if kind not in had)
        if not gained:
            continue
        regressions.append(
            Regression(
                page=page,
                gained=gained,
                had=had,
                recoded=page in blocks and blocks[page] != digest,
            )
        )
    return tuple(regressions)


def record(
    project,
    *,
    script: str,
    sources: Sequence | None,
    findings: Iterable[Finding],
    blocking_kinds: frozenset[str] | Iterable[str],
    pages: int,
    deck_file: Path | None = None,
) -> None:
    """Make this build the one the next build is compared against.

    Every measured page, not only the ones the reply showed: a page nobody looked
    at can still be broken by an edit to a page somebody did.

    `deck_file` is the file these findings were measured on, and its sha256 is written
    beside them. Nothing here reads it back -- it is what makes "the gates, the
    publication record and the delivered file describe the same bytes" a thing that
    can be checked off disk at the end of a run rather than assumed. A live run's
    delivery was edited in place after publication and all three records went on
    agreeing with each other about a file that no longer existed.
    """
    blocks = seen.blocks_of(script, sources)
    if not blocks:
        return
    now = blocking_kinds_by_page(findings, blocking_kinds)
    path = blocking_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "schema": SCHEMA,
                "pages": pages,
                **({"deck": digest} if (digest := _digest_of(deck_file)) else {}),
                "blocking": {
                    str(page): {"code": digest, "kinds": list(now.get(page, ()))}
                    for page, digest in sorted(blocks.items())
                },
            },
            ensure_ascii=False,
            indent=1,
        ),
        encoding="utf-8",
    )


def _digest_of(deck: Path | None) -> str | None:
    if deck is None:
        return None
    try:
        return hashlib.sha256(deck.read_bytes()).hexdigest()
    except OSError:
        return None


def forget(project) -> None:
    blocking_path(project).unlink(missing_ok=True)


def code_by_page(project) -> dict[int, str]:
    """Per page, the fingerprint of the code that drew it in the last build.

    The record this module already keeps, read back for callers that need page
    identity without a build outcome in hand: `seen.blocks_of` wants the script and
    the line spans, and only the build has those. Pages recorded without a
    fingerprint are left out, so a caller sees "unknown" rather than "empty".
    """
    previous = _load(project)
    if previous is None:
        return {}
    return {page: code for page, (code, _kinds) in previous.by_page.items() if code}


def _load(project) -> _Snapshot | None:
    """The previous build's record, or None when there is nothing to compare to.

    Never raises: a record this version cannot read is the same as no record, and
    the next `record` replaces it.
    """
    try:
        raw = json.loads(blocking_path(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or raw.get("schema") != SCHEMA:
        return None
    entries = raw.get("blocking")
    pages = raw.get("pages")
    if not isinstance(entries, dict) or not isinstance(pages, int):
        return None
    by_page: dict[int, tuple[str, tuple[str, ...]]] = {}
    for page, entry in entries.items():
        if not isinstance(entry, dict):
            continue
        try:
            number = int(page)
        except (TypeError, ValueError):
            continue
        kinds = entry.get("kinds")
        by_page[number] = (
            str(entry.get("code") or ""),
            tuple(sorted(str(kind) for kind in kinds)) if isinstance(kinds, list) else (),
        )
    return _Snapshot(pages=pages, by_page=by_page)
