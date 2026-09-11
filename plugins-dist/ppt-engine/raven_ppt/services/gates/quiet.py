"""Findings a build does not repeat, because repeating them changed nothing.

Six measured deck runs, 34 to 3 pages, were read build by build for what each
gate said and what the author did next. Four kinds of noise came out of it, each
with a rule here:

* A page the runner stood in for (`page_failed`) carried two to four more
  findings about the placeholder itself -- its font, its decoration share, its
  likeness to the page before. The placeholder is not the page; only the
  failure is reported.
* `repeated_layout` fired seventeen times in one deck, every one on the fifteen
  consecutive paper pages the user had confirmed as one uniform format. Two
  adjacent pages the outline gives the same prototype or layout are meant to
  match.
* `prototype_kept` ("N% of its shapes sit where the template's page puts one")
  was reported 26 times across 12 pages and acted on zero times; the delivered
  deck still carried it. It is said once per page and then left to the author.
* `excessive_whitespace` and `sparse_container` asked for 38 fills on a deck
  whose brief said white ground and generous whitespace. When the brief asks
  for air, air is not a finding.

`placeholder_marks` is said once per page for the same reason as `prototype_kept`:
the template's numerals and glyphs on a cloned page are advisory, the author may be
keeping them on purpose, and a page told twice is told for nothing.

A fifth rule, and the only one here about two gates contradicting each other
rather than one repeating itself. `type_drift` and `row_type_drift` measure the
same slot against two baselines: the size the slot was drawn at, and the row's
own spread. Where both speak about one slot on one page -- 3 of the 19 type
findings on one delivered 14-page deck -- their moves disagree, and one of them
is the move the other names as wrong: "give the boxes the height one size needs"
fixes the card it is aimed at and leaves the row uneven, which is what the row
reading exists to say. So the row's reading wins on the slot they share, and
`type_drift` keeps the 6 slots on that deck the row reading cannot see.

The slot they share is stated as the boxes both findings are about, because
neither one's own group key is available to the other: a row's level is a slot
inside a repeating unit and `type_drift`'s group is a shape repeated anywhere in
the deck. Comparing `[width, height]` instead made this rule read two levels of
one page that happen to be drawn the same size as one slot, and silenced a
finding the row reading had never looked at.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable
from pathlib import Path

from raven_ppt.contracts import Project, brief_path, load_brief, load_outline, outline_path
from raven_ppt.contracts.findings import Finding

SAID_ONCE_KINDS = frozenset({"prototype_kept", "placeholder_marks"})
# The pair whose baselines disagree, nearer answer first: where a slot on a page has
# both, the row's own spread is the reading the author acts on.
OUTRANKED_BY_ROW = ("type_drift", "row_type_drift")
WHITESPACE_KINDS = frozenset({"excessive_whitespace", "sparse_container"})
UNIFORM_KIND = "repeated_layout"
PLACEHOLDER_KIND = "page_failed"
SAID_ONCE_FILENAME = "said-once.json"

# What a brief says when the user asked for air. Read off the brief's own free
# text because that is where the intake writes the user's words about style.
# The two CJK terms are "generous whitespace" and "white ground", spelled as escapes
# so the source stays ASCII.
_AIR = re.compile(r"\u7559\u767d|\u767d\u5e95|whitespace|white space|airy|breathing room|minimal", re.I)


def brief_asks_for_air(project: Project) -> bool:
    brief = load_brief(brief_path(project))
    if brief is None:
        return False
    return any(_AIR.search(str(note)) for note in (*getattr(brief, "notes", ()), getattr(brief, "audience", "")))


def uniform_pairs(project: Project) -> set[tuple[int, int]]:
    """Adjacent outline pages meant to share a shape: same non-empty prototype or layout."""
    outline = load_outline(outline_path(project))
    if outline is None:
        return set()
    pairs: set[tuple[int, int]] = set()
    pages = sorted(outline.pages, key=lambda page: page.page)
    for before, after in zip(pages, pages[1:]):
        if after.page != before.page + 1:
            continue
        same_prototype = before.prototype is not None and before.prototype == after.prototype
        same_layout = bool(before.layout) and before.layout == after.layout
        if same_prototype or same_layout:
            pairs.add((before.page, after.page))
    return pairs


def _boxes_of(finding: Finding) -> list[tuple]:
    """The boxes a type finding is about, page included, as both readings record them."""
    boxes = (finding.detail or {}).get("boxes")
    if finding.page is None or not isinstance(boxes, list):
        return []
    return [(finding.page, tuple(box)) for box in boxes if isinstance(box, list)]


def _boxes_the_row_reading_holds(findings: Iterable[Finding]) -> set[tuple]:
    """Which boxes the row's own spread has spoken about, so the deck-wide baseline does not."""
    held: set[tuple] = set()
    for finding in findings:
        if finding.kind == OUTRANKED_BY_ROW[1]:
            held.update(_boxes_of(finding))
    return held


def _already_said_by_a_row(finding: Finding, held: set[tuple]) -> bool:
    """Every box this finding names is one a row finding on the same page already carries.

    Every box and not any: a `type_drift` finding covers the copies of one slot on one
    page, and where some of them are the row's and some are not, the ones that are not
    have had nothing said about them.
    """
    boxes = _boxes_of(finding)
    return bool(boxes) and all(box in held for box in boxes)


def _said_path(project: Project) -> Path:
    return project.state_dir / SAID_ONCE_FILENAME


def _said(project: Project) -> set[str]:
    try:
        held = json.loads(_said_path(project).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return set()
    return {str(item) for item in held} if isinstance(held, list) else set()


def quiet(project: Project, findings: Iterable[Finding], stood_in: Iterable[int] = ()) -> list[Finding]:
    """The findings worth repeating this build; see the module docstring for the four rules.

    ``stood_in`` is the pages the runner replaced with a placeholder, handed in by
    the stage (the runner's record is a backend's, and this layer reads none).
    """
    findings = list(findings)
    stood_in = set(stood_in)
    uniform = uniform_pairs(project)
    air = brief_asks_for_air(project)
    said = _said(project)
    superseded = _boxes_the_row_reading_holds(findings)
    kept: list[Finding] = []
    newly_said: list[str] = []
    for finding in findings:
        if finding.page in stood_in and finding.kind != PLACEHOLDER_KIND:
            continue
        if finding.kind == UNIFORM_KIND:
            same_as = finding.detail.get("same_as") if finding.detail else None
            if isinstance(same_as, int) and finding.page is not None and (same_as, finding.page) in uniform:
                continue
        if air and finding.kind in WHITESPACE_KINDS:
            continue
        if finding.kind == OUTRANKED_BY_ROW[0] and _already_said_by_a_row(finding, superseded):
            continue
        if finding.kind in SAID_ONCE_KINDS:
            key = f"{finding.kind}:{finding.page}"
            if key in said:
                continue
            newly_said.append(key)
        kept.append(finding)
    if newly_said:
        try:
            _said_path(project).parent.mkdir(parents=True, exist_ok=True)
            _said_path(project).write_text(json.dumps(sorted(said | set(newly_said))), encoding="utf-8")
        except OSError:
            pass
    return kept
