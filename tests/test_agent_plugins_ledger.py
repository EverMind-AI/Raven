"""The Agent Plugin package ledger: what was imported, where, and what it owns.

The ledger is what makes removal safe -- only the directories it can prove
it installed may ever be deleted -- so its load (including the v1 and
future-version files), its round-trip, its workspace scoping and its
marker-checked ownership are the things that decide whether an uninstall
touches anything it should not.
"""

from __future__ import annotations

import builtins
import io
import json
import os
import shutil
import stat
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent_plugins.ledger import (
    INSTALL_MARKER_FILENAME,
    LEDGER_VERSION,
    LedgerEntry,
    LedgerError,
    PackageLedger,
    read_install_marker,
    workspace_key,
    write_install_marker,
)

PACKAGE_NAME = "demo-pack"


def _record_install(
    ledger: PackageLedger,
    workspace: Path,
    name: str = PACKAGE_NAME,
    skills: tuple[str, ...] = ("alpha",),
    **overrides: object,
) -> LedgerEntry:
    """Record an entry the way an import would.

    The directories on disk carry the very markers the entry's tokens came
    from, so a claim against them answers the way a real install would.
    """
    root = workspace / "skills" / name
    root.mkdir(parents=True, exist_ok=True)
    tokens: dict[str, str] = {}
    for skill in skills:
        directory = root / skill
        directory.mkdir(parents=True, exist_ok=True)
        tokens[skill] = write_install_marker(directory)
    fields: dict[str, object] = {
        "name": name,
        "version": "1.0.0",
        "source": str(workspace / "src" / name),
        "imported_at": "2026-10-25T00:00:00Z",
        "skills": skills,
        "fingerprint": "sha256:0123456789",
        "workspace": workspace_key(workspace),
        "install_token": write_install_marker(root),
        "skill_tokens": tokens,
    }
    fields.update(overrides)
    entry = LedgerEntry(**fields)  # type: ignore[arg-type]
    ledger.record(entry)
    return entry


def _link_directory(target: Path, link: Path) -> None:
    """A directory link pointing at *target* (a symlink, or a junction on Windows)."""
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link, target_is_directory=True)
        return
    except (OSError, NotImplementedError, AttributeError):
        pass
    if os.name != "nt":
        pytest.skip("this platform cannot create a directory link")
    result = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(link), str(target)],
        capture_output=True,
    )
    if result.returncode != 0:
        pytest.skip(
            "cannot create a directory link: "
            + result.stdout.decode("utf-8", "replace").strip()
            + " "
            + result.stderr.decode("utf-8", "replace").strip()
        )


def _write_v1_ledger(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "packages": {
                    PACKAGE_NAME: {
                        "version": "1.0.0",
                        "source": "/tmp/demo-pack",
                        "imported_at": "2026-10-25T00:00:00Z",
                        "skills": ["alpha"],
                        "fingerprint": "sha256:0123456789",
                    }
                },
            }
        ),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Install markers
# ---------------------------------------------------------------------------


def test_an_install_marker_round_trips_through_its_file(tmp_path: Path) -> None:
    directory = tmp_path / "skill"
    directory.mkdir()

    token = write_install_marker(directory)

    assert read_install_marker(directory) == token
    payload = json.loads((directory / INSTALL_MARKER_FILENAME).read_text(encoding="utf-8"))
    assert payload == {"token": token}


def test_two_markers_never_share_a_token(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()

    assert write_install_marker(first) != write_install_marker(second)


def test_a_missing_or_corrupt_marker_reads_as_no_token(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    corrupt = tmp_path / "corrupt"
    corrupt.mkdir()
    (corrupt / INSTALL_MARKER_FILENAME).write_text("{not json at all", encoding="utf-8")
    alien = tmp_path / "alien"
    alien.mkdir()
    (alien / INSTALL_MARKER_FILENAME).write_text(json.dumps({"token": ""}), encoding="utf-8")

    assert read_install_marker(empty) == ""
    assert read_install_marker(corrupt) == ""
    assert read_install_marker(alien) == ""


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def test_load_on_a_missing_file_gives_an_empty_ledger(tmp_path: Path) -> None:
    ledger = PackageLedger.load(tmp_path / "ledger.json")

    workspace = tmp_path / "workspace"
    assert ledger.entries_for(workspace) == {}
    assert ledger.entry(PACKAGE_NAME, workspace=workspace) is None
    assert ledger.unrecognized == ""


def test_load_on_a_corrupt_file_gives_an_empty_ledger_that_refuses_to_save(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text("{not json at all", encoding="utf-8")
    before = path.read_bytes()

    ledger = PackageLedger.load(path)

    assert ledger.unrecognized
    assert ledger.entries_for(tmp_path / "workspace") == {}
    with pytest.raises(LedgerError):
        ledger.save()
    assert path.read_bytes() == before


def test_load_ignores_a_file_that_is_not_an_object_and_refuses_to_save(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text('["demo-pack"]', encoding="utf-8")

    ledger = PackageLedger.load(path)

    assert ledger.unrecognized
    assert ledger.entries_for(tmp_path / "workspace") == {}
    with pytest.raises(LedgerError):
        ledger.save()


def test_a_future_version_loads_empty_and_is_never_overwritten(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"version": 99, "packages": {}}), encoding="utf-8")
    before = path.read_bytes()

    ledger = PackageLedger.load(path)

    assert "version 99" in ledger.unrecognized
    with pytest.raises(LedgerError):
        ledger.save()
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "payload",
    [
        {"version": 2, "workspaces": "not-a-mapping"},
        {"version": 2, "workspaces": {"c:\\ws": ["not-a-mapping"]}},
        {"version": 2, "legacy": "not-a-mapping"},
        {"version": 1, "packages": "not-a-mapping"},
    ],
)
def test_a_structurally_damaged_ledger_refuses_to_save(tmp_path: Path, payload: dict) -> None:
    """A section that is present but unreadable is never coerced to empty."""
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    before = path.read_bytes()

    ledger = PackageLedger.load(path)

    assert ledger.unrecognized
    with pytest.raises(LedgerError):
        ledger.save()
    assert path.read_bytes() == before


def test_a_version_two_file_without_sections_is_an_empty_ledger(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    path.write_text(json.dumps({"version": 2}), encoding="utf-8")

    ledger = PackageLedger.load(path)

    assert ledger.unrecognized == ""
    assert ledger.entries_for(tmp_path / "workspace") == {}
    ledger.save()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["workspaces"] == {}
    assert payload["legacy"] == {}


# ---------------------------------------------------------------------------
# The v1 to v2 migration
# ---------------------------------------------------------------------------


def test_a_v1_file_loads_as_unverified_legacy_records(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    _write_v1_ledger(path)

    ledger = PackageLedger.load(path)

    entry = ledger.legacy_entry(PACKAGE_NAME)
    assert entry is not None
    assert entry.unverified
    assert entry.version == "1.0.0"
    assert entry.source == "/tmp/demo-pack"
    assert entry.skills == ("alpha",)
    assert entry.fingerprint == "sha256:0123456789"
    assert ledger.entry(PACKAGE_NAME, workspace=tmp_path / "workspace") is None


def test_a_v1_record_survives_save_and_reload_still_unverified(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    _write_v1_ledger(path)

    ledger = PackageLedger.load(path)
    ledger.save()

    assert json.loads(path.read_text(encoding="utf-8"))["version"] == LEDGER_VERSION
    reloaded = PackageLedger.load(path)
    entry = reloaded.legacy_entry(PACKAGE_NAME)
    assert entry is not None
    assert entry.unverified
    reloaded.save()
    assert PackageLedger.load(path).legacy_entry(PACKAGE_NAME).unverified


def test_a_verified_record_supersedes_the_legacy_record_of_the_same_name(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    _write_v1_ledger(path)
    ledger = PackageLedger.load(path)
    workspace = tmp_path / "workspace"

    _record_install(ledger, workspace)
    ledger.save()

    reloaded = PackageLedger.load(path)
    assert reloaded.legacy_entry(PACKAGE_NAME) is None
    assert reloaded.entry(PACKAGE_NAME, workspace=workspace) is not None


def test_a_record_in_another_workspace_supersedes_the_legacy_record(tmp_path: Path) -> None:
    """A version 1 record carries no workspace, so a fresh import anywhere replaces it."""
    path = tmp_path / "ledger.json"
    _write_v1_ledger(path)
    ledger = PackageLedger.load(path)
    workspace_a = tmp_path / "workspace-a"
    _record_install(ledger, workspace_a)
    ledger.save()

    # Put the v1 record back beside workspace A's verified one: a version 2
    # file can carry both sections, and only then does "another workspace"
    # mean something distinct from "the workspace that already has a record".
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["legacy"] = {
        PACKAGE_NAME: {
            "version": "1.0.0",
            "source": "/tmp/demo-pack",
            "imported_at": "2026-10-25T00:00:00Z",
            "skills": ["alpha"],
            "fingerprint": "sha256:0123456789",
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    ledger = PackageLedger.load(path)
    assert ledger.legacy_entry(PACKAGE_NAME) is not None

    workspace_b = tmp_path / "workspace-b"
    _record_install(ledger, workspace_b)
    ledger.save()

    reloaded = PackageLedger.load(path)
    assert reloaded.legacy_entry(PACKAGE_NAME) is None
    assert reloaded.entry(PACKAGE_NAME, workspace=workspace_a) is not None
    assert reloaded.entry(PACKAGE_NAME, workspace=workspace_b) is not None


def test_forgetting_a_workspace_record_leaves_the_legacy_record_alone(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    _write_v1_ledger(path)
    ledger = PackageLedger.load(path)
    workspace = tmp_path / "workspace"
    _record_install(ledger, workspace, name="other-pack")
    ledger.save()
    ledger = PackageLedger.load(path)

    assert ledger.forget(PACKAGE_NAME, workspace=workspace) is None
    assert ledger.legacy_entry(PACKAGE_NAME) is not None


# ---------------------------------------------------------------------------
# Recording, saving and workspace scoping
# ---------------------------------------------------------------------------


def test_record_save_and_load_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    workspace_a = tmp_path / "ws-a"
    workspace_b = tmp_path / "ws-b"
    ledger = PackageLedger(path)
    entry_a = _record_install(ledger, workspace_a)
    entry_b = _record_install(ledger, workspace_b, version="2.0.0", skills=("beta", "gamma"))

    ledger.save()

    reloaded = PackageLedger.load(path)
    assert set(reloaded.entries_for(workspace_a)) == {PACKAGE_NAME}
    assert set(reloaded.entries_for(workspace_b)) == {PACKAGE_NAME}
    loaded_a = reloaded.entry(PACKAGE_NAME, workspace=workspace_a)
    loaded_b = reloaded.entry(PACKAGE_NAME, workspace=workspace_b)
    assert loaded_a.version == "1.0.0"
    assert loaded_a.skills == ("alpha",)
    assert loaded_a.install_token == entry_a.install_token
    assert loaded_a.skill_tokens == {"alpha": entry_a.skill_tokens["alpha"]}
    assert loaded_b.version == "2.0.0"
    assert loaded_b.skills == ("beta", "gamma")


def test_a_record_without_a_workspace_is_refused(tmp_path: Path) -> None:
    ledger = PackageLedger(tmp_path / "ledger.json")

    with pytest.raises(ValueError):
        ledger.record(LedgerEntry(name=PACKAGE_NAME))


def test_the_saved_file_carries_the_version_and_the_workspaces(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    workspace = tmp_path / "workspace"
    ledger = PackageLedger(path)
    _record_install(ledger, workspace)

    ledger.save()

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["version"] == LEDGER_VERSION
    assert payload["version"] == 2
    assert list(payload["workspaces"]) == [workspace_key(workspace)]
    assert PACKAGE_NAME in payload["workspaces"][workspace_key(workspace)]
    assert payload["legacy"] == {}


def test_forget_removes_only_this_workspace_record(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    workspace_a = tmp_path / "ws-a"
    workspace_b = tmp_path / "ws-b"
    ledger = PackageLedger(path)
    _record_install(ledger, workspace_a)
    _record_install(ledger, workspace_b)
    ledger.save()

    removed = ledger.forget(PACKAGE_NAME, workspace=workspace_a)
    ledger.save()

    assert removed is not None
    assert removed.name == PACKAGE_NAME
    reloaded = PackageLedger.load(path)
    assert reloaded.entry(PACKAGE_NAME, workspace=workspace_a) is None
    assert reloaded.entry(PACKAGE_NAME, workspace=workspace_b) is not None


def test_save_writes_the_file_when_the_parent_directory_is_missing(tmp_path: Path) -> None:
    path = tmp_path / "state" / "nested" / "ledger.json"
    ledger = PackageLedger(path)
    _record_install(ledger, tmp_path / "workspace")

    ledger.save()

    assert path.is_file()
    workspace = tmp_path / "workspace"
    assert PackageLedger.load(path).entry(PACKAGE_NAME, workspace=workspace) is not None


def test_a_write_that_dies_leaves_the_ledger_that_was_on_disk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A save that fails must not cost the operator the ledger they had.

    The ledger is what proves what a workspace installed, so a file left
    half written would read as a broken install rather than as a failed
    write. The new ledger is written beside the old one and moved into
    place, so the ledger on disk stays whole when the write dies.
    """
    path = tmp_path / "ledger.json"
    ledger = PackageLedger(path)
    _record_install(ledger, tmp_path)
    ledger.save()
    before = path.read_text(encoding="utf-8")

    original_write_text = Path.write_text

    def dying_write_text(self: Path, data: str, *args: object, **kwargs: object) -> int:
        original_write_text(self, data[: len(data) // 2], *args, **kwargs)
        raise OSError("the disk is full")

    monkeypatch.setattr(Path, "write_text", dying_write_text)

    with pytest.raises(OSError):
        ledger.save()

    assert path.read_text(encoding="utf-8") == before
    assert PackageLedger.load(path).entry(PACKAGE_NAME, workspace=tmp_path) is not None


def test_the_ledger_file_is_replaced_in_one_step(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A reader that opens the ledger while a save runs sees one whole ledger.

    The file on disk is always the previous ledger or the next one -- never
    one a write is still filling in. A reader that caught it mid-write
    would read a file that is neither, and take it for a ledger whose
    packages are lost.
    """
    path = tmp_path / "ledger.json"
    ledger = PackageLedger(path)
    _record_install(ledger, tmp_path)
    ledger.save()
    before = path.read_text(encoding="utf-8")

    seen: list[str] = []
    original_write_text = Path.write_text

    def reading_write_text(self: Path, data: str, *args: object, **kwargs: object) -> int:
        original_write_text(self, data[: len(data) // 2], *args, **kwargs)
        seen.append(path.read_text(encoding="utf-8"))
        return original_write_text(self, data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", reading_write_text)

    ledger.forget(PACKAGE_NAME, workspace=tmp_path)
    ledger.save()

    assert seen == [before]
    assert json.loads(path.read_text(encoding="utf-8"))["workspaces"] == {}


def test_a_save_leaves_no_file_beside_the_ledger(tmp_path: Path) -> None:
    """The file the write filled in is moved into place, not left next to it.

    A stray file beside the ledger is a second thing to read, and a reader
    that finds one cannot tell a ledger the write abandoned from a ledger
    of its own.
    """
    path = tmp_path / "ledger.json"
    ledger = PackageLedger(path)
    _record_install(ledger, tmp_path)

    ledger.save()

    assert sorted(entry.name for entry in tmp_path.iterdir() if entry.is_file()) == ["ledger.json"]


def test_the_ledger_swap_uses_replace_because_rename_cannot_overwrite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The file written beside the ledger becomes the ledger by ``os.replace``.

    Writing the next ledger beside the old one is only half of what keeps a
    concurrent reader whole; the swap is the other half. A save that copied
    the new bytes into the target would rewrite the very file a reader is
    looking at, so the entry on disk must move from the file written beside
    the ledger to the ledger itself in one step.

    The pin is on ``os.replace`` rather than on the rename family in general
    because the two are not equivalent for this save: it overwrites a ledger
    that already exists, and ``os.rename`` raises ``FileExistsError`` on
    Windows whenever the destination exists, while ``os.replace`` overwrites
    it on every platform.
    """
    path = tmp_path / "ledger.json"
    ledger = PackageLedger(path)
    _record_install(ledger, tmp_path)
    ledger.save()
    before = path.read_bytes()

    replacements: list[tuple[Path, Path, bytes]] = []
    original_replace = os.replace

    def recording_replace(
        source: str | os.PathLike[str],
        destination: str | os.PathLike[str],
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        source_path = Path(source)
        try:
            source_bytes = source_path.read_bytes()
        except FileNotFoundError as exc:
            raise AssertionError(
                f"the source file was gone before replace, expected it to still exist: {source_path}"
            ) from exc
        replacements.append((source_path, Path(destination), source_bytes))
        original_replace(source, destination, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "replace", recording_replace)

    ledger.forget(PACKAGE_NAME, workspace=tmp_path)
    ledger.save()

    after = path.read_bytes()
    assert len(replacements) >= 1
    swaps = [
        (source, destination, source_bytes)
        for source, destination, source_bytes in replacements
        if destination == path
        and source.parent == path.parent
        and source != path
        and source_bytes != before
        and source_bytes == after
        and not source.exists()
    ]
    assert swaps, (
        "no replacement swapped the freshly written sibling onto the ledger: "
        f"{[(str(source), str(destination)) for source, destination, _ in replacements]}"
    )


def test_a_save_never_opens_the_ledger_for_writing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The ledger on disk is replaced, never rewritten through an open handle.

    A reader that shares the file sees every byte a write puts through an
    open handle, so a save must put its bytes somewhere else and only then
    move the result over the ledger. This guard raises when a save opens the
    ledger for writing through ``Path.open``, ``builtins.open``, ``io.open``
    or ``os.open``; it cannot see a write to a descriptor opened before the
    guard went up, nor a raw ``os.write`` on such a descriptor, nor a writer
    that bound its own alias before the guard went up, such as one that did
    ``from io import open``.
    """
    path = tmp_path / "ledger.json"
    ledger = PackageLedger(path)
    _record_install(ledger, tmp_path)
    ledger.save()
    before = path.read_text(encoding="utf-8")

    original_path_open = Path.open
    original_open = builtins.open
    original_os_open = os.open

    def opens_for_writing(mode: str) -> bool:
        return any(flag in mode for flag in "wax+")

    def guarded_path_open(self: Path, mode: str = "r", *args: object, **kwargs: object) -> object:
        if self == path and opens_for_writing(mode):
            raise AssertionError(f"the save opened the ledger for writing ({mode})")
        return original_path_open(self, mode, *args, **kwargs)

    def guarded_open(file: object, mode: str = "r", *args: object, **kwargs: object) -> object:
        if isinstance(file, (str, os.PathLike)) and Path(file) == path and opens_for_writing(mode):
            raise AssertionError(f"the save opened the ledger for writing ({mode})")
        return original_open(file, mode, *args, **kwargs)

    write_flags = os.O_WRONLY | os.O_RDWR | os.O_APPEND | os.O_TRUNC | os.O_CREAT

    def guarded_os_open(
        file: str | os.PathLike[str] | int,
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if isinstance(file, (str, os.PathLike)) and Path(file) == path and flags & write_flags:
            raise AssertionError(f"the save opened the ledger with os.open for writing (flags={flags:#o})")
        return original_os_open(file, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(Path, "open", guarded_path_open)
    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)

    ledger.forget(PACKAGE_NAME, workspace=tmp_path)
    ledger.save()

    after = path.read_text(encoding="utf-8")
    assert after != before
    assert json.loads(after)["workspaces"] == {}


def test_a_damaged_v2_entry_loads_as_an_unverified_record(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    key = workspace_key(tmp_path / "workspace")
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "workspaces": {
                    key: {
                        PACKAGE_NAME: {
                            "version": "1.0.0",
                            "skills": "not-a-list",
                            "install_token": 17,
                        }
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    ledger = PackageLedger.load(path)

    entry = ledger.entry(PACKAGE_NAME, workspace=tmp_path / "workspace")
    assert entry is not None
    assert entry.unverified
    assert entry.version == "1.0.0"


# ---------------------------------------------------------------------------
# The workspace key
# ---------------------------------------------------------------------------


def test_the_workspace_key_folds_the_spellings_of_one_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "ws-a"
    workspace.mkdir()
    monkeypatch.chdir(tmp_path)

    assert workspace_key(workspace) == workspace_key(tmp_path / "ws-a" / "")
    assert workspace_key(workspace) == workspace_key(Path("ws-a"))
    assert workspace_key(workspace) == workspace_key(tmp_path / "ws-b" / ".." / "ws-a")
    assert workspace_key(workspace) != workspace_key(tmp_path / "ws-b")


def test_the_workspace_key_folds_a_linked_workspace(tmp_path: Path) -> None:
    """A workspace reached through a directory link is the same workspace."""
    real = tmp_path / "real-ws"
    real.mkdir()
    link = tmp_path / "linked-ws"
    _link_directory(real, link)

    assert workspace_key(link) == workspace_key(real)


@pytest.mark.skipif(os.name != "nt", reason="case folding is a Windows path rule")
def test_the_workspace_key_folds_case_the_way_windows_does(tmp_path: Path) -> None:
    workspace = tmp_path / "MixedCase"
    workspace.mkdir()

    assert workspace_key(workspace) == workspace_key(tmp_path / "mixedcase")


@pytest.mark.skipif(os.name == "nt", reason="POSIX keeps distinct directories by case")
def test_the_workspace_key_keeps_case_distinct_on_posix(tmp_path: Path) -> None:
    assert workspace_key(tmp_path / "WS-A") != workspace_key(tmp_path / "ws-a")


# ---------------------------------------------------------------------------
# Ownership
# ---------------------------------------------------------------------------


def test_owns_the_marked_package_directory_and_its_skills(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    ledger = PackageLedger(tmp_path / "ledger.json")
    _record_install(ledger, workspace)

    assert ledger.owns_directory(workspace / "skills" / PACKAGE_NAME, workspace=workspace)
    assert ledger.owns_directory(workspace / "skills" / PACKAGE_NAME / "alpha", workspace=workspace)


def test_does_not_own_a_package_that_is_not_recorded(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    ledger = PackageLedger(tmp_path / "ledger.json")
    _record_install(ledger, workspace)

    claim = ledger.claim(workspace / "skills" / "other-pack", workspace=workspace)
    assert not claim.owned
    assert claim.reason == "is not owned by the package ledger"


def test_does_not_own_a_path_outside_the_skills_root(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    ledger = PackageLedger(tmp_path / "ledger.json")
    _record_install(ledger, workspace)

    assert not ledger.owns_directory(workspace / "elsewhere" / PACKAGE_NAME, workspace=workspace)
    assert not ledger.owns_directory(tmp_path / "outside", workspace=workspace)


def test_does_not_own_a_deeper_path(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    ledger = PackageLedger(tmp_path / "ledger.json")
    _record_install(ledger, workspace)

    claim = ledger.claim(workspace / "skills" / PACKAGE_NAME / "alpha" / "scripts", workspace=workspace)
    assert not claim.owned
    assert claim.reason == "lies deeper than any package the ledger claims"


def test_a_record_of_another_workspace_claims_nothing_here(tmp_path: Path) -> None:
    workspace_a = tmp_path / "ws-a"
    workspace_b = tmp_path / "ws-b"
    ledger = PackageLedger(tmp_path / "ledger.json")
    _record_install(ledger, workspace_a)

    claim = ledger.claim(workspace_b / "skills" / PACKAGE_NAME, workspace=workspace_b)
    assert not claim.owned
    assert "another workspace" in claim.reason


def test_a_legacy_record_claims_nothing(tmp_path: Path) -> None:
    path = tmp_path / "ledger.json"
    _write_v1_ledger(path)
    ledger = PackageLedger.load(path)
    workspace = tmp_path / "workspace"
    (workspace / "skills" / PACKAGE_NAME / "alpha").mkdir(parents=True)

    claim = ledger.claim(workspace / "skills" / PACKAGE_NAME, workspace=workspace)
    assert not claim.owned
    assert "legacy" in claim.reason


def test_a_skill_the_entry_does_not_list_is_not_owned(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    ledger = PackageLedger(tmp_path / "ledger.json")
    _record_install(ledger, workspace, skills=("alpha",))
    unlisted = workspace / "skills" / PACKAGE_NAME / "beta"
    unlisted.mkdir(parents=True)

    claim = ledger.claim(unlisted, workspace=workspace)
    assert not claim.owned
    assert claim.reason == "is not owned by the package ledger"


def test_a_missing_marker_breaks_the_claim(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    ledger = PackageLedger(tmp_path / "ledger.json")
    _record_install(ledger, workspace)
    (workspace / "skills" / PACKAGE_NAME / "alpha" / INSTALL_MARKER_FILENAME).unlink()

    claim = ledger.claim(workspace / "skills" / PACKAGE_NAME / "alpha", workspace=workspace)
    assert not claim.owned
    assert "install marker" in claim.reason


def test_a_foreign_marker_breaks_the_claim(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    ledger = PackageLedger(tmp_path / "ledger.json")
    _record_install(ledger, workspace)
    marker = workspace / "skills" / PACKAGE_NAME / "alpha" / INSTALL_MARKER_FILENAME
    marker.write_text(json.dumps({"token": "not-the-recorded-token"}), encoding="utf-8")

    claim = ledger.claim(workspace / "skills" / PACKAGE_NAME / "alpha", workspace=workspace)
    assert not claim.owned
    assert claim.reason == "does not match the recorded install identity"


def test_a_user_directory_at_the_moved_install_path_is_not_owned(tmp_path: Path) -> None:
    """The record outlives the directory it named; the replacement is not it."""
    workspace = tmp_path / "workspace"
    ledger = PackageLedger(tmp_path / "ledger.json")
    _record_install(ledger, workspace)
    root = workspace / "skills" / PACKAGE_NAME
    shutil.rmtree(root)
    root.mkdir()
    (root / "NOTES.md").write_text("The operator's own directory.\n", encoding="utf-8")
    write_install_marker(root)

    claim = ledger.claim(root, workspace=workspace)
    assert not claim.owned
    assert claim.reason == "does not match the recorded install identity"


def test_an_unverified_entry_claims_nothing(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    ledger = PackageLedger(tmp_path / "ledger.json")
    root = workspace / "skills" / PACKAGE_NAME
    root.mkdir(parents=True)
    token = write_install_marker(root)
    ledger.record(
        LedgerEntry(
            name=PACKAGE_NAME,
            skills=("alpha",),
            workspace=workspace_key(workspace),
            install_token=token,
            skill_tokens={"alpha": token},
            unverified=True,
        )
    )

    claim = ledger.claim(root, workspace=workspace)
    assert not claim.owned
    assert "no install identity" in claim.reason


def test_a_directory_that_does_not_exist_is_not_owned(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    ledger = PackageLedger(tmp_path / "ledger.json")
    _record_install(ledger, workspace)
    shutil.rmtree(workspace / "skills" / PACKAGE_NAME / "alpha")

    claim = ledger.claim(workspace / "skills" / PACKAGE_NAME / "alpha", workspace=workspace)
    assert not claim.owned
    assert claim.reason == "does not exist"


def test_a_link_pointing_out_of_the_skills_tree_claims_nothing(tmp_path: Path) -> None:
    """A link at the install path standing over a user directory elsewhere."""
    workspace = tmp_path / "workspace"
    ledger = PackageLedger(tmp_path / "ledger.json")
    _record_install(ledger, workspace, name="real-pack")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "BELONGS-TO-USER.txt").write_text("user content\n", encoding="utf-8")
    link = workspace / "skills" / "real-pack" / "alpha"
    shutil.rmtree(link)
    _link_directory(elsewhere, link)

    claim = ledger.claim(link, workspace=workspace)
    assert not claim.owned
    assert claim.reason == "is outside the workspace skills tree"
    assert (elsewhere / "BELONGS-TO-USER.txt").is_file()


def test_a_link_pointing_at_another_claimed_directory_is_not_followed(tmp_path: Path) -> None:
    """The marker the link shows is a real recorded token; the link is refused anyway."""
    workspace = tmp_path / "workspace"
    ledger = PackageLedger(tmp_path / "ledger.json")
    _record_install(ledger, workspace, skills=("alpha", "beta"))
    alpha = workspace / "skills" / PACKAGE_NAME / "alpha"
    beta = workspace / "skills" / PACKAGE_NAME / "beta"
    shutil.rmtree(alpha)
    _link_directory(beta, alpha)

    claim = ledger.claim(alpha, workspace=workspace)
    assert not claim.owned
    assert "symlink or junction" in claim.reason
    assert beta.is_dir()


def test_a_junction_reported_as_a_directory_is_refused_by_its_reparse_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Windows junction's lstat is S_IFDIR; st_reparse_tag is the only guard.

    On POSIX a link is refused by S_ISLNK and then by the S_ISDIR check that
    follows; on Windows a junction's lstat reports a directory, so the reparse
    tag is the only thing standing between the ledger and the junction's
    target. This fakes the lstat a junction returns there.
    """
    workspace = tmp_path / "workspace"
    ledger = PackageLedger(tmp_path / "ledger.json")
    _record_install(ledger, workspace)
    root = workspace / "skills" / PACKAGE_NAME
    real_lstat = Path.lstat

    def _junction_lstat(self: Path):
        if Path(self) == root:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_reparse_tag=0xA0000003)
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", _junction_lstat)

    claim = ledger.claim(root, workspace=workspace)

    assert not claim.owned
    assert "symlink or junction" in claim.reason


def test_a_directory_without_a_reparse_tag_is_still_owned(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The control for the faked Windows lstat: tag zero is a real directory."""
    workspace = tmp_path / "workspace"
    ledger = PackageLedger(tmp_path / "ledger.json")
    _record_install(ledger, workspace)
    root = workspace / "skills" / PACKAGE_NAME
    real_lstat = Path.lstat

    def _plain_lstat(self: Path):
        if Path(self) == root:
            return SimpleNamespace(st_mode=stat.S_IFDIR | 0o755, st_reparse_tag=0)
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", _plain_lstat)

    assert ledger.claim(root, workspace=workspace).owned


def test_a_skill_without_a_recorded_token_is_not_owned(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    ledger = PackageLedger(tmp_path / "ledger.json")
    root = workspace / "skills" / PACKAGE_NAME
    root.mkdir(parents=True)
    write_install_marker(root)
    ledger.record(
        LedgerEntry(
            name=PACKAGE_NAME,
            skills=("alpha",),
            workspace=workspace_key(workspace),
            install_token=write_install_marker(root),
        )
    )

    claim = ledger.claim(root / "alpha", workspace=workspace)
    assert not claim.owned
    assert "usable install identity" in claim.reason
