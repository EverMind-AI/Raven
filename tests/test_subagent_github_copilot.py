"""GitHub Copilot's refusals, in the words Copilot CLI 1.0.88 sent back.

A missing GitHub token is a bare ``Authentication required`` and is named by
the shared sign-in reading. The model call's own refusals come back as the
assistant message of a turn Copilot marks finished. Each sentence below is one
of those, from a stand-in provider. A reply that is not one of them stays a
reply.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent.subagent import github_copilot
from raven.agent.subagent.probe_state import Remedy

_KEY_401 = (
    "Error: Authentication failed with provider at http://127.0.0.1:9 (HTTP 401).\n"
    "  Check your COPILOT_PROVIDER_API_KEY, COPILOT_PROVIDER_API_KEY_COMMAND, "
    "or COPILOT_PROVIDER_BEARER_TOKEN."
)
_KEY_403 = _KEY_401.replace("401", "403")
_CREDIT = "Error: 402 Your credit balance is too low. Please add credits."
_EXPIRED = "Error: 400 Your Copilot subscription has expired. Please renew to continue."
_MODEL = (
    "Error: Model 'not-a-model' not found on provider at http://127.0.0.1:9 (HTTP 404).\n"
    "  Check that the model is available on your provider."
)
_LOCAL = "Error: Could not connect to local model provider at http://127.0.0.1:9."
_TUNNEL = (
    "Error: Execution failed: Failed to get response from the AI model; retried 5 times "
    "Last error: Failed native model HTTP request: error sending request for url "
    "(https://api.githubcopilot.com/chat/completions): client error (Connect): tunnel error"
)
_SERVER = "Error: Failed to get response from the AI model; retried 5 times Last error: 500 Internal server error"
_GATEWAY = "Error: Failed to get response from the AI model; retried 5 times Last error: 502 Bad Gateway"


def _cfg(preset: str = "github_copilot") -> SimpleNamespace:
    return SimpleNamespace(name="GitHub Copilot", preset=preset, kind="acp", command="copilot --acp")


@pytest.mark.parametrize(
    ("said", "kind"),
    [
        (_CREDIT, "billing"),
        (_EXPIRED, "plan"),
        (_MODEL, "model"),
        (_LOCAL, "network"),
        (_TUNNEL, "network"),
    ],
)
def test_copilot_names_the_fix_its_own_sentence_asked_for(said: str, kind: str) -> None:
    found = github_copilot.read(said)
    assert found is not None
    text, remedy = found
    assert remedy is not None and remedy.kind == kind
    assert said in text
    assert "copilot login" not in text


@pytest.mark.parametrize("said", [_SERVER, _GATEWAY])
def test_a_provider_server_error_is_a_failure_without_a_guessed_fix(said: str) -> None:
    found = github_copilot.read(said)
    assert found is not None
    text, remedy = found
    assert remedy is None
    assert "server error" in text
    assert said in text


@pytest.mark.parametrize(
    "said",
    [
        "PONG",
        "Reply with exactly: PONG",
        "Authentication required",
        "the model has a context window of 404 tokens",
        "Rate limit reached. Please slow down.",
    ],
)
def test_a_reply_that_is_not_one_of_those_refusals_is_left_alone(said: str) -> None:
    assert github_copilot.read(said) is None


def test_a_bad_provider_key_is_not_told_to_sign_in() -> None:
    """The sentence says Authentication, which the shared rule reads as a sign-in.

    Copilot uses that word for a refused provider key and tells the reader which
    variable holds it. Sending them to `copilot login` does not change that key.
    """
    from raven.agent.subagent.probe import _refusal

    text, remedy = _refusal(_cfg(), _KEY_401, _KEY_401)
    assert remedy is None
    assert "copilot login" not in text
    assert text.startswith("its model provider refused the API key")
    assert "COPILOT_PROVIDER_API_KEY" in text


def test_a_bare_authentication_required_still_names_copilot_login(monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.agent.subagent import probe as probe_mod

    monkeypatch.setattr(probe_mod.shutil, "which", lambda exe, path=None: "/opt/homebrew/bin/copilot")
    text, remedy = probe_mod._refusal(_cfg(), "request failed: [-32000] Authentication required")
    assert remedy == Remedy("sign_in", "copilot login")
    assert "sign in with `copilot login`" in text


def test_another_agents_reply_is_not_read_as_copilots() -> None:
    assert github_copilot.applies(_cfg("qwen_code")) is False
    assert github_copilot.applies(_cfg("grok")) is False


async def test_a_finished_turn_whose_message_is_the_refusal_is_not_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    """Connect records a non-empty reply as success. Copilot's refusals are those replies."""
    from raven.agent.subagent import probe as probe_mod

    class _Backend:
        async def run(self, *args: object, **kwargs: object) -> str:
            return _CREDIT

    monkeypatch.setattr(probe_mod, "build_third_party_backend", lambda *args, **kwargs: _Backend())
    result = await probe_mod.ping_agent(_cfg())
    assert result.ok is False
    assert result.remedy == Remedy("billing")
    assert _CREDIT in result.detail


async def test_grok_is_not_read_with_copilots_sentences(monkeypatch: pytest.MonkeyPatch) -> None:
    """Grok's refusals do not come back in these sentences, so they are not borrowed."""
    from raven.agent.subagent import probe as probe_mod

    class _Backend:
        async def run(self, *args: object, **kwargs: object) -> str:
            return _CREDIT

    monkeypatch.setattr(probe_mod, "build_third_party_backend", lambda *args, **kwargs: _Backend())
    result = await probe_mod.ping_agent(
        SimpleNamespace(name="Grok Build", preset="grok", kind="acp", command="grok agent stdio")
    )
    assert result.ok is True


def test_an_install_the_login_path_misses_is_not_reported_as_absent(tmp_path: Path) -> None:
    from raven.agent.subagent.probe import _installed_outside_login_path

    binary = tmp_path / "copilot"
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    binary.chmod(0o755)
    found = _installed_outside_login_path("copilot", str(tmp_path / "empty"), roots=(tmp_path,))
    assert found == str(binary)
    assert _installed_outside_login_path("qwen", "", roots=(tmp_path,)) is None
    assert _installed_outside_login_path("copilot", str(tmp_path), roots=(tmp_path,)) is None


def test_these_two_agents_name_a_start_that_quit_or_never_spoke() -> None:
    from raven.agent.subagent.probe import _process_refusal
    from raven.agent.subagent.probe_state import Remedy

    def shown(tail: str) -> str:
        return f"acp agent 'x': connection ended (exit 1); stderr tail: {tail}"

    grok = SimpleNamespace(preset="grok", kind="acp", command="grok agent stdio")
    copilot = SimpleNamespace(preset="github_copilot", kind="acp", command="copilot --acp")

    text, remedy = _process_refusal(grok, shown("error: unrecognized subcommand 'agent'"))
    assert remedy == Remedy("upgrade", "npm i -g @xai-official/grok@latest")
    assert "too old to be connected" in text

    text, remedy = _process_refusal(copilot, shown("error: unexpected argument '--acp' found"))
    assert remedy == Remedy("upgrade", "npm i -g @github/copilot@latest")
    assert "too old to be connected" in text

    text, remedy = _process_refusal(
        copilot,
        shown("GitHub Copilot CLI: no platform package found. Reinstall with `npm install -g @github/copilot`."),
    )
    assert remedy is not None and remedy.kind == "upgrade"
    assert "platform package" in text

    text, remedy = _process_refusal(
        copilot, shown("Offline mode requires a local model provider. Set COPILOT_PROVIDER_BASE_URL to configure one.")
    )
    assert remedy is None
    assert "COPILOT_PROVIDER_BASE_URL" in text
    assert "copilot login" not in text

    text, remedy = _process_refusal(grok, "acp agent 'Grok': initialize timed out after 30.0s")
    assert remedy is None
    assert "ACP server did not start" in text

    text, remedy = _process_refusal(copilot, "acp agent 'GitHub Copilot': session/prompt timed out after 30s")
    assert remedy == Remedy("silent", "copilot -p hi")


def test_an_expired_sign_in_is_not_described_as_a_missing_key() -> None:
    from raven.agent.subagent.probe import _refusal

    text, remedy = _refusal(
        SimpleNamespace(preset="grok", kind="acp"),
        "Failed to authenticate: OAuth session expired and could not be refreshed.",
    )
    assert remedy is not None and remedy.command == "grok login"
    assert text.startswith("its sign-in has expired")

    text, remedy = _refusal(
        SimpleNamespace(preset="grok", kind="acp"),
        "Incorrect API key provided",
    )
    assert remedy is not None and remedy.command == "grok login"
    assert text.startswith("its API key was refused")


async def test_an_ordinary_reply_is_still_a_success(monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.agent.subagent import probe as probe_mod

    class _Backend:
        async def run(self, *args: object, **kwargs: object) -> str:
            return "PONG"

    monkeypatch.setattr(probe_mod, "build_third_party_backend", lambda *args, **kwargs: _Backend())
    result = await probe_mod.ping_agent(_cfg())
    assert result.ok is True
    assert result.remedy is None


# ---- the dispatch path ------------------------------------------------------


_STUB = Path(__file__).with_name("acp_stub_server.py")


@pytest.fixture
async def _closed_pool():
    from raven.acp_client.pool import close_pool

    yield
    await close_pool()


def _dispatched(preset: str, reply: str):
    import sys

    from raven.agent.subagent.backends import build_third_party_backend
    from raven.config.schema import ThirdPartyAcpSubagentConfig
    from raven.utils.commands import command_quote

    return build_third_party_backend(
        ThirdPartyAcpSubagentConfig(
            name="GitHub Copilot",
            preset=preset,
            command=f"{command_quote(sys.executable)} {command_quote(str(_STUB))}",
            env={"ACP_STUB_REPLY": reply},
            ready_timeout_ms=15000,
        )
    )


async def test_a_dispatch_whose_reply_is_the_refusal_fails(tmp_path: Path, _closed_pool) -> None:
    """The run a spawn or a DAG node makes, not only the Connect ping.

    Copilot ends the turn normally with the refusal as its message, so the
    backend's own checks -- an empty turn, a failed call with nothing after it --
    pass it, and the sentence went back as the work: a node recorded completed
    whose output was "Your credit balance is too low", merged downstream.
    """
    from raven.acp_client.acp_agent import AcpEmptyTurnError

    backend = _dispatched("github_copilot", _CREDIT)

    with pytest.raises(AcpEmptyTurnError) as caught:
        await backend.run("summarize the README", task_id="t1", workspace=tmp_path, executor=None)

    assert "for want of credit" in str(caught.value)
    assert _CREDIT in str(caught.value), "the agent's own words travel with the reading"


async def test_an_answer_that_quotes_such_an_error_is_still_an_answer(tmp_path: Path, _closed_pool) -> None:
    """A refusal is the whole message: an answer that cites a 500 while
    debugging something is the work, and failing it would lose the work."""
    answer = (
        "The deploy failed because the worker crashed. Its log ends with "
        "'Last error: 500 Internal server error'; restart the worker and redeploy."
    )
    backend = _dispatched("github_copilot", answer)

    out = await backend.run("why did the deploy fail?", task_id="t2", workspace=tmp_path, executor=None)

    assert out == answer


@pytest.mark.parametrize("said", [_KEY_401, _KEY_403, _CREDIT, _EXPIRED, _MODEL, _LOCAL, _TUNNEL, _SERVER, _GATEWAY])
def test_every_measured_refusal_is_read_as_a_whole_reply(said: str) -> None:
    assert github_copilot.read_reply(said) is not None


@pytest.mark.parametrize(
    "answer",
    [
        "Error: 502 Bad Gateway from the staging load balancer; the upstream pool is empty.",
        "Error: GET /api/users failed (HTTP 404) because the route was renamed to /api/accounts.",
        "Error: your credit balance is too low is what the billing page shows; top it up there.",
    ],
)
def test_an_answer_that_opens_with_an_error_it_found_is_not_a_refusal(answer: str) -> None:
    """A task can be "curl staging and tell me what it returns": the answer then
    opens with the error it found. Copilot's own refusals are its fixed sentences
    -- "with provider at", "from the AI model; retried" -- and only those are."""
    assert github_copilot.read_reply(answer) is None


async def test_another_agents_dispatch_is_not_read_with_copilots_sentences(tmp_path: Path, _closed_pool) -> None:
    backend = _dispatched("qwen_code", _CREDIT)

    out = await backend.run("summarize the README", task_id="t3", workspace=tmp_path, executor=None)

    assert out == _CREDIT


async def test_a_connect_reads_a_refusal_once_now_that_the_run_raises_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _closed_pool
) -> None:
    """The connect builds the same backend a dispatch does, so the refusal now
    arrives as the run's failure rather than as its reply. Read again from the
    failure's text, the reading wrapped itself: "refused ... It said: acp agent
    ... refused ... It said: Error: 402 ...". It is read from the agent's words."""
    import sys

    from raven.agent.subagent import probe as probe_mod
    from raven.config.schema import ThirdPartyAcpSubagentConfig
    from raven.utils.commands import command_quote

    monkeypatch.chdir(tmp_path)
    cfg = ThirdPartyAcpSubagentConfig(
        name="GitHub Copilot",
        preset="github_copilot",
        command=f"{command_quote(sys.executable)} {command_quote(str(_STUB))}",
        env={"ACP_STUB_REPLY": _CREDIT},
        ready_timeout_ms=15000,
    )

    result = await probe_mod.ping_agent(cfg)

    assert result.ok is False
    assert result.remedy == Remedy("billing")
    assert result.detail == github_copilot.read(_CREDIT)[0]
