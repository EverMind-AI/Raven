"""The contracts' own rules: the ones other layers rely on without checking."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.ppt.contracts import (
    BuildOutcome,
    Capabilities,
    DeckPlan,
    Finding,
    PageSource,
    PageSpec,
    Profile,
    Project,
    Severity,
    StageSpec,
    blocking,
    warnings,
)


def test_a_finding_needs_something_the_model_can_act_on() -> None:
    with pytest.raises(ValueError, match="message"):
        Finding(kind="fact", severity=Severity.BLOCKING, message="")
    with pytest.raises(ValueError, match="kind"):
        Finding(kind="", severity=Severity.WARNING, message="something")


def test_finding_detail_cannot_be_mutated_through_the_caller_s_dict() -> None:
    detail = {"page": 3}
    finding = Finding(kind="type_floor", severity=Severity.WARNING, message="too small", detail=detail)
    detail["page"] = 9
    assert finding.detail["page"] == 3
    with pytest.raises(TypeError):
        finding.detail["page"] = 9  # type: ignore[index]


def test_findings_split_by_severity() -> None:
    a = Finding(kind="fact", severity=Severity.BLOCKING, message="unanchored 48.3")
    b = Finding(kind="density", severity=Severity.WARNING, message="295 words")
    assert blocking([a, b]) == [a]
    assert warnings([a, b]) == [b]


def test_project_refuses_a_slug_that_would_escape_the_workspace() -> None:
    for bad in ["../etc", "a/b", "Tar ViS", "", "-lead"]:
        with pytest.raises(ValueError):
            Project(workspace=Path("/ws"), slug=bad)


def test_project_paths_all_sit_under_the_project_root(tmp_path: Path) -> None:
    project = Project(workspace=tmp_path, slug="tarvis")
    project.root.mkdir(parents=True)
    for path in [project.ingest_dir, project.figures_dir, project.build_dir, project.review_dir, project.state_dir]:
        assert project.contains(path), path
    assert not project.contains(tmp_path / "exports")


def test_a_deck_plan_has_no_gaps() -> None:
    pages = (PageSpec(number=1, section="intro", headline="A"), PageSpec(number=3, section="body", headline="B"))
    with pytest.raises(ValueError, match="without gaps"):
        DeckPlan(title="d", pages=pages)


def test_a_page_spec_says_what_the_page_is_about() -> None:
    with pytest.raises(ValueError, match="headline"):
        PageSpec(number=1, section="intro", headline="   ")


def test_build_outcome_finds_the_code_that_drew_a_page() -> None:
    outcome = BuildOutcome(
        ok=True,
        pptx_path=Path("/deck.pptx"),
        pages=2,
        sources=(PageSource(page=1, first_line=10, last_line=20), PageSource(page=2, first_line=21, last_line=30)),
    )
    assert outcome.source_for(2) == PageSource(page=2, first_line=21, last_line=30)
    assert outcome.source_for(3) is None


def test_no_profile_may_hand_the_model_a_physical_quantity() -> None:
    stages = (StageSpec(name="build", tool="ppt_build"),)
    with pytest.raises(ValueError, match="physical geometry or a font size"):
        Profile(name="bad", backend="script", stages=stages, capabilities=Capabilities(physical_geometry=True))
    with pytest.raises(ValueError, match="physical geometry or a font size"):
        Profile(name="bad", backend="script", stages=stages, capabilities=Capabilities(font_size=True))


def test_a_profile_lists_its_tools_in_stage_order() -> None:
    profile = Profile(
        name="script_author",
        backend="script",
        stages=(
            StageSpec(name="ingest", tool="ppt_ingest"),
            StageSpec(name="build", tool="ppt_build"),
            StageSpec(name="publish", tool=None),
        ),
    )
    assert profile.tools == ("ppt_ingest", "ppt_build")
    assert profile.stage("publish") is not None and profile.stage("publish").tool is None
