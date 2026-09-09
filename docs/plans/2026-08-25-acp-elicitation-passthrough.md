# ACP Sub-agent Elicitation Pass-Through Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let an ACP sub-agent ask the user a question through `elicitation/create` and get the answer back, rendered on the `clarify.request` surface both frontends already implement.

**Architecture:** Declare the `elicitation.form` client capability; route each request by its `sessionId` to an elicitor attached for that run; decompose the requested schema into one clarify question per field in a pure, transport-free module; answer `accept` / `decline` / `cancel`. The read loop must stop awaiting request handlers inline before any of this is switched on.

**Tech Stack:** Python 3.12+, asyncio, `uv`, pytest. ACP protocol v1 (`@agentclientprotocol/sdk` 1.3.0 / 1.4.0 schema).

**Spec:** `docs/specs/2026-08-25-acp-elicitation-passthrough-design.md`

## Global Constraints

- Branch `feat/acp_elicitation_passthrough`, base `origin/main` @ `2a58b17b`. Worktree: `.claude/worktrees/feat+acp_elicitation_passthrough`.
- Run tests only as `uv run pytest` (AGENTS.md 5.4). Never bare `pytest`.
- **Do not commit without the user's explicit instruction** (AGENTS.md 3.4). The commit step in each task marks the intended commit boundary: stop there and report.
- Advertise `"elicitation": {"form": {}}` and never `url`.
- The wire method is `elicitation/create`. The notification `elicitation/complete` is url-mode only and is not implemented.
- **Every `elicitation/create` receives a response.** Never `-32601` for it: the capability is declared, so "method not found" is a lie the agent cannot act on.
- Every fallback answers `{"action": "decline"}`, except a cancelled raven turn, which answers `{"action": "cancel"}`.
- Field validation retries: 2 per field, then `decline`.
- Question timeout stays the broker's existing 600s per question. No additional whole-form budget.
- `content` values are limited to the five wire types: `str`, `int`, `float`, `bool`, `list[str]`.
- Comments in English, only where they explain *why* (AGENTS.md 1.1, 1.2).
- Pre-commit hooks are disabled in this repo; run `make lint-python` by hand before the final push.
- Baseline at `2a58b17b`: **10951 passed, 50 skipped, 13 deselected, 0 failed**. Any failure is this branch's.

---

### Task 1: Spike - capture a real `elicitation/create` frame

Write every later test against an observed payload instead of a reading of the schema. Throwaway code; the only kept artifact is a captured frame saved as a fixture.

**Files:**
- Create (throwaway, outside the repo): `<scratchpad>/probe_elicit.py`
- Create: `tests/fixtures/acp_elicitation_askuser.json`

**Interfaces:**
- Consumes: nothing.
- Produces: `tests/fixtures/acp_elicitation_askuser.json` - one real `elicitation/create` params object, used by Task 3's tests.

- [ ] **Step 1: Write a standalone minimal ACP client**

Deliberately not raven's `AcpClient`: this runs before the read-loop fix, and a long wait inside raven's pool is exactly what Task 2 exists to prevent.

```python
# <scratchpad>/probe_elicit.py
import asyncio, json, sys

CAPS = {"fs": {"readTextFile": False, "writeTextFile": False},
        "elicitation": {"form": {}}}


async def main(command: str, prompt: str) -> None:
    proc = await asyncio.create_subprocess_shell(
        command, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    state = {"nid": 0, "session": None}

    def send(frame: dict) -> None:
        proc.stdin.write((json.dumps(frame) + "\n").encode())

    def request(method: str, params: dict) -> None:
        state["nid"] += 1
        send({"jsonrpc": "2.0", "id": state["nid"], "method": method, "params": params})

    request("initialize", {"protocolVersion": 1, "clientCapabilities": CAPS})
    while True:
        line = await proc.stdout.readline()
        if not line:
            break
        try:
            frame = json.loads(line)
        except ValueError:
            continue
        print("<<", json.dumps(frame)[:400], flush=True)
        method = frame.get("method")
        if method == "elicitation/create":
            print("CAPTURED", json.dumps(frame["params"], indent=2), flush=True)
            send({"jsonrpc": "2.0", "id": frame["id"], "result": {"action": "decline"}})
        elif method == "session/request_permission":
            opts = frame["params"].get("options") or []
            outcome = ({"outcome": "selected", "optionId": opts[0]["optionId"]} if opts
                       else {"outcome": "cancelled"})
            send({"jsonrpc": "2.0", "id": frame["id"], "result": {"outcome": outcome}})
        elif method is not None and "id" in frame:
            send({"jsonrpc": "2.0", "id": frame["id"],
                  "error": {"code": -32601, "message": method}})
        elif method is None and "id" in frame:
            result = frame.get("result") or {}
            if state["session"] is None and "sessionId" in result:
                state["session"] = result["sessionId"]
                request("session/prompt", {"sessionId": state["session"],
                                           "prompt": [{"type": "text", "text": prompt}]})
            elif state["session"] is None:
                request("session/new", {"cwd": "/tmp", "mcpServers": []})


asyncio.run(main(sys.argv[1], sys.argv[2]))
```

- [ ] **Step 2: Run it against the claude-code adapter with a prompt that forces a question**

```bash
cd /tmp && python3 <scratchpad>/probe_elicit.py \
  "npx -y @agentclientprotocol/claude-agent-acp@0.66.0" \
  "I want to add a cache layer. Ask me which backend to use before doing anything - do not guess."
```

Expected: a `CAPTURED` block carrying `message`, `mode: "form"`, `sessionId`, and a `requestedSchema` whose properties include a `question_0` / `question_0_custom` pair.

If no `elicitation/create` arrives, that is the finding: record it, then run the same probe against `npx -y @agentclientprotocol/codex-acp@1.1.14` and `/root/.opencode/bin/opencode acp`. At least one must produce a frame, or Task 8's switch has nothing to serve.

- [ ] **Step 3: Save the captured params as a fixture**

```bash
mkdir -p tests/fixtures
# paste the CAPTURED object into this file verbatim, no edits
$EDITOR tests/fixtures/acp_elicitation_askuser.json
python3 -c "import json; d=json.load(open('tests/fixtures/acp_elicitation_askuser.json')); print(sorted(d), d['mode'], sorted(d['requestedSchema']['properties']))"
```

Expected: prints the top-level keys including `sessionId` and `requestedSchema`, `mode` is `form`, and the property names.

- [ ] **Step 4: Commit**

```bash
git add tests/fixtures/acp_elicitation_askuser.json
git commit -m "test(agent): record a real acp elicitation request as a fixture"
```

---

### Task 2: Stop the read loop awaiting request handlers inline

This must land before Task 8. With the capability declared and this unfixed, the first question freezes every session on the pooled connection: `_read_stdout` -> `_dispatch` (`client.py:546`) -> `_answer_request` (`:564`) -> `on_request` is one inline await chain, so a 600s human wait stops all `session/update` delivery and all response resolution on that connection.

**Files:**
- Modify: `raven/agent/acp/client.py` - `__init__`, `_dispatch` (`:546-555`), `close`
- Modify: `tests/acp_stub_server.py`
- Test: `tests/test_subagent_acp.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `_dispatch` answers requests on a task. `AcpClient._answer_tasks: set[asyncio.Task]`, cancelled in `close()`.

- [ ] **Step 1: Add a stub-server mode that asks and keeps streaming**

```python
# tests/acp_stub_server.py, inside handle_prompt
    if MODE == "elicits_then_streams":
        # Asks and does NOT wait: the test asserts the client's read loop is
        # still delivering updates while the question is unanswered.
        send({"jsonrpc": "2.0", "id": 9001, "method": "elicitation/create",
              "params": {"sessionId": session_id, "mode": "form",
                         "message": "which one?",
                         "requestedSchema": {"type": "object",
                                             "properties": {"pick": {"type": "string"}}}}})
        for i in range(3):
            update(session_id, {"sessionUpdate": "agent_message_chunk",
                                "content": {"type": "text", "text": f"chunk{i}"}})
        ok(request_id, {"stopReason": "end_turn"})
        return
```

- [ ] **Step 2: Write the failing test**

```python
async def test_an_unanswered_request_does_not_stall_the_read_loop() -> None:
    """A handler that never returns must not stop `session/update` delivery.

    Inline-awaited request handling made one pending question freeze every
    session on a pooled connection, which read as "two sub-agents at once
    hangs" with nothing pointing at the question.
    """
    from raven.agent.acp.client import AcpClient

    seen: list[str] = []
    blocked = asyncio.Event()

    async def never_answers(method: str, params: dict) -> object:
        blocked.set()
        await asyncio.sleep(3600)

    async def note(method: str, params: dict) -> None:
        text = (((params.get("update") or {}).get("content") or {}).get("text")) or ""
        if text:
            seen.append(text)

    client = await AcpClient.launch(
        name="stub", command=f"{sys.executable} {_STUB}",
        env={"ACP_STUB_MODE": "elicits_then_streams"},
        on_request=never_answers, on_notification=note)
    try:
        await client.request("initialize", protocol.initialize_params(), timeout=15.0)
        session = (await client.request("session/new", {"cwd": "/tmp", "mcpServers": []},
                                        timeout=15.0))["sessionId"]
        await client.request("session/prompt",
                            {"sessionId": session, "prompt": [{"type": "text", "text": "hi"}]},
                            timeout=15.0)
        assert blocked.is_set()
        assert seen == ["chunk0", "chunk1", "chunk2"]
    finally:
        await client.close()
```

- [ ] **Step 3: Run it and watch it fail**

Run: `uv run pytest tests/test_subagent_acp.py::test_an_unanswered_request_does_not_stall_the_read_loop -x -q`

Expected: FAIL - `session/prompt` times out at 15s, because the read loop is parked inside `never_answers` and never reads the response.

- [ ] **Step 4: Make `_dispatch` spawn the answer**

In `__init__`, beside the other per-connection state:

```python
        # Answering runs off the read loop: a handler may block on a human
        # (elicitation), and awaiting it here stops every other session on this
        # connection. Requests carry their own id and are independent, so
        # answering out of arrival order is sound.
        self._answer_tasks: set[asyncio.Task] = set()
```

In `_dispatch`, replacing the inline await at `:553-555`:

```python
        if "id" in frame:
            task = asyncio.create_task(self._answer_request(frame["id"], str(method), params))
            self._answer_tasks.add(task)
            task.add_done_callback(self._answer_tasks.discard)
            return
```

In `close()`, beside the existing teardown:

```python
        for task in list(self._answer_tasks):
            task.cancel()
```

- [ ] **Step 5: Run the test and the whole ACP suite**

Run: `uv run pytest tests/test_subagent_acp.py tests/test_acp_stdio.py tests/test_acp_journal.py tests/test_acp_dialects.py -q`

Expected: PASS with no regressions. The existing `permission` stub-mode tests now exercise the task path.

- [ ] **Step 6: Commit**

```bash
git add raven/agent/acp/client.py tests/acp_stub_server.py tests/test_subagent_acp.py
git commit -m "fix(agent): answer acp requests off the read loop so one question cannot stall a connection"
```

---

### Task 3: The pure elicitation module

**Files:**
- Create: `raven/agent/acp/elicitation.py`
- Test: `tests/test_acp_elicitation.py`

**Interfaces:**
- Consumes: `tests/fixtures/acp_elicitation_askuser.json` (Task 1).
- Produces:
  - `Ask`: `.message: str`, `.mode: str`, `.schema: dict`, `.session_id: str | None`, `.tool_call_id: str | None`, `.request_id: str | None`
  - `Field`: `.name: str`, `.prompt: str`, `.type: str`, `.options: list[str]`, `.required: bool`, `.constraints: dict`, `.custom_name: str | None`
  - `parse_request(params: dict) -> Ask | None`
  - `fields(schema: dict) -> list[Field]`
  - `coerce(field: Field, text: str) -> tuple[bool, Any]` - `(False, None)` when invalid
  - `accept(content: dict) -> dict`, `decline() -> dict`, `cancel() -> dict`
  - `METHOD: str = "elicitation/create"`, `MAX_FIELD_RETRIES: int = 2`

- [ ] **Step 1: Write the failing tests**

```python
"""Unit tests for the pure elicitation schema layer."""

import json
from pathlib import Path

_FIXTURE = Path(__file__).parent / "fixtures" / "acp_elicitation_askuser.json"


def test_a_real_askuser_request_parses() -> None:
    from raven.agent.acp.elicitation import parse_request

    ask = parse_request(json.loads(_FIXTURE.read_text()))
    assert ask is not None
    assert ask.mode == "form"
    assert ask.session_id
    assert ask.message


def test_fields_keep_the_schema_write_order() -> None:
    from raven.agent.acp.elicitation import fields

    schema = {"type": "object", "properties": {
        "b": {"type": "string"}, "a": {"type": "string"}, "c": {"type": "string"}}}
    assert [f.name for f in fields(schema)] == ["b", "a", "c"]


def test_an_enum_becomes_options_and_oneof_carries_labels() -> None:
    from raven.agent.acp.elicitation import fields

    schema = {"type": "object", "properties": {
        "pick": {"type": "string", "enum": ["redis", "memcached"]},
        "also": {"type": "string", "oneOf": [
            {"const": "yes", "description": "do it"}, {"const": "no"}]}}}
    got = {f.name: f.options for f in fields(schema)}
    assert got["pick"] == ["redis", "memcached"]
    assert got["also"] == ["yes", "no"]


def test_a_multi_select_reads_its_choices_off_items() -> None:
    from raven.agent.acp.elicitation import fields

    schema = {"type": "object", "properties": {
        "tags": {"type": "array", "items": {"anyOf": [{"const": "a"}, {"const": "b"}]}}}}
    assert fields(schema)[0].options == ["a", "b"]


def test_required_is_read_from_the_schema() -> None:
    from raven.agent.acp.elicitation import fields

    schema = {"type": "object", "required": ["a"],
              "properties": {"a": {"type": "string"}, "b": {"type": "string"}}}
    assert {f.name: f.required for f in fields(schema)} == {"a": True, "b": False}


def test_the_prompt_prefers_title_then_description_then_the_name() -> None:
    from raven.agent.acp.elicitation import fields

    schema = {"type": "object", "properties": {
        "a": {"type": "string", "title": "T", "description": "D"},
        "b": {"type": "string", "description": "D"},
        "c": {"type": "string"}}}
    assert [f.prompt for f in fields(schema)] == ["T", "D", "c"]


def _field(kind, **kw):
    from raven.agent.acp.elicitation import Field

    return Field(name="x", prompt="x", type=kind, options=kw.pop("options", []),
                 required=False, constraints=kw)


def test_coerce_handles_all_five_wire_types() -> None:
    from raven.agent.acp.elicitation import coerce

    assert coerce(_field("string"), "hi") == (True, "hi")
    assert coerce(_field("integer"), "7") == (True, 7)
    assert coerce(_field("number"), "1.5") == (True, 1.5)
    assert coerce(_field("boolean"), "yes") == (True, True)
    assert coerce(_field("boolean"), "no") == (True, False)
    assert coerce(_field("array", options=["a", "b"]), "a, b") == (True, ["a", "b"])


def test_coerce_rejects_what_does_not_fit() -> None:
    from raven.agent.acp.elicitation import coerce

    assert coerce(_field("integer"), "seven") == (False, None)
    assert coerce(_field("string", pattern=r"^v\d+$"), "nope") == (False, None)
    assert coerce(_field("string", options=["a"]), "b") == (False, None)
    assert coerce(_field("array", options=["a"]), "a, zzz") == (False, None)
    assert coerce(_field("integer", minimum=5), "1") == (False, None)


def test_a_malformed_schema_yields_no_fields() -> None:
    from raven.agent.acp.elicitation import fields

    assert fields({}) == []
    assert fields({"type": "object", "properties": "nope"}) == []
    assert fields({"type": "object", "properties": {"a": {"type": "wat"}}}) == []


def test_url_and_unknown_modes_parse_but_are_not_form() -> None:
    from raven.agent.acp.elicitation import parse_request

    url = parse_request({"message": "m", "mode": "url", "url": "https://x", "elicitationId": "e"})
    assert url is not None and url.mode == "url"
    other = parse_request({"message": "m", "mode": "_custom"})
    assert other is not None and other.mode == "_custom"
    assert parse_request({"mode": "form"}) is None


def test_the_response_builders_match_the_protocol() -> None:
    from raven.agent.acp.elicitation import accept, cancel, decline

    assert accept({"a": 1}) == {"action": "accept", "content": {"a": 1}}
    assert decline() == {"action": "decline"}
    assert cancel() == {"action": "cancel"}
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_acp_elicitation.py -q`

Expected: FAIL - `ModuleNotFoundError: No module named 'raven.agent.acp.elicitation'`

- [ ] **Step 3: Write the module**

```python
"""The schema half of ACP elicitation: wire params in, questions and content out.

Deliberately free of transport, broker and event loop, because everything here is
a decision about a JSON Schema and is worth testing without an agent process. The
glue that turns a `Field` into a question a human sees is `raven.agent.acp.elicitor`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field as dc_field
from typing import Any

METHOD = "elicitation/create"

MAX_FIELD_RETRIES = 2
"""Re-asks per field before the whole elicitation declines.

Bounded because a `pattern` the user cannot satisfy would otherwise re-ask
forever, and an honest decline beats a loop."""

_TRUE = {"yes", "y", "true", "1", "on"}
_FALSE = {"no", "n", "false", "0", "off"}
_TYPES = {"string", "integer", "number", "boolean", "array"}


@dataclass
class Field:
    name: str
    prompt: str
    type: str
    options: list[str] = dc_field(default_factory=list)
    required: bool = False
    constraints: dict[str, Any] = dc_field(default_factory=dict)
    custom_name: str | None = None
    """The paired free-text property a dialect folded into this one, if any."""


@dataclass
class Ask:
    message: str
    mode: str
    schema: dict[str, Any] = dc_field(default_factory=dict)
    session_id: str | None = None
    tool_call_id: str | None = None
    request_id: str | None = None


def parse_request(params: dict[str, Any]) -> Ask | None:
    """One `elicitation/create` params object, or `None` if it is not one.

    The scope fields are flattened onto the same object by the protocol's own
    `anyOf`, so `sessionId` sits beside `mode` rather than under a wrapper.
    """
    if not isinstance(params, dict):
        return None
    mode, message = params.get("mode"), params.get("message")
    if not isinstance(mode, str) or not isinstance(message, str):
        return None
    schema = params.get("requestedSchema")
    return Ask(
        message=message,
        mode=mode,
        schema=schema if isinstance(schema, dict) else {},
        session_id=_str(params.get("sessionId")),
        tool_call_id=_str(params.get("toolCallId")),
        request_id=_str(params.get("requestId")),
    )


def fields(schema: dict[str, Any]) -> list[Field]:
    """The schema's properties as an ordered question list.

    Empty for anything unusable, including a single property of an unknown type:
    a partly-asked form cannot produce content matching the schema, so the caller
    declines rather than asking half of it.
    """
    if not isinstance(schema, dict):
        return []
    props = schema.get("properties")
    if not isinstance(props, dict) or not props:
        return []
    required = schema.get("required")
    required = set(required) if isinstance(required, list) else set()
    out: list[Field] = []
    for name, spec in props.items():
        if not isinstance(name, str) or not isinstance(spec, dict):
            return []
        kind = spec.get("type", "string")
        if kind not in _TYPES:
            return []
        out.append(Field(
            name=name,
            prompt=_str(spec.get("title")) or _str(spec.get("description")) or name,
            type=kind,
            options=_options(spec),
            required=name in required,
            constraints={k: spec[k] for k in
                         ("pattern", "minLength", "maxLength", "minimum", "maximum")
                         if k in spec},
        ))
    return out


def coerce(field: Field, text: str) -> tuple[bool, Any]:
    """One typed value from what the user typed, or `(False, None)`."""
    text = text.strip()
    if field.type == "array":
        items = [p.strip() for p in text.split(",") if p.strip()]
        if field.options and any(i not in field.options for i in items):
            return (False, None)
        return (True, items)
    if field.type == "boolean":
        low = text.lower()
        if low in _TRUE:
            return (True, True)
        if low in _FALSE:
            return (True, False)
        return (False, None)
    if field.type in ("integer", "number"):
        try:
            value: Any = int(text) if field.type == "integer" else float(text)
        except ValueError:
            return (False, None)
        return (True, value) if _in_range(field, value) else (False, None)
    if field.options and text not in field.options:
        return (False, None)
    return (True, text) if _string_ok(field, text) else (False, None)


def accept(content: dict[str, Any]) -> dict[str, Any]:
    return {"action": "accept", "content": content}


def decline() -> dict[str, Any]:
    return {"action": "decline"}


def cancel() -> dict[str, Any]:
    return {"action": "cancel"}


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _options(spec: dict[str, Any]) -> list[str]:
    """Choices from `enum`, or from `oneOf` / `anyOf` `const` values.

    An array property carries its choices on `items`, so look there too: a
    multi-select is an array whose item schema holds the enum.
    """
    items = spec.get("items")
    for holder in (spec, items if isinstance(items, dict) else {}):
        enum = holder.get("enum")
        if isinstance(enum, list):
            return [v for v in enum if isinstance(v, str)]
        for key in ("oneOf", "anyOf"):
            variants = holder.get(key)
            if isinstance(variants, list):
                picked = [v.get("const") for v in variants if isinstance(v, dict)]
                picked = [c for c in picked if isinstance(c, str)]
                if picked:
                    return picked
    return []


def _in_range(field: Field, value: float) -> bool:
    low, high = field.constraints.get("minimum"), field.constraints.get("maximum")
    if isinstance(low, (int, float)) and value < low:
        return False
    return not (isinstance(high, (int, float)) and value > high)


def _string_ok(field: Field, text: str) -> bool:
    low, high = field.constraints.get("minLength"), field.constraints.get("maxLength")
    if isinstance(low, int) and len(text) < low:
        return False
    if isinstance(high, int) and len(text) > high:
        return False
    pattern = field.constraints.get("pattern")
    if isinstance(pattern, str):
        try:
            return re.search(pattern, text) is not None
        except re.error:
            # An agent's bad regexp is not the user's problem: accept rather than
            # declining a question that was otherwise answerable.
            return True
    return True


__all__ = ["METHOD", "MAX_FIELD_RETRIES", "Ask", "Field", "accept", "cancel",
           "coerce", "decline", "fields", "parse_request"]
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/test_acp_elicitation.py -q`

Expected: PASS, 11 tests.

- [ ] **Step 5: Commit**

```bash
git add raven/agent/acp/elicitation.py tests/test_acp_elicitation.py
git commit -m "feat(agent): read an acp elicitation schema into questions and typed content"
```

---

### Task 4: Session-keyed elicitor registry and the request dispatcher

**Files:**
- Modify: `raven/agent/acp/pool.py` - add `_SessionElicitors`, extend `_Connection.__init__` (`:100-128`), `acquire` (`:206`, `:221`)
- Modify: `raven/agent/acp/permissions.py`
- Test: `tests/test_subagent_acp.py`

**Interfaces:**
- Consumes: `raven.agent.acp.elicitation` (Task 3).
- Produces:
  - `_SessionElicitors(agent)` with `attach(session_id, elicitor)`, `detach(session_id, elicitor)`, `current(session_id)`, `async answer(params) -> dict | None`
  - `_Connection.elicitors: _SessionElicitors`
  - `request_dispatcher(name, observe=None, *, elicitors=None)` in `permissions.py`. `auto_approver` stays exported and unchanged.
  - An elicitor is any object with `async def elicit(self, params: dict) -> dict`.

- [ ] **Step 1: Write the failing tests**

```python
async def test_an_elicitation_reaches_the_elicitor_of_its_own_session() -> None:
    """A pooled connection carries several runs; sessionId is what separates them."""
    from raven.agent.acp.permissions import request_dispatcher
    from raven.agent.acp.pool import _SessionElicitors

    class Spy:
        def __init__(self, tag):
            self.tag, self.seen = tag, []

        async def elicit(self, params):
            self.seen.append(params)
            return {"action": "accept", "content": {"who": self.tag}}

    registry = _SessionElicitors("stub")
    a, b = Spy("a"), Spy("b")
    registry.attach("s-a", a)
    registry.attach("s-b", b)
    handle = request_dispatcher("stub", elicitors=registry)

    got = await handle("elicitation/create", {"sessionId": "s-b", "mode": "form", "message": "m"})
    assert got == {"action": "accept", "content": {"who": "b"}}
    assert a.seen == []


async def test_an_elicitation_for_an_unknown_session_declines() -> None:
    from raven.agent.acp.permissions import request_dispatcher
    from raven.agent.acp.pool import _SessionElicitors

    handle = request_dispatcher("stub", elicitors=_SessionElicitors("stub"))
    got = await handle("elicitation/create", {"sessionId": "gone", "mode": "form", "message": "m"})
    assert got == {"action": "decline"}


async def test_a_request_scoped_elicitation_declines_rather_than_erroring() -> None:
    """`-32601` on a declared capability is a lie the agent cannot act on."""
    from raven.agent.acp.permissions import request_dispatcher
    from raven.agent.acp.pool import _SessionElicitors

    handle = request_dispatcher("stub", elicitors=_SessionElicitors("stub"))
    got = await handle("elicitation/create", {"requestId": "r1", "mode": "form", "message": "m"})
    assert got == {"action": "decline"}


async def test_an_elicitor_that_raises_still_answers() -> None:
    from raven.agent.acp.permissions import request_dispatcher
    from raven.agent.acp.pool import _SessionElicitors

    class Boom:
        async def elicit(self, params):
            raise RuntimeError("nope")

    registry = _SessionElicitors("stub")
    registry.attach("s", Boom())
    handle = request_dispatcher("stub", elicitors=registry)
    assert await handle("elicitation/create",
                        {"sessionId": "s", "mode": "form", "message": "m"}) == {"action": "decline"}


async def test_the_dispatcher_still_approves_permissions_and_refuses_the_rest() -> None:
    from raven.agent.acp.client import UNHANDLED
    from raven.agent.acp.permissions import request_dispatcher

    handle = request_dispatcher("stub")
    approved = await handle("session/request_permission",
                            {"options": [{"optionId": "a", "kind": "allow_always"}]})
    assert approved == {"outcome": {"outcome": "selected", "optionId": "a"}}
    assert await handle("fs/read_text_file", {"path": "/etc/hostname"}) is UNHANDLED


def test_elicitor_detach_is_identity_checked() -> None:
    """An unconditional pop lets a finishing run unserve a later one."""
    from raven.agent.acp.pool import _SessionElicitors

    registry = _SessionElicitors("stub")
    first, second = object(), object()
    registry.attach("s", first)
    registry.attach("s", second)
    registry.detach("s", first)
    assert registry.current("s") is second
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_subagent_acp.py -k "elicitation or dispatcher or identity_checked" -q`

Expected: FAIL - `ImportError: cannot import name '_SessionElicitors' from 'raven.agent.acp.pool'`

- [ ] **Step 3: Add the registry to `pool.py`**

Beside `_SessionRouter`:

```python
class _SessionElicitors:
    """Which run answers an `elicitation/create` for a given session.

    A separate registry rather than a second job for `_SessionRouter`, because a
    router sink returns nothing and an elicitation needs an answer. Same
    attach/detach discipline, including the identity check, for the same reason.
    """

    def __init__(self, agent: str) -> None:
        self._agent = agent
        self._elicitors: dict[str, Any] = {}

    def attach(self, session_id: str, elicitor: Any) -> None:
        self._elicitors[session_id] = elicitor

    def detach(self, session_id: str, elicitor: Any) -> None:
        # Identity-checked for the reason `_SessionRouter.detach` documents: an
        # unconditional pop lets a finishing run tear down a later run's
        # answering path, and the symptom is that run's questions declining.
        if self._elicitors.get(session_id) is elicitor:
            del self._elicitors[session_id]

    def current(self, session_id: str) -> Any:
        return self._elicitors.get(session_id)

    async def answer(self, params: dict[str, Any]) -> dict[str, Any] | None:
        """The answer for one request, or `None` when no run owns it."""
        session_id = params.get("sessionId")
        elicitor = self._elicitors.get(session_id) if isinstance(session_id, str) else None
        if elicitor is None:
            return None
        return await elicitor.elicit(params)
```

In `_Connection.__init__`, add `elicitors: "_SessionElicitors | None" = None` to the signature and, beside `self.router = router`:

```python
        self.elicitors = elicitors if elicitors is not None else _SessionElicitors("")
```

In `acquire`, beside `router = _SessionRouter(name)` at `:206`:

```python
            elicitors = _SessionElicitors(name)
```

At the `on_request` call site (`:221`):

```python
                on_request=on_request if on_request is not None
                else request_dispatcher(name, router.dispatch, elicitors=elicitors),
```

and pass `elicitors=elicitors` into the `_Connection(...)` construction, replacing the `auto_approver` import with `request_dispatcher`.

- [ ] **Step 4: Add the dispatcher to `permissions.py`**

```python
def request_dispatcher(
    name: str,
    observe: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
    *,
    elicitors: Any = None,
) -> "Any":
    """The connection's `on_request`: permissions, elicitations, nothing else.

    One handler rather than a chain, because the two answers have nothing in
    common and the third case -- everything raven does not serve -- has to stay
    an explicit `method not found`.
    """
    approve = auto_approver(name, observe)

    async def handle(method: str, params: dict[str, Any]) -> Any:
        if method != elicitation.METHOD:
            return await approve(method, params)
        if elicitors is None:
            return elicitation.decline()
        try:
            answer = await elicitors.answer(params)
        except Exception as exc:  # noqa: BLE001 - a declared capability must answer
            logger.warning("acp agent {!r}: elicitation failed, declining: {}", name, exc)
            return elicitation.decline()
        # No run owns this session, or the scope was `requestId` -- an auth-phase
        # elicitation with no session at all. Declining is the answer; `-32601`
        # would deny a capability raven advertised.
        return answer if answer is not None else elicitation.decline()

    return handle
```

Add `from raven.agent.acp import elicitation` and extend `__all__` with `"request_dispatcher"`.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/test_subagent_acp.py -q`

Expected: PASS, including the pre-existing `permission_outcome` tests.

- [ ] **Step 6: Commit**

```bash
git add raven/agent/acp/pool.py raven/agent/acp/permissions.py tests/test_subagent_acp.py
git commit -m "feat(agent): route an acp elicitation to the run that owns its session"
```

---

### Task 5: The asker seam, origin-gated

The spec says to reuse `AskUserTool.ask_direct` but not how the ACP layer reaches it. The backend is built from config in `backends/__init__.py:264-278` with no loop reference, and the broker is late-bound at four transports, so a parallel `set_asker` at each is four chances to miss one.

Follow `ExecTool.start_approval_turn` instead: it solves exactly this problem and is called from exactly one place, `raven/rpc/spine.py:166`. A turn-scoped capability, rebound per turn, and **only for `Origin.USER`** so CRON and background turns fail closed as non-interactive with no special case.

Binding `conversation_id=cid` here is also what settles the spec's "main `session_key`, not the per-instance `direct_lane`" decision: `cid` is `_conversation_id(req)`, the conversation the reader is actually looking at. Do not derive a per-instance lane for this - `ui/` buckets sheets by conversation, so a lane key would file the question where nobody sees it.

**Files:**
- Create: `raven/agent/acp/asker.py`
- Modify: `raven/rpc/spine.py` - module scope plus `run()` at `:157-172`
- Test: `tests/test_acp_elicitation.py`

**Interfaces:**
- Consumes: nothing from earlier tasks.
- Produces:
  - `start_ask_turn(asker: Any, *, conversation_id: str) -> None`
  - `current_ask() -> tuple[Any, str]` - `(asker, conversation_id)`, `(None, "")` when unbound
  - `Asker` protocol: `async def ask(prompt: str, choices: list[str] | None, conversation_id: str) -> str | None`

- [ ] **Step 1: Write the failing tests**

```python
def test_no_asker_bound_reads_as_unavailable() -> None:
    from raven.agent.acp.asker import current_ask

    assert current_ask() == (None, "")


async def test_a_bound_asker_is_visible_to_a_child_task() -> None:
    """A sub-agent run is a background task; ContextVars copy into it."""
    import asyncio

    from raven.agent.acp.asker import current_ask, start_ask_turn

    class Tool:
        async def ask(self, prompt, choices, conversation_id):
            return f"{conversation_id}:{prompt}"

    start_ask_turn(Tool(), conversation_id="tui:c1")

    async def child():
        asker, cid = current_ask()
        assert asker is not None
        return await asker.ask("q?", None, cid)

    assert await asyncio.create_task(child()) == "tui:c1:q?"
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_acp_elicitation.py -k asker -q`

Expected: FAIL - `ModuleNotFoundError: No module named 'raven.agent.acp.asker'`

- [ ] **Step 3: Write the module**

```python
"""The turn's route to a human, for code with no tool registry to look in.

`AskUserTool` is registered per `AgentLoop`, and an ACP backend is built from
config with no loop reference, so it cannot resolve the tool the way
`AgentLoop._confirm_graph` does. A turn-scoped ContextVar is how `ExecTool`
already solves the same problem for shell approvals, and it inherits into the
background task a sub-agent run happens on.
"""

from __future__ import annotations

from contextvars import ContextVar
from typing import Any, Protocol


class Asker(Protocol):
    async def ask(self, prompt: str, choices: list[str] | None,
                  conversation_id: str) -> str | None: ...


_TURN: ContextVar[tuple[Any, str]] = ContextVar("acp_ask_turn", default=(None, ""))


def start_ask_turn(asker: Any, *, conversation_id: str) -> None:
    """Bind this turn's asker. `None` means no human is reachable."""
    _TURN.set((asker, conversation_id))


def current_ask() -> tuple[Any, str]:
    return _TURN.get()


__all__ = ["Asker", "current_ask", "start_ask_turn"]
```

- [ ] **Step 4: Bind it in the turn spine**

At module scope in `raven/rpc/spine.py`:

```python
class _AskViaTool:
    """Adapts `AskUserTool.ask_direct` to the `Asker` protocol.

    Bound per turn but resolved per question, which is what lets a transport
    bind its broker after the tool was registered.
    """

    def __init__(self, tool: AskUserTool) -> None:
        self._tool = tool

    async def ask(self, prompt: str, choices: list[str] | None,
                  conversation_id: str) -> str | None:
        return await self._tool.ask_direct(prompt, choices, conversation_id)
```

In `run()`, immediately after the `start_approval_turn` block:

```python
        # Same rebinding and the same origin gate as the shell approval above: a
        # CRON or otherwise background turn has no reader, and an ACP sub-agent's
        # question there must decline rather than wait on nobody.
        ask_tool = tools.get("ask_user") if tools is not None else None
        start_ask_turn(
            _AskViaTool(ask_tool)
            if req.origin is Origin.USER and isinstance(ask_tool, AskUserTool)
            else None,
            conversation_id=cid,
        )
```

- [ ] **Step 5: Run the tests plus the spine's own suite**

Run: `uv run pytest tests/test_acp_elicitation.py -q && uv run pytest tests/ -k spine -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add raven/agent/acp/asker.py raven/rpc/spine.py tests/test_acp_elicitation.py
git commit -m "feat(*): give the acp layer a turn-scoped route to the user, user turns only"
```

---

### Task 6: The elicitor - lock, field loop, attribution, attach lifecycle

**Files:**
- Create: `raven/agent/acp/elicitor.py`
- Modify: `raven/acp_client/acp_dialects/base.py` - the `pair_fields` no-op the elicitor calls
- Modify: `raven/acp_client/acp_agent.py:565-599`
- Modify: `tests/acp_stub_server.py`
- Test: `tests/test_acp_elicitation.py`, `tests/test_subagent_acp.py`

The base `pair_fields` belongs here, not in Task 7: the elicitor calls it, and
Task 7 only adds the `claude_code` override. Landing the hook with its caller is
what keeps both tasks independently green.

**Interfaces:**
- Consumes: `elicitation.*` (Task 3), `_SessionElicitors` (Task 4), `asker.current_ask` (Task 5).
- Produces: `Elicitor(agent: str, instance: str, dialect: Any | None = None)` with `async def elicit(self, params: dict) -> dict`.

- [ ] **Step 1: Write the failing unit tests**

```python
async def test_a_form_is_asked_field_by_field_and_assembled() -> None:
    from raven.agent.acp.asker import start_ask_turn
    from raven.agent.acp.elicitor import Elicitor

    asked: list[str] = []

    class Tool:
        async def ask(self, prompt, choices, conversation_id):
            asked.append(prompt)
            return "redis" if choices else "42"

    start_ask_turn(Tool(), conversation_id="tui:c1")
    got = await Elicitor("Coder", "api-refactor").elicit({
        "sessionId": "s", "mode": "form", "message": "set up a cache",
        "requestedSchema": {"type": "object", "properties": {
            "backend": {"type": "string", "enum": ["redis", "memcached"]},
            "ttl": {"type": "integer"}}}})
    assert got == {"action": "accept", "content": {"backend": "redis", "ttl": 42}}
    assert len(asked) == 2


async def test_the_prompt_names_the_agent_that_asked() -> None:
    from raven.agent.acp.asker import start_ask_turn
    from raven.agent.acp.elicitor import Elicitor

    seen: list[str] = []

    class Tool:
        async def ask(self, prompt, choices, conversation_id):
            seen.append(prompt)
            return "x"

    start_ask_turn(Tool(), conversation_id="tui:c1")
    await Elicitor("Coder", "api-refactor").elicit({
        "sessionId": "s", "mode": "form", "message": "which backend?",
        "requestedSchema": {"type": "object", "properties": {"b": {"type": "string"}}}})
    assert seen[0].startswith("Coder(api-refactor): ")
    assert "which backend?" in seen[0]


async def test_no_asker_declines() -> None:
    from raven.agent.acp.asker import start_ask_turn
    from raven.agent.acp.elicitor import Elicitor

    start_ask_turn(None, conversation_id="")
    got = await Elicitor("Coder", "h").elicit({
        "sessionId": "s", "mode": "form", "message": "m",
        "requestedSchema": {"type": "object", "properties": {"b": {"type": "string"}}}})
    assert got == {"action": "decline"}


async def test_a_skipped_optional_field_is_omitted_and_a_required_one_declines() -> None:
    from raven.agent.acp.asker import start_ask_turn
    from raven.agent.acp.elicitor import Elicitor

    class Silent:
        async def ask(self, prompt, choices, conversation_id):
            return ""

    start_ask_turn(Silent(), conversation_id="tui:c1")
    optional = await Elicitor("A", "h").elicit({
        "sessionId": "s", "mode": "form", "message": "m",
        "requestedSchema": {"type": "object", "properties": {"b": {"type": "string"}}}})
    assert optional == {"action": "accept", "content": {}}

    required = await Elicitor("A", "h").elicit({
        "sessionId": "s", "mode": "form", "message": "m",
        "requestedSchema": {"type": "object", "required": ["b"],
                            "properties": {"b": {"type": "string"}}}})
    assert required == {"action": "decline"}


async def test_a_bad_answer_is_re_asked_then_declines() -> None:
    from raven.agent.acp.asker import start_ask_turn
    from raven.agent.acp.elicitation import MAX_FIELD_RETRIES
    from raven.agent.acp.elicitor import Elicitor

    tries = 0

    class Wrong:
        async def ask(self, prompt, choices, conversation_id):
            nonlocal tries
            tries += 1
            return "not-a-number"

    start_ask_turn(Wrong(), conversation_id="tui:c1")
    got = await Elicitor("A", "h").elicit({
        "sessionId": "s", "mode": "form", "message": "m",
        "requestedSchema": {"type": "object", "required": ["n"],
                            "properties": {"n": {"type": "integer"}}}})
    assert got == {"action": "decline"}
    assert tries == MAX_FIELD_RETRIES + 1


async def test_an_unavailable_round_trip_declines_rather_than_accepting_nothing() -> None:
    """`ask_direct` returns None when there is no broker or no conversation.

    Nothing was put to anybody, so an `accept` with empty content would tell the
    agent a human chose to answer nothing. Distinct from a skip, which is a
    decision the user actually made.
    """
    from raven.agent.acp.asker import start_ask_turn
    from raven.agent.acp.elicitor import Elicitor

    class NoPath:
        async def ask(self, prompt, choices, conversation_id):
            return None

    start_ask_turn(NoPath(), conversation_id="tui:c1")
    got = await Elicitor("A", "h").elicit({
        "sessionId": "s", "mode": "form", "message": "m",
        "requestedSchema": {"type": "object", "properties": {"b": {"type": "string"}}}})
    assert got == {"action": "decline"}


async def test_a_cancelled_turn_answers_cancel_not_decline() -> None:
    """`cancel` aborts the agent's tool call, which is what a cancelled turn is.

    Answered rather than propagated: an unanswered request leaves the agent's
    turn pending for the life of the session.
    """
    import asyncio

    from raven.agent.acp.asker import start_ask_turn
    from raven.agent.acp.elicitor import Elicitor

    class Cancels:
        async def ask(self, prompt, choices, conversation_id):
            raise asyncio.CancelledError

    start_ask_turn(Cancels(), conversation_id="tui:c1")
    got = await Elicitor("A", "h").elicit({
        "sessionId": "s", "mode": "form", "message": "m",
        "requestedSchema": {"type": "object", "properties": {"b": {"type": "string"}}}})
    assert got == {"action": "cancel"}


async def test_url_and_unknown_modes_decline() -> None:
    from raven.agent.acp.elicitor import Elicitor

    for params in ({"sessionId": "s", "mode": "url", "url": "https://x",
                    "elicitationId": "e", "message": "m"},
                   {"sessionId": "s", "mode": "_weird", "message": "m"}):
        assert await Elicitor("A", "h").elicit(params) == {"action": "decline"}


async def test_two_forms_on_one_conversation_are_serialised() -> None:
    """Without the lock the broker fail-safes the stale question to its default,
    so one agent's question is silently dropped and never seen by anyone."""
    import asyncio

    from raven.agent.acp.asker import start_ask_turn
    from raven.agent.acp.elicitor import Elicitor

    inflight = peak = 0

    class Slow:
        async def ask(self, prompt, choices, conversation_id):
            nonlocal inflight, peak
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0.05)
            inflight -= 1
            return "x"

    start_ask_turn(Slow(), conversation_id="tui:c1")
    form = {"sessionId": "s", "mode": "form", "message": "m",
            "requestedSchema": {"type": "object", "properties": {"b": {"type": "string"}}}}
    both = await asyncio.gather(Elicitor("A", "h1").elicit(dict(form)),
                                Elicitor("B", "h2").elicit(dict(form)))
    assert peak == 1
    assert all(r == {"action": "accept", "content": {"b": "x"}} for r in both)
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_acp_elicitation.py -q`

Expected: FAIL - `ModuleNotFoundError: No module named 'raven.agent.acp.elicitor'`

- [ ] **Step 3: Write the elicitor**

```python
"""Turns one `elicitation/create` into questions a human answers, and back.

Separate from `raven.agent.acp.elicitation` on purpose: that module decides what a
schema means and is pure, this one holds the awaits -- the broker round trip, the
per-conversation lock -- and is the only part that needs a running loop.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from typing import Any

from loguru import logger

from raven.agent.acp import elicitation
from raven.agent.acp.asker import current_ask

_LOCKS: dict[str, asyncio.Lock] = {}


def _lock_for(conversation_id: str) -> asyncio.Lock:
    """One lock per conversation, held for a whole form.

    Per conversation because that is the broker's key: it allows one pending
    question per conversation and fail-safes an overlapping one to its default,
    which here would read as "the user skipped" and silently lose a question
    nobody ever saw. Held for the whole form so one agent's multi-field form is
    never interleaved with another's.
    """
    lock = _LOCKS.get(conversation_id)
    if lock is None:
        lock = asyncio.Lock()
        _LOCKS[conversation_id] = lock
    return lock


class Elicitor:
    """One run's answer to its agent's questions."""

    def __init__(self, agent: str, instance: str, dialect: Any | None = None) -> None:
        self._agent = agent
        self._instance = instance
        self._dialect = dialect

    def _prefix(self, message: str) -> str:
        # A bare separator, not a phrase: there is no backend i18n for
        # user-facing strings and both frontends are bilingual, so any wording
        # here would hardcode one language into them.
        who = f"{self._agent}({self._instance})" if self._instance else self._agent
        return f"{who}: {message}"

    async def elicit(self, params: dict[str, Any]) -> dict[str, Any]:
        try:
            return await self._elicit(params)
        except asyncio.CancelledError:
            # The turn was aborted, which is what `cancel` means. Answered rather
            # than propagated: an unanswered request leaves the agent's turn
            # pending for the life of the session.
            return elicitation.cancel()
        except Exception as exc:  # noqa: BLE001 - a declared capability must answer
            logger.warning("acp agent {!r}: elicitation failed, declining: {}", self._agent, exc)
            return elicitation.decline()

    async def _elicit(self, params: dict[str, Any]) -> dict[str, Any]:
        ask = elicitation.parse_request(params)
        if ask is None or ask.mode != "form":
            # `url` is never advertised, and an unknown mode must not be rendered
            # as a known one, so neither is answerable here.
            return elicitation.decline()
        fields = elicitation.fields(ask.schema)
        if self._dialect is not None:
            fields = self._dialect.pair_fields(fields)
        if not fields:
            return elicitation.decline()
        asker, conversation_id = current_ask()
        if asker is None or not conversation_id:
            return elicitation.decline()

        async with _lock_for(conversation_id):
            content: dict[str, Any] = {}
            for field in fields:
                status, value = await self._one(asker, conversation_id, ask, field)
                if status in ("unavailable", "invalid"):
                    # `unavailable`: no round trip happened, so nothing was put
                    # to anybody and accepting content the user never saw would
                    # be a lie. `invalid`: the answers never fitted the schema,
                    # and content that does not match it is not acceptable
                    # either. Both decline the whole form.
                    return elicitation.decline()
                if status == "skip":
                    if field.required:
                        return elicitation.decline()
                    continue
                # A typed answer that is not one of the offered options is the
                # user using the "Other" box, which the adapter reads from the
                # paired property rather than from this one.
                if field.custom_name and field.options and value not in field.options:
                    content[field.custom_name] = value
                else:
                    content[field.name] = value
            return elicitation.accept(content)

    async def _one(self, asker: Any, conversation_id: str, ask: elicitation.Ask,
                   field: elicitation.Field) -> tuple[str, Any]:
        """One field's value. Status is `ok`, `skip`, `invalid`, or `unavailable`.

        Four rather than a boolean because the spec answers each differently and
        they are genuinely different facts: a skip is the user's decision about
        an optional field, `invalid` is an answer that never fit its schema,
        and `unavailable` means the round trip could not happen at all.
        """
        prompt = self._prefix(
            ask.message if field.prompt == ask.message else f"{ask.message} - {field.prompt}"
        )
        # A paired field accepts an off-enum answer, because that is what its
        # free-text sibling is for; the enum check would reject it.
        probe = replace(field, options=[]) if field.custom_name else field
        for _ in range(elicitation.MAX_FIELD_RETRIES + 1):
            answer = await asker.ask(prompt, field.options or None, conversation_id)
            if answer is None:
                # `ask_direct`'s "structurally unavailable": no broker, or no
                # conversation. Nothing was put to anybody.
                return ("unavailable", None)
            if not answer.strip():
                # The broker's default on timeout or EOF, and also what the
                # sheet's close button sends. A decision, not an invalid answer
                # to re-ask.
                return ("skip", None)
            ok, value = elicitation.coerce(probe, answer)
            if ok:
                return ("ok", value)
        # Out of retries. Not a skip: the user did answer, and no answer fitted,
        # so there is no content for this field and none can be invented.
        return ("invalid", None)


__all__ = ["Elicitor"]
```

Then add the no-op hook the elicitor calls, on `AcpDialect` in
`raven/acp_client/acp_dialects/base.py`:

```python
    def pair_fields(self, fields: list[Any]) -> list[Any]:
        """Merge properties an adapter emits as one question. The spec pairs none."""
        return fields
```

- [ ] **Step 4: Attach it in the backend**

In `raven/acp_client/acp_agent.py`, beside the collector at `:565`:

```python
            # Built here, in the turn's context, for the reason `_TurnCollector`
            # documents: the read loop's ContextVars predate this run, and the
            # asker is bound per turn.
            elicitor = Elicitor(self.name, handle, dialect_for(connection.initialize))
```

Inside the `session_lock` block, beside the two router calls:

```python
                connection.router.attach(session_id, collector)
                connection.elicitors.attach(session_id, elicitor)
```

```python
                finally:
                    connection.router.detach(session_id, collector)
                    connection.elicitors.detach(session_id, elicitor)
```

- [ ] **Step 5: Add a stub mode that waits for the answer, and the end-to-end test**

In `tests/acp_stub_server.py`, mirroring the existing `_AWAITING_PERMISSION` pattern:

```python
# Elicitations held open until raven answers (elicits_and_waits).
_AWAITING_ELICITATION: list = []
```

```python
# in handle_response, before the permission branch
    if _AWAITING_ELICITATION:
        content = ((frame.get("result") or {}).get("content")) or {}
        picked = content.get("backend") or "no-answer"
        request_id, session_id = _AWAITING_ELICITATION.pop(0)
        update(session_id, {"sessionUpdate": "agent_message_chunk",
                            "content": {"type": "text", "text": f"using:{picked}"}})
        ok(request_id, {"stopReason": "end_turn"})
        return
```

```python
# in handle_prompt
    if MODE == "elicits_and_waits":
        _AWAITING_ELICITATION.append((request_id, session_id))
        send({"jsonrpc": "2.0", "id": 9002, "method": "elicitation/create",
              "params": {"sessionId": session_id, "mode": "form",
                         "message": "which backend?",
                         "requestedSchema": {"type": "object", "properties": {
                             "backend": {"type": "string",
                                         "enum": ["redis", "memcached"]}}}}})
        return
```

```python
# tests/test_subagent_acp.py
async def test_a_dispatched_acp_run_answers_its_agents_question(tmp_path: Path) -> None:
    """End to end: the agent asks mid-turn, the user answers, the agent uses it."""
    from raven.agent.acp.asker import start_ask_turn

    seen: list[str] = []

    class Tool:
        async def ask(self, prompt, choices, conversation_id):
            seen.append(prompt)
            return "redis"

    start_ask_turn(Tool(), conversation_id="tui:c1")
    cfg = stub_config("a", mode="elicits_and_waits")
    backend = AcpAgentBackend(name="a", command=cfg.command, env=dict(cfg.env),
                              snapshot=_snapshot("a", cfg, can_resume=False), registry=None)
    reply = await backend.run("hi", task_id="t1", workspace=tmp_path,
                              session_key="s", executor=None)
    assert "using:redis" in reply
    assert seen and seen[0].startswith("a(t1): ")
```

Run: `uv run pytest tests/test_acp_elicitation.py tests/test_subagent_acp.py -q`

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add raven/agent/acp/elicitor.py raven/acp_client/acp_agent.py \
        tests/acp_stub_server.py tests/test_acp_elicitation.py tests/test_subagent_acp.py
git commit -m "feat(agent): ask the user an acp sub-agent's question, one form at a time"
```

---

### Task 7: The claude_code pair rule

Without it, one `AskUserQuestion` asking two questions becomes four prompts: the adapter emits `question_<n>` plus an optional free-text `question_<n>_custom` per question (its `dist/elicitation.d.ts` documents this as mirroring the CLI's per-question "Other" box), and the clarify sheet already *is* choices plus a free-text box.

**Files:**
- Modify: `raven/acp_client/acp_dialects/claude_code.py`
- Test: `tests/test_acp_dialects.py`

**Interfaces:**
- Consumes: `elicitation.Field` (Task 3), which already carries `custom_name`; `AcpDialect.pair_fields` (Task 6), which returns its input unchanged.
- Produces: `ClaudeCodeDialect.pair_fields` merges `X` + `X_custom`, setting `.custom_name` on the survivor. Nothing else changes: `Elicitor` already calls `pair_fields` and already honours `custom_name`.

- [ ] **Step 1: Write the failing tests**

```python
def test_claude_code_merges_a_question_with_its_custom_box() -> None:
    from raven.agent.acp.elicitation import fields
    from raven.acp_client.acp_dialects.claude_code import ClaudeCodeDialect

    schema = {"type": "object", "properties": {
        "question_0": {"type": "string", "title": "Which backend?",
                       "oneOf": [{"const": "redis"}, {"const": "memcached"}]},
        "question_0_custom": {"type": "string", "title": "Other"},
        "question_1": {"type": "string", "title": "Which TTL?",
                       "oneOf": [{"const": "60"}, {"const": "600"}]},
        "question_1_custom": {"type": "string", "title": "Other"}}}
    merged = ClaudeCodeDialect().pair_fields(fields(schema))
    assert [f.name for f in merged] == ["question_0", "question_1"]
    assert [f.custom_name for f in merged] == ["question_0_custom", "question_1_custom"]
    assert merged[0].options == ["redis", "memcached"]


def test_claude_code_leaves_an_unpaired_custom_field_alone() -> None:
    from raven.agent.acp.elicitation import fields
    from raven.acp_client.acp_dialects.claude_code import ClaudeCodeDialect

    schema = {"type": "object", "properties": {"notes_custom": {"type": "string"}}}
    merged = ClaudeCodeDialect().pair_fields(fields(schema))
    assert [f.name for f in merged] == ["notes_custom"]
    assert merged[0].custom_name is None


def test_the_default_dialect_pairs_nothing() -> None:
    from raven.agent.acp.elicitation import fields
    from raven.acp_client.acp_dialects.base import AcpDialect

    schema = {"type": "object", "properties": {
        "a": {"type": "string", "enum": ["x"]}, "a_custom": {"type": "string"}}}
    assert [f.name for f in AcpDialect().pair_fields(fields(schema))] == ["a", "a_custom"]
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_acp_dialects.py -k pair -q`

Expected: FAIL - `AttributeError: 'AcpDialect' object has no attribute 'pair_fields'`

- [ ] **Step 3: Override `pair_fields` in `claude_code`**

```python
    def pair_fields(self, fields: list[Any]) -> list[Any]:
        """Fold `X_custom` into `X`: one question, choices plus a free-text box.

        The adapter renders each `AskUserQuestion` as an enum property plus an
        optional free-text sibling, mirroring the CLI's per-question "Other" box.
        Asked separately they are two prompts for what the user experiences as one
        question -- and the clarify sheet already offers exactly this shape.
        """
        by_name = {f.name: f for f in fields}
        merged: list[Any] = []
        folded: set[str] = set()
        for field in fields:
            if field.name in folded:
                continue
            custom = by_name.get(f"{field.name}_custom")
            if (custom is not None and field.options and custom.type == "string"
                    and not custom.options):
                folded.add(custom.name)
                field.custom_name = custom.name
            merged.append(field)
        return merged
```

- [ ] **Step 4: Run the dialect and elicitation suites**

Run: `uv run pytest tests/test_acp_dialects.py tests/test_acp_elicitation.py -q`

Expected: PASS. `Elicitor` already calls `pair_fields` and already honours `custom_name` (Task 6), so no elicitor change is needed here.

- [ ] **Step 5: Commit**

```bash
git add raven/acp_client/acp_dialects/claude_code.py tests/test_acp_dialects.py
git commit -m "feat(agent): ask a claude-code question and its other box as one prompt"
```

---

### Task 8: Declare the capability

The switch. Everything above must be green first: with the read loop unfixed (Task 2) this line freezes pooled connections, and with no elicitor attached (Task 6) it hands agents a capability that always declines.

**Files:**
- Modify: `raven/agent/acp/protocol.py:28-34`
- Test: `tests/test_acp_elicitation.py`

**Interfaces:**
- Consumes: everything above.
- Produces: `CLIENT_CAPABILITIES["elicitation"] == {"form": {}}`.

- [ ] **Step 1: Write the failing test**

```python
def test_raven_advertises_form_elicitation_and_never_url() -> None:
    """`url` elicitation exists for out-of-band credential and payment flows, so
    advertising it would let a sub-agent send the user to any URL to enter them.
    The two sub-capabilities are independently advertisable."""
    from raven.agent.acp.protocol import CLIENT_CAPABILITIES

    assert CLIENT_CAPABILITIES["elicitation"] == {"form": {}}
    assert "url" not in CLIENT_CAPABILITIES["elicitation"]
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_acp_elicitation.py -k advertises -q`

Expected: FAIL - `KeyError: 'elicitation'`

- [ ] **Step 3: Declare it**

```python
CLIENT_CAPABILITIES: dict[str, Any] = {
    # Declared false because raven does not yet serve these back. Advertising a
    # capability it cannot honour is worse than not having it: the agent would
    # route file access through raven and stall on a method that answers with an
    # error. Flipping either to true is the approval work, not this layer's.
    "fs": {"readTextFile": False, "writeTextFile": False},
    # Form only. `url` elicitation is for out-of-band OAuth, payment and
    # credential collection, so advertising it would let a sub-agent send the
    # user to an arbitrary URL to enter them. The two are independently
    # advertisable, so omitting one is a supported subset rather than a
    # half-honoured capability.
    "elicitation": {"form": {}},
}
```

- [ ] **Step 4: Run the full suite**

Run: `uv run pytest -q`

Expected: 10951 + the new tests passed, 0 failed. The baseline at `2a58b17b` was 10951 passed, 50 skipped, 13 deselected, 0 failed, so any failure here belongs to this branch.

- [ ] **Step 5: Commit**

```bash
git add raven/agent/acp/protocol.py tests/test_acp_elicitation.py
git commit -m "feat(agent): tell an acp agent raven can put a form to the user"
```

---

### Task 9: Live verification against the three agents

The spec's stated risk: that declaring the capability makes each adapter ask has been read from adapter source, not observed. `Coder` additionally *gains* a tool it did not have, so its behaviour changes.

**Files:** none - this task changes no code.

**Interfaces:**
- Consumes: Tasks 1-8.
- Produces: the verification paragraph for the merge-request description.

- [ ] **Step 1: Enable opencode**

It is installed at 1.18.21 but `enabled: false`, and its configured command pins `npx -y opencode-ai@1.18.16 acp`. Enable it through the subagents RPC (the hot-apply path) or restart raven; do not hand-edit the config while a raven is running.

- [ ] **Step 2: Ask each agent something genuinely ambiguous**

For `Coder`, `Writer` and `opencode` in turn, from the TUI:

```
spawn <agent> with: I want to add a cache layer here. Ask me which backend and
what TTL before you change anything - do not guess.
```

Expected per agent: a clarify sheet whose question begins `<Agent>(<handle>): `, answering it lets the run continue, and the agent's reply reflects the answer.

- [ ] **Step 3: Confirm the answer reached the agent, not just the sheet**

```bash
DAY=$(date -u +%Y-%m-%d)
ls -t ~/.raven/logs/acp-frames/$DAY/ | head -3
```

Then, against the newest file:

```bash
grep -c 'elicitation/create' ~/.raven/logs/acp-frames/$DAY/<newest>.jsonl
grep -o '"action": *"[a-z]*"' ~/.raven/logs/acp-frames/$DAY/<newest>.jsonl | sort | uniq -c
```

Expected: at least one `elicitation/create` inbound and one `"action": "accept"` outbound. The journal records both directions and is on by default.

- [ ] **Step 4: Verify the concurrency path**

Run a two-node `run_subagent_dag` where both nodes are told to ask. Expected: the two sheets arrive one after the other, both answerable, neither silently dropped.

- [ ] **Step 5: Record the result**

Write the observed outcome per agent into the merge-request description's verification section - the exact commands and what happened, not a claim that it was checked. Name any agent that never asked.

- [ ] **Step 6: Pre-submit sweep, then stop**

```bash
make lint-python
uv run pytest -q
```

Then run the `mr-review-patterns` pre-submit sweep over `git diff origin/main...HEAD`. Do not push or open the merge request without the user's instruction.

---

## Notes for the executor

- Task order is not a preference. Task 2 before Task 8 is a correctness requirement, and Task 6 before Task 8 is what keeps the declared capability from always declining.
- `tests/test_subagent_acp.py` is where ACP behaviour tests live, including the existing `permission_outcome` ones. Extend it; do not add a parallel file (AGENTS.md 5.4).
- The spec is the argument behind every choice here. Where this plan and the spec disagree, stop and say so rather than picking one.
