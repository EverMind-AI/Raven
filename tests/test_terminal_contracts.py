"""Terminal wire shapes and the peer envelope grammar shared with A-O."""

from uuid import UUID

import pytest
from pydantic import ValidationError

from raven.contracts.terminal import (
    Envelope,
    RuntimeInfo,
    SendResult,
    TerminalListResult,
    TerminalRecord,
    WaitResult,
    error_envelope,
    success_envelope,
    worktree_id,
)


def record():
    return TerminalRecord(worktree_id="repo::/tmp/work", worktree_path="/tmp/work", owner="human")


def test_terminal_wire_identity_and_visibility_are_independent():
    terminal = record()
    wire = terminal.model_dump(by_alias=True, mode="json")
    assert wire["handle"].startswith("term_")
    assert UUID(wire["incarnationId"]).version == 4
    assert wire["ptyId"].startswith("repo::/tmp/work@@")
    assert wire["paneKey"] == wire["tabId"] + ":" + wire["leafId"]
    terminal.connected = True
    terminal.visible = False
    assert TerminalRecord.model_validate(terminal.model_dump(by_alias=True)) == terminal
    assert record().incarnation_id != terminal.incarnation_id


def test_envelope_matches_the_ao_header_and_preserves_body():
    text = "[AO-A2A:v1] from=worker-a to=worker-b scope=headless/development terminal=term_abc nonce=a2a-012345abcdef ack_for=a2a-fedcba543210 message:\nfirst\nsecond"
    envelope = Envelope.from_text(text)
    assert envelope.sender == "worker-a"
    assert envelope.ack_for == "a2a-fedcba543210"
    assert envelope.to_text() == text
    assert Envelope.from_text(envelope.to_text()) == envelope


@pytest.mark.parametrize(
    "text",
    [
        "hello",
        "[AO-A2A:v1] from=a to=b scope=x/y nonce=a2a-012345abcdef message:\n",
        "[AO-A2A:v1] from=A to=b scope=x/y nonce=a2a-012345abcdef message:\nx",
    ],
)
def test_envelope_rejects_non_envelopes(text):
    with pytest.raises(ValueError):
        Envelope.from_text(text)


def test_runtime_and_cli_envelopes_share_one_process_identity():
    runtime = RuntimeInfo()
    assert RuntimeInfo().runtime_id == runtime.runtime_id
    wire = success_envelope("request", {"runtime": runtime.model_dump(by_alias=True)}, runtime)
    assert wire["result"]["runtime"]["runtimeId"] == wire["_meta"]["runtimeId"]
    failed = error_envelope("request", "agent_prompt_blocked", "Permission required", runtime=runtime)
    assert failed["ok"] is False
    assert failed["error"]["code"] == "agent_prompt_blocked"


def test_result_shapes_use_the_shim_aliases():
    wire = TerminalListResult(terminals=[record()]).model_dump(by_alias=True)
    assert wire["truncated"] is False
    assert wire["hostScope"]["hostIds"] == ["local"]
    assert "topologyRevisions" in wire
    assert (
        SendResult(handle="term_abc", accepted=True, state="accepted", bytes_written=9).model_dump(by_alias=True)[
            "bytesWritten"
        ]
        == 9
    )
    assert (
        WaitResult(handle="term_abc", satisfied=False, blocked_reason="permission").model_dump(by_alias=True)[
            "blockedReason"
        ]
        == "permission"
    )


def test_worktree_id_reuses_an_explicit_orca_repo_id(tmp_path):
    assert worktree_id(tmp_path, repo_id="orca-repo") == f"orca-repo::{tmp_path.resolve()}"


def test_invalid_terminal_status_is_rejected():
    with pytest.raises(ValidationError):
        TerminalRecord(worktree_id="repo::/tmp/work", worktree_path="/tmp/work", status="dead")
