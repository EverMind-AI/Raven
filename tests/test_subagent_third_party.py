"""Third-party subagent backends + manager wiring + spawn tool (req5, P3b)."""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import socket
import subprocess
import sys
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

import raven.agent.subagent.backends.env as env_mod
from raven.agent.subagent import instances as instances_mod
from raven.agent.subagent.backends import (
    AgentMeta,
    CliAgentBackend,
    OpenAIApiBackend,
    build_third_party_backend,
    format_agent_listing,
    third_party_agent_meta,
)
from raven.agent.subagent.backends.env import login_shell_env
from raven.agent.subagent.builtin_agents import GENERIC_AGENT
from raven.agent.subagent.manager import SPAWN_REFUSED_PREFIX, SubagentManager
from raven.agent.subagent.presets import third_party_subagent_preset, third_party_subagent_presets
from raven.agent.tools.spawn import SpawnTool
from raven.config.schema import (
    ACP_UNSUPPORTED_FIELDS,
    SubagentEverosConfig,
    SubagentsConfig,
    ThirdPartyAcpSubagentConfig,
    ThirdPartyCliSubagentConfig,
    ThirdPartyOpenAISubagentConfig,
)


@pytest.fixture(autouse=True)
def _isolated_instance_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test in this module must never touch the real user registry file.

    `SubagentManager` now writes spawn status through the process-wide
    `get_registry()` singleton (same reasoning as
    `tests/test_subagent_dag_runner.py`'s fixture of the same name): without
    this, any test whose manager spawn carries a session_key and a
    third-party agent name would accumulate junk rows in
    `~/.raven/subagent_instances.json` on every test run, forever.
    """
    monkeypatch.setattr(
        instances_mod, "_registry", instances_mod.InstanceRegistry(path=tmp_path / "_autouse_inst.json")
    )


# --- CLI backend ---------------------------------------------------------


@pytest.fixture
def _clear_login_env_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """The capture is cached per process, so each test needs a cold cache.

    `SHELL` is pinned too: the capture now runs the user's own login shell, so
    without this every test in this group would depend on the shell of whoever
    (or whatever CI runner) started pytest.
    """
    monkeypatch.setattr(env_mod, "_LOGIN_ENV", None)
    monkeypatch.setattr(env_mod, "_LOGIN_ENV_FAILED", False)
    monkeypatch.setenv("SHELL", "/bin/bash")


def test_login_shell_env_parses_nul_separated_output(
    monkeypatch: pytest.MonkeyPatch, _clear_login_env_cache: None
) -> None:
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"PATH=/usr/local/bin:/usr/bin\0HOME=/root\0", b"")

    monkeypatch.setattr(env_mod.subprocess, "run", fake_run)
    assert login_shell_env() == {"PATH": "/usr/local/bin:/usr/bin", "HOME": "/root"}
    # Cached: a second call must not shell out again.
    login_shell_env()
    assert len(calls) == 1
    assert calls[0][:2] == ["/bin/bash", "-lic"]


def test_login_shell_env_captures_from_the_users_own_shell(
    monkeypatch: pytest.MonkeyPatch, _clear_login_env_cache: None
) -> None:
    # Hardcoding bash on a zsh host walks ~/.bash_profile and never reads the
    # ~/.zshrc the user's PATH additions live in -- and because bash exists and
    # exits 0, that wrong PATH is cached as a success rather than falling back.
    monkeypatch.setenv("SHELL", "/bin/zsh")
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"PATH=/opt/homebrew/bin\0", b"")

    monkeypatch.setattr(env_mod.subprocess, "run", fake_run)
    assert login_shell_env() == {"PATH": "/opt/homebrew/bin"}
    assert calls[0][:2] == ["/bin/zsh", "-lic"]


def test_login_shell_env_keeps_a_shell_path_that_is_not_in_bin(
    monkeypatch: pytest.MonkeyPatch, _clear_login_env_cache: None
) -> None:
    # A homebrew or nix shell is matched on its basename but run by its path.
    monkeypatch.setenv("SHELL", "/opt/homebrew/bin/zsh")
    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, b"PATH=/x\0", b"")

    monkeypatch.setattr(env_mod.subprocess, "run", fake_run)
    login_shell_env()
    assert calls[0][0] == "/opt/homebrew/bin/zsh"


@pytest.mark.parametrize("shell", ["/usr/bin/fish", "/bin/sh", "", "/usr/bin/nu"])
def test_login_shell_env_refuses_to_stand_in_for_an_undrivable_shell(
    shell: str, monkeypatch: pytest.MonkeyPatch, _clear_login_env_cache: None
) -> None:
    # Substituting bash for a shell we cannot drive would produce a confidently
    # wrong PATH (and fish prints a greeting under -i that corrupts the parse).
    # Raven's own environment is the honest answer: no better than not
    # capturing, but never wrong about a shell the user does not use.
    monkeypatch.setenv("SHELL", shell)
    monkeypatch.setenv("RAVEN_ENV_PROBE", "inherited")
    called = False

    def fake_run(argv, **kwargs):  # pragma: no cover - must not run
        nonlocal called
        called = True
        raise AssertionError(f"no shell should have been started, got {argv}")

    monkeypatch.setattr(env_mod.subprocess, "run", fake_run)
    assert login_shell_env()["RAVEN_ENV_PROBE"] == "inherited"
    assert called is False


def test_login_shell_env_falls_back_when_capture_fails(
    monkeypatch: pytest.MonkeyPatch, _clear_login_env_cache: None
) -> None:
    def boom(argv, **kwargs):
        raise OSError("no bash")

    monkeypatch.setattr(env_mod.subprocess, "run", boom)
    monkeypatch.setenv("RAVEN_ENV_PROBE", "inherited")
    assert login_shell_env()["RAVEN_ENV_PROBE"] == "inherited"


def test_login_shell_env_falls_back_when_capture_is_empty(
    monkeypatch: pytest.MonkeyPatch, _clear_login_env_cache: None
) -> None:
    monkeypatch.setattr(
        env_mod.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"", b""),
    )
    monkeypatch.setenv("RAVEN_ENV_PROBE", "inherited")
    assert login_shell_env()["RAVEN_ENV_PROBE"] == "inherited"


def test_login_shell_env_falls_back_on_nonzero_exit(
    monkeypatch: pytest.MonkeyPatch, _clear_login_env_cache: None
) -> None:
    # A profile error can still print something to stdout before failing; a
    # nonzero exit must not be masked by a non-empty capture.
    monkeypatch.setattr(
        env_mod.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 1, b"PATH=/usr/bin\0", b"profile.sh: command not found"
        ),
    )
    monkeypatch.setenv("RAVEN_ENV_PROBE", "inherited")
    assert login_shell_env()["RAVEN_ENV_PROBE"] == "inherited"


def test_login_shell_env_cache_hit_returns_a_copy(
    monkeypatch: pytest.MonkeyPatch, _clear_login_env_cache: None
) -> None:
    monkeypatch.setattr(
        env_mod.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"PATH=/usr/bin\0", b""),
    )
    first = login_shell_env()
    first["PATH"] = "poisoned"
    second = login_shell_env()
    assert second["PATH"] == "/usr/bin"


def test_login_shell_env_capture_starts_from_a_minimal_base_not_ravens_env(
    monkeypatch: pytest.MonkeyPatch, _clear_login_env_cache: None
) -> None:
    # Inheriting raven's own environment for the `bash -ic` capture would only
    # overlay the profile on top, leaving an editor/launcher-injected variable
    # in place. The capture's own `env=` must exclude it.
    monkeypatch.setenv("RAVEN_ONLY_VAR", "should-not-reach-bash")
    captured_kwargs: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured_kwargs.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, b"PATH=/usr/bin\0", b"")

    monkeypatch.setattr(env_mod.subprocess, "run", fake_run)
    login_shell_env()
    assert "RAVEN_ONLY_VAR" not in captured_kwargs["env"]


def test_login_shell_env_capture_runs_in_its_own_session(
    monkeypatch: pytest.MonkeyPatch, _clear_login_env_cache: None
) -> None:
    # `-i` makes bash set up job control, which it does through /dev/tty even
    # with every stdio stream redirected. Sharing raven's session lets it
    # tcsetpgrp the terminal to itself and exit without restoring it, which
    # leaves `raven tui` in a background process group -- the next keystroke
    # then raises SIGTTIN and stops the whole job.
    captured_kwargs: dict[str, object] = {}

    def fake_run(argv, **kwargs):
        captured_kwargs.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, b"PATH=/usr/bin\0", b"")

    monkeypatch.setattr(env_mod.subprocess, "run", fake_run)
    login_shell_env()
    assert captured_kwargs["start_new_session"] is True


async def test_cli_backend_uses_login_env_and_per_agent_env_wins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clear_login_env_cache: None
) -> None:
    # The login shell's value is the base; the per-agent `env` still overrides it,
    # which is the escape hatch for a machine whose login shell resolves the
    # wrong interpreter.
    monkeypatch.setattr(
        env_mod.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(
            argv, 0, b"FROM_LOGIN=yes\0OVERRIDE_ME=login\0PATH=/usr/bin:/bin\0", b""
        ),
    )
    script = tmp_path / "show_env.sh"
    script.write_text('printf "%s/%s" "$FROM_LOGIN" "$OVERRIDE_ME"\n', encoding="utf-8")
    be = CliAgentBackend(
        name="envcheck",
        command=f"sh {script}",
        env={"OVERRIDE_ME": "agent"},
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    out = await be.run("task", task_id="t1", workspace=tmp_path, executor=None)
    assert out == "yes/agent"


async def test_cli_backend_does_not_leak_ravens_own_env_into_the_child(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _clear_login_env_cache: None
) -> None:
    # Pins the regression this task fixes: restoring `{**os.environ, **self.env}`
    # as the base would leak RAVEN_ONLY_VAR into the child, since it lives only
    # in raven's own process environment and is deliberately absent from the
    # mocked login-shell capture below.
    monkeypatch.setenv("RAVEN_ONLY_VAR", "leaked")
    monkeypatch.setattr(
        env_mod.subprocess,
        "run",
        lambda argv, **kwargs: subprocess.CompletedProcess(argv, 0, b"PATH=/usr/bin:/bin\0", b""),
    )
    script = tmp_path / "check_no_leak.sh"
    script.write_text('printf "%s" "${RAVEN_ONLY_VAR:-absent}"\n', encoding="utf-8")
    be = CliAgentBackend(
        name="noleak",
        command=f"sh {script}",
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    out = await be.run("task", task_id="t1", workspace=tmp_path, executor=None)
    assert out == "absent"


async def test_cli_backend_stdin_delivery(tmp_path: Path) -> None:
    # No placeholder in command -> prompt is delivered on stdin. `cat` echoes it.
    be = CliAgentBackend(name="echo", command="cat")
    out = await be.run("hello via stdin", task_id="t1", workspace=tmp_path, executor=None)
    assert out == "hello via stdin"


async def test_cli_backend_prompt_placeholder(tmp_path: Path) -> None:
    # {prompt} substituted as a single argv token.
    be = CliAgentBackend(name="pf", command="printf %s {prompt}")
    out = await be.run("tok en", task_id="t2", workspace=tmp_path, executor=None)
    assert out == "tok en"


async def test_cli_backend_prompt_file_placeholder(tmp_path: Path) -> None:
    be = CliAgentBackend(name="catfile", command="cat {prompt_file}")
    out = await be.run("from a file", task_id="t3", workspace=tmp_path, executor=None)
    assert out == "from a file"


async def test_cli_backend_nonzero_exit_raises(tmp_path: Path) -> None:
    be = CliAgentBackend(name="boom", command="false")
    with pytest.raises(RuntimeError):
        await be.run("x", task_id="t4", workspace=tmp_path, executor=None)


async def test_cli_failure_says_the_one_line_and_not_the_transcript(tmp_path: Path) -> None:
    """What a failed dispatch says reaches the instance's conversation verbatim,
    and these CLIs answer errors with a whole JSON frame per line. Quoting the
    tail of that put a page of `cache_creation_input_tokens` in front of whoever
    was watching the run; the sentence they need is inside it."""
    said = "Your organization has disabled Claude subscription access for Claude Code"
    frames = (
        '{"type":"assistant","message":{"content":[{"type":"text","text":"%s"}]},'
        '"usage":{"cache_creation_input_tokens":0},"error":"oauth_org_not_allowed"}\n'
        '{"is_error":true,"api_error_status":403,"result":"%s","type":"result",'
        '"usage":{"cache_read_input_tokens":0,"server_tool_use":{"web_search_requests":0}}}\n'
    ) % (said, said)
    frames_file = tmp_path / "frames.jsonl"
    frames_file.write_text(frames, encoding="utf-8")
    script = tmp_path / "fail.sh"
    script.write_text(f"#!/bin/sh\ncat {frames_file}\nexit 1\n", encoding="utf-8")
    script.chmod(0o755)

    be = CliAgentBackend(name="claude_code", command=f"{script} {{prompt}}")
    with pytest.raises(RuntimeError) as caught:
        await be.run("x", task_id="t6", workspace=tmp_path, executor=None)

    message = str(caught.value)
    assert said in message
    # Nothing of the frame itself: not its keys, not its braces, not its length.
    assert "cache_creation_input_tokens" not in message
    assert "{" not in message
    assert len(message) < 400


async def test_a_text_transport_failure_still_says_what_went_wrong(tmp_path: Path) -> None:
    """`transcript_format` defaults to `text`, and such a command puts its whole
    diagnostic on stdout as a plain line. Reading structured frames and then only
    stderr left it saying nothing but that it had exited."""
    script = tmp_path / "expired.sh"
    script.write_text("#!/bin/sh\necho 'token expired'\nexit 1\n", encoding="utf-8")
    script.chmod(0o755)

    be = CliAgentBackend(name="plain", command=f"{script} {{prompt}}")
    with pytest.raises(RuntimeError, match="token expired"):
        await be.run("x", task_id="t7", workspace=tmp_path, executor=None)


def test_human_failure_falls_back_to_stderr_then_stdout_then_nothing() -> None:
    from raven.agent.subagent.backends.cli_agent import _human_failure

    assert _human_failure("", "boom: no such flag\n") == "boom: no such flag"
    # `transcript_format` defaults to `text`: such a command says what went wrong
    # on stdout and nothing on stderr, and stderr alone left it saying nothing.
    assert _human_failure("token expired\n", "") == "token expired"
    assert _human_failure("out says this\n", "err says this\n") == "err says this"
    # An unparsed frame is the dump this exists to keep out, so it is not a
    # fallback either -- silence is better.
    assert _human_failure('{"noise": "' + "x" * 500 + '"}\n', "") == ""
    assert _human_failure("", "") == ""
    # Capped, so no single enormous line can stand in for the dump.
    assert len(_human_failure("", "x" * 5000)) == 240


def test_a_bracketed_diagnostic_is_prose_not_a_frame() -> None:
    """`[ERROR] token expired` is what a great many CLIs print. Taking every
    leading bracket for a structured frame discarded exactly the sentence this
    module exists to find, on both streams."""
    from raven.agent.subagent.backends.cli_agent import _human_failure

    assert _human_failure("[ERROR] token expired\n", "") == "[ERROR] token expired"
    assert _human_failure("", "[ERROR] token expired\n") == "[ERROR] token expired"
    # A timestamp prefix parses no better, and is no less a sentence.
    assert _human_failure("[2026-08-22T10:11:12] disk full\n", "") == "[2026-08-22T10:11:12] disk full"
    # A real array frame still is one: it parses, and an unread frame is the
    # dump this exists to keep out.
    assert _human_failure('[{"result": "boom"}]\n', "") == ""
    # And the protection that motivated the bracket test in the first place: an
    # object opener is structured output whether or not it parses, because
    # nothing prints prose that starts that way.
    assert _human_failure('{"result": "half a fr\n', "") == ""


def test_human_failure_is_one_line_even_when_the_field_is_not() -> None:
    """A structured field carries whatever the agent put in it. Returned as-is,
    the exception became three conversation lines despite the one-line promise."""
    from raven.agent.subagent.backends.cli_agent import _human_failure

    said = _human_failure('{"result": "first line\\nsecond line\\n\\tthird"}\n', "")
    assert said == "first line second line third"
    assert "\n" not in _human_failure("", "one\ntwo\n")


async def test_cli_backend_timeout_raises(tmp_path: Path) -> None:
    be = CliAgentBackend(name="slow", command="sleep 5", timeout=1)
    with pytest.raises(RuntimeError):
        await be.run("x", task_id="t5", workspace=tmp_path, executor=None)


# --- OpenAI-API backend (against a local stub) ---------------------------


def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def test_openai_backend_against_stub(tmp_path: Path) -> None:
    async def handler(request: web.Request) -> web.Response:
        body = await request.json()
        assert body["model"] == "mirothinker-1"
        assert body["messages"][-1]["content"] == "solve it"
        return web.json_response({"choices": [{"message": {"content": "stub answer"}}]})

    port = _free_port()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        be = OpenAIApiBackend(name="mirothinker", base_url=f"http://127.0.0.1:{port}/v1", model="mirothinker-1")
        out = await be.run("solve it", task_id="t6", workspace=tmp_path, executor=None)
        assert out == "stub answer"
    finally:
        await runner.cleanup()


async def test_openai_backend_requests_non_streaming(tmp_path: Path) -> None:
    # A provider may default to SSE when `stream` is omitted (mirothinker does),
    # which no JSON decoder can read. The request must opt out explicitly.
    async def handler(request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("stream") is not False:
            return web.Response(
                text='data: {"choices":[{"delta":{"content":"chunk"}}]}\n\ndata: [DONE]\n\n',
                content_type="text/event-stream",
            )
        return web.json_response({"choices": [{"message": {"content": "stub answer"}}]})

    port = _free_port()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    try:
        be = OpenAIApiBackend(name="mirothinker", base_url=f"http://127.0.0.1:{port}/v1", model="mirothinker-1")
        out = await be.run("solve it", task_id="t7", workspace=tmp_path, executor=None)
        assert out == "stub answer"
    finally:
        await runner.cleanup()


async def test_openai_backend_honors_env_proxy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # The only route to the target is the proxy: the base_url port is closed, so
    # a direct connection is refused. Reaching the stub proves the backend read
    # http_proxy from the environment -- aiohttp ignores it by default, and on a
    # host whose egress to the provider is blocked that silently becomes an
    # upstream error rather than a connection failure.
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"choices": [{"message": {"content": "via proxy"}}]})

    proxy_port = _free_port()
    dead_port = _free_port()
    app = web.Application()
    app.router.add_post("/{tail:.*}", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", proxy_port)
    await site.start()
    for var in ("no_proxy", "NO_PROXY", "https_proxy", "HTTPS_PROXY", "HTTP_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("http_proxy", f"http://127.0.0.1:{proxy_port}")
    try:
        be = OpenAIApiBackend(name="mirothinker", base_url=f"http://127.0.0.1:{dead_port}/v1", model="mirothinker-1")
        out = await be.run("solve it", task_id="t8", workspace=tmp_path, executor=None)
        assert out == "via proxy"
    finally:
        await runner.cleanup()


# --- manager wiring ------------------------------------------------------


class _FakeProvider:
    def get_default_model(self) -> str:
        return "fake-model"


def _stub_agents(mgr: SubagentManager, backends: dict[str, Any]) -> None:
    """Put stand-in backends on the manager's table, under real rows.

    A row has to exist for the name to resolve: the manager refuses to dispatch to
    a name the table does not hold rather than substituting the in-process loop, so
    a bare ``registry._backends[name] = stub`` is unreachable. Registering a cli row
    per name and then swapping the backend it built keeps the stub on the same code
    path a configured agent takes. Applied in one call because ``apply_agents``
    rebuilds the whole table, which would discard stubs swapped in earlier.
    """
    mgr.apply_agents([ThirdPartyCliSubagentConfig(name=name, command="cat {prompt}") for name in backends])
    mgr.registry._backends.update(backends)


def _mgr(tmp_path: Path, third_party=None, **kwargs) -> SubagentManager:
    return SubagentManager(
        provider=_FakeProvider(),
        workspace=tmp_path,
        model="fake-model",
        agents=third_party or [],
        **kwargs,
    )


def test_manager_resolves_backends(tmp_path: Path) -> None:
    cli = ThirdPartyCliSubagentConfig(name="claude_code", command="claude -p {prompt}", description="Claude Code")
    oa = ThirdPartyOpenAISubagentConfig(name="mirothinker", base_url="http://x/v1", model="m")
    mgr = _mgr(tmp_path, [cli, oa])

    assert isinstance(mgr._resolve_backend("claude_code"), CliAgentBackend)
    assert isinstance(mgr._resolve_backend("mirothinker"), OpenAIApiBackend)
    # A built-in row resolves to an in-process loop, and it is reached by name
    # like every other agent.
    assert mgr._resolve_backend(GENERIC_AGENT) is not None
    # An unknown name raises rather than falling back. Substituting the in-process
    # loop answered *as* the agent the caller asked for, with none of its history
    # and no sign to anyone that a substitution happened.
    with pytest.raises(RuntimeError, match="not on the agent table"):
        mgr._resolve_backend("nope")
    names = [a.name for a in mgr.list_agents()]
    # The built-in rows lead (package seeds first), then config order.
    assert names[-2:] == ["claude_code", "mirothinker"]
    assert GENERIC_AGENT in names


def test_manager_lists_stateful_flag(tmp_path: Path) -> None:
    stateless = ThirdPartyCliSubagentConfig(name="codex", command="codex exec {prompt}")
    stateful = ThirdPartyCliSubagentConfig(
        name="claude_code",
        command="claude -p {prompt} --session-id {agent_id}",
        resume_command="claude -p {prompt} --resume {agent_id}",
    )
    mgr = _mgr(tmp_path, [stateless, stateful])
    external = [m for m in mgr.list_agents() if m.name in {"codex", "claude_code"}]
    assert external == [
        AgentMeta("codex", "", False, True),
        AgentMeta("claude_code", "", True, True),
    ]


def test_manager_skips_bad_third_party_entry(tmp_path: Path) -> None:
    class _Bad:
        name = "bad"
        kind = "nope"

    mgr = _mgr(tmp_path, [_Bad()])
    # The bad entry is skipped and the manager still builds -- with the package's
    # own rows, which do not come from config and so cannot be sunk by it.
    assert "bad" not in [a.name for a in mgr.list_agents()]
    assert GENERIC_AGENT in [a.name for a in mgr.list_agents()]


class TestSharedRosterHelpers:
    """One definition of the roster, shared by spawn and run_subagent_dag —
    a split one would let the two tools disagree about the same agent."""

    def test_meta_reads_name_description_and_capabilities(self) -> None:
        stateful = ThirdPartyCliSubagentConfig(
            name="claude_code",
            description="Claude Code",
            command="claude -p {prompt} --session-id {agent_id}",
            resume_command="claude -p {prompt} --resume {agent_id}",
        )
        stateless = ThirdPartyCliSubagentConfig(name="codex", command="codex exec {prompt}")
        boxed = ThirdPartyCliSubagentConfig(name="boxed", command="cat", reads_local_files=False)
        remote = ThirdPartyOpenAISubagentConfig(name="custom_http", base_url="http://x", model="m")
        assert third_party_agent_meta(stateful) == AgentMeta("claude_code", "Claude Code", True, True, False)
        assert third_party_agent_meta(stateless) == AgentMeta("codex", "", False, True, False)
        assert third_party_agent_meta(boxed) == AgentMeta("boxed", "", False, False, False)
        # An HTTP endpoint is remote by default, so it reads no local path -- and
        # stateful by default, because raven's own replay backs every endpoint.
        assert third_party_agent_meta(remote) == AgentMeta("custom_http", "", True, False, False)

    def test_listing_degrades_to_the_bare_name_without_a_description(self) -> None:
        listing = format_agent_listing([AgentMeta("a", "does A", False, True), AgentMeta("b", "", True, False)])
        assert listing == (
            "a [stateless, local-files, no-progress] (does A); b [stateful, no-local-files, no-progress]"
        )

    def test_listing_renders_both_capabilities_for_every_agent(self) -> None:
        """A capability the roster leaves out reads the same as one it denies,
        and the DAG pre-check rejects graphs on exactly these two facts."""
        for meta, expected in (
            (AgentMeta("x", "", True, True), "x [stateful, local-files, no-progress]"),
            (AgentMeta("x", "", True, False), "x [stateful, no-local-files, no-progress]"),
            (AgentMeta("x", "", False, True), "x [stateless, local-files, no-progress]"),
            (AgentMeta("x", "", False, False), "x [stateless, no-local-files, no-progress]"),
        ):
            assert format_agent_listing([meta]) == expected

    def test_listing_drops_nameless_entries_and_empties(self) -> None:
        assert format_agent_listing([AgentMeta("", "orphan", False, True)]) == ""
        assert format_agent_listing([]) == ""


def test_enabled_defaults_to_true_on_both_kinds() -> None:
    # The default is what keeps an existing config.json working untouched: every
    # entry already on disk predates this field.
    cli = ThirdPartyCliSubagentConfig(name="c", command="echo {prompt}")
    api = ThirdPartyOpenAISubagentConfig(name="a", base_url="http://x/v1", model="m")
    assert cli.enabled is True
    assert api.enabled is True


def test_enabled_third_party_filters_only_disabled() -> None:
    from raven.agent.subagent.backends import enabled_third_party

    on = ThirdPartyCliSubagentConfig(name="on", command="echo {prompt}")
    off = ThirdPartyCliSubagentConfig(name="off", command="echo {prompt}", enabled=False)
    assert [c.name for c in enabled_third_party([on, off])] == ["on"]
    assert enabled_third_party([]) == []

    # An object with no `enabled` attribute counts as enabled, so a caller passing
    # something other than a validated config cannot silently lose agents.
    class _Bare:
        name = "bare"

    bare = _Bare()
    assert enabled_third_party([bare]) == [bare]


def test_manager_roster_omits_a_disabled_agent(tmp_path: Path) -> None:
    on = ThirdPartyCliSubagentConfig(name="on", command="echo {prompt}")
    off = ThirdPartyCliSubagentConfig(name="off", command="echo {prompt}", enabled=False)
    mgr = _mgr(tmp_path, [on, off])
    names = [m.name for m in mgr.list_agents()]
    assert "on" in names and "off" not in names


def test_spawn_tool_listing_omits_a_disabled_agent(tmp_path: Path) -> None:
    # This is the assertion that actually protects the tool description the model
    # reads: a disabled agent must not appear in the roster it chooses from.
    on = ThirdPartyCliSubagentConfig(name="on", description="stays", command="echo {prompt}")
    off = ThirdPartyCliSubagentConfig(name="off", description="goes", command="echo {prompt}", enabled=False)
    mgr = _mgr(tmp_path, [on, off])
    listing = format_agent_listing(mgr.list_agents())
    assert "on" in listing
    assert "off" not in listing


def test_dag_tool_roster_omits_a_disabled_agent(tmp_path: Path) -> None:
    from raven.agent.subagent_dag.tool import SubAgentDagTool

    on = ThirdPartyCliSubagentConfig(name="on", command="echo {prompt}")
    off = ThirdPartyCliSubagentConfig(name="off", command="echo {prompt}", enabled=False)
    tool = SubAgentDagTool(workspace=tmp_path, agents=[on, off])
    names = tool.registry.names()
    assert "on" in names and "off" not in names


def test_dag_tool_with_every_configured_agent_disabled_keeps_the_built_in_rows(tmp_path: Path) -> None:
    # Switching off every configured agent used to leave the roster empty. It
    # cannot now: the built-in rows are package seeds rather than config, so the
    # graph tool always has something to dispatch to.
    from raven.agent.subagent_dag.tool import SubAgentDagTool

    off = ThirdPartyCliSubagentConfig(name="off", command="echo {prompt}", enabled=False)
    tool = SubAgentDagTool(workspace=tmp_path, agents=[off])
    assert "off" not in tool.registry.names()
    assert GENERIC_AGENT in tool.registry.names()


# --- AgentLoop's run_subagent_dag registration gate -----------------------


class _StubLoopProvider:
    """The bare surface AgentLoop touches during construction; chat is never
    invoked by these registration-gate tests."""

    def get_default_model(self) -> str:
        return "stub"


def _make_agent_loop(tmp_path: Path, third_party: list | None = None):
    from raven.agent.loop import AgentLoop

    return AgentLoop(
        provider=_StubLoopProvider(),
        workspace=tmp_path,
        model="stub",
        max_iterations=2,
        restrict_to_workspace=True,
        agents=third_party,
    )


def test_dag_tool_is_registered_even_with_every_configured_agent_disabled(tmp_path: Path) -> None:
    # The old gate withheld the tool when the roster would be empty. The roster is
    # never empty now -- the built-in rows are always on the table -- so the gate
    # would only be withholding graph orchestration from every default install.
    off = ThirdPartyCliSubagentConfig(name="off", command="echo {prompt}", enabled=False)
    loop = _make_agent_loop(tmp_path, [off])
    assert loop.tools.get("run_subagent_dag") is not None


def test_dag_tool_registered_when_at_least_one_agent_is_enabled(tmp_path: Path) -> None:
    on = ThirdPartyCliSubagentConfig(name="on", command="echo {prompt}")
    off = ThirdPartyCliSubagentConfig(name="off", command="echo {prompt}", enabled=False)
    loop = _make_agent_loop(tmp_path, [on, off])
    assert loop.tools.get("run_subagent_dag") is not None


def test_a_hot_apply_refreshes_one_table_that_both_consumers_read(tmp_path: Path) -> None:
    # The point of the shared registry: applying once cannot leave the spawn
    # manager and the graph tool disagreeing, which two independently-refreshed
    # maps could.
    loop = _make_agent_loop(tmp_path, [])
    on = ThirdPartyCliSubagentConfig(name="on", command="echo {prompt}")
    loop.apply_agents([on])
    tool = loop.tools.get("run_subagent_dag")
    assert "on" in [m.name for m in loop.subagents.list_agents()]
    assert "on" in tool.registry.names()
    assert tool.registry is loop.subagents.registry


def test_apply_agents_keeps_the_dag_tool_registered(tmp_path: Path) -> None:
    loop = _make_agent_loop(tmp_path, [])
    on = ThirdPartyCliSubagentConfig(name="on", command="echo {prompt}")
    loop.apply_agents([on])
    assert loop.tools.get("run_subagent_dag") is not None


@pytest.mark.parametrize("registered_at", ["construction", "hot_apply"])
def test_the_registered_dag_tool_is_wired_to_the_subagent_lifecycle(tmp_path: Path, registered_at: str) -> None:
    """A backgrounded run is only bounded, stoppable, reportable and pausable
    because the host handed the tool these five hooks. A construction site that
    forgets one loses a guarantee silently -- an unbudgeted run, one `/stop`
    cannot reach, one whose result reaches nobody, or a graph that keeps fanning
    out behind a paused HUD -- and there are two such sites.
    """
    on = ThirdPartyCliSubagentConfig(name="on", command="echo {prompt}")
    if registered_at == "construction":
        loop = _make_agent_loop(tmp_path, [on])
    else:
        loop = _make_agent_loop(tmp_path, [])
        loop.apply_agents([on])

    tool = loop.tools.get("run_subagent_dag")
    mgr = loop.subagents
    assert tool._gate is mgr.dispatch_gate, "DAG nodes must share the spawn concurrency gate"
    assert tool._charge == mgr.charge_dag_run
    assert tool._adopt == mgr.adopt_background_run
    assert tool._announce == mgr.announce_dag_result
    # A lambda reading through to the manager's flag, so this asserts the read
    # rather than the identity a comparison could not see.
    mgr.set_paused(True)
    assert tool._is_paused is not None and tool._is_paused() is True
    mgr.set_paused(False)
    assert tool._is_paused() is False


# --- spawn tool ----------------------------------------------------------


def test_spawn_tool_exposes_agent_param_when_configured(tmp_path: Path) -> None:
    cli = ThirdPartyCliSubagentConfig(name="claude_code", command="claude -p {prompt}", description="Claude Code")
    tool = SpawnTool(manager=_mgr(tmp_path, [cli]))
    params = tool.parameters
    assert "subagent" in params["properties"]
    # The enum is the whole table: the built-in agents are choices too, which is
    # the point of them being on it.
    assert "claude_code" in params["properties"]["subagent"]["enum"]
    assert GENERIC_AGENT in params["properties"]["subagent"]["enum"]
    # And required, so every spawn names its agent rather than falling into a
    # default the model was never told about.
    assert "subagent" in params["required"]
    assert "claude_code" in tool.description


def test_spawn_tool_offers_the_builtin_rows_with_no_config_at_all(tmp_path: Path) -> None:
    """There is no such thing as an empty roster now.

    The parameter used to be omitted when no third-party agent was configured,
    which left the model unable to name -- or even see -- the built-in agents it
    was in fact dispatching to by omission.
    """
    tool = SpawnTool(manager=_mgr(tmp_path, []))
    params = tool.parameters
    assert GENERIC_AGENT in params["properties"]["subagent"]["enum"]
    assert "subagent" in params["required"]


def test_spawn_tool_points_at_the_dag_when_a_roster_exists(tmp_path: Path) -> None:
    """Repeated spawns for one task are the shape a DAG expresses, and this
    description is where the model is standing when it makes that mistake."""
    cli = ThirdPartyCliSubagentConfig(name="claude_code", command="claude -p {prompt}")
    assert "run_subagent_dag" in SpawnTool(manager=_mgr(tmp_path, [cli])).description


def test_spawn_tool_points_at_the_dag_even_with_no_configured_agent(tmp_path: Path) -> None:
    """The pointer used to be withheld when the roster was empty, because the DAG
    tool was registered off that same roster and would not exist. Both are now
    unconditional -- the built-in rows are always on the table -- so the advice is
    always about a tool the model can actually call."""
    assert "run_subagent_dag" in SpawnTool(manager=_mgr(tmp_path, [])).description


def test_spawn_tool_prefers_a_specialist_over_doing_the_work_itself(tmp_path: Path) -> None:
    """The roster alone does not say when to reach for it, and the observed
    failure is the agent researching or building inline while a specialist that
    does exactly that job sits unused on the table."""
    cli = ThirdPartyCliSubagentConfig(
        name="Scribe",
        command="scribe {prompt}",
        description="turns a source document into a deck",
    )
    desc = SpawnTool(manager=_mgr(tmp_path, [cli])).description
    assert "Prefer delegation over doing it yourself" in desc
    assert "your own tools" in desc
    # The generic row is on the roster too, and counting it as a specialist
    # would satisfy the rule without ever dispatching to one.
    assert f"`{GENERIC_AGENT}` is not a specialist" in desc


def test_spawn_tool_omits_the_delegation_preference_when_no_specialist_exists(tmp_path: Path) -> None:
    """With nothing configured the table still holds the generic row, so the
    sentence would name a specialist the model cannot pick."""
    desc = SpawnTool(manager=_mgr(tmp_path, [])).description
    assert GENERIC_AGENT in desc
    assert "Prefer delegation" not in desc
    assert "is not a specialist" not in desc


def test_spawn_tool_states_the_preference_between_the_roster_and_the_dag_pointer(tmp_path: Path) -> None:
    """Order is the whole point: a rule about which agent to name has to sit
    with the roster it applies to, and ahead of the pointer that sends the
    model to a different tool."""
    cli = ThirdPartyCliSubagentConfig(name="Scribe", command="scribe {prompt}", description="builds decks")
    desc = SpawnTool(manager=_mgr(tmp_path, [cli])).description
    assert desc.index("choose deliberately from") < desc.index("Prefer delegation")
    assert desc.index("Prefer delegation") < desc.index("run_subagent_dag")


async def test_spawn_tool_forwards_agent(tmp_path: Path) -> None:
    cli = ThirdPartyCliSubagentConfig(name="claude_code", command="cat")
    mgr = _mgr(tmp_path, [cli])
    captured = {}

    async def fake_spawn(**kwargs):
        captured.update(kwargs)
        return "started"

    mgr.spawn = fake_spawn  # type: ignore[method-assign]
    tool = SpawnTool(manager=mgr)
    await tool.execute(task="do it", subagent="claude_code")
    assert captured["agent"] == "claude_code"
    assert captured["task"] == "do it"


def test_spawn_tool_always_exposes_the_instance_param(tmp_path: Path) -> None:
    """The default sub-agent is resumable per handle whatever the roster holds, so
    the parameter is always offered and the description names who may receive it.
    This assertion used to read `not in` for a roster with nothing stateful, which
    left an empty-roster install minting handles the model was never able to pass
    back.
    """
    for roster in ([], [ThirdPartyCliSubagentConfig(name="codex", command="codex exec {prompt}")]):
        props = SpawnTool(manager=_mgr(tmp_path, roster)).parameters["properties"]
        assert "instance" in props
        assert props["instance"]["type"] == "string"
        # The built-in rows are stateful (raven replays their message list), so a
        # handle is always offered for at least those.
        assert GENERIC_AGENT in props["instance"]["description"]
        # `codex` has no resumeCommand, so it is not among the names offered one.
        assert "codex" not in props["instance"]["description"]

    stateful = ThirdPartyCliSubagentConfig(
        name="claude_code",
        command="claude -p {prompt} --session-id {agent_id}",
        resume_command="claude -p {prompt} --resume {agent_id}",
    )
    props = SpawnTool(manager=_mgr(tmp_path, [stateful])).parameters["properties"]
    assert "instance" in props
    assert props["instance"]["type"] == "string"
    # With a mixed roster the param names who else may receive it, beside the default.
    assert "claude_code" in props["instance"]["description"]


class TestSpawnInstanceGate:
    """`run_subagent_dag` refuses a handle a sub-agent cannot honour; the one-call
    surface has to refuse it too, or the same mistake stays silent here — the
    backend ignores the handle and the reply reads as the sub-agent forgetting."""

    @staticmethod
    def _tool(tmp_path: Path) -> SpawnTool:
        return SpawnTool(
            manager=_mgr(
                tmp_path,
                [
                    ThirdPartyCliSubagentConfig(name="codex", command="codex exec {prompt}"),
                    ThirdPartyCliSubagentConfig(
                        name="claude_code",
                        command="claude -p {prompt} --session-id {agent_id}",
                        resume_command="claude -p {prompt} --resume {agent_id}",
                    ),
                ],
            )
        )

    async def test_handle_on_a_stateless_agent_spawns_nothing(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path)
        spawned: list[dict] = []
        tool._manager.spawn = lambda **kw: spawned.append(kw)  # type: ignore[method-assign]

        out = await tool.execute(task="do it", subagent="codex", instance="author")

        assert out.startswith("Error:")
        assert "stateless" in out
        assert "claude_code" in out  # who can, not just who cannot
        assert spawned == []

    async def test_handle_without_an_agent_is_forwarded(self, tmp_path: Path) -> None:
        """A default Raven subagent is resumable now (its transcript is
        persisted per handle), so a handle with no `agent` named is
        forwarded like any other, not rejected."""
        tool = self._tool(tmp_path)
        captured: dict = {}

        async def fake_spawn(**kwargs):
            captured.update(kwargs)
            return "started"

        tool._manager.spawn = fake_spawn  # type: ignore[method-assign]
        out = await tool.execute(task="do it", instance="author")

        assert out == "started"
        assert captured["instance"] == "author"

    async def test_handle_on_a_stateful_agent_is_forwarded(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path)
        captured: dict = {}

        async def fake_spawn(**kwargs):
            captured.update(kwargs)
            return "started"

        tool._manager.spawn = fake_spawn  # type: ignore[method-assign]
        out = await tool.execute(task="do it", subagent="claude_code", instance="author")

        assert out == "started"
        assert captured["instance"] == "author"

    async def test_no_handle_is_never_gated(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path)
        captured: dict = {}

        async def fake_spawn(**kwargs):
            captured.update(kwargs)
            return "started"

        tool._manager.spawn = fake_spawn  # type: ignore[method-assign]
        assert await tool.execute(task="do it", subagent="codex") == "started"
        assert captured["instance"] is None


async def test_manager_forwards_instance_to_backend(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    class _Recorder:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None, **_):
            seen["session_key"] = session_key
            seen["instance"] = instance
            return "ok"

    mgr = _mgr(tmp_path, [])
    _stub_agents(mgr, {"rec": _Recorder()})
    mgr.set_submit(lambda req: None)
    await mgr.spawn("t", session_key="web:s1", agent="rec", instance="refactor-auth")
    for task in list(mgr._running_tasks.values()):
        await task
    assert seen == {"session_key": "web:s1", "instance": "refactor-auth"}


async def test_spawn_marks_a_minted_instance_as_automatic(tmp_path: Path) -> None:
    cli = ThirdPartyCliSubagentConfig(
        name="claude_code", command="cat {agent_id}", resume_command="cat --resume {agent_id}"
    )
    mgr = _mgr(tmp_path, [cli])
    captured: dict[str, Any] = {}

    async def fake_spawn(**kwargs):
        captured.update(kwargs)
        return "started"

    mgr.spawn = fake_spawn  # type: ignore[method-assign]
    tool = SpawnTool(manager=mgr)
    await tool.execute(task="do it", subagent="claude_code")
    assert captured["instance_auto"] is True
    await tool.execute(task="do it", subagent="claude_code", instance="author")
    assert captured["instance_auto"] is False


async def test_spawn_hands_the_minted_handle_to_the_turn_stream(tmp_path: Path) -> None:
    cli = ThirdPartyCliSubagentConfig(
        name="claude_code", command="cat {agent_id}", resume_command="cat --resume {agent_id}"
    )
    mgr = _mgr(tmp_path, [cli])

    async def fake_spawn(**kwargs):
        return "started"

    mgr.spawn = fake_spawn  # type: ignore[method-assign]
    tool = SpawnTool(manager=mgr)
    await tool.execute(task="do it", label="Refactor Auth", subagent="claude_code")
    meta = tool.take_metadata()
    assert re.fullmatch(r"refactor-auth-[0-9a-f]{6}", meta["instance"])
    assert meta["instance_auto"] is True
    assert tool.take_metadata() is None


async def test_spawn_publishes_no_metadata_for_a_stateless_agent(tmp_path: Path) -> None:
    cli = ThirdPartyCliSubagentConfig(name="oneshot", command="cat")
    mgr = _mgr(tmp_path, [cli])

    async def fake_spawn(**kwargs):
        return "started"

    mgr.spawn = fake_spawn  # type: ignore[method-assign]
    tool = SpawnTool(manager=mgr)
    await tool.execute(task="do it", subagent="oneshot")
    assert tool.take_metadata() is None


async def test_spawn_does_not_leak_an_uncollected_handle_to_a_later_stateless_call(
    tmp_path: Path,
) -> None:
    """A stateless call must not pop and report an earlier stateful call's
    handle just because nobody collected it (e.g. no tool-event sink)."""
    stateful = ThirdPartyCliSubagentConfig(
        name="claude_code", command="cat {agent_id}", resume_command="cat --resume {agent_id}"
    )
    stateless = ThirdPartyCliSubagentConfig(name="oneshot", command="cat")
    mgr = _mgr(tmp_path, [stateful, stateless])

    async def fake_spawn(**kwargs):
        return "started"

    mgr.spawn = fake_spawn  # type: ignore[method-assign]
    tool = SpawnTool(manager=mgr)
    await tool.execute(task="do it", subagent="claude_code")
    await tool.execute(task="do it", subagent="oneshot")
    assert tool.take_metadata() is None


async def test_spawn_publishes_no_handle_for_a_call_the_manager_refused(tmp_path: Path) -> None:
    """A refusal comes back as the result, not as an exception, so publishing the
    handle before the call meant every refused spawn still announced one -- an
    instance row and a `new` badge for a run that never started. Minting is what
    makes that reach unnamed calls too, i.e. every refused spawn.
    """
    mgr = _mgr(tmp_path, [])
    mgr.set_paused(True)
    tool = SpawnTool(manager=mgr)

    result = await tool.execute(task="summarize the log")

    assert result.startswith(SPAWN_REFUSED_PREFIX)
    assert tool.take_metadata() is None


async def test_spawn_publishes_the_handle_once_the_manager_takes_the_call(tmp_path: Path) -> None:
    """The positive half, so the refusal test above cannot pass by publishing
    nothing at all."""
    mgr = _mgr(tmp_path, [])

    async def fake_spawn(**kwargs):
        return "Spawned task abc12345"

    mgr.spawn = fake_spawn  # type: ignore[method-assign]
    tool = SpawnTool(manager=mgr)

    await tool.execute(task="summarize the log")

    meta = tool.take_metadata()
    assert meta is not None
    assert meta["instance"].startswith("summarize-the-log-")


# --- presets -------------------------------------------------------------


# --- transcript parsing --------------------------------------------------

from raven.agent.subagent.backends.transcript import (
    delta_reader,
    parse_claude_stream_json,
    parse_claude_stream_json_delta,
    parse_codex_jsonl,
    parse_openclaw_json,
    parse_opencode_json,
)


def test_parse_codex_jsonl_extracts_thread_and_last_reply() -> None:
    stdout = "\n".join(
        [
            '{"type":"thread.started","thread_id":"th_1"}',
            "not json at all",
            '{"type":"item.completed","item":{"type":"reasoning","text":"ignored"}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"first"}}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"final"}}',
        ]
    )
    assert parse_codex_jsonl(stdout) == ("th_1", "final")


def test_parse_codex_jsonl_empty_is_all_none() -> None:
    assert parse_codex_jsonl("") == (None, None)


def test_parse_claude_stream_json_extracts_result() -> None:
    stdout = "\n".join(
        [
            '{"type":"system","subtype":"hook_started","session_id":"sess-1"}',
            '{"type":"assistant","message":{"content":[]},"session_id":"sess-1"}',
            '{"type":"result","subtype":"success","is_error":false,"result":"OK","session_id":"sess-1"}',
        ]
    )
    assert parse_claude_stream_json(stdout) == ("sess-1", "OK", False)


def test_parse_claude_stream_json_flags_is_error() -> None:
    stdout = '{"type":"result","is_error":true,"result":"boom","session_id":"s"}'
    assert parse_claude_stream_json(stdout) == ("s", "boom", True)


def test_parse_claude_stream_json_without_result_event() -> None:
    # Session id still recoverable; no reply means the caller falls back to raw stdout.
    stdout = '{"type":"system","subtype":"init","session_id":"sess-2"}'
    assert parse_claude_stream_json(stdout) == ("sess-2", None, False)


def test_parse_claude_stream_json_delta_reads_one_text_chunk() -> None:
    line = (
        '{"type":"stream_event","event":{"type":"content_block_delta","index":0,'
        '"delta":{"type":"text_delta","text":"he"}},"session_id":"s","parent_tool_use_id":null}'
    )
    assert parse_claude_stream_json_delta(line) == "he"


@pytest.mark.parametrize(
    "line",
    [
        # The finished text, repeated by the same stream twice more.
        '{"type":"assistant","message":{"content":[{"type":"text","text":"whole"}]}}',
        '{"type":"result","is_error":false,"result":"whole"}',
        # Not reply text.
        '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"thinking_delta","thinking":"h"}}}',
        '{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"input_json_delta","text":"{"}}}',
        # A nested agent's output, which `result` does not carry either.
        '{"type":"stream_event","event":{"type":"content_block_delta",'
        '"delta":{"type":"text_delta","text":"x"}},"parent_tool_use_id":"toolu_1"}',
        # Frame kinds with no text at all.
        '{"type":"stream_event","event":{"type":"message_stop"}}',
        '{"type":"system","subtype":"init","session_id":"s"}',
        # This transport interleaves plain diagnostics with its transcript.
        "[plugins] loading",
        "",
    ],
)
def test_parse_claude_stream_json_delta_reads_nothing_else(line: str) -> None:
    assert parse_claude_stream_json_delta(line) == ""


def test_delta_reader_is_absent_for_a_format_with_no_partial_events() -> None:
    """Measured on ``codex exec --json``: a whole reply arrives as one
    ``item.completed``, so there is no partial event to read."""
    assert delta_reader("claude_stream_json") is parse_claude_stream_json_delta
    assert delta_reader("codex_jsonl") is None
    assert delta_reader("opencode_json") is None
    assert delta_reader("text") is None
    assert delta_reader(None) is None


def test_parse_openclaw_json_extracts_reply_and_session_id() -> None:
    stdout = json.dumps(
        {
            "payloads": [{"text": "the answer", "mediaUrl": None}],
            "meta": {
                "agentMeta": {"sessionId": "86287eee-186d-498f-82b5-27875a25ee42"},
                "finalAssistantVisibleText": "the answer",
            },
        }
    )
    assert parse_openclaw_json(stdout) == ("86287eee-186d-498f-82b5-27875a25ee42", "the answer")


def test_parse_openclaw_json_falls_back_to_final_visible_text() -> None:
    # A delivery-only run can come back with no payload; the reply is still in meta.
    stdout = json.dumps({"payloads": [], "meta": {"finalAssistantVisibleText": "delivered"}})
    assert parse_openclaw_json(stdout) == (None, "delivered")


def test_parse_openclaw_json_survives_non_json() -> None:
    # Never raise into the run path: a diagnostic-only stdout yields no reply,
    # and _attempt then falls back to the raw combined output.
    assert parse_openclaw_json("openclaw: something went wrong") == (None, None)


def test_parse_opencode_json_returns_only_the_last_messages_text() -> None:
    # Shape captured from opencode 1.18.4 `run --format json`: every event carries
    # a top-level sessionID, and a run that calls a tool puts the tool under one
    # messageID and the answer under the next. Returning every text part would
    # prepend the earlier message's narration to the answer.
    stdout = "\n".join(
        [
            '{"type":"step_start","sessionID":"ses_1","part":{"messageID":"msg_a","type":"step-start"}}',
            '{"type":"text","sessionID":"ses_1","part":{"messageID":"msg_a","type":"text","text":"let me look"}}',
            '{"type":"tool_use","sessionID":"ses_1","part":{"messageID":"msg_a","type":"tool"}}',
            "not json at all",
            '{"type":"step_start","sessionID":"ses_1","part":{"messageID":"msg_b","type":"step-start"}}',
            '{"type":"text","sessionID":"ses_1","part":{"messageID":"msg_b","type":"text","text":"the answer"}}',
            '{"type":"step_finish","sessionID":"ses_1","part":{"messageID":"msg_b","type":"step-finish"}}',
        ]
    )
    assert parse_opencode_json(stdout) == ("ses_1", "the answer")


def test_parse_opencode_json_joins_text_parts_within_one_message() -> None:
    stdout = "\n".join(
        [
            '{"type":"text","sessionID":"ses_2","part":{"messageID":"m","type":"text","text":"first"}}',
            '{"type":"text","sessionID":"ses_2","part":{"messageID":"m","type":"text","text":"second"}}',
        ]
    )
    assert parse_opencode_json(stdout) == ("ses_2", "first\nsecond")


def test_parse_opencode_json_reads_session_id_off_the_part_too() -> None:
    stdout = '{"type":"text","part":{"sessionID":"ses_3","messageID":"m","type":"text","text":"hi"}}'
    assert parse_opencode_json(stdout) == ("ses_3", "hi")


def test_parse_opencode_json_survives_a_transcript_with_no_text() -> None:
    # A run killed before the model answered: the id is still worth recovering,
    # so the caller can resume rather than orphan the session.
    stdout = '{"type":"step_start","sessionID":"ses_4","part":{"messageID":"m","type":"step-start"}}'
    assert parse_opencode_json(stdout) == ("ses_4", None)


def test_parse_opencode_json_survives_non_json() -> None:
    assert parse_opencode_json("Error: Session not found") == (None, None)


async def test_cli_backend_derived_id_from_opencode_json(tmp_path: Path) -> None:
    cmd = _fixture_cmd(
        tmp_path,
        "opencode.jsonl",
        [
            '{"type":"step_start","sessionID":"ses_9","part":{"messageID":"m1","type":"step-start"}}',
            '{"type":"text","sessionID":"ses_9","part":{"messageID":"m1","type":"text","text":"done"}}',
        ],
    )
    be = CliAgentBackend(
        name="opencodefake",
        command=cmd,
        resume_command="printf resumed-%s {agent_id}",
        id_source="derived",
        transcript_format="opencode_json",
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    first = await be.run("task", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert first == "done"  # reply extracted, not the raw JSONL
    second = await be.run("task", task_id="t2", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert second == "resumed-ses_9"  # id was derived from the transcript and reused


async def test_cli_backend_openclaw_json_provisioned_round_trip(tmp_path: Path) -> None:
    # openclaw takes the caller's id, so create and resume are the same command
    # and the reply must come out of the JSON rather than the raw document.
    payload = json.dumps({"payloads": [{"text": "hello"}], "meta": {"agentMeta": {"sessionId": "ignored"}}})
    path = tmp_path / "openclaw.json"
    path.write_text(payload, encoding="utf-8")
    # `sh -c <script> -- {agent_id}` keeps {agent_id} literally in the command,
    # which `provisioned` requires, while making it an inert positional the
    # script never reads. Appending it to `cat` instead would name a second file
    # that does not exist, and cat exits 1 on that.
    cmd = f"sh -c 'cat {path}' -- " + "{agent_id}"
    be = CliAgentBackend(
        name="openclawfake",
        command=cmd,
        resume_command=cmd,
        id_source="provisioned",
        transcript_format="openclaw_json",
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    out = await be.run("task", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert out == "hello"


# --- stateful CLI schema validation ----------

from pydantic import ValidationError


def test_cli_config_stateless_rejects_agent_id() -> None:
    with pytest.raises(ValidationError):
        ThirdPartyCliSubagentConfig(name="x", command="claude -p {prompt} --session-id {agent_id}")


def test_cli_config_provisioned_requires_agent_id_in_command() -> None:
    with pytest.raises(ValidationError):
        ThirdPartyCliSubagentConfig(
            name="x",
            command="claude -p {prompt}",
            resume_command="claude -p {prompt} --resume {agent_id}",
            id_source="provisioned",
        )


def test_cli_config_derived_rejects_agent_id_in_command() -> None:
    with pytest.raises(ValidationError):
        ThirdPartyCliSubagentConfig(
            name="x",
            command="codex exec {prompt} --session {agent_id}",
            resume_command="codex exec resume {agent_id} {prompt}",
            id_source="derived",
        )


def test_cli_config_resume_requires_agent_id() -> None:
    with pytest.raises(ValidationError):
        ThirdPartyCliSubagentConfig(
            name="x",
            command="claude -p {prompt} --session-id {agent_id}",
            resume_command="claude -p {prompt}",
        )


def test_cli_config_valid_stateful_provisioned_round_trips_camel() -> None:
    cfg = ThirdPartyCliSubagentConfig(
        name="claude_code",
        command="claude -p {prompt} --session-id {agent_id}",
        resume_command="claude -p {prompt} --resume {agent_id}",
        transcript_format="claude_stream_json",
    )
    dumped = cfg.model_dump(by_alias=True)
    assert dumped["resumeCommand"] == "claude -p {prompt} --resume {agent_id}"
    assert dumped["idSource"] == "provisioned"
    assert dumped["transcriptFormat"] == "claude_stream_json"
    # camelCase is accepted on the way back in.
    assert ThirdPartyCliSubagentConfig(**dumped).resume_command == cfg.resume_command


# --- declared capabilities ----------


def test_stateful_declaration_must_agree_with_the_resume_mechanism() -> None:
    """The roster advertises statefulness and the DAG pre-check enforces it, so a
    declaration the mechanism cannot honour is rejected at write time rather than
    surfacing as a node that quietly restarts from scratch."""
    with pytest.raises(ValidationError):
        ThirdPartyCliSubagentConfig(name="x", command="cat {prompt}", stateful=True)
    with pytest.raises(ValidationError):
        ThirdPartyCliSubagentConfig(
            name="x",
            command="claude -p {prompt} --session-id {agent_id}",
            resume_command="claude -p {prompt} --resume {agent_id}",
            stateful=False,
        )


def test_stateful_declaration_that_matches_is_accepted() -> None:
    stateless = ThirdPartyCliSubagentConfig(name="x", command="cat {prompt}", stateful=False)
    stateful = ThirdPartyCliSubagentConfig(
        name="y",
        command="claude -p {prompt} --session-id {agent_id}",
        resume_command="claude -p {prompt} --resume {agent_id}",
        stateful=True,
    )
    assert third_party_agent_meta(stateless).stateful is False
    assert third_party_agent_meta(stateful).stateful is True


def test_http_agent_may_declare_itself_stateful() -> None:
    """Raven replays the message list itself for this kind, so stateful=True is honoured now."""
    cfg = ThirdPartyOpenAISubagentConfig(name="x", base_url="http://x", model="m", stateful=True)
    assert cfg.stateful is True


def test_local_file_access_defaults_by_kind_and_round_trips_camel() -> None:
    """A CLI agent is a local subprocess; an HTTP endpoint is presumed remote."""
    cli = ThirdPartyCliSubagentConfig(name="x", command="cat {prompt}")
    http = ThirdPartyOpenAISubagentConfig(name="y", base_url="http://x", model="m")
    assert cli.reads_local_files is True
    assert http.reads_local_files is False

    boxed = ThirdPartyCliSubagentConfig(name="x", command="cat {prompt}", reads_local_files=False)
    dumped = boxed.model_dump(by_alias=True)
    assert dumped["readsLocalFiles"] is False
    assert ThirdPartyCliSubagentConfig(**dumped).reads_local_files is False


# --- instance registry ---------------------------------------------------

from raven.agent.subagent.instances import InstanceRegistry


async def test_registry_commit_then_lookup(tmp_path: Path) -> None:
    reg = InstanceRegistry(path=tmp_path / "inst.json")
    assert await reg.lookup("web:s1", "claude_code", "refactor") is None
    await reg.commit("web:s1", "claude_code", "refactor", "sess-abc")
    assert await reg.lookup("web:s1", "claude_code", "refactor") == "sess-abc"
    # Scoped by session and by agent, not by handle alone.
    assert await reg.lookup("web:s2", "claude_code", "refactor") is None
    assert await reg.lookup("web:s1", "codex", "refactor") is None


async def test_registry_persists_across_instances(tmp_path: Path) -> None:
    path = tmp_path / "inst.json"
    await InstanceRegistry(path=path).commit("web:s1", "codex", "h", "th_9")
    assert await InstanceRegistry(path=path).lookup("web:s1", "codex", "h") == "th_9"


async def test_registry_list_filters_by_session(tmp_path: Path) -> None:
    reg = InstanceRegistry(path=tmp_path / "inst.json")
    await reg.commit("web:s1", "claude_code", "a", "id-a")
    await reg.commit("web:s2", "claude_code", "b", "id-b")
    handles = {r["handle"] for r in reg.list_instances("web:s1")}
    assert handles == {"a"}
    assert len(reg.list_instances()) == 2


async def test_registry_keeps_old_records(tmp_path: Path) -> None:
    # No time-based expiry: an instance stays resumable for as long as its
    # session exists, however long ago it was last used.
    path = tmp_path / "inst.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "instances": [
                    {
                        "sessionKey": "web:s1",
                        "agent": "claude_code",
                        "handle": "old",
                        "agentId": "x",
                        "createdAtMs": 1,
                        "updatedAtMs": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    reg = InstanceRegistry(path=path)
    assert [r["handle"] for r in reg.list_instances()] == ["old"]
    assert await reg.lookup("web:s1", "claude_code", "old") == "x"


async def test_registry_delete_session_removes_only_that_session(tmp_path: Path) -> None:
    path = tmp_path / "inst.json"
    reg = InstanceRegistry(path=path)
    await reg.commit("web:s1", "claude_code", "a", "id-a")
    await reg.commit("web:s1", "codex", "b", "id-b")
    await reg.commit("web:s2", "claude_code", "c", "id-c")

    assert await reg.delete_session("web:s1") == 2
    assert [r["handle"] for r in reg.list_instances()] == ["c"]
    # Deleting an unknown session is a no-op, not an error.
    assert await reg.delete_session("web:nope") == 0
    # The removal is persisted, not just cached.
    assert [r["handle"] for r in InstanceRegistry(path=path).list_instances()] == ["c"]


async def test_registry_tolerates_corrupt_file(tmp_path: Path) -> None:
    path = tmp_path / "inst.json"
    path.write_text("{ not json", encoding="utf-8")
    reg = InstanceRegistry(path=path)
    assert reg.list_instances() == []
    await reg.commit("web:s1", "codex", "h", "th_1")
    assert await reg.lookup("web:s1", "codex", "h") == "th_1"


async def test_registry_commit_survives_flush_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # On OSError from _flush, commit does not raise; the mapping stays live in-process
    # but persistence is lost (will not survive restart).
    reg = InstanceRegistry(path=tmp_path / "inst.json")

    def failing_flush() -> None:
        raise OSError("disk full")

    monkeypatch.setattr(reg, "_flush", failing_flush)
    # commit does not raise even though _flush raises.
    await reg.commit("web:s1", "claude_code", "h", "agent_123")
    # Mapping is live in-process.
    assert await reg.lookup("web:s1", "claude_code", "h") == "agent_123"


async def test_registry_upsert_dag_node_roundtrip(tmp_path: Path) -> None:
    reg = InstanceRegistry(path=tmp_path / "inst.json")
    await reg.upsert_dag_node("web:s1", "run-1", "analyze", "claude_code", "running")
    rows = reg.list_instances("web:s1")
    assert len(rows) == 1
    assert rows[0]["kind"] == "dag-node"
    assert rows[0]["handle"] == "run-1/analyze"
    assert rows[0]["runId"] == "run-1"
    assert rows[0]["nodeId"] == "analyze"
    assert rows[0]["status"] == "running"

    created = rows[0]["createdAtMs"]
    await reg.upsert_dag_node("web:s1", "run-1", "analyze", "claude_code", "completed")
    rows = reg.list_instances("web:s1")
    # An update, not a second row, and the creation time survives.
    assert len(rows) == 1
    assert rows[0]["status"] == "completed"
    assert rows[0]["createdAtMs"] == created


async def test_registry_dag_node_persists_and_coexists_with_cli(tmp_path: Path) -> None:
    path = tmp_path / "inst.json"
    reg = InstanceRegistry(path=path)
    await reg.commit("web:s1", "claude_code", "refactor", "sess-a")
    await reg.upsert_dag_node("web:s1", "run-1", "review", "codex", "running")

    reloaded = InstanceRegistry(path=path)
    kinds = {r["handle"]: r["kind"] for r in reloaded.list_instances("web:s1")}
    assert kinds == {"refactor": "cli", "run-1/review": "dag-node"}
    # The CLI lookup path must not see the DAG row.
    assert await reloaded.lookup("web:s1", "claude_code", "refactor") == "sess-a"


async def test_registry_legacy_record_without_kind_reads_as_cli(tmp_path: Path) -> None:
    path = tmp_path / "inst.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "instances": [
                    {
                        "sessionKey": "web:s1",
                        "agent": "claude_code",
                        "handle": "old",
                        "agentId": "x",
                        "createdAtMs": 1,
                        "updatedAtMs": 1,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    reg = InstanceRegistry(path=path)
    assert reg.list_instances()[0]["kind"] == "cli"
    assert await reg.lookup("web:s1", "claude_code", "old") == "x"


async def test_registry_delete_session_removes_dag_nodes_too(tmp_path: Path) -> None:
    reg = InstanceRegistry(path=tmp_path / "inst.json")
    await reg.commit("web:s1", "claude_code", "h", "sess-a")
    await reg.upsert_dag_node("web:s1", "run-1", "n", "codex", "running")
    await reg.upsert_dag_node("web:s2", "run-2", "n", "codex", "running")
    assert await reg.delete_session("web:s1") == 2
    assert [r["handle"] for r in reg.list_instances()] == ["run-2/n"]


# --- stateful CLI backend ------------------------------------------------


async def test_cli_backend_substitutes_provisioned_agent_id(tmp_path: Path) -> None:
    be = CliAgentBackend(
        name="idecho",
        command="printf %s {agent_id}",
        resume_command="printf resumed-%s {agent_id}",
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    first = await be.run("task", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    # Provisioned: raven minted a uuid and passed it through.
    assert first and first != "{agent_id}"
    second = await be.run("task", task_id="t2", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert second == f"resumed-{first}"


async def test_cli_backend_provisioned_id_is_a_uuid_whatever_the_handle(tmp_path: Path) -> None:
    # The handle is a registry key, never the CLI's session id. It falls back to
    # task_id (8 hex chars) when the caller omits `instance`, and claude rejects
    # a non-UUID --session-id outright, so the minted id must not derive from it.
    be = CliAgentBackend(
        name="idecho",
        command="printf %s {agent_id}",
        resume_command="printf resumed-%s {agent_id}",
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    minted = await be.run("t", task_id="deadbeef", workspace=tmp_path, executor=None, session_key="s")
    assert uuid.UUID(minted)  # raises ValueError if the handle leaked through
    assert minted != "deadbeef"


async def test_cli_backend_stateless_ignores_instance(tmp_path: Path) -> None:
    be = CliAgentBackend(name="pf", command="printf %s {prompt}")
    out = await be.run("hi", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert out == "hi"


async def test_one_stateful_handle_admits_one_run_at_a_time(tmp_path: Path) -> None:
    """A handle names one session inside the CLI's own store, so two runs
    resuming it at once interleave or corrupt that session.

    Two backend objects on purpose: the DAG tool and the sub-agent manager each
    build their own from the same config, so a lock held on a backend instance
    would not stop a spawn and a DAG node meeting on one handle. The nodes of a
    single DAG run are already serialized by the runner; this is the case that
    is not.
    """
    script = tmp_path / "slow.sh"
    script.write_text("sleep 0.2\nprintf ok\n", encoding="utf-8")
    live = 0
    peak = 0

    def _backend() -> CliAgentBackend:
        return CliAgentBackend(
            name="agent",
            command=f"sh {script}",
            resume_command=f"sh {script} {{agent_id}}",
            registry=InstanceRegistry(path=tmp_path / "inst.json"),
        )

    async def _run(be: CliAgentBackend, task_id: str) -> None:
        nonlocal live, peak
        original = be._attempt

        async def _counted(*a, **kw):
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            try:
                return await original(*a, **kw)
            finally:
                live -= 1

        be._attempt = _counted
        await be.run("hi", task_id=task_id, workspace=tmp_path, executor=None, session_key="s1", instance="author")

    await asyncio.gather(_run(_backend(), "t1"), _run(_backend(), "t2"))

    assert peak == 1, f"{peak} runs held the handle 'author' at once"


async def test_an_unnamed_handle_does_not_serialize_runs_that_share_nothing(tmp_path: Path) -> None:
    """Without an `instance` there is no session to protect, so no lock.

    The handle falls back to `task_id`, which for a DAG node is its
    author-chosen id -- so two graphs that both contain a node called
    `research` meet on one key while sharing nothing: neither resumes, each
    mints its own agent_id. Serializing them costs more than their own time,
    because a node holds its slot on the shared dispatch gate while it waits,
    so unrelated spawns queue behind a lock that guards nothing.
    """
    script = tmp_path / "slow.sh"
    script.write_text("sleep 0.2\nprintf ok\n", encoding="utf-8")
    live = 0
    peak = 0

    def _backend() -> CliAgentBackend:
        return CliAgentBackend(
            name="agent",
            command=f"sh {script}",
            resume_command=f"sh {script} {{agent_id}}",
            registry=InstanceRegistry(path=tmp_path / "inst.json"),
        )

    async def _run(be: CliAgentBackend) -> None:
        original = be._attempt

        async def _counted(*a: Any, **kw: Any) -> str:
            nonlocal live, peak
            live += 1
            peak = max(peak, live)
            try:
                return await original(*a, **kw)
            finally:
                live -= 1

        be._attempt = _counted
        await be.run("hi", task_id="research", workspace=tmp_path, executor=None, session_key="s1")

    await asyncio.gather(_run(_backend()), _run(_backend()))

    assert peak == 2, f"two unnamed runs on the id 'research' serialized (peak {peak})"


def _handle_probe_cmd(tmp_path: Path) -> str:
    """A stateful CLI that logs `<mode> <agent_id>` per invocation."""
    log = tmp_path / "invocations.log"
    script = tmp_path / "probe.sh"
    script.write_text(f'printf "%s %s\\n" "$1" "$2" >> {log}\nprintf ok\n', encoding="utf-8")
    return f"sh {script}"


def _invocations(tmp_path: Path) -> list[tuple[str, str]]:
    lines = (tmp_path / "invocations.log").read_text().strip().splitlines()
    return [(ln.split()[0], ln.split()[1]) for ln in lines]


async def test_a_run_without_a_named_instance_never_resumes_an_earlier_one(tmp_path: Path) -> None:
    """The handle falls back to ``task_id``, which for a DAG node is its
    author-chosen id -- so a second graph with a node called ``n`` would pick up
    the first graph's session having asked for nothing of the sort. Only an
    explicit ``instance`` may resume.
    """
    base = _handle_probe_cmd(tmp_path)
    be = CliAgentBackend(
        name="writer",
        command=f"{base} CREATE {{agent_id}}",
        resume_command=f"{base} RESUME {{agent_id}}",
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    for _ in range(2):
        await be.run("hi", task_id="n", workspace=tmp_path, executor=None, session_key="web:s1")

    modes = [mode for mode, _ in _invocations(tmp_path)]
    ids = {agent_id for _, agent_id in _invocations(tmp_path)}
    assert modes == ["CREATE", "CREATE"]
    assert len(ids) == 2, "the second run inherited the first run's session"
    # The binding is still recorded: it is what says which session a node ran
    # in, and the web monitor reads it by `<agent>/<node id>`.
    assert await be._registry.lookup("web:s1", "writer", "n") is not None


async def test_a_named_instance_still_resumes(tmp_path: Path) -> None:
    """The other half of the rule: naming a handle is how a caller asks for the
    session to carry over, and that must keep working."""
    base = _handle_probe_cmd(tmp_path)
    be = CliAgentBackend(
        name="writer",
        command=f"{base} CREATE {{agent_id}}",
        resume_command=f"{base} RESUME {{agent_id}}",
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    for _ in range(2):
        await be.run("hi", task_id="n", workspace=tmp_path, executor=None, session_key="web:s1", instance="author")

    modes = [mode for mode, _ in _invocations(tmp_path)]
    ids = {agent_id for _, agent_id in _invocations(tmp_path)}
    assert modes == ["CREATE", "RESUME"]
    assert len(ids) == 1


def _fixture_cmd(tmp_path: Path, name: str, lines: list[str]) -> str:
    """A `cat` command that replays a canned transcript.

    The transcript must reach the backend through a file, not inline in the
    command: `command` is parsed with shlex.split, which strips JSON's double
    quotes ('{"type":"x"}' -> '{type:x}') and splits on the newlines, so an
    inline JSONL fixture never survives to the parser.
    """
    path = tmp_path / name
    path.write_text("\n".join(lines), encoding="utf-8")
    return f"cat {path}"


def _split_stream_cmd(tmp_path: Path, stdout_text: str, stderr_text: str) -> str:
    """A script that writes the reply to stdout and the session id to stderr.

    A script file, not an inline command: `command` goes through shlex.split,
    which would eat the redirection and the quoting.
    """
    path = tmp_path / "split_stream.sh"
    path.write_text(f"printf %s {stdout_text!r}\nprintf %s {stderr_text!r} >&2\n", encoding="utf-8")
    return f"sh {path}"


async def test_cli_backend_derived_id_from_stderr(tmp_path: Path) -> None:
    # hermes prints the reply on stdout and `session_id: <id>` on stderr, so the
    # transcript for id recovery has to be both streams.
    be = CliAgentBackend(
        name="hermesfake",
        command=_split_stream_cmd(tmp_path, "the answer", "session_id: 20260805_093449_6486bf"),
        resume_command="printf resumed-%s {agent_id}",
        id_source="derived",
        transcript_format="text",
        session_id_pattern=r"session_id:\s*(\S+)",
        output_pattern=r"(?s)\A(.*?)\s*\Z",
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    first = await be.run("task", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    # output_pattern selects stdout, so the stderr id line stays out of the reply.
    assert first == "the answer"
    second = await be.run("task", task_id="t2", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert second == "resumed-20260805_093449_6486bf"


async def test_cli_backend_prefers_stdout_id_over_stderr(tmp_path: Path) -> None:
    # stdout stays authoritative: a CLI that prints the id on both streams must
    # not have the stderr copy win.
    be = CliAgentBackend(
        name="bothstreams",
        command=_split_stream_cmd(tmp_path, "session_id: from-stdout", "session_id: from-stderr"),
        resume_command="printf resumed-%s {agent_id}",
        id_source="derived",
        transcript_format="text",
        session_id_pattern=r"session_id:\s*(\S+)",
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    await be.run("task", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    second = await be.run("task", task_id="t2", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert second == "resumed-from-stdout"


async def test_cli_backend_derived_id_from_codex_jsonl(tmp_path: Path) -> None:
    cmd = _fixture_cmd(
        tmp_path,
        "codex.jsonl",
        [
            '{"type":"thread.started","thread_id":"th_7"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"done"}}',
        ],
    )
    be = CliAgentBackend(
        name="codexfake",
        command=cmd,
        resume_command="printf resumed-%s {agent_id}",
        id_source="derived",
        transcript_format="codex_jsonl",
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    first = await be.run("task", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert first == "done"  # reply extracted, not the raw JSONL
    second = await be.run("task", task_id="t2", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert second == "resumed-th_7"  # id was derived from the transcript and reused


async def test_cli_backend_claude_stream_json_extracts_result(tmp_path: Path) -> None:
    cmd = _fixture_cmd(
        tmp_path,
        "claude.jsonl",
        [
            '{"type":"system","subtype":"init","session_id":"s1"}',
            '{"type":"result","is_error":false,"result":"the answer","session_id":"s1"}',
        ],
    )
    be = CliAgentBackend(name="claudefake", command=cmd, transcript_format="claude_stream_json")
    assert await be.run("q", task_id="t1", workspace=tmp_path, executor=None) == "the answer"


async def test_cli_backend_claude_is_error_raises_on_zero_exit(tmp_path: Path) -> None:
    # `cat` exits 0; only is_error marks the failure.
    cmd = _fixture_cmd(
        tmp_path,
        "claude-err.jsonl",
        ['{"type":"result","is_error":true,"result":"blew up","session_id":"s1"}'],
    )
    be = CliAgentBackend(name="claudefake", command=cmd, transcript_format="claude_stream_json")
    with pytest.raises(RuntimeError, match="blew up"):
        await be.run("q", task_id="t1", workspace=tmp_path, executor=None)


async def test_cli_backend_failed_create_does_not_bind_handle(tmp_path: Path) -> None:
    reg = InstanceRegistry(path=tmp_path / "inst.json")
    be = CliAgentBackend(
        name="boom",
        command="false {agent_id}",
        resume_command="printf resumed-%s {agent_id}",
        registry=reg,
    )
    with pytest.raises(RuntimeError):
        await be.run("t", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    # Deferred commit: a poisoned handle would resume a session that never existed.
    assert await reg.lookup("s", "boom", "h") is None


async def test_cli_backend_output_pattern_extracts_reply(tmp_path: Path) -> None:
    be = CliAgentBackend(name="pat", command="printf 'noise BEGIN kept END noise'", output_pattern=r"BEGIN (.*?) END")
    assert await be.run("x", task_id="t1", workspace=tmp_path, executor=None) == "kept"


async def test_cli_backend_derived_without_id_warns(tmp_path: Path) -> None:
    be = CliAgentBackend(
        name="noid",
        command="printf %s plain-text-output",
        resume_command="printf resumed-%s {agent_id}",
        id_source="derived",
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    out = await be.run("t", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert out.startswith("plain-text-output")
    assert "not resumable" in out


async def test_cli_backend_truncation_preserves_warning(tmp_path: Path) -> None:
    # When base output exceeds max_output_chars and no id is extracted,
    # the warning must still appear in full, not be truncated away.
    large_output = "x" * 500
    cmd = _fixture_cmd(tmp_path, "large.txt", [large_output])
    be = CliAgentBackend(
        name="noid",
        command=cmd,
        resume_command="printf resumed-%s {agent_id}",
        id_source="derived",
        max_output_chars=200,
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    out = await be.run("t", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    warning_text = "[raven] Warning: no session id could be extracted"
    assert warning_text in out
    assert out.endswith("output, so this instance is not resumable. Use a new handle to recreate.")
    assert len(out) <= 200


async def test_cli_backend_truncation_respects_cap_below_warning_length(
    tmp_path: Path,
) -> None:
    # Edge case: when max_output_chars is smaller than the warning length (140 chars),
    # the returned string must still respect the cap, never exceed it.
    large_output = "x" * 500
    cmd = _fixture_cmd(tmp_path, "large.txt", [large_output])
    be = CliAgentBackend(
        name="noid",
        command=cmd,
        resume_command="printf resumed-%s {agent_id}",
        id_source="derived",
        max_output_chars=50,
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    out = await be.run("t", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert len(out) <= 50


async def test_cli_backend_derived_id_from_regex_pattern(tmp_path: Path) -> None:
    cmd = _fixture_cmd(
        tmp_path,
        "plaintext.txt",
        ["Session started: sess-abc-123"],
    )
    be = CliAgentBackend(
        name="plaintext",
        command=cmd,
        resume_command="printf resumed-%s {agent_id}",
        id_source="derived",
        transcript_format="text",
        session_id_pattern=r"Session started: ([a-z0-9-]+)",
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    first = await be.run("task", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert "Session started: sess-abc-123" in first
    second = await be.run("task", task_id="t2", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert "resumed-sess-abc-123" in second


async def test_cli_backend_stateful_is_error_does_not_bind_handle(tmp_path: Path) -> None:
    reg = InstanceRegistry(path=tmp_path / "inst.json")
    cmd = _fixture_cmd(
        tmp_path,
        "error.jsonl",
        ['{"type":"result","is_error":true,"result":"something failed","session_id":"s1"}'],
    )
    be = CliAgentBackend(
        name="claudefake",
        command=cmd,
        resume_command="printf resumed-%s {agent_id}",
        transcript_format="claude_stream_json",
        registry=reg,
    )
    with pytest.raises(RuntimeError, match="something failed"):
        await be.run("q", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert await reg.lookup("s", "claudefake", "h") is None


async def test_cli_backend_resume_failure_forgets_and_retries_as_create(tmp_path: Path) -> None:
    reg = InstanceRegistry(path=tmp_path / "inst.json")
    be = CliAgentBackend(
        name="flaky",
        command="printf %s {agent_id}",
        resume_command="false {agent_id}",
        registry=reg,
    )
    first = await be.run("t", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert uuid.UUID(first)
    # Every resume of `h` now fails (`false`), as if the CLI's own session
    # store had pruned that id; the backend must drop it and retry as a create
    # rather than wedging the handle.
    second = await be.run("t", task_id="t2", workspace=tmp_path, executor=None, session_key="s", instance="h")
    assert uuid.UUID(second)
    assert second != first
    assert await reg.lookup("s", "flaky", "h") == second


async def test_cli_backend_create_after_failed_resume_also_fails_leaves_no_record(tmp_path: Path) -> None:
    reg = InstanceRegistry(path=tmp_path / "inst.json")
    await reg.commit("s", "allfail", "h", "stale-id")
    be = CliAgentBackend(
        name="allfail",
        command="false {agent_id}",
        resume_command="false {agent_id}",
        registry=reg,
    )
    with pytest.raises(RuntimeError):
        await be.run("t", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    # The failed resume dropped the stale record; the retried create also
    # failed, so the error propagates and no record (stale or new) survives.
    assert await reg.lookup("s", "allfail", "h") is None


def test_presets_are_valid_and_complete() -> None:
    presets = {p["name"]: p for p in third_party_subagent_presets()}
    assert set(presets) == {"claude_code", "codex", "mirothinker", "openclaw", "hermes", "opencode"}
    assert {p["preset"] for p in presets.values()} == set(presets)
    # Every preset validates against the schema (discriminated union), so "Add
    # from preset" can never produce a config the write path would reject ...
    cfg = SubagentsConfig(agents=list(presets.values()))
    assert len(cfg.agents) == len(presets)
    # ... and each one builds into a concrete backend.
    for entry in cfg.agents:
        assert build_third_party_backend(entry) is not None

    # One preset per agent, each already carrying that agent's transport: there is
    # no second preset for the same tool over a different one, because connecting
    # must not have to try two transports (and the cli test dispatch spends the
    # user's own quota to answer a question the repo already knows).
    assert {name for name, p in presets.items() if p["kind"] == "acp"} == {
        "claude_code",
        "codex",
        "opencode",
        "hermes",
        "openclaw",
    }
    assert presets["mirothinker"]["kind"] == "openai"

    for name, acp in ((n, p) for n, p in presets.items() if p["kind"] == "acp"):
        # An acp command starts a server, so it carries no task placeholder, and
        # none of the cli fields that declare what a handshake reports.
        assert not any(ph in acp["command"] for ph in ("{prompt}", "{prompt_file}", "{agent_id}")), name
        assert not (set(acp) & {"resumeCommand", "idSource", "transcriptFormat", "stateful", "readsLocalFiles"}), name

    # Adapter versions are pinned: `npx -y` fetches what is absent, so an unpinned
    # command would silently change which build a user runs.
    for name in ("claude_code", "codex", "opencode"):
        assert "npx -y " in presets[name]["command"], name
        assert "@" in presets[name]["command"].split("npx -y ")[1], name
        # npx may download on the first connect, which is far slower than starting
        # an installed binary.
        assert presets[name]["readyTimeoutMs"] >= 120000, name

    # Measured: `openclaw acp` is a gateway-backed bridge and did not answer
    # `initialize` within 20s, so the shared default would report a working
    # install unreachable.
    assert presets["openclaw"]["readyTimeoutMs"] > presets["hermes"]["readyTimeoutMs"]


def test_presets_declare_their_own_provenance() -> None:
    # The UI groups by provenance rather than by name, because a configured
    # preset's name is user-editable. A preset that shipped without this would
    # reappear as unconfigured the moment the user renamed it.
    for preset in third_party_subagent_presets():
        assert preset["preset"] == preset["name"], preset["name"]


def test_provenance_survives_a_rename() -> None:
    entry = dict(third_party_subagent_preset("claude_code"))
    entry["name"] = "frontend_reviewer"
    cfg = SubagentsConfig(agents=[entry]).agents[0]
    assert cfg.name == "frontend_reviewer"
    assert cfg.preset == "claude_code"
    # Round-trips under the wire alias, which is what the gateway persists.
    assert cfg.model_dump(by_alias=True)["preset"] == "claude_code"


def test_hand_written_entry_has_no_provenance() -> None:
    cfg = SubagentsConfig(agents=[{"name": "mine", "kind": "cli", "command": "echo hi"}])
    assert cfg.agents[0].preset is None


def test_provenance_backfilled_when_name_matches_a_preset_cli() -> None:
    # A preset entry written by an earlier build has no `preset` field yet, so
    # backfilling by name is what lets it be edited and saved again.
    cfg = SubagentsConfig(agents=[{"name": "claude_code", "kind": "cli", "command": "claude -p {prompt}"}]).agents[0]
    assert cfg.preset == "claude_code"


def test_provenance_backfilled_when_name_matches_a_preset_openai() -> None:
    cfg = SubagentsConfig(
        agents=[
            {
                "name": "mirothinker",
                "kind": "openai",
                "baseUrl": "https://api.miromind.ai/v1",
                "model": "mirothinker-1-7-deepresearch",
            }
        ]
    ).agents[0]
    assert cfg.preset == "mirothinker"


def test_provenance_not_guessed_for_a_renamed_hand_written_entry() -> None:
    # `Coder` is evidently a renamed preset on this machine, but its origin is
    # genuinely unknowable from `command` alone and must not be guessed.
    cfg = SubagentsConfig(agents=[{"name": "Coder", "kind": "cli", "command": "echo hi"}]).agents[0]
    assert cfg.preset is None


def test_explicit_provenance_is_left_untouched() -> None:
    entry = {
        "name": "frontend_reviewer",
        "kind": "cli",
        "command": "echo hi",
        "preset": "claude_code",
    }
    cfg = SubagentsConfig(agents=[entry]).agents[0]
    assert cfg.preset == "claude_code"


def test_unknown_preset_value_is_rejected() -> None:
    from pydantic import ValidationError

    entry = {"name": "mine", "kind": "cli", "command": "echo hi", "preset": "not-a-real-preset"}
    with pytest.raises(ValidationError, match="not-a-real-preset"):
        SubagentsConfig(agents=[entry])


# --- manager: instance registry + one-instance cancellation --------------


async def test_registry_upsert_spawn_preserves_a_committed_agent_id(tmp_path: Path) -> None:
    reg = instances_mod.InstanceRegistry(path=tmp_path / "inst.json")
    await reg.upsert_spawn("web:s1", "claude_code", "h", "running")
    await reg.commit("web:s1", "claude_code", "h", "sess-a")
    await reg.upsert_spawn("web:s1", "claude_code", "h", "completed")
    row = reg.list_instances("web:s1")[0]
    # Status and session id must coexist: the terminal status write must not
    # clobber the id the create committed.
    assert row["status"] == "completed"
    assert row["agentId"] == "sess-a"
    assert await reg.lookup("web:s1", "claude_code", "h") == "sess-a"


async def test_manager_records_and_cancels_one_instance(tmp_path: Path) -> None:
    started = asyncio.Event()

    class _Hang:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None, **_):
            started.set()
            await asyncio.sleep(3600)
            return "never"

    mgr = _mgr(tmp_path, [])
    _stub_agents(mgr, {"hang": _Hang()})
    mgr.set_submit(lambda req: None)
    await mgr.spawn("t", session_key="web:s1", agent="hang", instance="h1")
    await asyncio.wait_for(started.wait(), timeout=5)

    assert ("hang", "h1") in mgr.live_handles("web:s1")
    assert await mgr.cancel_by_instance("web:s1", "hang", "h1") is True
    # A second cancel finds nothing live.
    assert await mgr.cancel_by_instance("web:s1", "hang", "h1") is False
    assert mgr.live_handles("web:s1") == set()


async def test_manager_cancel_releases_the_concurrency_slot(tmp_path: Path) -> None:
    # The point of cancelling rather than marking: the `async with self._gate`
    # must unwind so the slot is reusable. Proven observably rather than by
    # reading the semaphore's private counter: fill every slot, cancel one,
    # and confirm a spawn that was blocked on the full gate then starts.
    # The cap is pinned here rather than taken from the default -- what is
    # under test is that a slot comes back, not how many there are.
    slots = 4
    hang_started = [asyncio.Event() for _ in range(slots)]
    canary_started = asyncio.Event()

    class _Hang:
        def __init__(self, ev: asyncio.Event) -> None:
            self._ev = ev

        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None, **_):
            self._ev.set()
            await asyncio.sleep(3600)
            return "never"

    class _Canary:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None, **_):
            canary_started.set()
            await asyncio.sleep(3600)
            return "never"

    mgr = _mgr(tmp_path, [], max_concurrent=slots)
    _stub_agents(mgr, {f"hang{i}": _Hang(ev) for i, ev in enumerate(hang_started)} | {"canary": _Canary()})
    mgr.set_submit(lambda req: None)

    for i in range(slots):
        await mgr.spawn("t", session_key="web:s1", agent=f"hang{i}", instance=f"h{i}")
    await asyncio.wait_for(asyncio.gather(*(ev.wait() for ev in hang_started)), timeout=5)

    # Every slot is held: the next spawn's coroutine blocks on the gate before
    # its backend ever runs.
    await mgr.spawn("t", session_key="web:s1", agent="canary", instance="c1")
    await asyncio.sleep(0.05)
    assert not canary_started.is_set(), "the canary should still be blocked on a full gate"

    assert await mgr.cancel_by_instance("web:s1", "hang0", "h0") is True
    await asyncio.wait_for(canary_started.wait(), timeout=5)


async def test_manager_two_spawns_on_one_handle_both_cancelled_and_gate_freed(tmp_path: Path) -> None:
    """Two spawns can race onto the same (agent, instance) key before the first
    completes; `_instance_tasks` must hold both task ids rather than letting the
    second overwrite the first, and cancel_by_instance must reach both -- proven
    observably by filling the gate with the pair and confirming a third,
    gate-blocked spawn then starts once the instance is cancelled.

    The second one *queues* rather than running alongside the first: they address
    one handle, and one handle is one conversation. It still holds a gate slot
    while it waits, which is why the canary below stays blocked."""
    hang_started = [asyncio.Event(), asyncio.Event()]
    canary_started = asyncio.Event()

    class _Hang:
        def __init__(self) -> None:
            self._n = 0

        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None, **_):
            hang_started[self._n].set()
            self._n += 1
            await asyncio.sleep(3600)
            return "never"

    class _Canary:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None, **_):
            canary_started.set()
            await asyncio.sleep(3600)
            return "never"

    mgr = SubagentManager(provider=_FakeProvider(), workspace=tmp_path, model="fake-model", max_concurrent=2)
    _stub_agents(mgr, {"hang": _Hang(), "canary": _Canary()})
    mgr.set_submit(lambda req: None)

    await mgr.spawn("t1", session_key="web:s1", agent="hang", instance="reviewer")
    await mgr.spawn("t2", session_key="web:s1", agent="hang", instance="reviewer")
    await asyncio.wait_for(hang_started[0].wait(), timeout=5)
    await asyncio.sleep(0.05)
    assert not hang_started[1].is_set(), "the second spawn on one handle must queue, not interleave"

    assert len(mgr._instance_tasks[("web:s1", "hang", "reviewer")]) == 2
    tasks = list(mgr._running_tasks.values())
    assert len(tasks) == 2

    # Both slots of a gate of 2 are held: a third spawn blocks before its
    # backend ever runs.
    await mgr.spawn("t3", session_key="web:s1", agent="canary", instance="c1")
    await asyncio.sleep(0.05)
    assert not canary_started.is_set(), "the canary should still be blocked on a full gate"

    assert await mgr.cancel_by_instance("web:s1", "hang", "reviewer") is True
    assert all(t.cancelled() for t in tasks)
    assert ("hang", "reviewer") not in mgr.live_handles("web:s1")

    # Both slots are now free: the canary, previously blocked, starts.
    await asyncio.wait_for(canary_started.wait(), timeout=5)


async def test_manager_spawn_writes_pending_row_before_the_gate(tmp_path: Path) -> None:
    """A spawn queued behind a full gate must have a registry row -- and be
    cancellable -- from the moment it's requested, not only once it starts
    running. Also proves cancelling a queued (not yet gate-acquired) spawn
    leaves the gate itself consistent: freeing the real occupant afterwards
    still lets a further spawn through."""
    hang_started = asyncio.Event()
    canary_started = asyncio.Event()

    class _Hang:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None, **_):
            hang_started.set()
            await asyncio.sleep(3600)
            return "never"

    class _Queued:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None, **_):
            raise AssertionError("must never run: cancelled while still queued on the gate")

    class _Canary:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None, **_):
            canary_started.set()
            await asyncio.sleep(3600)
            return "never"

    mgr = SubagentManager(provider=_FakeProvider(), workspace=tmp_path, model="fake-model", max_concurrent=1)
    _stub_agents(mgr, {"hang": _Hang(), "queued": _Queued(), "canary": _Canary()})
    mgr.set_submit(lambda req: None)

    await mgr.spawn("t1", session_key="web:s1", agent="hang", instance="h1")
    await asyncio.wait_for(hang_started.wait(), timeout=5)

    # The gate's one slot is held: this spawn's coroutine blocks on
    # `async with self._gate` before its backend ever runs, yet it must
    # already have a row.
    await mgr.spawn("t2", session_key="web:s1", agent="queued", instance="q1")
    await asyncio.sleep(0.05)

    rows = {r["handle"]: r["status"] for r in instances_mod.get_registry().list_instances("web:s1")}
    assert rows["h1"] == "running"
    assert rows["q1"] == "pending"

    assert await mgr.cancel_by_instance("web:s1", "queued", "q1") is True

    # Cancelling the queued spawn must not corrupt the gate: the still-hung
    # occupant, once itself cancelled, must free its slot cleanly for a
    # further spawn to acquire.
    assert await mgr.cancel_by_instance("web:s1", "hang", "h1") is True
    await mgr.spawn("t3", session_key="web:s1", agent="canary", instance="c1")
    await asyncio.wait_for(canary_started.wait(), timeout=5)


async def test_manager_cancel_all_cancels_every_running_spawn(tmp_path: Path) -> None:
    """The helper the gateway's shutdown `finally` calls before `agent.stop()`:
    without it, a CLI child survives the gateway's own exit, since
    `start_new_session=True` (cli_agent.py) detaches it from the gateway's own
    process group."""
    started = [asyncio.Event(), asyncio.Event()]

    class _Hang:
        def __init__(self) -> None:
            self._n = 0

        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None, **_):
            started[self._n].set()
            self._n += 1
            await asyncio.sleep(3600)
            return "never"

    mgr = _mgr(tmp_path, [])
    _stub_agents(mgr, {"hang": _Hang()})
    mgr.set_submit(lambda req: None)

    await mgr.spawn("t1", session_key="web:s1", agent="hang", instance="a")
    await mgr.spawn("t2", session_key="web:s1", agent="hang", instance="b")
    await asyncio.wait_for(asyncio.gather(*(e.wait() for e in started)), timeout=5)

    cancelled = await mgr.cancel_all()
    assert cancelled == 2
    assert mgr.get_running_count() == 0
    assert mgr.live_handles("web:s1") == set()

    # A second call is a no-op, not an error, on an already-idle manager.
    assert await mgr.cancel_all() == 0


# --- automatic timeout removal (Task 6) -----------------------------------


async def test_cli_backend_without_timeout_waits(tmp_path: Path) -> None:
    # No timeout configured: a slow child runs to completion instead of being killed.
    be = CliAgentBackend(name="slow", command="sh -c 'sleep 2; printf done'", timeout=None)
    assert await be.run("x", task_id="t1", workspace=tmp_path, executor=None) == "done"


async def test_cli_backend_with_timeout_still_kills(tmp_path: Path) -> None:
    # An explicit timeout remains an opt-in backstop.
    be = CliAgentBackend(name="slow", command="sleep 5", timeout=1)
    with pytest.raises(RuntimeError, match="timed out"):
        await be.run("x", task_id="t1", workspace=tmp_path, executor=None)


def test_presets_have_no_timeout() -> None:
    for preset in third_party_subagent_presets():
        assert preset.get("timeout") is None, preset["name"]


def test_dag_tool_is_not_timer_killed() -> None:
    from raven.agent.subagent_dag.tool import SubAgentDagTool

    # The registry skips asyncio.wait_for for blocking_interaction tools, which is
    # what lets a long DAG run to completion under manual stop control.
    assert SubAgentDagTool.blocking_interaction is True


async def test_cli_backend_resume_timeout_leaves_registry_record_intact(tmp_path: Path) -> None:
    reg = InstanceRegistry(path=tmp_path / "inst.json")
    await reg.commit("s", "flaky", "h", "existing-id")
    be = CliAgentBackend(
        name="flaky",
        command="printf %s {agent_id}",
        resume_command="sleep 5",
        timeout=1,
        registry=reg,
    )
    with pytest.raises(RuntimeError, match="timed out"):
        await be.run("t", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    # A timeout is not evidence of a pruned session; forgetting here would
    # discard a perfectly valid handle-to-session binding.
    assert await reg.lookup("s", "flaky", "h") == "existing-id"


async def test_cli_backend_resume_is_error_leaves_registry_record_intact(tmp_path: Path) -> None:
    reg = InstanceRegistry(path=tmp_path / "inst.json")
    await reg.commit("s", "flaky", "h", "existing-id")
    cmd = _fixture_cmd(
        tmp_path, "resume-err.jsonl", ['{"type":"result","is_error":true,"result":"boom","session_id":"s1"}']
    )
    be = CliAgentBackend(
        name="flaky",
        command="printf %s {agent_id}",
        resume_command=cmd,
        transcript_format="claude_stream_json",
        registry=reg,
    )
    with pytest.raises(RuntimeError, match="boom"):
        await be.run("t", task_id="t1", workspace=tmp_path, executor=None, session_key="s", instance="h")
    # Same reasoning as the timeout case: the transcript's own error signal is
    # not proof the CLI's session store pruned the handle.
    assert await reg.lookup("s", "flaky", "h") == "existing-id"


async def test_cli_backend_cancel_kills_the_whole_process_group(tmp_path: Path) -> None:
    # A codex-style child reparents its real worker to a grandchild process, so
    # killing only the launcher would leave the actual work running. Prove the
    # process *group* dies by spawning a shell that forks a detached grandchild
    # and checking that it, not just the launcher, is gone after cancel.
    pid_file = tmp_path / "grandchild.pid"
    script = tmp_path / "spawn.sh"
    script.write_text(f"#!/bin/sh\nsleep 100 &\necho $! > {pid_file}\nwait\n", encoding="utf-8")
    script.chmod(0o755)

    be = CliAgentBackend(name="nested", command=f"sh {script}")
    task = asyncio.create_task(be.run("x", task_id="t1", workspace=tmp_path, executor=None))

    for _ in range(100):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("grandchild never started")
    grandchild_pid = int(pid_file.read_text().strip())
    os.kill(grandchild_pid, 0)  # still alive before cancel

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    for _ in range(40):
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("grandchild survived the process-group kill")


async def test_cli_backend_timeout_kills_reparented_child_after_launcher_already_exited(
    tmp_path: Path,
) -> None:
    """The shape `_kill_process_group` exists for, and the one the previous
    (reverted) `if proc.returncode is not None: return` guard silently
    no-opped on: a codex-style launcher backgrounds its real worker and exits
    immediately, so `proc.returncode` is already set by the time the timeout
    fires -- while the worker, still in the same process group and still
    holding the pipes open (inherited, not closed), is why `communicate()`
    never returned in the first place. `killpg` must fire unconditionally,
    keyed off the pgid captured at spawn, not gated on the launcher's own
    exit status."""
    pid_file = tmp_path / "grandchild.pid"
    script = tmp_path / "reparent.sh"
    script.write_text(f"#!/bin/sh\nsleep 300 &\necho $! > {pid_file}\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)

    be = CliAgentBackend(name="reparented", command=f"sh {script}", timeout=1)
    with pytest.raises(RuntimeError, match="timed out"):
        await be.run("x", task_id="t1", workspace=tmp_path, executor=None)

    for _ in range(100):
        if pid_file.exists() and pid_file.read_text().strip():
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("grandchild never started")
    grandchild_pid = int(pid_file.read_text().strip())

    for _ in range(40):
        try:
            os.kill(grandchild_pid, 0)
        except ProcessLookupError:
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError(
            "grandchild survived: the launcher had already exited, so a "
            "returncode-gated killpg would silently no-op here"
        )


# --- kind-aware stateful ---------------------------------------------------


def test_openai_kind_is_stateful_when_declared():
    """A custom endpoint's operator is the only one who knows replay works there."""
    from raven.agent.subagent.backends import third_party_agent_meta
    from raven.config.schema import SubagentsConfig

    cfg = SubagentsConfig(
        agents=[
            {
                "kind": "openai",
                "name": "custom-http",
                "baseUrl": "http://localhost:8000/v1",
                "model": "m",
                "stateful": True,
            }
        ]
    ).agents[0]

    assert third_party_agent_meta(cfg).stateful is True


def test_openai_kind_is_stateful_by_default():
    """No UI surface writes this field, so the default is what a custom entry gets.

    It defaults on because raven's replay is what delivers the capability and it
    works at any endpoint; a false is the endpoint-specific exception below.
    """
    from raven.agent.subagent.backends import third_party_agent_meta
    from raven.config.schema import SubagentsConfig

    cfg = SubagentsConfig(
        agents=[
            {
                "kind": "openai",
                "name": "custom-http",
                "baseUrl": "http://localhost:8000/v1",
                "model": "m",
            }
        ]
    ).agents[0]

    assert third_party_agent_meta(cfg).stateful is True


def test_openai_kind_honours_a_declared_false():
    """The exception has to survive the load path, or the default swallows it."""
    from raven.agent.subagent.backends import third_party_agent_meta
    from raven.config.schema import SubagentsConfig

    cfg = SubagentsConfig(
        agents=[
            {
                "kind": "openai",
                "name": "custom-http",
                "baseUrl": "http://localhost:8000/v1",
                "model": "m",
                "stateful": False,
            }
        ]
    ).agents[0]

    assert cfg.stateful is False
    assert third_party_agent_meta(cfg).stateful is False


def test_a_stored_null_stateful_still_loads():
    """The field was optional until its default became true, so entries written
    then carry an explicit null. Rejecting one fails validation of the whole
    top-level Config -- raven stops starting, and the config that could be fixed
    sits behind the loader that no longer reads it.
    """
    from raven.agent.subagent.backends import third_party_agent_meta
    from raven.config.schema import SubagentsConfig

    cfg = SubagentsConfig(
        agents=[
            {
                "kind": "openai",
                "name": "written-by-the-old-form",
                "baseUrl": "http://localhost:8000/v1",
                "model": "m",
                "stateful": None,
            }
        ]
    ).agents[0]

    assert cfg.stateful is True
    assert third_party_agent_meta(cfg).stateful is True


def test_the_mirothinker_preset_is_pinned_stateless():
    """A preset states replay-suitability for the endpoint it names.

    The default is stateful, so this pin is what makes the roster the model
    reads say otherwise for this one endpoint.
    """
    from raven.agent.subagent.backends import format_agent_listing, third_party_agent_meta
    from raven.agent.subagent.presets import third_party_subagent_presets
    from raven.config.schema import SubagentsConfig

    entry = next(e for e in third_party_subagent_presets() if e.get("preset") == "mirothinker")
    assert entry["stateful"] is False

    cfg = SubagentsConfig(agents=[entry]).agents[0]
    meta = third_party_agent_meta(cfg)
    assert meta.stateful is False
    assert "stateless" in format_agent_listing([meta])


def test_cli_kind_still_derives_stateful_from_resume_command():
    from raven.agent.subagent.backends import third_party_agent_meta
    from raven.config.schema import SubagentsConfig

    entries = SubagentsConfig(
        agents=[
            {"kind": "cli", "name": "plain", "command": "true --prompt {prompt}"},
            {
                "kind": "cli",
                "name": "resumable",
                "command": "true --session {agent_id} --prompt {prompt}",
                "resumeCommand": "true --resume {agent_id} --prompt {prompt}",
                "idSource": "provisioned",
            },
        ]
    ).agents

    assert third_party_agent_meta(entries[0]).stateful is False
    assert third_party_agent_meta(entries[1]).stateful is True


async def test_openai_backend_streams_when_a_delta_hook_is_wired(tmp_path: Path) -> None:
    """The same reply, delivered in frames: the caller renders it as it forms and
    must not render the return value again."""
    seen_body: dict[str, Any] = {}

    async def handler(request: web.Request) -> web.StreamResponse:
        seen_body.update(await request.json())
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for piece in ("stub ", "answer"):
            frame = json.dumps({"choices": [{"delta": {"content": piece}}]})
            await resp.write(f"data: {frame}\n\n".encode())
        await resp.write(b"data: [DONE]\n\n")
        return resp

    port = _free_port()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    seen: list[str] = []

    async def on_delta(text: str) -> None:
        seen.append(text)

    try:
        be = OpenAIApiBackend(name="mirothinker", base_url=f"http://127.0.0.1:{port}/v1", model="mirothinker-1")
        out = await be.run("solve it", task_id="t9", workspace=tmp_path, executor=None, on_delta=on_delta)
    finally:
        await runner.cleanup()

    assert seen == ["stub ", "answer"]
    assert out == "stub answer"
    assert seen_body["stream"] is True


async def test_openai_backend_streams_no_more_than_it_returns(tmp_path: Path) -> None:
    """An uncapped stream would render text the record never stores, and it
    would vanish on the next switch into the instance."""

    async def handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        for piece in ("abcd", "efgh"):
            frame = json.dumps({"choices": [{"delta": {"content": piece}}]})
            await resp.write(f"data: {frame}\n\n".encode())
        await resp.write(b"data: [DONE]\n\n")
        return resp

    port = _free_port()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()
    seen: list[str] = []

    async def on_delta(text: str) -> None:
        seen.append(text)

    try:
        be = OpenAIApiBackend(
            name="mirothinker",
            base_url=f"http://127.0.0.1:{port}/v1",
            model="mirothinker-1",
            max_output_chars=6,
        )
        out = await be.run("solve it", task_id="t10", workspace=tmp_path, executor=None, on_delta=on_delta)
    finally:
        await runner.cleanup()

    assert "".join(seen) == out == "abcdef"


async def test_openai_backend_skips_frames_it_cannot_read(tmp_path: Path) -> None:
    """These endpoints are only nominally OpenAI-compatible; a keep-alive comment
    or an unrecognised frame must not fail a turn that is answering fine."""

    async def handler(request: web.Request) -> web.StreamResponse:
        resp = web.StreamResponse(headers={"Content-Type": "text/event-stream"})
        await resp.prepare(request)
        await resp.write(b": keep-alive\n\n")
        await resp.write(b"data: {not json}\n\n")
        await resp.write(b'data: {"choices":[{"delta":{}}]}\n\n')
        await resp.write(b'data: {"choices":["not-an-object"]}\n\n')
        await resp.write(b'data: {"choices":[{"delta":null}]}\n\n')
        await resp.write(b'data: {"choices":[{"delta":{"content":"fine"}}]}\n\n')
        await resp.write(b"data: [DONE]\n\n")
        return resp

    port = _free_port()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    async def on_delta(text: str) -> None:
        return None

    try:
        be = OpenAIApiBackend(name="mirothinker", base_url=f"http://127.0.0.1:{port}/v1", model="mirothinker-1")
        out = await be.run("solve it", task_id="t11", workspace=tmp_path, executor=None, on_delta=on_delta)
    finally:
        await runner.cleanup()

    assert out == "fine"


def test_streaming_capability_is_read_from_the_transport() -> None:
    """A cli agent cannot be made to stream by declaring that it does: its stdout
    is buffered whole and the reply parsed out of it after exit."""
    from raven.agent.subagent.backends import AcpAgentBackend, RavenLoopBackend

    assert RavenLoopBackend.streams is True
    assert OpenAIApiBackend.streams is True
    assert AcpAgentBackend.streams is True
    assert CliAgentBackend.streams is False


def test_every_backend_accepts_the_managers_call_shape() -> None:
    """One keyword set reaches whichever backend the manager resolved, so a
    backend that declares fewer parameters fails the dispatch with a TypeError
    before its agent is ever contacted. Checked over all four rather than per
    backend: the drift is invisible in a test that calls one `run` directly with
    the arguments that backend happens to declare.
    """
    import inspect

    from raven.agent.subagent.backends import AcpAgentBackend, RavenLoopBackend

    passed = {"task_id", "workspace", "executor", "session_key", "instance", "provider", "model"}
    for cls in (RavenLoopBackend, CliAgentBackend, OpenAIApiBackend, AcpAgentBackend):
        params = set(inspect.signature(cls.run).parameters)
        assert passed <= params, f"{cls.__name__}.run cannot be called by the manager: missing {passed - params}"
        # The manager offers the delta hook to any backend that declares it can
        # stream, so every backend has to be able to receive it -- cli included,
        # where whether it *will* stream is decided per instance from the command.
        assert "on_delta" in params, cls.__name__


async def test_openai_backend_streaming_reports_an_upstream_refusal(tmp_path: Path) -> None:
    """A 401 answered as a stream must fail the turn with the endpoint's own
    words, exactly as the buffered path does -- not read as an empty reply."""

    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=401, text="provider rejected the credential")

    port = _free_port()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    await site.start()

    async def on_delta(text: str) -> None:
        return None

    try:
        be = OpenAIApiBackend(name="mirothinker", base_url=f"http://127.0.0.1:{port}/v1", model="mirothinker-1")
        with pytest.raises(RuntimeError, match="HTTP 401"):
            await be.run("solve it", task_id="t12", workspace=tmp_path, executor=None, on_delta=on_delta)
    finally:
        await runner.cleanup()


# --- cli streaming (claude_stream_json partial messages) ----------------------


def _claude_partial_transcript(pieces: tuple[str, ...], *, session: str = "sess-1") -> list[str]:
    """The frames a real ``claude -p ... --include-partial-messages`` run emits.

    Shapes taken from a live run, not invented: the answer arrives as
    ``stream_event`` / ``content_block_delta`` / ``text_delta`` frames and is
    then repeated whole on ``result``, which is what the buffered parse reads.
    """
    lines = [json.dumps({"type": "system", "subtype": "init", "session_id": session})]
    lines += [
        json.dumps(
            {
                "type": "stream_event",
                "event": {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": piece}},
                "session_id": session,
                "parent_tool_use_id": None,
            }
        )
        for piece in pieces
    ]
    lines.append(json.dumps({"type": "result", "session_id": session, "is_error": False, "result": "".join(pieces)}))
    return lines


def _transcript_command(tmp_path: Path, lines: list[str], *, name: str = "claude_stub") -> str:
    """A command that prints ``lines`` one at a time, as a real CLI would."""
    script = tmp_path / f"{name}.py"
    body = "\n".join(f"print({line!r}, flush=True)" for line in lines)
    script.write_text(body + "\n", encoding="utf-8")
    return f"{sys.executable} {script}"


def _streaming_cli(tmp_path: Path, command: str, **kwargs: Any) -> CliAgentBackend:
    return CliAgentBackend(
        name="Coder",
        command=f"{command} --include-partial-messages",
        transcript_format="claude_stream_json",
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
        **kwargs,
    )


def test_cli_streaming_is_read_from_the_command_not_declared() -> None:
    """Only the flag makes claude emit deltas, so only the flag may claim it."""
    base = "claude -p {prompt} --output-format stream-json --verbose"

    assert CliAgentBackend(name="a", command=base, transcript_format="claude_stream_json").streams is False
    assert (
        CliAgentBackend(
            name="a", command=f"{base} --include-partial-messages", transcript_format="claude_stream_json"
        ).streams
        is True
    )
    # codex has no partial event to ask for: the flag would be a wish.
    assert (
        CliAgentBackend(
            name="a", command="codex exec --json --include-partial-messages", transcript_format="codex_jsonl"
        ).streams
        is False
    )


def test_cli_streaming_needs_the_flag_on_the_resume_template_too() -> None:
    """A direct chat is almost entirely resumes. Streaming the first turn and
    nothing afterwards is worse than not claiming the capability at all."""
    create = "claude -p {prompt} --output-format stream-json --verbose --include-partial-messages"

    assert (
        CliAgentBackend(
            name="a",
            command=create,
            resume_command="claude -p {prompt} --resume {agent_id} --output-format stream-json --verbose",
            transcript_format="claude_stream_json",
        ).streams
        is False
    )
    assert (
        CliAgentBackend(
            name="a",
            command=create,
            resume_command=f"{create} --resume {{agent_id}}",
            transcript_format="claude_stream_json",
        ).streams
        is True
    )


async def test_cli_backend_streams_text_deltas_as_the_transcript_arrives(tmp_path: Path) -> None:
    lines = _claude_partial_transcript(("he", "llo ", "there"))
    be = _streaming_cli(tmp_path, _transcript_command(tmp_path, lines))
    seen: list[str] = []

    async def on_delta(text: str) -> None:
        seen.append(text)

    reply = await be.run("hi", task_id="t1", workspace=tmp_path, executor=None, on_delta=on_delta)

    assert seen == ["he", "llo ", "there"]
    # The reply still comes from the buffered parse of `result`, so the record
    # holds what it always held.
    assert reply == "hello there"


async def test_cli_backend_streams_nothing_without_a_hook(tmp_path: Path) -> None:
    """A spawn keeps the buffered path; the same transcript yields the same reply."""
    lines = _claude_partial_transcript(("he", "llo ", "there"))
    be = _streaming_cli(tmp_path, _transcript_command(tmp_path, lines))

    assert await be.run("hi", task_id="t1", workspace=tmp_path, executor=None) == "hello there"


async def test_cli_backend_streams_only_the_reply_text(tmp_path: Path) -> None:
    """Thinking, a tool call's arguments assembling, and a nested agent's output
    all ride the same delta channel and none of them is the reply."""
    session = "sess-2"
    lines = [
        json.dumps(
            {
                "type": "stream_event",
                "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "hmm"}},
                "parent_tool_use_id": None,
            }
        ),
        json.dumps(
            {
                "type": "stream_event",
                "event": {"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": '{"a'}},
                "parent_tool_use_id": None,
            }
        ),
        json.dumps(
            {
                "type": "stream_event",
                "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "nested"}},
                "parent_tool_use_id": "toolu_1",
            }
        ),
        # An allowed list, not a denied one: a delta type this reader has never
        # seen may still carry a `text` field, and it is not the reply either.
        json.dumps(
            {
                "type": "stream_event",
                "event": {"type": "content_block_delta", "delta": {"type": "some_future_delta", "text": "not it"}},
                "parent_tool_use_id": None,
            }
        ),
        json.dumps(
            {
                "type": "stream_event",
                "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "real"}},
                "parent_tool_use_id": None,
            }
        ),
        # The finished text repeats here and on `result`; reading either would
        # render the answer twice.
        json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "real"}]}}),
        json.dumps({"type": "result", "session_id": session, "is_error": False, "result": "real"}),
    ]
    be = _streaming_cli(tmp_path, _transcript_command(tmp_path, lines, name="filtered"))
    seen: list[str] = []

    async def on_delta(text: str) -> None:
        seen.append(text)

    reply = await be.run("hi", task_id="t1", workspace=tmp_path, executor=None, on_delta=on_delta)

    assert seen == ["real"]
    assert reply == "real"


async def test_cli_backend_streams_past_a_line_longer_than_the_read_chunk(tmp_path: Path) -> None:
    """One transcript line carries a whole tool result. `StreamReader.readline`
    raises above 64 KiB, which the buffered path never had to care about."""
    filler = "x" * 200_000
    lines = [
        json.dumps({"type": "user", "message": {"content": [{"type": "tool_result", "content": filler}]}}),
        *_claude_partial_transcript(("after the big line",)),
    ]
    be = _streaming_cli(tmp_path, _transcript_command(tmp_path, lines, name="bigline"))
    seen: list[str] = []

    async def on_delta(text: str) -> None:
        seen.append(text)

    reply = await be.run("hi", task_id="t1", workspace=tmp_path, executor=None, on_delta=on_delta)

    assert seen == ["after the big line"]
    assert reply == "after the big line"


async def test_cli_backend_streams_no_more_than_it_returns(tmp_path: Path) -> None:
    lines = _claude_partial_transcript(("abcd", "efgh"))
    be = _streaming_cli(tmp_path, _transcript_command(tmp_path, lines, name="capped"), max_output_chars=6)
    seen: list[str] = []

    async def on_delta(text: str) -> None:
        seen.append(text)

    reply = await be.run("hi", task_id="t1", workspace=tmp_path, executor=None, on_delta=on_delta)

    assert "".join(seen) == reply == "abcdef"


async def test_cli_backend_ignores_a_delta_hook_it_cannot_honour(tmp_path: Path) -> None:
    """A caller driving the backend directly never consulted `streams`."""
    lines = _claude_partial_transcript(("hi",))
    be = CliAgentBackend(
        name="Writer",
        command=_transcript_command(tmp_path, lines, name="nonstreaming"),
        transcript_format="claude_stream_json",
        registry=InstanceRegistry(path=tmp_path / "inst.json"),
    )
    seen: list[str] = []

    async def on_delta(text: str) -> None:
        seen.append(text)

    assert be.streams is False
    assert await be.run("hi", task_id="t1", workspace=tmp_path, executor=None, on_delta=on_delta) == "hi"
    assert seen == []


async def test_cli_backend_kills_the_child_when_the_delta_sink_raises(tmp_path: Path) -> None:
    """A sink whose client went away abandons the capture. Without the kill the
    reparented worker keeps running, and its two pump tasks keep reading it.
    """
    script = tmp_path / "slow_stream.py"
    script.write_text(
        "import json, sys, time\n"
        "frame = {'type': 'stream_event', 'parent_tool_use_id': None,\n"
        "         'event': {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'x'}}}\n"
        "print(json.dumps(frame), flush=True)\n"
        "time.sleep(30)\n"
        "print('never', flush=True)\n",
        encoding="utf-8",
    )
    be = _streaming_cli(tmp_path, f"{sys.executable} {script}")

    async def on_delta(text: str) -> None:
        raise RuntimeError("client went away")

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="client went away"):
        await be.run("hi", task_id="t1", workspace=tmp_path, executor=None, on_delta=on_delta)

    # Returned on the sink's failure rather than after the child's own sleep.
    assert time.monotonic() - started < 10
    assert not [t for t in asyncio.all_tasks() if "pump" in repr(t.get_coro())]


async def test_the_line_pump_leaves_no_task_behind_when_the_sink_raises(tmp_path: Path) -> None:
    """``asyncio.gather`` leaves its siblings running when one of them raises.

    Tested on the pump directly: through ``run`` the abandoned child is killed,
    which ends the other pumps at EOF anyway, so only this bounds the window
    without depending on that.
    """
    script = tmp_path / "pump_leak.py"
    script.write_text(
        "import json, time\n"
        "frame = {'type': 'stream_event', 'parent_tool_use_id': None,\n"
        "         'event': {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'x'}}}\n"
        "print(json.dumps(frame), flush=True)\n"
        "time.sleep(30)\n",
        encoding="utf-8",
    )
    be = _streaming_cli(tmp_path, "unused")
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    async def boom(text: str) -> None:
        raise RuntimeError("client went away")

    before = asyncio.all_tasks()
    try:
        with pytest.raises(RuntimeError, match="client went away"):
            await be._communicate_streaming(proc, None, boom)
        assert asyncio.all_tasks() - before - {asyncio.current_task()} == set()
    finally:
        proc.kill()
        await proc.wait()


def test_mint_handle_slugifies_the_seed_and_appends_a_unique_suffix() -> None:
    handle = instances_mod.mint_handle("Refactor Auth!")
    assert handle.startswith("refactor-auth-")
    assert re.fullmatch(r"refactor-auth-[0-9a-f]{6}", handle)


def test_mint_handle_draws_its_suffix_afresh_on_every_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two mints of one seed differ, because the suffix is drawn and not derived.

    This replaced an assertion that 200 mints are 200 distinct handles. The code
    does not promise that and cannot: the suffix is six hex characters, so 200
    draws collide with probability about ``200**2 / (2 * 16**6)`` -- roughly one
    run in 840, which is a red pipeline every few hundred builds and blocks
    whoever is trying to merge that day. Widening the suffix would only move the
    number; six characters is a deliberate trade for a handle a person reads and
    types, and no caller needs more.

    What a caller does rely on is that a second spawn is not handed the first
    one's handle, and therefore its session. That is a statement about the draw
    rather than about luck, and it is checkable exactly.
    """
    drawn = itertools.count(1)
    monkeypatch.setattr(instances_mod.uuid, "uuid4", lambda: uuid.UUID(int=next(drawn) << 104))

    assert [instances_mod.mint_handle("research") for _ in range(3)] == [
        "research-000001",
        "research-000002",
        "research-000003",
    ]


def test_mint_handle_bounds_the_slug_and_trims_a_dangling_separator() -> None:
    handle = instances_mod.mint_handle("a" * 40)
    slug = handle.rsplit("-", 1)[0]
    assert slug == "a" * 32
    handle = instances_mod.mint_handle("x" * 32 + " tail")
    assert handle.rsplit("-", 1)[0] == "x" * 32


def test_mint_handle_falls_back_when_the_seed_carries_no_usable_characters() -> None:
    assert instances_mod.mint_handle("!!! ???").startswith("agent-")
    assert instances_mod.mint_handle("").startswith("agent-")


def test_mint_handle_falls_back_to_the_agent_name_for_a_non_ascii_seed() -> None:
    handle = instances_mod.mint_handle("重构认证", fallback="claude_code")
    assert re.fullmatch(r"claude-code-[0-9a-f]{6}", handle)


def test_mint_handle_falls_back_to_agent_when_the_fallback_is_also_non_ascii() -> None:
    handle = instances_mod.mint_handle("重构认证", fallback="重构认证")
    assert handle.startswith("agent-")


async def test_spawn_mints_an_instance_for_a_stateful_agent(tmp_path: Path) -> None:
    cli = ThirdPartyCliSubagentConfig(
        name="claude_code", command="cat {agent_id}", resume_command="cat --resume {agent_id}"
    )
    mgr = _mgr(tmp_path, [cli])
    captured: dict[str, Any] = {}

    async def fake_spawn(**kwargs):
        captured.update(kwargs)
        return "started"

    mgr.spawn = fake_spawn  # type: ignore[method-assign]
    await SpawnTool(manager=mgr).execute(task="do it", label="Refactor Auth", subagent="claude_code")
    assert re.fullmatch(r"refactor-auth-[0-9a-f]{6}", captured["instance"])


async def test_spawn_leaves_a_stateless_agent_without_an_instance(tmp_path: Path) -> None:
    cli = ThirdPartyCliSubagentConfig(name="oneshot", command="cat")
    mgr = _mgr(tmp_path, [cli])
    captured: dict[str, Any] = {}

    async def fake_spawn(**kwargs):
        captured.update(kwargs)
        return "started"

    mgr.spawn = fake_spawn  # type: ignore[method-assign]
    await SpawnTool(manager=mgr).execute(task="do it", subagent="oneshot")
    assert captured["instance"] is None


async def test_spawn_never_overwrites_an_instance_the_model_chose(tmp_path: Path) -> None:
    cli = ThirdPartyCliSubagentConfig(
        name="claude_code", command="cat {agent_id}", resume_command="cat --resume {agent_id}"
    )
    mgr = _mgr(tmp_path, [cli])
    captured: dict[str, Any] = {}

    async def fake_spawn(**kwargs):
        captured.update(kwargs)
        return "started"

    mgr.spawn = fake_spawn  # type: ignore[method-assign]
    await SpawnTool(manager=mgr).execute(task="do it", subagent="claude_code", instance="author")
    assert captured["instance"] == "author"


async def test_spawn_mints_for_the_built_in_sub_agent_too(tmp_path: Path) -> None:
    """`agent=None` is the in-process sub-agent, which resumes per handle.

    The roster is empty, which is the default install -- so this also pins the read
    back: the handle it mints is only worth minting if the same schema offers the
    parameter the announcement then tells the model to pass it back in.
    """
    mgr = _mgr(tmp_path, [])
    captured: dict[str, Any] = {}

    async def fake_spawn(**kwargs):
        captured.update(kwargs)
        return "started"

    mgr.spawn = fake_spawn  # type: ignore[method-assign]
    tool = SpawnTool(manager=mgr)
    await tool.execute(task="summarize the log")
    assert captured["instance"]
    assert captured["instance"].startswith("summarize-the-log-")
    assert "instance" in tool.parameters["properties"]


def test_spawn_instance_description_states_that_omission_assigns_one(tmp_path: Path) -> None:
    cli = ThirdPartyCliSubagentConfig(
        name="claude_code", command="cat {agent_id}", resume_command="cat --resume {agent_id}"
    )
    desc = SpawnTool(manager=_mgr(tmp_path, [cli])).parameters["properties"]["instance"]["description"]
    assert "assigned automatically" in desc
    assert "start a fresh one" not in desc


async def test_two_spawns_on_one_handle_do_not_lose_each_others_turns(tmp_path: Path) -> None:
    """The reason the second one queues: the instance state is a whole-file
    read-modify-write.

    Interleaved, the second dispatch reads the message list before the first
    appends to it and then writes its own version over the top -- the first
    agent's turn is gone, nothing raises, and the next resume reads a
    conversation that never happened. Reachable without any playbook: an openai
    agent declaring itself stateful is replayed the same way, and so is every
    built-in agent now that a graph can name one.
    """
    order: list[str] = []

    class _Appends:
        async def run(self, task, *, task_id, history=None, on_messages=None, **_):
            order.append(f"start:{task}")
            messages = list(history or [])
            await asyncio.sleep(0.05)  # the window an interleave would land in
            messages.append({"role": "assistant", "content": task})
            if on_messages is not None:
                on_messages(messages)
            order.append(f"end:{task}")
            return task

    from raven.config.schema import ThirdPartyOpenAISubagentConfig

    mgr = SubagentManager(
        provider=_FakeProvider(),
        workspace=tmp_path,
        model="fake-model",
        session_dir=lambda _k: tmp_path,
    )
    mgr.apply_agents([ThirdPartyOpenAISubagentConfig(name="replayed", base_url="http://x", model="m", stateful=True)])
    mgr.registry._backends["replayed"] = _Appends()
    mgr.set_submit(lambda req: None)

    await mgr.spawn("first", session_key="web:s1", agent="replayed", instance="h")
    await mgr.spawn("second", session_key="web:s1", agent="replayed", instance="h")
    await asyncio.gather(*list(mgr._running_tasks.values()), return_exceptions=True)

    # Serialized end to end, not overlapped.
    assert order == ["start:first", "end:first", "start:second", "end:second"]
    state = mgr.instance_state("web:s1", "replayed", "h")
    assert state is not None
    assert [m["content"] for m in state.load()] == ["first", "second"]


def test_the_registered_dag_tool_can_reach_a_human_for_the_confirm_gate(tmp_path: Path) -> None:
    """The gate is only a gate if the tool the *model* calls has an asker.

    It did not: the asker was built as a closure inside the playbook wiring, which
    only runs when `playbooks.enabled`, and only the executor's private tool
    instance got it. So a model-composed graph with `confirm: true` -- the schema
    invites it for "publishing, sending, spending" -- dispatched every node and
    reported that a human had approved something no human saw.
    """
    loop = _make_agent_loop(tmp_path, [])
    tool = loop.tools.get("run_subagent_dag")

    assert tool is not None
    assert tool._ask is not None, "the model-facing DAG tool has no route to a human"
    # The same asker the playbook path uses, so one answer means one thing.
    assert tool._ask == loop._confirm_graph


async def test_the_old_agent_keyword_still_names_the_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The pre-rename spelling must not resolve to a different agent in silence.

    ``execute`` takes ``**kwargs``, so ``agent=`` from a stale caller lands there
    and leaves ``subagent`` unset -- which the manager reads as "no name given"
    and answers with the generic built-in row. The task then runs, reports
    success, and was carried out by an agent nobody asked for. Two of this file's
    own call sites did exactly that and stayed green, which is why the case is
    worth a test rather than a comment.
    """
    captured: dict[str, Any] = {}

    class _Mgr:
        def list_agents(self):
            return [third_party_agent_meta(ThirdPartyCliSubagentConfig(name="claude_code", command="cat"))]

        async def spawn(self, **kw):
            captured.update(kw)
            return "started"

    tool = SpawnTool(manager=_Mgr())
    assert await tool.execute(task="do it", agent="claude_code") == "started"
    assert captured["agent"] == "claude_code"


def test_cli_agent_accepts_a_declared_everos_identity() -> None:
    cfg = ThirdPartyCliSubagentConfig(
        name="Raven-Code",
        command="run.py --session {agent_id}",
        resume_command="run.py --session {agent_id}",
        everos={"userId": "raven-code", "agentId": "raven-code"},
    )
    assert cfg.everos is not None
    assert cfg.everos.user_id == "raven-code"
    assert cfg.everos.agent_id == "raven-code"
    assert cfg.everos.base_url is None
    assert cfg.everos.session_prefix == "cli:"


def test_cli_agent_without_an_everos_block_declares_none() -> None:
    cfg = ThirdPartyCliSubagentConfig(name="Coder", command="claude-acp")
    assert cfg.everos is None


def test_an_everos_block_naming_no_owner_is_rejected() -> None:
    with pytest.raises(ValidationError, match="userId or agentId"):
        ThirdPartyCliSubagentConfig(
            name="Raven-Code",
            command="run.py",
            everos={"baseUrl": "http://localhost:18791"},
        )


class TestEverosSource:
    """`everos.source` says whether the agent writes its own memories."""

    def test_source_defaults_to_agent(self) -> None:
        cfg = SubagentEverosConfig(agent_id="raven-code")
        assert cfg.source == "agent"

    def test_trace_source_accepted_with_both_owners(self) -> None:
        cfg = SubagentEverosConfig(user_id="liv", agent_id="coder", source="trace")
        assert cfg.source == "trace"

    def test_trace_source_needs_both_owners(self) -> None:
        # Extraction writes a `user` row and `assistant`/`tool` rows, which land
        # on two different owners; one id alone silently drops half the payload.
        with pytest.raises(ValidationError, match="trace"):
            SubagentEverosConfig(agent_id="coder", source="trace")
        with pytest.raises(ValidationError, match="trace"):
            SubagentEverosConfig(user_id="liv", source="trace")

    def test_agent_source_still_needs_only_one_owner(self) -> None:
        assert SubagentEverosConfig(agent_id="raven-code").agent_id == "raven-code"
        assert SubagentEverosConfig(user_id="liv").user_id == "liv"

    def test_unknown_source_rejected(self) -> None:
        with pytest.raises(ValidationError):
            SubagentEverosConfig(agent_id="coder", source="guess")


class TestAcpEverosIdentity:
    """An acp entry may declare an everos identity."""

    def test_acp_accepts_everos(self) -> None:
        cfg = ThirdPartyAcpSubagentConfig(
            name="Coder",
            command="hermes acp",
            everos={"userId": "liv", "agentId": "coder", "source": "trace"},
        )
        assert cfg.everos is not None
        assert cfg.everos.agent_id == "coder"
        assert cfg.everos.source == "trace"

    def test_acp_everos_defaults_to_none(self) -> None:
        cfg = ThirdPartyAcpSubagentConfig(name="Coder", command="hermes acp")
        assert cfg.everos is None

    def test_everos_is_not_an_unsupported_acp_field(self) -> None:
        # The seven in ACP_UNSUPPORTED_FIELDS are declarations the `initialize`
        # handshake makes instead. An everos identity is host-side addressing,
        # which no handshake reports.
        assert "everos" not in ACP_UNSUPPORTED_FIELDS

    def test_openai_accepts_everos(self) -> None:
        # The openai backend calls turn_rows.rows on both its streaming and
        # buffered paths, the same builder acp uses, so its turn is as
        # extractable as an acp one.
        cfg = ThirdPartyOpenAISubagentConfig(
            name="Researcher",
            base_url="http://127.0.0.1:8000/v1",
            model="mirothinker",
            everos={"userId": "liv", "agentId": "researcher", "source": "trace"},
        )
        assert cfg.everos is not None
        assert cfg.everos.source == "trace"

    def test_openai_everos_defaults_to_none(self) -> None:
        cfg = ThirdPartyOpenAISubagentConfig(
            name="Researcher", base_url="http://127.0.0.1:8000/v1", model="mirothinker"
        )
        assert cfg.everos is None

    def test_acp_everos_does_not_warn(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level("WARNING"):
            ThirdPartyAcpSubagentConfig(
                name="Coder",
                command="hermes acp",
                everos={"userId": "liv", "agentId": "coder"},
            )
        assert "everos" not in caplog.text
