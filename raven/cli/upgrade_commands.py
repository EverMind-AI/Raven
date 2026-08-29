"""The `raven upgrade` command surface.

Upgrades the installed Raven in place without rebuilding the whole
environment: resolve the target version, swap the package, and refuse to
serve out of a half-written installation (see the guard in the serve
path). Everything here is transport: rendering, prompts and exit codes.
"""

from __future__ import annotations

import base64
import json
import os
import re
import shutil
import subprocess
import sys
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from importlib import metadata
from pathlib import Path
from urllib.parse import urlparse

import httpx
import typer
from rich.console import Console

from raven.cli import _install_guard

LATEST_RELEASE_API = "https://api.github.com/repos/EverMind-AI/Raven/releases/latest"
LATEST_RELEASE_WEB = "https://github.com/EverMind-AI/Raven/releases/latest"
RELEASE_TAG_PREFIX = "https://github.com/EverMind-AI/Raven/releases/tag/"
RELEASE_DOWNLOAD_PREFIX = "https://github.com/EverMind-AI/Raven/releases/download/"
_VERSION_RE = re.compile(r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")
_REQUEST_TIMEOUT = 10.0
# The fallback exists to rescue a failing command, so it may not add another full
# timeout to the wait: three sequential requests at 10s would triple the worst case.
_FALLBACK_TIMEOUT = 5.0
console = Console()


class UpgradeError(RuntimeError):
    pass


class ReleaseLookupError(UpgradeError):
    """Latest-release discovery failed. The local installation is fine, so the
    caller must not advise reinstalling."""


@dataclass(frozen=True)
class ReleaseInfo:
    version: str
    wheel_url: str


@dataclass(frozen=True)
class ToolInstallTarget:
    tool_dir: Path
    bin_dir: Path


_UPGRADE_HELPER_SOURCE = r"""import json
import os
import subprocess
import sys
import time

# How long to wait for the process being replaced to exit. Sized for a real
# `raven serve` shutdown, which cancels detached sub-agents, closes every MCP
# server, flushes the memory backend and closes the browser driver first, and so
# takes tens of seconds routinely and minutes at worst. A tight bound is not a
# safety margin here: the parent is going away either way, and giving up early
# means the install never runs and the surface the upgrade was clicked from
# never comes back. Waiting too long costs one sleeping helper; waiting too
# little costs the user a Raven they have to restart by hand.
PARENT_EXIT_TIMEOUT_S = 300

# Where the spawning side recorded that this environment is being replaced. The
# helper runs under `python -I` outside the environment it is rewriting, so it
# cannot import raven to ask; the path is handed over instead.
MARKER_PATH = os.environ.get("RAVEN_UPGRADE_MARKER") or None


def stamp_marker():
    # Claim the marker for this helper. The pid is the only thing that tells an
    # install still running from one whose helper was killed, and those two need
    # opposite answers from whoever is starting up.
    if not MARKER_PATH:
        return
    try:
        with open(MARKER_PATH, encoding="utf-8") as handle:
            payload = json.load(handle)
        if not isinstance(payload, dict):
            payload = {}
    except (OSError, ValueError):
        payload = {}
    payload.setdefault("started_at", time.time())
    payload["pid"] = os.getpid()
    try:
        with open(MARKER_PATH, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
    except OSError:
        pass


def clear_marker():
    if not MARKER_PATH:
        return
    try:
        os.unlink(MARKER_PATH)
    except OSError:
        pass


def wait_for_parent(parent_pid):
    if sys.platform != "win32":
        return wait_for_parent_posix(parent_pid)
    return wait_for_parent_windows(parent_pid)


def wait_for_parent_posix(parent_pid):
    # POSIX has no waitable handle for a non-child process, so poll the pid.
    # Bounded, so a parent that refuses to die cannot leave the helper resident.
    deadline = time.monotonic() + PARENT_EXIT_TIMEOUT_S
    while time.monotonic() < deadline:
        try:
            os.kill(parent_pid, 0)
        except ProcessLookupError:
            return 0
        except PermissionError:
            # The pid exists but belongs to someone else, i.e. the parent is
            # gone and its pid was recycled. Treat that as exited.
            return 0
        except OSError as exc:
            print(f"Unable to upgrade Raven: could not watch Raven ({exc}).", file=sys.stderr)
            return 1
        time.sleep(0.2)
    print("Unable to upgrade Raven: waiting for Raven to exit timed out.", file=sys.stderr)
    return 1


def wait_for_parent_windows(parent_pid):
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
        wait_status = kernel32.WaitForSingleObject(handle, PARENT_EXIT_TIMEOUT_S * 1000)
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
    # Claimed before the argument check, and released however this ends: a
    # helper that exits without clearing the marker leaves every later start
    # waiting on an install that is not running.
    stamp_marker()
    try:
        return run(argv)
    finally:
        clear_marker()


def run(argv=None):
    args = sys.argv[1:] if argv is None else argv
    if len(args) not in (4, 5, 6):
        print("Unable to upgrade Raven: invalid upgrade helper arguments.", file=sys.stderr)
        return 2

    uv_path, wheel_url, current_version, latest_version = args[:4]
    # A 6th argument is a JSON argv the helper runs after a successful install:
    # `raven serve` uses it to bring the gateway back up on the same port, so an
    # open GUI can reconnect instead of dying with the process it asked to
    # replace. The CLI passes 4 args and keeps the old exec-in-place behaviour.
    relaunch = None
    if len(args) == 6:
        try:
            relaunch = json.loads(args[5])
        except ValueError:
            print("Unable to upgrade Raven: invalid upgrade helper arguments.", file=sys.stderr)
            return 2
        if not (isinstance(relaunch, list) and relaunch and all(isinstance(x, str) for x in relaunch)):
            print("Unable to upgrade Raven: invalid upgrade helper arguments.", file=sys.stderr)
            return 2
    if len(args) >= 5:
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

    def run_uv(requirement, mode):
        command = [uv_path, "tool", "install"] + mode
        if constraints_path:
            command += ["-c", constraints_path]
        command.append(requirement)
        return subprocess.run(command, check=False).returncode

    def install(requirement):
        # Cheap shape first. `--force` tears the whole environment down and
        # writes 150-odd packages back even when the only thing that moved is
        # raven's own wheel; `--reinstall-package raven` replaces that wheel and
        # leaves the dependencies -- and their bytecode -- where they are. It
        # still resolves and syncs the rest, so a dependency the constraints
        # moved is moved here too; it is not a shortcut past correctness.
        #
        # `--force` stays as the fallback for the failures the cheap shape
        # reports. The reachable one is a stale entry on the executable name --
        # which is what a helper killed between writing the launcher and
        # finishing leaves: uv refuses with "Executable already exists: raven
        # (use `--force` to overwrite)" and only `--force` gets past it.
        #
        # What the fallback does NOT cover, said out loud because the shape of
        # the code invites the opposite assumption: a dependency whose files are
        # gone while its metadata survives. uv cannot see that, so the cheap
        # shape reports success, the fallback never runs, and the environment
        # stays broken. `--force` used to repair it by accident on every
        # upgrade. Nothing repairs it now short of rerunning the installer.
        status = run_uv(requirement, ["--reinstall-package", "raven"])
        if status == 0:
            return 0
        return run_uv(requirement, ["--force"])

    def restart():
        # Called on every path out of the install, not just the successful one.
        # Once the parent has exited, this helper holds the only handle to the
        # surface the user was sitting in -- so a failed install must still put
        # it back. Leaving nothing running is strictly worse than not upgrading:
        # the page the upgrade was clicked from cannot even reconnect to say
        # what went wrong, and the user is left with a dead window.
        #
        # The marker goes first, before anything is launched: the process being
        # started here reads it, and would wait out an upgrade that is over.
        clear_marker()
        if relaunch is None:
            return True
        try:
            kwargs = {} if sys.platform == "win32" else {"start_new_session": True}
            subprocess.Popen(relaunch, **kwargs)
        except OSError as exc:
            print(f"Could not restart Raven: {exc}.", file=sys.stderr)
            return False
        print("Raven restarted.")
        return True

    try:
        channel_status = install(f"raven[channels] @ {wheel_url}")
        if channel_status != 0:
            base_status = install(wheel_url)
            if base_status != 0:
                print(
                    f"Unable to upgrade Raven: uv exited with status {base_status}.",
                    file=sys.stderr,
                )
                restart()
                return base_status
            print(
                "Warning: Channel dependencies failed to install; installed base raven only. "
                "Some channels stay unavailable (see: raven channels list).",
                file=sys.stderr,
            )
    except OSError as exc:
        print(f"Unable to upgrade Raven: could not run uv: {exc}.", file=sys.stderr)
        restart()
        return 1

    print(f"Raven upgraded: {current_version} -> {latest_version}")
    if relaunch is None:
        print("Restart any other running Raven process to use the new version.")
        return 0
    return 0 if restart() else 1


if __name__ == "__main__":
    raise SystemExit(main())
"""


def _upgrade_helper_bootstrap() -> str:
    encoded = base64.b64encode(_UPGRADE_HELPER_SOURCE.encode("utf-8")).decode("ascii")
    return f'exec(compile(__import__("base64").b64decode("{encoded}"),"<raven-upgrade>","exec"))'


def _version_key(value: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(value)
    if match is None:
        raise UpgradeError(f"Unsupported Raven version: {value!r}")
    major, minor, patch = match.groups()
    return int(major), int(minor), int(patch)


def _current_version() -> str:
    return metadata.version("raven")


def _user_agent() -> str:
    return f"raven/{_current_version()}"


def _release_wheel_name(version: str) -> str:
    return f"raven-{version}-py3-none-any.whl"


def _release_wheel_url(version: str) -> str:
    return f"{RELEASE_DOWNLOAD_PREFIX}v{version}/{_release_wheel_name(version)}"


def _parse_release_payload(payload: object) -> ReleaseInfo:
    if not isinstance(payload, dict):
        raise ReleaseLookupError("Malformed GitHub release payload")

    draft = payload.get("draft")
    prerelease = payload.get("prerelease")
    if not isinstance(draft, bool) or not isinstance(prerelease, bool):
        raise ReleaseLookupError("Malformed GitHub release payload")
    if draft or prerelease:
        raise ReleaseLookupError("Latest Raven release is not stable")

    tag_name = payload.get("tag_name")
    if not isinstance(tag_name, str) or not tag_name.startswith("v"):
        raise ReleaseLookupError("Malformed GitHub release payload")
    version = ".".join(str(part) for part in _version_key(tag_name))

    assets = payload.get("assets")
    if not isinstance(assets, list):
        raise ReleaseLookupError("Malformed GitHub release payload")

    wheel_name = _release_wheel_name(version)
    exact_wheels: list[str] = []
    for asset in assets:
        if not isinstance(asset, dict):
            raise ReleaseLookupError("Malformed GitHub release payload")
        name = asset.get("name")
        wheel_url = asset.get("browser_download_url")
        if not isinstance(name, str) or not isinstance(wheel_url, str):
            raise ReleaseLookupError("Malformed GitHub release payload")
        if name == wheel_name:
            exact_wheels.append(wheel_url)

    if len(exact_wheels) != 1:
        raise ReleaseLookupError(f"Expected exactly one release wheel named {wheel_name}")

    wheel_url = exact_wheels[0]
    if wheel_url != _release_wheel_url(version):
        raise ReleaseLookupError(f"Untrusted Raven release wheel URL: {wheel_url}")

    return ReleaseInfo(version=version, wheel_url=wheel_url)


def _rate_limit_detail(response: httpx.Response) -> str:
    detail = "GitHub rate limit exhausted (unauthenticated requests share 60 per hour per IP)"
    try:
        resets_at = datetime.fromtimestamp(int(response.headers["x-ratelimit-reset"]))
    except (KeyError, ValueError, OSError, OverflowError):
        return detail
    return f"{detail}, resetting at {resets_at:%H:%M:%S}"


def _github_failure_detail(error: Exception) -> str:
    if isinstance(error, httpx.HTTPStatusError):
        response = error.response
        if response.status_code in (403, 429):
            if response.headers.get("x-ratelimit-remaining") == "0":
                return _rate_limit_detail(response)
            # The secondary (abuse) limit leaves the primary budget untouched and
            # says when to come back instead.
            retry_after = response.headers.get("retry-after")
            if retry_after:
                return f"GitHub asked us to retry in {retry_after}s (HTTP {response.status_code})"
        return f"HTTP {response.status_code} {response.reason_phrase}".strip()
    return str(error).strip() or type(error).__name__


def _sentence(error: Exception) -> str:
    """Terminate the message with a period unless it already ends in one.

    Strips a single trailing period rather than the whole run, so a message ending in
    an ellipsis keeps it.
    """
    return f"{str(error).removesuffix('.')}."


def _fetch_latest_release_via_api(client: httpx.Client) -> ReleaseInfo:
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": _user_agent(),
        "X-GitHub-Api-Version": "2022-11-28",
    }
    response = client.get(LATEST_RELEASE_API, headers=headers)
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        # A proxy or captive portal answering 200 with HTML is a remote failure the
        # release page can recover from, so it has to reach the fallback: DecodingError
        # is an httpx.HTTPError but not a TransportError, so it never reads as "offline".
        raise httpx.DecodingError("GitHub API returned a non-JSON body", request=response.request) from exc
    return _parse_release_payload(payload)


def _fetch_latest_version_via_redirect(client: httpx.Client, *, timeout: float = _REQUEST_TIMEOUT) -> str:
    """Read the latest stable version off the release page, which no API quota applies to.

    The timeout belongs to the caller: this is one of three sequential requests when
    `raven upgrade` falls back, but the only request the update notice makes.
    """
    response = client.get(
        LATEST_RELEASE_WEB,
        headers={"User-Agent": _user_agent()},
        follow_redirects=False,
        timeout=timeout,
    )
    location = response.headers.get("location", "")
    tag = location[len(RELEASE_TAG_PREFIX) :] if location.startswith(RELEASE_TAG_PREFIX) else ""
    if not response.has_redirect_location or not tag:
        raise ReleaseLookupError(f"HTTP {response.status_code} without a release tag redirect")
    return ".".join(str(part) for part in _version_key(tag))


def _fetch_latest_release_via_redirect(client: httpx.Client) -> ReleaseInfo:
    version = _fetch_latest_version_via_redirect(client, timeout=_FALLBACK_TIMEOUT)
    wheel_url = _release_wheel_url(version)
    try:
        client.head(
            wheel_url,
            headers={"User-Agent": _user_agent()},
            follow_redirects=True,
            timeout=_FALLBACK_TIMEOUT,
        ).raise_for_status()
    except httpx.HTTPError as exc:
        raise ReleaseLookupError(
            f"release {version} has no wheel at the expected URL ({_github_failure_detail(exc)})"
        ) from exc
    return ReleaseInfo(version=version, wheel_url=wheel_url)


def _resolve_latest_release(client: httpx.Client) -> ReleaseInfo:
    try:
        return _fetch_latest_release_via_api(client)
    except httpx.HTTPError as error:
        # Only transport / status / decoding failures fall back. No payload-level
        # failure is routed around, because the release page cannot re-check what the
        # payload carries -- above all the draft / prerelease flags, where falling
        # back would install exactly what the API rejected.
        api_error = error

    try:
        return _fetch_latest_release_via_redirect(client)
    except (UpgradeError, httpx.HTTPError) as web_error:
        message = (
            f"could not resolve the latest Raven release "
            f"(GitHub API: {_github_failure_detail(api_error)}; "
            f"release page: {_github_failure_detail(web_error)})"
        )
        if isinstance(api_error, httpx.TransportError) and isinstance(web_error, httpx.TransportError):
            message += "; check your network and try again"
        raise ReleaseLookupError(message) from web_error


def _fetch_latest_release(client: httpx.Client | None = None) -> ReleaseInfo:
    if client is not None:
        return _resolve_latest_release(client)
    with httpx.Client(timeout=_REQUEST_TIMEOUT, follow_redirects=True) as owned_client:
        return _resolve_latest_release(owned_client)


def fetch_latest_version(client: httpx.Client | None = None) -> str:
    """Return the latest stable version, resolved without touching the API quota.

    The update notice needs a version string and nothing else -- no payload, no wheel
    URL -- so it stays off `api.github.com` entirely. That budget is 60 requests per
    hour per IP for unauthenticated callers, and a daily check from every install
    behind one egress is what drains it.
    """
    if client is not None:
        return _fetch_latest_version_via_redirect(client)
    with httpx.Client(timeout=_REQUEST_TIMEOUT, follow_redirects=True) as owned_client:
        return _fetch_latest_version_via_redirect(owned_client)


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
    env["RAVEN_UPGRADE_MARKER"] = str(_install_guard.write_marker(to_version=release.version))
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
        _install_guard.clear_marker()
        raise UpgradeError(f"Could not start the Raven upgrade helper: {exc}") from exc
    raise UpgradeError("The Raven upgrade helper returned unexpectedly")


@dataclass(frozen=True)
class UpgradePlan:
    current_version: str
    release: ReleaseInfo
    target: ToolInstallTarget


def fetch_latest_for_channel() -> tuple[ReleaseInfo, Callable[[str], tuple[int, ...]]]:
    """The newest build this install should see, and the ordering to judge it by.

    Imported lazily because ``beta_channel`` imports this module for its release
    type. Its comparison reads a plain ``X.Y.Z`` too, so the beta channel can
    keep using it after a tester's build catches up with a stable one.
    """
    from raven.cli import beta_channel

    chan = beta_channel.channel()
    if chan is None:
        return _fetch_latest_release(), _version_key
    return beta_channel.fetch_latest(chan), beta_channel.release_key


def plan_upgrade() -> UpgradePlan:
    """Resolve what an upgrade would install, or raise ``UpgradeError`` with why.

    Network-bound (fetches the latest release), so callers on an event loop must
    run it in a thread.
    """
    current_version = _current_version()
    release, version_key = fetch_latest_for_channel()
    if version_key(current_version) >= version_key(release.version):
        raise UpgradeError(f"Raven {current_version} is already up to date")
    if _is_editable_install():
        raise UpgradeError("Editable Raven installations cannot be upgraded automatically")
    target = _uv_tool_target()
    if target is None:
        raise UpgradeError("This Raven installation is not managed by uv")
    return UpgradePlan(current_version=current_version, release=release, target=target)


def spawn_detached_upgrade(
    plan: UpgradePlan,
    *,
    parent_pid: int,
    relaunch: list[str] | None = None,
    extra_env: dict[str, str] | None = None,
) -> None:
    """Start the upgrade helper as a process that outlives this one.

    The CLI's ``_handoff_upgrade`` execs the helper in place, which a server
    cannot do: it still owes its caller a reply. So the helper is detached
    instead, told which pid to wait for, and optionally given a command to run
    once the install lands (see the helper's 6th argument).
    """
    uv_value = shutil.which("uv")
    if uv_value is None:
        raise UpgradeError("uv was not found on PATH")
    uv_path = _external_executable(uv_value, label="uv")
    base_python = _external_executable(getattr(sys, "_base_executable", None), label="Raven base Python")

    env = os.environ.copy()
    env["UV_TOOL_DIR"] = str(plan.target.tool_dir)
    env["UV_TOOL_BIN_DIR"] = str(plan.target.bin_dir)
    # Written here rather than by the helper: the window this marker exists for
    # opens the moment the caller lets go of the port, which is before the
    # helper has run its first instruction.
    env["RAVEN_UPGRADE_MARKER"] = str(_install_guard.write_marker(to_version=plan.release.version))
    if extra_env:
        env.update(extra_env)

    argv = [
        str(base_python),
        "-I",
        "-c",
        _upgrade_helper_bootstrap(),
        str(uv_path),
        plan.release.wheel_url,
        plan.current_version,
        plan.release.version,
        str(parent_pid),
    ]
    if relaunch is not None:
        argv.append(json.dumps(relaunch))

    kwargs: dict[str, object] = {} if sys.platform == "win32" else {"start_new_session": True}
    try:
        subprocess.Popen(argv, env=env, **kwargs)  # noqa: S603 - argv is built from resolved executables
    except OSError as exc:
        _install_guard.clear_marker()
        raise UpgradeError(f"Could not start the Raven upgrade helper: {exc}") from exc


def register(app: typer.Typer) -> None:
    @app.command()
    def upgrade(
        check: bool = typer.Option(
            False,
            "--check",
            help="Check for a newer Raven build without installing it.",
        ),
    ) -> None:
        """Check for and install the newest Raven build this install is entitled to.

        Stable by default; an install that joined the beta channel gets its
        newest beta instead. The failure path of the served page's one-click
        update tells the reader to run this, so it has to follow the same
        channel that offered them the update.
        """
        try:
            current_version = _current_version()
            release, version_key = fetch_latest_for_channel()
            current_key = version_key(current_version)
            latest_key = version_key(release.version)

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
        except ReleaseLookupError as exc:
            console.print(f"[red]Unable to upgrade Raven:[/red] {_sentence(exc)}")
            raise typer.Exit(1) from exc
        except (
            UpgradeError,
            httpx.HTTPError,
            ValueError,
            metadata.PackageNotFoundError,
        ) as exc:
            console.print(
                f"[red]Unable to upgrade Raven:[/red] {_sentence(exc)} "
                "If the problem persists, rerun the official installer."
            )
            raise typer.Exit(1) from exc
