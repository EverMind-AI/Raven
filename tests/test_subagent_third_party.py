"""Third-party subagent backends + manager wiring + spawn tool (req5, P3b)."""

from __future__ import annotations

import asyncio
import json
import os
import socket
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
from aiohttp import web

from raven.agent.subagent import instances as instances_mod
from raven.agent.subagent.backends import (
    AgentMeta,
    CliAgentBackend,
    OpenAIApiBackend,
    build_third_party_backend,
    format_agent_listing,
    third_party_agent_meta,
)
from raven.agent.subagent.manager import SubagentManager
from raven.agent.subagent.presets import third_party_subagent_presets
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


def _mgr(tmp_path: Path, third_party=None) -> SubagentManager:
    return SubagentManager(
        provider=_FakeProvider(),
        workspace=tmp_path,
        model="fake-model",
        third_party_subagents=third_party or [],
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
        assert third_party_agent_meta(stateful) == AgentMeta("claude_code", "Claude Code", True, True)
        assert third_party_agent_meta(stateless) == AgentMeta("codex", "", False, True)
        assert third_party_agent_meta(boxed) == AgentMeta("boxed", "", False, False)
        # An HTTP endpoint is remote by default, so it reads no local path.
        assert third_party_agent_meta(remote) == AgentMeta("miro", "", False, False)

    def test_listing_degrades_to_the_bare_name_without_a_description(self) -> None:
        listing = format_agent_listing([AgentMeta("a", "does A", False, True), AgentMeta("b", "", True, False)])
        assert listing == "a [stateless, local-files] (does A); b [stateful, no-local-files]"

    def test_listing_renders_both_capabilities_for_every_agent(self) -> None:
        """A capability the roster leaves out reads the same as one it denies,
        and the DAG pre-check rejects graphs on exactly these two facts."""
        for meta, expected in (
            (AgentMeta("x", "", True, True), "x [stateful, local-files]"),
            (AgentMeta("x", "", True, False), "x [stateful, no-local-files]"),
            (AgentMeta("x", "", False, True), "x [stateless, local-files]"),
            (AgentMeta("x", "", False, False), "x [stateless, no-local-files]"),
        ):
            assert format_agent_listing([meta]) == expected

    def test_listing_drops_nameless_entries_and_empties(self) -> None:
        assert format_agent_listing([AgentMeta("", "orphan", False, True)]) == ""
        assert format_agent_listing([]) == ""


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
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None):
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

from raven.agent.subagent.backends.transcript import parse_claude_stream_json, parse_codex_jsonl


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
    assert set(presets) == {"claude_code", "codex", "mirothinker"}
    # Every preset validates against the schema (discriminated union), so "Add
    # from preset" can never produce a config the write path would reject ...
    cfg = SubagentsConfig(third_party=list(presets.values()))
    assert len(cfg.third_party) == len(presets)
    # ... and each one builds into a concrete backend.
    for entry in cfg.third_party:
        assert build_third_party_backend(entry) is not None

    claude = presets["claude_code"]
    assert "--output-format stream-json" in claude["command"]
    assert "--verbose" in claude["command"]
    assert "--session-id {agent_id}" in claude["command"]
    assert "--resume {agent_id}" in claude["resumeCommand"]
    assert claude["transcriptFormat"] == "claude_stream_json"
    assert claude["idSource"] == "provisioned"

    codex = presets["codex"]
    assert "-s workspace-write" in codex["command"]
    assert "--json" in codex["command"]
    assert codex["transcriptFormat"] == "codex_jsonl"
    assert codex["idSource"] == "derived"
    assert "{agent_id}" not in codex["command"]
    # -a/--ask-for-approval is a root-level codex flag: `codex exec -a never`
    # exits 2 with "unexpected argument '-a'", so it has to precede the
    # subcommand. --skip-git-repo-check is required or every spawn fails at
    # Raven's default (non-git) workspace cwd. Both verified against
    # codex-cli 0.144.5.
    assert codex["command"].startswith("codex -a never exec --skip-git-repo-check")
    assert codex["resumeCommand"].startswith("codex -a never exec --skip-git-repo-check")
    assert "resume {agent_id}" in codex["resumeCommand"]
    # `resume` is an `exec` subcommand and must follow the option flags.
    assert codex["resumeCommand"].index("--json") < codex["resumeCommand"].index("resume {agent_id}")

    assert presets["mirothinker"]["baseUrl"] == "https://api.miromind.ai/v1"


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
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None):
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
    hang_started = [asyncio.Event() for _ in range(4)]
    canary_started = asyncio.Event()

    class _Hang:
        def __init__(self, ev: asyncio.Event) -> None:
            self._ev = ev

        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None):
            self._ev.set()
            await asyncio.sleep(3600)
            return "never"

    class _Canary:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None):
            canary_started.set()
            await asyncio.sleep(3600)
            return "never"

    mgr = _mgr(tmp_path, [])
    for i, ev in enumerate(hang_started):
        mgr._backends[f"hang{i}"] = _Hang(ev)
    mgr._backends["canary"] = _Canary()
    mgr.set_submit(lambda req: None)

    for i in range(4):
        await mgr.spawn("t", session_key="web:s1", agent=f"hang{i}", instance=f"h{i}")
    await asyncio.wait_for(asyncio.gather(*(ev.wait() for ev in hang_started)), timeout=5)

    # All four slots are held: a fifth spawn's coroutine blocks on the gate
    # before its backend ever runs.
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

        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None):
            hang_started[self._n].set()
            self._n += 1
            await asyncio.sleep(3600)
            return "never"

    class _Canary:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None):
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
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None):
            hang_started.set()
            await asyncio.sleep(3600)
            return "never"

    class _Queued:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None):
            raise AssertionError("must never run: cancelled while still queued on the gate")

    class _Canary:
        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None):
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

        async def run(self, task, *, task_id, workspace, executor, session_key=None, instance=None):
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
