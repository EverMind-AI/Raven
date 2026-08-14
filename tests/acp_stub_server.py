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
- ``asks``         - requests permission mid-turn and, having been refused, ends the
                     turn with no content and nothing on stderr. This is the shape
                     an adapter takes when raven answers its `session/request_permission`
                     with "method not found".
- ``asks_late``    - holds prompts until two are in flight, then asks permission on
                     the *second* session only and ends both turns empty. For the
                     cross-talk case: one shared connection, two sessions, and only
                     one of them asked for anything.
- ``silent``       - reads and never answers, for the timeout path.

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

CAPABILITIES = {
    "protocolVersion": 1,
    "agentInfo": {"name": "stub-agent", "version": "9.9.9"},
    "agentCapabilities": {
        "loadSession": True,
        "promptCapabilities": {"image": True},
        "sessionCapabilities": {"fork": {}, "list": {}, "resume": {}},
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
                "method": "session/request_permission",
                "params": {"sessionId": asker, "toolCall": {"toolCallId": "t1"}},
            }
        )
        for rid, _sid in _PENDING:
            ok(rid, {"stopReason": "end_turn"})
        _PENDING.clear()
        return
    if MODE == "asks":
        send(
            {
                "jsonrpc": "2.0",
                "id": 9001,
                "method": "session/request_permission",
                "params": {"sessionId": session_id, "toolCall": {"toolCallId": "t1"}},
            }
        )
        # A real adapter gives up on the tool it could not run; the turn ends
        # with nothing, and stderr stays empty.
        ok(request_id, {"stopReason": "end_turn"})
        return
    if MODE == "empty_turn":
        print("provider rejected the credential: HTTP 401", file=sys.stderr, flush=True)
        ok(request_id, {"stopReason": "end_turn"})
        return
    update(session_id, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "thinking"}})
    update(
        session_id,
        {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "read_file src/a.py", "status": "pending"},
    )
    update(session_id, {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"})
    update(session_id, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "pong"}})
    update(session_id, {"sessionUpdate": "usage_update", "size": 1000, "used": 42})
    ok(request_id, {"stopReason": "end_turn"})


def main() -> None:
    if MODE == "noisy":
        sys.stdout.write("[plugins] stub diagnostics on stdout\n")
        sys.stdout.flush()
        sys.stderr.write("x" * 50_000 + "\n")
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
        if method == "initialize":
            if MODE == "reject_init":
                err(request_id, -32602, "Invalid params")
            else:
                global _INITIALIZED
                _INITIALIZED = True
                ok(request_id, CAPABILITIES)
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
        elif method == "session/prompt":
            handle_prompt(request_id, params)
        elif request_id is not None:
            err(request_id, -32601, f"unknown method {method}")


if __name__ == "__main__":
    main()
