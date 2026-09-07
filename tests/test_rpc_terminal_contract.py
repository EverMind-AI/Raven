"""Validate emitted terminal payloads against both public contract forms."""

import json
from pathlib import Path

import jsonschema
import pytest
from pydantic import TypeAdapter

from raven.contracts.terminal import TerminalRecord
from raven.rpc.models import TurnEvent


@pytest.mark.parametrize(
    "event,payload",
    [
        ("terminal.created", TerminalRecord(worktree_id="repo::/tmp", worktree_path="/tmp").model_dump(by_alias=True)),
        ("terminal.closed", {"handle": "term-test"}),
        ("terminal.status", {"handle": "term-test", "status": "idle", "liveness": "live"}),
        ("a2a.send", {"handle": "term-test", "state": "accepted", "nonce": None}),
        (
            "a2a.ack.matched",
            {"handle": "term-test", "nonce": None, "ack_for": "a2a-0123456789ab", "from": "worker-a", "to": "raven"},
        ),
    ],
)
def test_terminal_events_match_python_and_openrpc(event, payload):
    value = {"type": event, "payload": payload}
    assert TypeAdapter(TurnEvent).validate_python(value).type == event
    schema = json.loads((Path(__file__).parents[1] / "rpc-schema/openrpc.json").read_text())
    ref = schema["components"]["schemas"]["TurnEvent"]["discriminator"]["mapping"][event]
    jsonschema.Draft202012Validator({"$ref": ref, "components": schema["components"]}).validate(value)
