"""Build the current checkout and publish it to the beta channel.

Run from a clean worktree after merging what testers should get:

    make beta

The build steps mirror ``.github/workflows/release.yml`` exactly -- the TUI
bundle and the served page are gitignored build artifacts, so a wheel built
without them installs a Raven whose `raven tui` cannot start and whose page
answers "No front end built here".

Versions are ``<next patch>b<serial>``: a beta of 0.1.11 is 0.1.12b1, which
orders above released 0.1.11 and below an eventual 0.1.12. The serial comes
from whatever the channel already points at, so publishing twice in a row
gives b1 then b2.

The publish credential is a GitLab deploy token scoped to
``write_package_registry`` on the distribution project, read from
``RAVEN_BETA_PUBLISH_TOKEN`` or from ``~/.raven/beta_publish.json``. It is not
the token testers hold: theirs only reads.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import httpx

_HOST = "gitlab.com"
_TIMEOUT_S = 120.0
_POINTER_PATH = "latest/latest.json"
_VERSION_RE = re.compile(r"^(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_PYPROJECT_VERSION_RE = re.compile(r'^version = "[^"]*"$', re.MULTILINE)


class PublishError(RuntimeError):
    pass


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _run(argv: list[str], *, cwd: Path) -> None:
    print(f"  $ {' '.join(argv)}", flush=True)
    result = subprocess.run(argv, cwd=cwd)  # noqa: S603 - argv is built here, not from input
    if result.returncode != 0:
        raise PublishError(f"Command failed ({result.returncode}): {' '.join(argv)}")


def _credentials() -> tuple[str, str, str]:
    """``(project, publish token, tester token)``, or raise with how to supply them.

    Two tokens because they are two different powers: the publish one writes to
    the registry, the tester one only reads it and is what gets baked into the
    installer we hand out.
    """
    from raven.config.loader import raven_home

    path = raven_home() / "beta_publish.json"
    data: dict[str, object] = {}
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(parsed, dict):
            data = parsed
    except (OSError, ValueError):
        pass

    def _field(env: str, key: str) -> str:
        return (os.environ.get(env, "").strip()) or str(data.get(key, "")).strip()

    project = _field("RAVEN_BETA_PROJECT", "project")
    publish_token = _field("RAVEN_BETA_PUBLISH_TOKEN", "publish_token")
    read_token = _field("RAVEN_BETA_READ_TOKEN", "read_token")
    if not project or not publish_token:
        raise PublishError(
            f"No publish credentials. Set RAVEN_BETA_PROJECT and RAVEN_BETA_PUBLISH_TOKEN, or write {path}"
        )
    return project, publish_token, read_token


def _api_base(project: str) -> str:
    return f"https://{_HOST}/api/v4/projects/{project}/packages/generic/raven"


def _published_version(api_base: str, token: str) -> str | None:
    """The version the channel currently points at, or ``None`` for an empty one."""
    with httpx.Client(timeout=_TIMEOUT_S, follow_redirects=True) as client:
        response = client.get(f"{api_base}/{_POINTER_PATH}", headers={"DEPLOY-TOKEN": token})
        if response.status_code == 404:
            return None
        response.raise_for_status()
        payload = response.json()
    if not isinstance(payload, dict):
        return None
    version = payload.get("version")
    return version if isinstance(version, str) else None


def _next_version(base_version: str, published: str | None) -> str:
    """``<next patch>b<serial>``, continuing the serial when the base is unchanged."""
    match = _VERSION_RE.fullmatch(base_version)
    if match is None:
        raise PublishError(f"pyproject version is not X.Y.Z: {base_version}")
    major, minor, patch = (int(part) for part in match.groups())
    target = f"{major}.{minor}.{patch + 1}"

    serial = 1
    if published is not None:
        prior = re.fullmatch(r"(\d+\.\d+\.\d+)b(\d+)", published)
        if prior is not None and prior.group(1) == target:
            serial = int(prior.group(2)) + 1
    return f"{target}b{serial}"


def _build(root: Path, version: str) -> tuple[Path, Path]:
    """Build the wheel at ``version``, returning ``(wheel, constraints)``.

    The version reaches the wheel through pyproject, which is restored before
    this returns whether the build worked or not -- a half-published beta must
    not leave the checkout claiming to be one.
    """
    dist = root / "dist"
    if dist.exists():
        shutil.rmtree(dist)

    print("Building the TUI bundle...", flush=True)
    _run(["npm", "ci"], cwd=root / "ui-tui")
    _run(["npm", "run", "build"], cwd=root / "ui-tui")
    if not (root / "ui-tui/dist/entry.js").is_file():
        raise PublishError("ui-tui/dist/entry.js did not land")

    print("Building the served page...", flush=True)
    _run(["npm", "ci", "--prefix", "ui-web"], cwd=root)
    _run(["npm", "run", "--prefix", "ui-web", "build"], cwd=root)
    _run([sys.executable, "ui-web/build.py"], cwd=root)
    if not (root / "ui-web/dist/index.html").is_file():
        raise PublishError("ui-web/dist/index.html did not land")

    pyproject = root / "pyproject.toml"
    original = pyproject.read_text(encoding="utf-8")
    stamped, count = _PYPROJECT_VERSION_RE.subn(f'version = "{version}"', original, count=1)
    if count != 1:
        raise PublishError("Could not find the version line in pyproject.toml")

    # uv.lock records the project's own version, so `uv export` rewrites it to
    # match the stamp and leaves the checkout claiming to be a beta. That
    # survives into whatever you commit next, and a lockfile disagreeing with
    # pyproject is exactly what `uv export --locked` fails the release on.
    lock = root / "uv.lock"
    original_lock = lock.read_text(encoding="utf-8") if lock.is_file() else None
    try:
        pyproject.write_text(stamped, encoding="utf-8")
        print(f"Building the wheel at {version}...", flush=True)
        _run(["uv", "build", "--wheel"], cwd=root)
        _run(
            ["uv", "export", "--all-extras", "--no-hashes", "--no-emit-workspace", "-o", "dist/raven-constraints.txt"],
            cwd=root,
        )
    finally:
        pyproject.write_text(original, encoding="utf-8")
        if original_lock is not None:
            lock.write_text(original_lock, encoding="utf-8")

    wheel = dist / f"raven-{version}-py3-none-any.whl"
    constraints = dist / "raven-constraints.txt"
    if not wheel.is_file():
        # Listed defensively: a build that failed early leaves no dist/ at all,
        # and an iterdir() on it would replace the real reason with an
        # unrelated FileNotFoundError.
        found = sorted(p.name for p in dist.iterdir()) if dist.is_dir() else "no dist/ directory"
        raise PublishError(f"Expected {wheel.name} in dist/, found: {found}")
    if not constraints.is_file():
        raise PublishError("Constraints export did not land")
    _verify_wheel(wheel)
    return wheel, constraints


def _verify_wheel(wheel: Path) -> None:
    """The same gate the release workflow applies, for the same reason."""
    import zipfile

    names = zipfile.ZipFile(wheel).namelist()
    if "raven/ui-tui/dist/entry.js" not in names:
        raise PublishError("entry.js missing from wheel")
    if "raven/ui/dist/index.html" not in names:
        raise PublishError("index.html missing from wheel")
    if not any(name.startswith("raven/ui/dist/assets/") for name in names):
        raise PublishError("page assets missing from wheel")
    if any("node_modules" in name for name in names):
        raise PublishError("node_modules leaked into wheel")
    # The agent-product tree degrades quietly in the other direction: a wheel built
    # from an sdist simply has no folders, and onboarding step 5 reports an empty
    # installation rather than failing.
    if not any(name.endswith("/subagent.json") for name in names):
        raise PublishError("agent products missing from wheel")
    leaked = [name for name in names if _is_secret_entry(name)]
    if leaked:
        raise PublishError(f"secrets leaked into wheel: {leaked}")


def _is_secret_entry(name: str) -> bool:
    """A filled-in secrets file rather than the template beside it."""
    leaf = name.rsplit("/", 1)[-1]
    if not (leaf == ".env" or leaf.startswith(".env.")):
        return False
    return not leaf.endswith((".example", ".sample", ".template"))


def _upload(client: httpx.Client, url: str, token: str, body: bytes) -> None:
    response = client.put(url, headers={"DEPLOY-TOKEN": token}, content=body)
    response.raise_for_status()


def _installer_script(root: Path, project: str, read_token: str) -> bytes:
    """``beta.sh`` with the channel filled in.

    The repository copy carries placeholders so the credential lives only in
    the registry, behind that same credential.
    """
    text = (root / "beta.sh").read_text(encoding="utf-8")
    for placeholder, value in (("__RAVEN_BETA_PROJECT__", project), ("__RAVEN_BETA_TOKEN__", read_token)):
        if placeholder not in text:
            raise PublishError(f"beta.sh no longer contains {placeholder}")
        text = text.replace(placeholder, value)
    return text.encode("utf-8")


def _publish(
    api_base: str,
    token: str,
    version: str,
    wheel: Path,
    constraints: Path,
    installer: bytes | None,
) -> None:
    """Upload the build, then move the pointer -- never the other way round.

    The pointer is what every tester polls, so it goes last: until it moves,
    the channel still names a version that is fully uploaded.
    """
    with httpx.Client(timeout=_TIMEOUT_S, follow_redirects=True) as client:
        print(f"Uploading {wheel.name} ({wheel.stat().st_size // 1024} KiB)...", flush=True)
        _upload(client, f"{api_base}/{version}/{wheel.name}", token, wheel.read_bytes())
        print("Uploading raven-constraints.txt...", flush=True)
        _upload(client, f"{api_base}/{version}/raven-constraints.txt", token, constraints.read_bytes())
        if installer is not None:
            print("Uploading beta.sh...", flush=True)
            _upload(client, f"{api_base}/latest/beta.sh", token, installer)
        print("Moving the pointer...", flush=True)
        pointer = json.dumps({"version": version}).encode("utf-8")
        _upload(client, f"{api_base}/{_POINTER_PATH}", token, pointer)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Publish this checkout to the Raven beta channel.")
    parser.add_argument("--version", help="Publish exactly this version instead of the next serial.")
    parser.add_argument("--dry-run", action="store_true", help="Build and report, upload nothing.")
    args = parser.parse_args(argv)

    root = _repo_root()
    try:
        project, publish_token, read_token = _credentials()
        api_base = _api_base(project)
        base_version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        published = _published_version(api_base, publish_token)
        version = args.version or _next_version(base_version, published)
        print(f"Channel is at {published or '(empty)'}; publishing {version}\n", flush=True)

        installer = _installer_script(root, project, read_token) if read_token else None
        if installer is None:
            print("note: no tester token configured, so beta.sh will not be refreshed", flush=True)

        wheel, constraints = _build(root, version)
        if args.dry_run:
            print(f"\nDry run: built {wheel.name}, uploaded nothing.")
            return 0
        _publish(api_base, publish_token, version, wheel, constraints, installer)
    except PublishError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"error: publishing failed: {exc}", file=sys.stderr)
        return 1

    print(f"\nPublished {version}.")
    print("Every tester's gateway polls this pointer once a minute, so they are offered it within about that.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
