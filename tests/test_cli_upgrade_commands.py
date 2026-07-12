from __future__ import annotations

import json
import tomllib
from dataclasses import FrozenInstanceError
from unittest.mock import Mock

import httpx
import pytest
from typer.testing import CliRunner

from raven.cli import upgrade_commands
from raven.cli.commands import app

WHEEL_NAME = "raven-0.1.4-py3-none-any.whl"
WHEEL_URL = "https://github.com/EverMind-AI/Raven/releases/download/v0.1.4/raven-0.1.4-py3-none-any.whl"
MALFORMED_DIRECT_URL_METADATA = [
    pytest.param("", id="empty-document"),
    pytest.param("[]", id="top-level-list"),
    pytest.param('{"dir_info": "editable"}', id="invalid-dir-info"),
    pytest.param('{"dir_info": {"editable": "true"}}', id="invalid-editable-flag"),
]
runner = CliRunner()


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


def _patch_available_release(monkeypatch: pytest.MonkeyPatch) -> upgrade_commands.ReleaseInfo:
    release = upgrade_commands.ReleaseInfo("0.1.4", WHEEL_URL)
    monkeypatch.setattr(upgrade_commands, "_current_version", lambda: "0.1.3")
    monkeypatch.setattr(upgrade_commands, "_fetch_latest_release", lambda: release)
    return release


def test_is_editable_install_reads_pep_610_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    distribution = Mock()
    distribution.read_text.return_value = '{"dir_info": {"editable": true}}'
    distribution_lookup = Mock(return_value=distribution)
    monkeypatch.setattr(upgrade_commands.metadata, "distribution", distribution_lookup)

    assert upgrade_commands._is_editable_install() is True
    distribution_lookup.assert_called_once_with("raven")
    distribution.read_text.assert_called_once_with("direct_url.json")


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "{}",
        '{"dir_info": {}}',
        '{"dir_info": {"editable": false}}',
    ],
)
def test_is_editable_install_returns_false_for_missing_or_noneditable_metadata(
    monkeypatch: pytest.MonkeyPatch,
    raw: str | None,
) -> None:
    distribution = Mock()
    distribution.read_text.return_value = raw
    monkeypatch.setattr(upgrade_commands.metadata, "distribution", lambda name: distribution)

    assert upgrade_commands._is_editable_install() is False


@pytest.mark.parametrize("raw", MALFORMED_DIRECT_URL_METADATA)
def test_is_editable_install_rejects_malformed_metadata(
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
) -> None:
    distribution = Mock()
    distribution.read_text.return_value = raw
    monkeypatch.setattr(upgrade_commands.metadata, "distribution", lambda name: distribution)

    with pytest.raises(upgrade_commands.UpgradeError, match="Malformed Raven installation metadata"):
        upgrade_commands._is_editable_install()


def test_is_uv_tool_install_reads_raven_receipt(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    (tmp_path / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "raven" }]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(upgrade_commands.sys, "prefix", str(tmp_path))

    assert upgrade_commands._is_uv_tool_install() is True


def test_is_uv_tool_install_rejects_missing_receipt(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.setattr(upgrade_commands.sys, "prefix", str(tmp_path))

    assert upgrade_commands._is_uv_tool_install() is False


def test_is_uv_tool_install_rejects_unrelated_receipt(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    (tmp_path / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "other" }]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(upgrade_commands.sys, "prefix", str(tmp_path))

    assert upgrade_commands._is_uv_tool_install() is False


def test_run_uv_uses_force_install_with_inherited_output(monkeypatch: pytest.MonkeyPatch) -> None:
    completed = Mock(returncode=17)
    run = Mock(return_value=completed)
    monkeypatch.setattr(upgrade_commands.subprocess, "run", run)

    assert upgrade_commands._run_uv("/usr/bin/uv", WHEEL_URL) == 17
    run.assert_called_once_with(
        ["/usr/bin/uv", "tool", "install", "--force", WHEEL_URL],
        check=False,
    )


def test_install_release_requires_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_commands.shutil, "which", lambda executable: None)

    with pytest.raises(upgrade_commands.UpgradeError, match="uv was not found on PATH"):
        upgrade_commands._install_release(upgrade_commands.ReleaseInfo("0.1.4", WHEEL_URL))


def test_install_release_stops_after_channel_install_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(upgrade_commands.shutil, "which", lambda executable: "/usr/bin/uv")

    def run_uv(uv_path: str, requirement: str) -> int:
        calls.append((uv_path, requirement))
        return 0

    monkeypatch.setattr(upgrade_commands, "_run_uv", run_uv)

    upgrade_commands._install_release(upgrade_commands.ReleaseInfo("0.1.4", WHEEL_URL))

    assert calls == [("/usr/bin/uv", f"raven[channels] @ {WHEEL_URL}")]


def test_install_release_falls_back_to_base_wheel(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    return_codes = iter([1, 0])
    monkeypatch.setattr(upgrade_commands.shutil, "which", lambda executable: "/usr/bin/uv")

    def run_uv(uv_path: str, requirement: str) -> int:
        calls.append((uv_path, requirement))
        return next(return_codes)

    monkeypatch.setattr(upgrade_commands, "_run_uv", run_uv)

    upgrade_commands._install_release(upgrade_commands.ReleaseInfo("0.1.4", WHEEL_URL))

    assert calls == [
        ("/usr/bin/uv", f"raven[channels] @ {WHEEL_URL}"),
        ("/usr/bin/uv", WHEEL_URL),
    ]


def test_install_release_reports_both_attempts_failing(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(upgrade_commands.shutil, "which", lambda executable: "/usr/bin/uv")

    def run_uv(uv_path: str, requirement: str) -> int:
        calls.append((uv_path, requirement))
        return 1

    monkeypatch.setattr(upgrade_commands, "_run_uv", run_uv)

    with pytest.raises(upgrade_commands.UpgradeError, match="uv could not install"):
        upgrade_commands._install_release(upgrade_commands.ReleaseInfo("0.1.4", WHEEL_URL))

    assert calls == [
        ("/usr/bin/uv", f"raven[channels] @ {WHEEL_URL}"),
        ("/usr/bin/uv", WHEEL_URL),
    ]


def test_upgrade_check_reports_available_without_install(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_available_release(monkeypatch)
    install = Mock()
    monkeypatch.setattr(upgrade_commands, "_install_release", install)

    result = runner.invoke(app, ["upgrade", "--check"])

    assert result.exit_code == 0
    assert "0.1.3 -> 0.1.4" in result.stdout
    assert "raven upgrade" in result.stdout
    install.assert_not_called()


def test_upgrade_reports_current_release_as_up_to_date(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_commands, "_current_version", lambda: "0.1.4")
    monkeypatch.setattr(
        upgrade_commands,
        "_fetch_latest_release",
        lambda: upgrade_commands.ReleaseInfo("0.1.4", WHEEL_URL),
    )
    install = Mock()
    monkeypatch.setattr(upgrade_commands, "_install_release", install)

    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code == 0
    assert "up to date" in result.stdout
    install.assert_not_called()


def test_upgrade_does_not_downgrade_newer_local_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(upgrade_commands, "_current_version", lambda: "0.1.5")
    monkeypatch.setattr(
        upgrade_commands,
        "_fetch_latest_release",
        lambda: upgrade_commands.ReleaseInfo("0.1.4", WHEEL_URL),
    )
    install = Mock()
    monkeypatch.setattr(upgrade_commands, "_install_release", install)

    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code == 0
    assert "newer than the latest release" in result.stdout
    assert "0.1.5" in result.stdout
    assert "0.1.4" in result.stdout
    install.assert_not_called()


def test_upgrade_refuses_editable_install(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_available_release(monkeypatch)
    monkeypatch.setattr(upgrade_commands, "_is_editable_install", lambda: True)
    uv_install = Mock(return_value=True)
    monkeypatch.setattr(upgrade_commands, "_is_uv_tool_install", uv_install)
    install = Mock()
    monkeypatch.setattr(upgrade_commands, "_install_release", install)

    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code == 1
    assert "editable" in result.stdout.lower()
    assert "source checkout" in result.stdout.lower()
    uv_install.assert_not_called()
    install.assert_not_called()


def test_upgrade_refuses_unsupported_install(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_available_release(monkeypatch)
    monkeypatch.setattr(upgrade_commands, "_is_editable_install", lambda: False)
    monkeypatch.setattr(upgrade_commands, "_is_uv_tool_install", lambda: False)
    install = Mock()
    monkeypatch.setattr(upgrade_commands, "_install_release", install)

    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code == 1
    assert "not managed by uv" in result.stdout.lower()
    assert "official installer" in result.stdout.lower()
    install.assert_not_called()


@pytest.mark.parametrize("raw", MALFORMED_DIRECT_URL_METADATA)
def test_upgrade_rejects_malformed_direct_url_before_install(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
    raw: str,
) -> None:
    _patch_available_release(monkeypatch)
    distribution = Mock()
    distribution.read_text.return_value = raw
    monkeypatch.setattr(upgrade_commands.metadata, "distribution", lambda name: distribution)
    (tmp_path / "uv-receipt.toml").write_text(
        '[tool]\nrequirements = [{ name = "raven" }]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(upgrade_commands.sys, "prefix", str(tmp_path))
    install = Mock()
    monkeypatch.setattr(upgrade_commands, "_install_release", install)

    assert upgrade_commands._is_uv_tool_install() is True

    result = runner.invoke(app, ["upgrade"])

    output = " ".join(result.stdout.lower().split())
    assert result.exit_code == 1
    assert "malformed raven installation metadata" in output
    assert "official installer" in output
    install.assert_not_called()


def test_upgrade_installs_release_and_requests_restart(monkeypatch: pytest.MonkeyPatch) -> None:
    release = _patch_available_release(monkeypatch)
    monkeypatch.setattr(upgrade_commands, "_is_editable_install", lambda: False)
    monkeypatch.setattr(upgrade_commands, "_is_uv_tool_install", lambda: True)
    install = Mock()
    monkeypatch.setattr(upgrade_commands, "_install_release", install)

    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code == 0
    assert "0.1.3 -> 0.1.4" in result.stdout
    assert "restart" in result.stdout.lower()
    install.assert_called_once_with(release)


def test_upgrade_reports_missing_uv(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_available_release(monkeypatch)
    monkeypatch.setattr(upgrade_commands, "_is_editable_install", lambda: False)
    monkeypatch.setattr(upgrade_commands, "_is_uv_tool_install", lambda: True)

    def install(release: upgrade_commands.ReleaseInfo) -> None:
        raise upgrade_commands.UpgradeError("uv was not found on PATH")

    monkeypatch.setattr(upgrade_commands, "_install_release", install)

    result = runner.invoke(app, ["upgrade"])

    assert result.exit_code == 1
    assert "uv was not found on PATH" in result.stdout
    assert "Traceback" not in result.stdout


@pytest.mark.parametrize(
    "error",
    [
        upgrade_commands.UpgradeError("release unavailable"),
        httpx.ReadTimeout("timed out"),
        json.JSONDecodeError("malformed release JSON", "{", 0),
        ValueError("malformed release JSON"),
        upgrade_commands.metadata.PackageNotFoundError("raven"),
    ],
    ids=["release", "network", "json", "value-error", "package-metadata"],
)
def test_upgrade_reports_release_errors(
    monkeypatch: pytest.MonkeyPatch,
    error: Exception,
) -> None:
    monkeypatch.setattr(upgrade_commands, "_current_version", lambda: "0.1.3")

    def fetch() -> upgrade_commands.ReleaseInfo:
        raise error

    monkeypatch.setattr(upgrade_commands, "_fetch_latest_release", fetch)

    result = runner.invoke(app, ["upgrade"])

    output = " ".join(result.stdout.lower().split())
    assert result.exit_code == 1
    assert "Unable to upgrade Raven" in result.stdout
    assert "try again" in output
    assert "official installer" in output
    assert "Traceback" not in result.stdout


@pytest.mark.parametrize("guard", ["editable", "receipt"])
def test_upgrade_reports_malformed_installation_metadata(
    monkeypatch: pytest.MonkeyPatch,
    guard: str,
) -> None:
    _patch_available_release(monkeypatch)
    if guard == "editable":

        def malformed_editable_metadata() -> bool:
            json.loads("{")
            return False

        monkeypatch.setattr(upgrade_commands, "_is_editable_install", malformed_editable_metadata)
    else:
        monkeypatch.setattr(upgrade_commands, "_is_editable_install", lambda: False)

        def malformed_receipt() -> bool:
            tomllib.loads("[tool")
            return False

        monkeypatch.setattr(upgrade_commands, "_is_uv_tool_install", malformed_receipt)

    result = runner.invoke(app, ["upgrade"])

    output = " ".join(result.stdout.lower().split())
    assert result.exit_code == 1
    assert "Unable to upgrade Raven" in result.stdout
    assert "try again" in output
    assert "official installer" in output
    assert "Traceback" not in result.stdout
