from __future__ import annotations

from dataclasses import FrozenInstanceError

import httpx
import pytest

from raven.cli import upgrade_commands

WHEEL_NAME = "raven-0.1.4-py3-none-any.whl"
WHEEL_URL = "https://github.com/EverMind-AI/Raven/releases/download/v0.1.4/raven-0.1.4-py3-none-any.whl"


def _release_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "tag_name": "v0.1.4",
        "draft": False,
        "prerelease": False,
        "assets": [
            {
                "name": WHEEL_NAME,
                "browser_download_url": WHEEL_URL,
            }
        ],
    }
    payload.update(overrides)
    return payload


def test_release_info_is_immutable() -> None:
    release = upgrade_commands.ReleaseInfo(version="0.1.4", wheel_url=WHEEL_URL)

    with pytest.raises(FrozenInstanceError):
        setattr(release, "version", "0.1.5")


def test_version_key_accepts_documented_stable_versions() -> None:
    assert upgrade_commands._version_key("0.1.3") == (0, 1, 3)
    assert upgrade_commands._version_key("v2.10.4") == (2, 10, 4)


@pytest.mark.parametrize("value", ["0.1", "0.1.3-rc1", "latest", "01.2.3"])
def test_version_key_rejects_nonstable_versions(value: str) -> None:
    with pytest.raises(upgrade_commands.UpgradeError):
        upgrade_commands._version_key(value)


def test_current_version_reads_raven_package_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    requested: list[str] = []

    def package_version(distribution_name: str) -> str:
        requested.append(distribution_name)
        return "0.1.3"

    monkeypatch.setattr(upgrade_commands.metadata, "version", package_version)

    assert upgrade_commands._current_version() == "0.1.3"
    assert requested == ["raven"]


def test_parse_release_payload_selects_exact_release_wheel() -> None:
    release = upgrade_commands._parse_release_payload(
        {
            "tag_name": "v0.1.4",
            "draft": False,
            "prerelease": False,
            "assets": [
                {
                    "name": "raven-0.1.4-py3-none-any.whl",
                    "browser_download_url": "https://github.com/EverMind-AI/Raven/releases/download/v0.1.4/raven-0.1.4-py3-none-any.whl",
                }
            ],
        }
    )
    assert release.version == "0.1.4"
    assert release.wheel_url == WHEEL_URL


@pytest.mark.parametrize("field", ["draft", "prerelease"])
def test_parse_release_payload_rejects_unstable_releases(field: str) -> None:
    with pytest.raises(upgrade_commands.UpgradeError):
        upgrade_commands._parse_release_payload(_release_payload(**{field: True}))


@pytest.mark.parametrize(
    ("field", "value"),
    [("draft", 0), ("prerelease", "false")],
)
def test_parse_release_payload_requires_boolean_release_flags(field: str, value: object) -> None:
    with pytest.raises(upgrade_commands.UpgradeError):
        upgrade_commands._parse_release_payload(_release_payload(**{field: value}))


@pytest.mark.parametrize(
    "payload",
    [
        None,
        {},
        _release_payload(tag_name=1),
        _release_payload(assets={}),
        _release_payload(assets=[None]),
    ],
)
def test_parse_release_payload_rejects_malformed_payloads(payload: object) -> None:
    with pytest.raises(upgrade_commands.UpgradeError):
        upgrade_commands._parse_release_payload(payload)


def test_parse_release_payload_rejects_wrong_wheel_filename() -> None:
    assets = [
        {
            "name": "raven-0.1.5-py3-none-any.whl",
            "browser_download_url": WHEEL_URL,
        }
    ]

    with pytest.raises(upgrade_commands.UpgradeError):
        upgrade_commands._parse_release_payload(_release_payload(assets=assets))


def test_parse_release_payload_rejects_duplicate_exact_wheels() -> None:
    asset = {"name": WHEEL_NAME, "browser_download_url": WHEEL_URL}

    with pytest.raises(upgrade_commands.UpgradeError):
        upgrade_commands._parse_release_payload(_release_payload(assets=[asset, asset.copy()]))


@pytest.mark.parametrize(
    "wheel_url",
    [
        WHEEL_URL.replace("https://", "http://"),
        WHEEL_URL.replace("github.com", "downloads.example.com"),
        WHEEL_URL.replace("/EverMind-AI/Raven/", "/EverMind-AI/Other/"),
    ],
    ids=["http-url", "wrong-host", "wrong-repository-path"],
)
def test_parse_release_payload_rejects_untrusted_wheel_urls(wheel_url: str) -> None:
    assets = [{"name": WHEEL_NAME, "browser_download_url": wheel_url}]

    with pytest.raises(upgrade_commands.UpgradeError):
        upgrade_commands._parse_release_payload(_release_payload(assets=assets))


def test_fetch_latest_release_uses_github_api_contract(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_commands, "_current_version", lambda: "0.1.3")

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == upgrade_commands.LATEST_RELEASE_API
        assert request.headers["Accept"] == "application/vnd.github+json"
        assert request.headers["User-Agent"] == "raven/0.1.3"
        assert request.headers["X-GitHub-Api-Version"] == "2022-11-28"
        return httpx.Response(200, json=_release_payload())

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        release = upgrade_commands._fetch_latest_release(client)

    assert release == upgrade_commands.ReleaseInfo(version="0.1.4", wheel_url=WHEEL_URL)


def test_fetch_latest_release_propagates_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.ReadTimeout):
            upgrade_commands._fetch_latest_release(client)


def test_fetch_latest_release_propagates_non_2xx_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"message": "unavailable"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(httpx.HTTPStatusError):
            upgrade_commands._fetch_latest_release(client)


def test_fetch_latest_release_propagates_invalid_json() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=b"{not-json",
            headers={"Content-Type": "application/json"},
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError):
            upgrade_commands._fetch_latest_release(client)
