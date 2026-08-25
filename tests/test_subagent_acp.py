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
    cfg = SubagentsConfig(agents=[{"name": "a", "kind": "acp", "command": "hermes acp"}]).agents[0]
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
    cfg = SubagentsConfig(agents=entries).agents[0]
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

    legacy = SubagentsConfig(agents=[{"name": "codex", "kind": "cli", "command": "codex exec --json {prompt}"}]).agents[
        0
    ]
    assert legacy.preset == "codex"
    assert THIRD_PARTY_SUBAGENT_PRESETS["codex"]["kind"] == "acp"
    assert _upgrade_transport(legacy, "config") == "acp"

    # An entry already on the preset's transport is current, and a preset row is
    # by definition current.
    current = SubagentsConfig(agents=[dict(THIRD_PARTY_SUBAGENT_PRESETS["codex"])]).agents[0]
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

    The call is named by the transport, not by the adapter's title and not in
    raven's vocabulary: the record has to say what the agent actually ran, and
    the read boundary is what turns that name into the one a renderer keys a
    verb off.
    """
    from raven.agent.subagent import activity

    backend = build_third_party_backend(stub_config("a"))
    with activity.collecting() as did:
        await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None)

    assert did.transcript, "the acp backend must publish its transcript"
    call = next(m for m in did.transcript if m.get("tool_calls"))
    assert call["role"] == "assistant"
    assert call["reasoning_content"] == "thinking"
    assert call["tool_calls"][0]["function"]["name"] == "read", "the record keeps the transport's own name"
    assert json.loads(call["tool_calls"][0]["function"]["arguments"]) == {"path": "src/a.py"}
    result = next(m for m in did.transcript if m.get("role") == "tool")
    assert result["tool_call_id"] == "t1"
    assert result["content"] == "the file says hello"


async def test_the_collector_serves_a_timestamped_partial_while_in_flight() -> None:
    """A watching panel reads the run as it happens: every rendered message
    carries the wall clock of the event that opened it, and the in-flight shape
    appends whatever answer text has streamed so far -- which the settled shape
    must NOT carry, because the record keeps the answer in out.md.

    The shape itself is pinned in ``tests/test_subagent_turn_rows.py`` now that
    both transports build it through one module. This case stays end to end on
    purpose: an adapter that mapped the wrong event field would satisfy every
    unit test over there and still produce the wrong rows here."""
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
    assert attrs["subagent.external.tool_calls"] == ["read src/a.py"]
    assert attrs["subagent.external.usage"] == {"size": 1000, "used": 42}
    assert attrs["subagent.external.update_counts"]["agent_message_chunk"] == 1
    # Every update kind seen becomes an event, so a timeline is readable without
    # opening the artifact.
    assert {e["name"] for e in span["events"]} >= {"acp.tool_call", "acp.usage_update"}


async def test_the_call_names_its_own_stretch_of_the_wire_journal(trace_dir, tmp_path: Path) -> None:
    """The span points at the frames rather than copying them.

    The journal is written as the frames cross the wire, so persisting a second
    per-call copy would double the audit trail and give two places for it to
    disagree. What the call owes instead is the range it occupied: its own
    ``[start, end)``, plus where the connection's handshake ends, since that
    belongs to the connection and not to any one call.

    Deliberately not subject to ``max_output_chars`` -- that cap protects the
    model's context, and none of this enters it.
    """
    backend = build_third_party_backend(stub_config("a", max_output_chars=1))
    assert await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None) == "p"

    attrs = next(s for s in _spans_written(trace_dir) if s.get("name") == "subagent.external")["attributes"]
    path = Path(attrs["subagent.external.frames.path"])
    start = attrs["subagent.external.frames.start"]
    end = attrs["subagent.external.frames.end"]
    handshake_end = attrs["subagent.external.frames.handshake_end"]
    assert path.is_file()
    assert 0 < handshake_end <= start < end <= path.stat().st_size
    assert attrs["subagent.external.frames.session_id"] == attrs["subagent.external.session_id"]

    # The range is a byte range into the file, so it has to slice cleanly.
    lines = [json.loads(line) for line in path.read_bytes()[start:end].decode().splitlines() if line.strip()]
    sent = [r["frame"]["method"] for r in lines if r.get("dir") == "out" and "method" in r.get("frame", {})]
    assert sent == ["session/new", "session/prompt"]
    kinds = [
        r["frame"]["params"]["update"]["sessionUpdate"]
        for r in lines
        if r.get("dir") == "in" and r.get("frame", {}).get("method") == "session/update"
    ]
    assert kinds == [
        "agent_thought_chunk",
        "tool_call",
        "tool_call_update",
        "tool_call_update",
        "agent_message_chunk",
        "usage_update",
    ]


def _journal_records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _journal_for(agent: str) -> Path:
    from raven.agent.acp.journal import journal_root

    files = sorted(journal_root().rglob(f"{agent}-*.jsonl"))
    assert files, f"no journal was opened for {agent!r}"
    return files[-1]


async def test_the_handshake_opens_the_journal_before_any_call(tmp_path: Path) -> None:
    """``initialize`` belongs to the connection, so it is the head of the file.

    A per-call record could never hold it: one process serves every session of
    an agent, and the call that happened to start the connection is not the one
    the handshake is about.
    """
    backend = build_third_party_backend(stub_config("a"))
    await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None)

    records = _journal_records(_journal_for("a"))
    assert records[0]["dir"] == "out"
    assert records[0]["frame"]["method"] == "initialize"
    assert "session" not in records[0], "a connection-level frame belongs to no session"
    assert records[1]["dir"] == "in"
    assert records[1]["frame"]["result"]["protocolVersion"] == 1


async def test_an_agent_request_and_ravens_answer_are_both_journalled(tmp_path: Path) -> None:
    """The one exchange nothing else records.

    ``session/request_permission`` is answered automatically and unattended, so
    before the journal there was no record anywhere of what raven approved on a
    sub-agent's behalf -- only a debug log line. Both halves are journalled, and
    the answer is attributed to the session that asked even though a response
    frame carries only the id it answers.
    """
    backend = build_third_party_backend(stub_config("a", mode="permission"))
    await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None)

    records = _journal_records(_journal_for("a"))
    ask = next(
        r for r in records if r.get("dir") == "in" and r.get("frame", {}).get("method") == "session/request_permission"
    )
    answer = next(r for r in records if r.get("dir") == "out" and r.get("frame", {}).get("id") == ask["frame"]["id"])
    assert answer["frame"]["result"]["outcome"] == {"outcome": "selected", "optionId": "always"}
    assert ask["session"] == answer["session"], "the answer must carry the session that asked"


async def test_an_unrouted_notification_is_journalled_rather_than_dropped(tmp_path: Path) -> None:
    """A frame no session is listening for still crossed the wire.

    The router drops it with a debug line, which is the right call for the
    transcript and leaves nothing behind. The journal records inbound frames
    before routing, so a late or stray update is still evidence.
    """
    backend = build_third_party_backend(stub_config("a", mode="stray_session"))
    assert await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None) == "pong"

    records = _journal_records(_journal_for("a"))
    assert any(r.get("session") == "no-such-session" for r in records), "the stray update went unrecorded"


async def test_stderr_is_journalled_beside_the_frames(tmp_path: Path) -> None:
    """An adapter can report a fatal condition only on stderr.

    Measured: a provider ``HTTP 401`` still answered ``stopReason: end_turn``.
    The tail is carried in the raised error, but the record kept none of it.
    """
    backend = build_third_party_backend(stub_config("a", mode="empty_turn"))
    with pytest.raises(AcpEmptyTurnError):
        await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None)

    records = _journal_records(_journal_for("a"))
    assert any(r.get("dir") == "err" and "401" in r.get("text", "") for r in records)


async def test_an_oversized_stderr_line_does_not_stop_the_drain(tmp_path: Path) -> None:
    """The measured deadlock: one long stderr line took the drain down for good.

    ``readline`` raises past its limit, and the reader used to exit its loop on
    that -- so nothing emptied the pipe again. The child blocked at its next
    write, inside ``logging.StreamHandler.emit`` holding the handler lock, and
    every thread that logged queued behind it: 0% CPU, no error, no exit. It was
    litellm's DEBUG request dumps that supplied the 146 KiB line.

    Answering the turn only proves the raise was survived. The line written
    *after* the oversized one is what proves the drain went on.
    """
    backend = build_third_party_backend(stub_config("a", mode="flood"))
    assert await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None) == "pong"

    records = _journal_records(_journal_for("a"))
    assert any(r.get("dir") == "err" and "still talking after the flood" in r.get("text", "") for r in records), (
        "the reader stopped draining at the oversized line"
    )


async def test_a_failed_turn_still_records_where_its_frames_are(tmp_path: Path) -> None:
    """The failing call is the one whose wire log is worth finding."""
    from raven.agent.subagent import activity

    backend = build_third_party_backend(stub_config("a", mode="empty_turn"))
    with activity.collecting() as did:
        with pytest.raises(AcpEmptyTurnError):
            await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None)

    assert Path(did.frames["path"]).is_file()
    assert did.frames["start"] < did.frames["end"]
    assert did.as_meta()["acp_frames"] == did.frames


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


async def test_an_observer_that_raises_still_yields_the_approval() -> None:
    """An unanswered permission request cancels the whole turn."""
    from raven.agent.acp.permissions import auto_approver
    from tests import acp_frames

    async def boom(method: str, params: dict[str, object]) -> None:
        raise RuntimeError("observer is broken")

    handle = auto_approver("codex", observe=boom)
    answer = await handle("session/request_permission", acp_frames.CODEX_READ_PERMISSION)

    assert answer == {"outcome": {"outcome": "selected", "optionId": "allow_always"}}


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


# ---- what a tool call actually leaves in the transcript ---------------------
#
# Every frame quoted below was captured from a live adapter, because the shapes
# the collector was written against turned out to be the ones no adapter sends:
# claude-agent-acp 0.66.0, codex-acp 1.1.14 and opencode-ai 1.18.16 all left the
# transcript with empty tool arguments and an empty tool result.


async def test_the_collector_reads_a_tool_result_through_the_acp_content_wrapper() -> None:
    """A tool's output sits one level deeper than a message's.

    ``agent_message_chunk`` carries a bare content block, but a tool call's
    content is a list of ``ToolCallContent`` -- ``{"type": "content", "content":
    <block>}`` -- and reading it as a block yields nothing. Measured on both
    claude-agent-acp and opencode: every tool result in the record was an empty
    string, which reads as a tool that returned nothing rather than a reader
    that could not see it.
    """
    from raven.agent.subagent.backends.acp_agent import _TurnCollector

    col = _TurnCollector()

    async def feed(payload: dict) -> None:
        await col("session/update", {"update": payload})

    await feed({"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "read", "status": "pending"})
    await feed(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "t1",
            "status": "completed",
            "content": [{"type": "content", "content": {"type": "text", "text": "ZORKMID-4417"}}],
        }
    )

    result = next(m for m in col.messages() if m.get("role") == "tool")
    assert result["content"] == "ZORKMID-4417"


async def test_the_collector_backfills_tool_input_arriving_after_the_call() -> None:
    """The opening ``tool_call`` announces the call; the input follows it.

    Measured on claude-agent-acp and opencode alike: the first frame carries
    ``rawInput: {}`` and the real arguments arrive on a later
    ``tool_call_update``. Reading only the opening frame recorded every call in
    the transcript as ``arguments: "{}"`` -- a call with no arguments is
    indistinguishable from one whose arguments were never read.
    """
    from raven.agent.subagent.backends.acp_agent import _TurnCollector

    col = _TurnCollector()

    async def feed(payload: dict) -> None:
        await col("session/update", {"update": payload})

    await feed({"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "read", "rawInput": {}})
    await feed(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "t1",
            "status": "in_progress",
            "rawInput": {"filePath": "/w/note.txt"},
        }
    )
    await feed({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"})

    call = next(m for m in col.messages() if m.get("tool_calls"))
    assert json.loads(call["tool_calls"][0]["function"]["arguments"]) == {"filePath": "/w/note.txt"}


async def test_the_collector_falls_back_to_raw_output_when_a_tool_reports_no_content() -> None:
    """``content`` is optional, and codex-acp does not send it at all.

    Its results arrive only as ``rawOutput``, so without this the whole codex
    lane records every tool result as empty. Serialised rather than skipped when
    it is not a string: the shape differs per adapter and per tool, and a reader
    is better served by the adapter's own JSON than by nothing.
    """
    from raven.agent.subagent.backends.acp_agent import _TurnCollector

    col = _TurnCollector()

    async def feed(payload: dict) -> None:
        await col("session/update", {"update": payload})

    await feed({"sessionUpdate": "tool_call", "toolCallId": "exec-1", "title": "Read file '/w/note.txt'"})
    await feed(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "exec-1",
            "status": "completed",
            "rawOutput": {"formatted_output": "ZORKMID-4417\n", "exit_code": 0},
        }
    )

    result = next(m for m in col.messages() if m.get("role") == "tool")
    assert "ZORKMID-4417" in result["content"]


async def test_the_collector_prefers_content_over_raw_output() -> None:
    """``rawOutput`` is the fallback, not the source.

    claude-agent-acp sends both, and its ``rawOutput`` is the tool's untreated
    output (line-numbered file text) where ``content`` is what the adapter chose
    to show. Preferring the raw form would swap a rendered result for a noisier
    one on every adapter that sends both.
    """
    from raven.agent.subagent.backends.acp_agent import _TurnCollector

    col = _TurnCollector()

    async def feed(payload: dict) -> None:
        await col("session/update", {"update": payload})

    await feed({"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Read File"})
    await feed(
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "t1",
            "status": "completed",
            "rawOutput": "1\tZORKMID-4417\n2\t",
            "content": [{"type": "content", "content": {"type": "text", "text": "```\n1\tZORKMID-4417\n```"}}],
        }
    )

    result = next(m for m in col.messages() if m.get("role") == "tool")
    assert result["content"] == "```\n1\tZORKMID-4417\n```"


async def test_a_revised_tool_input_replaces_the_one_already_recorded() -> None:
    """A later ``rawInput`` is a revision, not a duplicate.

    ACP defines a ``tool_call_update`` as replacing the fields it carries, so
    the newest input is the one the tool actually ran with. The empty
    ``rawInput`` every measured adapter sends on the opening frame is the one
    exception: it is not a revision to no arguments, so it cannot erase a real
    input recorded before it.
    """
    from raven.agent.subagent.backends.acp_agent import _TurnCollector

    col = _TurnCollector()

    async def feed(payload: dict) -> None:
        await col("session/update", {"update": payload})

    await feed({"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "read", "rawInput": {"path": "a.py"}})
    await feed({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "rawInput": {"path": "b.py"}})
    await feed({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed", "rawInput": {}})

    call = next(m for m in col.messages() if m.get("tool_calls"))
    assert json.loads(call["tool_calls"][0]["function"]["arguments"]) == {"path": "b.py"}


async def test_the_dispatch_binds_the_session_to_its_instance(tmp_path: Path) -> None:
    """The one thing ACP cannot carry, written where both sides are known.

    ``AcpAgentBackend.run`` is the only place holding raven's identity and the
    agent's session id at the same time: the client that writes the journal sees
    a connection and a session, and has never heard of an instance.
    """
    backend = build_third_party_backend(stub_config("a"))
    await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None, session_key="web:abc", instance="h1")

    bind = next(r for r in _journal_records(_journal_for("a")) if r.get("_type") == "acp_call")
    assert bind["agent"] == "a"
    assert bind["instance"] == "h1"
    assert bind["task_id"] == "t1"
    assert bind["session_key"] == "web:abc"
    assert bind["resumed"] is False
    assert bind["session"], "the ACP session id is the join key the frames carry"


async def test_a_call_without_an_instance_binds_its_task_id(tmp_path: Path) -> None:
    """``handle = instance or task_id``, so a call that named no instance is
    still addressable in the journal rather than anonymous."""
    backend = build_third_party_backend(stub_config("a"))
    await backend.run("ping", task_id="t7", workspace=tmp_path, executor=None)

    bind = next(r for r in _journal_records(_journal_for("a")) if r.get("_type") == "acp_call")
    assert bind["instance"] == "t7"


async def test_the_instance_gets_a_transcript_in_the_session_log_format(tmp_path: Path) -> None:
    """An instance's conversation reads like a raven session, because it is one.

    Same grammar as ``sessions/<group>/<chat_id>.jsonl`` -- a ``_type:
    "metadata"`` header and untagged message rows -- so anything that can read a
    conversation can read a sub-agent instance's.
    """
    from raven.agent.subagent import activity
    from raven.agent.subagent.instance_log import transcript_path
    from raven.agent.subagent_history import SpawnRecord

    session_dir = tmp_path / "session"
    backend = build_third_party_backend(stub_config("a"))
    record = SpawnRecord.open(
        session_dir, task_id="t1", task="ping", meta={"agent": "a", "handle": "h1", "session_key": "web:abc"}
    )
    with activity.collecting() as did:
        reply = await backend.run(
            "ping", task_id="t1", workspace=tmp_path, executor=None, session_key="web:abc", instance="h1"
        )
    record.finish(status="completed", output=reply, activity=did)

    rows = [json.loads(x) for x in transcript_path(session_dir, "a", "h1").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["_type"] == "metadata"
    assert rows[0]["metadata"] == {
        "session_key": "web:abc",
        "agent": "a",
        "handle": "h1",
        "opened_by": "spawn",
    }
    assert rows[1] == {"role": "user", "content": "ping", "timestamp": rows[1]["timestamp"]}
    assert any(r.get("tool_calls") for r in rows[2:]), "the steps the transport could see"
    assert rows[-1]["role"] == "assistant" and rows[-1]["content"] == reply
    assert all("_type" not in r for r in rows[1:]), "a message row is untagged, as the session log writes it"


async def test_two_calls_of_one_instance_land_in_one_file(tmp_path: Path) -> None:
    """The whole point of an instance log: a handle dispatched twice is one
    conversation, not two records to stitch together."""
    from raven.agent.subagent import activity
    from raven.agent.subagent.instance_log import transcript_path
    from raven.agent.subagent_history import SpawnRecord

    session_dir = tmp_path / "session"
    backend = build_third_party_backend(stub_config("a"))
    for i in (1, 2):
        record = SpawnRecord.open(
            session_dir,
            task_id=f"t{i}",
            task=f"ping {i}",
            meta={"agent": "a", "handle": "h1", "session_key": "web:abc"},
        )
        with activity.collecting() as did:
            reply = await backend.run(
                f"ping {i}", task_id=f"t{i}", workspace=tmp_path, executor=None, session_key="web:abc", instance="h1"
            )
        record.finish(status="completed", output=reply, activity=did)

    rows = [json.loads(x) for x in transcript_path(session_dir, "a", "h1").read_text(encoding="utf-8").splitlines()]
    assert sum(1 for r in rows if r.get("_type") == "metadata") == 1, "one header, written once"
    assert [r["content"] for r in rows if r.get("role") == "user"] == ["ping 1", "ping 2"]


async def test_a_lane_with_no_frames_still_joins_the_instance_conversation(tmp_path: Path) -> None:
    """The cli transport sees no wire, so it contributes what it has -- the
    prompt and the answer -- and the instance's conversation is the union of
    every lane that addressed it, whatever each could see."""
    from raven.agent.subagent import activity
    from raven.agent.subagent.instance_log import transcript_path
    from raven.agent.subagent_history import SpawnRecord

    session_dir = tmp_path / "session"
    backend = build_third_party_backend(_cli_stub_config())
    record = SpawnRecord.open(
        session_dir, task_id="t1", task="ping", meta={"agent": "cli", "handle": "h1", "session_key": "web:abc"}
    )
    with activity.collecting() as did:
        reply = await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None)
    record.finish(status="completed", output=reply, activity=did)

    assert did.frames == {}
    rows = [json.loads(x) for x in transcript_path(session_dir, "cli", "h1").read_text(encoding="utf-8").splitlines()]
    assert [r.get("role") for r in rows[1:]] == ["user", "assistant"]


async def test_a_cancelled_turn_still_records_where_its_frames_are(tmp_path: Path) -> None:
    """A turn cut short is the one whose wire log matters most.

    The cancellation path returns no result, so nothing downstream would publish
    the range -- and a timed-out call's record would point at nothing while the
    connection journal held the whole exchange. Measured against a real adapter:
    an ``opencode`` dispatch that ran past its budget left a record with no
    frames at all before this.
    """
    from raven.agent.acp import client as client_mod
    from raven.agent.subagent import activity

    backend = build_third_party_backend(stub_config("a", mode="cancel_deaf"))
    streamed = asyncio.Event()

    async def _on_delta(_text: str) -> None:
        streamed.set()

    settle = client_mod._CANCEL_SETTLE_S
    client_mod._CANCEL_SETTLE_S = 0.3
    try:
        with activity.collecting() as did:
            task = asyncio.create_task(
                backend.run("hi", task_id="t1", workspace=tmp_path, executor=None, on_delta=_on_delta)
            )
            await asyncio.wait_for(streamed.wait(), 30)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
    finally:
        client_mod._CANCEL_SETTLE_S = settle

    assert did.frames, "a cancelled turn must still say where its frames are"
    assert Path(did.frames["path"]).is_file()
    assert did.frames["start"] < did.frames["end"]


async def test_narration_lands_on_the_step_it_preceded(tmp_path: Path) -> None:
    """A turn that talks as it works reads as a conversation, not a blob.

    Measured on codex-acp: a turn states a plan, then a progress note before
    each of three calls, then reports. Every burst used to be joined into the
    single closing message, so the transcript showed no prose at all and the
    answer opened with a restated plan followed by two notes about work the
    reader could already see was done.
    """
    from raven.agent.subagent import activity

    backend = build_third_party_backend(stub_config("a", mode="two_messages"))
    with activity.collecting() as did:
        out = await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None)

    # Unchanged: the caller receiving the run's answer wants all of it.
    assert out == "let me look.\n\nit is a repo."

    call = next(m for m in did.transcript if m.get("tool_calls"))
    assert call["content"] == "let me look.", "the preamble belongs to the step it announced"
    assert did.closing == "it is a repo.", "and only what followed the last step is the reply"


async def test_the_answer_row_is_what_was_said_last_not_every_burst(tmp_path: Path) -> None:
    """The closing row and the narration rows must not hold the same prose.

    End to end, because the split spans three places -- the collector decides
    it, the activity record carries it, and the log writer prefers it over the
    run's full output.
    """
    from raven.agent.subagent import activity
    from raven.agent.subagent.instance_log import transcript_path
    from raven.agent.subagent_history import SpawnRecord

    session_dir = tmp_path / "session"
    backend = build_third_party_backend(stub_config("a", mode="two_messages"))
    record = SpawnRecord.open(
        session_dir, task_id="t1", task="ping", meta={"agent": "a", "handle": "h1", "session_key": "web:abc"}
    )
    with activity.collecting() as did:
        reply = await backend.run(
            "ping", task_id="t1", workspace=tmp_path, executor=None, session_key="web:abc", instance="h1"
        )
    record.finish(status="completed", output=reply, activity=did)

    rows = [json.loads(x) for x in transcript_path(session_dir, "a", "h1").read_text(encoding="utf-8").splitlines()]
    assert rows[-1] == {"role": "assistant", "content": "it is a repo.", "timestamp": rows[-1]["timestamp"]}
    assert sum(1 for r in rows if "let me look." in str(r.get("content"))) == 1


async def test_a_turn_that_ends_on_a_step_writes_no_answer_row(tmp_path: Path) -> None:
    """Saying nothing after the last call is a real outcome, not a missing one.

    So the closing is empty rather than absent, and an empty closing must not
    fall back to the full output -- that is what would put the prose in twice.
    """
    from raven.agent.subagent.backends.acp_agent import _TurnCollector

    col = _TurnCollector()

    async def feed(payload: dict) -> None:
        await col("session/update", {"update": payload})

    await feed({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "on it."}})
    await feed({"sessionUpdate": "tool_call", "toolCallId": "t1", "kind": "execute", "title": "ls"})
    await feed({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"})

    assert col.text == "on it."
    assert col.closing_text == ""
    call = next(m for m in col.messages() if m.get("tool_calls"))
    assert call["content"] == "on it."


async def test_the_live_transcript_streams_only_the_closing_burst() -> None:
    """The live view and the settled record must agree on where prose sits.

    In flight the tail is appended as a message; if that tail were the whole
    answer it would repeat the narration already sitting on the steps above it.
    """
    from raven.agent.subagent.backends.acp_agent import _TurnCollector

    col = _TurnCollector()

    async def feed(payload: dict) -> None:
        await col("session/update", {"update": payload})

    await feed({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "looking."}})
    await feed({"sessionUpdate": "tool_call", "toolCallId": "t1", "kind": "execute", "title": "ls"})
    await feed({"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"})
    await feed({"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "found it."}})

    live = col.messages(in_flight=True)
    assert live[0]["content"] == "looking."
    assert live[-1] == {"role": "assistant", "content": "found it.", "timestamp": live[-1]["timestamp"]}


async def test_the_partial_notice_reaches_the_closing_row(tmp_path: Path) -> None:
    """The one line saying the reply is incomplete must be in the record too.

    It is appended by raven after the agent has stopped, so it is not in any
    burst the collector saw -- without this the instance log's last row is the
    partial answer with nothing marking it as partial.
    """
    from raven.agent.subagent import activity

    backend = build_third_party_backend(stub_config("a", mode="cancelled"))
    with activity.collecting() as did:
        await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None)

    assert did.closing is not None
    assert "stopped before finishing" in did.closing


async def test_tool_results_arrive_without_their_transport_wrapping(tmp_path: Path) -> None:
    """What the renderer shows is the output, not the envelope it came in.

    The stub sends the measured ``ToolCallContent`` wrapper; a codex-shaped
    ``rawOutput`` object and a claude-shaped markdown fence are covered by
    ``test_acp_dialects.py`` against the frames each adapter really sends.
    """
    from raven.agent.subagent import activity

    backend = build_third_party_backend(stub_config("a"))
    with activity.collecting() as did:
        await backend.run("ping", task_id="t1", workspace=tmp_path, executor=None)

    result = next(m for m in did.transcript if m.get("role") == "tool")
    assert result["content"] == "the file says hello"
    assert not result["content"].startswith("[failed]")


import pytest

from raven.agent.subagent.acp_dialects import CodexDialect
from raven.agent.subagent.backends.acp_agent import _TurnCollector
from tests import acp_frames


@pytest.mark.asyncio
async def test_a_completing_frame_does_not_rename_the_call() -> None:
    """The name survives a completing frame, open to close.

    Not proof of the `names_call` guard by itself: both `CODEX_WEBSEARCH_OPEN`
    and `CODEX_WEBSEARCH_DONE` carry `rawInput.type == "webSearch"`, so
    `tool_name` already resolves `webSearch` on either frame regardless of
    `kind`, and the guard's decision is never observed here. It is observed on
    the MCP shape below, where the completing frame carries no discriminator at
    all.
    """
    collector = _TurnCollector(dialect=CodexDialect())

    await collector("session/update", {"update": acp_frames.CODEX_WEBSEARCH_OPEN})
    await collector("session/update", {"update": acp_frames.CODEX_WEBSEARCH_DONE})

    assert [c.name for c in collector.calls] == ["webSearch"]


@pytest.mark.asyncio
async def test_a_kindless_mcp_completion_does_not_rename_the_call() -> None:
    """The shape where the `names_call` guard is the reason the name survives.

    An MCP completion carries neither `kind` nor `_meta`: `revises_call` is
    True (there is a non-empty `rawInput`) but `names_call` is False, so the
    guard is what keeps the opening frame's `mcp.fs.read` instead of falling
    through to the base dialect's fallback `tool_call` -- the pre-Task-1 bug.
    Synthetic frames, shaped from the adapter's `createMcpToolCallUpdate`
    (open) and `completeItemEvent`'s `mcpToolCall` arm (completion); no capture
    reached this branch, so these are not in `tests/acp_frames.py`.
    """
    collector = _TurnCollector(dialect=CodexDialect())

    mcp_open = {
        "sessionUpdate": "tool_call",
        "toolCallId": "mcp-1",
        "kind": "execute",
        "status": "in_progress",
        "_meta": {"is_mcp_tool_call": True},
        "rawInput": {"server": "fs", "tool": "read", "arguments": {}},
    }
    mcp_done = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "mcp-1",
        "status": "completed",
        "rawInput": {"server": "fs", "tool": "read", "arguments": {}},
    }

    await collector("session/update", {"update": mcp_open})
    await collector("session/update", {"update": mcp_done})

    assert [c.name for c in collector.calls] == ["mcp.fs.read"]


@pytest.mark.asyncio
async def test_a_patch_row_names_the_file_it_changed() -> None:
    """Two edits rendered identically as `exec apply_patch` before this."""
    collector = _TurnCollector(dialect=CodexDialect())

    await collector("session/update", {"update": acp_frames.CODEX_PATCH_OPEN})
    await collector("session/update", {"update": acp_frames.CODEX_PATCH_DONE})

    call = collector.calls[0]
    assert call.name == "apply_patch"
    assert call.subject == "calc.py"


@pytest.mark.asyncio
async def test_a_patch_rows_stored_command_does_not_shadow_its_subject() -> None:
    """`rawInput.command` on `apply_patch` is the tool's own name, not an argument.

    `_TurnCollector._backfill_subject` must drop it once the file subject is
    known, or the stored record still reads `{"path": "calc.py", "command":
    "apply_patch", ...}` -- correct by insertion order, but a reader that picks
    a subject by scanning known key names instead (``ui-tui``'s
    ``callSubject``) finds ``command`` first and shows the tool's own name.
    """
    collector = _TurnCollector(dialect=CodexDialect())

    await collector("session/update", {"update": acp_frames.CODEX_PATCH_OPEN})
    await collector("session/update", {"update": acp_frames.CODEX_PATCH_DONE})

    stored = json.loads(collector.calls[0].arguments_json())
    assert "command" not in stored
    assert next(iter(stored)) == "path"
    assert stored["path"] == "calc.py"


@pytest.mark.asyncio
async def test_a_search_row_keeps_its_own_subject_despite_a_patch_shaped_result() -> None:
    """`subject_from_result` is scoped to `apply_patch`/`imageGeneration`.

    Without that gate, a `commandExecution.search` row is exposed to any result
    whose output happens to contain a patch-envelope-shaped line: nothing on
    the completed frame identifies which tool it belongs to (`tool_name` falls
    through to the base fallback on it, the same gap `_backfill_subject`'s own
    docstring notes), so an ungated match would silently relabel this row's
    subject with the injected path instead of leaving it alone.

    Synthetic frames, shaped from the adapter's `createCommandActionEvent`
    `search` arm (`ParsedCommand::Search` carries no `rawInput` on either
    frame); no capture reached a `.search` row, so these are not in
    `tests/acp_frames.py`.
    """
    collector = _TurnCollector(dialect=CodexDialect())

    search_open = {
        "sessionUpdate": "tool_call",
        "toolCallId": "exec-search-1",
        "status": "in_progress",
        "kind": "search",
        "title": "Searching for 'Update File' in .",
    }
    search_done = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "exec-search-1",
        "status": "completed",
        "rawOutput": {"formatted_output": "*** Update File: sneaky.py\n", "exit_code": 0},
    }

    await collector("session/update", {"update": search_open})
    await collector("session/update", {"update": search_done})

    call = collector.calls[0]
    assert call.name == "commandExecution.search"
    assert call.subject == "Searching for 'Update File' in ."


@pytest.mark.asyncio
async def test_a_permission_frame_restores_the_command_a_read_hid() -> None:
    """codex badges `sed -n ... calc.py` as a read and drops the command.

    The permission request for the same toolCallId still has it.
    """
    collector = _TurnCollector(dialect=CodexDialect())

    await collector("session/update", {"update": acp_frames.CODEX_READ_OPEN})
    await collector("session/request_permission", acp_frames.CODEX_READ_PERMISSION)

    call = collector.calls[0]
    assert call.name == "commandExecution.read"
    assert call.subject == "sed -n '1,200p' calc.py"


@pytest.mark.asyncio
async def test_a_recovered_read_command_survives_the_apply_patch_guard() -> None:
    """The `apply_patch` guard must not over-drop a real recovered command.

    `commandExecution.read`'s `command` comes from the permission frame and is
    the actual shell command, never the call's own name, so the guard added
    for `apply_patch` -- drop `command` only when it equals `previous.name` --
    must leave it in place.
    """
    collector = _TurnCollector(dialect=CodexDialect())

    await collector("session/update", {"update": acp_frames.CODEX_READ_OPEN})
    await collector("session/request_permission", acp_frames.CODEX_READ_PERMISSION)

    stored = json.loads(collector.calls[0].arguments_json())
    assert stored["command"] == "sed -n '1,200p' calc.py"


@pytest.mark.asyncio
async def test_a_plan_is_one_row_that_moves() -> None:
    """Five snapshots for one plan; one row per frame would be five rows."""
    collector = _TurnCollector(dialect=CodexDialect())

    await collector("session/update", {"update": acp_frames.CODEX_PLAN_FIRST})
    await collector("session/update", {"update": acp_frames.CODEX_PLAN_SECOND})

    assert [c.name for c in collector.calls] == ["update_plan"]
    assert collector.calls[0].subject == "Fix add() using the patch tool"

    rows = collector.messages()
    results = [r for r in rows if r.get("role") == "tool"]
    assert len(results) == 1
    assert results[0]["content"] == (
        "[x] Show the contents of calc.py with a shell command\n[>] Fix add() using the patch tool"
    )


@pytest.mark.asyncio
async def test_only_the_first_plan_frame_breaks_the_message() -> None:
    """A moving plan must not fragment the narration around it."""
    collector = _TurnCollector(dialect=CodexDialect())

    await collector("session/update", {"update": acp_frames.CODEX_PLAN_FIRST})
    await collector(
        "session/update",
        {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Working on it."}}},
    )
    await collector("session/update", {"update": acp_frames.CODEX_PLAN_SECOND})
    await collector(
        "session/update",
        {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": " Nearly done."}}},
    )

    assert collector.text == "Working on it. Nearly done."


@pytest.mark.asyncio
async def test_a_backfilled_subject_overwrites_a_colliding_raw_input_key() -> None:
    """The recovered subject must win a `raw_input["path"]` collision, not lose to it.

    An `imageGeneration` call's own `rawInput` can already carry a `path` of its
    own; the completed frame's `savedPath` must replace it, not be shadowed by
    it once the two are merged into one dict. Synthetic -- no capture carries a
    colliding `path`. Shape from the adapter's `imageGenerationRawOutput`, the
    same source as the dialect-level fixture in `test_acp_dialects.py`.
    """
    collector = _TurnCollector(dialect=CodexDialect())

    image_open = {
        "sessionUpdate": "tool_call",
        "toolCallId": "img-1",
        "kind": "other",
        "status": "in_progress",
        "title": "Image generation",
        "rawInput": {"prompt": "a red bicycle", "path": "/tmp/codexprobe/ws/reference.png"},
    }
    image_done = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "img-1",
        "status": "completed",
        "rawOutput": {"revisedPrompt": "a red bicycle", "savedPath": "/w/bike.png"},
    }

    await collector("session/update", {"update": image_open})
    await collector("session/update", {"update": image_done})

    call = collector.calls[0]
    assert call.name == "imageGeneration"
    assert call.subject == "/w/bike.png"
    assert json.loads(call.arguments_json())["path"] == "/w/bike.png"
