"""The console-free device sign-in a page starts: the pair comes back at once,
the poll goes on in the background, and a second start answers with the first
one's code."""

from __future__ import annotations

import asyncio
import json
import stat
import threading
import time
from pathlib import Path

import httpx
import pytest

from raven.providers import oauth_login
from raven.providers.minimax_oauth import CLIENT_ID, load_token


@pytest.fixture(autouse=True)
def _isolated_starters(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(oauth_login, "_STARTERS", dict(oauth_login._STARTERS))
    monkeypatch.setattr(oauth_login, "_PENDING", {})
    monkeypatch.setattr(oauth_login, "_HANDOFFS", {})


async def test_start_returns_the_pair_and_keeps_polling_until_the_starter_ends() -> None:
    finished = threading.Event()
    released = threading.Event()

    def fake(resolve: oauth_login.Resolve) -> None:
        resolve("https://vendor.test/device", "ABCD-1234", 600)
        released.wait(5)
        finished.set()

    oauth_login._STARTERS["fake_vendor"] = fake
    reply = await oauth_login.start("fake_vendor")
    assert reply == {"verification_uri": "https://vendor.test/device", "user_code": "ABCD-1234", "expires_in": 600}
    assert "fake_vendor" in oauth_login.pending()
    released.set()
    await asyncio.wait_for(oauth_login._PENDING["fake_vendor"], 5)
    assert finished.is_set()
    assert oauth_login.pending() == {}


async def test_a_second_start_answers_with_the_code_the_first_is_polling_for() -> None:
    """A second click is a reader who lost the vendor's tab, not a request for a
    second code. Refusing it left the page showing a code it had stopped
    watching, and minting another would hand out one the first poller never
    sees."""
    runs = 0
    released = threading.Event()

    def fake(resolve: oauth_login.Resolve) -> None:
        nonlocal runs
        runs += 1
        resolve("https://vendor.test/device", "ABCD-1234", 600)
        released.wait(5)

    oauth_login._STARTERS["fake_vendor"] = fake
    first = await oauth_login.start("fake_vendor")
    again = await oauth_login.start("fake_vendor")
    task = oauth_login.pending()["fake_vendor"]
    released.set()
    await asyncio.wait_for(task, 5)

    assert (again["verification_uri"], again["user_code"]) == (first["verification_uri"], first["user_code"])
    assert 0 < again["expires_in"] <= first["expires_in"]
    assert runs == 1


async def test_a_code_that_has_expired_is_not_handed_out_again() -> None:
    """The driver's poll outlives its code by up to one poll interval, so a click
    in that gap still finds the attempt running. That code is dead; the click
    gets a fresh one, and the stale attempt ending does not evict it."""
    codes = iter(["OLD-1", "NEW-2"])
    gates = {"OLD-1": threading.Event(), "NEW-2": threading.Event()}

    def fake(resolve: oauth_login.Resolve) -> None:
        code = next(codes)
        resolve("https://vendor.test/device", code, 0 if code == "OLD-1" else 600)
        gates[code].wait(5)

    oauth_login._STARTERS["fake_vendor"] = fake
    first = await oauth_login.start("fake_vendor")
    stale = oauth_login.pending()["fake_vendor"]
    again = await oauth_login.start("fake_vendor")
    live = oauth_login.pending()["fake_vendor"]

    assert (first["user_code"], again["user_code"]) == ("OLD-1", "NEW-2")
    gates["OLD-1"].set()
    await asyncio.wait_for(stale, 5)
    await asyncio.sleep(0)
    assert oauth_login.pending().get("fake_vendor") is live
    gates["NEW-2"].set()
    await asyncio.wait_for(live, 5)


async def test_a_credential_that_cannot_be_restricted_is_logged_not_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pair is on the page before the driver finishes, so a restriction that
    fails there has no caller left to raise to: raised, it ended a task nobody
    awaits."""
    from raven.config import paths

    def refuse(*_paths: Path) -> None:
        raise PermissionError("not the owner")

    monkeypatch.setattr(paths, "restrict_to_owner", refuse)
    gate = threading.Event()

    def fake(resolve: oauth_login.Resolve) -> None:
        resolve("https://vendor.test/device", "OK-1", 600)
        gate.wait(5)

    oauth_login._STARTERS["fake_vendor"] = fake
    await oauth_login.start("fake_vendor")
    task = oauth_login.pending()["fake_vendor"]
    gate.set()
    await asyncio.gather(task, return_exceptions=True)

    assert task.exception() is None


async def test_starts_that_race_the_vendor_share_one_code() -> None:
    """Both clicks land before the vendor has answered the first: the second
    waits for that answer rather than starting a flow of its own."""
    runs = 0
    answer = threading.Event()
    released = threading.Event()

    def fake(resolve: oauth_login.Resolve) -> None:
        nonlocal runs
        runs += 1
        answer.wait(5)
        resolve("https://vendor.test/device", "SLOW-1", 600)
        released.wait(5)

    oauth_login._STARTERS["fake_vendor"] = fake
    first = asyncio.create_task(oauth_login.start("fake_vendor"))
    second = asyncio.create_task(oauth_login.start("fake_vendor"))
    await asyncio.sleep(0.05)
    answer.set()
    replies = await asyncio.wait_for(asyncio.gather(first, second), 5)
    task = oauth_login.pending()["fake_vendor"]
    released.set()
    await asyncio.wait_for(task, 5)

    assert [r["user_code"] for r in replies] == ["SLOW-1", "SLOW-1"]
    assert runs == 1


async def test_a_cancelled_start_leaves_the_code_to_the_others() -> None:
    """Closing a tab cancels its request, since the WebSocket transport cancels
    every request still running on a connection it loses, and two tabs signing
    in to one vendor are two waits on one answer. The wait that is left still
    gets the code, and a later start is handed that code rather than a new one."""
    runs = 0
    answer = threading.Event()
    released = threading.Event()

    def fake(resolve: oauth_login.Resolve) -> None:
        nonlocal runs
        runs += 1
        answer.wait(5)
        resolve("https://vendor.test/device", f"SLOW-{runs}", 600)
        released.wait(5)

    oauth_login._STARTERS["fake_vendor"] = fake
    closed = asyncio.create_task(oauth_login.start("fake_vendor"))
    left = asyncio.create_task(oauth_login.start("fake_vendor"))
    await asyncio.sleep(0.05)
    closed.cancel()
    with pytest.raises(asyncio.CancelledError):
        await closed
    answer.set()
    await asyncio.wait({left}, timeout=5)
    later = await asyncio.wait_for(oauth_login.start("fake_vendor"), 5)
    task = oauth_login.pending()["fake_vendor"]
    released.set()
    await asyncio.wait_for(task, 5)

    assert left.done() and not left.cancelled()
    assert [left.result()["user_code"], later["user_code"]] == ["SLOW-1", "SLOW-1"]
    assert runs == 1


async def test_start_raises_what_the_vendor_raised_before_the_code_existed() -> None:
    def fake(_resolve: oauth_login.Resolve) -> None:
        raise ConnectionError("vendor down")

    oauth_login._STARTERS["fake_vendor"] = fake
    with pytest.raises(ConnectionError):
        await oauth_login.start("fake_vendor")
    await asyncio.gather(*oauth_login._PENDING.values(), return_exceptions=True)
    await asyncio.sleep(0)
    assert oauth_login.pending() == {}


async def test_start_refuses_a_provider_without_a_device_flow() -> None:
    with pytest.raises(LookupError):
        await oauth_login.start("deepseek")


def test_minimax_starter_hands_over_the_pair_then_saves_the_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MINIMAX_OAUTH_TOKEN_DIR", str(tmp_path))
    from raven.providers import minimax_oauth

    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        form = dict(httpx.QueryParams(request.content.decode()))
        if request.url.path.endswith("/device/code"):
            assert form["client_id"] == CLIENT_ID
            return httpx.Response(
                200,
                json={
                    "verification_uri": "https://platform.minimax.io/oauth-authorize",
                    "user_code": "ABCD",
                    "expired_in": int(time.time() * 1000) + 60_000,
                    "interval": 2_000,
                    "state": form["state"],
                },
            )
        if calls == 2:
            return httpx.Response(400, json={"error": "authorization_pending"})
        return httpx.Response(
            200,
            json={
                "status": "success",
                "access_token": "access",
                "refresh_token": "refresh",
                "expired_in": int(time.time() * 1000) + 3_600_000,
                "resource_url": "https://api.minimax.io/anthropic/v1",
            },
        )

    handed: list[tuple[str, str, int]] = []
    real_login = minimax_oauth.login

    def login_with_fake_vendor(region: str, **kw):
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            return real_login(region, client=client, sleep_fn=lambda _s: None, **kw)

    monkeypatch.setattr(minimax_oauth, "login", login_with_fake_vendor)
    oauth_login._minimax("global")(lambda uri, code, ttl: handed.append((uri, code, ttl)))
    assert handed[0][:2] == ("https://platform.minimax.io/oauth-authorize", "ABCD")
    assert 0 < handed[0][2] <= 60
    assert load_token("global") is not None


def test_supports_names_only_the_providers_with_a_device_flow() -> None:
    assert oauth_login.supports("minimax_global") and oauth_login.supports("github_copilot")
    assert not oauth_login.supports("deepseek")


async def test_a_starter_that_fails_after_the_pair_only_logs_it() -> None:
    """The page already has its code; the vendor's later failure is the
    poller's business, not the caller's."""

    def fake(resolve):
        resolve("https://v.example/device", "LATE", 5)
        raise RuntimeError("code expired before it was entered")

    oauth_login._STARTERS["fake_vendor"] = fake
    reply = await oauth_login.start("fake_vendor")
    assert reply["user_code"] == "LATE"
    task = oauth_login.pending().get("fake_vendor")
    if task is not None:
        await asyncio.gather(task, return_exceptions=True)
    assert oauth_login.pending() == {}


class _FakeChatgptAuth:
    made: list["_FakeChatgptAuth"] = []

    def __init__(self) -> None:
        self.requests = 0
        _FakeChatgptAuth.made.append(self)

    def _request_device_code(self) -> dict[str, str]:
        self.requests += 1
        return {"user_code": "CODE-1", "device_code": "dev-1"}

    def _login_device_code(self) -> None:
        self._request_device_code()


def _fake_module(name: str, **attrs):
    import types

    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    return mod


def test_openai_codex_starter_hands_over_the_code_the_driver_mints(monkeypatch: pytest.MonkeyPatch) -> None:
    import sys

    from raven.providers import chatgpt_token, litellm_setup

    cleared: list[bool] = []
    monkeypatch.setattr(litellm_setup, "import_litellm", lambda: None)
    monkeypatch.setattr(chatgpt_token, "clear_abandoned_device_code", lambda: cleared.append(True) or True)
    monkeypatch.setitem(
        sys.modules,
        "litellm.llms.chatgpt.authenticator",
        _fake_module("litellm.llms.chatgpt.authenticator", Authenticator=_FakeChatgptAuth),
    )
    monkeypatch.setitem(
        sys.modules,
        "litellm.llms.chatgpt.common_utils",
        _fake_module("litellm.llms.chatgpt.common_utils", CHATGPT_DEVICE_VERIFY_URL="https://chatgpt.com/device"),
    )
    handed: list[tuple[str, str, int]] = []
    oauth_login._openai_codex(lambda uri, code, ttl: handed.append((uri, code, ttl)))
    assert cleared == [True]
    assert handed == [("https://chatgpt.com/device", "CODE-1", oauth_login.DEFAULT_TTL_S)]
    assert _FakeChatgptAuth.made[-1].requests == 1


async def test_a_page_sign_in_runs_the_real_driver_and_leaves_the_token_owner_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the vendor is faked here, so this pins what the starter borrows from
    LiteLLM: that its own ``_login_device_code`` still mints the code through
    ``_request_device_code``, the one step the starter listens in on. The test
    above fakes the whole driver and would stay green if that stopped being so.

    The driver writes the credential with a plain ``open()`` under the process
    umask. ``provider login`` restricts it afterwards; a sign-in started from the
    page has to as well, or the token lands readable by anyone.
    """
    from litellm.llms.chatgpt import authenticator as driver
    from litellm.llms.chatgpt.common_utils import (
        CHATGPT_DEVICE_CODE_URL,
        CHATGPT_DEVICE_TOKEN_URL,
        CHATGPT_DEVICE_VERIFY_URL,
        CHATGPT_OAUTH_TOKEN_URL,
    )

    token_dir = tmp_path / "chatgpt"
    monkeypatch.setenv("CHATGPT_TOKEN_DIR", str(token_dir))
    answers = {
        CHATGPT_DEVICE_CODE_URL: {"device_auth_id": "dev-1", "user_code": "CODE-1", "interval": "0"},
        CHATGPT_DEVICE_TOKEN_URL: {"authorization_code": "ac", "code_challenge": "cc", "code_verifier": "cv"},
        CHATGPT_OAUTH_TOKEN_URL: {"access_token": "access", "refresh_token": "refresh", "id_token": "id"},
    }

    class _Vendor:
        def post(self, url: str, **_kw) -> httpx.Response:
            return httpx.Response(200, json=answers[url], request=httpx.Request("POST", url))

    monkeypatch.setattr(driver, "_get_httpx_client", lambda *_a, **_k: _Vendor())
    # A driver that no longer mints through the hooked step never hands a code
    # over; fail on that in seconds rather than the page's minute.
    monkeypatch.setattr(oauth_login, "_HANDOFF_TIMEOUT_S", 5)

    reply = await oauth_login.start("openai_codex")
    for _ in range(200):
        if not oauth_login.pending():
            break
        await asyncio.sleep(0.02)

    auth = token_dir / "auth.json"
    assert reply == {
        "verification_uri": CHATGPT_DEVICE_VERIFY_URL,
        "user_code": "CODE-1",
        "expires_in": oauth_login.DEFAULT_TTL_S,
    }
    assert oauth_login.pending() == {}, "the driver never finished its flow"
    stored = json.loads(auth.read_text(encoding="utf-8"))
    assert (stored["access_token"], stored["refresh_token"]) == ("access", "refresh")
    assert stat.S_IMODE(auth.stat().st_mode) == 0o600, "the token was left readable by others"


class _FakeCopilotAuth:
    polls_before_token = 1
    token_dir: Path | None = None

    def __init__(self) -> None:
        self.polls = 0
        self.access_token_file = str((_FakeCopilotAuth.token_dir or Path(".")) / "token")
        self.api_key_read = False

    def _get_device_code(self) -> dict:
        return {
            "verification_uri": "https://github.com/login/device",
            "user_code": "GH-42",
            "device_code": "d",
            "expires_in": 30,
        }

    def _poll_for_access_token(self, device_code: str) -> str:
        self.polls += 1
        if self.polls <= self.polls_before_token:
            raise TimeoutError("still waiting")
        return "gho_token"

    def _ensure_token_dir(self) -> None:
        Path(self.access_token_file).parent.mkdir(parents=True, exist_ok=True)

    def get_api_key(self) -> str:
        self.api_key_read = True
        return "key"


def test_github_copilot_starter_hands_over_the_pair_and_keeps_polling_past_the_drivers_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from raven.providers import litellm_setup

    monkeypatch.setattr(litellm_setup, "import_litellm", lambda: None)
    _FakeCopilotAuth.token_dir = tmp_path
    _FakeCopilotAuth.polls_before_token = 1
    monkeypatch.setitem(
        sys.modules,
        "litellm.llms.github_copilot.authenticator",
        _fake_module("litellm.llms.github_copilot.authenticator", Authenticator=_FakeCopilotAuth),
    )
    handed: list[tuple[str, str, int]] = []
    oauth_login._github_copilot(lambda uri, code, ttl: handed.append((uri, code, ttl)))
    assert handed == [("https://github.com/login/device", "GH-42", 30)]
    assert (tmp_path / "token").read_text(encoding="utf-8") == "gho_token"


def test_github_copilot_starter_gives_up_when_the_code_expires_unentered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import sys

    from raven.providers import litellm_setup

    monkeypatch.setattr(litellm_setup, "import_litellm", lambda: None)

    class Expired(_FakeCopilotAuth):
        def _get_device_code(self) -> dict:
            return {**super()._get_device_code(), "expires_in": 1}

        def _poll_for_access_token(self, device_code: str) -> str:
            raise TimeoutError("still waiting")

    _FakeCopilotAuth.token_dir = tmp_path
    monkeypatch.setitem(
        sys.modules,
        "litellm.llms.github_copilot.authenticator",
        _fake_module("litellm.llms.github_copilot.authenticator", Authenticator=Expired),
    )
    with pytest.raises(RuntimeError, match="expired"):
        oauth_login._github_copilot(lambda *a: None)
    assert not (tmp_path / "token").exists()
