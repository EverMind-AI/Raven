"""Third-party subagent backends + manager wiring + spawn tool (req5, P3b)."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
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
from raven.agent.subagent.manager import SubagentManager
from raven.agent.subagent.presets import third_party_subagent_preset, third_party_subagent_presets
from raven.agent.tools.spawn import SpawnTool
from raven.config.schema import (
    SubagentsConfig,
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


def _mgr(tmp_path: Path, third_party=None, **kwargs) -> SubagentManager:
    return SubagentManager(
        provider=_FakeProvider(),
        workspace=tmp_path,
        model="fake-model",
        third_party_subagents=third_party or [],
        **kwargs,
    )


def test_manager_resolves_backends(tmp_path: Path) -> None:
    cli = ThirdPartyCliSubagentConfig(name="claude_code", command="claude -p {prompt}", description="Claude Code")
    oa = ThirdPartyOpenAISubagentConfig(name="mirothinker", base_url="http://x/v1", model="m")
    mgr = _mgr(tmp_path, [cli, oa])

    assert isinstance(mgr._resolve_backend("claude_code"), CliAgentBackend)
    assert isinstance(mgr._resolve_backend("mirothinker"), OpenAIApiBackend)
    # Unknown / None -> the default raven-loop backend.
    assert mgr._resolve_backend(None) is mgr._raven_backend
    assert mgr._resolve_backend("nope") is mgr._raven_backend
    names = [a.name for a in mgr.list_third_party_agents()]
    assert names == ["claude_code", "mirothinker"]


def test_manager_lists_stateful_flag(tmp_path: Path) -> None:
    stateless = ThirdPartyCliSubagentConfig(name="codex", command="codex exec {prompt}")
    stateful = ThirdPartyCliSubagentConfig(
        name="claude_code",
        command="claude -p {prompt} --session-id {agent_id}",
        resume_command="claude -p {prompt} --resume {agent_id}",
    )
    mgr = _mgr(tmp_path, [stateless, stateful])
    assert mgr.list_third_party_agents() == [
        AgentMeta("codex", "", False, True),
        AgentMeta("claude_code", "", True, True),
    ]


def test_manager_skips_bad_third_party_entry(tmp_path: Path) -> None:
    class _Bad:
        name = "bad"
        kind = "nope"

    mgr = _mgr(tmp_path, [_Bad()])
    assert mgr.list_third_party_agents() == []  # bad entry skipped, manager still builds


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
        remote = ThirdPartyOpenAISubagentConfig(name="miro", base_url="http://x", model="m")
        assert third_party_agent_meta(stateful) == AgentMeta("claude_code", "Claude Code", True, True, False)
        assert third_party_agent_meta(stateless) == AgentMeta("codex", "", False, True, False)
        assert third_party_agent_meta(boxed) == AgentMeta("boxed", "", False, False, False)
        # An HTTP endpoint is remote by default, so it reads no local path.
        assert third_party_agent_meta(remote) == AgentMeta("miro", "", False, False, False)

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
    assert [m.name for m in mgr.list_third_party_agents()] == ["on"]


def test_spawn_tool_listing_omits_a_disabled_agent(tmp_path: Path) -> None:
    # This is the assertion that actually protects the tool description the model
    # reads: a disabled agent must not appear in the roster it chooses from.
    on = ThirdPartyCliSubagentConfig(name="on", description="stays", command="echo {prompt}")
    off = ThirdPartyCliSubagentConfig(name="off", description="goes", command="echo {prompt}", enabled=False)
    mgr = _mgr(tmp_path, [on, off])
    listing = format_agent_listing(mgr.list_third_party_agents())
    assert "on" in listing
    assert "off" not in listing


def test_dag_tool_roster_omits_a_disabled_agent(tmp_path: Path) -> None:
    from raven.agent.subagent_dag.tool import SubAgentDagTool

    on = ThirdPartyCliSubagentConfig(name="on", command="echo {prompt}")
    off = ThirdPartyCliSubagentConfig(name="off", command="echo {prompt}", enabled=False)
    tool = SubAgentDagTool(workspace=tmp_path, third_party_subagents=[on, off])
    assert [m.name for m in tool._subagent_meta] == ["on"]
    assert set(tool._subagents) == {"on"}


def test_dag_tool_with_everything_disabled_has_an_empty_roster(tmp_path: Path) -> None:
    # Reachable by switch now, not only by deleting entries, so it must degrade to
    # "no agents" rather than raise.
    from raven.agent.subagent_dag.tool import SubAgentDagTool

    off = ThirdPartyCliSubagentConfig(name="off", command="echo {prompt}", enabled=False)
    tool = SubAgentDagTool(workspace=tmp_path, third_party_subagents=[off])
    assert tool._subagent_meta == []
    assert tool._subagents == {}


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
        third_party_subagents=third_party,
    )


def test_dag_tool_not_registered_when_the_only_agent_is_disabled(tmp_path: Path) -> None:
    # A raw config list is non-empty here, but every entry is disabled: the
    # roster the tool would advertise is empty, so it must not register at all
    # -- matching the empty-config case rather than diverging from it.
    off = ThirdPartyCliSubagentConfig(name="off", command="echo {prompt}", enabled=False)
    loop = _make_agent_loop(tmp_path, [off])
    assert loop.tools.get("run_subagent_dag") is None


def test_dag_tool_registered_when_at_least_one_agent_is_enabled(tmp_path: Path) -> None:
    on = ThirdPartyCliSubagentConfig(name="on", command="echo {prompt}")
    off = ThirdPartyCliSubagentConfig(name="off", command="echo {prompt}", enabled=False)
    loop = _make_agent_loop(tmp_path, [on, off])
    assert loop.tools.get("run_subagent_dag") is not None


def test_apply_third_party_subagents_does_not_register_dag_tool_for_disabled_only(
    tmp_path: Path,
) -> None:
    # Hot-apply hits the same gate as construction: going from no config to a
    # disabled-only config must not register the tool either.
    loop = _make_agent_loop(tmp_path, [])
    off = ThirdPartyCliSubagentConfig(name="off", command="echo {prompt}", enabled=False)
    loop.apply_third_party_subagents([off])
    assert loop.tools.get("run_subagent_dag") is None


def test_apply_third_party_subagents_registers_dag_tool_once_enabled(tmp_path: Path) -> None:
    loop = _make_agent_loop(tmp_path, [])
    on = ThirdPartyCliSubagentConfig(name="on", command="echo {prompt}")
    loop.apply_third_party_subagents([on])
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
        loop.apply_third_party_subagents([on])

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
    assert "agent" in params["properties"]
    assert params["properties"]["agent"]["enum"] == ["claude_code"]
    assert "claude_code" in tool.description


def test_spawn_tool_no_agent_param_when_none(tmp_path: Path) -> None:
    tool = SpawnTool(manager=_mgr(tmp_path, []))
    assert "agent" not in tool.parameters["properties"]


def test_spawn_tool_points_at_the_dag_when_a_roster_exists(tmp_path: Path) -> None:
    """Repeated spawns for one task are the shape a DAG expresses, and this
    description is where the model is standing when it makes that mistake."""
    cli = ThirdPartyCliSubagentConfig(name="claude_code", command="claude -p {prompt}")
    assert "run_subagent_dag" in SpawnTool(manager=_mgr(tmp_path, [cli])).description


def test_spawn_tool_omits_the_dag_pointer_without_a_roster(tmp_path: Path) -> None:
    """``run_subagent_dag`` is registered off the same enabled roster this
    listing is built from, so an empty roster means the pointer would name a
    tool the model cannot call."""
    assert "run_subagent_dag" not in SpawnTool(manager=_mgr(tmp_path, [])).description


async def test_spawn_tool_forwards_agent(tmp_path: Path) -> None:
    cli = ThirdPartyCliSubagentConfig(name="claude_code", command="cat")
    mgr = _mgr(tmp_path, [cli])
    captured = {}

    async def fake_spawn(**kwargs):
        captured.update(kwargs)
        return "started"

    mgr.spawn = fake_spawn  # type: ignore[method-assign]
    tool = SpawnTool(manager=mgr)
    await tool.execute(task="do it", agent="claude_code")
    assert captured["agent"] == "claude_code"
    assert captured["task"] == "do it"


def test_spawn_tool_exposes_instance_param_only_when_stateful(tmp_path: Path) -> None:
    stateless = ThirdPartyCliSubagentConfig(name="codex", command="codex exec {prompt}")
    assert "instance" not in SpawnTool(manager=_mgr(tmp_path, [stateless])).parameters["properties"]

    stateful = ThirdPartyCliSubagentConfig(
        name="claude_code",
        command="claude -p {prompt} --session-id {agent_id}",
        resume_command="claude -p {prompt} --resume {agent_id}",
    )
    props = SpawnTool(manager=_mgr(tmp_path, [stateful])).parameters["properties"]
    assert "instance" in props
    assert props["instance"]["type"] == "string"
    # With a mixed roster the param exists, so it names who may receive it.
    assert "['claude_code']" in props["instance"]["description"]


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

        out = await tool.execute(task="do it", agent="codex", instance="author")

        assert out.startswith("Error:")
        assert "stateless" in out
        assert "['claude_code']" in out  # who can, not just who cannot
        assert spawned == []

    async def test_handle_without_an_agent_spawns_nothing(self, tmp_path: Path) -> None:
        """A default Raven subagent has no resumable session either, and the
        manager only keys an instance row when `agent` is set."""
        tool = self._tool(tmp_path)
        spawned: list[dict] = []
        tool._manager.spawn = lambda **kw: spawned.append(kw)  # type: ignore[method-assign]

        out = await tool.execute(task="do it", instance="author")

        assert out.startswith("Error:")
        assert spawned == []

    async def test_handle_on_a_stateful_agent_is_forwarded(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path)
        captured: dict = {}

        async def fake_spawn(**kwargs):
            captured.update(kwargs)
            return "started"

        tool._manager.spawn = fake_spawn  # type: ignore[method-assign]
        out = await tool.execute(task="do it", agent="claude_code", instance="author")

        assert out == "started"
        assert captured["instance"] == "author"

    async def test_no_handle_is_never_gated(self, tmp_path: Path) -> None:
        tool = self._tool(tmp_path)
        captured: dict = {}

        async def fake_spawn(**kwargs):
            captured.update(kwargs)
            return "started"

        tool._manager.spawn = fake_spawn  # type: ignore[method-assign]
        assert await tool.execute(task="do it", agent="codex") == "started"
        assert captured["instance"] is None


async def test_manager_forwards_instance_to_backend(tmp_path: Path) -> None:
    seen: dict[str, Any] = {}

    class _Recorder:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None, **_):
            seen["session_key"] = session_key
            seen["instance"] = instance
            return "ok"

    mgr = _mgr(tmp_path, [])
    mgr._backends["rec"] = _Recorder()
    mgr.set_submit(lambda req: None)
    await mgr.spawn("t", session_key="web:s1", agent="rec", instance="refactor-auth")
    for task in list(mgr._running_tasks.values()):
        await task
    assert seen == {"session_key": "web:s1", "instance": "refactor-auth"}


# --- presets -------------------------------------------------------------


# --- transcript parsing --------------------------------------------------

from raven.agent.subagent.backends.transcript import (
    parse_claude_stream_json,
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


def test_http_agent_cannot_declare_itself_stateful() -> None:
    with pytest.raises(ValidationError):
        ThirdPartyOpenAISubagentConfig(name="x", base_url="http://x", model="m", stateful=True)


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
    cfg = SubagentsConfig(third_party=list(presets.values()))
    assert len(cfg.third_party) == len(presets)
    # ... and each one builds into a concrete backend.
    for entry in cfg.third_party:
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
    cfg = SubagentsConfig(third_party=[entry]).third_party[0]
    assert cfg.name == "frontend_reviewer"
    assert cfg.preset == "claude_code"
    # Round-trips under the wire alias, which is what the gateway persists.
    assert cfg.model_dump(by_alias=True)["preset"] == "claude_code"


def test_hand_written_entry_has_no_provenance() -> None:
    cfg = SubagentsConfig(third_party=[{"name": "mine", "kind": "cli", "command": "echo hi"}])
    assert cfg.third_party[0].preset is None


def test_provenance_backfilled_when_name_matches_a_preset_cli() -> None:
    # A preset entry written by an earlier build has no `preset` field yet, so
    # backfilling by name is what lets it be edited and saved again.
    cfg = SubagentsConfig(
        third_party=[{"name": "claude_code", "kind": "cli", "command": "claude -p {prompt}"}]
    ).third_party[0]
    assert cfg.preset == "claude_code"


def test_provenance_backfilled_when_name_matches_a_preset_openai() -> None:
    cfg = SubagentsConfig(
        third_party=[
            {
                "name": "mirothinker",
                "kind": "openai",
                "baseUrl": "https://api.miromind.ai/v1",
                "model": "mirothinker-1-7-deepresearch",
            }
        ]
    ).third_party[0]
    assert cfg.preset == "mirothinker"


def test_provenance_not_guessed_for_a_renamed_hand_written_entry() -> None:
    # `Coder` is evidently a renamed preset on this machine, but its origin is
    # genuinely unknowable from `command` alone and must not be guessed.
    cfg = SubagentsConfig(third_party=[{"name": "Coder", "kind": "cli", "command": "echo hi"}]).third_party[0]
    assert cfg.preset is None


def test_explicit_provenance_is_left_untouched() -> None:
    entry = {
        "name": "frontend_reviewer",
        "kind": "cli",
        "command": "echo hi",
        "preset": "claude_code",
    }
    cfg = SubagentsConfig(third_party=[entry]).third_party[0]
    assert cfg.preset == "claude_code"


def test_unknown_preset_value_is_rejected() -> None:
    from pydantic import ValidationError

    entry = {"name": "mine", "kind": "cli", "command": "echo hi", "preset": "not-a-real-preset"}
    with pytest.raises(ValidationError, match="not-a-real-preset"):
        SubagentsConfig(third_party=[entry])


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
    mgr._backends["hang"] = _Hang()
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
    for i, ev in enumerate(hang_started):
        mgr._backends[f"hang{i}"] = _Hang(ev)
    mgr._backends["canary"] = _Canary()
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
    """Two spawns can race onto the same (agent, instance) key before the
    first completes; `_instance_tasks` must hold both task ids rather than
    letting the second overwrite the first, and cancel_by_instance must reach
    both -- proven observably by filling the gate with the pair and confirming
    a third, gate-blocked spawn then starts once the instance is cancelled."""
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
    mgr._backends["hang"] = _Hang()
    mgr._backends["canary"] = _Canary()
    mgr.set_submit(lambda req: None)

    await mgr.spawn("t1", session_key="web:s1", agent="hang", instance="reviewer")
    await mgr.spawn("t2", session_key="web:s1", agent="hang", instance="reviewer")
    await asyncio.wait_for(asyncio.gather(*(e.wait() for e in hang_started)), timeout=5)

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
    mgr._backends["hang"] = _Hang()
    mgr._backends["queued"] = _Queued()
    mgr._backends["canary"] = _Canary()
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
    mgr._backends["hang"] = _Hang()
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
