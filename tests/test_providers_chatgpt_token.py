"""Unit tests for the one way in to the ChatGPT credential LiteLLM's driver owns.

The behaviour under test is a refusal: everything except ``provider login`` has to
be able to ask for a token without the driver starting a device flow, which prints
a code to stdout and polls for fifteen minutes. Three callers depend on it -- the
request path, the ``model.options`` RPC and ``provider test`` -- and all three run
where nobody is waiting to type a code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.providers import chatgpt_token


@pytest.fixture
def token_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(tmp_path / "chatgpt"))
    (tmp_path / "chatgpt").mkdir()
    return tmp_path / "chatgpt"


def _write(token_dir: Path, payload: dict) -> None:
    (token_dir / "auth.json").write_text(json.dumps(payload), encoding="utf-8")


def test_no_credential_at_all_says_to_log_in(token_dir: Path) -> None:
    with pytest.raises(RuntimeError, match="provider login openai-codex"):
        chatgpt_token.access_token_and_account()


def test_a_revoked_refresh_token_does_not_open_a_device_flow(
    token_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The file exists, so the missing-credential check passes -- and then the
    driver's own fallback is a device flow. Reaching it means a request the user
    is waiting on prints a code and polls instead of failing."""
    from litellm.llms.chatgpt.authenticator import Authenticator, RefreshAccessTokenError

    _write(token_dir, {"refresh_token": "revoked"})
    monkeypatch.setattr(
        Authenticator,
        "_refresh_tokens",
        lambda self, token: (_ for _ in ()).throw(RefreshAccessTokenError(message="revoked", status_code=401)),
    )
    monkeypatch.setattr(
        Authenticator,
        "_request_device_code",
        lambda self: pytest.fail("a device flow was started underneath a request"),
    )

    with pytest.raises(RuntimeError, match="could not renew"):
        chatgpt_token.access_token_and_account()


def test_a_pending_device_code_is_not_waited_on(
    token_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A recent ``provider login`` leaves a cooldown behind, and the driver's
    answer to one is to block until it expires -- five minutes of a picker that
    has handed over the terminal and printed nothing."""
    import time

    from litellm.llms.chatgpt.authenticator import Authenticator, RefreshAccessTokenError

    _write(token_dir, {"refresh_token": "revoked", "device_code_requested_at": time.time()})
    monkeypatch.setattr(
        Authenticator,
        "_refresh_tokens",
        lambda self, token: (_ for _ in ()).throw(RefreshAccessTokenError(message="revoked", status_code=401)),
    )
    monkeypatch.setattr(
        Authenticator,
        "_wait_for_access_token",
        lambda self, remaining: pytest.fail("waited on a pending device code"),
    )

    with pytest.raises(RuntimeError, match="could not renew"):
        chatgpt_token.access_token_and_account()


def test_a_network_failure_is_not_reported_as_a_revocation(
    token_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The driver wraps every refresh failure in one error type, an unreachable
    network included, so this side cannot tell them apart -- and must not claim to.
    Telling a user offline that their credential is gone sends them to re-authorise
    something that never expired."""
    import httpx

    _write(token_dir, {"refresh_token": "fine"})

    # Stubbed at the transport, not at ``_refresh_tokens``: the wrapping under test
    # is LiteLLM's own, and replacing the method that does it would prove nothing.
    class _Offline:
        def post(self, *_args, **_kwargs):
            raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(
        "litellm.llms.chatgpt.authenticator._get_httpx_client",
        lambda *_a, **_k: _Offline(),
    )

    with pytest.raises(RuntimeError) as excinfo:
        chatgpt_token.access_token_and_account()

    assert "network" in str(excinfo.value), "offline reported as a dead credential"


def test_a_live_credential_comes_back_with_its_account(
    token_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from litellm.llms.chatgpt.authenticator import Authenticator

    _write(token_dir, {"access_token": "live", "account_id": "acct-1"})
    monkeypatch.setattr(Authenticator, "_is_token_expired", lambda self, data, token: False)

    assert chatgpt_token.access_token_and_account() == ("live", "acct-1")


def test_the_refusal_is_wired_to_the_methods_the_driver_actually_falls_back_to() -> None:
    """The refusal replaces two private methods. Renamed upstream, the assignment
    would create new attributes and the fallback would be live again with every
    test above still green.
    """
    from litellm.llms.chatgpt.authenticator import Authenticator

    for name in ("_login_device_code", "_wait_for_access_token"):
        assert callable(getattr(Authenticator, name, None)), (
            f"LiteLLM no longer has Authenticator.{name}: re-read get_access_token and "
            "re-establish how a token is fetched without starting a login"
        )


def test_a_refresh_token_alone_counts_as_signed_in(token_dir: Path) -> None:
    """The driver exchanges it for an access token, so an expired access token is
    not the same as being signed out."""
    _write(token_dir, {"refresh_token": "r"})

    assert chatgpt_token.stored_credentials() == {"refresh_token": "r"}


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("not json at all", id="unparseable"),
        pytest.param(json.dumps([1, 2]), id="not-a-mapping"),
        pytest.param(json.dumps({"account_id": "acct"}), id="no-token-of-either-kind"),
    ],
)
def test_a_file_without_a_usable_token_is_not_a_credential(token_dir: Path, payload: str) -> None:
    (token_dir / "auth.json").write_text(payload, encoding="utf-8")

    assert chatgpt_token.stored_credentials() is None


def test_an_abandoned_device_code_is_forgotten(token_dir: Path) -> None:
    _write(token_dir, {"device_code_requested_at": 1.0})

    assert chatgpt_token.clear_abandoned_device_code() is True
    assert not (token_dir / "auth.json").exists(), "a file holding nothing else was left behind"


def test_clearing_one_never_takes_a_credential_with_it(token_dir: Path) -> None:
    """The driver stamps the file it already keeps the tokens in, so the stamp and
    a live credential coexist. Dropping the stamp must leave the rest intact --
    this rewrites the only copy of the credential there is."""
    _write(token_dir, {"access_token": "live", "refresh_token": "r", "device_code_requested_at": 1.0})

    assert chatgpt_token.clear_abandoned_device_code() is True
    assert chatgpt_token.stored_credentials() == {"access_token": "live", "refresh_token": "r"}


def test_clearing_one_leaves_the_credential_owner_only(token_dir: Path) -> None:
    """This rewrite replaces the file rather than truncating it, so the mode the
    credential had is on the inode being replaced -- the new one gets whatever the
    umask says unless it is set here."""
    import stat

    auth = token_dir / "auth.json"
    _write(token_dir, {"access_token": "live", "device_code_requested_at": 1.0})
    auth.chmod(0o600)

    assert chatgpt_token.clear_abandoned_device_code() is True
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600, "the rewrite widened the credential"


def test_nothing_to_clear_leaves_the_file_alone(token_dir: Path) -> None:
    _write(token_dir, {"access_token": "live"})
    before = (token_dir / "auth.json").read_bytes()

    assert chatgpt_token.clear_abandoned_device_code() is False
    assert (token_dir / "auth.json").read_bytes() == before


def test_no_file_at_all_is_not_an_error(token_dir: Path) -> None:
    assert chatgpt_token.clear_abandoned_device_code() is False


def test_a_stamp_alone_is_not_a_credential(token_dir: Path) -> None:
    """An interrupted sign-in leaves this behind, and reporting it as a credential
    is what makes a provider look connected with no way to authenticate."""
    _write(token_dir, {"device_code_requested_at": 1.0})

    assert chatgpt_token.stored_credentials() is None
