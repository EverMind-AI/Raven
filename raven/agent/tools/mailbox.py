"""Scoped native mailbox tools using the runner's existing session context."""

import json

from raven.agent.tools.terminal import _turn_session_key
from raven.contracts.mailbox import MailboxError
from raven.contracts.tool import Tool
from raven.security.trust import wrap_untrusted


class NativeMailboxTool(Tool):
    def __init__(self, name, description, method, properties, required, rpc):
        self._name = name
        self._description = description
        self.method = method
        self._parameters = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }
        self.rpc = rpc

    @property
    def name(self):
        return self._name

    @property
    def description(self):
        return self._description

    @property
    def parameters(self):
        return self._parameters

    async def execute(self, **kwargs):
        try:
            session_key = _turn_session_key.get()
            if session_key is None:
                raise MailboxError("receiver_unauthorized")
            return wrap_untrusted(json.dumps(await self.rpc(self.method, kwargs, session_key)), source="peer mailbox")
        except MailboxError as error:
            return "Error: " + json.dumps({"code": error.code, "retryable": error.retryable})


def native_mailbox_tools(rpc):
    """Expose only receiver operations; host enrollment and ownership commit stay absent."""
    string = {"type": "string"}
    object_value = {"type": "object"}
    rows = (
        (
            "mailbox_poll",
            "Peek or claim work only in this session's authorized task scope.",
            "mailbox.poll",
            {
                "request_id": string,
                "peek": {"type": "boolean"},
                "limit": {"type": "integer", "minimum": 1, "maximum": 16},
                "lease_seconds": {"type": "integer", "minimum": 10, "maximum": 3600},
            },
            [],
        ),
        (
            "mailbox_send",
            "Send an immutable envelope within this session's authorized task scope.",
            "mailbox.send",
            {"envelope": object_value},
            ["envelope"],
        ),
        (
            "mailbox_finish",
            "Finish or explicitly retry/reject a current mailbox claim; this does not accept a task.",
            "mailbox.ack",
            {
                "claim": object_value,
                "result": object_value,
                "action": {"type": "string", "enum": ["finish", "retry", "reject"]},
                "reason": string,
            },
            ["claim"],
        ),
        (
            "mailbox_renew",
            "Renew one current claim with a fixed request UUID.",
            "mailbox.renew",
            {
                "claim": object_value,
                "request_id": string,
                "lease_seconds": {"type": "integer", "minimum": 10, "maximum": 3600},
            },
            ["claim", "request_id"],
        ),
        (
            "mailbox_read_artifact",
            "Read and verify a declared handoff artifact and record actual full-byte coverage.",
            "mailbox.artifact.read",
            {"offer_message_id": string, "artifact_hash": string},
            ["offer_message_id", "artifact_hash"],
        ),
        (
            "mailbox_propose_handoff",
            "Validate an artifact-backed handoff proposal without transferring task ownership.",
            "mailbox.handoff.propose",
            {"offer_message_id": string, "accept_message_id": string},
            ["offer_message_id", "accept_message_id"],
        ),
    )
    return tuple(NativeMailboxTool(*row, rpc) for row in rows)
