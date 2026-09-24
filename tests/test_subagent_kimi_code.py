"""Kimi Code's refusals, read the way Kimi Code 2.1.0 reports them.

Every sentence here is one Kimi Code printed, measured 2026-09-24 against a
stand-in endpoint answering each status and against a real account whose plan
does not include it -- except a membership's own limits, which no account at
hand could reach, given in its error reference's words. Over ACP it says only
"Authentication required" (bare, or
with the provider's status behind it) or nothing at all; `kimi -p` prints the
reason as ``error: failed to run prompt: ...``. A stand-in `kimi` plays both
halves: ``acp`` runs the stub ACP server, ``-p`` prints what the test gives it.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import sys
from pathlib import Path
from typing import Any

import pytest

from raven.agent.subagent import kimi_code
from raven.agent.subagent.probe_state import Remedy
from raven.config.schema import ThirdPartyAcpSubagentConfig

_STUB = Path(__file__).with_name("acp_stub_server.py")

PLAN = (
    "Your current subscription does not have access to Kimi Code right now. Upgrade your plan to keep "
    "coding with Kimi Code: https://www.kimi.com/code/#pricing"
)
PRICING = "https://www.kimi.com/code/#pricing"
NO_MODEL = "No model configured. Run `kimi` and use /login to sign in, then retry; or set default_model in config.toml."
SWITCH = ("kimi", "/model")
EDIT_PROVIDER = ("kimi", "/provider")


def _cfg(command: str = "kimi acp", **kw: Any) -> ThirdPartyAcpSubagentConfig:
    return ThirdPartyAcpSubagentConfig(name="Kimi Code", preset="kimi_code", command=command, **kw)


@pytest.fixture(autouse=True)
def _no_login_shell(monkeypatch: pytest.MonkeyPatch) -> None:
    """Hand the launch and the ask this process's environment rather than a login shell's.

    What is under test is how Kimi Code's words are read, not the capture; the
    first test to reach it would otherwise spend seconds running the developer's
    own shell, and the stand-in `kimi` travels on each row's own PATH anyway.
    """
    monkeypatch.setattr("raven.agent.subagent.backends.env._LOGIN_ENV", dict(os.environ))


@pytest.mark.parametrize(
    "said, remedy",
    [
        # Over ACP, on `session/prompt`.
        (f"Authentication required: 403 {PLAN}", Remedy("plan", PRICING)),
        ("Authentication required: 401 Invalid Authentication", Remedy("setup", *EDIT_PROVIDER)),
        # From `kimi -p`.
        (f"provider.auth_error: 403 {PLAN}", Remedy("plan", PRICING)),
        ("provider.auth_error: 401 Invalid Authentication", Remedy("setup", *EDIT_PROVIDER)),
        ("The provided authorization grant is invalid", Remedy("sign_in", "kimi login")),
        (NO_MODEL, Remedy("sign_in", "kimi login")),
        ("provider managed:kimi-code has no credential configured", Remedy("sign_in", "kimi login")),
        ("provider moonshotai-cn has no credential configured", Remedy("setup", *EDIT_PROVIDER)),
        ('Model "moonshotai-cn/no-such-model" is not configured in config.toml.', Remedy("model", *SWITCH)),
        (
            "provider.api_error: 402 This request requires more credits, or fewer max_tokens. You requested up to "
            "32768 tokens, but can only afford 2048.",
            Remedy("billing", *SWITCH),
        ),
        ("provider.api_error: 404 Not found the model some-model or Permission denied", Remedy("model", *SWITCH)),
        (
            "provider.rate_limit: 429 Your account org-test request reached organization max RPM: 3, please try "
            "again after 1 seconds",
            Remedy("quota", *SWITCH),
        ),
        (
            "provider.api_error: 429 Your account org-test is suspended due to insufficient balance, please "
            "recharge your account or check your plan and billing details",
            Remedy("billing", *SWITCH),
        ),
        (
            "no provider configured; complete onboarding via /login or the providers endpoint",
            Remedy("sign_in", "kimi login"),
        ),
        # Kimi Code's error reference: a signed-in account's own limits arrive as
        # 401 or 403 too, and none of them is a key to replace.
        (
            "Authentication required: 403 You've reached your 5-hour usage limit. Your quota will reset when the "
            "current 5-hour window ends.",
            Remedy("quota", *SWITCH),
        ),
        (
            "Authentication required: 403 You've reached your weekly (7-day) usage limit. Your quota will reset "
            "when the current 7-day window ends.",
            Remedy("quota", *SWITCH),
        ),
        (
            "Authentication required: 403 You've reached your monthly usage limit for this billing cycle. Your "
            "quota will be refreshed in the next cycle.",
            Remedy("quota", *SWITCH),
        ),
        (
            "Authentication required: 403 You've reached your concurrent request limit. Please wait for your "
            "ongoing requests to finish and try again.",
            Remedy("quota", *SWITCH),
        ),
        (
            "Authentication required: 401 Your current subscription does not have access to k3. Upgrade to "
            "higher-tier Kimi Code plans.",
            Remedy("model", *SWITCH),
        ),
        (
            "Authentication required: 401 Your current plan supports only k3 up to 256K context. 1M context is "
            "available on higher-tier Kimi Code plans.",
            Remedy("model", *SWITCH),
        ),
        (
            "Authentication required: 401 Your current subscription does not have access to "
            "kimi-for-coding-highspeed. Upgrade to higher-tier Kimi Code plans.",
            Remedy("model", *SWITCH),
        ),
        (
            "Authentication required: 401 Your model id does not exist, recognized as other:claude-sonnet.",
            Remedy("model", *SWITCH),
        ),
        (
            "Authentication required: 401 The API Key appears to be invalid or may have expired. Please verify "
            "your credentials and try again.",
            Remedy("setup", *EDIT_PROVIDER),
        ),
        (
            "provider.api_error: 402 We're unable to verify your membership benefits at this time. Please ensure "
            "your membership is active.",
            Remedy("plan"),
        ),
        # Measured with a made-up key: one provider's words for a key it does not
        # know. Any other 401 is a refused key however it is worded.
        ("Authentication required: 401 Missing Authentication header", Remedy("setup", *EDIT_PROVIDER)),
        # A plan refusal wrapped in the provider's JSON, or ending a sentence,
        # keeps only the address.
        (f'Authentication required: 403 {{"error": {{"message": "{PLAN}"}}}}', Remedy("plan", PRICING)),
        (f"Authentication required: 403 {PLAN}.", Remedy("plan", PRICING)),
    ],
)
def test_kimi_codes_own_words_name_the_fix(said: str, remedy: Remedy) -> None:
    """Every refusal Kimi Code names is told as the fix it needs, not as "sign in".

    Over ACP all of these but the no-reason ones read "Authentication
    required", which the general reading takes for a missing sign-in: measured
    on a signed-in account, the page said "sign in to Kimi Code first" for a
    plan that does not include it, and signing in again changed nothing.
    """
    verdict = kimi_code.read(_cfg(), said)
    assert verdict is not None
    assert verdict[2] == remedy


@pytest.mark.parametrize(
    "said",
    [
        "Authentication required",
        "provider.api_error: 400 Invalid request: the max_tokens is too large",
        "Provider safety policy blocked the response.",
        "provider.connection_error: Connection error.",
        # A 401 or 403 that does not read as a credential is not told as one.
        "Authentication required: 403 We consider the current URL poses a security risk",
        # A port is not a status.
        "connect ECONNREFUSED 127.0.0.1:403",
    ],
)
def test_words_that_name_no_fix_are_left_as_they_came(said: str) -> None:
    """A bare refusal is asked about; one with a reason this cannot fix keeps its own words."""
    assert kimi_code.read(_cfg(), said) is None


def _through_kimi(
    tmp_path: Path,
    mode: str,
    *,
    says: str | None = None,
    doctor: str | None = None,
    message: str | None = None,
    no_acp: bool = False,
    slow_ask: bool = False,
    ansi: bool = False,
    command: str = "kimi acp",
    **kw: Any,
) -> ThirdPartyAcpSubagentConfig:
    """The Kimi Code preset, run by a stand-in `kimi` on PATH.

    ``acp`` starts the stub server in ``mode``; ``-p`` prints ``says`` the way
    Kimi Code 2.1.0 does (in colour with ``ansi``), or answers when there is
    nothing to say, and notes that it was asked; with ``slow_ask`` it notes its
    pid and never finishes. ``doctor config`` reports ``doctor`` as its one issue.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    asked = tmp_path / "asked"
    start = (
        "printf '%s\\n' \"error: unknown command 'acp'\" >&2; exit 1"
        if no_acp
        else f"exec {shlex.quote(sys.executable)} {shlex.quote(str(_STUB))}"
    )
    slow = f"    printf '%s\\n' \"$$\" > {shlex.quote(str(tmp_path / 'ask.pid'))}; exec sleep 30\n" if slow_ask else ""
    red, plain = ("\\033[91m", "\\033[39m") if ansi else ("", "")
    kimi = bin_dir / "kimi"
    kimi.write_text(
        "#!/bin/sh\n"
        'case "$1" in\n'
        f"  acp) {start} ;;\n"
        "  -p)\n"
        f"    printf '%s\\n' \"$*\" >> {shlex.quote(str(asked))}\n"
        f"{slow}"
        '    if [ -n "$KIMI_STUB_SAYS" ]; then\n'
        "      printf '%s\\n' 'kimi version 2.1.0' >&2\n"
        f"      printf '{red}error: failed to run prompt: %s{plain}\\n' \"$KIMI_STUB_SAYS\" >&2\n"
        "      printf '%s\\n' 'See log: /nowhere/logs/kimi-code.log' >&2\n"
        "      exit 1\n"
        "    fi\n"
        "    printf '%s\\n' 'PONG'; exit 0 ;;\n"
        "  doctor)\n"
        '    if [ -n "$KIMI_STUB_DOCTOR" ]; then\n'
        "      printf '%s\\n' 'Kimi doctor found 1 issue.' '' 'ERROR config.toml  /nowhere/config.toml' >&2\n"
        "      printf '  %s\\n' \"$KIMI_STUB_DOCTOR\" >&2\n"
        "      exit 1\n"
        "    fi\n"
        "    printf '%s\\n' 'OK config.toml  /nowhere/config.toml'; exit 0 ;;\n"
        "esac\n"
        "exit 2\n"
    )
    kimi.chmod(0o755)
    env = {"ACP_STUB_MODE": mode, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"}
    for key, value in (("KIMI_STUB_SAYS", says), ("KIMI_STUB_DOCTOR", doctor), ("ACP_STUB_MESSAGE", message)):
        if value is not None:
            env[key] = value
    return _cfg(
        command=command.replace("{bin}", str(bin_dir)),
        env=env,
        ready_timeout_ms=kw.pop("ready_timeout_ms", 15000),
        **kw,
    )


def _asked(tmp_path: Path) -> list[str]:
    log = tmp_path / "asked"
    return log.read_text().splitlines() if log.exists() else []


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def test_a_plan_that_does_not_include_kimi_code_is_named_with_its_page(tmp_path: Path) -> None:
    """Measured on a real signed-in account: the refusal carries the reason, so nothing is asked."""
    from raven.agent.subagent.probe import ping_agent

    pinged = await ping_agent(_through_kimi(tmp_path, "kimi_refuses", message=f"Authentication required: 403 {PLAN}"))
    assert pinged.ok is False
    assert pinged.remedy == Remedy("plan", PRICING)
    assert pinged.detail.startswith("it is signed in, but the account's plan does not include Kimi Code; ")
    assert PLAN in pinged.detail, "Kimi Code's own words are kept for the fold"
    assert _asked(tmp_path) == []


async def test_a_refused_key_is_put_right_in_kimi_codes_provider_list(tmp_path: Path) -> None:
    from raven.agent.subagent.probe import ping_agent

    cfg = _through_kimi(tmp_path, "kimi_refuses", message="Authentication required: 401 Invalid Authentication")
    pinged = await ping_agent(cfg)
    assert pinged.remedy == Remedy("setup", *EDIT_PROVIDER)
    assert "(401)" in pinged.detail
    assert _asked(tmp_path) == []


async def test_a_session_kimi_code_will_not_start_is_asked_about(tmp_path: Path) -> None:
    """The bare refusal says nothing, so `kimi -p` is asked, once, with the connect's own prompt."""
    from raven.agent.subagent.probe import PROBE_PROMPT, ping_agent

    pinged = await ping_agent(_through_kimi(tmp_path, "kimi_not_ready", says=NO_MODEL))
    assert pinged.remedy == Remedy("sign_in", "kimi login")
    assert pinged.detail.startswith("it has no model to use: it is not signed in and has no model provider set up; ")
    assert _asked(tmp_path) == [f"-p {PROBE_PROMPT}"]


async def test_a_config_that_does_not_parse_is_named_by_kimi_doctor(tmp_path: Path) -> None:
    """Measured: a TOML syntax error reads as "No model configured" to `kimi -p`; signing in would not fix it."""
    from raven.agent.subagent.probe import ping_agent

    broken = "Invalid TOML in /nowhere/config.toml: only letter, numbers, dashes and underscores are allowed in keys (line 3, column 12)"
    pinged = await ping_agent(_through_kimi(tmp_path, "kimi_not_ready", says=NO_MODEL, doctor=broken))
    assert pinged.remedy == Remedy("config", "kimi doctor config")
    assert "(line 3, column 12)" in pinged.detail


@pytest.mark.parametrize(
    "says, remedy",
    [
        ("The provided authorization grant is invalid", Remedy("sign_in", "kimi login")),
        ('Model "moonshotai-cn/no-such-model" is not configured in config.toml.', Remedy("model", *SWITCH)),
        ("provider.api_error: 402 This request requires more credits", Remedy("billing", *SWITCH)),
    ],
)
async def test_a_turn_kimi_code_ended_with_nothing_is_asked_about(tmp_path: Path, says: str, remedy: Remedy) -> None:
    """A sign-in that no longer renews, 402, 404: over ACP each is an empty turn and nothing else."""
    from raven.agent.subagent.probe import ping_agent

    pinged = await ping_agent(_through_kimi(tmp_path, "empty_turn", says=says))
    assert pinged.remedy == remedy
    assert says in pinged.detail
    assert len(_asked(tmp_path)) == 1


async def test_words_kimi_code_gives_that_name_no_fix_are_shown_instead_of_the_empty_turn(tmp_path: Path) -> None:
    from raven.agent.subagent.probe import ping_agent

    pinged = await ping_agent(
        _through_kimi(tmp_path, "empty_turn", says="Provider safety policy blocked the response.")
    )
    assert pinged.remedy is None
    assert pinged.detail.endswith("asked again with `kimi -p`, it said: Provider safety policy blocked the response.")


async def test_an_empty_turn_that_answers_when_asked_again_says_so(tmp_path: Path) -> None:
    from raven.agent.subagent.probe import ping_agent

    pinged = await ping_agent(_through_kimi(tmp_path, "empty_turn"))
    assert pinged.ok is False
    assert pinged.remedy is None
    assert "it answered, so the failure may have passed" in pinged.detail


@pytest.mark.production_timing
async def test_an_ask_that_runs_out_leaves_the_connects_own_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Retried failures take `kimi -p` about 150 s; the ask gives up rather than hold the connect.

    It waits the cap out, a second here, to prove it holds.
    """
    from raven.agent.subagent.probe import PROBE_PROMPT, ping_agent

    monkeypatch.setattr(kimi_code, "_ASK_TIMEOUT_S", 1.0)
    pinged = await ping_agent(_through_kimi(tmp_path, "kimi_not_ready", says=NO_MODEL, slow_ask=True))
    # The general reading of a bare credential refusal, with the preset's sign-in.
    assert pinged.remedy == Remedy("sign_in", "kimi login")
    assert pinged.detail.startswith("it is installed but has no usable credential; ")
    assert _asked(tmp_path) == [f"-p {PROBE_PROMPT}"]
    assert not _alive(int((tmp_path / "ask.pid").read_text())), "the `kimi` asked goes when the ask does"


async def test_a_connect_that_goes_away_mid_ask_takes_the_kimi_it_asked(tmp_path: Path) -> None:
    """The page that asked going away cancels the connect; the `kimi -p` it started does not outlive it."""
    asking = asyncio.create_task(kimi_code.ask(_through_kimi(tmp_path, "ok", slow_ask=True), "hi"))
    started = tmp_path / "ask.pid"
    for _ in range(500):
        if started.exists() and started.read_text().strip():
            break
        await asyncio.sleep(0.01)
    pid = int(started.read_text())
    assert _alive(pid)
    asking.cancel()
    with pytest.raises(asyncio.CancelledError):
        await asking
    assert not _alive(pid)


async def test_a_coloured_answer_is_read_like_a_plain_one(tmp_path: Path) -> None:
    """With FORCE_COLOR set, `kimi -p` prints its error line in colour (measured); the colour is not the reason."""
    from raven.agent.subagent.probe import ping_agent

    pinged = await ping_agent(_through_kimi(tmp_path, "kimi_not_ready", says=NO_MODEL, ansi=True))
    assert pinged.remedy == Remedy("sign_in", "kimi login")
    assert pinged.detail.startswith("it has no model to use: ")
    assert "\x1b" not in pinged.detail


async def test_a_bare_refusal_asked_about_in_words_it_does_not_know_still_reads_as_a_sign_in(
    tmp_path: Path,
) -> None:
    """Asking only adds to the general reading: words it cannot place (made up here) keep "sign in" and are shown."""
    from raven.agent.subagent.probe import ping_agent

    pinged = await ping_agent(_through_kimi(tmp_path, "kimi_not_ready", says="Something unexpected happened."))
    assert pinged.remedy == Remedy("sign_in", "kimi login")
    assert pinged.detail.endswith("asked again with `kimi -p`, it said: Something unexpected happened.")


async def test_a_prompt_kimi_code_never_finishes_names_the_command_that_says_why(tmp_path: Path) -> None:
    """Its retried failures outlast the wait: the general `silent` verdict, with Kimi Code's own diagnose command."""
    from raven.agent.subagent.probe import ping_agent

    pinged = await ping_agent(_through_kimi(tmp_path, "prompt_hangs", timeout=1))
    assert pinged.remedy == Remedy("silent", "kimi -p hi")
    assert _asked(tmp_path) == [], "a retry that outlasts the connect would outlast the ask too"


async def test_a_start_that_never_finishes_its_handshake_is_not_a_working_kimi_code(tmp_path: Path) -> None:
    from raven.agent.subagent.probe import ping_agent

    pinged = await ping_agent(_through_kimi(tmp_path, "silent", ready_timeout_ms=1000))
    assert pinged.remedy == Remedy("upgrade")
    assert pinged.detail.startswith("it started but never finished its ACP handshake")


async def test_a_kimi_that_does_not_know_acp_is_told_to_upgrade(tmp_path: Path) -> None:
    """commander's spelling, "error: unknown command 'acp'", read as the too-old verdict yargs' already is."""
    from raven.agent.subagent.probe import ping_agent

    pinged = await ping_agent(_through_kimi(tmp_path, "ok", no_acp=True))
    assert pinged.remedy == Remedy("upgrade")
    assert pinged.detail.startswith("it is too old to be connected: it does not know the flag or command")


async def test_a_row_whose_command_was_edited_is_not_asked_about(tmp_path: Path) -> None:
    """Its command is its operator's: the general reading stands, and its executable is not run a second way."""
    from raven.agent.subagent.probe import ping_agent

    pinged = await ping_agent(_through_kimi(tmp_path, "kimi_not_ready", says=NO_MODEL, command="{bin}/kimi acp"))
    assert pinged.remedy == Remedy("sign_in", "kimi login")
    assert _asked(tmp_path) == []


async def test_the_test_button_asks_about_a_refused_handshake_too(tmp_path: Path) -> None:
    """Test stops at the handshake that Connect's ping would have failed on, and names the same fix."""
    from raven.agent.subagent.probe import run_test

    cfg = _through_kimi(
        tmp_path, "kimi_not_ready", says='Model "moonshotai-cn/no-such-model" is not configured in config.toml.'
    )
    tested = await run_test(cfg, source="config")
    assert tested.ok is False
    assert tested.remedy == Remedy("model", *SWITCH)


async def test_the_test_button_names_an_unfinished_handshake_the_way_the_connect_does(tmp_path: Path) -> None:
    from raven.agent.subagent.probe import run_test

    tested = await run_test(_through_kimi(tmp_path, "silent", ready_timeout_ms=1000), source="config")
    assert tested.remedy == Remedy("upgrade")


def test_kimi_code_is_in_each_table_a_verdict_reads_its_commands_from() -> None:
    from raven.agent.subagent.presets import diagnose_hint_for, model_switch_hint_for, sign_in_hint_for

    cfg = _cfg()
    sign_in = sign_in_hint_for(cfg)
    assert sign_in is not None and sign_in.local == "kimi login"
    switch = model_switch_hint_for(cfg)
    assert switch is not None and (switch.command, switch.then) == SWITCH
    assert diagnose_hint_for(cfg) == "kimi -p hi"


def test_the_plan_and_config_kinds_travel_to_the_page() -> None:
    assert Remedy.from_wire(Remedy("plan", PRICING).to_wire()) == Remedy("plan", PRICING)
    assert Remedy.from_wire(Remedy("config", "kimi doctor config").to_wire()) == Remedy("config", "kimi doctor config")
    assert Remedy.from_wire({"kind": "plan"}) == Remedy("plan")
