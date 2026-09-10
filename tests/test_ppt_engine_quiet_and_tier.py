"""What a build stops repeating, and what a tier caps.

Six recorded deck runs were read build by build (docs/ppt-raven-design.md D35):
four kinds of finding drove the author round in circles without changing the
deck, the reader's replies described the wrong page fourteen times, and every
useful fix was in by the tenth whole-deck build and the third reading.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven_ppt.contracts import Project
from raven_ppt.contracts.brief import DeckBrief, PageBudget
from raven_ppt.contracts.findings import Finding, Severity
from raven_ppt.contracts.outline import Outline, PagePlan, outline_path, write_outline
from raven_ppt.services import review_ledger, tier
from raven_ppt.services.gates import quiet
from raven_ppt.tools.review import _headline_on_page

pytest.importorskip("pptx")


def _project(tmp_path: Path) -> Project:
    project = Project(workspace=tmp_path, slug="deck")
    for d in (project.state_dir, project.review_dir, project.build_dir, project.ingest_dir):
        d.mkdir(parents=True, exist_ok=True)
    return project


def _finding(kind: str, page: int, **detail) -> Finding:
    return Finding(kind=kind, severity=Severity.WARNING, message=kind, page=page, detail=detail)


def test_a_stood_in_page_carries_only_its_failure(tmp_path: Path) -> None:
    project = _project(tmp_path)
    kept = quiet.quiet(
        project,
        [
            Finding(kind="page_failed", severity=Severity.BLOCKING, message="page 4 did not draw", page=4),
            _finding("type_floor", 4),
            _finding("repeated_layout", 4, same_as=3),
            _finding("type_floor", 5),
        ],
        stood_in={4},
    )
    assert [(f.kind, f.page) for f in kept] == [("page_failed", 4), ("type_floor", 5)]


def test_adjacent_pages_the_outline_gives_one_shape_may_match(tmp_path: Path) -> None:
    project = _project(tmp_path)
    write_outline(
        Outline(
            takeaway="t",
            pages=(
                PagePlan(page=12, claim="matrix", layout="matrix"),
                PagePlan(page=13, claim="PolySkill", prototype=7, layout="paper"),
                PagePlan(page=14, claim="ReasoningBank", prototype=7, layout="paper"),
                PagePlan(page=15, claim="ACE", prototype=7, layout="paper"),
                PagePlan(page=16, claim="XSkill", layout="cards"),
            ),
        ),
        outline_path(project),
    )
    kept = quiet.quiet(
        project,
        [
            _finding("repeated_layout", 14, same_as=13),
            _finding("repeated_layout", 15, same_as=14),
            _finding("repeated_layout", 16, same_as=15),
            _finding("repeated_layout", 13, same_as=12),
        ],
    )
    assert [f.page for f in kept] == [16, 13], "the confirmed uniform pages are quiet; a change of shape is not"


def test_air_is_not_a_finding_when_the_brief_asked_for_it(tmp_path: Path) -> None:
    from raven_ppt.contracts.brief import brief_path, write_brief

    project = _project(tmp_path)
    findings = [_finding("excessive_whitespace", 3), _finding("sparse_container", 4), _finding("box_overflow", 5)]
    assert [f.kind for f in quiet.quiet(project, findings)] == [
        "excessive_whitespace",
        "sparse_container",
        "box_overflow",
    ]
    write_brief(
        DeckBrief(
            language="zh", audience="组会", pages=PageBudget(25, 30), notes=("风格：白底、留白充足、单色系强调色",)
        ),
        brief_path(project),
    )
    assert [f.kind for f in quiet.quiet(project, findings)] == ["box_overflow"]


def test_prototype_kept_is_said_once_per_page(tmp_path: Path) -> None:
    project = _project(tmp_path)
    first = quiet.quiet(project, [_finding("prototype_kept", 1), _finding("prototype_kept", 6)])
    second = quiet.quiet(
        project, [_finding("prototype_kept", 1), _finding("prototype_kept", 6), _finding("prototype_kept", 34)]
    )
    assert [f.page for f in first] == [1, 6]
    assert [f.page for f in second] == [34]


def test_the_tier_file_round_trips_and_an_empty_overlay_clears_the_caps(tmp_path: Path) -> None:
    caps = tier.write_mode(tmp_path, {"buildCap": 10, "readingCap": 3}, "high")
    assert caps == tier.Caps(mode="high", build_cap=10, reading_cap=3)
    assert tier.read_caps(tmp_path) == caps
    assert tier.write_mode(tmp_path, {}, "max") == tier.Caps(mode="max")
    assert tier.read_caps(tmp_path) == tier.Caps(mode="max")
    assert tier.read_caps(tmp_path / "nowhere") == tier.Caps()
    assert tier.write_mode(tmp_path, {"buildCap": "ten", "readingCap": 0}, "high") == tier.Caps(mode="high")


def test_whole_builds_are_counted_on_disk(tmp_path: Path) -> None:
    project = _project(tmp_path)
    assert tier.whole_builds_taken(project) == 0
    assert tier.count_whole_build(project) == 1
    assert tier.count_whole_build(project) == 2
    assert tier.whole_builds_taken(project) == 2


def test_a_lost_page_is_counted_against_its_own_bounded_allowance(tmp_path: Path) -> None:
    """`CRASH_REPRIEVES` of them, and then a lost page costs a whole build like anything
    else -- so the total is the tier's cap plus the allowance and not more, and a deck
    cannot extend its run by crashing."""
    project = _project(tmp_path)
    assert tier.reprieves_taken(project) == 0
    assert [tier.count_lost_build(project) for _ in range(tier.CRASH_REPRIEVES)] == [1, 2]
    assert (tier.whole_builds_taken(project), tier.reprieves_taken(project)) == (0, 2)

    assert tier.count_lost_build(project) is None, "the allowance is spent, so the caller counts it"
    assert (tier.whole_builds_taken(project), tier.reprieves_taken(project)) == (0, 2)
    # And the two counts do not overwrite each other on the one file they share.
    assert tier.count_whole_build(project) == 1
    assert tier.reprieves_taken(project) == 2


def test_taste_kinds_are_answered_in_the_reply_and_not_carried(tmp_path: Path) -> None:
    project = _project(tmp_path)
    read = {
        3: [
            {"kind": "type", "where": "line 2", "what": "a space before the full stop", "fix": ""},
            {"kind": "alignment", "where": "rule", "what": "half the width of its word", "fix": ""},
            {"kind": "listed", "where": "cards", "what": "three parallel cards", "fix": ""},
            {"kind": "claim", "where": "page", "what": "the claim is elsewhere", "fix": ""},
        ]
    }
    moved = review_ledger.record_reading(project, read, {3: "v1"}, 1)
    assert moved["opened"] == 1
    assert [entry["kind"] for entry in review_ledger.open_findings(project)] == ["claim"]


@pytest.mark.parametrize(
    ("headline", "text", "on_page"),
    [
        ("Self-Evolving Agent Skills 2026 深度调研", "Self-Evolving Agent Skills 2026 深度调研 15 篇论文", True),
        ("目录", "Self-Evolving Agent Skills 2026 深度调研 15 篇论文", False),
        ("PolySkill：抽象接口 + 环境实现", "PolySkill ：抽象接口 + 环境实现 + 多态分发 问题：", True),
        ("", "anything", True),
        ("Anything at all", "", True),
    ],
)
def test_a_headline_has_to_be_on_the_page_it_was_read_from(headline: str, text: str, on_page: bool) -> None:
    assert _headline_on_page(headline, text) is on_page
