"""The regenerator's refusals, which are what keep the shipped pin true.

The icon data is the one third-party import a reader cannot check by eye: 1304
glyphs converted out of five thousand SVGs, and the only thing standing between
that and a fabrication is the pin in the file header. A pin is worth exactly what
happens when somebody regenerates -- if the script writes whatever it is handed,
the first rerun turns a fact back into a claim without anyone noticing, which is
the state the header was in when it carried a bare version string and nothing else.

So the refusals are the subject here, not the conversion: the conversion is
exercised by every icon in `test_assets_icons`, and it is only trustworthy because
these hold.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from scripts.build_tabler_icons import ARCHIVE_URL, ProvenanceError, pin, read_archive, read_icon

SHIPPED = {
    "package": "tabler-icons",
    "version": "3.46.0",
    "variant": "outline",
    "commit": "8ac7d81b72ece11072ef25ea9fd92e80c6f3c9fc",
    "files": "icons/outline/*.svg",
    "archive": ARCHIVE_URL.format(commit="8ac7d81b72ece11072ef25ea9fd92e80c6f3c9fc"),
    "sha256": "6d7ecda12c53a543f305859c247b8beb4266205e40c58487b8469aeaeb7797b1",
}
ANOTHER_COMMIT = "0" * 40
ANOTHER_HASH = "f" * 64


def _args(**overrides) -> argparse.Namespace:
    """The parsed command line, with everything the pin reads defaulted to absent."""
    return argparse.Namespace(**{"version": None, "commit": None, "sha256": None, "repin": False, **overrides})


def test_a_rerun_from_the_pinned_tarball_keeps_the_pin_it_checked() -> None:
    """The ordinary case: what was handed over is what the file says, so nothing moves."""
    assert pin(SHIPPED, SHIPPED["sha256"], _args()) == SHIPPED


def test_a_tarball_that_is_not_the_one_the_pin_names_stops_the_run() -> None:
    """The loudest failure, because everything downstream would still look right.

    A different tree converts perfectly well. The icons come out, the file writes, and
    the header goes on naming a commit that had nothing to do with the bytes beside it.
    """
    with pytest.raises(ProvenanceError, match="the upstream moved"):
        pin(SHIPPED, ANOTHER_HASH, _args())


def test_a_hash_given_by_hand_has_to_be_the_tarball_that_was_read() -> None:
    """`--sha256` says what the artifact is; it does not get to overrule what it is."""
    with pytest.raises(ProvenanceError, match="is not the tarball just read"):
        pin(SHIPPED, SHIPPED["sha256"], _args(sha256=ANOTHER_HASH, repin=True))


def test_moving_to_a_new_upstream_has_to_be_asked_for() -> None:
    """Re-pinning is legitimate and is not something a rerun should do by accident."""
    with pytest.raises(ProvenanceError, match="the upstream moved"):
        pin(SHIPPED, ANOTHER_HASH, _args(version="3.47.0", commit=ANOTHER_COMMIT))

    moved = pin(SHIPPED, ANOTHER_HASH, _args(version="3.47.0", commit=ANOTHER_COMMIT, repin=True))
    assert moved["version"] == "3.47.0"
    assert moved["commit"] == ANOTHER_COMMIT
    assert moved["sha256"] == ANOTHER_HASH
    # The archive is derived from the commit rather than carried over, so the URL and
    # the hash beside it can never end up describing two different trees.
    assert moved["archive"] == ARCHIVE_URL.format(commit=ANOTHER_COMMIT)


def test_provenance_may_be_established_or_moved_but_not_dropped() -> None:
    """The header this replaced had a version and nothing else, and that must not return.

    A run from a directory has no hash to measure, so without this the easiest way to
    lose the pin is the most convenient command to type.
    """
    unpinned = {"package": "tabler-icons", "version": "3.46.0", "variant": "outline"}
    with pytest.raises(ProvenanceError, match="no version, commit or hash"):
        pin(unpinned, None, _args())

    established = pin(unpinned, None, _args(commit=ANOTHER_COMMIT, sha256=ANOTHER_HASH))
    assert established["commit"] == ANOTHER_COMMIT
    assert established["sha256"] == ANOTHER_HASH


def test_a_rerun_from_a_directory_carries_the_pin_it_could_not_check() -> None:
    """Allowed, because the directory is what upstream's own checkout gives you.

    It is the one path that writes a pin nobody verified on this run, so the caller is
    told as much on stdout -- see `main`. What must not happen is it silently changing.
    """
    assert pin(SHIPPED, None, _args()) == SHIPPED


def _tarball(directory: Path, entries: dict[str, str]) -> Path:
    """A stand-in for the release archive, wrapped the way GitHub wraps one."""
    path = directory / "upstream.tar.gz"
    with tarfile.open(path, "w:gz") as bundle:
        for name, text in entries.items():
            payload = text.encode("utf-8")
            info = tarfile.TarInfo(f"tabler-icons-abc123/{name}")
            info.size = len(payload)
            bundle.addfile(info, io.BytesIO(payload))
    return path


SVG = """<!--
tags: [check, tick]
category: System
-->
<svg viewBox="0 0 24 24"><path d="M5 12l5 5l9 -9" /></svg>
"""


def test_reading_the_archive_takes_the_outline_set_and_hashes_what_it_read(tmp_path: Path) -> None:
    """Both halves in one pass, because a hash of anything but the bytes read is decoration.

    The filled set sits beside the outline one in the same archive and is not in here;
    matching on the directory rather than on the suffix is what keeps it out, and the
    `<repo>-<commit>/` wrapper GitHub adds is why the match cannot be on a whole path.
    """
    archive = _tarball(
        tmp_path,
        {
            "icons/outline/check.svg": SVG,
            "icons/filled/check.svg": SVG,
            "package.json": '{"version": "3.46.0"}',
        },
    )
    icons, digest = read_archive(archive)

    assert sorted(icons) == ["check"], "the filled set and the metadata are not icon data"
    assert icons["check"] == read_icon(SVG)
    assert icons["check"]["category"] == "System"
    assert digest == hashlib.sha256(archive.read_bytes()).hexdigest()


def test_the_shipped_header_is_the_shape_the_regenerator_writes() -> None:
    """A pin the script could not reproduce would be one nobody could check by rerunning."""
    header = json.loads(
        (Path(__file__).resolve().parents[2] / "raven/ppt/services/assets/data/tabler_outline.json").read_text(
            encoding="utf-8"
        )
    )
    assert header["upstream"] == pin(header["upstream"], header["upstream"]["sha256"], _args())
