"""Tests for the local ``raven mailbox`` CLI."""

import hashlib
import json
import os
import stat
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app
from raven.contracts.mailbox import MailboxError, MailboxInstanceRef
from raven.mailbox.codec import encode_envelope, seal_envelope
from raven.mailbox.delivery import MailboxDelivery
from raven.mailbox.store import MailboxStore

runner = CliRunner()


def _run(*args: object):
    return runner.invoke(app, ["mailbox", *(str(arg) for arg in args)])


def _payload(result) -> dict:
    payload = json.loads(result.stdout)
    assert set(payload) == {"ok", "result", "error"}
    if payload["error"] is not None:
        assert set(payload["error"]) == {"code", "retryable", "message_id"}
    return payload


def test_receiver_rpc_rejects_public_or_unprotected_credentials(tmp_path):
    credential = tmp_path / "receiver.json"
    credential.write_text(json.dumps({"url": "http://example.com/rpc", "token": "s" * 43}))
    credential.chmod(0o600)
    result = _run("rpc", "--credential-file", credential, "--method", "mailbox.poll", "--json")
    assert _payload(result)["error"]["code"] == "invalid_credential_file"
    assert "s" * 43 not in result.output
    credential.write_text(json.dumps({"url": "http://127.0.0.1:18792/rpc", "token": "s" * 43}))
    credential.chmod(0o644)
    result = _run("rpc", "--credential-file", credential, "--method", "mailbox.poll", "--json")
    assert _payload(result)["error"]["code"] == "invalid_credential_file"


def test_receiver_rpc_cli_cannot_call_admin_or_legacy_methods(tmp_path):
    credential = tmp_path / "receiver.json"
    credential.write_text(json.dumps({"url": "http://127.0.0.1:18792/rpc", "token": "s" * 43}))
    credential.chmod(0o600)
    result = _run("rpc", "--credential-file", credential, "--method", "agents.register", "--json")
    assert _payload(result)["error"]["code"] == "receiver_method_forbidden"


def _init(root: Path, agent_id: str, instance_out: Path, request_id: str | None = None) -> MailboxInstanceRef:
    result = _run(
        "init",
        "--root",
        root,
        "--json",
        "--agent-id",
        agent_id,
        "--request-id",
        request_id or str(uuid4()),
        "--task-id",
        "task-1",
        "--workspace-id",
        "workspace-1",
        "--kind",
        "task.request",
        "--kind",
        "receipt",
        "--instance-out",
        instance_out,
    )
    assert result.exit_code == 0, result.output
    assert _payload(result)["ok"] is True
    return MailboxInstanceRef.model_validate(_payload(result)["result"])


def _wire(
    sender: MailboxInstanceRef,
    target: MailboxInstanceRef,
    *,
    receipt_policy: str = "terminal",
    created_at: datetime | None = None,
    ttl: int = 3600,
    artifacts: list[dict] | None = None,
) -> bytes:
    created_at = (created_at or datetime.now(timezone.utc)).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
    return encode_envelope(
        seal_envelope(
            {
                "protocol_version": "1.0",
                "message_id": str(uuid4()),
                "trace_id": str(uuid4()),
                "in_reply_to": None,
                "kind": "task.request",
                "sender_identity": {
                    "authority_id": sender.authority_id,
                    "tenant_id": sender.tenant_id,
                    "agent_id": sender.agent_id,
                    "instance_id": sender.instance_id,
                },
                "target_identity": {
                    "authority_id": target.authority_id,
                    "tenant_id": target.tenant_id,
                    "agent_id": target.agent_id,
                    "instance_id": None,
                },
                "scope": {"task_id": "task-1", "workspace_id": "workspace-1"},
                "created_at": created_at,
                "ttl": ttl,
                "receipt_policy": receipt_policy,
                "payload": {
                    "content_type": "application/json",
                    "schema": "opena2a.task/1",
                    "data": {
                        "operation": "review",
                        "arguments": {},
                        "idempotency_key": "request-1",
                        "acceptance": [],
                    },
                },
                "artifacts": artifacts or [],
                "extensions": {},
            }
        )
    )


def test_mailbox_group_is_registered_without_changing_terminal() -> None:
    mailbox = runner.invoke(app, ["mailbox", "--help"])
    terminal = runner.invoke(app, ["terminal", "--help"])

    assert mailbox.exit_code == 0
    for command in ("init", "resume", "send", "poll", "ack", "renew", "status", "recover", "flush-receipts", "gc"):
        assert command in mailbox.stdout
    assert terminal.exit_code == 0
    assert "terminal" in terminal.stdout.lower()


def test_group_level_json_usage_error_is_structured() -> None:
    result = runner.invoke(app, ["mailbox", "--json"])

    assert result.exit_code == 2
    assert _payload(result)["error"]["code"] == "invalid_argument"


def test_malformed_equals_json_usage_error_is_structured() -> None:
    result = runner.invoke(app, ["mailbox", "send", "--json=foo"])

    assert result.exit_code == 2
    assert _payload(result)["error"]["code"] == "invalid_argument"


def test_offline_send_peek_ack_and_receipt_flow(tmp_path: Path) -> None:
    root = tmp_path / "mailbox"
    sender_path = tmp_path / "sender.json"
    target_path = tmp_path / "target.json"
    sender = _init(root, str(uuid4()), sender_path)
    target = _init(root, str(uuid4()), target_path)

    envelope_path = tmp_path / "envelope.json"
    original = _wire(sender, target)
    envelope_path.write_bytes(original)
    send_args = (
        "send",
        "--root",
        root,
        "--json",
        "--instance-file",
        sender_path,
        "--file",
        envelope_path,
    )
    sent = _run(*send_args)
    replay = _run(*send_args)
    assert sent.exit_code == replay.exit_code == 0
    sent_result = _payload(sent)["result"]
    assert sent_result["phase"] == "pending"
    assert _payload(replay)["result"]["duplicate"] is True
    assert envelope_path.read_bytes() == original

    inspected = _run(
        "poll",
        "--root",
        root,
        "--json",
        "--instance-file",
        target_path,
        "--peek",
    )
    assert inspected.exit_code == 0
    assert "claim_token" not in json.dumps(_payload(inspected)["result"])
    assert _payload(inspected)["result"][0]["attempt"] == 0
    store = MailboxStore(root)
    assert store.status(target.agent_id, sent_result["message_id"])["phase"] == "pending"

    status = _run(
        "status",
        "--root",
        root,
        "--json",
        "--agent-id",
        target.agent_id,
        "--message-id",
        sent_result["message_id"],
    )
    assert status.exit_code == 0
    assert _payload(status)["result"]["phase"] == "pending"

    claim_path = tmp_path / "target-claim.json"
    polled = _run(
        "poll",
        "--root",
        root,
        "--json",
        "--instance-file",
        target_path,
        "--request-id",
        str(uuid4()),
        "--claim-out",
        claim_path,
    )
    assert polled.exit_code == 0
    claim = _payload(polled)["result"][0]
    assert claim["claim_token"]
    assert stat.S_IMODE(claim_path.stat().st_mode) == 0o600

    ack = _run(
        "ack",
        "--root",
        root,
        "--json",
        "--instance-file",
        target_path,
        "--claim-file",
        claim_path,
        "--outcome",
        "succeeded",
        "--summary",
        "Done",
    )
    assert ack.exit_code == 0
    assert _payload(ack)["result"]["phase"] == "completed"

    flushed = _run("flush-receipts", "--root", root, "--json", "--instance-file", target_path)
    assert flushed.exit_code == 0
    assert _payload(flushed)["result"]["published"] == 1

    receipt_claim_path = tmp_path / "receipt-claim.json"
    receipt_poll = _run(
        "poll",
        "--root",
        root,
        "--json",
        "--instance-file",
        sender_path,
        "--request-id",
        str(uuid4()),
        "--claim-out",
        receipt_claim_path,
    )
    assert receipt_poll.exit_code == 0
    assert len(_payload(receipt_poll)["result"]) == 1
    result_path = tmp_path / "receipt-result.json"
    result_path.write_text(json.dumps({"outcome": "succeeded", "summary": "", "evidence": []}), encoding="utf-8")
    receipt_ack = _run(
        "ack",
        "--root",
        root,
        "--json",
        "--instance-file",
        sender_path,
        "--claim-file",
        receipt_claim_path,
        "--result-file",
        result_path,
    )
    assert receipt_ack.exit_code == 0
    assert _payload(receipt_ack)["result"]["phase"] == "completed"

    assert _run("recover", "--root", root, "--json").exit_code == 0
    assert _run("gc", "--root", root, "--json").exit_code == 0


def test_poll_output_failure_can_replay_fixed_request(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "mailbox"
    sender_path = tmp_path / "sender.json"
    target_path = tmp_path / "target.json"
    sender = _init(root, str(uuid4()), sender_path)
    target = _init(root, str(uuid4()), target_path)
    envelope_path = tmp_path / "envelope.json"
    envelope_path.write_bytes(_wire(sender, target, receipt_policy="none"))
    assert (
        _run("send", "--root", root, "--json", "--instance-file", sender_path, "--file", envelope_path).exit_code == 0
    )

    request_id = str(uuid4())
    claim_path = tmp_path / "claim.json"
    real_link = os.link

    def fail_claim_publication(source, destination, *args, **kwargs):
        if Path(destination) == claim_path:
            raise PermissionError("claim output unavailable")
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr("raven.cli.mailbox_commands.os.link", fail_claim_publication)
    failed = _run(
        "poll",
        "--root",
        root,
        "--json",
        "--instance-file",
        target_path,
        "--request-id",
        request_id,
        "--claim-out",
        claim_path,
    )
    assert failed.exit_code == 5
    error = _payload(failed)["error"]
    assert error["code"] == "output_error"
    assert error["retryable"] is True
    assert not claim_path.exists()

    monkeypatch.setattr("raven.cli.mailbox_commands.os.link", real_link)
    replay = _run(
        "poll",
        "--root",
        root,
        "--json",
        "--instance-file",
        target_path,
        "--request-id",
        request_id,
        "--claim-out",
        claim_path,
    )
    assert replay.exit_code == 0
    assert _payload(replay)["result"][0]["claim_token"]
    assert stat.S_IMODE(claim_path.stat().st_mode) == 0o600


def test_renew_replays_and_ack_retry_keeps_the_claim_transition_explicit(tmp_path: Path) -> None:
    root = tmp_path / "mailbox"
    sender_path = tmp_path / "sender.json"
    target_path = tmp_path / "target.json"
    sender = _init(root, str(uuid4()), sender_path)
    target = _init(root, str(uuid4()), target_path)
    envelope_path = tmp_path / "envelope.json"
    envelope_path.write_bytes(_wire(sender, target, receipt_policy="none"))
    sent = _run("send", "--root", root, "--json", "--instance-file", sender_path, "--file", envelope_path)
    assert sent.exit_code == 0

    original_claim_path = tmp_path / "original-claim.json"
    polled = _run(
        "poll",
        "--root",
        root,
        "--json",
        "--instance-file",
        target_path,
        "--request-id",
        str(uuid4()),
        "--claim-out",
        original_claim_path,
    )
    assert polled.exit_code == 0
    request_id = str(uuid4())
    renewed_path = tmp_path / "renewed-claim.json"
    renew_args = (
        "renew",
        "--root",
        root,
        "--json",
        "--instance-file",
        target_path,
        "--claim-file",
        original_claim_path,
        "--request-id",
        request_id,
        "--claim-out",
        renewed_path,
    )
    renewed = _run(*renew_args)
    replay = _run(*renew_args)
    assert renewed.exit_code == replay.exit_code == 0
    assert _payload(renewed)["result"]["claim_token"] == _payload(replay)["result"]["claim_token"]
    published = renewed_path.read_bytes()
    assert json.loads(published) == _payload(renewed)["result"]
    assert renewed_path.read_bytes() == published

    invalid = _run(
        "ack",
        "--root",
        root,
        "--json",
        "--instance-file",
        target_path,
        "--claim-file",
        renewed_path,
        "--retry",
        "--reject",
        "--reason",
        "not-a-transition",
    )
    assert invalid.exit_code == 2
    assert _payload(invalid)["error"]["code"] == "invalid_argument"

    retried = _run(
        "ack",
        "--root",
        root,
        "--json",
        "--instance-file",
        target_path,
        "--claim-file",
        renewed_path,
        "--retry",
        "--reason",
        "temporarily_busy",
    )
    assert retried.exit_code == 0
    assert _payload(retried)["result"]["phase"] == "pending"


def test_resume_replays_the_same_reference_and_instance_output_is_no_overwrite(tmp_path: Path) -> None:
    root = tmp_path / "mailbox"
    instance_path = tmp_path / "instance.json"
    agent_id = str(uuid4())
    original = _init(root, agent_id, instance_path)
    request_id = str(uuid4())
    output_path = tmp_path / "resumed.json"

    first = _run(
        "resume",
        "--root",
        root,
        "--json",
        "--agent-id",
        agent_id,
        "--request-id",
        request_id,
        "--instance-out",
        output_path,
    )
    assert first.exit_code == 0
    resumed = MailboxInstanceRef.model_validate(_payload(first)["result"])
    assert resumed.generation == original.generation + 1
    published = output_path.read_bytes()
    replay = _run(
        "resume",
        "--root",
        root,
        "--json",
        "--agent-id",
        agent_id,
        "--request-id",
        request_id,
        "--instance-out",
        output_path,
    )
    assert replay.exit_code == 0
    assert output_path.read_bytes() == published
    assert _payload(replay)["result"] == _payload(first)["result"]
    assert stat.S_IMODE(output_path.stat().st_mode) == 0o600

    conflict = tmp_path / "conflict.json"
    conflict.write_bytes(b"keep")
    conflict.chmod(0o600)
    failed = _run(
        "resume",
        "--root",
        root,
        "--json",
        "--agent-id",
        agent_id,
        "--request-id",
        request_id,
        "--instance-out",
        conflict,
    )
    assert failed.exit_code == 3
    assert _payload(failed)["error"]["code"] == "file_conflict"
    assert conflict.read_bytes() == b"keep"

    resumed_again = _run(
        "resume",
        "--root",
        root,
        "--json",
        "--instance-file",
        instance_path,
        "--request-id",
        str(uuid4()),
        "--instance-out",
        tmp_path / "resumed-again.json",
    )
    assert resumed_again.exit_code == 0
    assert MailboxInstanceRef.model_validate(_payload(resumed_again)["result"]).generation == 3


def test_init_fixed_request_replays_the_store_generated_instance(tmp_path: Path) -> None:
    root = tmp_path / "mailbox"
    instance_path = tmp_path / "instance.json"
    request_id = str(uuid4())
    args = (
        "init",
        "--root",
        root,
        "--json",
        "--agent-id",
        str(uuid4()),
        "--request-id",
        request_id,
        "--task-id",
        "task-1",
        "--workspace-id",
        "workspace-1",
        "--kind",
        "task.request",
        "--instance-out",
        instance_path,
    )
    first = _run(*args)
    published = instance_path.read_bytes()
    replay = _run(*args)

    assert first.exit_code == replay.exit_code == 0
    assert _payload(replay)["result"] == _payload(first)["result"]
    assert instance_path.read_bytes() == published
    assert stat.S_IMODE(instance_path.stat().st_mode) == 0o600


def test_ack_reject_uses_the_core_terminal_transition(tmp_path: Path) -> None:
    root = tmp_path / "mailbox"
    sender_path = tmp_path / "sender.json"
    target_path = tmp_path / "target.json"
    sender = _init(root, str(uuid4()), sender_path)
    _init(root, str(uuid4()), target_path)
    envelope_path = tmp_path / "envelope.json"
    envelope_path.write_bytes(
        _wire(sender, MailboxInstanceRef.model_validate(json.loads(target_path.read_text())), receipt_policy="none")
    )
    assert (
        _run("send", "--root", root, "--json", "--instance-file", sender_path, "--file", envelope_path).exit_code == 0
    )

    claim_path = tmp_path / "claim.json"
    polled = _run(
        "poll",
        "--root",
        root,
        "--json",
        "--instance-file",
        target_path,
        "--request-id",
        str(uuid4()),
        "--claim-out",
        claim_path,
    )
    assert polled.exit_code == 0
    rejected = _run(
        "ack",
        "--root",
        root,
        "--json",
        "--instance-file",
        target_path,
        "--claim-file",
        claim_path,
        "--reject",
        "--reason",
        "scope_rejected",
    )
    assert rejected.exit_code == 0
    assert _payload(rejected)["result"]["phase"] == "dead_letter"
    assert _payload(rejected)["result"]["terminal_reason"] == "scope_rejected"


def test_send_passes_explicit_artifact_mapping_without_changing_the_envelope(tmp_path: Path) -> None:
    root = tmp_path / "mailbox"
    sender_path = tmp_path / "sender.json"
    target = _init(root, str(uuid4()), tmp_path / "target.json")
    sender = _init(root, str(uuid4()), sender_path)
    artifact_path = tmp_path / "artifact.txt"
    artifact_bytes = b"review attachment"
    artifact_path.write_bytes(artifact_bytes)
    digest = hashlib.sha256(artifact_bytes).hexdigest()
    envelope_path = tmp_path / "envelope.json"
    raw = _wire(
        sender,
        target,
        receipt_policy="none",
        artifacts=[{"sha256": digest, "size": len(artifact_bytes), "media_type": "text/plain", "name": "input.txt"}],
    )
    envelope_path.write_bytes(raw)

    sent = _run(
        "send",
        "--root",
        root,
        "--json",
        "--instance-file",
        sender_path,
        "--file",
        envelope_path,
        "--artifact",
        f"{digest}={artifact_path}",
    )

    assert sent.exit_code == 0
    assert _payload(sent)["result"]["phase"] == "pending"
    assert envelope_path.read_bytes() == raw
    assert artifact_path.read_bytes() == artifact_bytes


def test_send_rejects_symlink_artifact_source_with_invalid_source_exit_code(tmp_path: Path) -> None:
    root = tmp_path / "mailbox"
    sender_path = tmp_path / "sender.json"
    sender = _init(root, str(uuid4()), sender_path)
    target = _init(root, str(uuid4()), tmp_path / "target.json")
    artifact_path = tmp_path / "artifact.txt"
    artifact_bytes = b"review attachment"
    artifact_path.write_bytes(artifact_bytes)
    linked_artifact = tmp_path / "linked-artifact.txt"
    linked_artifact.symlink_to(artifact_path)
    digest = hashlib.sha256(artifact_bytes).hexdigest()
    envelope_path = tmp_path / "envelope.json"
    envelope_path.write_bytes(
        _wire(
            sender,
            target,
            receipt_policy="none",
            artifacts=[
                {"sha256": digest, "size": len(artifact_bytes), "media_type": "text/plain", "name": "input.txt"}
            ],
        )
    )

    rejected = _run(
        "send",
        "--root",
        root,
        "--json",
        "--instance-file",
        sender_path,
        "--file",
        envelope_path,
        "--artifact",
        f"{digest}={linked_artifact}",
    )

    assert rejected.exit_code == 2
    assert _payload(rejected)["error"]["code"] == "unsafe_artifact_path"
    assert artifact_path.read_bytes() == artifact_bytes


def test_init_rejects_fifo_instance_output_without_blocking(tmp_path: Path) -> None:
    root = tmp_path / "mailbox"
    instance_path = tmp_path / "instance.fifo"
    os.mkfifo(instance_path, 0o600)
    command = [
        sys.executable,
        "-m",
        "raven",
        "mailbox",
        "init",
        "--root",
        str(root),
        "--json",
        "--agent-id",
        str(uuid4()),
        "--request-id",
        str(uuid4()),
        "--task-id",
        "task-1",
        "--workspace-id",
        "workspace-1",
        "--kind",
        "task.request",
        "--instance-out",
        str(instance_path),
    ]
    try:
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
    except subprocess.TimeoutExpired:
        pytest.fail("CLI blocked while checking an existing FIFO credential output")

    assert result.returncode == 3, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["error"]["code"] == "file_conflict"


def test_init_requires_explicit_paired_scopes(tmp_path: Path) -> None:
    root = tmp_path / "mailbox"
    result = _run(
        "init",
        "--root",
        root,
        "--json",
        "--agent-id",
        str(uuid4()),
        "--request-id",
        str(uuid4()),
        "--task-id",
        "task-1",
        "--kind",
        "task.request",
        "--instance-out",
        tmp_path / "instance.json",
    )

    assert result.exit_code == 4
    assert _payload(result)["error"]["code"] == "invalid_policy"
    assert not root.exists()


def test_cli_error_exit_codes_cover_expired_quota_stale_and_unknown(tmp_path: Path) -> None:
    root = tmp_path / "mailbox"
    sender_path = tmp_path / "sender.json"
    target_path = tmp_path / "target.json"
    sender = _init(root, str(uuid4()), sender_path)
    target = _init(root, str(uuid4()), target_path)

    expired_path = tmp_path / "expired.json"
    expired_path.write_bytes(_wire(sender, target, created_at=datetime.now(timezone.utc) - timedelta(minutes=2), ttl=1))
    expired = _run("send", "--root", root, "--json", "--instance-file", sender_path, "--file", expired_path)
    assert expired.exit_code == 7
    assert _payload(expired)["error"]["code"] == "message_expired"

    envelope_path = tmp_path / "envelope.json"
    envelope_path.write_bytes(_wire(sender, target, receipt_policy="none"))
    store = MailboxStore(root)
    store.configure_quotas(record_limit=0)
    quota = _run("send", "--root", root, "--json", "--instance-file", sender_path, "--file", envelope_path)
    assert quota.exit_code == 6
    assert _payload(quota)["error"]["code"] == "quota_exceeded"

    unknown = _run(
        "status",
        "--root",
        root,
        "--json",
        "--agent-id",
        target.agent_id,
        "--message-id",
        str(uuid4()),
    )
    assert unknown.exit_code == 4
    assert _payload(unknown)["error"]["code"] == "message_not_found"

    store.configure_quotas(record_limit=10)
    assert (
        _run("send", "--root", root, "--json", "--instance-file", sender_path, "--file", envelope_path).exit_code == 0
    )
    claim_path = tmp_path / "claim.json"
    polled = _run(
        "poll",
        "--root",
        root,
        "--json",
        "--instance-file",
        target_path,
        "--request-id",
        str(uuid4()),
        "--lease-seconds",
        10,
        "--claim-out",
        claim_path,
    )
    assert polled.exit_code == 0
    MailboxDelivery(store).recover(now=int(time.time()) + 12)
    stale = _run(
        "ack",
        "--root",
        root,
        "--json",
        "--instance-file",
        target_path,
        "--claim-file",
        claim_path,
        "--outcome",
        "succeeded",
    )
    assert stale.exit_code == 8
    assert _payload(stale)["error"]["code"] == "stale_claim"
    assert _payload(stale)["error"]["message_id"] == _payload(polled)["result"][0]["envelope"]["message_id"]


def test_json_argument_and_credential_errors_are_structured_and_redacted(tmp_path: Path) -> None:
    root = tmp_path / "mailbox"
    missing = _run("init", "--root", root, "--json")
    assert missing.exit_code == 2
    assert _payload(missing)["ok"] is False
    assert _payload(missing)["error"]["code"] == "invalid_argument"
    assert not root.exists()

    instance_path = tmp_path / "instance.json"
    sender = _init(root, str(uuid4()), instance_path)
    malformed_path = tmp_path / "malformed-envelope.json"
    malformed_path.write_text('{"claim_token":"do-not-print"}', encoding="utf-8")
    malformed = _run("send", "--root", root, "--json", "--instance-file", instance_path, "--file", malformed_path)
    assert malformed.exit_code == 2
    assert _payload(malformed)["ok"] is False
    assert "do-not-print" not in malformed.stdout

    instance_path.chmod(0o644)
    invalid_permissions = _run(
        "send", "--root", root, "--json", "--instance-file", instance_path, "--file", malformed_path
    )
    assert invalid_permissions.exit_code == 2
    assert _payload(invalid_permissions)["error"]["code"] == "invalid_credential_file"
    assert MailboxStore(root).card(sender.agent_id).agent_id == sender.agent_id

    instance_path.chmod(0o600)
    linked_instance = tmp_path / "linked-instance.json"
    linked_instance.symlink_to(instance_path)
    symlink = _run("send", "--root", root, "--json", "--instance-file", linked_instance, "--file", malformed_path)
    assert symlink.exit_code == 2
    assert _payload(symlink)["error"]["code"] == "invalid_credential_file"

    oversized_path = tmp_path / "oversized-envelope.json"
    oversized = b"{" + b" " * 65536
    oversized_path.write_bytes(oversized)
    rejected_size = _run("send", "--root", root, "--json", "--instance-file", instance_path, "--file", oversized_path)
    assert rejected_size.exit_code == 2
    assert _payload(rejected_size)["error"]["code"] == "envelope_too_large"
    assert oversized_path.read_bytes() == oversized


def test_help_is_plain_and_does_not_initialize_the_mailbox(tmp_path: Path) -> None:
    root = tmp_path / "mailbox"
    result = _run("init", "--root", root, "--help")

    assert result.exit_code == 0
    assert "--agent-id" in result.stdout
    assert not root.exists()


@pytest.mark.parametrize(
    "code,retryable,exit_code",
    [
        ("storage_busy", True, 5),
        ("storage_permission", False, 5),
        ("storage_full", False, 5),
        ("storage_error", False, 5),
        ("commit_unknown", True, 9),
    ],
)
def test_storage_fault_json_mapping(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code, retryable, exit_code):
    def fail_status(self, agent_id, message_id):
        raise MailboxError(code, retryable=retryable, message_id=message_id)

    monkeypatch.setattr(MailboxStore, "status", fail_status)
    message_id = str(uuid4())
    result = _run(
        "status",
        "--root",
        tmp_path / "mailbox",
        "--json",
        "--agent-id",
        str(uuid4()),
        "--message-id",
        message_id,
    )
    assert result.exit_code == exit_code
    payload = _payload(result)
    assert payload == {
        "ok": False,
        "result": None,
        "error": {"code": code, "retryable": retryable, "message_id": message_id},
    }
    assert json.loads(result.output) == payload
    assert "claim_token" not in result.output
