"""Detecting an environment an upgrade has not finished writing.

The case these guard: `system.upgrade` makes serve exit, a supervisor respawns
it, and the respawn lands between uv deleting the old environment and writing
the new one. Nothing downstream can recover from that on its own -- the page
route is chosen once -- so the detection here is the whole of the fix.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from raven.cli import _install_guard as guard


@pytest.fixture(autouse=True)
def raven_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RAVEN_HOME", str(home))
    return home


@pytest.fixture
def whole_install(monkeypatch: pytest.MonkeyPatch) -> None:
    """An installation with nothing missing, so markers decide alone."""
    monkeypatch.setattr(guard, "missing_pieces", lambda: [])


class FakeDistribution:
    """Stands in for the installed raven distribution and its RECORD."""

    def __init__(self, record: str | None, root: Path) -> None:
        self._record = record
        self._root = root

    def read_text(self, name: str) -> str | None:
        return self._record if name == "RECORD" else None

    def locate_file(self, path: str) -> Path:
        return self._root / path


class TestTheMarker:
    def test_it_round_trips_what_the_helper_needs(self, raven_home: Path) -> None:
        path = guard.write_marker(to_version="1.2.3")
        assert path == raven_home / guard.MARKER_NAME

        marker = guard.read_marker()
        assert marker is not None
        assert marker.to_version == "1.2.3"
        assert marker.pid is None
        assert marker.started_at > 0

    def test_it_reads_as_absent_when_the_file_is_garbage(self, raven_home: Path) -> None:
        (raven_home / guard.MARKER_NAME).write_text("{not json", encoding="utf-8")
        assert guard.read_marker() is None

    def test_it_reads_as_absent_without_a_start_time(self, raven_home: Path) -> None:
        (raven_home / guard.MARKER_NAME).write_text(json.dumps({"pid": 1}), encoding="utf-8")
        assert guard.read_marker() is None

    def test_clearing_a_marker_that_is_not_there_is_not_an_error(self) -> None:
        guard.clear_marker()
        guard.clear_marker()

    def test_it_survives_a_home_that_does_not_exist_yet(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "not-created"))
        guard.write_marker()
        assert guard.read_marker() is not None


class TestWhetherTheUpgradeIsStillRunning:
    def test_a_helper_that_is_running_is_live(self) -> None:
        marker = guard.UpgradeMarker(started_at=0.0, pid=os.getpid())
        assert guard.marker_is_live(marker, now=1.0)

    def test_a_helper_that_is_gone_is_not(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(guard, "_process_alive", lambda pid: False)
        marker = guard.UpgradeMarker(started_at=0.0, pid=4242)
        assert not guard.marker_is_live(marker, now=1.0)

    def test_a_marker_with_no_helper_yet_is_believed_briefly(self) -> None:
        marker = guard.UpgradeMarker(started_at=0.0)
        assert guard.marker_is_live(marker, now=guard._UNPARENTED_GRACE_S - 1)
        assert not guard.marker_is_live(marker, now=guard._UNPARENTED_GRACE_S + 1)

    def test_a_wedged_helper_stops_holding_the_installation_shut(self) -> None:
        """A helper stuck on a dead socket must not lock the user out forever."""
        marker = guard.UpgradeMarker(started_at=0.0, pid=os.getpid())
        assert not guard.marker_is_live(marker, now=guard._MAX_MARKER_AGE_S + 1)


class TestWhatIsMissing:
    def test_a_build_that_never_shipped_a_page_is_not_missing_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(guard, "_packaged_page_is_owed", lambda: None)
        assert guard.missing_pieces() == []

    def test_a_promised_page_that_is_absent_is_missing(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        monkeypatch.setattr(guard, "_packaged_page_is_owed", lambda: tmp_path / "gone" / "index.html")
        assert guard.missing_pieces() == ["the packaged page (raven/ui/dist)"]

    def test_a_promised_page_that_is_there_is_not(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        page = tmp_path / "index.html"
        page.write_text("<html></html>", encoding="utf-8")
        monkeypatch.setattr(guard, "_packaged_page_is_owed", lambda: page)
        assert guard.missing_pieces() == []

    def test_the_page_is_read_from_the_record_not_assumed(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        record = "raven/cli/serve_commands.py,sha256=abc,10\nraven/ui/dist/index.html,sha256=def,944282\n"
        monkeypatch.setattr(guard.metadata, "distribution", lambda name: FakeDistribution(record, tmp_path))

        assert guard._packaged_page_is_owed() == tmp_path / guard.PAGE_RECORD_ENTRY

    def test_no_page_entry_means_none_was_promised(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        record = "raven/cli/serve_commands.py,sha256=abc,10\n"
        monkeypatch.setattr(guard.metadata, "distribution", lambda name: FakeDistribution(record, tmp_path))

        assert guard._packaged_page_is_owed() is None

    def test_a_record_that_cannot_be_read_promises_nothing(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setattr(guard.metadata, "distribution", lambda name: FakeDistribution(None, tmp_path))
        assert guard._packaged_page_is_owed() is None

    def test_the_promise_is_read_from_the_record_even_when_the_file_is_gone(self) -> None:
        """The regression that made the first version of this check useless:
        `importlib.metadata.files` omits entries whose files are missing, so it
        reports on what survived, never on what was owed. Asserted against this
        very installation, whose RECORD is real."""
        record = guard.metadata.distribution("raven").read_text("RECORD") or ""
        promised = {line.split(",", 1)[0] for line in record.splitlines() if line.strip()}
        listed = {entry.as_posix() for entry in (guard.metadata.files("raven") or ())}

        assert promised >= listed

    def test_missing_metadata_is_reported_and_stops_the_search(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """uv removes the distribution before writing the new one, so this is
        what a start landing in the middle of the replacement actually sees."""

        def gone(_name: str) -> str:
            raise guard.metadata.PackageNotFoundError("raven")

        monkeypatch.setattr(guard.metadata, "version", gone)
        assert guard.missing_pieces() == ["its own package metadata"]


class TestTheVerdict:
    def test_a_sound_installation_has_no_fault(self, whole_install: None) -> None:
        assert guard.inspect_install() is None

    def test_a_running_upgrade_is_named_as_one(self, whole_install: None) -> None:
        guard.write_marker(to_version="9.9.9", pid=os.getpid())
        fault = guard.inspect_install()
        assert fault is not None
        assert fault.reason == "upgrading"
        assert "9.9.9" in fault.detail

    def test_an_incomplete_installation_is_named_as_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(guard, "missing_pieces", lambda: ["the packaged page (raven/ui/dist)"])
        fault = guard.inspect_install()
        assert fault is not None
        assert fault.reason == "incomplete"
        assert "raven/ui/dist" in fault.detail

    def test_an_interrupted_helper_that_still_landed_a_whole_install_heals(
        self, whole_install: None, monkeypatch: pytest.MonkeyPatch, raven_home: Path
    ) -> None:
        """The kill arrived after uv had finished. Nothing is missing, so the
        marker is stale bookkeeping and must not outlive the check that read it."""
        monkeypatch.setattr(guard, "_process_alive", lambda pid: False)
        guard.write_marker(to_version="9.9.9", pid=4242)

        assert guard.inspect_install() is None
        assert not (raven_home / guard.MARKER_NAME).exists()

    def test_an_interrupted_helper_that_left_a_hole_reports_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(guard, "_process_alive", lambda pid: False)
        monkeypatch.setattr(guard, "missing_pieces", lambda: ["its own package metadata"])
        guard.write_marker(pid=4242)

        fault = guard.inspect_install()
        assert fault is not None
        assert fault.reason == "incomplete"

    def test_a_live_upgrade_outranks_a_half_written_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Mid-install the environment is legitimately incomplete; saying so
        would send the reader to repair an upgrade that is still working."""
        monkeypatch.setattr(guard, "missing_pieces", lambda: ["its own package metadata"])
        guard.write_marker(pid=os.getpid())

        fault = guard.inspect_install()
        assert fault is not None
        assert fault.reason == "upgrading"
