"""The ppt-engine template payload machinery: pins, the gitignore fence, the pulls.

The 12 bundled deck templates (70.53 MiB, 11 over the repo's 1 MiB cap)
never enter git; ``plugins-dist/ppt-engine/templates.manifest.json`` is the
tracked truth and ``fetch_templates.py`` the only way payload reaches the
gitignored destination. What this family pins: the manifest agrees with the
vendored fork tree byte-for-byte (while that tree exists -- after the
retirement wave the fetched payload itself is what the pins hold against),
the destination directory refuses git, a pull verifies as it lands and
removes what fails, and the un-named hosting endpoint (verdict C4) is
refused with the owner card rather than guessed at.
"""

import hashlib
import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
ENGINE_HOME = REPO / "plugins-dist" / "ppt-engine"
MANIFEST = ENGINE_HOME / "templates.manifest.json"
FORK_TEMPLATES = REPO / "subagents" / "raven-ppt" / "Raven-PPT" / "raven" / "ppt" / "assets" / "templates"

fork_present = pytest.mark.skipif(
    not FORK_TEMPLATES.is_dir(),
    reason="the vendored fork tree is retired; the pins now hold against fetched payload",
)


@pytest.fixture()
def machinery():
    spec = importlib.util.spec_from_file_location("ppt_fetch_templates", ENGINE_HOME / "fetch_templates.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture()
def manifest():
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


def _pinned(payload: dict[str, bytes]) -> dict:
    """A manifest fragment whose pins match ``payload`` exactly."""
    return {
        "endpoint": {"url": "stub://test", "status": "UN-NAMED"},
        "destination": "dest",
        "files": [
            {"name": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
            for name, data in sorted(payload.items())
        ],
    }


@fork_present
def test_the_manifest_pins_exactly_the_forks_twelve_templates(manifest):
    """Names, sizes and sha256 all recomputed from the vendored tree: the
    manifest is derived truth, never hand-kept."""
    fork = {
        path.name: {"bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        for path in FORK_TEMPLATES.glob("*.pptx")
    }
    assert len(manifest["files"]) == 12
    assert {entry["name"] for entry in manifest["files"]} == set(fork)
    for entry in manifest["files"]:
        assert entry["bytes"] == fork[entry["name"]]["bytes"], entry["name"]
        assert entry["sha256"] == fork[entry["name"]]["sha256"], entry["name"]


def test_the_destination_gitignore_fences_every_pinned_file(manifest):
    """The .gitignore landed before any payload can: git must report every
    pinned path ignored, and the .gitignore itself not ignored -- so a
    `git add -A` after a pull stages the fence and never the freight."""
    dest = (MANIFEST.parent / manifest["destination"]).resolve()
    assert (dest / ".gitignore").is_file()
    for entry in manifest["files"]:
        rc = subprocess.run(
            ["git", "-C", str(REPO), "check-ignore", "-q", str(dest / entry["name"])],
            capture_output=True,
        ).returncode
        assert rc == 0, f"{entry['name']} is not gitignored at the destination"
    rc = subprocess.run(
        ["git", "-C", str(REPO), "check-ignore", "-q", str(dest / ".gitignore")],
        capture_output=True,
    ).returncode
    assert rc == 1, "the fence itself must stay trackable"


def test_the_stub_endpoint_is_refused_with_the_owner_card(machinery, manifest, tmp_path, capsys):
    """Constraint (c): machinery lands against a stub, never an invented
    host. The refusal names the owners and the ruling instead of guessing."""
    assert manifest["endpoint"]["status"] == "UN-NAMED"
    assert manifest["endpoint"]["url"].startswith("stub://")
    rc = machinery.main(["--dest", str(tmp_path / "dest")])
    assert rc == machinery.EXIT_UNNAMED
    err = capsys.readouterr().err
    assert "refusing to invent one" in err
    assert "owner" in err
    assert "C4" in err
    assert not (tmp_path / "dest").exists()


def test_a_local_pull_copies_and_verifies(machinery, tmp_path, capsys):
    source, dest = tmp_path / "src", tmp_path / "dest"
    source.mkdir()
    payload = {"a.pptx": b"deck-a", "b.pptx": b"deck-b"}
    for name, data in payload.items():
        (source / name).write_bytes(data)
    manifest_path = tmp_path / "m.json"
    manifest_path.write_text(json.dumps(_pinned(payload)))
    rc = machinery.main(["--manifest", str(manifest_path), "--dest", str(dest), "--from", str(source)])
    assert rc == machinery.EXIT_OK
    assert (dest / "a.pptx").read_bytes() == b"deck-a"
    assert (dest / "b.pptx").read_bytes() == b"deck-b"


def test_a_corrupted_payload_is_refused_and_removed(machinery, tmp_path):
    """A wheel built over the destination must never package a byte the
    manifest did not sign: the mismatching copy is deleted, not left."""
    source, dest = tmp_path / "src", tmp_path / "dest"
    source.mkdir()
    (source / "a.pptx").write_bytes(b"tampered")
    manifest_path = tmp_path / "m.json"
    manifest_path.write_text(json.dumps(_pinned({"a.pptx": b"deck-a"})))
    rc = machinery.main(["--manifest", str(manifest_path), "--dest", str(dest), "--from", str(source)])
    assert rc == machinery.EXIT_VERIFY
    assert not (dest / "a.pptx").exists()


def test_verify_reports_missing_payload_nonzero(machinery, tmp_path):
    """The runtime degrades soft on an empty catalogue (the engine's own
    shipped behavior); CI must not -- a wheel build without the payload is
    a failure, not a warning."""
    manifest_path = tmp_path / "m.json"
    manifest_path.write_text(json.dumps(_pinned({"a.pptx": b"deck-a"})))
    rc = machinery.main(["--manifest", str(manifest_path), "--dest", str(tmp_path / "empty"), "--verify"])
    assert rc == machinery.EXIT_VERIFY


def test_a_named_file_endpoint_pulls_through_the_url_lane(machinery, tmp_path):
    """The wired pull path, proven without inventing a host: a file:// base
    stands in for the future endpoint, and naming it in the manifest is the
    owners' whole edit."""
    source, dest = tmp_path / "src", tmp_path / "dest"
    source.mkdir()
    (source / "a.pptx").write_bytes(b"deck-a")
    fragment = _pinned({"a.pptx": b"deck-a"})
    fragment["endpoint"] = {"url": source.as_uri(), "status": "NAMED"}
    manifest_path = tmp_path / "m.json"
    manifest_path.write_text(json.dumps(fragment))
    rc = machinery.main(["--manifest", str(manifest_path), "--dest", str(dest)])
    assert rc == machinery.EXIT_OK
    assert (dest / "a.pptx").read_bytes() == b"deck-a"


@fork_present
def test_the_real_fork_payload_satisfies_the_shipped_manifest(machinery, manifest):
    """The strongest whole-cloth check: every pinned template hashed against
    the vendored tree through the machinery's own verify."""
    assert machinery.verify(FORK_TEMPLATES, manifest) == []
