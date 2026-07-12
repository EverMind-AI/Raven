from __future__ import annotations

import re
from dataclasses import dataclass
from importlib import metadata
from urllib.parse import urlparse

import httpx

LATEST_RELEASE_API = "https://api.github.com/repos/EverMind-AI/Raven/releases/latest"
_VERSION_RE = re.compile(r"^v?(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)$")


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
