"""Delivery, and the three ways the predecessor lost control of it."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.ppt.contracts import Finding, Project, Severity
from raven.ppt.services.publish import PublishRefusedError, publish, stage


@pytest.fixture()
def project(tmp_path: Path) -> Project:
    p = Project(workspace=tmp_path, slug="tarvis")
    p.build_dir.mkdir(parents=True)
    p.state_dir.mkdir(parents=True)
    return p


def _deck(project: Project, body: bytes = b"PK\x03\x04 deck") -> Path:
    path = project.build_dir / "deck.pptx"
    path.write_bytes(body)
    return path


def _fact() -> Finding:
    return Finding(kind="fact", severity=Severity.BLOCKING, message="48.3 is not in the sources", page=11)


def _warning() -> Finding:
    return Finding(kind="type_floor", severity=Severity.WARNING, message="11.5pt body", page=3)


def test_a_deck_is_delivered_atomically_to_the_named_path(project: Project) -> None:
    staged = stage(project, _deck(project), pages=18)
    out = publish(project, staged, project.exports_dir / "TarViS.pptx", findings=[], blocking_kinds=frozenset())
    assert out.is_file() and out.read_bytes() == b"PK\x03\x04 deck"
    assert not list(out.parent.glob(".*tmp"))


def test_a_warning_does_not_stop_delivery(project: Project) -> None:
    """Measurements of the rendered page warn and ride along -- see design D2."""
    staged = stage(project, _deck(project))
    out = publish(
        project, staged, project.exports_dir / "d.pptx", findings=[_warning()], blocking_kinds=frozenset({"band"})
    )
    assert out.is_file()


def test_the_gate_cannot_be_forgotten_because_it_is_an_argument(project: Project) -> None:
    """Fail-closed that depends on being remembered is not fail-closed.

    The route that forgot it shipped a deck with seventeen colour bars and an
    unanchored number, because its publish step was a bare file copy.
    """
    staged = stage(project, _deck(project))
    with pytest.raises(TypeError):
        publish(project, staged, project.exports_dir / "d.pptx")  # type: ignore[call-arg]


def test_a_blocking_finding_refuses_and_names_what_stands(project: Project) -> None:
    staged = stage(project, _deck(project))
    with pytest.raises(PublishRefusedError, match="1 fact"):
        publish(project, staged, project.exports_dir / "d.pptx", findings=[_fact()], blocking_kinds=frozenset())
    assert not (project.exports_dir / "d.pptx").exists()


def test_a_kind_the_route_calls_fatal_refuses_even_at_warning_severity(project: Project) -> None:
    """A route's own kind, which the shared checks do not rank at all.

    `unmapped_page` is built inside the route that cares whether a page traces to
    its own code, so severity is not where its weight lives. The example used to be
    `band`, and that was the bug: the gate reported it, one route still called it
    fatal, and this test froze the contradiction as intended behaviour.
    """
    orphan = Finding(kind="unmapped_page", severity=Severity.WARNING, message="page 4 maps to no block", page=4)
    staged = stage(project, _deck(project))
    with pytest.raises(PublishRefusedError, match="1 unmapped_page"):
        publish(
            project,
            staged,
            project.exports_dir / "d.pptx",
            findings=[orphan],
            blocking_kinds=frozenset({"unmapped_page"}),
        )


def test_a_deck_that_changed_after_it_was_checked_is_refused(project: Project) -> None:
    """The findings describe a file that no longer exists."""
    staged = stage(project, _deck(project))
    staged.path.write_bytes(b"PK\x03\x04 something else")
    with pytest.raises(PublishRefusedError, match="changed after it was checked"):
        publish(project, staged, project.exports_dir / "d.pptx", findings=[], blocking_kinds=frozenset())


def test_staging_keeps_the_build_directory_copy(project: Project) -> None:
    """It is what a failed edit gets repaired against."""
    built = _deck(project)
    stage(project, built)
    assert built.is_file()


@pytest.mark.parametrize(
    "destination",
    ["../outside.pptx", "/etc/deck.pptx", "exports/deck.txt", "exports"],
)
def test_delivery_outside_the_workspace_or_of_the_wrong_kind_is_refused(project: Project, destination: str) -> None:
    (project.workspace / "exports").mkdir(exist_ok=True)
    staged = stage(project, _deck(project))
    with pytest.raises(PublishRefusedError):
        publish(project, staged, Path(destination), findings=[], blocking_kinds=frozenset())


def test_an_empty_build_is_not_stageable(project: Project) -> None:
    empty = project.build_dir / "deck.pptx"
    empty.write_bytes(b"")
    with pytest.raises(PublishRefusedError, match="empty"):
        stage(project, empty)


def test_a_missing_build_says_where_it_looked(project: Project) -> None:
    with pytest.raises(PublishRefusedError, match="no deck at"):
        stage(project, project.build_dir / "nope.pptx")
