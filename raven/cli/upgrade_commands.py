from __future__ import annotations

import json
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
    if not isinstance(tag_name, str):
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


def _direct_url_data() -> dict[str, object]:
    raw = metadata.distribution("raven").read_text("direct_url.json")
    if raw is None:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise UpgradeError("Malformed Raven installation metadata") from exc
    if not isinstance(data, dict):
        raise UpgradeError("Malformed Raven installation metadata")
    return data


def _is_editable_install() -> bool:
    data = _direct_url_data()
    if "dir_info" not in data:
        return False
    directory = data["dir_info"]
    if not isinstance(directory, dict):
        raise UpgradeError("Malformed Raven installation metadata")
    editable = directory.get("editable", False)
    if not isinstance(editable, bool):
        raise UpgradeError("Malformed Raven installation metadata")
    return editable


def _is_uv_tool_install() -> bool:
    receipt_path = Path(sys.prefix) / "uv-receipt.toml"
    if not receipt_path.is_file():
        return False
    receipt = tomllib.loads(receipt_path.read_text(encoding="utf-8"))
    tool = receipt.get("tool")
    if not isinstance(tool, dict):
        return False
    requirements = tool.get("requirements")
    if not isinstance(requirements, list):
        return False
    return any(isinstance(item, dict) and item.get("name") == "raven" for item in requirements)


def _run_uv(uv_path: str, requirement: str) -> int:
    completed = subprocess.run(
        [uv_path, "tool", "install", "--force", requirement],
        check=False,
    )
    return completed.returncode


def _install_release(release: ReleaseInfo) -> None:
    uv_path = shutil.which("uv")
    if uv_path is None:
        raise UpgradeError("uv was not found on PATH")
    if _run_uv(uv_path, f"raven[channels] @ {release.wheel_url}") == 0:
        return
    if _run_uv(uv_path, release.wheel_url) != 0:
        raise UpgradeError("uv could not install the Raven release wheel")


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
            if not _is_uv_tool_install():
                raise UpgradeError(
                    "This Raven installation is not managed by uv. "
                    "Reinstall Raven with the official installer, then run raven upgrade."
                )

            _install_release(release)
            console.print(f"[green]Raven upgraded: {current_version} -> {release.version}[/green]")
            console.print("Restart any running Raven process to use the new version.")
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
