"""Delivery, and the three ways the predecessor lost control of it."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven_ppt.contracts import Finding, Project, Severity
from raven_ppt.services.publish import PublishRefusedError, publish, stage
from raven_ppt.services.publish.deliver import (
    delivery_report,
    published_digests,
    tampered_delivery,
    unrecorded_deliveries,
)


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


# --- the record against the file it names ---------------------------------------


def test_the_record_names_the_bytes_that_reached_the_path(project: Project) -> None:
    """Read back off the delivered file, not taken from the copy it was made from.

    The record's whole promise is "this sha256 is the file at this path", and the only
    way to keep it is to hash the file at that path after writing it.
    """
    import hashlib

    staged = stage(project, _deck(project), pages=4)
    out = publish(project, staged, project.exports_dir / "deck.pptx", findings=[], blocking_kinds=frozenset())

    assert published_digests(project.state_dir) == {hashlib.sha256(out.read_bytes()).hexdigest()}
    assert tampered_delivery(project.state_dir, out) is None


def test_a_delivery_edited_where_it_lies_is_named_and_not_believed(project: Project) -> None:
    """The live failure this reading exists for.

    A run holding a deck it had already delivered ran `python3 - # Apply the same fix to
    the published deck (out/deck.pptx) in place` through `exec` and rewrote the delivery
    where it lay. Nothing was copied anywhere -- the build directory and the staged copy
    both still held the recorded bytes -- so every record in the deck folder went on
    describing a file that no longer existed, and the user held a deck no gate had seen.
    """
    staged = stage(project, _deck(project), pages=4)
    out = publish(project, staged, project.exports_dir / "deck.pptx", findings=[], blocking_kinds=frozenset())

    out.write_bytes(b"PK\x03\x04 edited in place")

    report = tampered_delivery(project.state_dir, out)
    assert report is not None
    assert "changed after it was published" in report
    assert "never in the delivered file" in report
    assert unrecorded_deliveries(project.state_dir) == [report]
    # And the same answer through the entry point the stage uses, which resolves the
    # destination the way `publish` does.
    assert delivery_report(project, project.exports_dir / "deck.pptx") == report


def test_the_next_publish_puts_the_measured_deck_back_and_the_record_agrees(project: Project) -> None:
    """The repair is the publish itself: the gated deck is written over the edited one.

    Which is why this reads and never refuses -- the user must still get a deck, and the
    deck they should get is the one that passed.
    """
    import hashlib

    staged = stage(project, _deck(project), pages=4)
    out = publish(project, staged, project.exports_dir / "deck.pptx", findings=[], blocking_kinds=frozenset())
    out.write_bytes(b"PK\x03\x04 edited in place")

    again = stage(project, _deck(project, b"PK\x03\x04 rebuilt"), pages=4)
    out = publish(project, again, project.exports_dir / "deck.pptx", findings=[], blocking_kinds=frozenset())

    assert out.read_bytes() == b"PK\x03\x04 rebuilt"
    assert unrecorded_deliveries(project.state_dir) == []
    assert hashlib.sha256(out.read_bytes()).hexdigest() in published_digests(project.state_dir)


def test_a_record_that_cannot_be_written_is_not_a_silent_delivery(project: Project) -> None:
    """A delivery with no record is one the harness reads as a copy the model made.

    The write is small and the deck is megabytes, so this is the unlikely half of the
    pair -- and it is the half that used to raise a bare OSError past a caller catching
    only `PublishRefusedError`, leaving the file in place and the record describing the
    build before it.
    """
    from raven_ppt.services.publish import deliver

    staged = stage(project, _deck(project), pages=4)
    record = project.state_dir / deliver.PUBLISHED_RECORD
    record.mkdir()  # a directory where the record goes: os.replace onto it fails

    with pytest.raises(PublishRefusedError, match="its record could not be"):
        publish(project, staged, project.exports_dir / "deck.pptx", findings=[], blocking_kinds=frozenset())


def test_a_deck_with_no_record_is_not_reported_as_changed(project: Project) -> None:
    """Nothing recorded for a path is not a claim about it, so there is nothing to break."""
    loose = project.exports_dir / "someone_elses.pptx"
    loose.parent.mkdir(parents=True, exist_ok=True)
    loose.write_bytes(b"PK\x03\x04 not ours")

    assert tampered_delivery(project.state_dir, loose) is None
    assert unrecorded_deliveries(project.state_dir) == []
