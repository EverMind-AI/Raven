"""Whether a rebuild made a page worse, compared against the build before it.

The failure this closes is a loop answering its own damage: a page is fixed, the
fix spills a word past a card, and the next reply reports the collision with no
trace that it arrived with the fix. Every finding in that reply was measured
correctly; what was missing was the comparison.
"""

from __future__ import annotations

import json
from pathlib import Path

from raven_ppt.contracts import Finding, PageSource, Project, Severity
from raven_ppt.services import regress

_SCRIPT = "".join(f"line {n}\n" for n in range(40))
_EDITED = _SCRIPT.replace("line 3\n", "line 3 rewritten\n")

_PAGES = (
    PageSource(page=1, first_line=0, last_line=10),
    PageSource(page=2, first_line=10, last_line=20),
    PageSource(page=3, first_line=20, last_line=30),
)

# The script route refuses a filled colour bar by kind rather than by severity, so
# it is the case that proves a profile's `blocking_kinds` is honoured here too.
_ROUTE_FATAL = frozenset({"band", "unplaced_figure"})


def _project(tmp_path: Path) -> Project:
    return Project(workspace=tmp_path, slug="tarvis")


def _finding(kind: str, page: int, severity: Severity = Severity.BLOCKING) -> Finding:
    return Finding(
        kind=kind,
        severity=severity,
        message=f"{kind} on page {page}",
        page=page,
    )


def _pass(project: Project, findings, *, script: str = _SCRIPT, pages: int = 3) -> None:
    regress.record(
        project,
        script=script,
        sources=_PAGES,
        findings=findings,
        blocking_kinds=_ROUTE_FATAL,
        pages=pages,
    )


def _compare(project: Project, findings, *, script: str = _SCRIPT, pages: int = 3):
    return regress.compare(
        project,
        script=script,
        sources=_PAGES,
        findings=findings,
        blocking_kinds=_ROUTE_FATAL,
        pages=pages,
    )


def test_the_code_fingerprints_are_readable_without_a_build_outcome(tmp_path: Path) -> None:
    """Page identity, for a caller that has no build in hand.

    `seen.blocks_of` wants the script and the line spans and only the build has those,
    so what needs to tell one version of a page from another later -- which page-version
    a reading covered -- reads it back off this record rather than hashing a second time.
    """
    project = _project(tmp_path)
    assert regress.code_by_page(project) == {}, "no record is not a deck of pages at version zero"

    _pass(project, [])
    first = regress.code_by_page(project)
    assert sorted(first) == [1, 2, 3]
    assert len(set(first.values())) == 3, "one fingerprint per page, off that page's own lines"

    _pass(project, [], script=_EDITED)
    after = regress.code_by_page(project)
    assert after[1] != first[1], "page 1 is the block holding the edited line"
    assert {page: after[page] for page in (2, 3)} == {page: first[page] for page in (2, 3)}


def test_a_page_that_gains_a_blocking_finding_is_reported_with_what_it_had_before(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _pass(project, [_finding("overset_copy", 2, Severity.WARNING)])

    regressed = _compare(project, [_finding("word_collision", 2)], script=_EDITED)

    assert [entry.page for entry in regressed] == [2]
    assert regressed[0].gained == ("word_collision",)
    # What the page had before is the whole point: without it the author cannot tell
    # that the version it just replaced was the better one.
    assert regressed[0].had == ()


def test_the_kinds_the_page_carried_before_are_named_not_only_counted(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _pass(project, [_finding("placeholder_copy", 1)])

    regressed = _compare(project, [_finding("placeholder_copy", 1), _finding("word_collision", 1)])

    assert regressed[0].had == ("placeholder_copy",)
    assert regressed[0].gained == ("word_collision",)


def test_a_page_that_gains_only_a_warning_is_not_a_regression(tmp_path: Path) -> None:
    """Warnings are satisfiable by shrinking the copy, so a reply that interrupted
    the author for one would be asking it to make the page worse to quiet a report."""
    project = _project(tmp_path)
    _pass(project, [])

    assert _compare(project, [_finding("card_overflow", 1, Severity.WARNING)]) == ()


def test_more_of_the_same_blocking_kind_is_the_same_problem(tmp_path: Path) -> None:
    """Two collisions becoming five is a page that still has the problem it had.
    Reporting it would fire on every intermediate build of a page being worked on."""
    project = _project(tmp_path)
    _pass(project, [_finding("word_collision", 3)])

    regressed = _compare(
        project,
        [_finding("word_collision", 3), _finding("word_collision", 3), _finding("word_collision", 3)],
    )

    assert regressed == ()


def test_a_kind_this_route_declares_fatal_counts_even_though_it_only_warns(tmp_path: Path) -> None:
    """Severity comes from the measurement; which kinds refuse is the route's call,
    and a page must not be reported as regressed on something that would not stop
    the deck -- nor spared on something that would."""
    project = _project(tmp_path)
    _pass(project, [])

    regressed = _compare(project, [_finding("band", 2, Severity.WARNING)])

    assert [entry.gained for entry in regressed] == [("band",)]


def test_the_first_build_reports_nothing_and_becomes_the_baseline(tmp_path: Path) -> None:
    project = _project(tmp_path)

    assert _compare(project, [_finding("word_collision", 1)]) == ()
    _pass(project, [_finding("word_collision", 1)])
    assert _compare(project, [_finding("word_collision", 1)]) == ()
    assert _compare(project, [_finding("word_collision", 1), _finding("placeholder_copy", 1)])[0].gained == (
        "placeholder_copy",
    )


def test_a_build_that_changed_the_deck_s_length_reports_nothing(tmp_path: Path) -> None:
    """Insert a page and every banner below it renumbers, so page 7's record would be
    compared against what is now a different page. Under-reporting on the build that
    changes the length beats inventing a regression that never happened."""
    project = _project(tmp_path)
    _pass(project, [], pages=3)

    assert _compare(project, [_finding("word_collision", 2)], pages=4) == ()


def test_it_says_whether_the_page_s_own_code_changed(tmp_path: Path) -> None:
    """The author has to tell "my edit did this" from "something shared did this":
    a page broken without its own block changing was broken by the prelude, by a
    helper it calls, or by the design pass."""
    project = _project(tmp_path)
    _pass(project, [])

    mine = _compare(project, [_finding("word_collision", 1)], script=_EDITED)
    assert mine[0].recoded is True

    elsewhere = _compare(project, [_finding("word_collision", 1)], script=_SCRIPT)
    assert elsewhere[0].recoded is False


def test_a_deck_wide_finding_is_not_attributed_to_any_page(tmp_path: Path) -> None:
    """The three findings a draft is not held to -- page_budget, page_mapping,
    unseen_page -- are all of this shape, which is what keeps a draft and a
    finished build comparable page by page."""
    project = _project(tmp_path)
    _pass(project, [])

    deck_wide = Finding(kind="page_budget", severity=Severity.BLOCKING, message="too long")

    # Asserted on the grouping rather than only on the comparison: a deck-wide
    # finding attributed to every page would be caught here and nowhere else.
    assert regress.blocking_kinds_by_page([deck_wide], _ROUTE_FATAL) == {}
    assert _compare(project, [deck_wide]) == ()


def test_the_record_covers_every_measured_page_not_only_the_ones_shown(tmp_path: Path) -> None:
    """A page nobody looked at can still be broken by an edit to a page somebody did."""
    project = _project(tmp_path)
    _pass(project, [_finding("word_collision", 3)])

    stored = json.loads(regress.blocking_path(project).read_text(encoding="utf-8"))

    assert sorted(stored["blocking"]) == ["1", "2", "3"]
    assert stored["blocking"]["3"]["kinds"] == ["word_collision"]


def test_a_record_this_version_cannot_read_is_the_same_as_no_record(tmp_path: Path) -> None:
    project = _project(tmp_path)
    path = regress.blocking_path(project)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ not json", encoding="utf-8")

    assert _compare(project, [_finding("word_collision", 1)]) == ()

    path.write_text(json.dumps({"schema": "something.else.v9", "pages": 3, "blocking": {}}), encoding="utf-8")

    assert _compare(project, [_finding("word_collision", 1)]) == ()


def test_a_program_with_no_page_blocks_records_nothing(tmp_path: Path) -> None:
    """No mapping from page to code means no page identity to key a record on."""
    project = _project(tmp_path)

    regress.record(
        project,
        script=_SCRIPT,
        sources=(),
        findings=[_finding("word_collision", 1)],
        blocking_kinds=_ROUTE_FATAL,
        pages=3,
    )

    assert not regress.blocking_path(project).exists()


def test_forget_drops_the_baseline(tmp_path: Path) -> None:
    project = _project(tmp_path)
    _pass(project, [_finding("word_collision", 1)])
    assert regress.blocking_path(project).is_file()

    regress.forget(project)

    assert _compare(project, [_finding("word_collision", 1), _finding("off_page", 1)]) == ()
