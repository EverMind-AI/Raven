"""Free availability probe for third-party subagents (cli PATH + openai /models)."""

from __future__ import annotations

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
from raven.config.schema import Config, ThirdPartyCliSubagentConfig, ThirdPartyOpenAISubagentConfig


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
    from raven.agent.subagent.test_state import LastTest

    monkeypatch.setattr(probe_mod, "_login_path", lambda: str(tmp_path))
    a = _cli("a {prompt}", name="a")
    b = _cli("b {prompt}", name="b")
    verdicts = {"config:a": LastTest(ok=False, detail="auth failed", tested_at_ms=1700)}
    results = await probe_all([(a, "config"), (b, "config")], verdicts=verdicts)
    assert results[0].last_test == LastTest(ok=False, detail="auth failed", tested_at_ms=1700)
    assert results[1].last_test is None


async def test_probe_wire_shape_carries_last_test(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from raven.agent.subagent.test_state import LastTest

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
        monkeypatch.setattr(probe_mod, "verify_agent", fake_verify)
        monkeypatch.setattr(
            "raven.agent.subagent.probe.SnapshotStore",
            lambda: type("S", (), {"record": staticmethod(lambda s: recorded.append(getattr(s, "status")))})(),
        )
        monkeypatch.setattr(probe_mod, "_SCHEDULED", False)

        manager = _FakeManager([fresh, missing, stale], with_refresh=True)
        task = schedule_snapshot_verification(manager)
        assert task is not None
        assert task in probe_mod._VERIFY_TASKS, "an unrefed task can be collected mid-run"
        await task
        assert task not in probe_mod._VERIFY_TASKS, "the done callback must release the reference"

        assert len(recorded) == 2  # both missing and stale round-tripped to a fresh snapshot
        assert manager.refresh_calls == 1, "the materialized rows must be rebuilt after recording"

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
        monkeypatch.setattr(probe_mod, "verify_agent", fake_verify)
        monkeypatch.setattr(
            "raven.agent.subagent.probe.SnapshotStore",
            lambda: type("S", (), {"record": staticmethod(lambda s: None)})(),
        )
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

        monkeypatch.setattr(probe_mod, "verify_agent", fake_verify)
        monkeypatch.setattr(
            "raven.agent.subagent.probe.SnapshotStore",
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

        monkeypatch.setattr(probe_mod, "verify_agent", fake_verify)
        monkeypatch.setattr(
            "raven.agent.subagent.probe.SnapshotStore",
            lambda: type("S", (), {"record": staticmethod(lambda s: None)})(),
        )
        monkeypatch.setattr(probe_mod, "_SCHEDULED", False)

        rows = [
            _FakeRow("off", enabled=False),
            _FakeRow("cli-kind", kind="cli"),
            _FakeRow("on"),
        ]
        task = schedule_snapshot_verification(_FakeManager(rows))
        await task

        assert called == ["on"]
