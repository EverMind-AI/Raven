# Sub-agent OpenAI-API trace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give an `openai` sub-agent's Instance Log the steps between its prompt
and its answer, in the same rows an `acp` run already leaves.

**Architecture:** One row builder (`turn_rows.py`) owns the row shape; two
transports feed it a neutral event list - the existing ACP collector and a new
OpenAI step reader. Records store each transport's own tool name, and the
mapping into raven's vocabulary moves from write time to the three RPC read
sites, so the wire is byte-identical to today and no front end changes.

**Tech Stack:** Python 3.12+, pydantic v2 (RPC models), pytest, aiohttp
(existing openai backend transport), uv for every command.

**Spec:** `docs/specs/2026-08-21-subagent-openai-trace-design.md`

## Global Constraints

- Every command runs through `uv`: `uv run pytest ...`, never bare `pytest`
  (AGENTS.md 4, 5.4).
- **Do not commit unprompted** (AGENTS.md 3.4). Each task's Commit step gives
  the message to use *when the maintainer authorises it*; a plan is not
  authorisation.
- The branch base is confirmed with the maintainer before any branch is cut
  (AGENTS.md 2.2). Default `main`.
- Commit messages: Conventional Commits, all-English, ASCII-only, header <= 100
  chars, `Co-authored-by: Claude (<actual-session-model-id>) <noreply@anthropic.com>`
  (AGENTS.md 3.1, 3.1.1, 3.3).
- Every new file carries an English module docstring. Inline comments only where
  the logic is non-obvious or a constraint is hidden - if neighbouring lines
  carry no comments, add none (AGENTS.md 1).
- New domain terms are defined in `CONTEXT.md` in the same change (AGENTS.md 6).
- Test file names follow AGENTS.md 5.1; no phase or ticket suffixes.
- Nothing on this path may fail the run it describes: every publish and every
  parse degrades to "fewer rows", never to an exception.

---

## File Structure

**Phase 1 - read-boundary normalization and ACP provenance (MR 1).**

| File | Responsibility |
| --- | --- |
| Create `raven/agent/subagent/tool_vocabulary.py` | The moved tables (`_KIND_TO_TOOL`, claude's `_TOOL_NAMES`, `ARGUMENT_KEY`, `_SUBJECT_KEYS`) and one pure function that normalizes a stored row for the wire. |
| Modify `raven/acp_client/acp_dialects/base.py` | `tool_name` returns the spec `kind` verbatim; `arguments_json` stops renaming the subject. Tables leave. |
| Modify `raven/acp_client/acp_dialects/claude_code.py` | `tool_name` returns `_meta.claudeCode.toolName` verbatim. Table leaves. |
| Modify `raven/rpc/methods/instances.py` | `_log_turns` normalizes each row. Covers the live path too. |
| Modify `raven/rpc/methods/dag.py` | `_with_messages` normalizes before `_map_to_wire`. |
| Modify `raven/rpc/methods/subagent.py` | `subagent.context` normalizes before its mapper. |
| Modify `CONTEXT.md` | Correct the **ACP Dialect** entry. |

**Phase 2 - Turn Rows, the Step Dialect, and the openai wiring (MR 2).**

| File | Responsibility |
| --- | --- |
| Create `raven/agent/subagent/backends/turn_rows.py` | The row shape, and the event constructors that feed it. Knows no transport. |
| Create `raven/agent/subagent/openai_steps.py` | `reasoning_steps` -> turn events, with the thinking accumulator. |
| Modify `raven/acp_client/acp_agent.py` | `_TurnCollector.messages()` becomes an adapter onto `turn_rows.rows()`. |
| Modify `raven/agent/subagent/backends/openai_api.py` | Feed the reader on both response paths; publish transcript and usage. |
| Create `tests/fixtures/mirothinker/` | The captured payloads the reader tests run on. |
| Modify `CONTEXT.md` | Add **Turn Rows** and **Step Dialect**. |

---

# Phase 1: read boundary and ACP provenance

### Task 1: The tool-vocabulary module and its normalizer

**Files:**
- Create: `raven/agent/subagent/tool_vocabulary.py`
- Test: `tests/test_subagent_tool_vocabulary.py`

**Interfaces:**
- Consumes: nothing.
- Produces: `normalize_row(row: dict[str, Any]) -> dict[str, Any]` - returns a
  new row with each `tool_calls[].function.name` mapped into raven's vocabulary
  and the subject promoted onto that tool's own argument key. Rows with no
  `tool_calls` are returned unchanged. Also exports `RAVEN_NAME:
  dict[str, str]`, `ARGUMENT_KEY: dict[str, str]`, `SUBJECT_KEYS: tuple[str, ...]`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_subagent_tool_vocabulary.py
"""The read boundary's name and argument normalization."""

from __future__ import annotations

import json

from raven.agent.subagent.tool_vocabulary import normalize_row


def _call(name: str, arguments: dict) -> dict:
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
        ],
    }


def test_a_claude_tool_name_becomes_ravens() -> None:
    out = normalize_row(_call("Bash", {"command": "ls", "description": "list"}))
    fn = out["tool_calls"][0]["function"]
    assert fn["name"] == "exec"
    assert json.loads(fn["arguments"]) == {"command": "ls", "description": "list"}


def test_an_acp_kind_becomes_ravens_and_the_subject_moves_onto_its_key() -> None:
    """codex-acp sends no tool name, so the stored name is the spec kind.

    ``filePath`` is the adapter's spelling of a subject raven calls ``path``;
    promoting it is what keeps one renderer correct for both.
    """
    out = normalize_row(_call("read", {"filePath": "src/a.py"}))
    fn = out["tool_calls"][0]["function"]
    assert fn["name"] == "read_file"
    assert json.loads(fn["arguments"]) == {"path": "src/a.py"}


def test_an_unmapped_name_and_its_arguments_pass_through() -> None:
    """An openai step type is not in any table and needs no translation."""
    out = normalize_row(_call("fetch_url_content", {"url": "https://example.com"}))
    fn = out["tool_calls"][0]["function"]
    assert fn["name"] == "fetch_url_content"
    assert json.loads(fn["arguments"]) == {"url": "https://example.com"}


def test_a_list_valued_subject_is_left_alone() -> None:
    """``web_search`` is already a raven name, and its payload has no string
    subject to promote. The row keeps the payload exactly as stored."""
    out = normalize_row(_call("web_search", {"search_keywords": ["aiohttp version"]}))
    fn = out["tool_calls"][0]["function"]
    assert fn["name"] == "web_search"
    assert json.loads(fn["arguments"]) == {"search_keywords": ["aiohttp version"]}


def test_the_tools_own_key_beats_the_generic_order() -> None:
    """A `grep` scoped to a directory carries both `pattern` and `path`.

    Scanning the generic order first would report the directory and destroy the
    pattern, which is what the code this replaced did.
    """
    out = normalize_row(_call("Grep", {"pattern": "TODO", "path": "src/", "output_mode": "content"}))
    fn = out["tool_calls"][0]["function"]
    assert fn["name"] == "grep"
    assert json.loads(fn["arguments"]) == {"pattern": "TODO", "path": "src/", "output_mode": "content"}


def test_an_unrelated_field_sharing_the_subjects_value_survives() -> None:
    """Dropping by value deletes a field that merely holds the same string."""
    out = normalize_row(_call("Edit", {"filePath": "src/a.py", "old_string": "src/a.py", "new_string": "lib/a.py"}))
    assert json.loads(out["tool_calls"][0]["function"]["arguments"]) == {
        "path": "src/a.py",
        "old_string": "src/a.py",
        "new_string": "lib/a.py",
    }


def test_a_row_without_calls_is_returned_unchanged() -> None:
    row = {"role": "tool", "tool_call_id": "c1", "content": "done"}
    assert normalize_row(row) == row


def test_the_input_row_is_not_mutated() -> None:
    """The caller's list is the stored transcript; normalizing must copy."""
    row = _call("Bash", {"command": "ls"})
    before = json.loads(json.dumps(row))
    normalize_row(row)
    assert row == before
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_subagent_tool_vocabulary.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'raven.agent.subagent.tool_vocabulary'`

- [ ] **Step 3: Write the module**

```python
# raven/agent/subagent/tool_vocabulary.py
"""Raven's own tool vocabulary, and the read-boundary mapping into it.

A delegated run's record carries the transport's own tool name: presentation is
recoverable from provenance and provenance is not recoverable from
presentation, and the record is what an extractor reads. The mapping into
raven's names therefore happens here, on the way to a client, so the wire keeps
the one vocabulary every renderer's verb table is keyed by.

These tables lived in ``acp_dialects`` and were applied when the record was
written. They are not ACP's -- the main session log stores real raven calls
under the same names -- which is why they sit outside that package now.

Must not be applied inside ``session.py``'s ``_map_to_wire``: that mapper also
serves the main session transcript, whose calls are the host's own and already
raven-named.
"""

from __future__ import annotations

import json
from typing import Any

RAVEN_NAME = {
    # The ACP spec's `kind` enum, which is all codex-acp reports.
    "read": "read_file",
    "edit": "edit_file",
    "delete": "delete_file",
    "move": "move_file",
    "search": "grep",
    "execute": "exec",
    "think": "think",
    "fetch": "web_fetch",
    "switch_mode": "switch_mode",
    # claude-agent-acp's own tool names, which are finer than `kind`: it cannot
    # tell `Glob` from `Grep`, and both are `kind: "search"`.
    "Bash": "exec",
    "BashOutput": "exec",
    "Read": "read_file",
    "Write": "write_file",
    "Edit": "edit_file",
    "NotebookEdit": "edit_file",
    "Glob": "find",
    "Grep": "grep",
    "LS": "list_dir",
    "WebFetch": "web_fetch",
    "WebSearch": "web_search",
    "Task": "spawn",
}

ARGUMENT_KEY = {
    "exec": "command",
    "read_file": "path",
    "write_file": "path",
    "edit_file": "path",
    "list_dir": "path",
    "delete_file": "path",
    "move_file": "path",
    "grep": "pattern",
    "find": "pattern",
    "web_fetch": "url",
    "web_search": "query",
}

SUBJECT_KEYS = ("command", "path", "file_path", "abs_path", "filePath", "pattern", "query", "url", "prompt")


def _promote(arguments: dict[str, Any], key: str | None) -> dict[str, Any]:
    """Move the subject onto ``key``, dropping the adapter's spelling of it.

    Insertion order is load-bearing: a reader that does not know this tool takes
    the first string value it finds, so the subject has to be it.

    The tool's own key is tried before the generic order, and the field the
    subject came from is dropped by key rather than by value. Scanning the
    generic order first picks the wrong field whenever a call carries two
    candidates -- ``grep`` with a ``path`` scope reports the directory it
    searched and loses the pattern -- and dropping by value deletes an unrelated
    field that happens to hold the same string.
    """
    if key is None:
        return arguments
    source = next(
        (k for k in (key, *SUBJECT_KEYS) if isinstance(v := arguments.get(k), str) and v.strip()),
        None,
    )
    if source is None:
        return arguments
    merged: dict[str, Any] = {key: arguments[source].strip()}
    for name, value in arguments.items():
        if name in (key, source):
            continue
        merged[name] = value
    return merged


def normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    """One stored transcript row, named in raven's vocabulary for the wire.

    A copy, never in place: the input is the stored transcript, which a live
    read hands over by reference.
    """
    calls = row.get("tool_calls")
    if not isinstance(calls, list) or not calls:
        return row
    out: list[dict[str, Any]] = []
    for call in calls:
        if not isinstance(call, dict):
            out.append(call)
            continue
        fn = call.get("function") if isinstance(call.get("function"), dict) else {}
        stored = fn.get("name")
        name = RAVEN_NAME.get(stored, stored) if isinstance(stored, str) else stored
        arguments = fn.get("arguments")
        try:
            parsed = json.loads(arguments) if isinstance(arguments, str) else None
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, dict):
            promoted = _promote(parsed, ARGUMENT_KEY.get(name) if isinstance(name, str) else None)
            arguments = json.dumps(promoted, ensure_ascii=False, default=str)
        out.append({**call, "function": {**fn, "name": name, "arguments": arguments}})
    return {**row, "tool_calls": out}


__all__ = ["ARGUMENT_KEY", "RAVEN_NAME", "SUBJECT_KEYS", "normalize_row"]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_subagent_tool_vocabulary.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add raven/agent/subagent/tool_vocabulary.py tests/test_subagent_tool_vocabulary.py
git commit -m "feat(subagent): map a delegated call into raven's vocabulary at read time"
```

---

### Task 2: Apply the normalizer at all three read sites

**Files:**
- Modify: `raven/rpc/methods/instances.py` (in `_log_turns`, around `:225-238`)
- Modify: `raven/rpc/methods/dag.py` (in `_with_messages`, around `:127-139`)
- Modify: `raven/rpc/methods/subagent.py` (around `:412-421`)
- Test: `tests/test_rpc_instances.py`, `tests/test_rpc_dag.py`

**Interfaces:**
- Consumes: `normalize_row` from Task 1.
- Produces: no new symbols. The contract is behavioural: a stored row named
  `Bash` leaves these methods named `exec`.

This task is behaviour-preserving on its own - nothing yet stores a transport
name - and that is the point: it lands the read path before Task 3 changes what
is written.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_rpc_instances.py - append
async def test_instance_history_names_a_stored_call_in_ravens_vocabulary(tmp_path) -> None:
    """The record keeps the transport's name; the wire keeps raven's.

    This is what lets three front ends -- the TUI verb table, webui's
    per-name renderer dispatch, and the served page -- go unchanged while the
    file underneath gains provenance.
    """
    from raven.agent.subagent.instance_log import append_turn
    from raven.rpc.methods.instances import instances_history

    append_turn(
        tmp_path,
        agent="Coder",
        handle="h1",
        session_key="s1",
        prompt="go",
        messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "c1",
                        "type": "function",
                        "function": {"name": "Bash", "arguments": '{"command": "ls"}'},
                    }
                ],
            },
            {"role": "tool", "tool_call_id": "c1", "content": "a.py"},
        ],
        answer="done",
    )

    result = await instances_history(
        {"session_key": "s1", "agent": "Coder", "handle": "h1"},
        agent_loop_factory=_factory_for(tmp_path),
    )

    call = next(t for t in result["turns"] if t.get("tool_calls"))
    assert call["tool_calls"][0]["name"] == "exec"
    assert call["tool_calls"][0]["arguments"] == '{"command": "ls"}'
```

`_factory_for` is the existing helper in this file that returns an
`agent_loop_factory` whose manager resolves `session_dir` to `tmp_path`; reuse
it rather than building a second one.

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_rpc_instances.py -k names_a_stored_call -v`
Expected: FAIL - `assert 'Bash' == 'exec'`

- [ ] **Step 3: Apply the normalizer at the three sites**

In `raven/rpc/methods/instances.py`, inside `_log_turns`'s loop, normalize
before reading the call fields:

```python
    for index, row in enumerate(rows):
        row = normalize_row(row)
        role = row.get("role")
```

with `from raven.agent.subagent.tool_vocabulary import normalize_row` at the top.
`_live_turns` calls `_log_turns`, so the in-flight path is covered by this one
edit - do not add a second call there.

In `raven/rpc/methods/dag.py::_with_messages`, normalize as the rows are
collected:

```python
    stored.extend(normalize_row(turn) for turn in turns)
```

In `raven/rpc/methods/subagent.py`, normalize both the file rows and the live
rows as they are appended:

```python
                if isinstance(entry, dict) and entry.get("role"):
                    stored.append(normalize_row(entry))
                    transcribed = True
```

```python
    if not transcribed and (live := run_activity.live(directory.name)) is not None:
        stored.extend(
            normalize_row(entry) for entry in list(live.transcript)
            if isinstance(entry, dict) and entry.get("role")
        )
```

Do **not** touch `session.py::_map_to_wire`: it also serves the main session
transcript, whose calls are already raven-named.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_rpc_instances.py tests/test_rpc_dag.py tests/test_rpc_session*.py -v`
Expected: PASS, and no regression in the session-transcript tests - which is the
check that the mapper was left alone.

- [ ] **Step 5: Commit**

```bash
git add raven/rpc/methods/instances.py raven/rpc/methods/dag.py raven/rpc/methods/subagent.py tests/test_rpc_instances.py
git commit -m "feat(rpc): normalize a delegated call's tool name on the way out"
```

---

### Task 3: ACP dialects store the transport's own name

**Files:**
- Modify: `raven/acp_client/acp_dialects/base.py:34-62` (tables out), `:196-202` (`tool_name`), `:118-147` (`arguments_json`)
- Modify: `raven/acp_client/acp_dialects/claude_code.py:26-38` (table out), `:55-59` (`tool_name`)
- Modify: `raven/acp_client/acp_dialects/__init__.py:18,45,264` (drop the `ARGUMENT_KEY` re-export)
- Test: `tests/test_acp_dialects.py`, `tests/test_subagent_acp.py:405-427`

**Interfaces:**
- Consumes: Task 2's read path.
- Produces: `AcpDialect.tool_name(update)` returns the transport's own name -
  `_meta.claudeCode.toolName` where the adapter sends one, else the spec `kind`
  verbatim, else `"tool_call"`. `ToolCall.arguments_json()` returns the
  adapter's `rawInput` with no key renamed.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_acp_dialects.py - append
def test_the_base_dialect_reports_the_specs_own_kind() -> None:
    """codex-acp sends no tool name of its own, so `kind` is the finest the
    transport offers. Stored verbatim; the read boundary maps it."""
    from raven.acp_client.acp_dialects.base import AcpDialect

    assert AcpDialect().tool_name({"kind": "execute"}) == "execute"
    assert AcpDialect().tool_name({"kind": "read"}) == "read"


def test_a_kindless_update_is_the_no_name_case() -> None:
    from raven.acp_client.acp_dialects.base import AcpDialect

    assert AcpDialect().tool_name({}) == "tool_call"


def test_the_claude_dialect_reports_claudes_own_tool_name() -> None:
    from raven.acp_client.acp_dialects.claude_code import ClaudeCodeDialect

    update = {"kind": "search", "_meta": {"claudeCode": {"toolName": "Glob"}}}
    assert ClaudeCodeDialect().tool_name(update) == "Glob"


def test_arguments_keep_the_adapters_own_spelling() -> None:
    """No key is renamed at write time any more."""
    from raven.acp_client.acp_dialects.base import AcpDialect
    import json

    call = AcpDialect().call({"toolCallId": "t1", "kind": "read", "rawInput": {"filePath": "src/a.py"}})
    assert json.loads(call.arguments_json()) == {"filePath": "src/a.py"}
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_acp_dialects.py -k "specs_own_kind or claudes_own_tool_name or adapters_own_spelling" -v`
Expected: FAIL - `assert 'exec' == 'execute'`, `assert 'find' == 'Glob'`,
`assert {'path': 'src/a.py'} == {'filePath': 'src/a.py'}`

- [ ] **Step 3: Make the dialects report the transport's name**

In `base.py`, delete `_KIND_TO_TOOL` and `ARGUMENT_KEY` (they now live in
`tool_vocabulary.py`) and delete `_SUBJECT_KEYS` too, importing the one
definition instead - `argument()` still needs it for the subject a span label
shows, and two copies of one tuple in two modules is the drift this move was
supposed to prevent:

```python
from raven.agent.subagent.tool_vocabulary import SUBJECT_KEYS
```

Update `argument()`'s loop and `_SUBJECT_KEYS`'s other readers in this file to
the imported name. Then reduce `tool_name`:

```python
    def tool_name(self, update: dict[str, Any]) -> str:
        """The transport's own name for the call, at the finest grain it gives.

        The spec's `kind` verbatim: mapping it into raven's vocabulary is the
        read boundary's job (`raven/agent/subagent/tool_vocabulary.py`), because
        a record that renamed it could never be read back for what the agent
        actually ran.
        """
        kind = update.get("kind")
        return kind if isinstance(kind, str) and kind else _FALLBACK_TOOL
```

and reduce `arguments_json` to the adapter's own fields:

```python
    def arguments_json(self) -> str:
        """The call's arguments, in the adapter's own spelling.

        Renaming the subject onto raven's key moved to the read boundary along
        with the tool name: the two shared one lookup, so they had to move
        together or a row would be named one way and keyed the other.
        """
        merged = {k: v for k, v in self.raw_input.items() if v not in (None, "", {}, [])}
        if not merged and self.subject:
            merged["argument"] = self.subject
        return json.dumps(merged, ensure_ascii=False, default=str)
```

In `claude_code.py`, delete `_TOOL_NAMES` and return the adapter's name:

```python
    def tool_name(self, update: dict[str, Any]) -> str:
        named = _dict(_dict(update.get("_meta")).get("claudeCode")).get("toolName")
        if isinstance(named, str) and named:
            return named
        return super().tool_name(update)
```

Update the module docstring's "of the table is Claude Code's published tool set"
paragraph: there is no table here now, and an unrecognised name no longer
degrades to the `kind` mapping - it is simply reported.

In `__init__.py`, drop `ARGUMENT_KEY` from the import, the `__all__` and the
re-export list.

- [ ] **Step 4: Update the assertion that pinned the old behaviour**

`tests/test_subagent_acp.py:405-427` asserts the *record*. Change the stored
name and add the wire's, so both halves of the contract are pinned:

```python
    assert call["tool_calls"][0]["function"]["name"] == "read", "the record keeps the transport's own name"
    assert json.loads(call["tool_calls"][0]["function"]["arguments"]) == {"filePath": "src/a.py"}
```

and update its docstring: the call is named by the transport, and the read
boundary is what lets one renderer choose a verb for it.

Check the stub's frame while doing this - `tests/acp_stub_server.py` decides
whether the update carries `kind`, `rawInput`, or a `_meta` name, and the
assertion has to match what it actually sends.

- [ ] **Step 5: Run the whole ACP suite**

Run: `uv run pytest tests/test_acp_dialects.py tests/test_subagent_acp.py -v`
Expected: PASS. Any other failure here is a place that read a raven name off a
record - fix it by reading the wire instead, or by calling `normalize_row`.

- [ ] **Step 6: Commit**

```bash
git add raven/acp_client/acp_dialects/ tests/test_acp_dialects.py tests/test_subagent_acp.py
git commit -m "feat(subagents): record the transport's own tool name, not raven's"
```

---

### Task 4: Correct the ACP Dialect term

**Files:**
- Modify: `CONTEXT.md` (the **ACP Dialect** entry, around `:884-900`)

- [ ] **Step 1: Rewrite the entry's first sentence**

It currently claims the dialect produces "a raven tool name (`exec`,
`read_file`, ...)". Replace with: it produces the adapter's own name at the
finest grain the transport gives - `_meta.claudeCode.toolName` where the adapter
sends one, the spec's `kind` otherwise - and the mapping into raven's vocabulary
happens at the read boundary (`raven/agent/subagent/tool_vocabulary.py`). Keep
the rest of the entry, including the `_Avoid_` on reading `title` as the tool
name, and keep the paragraph on per-adapter result unwrapping - that part did
not move.

- [ ] **Step 2: Define the Tool Vocabulary term**

**Tool Vocabulary** (`raven/agent/subagent/tool_vocabulary.py`): raven's own
tool names (`exec`, `read_file`, ...), and the mapping into them applied when a
delegated run's rows go on the wire. A record carries the transport's name
because provenance is not recoverable from presentation; the wire carries
raven's because every renderer's verb table is keyed by it, and the main session
log stores the host's own calls under those same names. Not applied inside
`_map_to_wire`, which also serves that session log.
_Avoid_: applying it at write time - that is what this replaced.

This is the term for the module Task 1 created. It belongs in this MR, not the
next one: AGENTS.md 6 requires a new domain term to be defined in the same
change as the code that coins it.

- [ ] **Step 3: Check the comment that states the same invariant**

`ui-tui/src/domain/directEpisodes.ts:15-19` says the runtime hands over calls
already named in raven's vocabulary and points at `acp_dialects/` for the
parsing that gets them there. Still true of the *wire*, wrong about *where*:
repoint it at `raven/agent/subagent/tool_vocabulary.py`. No code changes.

- [ ] **Step 4: Verify no other doc repeats the old claim**

Run: `grep -rn "raven's own vocabulary\|raven tool name" --include=*.md --include=*.ts --include=*.py . | grep -v node_modules`
Expected: every hit either points at `tool_vocabulary.py` or is describing the
wire, not the record.

- [ ] **Step 5: Commit**

```bash
git add CONTEXT.md ui-tui/src/domain/directEpisodes.ts
git commit -m "docs(subagent): acp dialect reports the adapter's name, not raven's"
```

---

# Phase 2: Turn Rows, the Step Dialect, and the openai wiring

### Task 5: Extract the row builder

**Files:**
- Create: `raven/agent/subagent/backends/turn_rows.py`
- Test: `tests/test_subagent_turn_rows.py`

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `say(text: str) -> dict`
  - `thought(text: str, at: str | None = None) -> dict`
  - `call(*, id: str, name: str, arguments_json: str, at: str | None = None) -> dict`
  - `result(*, id: str, text: str, ok: bool, at: str | None = None) -> dict`
  - `rows(events: list[dict], *, in_flight_answer: str | None = None, in_flight_answer_at: str | None = None) -> list[dict]`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_subagent_turn_rows.py
"""The rows one delegated turn contributes, independent of transport."""

from __future__ import annotations

from raven.agent.subagent.backends import turn_rows as tr


def test_a_call_wears_the_thought_that_preceded_it() -> None:
    rows = tr.rows(
        [
            tr.thought("first I look", at="2026-08-21T10:00:00"),
            tr.call(id="c1", name="Bash", arguments_json='{"command": "ls"}', at="2026-08-21T10:00:01"),
        ]
    )
    assert len(rows) == 1
    assert rows[0]["role"] == "assistant"
    assert rows[0]["reasoning_content"] == "first I look"
    assert rows[0]["tool_calls"][0]["function"] == {"name": "Bash", "arguments": '{"command": "ls"}'}
    assert rows[0]["timestamp"] == "2026-08-21T10:00:00", "the thought's clock opens the row"


def test_narration_lands_on_the_call_it_preceded() -> None:
    rows = tr.rows([tr.say("checking the tree"), tr.call(id="c1", name="Bash", arguments_json="{}")])
    assert rows[0]["content"] == "checking the tree"


def test_a_failed_result_is_prefixed() -> None:
    rows = tr.rows([tr.result(id="c1", text="no such file", ok=False)])
    assert rows[0] == {"role": "tool", "tool_call_id": "c1", "content": "[failed] no such file"}


def test_a_trailing_thought_gets_its_own_row() -> None:
    """A turn that thought after its last call would otherwise lose it."""
    rows = tr.rows([tr.call(id="c1", name="Bash", arguments_json="{}"), tr.thought("now I answer")])
    assert rows[-1] == {"role": "assistant", "content": "", "reasoning_content": "now I answer"}


def test_the_answer_is_not_a_row() -> None:
    """The record keeps the answer and the reader appends it as the closing
    message; a transcript that also carried it would say it twice."""
    rows = tr.rows([tr.call(id="c1", name="Bash", arguments_json="{}")])
    assert all(r.get("content") != "the answer" for r in rows)


def test_an_in_flight_read_appends_the_partial_answer() -> None:
    """A live view has no record to append the answer from."""
    rows = tr.rows(
        [tr.call(id="c1", name="Bash", arguments_json="{}")],
        in_flight_answer="half an ans",
        in_flight_answer_at="2026-08-21T10:00:09",
    )
    assert rows[-1] == {
        "role": "assistant",
        "content": "half an ans",
        "timestamp": "2026-08-21T10:00:09",
    }
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_subagent_turn_rows.py -v`
Expected: FAIL - `ImportError: cannot import name 'turn_rows'`

- [ ] **Step 3: Write the module**

```python
# raven/agent/subagent/backends/turn_rows.py
"""The rows one delegated turn contributes to an instance's conversation.

Owns the row shape and nothing else: an ordered event list in, provider-shaped
message rows out. Two transports produce that list -- the ACP collector reading
`session/update` notifications, and the OpenAI Step Dialect reading a response
field -- and one implementation of the shape is what makes an `openai`
instance's conversation read identically to an `acp` one.

Deliberately ignorant of both. A `call` event carries a tool name and a JSON
string, never an `acp_dialects.ToolCall`: a shared builder that imported one
transport's vocabulary would not be shared.

The final answer is not a row here. The record keeps it and the reader appends
it as the Closing Message; `in_flight_answer` is for a live read, which has no
record to append it from.
"""

from __future__ import annotations

from typing import Any


def say(text: str) -> dict[str, Any]:
    """Something the agent said between steps."""
    return {"t": "say", "text": text}


def thought(text: str, at: str | None = None) -> dict[str, Any]:
    return {"t": "thought", "text": text, "at": at}


def call(*, id: str, name: str, arguments_json: str, at: str | None = None) -> dict[str, Any]:
    return {"t": "call", "id": id, "name": name, "arguments_json": arguments_json, "at": at}


def result(*, id: str, text: str, ok: bool, at: str | None = None) -> dict[str, Any]:
    return {"t": "result", "id": id, "text": text, "ok": ok, "at": at}


def rows(
    events: list[dict[str, Any]],
    *,
    in_flight_answer: str | None = None,
    in_flight_answer_at: str | None = None,
) -> list[dict[str, Any]]:
    """The ordered events as provider-shaped messages.

    One assistant message per tool call, wearing whatever thought preceded it,
    followed by a ``role="tool"`` result matched through the call id -- the exact
    shape ``session.resume`` stores, so a client renders a delegated run with the
    renderer it already has.
    """
    msgs: list[dict[str, Any]] = []
    pending: list[str] = []
    pending_at: str | None = None
    narration: list[str] = []
    for ev in events:
        kind = ev.get("t")
        if kind == "say":
            narration.append(ev.get("text") or "")
        elif kind == "thought":
            if not pending:
                pending_at = ev.get("at")
            pending.append(ev.get("text") or "")
        elif kind == "call":
            entry: dict[str, Any] = {
                "role": "assistant",
                "content": "".join(narration).strip(),
                "tool_calls": [
                    {
                        "id": ev.get("id") or "",
                        "type": "function",
                        "function": {"name": ev.get("name") or "", "arguments": ev.get("arguments_json") or "{}"},
                    }
                ],
            }
            if at := (pending_at or ev.get("at")):
                entry["timestamp"] = at
            if pending:
                entry["reasoning_content"] = "".join(pending)
                pending = []
                pending_at = None
            narration = []
            msgs.append(entry)
        elif kind == "result":
            text = ev.get("text") or ""
            row: dict[str, Any] = {
                "role": "tool",
                "tool_call_id": ev.get("id") or "",
                "content": text if ev.get("ok") else f"[failed] {text}".strip(),
            }
            if at := ev.get("at"):
                row["timestamp"] = at
            msgs.append(row)
    if pending:
        trailing: dict[str, Any] = {"role": "assistant", "content": "", "reasoning_content": "".join(pending)}
        if pending_at:
            trailing["timestamp"] = pending_at
        msgs.append(trailing)
    if in_flight_answer:
        streaming: dict[str, Any] = {"role": "assistant", "content": in_flight_answer}
        if in_flight_answer_at:
            streaming["timestamp"] = in_flight_answer_at
        msgs.append(streaming)
    return msgs


__all__ = ["call", "result", "rows", "say", "thought"]
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_subagent_turn_rows.py -v`
Expected: PASS, 6 tests

- [ ] **Step 5: Commit**

```bash
git add raven/agent/subagent/backends/turn_rows.py tests/test_subagent_turn_rows.py
git commit -m "feat(subagent): give a delegated turn's rows one builder"
```

---

### Task 6: The ACP collector builds its rows through it

**Files:**
- Modify: `raven/acp_client/acp_agent.py:291-355` (`_TurnCollector.messages`)
- Test: `tests/test_subagent_acp.py:432-454`

**Interfaces:**
- Consumes: `turn_rows` from Task 5.
- Produces: no signature change. `messages(in_flight=...)` returns what it
  returns today.

- [ ] **Step 1: Run the existing tests and record the baseline**

Run: `uv run pytest tests/test_subagent_acp.py -v 2>&1 | tail -5`
Expected: PASS. This is a pure refactor - the suite must be as green after as
before, and knowing the count now is how you tell.

- [ ] **Step 2: Replace the body with an adapter onto the builder**

```python
    def messages(self, *, in_flight: bool = False) -> list[dict[str, Any]]:
        """The ordered events as provider-shaped messages.

        The shape itself lives in
        :mod:`raven.agent.subagent.backends.turn_rows`, shared with the OpenAI
        Step Dialect: one implementation is what keeps the two transports'
        conversations readable by one renderer. This maps ACP's events onto that
        vocabulary and nothing else.
        """
        events: list[dict[str, Any]] = []
        for ev in self.events:
            kind = ev["t"]
            if kind == "say":
                events.append(turn_rows.say(ev["text"]))
            elif kind == "thought":
                events.append(turn_rows.thought(ev["text"], at=ev.get("at")))
            elif kind == "call":
                acp_call: ToolCall = ev["call"]
                events.append(
                    turn_rows.call(
                        id=ev["id"],
                        name=acp_call.name,
                        arguments_json=acp_call.arguments_json(),
                        at=ev.get("at"),
                    )
                )
            elif kind == "result":
                events.append(
                    turn_rows.result(id=ev["id"], text=ev["text"], ok=ev["ok"], at=ev.get("at"))
                )
        return turn_rows.rows(
            events,
            in_flight_answer=self.closing_text if in_flight else None,
            in_flight_answer_at=self.answer_at if in_flight else None,
        )
```

Add `from raven.agent.subagent.backends import turn_rows` to the imports.

- [ ] **Step 3: Run the suite and confirm the baseline is unchanged**

Run: `uv run pytest tests/test_subagent_acp.py tests/test_subagent_direct_chat.py -v 2>&1 | tail -5`
Expected: the same pass count as Step 1. A refactor that changes a count changed
behaviour.

- [ ] **Step 4: Point the two shape assertions at the new owner**

`tests/test_subagent_acp.py:432-454` tests the row shape through the collector.
Keep one end-to-end case here - so a break in the ACP lane still fails its own
suite - and note in its docstring that the shape itself is now pinned in
`tests/test_subagent_turn_rows.py`. Do not delete it: an adapter that maps the
wrong event field would pass every unit test in the new file.

- [ ] **Step 5: Commit**

```bash
git add raven/acp_client/acp_agent.py tests/test_subagent_acp.py
git commit -m "refactor(subagent): build acp rows through the shared builder"
```

---

### Task 7: The OpenAI Step Dialect, buffered path

**Files:**
- Create: `raven/agent/subagent/openai_steps.py`
- Create: `tests/fixtures/mirothinker/buffered_research.json`
- Test: `tests/test_subagent_openai_steps.py`

**Interfaces:**
- Consumes: `turn_rows` from Task 5.
- Produces: `OpenAIStepReader` with `feed_steps(steps: list[dict]) -> None`,
  `feed_delta(step: dict) -> None`, `events() -> list[dict]`.

- [ ] **Step 1: Install the captured payload as a fixture**

The buffered response captured against `mirothinker-1-7-deepresearch` on
2026-08-21 is in this session's scratchpad as `r_tool.json` (17 KB, 9 steps:
`thinking` x5, `web_search` x3, `fetch_url_content` x1). Copy it to
`tests/fixtures/mirothinker/buffered_research.json`. If it is gone, re-capture
with a `POST` to `https://api.miromind.ai/v1/chat/completions`, `stream: false`,
one user message asking a question that forces a lookup, using the `apiKey` from
the `openai` entry in `~/.raven/config.json`. Do not commit the key, and do not
put it in the fixture - the response body carries none.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_subagent_openai_steps.py
"""How an OpenAI-compatible endpoint's reasoning_steps become turn events."""

from __future__ import annotations

import json
from pathlib import Path

from raven.agent.subagent.backends import turn_rows
from raven.agent.subagent.openai_steps import OpenAIStepReader

_FIXTURES = Path(__file__).parent / "fixtures" / "mirothinker"


def _buffered_steps() -> list[dict]:
    body = json.loads((_FIXTURES / "buffered_research.json").read_text(encoding="utf-8"))
    return body["choices"][0]["message"]["reasoning_steps"]


def test_a_thinking_step_becomes_a_thought() -> None:
    reader = OpenAIStepReader()
    reader.feed_steps([{"type": "thinking", "thought": "I should look this up"}])
    assert reader.events() == [turn_rows.thought("I should look this up")]


def test_consecutive_thinking_steps_join_into_one_thought() -> None:
    reader = OpenAIStepReader()
    reader.feed_steps([{"type": "thinking", "thought": "a"}, {"type": "thinking", "thought": "b"}])
    assert reader.events() == [turn_rows.thought("ab")]


def test_a_web_search_step_splits_into_a_call_and_its_result() -> None:
    """One step's payload holds both halves, so the halves get their own rows.

    The request key is the endpoint's own (`search_keywords`, not `query`): the
    record says what was sent.
    """
    reader = OpenAIStepReader()
    reader.feed_steps(
        [
            {
                "type": "web_search",
                "web_search": {
                    "search_keywords": ["aiohttp latest stable version"],
                    "search_results": [{"title": "t", "url": "u", "snippet": "s"}],
                },
            }
        ]
    )
    events = reader.events()
    assert [e["t"] for e in events] == ["call", "result"]
    assert events[0]["name"] == "web_search"
    assert json.loads(events[0]["arguments_json"]) == {"search_keywords": ["aiohttp latest stable version"]}
    assert json.loads(events[1]["text"]) == [{"title": "t", "url": "u", "snippet": "s"}]
    assert events[1]["ok"] is True
    assert events[0]["id"] == events[1]["id"], "the two rows pair through this id"


def test_a_fetch_step_is_unwrapped_to_its_extracted_info() -> None:
    """`snippet` is a JSON string inside the payload - the transport's wrapping,
    removed here rather than left for a reader to peel."""
    reader = OpenAIStepReader()
    reader.feed_steps(
        [
            {
                "type": "fetch_url_content",
                "fetch_url_content": {
                    "url": "https://pypi.org/project/aiohttp/",
                    "snippet": json.dumps(
                        {"error": "", "extracted_info": "latest is 3.14.3", "success": True, "tokens_used": 127696}
                    ),
                },
            }
        ]
    )
    events = reader.events()
    assert json.loads(events[0]["arguments_json"]) == {"url": "https://pypi.org/project/aiohttp/"}
    assert events[1]["text"] == "latest is 3.14.3"
    assert events[1]["ok"] is True


def test_a_failed_fetch_reports_not_ok() -> None:
    reader = OpenAIStepReader()
    reader.feed_steps(
        [
            {
                "type": "fetch_url_content",
                "fetch_url_content": {
                    "url": "https://example.com",
                    "snippet": json.dumps({"error": "403", "extracted_info": "", "success": False}),
                },
            }
        ]
    )
    assert reader.events()[1]["ok"] is False


def test_an_unmeasured_type_yields_a_call_and_no_result() -> None:
    """Which key holds the result is not knowable without seeing one, and a
    guessed split would put a guess in an audit record."""
    reader = OpenAIStepReader()
    reader.feed_steps([{"type": "execute_python", "execute_python": {"code": "print(1)"}}])
    events = reader.events()
    assert [e["t"] for e in events] == ["call"]
    assert events[0]["name"] == "execute_python"
    assert json.loads(events[0]["arguments_json"]) == {"code": "print(1)"}


def test_a_malformed_step_is_skipped_rather_than_raised_on() -> None:
    reader = OpenAIStepReader()
    reader.feed_steps([{"no_type": True}, "not a dict", None, {"type": ""}])
    assert reader.events() == []


def test_the_captured_response_produces_the_expected_row_run() -> None:
    """The real 9-step payload: 5 thoughts, 4 actions, 3 of them answered."""
    reader = OpenAIStepReader()
    reader.feed_steps(_buffered_steps())
    events = reader.events()
    assert [e["t"] for e in events].count("call") == 4
    assert [e["t"] for e in events].count("result") == 4
    rows = turn_rows.rows(events)
    assert all(r["role"] in ("assistant", "tool") for r in rows)
    assert any(r.get("reasoning_content") for r in rows), "the thoughts ride on the calls"
```

- [ ] **Step 3: Run the test to verify it fails**

Run: `uv run pytest tests/test_subagent_openai_steps.py -v`
Expected: FAIL - `ModuleNotFoundError: No module named 'raven.agent.subagent.openai_steps'`

- [ ] **Step 4: Write the module**

```python
# raven/agent/subagent/openai_steps.py
"""How one OpenAI-compatible endpoint's ``reasoning_steps`` become turn events.

A deep-research endpoint reports its steps in a response field rather than in a
notification, so this reads the field: the step's own tool name, its payload,
and its result with the transport's wrapping removed. Sibling to
:mod:`raven.acp_client.acp_dialects`, for a transport that has no
notifications to read.

Two response shapes, one accumulator. Buffered, every step arrives whole;
streamed, a ``thinking`` step arrives as token fragments across many frames
while an action step arrives whole in one. Feeding both through
:meth:`OpenAIStepReader.feed_delta` is what makes the two paths produce one
event list -- which is the property the tests pin, and the only reason a live
view and a settled record agree.

Names and argument keys are the endpoint's own. Mapping them into raven's
vocabulary is the read boundary's job
(:mod:`raven.agent.subagent.tool_vocabulary`): presentation is recoverable from
provenance, and provenance is not recoverable from presentation.
"""

from __future__ import annotations

import json
from typing import Any

from loguru import logger

from raven.agent.subagent.backends import turn_rows

_THINKING = "thinking"
_THOUGHT_KEY = "thought"

# Per measured type: which payload key carries the result half, and how to read
# it. A type absent here is reported as a call with no result rather than split
# on a guess -- see the module docstring.
_RESULT_KEY = {
    "web_search": "search_results",
    "fetch_url_content": "snippet",
}


def _text_of(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _unwrap_fetch(snippet: Any) -> tuple[str, bool]:
    """``fetch_url_content``'s result: a JSON string inside the payload.

    Degrades to the raw snippet when it will not parse: a page that came back is
    worth keeping even when its envelope is not the measured one.
    """
    if not isinstance(snippet, str):
        return _text_of(snippet), True
    try:
        inner = json.loads(snippet)
    except ValueError:
        return snippet, True
    if not isinstance(inner, dict):
        return snippet, True
    ok = inner.get("success") is not False and not inner.get("error")
    return _text_of(inner.get("extracted_info") or inner.get("error") or ""), ok


class OpenAIStepReader:
    """One call's ``reasoning_steps``, accumulated into turn events."""

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._thought: list[str] = []
        self._calls = 0

    def feed_steps(self, steps: Any) -> None:
        """The buffered response's whole list, in order."""
        if not isinstance(steps, list):
            return
        for step in steps:
            self.feed_delta(step)

    def feed_delta(self, step: Any) -> None:
        """One step, whole or fragmentary.

        A ``thinking`` fragment appends to the open thought; anything else closes
        it. Nothing here raises: a step the reader cannot make sense of costs its
        own rows, never the turn.
        """
        if not isinstance(step, dict):
            return
        kind = step.get("type")
        if not isinstance(kind, str) or not kind:
            return
        if kind == _THINKING:
            piece = step.get(_THOUGHT_KEY)
            if isinstance(piece, str) and piece:
                self._thought.append(piece)
            return
        self._close_thought()
        payload = step.get(kind)
        try:
            self._append_action(kind, payload)
        except Exception as exc:  # noqa: BLE001 - an audit trail may not break a run
            logger.debug("openai step {!r} could not be read ({})", kind, exc)

    def events(self) -> list[dict[str, Any]]:
        """The events so far, with any open thought closed.

        Non-destructive, because a streamed run publishes on every frame: the
        open thought is appended to the answer rather than consumed, so the next
        frame still extends it.
        """
        out = list(self._events)
        if self._thought:
            out.append(turn_rows.thought("".join(self._thought)))
        return out

    def _close_thought(self) -> None:
        if self._thought:
            self._events.append(turn_rows.thought("".join(self._thought)))
            self._thought = []

    def _append_action(self, kind: str, payload: Any) -> None:
        self._calls += 1
        call_id = f"mi-{self._calls}"
        fields = dict(payload) if isinstance(payload, dict) else {"argument": _text_of(payload)}
        result_key = _RESULT_KEY.get(kind)
        raw_result = fields.pop(result_key, None) if result_key else None
        self._events.append(
            turn_rows.call(
                id=call_id,
                name=kind,
                arguments_json=json.dumps(fields, ensure_ascii=False, default=str),
            )
        )
        if result_key is None:
            return
        text, ok = _unwrap_fetch(raw_result) if kind == "fetch_url_content" else (_text_of(raw_result), True)
        self._events.append(turn_rows.result(id=call_id, text=text, ok=ok))


__all__ = ["OpenAIStepReader"]
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run pytest tests/test_subagent_openai_steps.py -v`
Expected: PASS, 8 tests

- [ ] **Step 6: Commit**

```bash
git add raven/agent/subagent/openai_steps.py tests/test_subagent_openai_steps.py tests/fixtures/mirothinker/
git commit -m "feat(subagent): read an openai endpoint's reasoning steps into turn events"
```

---

### Task 8: The streaming path produces the same events

**Files:**
- Create: `tests/fixtures/mirothinker/stream_research.sse`
- Test: `tests/test_subagent_openai_steps.py` (append)

**Interfaces:**
- Consumes: `OpenAIStepReader` from Task 7. No new symbols - `feed_delta` was
  built for this and needs no change if Task 7 was done right. If it does need
  one, that is the finding this task exists to surface.

- [ ] **Step 1: Install the captured stream as a fixture**

The SSE capture from 2026-08-21 is in this session's scratchpad as
`r_stream.sse` (81 KB, 305 frames: 109 `reasoning_steps` frames of which 106 are
`thinking` fragments, 171 `content` frames, `usage` and `search_results` on the
last frame only). Copy it to
`tests/fixtures/mirothinker/stream_research.sse`. Re-capture as in Task 7 with
`stream: true` if it is gone.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_subagent_openai_steps.py - append

def _stream_steps() -> list[dict]:
    """Every `reasoning_steps` entry from the captured SSE, in frame order."""
    steps: list[dict] = []
    for line in (_FIXTURES / "stream_research.sse").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        body = line[len("data:") :].strip()
        if not body or body == "[DONE]":
            continue
        try:
            frame = json.loads(body)
        except ValueError:
            continue
        delta = (frame.get("choices") or [{}])[0].get("delta") or {}
        steps.extend(s for s in (delta.get("reasoning_steps") or []) if isinstance(s, dict))
    return steps


def test_the_capture_really_is_fragmented() -> None:
    """Guards the fixture, not the code: if a future capture stops fragmenting
    `thinking`, the accumulator below stops being tested by it."""
    steps = _stream_steps()
    thinking = [s for s in steps if s.get("type") == "thinking"]
    assert len(thinking) > 50
    assert min(len(s.get("thought") or "") for s in thinking) < 20


def test_a_fragmented_thought_accumulates_into_one_row() -> None:
    reader = OpenAIStepReader()
    for step in _stream_steps():
        reader.feed_delta(step)
    events = reader.events()
    thoughts = [e for e in events if e["t"] == "thought"]
    assert thoughts, "the stream's thinking fragments produced no thought"
    assert len(thoughts) < 20, "fragments were not joined -- one row per frame"
    assert any(len(t["text"]) > 100 for t in thoughts)


def test_both_response_shapes_produce_the_same_event_kinds() -> None:
    """The core guarantee: a buffered call and a streamed call of the same
    endpoint leave the same *kind* of account, so a live view and the settled
    record cannot disagree in shape.

    Sequences, not counts: the two captures answered different prompts, so the
    number of searches differs. What must match is that each maps to the same
    grammar of thought / call / result.
    """
    buffered = OpenAIStepReader()
    buffered.feed_steps(_buffered_steps())
    streamed = OpenAIStepReader()
    for step in _stream_steps():
        streamed.feed_delta(step)

    for reader in (buffered, streamed):
        events = reader.events()
        kinds = [e["t"] for e in events]
        assert set(kinds) <= {"thought", "call", "result"}
        # every result is immediately preceded by the call it answers
        for index, kind in enumerate(kinds):
            if kind == "result":
                assert kinds[index - 1] == "call"
                assert events[index]["id"] == events[index - 1]["id"]
        rows = turn_rows.rows(events)
        assert rows and all(r["role"] in ("assistant", "tool") for r in rows)


def test_feeding_the_same_reader_twice_does_not_duplicate_an_open_thought() -> None:
    """A streamed run publishes on every frame, so `events()` is called many
    times mid-thought."""
    reader = OpenAIStepReader()
    reader.feed_delta({"type": "thinking", "thought": "ab"})
    first = reader.events()
    second = reader.events()
    assert first == second
    reader.feed_delta({"type": "thinking", "thought": "cd"})
    assert reader.events() == [turn_rows.thought("abcd")]
```

- [ ] **Step 3: Run the tests**

Run: `uv run pytest tests/test_subagent_openai_steps.py -v`
Expected: PASS. If `test_both_response_shapes_produce_the_same_event_kinds`
fails, the two paths diverged - fix `feed_delta`, not the test.

- [ ] **Step 4: Commit**

```bash
git add tests/test_subagent_openai_steps.py tests/fixtures/mirothinker/stream_research.sse
git commit -m "test(subagent): pin one event list for both openai response shapes"
```

---

### Task 9: Wire the reader into the openai backend

**Files:**
- Modify: `raven/agent/subagent/backends/openai_api.py:52-75` (`_post_chat` returns the body already), `:86-138` (`_stream_chat`), `:140-235` (`run`)
- Test: `tests/test_subagent_openai_backend.py` (or the existing openai backend test file if one is present - check `ls tests | grep openai` first and extend it rather than adding a second)

**Interfaces:**
- Consumes: `OpenAIStepReader` from Task 7, `turn_rows.rows` from Task 5.
- Produces: no signature change to `run`. Behaviour: `activity.transcript` is
  populated and `activity.note_usage` is called on both paths.

- [ ] **Step 1: Write the failing test**

```python
async def test_a_buffered_openai_call_publishes_its_steps_and_its_cost(monkeypatch) -> None:
    """The lane could always see the middle -- the endpoint sends it in
    `reasoning_steps` -- and published none of it."""
    from raven.agent.subagent import activity
    from raven.agent.subagent.backends.openai_api import OpenAIApiBackend

    body = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "3.14.3",
                    "reasoning_steps": [
                        {"type": "thinking", "thought": "look it up"},
                        {
                            "type": "web_search",
                            "web_search": {"search_keywords": ["aiohttp"], "search_results": [{"url": "u"}]},
                        },
                    ],
                }
            }
        ],
        "usage": {"prompt_tokens": 10, "completion_tokens": 4},
    }
    backend = OpenAIApiBackend(name="Researcher", base_url="https://x/v1", model="m")

    async def fake_post(self, url, *, json, headers, timeout):
        return body

    monkeypatch.setattr(OpenAIApiBackend, "_post_chat", fake_post)

    with activity.collecting() as did:
        reply = await backend.run("q", task_id="t1", workspace=None, executor=None)

    assert reply == "3.14.3"
    call = next(m for m in did.transcript if m.get("tool_calls"))
    assert call["tool_calls"][0]["function"]["name"] == "web_search"
    assert call["reasoning_content"] == "look it up"
    assert next(m for m in did.transcript if m["role"] == "tool")
    assert all(m.get("content") != "3.14.3" for m in did.transcript), "the answer is the record's, not a row"
    assert did.tokens_in == 10 and did.tokens_out == 4
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_subagent_openai_backend.py -k buffered_openai_call -v`
Expected: FAIL - `StopIteration` on `next(...)`, because `did.transcript` is empty.

- [ ] **Step 3: Publish on the buffered path**

In `run`, after the non-streamed branch has the `message` dict, feed the reader
and publish. Keep the existing `reasoning_content` / `reasoning` fallback for
`content` exactly as it is - a different field, for a different purpose:

```python
            data = await self._post_chat(url, json=body, headers=headers, timeout=client_timeout)
            try:
                message = data["choices"][0]["message"]
            except (KeyError, IndexError, TypeError) as exc:
                raise RuntimeError(f"OpenAI-API agent {self.name!r}: unexpected response shape") from exc
            content = message.get("content")
            if not content:
                content = message.get("reasoning_content") or message.get("reasoning") or ""
            reader.feed_steps(message.get("reasoning_steps"))
            activity.note_usage(data.get("usage"))
```

with `reader = OpenAIStepReader()` created before the branch, and after both
branches:

```python
        activity.note_transcript(turn_rows.rows(reader.events()))
```

Imports: `from raven.agent.subagent import activity`,
`from raven.agent.subagent.backends import turn_rows`,
`from raven.agent.subagent.openai_steps import OpenAIStepReader`.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_subagent_openai_backend.py -k buffered_openai_call -v`
Expected: PASS

- [ ] **Step 5: Write the failing test for the streamed path**

```python
async def test_a_streamed_openai_call_republishes_its_steps_as_they_arrive() -> None:
    """A live view has only the activity to read, so the steps have to land
    before the turn does."""
    seen: list[int] = []

    def note(rows):
        seen.append(len(rows))

    # Feed `_stream_chat` a response whose frames carry reasoning_steps, assert
    # `note_transcript` was called more than once and that the last call carried
    # every step. Build the frames from
    # `tests/fixtures/mirothinker/stream_research.sse` so the shape is the
    # measured one; monkeypatch `activity.note_transcript` with `note`.
```

Write this against whatever fake-response helper the file already uses for
`_stream_chat`; if there is none, add one that yields the fixture's lines from
an object exposing `.content` as an async iterator, matching what
`aiohttp`'s response gives.

- [ ] **Step 6: Publish on the streamed path**

In `_stream_chat`'s frame loop, after the existing `content` and
`reasoning_content` reads, add the steps branch and republish:

```python
                    for step in delta.get("reasoning_steps") or []:
                        reader.feed_delta(step)
                        activity.note_transcript(turn_rows.rows(reader.events()))
```

`_stream_chat` needs the reader passed in - add a keyword parameter rather than
constructing one inside, so `run` owns the per-call reader and both paths
publish from the same object. Read `usage` off the final frame in the same loop
(`if frame.get("usage"): activity.note_usage(frame["usage"])`).

Do **not** add anything for the frame's `search_results` or its
`num_search_queries`. Both are redundant with the rows this task now writes -
the citation list is the union of the per-step `search_results` already in the
result rows, and the count is the number of `web_search` call rows - and
`RunActivity.as_meta` is a closed field set that would need widening for a
rollup nobody needs. Spec section 7 states this.

- [ ] **Step 7: Run the whole openai suite**

Run: `uv run pytest tests/test_subagent_openai_backend.py -v`
Expected: PASS

- [ ] **Step 8: Run the full suite**

Run: `uv run pytest -q 2>&1 | tail -15`
Expected: no new failures. Known pre-existing failures on this box: the
`provider-rates` tests track litellm's live price map and the TUI python tests
fail with `PermissionError` on `main` - neither is yours. Confirm by comparing
against `git stash && uv run pytest -q | tail -3` if unsure.

- [ ] **Step 9: Commit**

```bash
git add raven/agent/subagent/backends/openai_api.py tests/test_subagent_openai_backend.py
git commit -m "feat(subagent): publish an openai call's steps and its token cost"
```

---

### Task 10: Define the two new terms

**Files:**
- Modify: `CONTEXT.md` (the sub-agent cluster, after the **Instance Log** entry)

- [ ] **Step 1: Add Turn Rows**

**Turn Rows** (`raven/agent/subagent/backends/turn_rows.py`): the
provider-shaped message rows one delegated turn contributes to the Instance Log,
built from a transport-neutral event list (`say` / `thought` / `call` /
`result`). Both the ACP collector and the OpenAI Step Dialect produce that list,
which is what makes an `openai` instance's conversation read identically to an
`acp` one - two implementations of one shape would diverge at the first fix
applied to only one. The final answer is not among them: the record keeps it and
the reader appends it as the Closing Message.
_Avoid_: confusing them with **Live rows** - the same shape from a different
source, and only the latter is a snapshot.

- [ ] **Step 2: Add Step Dialect**

**Step Dialect** (`raven/agent/subagent/openai_steps.py`): how one
OpenAI-compatible endpoint's `reasoning_steps` extension is read into Turn Rows
events - the step's own tool name, its own argument keys, and its result with
the transport's wrapping removed (`fetch_url_content` nests its result as a JSON
string). Sibling to **ACP Dialect**, for a transport that reports its steps in a
response field instead of a notification. Buffered and streamed responses differ
in shape - a streamed `thinking` step arrives as token fragments - and one
accumulator serves both, which is what keeps a live view and a settled record
in agreement.
_Avoid_: reading a step type as a raven tool name - it is the endpoint's, and
**Tool Vocabulary** maps it.

- [ ] **Step 3: Check the map**

`CONTEXT-MAP.md`'s "Proposed missing Runtime terms" list is under review and
already names Subagent. Add nothing there: these three are defined, not
proposed.

- [ ] **Step 4: Commit**

```bash
git add CONTEXT.md
git commit -m "docs(subagent): define turn rows and step dialect"
```

---

## Verification before the MRs

- [ ] `uv run pytest -q` - compare the failure list against `main`; only the two
  known pre-existing families may differ.
- [ ] `make check-large-files` - the two fixtures are 17 KB and 81 KB, well
  under the 1 MiB gate, but the command is what AGENTS.md 7 asks for when a
  change adds assets.
- [ ] `grep -rn "ARGUMENT_KEY\|_KIND_TO_TOOL\|_TOOL_NAMES" raven/ tests/` - every
  hit is in `tool_vocabulary.py` or its test.
- [ ] Run the TUI against a real `openai` spawn and open the instance
  conversation: the thoughts and searches draw, and `web_search` rows show a
  verb with no subject - the accepted cost of storing the payload verbatim.
- [ ] `.claude/skills/mr-review-patterns/` pre-submit sweep, per the repo's own
  gate, before either MR is opened.
