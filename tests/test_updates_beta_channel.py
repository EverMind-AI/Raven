"""The beta channel -- joining it, ordering it, and what a pointer may say.

Two properties carry the weight here. A build published to the channel names a
version and nothing else that reaches the network: the wheel URL is derived
from the configured project, so a rewritten pointer cannot redirect an install
to another host. And beta versions have to order against each other AND against
the stable line, or a tester either never gets offered the next build or gets
offered one they already run.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest

from raven.updates import beta_channel, update_notice
from raven.updates.upgrade import UpgradeError

_PROJECT = "85454048"
_TOKEN = "gldt-secret"


@pytest.fixture
def joined(tmp_path: Path, monkeypatch) -> beta_channel.BetaChannel:
    """An install that has joined the channel."""
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
    payload = {"project": _PROJECT, "username": "raven-beta", "token": _TOKEN}
    (tmp_path / "beta.json").write_text(json.dumps(payload), encoding="utf-8")
    chan = beta_channel.channel()
    assert chan is not None
    return chan


def _pointing_at(version: str) -> httpx.Client:
    """A client whose registry answers the pointer with ``version``."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"version": version})

    return httpx.Client(transport=httpx.MockTransport(handler))


class TestTheOrderingSpansBothChannels:
    """A beta sits above the release it builds on and below its own successor."""

    def test_serials_order_among_themselves(self):
        assert beta_channel.release_key("0.1.12b1") < beta_channel.release_key("0.1.12b2")
        assert beta_channel.release_key("0.1.12b9") < beta_channel.release_key("0.1.12b10")

    def test_a_beta_is_below_the_release_it_builds_on(self):
        assert beta_channel.release_key("0.1.12b7") < beta_channel.release_key("0.1.12")

    def test_a_beta_is_above_the_previous_release(self):
        # The publisher names betas after the *next* patch for exactly this
        # reason: a tester on stable 0.1.11 has to be offered 0.1.12b1.
        assert beta_channel.release_key("0.1.11") < beta_channel.release_key("0.1.12b1")

    def test_a_plain_release_still_reads(self):
        assert beta_channel.release_key("v2.0.0") == beta_channel.release_key("2.0.0")

    @pytest.mark.parametrize("value", ["", "0.1", "0.1.12b", "0.1.12b0", "0.1.12rc1", "0.1.12-beta", "nightly"])
    def test_anything_else_is_refused(self, value: str):
        with pytest.raises(UpgradeError):
            beta_channel.release_key(value)


class TestJoiningTheChannel:
    def test_no_file_means_stable(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
        assert beta_channel.channel() is None
        assert beta_channel.is_active() is False

    def test_a_complete_file_joins(self, joined: beta_channel.BetaChannel):
        assert joined.project == _PROJECT
        assert joined.token == _TOKEN
        assert joined.api_base == f"https://gitlab.com/api/v4/projects/{_PROJECT}/packages/generic/raven"

    @pytest.mark.parametrize(
        "payload",
        [
            '{"project": "1", "username": "u"}',
            '{"project": "1", "username": "u", "token": ""}',
            '{"project": "not-a-number", "username": "u", "token": "t"}',
            '{"project": 1, "username": "u", "token": "t"}',
            "[]",
            "not json at all",
        ],
        ids=["missing-token", "empty-token", "project-not-numeric", "project-not-string", "not-an-object", "not-json"],
    )
    def test_a_file_that_does_not_read_leaves_the_install_stable(self, tmp_path: Path, monkeypatch, payload: str):
        # Silence rather than an exception: a bad edit should cost the update
        # notice, not the launch that reads it.
        monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
        (tmp_path / "beta.json").write_text(payload, encoding="utf-8")
        assert beta_channel.channel() is None


class TestThePointerCannotRedirectTheInstall:
    def test_the_wheel_url_is_built_from_the_configured_project(self, joined: beta_channel.BetaChannel):
        with _pointing_at("0.1.12b3") as client:
            release = beta_channel.fetch_latest(joined, client=client)
        assert release.version == "0.1.12b3"
        assert release.wheel_url == (
            f"https://raven-beta:{_TOKEN}@gitlab.com/api/v4/projects/{_PROJECT}"
            "/packages/generic/raven/0.1.12b3/raven-0.1.12b3-py3-none-any.whl"
        )

    def test_a_url_in_the_payload_is_ignored(self, joined: beta_channel.BetaChannel):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"version": "0.1.12b1", "wheel": "https://evil.example/x.whl"})

        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            release = beta_channel.fetch_latest(joined, client=client)
        assert "evil.example" not in release.wheel_url
        assert release.wheel_url.endswith("/raven-0.1.12b1-py3-none-any.whl")

    @pytest.mark.parametrize("version", ["../../../etc/passwd", "0.1.12b1 ; rm -rf /", "", "latest"])
    def test_a_version_that_is_not_a_version_is_refused(self, joined: beta_channel.BetaChannel, version: str):
        with _pointing_at(version) as client, pytest.raises(UpgradeError):
            beta_channel.fetch_latest(joined, client=client)

    def test_a_payload_that_is_not_an_object_is_refused(self, joined: beta_channel.BetaChannel):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json=["0.1.12b1"])

        with httpx.Client(transport=httpx.MockTransport(handler)) as client, pytest.raises(UpgradeError):
            beta_channel.fetch_latest(joined, client=client)

    def test_a_stable_install_has_nothing_to_fetch(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
        with pytest.raises(UpgradeError, match="not on the beta channel"):
            beta_channel.fetch_latest()

    def test_the_credential_is_escaped_into_the_url(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
        payload = {"project": _PROJECT, "username": "a/b", "token": "p@ss:word/1"}
        (tmp_path / "beta.json").write_text(json.dumps(payload), encoding="utf-8")
        chan = beta_channel.channel()
        assert chan is not None
        with _pointing_at("0.1.12b1") as client:
            release = beta_channel.fetch_latest(chan, client=client)
        # Unescaped, the `/` and `:` would end the credential early and point
        # the installer somewhere else entirely.
        assert release.wheel_url.startswith("https://a%2Fb:p%40ss%3Aword%2F1@gitlab.com/")


class TestTheNudgeFollowsTheChannel:
    """``update_notice`` compares versions; which grammar it uses is the channel's call."""

    def test_a_stable_install_still_folds_a_prerelease_into_its_release(self, tmp_path: Path, monkeypatch):
        monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
        assert update_notice._version_key("0.2.0rc1") == update_notice._version_key("0.2.0")

    def test_the_beta_channel_keeps_the_two_apart(self, joined: beta_channel.BetaChannel):
        # The stable reduction maps both of these to 0.1.12, which would leave
        # a tester on b1 never offered b2 -- the whole point of the channel.
        first = update_notice._version_key("0.1.12b1")
        second = update_notice._version_key("0.1.12b2")
        assert first is not None and second is not None
        assert first < second

    def test_an_unreadable_version_shows_no_notice(self, joined: beta_channel.BetaChannel):
        assert update_notice._version_key("who-knows") is None


class TestTheInstallerTemplateStaysCredentialFree:
    def test_the_repository_copy_carries_placeholders(self):
        # The publisher substitutes these; git must never hold the real ones.
        text = (Path(__file__).resolve().parent.parent / "beta.sh").read_text(encoding="utf-8")
        assert "__RAVEN_BETA_PROJECT__" in text
        assert "__RAVEN_BETA_TOKEN__" in text

    def test_the_template_refuses_to_run_as_itself(self):
        # Piping the git copy into sh would otherwise write a beta.json whose
        # token is the literal placeholder, and the failure would surface much
        # later as an unexplained 401 on the next update check.
        import subprocess

        script = Path(__file__).resolve().parent.parent / "beta.sh"
        result = subprocess.run(
            ["sh", str(script)],
            capture_output=True,
            text=True,
            env={**os.environ, "RAVEN_HOME": "/nonexistent-should-not-be-written"},
        )
        assert result.returncode != 0
        assert "template copy" in result.stderr


class TestPublishingLeavesTheCheckoutAsItFoundIt:
    """The version reaches the wheel by stamping pyproject, which is a mutation
    of a tracked file. Both files it touches have to come back."""

    def test_it_restores_the_lockfile_as_well_as_pyproject(self, tmp_path: Path, monkeypatch):
        # uv.lock records the project's own version, so `uv export` rewrites it
        # to match the stamp. Left behind, it rides into the next commit and
        # fails the release, where `uv export --locked` compares the two.
        import sys

        sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
        import publish_beta

        (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.1.11"\n', encoding="utf-8")
        (tmp_path / "uv.lock").write_text('name = "raven"\nversion = "0.1.11"\n', encoding="utf-8")
        (tmp_path / "ui-tui" / "dist").mkdir(parents=True)
        (tmp_path / "ui-tui" / "dist" / "entry.js").write_text("x", encoding="utf-8")
        (tmp_path / "ui" / "dist").mkdir(parents=True)
        (tmp_path / "ui" / "dist" / "index.html").write_text("x", encoding="utf-8")

        def _fake_run(argv, *, cwd):
            # Stand in for `uv export`, which is what rewrites the lockfile.
            if argv[:2] == ["uv", "export"]:
                (tmp_path / "uv.lock").write_text('name = "raven"\nversion = "0.1.12b1"\n', encoding="utf-8")

        monkeypatch.setattr(publish_beta, "_run", _fake_run)

        with pytest.raises(publish_beta.PublishError):
            # The wheel never appears, so the build raises -- and the restore
            # still has to have happened, which is the half being asserted.
            publish_beta._build(tmp_path, "0.1.12b1")

        assert 'version = "0.1.11"' in (tmp_path / "pyproject.toml").read_text(encoding="utf-8")
        assert 'version = "0.1.11"' in (tmp_path / "uv.lock").read_text(encoding="utf-8")


class TestWatchingTheChannelIsNearlyFree:
    """A one-minute poll is only affordable because an unchanged pointer answers
    304 with no body: 23 bytes when something moved, a bare validator check when
    nothing did. These pin both halves -- the validator goes back out, and a 304
    is read as "keep what you already had" rather than parsed."""

    @staticmethod
    def _registry(status: int, *, etag: str | None, version: str = "0.1.12b6") -> tuple[httpx.Client, list[str | None]]:
        """A registry that records the validator each request offered."""
        offered: list[str | None] = []

        def handler(request: httpx.Request) -> httpx.Response:
            offered.append(request.headers.get("If-None-Match"))
            headers = {"ETag": etag} if etag is not None else {}
            if status == 304:
                return httpx.Response(304, headers=headers)
            return httpx.Response(status, json={"version": version}, headers=headers)

        return httpx.Client(transport=httpx.MockTransport(handler)), offered

    def test_a_first_read_offers_no_validator_and_brings_one_back(self, joined: beta_channel.BetaChannel):
        client, offered = self._registry(200, etag='"c9b42d2d"')
        with client:
            read = beta_channel.read_pointer(joined, client=client)

        assert offered == [None]
        assert read.unchanged is False
        assert read.release is not None
        assert read.release.version == "0.1.12b6"
        assert read.etag == '"c9b42d2d"', "without the validator the next poll cannot be conditional"

    def test_a_known_validator_is_offered_back(self, joined: beta_channel.BetaChannel):
        client, offered = self._registry(200, etag='"new"')
        with client:
            beta_channel.read_pointer(joined, client=client, etag='"old"')

        assert offered == ['"old"']

    def test_an_unchanged_pointer_is_not_parsed(self, joined: beta_channel.BetaChannel):
        """A 304 has no body. Reading one as a pointer is the bug this rules out."""
        client, offered = self._registry(304, etag='"same"')
        with client:
            read = beta_channel.read_pointer(joined, client=client, etag='"same"')

        assert offered == ['"same"']
        assert read.unchanged is True
        assert read.release is None
        assert read.etag == '"same"'

    def test_a_304_that_repeats_no_validator_keeps_the_one_we_sent(self, joined: beta_channel.BetaChannel):
        """Echoing the ETag on a 304 is conventional, not guaranteed. Dropping it
        would make the next poll unconditional and pay for a body every minute."""
        client, offered = self._registry(304, etag=None)
        with client:
            read = beta_channel.read_pointer(joined, client=client, etag='"same"')

        assert read.unchanged is True
        assert read.etag == '"same"'

    def test_the_upgrade_path_never_asks_conditionally(self, joined: beta_channel.BetaChannel):
        """`raven upgrade` has to name the version it will install, so the read
        behind it may not be answerable with "unchanged"."""
        client, offered = self._registry(200, etag='"c9b42d2d"')
        with client:
            release = beta_channel.fetch_latest(joined, client=client)

        assert offered == [None]
        assert release.version == "0.1.12b6"

    def test_an_unasked_for_304_is_refused_rather_than_returned_empty(self, joined: beta_channel.BetaChannel):
        """It cannot happen against a request that sent no validator, so if it
        does, `fetch_latest` must raise rather than hand back a release-less read
        that its callers would dereference."""
        client, _offered = self._registry(304, etag=None)
        with client, pytest.raises(UpgradeError):
            beta_channel.fetch_latest(joined, client=client)
