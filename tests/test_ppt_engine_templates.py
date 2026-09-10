"""The ppt-engine template payload machinery: pins, the gitignore fence, the pulls.

The 10 bundled deck templates (about 47 MiB, 7 over the repo's 1 MiB cap)
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
REGISTRY = "https://gitlab.com/api/v4/projects/84258686/packages/generic/ppt-templates/"

payload_present = pytest.mark.skipif(
    not any((ENGINE_HOME / "raven_ppt" / "assets" / "templates").glob("*.pptx")),
    reason="the template payload is fetched, not tracked; run plugins-dist/ppt-engine/fetch_templates.py",
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


def test_the_manifest_names_the_ten_templates_and_a_real_endpoint(manifest):
    """The pins are the tracked truth and the endpoint is a place, not a placeholder:
    the ten templates live as a generic package in the project's own GitLab
    package registry, so a fresh clone fetches them with the token that cloned it."""
    assert len(manifest["files"]) == 10
    assert [entry["name"] for entry in manifest["files"]] == sorted(entry["name"] for entry in manifest["files"])
    for entry in manifest["files"]:
        assert entry["name"].endswith(".pptx")
        assert entry["bytes"] > 0
        assert len(entry["sha256"]) == 64 and int(entry["sha256"], 16) >= 0
    assert manifest["endpoint"]["status"] == "NAMED"
    assert manifest["endpoint"]["url"].startswith(REGISTRY)
    assert "GITLAB_TOKEN" in manifest["endpoint"]["auth"]


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


@payload_present
def test_the_fetched_payload_satisfies_the_shipped_manifest(machinery, manifest):
    """The strongest whole-cloth check: every pinned template hashed against what the
    registry served, through the machinery's own verify."""
    assert machinery.verify(ENGINE_HOME / "raven_ppt" / "assets" / "templates", manifest) == []
