"""A minimal ACP server, run as a subprocess by ``tests/test_subagent_acp.py``.

Not a test module (pytest collects ``test_*``): this is the executable under
test's other end. It is a real child process talking real newline-delimited
JSON-RPC over stdio, so the tests exercise launch, the read loop, request
correlation and teardown rather than a mocked client.

Behaviour is chosen by ``ACP_STUB_MODE``:

- ``ok``           - full handshake, one text chunk plus a tool call, then end_turn.
- ``reject_init``  - answers ``initialize`` with a JSON-RPC error.
- ``no_session``   - handshake fine, ``session/new`` errors (an auth-shaped message).
- ``empty_turn``   - handshake and session fine, but the prompt produces no content
                     and still reports ``stopReason: end_turn``, with the real
                     reason on stderr. This is the shape measured on a live
                     ``hermes acp`` whose provider rejected the credential.
- ``noisy``        - like ``ok``, but writes non-JSON diagnostics to stdout and a
                     large volume to stderr before answering.
- ``flood``        - like ``ok``, but writes one stderr line past the reader's limit,
                     followed by an ordinary one, before answering.
- ``asks``         - requests something raven does not implement (``fs/read_text_file``,
                     declared unsupported in ``CLIENT_CAPABILITIES``) and, having been
                     refused, ends the turn with no content and nothing on stderr. This
                     is the shape an adapter takes when raven answers "method not found".
- ``asks_late``    - holds prompts until two are in flight, then makes that same
                     unsupported request on the *second* session only and ends both
                     turns empty. For the cross-talk case: one shared connection, two
                     sessions, and only one of them asked for anything.
- ``permission``   - asks ``session/request_permission`` with a real option list and
                     answers the prompt with the ``optionId`` raven chose, so a test can
                     assert *which* option it picked rather than only that it answered.
- ``elicits_then_streams`` - asks ``elicitation/create`` and does not wait for an
                     answer, then streams three chunks and ends the turn. Proves a
                     handler that blocks on a human does not stall delivery on the
                     same connection.
- ``elicits_then_dies`` - asks ``elicitation/create``, then exits: the connection
                     ends while raven is still answering. For the read-loop death
                     path, where a pending answer task must not outlive the loop.
- ``elicits_and_waits`` - asks ``elicitation/create`` and holds the prompt open
                     until raven answers it, then reports what was chosen. For the
                     end-to-end path: an agent's question reaching a real human and
                     the answer coming back into the same turn.
- ``elicits_first_then_serves`` - the first session's prompt asks
                     ``elicitation/create`` and holds; every later session streams
                     and ends the turn. The two-session case of the pooled
                     connection: one session's pending question must not stall
                     another's whole round trip.
- ``no_session_delete`` - like ``ok`` but ``sessionCapabilities`` does not
                     advertise ``delete``, so a gated client must skip the call.
- ``delete_fails``   - advertises ``delete`` and answers ``session/delete`` with
                     method-not-found, for the failure-log path.
- ``two_messages`` - a preamble, the tool calls it announced, then the answer, in the
                     order measured on codex-acp. One turn, two messages, and no
                     boundary in the chunks themselves.
- ``cancelled``    - streams one chunk, then reports ``stopReason: "cancelled"``. The
                     shape measured on codex-acp when a turn is torn down part-way: a
                     reply that reads finished and is not.
- ``cancel_aware``  - holds the prompt open and answers it with
                      ``stopReason: "cancelled"`` only after a ``session/cancel``
                      notification arrives. Proves raven both sends the
                      notification and waits for the turn to settle.
- ``cancel_deaf``   - holds the prompt open and ignores ``session/cancel``
                      entirely, so the settle budget expires. Proves the timeout
                      path, which unbinds the session rather than reusing it.
- ``silent``       - reads and never answers, for the timeout path.
- ``mute``         - answers ``initialize``, then closes stdout and sleeps with
                     the process alive: the connection is dead, the pid is not.

Requests before ``initialize`` are refused, deliberately. A permissive stub is
what let a real bug through once already: two of the three live servers answer a
``session/new`` sent without a handshake, so the missing ``initialize`` in the
connection pool only surfaced against ``codex-acp``, which rejects it.
"""

from __future__ import annotations

import json
import os
import sys

MODE = os.environ.get("ACP_STUB_MODE", "ok")

_INITIALIZED = False

# A real server mints a distinct id per session; the counter keeps the stub from
# making concurrent sessions collide in a way no real agent would.
_SESSIONS = 0

_SESSION_CAPS = {"fork": {}, "list": {}, "resume": {}}
if MODE != "no_session_delete":
    _SESSION_CAPS["delete"] = {}

CAPABILITIES = {
    "protocolVersion": 1,
    "agentInfo": {"name": "stub-agent", "version": "9.9.9"},
    "agentCapabilities": {
        "loadSession": True,
        "promptCapabilities": {"image": True},
        "sessionCapabilities": _SESSION_CAPS,
    },
    "authMethods": [{"id": "stub-auth", "name": "Stub auth"}],
}


def send(frame: dict) -> None:
    sys.stdout.write(json.dumps(frame) + "\n")
    sys.stdout.flush()


def ok(request_id, result) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": result})


def err(request_id, code, message) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def notify(method: str, params: dict) -> None:
    send({"jsonrpc": "2.0", "method": method, "params": params})


def update(session_id: str, payload: dict) -> None:
    notify("session/update", {"sessionId": session_id, "update": payload})


_PENDING: list = []

# Prompts held open until raven answers the permission request they triggered.
_AWAITING_PERMISSION: list = []

# Elicitations held open until raven answers (elicits_and_waits).
_AWAITING_ELICITATION: list = []

# Prompts held open until the client cancels them (cancel_aware / cancel_deaf).
_AWAITING_CANCEL: list = []


def handle_response(frame) -> None:
    """Finish a held prompt once raven has answered the permission or elicitation request."""
    if _AWAITING_ELICITATION:
        content = ((frame.get("result") or {}).get("content")) or {}
        picked = content.get("backend") or "no-answer"
        request_id, session_id = _AWAITING_ELICITATION.pop(0)
        update(
            session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": f"using:{picked}"}}
        )
        ok(request_id, {"stopReason": "end_turn"})
        return
    if not _AWAITING_PERMISSION:
        return
    outcome = ((frame.get("result") or {}).get("outcome")) or {}
    chosen = outcome.get("optionId") or outcome.get("outcome") or "no-answer"
    request_id, session_id = _AWAITING_PERMISSION.pop(0)
    update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": f"chose:{chosen}"}})
    ok(request_id, {"stopReason": "end_turn"})


def handle_prompt(request_id, params) -> None:
    session_id = params.get("sessionId") or "stub-session-1"
    if MODE == "asks_late":
        _PENDING.append((request_id, session_id))
        if len(_PENDING) < 2:
            return
        asker = _PENDING[-1][1]
        send(
            {
                "jsonrpc": "2.0",
                "id": 9002,
                "method": "fs/read_text_file",
                "params": {"sessionId": asker, "path": "/etc/hostname"},
            }
        )
        for rid, _sid in _PENDING:
            ok(rid, {"stopReason": "end_turn"})
        _PENDING.clear()
        return
    if MODE == "stray_session":
        # An update for a session nobody is listening to. Real adapters produce
        # these as late frames after a turn settles; emitting one mid-turn is the
        # only way to make the case reproducible.
        update("no-such-session", {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "??"}})
        update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "pong"}})
        ok(request_id, {"stopReason": "end_turn"})
        return
    if MODE == "asks":
        send(
            {
                "jsonrpc": "2.0",
                "id": 9001,
                "method": "fs/read_text_file",
                "params": {"sessionId": session_id, "path": "/etc/hostname"},
            }
        )
        # A real adapter gives up on the tool it could not run; the turn ends
        # with nothing, and stderr stays empty.
        ok(request_id, {"stopReason": "end_turn"})
        return
    if MODE == "permission":
        # Reversed relative to how an adapter lists them, so a client that
        # answered with "the first option" instead of choosing by kind fails
        # this rather than passing by luck.
        send(
            {
                "jsonrpc": "2.0",
                "id": 9003,
                "method": "session/request_permission",
                "params": {
                    "sessionId": session_id,
                    "toolCall": {"toolCallId": "t1", "kind": "execute"},
                    "options": [
                        {"optionId": "no", "name": "Reject", "kind": "reject_once"},
                        {"optionId": "once", "name": "Allow Once", "kind": "allow_once"},
                        {"optionId": "always", "name": "Allow for Session", "kind": "allow_always"},
                    ],
                },
            }
        )
        _AWAITING_PERMISSION.append((request_id, session_id))
        return
    if MODE == "elicits_and_waits":
        _AWAITING_ELICITATION.append((request_id, session_id))
        send(
            {
                "jsonrpc": "2.0",
                "id": 9002,
                "method": "elicitation/create",
                "params": {
                    "sessionId": session_id,
                    "mode": "form",
                    "message": "which backend?",
                    "requestedSchema": {
                        "type": "object",
                        "properties": {"backend": {"type": "string", "enum": ["redis", "memcached"]}},
                    },
                },
            }
        )
        return
    if MODE == "elicits_first_then_serves":
        if session_id == "stub-session-1":
            _AWAITING_ELICITATION.append((request_id, session_id))
            send(
                {
                    "jsonrpc": "2.0",
                    "id": 9002,
                    "method": "elicitation/create",
                    "params": {
                        "sessionId": session_id,
                        "mode": "form",
                        "message": "which backend?",
                        "requestedSchema": {
                            "type": "object",
                            "properties": {"backend": {"type": "string", "enum": ["redis", "memcached"]}},
                        },
                    },
                }
            )
            return
        update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "pong"}})
        ok(request_id, {"stopReason": "end_turn"})
        return
    if MODE == "elicits_then_streams":
        # Asks and does NOT wait: the test asserts the client's read loop is
        # still delivering updates while the question is unanswered.
        send(
            {
                "jsonrpc": "2.0",
                "id": 9001,
                "method": "elicitation/create",
                "params": {
                    "sessionId": session_id,
                    "mode": "form",
                    "message": "which one?",
                    "requestedSchema": {"type": "object", "properties": {"pick": {"type": "string"}}},
                },
            }
        )
        for i in range(3):
            update(
                session_id,
                {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": f"chunk{i}"}},
            )
        ok(request_id, {"stopReason": "end_turn"})
        return
    if MODE == "elicits_then_dies":
        send(
            {
                "jsonrpc": "2.0",
                "id": 9001,
                "method": "elicitation/create",
                "params": {
                    "sessionId": session_id,
                    "mode": "form",
                    "message": "which one?",
                    "requestedSchema": {"type": "object", "properties": {"pick": {"type": "string"}}},
                },
            }
        )
        sys.exit(0)
    if MODE == "two_messages":
        # The order measured on codex-acp: a preamble in several chunks, the
        # tool calls it announced, then the answer itself.
        # The thought and the usage update sit *inside* a message, which is what
        # makes this stub able to tell a tool-call boundary from any-other-update:
        # breaking on those would split both messages in half.
        update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "let me "}})
        update(session_id, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "planning"}})
        update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "look."}})
        update(session_id, {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "ls", "status": "pending"})
        update(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"})
        update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "it is "}})
        update(session_id, {"sessionUpdate": "usage_update", "size": 1000, "used": 7})
        update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "a repo."}})
        ok(request_id, {"stopReason": "end_turn"})
        return
    if MODE in ("cancel_aware", "cancel_deaf"):
        update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "working"}})
        _AWAITING_CANCEL.append((request_id, session_id))
        return
    if MODE == "cancelled":
        update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "I will "}})
        update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "start by"}})
        ok(request_id, {"stopReason": "cancelled"})
        return
    if MODE == "empty_turn":
        print("provider rejected the credential: HTTP 401", file=sys.stderr, flush=True)
        ok(request_id, {"stopReason": "end_turn"})
        return
    update(session_id, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "thinking"}})
    # The shape measured on claude-agent-acp and opencode alike: the opening
    # frame announces the call with an empty rawInput, the arguments follow on a
    # later update, and the result arrives wrapped as ToolCallContent rather
    # than as a bare content block. `kind` rides on both, as it does on the wire
    # -- it is what names the tool, and a stub that omitted it would only ever
    # exercise the unclassified fallback.
    update(
        session_id,
        {
            "sessionUpdate": "tool_call",
            "toolCallId": "t1",
            "title": "read_file src/a.py",
            "kind": "read",
            "status": "pending",
            "rawInput": {},
        },
    )
    update(
        session_id,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "t1",
            "kind": "read",
            "status": "in_progress",
            "rawInput": {"path": "src/a.py"},
        },
    )
    update(
        session_id,
        {
            "sessionUpdate": "tool_call_update",
            "toolCallId": "t1",
            "status": "completed",
            "content": [{"type": "content", "content": {"type": "text", "text": "the file says hello"}}],
        },
    )
    update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "pong"}})
    update(session_id, {"sessionUpdate": "usage_update", "size": 1000, "used": 42})
    ok(request_id, {"stopReason": "end_turn"})


def handle_cancel(params) -> None:
    """Settle a held prompt on ``session/cancel``, unless this mode ignores it."""
    if MODE != "cancel_aware":
        return
    session_id = params.get("sessionId")
    for held in list(_AWAITING_CANCEL):
        if held[1] != session_id:
            continue
        _AWAITING_CANCEL.remove(held)
        ok(held[0], {"stopReason": "cancelled"})


def main() -> None:
    if MODE == "abort":
        # Dies before speaking, with the reason on stderr only -- the npx shape
        # where the adapter's install/startup fails.
        print("stub: cannot start, the registry is unreachable", file=sys.stderr, flush=True)
        sys.exit(3)
    if MODE == "noisy":
        sys.stdout.write("[plugins] stub diagnostics on stdout\n")
        sys.stdout.flush()
        sys.stderr.write("x" * 50_000 + "\n")
        sys.stderr.flush()
    if MODE == "flood":
        # One line past the reader's limit, then an ordinary one. Sized against
        # `client._READER_LIMIT` rather than against the litellm dump that found
        # this (146 KiB, which the raised limit now carries), because the case
        # under test is what the reader does when a line does not fit at all. The
        # line after it is how a test tells "survived the raise" apart from "went
        # on draining".
        sys.stderr.write("x" * (9 * 1024 * 1024) + "\n")
        sys.stderr.write("stub: still talking after the flood\n")
        sys.stderr.flush()
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        if MODE == "silent":
            continue
        method = frame.get("method")
        request_id = frame.get("id")
        params = frame.get("params") or {}
        # A frame with no method is raven answering something this stub asked.
        # Without this branch it fell through to the unknown-method arm below
        # and the stub replied to a reply.
        if method is None:
            handle_response(frame)
            continue
        if method == "initialize":
            if MODE == "reject_init":
                err(request_id, -32602, "Invalid params")
            else:
                global _INITIALIZED
                _INITIALIZED = True
                ok(request_id, CAPABILITIES)
                if MODE == "mute":
                    # The pathological shape behind one real outage: the server
                    # stops speaking (stdout closes) while the PROCESS lives on
                    # -- an npx wrapper waiting on a dead child looks exactly
                    # like this. The stderr line is the only account of why.
                    # os.close(1), not sys.stdout.close(): only the raw close
                    # actually drops the pipe's write end.
                    print("stub: worker died after the handshake", file=sys.stderr, flush=True)
                    sys.stdout.flush()
                    os.close(1)
                    import time

                    time.sleep(60)
                    return
        elif method == "session/cancel":
            handle_cancel(params)
            continue
        elif not _INITIALIZED:
            err(request_id, -32603, "Internal error: no initialize was received on this connection")
        elif method == "session/new":
            if MODE == "no_session":
                err(request_id, -32000, "no api key configured for this agent")
            else:
                global _SESSIONS
                _SESSIONS += 1
                ok(
                    request_id,
                    {
                        "sessionId": f"stub-session-{_SESSIONS}",
                        "models": {
                            "availableModels": [
                                {"modelId": "stub:model-a", "name": "model-a"},
                                {"modelId": "stub:model-b", "name": "model-b"},
                                {"name": "no-id-so-skipped"},
                            ]
                        },
                    },
                )
        elif method == "session/load":
            if params.get("sessionId") == "pruned-session":
                err(request_id, -32001, "Session not found")
            else:
                ok(request_id, {})
        elif method == "session/delete":
            if MODE == "delete_fails":
                err(request_id, -32601, "session/delete is not implemented")
            else:
                print(f"stub: session/delete for {params.get('sessionId')}", file=sys.stderr, flush=True)
                ok(request_id, {})
        elif method == "session/prompt":
            handle_prompt(request_id, params)
        elif request_id is not None:
            err(request_id, -32601, f"unknown method {method}")


if __name__ == "__main__":
    main()
