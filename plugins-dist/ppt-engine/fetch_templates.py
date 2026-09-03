#!/usr/bin/env python3
"""Pull and verify the ppt engine's bundled deck templates.

The 12 template .pptx (70.53 MiB, 11 over the repo's 1 MiB cap) never enter
git: ``templates.manifest.json`` beside this file is the tracked truth --
one sha256 pin per file -- and this tool is the only way payload reaches the
gitignored destination directory. CI runs it before building the ppt-engine
wheel, the same conditional-payload discipline the trunk wheel already
applies to ui-tui/dist; a source checkout that never runs it degrades soft
at runtime (the engine offers an empty catalogue, its own shipped behavior).

The manifest's endpoint is a deliberate stub: the physical hosting point for
the payload is a ruling still owed by its owners (verdict C4), and this tool
refuses to guess -- a pull against the stub stops with the owner card, not a
request. Until the endpoint is named, ``--from`` pulls out of the vendored
fork tree, which is where the pinned bytes live today.

Standard-library only, like the launchers: CI and a bare python3 both run it.

Exit codes: 0 verified; 1 payload missing or failing its pin; 2 endpoint
still un-named (the owner card was printed).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_MANIFEST = HERE / "templates.manifest.json"

EXIT_OK = 0
EXIT_VERIFY = 1
EXIT_UNNAMED = 2

_FETCHABLE_SCHEMES = ("https://", "http://", "file://")


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def load_manifest(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def destination(manifest: dict, manifest_path: Path) -> Path:
    """The payload directory, resolved against the manifest's own location."""
    return (manifest_path.parent / manifest["destination"]).resolve()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def check_one(target: Path, entry: dict) -> str | None:
    """One complaint, or None when ``target`` matches its pin exactly."""
    if not target.is_file():
        return f"missing: {entry['name']}"
    size = target.stat().st_size
    if size != entry["bytes"]:
        return f"size mismatch: {entry['name']} holds {size} bytes, pinned {entry['bytes']}"
    digest = sha256_file(target)
    if digest != entry["sha256"]:
        return f"sha256 mismatch: {entry['name']} is {digest}, pinned {entry['sha256']}"
    return None


def verify(dest: Path, manifest: dict) -> list[str]:
    """Every complaint against the destination, empty when all 12 pins hold."""
    return [complaint for entry in manifest["files"] if (complaint := check_one(dest / entry["name"], entry))]


def refuse_unnamed(endpoint: dict) -> int:
    """The owner card: who names the host, and what works meanwhile."""
    log("error: the template hosting endpoint is not yet named; refusing to invent one.")
    log(f"  url:    {endpoint.get('url')}")
    log(f"  status: {endpoint.get('status')}")
    log(f"  owner:  {endpoint.get('owner')}")
    log(f"  ruling: {endpoint.get('ruling')}")
    log(f"  today:  {sys.executable} {Path(__file__).name} --from <dir holding the pinned .pptx>")
    return EXIT_UNNAMED


def pull(source: str, dest: Path, manifest: dict) -> list[str]:
    """Copy every pinned file from ``source`` (a directory or a URL base)
    into ``dest``, verifying each against its pin as it lands.

    A file failing its pin is removed rather than left behind: a wheel built
    over this directory must never package a byte the manifest did not sign.
    """
    complaints: list[str] = []
    dest.mkdir(parents=True, exist_ok=True)
    source_dir = Path(source) if not source.startswith(_FETCHABLE_SCHEMES) else None
    for entry in manifest["files"]:
        name = entry["name"]
        target = dest / name
        if source_dir is not None:
            candidate = source_dir / name
            if not candidate.is_file():
                complaints.append(f"missing at source: {name}")
                continue
            shutil.copyfile(candidate, target)
        else:
            with urllib.request.urlopen(f"{source.rstrip('/')}/{name}") as response:  # noqa: S310 -- scheme gated in main()
                target.write_bytes(response.read())
        if complaint := check_one(target, entry):
            target.unlink(missing_ok=True)
            complaints.append(complaint + " (removed)")
        else:
            log(f"[templates] verified {name} ({entry['bytes']} bytes)")
    return complaints


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fetch or verify the pinned deck templates.")
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--dest", default=None, help="override the manifest's destination directory")
    parser.add_argument(
        "--from", dest="source_dir", default=None, help="pull from a local directory instead of the endpoint"
    )
    parser.add_argument("--verify", action="store_true", help="verify what is already in place; pull nothing")
    args = parser.parse_args(argv)

    manifest_path = Path(args.manifest).resolve()
    manifest = load_manifest(manifest_path)
    dest = Path(args.dest).resolve() if args.dest else destination(manifest, manifest_path)

    if args.verify:
        complaints = verify(dest, manifest)
    elif args.source_dir:
        complaints = pull(args.source_dir, dest, manifest)
    else:
        endpoint = manifest.get("endpoint") or {}
        url = str(endpoint.get("url") or "")
        if endpoint.get("status") != "NAMED" or not url.startswith(_FETCHABLE_SCHEMES):
            return refuse_unnamed(endpoint)
        complaints = pull(url, dest, manifest)

    for complaint in complaints:
        log(f"[templates] {complaint}")
    if complaints:
        return EXIT_VERIFY
    log(f"[templates] all {len(manifest['files'])} pins hold under {dest}")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
