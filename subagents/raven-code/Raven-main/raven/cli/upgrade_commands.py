from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from urllib.parse import urlparse

import httpx
import typer
from rich.console import Console

LATEST_RELEASE_API = "https://api.github.com/repos/EverMind-AI/Raven/releases/latest"
_VERSION_RE = re.compile(r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
console = Console()


class UpgradeError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    wheel_url: str


@dataclass(frozen=True)
class ToolInstallTarget:
    tool_dir: Path
    bin_dir: Path


_UPGRADE_HELPER_SOURCE = r"""import subprocess
import sys


def wait_for_parent(parent_pid):
    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102
    wait_failed = 0xFFFFFFFF
    error_invalid_parameter = 87

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(synchronize, False, parent_pid)
    if not handle:
        error = ctypes.get_last_error()
        if error == error_invalid_parameter:
            return 0
        print(
            f"Unable to upgrade Raven: could not wait for Raven to exit "
            f"(Windows error {error}).",
            file=sys.stderr,
        )
        return 1

    try:
        wait_status = kernel32.WaitForSingleObject(handle, 30_000)
        wait_error = ctypes.get_last_error()
    finally:
        kernel32.CloseHandle(handle)

    if wait_status == wait_object_0:
        return 0
    if wait_status == wait_timeout:
        detail = "timed out"
    elif wait_status == wait_failed:
        detail = f"failed with Windows error {wait_error}"
    else:
        detail = f"returned unexpected status {wait_status}"
    print(f"Unable to upgrade Raven: waiting for Raven to exit {detail}.", file=sys.stderr)
    return 1


def main(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (4, 5):
        print("Unable to upgrade Raven: invalid upgrade helper arguments.", file=sys.stderr)
        return 2

    uv_path, wheel_url, current_version, latest_version = args[:4]
    if len(args) == 5:
        try:
            parent_pid = int(args[4])
        except (TypeError, ValueError):
            print("Unable to upgrade Raven: invalid upgrade helper arguments.", file=sys.stderr)
            return 2
        if parent_pid <= 0:
            print("Unable to upgrade Raven: invalid upgrade helper arguments.", file=sys.stderr)
            return 2
        parent_status = wait_for_parent(parent_pid)
        if parent_status != 0:
            return parent_status

    # Pin to the same locked constraints the installer uses. Derive the URL from
    # the (already trust-checked) wheel URL so the constraints always match the
    # wheel being installed. Missing asset / download failure -> upgrade without
    # pinning rather than abort.
    constraints_path = None
    constraints_url = wheel_url.rsplit("/", 1)[0] + "/raven-constraints.txt"
    try:
        import os
        import socket
        import tempfile
        import urllib.request

        socket.setdefaulttimeout(30)
        fd, constraints_path = tempfile.mkstemp(prefix="raven-constraints-", suffix=".txt")
        os.close(fd)
        urllib.request.urlretrieve(constraints_url, constraints_path)
    except Exception:
        print(
            "Warning: could not download locked constraints; "
            "upgrading without version pinning.",
            file=sys.stderr,
        )
        constraints_path = None

    def install(requirement):
        command = [uv_path, "tool", "install", "--force"]
        if constraints_path:
            command += ["-c", constraints_path]
        command.append(requirement)
        return subprocess.run(command, check=False).returncode

    try:
        channel_status = install(f"raven[channels] @ {wheel_url}")
        if channel_status != 0:
            base_status = install(wheel_url)
            if base_status != 0:
                print(
                    f"Unable to upgrade Raven: uv exited with status {base_status}.",
                    file=sys.stderr,
                )
                return base_status
            print(
                "Warning: Channel dependencies failed to install; installed base raven only. "
                "Some channels stay unavailable (see: raven channels list).",
                file=sys.stderr,
            )
    except OSError as exc:
        print(f"Unable to upgrade Raven: could not run uv: {exc}.", file=sys.stderr)
        return 1

    print(f"Raven upgraded: {current_version} -> {latest_version}")
    print("Restart any other running Raven process to use the new version.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _upgrade_helper_bootstrap() -> str:
    encoded = base64.b64encode(_UPGRADE_HELPER_SOURCE.encode("utf-8")).decode("ascii")
    return f'exec(compile(__import__("base64").b64decode("{encoded}"),"<raven-upgrade>","exec"))'


def _version_key(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(value)
    if match is None:
        raise UpgradeError(f"Unsupported Raven version: {value}")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _current_version() -> str:
    return metadata.version("raven")


def _parse_release_payload(payload: object) -> ReleaseInfo:
    if not isinstance(payload, dict):
        raise UpgradeError("Malformed GitHub release payload")

    draft = payload.get("draft")
    prerelease = payload.get("prerelease")
    if not isinstance(draft, bool) or not isinstance(prerelease, bool):
        raise UpgradeError("Malformed GitHub release payload")
    if draft or prerelease:
        raise UpgradeError("Latest Raven release is not stable")

    tag_name = payload.get("tag_name")
    if not isinstance(tag_name, str) or not tag_name.startswith("v"):
        raise UpgradeError("Malformed GitHub release payload")
    version = ".".join(str(part) for part in _version_key(tag_name))

    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise UpgradeError("Malformed GitHub release payload")

    wheel_name = f"raven-{version}-py3-none-any.whl"
    exact_wheels: list[str] = []
    for asset in assets:
        if not isinstance(asset, dict):
            raise UpgradeError("Malformed GitHub release payload")
        name = asset.get("name")
        wheel_url = asset.get("browser_download_url")
        if not isinstance(name, str) or not isinstance(wheel_url, str):
            raise UpgradeError("Malformed GitHub release payload")
        if name == wheel_name:
            exact_wheels.append(wheel_url)

    if len(exact_wheels) != 1:
        raise UpgradeError(f"Expected exactly one release wheel named {wheel_name}")

    wheel_url = exact_wheels[0]
    parsed_url = urlparse(wheel_url)
    expected_path = f"/EverMind-AI/Raven/releases/download/v{version}/{wheel_name}"
    if parsed_url.scheme != "https" or parsed_url.netloc != "github.com" or parsed_url.path != expected_path:
        raise UpgradeError(f"Untrusted Raven release wheel URL: {wheel_url}")

    return ReleaseInfo(version=version, wheel_url=wheel_url)


def _fetch_latest_release(client: httpx.Client | None = None) -> ReleaseInfo:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": f"raven/{_current_version()}",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if client is not None:
        response = client.get(LATEST_RELEASE_API, headers=headers)
        response.raise_for_status()
        return _parse_release_payload(response.json())
    with httpx.Client(timeout=10.0, follow_redirects=True) as owned_client:
        response = owned_client.get(LATEST_RELEASE_API, headers=headers)
        response.raise_for_status()
        return _parse_release_payload(response.json())


def _direct_url_data() -> dict[str, object] | None:
    raw = metadata.distribution("raven").read_text("direct_url.json")
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UpgradeError("Malformed Raven installation metadata") from exc
    if not isinstance(data, dict):
        raise UpgradeError("Malformed Raven installation metadata")

    url = data.get("url")
    origins = [key for key in ("archive_info", "dir_info", "vcs_info") if key in data]
    if not isinstance(url, str) or not url.strip() or not urlparse(url).scheme or len(origins) != 1:
        raise UpgradeError("Malformed Raven installation metadata")

    origin_name = origins[0]
    origin = data[origin_name]
    if not isinstance(origin, dict):
        raise UpgradeError("Malformed Raven installation metadata")

    if origin_name == "archive_info":
        archive_hash = origin.get("hash")
        hashes = origin.get("hashes")
        if archive_hash is not None and (not isinstance(archive_hash, str) or not archive_hash.strip()):
            raise UpgradeError("Malformed Raven installation metadata")
        if hashes is not None and (
            not isinstance(hashes, dict)
            or any(
                not isinstance(algorithm, str)
                or not algorithm.strip()
                or not isinstance(digest, str)
                or not digest.strip()
                for algorithm, digest in hashes.items()
            )
        ):
            raise UpgradeError("Malformed Raven installation metadata")
    elif origin_name == "dir_info":
        editable = origin.get("editable", False)
        if not isinstance(editable, bool):
            raise UpgradeError("Malformed Raven installation metadata")
    elif origin_name == "vcs_info":
        vcs = origin.get("vcs")
        commit_id = origin.get("commit_id")
        requested_revision = origin.get("requested_revision")
        if (
            not isinstance(vcs, str)
            or not vcs.strip()
            or not isinstance(commit_id, str)
            or not commit_id.strip()
            or (requested_revision is not None and not isinstance(requested_revision, str))
        ):
            raise UpgradeError("Malformed Raven installation metadata")
    return data


def _is_editable_install() -> bool:
    data = _direct_url_data()
    if data is None or "dir_info" not in data:
        return False
    directory = data["dir_info"]
    editable = directory.get("editable", False)
    return editable


def _uv_tool_target() -> ToolInstallTarget | None:
    prefix = Path(sys.prefix)
    if not prefix.is_absolute():
        raise UpgradeError("Malformed Raven uv tool receipt")

    receipt_path = prefix / "uv-receipt.toml"
    if not receipt_path.is_file():
        return None
    try:
        receipt = tomllib.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise UpgradeError("Malformed Raven uv tool receipt") from exc

    tool = receipt.get("tool")
    if not isinstance(tool, dict):
        raise UpgradeError("Malformed Raven uv tool receipt")
    requirements = tool.get("requirements")
    if not isinstance(requirements, list):
        raise UpgradeError("Malformed Raven uv tool receipt")
    if any(not isinstance(item, dict) for item in requirements):
        raise UpgradeError("Malformed Raven uv tool receipt")
    if not any(item.get("name") == "raven" for item in requirements):
        return None

    entrypoints = tool.get("entrypoints")
    if not isinstance(entrypoints, list) or any(not isinstance(item, dict) for item in entrypoints):
        raise UpgradeError("Malformed Raven uv tool receipt")
    raven_entrypoints = [item for item in entrypoints if item.get("name") == "raven"]
    if len(raven_entrypoints) != 1:
        raise UpgradeError("Malformed Raven uv tool receipt")

    install_path_value = raven_entrypoints[0].get("install-path")
    if not isinstance(install_path_value, str) or not install_path_value.strip():
        raise UpgradeError("Malformed Raven uv tool receipt")
    install_path = Path(install_path_value)
    if not install_path.is_absolute():
        raise UpgradeError("Malformed Raven uv tool receipt")

    return ToolInstallTarget(tool_dir=prefix.parent, bin_dir=install_path.parent)


def _is_uv_tool_install() -> bool:
    return _uv_tool_target() is not None


def _external_executable(value: object, *, label: str) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise UpgradeError(f"{label} was not found")
    try:
        executable = Path(value).resolve(strict=True)
        prefix = Path(sys.prefix).resolve(strict=True)
    except OSError as exc:
        raise UpgradeError(f"{label} is unavailable") from exc
    if not executable.is_file() or executable.is_relative_to(prefix):
        raise UpgradeError(f"{label} must be outside the active Raven tool environment")
    return executable


def _handoff_upgrade(
    release: ReleaseInfo,
    current_version: str,
    target: ToolInstallTarget,
) -> None:
    uv_value = shutil.which("uv")
    if uv_value is None:
        raise UpgradeError("uv was not found on PATH")
    uv_path = _external_executable(uv_value, label="uv")
    base_python = _external_executable(getattr(sys, "_base_executable", None), label="Raven base Python")

    env = os.environ.copy()
    env["UV_TOOL_DIR"] = str(target.tool_dir)
    env["UV_TOOL_BIN_DIR"] = str(target.bin_dir)
    argv = [
        str(base_python),
        "-I",
        "-c",
        _upgrade_helper_bootstrap(),
        str(uv_path),
        release.wheel_url,
        current_version,
        release.version,
    ]
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        if sys.platform == "win32":
            argv.append(str(os.getppid()))
            subprocess.Popen(argv, env=env)
            print("Raven upgrade started. Wait for the completion message before running Raven again.")
            return
        os.execve(str(base_python), argv, env)
    except OSError as exc:
        raise UpgradeError(f"Could not start the Raven upgrade helper: {exc}") from exc
    raise UpgradeError("The Raven upgrade helper returned unexpectedly")


def register(app: typer.Typer) -> None:
    @app.command()
    def upgrade(
        check: bool = typer.Option(
            False,
            "--check",
            help="Check for a newer stable Raven release without installing it.",
        ),
    ) -> None:
        """Check for and install the latest stable Raven release."""
        try:
            current_version = _current_version()
            release = _fetch_latest_release()
            current_key = _version_key(current_version)
            latest_key = _version_key(release.version)

            if current_key == latest_key:
                console.print(f"Raven {current_version} is up to date.")
                return
            if current_key > latest_key:
                console.print(
                    f"Raven {current_version} is newer than the latest release "
                    f"{release.version}; no downgrade was performed."
                )
                return
            if check:
                console.print(f"Raven upgrade available: {current_version} -> {release.version}")
                console.print("Run [cyan]raven upgrade[/cyan] to install it.")
                return
            if _is_editable_install():
                raise UpgradeError(
                    "Editable Raven installations cannot be upgraded automatically. "
                    "Pull the source checkout and rebuild Raven."
                )
            target = _uv_tool_target()
            if target is None:
                raise UpgradeError(
                    "This Raven installation is not managed by uv. "
                    "Reinstall Raven with the official installer, then run raven upgrade."
                )

            _handoff_upgrade(release, current_version, target)
        except (
            UpgradeError,
            httpx.HTTPError,
            ValueError,
            metadata.PackageNotFoundError,
        ) as exc:
            console.print(
                f"[red]Unable to upgrade Raven:[/red] {exc}. "
                "Check your network and try again; if the problem persists, "
                "rerun the official installer."
            )
            raise typer.Exit(1) from exc
