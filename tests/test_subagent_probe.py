"""Free availability probe for third-party subagents (cli PATH + openai /models)."""

from __future__ import annotations

import asyncio
import os
import socket
from contextlib import closing
from pathlib import Path

import pytest
from aiohttp import web

import raven.agent.subagent.backends.env as env_mod
import raven.agent.subagent.probe as probe_mod
from raven.agent.subagent import instances as instances_mod
from raven.agent.subagent.probe import probe_all, probe_one, run_test
from raven.config.schema import (
    Config,
    ThirdPartyAcpSubagentConfig,
    ThirdPartyCliSubagentConfig,
    ThirdPartyOpenAISubagentConfig,
)


def _free_port() -> int:
    with closing(socket.socket()) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(autouse=True)
def _isolated_instance_registry(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """No test here may touch the real ~/.raven/subagent_instances.json.

    Same reasoning as the fixture of this name in
    `tests/test_subagent_third_party.py`: a stateful create commits a handle
    binding through the process-wide `get_registry()` singleton, so without
    this every run would accumulate junk rows in the user's real file.
    """
    monkeypatch.setattr(
        instances_mod, "_registry", instances_mod.InstanceRegistry(path=tmp_path / "_autouse_inst.json")
    )


def _cli(command: str, name: str = "agent") -> ThirdPartyCliSubagentConfig:
    return ThirdPartyCliSubagentConfig(name=name, command=command)


def _openai(base_url: str, model: str = "m1", api_key: str = "k") -> ThirdPartyOpenAISubagentConfig:
    return ThirdPartyOpenAISubagentConfig(name="api", base_url=base_url, model=model, api_key=api_key)


# --- cli probe -----------------------------------------------------------


async def test_cli_probe_reports_the_resolved_absolute_path(tmp_path: Path) -> None:
    exe = tmp_path / "faux-agent"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    res = await probe_one(_cli("faux-agent -p {prompt}"), source="config", path=str(tmp_path))
    assert res.status == "ready"
    assert res.target == str(exe)
    assert res.kind == "cli"
    assert res.source == "config"


async def test_a_missing_executable_says_what_installs_it(tmp_path: Path) -> None:
    """The install belongs on the surface a person reads, not in the roster prose.

    ``which`` can only report the executable it looked for, which is not the
    package that ships it: ``qodercli`` does not spell ``@qoder-ai/qodercli``.
    Carried by the row's provenance rather than its name, so an owner who renamed
    the row keeps the hint.
    """
    cfg = ThirdPartyAcpSubagentConfig(name="Renamed By Its Owner", preset="qoder", command="qodercli --acp")
    res = await probe_one(cfg, source="config", path=str(tmp_path))
    assert res.status == "missing"
    assert res.target == "qodercli"
    assert "npm i -g @qoder-ai/qodercli" in res.detail


async def test_a_missing_executable_invents_no_install_it_does_not_know(tmp_path: Path) -> None:
    """A hand-written row is not the preset whose name it happens to wear.

    Its command is the owner's, so naming a package would send them to install
    something they never asked for. The executable name already reported is what
    they search with instead.
    """
    cfg = ThirdPartyAcpSubagentConfig(name="Qoder", command="my-own-agent --acp")
    res = await probe_one(cfg, source="config", path=str(tmp_path))
    assert res.status == "missing"
    assert res.detail == "my-own-agent is not on the login shell PATH"
    assert "npm" not in res.detail


def _fake_executable(tmp_path: Path, name: str) -> Path:
    exe = tmp_path / name
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    return exe


async def test_a_shim_row_is_missing_when_the_agent_it_drives_is_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``npx`` resolving says nothing about whether ``pi`` is installed.

    The shim is fetched on connect and fails a minute later with "executable not
    found" when the agent it drives is absent. That is the sentence the probe can
    say up front, with the install beside it, so the row reads absent rather
    than connectable -- the same reading a local-executable row gets.
    """
    _fake_executable(tmp_path, "npx")
    monkeypatch.setattr(probe_mod, "acp_snapshot_for", lambda cfg: None)
    cfg = ThirdPartyAcpSubagentConfig(name="Pi", preset="pi", command="npx -y pi-acp@0.0.33")

    res = await probe_one(cfg, source="preset", path=str(tmp_path))

    assert res.status == "missing"
    assert res.target == "pi"
    assert res.detail == (
        "pi is not on the login shell PATH; install with npm install -g @earendil-works/pi-coding-agent"
    )


async def test_a_shim_row_whose_agent_is_installed_goes_on_to_the_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    npx = _fake_executable(tmp_path, "npx")
    _fake_executable(tmp_path, "pi")
    monkeypatch.setattr(probe_mod, "acp_snapshot_for", lambda cfg: None)
    cfg = ThirdPartyAcpSubagentConfig(name="Pi", preset="pi", command="npx -y pi-acp@0.0.33")

    res = await probe_one(cfg, source="preset", path=str(tmp_path))

    assert res.status == "attention"
    assert res.target == str(npx)


async def test_a_shim_that_ships_its_own_agent_asks_after_nothing_else(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``codex-acp`` bundles the agent as its own binary; what it wants is a login, not an install."""
    _fake_executable(tmp_path, "npx")
    monkeypatch.setattr(probe_mod, "acp_snapshot_for", lambda cfg: None)
    cfg = ThirdPartyAcpSubagentConfig(
        name="Codex", preset="codex", command="npx -y @agentclientprotocol/codex-acp@1.1.14"
    )

    res = await probe_one(cfg, source="preset", path=str(tmp_path))

    assert res.status == "attention"


async def test_a_hand_written_npx_row_is_not_held_to_a_shims_requirement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The requirement rides on provenance: a row that merely wears the preset's name runs its own command."""
    _fake_executable(tmp_path, "npx")
    monkeypatch.setattr(probe_mod, "acp_snapshot_for", lambda cfg: None)
    cfg = ThirdPartyAcpSubagentConfig(name="Pi", command="npx -y my-own-acp-shim@1.0.0")

    res = await probe_one(cfg, source="config", path=str(tmp_path))

    assert res.status == "attention"


async def test_cli_probe_reports_missing_and_still_names_what_it_looked_for(tmp_path: Path) -> None:
    res = await probe_one(_cli("definitely-not-installed {prompt}"), source="config", path=str(tmp_path))
    assert res.status == "missing"
    # A `missing` result still names the executable, or the user cannot tell
    # which of several tokens in the command was the one not found.
    assert res.target == "definitely-not-installed"
    assert "definitely-not-installed" in res.detail


async def test_cli_probe_reports_unparseable_command_as_unknown() -> None:
    res = await probe_one(_cli("claude -p 'unbalanced {prompt}"), source="config", path="/usr/bin")
    assert res.status == "unknown"
    assert "cannot be parsed" in res.detail


async def test_cli_probe_reports_a_placeholder_executable_as_unknown() -> None:
    # `{prompt}` as argv[0] names no executable, so "not installed" would be a
    # wrong diagnosis of a malformed command.
    res = await probe_one(_cli("{prompt} --go"), source="config", path="/usr/bin")
    assert res.status == "unknown"
    assert "placeholder" in res.detail


async def test_cli_probe_resolves_on_the_login_shell_path_not_ravens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The whole point: `CliAgentBackend._exec` runs the child under
    # `login_shell_env()`, so a probe reading os.environ would report an agent
    # missing while real dispatches find it (codex lives on the login PATH only).
    exe = tmp_path / "login-only-agent"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setattr(probe_mod, "_login_path", lambda: str(tmp_path))
    res = await probe_one(_cli("login-only-agent {prompt}"), source="config")
    assert res.status == "ready"


async def test_cli_probe_honors_a_configured_path_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # CliAgentBackend._exec builds the child's env as {**login_shell_env(), **self.env}
    # (cli_agent.py:139-140), so a configured PATH wins over the login shell's for a
    # real spawn. The probe must resolve the same way, or a subagent with an env PATH
    # override would be reported missing while a real spawn finds it fine.
    login_only = tmp_path / "login_only"
    login_only.mkdir()
    override = tmp_path / "override"
    override.mkdir()
    exe = override / "custom-agent"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    cfg = ThirdPartyCliSubagentConfig(name="agent", command="custom-agent {prompt}", env={"PATH": str(override)})
    res = await probe_one(cfg, source="config", path=str(login_only))
    assert res.status == "ready"
    assert res.target == str(exe)


# --- openai probe --------------------------------------------------------


async def _serve(handler, route: str = "/v1/models", method: str = "GET") -> tuple[str, web.AppRunner, list[str]]:
    """Start a stub endpoint; returns (base_url, runner, recorded request paths)."""
    seen: list[str] = []

    async def wrapped(request: web.Request):
        seen.append(request.path)
        return await handler(request)

    port = _free_port()
    app = web.Application()
    app.router.add_route(method, route, wrapped)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return f"http://127.0.0.1:{port}/v1", runner, seen


async def test_openai_probe_ready_when_the_model_is_listed() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"object": "list", "data": [{"id": "m1"}, {"id": "m2"}]})

    base, runner, _ = await _serve(handler)
    try:
        res = await probe_one(_openai(base, model="m1"), source="config")
        assert res.status == "ready"
        assert res.target == base
    finally:
        await runner.cleanup()


async def test_openai_probe_flags_a_model_absent_from_the_list() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": "m1"}, {"id": "m2"}]})

    base, runner, _ = await _serve(handler)
    try:
        res = await probe_one(_openai(base, model="typo-model"), source="config")
        assert res.status == "attention"
        assert "typo-model" in res.detail
    finally:
        await runner.cleanup()


async def test_openai_probe_is_ready_but_honest_when_the_body_has_no_model_list() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    base, runner, _ = await _serve(handler)
    try:
        res = await probe_one(_openai(base), source="config")
        assert res.status == "ready"
        assert "unverified" in res.detail
    finally:
        await runner.cleanup()


async def test_openai_probe_reports_a_rejected_key() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"error": "invalid api key"}, status=401)

    base, runner, _ = await _serve(handler)
    try:
        res = await probe_one(_openai(base), source="config")
        assert res.status == "attention"
        assert "401" in res.detail
    finally:
        await runner.cleanup()


async def test_openai_probe_reports_a_missing_models_endpoint_as_unverified() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.Response(status=404)

    base, runner, _ = await _serve(handler)
    try:
        res = await probe_one(_openai(base), source="config")
        assert res.status == "attention"
        assert "unverified" in res.detail
    finally:
        await runner.cleanup()


async def test_openai_probe_reports_an_unreachable_endpoint_as_missing() -> None:
    res = await probe_one(_openai(f"http://127.0.0.1:{_free_port()}/v1"), source="config")
    assert res.status == "missing"
    assert "unreachable" in res.detail


async def test_openai_probe_honors_env_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    # Same guarantee `test_openai_backend_honors_env_proxy` pins for the backend,
    # and it must hold here too: aiohttp ignores HTTP_PROXY unless trust_env is
    # set, so on a host that can only reach the provider through a proxy a probe
    # without it would report a working agent unreachable.
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": "m1"}]})

    proxy_base, runner, _ = await _serve(handler, route="/{tail:.*}")
    dead_port = _free_port()
    for var in ("no_proxy", "NO_PROXY", "https_proxy", "HTTPS_PROXY", "HTTP_PROXY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("http_proxy", proxy_base.removesuffix("/v1"))
    try:
        res = await probe_one(_openai(f"http://127.0.0.1:{dead_port}/v1", model="m1"), source="config")
        assert res.status == "ready"
    finally:
        await runner.cleanup()


async def test_openai_probe_sends_nothing_for_a_keyless_preset() -> None:
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"data": []})

    base, runner, seen = await _serve(handler)
    try:
        res = await probe_one(_openai(base, api_key=""), source="preset")
        assert res.status == "attention"
        assert seen == []  # a template must not fire a request that is certain to 401
    finally:
        await runner.cleanup()


async def test_openai_probe_does_send_for_a_keyless_configured_entry() -> None:
    # A keyless endpoint is legitimate (a local vLLM), so short-circuiting a
    # configured entry would report a working agent as broken.
    async def handler(request: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": "m1"}]})

    base, runner, seen = await _serve(handler)
    try:
        res = await probe_one(_openai(base, api_key=""), source="config")
        assert res.status == "ready"
        assert seen == ["/v1/models"]
    finally:
        await runner.cleanup()


# --- capability limits ---------------------------------------------------


def test_openai_config_coerces_declared_local_file_access_away() -> None:
    # `reads_local_files` is rendered into the spawn / DAG tool descriptions as a
    # `local-files` tag, so honouring it here would licence the dispatching model
    # to hand this agent a path that nothing can open. It is coerced rather than
    # rejected because the older web form defaulted the checkbox to true and
    # rendered it for both kinds: rejecting on load would raise inside the whole
    # top-level Config for anyone with such an entry stored, so raven would stop
    # starting and the UI that could fix the field sits behind that config.
    cfg = ThirdPartyOpenAISubagentConfig(name="api", base_url="http://x/v1", model="m", reads_local_files=True)
    assert cfg.reads_local_files is False


def test_a_stored_openai_entry_with_local_file_access_still_loads() -> None:
    # The regression this guards is startup, not the field: one such entry used
    # to fail the entire Config, taking every unrelated section with it.
    config = Config.model_validate(
        {
            "subagents": {
                "thirdParty": [
                    {
                        "name": "legacy",
                        "kind": "openai",
                        "baseUrl": "http://x/v1",
                        "model": "m",
                        "readsLocalFiles": True,
                    }
                ]
            }
        }
    )
    assert config.subagents.agents[0].reads_local_files is False


def test_openai_config_accepts_false_and_omitted() -> None:
    assert ThirdPartyOpenAISubagentConfig(name="a", base_url="http://x/v1", model="m").reads_local_files is False
    explicit = ThirdPartyOpenAISubagentConfig(name="b", base_url="http://x/v1", model="m", reads_local_files=False)
    assert explicit.reads_local_files is False


def test_cli_config_still_accepts_local_file_access() -> None:
    # A cli agent is a local subprocess, so the declaration is real there.
    assert ThirdPartyCliSubagentConfig(name="c", command="echo {prompt}", reads_local_files=True).reads_local_files


# --- batch ---------------------------------------------------------------


async def test_probe_all_preserves_order_and_captures_the_path_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[int] = []

    def counted() -> str:
        calls.append(1)
        return str(tmp_path)

    monkeypatch.setattr(probe_mod, "_login_path", counted)
    entries = [(_cli("a {prompt}", name="a"), "config"), (_cli("b {prompt}", name="b"), "preset")]
    results = await probe_all(entries)
    assert [r.name for r in results] == ["a", "b"]
    assert [r.source for r in results] == ["config", "preset"]
    # login_shell_env shells out to `bash -lic` and can block for seconds on its
    # first call; per-agent capture would serialise the whole batch behind it.
    assert len(calls) == 1


async def test_probe_all_survives_a_raising_login_path_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    # Neither probe_one nor probe_all may raise, on any path -- including the
    # shared PATH capture, which today only degrades (login_shell_env falls
    # back to os.environ) but is not guaranteed to.
    def boom() -> str:
        raise OSError("no bash")

    monkeypatch.setattr(probe_mod, "_login_path", boom)
    entries = [(_cli("a {prompt}", name="a"), "config"), (_cli("b {prompt}", name="b"), "preset")]
    results = await probe_all(entries)
    assert [r.name for r in results] == ["a", "b"]
    for res in results:
        assert res.status in {"ready", "attention", "missing", "unknown"}


async def test_probe_result_wire_shape_is_camel_case(tmp_path: Path) -> None:
    res = await probe_one(_cli("nope {prompt}"), source="config", path=str(tmp_path))
    assert set(res.to_wire()) == {"name", "source", "kind", "status", "detail", "target", "elapsedMs", "lastTest"}


async def test_probe_all_attaches_a_verdict_to_the_matching_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from raven.agent.subagent.probe_state import LastTest

    monkeypatch.setattr(probe_mod, "_login_path", lambda: str(tmp_path))
    a = _cli("a {prompt}", name="a")
    b = _cli("b {prompt}", name="b")
    verdicts = {"config:a": LastTest(ok=False, detail="auth failed", tested_at_ms=1700)}
    results = await probe_all([(a, "config"), (b, "config")], verdicts=verdicts)
    assert results[0].last_test == LastTest(ok=False, detail="auth failed", tested_at_ms=1700)
    assert results[1].last_test is None


async def test_probe_wire_shape_carries_last_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.agent.subagent.probe_state import LastTest

    monkeypatch.setattr(probe_mod, "_login_path", lambda: str(tmp_path))
    cfg = _cli("nope {prompt}", name="a")
    [bare] = await probe_all([(cfg, "config")])
    assert bare.to_wire()["lastTest"] is None
    [withv] = await probe_all([(cfg, "config")], verdicts={"config:a": LastTest(True, "ran", 42)})
    assert withv.to_wire()["lastTest"] == {"ok": True, "detail": "ran", "testedAtMs": 42}


# --- explicit test -------------------------------------------------------


def _patch_login_env_for_spawn(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Point both the probe's PATH read and a real spawn's child env at ``tmp_path``.

    Patching ``probe_mod._login_path`` alone (as the probe-only tests above do)
    only steers ``probe_one``: ``CliAgentBackend._exec`` builds its child's
    environment from ``login_shell_env()`` directly, never through anything the
    probe passes, so a run that reaches a real spawn needs the shared cache both
    layers read from patched instead - the same pattern
    ``test_subagent_third_party.py``'s ``_clear_login_env_cache`` fixture uses.
    """
    monkeypatch.setattr(
        env_mod,
        "_LOGIN_ENV",
        {"PATH": f"{tmp_path}:/usr/bin:/bin", "HOME": os.environ.get("HOME", "/root")},
    )


async def test_test_of_a_missing_cli_fails_without_building_a_backend(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A failed probe must short-circuit: there is nothing to execute, so no
    # backend is built and no process is spawned.
    def must_not_build(*args, **kwargs):
        raise AssertionError("a failed probe must not reach the backend")

    monkeypatch.setattr(probe_mod, "build_third_party_backend", must_not_build)
    monkeypatch.setattr(probe_mod, "_login_path", lambda: str(tmp_path))
    res = await run_test(_cli("not-installed-at-all {prompt}"), source="config")
    assert res.ok is False
    assert res.kind == "cli"
    assert "not on the login shell PATH" in res.detail


async def test_test_of_a_working_cli_returns_the_reply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    exe = tmp_path / "echo-agent"
    exe.write_text("#!/bin/sh\necho PONG\n")
    exe.chmod(0o755)
    _patch_login_env_for_spawn(monkeypatch, tmp_path)
    res = await run_test(_cli("echo-agent {prompt}"), source="config")
    assert res.ok is True
    assert res.reply == "PONG"
    assert res.elapsed_ms >= 0


async def test_test_of_a_cli_that_returns_nothing_fails(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Exit 0 with an empty answer is exactly the openclaw symptom of replying to
    # its workspace bootstrap instead of the task, so it is not a pass.
    exe = tmp_path / "silent-agent"
    exe.write_text("#!/bin/sh\nexit 0\n")
    exe.chmod(0o755)
    _patch_login_env_for_spawn(monkeypatch, tmp_path)
    res = await run_test(_cli("silent-agent {prompt}"), source="config")
    assert res.ok is False
    assert "returned nothing" in res.detail


async def test_test_of_a_failing_cli_reports_its_error_rather_than_raising(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "broken-agent"
    exe.write_text("#!/bin/sh\necho 'ProviderAuthError' >&2\nexit 1\n")
    exe.chmod(0o755)
    _patch_login_env_for_spawn(monkeypatch, tmp_path)
    res = await run_test(_cli("broken-agent {prompt}"), source="config")
    assert res.ok is False
    assert "ProviderAuthError" in res.detail


async def test_stateful_cli_test_leaves_the_real_registry_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    exe = tmp_path / "stateful-agent"
    exe.write_text("#!/bin/sh\necho PONG\n")
    exe.chmod(0o755)
    _patch_login_env_for_spawn(monkeypatch, tmp_path)
    cfg = ThirdPartyCliSubagentConfig(
        name="stateful",
        command="stateful-agent {prompt} --session-id {agent_id}",
        resume_command="stateful-agent {prompt} --resume {agent_id}",
    )
    res = await run_test(cfg, source="config")
    assert res.ok is True
    # The create commits a handle binding; it must land in the probe's throwaway
    # registry, not in the one the running gateway reads.
    assert instances_mod._registry.list_instances() == []


async def test_openai_test_never_sends_a_completion() -> None:
    async def models(request: web.Request) -> web.Response:
        return web.json_response({"data": [{"id": "m1"}]})

    seen: list[str] = []
    port = _free_port()
    app = web.Application()

    async def record_models(request: web.Request) -> web.Response:
        seen.append(request.path)
        return await models(request)

    async def record_completion(request: web.Request) -> web.Response:
        seen.append(request.path)
        return web.json_response({"choices": [{"message": {"content": "billed!"}}]})

    app.router.add_get("/v1/models", record_models)
    app.router.add_post("/v1/chat/completions", record_completion)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    try:
        res = await run_test(_openai(f"http://127.0.0.1:{port}/v1", model="m1"), source="config")
        assert res.ok is True
        assert res.reply is None
        assert seen == ["/v1/models"]
    finally:
        await runner.cleanup()


async def test_test_result_wire_shape_is_camel_case(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(probe_mod, "_login_path", lambda: str(tmp_path))
    wire = (await run_test(_cli("nope {prompt}"), source="config")).to_wire()
    assert set(wire) == {"name", "source", "kind", "ok", "detail", "reply", "elapsedMs"}


async def test_testing_a_builtin_agent_is_refused_rather_than_attempted() -> None:
    """There is nothing a test could check about an in-process agent.

    No command to launch, no endpoint to authenticate against, no transcript
    parser to exercise -- the three things a test exists to catch. Falling through
    to the cli branch did run, though: the probe now reports a built-in row
    "ready", so it reached ``build_third_party_backend``, which raises on the kind,
    and the bare-except turned that into a verdict claiming ``kind == "cli"`` with
    the detail "unknown third-party subagent kind". The caller then *records*
    verdicts, so that row would have shown a failed test permanently.
    """
    from raven.agent.subagent.probe import run_test
    from raven.config.schema import BuiltinAgentConfig

    result = await run_test(BuiltinAgentConfig(name="research-raven"), source="config")

    assert result.kind == "builtin"
    assert result.ok is False
    assert "nothing to test" in result.detail
    assert "unknown third-party" not in result.detail


# --- ping ----------------------------------------------------------------


async def test_ping_dispatches_the_probe_prompt_and_passes_on_a_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Layer 2 is a real turn, because layer 1 cannot see a credential.

    Measured 2026-09-07: six registry agents answered `initialize` and
    `session/new` and then failed the first model call, which is where their
    credential is actually checked.
    """
    sent: list[str] = []

    class _Answers:
        async def run(self, prompt, **kwargs):
            sent.append(prompt)
            return "PONG"

    monkeypatch.setattr(probe_mod, "build_third_party_backend", lambda cfg, **kw: _Answers())

    cfg = ThirdPartyAcpSubagentConfig(name="ping-acp", kind="acp", command="fake-agent acp")
    result = await probe_mod.ping_agent(cfg)

    assert sent == [probe_mod.PROBE_PROMPT]
    assert result.ok is True
    assert "replied" in result.detail


async def test_ping_fails_when_the_agent_answers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """An empty reply is a signal, not a pass -- openclaw answered its own
    workspace bootstrap instead of the task and returned nothing."""

    class _Silent:
        async def run(self, prompt, **kwargs):
            return "   "

    monkeypatch.setattr(probe_mod, "build_third_party_backend", lambda cfg, **kw: _Silent())

    cfg = ThirdPartyAcpSubagentConfig(name="silent-acp", kind="acp", command="fake-agent acp")
    result = await probe_mod.ping_agent(cfg)

    assert result.ok is False
    assert "answered nothing" in result.detail


async def test_ping_fails_and_keeps_the_reason_when_the_model_call_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The exact shape six measured agents have: layer 1 fine, model call refused.

    The reason has to survive into the detail, because it is the whole value of
    the failure -- "sign in" and "-32603" send an operator to different places.
    """

    class _Refuses:
        async def run(self, prompt, **kwargs):
            raise RuntimeError("Internal error: You need to sign in to use this model.")

    monkeypatch.setattr(probe_mod, "build_third_party_backend", lambda cfg, **kw: _Refuses())

    cfg = ThirdPartyAcpSubagentConfig(name="unauthed-acp", kind="acp", command="fake-agent acp")
    result = await probe_mod.ping_agent(cfg)

    assert result.ok is False
    assert "sign in" in result.detail


async def test_ping_fails_on_a_timeout_rather_than_waiting(monkeypatch: pytest.MonkeyPatch) -> None:
    """gemini and dirac hang rather than refuse, so an unbounded wait would sit
    on the settings switch and then say nothing useful."""

    class _Hangs:
        async def run(self, prompt, **kwargs):
            await asyncio.sleep(30)
            return "too late"

    monkeypatch.setattr(probe_mod, "build_third_party_backend", lambda cfg, **kw: _Hangs())
    monkeypatch.setattr(probe_mod, "_ENABLE_PING_TIMEOUT_SECONDS", 1)

    cfg = ThirdPartyAcpSubagentConfig(name="hanging-acp", kind="acp", command="fake-agent acp")
    result = await probe_mod.ping_agent(cfg)

    assert result.ok is False
    assert "did not answer within" in result.detail


async def test_ping_hands_the_backend_the_same_bound_it_waits_for(monkeypatch: pytest.MonkeyPatch) -> None:
    """One number, or the inner bound is unreachable: a backend allowed the
    longer explicit-Test budget can only ever be cancelled by the `wait_for`
    around it, never reach its own timeout and describe the failure.

    This half asserts what `ping_agent` asks for, and can assert nothing more:
    the builder is replaced below, so a builder that accepted both numbers and
    used neither would satisfy every line of it. The other half is
    `test_the_factory_honours_the_bounds_its_caller_overrides`, which builds for
    real and reads them back off the backend."""
    seen: dict = {}

    class _Answers:
        async def run(self, prompt, **kwargs):
            return "PONG"

    def _capture(cfg, **kw):
        seen.update(kw)
        return _Answers()

    monkeypatch.setattr(probe_mod, "build_third_party_backend", _capture)

    await probe_mod.ping_agent(ThirdPartyAcpSubagentConfig(name="ping-acp", kind="acp", command="fake-agent acp"))
    assert seen["timeout"] == probe_mod._ENABLE_PING_TIMEOUT_SECONDS

    # A row that asks for less than the cap still gets its own number.
    tight = ThirdPartyAcpSubagentConfig(name="ping-tight", kind="acp", command="fake-agent acp", timeout=5)
    await probe_mod.ping_agent(tight)
    assert seen["timeout"] == 5

    # The handshake is bounded the same way, or the claim above holds only for
    # the run: several shipped presets declare a readiness window of 120s, twice
    # this cap, and a stalled handshake would then be cancelled from outside
    # instead of the backend saying the agent never became ready.
    slow_start = ThirdPartyAcpSubagentConfig(
        name="ping-slow-start", kind="acp", command="fake-agent acp", ready_timeout_ms=120000
    )
    await probe_mod.ping_agent(slow_start)
    assert seen["ready_timeout_ms"] == probe_mod._ENABLE_PING_TIMEOUT_SECONDS * 1000

    quick_start = ThirdPartyAcpSubagentConfig(
        name="ping-quick-start", kind="acp", command="fake-agent acp", ready_timeout_ms=4000
    )
    await probe_mod.ping_agent(quick_start)
    assert seen["ready_timeout_ms"] == 4000


async def test_ping_runs_on_a_connection_pool_of_its_own(monkeypatch: pytest.MonkeyPatch) -> None:
    """A ping must not reach the pool the running roster dispatches on.

    The pool keys a connection on its launch arguments, and ``cwd`` falls back
    to the caller's workspace -- which for a ping is a fresh temporary directory
    every time. So a ping on the shared pool always computes a launch key no
    held connection matches, and ``acquire`` retires every connection of that
    agent before launching its own. Two consequences, both measured against a
    live adapter on 2026-09-20: a second Connect pressed while the first was
    still running killed the first one's connection, and the first came back
    "did not answer a test message" for a failure raven had caused; and a ping
    fired while that agent was serving a real run would have taken that run's
    connection down with it.

    This half asserts what ``ping_agent`` asks the factory for. The other half
    is ``test_the_factory_honours_the_pool_its_caller_overrides``.
    """
    from raven.acp_client.pool import AcpConnectionPool, get_pool

    seen: dict = {}

    class _Answers:
        async def run(self, prompt, **kwargs):
            return "PONG"

    def _capture(cfg, **kw):
        seen.update(kw)
        return _Answers()

    monkeypatch.setattr(probe_mod, "build_third_party_backend", _capture)

    await probe_mod.ping_agent(ThirdPartyAcpSubagentConfig(name="ping-acp", kind="acp", command="fake-agent acp"))

    assert isinstance(seen["pool"], AcpConnectionPool)
    assert seen["pool"] is not get_pool()


async def test_ping_closes_the_pool_it_opened(monkeypatch: pytest.MonkeyPatch) -> None:
    """Or every press leaks the agent process it launched.

    The private pool above is per-ping, so nothing else will ever close it: a
    pool left open holds a live child process for the rest of the gateway's
    life, and the roster would accumulate one per Connect pressed.
    """
    import raven.acp_client.pool as pool_mod

    closed: list[int] = []

    class _Counting(pool_mod.AcpConnectionPool):
        async def close_all(self) -> None:
            closed.append(1)
            await super().close_all()

    monkeypatch.setattr(pool_mod, "AcpConnectionPool", _Counting)

    class _Answers:
        async def run(self, prompt, **kwargs):
            return "PONG"

    monkeypatch.setattr(probe_mod, "build_third_party_backend", lambda cfg, **kw: _Answers())

    await probe_mod.ping_agent(ThirdPartyAcpSubagentConfig(name="ping-acp", kind="acp", command="fake-agent acp"))
    assert closed == [1]

    # And on the failing path too, which is the one that runs when an agent is
    # the reason the press is happening at all.
    class _Refuses:
        async def run(self, prompt, **kwargs):
            raise RuntimeError("Internal error: You need to sign in to use this model.")

    monkeypatch.setattr(probe_mod, "build_third_party_backend", lambda cfg, **kw: _Refuses())

    result = await probe_mod.ping_agent(
        ThirdPartyAcpSubagentConfig(name="ping-acp", kind="acp", command="fake-agent acp")
    )
    assert result.ok is False
    assert closed == [1, 1]


def test_the_factory_honours_the_pool_its_caller_overrides() -> None:
    """Read off a real build, for the reason the bounds test states: a request
    recorded by a replaced builder proves nothing about what the backend then
    dispatches on."""
    from raven.acp_client.pool import AcpConnectionPool, get_pool
    from raven.agent.subagent.backends import build_third_party_backend

    cfg = ThirdPartyAcpSubagentConfig(name="factory-pool", kind="acp", command="fake-agent acp")
    private = AcpConnectionPool()

    assert build_third_party_backend(cfg, pool=private).pool is private
    # Omitted, and a dispatch goes where every other dispatch goes.
    assert build_third_party_backend(cfg).pool is get_pool()


def test_the_factory_honours_the_bounds_its_caller_overrides() -> None:
    """Both overrides exist for one caller, so both are read off a real build.

    Asserting on the arguments a replaced builder recorded proves the request and
    not the effect: dropping either honouring line left the whole subagent suite
    green. Read them back off the object instead, and the test stays true of the
    next bound the factory learns to take.
    """
    from raven.agent.subagent.backends import build_third_party_backend

    cfg = ThirdPartyAcpSubagentConfig(
        name="factory-bounds",
        kind="acp",
        command="fake-agent acp",
        ready_timeout_ms=120000,
        timeout=300,
    )
    backend = build_third_party_backend(cfg, ready_timeout_ms=45000, timeout=60)
    assert backend.ready_timeout_ms == 45000
    assert backend.timeout == 60

    # Omitted, and the config's own numbers stand -- the overrides are for the
    # caller that has a shorter budget, not a rewrite of the row.
    untouched = build_third_party_backend(cfg)
    assert untouched.ready_timeout_ms == 120000
    assert untouched.timeout == 300


class _FakeRow:
    def __init__(self, name: str, *, kind: str = "acp", enabled: bool = True, config: object = None) -> None:
        self.name = name
        self.kind = kind
        self.enabled = enabled
        self.config = config if config is not None else type("C", (), {"name": name})()


class _FakeManager:
    def __init__(self, rows: list[_FakeRow], *, with_refresh: bool = False) -> None:
        self.registry = type("R", (), {"rows": lambda self: rows})()
        if with_refresh:
            self.refresh_calls = 0

            def refresh_agents() -> None:
                self.refresh_calls += 1

            self.refresh_agents = refresh_agents


class TestAutomaticSnapshotVerification:
    async def test_only_missing_or_stale_rows_are_verified(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from dataclasses import dataclass

        from raven.agent.subagent.probe import schedule_snapshot_verification

        @dataclass
        class Snap:
            status: str = "ready"
            stale: bool = False

        fresh, missing, stale = _FakeRow("fresh"), _FakeRow("missing"), _FakeRow("stale")
        recorded: list[str] = []

        def fake_snapshot_for(cfg: object) -> Snap | None:
            return {id(fresh.config): Snap(), id(missing.config): None, id(stale.config): Snap(stale=True)}[id(cfg)]

        async def fake_verify(cfg: object) -> Snap:
            return Snap()

        monkeypatch.setattr(probe_mod, "acp_snapshot_for", fake_snapshot_for)
        monkeypatch.setattr("raven.acp_client.capabilities.verify_agent", fake_verify)
        monkeypatch.setattr(
            "raven.acp_client.capabilities.SnapshotStore",
            lambda: type("S", (), {"record": staticmethod(lambda s: recorded.append(getattr(s, "status")))})(),
        )
        # This one pins the registry half. The preset half reads the machine's
        # own PATH, so leaving it live would make the assertions below depend on
        # which agents happen to be installed here.
        monkeypatch.setattr(probe_mod, "_unconfigured_acp_preset_rows", lambda configured, path=None: [])
        monkeypatch.setattr(probe_mod, "_SCHEDULED", False)

        manager = _FakeManager([fresh, missing, stale], with_refresh=True)
        task = schedule_snapshot_verification(manager)
        assert task is not None
        assert task in probe_mod._VERIFY_TASKS, "an unrefed task can be collected mid-run"
        await task
        assert task not in probe_mod._VERIFY_TASKS, "the done callback must release the reference"

        assert len(recorded) == 2  # both missing and stale round-tripped to a fresh snapshot
        assert manager.refresh_calls == 1, "the materialized rows must be rebuilt after recording"

    async def test_a_snapshot_missing_the_model_choices_key_is_reverified(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A snapshot recorded before ``modelChoices`` existed is fresh and not
        stale by every other measure, but a row stuck on it would never learn
        the agent's model menu until the agent is edited or Test is pressed by
        hand -- so the older format alone must trigger a re-verify."""
        from dataclasses import dataclass

        from raven.agent.subagent.probe import schedule_snapshot_verification

        @dataclass
        class Snap:
            status: str = "ready"
            stale: bool = False

        row = _FakeRow("old-format")
        recorded: list[str] = []

        async def fake_verify(cfg: object) -> Snap:
            recorded.append(getattr(cfg, "name", "?"))
            return Snap()

        monkeypatch.setattr(probe_mod, "acp_snapshot_for", lambda cfg: Snap())
        monkeypatch.setattr("raven.acp_client.capabilities.verify_agent", fake_verify)
        monkeypatch.setattr(
            "raven.acp_client.capabilities.SnapshotStore",
            lambda: type(
                "S",
                (),
                {
                    "record": staticmethod(lambda s: None),
                    "has_model_menu": staticmethod(lambda agent: False),
                },
            )(),
        )
        monkeypatch.setattr(probe_mod, "_unconfigured_acp_preset_rows", lambda configured, path=None: [])
        monkeypatch.setattr(probe_mod, "_SCHEDULED", False)

        task = schedule_snapshot_verification(_FakeManager([row]))
        await task

        assert recorded == ["old-format"]

    async def test_a_store_without_has_model_menu_does_not_force_a_reverify(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A caller's stand-in store (a bare ``record``-only object, as several
        tests in this class substitute) must not be treated as "every row is on
        the older format" -- the predicate is optional, and its absence must
        leave a fresh, non-stale snapshot alone exactly as before this hook."""
        from dataclasses import dataclass

        from raven.agent.subagent.probe import schedule_snapshot_verification

        @dataclass
        class Snap:
            status: str = "ready"
            stale: bool = False

        row = _FakeRow("fresh")
        recorded: list[str] = []

        async def fake_verify(cfg: object) -> Snap:
            recorded.append(getattr(cfg, "name", "?"))
            return Snap()

        monkeypatch.setattr(probe_mod, "acp_snapshot_for", lambda cfg: Snap())
        monkeypatch.setattr("raven.acp_client.capabilities.verify_agent", fake_verify)
        monkeypatch.setattr(
            "raven.acp_client.capabilities.SnapshotStore",
            lambda: type("S", (), {"record": staticmethod(lambda s: None)})(),
        )
        monkeypatch.setattr(probe_mod, "_unconfigured_acp_preset_rows", lambda configured, path=None: [])
        monkeypatch.setattr(probe_mod, "_SCHEDULED", False)

        task = schedule_snapshot_verification(_FakeManager([row]))
        await task

        assert recorded == []

    async def test_a_credential_refusal_is_recorded_and_not_only_a_pass(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Recording only ``ready`` meant the one verdict worth remembering was
        the one thrown away.

        An agent that answers the handshake and then refuses to open a session
        without a credential has told us something durable about itself, and the
        agents page reads exactly that to say `Unauthorized` instead of offering
        a Connect that spends a launch to be refused again. Every other failure
        stays unrecorded, deliberately: a timeout or a crashed adapter is a fact
        about this minute, and freezing it into a snapshot would label a working
        agent broken until someone pressed Test.
        """
        from raven.agent.subagent.probe import schedule_snapshot_verification

        recorded: list[str] = []
        verdicts = {
            "refused": type("S", (), {"status": "attention", "needs_auth": True})(),
            "wedged": type("S", (), {"status": "unknown", "needs_auth": False})(),
            "fine": type("S", (), {"status": "ready", "needs_auth": False})(),
        }

        async def fake_verify(cfg: object) -> object:
            return verdicts[cfg.name]

        monkeypatch.setattr(probe_mod, "acp_snapshot_for", lambda cfg: None)
        monkeypatch.setattr("raven.acp_client.capabilities.verify_agent", fake_verify)
        monkeypatch.setattr(
            "raven.acp_client.capabilities.SnapshotStore",
            lambda: type("S", (), {"record": staticmethod(lambda s: recorded.append(s.status))})(),
        )
        monkeypatch.setattr(probe_mod, "_unconfigured_acp_preset_rows", lambda configured, path=None: [])
        monkeypatch.setattr(probe_mod, "_SCHEDULED", False)

        await schedule_snapshot_verification(_FakeManager([_FakeRow("refused"), _FakeRow("wedged"), _FakeRow("fine")]))
        assert sorted(recorded) == ["attention", "ready"]

    async def test_a_preset_nobody_has_configured_is_verified_too(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Or the page can never say `Unauthorized` before the first press.

        The rows this backfill was written for come off the registry, which
        holds configured agents only -- so an agent the reader has not connected
        yet is exactly the one it never reached, and exactly the one whose
        Connect button is about to lie to them.
        """
        from raven.agent.subagent.probe import schedule_snapshot_verification

        seen: list[str] = []

        async def fake_verify(cfg: object) -> object:
            seen.append(cfg.name)
            return type("S", (), {"status": "ready", "needs_auth": False})()

        monkeypatch.setattr(probe_mod, "acp_snapshot_for", lambda cfg: None)
        monkeypatch.setattr("raven.acp_client.capabilities.verify_agent", fake_verify)
        monkeypatch.setattr(
            "raven.acp_client.capabilities.SnapshotStore",
            lambda: type("S", (), {"record": staticmethod(lambda s: None)})(),
        )
        monkeypatch.setattr(
            probe_mod,
            "_unconfigured_acp_preset_rows",
            lambda configured, path=None: [_FakeRow("a-preset")] if configured else [],
        )
        monkeypatch.setattr(probe_mod, "_SCHEDULED", False)

        await schedule_snapshot_verification(_FakeManager([_FakeRow("configured")]))
        assert seen == ["configured", "a-preset"], "the configured rows first: they are the ones a run can dispatch to"

    async def test_a_recorded_credential_refusal_is_measured_again(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The one recorded verdict a restart must not take on trust.

        Every other snapshot is skipped while it is fresh, and rightly: a `ready`
        agent that has not been relaunched is still ready, and re-proving it
        would spend a process per row per boot. A credential refusal is the
        opposite kind of fact -- it is a state the user is expected to go and
        fix, and nothing about fixing it touches the launch config, so the
        snapshot never goes stale and the row would keep saying "Unauthorized"
        across every restart after the sign-in that cured it.
        """
        from dataclasses import dataclass

        from raven.agent.subagent.probe import schedule_snapshot_verification

        @dataclass
        class Snap:
            status: str = "ready"
            stale: bool = False
            needs_auth: bool = False

        fine, refused = _FakeRow("fine"), _FakeRow("refused")
        verified: list[str] = []

        def fake_snapshot_for(cfg: object) -> Snap:
            return {
                id(fine.config): Snap(),
                id(refused.config): Snap(status="attention", needs_auth=True),
            }[id(cfg)]

        async def fake_verify(cfg: object) -> Snap:
            verified.append(cfg.name)
            return Snap()

        monkeypatch.setattr(probe_mod, "acp_snapshot_for", fake_snapshot_for)
        monkeypatch.setattr("raven.acp_client.capabilities.verify_agent", fake_verify)
        monkeypatch.setattr(
            "raven.acp_client.capabilities.SnapshotStore",
            lambda: type("S", (), {"record": staticmethod(lambda s: None)})(),
        )
        # This one pins the registry half. The preset half reads the machine's
        # own PATH, so leaving it live would make the assertions below depend on
        # which agents happen to be installed here.
        monkeypatch.setattr(probe_mod, "_unconfigured_acp_preset_rows", lambda configured, path=None: [])
        monkeypatch.setattr(probe_mod, "_SCHEDULED", False)

        await schedule_snapshot_verification(_FakeManager([fine, refused]))
        assert verified == ["refused"], "a fresh pass is still trusted; a fresh refusal is not"

    async def test_failed_verification_does_not_stop_the_rest(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from raven.agent.subagent.probe import schedule_snapshot_verification

        calls: list[str] = []

        async def fake_verify(cfg: object) -> object:
            calls.append(getattr(cfg, "tag", "?"))
            if getattr(cfg, "tag", "") == "boom":
                raise RuntimeError("no")
            return type("S", (), {"status": "ready"})()

        def make(tag: str) -> object:
            return type(
                "Row", (), {"name": tag, "kind": "acp", "enabled": True, "config": type("C", (), {"tag": tag})()}
            )()

        monkeypatch.setattr(probe_mod, "acp_snapshot_for", lambda cfg: None)
        monkeypatch.setattr("raven.acp_client.capabilities.verify_agent", fake_verify)
        monkeypatch.setattr(
            "raven.acp_client.capabilities.SnapshotStore",
            lambda: type("S", (), {"record": staticmethod(lambda s: None)})(),
        )
        # This one pins the registry half. The preset half reads the machine's
        # own PATH, so leaving it live would make the assertions below depend on
        # which agents happen to be installed here.
        monkeypatch.setattr(probe_mod, "_unconfigured_acp_preset_rows", lambda configured, path=None: [])
        monkeypatch.setattr(probe_mod, "_SCHEDULED", False)

        task = schedule_snapshot_verification(_FakeManager([make("boom"), make("after")]))
        await task

        assert calls == ["boom", "after"]

    async def test_a_manager_without_a_registry_defers_to_the_host(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A mounted stack's subagents substitute may not carry the registry the
        backfill reads; scheduling nothing must not burn the once-per-process
        ticket, so a real host later in the same process still gets its backfill."""
        from raven.agent.subagent.probe import schedule_snapshot_verification

        called: list[str] = []
        monkeypatch.setattr(
            probe_mod,
            "acp_snapshot_for",
            lambda cfg: None,
        )

        async def fake_verify(cfg: object) -> object:
            called.append("verified")
            return type("S", (), {"status": "ready"})()

        monkeypatch.setattr("raven.acp_client.capabilities.verify_agent", fake_verify)
        monkeypatch.setattr(
            "raven.acp_client.capabilities.SnapshotStore",
            lambda: type("S", (), {"record": staticmethod(lambda s: None)})(),
        )
        monkeypatch.setattr(probe_mod, "_SCHEDULED", False)

        stub = type("Stub", (), {})()
        assert schedule_snapshot_verification(stub) is None
        assert probe_mod._SCHEDULED is False, "the ticket must not be spent on a mount that cannot backfill"
        assert called == []

    async def test_disabled_and_non_acp_rows_are_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from raven.agent.subagent.probe import schedule_snapshot_verification

        called: list[str] = []
        monkeypatch.setattr(
            probe_mod,
            "acp_snapshot_for",
            lambda cfg: None,
        )

        async def fake_verify(cfg: object) -> object:
            called.append(cfg.name)
            return type("S", (), {"status": "ready"})()

        monkeypatch.setattr("raven.acp_client.capabilities.verify_agent", fake_verify)
        monkeypatch.setattr(
            "raven.acp_client.capabilities.SnapshotStore",
            lambda: type("S", (), {"record": staticmethod(lambda s: None)})(),
        )
        # This one pins the registry half. The preset half reads the machine's
        # own PATH, so leaving it live would make the assertions below depend on
        # which agents happen to be installed here.
        monkeypatch.setattr(probe_mod, "_unconfigured_acp_preset_rows", lambda configured, path=None: [])
        monkeypatch.setattr(probe_mod, "_SCHEDULED", False)

        rows = [
            _FakeRow("off", enabled=False),
            _FakeRow("cli-kind", kind="cli"),
            _FakeRow("on"),
        ]
        task = schedule_snapshot_verification(_FakeManager(rows))
        await task

        assert called == ["on"]


def test_a_preset_the_table_cannot_be_read_from_offers_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Three ways the shipped table can fail to answer, and none may reach the boot.

    This runs at startup, before anything a user did: a packaging fault here is
    not their typo to see, and taking the gateway down over it would trade a
    stale row for no gateway. Each branch answers with the rows it could build,
    which for these inputs is none.
    """
    # A command no shell could split. `shlex` raises rather than returning, and
    # the entry is simply not a candidate.
    monkeypatch.setattr(
        probe_mod,
        "third_party_subagent_presets",
        lambda: [{"name": "Torn", "preset": "claude_code", "kind": "acp", "command": 'x "unclosed'}],
    )
    assert probe_mod._unconfigured_acp_preset_rows(set(), path=None) == []

    # Nothing on this machine resolves, so there is nothing to hand back and the
    # schema is never asked.
    monkeypatch.setattr(
        probe_mod,
        "third_party_subagent_presets",
        lambda: [{"name": "Gone", "preset": "codex", "kind": "acp", "command": "absent-agent acp"}],
    )
    monkeypatch.setattr(probe_mod.shutil, "which", lambda exe, path=None: None)
    assert probe_mod._unconfigured_acp_preset_rows(set(), path=None) == []

    # And a table the schema refuses: logged, and the boot goes on without it.
    monkeypatch.setattr(probe_mod.shutil, "which", lambda exe, path=None: "/bin/x")
    monkeypatch.setattr(
        probe_mod,
        "third_party_subagent_presets",
        lambda: [{"name": "Bad", "preset": "codex", "kind": "acp", "command": "x acp", "readyTimeoutMs": "soon"}],
    )
    assert probe_mod._unconfigured_acp_preset_rows(set(), path=None) == []


def test_only_resolvable_unconfigured_acp_presets_are_offered_for_verification(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Three filters, each for its own reason.

    Already configured: the registry half of the backfill has it. Not acp: there
    is no handshake to record. Not on PATH: launching it is how you learn it is
    not there, and the free probe answered that for nothing.
    """
    from raven.config.schema import ThirdPartyAcpSubagentConfig

    # Real preset keys: the schema checks `preset` against the shipped table, so
    # a made-up one would fail validation rather than the filter under test.
    presets = [
        {"name": "Here", "preset": "claude_code", "kind": "acp", "command": "present-agent acp"},
        {"name": "Gone", "preset": "codex", "kind": "acp", "command": "absent-agent acp"},
        {"name": "Mine", "preset": "opencode", "kind": "acp", "command": "present-agent acp"},
        {"name": "Http", "preset": "mirothinker", "kind": "openai", "baseUrl": "https://x/v1", "model": "m"},
    ]
    seen_paths: list[str | None] = []

    def fake_which(exe: str, path: str | None = None) -> str | None:
        seen_paths.append(path)
        return "/bin/x" if exe == "present-agent" else None

    monkeypatch.setattr(probe_mod, "third_party_subagent_presets", lambda: presets)
    monkeypatch.setattr(probe_mod.shutil, "which", fake_which)

    rows = probe_mod._unconfigured_acp_preset_rows({"Mine"}, path="/login/shell/bin")

    assert [row.name for row in rows] == ["Here"]
    assert isinstance(rows[0].config, ThirdPartyAcpSubagentConfig)
    # Resolved against the caller's PATH, not this process's. `_probe_acp` answers
    # the row on screen from the login shell's, so reading a different one here
    # would skip an agent the page is reporting as installed.
    assert set(seen_paths) == {"/login/shell/bin"}
