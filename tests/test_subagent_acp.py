"""External agent registration over ACP: schema, snapshot, roster, dispatch.

Everything here runs against ``tests/acp_stub_server.py`` as a real child process,
so the launch path, the read loop and teardown are exercised rather than mocked.
The three regression classes at the bottom are the ones worth naming: each is a
place where an acp entry walks into code written for the cli transport and would
fail *silently* -- reporting a working agent as stateless, sharing one test verdict
between two agents, or showing a green light for an agent that cannot run a task.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from raven.agent.acp.capabilities import CapabilitySnapshot, SnapshotStore, snapshot_fingerprint, verify_agent
from raven.agent.acp.pool import close_pool, get_pool
from raven.agent.subagent.backends import acp_snapshot_for, build_third_party_backend, third_party_agent_meta
from raven.agent.subagent.backends.acp_agent import AcpAgentBackend, AcpEmptyTurnError
from raven.agent.subagent.instances import InstanceRegistry
from raven.agent.subagent.probe import probe_one
from raven.agent.subagent.test_state import fingerprint
from raven.config.schema import SubagentsConfig, ThirdPartyAcpSubagentConfig, ThirdPartyCliSubagentConfig
from raven.config.update_subagents import reject_unsupported_acp_fields

_STUB = Path(__file__).with_name("acp_stub_server.py")


def stub_config(name: str = "stub", *, mode: str = "ok", **kw: Any) -> ThirdPartyAcpSubagentConfig:
    return ThirdPartyAcpSubagentConfig(
        name=name,
        command=f"{sys.executable} {_STUB}",
        env={"ACP_STUB_MODE": mode},
        ready_timeout_ms=kw.pop("ready_timeout_ms", 15000),
        **kw,
    )


@pytest.fixture(autouse=True)
async def _no_pooled_connections():
    """Close pooled connections between tests.

    The pool is process state by design (two backends of one agent must share a
    connection), so without this a stub process would outlive its test and the
    next test would be handed a connection configured for a different mode.
    """
    yield
    await close_pool()


# ---- schema ----------------------------------------------------------------


def test_acp_entry_keeps_only_launch_fields() -> None:
    cfg = SubagentsConfig(third_party=[{"name": "a", "kind": "acp", "command": "hermes acp"}]).third_party[0]
    assert cfg.kind == "acp"
    assert cfg.command == "hermes acp"
    assert not hasattr(cfg, "resume_command")
    assert not hasattr(cfg, "transcript_format")


def test_cli_declarations_on_an_acp_entry_are_dropped_not_fatal(caplog) -> None:
    """A stored config carrying one must still load, or raven cannot start.

    The whole point of the load/write split: raising here would take the config
    down, and the UI that could remove the field sits behind that config.
    """
    entries = [
        {
            "name": "a",
            "kind": "acp",
            "command": "hermes acp",
            "resumeCommand": "hermes acp --resume {agent_id}",
            "transcriptFormat": "text",
            "stateful": True,
        }
    ]
    cfg = SubagentsConfig(third_party=entries).third_party[0]
    assert cfg.name == "a"
    assert not hasattr(cfg, "resume_command")


def test_write_path_rejects_cli_declarations_on_an_acp_entry() -> None:
    with pytest.raises(ValueError, match="not supported for kind 'acp'"):
        reject_unsupported_acp_fields([{"name": "a", "kind": "acp", "command": "x acp", "transcriptFormat": "text"}])
    # Snake spelling must not slip past the camel check.
    with pytest.raises(ValueError, match="not supported for kind 'acp'"):
        reject_unsupported_acp_fields([{"name": "a", "kind": "acp", "command": "x acp", "reads_local_files": True}])


def test_write_path_rejects_a_task_placeholder_in_an_acp_command() -> None:
    with pytest.raises(ValueError, match="starts a\n?\\s*server|server, not one task"):
        reject_unsupported_acp_fields([{"name": "a", "kind": "acp", "command": "hermes acp {prompt}"}])


def test_write_path_leaves_cli_and_openai_entries_alone() -> None:
    reject_unsupported_acp_fields(
        [
            {"name": "c", "kind": "cli", "command": "claude -p {prompt}", "transcriptFormat": "text"},
            {"name": "o", "kind": "openai", "baseUrl": "https://x/v1", "model": "m"},
        ]
    )


def test_a_legacy_cli_entry_keeps_its_preset_and_is_offered_an_upgrade() -> None:
    """One preset per agent means a stored entry on the older transport is still
    that preset's -- which is what turns the mismatch into an upgrade offer.

    Matching on kind instead would read it as hand-written: the upgrade would
    disappear and the Presets group would offer the agent again as unconfigured,
    inviting a second copy.
    """
    from raven.agent.subagent.presets import THIRD_PARTY_SUBAGENT_PRESETS
    from raven.rpc.methods.subagents import _upgrade_transport

    legacy = SubagentsConfig(
        third_party=[{"name": "codex", "kind": "cli", "command": "codex exec --json {prompt}"}]
    ).third_party[0]
    assert legacy.preset == "codex"
    assert THIRD_PARTY_SUBAGENT_PRESETS["codex"]["kind"] == "acp"
    assert _upgrade_transport(legacy, "config") == "acp"

    # An entry already on the preset's transport is current, and a preset row is
    # by definition current.
    current = SubagentsConfig(third_party=[dict(THIRD_PARTY_SUBAGENT_PRESETS["codex"])]).third_party[0]
    assert _upgrade_transport(current, "config") is None
    assert _upgrade_transport(legacy, "preset") is None


# ---- capability snapshot ---------------------------------------------------


async def test_verify_reads_capabilities_from_the_handshake() -> None:
    snapshot = await verify_agent(stub_config())
    assert snapshot.status == "ready"
    assert snapshot.usable
    assert (snapshot.agent_name, snapshot.agent_version) == ("stub-agent", "9.9.9")
    assert snapshot.protocol_version == 1
    assert (snapshot.can_resume, snapshot.can_fork, snapshot.can_load) == (True, True, True)
    assert snapshot.prompt_modalities == ("text", "image")
    # Only entries that actually carry a modelId are advertised; the third stub
    # entry has none and must not become an invented id.
    assert snapshot.available_models == ("stub:model-a", "stub:model-b")
    assert snapshot.auth_methods == ("stub-auth",)


async def test_verify_reports_a_rejected_handshake_as_present_but_unusable() -> None:
    """Not ``missing``: the agent answered, so it is installed.

    Measured motivation: sending one extra ``clientInfo`` param made a real
    ``hermes acp`` answer ``-32602``. Calling that "missing" would send the
    operator hunting for an install that is already there.
    """
    snapshot = await verify_agent(stub_config(mode="reject_init"))
    assert snapshot.status == "unknown"
    assert "rejected the ACP handshake" in snapshot.detail


async def test_verify_reports_a_failed_session_as_needing_attention() -> None:
    snapshot = await verify_agent(stub_config(mode="no_session"))
    assert snapshot.status == "attention"
    assert "no session could be opened" in snapshot.detail
    # The handshake still happened, so its facts are kept rather than blanked.
    assert snapshot.agent_name == "stub-agent"
    assert "stub-auth" in snapshot.detail


async def test_verify_reports_an_unlaunchable_command_as_missing() -> None:
    cfg = ThirdPartyAcpSubagentConfig(name="ghost", command="raven-no-such-acp-binary", ready_timeout_ms=2000)
    snapshot = await verify_agent(cfg)
    assert snapshot.status == "missing"
    assert snapshot.can_resume is False


async def test_verify_times_out_on_a_silent_server() -> None:
    snapshot = await verify_agent(stub_config(mode="silent", ready_timeout_ms=1000))
    assert snapshot.status == "missing"
    assert "timed out" in snapshot.detail


async def test_verify_survives_diagnostics_on_stdout_and_bulk_stderr() -> None:
    """A stray non-JSON stdout line must not break the connection.

    Both halves are measured behaviour: openclaw interleaves plugin chatter, and
    one failing openclaw run wrote 35 KB to stderr -- which would deadlock a
    connection that drained stderr only after stdout.
    """
    snapshot = await verify_agent(stub_config(mode="noisy"))
    assert snapshot.status == "ready"


async def test_snapshot_store_round_trips_and_invalidates_on_launch_change(tmp_path: Path) -> None:
    store = SnapshotStore(path=tmp_path / "caps.json")
    cfg = stub_config("a")
    store.record(await verify_agent(cfg))

    assert store.load([cfg])["a"].can_resume is True

    moved = stub_config("a", ready_timeout_ms=999)
    assert store.load([moved]) == {}, "a snapshot must not survive a change to how the agent is launched"

    renamed = ThirdPartyAcpSubagentConfig(
        name="renamed", command=cfg.command, env=dict(cfg.env), ready_timeout_ms=cfg.ready_timeout_ms
    )
    assert snapshot_fingerprint(renamed) == snapshot_fingerprint(cfg), "renaming does not change what an agent can do"


async def test_an_edited_launch_config_does_not_cost_an_agent_its_resume(tmp_path: Path, monkeypatch) -> None:
    """The measured incident: adding one env var made an agent stateless.

    A skipped snapshot is indistinguishable from one never taken, and the
    fallback for never-taken is *stateless* -- so the agent stopped committing
    instance rows, which took its chip off the strip, its handle out of
    ``/instance`` and ``instance`` out of the spawn schema. Nothing said so, and
    it does not recover on its own: only a test records a new snapshot.
    """
    path = tmp_path / "caps.json"
    monkeypatch.setattr("raven.agent.acp.capabilities.default_snapshot_path", lambda: path)
    cfg = stub_config("a")
    SnapshotStore(path=path).record(await verify_agent(cfg))
    assert third_party_agent_meta(cfg).stateful is True

    edited = stub_config("a", ready_timeout_ms=999)
    snapshot = acp_snapshot_for(edited)
    assert snapshot is not None and snapshot.stale is True
    assert third_party_agent_meta(edited).stateful is True, "an edit to how it launches is not a capability change"


async def test_a_stale_snapshot_is_never_reported_as_a_verdict(tmp_path: Path, monkeypatch) -> None:
    """Paired with the case above: capabilities are taken, the status is not.

    A green light measured against a command the entry no longer has is a claim
    no measurement backs, so the row asks for a test instead.
    """
    path = tmp_path / "caps.json"
    monkeypatch.setattr("raven.agent.acp.capabilities.default_snapshot_path", lambda: path)
    cfg = stub_config("a")
    SnapshotStore(path=path).record(await verify_agent(cfg))

    fresh = await probe_one(cfg, source="config")
    assert fresh.status == "ready"

    edited = stub_config("a", ready_timeout_ms=999)
    stale = await probe_one(edited, source="config")
    assert stale.status == "attention"
    assert "launch config changed" in stale.detail
    assert acp_snapshot_for(edited).usable is False


def test_snapshot_store_ignores_a_row_it_cannot_read(tmp_path: Path) -> None:
    path = tmp_path / "caps.json"
    path.write_text(json.dumps({"version": 1, "snapshots": [{"agent": "a"}, "not-a-dict"]}), encoding="utf-8")
    assert SnapshotStore(path=path).load([stub_config("a")]) == {}


# ---- the roster ------------------------------------------------------------


def _snapshot(agent: str, cfg: Any, *, can_resume: bool, can_load: bool = False) -> CapabilitySnapshot:
    return CapabilitySnapshot(
        agent=agent,
        fingerprint=snapshot_fingerprint(cfg),
        status="ready",
        detail="",
        measured_at_ms=1,
        can_resume=can_resume,
        can_load=can_load,
    )


def test_acp_statefulness_comes_from_the_snapshot_not_a_config_field() -> None:
    """The regression that would silently disable the whole session story.

    ``third_party_agent_meta`` derived ``stateful`` from ``resume_command``, which
    an acp entry does not have. Left alone, every acp agent reads as stateless --
    which strips ``instance`` out of the spawn schema entirely and makes the DAG
    pre-check reject any graph sharing a handle.
    """
    cfg = stub_config("a")
    assert third_party_agent_meta(cfg, snapshot=None).stateful is False
    assert third_party_agent_meta(cfg, snapshot=_snapshot("a", cfg, can_resume=True)).stateful is True
    assert third_party_agent_meta(cfg, snapshot=_snapshot("a", cfg, can_resume=False)).stateful is False


def test_acp_meta_reads_the_stored_snapshot_when_none_is_passed(tmp_path: Path, monkeypatch) -> None:
    """So a caller that knows nothing about ACP needs no change.

    The spawn manager and the DAG tool both call ``third_party_agent_meta(cfg)``
    with no snapshot; keeping the lookup inside is what lets them stay untouched.
    """
    path = tmp_path / "caps.json"
    monkeypatch.setattr("raven.agent.acp.capabilities.default_snapshot_path", lambda: path)
    cfg = stub_config("a")
    assert third_party_agent_meta(cfg).stateful is False
    SnapshotStore(path=path).record(_snapshot("a", cfg, can_resume=True))
    assert third_party_agent_meta(cfg).stateful is True


def test_acp_backend_statefulness_follows_the_snapshot() -> None:
    cfg = stub_config("a")
    assert build_third_party_backend(cfg).is_stateful is False
    backend = AcpAgentBackend(
        name="a", command=cfg.command, snapshot=_snapshot("a", cfg, can_resume=True), registry=None
    )
    assert backend.is_stateful is True


# ---- test-verdict fingerprinting -------------------------------------------


def test_two_acp_agents_do_not_share_one_test_verdict() -> None:
    """The regression that would hand agent A's verdict to agent B.

    ``test_state.fingerprint`` dispatched on ``kind == "openai"`` and fell through
    to the cli field list for everything else. An acp entry has none of those
    fields, so every acp agent digested identically.
    """
    a = ThirdPartyAcpSubagentConfig(name="a", command="hermes acp")
    b = ThirdPartyAcpSubagentConfig(name="b", command="openclaw acp")
    c = ThirdPartyAcpSubagentConfig(name="c", command="hermes acp", ready_timeout_ms=45000)
    assert len({fingerprint(a), fingerprint(b), fingerprint(c)}) == 3


def test_cli_verdict_digests_are_unchanged() -> None:
    """Adding a kind must not silently discard every remembered cli verdict."""
    import hashlib

    from raven.agent.subagent.test_state import _CLI_FIELDS

    cfg = ThirdPartyCliSubagentConfig(name="x", command="claude -p {prompt}")
    payload = {name: getattr(cfg, name, None) for name in _CLI_FIELDS}
    expected = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]
    assert fingerprint(cfg) == expected


# ---- the free probe --------------------------------------------------------


async def test_probe_does_not_show_a_green_light_before_verification(tmp_path: Path, monkeypatch) -> None:
    """The regression that would report an unusable agent as ready.

    For a cli agent ``shutil.which`` is a fair proxy for "usable". For an ACP
    server it is not: the executable existing says nothing about whether it
    speaks the protocol, holds a credential, or can reach the gateway it bridges
    to. So an unverified entry is ``attention``, never ``ready``.
    """
    monkeypatch.setattr("raven.agent.acp.capabilities.default_snapshot_path", lambda: tmp_path / "caps.json")
    cfg = stub_config("a")
    result = await probe_one(cfg, source="config")
    assert result.kind == "acp"
    assert result.status == "attention"
    assert "have not been recorded yet" in result.detail

    SnapshotStore(path=tmp_path / "caps.json").record(await verify_agent(cfg))
    assert (await probe_one(cfg, source="config")).status == "ready"


async def test_probe_reports_a_missing_launcher() -> None:
    cfg = ThirdPartyAcpSubagentConfig(name="ghost", command="raven-no-such-acp-binary")
    result = await probe_one(cfg, source="config")
    assert result.status == "missing"
    assert "not on the login shell PATH" in result.detail


async def test_an_explicit_test_retries_past_a_stale_recorded_failure(tmp_path: Path, monkeypatch) -> None:
    """The regression that made one slow first launch permanent.

    A failed verify (e.g. npx downloading the adapter past the ready timeout)
    records a ``missing`` snapshot; the probe then reports that snapshot, and a
    test that trusted the probe returned the stale verdict in milliseconds
    without ever reconnecting. An explicit test is a request for a fresh
    verdict, so it must connect live and overwrite the recorded failure.
    """
    from raven.agent.subagent.probe import run_test

    monkeypatch.setattr("raven.agent.acp.capabilities.default_snapshot_path", lambda: tmp_path / "caps.json")
    cfg = stub_config("a")
    stale = await verify_agent(stub_config("a", mode="silent", ready_timeout_ms=1000))
    assert stale.status == "missing"
    SnapshotStore(path=tmp_path / "caps.json").record(
        CapabilitySnapshot(**{**stale.__dict__, "agent": "a", "fingerprint": snapshot_fingerprint(cfg)})
    )
    assert (await probe_one(cfg, source="config")).status == "missing"

    result = await run_test(cfg, source="config")
    assert result.ok, result.detail
    assert (await probe_one(cfg, source="config")).status == "ready"


# ---- dispatch --------------------------------------------------------------


async def test_dispatch_returns_the_agents_answer(tmp_path: Path) -> None:
    backend = build_third_party_backend(stub_config("a"))
    reply = await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None)
    assert reply == "pong"


async def test_dispatch_records_the_runs_own_transcript(tmp_path: Path) -> None:
    """The acp lane can see every step, so the record keeps them.

    Provider-shaped on purpose: one assistant message per tool call wearing the
    thought that preceded it, then a role=tool result matched by call id --
    the exact shape session.resume stores, so one renderer draws both.
    """
    from raven.agent.subagent import activity

    backend = build_third_party_backend(stub_config("a"))
    with activity.collecting() as did:
        await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None)

    assert did.transcript, "the acp backend must publish its transcript"
    call = next(m for m in did.transcript if m.get("tool_calls"))
    assert call["role"] == "assistant"
    assert call["reasoning_content"] == "thinking"
    assert call["tool_calls"][0]["function"]["name"] == "read_file src/a.py"
    result = next(m for m in did.transcript if m.get("role") == "tool")
    assert result["tool_call_id"] == "t1"


async def test_the_collector_serves_a_timestamped_partial_while_in_flight() -> None:
    """A watching panel reads the run as it happens: every rendered message
    carries the wall clock of the event that opened it, and the in-flight shape
    appends whatever answer text has streamed so far -- which the settled shape
    must NOT carry, because the record keeps the answer in out.md."""
    from raven.agent.subagent.backends.acp_agent import _TurnCollector

    col = _TurnCollector()

    async def feed(payload: dict) -> None:
        await col("session/update", {"update": payload})

    await feed({"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "hmm"}})
    await feed({"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "read x"})
    await feed({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"})
    await feed({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "half an ans"}})

    live = col.messages(in_flight=True)
    assert live[-1] == {"role": "assistant", "content": "half an ans", "timestamp": live[-1]["timestamp"]}
    assert all(m.get("timestamp") for m in live), f"unstamped message in {live}"

    settled = col.messages()
    assert all(m.get("content") != "half an ans" for m in settled), "the answer belongs to out.md, not the transcript"


async def test_dispatch_accepts_the_managers_full_keyword_set(tmp_path: Path) -> None:
    """``SubagentManager`` passes one keyword set to whichever backend it
    resolved, ``provider`` and ``model`` included. This backend ignores both --
    the agent holds its own credential -- but it has to accept them: without
    them every acp dispatch died of a TypeError before the agent was contacted,
    spawn, DAG node and direct chat alike, and the tests missed it by calling
    ``run`` directly with the arguments this backend happened to declare.
    """
    backend = build_third_party_backend(stub_config("a"))
    reply = await backend.run(
        "ping",
        task_id="t1",
        workspace=tmp_path,
        executor=None,
        session_key="s1",
        instance="h1",
        provider=object(),
        model="whatever",
    )
    assert reply == "pong"


async def test_dispatch_truncates_to_max_output_chars(tmp_path: Path) -> None:
    backend = build_third_party_backend(stub_config("a", max_output_chars=2))
    assert await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None) == "po"


async def test_dispatch_streams_the_answer_and_nothing_else(tmp_path: Path) -> None:
    """This transport already receives the agent's work as it happens; streaming
    is forwarding the answer chunks and only those. A thought or a tool call is
    commentary, and the wire has no instance-tagged event to carry it -- it would
    be rendered into the main agent's transcript.
    """
    seen: list[str] = []

    async def on_delta(text: str) -> None:
        seen.append(text)

    backend = build_third_party_backend(stub_config("a"))
    reply = await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None, on_delta=on_delta)

    assert seen == ["pong"]
    assert reply == "pong"


async def test_dispatch_streams_no_more_than_it_returns(tmp_path: Path) -> None:
    """The reply is truncated to ``max_output_chars``; an uncapped stream would
    render text the record never stores."""
    seen: list[str] = []

    async def on_delta(text: str) -> None:
        seen.append(text)

    backend = build_third_party_backend(stub_config("a", max_output_chars=2))
    reply = await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None, on_delta=on_delta)

    assert "".join(seen) == reply == "po"


async def test_an_empty_turn_is_a_failure_carrying_the_stderr_tail(tmp_path: Path) -> None:
    """``stopReason`` alone is not trustworthy.

    Measured on a live ``hermes acp``: a provider ``HTTP 401`` still returned
    ``stopReason: "end_turn"`` and reported the real cause only on stderr.
    """
    backend = build_third_party_backend(stub_config("a", mode="empty_turn"))
    with pytest.raises(AcpEmptyTurnError) as excinfo:
        await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None)
    assert "no content" in str(excinfo.value)
    assert "HTTP 401" in str(excinfo.value)


async def test_an_empty_turn_names_a_request_raven_could_not_answer(tmp_path: Path) -> None:
    """The one cause the stderr tail cannot carry.

    ``fs/read_text_file`` is declared unsupported in ``CLIENT_CAPABILITIES``, so
    an agent that asks for it anyway is answered "method not found". An adapter
    that is refused ends the turn with no content and says nothing on stderr, so
    without this the error reads as an unexplained empty turn.

    Permission requests used to be refused this way too and are now answered
    (``raven/agent/acp/permissions.py``); this covers what raven still refuses.
    """
    backend = build_third_party_backend(stub_config("a", mode="asks"))
    for task_id in ("t1", "t2"):
        with pytest.raises(AcpEmptyTurnError) as excinfo:
            await backend.run("ping", task_id=task_id, workspace=tmp_path, executor=None)
        # Every turn, not only the first: the connection is process-wide and an
        # adapter that asks for something unsupported asks again on the next
        # tool-using turn, so a record deduped per connection would explain the
        # failure once and then go quiet -- worse on a shared connection, where
        # a DAG node's refusal would consume the one explanation a later spawn
        # needed.
        assert "fs/read_text_file" in str(excinfo.value), task_id


async def test_a_refusal_is_reported_only_to_the_session_that_asked(tmp_path: Path) -> None:
    """A connection is shared, and two turns on it run on different sessions.

    The per-session lock does not serialise them -- it serialises one session --
    so a refusal recorded connection-wide was reported to every turn in flight.
    A turn that was empty for an unrelated reason (the provider 401 this error
    was invented for) would then point the operator at approvals.
    """
    import asyncio

    cfg = stub_config("shared", mode="asks_late")
    backends = [build_third_party_backend(cfg) for _ in range(2)]
    results = await asyncio.gather(
        *(b.run("go", task_id=f"t{i}", workspace=tmp_path, executor=None) for i, b in enumerate(backends)),
        return_exceptions=True,
    )

    named = [r for r in results if "fs/read_text_file" in str(r)]
    assert len(named) == 1, f"only one session asked; {len(named)} turns named a refusal"


async def test_a_permission_request_is_approved_rather_than_refused(tmp_path: Path) -> None:
    """Refusing one cancels the whole turn, part-answered.

    Measured in ``@agentclientprotocol/codex-acp@1.1.14``: ``CodexApprovalHandler``
    turns any error from this request -- the "method not found" raven used to
    send included -- into ``{decision: "cancel"}``. The turn came back with the
    sentence the agent had already streamed and ``stopReason: "cancelled"``,
    which read as a short answer rather than as a failure.

    The stub answers with the ``optionId`` it was given, so this asserts the
    choice and not merely that something was sent: ``allow_always`` outranks
    ``allow_once``, and both outrank the reject the stub lists first.
    """
    backend = build_third_party_backend(stub_config("a", mode="permission"))
    out = await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None)
    assert out == "chose:always"


async def test_a_turn_that_stopped_early_says_so_in_its_reply(tmp_path: Path) -> None:
    """A partial reply that reads finished is the failure mode being fixed.

    Only ``end_turn`` means the agent said everything it meant to. The reply is
    kept -- it is worth having -- but a reader who cannot see the stop reason
    has no other way to tell it is half an answer.
    """
    backend = build_third_party_backend(stub_config("a", mode="cancelled"))
    out = await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None)
    assert out.startswith("I will start by")
    assert "stopReason=cancelled" in out
    assert "partial" in out


async def test_two_messages_in_one_turn_are_kept_apart(tmp_path: Path) -> None:
    """Chunks carry no message boundary, and one turn can hold several messages.

    Codex answers a question about a project by saying what it will look at,
    running the commands, and only then answering. Joined chunk-to-chunk those
    are one paragraph whose halves do not follow from each other; the tool calls
    between them are the only mark that the message ended.

    The stub puts a thought inside the first message and a usage update inside
    the second, so "break on a tool call" and "break on any other update" give
    different answers here -- otherwise this passes either way, which is what a
    first version of it did.
    """
    backend = build_third_party_backend(stub_config("a", mode="two_messages"))
    seen: list[str] = []

    async def on_delta(text: str) -> None:
        seen.append(text)

    out = await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None, on_delta=on_delta)
    assert out == "let me look.\n\nit is a repo."
    # The live view and the stored reply must agree, or a direct chat renders
    # the run-on version and the record keeps the readable one.
    assert "".join(seen) == "let me look.\n\nit is a repo."


async def test_the_partial_notice_reaches_a_caller_that_streamed(tmp_path: Path) -> None:
    """The return value is not delivered twice, so a notice riding only on it
    never reaches the screen.

    A direct chat with an acp instance always streams (``streams = True``), and
    ``AgentLoop.run_turn`` skips the closing text once anything streamed. So the
    notice has to go through the same sink the reply did: on it, the record kept
    the warning and the reader who needed it saw a reply that simply stopped.
    """
    backend = build_third_party_backend(stub_config("a", mode="cancelled"))
    seen: list[str] = []

    async def on_delta(text: str) -> None:
        seen.append(text)

    out = await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None, on_delta=on_delta)
    streamed = "".join(seen)
    assert "stopReason=cancelled" in streamed, "the notice never reached the stream"
    assert streamed == out, "what streamed and what the record stores must agree"


async def test_the_notice_survives_a_reply_that_used_the_whole_budget(tmp_path: Path) -> None:
    """The cap bounds what the *agent* says; the notice is raven's own line.

    Sharing the reply's `bounded_delta` budget meant a reply that saturated
    `maxOutputChars` swallowed the one line explaining it had been cut off --
    which is precisely the reply that needs it.
    """
    cfg = stub_config("a", mode="cancelled", max_output_chars=4)
    backend = build_third_party_backend(cfg)
    seen: list[str] = []

    async def on_delta(text: str) -> None:
        seen.append(text)

    await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None, on_delta=on_delta)
    assert "stopReason=cancelled" in "".join(seen), "the reply's budget swallowed the notice"


async def test_a_finished_turn_carries_no_notice(tmp_path: Path) -> None:
    """Paired with the case above so the notice cannot be unconditional."""
    backend = build_third_party_backend(stub_config("a"))
    out = await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None)
    assert out == "pong"


async def test_a_handle_resumes_the_same_session(tmp_path: Path) -> None:
    cfg = stub_config("a")
    registry = InstanceRegistry(path=tmp_path / "instances.json")
    backend = AcpAgentBackend(
        name="a",
        command=cfg.command,
        env=dict(cfg.env),
        snapshot=_snapshot("a", cfg, can_resume=True),
        registry=registry,
    )
    await backend.run("one", task_id="t1", workspace=tmp_path, session_key="s", instance="work", executor=None)
    bound = await registry.lookup("s", "a", "work", kind="acp")
    assert bound is not None
    # A cli lookup of the same handle must not find it: a session id is only
    # meaningful to the transport that minted it.
    assert await registry.lookup("s", "a", "work", kind="cli") is None
    await backend.run("two", task_id="t2", workspace=tmp_path, session_key="s", instance="work", executor=None)


async def test_a_pruned_session_falls_back_to_a_fresh_one(tmp_path: Path) -> None:
    cfg = stub_config("a")
    registry = InstanceRegistry(path=tmp_path / "instances.json")
    await registry.commit("s", "a", "work", "pruned-session", kind="acp")
    backend = AcpAgentBackend(
        name="a",
        command=cfg.command,
        env=dict(cfg.env),
        snapshot=_snapshot("a", cfg, can_resume=True),
        registry=registry,
    )
    assert await backend.run("go", task_id="t1", workspace=tmp_path, session_key="s", instance="work", executor=None)
    rebound = await registry.lookup("s", "a", "work", kind="acp")
    assert rebound is not None and rebound != "pruned-session"


async def test_a_pruned_session_keeps_the_instance_on_the_strip(tmp_path: Path) -> None:
    """A failed resume learned the id is stale, not that the instance is gone.

    The chip strip and ``/instance`` are drawn from these rows, and the
    replacement id is only committed by a turn that *succeeds* -- so deleting
    the row here took the instance off screen for the length of the recovering
    turn, and left it off if that turn then failed.
    """
    cfg = stub_config("a")
    registry = InstanceRegistry(path=tmp_path / "instances.json")
    await registry.commit("s", "a", "work", "pruned-session", kind="acp")

    assert await registry.unbind("s", "a", "work") is True
    listed = registry.list_instances("s")
    assert [(r["agent"], r["handle"]) for r in listed] == [("a", "work")]
    assert await registry.lookup("s", "a", "work", kind="acp") is None
    # Idempotent: a second failed resume of an already-unbound handle has
    # nothing left to drop and must not report that it did.
    assert await registry.unbind("s", "a", "work") is False

    backend = AcpAgentBackend(
        name="a",
        command=cfg.command,
        env=dict(cfg.env),
        snapshot=_snapshot("a", cfg, can_resume=True),
        registry=registry,
    )
    await backend.run("go", task_id="t1", workspace=tmp_path, session_key="s", instance="work", executor=None)
    assert len(registry.list_instances("s")) == 1


async def test_a_stateless_agent_binds_no_handle(tmp_path: Path) -> None:
    registry = InstanceRegistry(path=tmp_path / "instances.json")
    cfg = stub_config("a")
    backend = AcpAgentBackend(name="a", command=cfg.command, env=dict(cfg.env), snapshot=None, registry=registry)
    await backend.run("go", task_id="t1", workspace=tmp_path, session_key="s", instance="work", executor=None)
    assert await registry.lookup("s", "a", "work", kind="acp") is None


# ---- the pool --------------------------------------------------------------


async def test_two_backends_of_one_agent_share_a_connection(tmp_path: Path) -> None:
    """Why the pool is module state.

    ``SubagentManager`` and ``SubAgentDagTool`` each build their own backend from
    the same config, so a per-backend pool would run two server processes for one
    agent and neither would know about the other.
    """
    cfg = stub_config("shared")
    first, second = build_third_party_backend(cfg), build_third_party_backend(cfg)
    await first.run("a", task_id="t1", workspace=tmp_path, executor=None)
    await second.run("b", task_id="t2", workspace=tmp_path, executor=None)
    assert get_pool().live_agents() == ["shared"]


async def test_concurrent_dispatches_do_not_launch_two_servers(tmp_path: Path) -> None:
    cfg = stub_config("shared")
    backends = [build_third_party_backend(cfg) for _ in range(4)]
    replies = await asyncio.gather(
        *(b.run("go", task_id=f"t{i}", workspace=tmp_path, executor=None) for i, b in enumerate(backends))
    )
    assert replies == ["pong"] * 4
    assert get_pool().live_agents() == ["shared"]


async def test_two_tasks_on_one_handle_take_turns(tmp_path: Path) -> None:
    """Concurrent prompts on one session are serialised, not interleaved.

    ACP notifications name a session but not a request, so two turns in flight on
    one session produce a stream that cannot be split back apart -- one task would
    collect the other's updates and be reported as an empty turn. A stateful agent
    resolves the same ``instance`` handle to the same session id, and ``spawn``
    puts no ordering on that, so this is reachable rather than theoretical.
    """
    cfg = stub_config("shared")
    registry = InstanceRegistry(path=tmp_path / "instances.json")
    # Bound up front, and `can_load` so the binding resolves. Without this the
    # three tasks all look up the handle before any of them has committed one,
    # so all three open their own session and the lock this test is named for is
    # never contended -- it passed with the lock deleted.
    await registry.commit("s", "shared", "work", "stub-session-1", kind="acp")
    backends = [
        AcpAgentBackend(
            name="shared",
            command=cfg.command,
            env=dict(cfg.env),
            snapshot=_snapshot("shared", cfg, can_resume=True, can_load=True),
            registry=registry,
        )
        for _ in range(3)
    ]
    replies = await asyncio.gather(
        *(
            b.run("go", task_id=f"t{i}", workspace=tmp_path, session_key="s", instance="work", executor=None)
            for i, b in enumerate(backends)
        )
    )
    assert replies == ["pong"] * 3
    assert await registry.lookup("s", "shared", "work", kind="acp") == "stub-session-1"


async def test_a_reconfigured_agent_stops_being_served_by_the_old_process(tmp_path: Path) -> None:
    """The pool used to key on the agent name alone.

    So the command an operator edited was read once and then discarded, and the
    old process kept every later dispatch -- while `Test`, which launches its
    own un-pooled client, handshook the new command and reported it ready.
    """
    old = build_third_party_backend(stub_config("a", mode="ok"))
    assert await old.run("ping", task_id="t1", workspace=tmp_path, executor=None) == "pong"
    old_pid = get_pool()._connections["a"].client._proc.pid

    # Same name, different launch config. `empty_turn` is what the new process
    # answers with, so reaching it is observable rather than inferred.
    new = build_third_party_backend(stub_config("a", mode="empty_turn"))
    with pytest.raises(AcpEmptyTurnError):
        await new.run("ping", task_id="t2", workspace=tmp_path, executor=None)
    assert get_pool()._connections["a"].client._proc.pid != old_pid


async def test_an_unchanged_agent_keeps_its_process(tmp_path: Path) -> None:
    """The other half: relaunching on every dispatch would defeat the pool."""
    cfg = stub_config("a")
    first = build_third_party_backend(cfg)
    await first.run("ping", task_id="t1", workspace=tmp_path, executor=None)
    pid = get_pool()._connections["a"].client._proc.pid

    second = build_third_party_backend(stub_config("a"))
    await second.run("ping", task_id="t2", workspace=tmp_path, executor=None)
    assert get_pool()._connections["a"].client._proc.pid == pid


def test_the_launch_key_covers_every_launch_parameter() -> None:
    """A parameter `acquire` launches with but does not key on is discarded.

    Pinned structurally rather than by listing the fields twice: the signature
    is the authority, so a parameter added there fails this until it is either
    keyed on or named as deliberately outside the key.
    """
    import inspect

    from raven.agent.acp.pool import LAUNCH_PARAMS, AcpConnectionPool, launch_key

    # `name` is the dict key itself; `on_request` is a callable, and swapping the
    # handler does not make the running process the wrong one. `ready_timeout_s`
    # is how long this caller waits for the handshake, not what was launched: two
    # entries differing only in patience share the connection they both wanted.
    outside = {"self", "name", "on_request", "ready_timeout_s"}
    params = {p for p in inspect.signature(AcpConnectionPool.acquire).parameters} - outside
    assert params == set(LAUNCH_PARAMS)

    base = {"command": "agent acp", "cwd": "/w", "env": {"A": "1"}}
    for field, other in (("command", "agent acp --url x"), ("cwd", "/other"), ("env", {"A": "2"})):
        assert launch_key(**base) != launch_key(**{**base, field: other}), field
    assert launch_key(**base) == launch_key(**base)
    # An absent env and an empty one are the same launch.
    assert launch_key(command="a", cwd=None, env=None) == launch_key(command="a", cwd=None, env={})


async def test_a_dead_connection_is_replaced(tmp_path: Path) -> None:
    cfg = stub_config("shared")
    backend = build_third_party_backend(cfg)
    await backend.run("a", task_id="t1", workspace=tmp_path, executor=None)
    connection = await get_pool().acquire(name="shared", command=cfg.command, env=dict(cfg.env))
    await connection.client.close()
    assert get_pool().live_agents() == []
    assert await backend.run("b", task_id="t2", workspace=tmp_path, executor=None) == "pong"


# ---- observability ---------------------------------------------------------


@pytest.fixture
def trace_dir(tmp_path, monkeypatch):
    from raven.tracing import spans as _spans

    monkeypatch.setenv("RAVEN_TRACING", "1")
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(tmp_path / "traces"))
    _spans._store = None  # force the store to re-init against the temp dir
    yield tmp_path / "traces"
    _spans._store = None


def _spans_written(trace_dir: Path) -> list[dict]:
    log = trace_dir / "logs" / "audit-spans.log"
    if not log.exists():
        return []
    return [json.loads(line) for line in log.read_text().splitlines() if line.strip()]


async def test_dispatch_records_its_own_span_under_the_calling_span(trace_dir, tmp_path: Path) -> None:
    """Process data lands on the backend's own span, nested under the caller's.

    Opened inside the backend on purpose: `trace` nests on a contextvar, so this
    is what lets `spawn` and `run_subagent_dag` stay untouched and still get the
    external agent's work recorded -- and a DAG node has no span of its own at
    all, so this is the only observability it has.
    """
    from raven.tracing import trace

    backend = build_third_party_backend(stub_config("a"))
    with trace.span("tool.call", {"tool.name": "run_subagent_dag"}) as caller:
        caller_id = caller.span_id
        await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None)

    external = [s for s in _spans_written(trace_dir) if s.get("name") == "subagent.external"]
    assert len(external) == 1
    span = external[0]
    assert span["parentSpanId"] == caller_id
    attrs = span["attributes"]
    assert attrs["subagent.external.agent"] == "a"
    assert attrs["subagent.external.transport"] == "acp"
    assert attrs["subagent.external.stop_reason"] == "end_turn"
    assert attrs["subagent.external.tool_calls"] == ["read_file src/a.py"]
    assert attrs["subagent.external.usage"] == {"size": 1000, "used": 42}
    assert attrs["subagent.external.update_counts"]["agent_message_chunk"] == 1
    # Every update kind seen becomes an event, so a timeline is readable without
    # opening the artifact.
    assert {e["name"] for e in span["events"]} >= {"acp.tool_call", "acp.usage_update"}


async def test_the_full_transcript_is_stored_out_of_line(trace_dir, tmp_path: Path) -> None:
    """The frames go to an artifact, not an attribute.

    It is the audit record: unbounded, and deliberately not subject to
    ``max_output_chars`` -- that cap protects the model's context, and none of
    this enters the model's context.
    """
    backend = build_third_party_backend(stub_config("a", max_output_chars=1))
    assert await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None) == "p"

    span = next(s for s in _spans_written(trace_dir) if s.get("name") == "subagent.external")
    attrs = span["attributes"]
    stored = json.loads(Path(attrs["subagent.external.transcript.artifact_path"]).read_text(encoding="utf-8"))
    kinds = [f["params"]["update"]["sessionUpdate"] for f in stored["frames"]]
    assert kinds == [
        "agent_thought_chunk",
        "tool_call",
        "tool_call_update",
        "agent_message_chunk",
        "usage_update",
    ]


async def test_a_failed_turn_marks_the_span_and_still_stores_the_transcript(trace_dir, tmp_path: Path) -> None:
    backend = build_third_party_backend(stub_config("a", mode="empty_turn"))
    with pytest.raises(AcpEmptyTurnError):
        await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None)

    span = next(s for s in _spans_written(trace_dir) if s.get("name") == "subagent.external")
    assert span["status"]["code"] == "ERROR"


# ---- the cli lane records too -----------------------------------------------


def _cli_stub_config(name: str = "clistub", *, exit_code: int = 0) -> ThirdPartyCliSubagentConfig:
    script = (
        "import sys; "
        "sys.stdout.write('hello from the cli\\n'); "
        "sys.stderr.write('some diagnostics\\n'); "
        f"sys.exit({exit_code})"
    )
    return ThirdPartyCliSubagentConfig(name=name, command=f"{sys.executable} -c {script!r} {{prompt}}")


async def test_the_cli_lane_records_the_same_span_shape(trace_dir, tmp_path: Path) -> None:
    """One span name for both transports, so an audit does not care which ran.

    The cli lane used to keep nothing: stdout was read for a session id and a
    reply and then dropped, with a 2000-char tail reaching an exception message on
    failure and nothing at all on success.
    """
    from raven.agent.subagent.backends.observability import SPAN_NAME

    backend = build_third_party_backend(_cli_stub_config())
    reply = await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None)
    assert "hello from the cli" in reply

    span = next(s for s in _spans_written(trace_dir) if s.get("name") == SPAN_NAME)
    attrs = span["attributes"]
    assert attrs["subagent.external.transport"] == "cli"
    assert attrs["subagent.external.exit_code"] == 0
    assert attrs["subagent.external.invocations"] == 1
    # No timeline, and deliberately not a zero-valued one: this transport has no
    # per-step visibility, which is what the roster's `no-progress` tag says.
    assert "subagent.external.update_counts" not in attrs
    assert not span["events"]

    stored = json.loads(Path(attrs["subagent.external.transcript.artifact_path"]).read_text(encoding="utf-8"))
    assert stored["invocations"][0]["stdout"].strip() == "hello from the cli"
    assert stored["invocations"][0]["stderr"].strip() == "some diagnostics"


async def test_a_failed_cli_invocation_keeps_its_transcript(trace_dir, tmp_path: Path) -> None:
    """The failing run is the one whose output is worth having."""
    from raven.agent.subagent.backends.observability import SPAN_NAME

    backend = build_third_party_backend(_cli_stub_config(exit_code=3))
    with pytest.raises(RuntimeError, match="exited 3"):
        await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None)

    span = next(s for s in _spans_written(trace_dir) if s.get("name") == SPAN_NAME)
    assert span["status"]["code"] == "ERROR"
    assert span["attributes"]["subagent.external.exit_code"] == 3
    stored = json.loads(
        Path(span["attributes"]["subagent.external.transcript.artifact_path"]).read_text(encoding="utf-8")
    )
    assert stored["invocations"][0]["stdout"].strip() == "hello from the cli"


def test_only_the_acp_transport_advertises_live_progress() -> None:
    """An empty timeline must be distinguishable from a quiet run."""
    from raven.agent.subagent.backends import format_agent_listing

    acp = third_party_agent_meta(stub_config("a"))
    cli = third_party_agent_meta(_cli_stub_config("b"))
    assert acp.live_progress is True
    assert cli.live_progress is False
    listing = format_agent_listing([acp, cli])
    assert "a [stateless, local-files, live-progress]" in listing
    assert "b [stateless, local-files, no-progress]" in listing


# ---- shutdown --------------------------------------------------------------


async def test_the_rpc_stack_teardown_closes_the_pool(tmp_path: Path) -> None:
    """An ACP server outlives raven unless something closes it.

    The child is launched with ``start_new_session``, deliberately: an agent that
    reparents a worker would otherwise survive ``proc.kill()``. The cost is that
    it does not get the terminal's signals either, so `raven tui` or `raven serve`
    exiting leaves the agent process running until the machine is rebooted. Only
    the tests called ``close_pool`` before this.
    """
    import raven.rpc.bootstrap as bootstrap

    cfg = stub_config("leftover")
    backend = build_third_party_backend(cfg)
    await backend.run("a", task_id="t1", workspace=tmp_path, executor=None)
    assert get_pool().live_agents() == ["leftover"]

    async def _sink(_frame: dict) -> None:
        return None

    stack = await bootstrap.build_rpc_stack(_sink)
    await asyncio.wait_for(stack.teardown(), timeout=30)

    assert get_pool().live_agents() == [], "the agent process is still running after teardown"


async def test_a_pooled_connection_handshakes_before_anyone_uses_it(tmp_path: Path) -> None:
    """ACP has no usable state before ``initialize``, so the pool owes it.

    Found against a live server, not here: two of the three agents measured answer
    a ``session/new`` sent with no handshake and simply work, so a pool that never
    initialised looked correct until ``codex-acp`` answered ``-32603 Internal
    error``. The stub now refuses pre-handshake requests for that reason.
    """
    connection = await get_pool().acquire(name="a", command=stub_config("a").command, env={"ACP_STUB_MODE": "ok"})
    assert connection.initialize["agentInfo"]["name"] == "stub-agent"
    # And the connection is usable straight away, by a caller that sends nothing
    # but its own request.
    session = await connection.client.request("session/new", {"cwd": str(tmp_path), "mcpServers": []}, timeout=10)
    assert session["sessionId"]


async def test_the_handshake_runs_on_the_entrys_own_budget(tmp_path: Path) -> None:
    """`readyTimeoutMs` is documented as exactly this budget.

    When the handshake moved into the connection it started using a module
    constant instead, so the field stopped governing the one thing its
    documentation names: an operator who raised it for a slow adapter had
    `verify` handshake at 150s and report the agent healthy, while every
    dispatch failed at the pool's 120.
    """
    asked: list[float | None] = []
    pool = get_pool()
    real = pool.acquire

    async def spy(**kw: Any) -> Any:
        asked.append(kw.get("ready_timeout_s"))
        return await real(**kw)

    backend = build_third_party_backend(stub_config("a", ready_timeout_ms=7000))
    pool.acquire = spy  # type: ignore[method-assign]
    try:
        await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None)
    finally:
        pool.acquire = real  # type: ignore[method-assign]

    assert asked == [7.0]


async def test_a_caller_with_no_budget_still_gets_a_bounded_handshake() -> None:
    """The fallback, observed rather than restated.

    Without it the handshake runs on ``timeout=None``, which ``AcpClient.request``
    reads as a bare await: an agent that accepts the connection and never
    answers ``initialize`` hangs ``acquire`` forever while holding that agent's
    lock, so every dispatch to it blocks with nothing to end the wait. That is a
    worse failure than the fixed 120s this revision replaced.
    """
    from raven.agent.acp import pool as pool_mod
    from raven.agent.acp.client import AcpClient

    seen: list[float | None] = []
    real = AcpClient.request

    async def spy(self: AcpClient, method: str, params: Any = None, *, timeout: float | None = None) -> Any:
        if method == "initialize":
            seen.append(timeout)
        return await real(self, method, params, timeout=timeout)

    AcpClient.request = spy  # type: ignore[method-assign]
    try:
        await get_pool().acquire(name="a", command=stub_config("a").command, env={"ACP_STUB_MODE": "ok"})
    finally:
        AcpClient.request = real  # type: ignore[method-assign]

    assert seen == [pool_mod._HANDSHAKE_TIMEOUT_S]


async def test_a_connection_that_cannot_handshake_is_not_kept(tmp_path: Path) -> None:
    """A failed acquire must not leave the process running behind it."""
    cfg = stub_config("a", mode="reject_init")
    with pytest.raises(Exception, match="Invalid params"):
        await get_pool().acquire(name="a", command=cfg.command, env=dict(cfg.env))
    assert get_pool().live_agents() == []


async def test_a_cancelled_connect_does_not_leak_the_process(monkeypatch) -> None:
    """Cancelling an acquire mid-handshake must still reap the child.

    The handshake is the one await in `acquire` long enough to be interrupted --
    an adapter fetched by `npx` may be downloading itself -- and a connection
    cancelled there is in nobody's bookkeeping: it never reached `_connections`,
    so `close_all` at shutdown cannot find it and the process outlives raven.
    """
    from raven.agent.acp.client import AcpClient

    launched: list[AcpClient] = []
    real_launch = AcpClient.launch.__func__

    async def capture(cls: Any, **kwargs: Any) -> AcpClient:
        client = await real_launch(cls, **kwargs)
        launched.append(client)
        return client

    monkeypatch.setattr(AcpClient, "launch", classmethod(capture))

    cfg = stub_config("a", mode="silent")
    task = asyncio.create_task(get_pool().acquire(name="a", command=cfg.command, env=dict(cfg.env)))
    while not launched:
        await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert not launched[0].alive, "the agent process is still running after a cancelled connect"


async def test_a_connection_that_stopped_speaking_is_dead_and_gets_replaced() -> None:
    """The outage shape measured on a live npx adapter: the worker dies, the
    wrapper pid lives on, and `returncode is None` said "alive" forever -- so
    the pool re-issued the same dead connection and every later task failed in
    milliseconds. A closed read loop is a dead connection, whatever ps says.
    """
    from raven.agent.acp.protocol import AcpConnectionError

    cfg = stub_config("mute", mode="mute")
    first = await get_pool().acquire(name="mute", command=cfg.command, env=dict(cfg.env))
    # The stub answered `initialize` and closed stdout; wait for the read loop
    # to notice rather than racing it.
    for _ in range(100):
        if not first.alive:
            break
        await asyncio.sleep(0.05)
    assert not first.alive, "a connection nobody can read from must not read as alive"
    with pytest.raises(AcpConnectionError):
        await first.client.request("session/new", {"cwd": "/tmp", "mcpServers": []}, timeout=5)

    second = await get_pool().acquire(name="mute", command=cfg.command, env=dict(cfg.env))
    assert second is not first, "the pool handed out the dead connection again"
    assert second.alive


async def test_the_eof_error_carries_the_exit_code_and_the_last_stderr() -> None:
    """`exit None; stderr tail: <empty>` was the whole diagnostic for a child
    that had both an exit code and a written reason: EOF races the reap and the
    stderr drain. The read loop now waits for them before composing the error.
    """
    from raven.agent.acp import protocol as acp_protocol
    from raven.agent.acp.client import AcpClient
    from raven.agent.acp.protocol import AcpConnectionError

    cfg = stub_config("abort", mode="abort")
    client = await AcpClient.launch(name="abort", command=cfg.command, env=dict(cfg.env))
    try:
        with pytest.raises(AcpConnectionError) as excinfo:
            await client.request("initialize", acp_protocol.initialize_params(), timeout=10)
        assert "exit 3" in str(excinfo.value), f"the real exit code is missing: {excinfo.value}"
        assert "registry is unreachable" in str(excinfo.value), f"the stderr reason is missing: {excinfo.value}"
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# the live republish has to reach the run it belongs to, and only that one
# ---------------------------------------------------------------------------

_LIVE_UPDATE = {"update": {"sessionUpdate": "agent_message_chunk", "content": [{"type": "text", "text": "pong"}]}}


async def _reader_loop(queue: "asyncio.Queue") -> None:
    """Stands in for the connection's read loop: one long-lived task, created
    when the connection is opened and therefore carrying the context as it was
    *then*, dispatching every later update."""
    while True:
        item = await queue.get()
        if item is None:
            return
        col, method, params = item
        await col(method, params)


async def test_a_live_republish_reaches_the_run_it_belongs_to() -> None:
    """The read loop is created by `pool.acquire()`, before any run opens its
    collection, and a task copies the context at creation -- so publishing
    through the ambient variable from there reached nothing at all on a warm
    connection, while the on-disk record stayed fine."""
    from raven.agent.subagent import activity
    from raven.agent.subagent.backends.acp_agent import _TurnCollector

    queue: asyncio.Queue = asyncio.Queue()
    reader = asyncio.create_task(_reader_loop(queue))  # created OUTSIDE any run
    try:
        with activity.collecting("run") as run:
            col = _TurnCollector()
            await queue.put((col, "session/update", _LIVE_UPDATE))
            await asyncio.sleep(0.05)

            assert run.transcript, "the live view saw nothing while the run was in flight"
    finally:
        await queue.put(None)
        await reader


async def test_one_runs_steps_do_not_land_on_another_runs_record() -> None:
    """Two spawns sharing one pooled connection is what the pool is for. Read
    through the ambient variable, the second run's collector published onto
    whichever run was current when the connection opened -- so a reader watching
    run A was shown run B's tool calls."""
    from raven.agent.subagent import activity
    from raven.agent.subagent.backends.acp_agent import _TurnCollector

    queue: asyncio.Queue = asyncio.Queue()
    with activity.collecting("run-a") as run_a:
        # A opens the connection, so the read loop carries A's context.
        reader = asyncio.create_task(_reader_loop(queue))
        _TurnCollector()
        try:
            with activity.collecting("run-b") as run_b:
                col_b = _TurnCollector()
                await queue.put((col_b, "session/update", _LIVE_UPDATE))
                await asyncio.sleep(0.05)

                assert run_b.transcript, "B's own live view must have B's steps"
            assert not run_a.transcript, "B's steps must not appear on A's record"
        finally:
            await queue.put(None)
            await reader


class TestBackendDispatchSignature:
    """Every backend must accept what ``SubagentManager`` unconditionally sends.

    The manager passes ``provider=`` and ``model=`` to whichever backend it
    resolved, so a backend missing them raises ``TypeError`` before the run
    starts -- the agent never launches, and the user sees a failed task with a
    dispatch error where its output should be. The base protocol declares both,
    but a Protocol is not enforced at runtime and three of the four backends
    grew the parameters while the fourth did not, so nothing caught it.
    """

    def test_every_backend_accepts_the_arguments_the_manager_sends(self) -> None:
        import inspect

        from raven.agent.subagent.backends.acp_agent import AcpAgentBackend
        from raven.agent.subagent.backends.cli_agent import CliAgentBackend
        from raven.agent.subagent.backends.openai_api import OpenAIApiBackend
        from raven.agent.subagent.backends.raven_loop import RavenLoopBackend

        sent = {"task_id", "workspace", "executor", "session_key", "instance", "provider", "model"}
        for backend in (AcpAgentBackend, CliAgentBackend, OpenAIApiBackend, RavenLoopBackend):
            params = inspect.signature(backend.run).parameters
            missing = sent - set(params)
            assert not missing, f"{backend.__name__}.run() cannot accept {sorted(missing)}"


# ---- choosing a permission option -----------------------------------------


def test_the_most_permissive_offered_option_is_the_one_chosen() -> None:
    """By ``kind``, never by ``optionId``.

    The ids are the agent's own vocabulary -- codex happens to mint
    ``allow_always``, another adapter may mint anything -- while the four kinds
    are the protocol's. An id raven invented comes back from codex as a decline.
    """
    from raven.agent.acp.permissions import permission_outcome

    offered = {
        "options": [
            {"optionId": "r", "kind": "reject_once"},
            {"optionId": "o", "kind": "allow_once"},
            {"optionId": "a", "kind": "allow_always"},
        ]
    }
    assert permission_outcome(offered) == {"outcome": "selected", "optionId": "a"}


def test_a_reject_is_never_chosen_over_an_allow() -> None:
    from raven.agent.acp.permissions import permission_outcome

    offered = {"options": [{"optionId": "r", "kind": "reject_always"}, {"optionId": "o", "kind": "allow_once"}]}
    assert permission_outcome(offered) == {"outcome": "selected", "optionId": "o"}


def test_only_rejects_offered_still_selects_one() -> None:
    """Selecting the refusal keeps the turn alive; cancelling ends it.

    Measured on codex-acp: an ``outcome: cancelled`` is ``{decision: "cancel"}``
    for the whole turn, while a selected reject is one tool call declined.
    """
    from raven.agent.acp.permissions import permission_outcome

    assert permission_outcome({"options": [{"optionId": "r", "kind": "reject_once"}]}) == {
        "outcome": "selected",
        "optionId": "r",
    }


def test_an_option_with_no_kind_is_still_answerable() -> None:
    from raven.agent.acp.permissions import permission_outcome

    assert permission_outcome({"options": [{"optionId": "x"}]}) == {"outcome": "selected", "optionId": "x"}


def test_no_options_at_all_is_the_only_cancel() -> None:
    """An ``optionId`` raven made up is indistinguishable from a real choice."""
    from raven.agent.acp.permissions import permission_outcome

    assert permission_outcome({"options": []}) == {"outcome": "cancelled"}
    assert permission_outcome({}) == {"outcome": "cancelled"}


async def test_the_approver_leaves_unsupported_methods_refused() -> None:
    """It answers permissions only; ``fs/*`` is advertised as unsupported."""
    from raven.agent.acp.client import UNHANDLED
    from raven.agent.acp.permissions import auto_approver

    handle = auto_approver("a")
    assert await handle("fs/read_text_file", {"path": "/etc/hostname"}) is UNHANDLED


# ---- cancelling a turn on the agent, not only locally -----------------------


async def test_a_cancelled_turn_is_cancelled_on_the_agent_too() -> None:
    from raven.agent.acp.pool import get_pool

    connection = await get_pool().acquire(
        name="stub",
        command=f"{sys.executable} {_STUB}",
        env={"ACP_STUB_MODE": "cancel_aware"},
        ready_timeout_s=15.0,
    )
    client = connection.client
    session = (await client.request("session/new", {"cwd": "/tmp", "mcpServers": []}, timeout=15.0))["sessionId"]

    task = asyncio.create_task(
        client.request(
            "session/prompt",
            {"sessionId": session, "prompt": [{"type": "text", "text": "hi"}]},
            cancel_session=session,
        )
    )
    await asyncio.sleep(0.5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The stub only answers a held prompt when it is told to stop, so a settled
    # turn is the proof the notification went out and was waited for.
    assert client.take_unsettled_cancel(session) is False


async def test_an_agent_that_ignores_the_cancel_marks_the_session_unsettled() -> None:
    from raven.agent.acp import client as client_mod
    from raven.agent.acp.pool import get_pool

    monkeyed = client_mod._CANCEL_SETTLE_S
    client_mod._CANCEL_SETTLE_S = 0.3
    try:
        connection = await get_pool().acquire(
            name="stub",
            command=f"{sys.executable} {_STUB}",
            env={"ACP_STUB_MODE": "cancel_deaf"},
            ready_timeout_s=15.0,
        )
        client = connection.client
        session = (await client.request("session/new", {"cwd": "/tmp", "mcpServers": []}, timeout=15.0))["sessionId"]

        task = asyncio.create_task(
            client.request(
                "session/prompt",
                {"sessionId": session, "prompt": [{"type": "text", "text": "hi"}]},
                cancel_session=session,
            )
        )
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert client.take_unsettled_cancel(session) is True
        # Consumed: a second read must not unbind a second time.
        assert client.take_unsettled_cancel(session) is False
    finally:
        client_mod._CANCEL_SETTLE_S = monkeyed


async def test_draining_notifies_without_waiting_for_the_turn_to_settle() -> None:
    from raven.agent.acp import client as client_mod
    from raven.agent.acp.pool import get_pool

    connection = await get_pool().acquire(
        name="stub",
        command=f"{sys.executable} {_STUB}",
        env={"ACP_STUB_MODE": "cancel_deaf"},
        ready_timeout_s=15.0,
    )
    client = connection.client
    session = (await client.request("session/new", {"cwd": "/tmp", "mcpServers": []}, timeout=15.0))["sessionId"]

    task = asyncio.create_task(
        client.request(
            "session/prompt",
            {"sessionId": session, "prompt": [{"type": "text", "text": "hi"}]},
            cancel_session=session,
        )
    )
    await asyncio.sleep(0.5)
    client_mod.begin_drain()
    started = time.monotonic()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    elapsed = time.monotonic() - started

    # The default budget is seconds; draining must not pay any of it.
    assert elapsed < 1.0
    assert client.take_unsettled_cancel(session) is False
    assert client_mod.is_draining() is True


async def test_closing_the_pool_leaves_drain_mode() -> None:
    from raven.agent.acp import client as client_mod
    from raven.agent.acp.pool import close_pool

    client_mod.begin_drain()
    await close_pool()
    assert client_mod.is_draining() is False


async def test_a_turn_that_would_not_stop_drops_its_session_binding(tmp_path: Path) -> None:
    """The settle budget expired, so the turn is still running on the agent while
    the session lock is released -- prompting that session again would collide."""
    from raven.agent.acp import client as client_mod

    cfg = stub_config("stub", mode="cancel_deaf")
    registry = InstanceRegistry(path=tmp_path / "inst.json")
    await registry.commit("web:s1", "stub", "h1", "stub-session-1", kind="acp")
    backend = AcpAgentBackend(
        name="stub",
        command=cfg.command,
        env=dict(cfg.env),
        snapshot=_snapshot("stub", cfg, can_resume=True, can_load=False),
        registry=registry,
    )

    streamed = asyncio.Event()

    async def _on_delta(_text: str) -> None:
        streamed.set()

    monkeyed = client_mod._CANCEL_SETTLE_S
    client_mod._CANCEL_SETTLE_S = 0.3
    try:
        task = asyncio.create_task(
            backend.run(
                "hi",
                task_id="t1",
                workspace=tmp_path,
                executor=None,
                session_key="web:s1",
                instance="h1",
                on_delta=_on_delta,
            )
        )
        await asyncio.wait_for(streamed.wait(), 30)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        client_mod._CANCEL_SETTLE_S = monkeyed

    assert await registry.lookup("web:s1", "stub", "h1", kind="acp") is None
