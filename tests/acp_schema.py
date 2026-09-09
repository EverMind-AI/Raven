"""Validate raven's outbound ACP frames against the vendored official schema.

Not a test module (pytest collects ``test_*``): this is the judge the ACP unit
tests run their frames through, so a mapping that drifts from the spec fails in
the test that produced the frame rather than in a reviewer's reading.

The fixture under ``tests/fixtures/acp/`` is the schema published by
``agentclientprotocol/agent-client-protocol``, vendored rather than fetched: a
test that reaches the network is not a test, and a schema that moves under us
turns an unrelated change red. ``VERSION.json`` pins which release it is and the
sha256 of each file, so upgrading the spec is a deliberate edit with a visible
diff instead of a silent drift (``test_acp_schema.py`` asserts the pair).

Direction, in the schema's own words: ``Agent*`` unions are what the agent
*sends*. Raven is the agent here, so every frame it writes must validate against
the top-level ``Agent`` branch -- ``AgentResponse`` for a reply, ``AgentRequest``
for something it asks the client to do, ``AgentNotification`` for
``session/update``. The mirrored ``Client`` branch is what the stub client sends,
and :func:`validate_inbound` exists so the stub's own frames are held to the
same standard rather than being trusted because we wrote them.

**What this cannot catch, measured, not assumed.** The schema sets
``additionalProperties`` 118 times and every one of them is ``true``; not one is
``false``. So an extra or misspelled key validates -- which is exactly the
characteristic bug of a hand-written mapper. What it does catch is a missing
required field, a value of the wrong type, and a value outside a closed enum
(``sessionUpdate``, ``stopReason``, ``ToolKind``, ``ToolCallStatus``). The
union-level checks are looser still: ``method`` is declared ``type: string`` with
no enum, and ``params`` is ``anyOf[..., null]``, so a frame with an invented
method name and no params satisfies ``AgentNotification``. Method names are
therefore checked separately, against ``meta-v1.json``, by
:func:`agent_method_names` -- and payloads are checked against their own
``$defs`` entry by :func:`validate_def`, which is where the discriminating power
actually lives. Prefer it over the frame-level helpers when a test knows what it
built.
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

FIXTURE_DIR = Path(__file__).parent / "fixtures" / "acp"
SCHEMA_PATH = FIXTURE_DIR / "schema-v1.json"
META_PATH = FIXTURE_DIR / "meta-v1.json"
VERSION_PATH = FIXTURE_DIR / "VERSION.json"


class AcpSchemaError(AssertionError):
    """An outbound frame does not match the official schema.

    An ``AssertionError`` subclass so a failure reads as the test failing rather
    than as the validator erroring: the frame is the thing under test.
    """


@lru_cache(maxsize=1)
def schema() -> dict[str, Any]:
    """The vendored schema document, parsed once per process."""
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def meta() -> dict[str, Any]:
    """The vendored method-name manifest (``meta-v1.json``)."""
    return json.loads(META_PATH.read_text(encoding="utf-8"))


@lru_cache(maxsize=1)
def version_stamp() -> dict[str, Any]:
    """The pinned release and per-file digests (``VERSION.json``)."""
    return json.loads(VERSION_PATH.read_text(encoding="utf-8"))


def sha256_of(name: str) -> str:
    """Hex digest of one fixture file, read as bytes.

    Bytes rather than re-serialised JSON: the digest has to answer "is this the
    file that was vendored", and a round-trip through ``json.dumps`` would
    change key order and whitespace while claiming the file was untouched.
    """
    return hashlib.sha256((FIXTURE_DIR / name).read_bytes()).hexdigest()


@lru_cache(maxsize=1)
def agent_method_names() -> frozenset[str]:
    """Methods a *client* may call on the agent, per the manifest.

    This is the set raven's dispatcher is allowed to answer with anything other
    than method-not-found. It is read from the manifest rather than typed out
    here so the 18 unstable methods, which the manifest omits, cannot be
    registered by accident and then blessed by a test that lists them too.
    """
    return frozenset(str(v) for v in meta().get("agentMethods", {}).values())


@lru_cache(maxsize=1)
def client_method_names() -> frozenset[str]:
    """Methods the agent may call on the client, per the manifest."""
    return frozenset(str(v) for v in meta().get("clientMethods", {}).values())


@lru_cache(maxsize=None)
def _validator(pointer: str) -> Draft202012Validator:
    """A validator for one JSON pointer into the vendored document.

    Built by wrapping the target in a document that carries the whole ``$defs``
    table, which works because all 168 ``$ref``s in the schema are local
    ``#/$defs/...`` pointers -- verified, and asserted by
    ``test_acp_schema.py`` so a future schema with an external ref fails here
    loudly instead of resolving to nothing.
    """
    root = schema()
    Draft202012Validator.check_schema(root)
    subject: dict[str, Any] = {
        "$schema": root.get("$schema", "https://json-schema.org/draft/2020-12/schema"),
        "$defs": root["$defs"],
        "$ref": pointer,
    }
    return Draft202012Validator(subject)


@lru_cache(maxsize=None)
def _direction_validator(title: str) -> Draft202012Validator:
    """A validator for one top-level branch (``Agent`` / ``Client``).

    Selected by ``title`` rather than by index so a reordering of the schema's
    ``anyOf`` cannot silently swap the two directions -- which would make every
    outbound assertion check the wrong half of the protocol and still pass.
    """
    root = schema()
    for branch in root["anyOf"]:
        if branch.get("title") == title:
            subject = {
                "$schema": root.get("$schema", "https://json-schema.org/draft/2020-12/schema"),
                "$defs": root["$defs"],
                **{k: v for k, v in branch.items() if k not in ("title", "description")},
            }
            return Draft202012Validator(subject)
    raise AcpSchemaError(f"the vendored schema has no top-level branch titled {title!r}")


def _explain(errors: list[Any], subject: str, payload: Any) -> str:
    """Render validation errors with enough context to act on.

    The path is included because a failure three levels inside a content block
    reads as "the whole notification is wrong" without it, and the payload is
    truncated because a 3 MB pasted image would otherwise bury the message it
    came with.
    """
    lines = [f"{subject} does not match the official ACP schema:"]
    for err in errors:
        where = "/".join(str(p) for p in err.absolute_path) or "<root>"
        lines.append(f"  at {where}: {err.message}")
    rendered = json.dumps(payload, ensure_ascii=False, default=repr)
    lines.append(f"  payload: {rendered[:800]}")
    return "\n".join(lines)


def _check(validator: Draft202012Validator, payload: Any, subject: str) -> None:
    errors = sorted(validator.iter_errors(payload), key=lambda e: list(e.absolute_path))
    if errors:
        raise AcpSchemaError(_explain(errors, subject, payload))


def validate_def(name: str, payload: Any) -> None:
    """Validate ``payload`` against ``#/$defs/<name>``, raising on mismatch.

    The precise check, and the one worth reaching for: it holds a
    ``SessionNotification`` to the ``SessionUpdate`` union, a
    ``RequestPermissionRequest`` to its required ``toolCall``, and a
    ``StopReason`` to the five values that exist.
    """
    if name not in schema()["$defs"]:
        raise AcpSchemaError(f"the vendored schema has no definition named {name!r}")
    _check(_validator(f"#/$defs/{name}"), payload, name)


def validate_outbound(frame: Any) -> None:
    """Validate a frame raven writes to stdout (the ``Agent`` direction)."""
    _check(_direction_validator("Agent"), frame, "outbound frame")


def validate_inbound(frame: Any) -> None:
    """Validate a frame a client writes to raven (the ``Client`` direction).

    Used by the stub client's own tests: a stub that sends an illegal frame
    would prove raven tolerant of something no real client can produce.
    """
    _check(_direction_validator("Client"), frame, "inbound frame")


def is_valid_def(name: str, payload: Any) -> bool:
    """Whether ``payload`` matches ``#/$defs/<name>``.

    For the negative half of a discrimination test, where the point is that the
    validator says no.
    """
    try:
        validate_def(name, payload)
    except AcpSchemaError:
        return False
    return True


__all__ = [
    "FIXTURE_DIR",
    "META_PATH",
    "SCHEMA_PATH",
    "VERSION_PATH",
    "AcpSchemaError",
    "agent_method_names",
    "client_method_names",
    "is_valid_def",
    "meta",
    "schema",
    "sha256_of",
    "validate_def",
    "validate_inbound",
    "validate_outbound",
    "version_stamp",
]
