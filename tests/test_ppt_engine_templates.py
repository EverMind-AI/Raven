"""The ppt-engine template payload: what git tracks, and the refresh machinery.

The 10 bundled deck templates (about 47 MiB, 7 over the repo's 1 MiB cap) are
tracked, by maintainer approval, because an install that cannot build a deck is
not an install: the registry they used to be fetched from is private, so a fresh
clone got an empty catalogue and the ppt-engine wheel shipped zero templates.
``plugins-dist/ppt-engine/templates.manifest.json`` still pins every file by
sha256. What this family pins: git holds all ten and their bytes satisfy the
pins, the large-file gate allows exactly the pinned names, a pull verifies as it
lands and removes what fails, and the un-named hosting endpoint (verdict C4) is
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
REGISTRY = "https://gitlab.com/api/v4/projects/84258686/packages/generic/ppt-templates/"


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


def test_the_manifest_names_the_ten_templates_and_a_real_endpoint(manifest):
    """The pins sign the tracked payload, and the endpoint stays a place rather
    than a placeholder: it is the archive of what shipped and the lane a re-cut
    arrives through, not something a fresh clone has to reach."""
    assert len(manifest["files"]) == 10
    assert [entry["name"] for entry in manifest["files"]] == sorted(entry["name"] for entry in manifest["files"])
    for entry in manifest["files"]:
        assert entry["name"].endswith(".pptx")
        assert entry["bytes"] > 0
        assert len(entry["sha256"]) == 64 and int(entry["sha256"], 16) >= 0
    assert manifest["endpoint"]["status"] == "NAMED"
    assert manifest["endpoint"]["url"].startswith(REGISTRY)
    assert "GITLAB_TOKEN" in manifest["endpoint"]["auth"]


def test_git_tracks_every_pinned_template(manifest):
    """The whole point of the change: a clone has the templates. Every pinned
    file must be tracked -- an untracked one means the payload rides on the
    committer's disk and a fresh clone builds a wheel with an empty catalogue."""
    dest = (MANIFEST.parent / manifest["destination"]).resolve()
    listed = subprocess.run(
        ["git", "-C", str(REPO), "ls-files", "--", str(dest)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    tracked = {Path(line).name for line in listed}
    assert tracked == {entry["name"] for entry in manifest["files"]}


def test_the_tracked_payload_satisfies_the_pins(machinery, manifest):
    """The strongest whole-cloth check, and now unconditional: every template
    git holds, hashed against its pin through the machinery's own verify."""
    dest = (MANIFEST.parent / manifest["destination"]).resolve()
    assert machinery.verify(dest, manifest) == []


def test_the_large_file_gate_allows_exactly_the_pinned_templates():
    """Seven of the ten clear the 1 MiB ceiling, so the gate has to let them
    through -- by pinned name, not by directory. An unpinned .pptx dropped
    beside them stays an oversized file, and so does one a directory up."""
    from scripts import check_large_files

    directory = check_large_files.BUNDLED_DECK_TEMPLATE_DIR
    pinned = check_large_files.PINNED_DECK_TEMPLATES

    allowed = [f"{directory}/{name}" for name in sorted(pinned)]
    blocked = [
        f"{directory}/unpinned_extra.pptx",
        f"{directory}/nested/{sorted(pinned)[0]}",
        f"plugins-dist/ppt-engine/{sorted(pinned)[0]}",
    ]
    violations = check_large_files.find_oversized_files(
        allowed + blocked,
        max_bytes=1024,
        root=REPO,
    )

    assert [violation.path for violation in violations] == []
    assert all(not (REPO / path).is_file() for path in blocked)
    assert [check_large_files._is_pinned_deck_template(path) for path in blocked] == [False, False, False]


def test_the_gates_roster_and_the_manifest_name_the_same_ten(manifest):
    """Two lists of the same ten, kept apart on purpose: the gate's roster is
    the maintainer's approval and the manifest is what the bytes must be. They
    have to agree, and this is where they are held together -- re-cutting or
    adding a template means editing both."""
    from scripts import check_large_files

    assert check_large_files.PINNED_DECK_TEMPLATES == check_large_files.manifest_deck_template_names(REPO)
    assert check_large_files.PINNED_DECK_TEMPLATES == {entry["name"] for entry in manifest["files"]}


def test_a_manifest_entry_alone_does_not_buy_a_large_file_exception(tmp_path):
    """The gate must not read its own exception list out of the tree it checks.

    A change that adds an oversized file can also add the manifest entry that
    would name it, which is the change under review granting itself the
    per-file approval AGENTS.md section 7 reserves for the maintainer. So the
    roster is source in the gate, and a manifest that claims one more template
    buys nothing."""
    from scripts import check_large_files

    directory = check_large_files.BUNDLED_DECK_TEMPLATE_DIR
    intruder = tmp_path / directory / "self_granted.pptx"
    intruder.parent.mkdir(parents=True)
    intruder.write_bytes(b"\x00" * 4096)

    manifest_path = tmp_path / check_large_files.BUNDLED_DECK_TEMPLATE_MANIFEST
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps({"files": [{"name": "self_granted.pptx", "bytes": 4096}]}),
        encoding="utf-8",
    )

    violations = check_large_files.find_oversized_files(
        [f"{directory}/self_granted.pptx"],
        max_bytes=1024,
        root=tmp_path,
    )

    assert [v.path for v in violations] == [f"{directory}/self_granted.pptx"], (
        "a manifest the same change writes granted its own large-file exception"
    )


def test_the_stub_endpoint_is_refused_with_the_owner_card(machinery, tmp_path, capsys):
    """Constraint (c): machinery lands against a stub, never an invented
    host. The refusal names the owners and the ruling instead of guessing."""
    manifest_path = tmp_path / "m.json"
    stub = _pinned({"a.pptx": b"deck-a"})
    stub["endpoint"] = {
        "url": "stub://ppt-templates-host.un-named",
        "status": "UN-NAMED",
        "owner": "maintainer + raven-ppt product team, jointly (ppt verdict C4)",
        "ruling": "card-ppt-c4-template-hosting-0901.md",
    }
    manifest_path.write_text(json.dumps(stub))
    rc = machinery.main(["--manifest", str(manifest_path), "--dest", str(tmp_path / "dest")])
    assert rc == machinery.EXIT_UNNAMED
    err = capsys.readouterr().err
    assert "refusing to invent one" in err
    assert "owner" in err
    assert "C4" in err
    assert not (tmp_path / "dest").exists()


def test_a_private_registry_gets_the_token_the_environment_holds(machinery, tmp_path, monkeypatch):
    """The project is private: without a header the registry answers 404 for every
    file. The personal token wins over the CI job token, and a public host gets none."""
    import io

    assert machinery.auth_headers({"GITLAB_TOKEN": "glpat-x", "CI_JOB_TOKEN": "job"}) == {"PRIVATE-TOKEN": "glpat-x"}
    assert machinery.auth_headers({"CI_JOB_TOKEN": "job"}) == {"JOB-TOKEN": "job"}
    assert machinery.auth_headers({}) == {}

    monkeypatch.setenv("GITLAB_TOKEN", "glpat-x")
    seen: list[tuple[str, dict]] = []

    class _Response(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def fake_urlopen(request):
        seen.append((request.full_url, dict(request.header_items())))
        return _Response(b"deck-a")

    monkeypatch.setattr(machinery.urllib.request, "urlopen", fake_urlopen)
    manifest_path = tmp_path / "m.json"
    named = _pinned({"a.pptx": b"deck-a"})
    named["endpoint"] = {"url": "https://registry.example/ppt-templates/1", "status": "NAMED"}
    manifest_path.write_text(json.dumps(named))

    rc = machinery.main(["--manifest", str(manifest_path), "--dest", str(tmp_path / "dest")])

    assert rc == machinery.EXIT_OK
    assert seen == [("https://registry.example/ppt-templates/1/a.pptx", {"Private-token": "glpat-x"})]
    assert (tmp_path / "dest" / "a.pptx").read_bytes() == b"deck-a"


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
