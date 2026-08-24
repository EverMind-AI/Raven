# codex-acp tool parsing - implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Name every codex tool row by what codex itself calls it, give each row a subject naming what the call touched, and stop dropping codex's plan.

**Architecture:** `CodexDialect` grows from a two-method patch into the full parser: a discriminator ladder over `kind` + `rawInput` + `_meta` names each call, and a per-name subject rule supplies both the value and the key it is stored under. Three small hooks land on the base dialect so the collector can ask a question it could not ask before (does this frame name the call, does this result reveal a subject, what command did this permission request carry). One generic defect is fixed in `_revise_call`. Codex's names bypass `RAVEN_NAME` untouched, so the TUI gets its own codex verb table to keep tool folding.

**Tech Stack:** Python 3.12+, pytest + pytest-asyncio, uv. TypeScript + vitest for `ui-tui`.

**Spec:** `docs/specs/2026-08-23-codex-acp-tool-parsing-design.md`

**Base:** `feat/subagent_everos_memory_record` at `b7b05841`; `origin/main` at `ffecc51a`.

## Global Constraints

- Measured against `@agentclientprotocol/codex-acp@1.1.14` only. Do not add a row for an adapter behaviour with no captured frame behind it.
- Every test payload is a frame copied verbatim from a capture, matching the rule already stated at the top of `tests/test_acp_dialects.py`. Do not hand-write plausible JSON.
- The record keeps the transport's own vocabulary. Nothing in this change may rename a codex tool into raven's names.
- Comments follow AGENTS.md section 1: English, only where the logic is non-obvious or a constraint is hidden. New modules need a module docstring.
- Run tests with `uv run pytest`, never bare `pytest` (AGENTS.md section 5.4).
- `ui-tui` tests run serially; the suite flakes above ~100 files under default worker parallelism.
- Run `make lint-python` before every commit touching Python. It is the exact gate CI's
  `lint-python` job runs. This repo sets `core.hooksPath` to a nonexistent directory, so the
  `ruff format` pre-commit hook never fires and an unformatted file reaches CI unnoticed.
  Multi-line dict literals in test files are the usual offender.
- Do not commit unless the user asks (AGENTS.md section 3.4). The commit step in each task is the message to use *when* they do.
- Every commit carries the trailer `Co-authored-by: Claude (claude-opus-5[1m]) <noreply@anthropic.com>` (AGENTS.md section 3.3). Task 1's commit predates this constraint and does not have it; do not amend it.

## Discovered during planning, not in the spec

Extracting the fixtures turned up a richer source for Task 5 than the spec's section 4 describes. **This plan is the authority; update the spec's section 4 in Task 5.**

The permission frame's `_meta.codex.params` carries codex's whole `ParsedCommand`, not just a command string:

```json
"_meta": {"codex": {"params": {
  "reason": "Allow the requested read-only command to display calc.py after the sandbox namespace failed?",
  "command": "/bin/bash -lc \"sed -n '1,200p' calc.py\"",
  "commandActions": [{"type": "read", "command": "sed -n '1,200p' calc.py",
                      "name": "calc.py", "path": "/tmp/codexprobe/ws/calc.py"}]}}}
```

Two consequences:

1. `toolCall.rawInput.command` is the *bash argument*, arriving double-quoted: `"\"sed -n '1,200p' calc.py\""`. `commandActions[0].command` is the clean `sed -n '1,200p' calc.py`. Prefer the latter; fall back to the former with surrounding quotes stripped.
2. `commandActions[0].type` is codex's own `ParsedCommand` tag (`read` / `listFiles` / `search` / `unknown`). Task 2 discriminates the same distinction from `kind` + `title` because most calls have no permission frame. Do not rewrite Task 2 around it -- it is a confirmation, not a replacement.

---

### Task 1: Base hooks and the kind-less rename guard

The generic defect, plus the fixtures every later task uses. A `tool_call_update` that carries no `kind` currently renames the call to the fallback `tool_call`; measured on the real session, both web searches lost `kind: "search"` this way.

**Files:**
- Create: `tests/acp_frames.py`
- Modify: `raven/agent/subagent/acp_dialects/base.py`
- Modify: `raven/agent/subagent/backends/acp_agent.py` (in `_revise_call`)
- Test: `tests/test_acp_dialects.py`, `tests/test_subagent_acp.py`

**Interfaces:**
- Produces: `AcpDialect.names_call(update: dict[str, Any]) -> bool`; `AcpDialect.subject_from_result(update: dict[str, Any]) -> str | None`; `AcpDialect.permission_command(params: dict[str, Any]) -> str | None`. Fixture module `tests/acp_frames.py` exporting the frame constants named below.

- [ ] **Step 1: Create the frame fixtures**

Create `tests/acp_frames.py`. Every constant is a frame copied from a capture.

```python
"""Frames copied verbatim from ACP captures, for tests to read instead of invent.

Sources: a codex-acp 1.1.14 direct chat
(`traces/logs/acp-frames/2026-08-23/Writer-092353638638.jsonl`) and a probe run
driving a four-step task to force a plan, a patch and two shell commands. Fields
none of the readers touch are trimmed; nothing is rephrased.
"""

from __future__ import annotations

from typing import Any

CODEX_WEBSEARCH_OPEN: dict[str, Any] = {
    "sessionUpdate": "tool_call",
    "toolCallId": "exec-02ba4d28",
    "kind": "search",
    "title": "Web search",
    "status": "in_progress",
    "rawInput": {"type": "webSearch", "id": "exec-02ba4d28", "query": "", "action": None},
}

CODEX_WEBSEARCH_DONE: dict[str, Any] = {
    "sessionUpdate": "tool_call_update",
    "toolCallId": "exec-02ba4d28",
    "title": "Web search: site:developers.openai.com/codex Codex overview coding agent capabilities",
    "status": "completed",
    "rawInput": {
        "type": "webSearch",
        "id": "exec-02ba4d28",
        "query": "site:developers.openai.com/codex Codex overview coding agent capabilities ...",
        "action": {
            "type": "search",
            "query": None,
            "queries": [
                "site:developers.openai.com/codex Codex overview coding agent capabilities",
                "site:developers.openai.com/codex models GPT-5 Codex",
            ],
        },
    },
}

CODEX_READ_OPEN: dict[str, Any] = {
    "sessionUpdate": "tool_call",
    "toolCallId": "exec-9694bddc",
    "status": "in_progress",
    "kind": "read",
    "title": "Read file '/tmp/codexprobe/ws/calc.py'",
    "locations": [{"path": "/tmp/codexprobe/ws/calc.py"}],
}

CODEX_READ_DONE: dict[str, Any] = {
    "sessionUpdate": "tool_call_update",
    "toolCallId": "exec-9694bddc",
    "status": "completed",
    "rawOutput": {"formatted_output": "def add(a, b):\n    return a - b\n", "exit_code": 0},
}

CODEX_READ_PERMISSION: dict[str, Any] = {
    "sessionId": "01a02e1a",
    "toolCall": {
        "toolCallId": "exec-9694bddc",
        "kind": "execute",
        "status": "pending",
        "rawInput": {"command": "\"sed -n '1,200p' calc.py\"", "cwd": "/tmp/codexprobe/ws"},
    },
    "options": [
        {"optionId": "allow_always", "name": "Allow for Session", "kind": "allow_always"},
        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
    ],
    "_meta": {
        "codex": {
            "params": {
                "command": "/bin/bash -lc \"sed -n '1,200p' calc.py\"",
                "cwd": "/tmp/codexprobe/ws",
                "commandActions": [
                    {
                        "type": "read",
                        "command": "sed -n '1,200p' calc.py",
                        "name": "calc.py",
                        "path": "/tmp/codexprobe/ws/calc.py",
                    }
                ],
            }
        }
    },
}

CODEX_PATCH_OPEN: dict[str, Any] = {
    "sessionUpdate": "tool_call",
    "toolCallId": "exec-9a186209",
    "status": "in_progress",
    "kind": "execute",
    "title": "apply_patch",
    "content": [{"type": "terminal", "terminalId": "exec-9a186209"}],
    "rawInput": {"command": "apply_patch", "cwd": "/tmp/codexprobe/ws"},
    "_meta": {"terminal_info": {"cwd": "/tmp/codexprobe/ws", "terminal_id": "exec-9a186209"}},
}

CODEX_PATCH_DONE: dict[str, Any] = {
    "sessionUpdate": "tool_call_update",
    "toolCallId": "exec-9a186209",
    "status": "completed",
    "rawOutput": {
        "formatted_output": (
            "*** Begin Patch\r\n*** Update File: calc.py\r\n@@\r\n def add(a, b):\r\n"
            "-    return a - b\r\n+    return a + b\r\n*** End Patch\r\n"
            "Success. Updated the following files:\r\nM calc.py\r\n"
        ),
        "exit_code": 0,
    },
    "_meta": {"terminal_exit": {"exit_code": 0, "signal": None, "terminal_id": "exec-9a186209"}},
}

CODEX_EXEC_OPEN: dict[str, Any] = {
    "sessionUpdate": "tool_call",
    "toolCallId": "exec-569fa7ee",
    "status": "in_progress",
    "kind": "execute",
    "title": 'python3 -c "import calc; print(calc.add(2,3))"',
    "rawInput": {"command": 'python3 -c "import calc; print(calc.add(2,3))"', "cwd": "/tmp/codexprobe/ws"},
}

CODEX_PLAN_FIRST: dict[str, Any] = {
    "sessionUpdate": "plan",
    "entries": [
        {"status": "in_progress", "content": "Show the contents of calc.py with a shell command", "priority": "medium"},
        {"status": "pending", "content": "Fix add() using the patch tool", "priority": "medium"},
    ],
}

CODEX_PLAN_SECOND: dict[str, Any] = {
    "sessionUpdate": "plan",
    "entries": [
        {"status": "completed", "content": "Show the contents of calc.py with a shell command", "priority": "medium"},
        {"status": "in_progress", "content": "Fix add() using the patch tool", "priority": "medium"},
    ],
}

# claude-agent-acp 0.66.0. Every one of its 132 captured updates that carried a
# `rawInput` also carried a `kind`, which is why the Task 1 guard cannot change
# how this adapter reads.
CLAUDE_EXEC_UPDATE: dict[str, Any] = {
    "sessionUpdate": "tool_call_update",
    "toolCallId": "toolu_01",
    "kind": "execute",
    "status": "completed",
    "rawInput": {"command": "pwd"},
    "rawOutput": "/root\n",
}
```

- [ ] **Step 2: Write the failing tests**

Append to `tests/test_acp_dialects.py`:

```python
from tests import acp_frames


def test_a_frame_without_a_kind_cannot_name_the_call() -> None:
    """codex does not repeat ``kind`` when it completes a call.

    Re-reading the name from such a frame returned the fallback and overwrote a
    name the opening frame had right.
    """
    spec = AcpDialect()

    assert spec.names_call({"kind": "execute"}) is True
    assert spec.names_call(acp_frames.CLAUDE_EXEC_UPDATE) is True
    assert spec.names_call({"rawInput": {"command": "pwd"}}) is False
    assert spec.names_call({}) is False


def test_a_result_reveals_no_subject_by_default() -> None:
    assert AcpDialect().subject_from_result(acp_frames.CODEX_PATCH_DONE) is None


def test_the_spec_reads_a_permission_command_from_the_tool_call() -> None:
    assert AcpDialect().permission_command(acp_frames.CODEX_READ_PERMISSION) == "\"sed -n '1,200p' calc.py\""
    assert AcpDialect().permission_command({"toolCall": {}}) is None
```

Append to `tests/test_subagent_acp.py`:

```python
import pytest

from raven.agent.subagent.acp_dialects import CodexDialect
from raven.agent.subagent.backends.acp_agent import _TurnCollector
from tests import acp_frames


@pytest.mark.asyncio
async def test_a_completing_frame_does_not_rename_the_call() -> None:
    """Measured on codex: both web searches were renamed to the fallback.

    The completing frame carries `rawInput` and no `kind`, so it counts as a
    revision but must not be read for a name.
    """
    collector = _TurnCollector(dialect=CodexDialect())

    await collector("session/update", {"update": acp_frames.CODEX_WEBSEARCH_OPEN})
    await collector("session/update", {"update": acp_frames.CODEX_WEBSEARCH_DONE})

    assert [c.name for c in collector.calls] == ["search"]
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `uv run pytest tests/test_acp_dialects.py -k "names_call or subject_from_result or permission_command" tests/test_subagent_acp.py -k rename -v`
Expected: FAIL -- `AttributeError: 'AcpDialect' object has no attribute 'names_call'`.

- [ ] **Step 4: Add the three hooks to the base dialect**

In `raven/agent/subagent/acp_dialects/base.py`, add to `AcpDialect` after `result`:

```python
    def names_call(self, update: dict[str, Any]) -> bool:
        """Whether this frame carries enough to name the call it revises.

        A ``tool_call_update`` replaces the fields it carries, and codex-acp
        does not repeat ``kind`` on one. Re-reading the name from such a frame
        returned :data:`_FALLBACK_TOOL` and overwrote a name the opening frame
        had right -- measured, ``kind: "search"`` became ``tool_call``.
        """
        kind = update.get("kind")
        return isinstance(kind, str) and bool(kind)

    def subject_from_result(self, update: dict[str, Any]) -> str | None:
        """A subject that exists only in the completed frame, if this adapter has one."""
        return None

    def permission_command(self, params: dict[str, Any]) -> str | None:
        """The command a ``session/request_permission`` says the call will run.

        The spec puts it on the embedded tool call. Its value here is that an
        adapter may badge a call as something other than what it ran; this is
        the frame that still has the command.
        """
        command = _dict(_dict(params.get("toolCall")).get("rawInput")).get("command")
        return command.strip() if isinstance(command, str) and command.strip() else None
```

- [ ] **Step 5: Guard the name in `_revise_call`**

In `raven/agent/subagent/backends/acp_agent.py`, inside `_revise_call`, change the `ToolCall(...)` construction:

```python
                event["call"] = ToolCall(
                    id=previous.id,
                    # Guarded like the fields below it: a frame that carries no
                    # kind cannot name the call, and reading one anyway is what
                    # renamed codex's searches to the fallback.
                    name=revised.name if self._dialect.names_call(update) else previous.name,
                    argument=revised.argument or previous.argument,
```

and extend the docstring's last paragraph:

```python
        same frame also carries the ``kind`` and ``_meta`` a dialect names the
        tool from -- but only when it does: ``names_call`` is what decides, and
        a frame that answers False leaves the name alone.
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `uv run pytest tests/test_acp_dialects.py tests/test_subagent_acp.py -v`
Expected: PASS, including every pre-existing test.

- [ ] **Step 7: Commit**

```bash
git add tests/acp_frames.py tests/test_acp_dialects.py tests/test_subagent_acp.py \
        raven/agent/subagent/acp_dialects/base.py raven/agent/subagent/backends/acp_agent.py
git commit -m "fix(agent): stop a kind-less acp update from renaming the call" \
  -m "Co-authored-by: Claude (claude-opus-5[1m]) <noreply@anthropic.com>"
```

---

### Task 2: Name every codex tool the way codex does

**Files:**
- Modify: `raven/agent/subagent/acp_dialects/codex.py`
- Modify: `CONTEXT.md`
- Test: `tests/test_acp_dialects.py`

**Interfaces:**
- Consumes: `tests/acp_frames.py` constants; `AcpDialect.names_call` from Task 1.
- Produces: `CodexDialect.tool_name(update) -> str` covering the fifteen-row ladder; `CodexDialect.names_call(update) -> bool`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_acp_dialects.py`:

```python
def test_codex_names_each_tool_the_way_codex_does() -> None:
    """The ACP `kind` enum is five values for eleven codex tools.

    `apply_patch` and an MCP tool keep their model-facing names because codex
    runs them as commands and the command is on the frame; the rest are named
    by codex's own item type.
    """
    codex = CodexDialect()

    assert codex.tool_name(acp_frames.CODEX_PATCH_OPEN) == "apply_patch"
    assert codex.tool_name(acp_frames.CODEX_EXEC_OPEN) == "commandExecution"
    assert codex.tool_name(acp_frames.CODEX_READ_OPEN) == "commandExecution.read"
    assert codex.tool_name(acp_frames.CODEX_WEBSEARCH_OPEN) == "webSearch"
    assert codex.tool_name({"kind": "read", "title": "List files in '/tmp'"}) == "commandExecution.listFiles"
    assert codex.tool_name({"kind": "read", "title": "View Image /tmp/a.png"}) == "imageView"
    assert codex.tool_name({"kind": "search", "title": "Search for 'x'"}) == "commandExecution.search"
    assert codex.tool_name({"kind": "other", "title": "Image generation"}) == "imageGeneration"
    assert codex.tool_name({"kind": "other", "_meta": {"contextCompaction": True}}) == "contextCompaction"
    assert (
        codex.tool_name({"kind": "execute", "_meta": {"is_mcp_tool_call": True},
                         "rawInput": {"server": "fs", "tool": "read", "arguments": {}}})
        == "mcp.fs.read"
    )
    assert (
        codex.tool_name({"kind": "other", "title": "Start subagent docs",
                         "_meta": {"codex": {"subagent": {"path": "a/docs"}}}})
        == "subAgentActivity"
    )
    assert (
        codex.tool_name({"kind": "other", "title": "handoff",
                         "_meta": {"codex": {"collaboration": {"tool": "handoff"}}}})
        == "collabAgentToolCall"
    )
    # A dynamic tool puts its real name in the title and sends no command.
    assert codex.tool_name({"kind": "execute", "title": "my_tool", "rawInput": {"arguments": {"a": 1}}}) == "my_tool"


def test_codex_discriminators_name_a_call_without_a_kind() -> None:
    """The completing web-search frame has no `kind` but says what it is."""
    codex = CodexDialect()

    assert codex.names_call(acp_frames.CODEX_WEBSEARCH_DONE) is True
    assert codex.tool_name(acp_frames.CODEX_WEBSEARCH_DONE) == "webSearch"
    # An MCP completion carries neither, so the opening frame's name must stand.
    assert codex.names_call({"rawInput": {"server": "fs", "tool": "read", "arguments": {}}}) is False
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_acp_dialects.py -k "codex_names_each or codex_discriminators" -v`
Expected: FAIL -- `assert 'execute' == 'apply_patch'`.

- [ ] **Step 3: Implement the ladder**

Replace the body of `raven/agent/subagent/acp_dialects/codex.py`'s class with the version below, keeping the existing `result` and `argument` methods in place for now (Task 3 rewrites `argument`).

Add at module level:

```python
import shlex

# Codex's own item types (`codex-rs/protocol/src/items.rs`), which the adapter
# flattens into five ACP `kind` values on the way out. Recovering them is what
# lets a row say `webSearch` instead of `search`.
_MCP = "mcpToolCall"

# Three item types all arrive as `kind: "read"` and are separated only by the
# title the adapter wrote. Pinned to 1.1.14: a rephrased title degrades to
# `commandExecution.read`, which is the honest reading of a `kind: "read"` with
# no other marker, rather than a wrong one.
_VIEW_IMAGE_TITLE = "View Image"
_LIST_FILES_TITLE = "List files"
_IMAGE_GEN_TITLE = "Image generation"


def _argv0(command: str) -> str:
    """The program a command runs, for the one case where it is the tool's name.

    `apply_patch` is a real command whose argv[0] is literally that
    (`codex-rs/apply-patch/src/invocation.rs`); the patch body arrives on stdin.
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    return parts[0] if parts else ""
```

and the methods:

```python
    def tool_name(self, update: dict[str, Any]) -> str:
        raw = _dict(update.get("rawInput"))
        meta = _dict(update.get("_meta"))
        codex_meta = _dict(meta.get("codex"))
        title = update.get("title") if isinstance(update.get("title"), str) else ""
        kind = update.get("kind")

        command = raw.get("command")
        if isinstance(command, str) and _argv0(command) == "apply_patch":
            return "apply_patch"
        if meta.get("is_mcp_tool_call"):
            server, tool = raw.get("server"), raw.get("tool")
            if isinstance(server, str) and isinstance(tool, str):
                return f"mcp.{server}.{tool}"
            return _MCP
        if kind == "execute" and "arguments" in raw and "command" not in raw:
            # A dynamic tool's real name is the title; the adapter puts it there
            # and nowhere else.
            return title or "dynamicToolCall"
        if raw.get("type") == "webSearch":
            return "webSearch"
        if codex_meta.get("collaboration"):
            return "collabAgentToolCall"
        if codex_meta.get("subagent"):
            return "subAgentActivity"
        if meta.get("contextCompaction"):
            return "contextCompaction"
        if kind == "other" and title.startswith(_IMAGE_GEN_TITLE):
            return "imageGeneration"
        if kind == "read":
            if title.startswith(_VIEW_IMAGE_TITLE):
                return "imageView"
            return "commandExecution.listFiles" if title.startswith(_LIST_FILES_TITLE) else "commandExecution.read"
        if kind == "edit":
            # Unmeasured: neither capture reached this branch, because codex ran
            # `apply_patch` as a command both times. Written from the adapter's
            # `createFileChangeUpdate`; the first real frame is the confirmation.
            return "fileChange"
        if kind == "search":
            return "commandExecution.search"
        if kind == "execute":
            return "commandExecution"
        return super().tool_name(update)

    def names_call(self, update: dict[str, Any]) -> bool:
        """A codex discriminator names the call even when ``kind`` is absent."""
        if super().names_call(update):
            return True
        raw = _dict(update.get("rawInput"))
        meta = _dict(update.get("_meta"))
        codex_meta = _dict(meta.get("codex"))
        return bool(
            raw.get("type") == "webSearch"
            or meta.get("is_mcp_tool_call")
            or codex_meta.get("collaboration")
            or codex_meta.get("subagent")
            or meta.get("contextCompaction")
        )
```

Replace the module docstring's opening paragraph so it describes the parser rather than the old two-method patch:

```python
"""codex-acp: reading eleven codex tools out of five ACP ``kind`` values.

The adapter reports a call three ways -- the spec's ``kind`` enum, a title
written for a human, and discriminators in ``rawInput`` and ``_meta`` that no
other adapter sends -- and none of the three is codex's model-facing tool name.
Two of those names survive anyway, because codex runs those tools as shell
commands and the command string is on the frame: ``apply_patch`` is argv[0] of a
real command, and an MCP call names its server and tool in ``rawInput``.

Measured on v1.1.14. See ``docs/specs/2026-08-23-codex-acp-tool-parsing-design.md``
for the frame captures every row here is read from.
"""
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_acp_dialects.py -v`
Expected: PASS. The pre-existing `test_codex_prefers_the_command_over_the_titles_truncation_of_it` must still pass.

- [ ] **Step 5: Define the new domain term**

In `CONTEXT.md`, in the section that covers ACP terms, add:

```markdown
**Dialect discriminator** -- the field on an ACP frame that identifies which of
one adapter's tools a call is, when the spec's `kind` cannot. codex-acp sends
five `kind` values for eleven tools, and separates them with `rawInput.type`,
`_meta.is_mcp_tool_call`, `_meta.codex.collaboration`, `_meta.codex.subagent`
and `_meta.contextCompaction`. Read by `acp_dialects/codex.py`.
```

- [ ] **Step 6: Commit**

```bash
git add raven/agent/subagent/acp_dialects/codex.py tests/test_acp_dialects.py CONTEXT.md
git commit -m "feat(agent): name each codex tool the way codex names it" \
  -m "Co-authored-by: Claude (claude-opus-5[1m]) <noreply@anthropic.com>"
```

---

### Task 3: The subject each codex row shows, and the key it is stored under

**Files:**
- Modify: `raven/agent/subagent/acp_dialects/codex.py`
- Test: `tests/test_acp_dialects.py`, `tests/test_subagent_tool_vocabulary.py`

**Interfaces:**
- Consumes: `CodexDialect.tool_name` from Task 2.
- Produces: `CodexDialect.subject_field(update) -> tuple[str, str]` returning `(key, value)`; `CodexDialect.call(update) -> ToolCall` with a curated `raw_input` whose first entry is the subject.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_acp_dialects.py`:

```python
def test_codex_stores_each_subject_under_a_key_that_names_it() -> None:
    """The subject must be the first string in the arguments, truthfully keyed.

    A reader that does not know the tool takes the first string value it finds
    (`tool_vocabulary._promote`), so a path stored under `command` is a lie that
    reaches the row.
    """
    codex = CodexDialect()

    assert codex.subject_field(acp_frames.CODEX_EXEC_OPEN) == (
        "command",
        'python3 -c "import calc; print(calc.add(2,3))"',
    )
    # No rawInput on a read: the path is all the frame has until Task 5's
    # permission back-fill supplies the command.
    assert codex.subject_field(acp_frames.CODEX_READ_OPEN) == ("path", "/tmp/codexprobe/ws/calc.py")
    assert codex.subject_field(acp_frames.CODEX_WEBSEARCH_DONE) == (
        "query",
        "site:developers.openai.com/codex Codex overview coding agent capabilities, "
        "site:developers.openai.com/codex models GPT-5 Codex",
    )
    assert codex.subject_field({"kind": "read", "title": "View Image /tmp/a.png",
                                "rawInput": {"path": "/tmp/a.png"}}) == ("path", "/tmp/a.png")


def test_a_codex_call_puts_its_subject_first_in_the_arguments() -> None:
    codex = CodexDialect()

    call = codex.call(acp_frames.CODEX_WEBSEARCH_DONE)
    fields = json.loads(call.arguments_json())

    assert next(iter(fields)) == "query"
    assert fields["query"].startswith("site:developers.openai.com/codex Codex overview")
    # The adapter's own fields survive beside it; the record keeps what it sent.
    assert fields["type"] == "webSearch"
```

Append to `tests/test_subagent_tool_vocabulary.py`:

```python
def test_codex_names_are_not_renamed_at_the_read_boundary() -> None:
    """Codex's vocabulary is not raven's, so nothing in RAVEN_NAME matches it."""
    row = {
        "role": "assistant",
        "tool_calls": [
            {
                "id": "exec-1",
                "type": "function",
                "function": {"name": "commandExecution.read", "arguments": '{"path": "/tmp/a.py"}'},
            }
        ],
    }

    out = normalize_row(row)

    assert out["tool_calls"][0]["function"]["name"] == "commandExecution.read"
    assert json.loads(out["tool_calls"][0]["function"]["arguments"])["path"] == "/tmp/a.py"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_acp_dialects.py -k subject_field tests/test_subagent_tool_vocabulary.py -k codex_names -v`
Expected: FAIL -- `AttributeError: 'CodexDialect' object has no attribute 'subject_field'`.

- [ ] **Step 3: Implement the subject rules**

Add to `raven/agent/subagent/acp_dialects/codex.py`:

```python
def _web_search_subject(raw: dict[str, Any]) -> str:
    """The queries, chosen the way the adapter's own title formatter chooses them.

    Reimplemented rather than lifted off the title, because the title is
    prefixed (``Web search: ``) and a prefix inside a subject reads as part of
    the value.
    """
    action = _dict(raw.get("action"))
    kind = action.get("type")
    if kind == "openPage":
        url = action.get("url")
        return url if isinstance(url, str) else ""
    if kind == "findInPage":
        pattern = action.get("pattern")
        return pattern if isinstance(pattern, str) else ""
    queries = [q for q in (action.get("queries") or []) if isinstance(q, str) and q]
    if queries:
        return ", ".join(queries)
    for candidate in (action.get("query"), raw.get("query")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""
```

and the methods:

```python
    def subject_field(self, update: dict[str, Any]) -> tuple[str, str]:
        """The subject to show beside the verb, and the key that names it.

        A pair rather than a string because one name can carry either of two
        things: a ``commandExecution.read`` shows the command when the
        permission frame recovered one and the path when it did not, and storing
        a path under ``command`` would mislabel it for every later reader.
        """
        name = self.tool_name(update)
        raw = _dict(update.get("rawInput"))
        meta_codex = _dict(_dict(update.get("_meta")).get("codex"))

        if name == "webSearch":
            return "query", _web_search_subject(raw)
        if name == "collabAgentToolCall":
            prompt = raw.get("prompt")
            return "prompt", prompt.strip() if isinstance(prompt, str) else ""
        if name == "subAgentActivity":
            path = _dict(meta_codex.get("subagent")).get("path")
            leaf = path.rstrip("/").rsplit("/", 1)[-1] if isinstance(path, str) and path else ""
            return "argument", leaf
        if name == "contextCompaction":
            return "argument", ""
        # An MCP or dynamic call is the only shape carrying `arguments`; a
        # dynamic one is named by its title, so the name cannot be matched on.
        if "arguments" in raw:
            arguments = raw.get("arguments")
            first = next(
                (v.strip() for v in _dict(arguments).values() if isinstance(v, str) and v.strip()),
                "",
            )
            return "argument", first
        if name == "apply_patch":
            # Filled from the patch envelope in the completed frame; the opening
            # frame's only "command" is the tool's own name.
            return "path", ""
        if name == "imageGeneration":
            # Same shape as apply_patch: the subject is in the completed frame.
            # An explicit empty subject is what keeps the generic tail below from
            # keying a free-text prompt as a `path`.
            return "argument", ""

        command = raw.get("command")
        if isinstance(command, str) and command.strip():
            return "command", command.strip()
        path = raw.get("path")
        if isinstance(path, str) and path.strip():
            return "path", path.strip()
        located = super().argument(update)
        return ("path" if located else "argument"), located

    def argument(self, update: dict[str, Any]) -> str:
        return self.subject_field(update)[1]

    def call(self, update: dict[str, Any]) -> ToolCall:
        """The adapter's call with the subject promoted to the front of its input.

        Insertion order is load-bearing at the read boundary, which takes the
        first string value it finds. Building the dict here rather than adding
        a ``tool_vocabulary.ARGUMENT_KEY`` row is what keeps a promotion keyed on
        a raven name from relabelling a codex field.
        """
        base = super().call(update)
        key, subject = self.subject_field(update)
        # A title that only repeats the verb says nothing, and `ToolCall.subject`
        # falls back to it: codex titles an `apply_patch` "apply_patch", which
        # rendered as `apply_patch apply_patch` until its result named the file.
        title = "" if base.title == base.name else base.title
        if not subject:
            return ToolCall(
                id=base.id, name=base.name, argument="", title=title, raw_input=base.raw_input
            )
        merged: dict[str, Any] = {key: subject}
        for name, value in base.raw_input.items():
            if name != key:
                merged[name] = value
        return ToolCall(
            id=base.id, name=base.name, argument=subject, title=title, raw_input=merged
        )
```

Import `ToolCall` at the top of the module (`from raven.agent.subagent.acp_dialects.base import AcpDialect, ToolCall, ToolResult, _dict`).

Delete the old `argument` docstring about the title's truncation -- `commandExecution` now reaches `rawInput.command` through `subject_field`, which is the same behaviour with the reason moved.

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_acp_dialects.py tests/test_subagent_tool_vocabulary.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add raven/agent/subagent/acp_dialects/codex.py tests/test_acp_dialects.py tests/test_subagent_tool_vocabulary.py
git commit -m "feat(agent): give each codex row a subject and a key that names it" \
  -m "Co-authored-by: Claude (claude-opus-5[1m]) <noreply@anthropic.com>"
```

---

### Task 4: The patched file, and CRLF

`apply_patch`'s subject exists only in the result: codex sends the tool's own name as the command and the envelope names the files.

**Files:**
- Modify: `raven/agent/subagent/acp_dialects/codex.py`
- Modify: `raven/agent/subagent/backends/acp_agent.py`
- Modify: `CONTEXT.md`
- Test: `tests/test_acp_dialects.py`, `tests/test_subagent_acp.py`

Also fix one stale docstring carried over from Task 3: in
`tests/test_acp_dialects.py::test_the_subject_falls_back_to_locations_when_there_is_no_input`,
the docstring still says the subject "keeps the literal key `argument`" while the
assertion below it now reads `{"path": ...}`. It describes `arguments_json()`'s
empty-input fallback, which no longer fires -- `CodexDialect.call()` populates
`raw_input` with the `path` key first. Reword it to describe what actually happens.
Change the docstring only.

**Interfaces:**
- Consumes: `AcpDialect.subject_from_result` from Task 1; `CodexDialect.subject_field` from Task 3.
- Produces: `_TurnCollector._backfill_subject(call_id: str, subject: str) -> None`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_acp_dialects.py`:

```python
def test_codex_reads_the_patched_files_out_of_the_envelope() -> None:
    """`apply_patch` names itself as the command; the files are in the result."""
    codex = CodexDialect()

    assert codex.subject_from_result(acp_frames.CODEX_PATCH_DONE) == "calc.py"
    assert codex.subject_from_result(acp_frames.CODEX_READ_DONE) is None


def test_codex_takes_an_images_path_but_never_its_prompt() -> None:
    """`savedPath` is a path; `revisedPrompt` is prose and would be keyed as one.

    Synthetic -- no capture reached image generation. Shape from the adapter's
    `imageGenerationRawOutput`.
    """
    codex = CodexDialect()
    raw = {"status": "completed", "revisedPrompt": "a red bicycle", "savedPath": "/w/bike.png"}

    assert codex.subject_from_result({"rawOutput": raw}) == "/w/bike.png"
    assert codex.subject_from_result({"rawOutput": {k: v for k, v in raw.items() if k != "savedPath"}}) is None


def test_codex_results_lose_the_terminals_crlf() -> None:
    codex = CodexDialect()

    text = codex.result(acp_frames.CODEX_PATCH_DONE).text

    assert "\r" not in text
    assert "*** Update File: calc.py" in text
```

Append to `tests/test_subagent_acp.py`:

```python
@pytest.mark.asyncio
async def test_a_patch_row_names_the_file_it_changed() -> None:
    """Two edits rendered identically as `exec apply_patch` before this."""
    collector = _TurnCollector(dialect=CodexDialect())

    await collector("session/update", {"update": acp_frames.CODEX_PATCH_OPEN})
    await collector("session/update", {"update": acp_frames.CODEX_PATCH_DONE})

    call = collector.calls[0]
    assert call.name == "apply_patch"
    assert call.subject == "calc.py"
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_acp_dialects.py -k "patched_files or crlf" tests/test_subagent_acp.py -k patch_row -v`
Expected: FAIL -- `subject_from_result` returns None and `call.subject` is `''`.

- [ ] **Step 3: Implement the envelope reader and CRLF normalisation**

Add to `raven/agent/subagent/acp_dialects/codex.py`:

```python
import re

# codex's own patch envelope (`codex-rs/apply-patch`). The body reaches the
# command on stdin, so the file it touched is in the output and nowhere else.
_PATCH_TARGET = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (?P<path>.+?)\s*$", re.MULTILINE)

# Terminal output arrives CRLF-terminated, and a C0 control character reaches
# the transcript as garbage. Tab and newline are the two that carry meaning.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]")


def _clean(text: str) -> str:
    return _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))
```

and the method:

```python
    def subject_from_result(self, update: dict[str, Any]) -> str | None:
        """A subject codex reports only once the call has finished.

        Two tools do this. ``apply_patch`` names the files it changed in its own
        envelope, and ``imageGeneration`` names where it wrote the image. Only
        ``savedPath`` is taken from the latter, never ``revisedPrompt``: the
        back-fill stores what it finds under ``path``, and a prompt keyed as a
        path is the mislabel this dialect exists to prevent.
        """
        raw = update.get("rawOutput")
        if not isinstance(raw, dict):
            return None
        formatted = raw.get("formatted_output")
        if isinstance(formatted, str):
            targets = list(dict.fromkeys(_PATCH_TARGET.findall(formatted)))
            if targets:
                return ", ".join(targets)
        saved = raw.get("savedPath")
        return saved.strip() if isinstance(saved, str) and saved.strip() else None
```

In the existing `result` method, wrap both return paths so the text is cleaned:

```python
            if isinstance(formatted, str):
                # An empty-but-present formatted_output is the real answer for a
                # command that printed nothing, so the exit code is what says so.
                return ToolResult(text=_clean(formatted) or f"(no output, exit {exit_code})", ok=ok)

        text = super().result(update).text
        return ToolResult(text=_clean(text), ok=ok)
```

- [ ] **Step 4: Call the hook from the collector**

In `raven/agent/subagent/backends/acp_agent.py`, inside `__call__`'s `else:` branch (the `tool_call_update` path), after the result event is appended:

```python
                    subject = self._dialect.subject_from_result(update)
                    if subject:
                        self._backfill_subject(str(update.get("toolCallId") or ""), subject)
```

and add the method beside `_revise_call`:

```python
    def _backfill_subject(self, call_id: str, subject: str) -> None:
        """Set a call's subject from its own result.

        The frame that opened the call did not have it: codex sends
        ``apply_patch`` as the command and names the files it patched only in
        the output. Never overwrites a subject already known -- a later frame
        revising an argument is ``_revise_call``'s business, not this.
        """
        for event in reversed(self.events):
            if event.get("t") == "call" and event.get("id") == call_id:
                previous: ToolCall = event["call"]
                if previous.argument:
                    return
                event["call"] = ToolCall(
                    id=previous.id,
                    name=previous.name,
                    argument=subject,
                    title=previous.title,
                    raw_input={"path": subject, **previous.raw_input},
                )
                return
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_acp_dialects.py tests/test_subagent_acp.py -v`
Expected: PASS.

- [ ] **Step 6: Define the new domain term**

In `CONTEXT.md`, beside the term added in Task 2:

```markdown
**Subject back-fill** -- setting a tool call's subject from a frame later than
the one that opened it. Three frames can supply one: a `tool_call_update`
revising `rawInput` (`_revise_call`), a completed result carrying the subject in
its output (`_backfill_subject`, used by codex's `apply_patch`), and a
`session/request_permission` carrying the command a re-badged call really ran.
```

- [ ] **Step 7: Commit**

```bash
git add raven/agent/subagent/acp_dialects/codex.py raven/agent/subagent/backends/acp_agent.py \
        tests/test_acp_dialects.py tests/test_subagent_acp.py CONTEXT.md
git commit -m "feat(agent): name the file an apply_patch changed, and drop terminal crlf" \
  -m "Co-authored-by: Claude (claude-opus-5[1m]) <noreply@anthropic.com>"
```

---

### Task 5: Recover the command from the permission frame

Three of codex's four command shapes drop the command from the session update. It survives on `session/request_permission`, which raven answers and discards.

**Files:**
- Modify: `raven/agent/acp/permissions.py`
- Modify: `raven/agent/acp/pool.py:221`
- Modify: `raven/agent/subagent/acp_dialects/codex.py`
- Modify: `raven/agent/subagent/backends/acp_agent.py`
- Modify: `docs/specs/2026-08-23-codex-acp-tool-parsing-design.md` (section 4)
- Test: `tests/test_acp_dialects.py`, `tests/test_subagent_acp.py`

**Interfaces:**
- Consumes: `AcpDialect.permission_command` from Task 1; `_TurnCollector._backfill_subject` from Task 4.
- Produces: `auto_approver(name: str, observe: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_acp_dialects.py`:

```python
def test_codex_prefers_the_parsed_command_over_the_bash_argument() -> None:
    """`rawInput.command` is the quoted bash argument; `commandActions` is clean."""
    assert CodexDialect().permission_command(acp_frames.CODEX_READ_PERMISSION) == "sed -n '1,200p' calc.py"
```

Append to `tests/test_subagent_acp.py`:

```python
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
async def test_an_observer_that_raises_still_yields_the_approval() -> None:
    """An unanswered permission request cancels the whole turn."""
    from raven.agent.acp.permissions import auto_approver

    async def boom(method: str, params: dict[str, object]) -> None:
        raise RuntimeError("observer is broken")

    handle = auto_approver("codex", observe=boom)
    answer = await handle("session/request_permission", acp_frames.CODEX_READ_PERMISSION)

    assert answer == {"outcome": {"outcome": "selected", "optionId": "allow_always"}}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_acp_dialects.py -k parsed_command tests/test_subagent_acp.py -k "permission_frame or observer" -v`
Expected: FAIL -- `permission_command` returns the quoted form; `auto_approver` takes one argument.

- [ ] **Step 3: Read the parsed command in the codex dialect**

Add to `raven/agent/subagent/acp_dialects/codex.py`:

```python
    def permission_command(self, params: dict[str, Any]) -> str | None:
        """The command as codex parsed it, not as bash received it.

        ``toolCall.rawInput.command`` is the argument to ``bash -lc`` and arrives
        wrapped in its own quotes. ``_meta.codex.params.commandActions`` holds the
        same command already parsed (codex's ``ParsedCommand``), which is what a
        row should show.
        """
        actions = _dict(_dict(_dict(params.get("_meta")).get("codex")).get("params")).get("commandActions")
        if isinstance(actions, list):
            for action in actions:
                command = _dict(action).get("command")
                if isinstance(command, str) and command.strip():
                    return command.strip()
        command = super().permission_command(params)
        return command.strip('"').strip() if command else None
```

- [ ] **Step 4: Give `auto_approver` an observer**

In `raven/agent/acp/permissions.py`, change the signature and body:

```python
def auto_approver(name: str, observe: "Any" = None) -> "Any":
    """An ``on_request`` handler that approves permissions and refuses the rest.

    Refusing the rest is deliberate: ``fs/read_text_file`` and its siblings are
    advertised as unsupported in ``CLIENT_CAPABILITIES``, and answering one
    here would claim a capability raven does not serve.

    ``observe`` is handed each permission request after the outcome is decided.
    It exists because the request carries what the matching ``session/update``
    does not -- codex badges a shell command as a ``read`` and sends the command
    only here. It runs inside a ``try``: an unanswered request cancels the whole
    turn, so nothing an observer does may reach the answer.
    """

    async def handle(method: str, params: dict[str, Any]) -> Any:
        if method != PERMISSION_METHOD:
            return UNHANDLED
        outcome = permission_outcome(params)
        tool = params.get("toolCall")
        title = tool.get("title") or tool.get("kind") if isinstance(tool, dict) else None
        logger.debug("acp agent {!r}: approving {} ({})", name, title or "a tool call", outcome)
        if observe is not None:
            try:
                await observe(method, params)
            except Exception as exc:  # noqa: BLE001 - an observer must not reach the answer
                logger.debug("acp agent {!r}: permission observer failed: {}", name, exc)
        return {"outcome": outcome}

    return handle
```

- [ ] **Step 5: Wire the router as the observer**

In `raven/agent/acp/pool.py`, at the `on_request` default (line 221):

```python
                on_request=on_request if on_request is not None else auto_approver(name, observe=router.dispatch),
```

`capabilities.py:375` stays as it is: it opens a connection with no session router, so there is nothing to observe with.

- [ ] **Step 6: Handle the frame in the collector**

In `raven/agent/subagent/backends/acp_agent.py`, add the import and constant:

```python
from raven.agent.acp.permissions import PERMISSION_METHOD
```

and at the very top of `__call__`, **before** the `update` guard (the frame has no `update` key, so the guard would drop it):

```python
    async def __call__(self, method: str, params: dict[str, Any]) -> None:
        if method == PERMISSION_METHOD:
            # Subject only, never the name: this frame reports `kind: "execute"`
            # even for a call the session update badged `read`, and reading it
            # for a name would erase the distinction codex drew.
            command = self._dialect.permission_command(params)
            call_id = str(_dict(params.get("toolCall")).get("toolCallId") or "")
            if command and call_id:
                self._backfill_subject(call_id, command)
            return
        update = params.get("update")
```

Add a module-level `_dict` helper import from the dialect package if one is not already in scope:

```python
from raven.agent.subagent.acp_dialects import AcpDialect, ToolCall, _dict, content_texts, dialect_for
```

and export `_dict` from `raven/agent/subagent/acp_dialects/__init__.py`'s import line and `__all__`.

`_backfill_subject` already refuses to overwrite a known subject, which is what keeps this from replacing a `commandExecution`'s real command with the same string.

- [ ] **Step 7: Run the tests to verify they pass**

Run: `uv run pytest tests/test_acp_dialects.py tests/test_subagent_acp.py tests/test_acp_stdio.py -v`
Expected: PASS.

- [ ] **Step 8: Update the spec**

In `docs/specs/2026-08-23-codex-acp-tool-parsing-design.md`, section 4, replace the third bullet with:

```markdown
- The collector handles `session/request_permission` as a **subject-only**
  revision. The command comes from `_meta.codex.params.commandActions[].command`
  -- codex's own parse -- rather than `toolCall.rawInput.command`, which is the
  argument to `bash -lc` and arrives wrapped in its own quotes. Deliberately not
  the frame's `kind`, which is `execute` even for a call the session update
  badged `read`; letting it rename the call would erase the `ParsedCommand`
  distinction section 1 just recovered.
```

- [ ] **Step 9: Commit**

```bash
git add raven/agent/acp/permissions.py raven/agent/acp/pool.py \
        raven/agent/subagent/acp_dialects/codex.py raven/agent/subagent/acp_dialects/__init__.py \
        raven/agent/subagent/backends/acp_agent.py \
        tests/test_acp_dialects.py tests/test_subagent_acp.py \
        docs/specs/2026-08-23-codex-acp-tool-parsing-design.md
git commit -m "feat(agent): recover the command a codex permission request carries" \
  -m "Co-authored-by: Claude (claude-opus-5[1m]) <noreply@anthropic.com>"
```

---

### Task 6: The plan becomes one `update_plan` row

Five snapshot frames for one four-entry plan, all dropped today.

**Files:**
- Modify: `raven/agent/subagent/acp_dialects/base.py`
- Modify: `raven/agent/subagent/acp_dialects/codex.py`
- Modify: `raven/agent/subagent/backends/acp_agent.py`
- Test: `tests/test_subagent_acp.py`

**Interfaces:**
- Consumes: nothing from earlier tasks beyond the dialect.
- Produces: `AcpDialect.plan_tool_name` (class attribute, `"plan"`; codex `"update_plan"`); `AcpDialect.plan_rows(update) -> tuple[str, str]` returning `(subject, checklist)`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_subagent_acp.py`:

```python
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
    await collector("session/update", {"update": {"sessionUpdate": "agent_message_chunk",
                                                  "content": {"type": "text", "text": "Working on it."}}})
    await collector("session/update", {"update": acp_frames.CODEX_PLAN_SECOND})
    await collector("session/update", {"update": {"sessionUpdate": "agent_message_chunk",
                                                  "content": {"type": "text", "text": " Nearly done."}}})

    assert collector.text == "Working on it. Nearly done."
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run pytest tests/test_subagent_acp.py -k plan -v`
Expected: FAIL -- `collector.calls` is empty.

- [ ] **Step 3: Add the plan reader to the dialects**

In `raven/agent/subagent/acp_dialects/base.py`, add to `AcpDialect`:

```python
    plan_tool_name = "plan"
    """What to call the tool behind a ``sessionUpdate: "plan"`` frame."""

    def plan_rows(self, update: dict[str, Any]) -> tuple[str, str]:
        """One plan snapshot as a subject and a checklist.

        The frame is the whole list every time, so a reader shows the current
        state rather than a diff. The subject is the step being worked on,
        because that is the one thing a folded row can usefully say.
        """
        entries = update.get("entries")
        rows: list[str] = []
        current = ""
        for entry in entries if isinstance(entries, list) else []:
            fields = _dict(entry)
            content = fields.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            status = fields.get("status")
            mark = {"completed": "x", "in_progress": ">"}.get(status if isinstance(status, str) else "", " ")
            if mark == ">" and not current:
                current = content.strip()
            rows.append(f"[{mark}] {content.strip()}")
        return current or (f"{len(rows)} steps" if rows else ""), "\n".join(rows)
```

In `raven/agent/subagent/acp_dialects/codex.py`, add to `CodexDialect`:

```python
    plan_tool_name = "update_plan"
    """codex's own name for the tool behind a plan frame (``plan_tool.rs``)."""
```

- [ ] **Step 4: Handle the frame in the collector**

In `raven/agent/subagent/backends/acp_agent.py`, add the constant beside `_MESSAGE_BREAK`:

```python
# One synthetic id for the turn's plan. The frame carries no call id of its own,
# and codex re-sends the whole list on every change -- five times for one plan in
# the capture -- so a row per frame would be five near-identical rows. The branch
# is dialect-independent, so the id does not name codex.
_PLAN_CALL_ID = "acp-plan"
```

and a branch in `__call__` after the `_BREAKING_UPDATES` branch:

```python
        elif kind == "plan":
            self._plan(update)
```

and the method:

```python
    def _plan(self, update: dict[str, Any]) -> None:
        """Open the turn's plan row, or move the one already open.

        Only the opening frame breaks the message. A revision that broke it too
        would split the narration around a plan that changes four times.
        """
        subject, checklist = self._dialect.plan_rows(update)
        if not checklist:
            return
        call = ToolCall(
            id=_PLAN_CALL_ID,
            name=self._dialect.plan_tool_name,
            argument=subject,
            title="",
            raw_input={"argument": subject} if subject else {},
        )
        for event in self.events:
            if event.get("t") == "call" and event.get("id") == _PLAN_CALL_ID:
                event["call"] = call
                break
        else:
            self._tool_ran = True
            self.events.append({"t": "call", "id": _PLAN_CALL_ID, "call": call, "at": self._now()})
        for event in self.events:
            if event.get("t") == "result" and event.get("id") == _PLAN_CALL_ID:
                event["text"] = checklist
                return
        self.events.append(
            {"t": "result", "id": _PLAN_CALL_ID, "ok": True, "text": checklist, "at": self._now()}
        )
```

Add `"plan"` to the live-republish condition at the end of `__call__`:

```python
        if kind in (*_ANSWER_UPDATES, *_THOUGHT_UPDATES, "tool_call", "tool_call_update", "plan"):
            activity.set_transcript(self._run, self.messages(in_flight=True))
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `uv run pytest tests/test_subagent_acp.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add raven/agent/subagent/acp_dialects/base.py raven/agent/subagent/acp_dialects/codex.py \
        raven/agent/subagent/backends/acp_agent.py tests/test_subagent_acp.py
git commit -m "feat(agent): render a codex plan as one update_plan row that moves" \
  -m "Co-authored-by: Claude (claude-opus-5[1m]) <noreply@anthropic.com>"
```

---

### Task 7: The TUI keeps folding codex rows

Codex's names are absent from `RAVEN_NAME`, so `OVERRIDES` no longer matches any codex call and every row falls to the generic `{style: 'target', unit: 'calls'}`. Six reads stop collapsing to one line.

**Files:**
- Create: `ui-tui/src/domain/codexTools.ts`
- Modify: `ui-tui/src/domain/episodeSummary.ts`
- Modify: `ui-tui/CONTEXT.md`
- Test: `ui-tui/src/__tests__/episodeSummary.test.ts`

**Interfaces:**
- Consumes: the codex names produced by Tasks 2 and 6.
- Produces: `CODEX_VERBS: Record<string, VerbRule>`; `VerbRule` exported from `episodeSummary.ts`.

- [ ] **Step 1: Write the failing test**

Append to `ui-tui/src/__tests__/episodeSummary.test.ts`:

```ts
import { toolParts, toolsPhrase } from '../domain/episodeSummary.js'

const tool = (name: string, summary: string) => ({
  id: name + summary, name, summary, ok: true, done: true
})

describe('codex tool rows', () => {
  it('keeps codex names as the verb so the conversation reads as codex', () => {
    expect(toolParts(tool('apply_patch', 'calc.py')).verb).toBe('apply_patch')
    expect(toolParts(tool('webSearch', 'gpt-5 codex')).verb).toBe('webSearch')
    expect(toolParts(tool('update_plan', '4 steps')).verb).toBe('update_plan')
  })

  it('folds a run of codex reads by count, the way raven reads fold', () => {
    const reads = [
      tool('commandExecution.read', "sed -n '1,200p' a.py"),
      tool('commandExecution.read', "sed -n '1,200p' b.py"),
      tool('commandExecution.read', "sed -n '1,200p' c.py")
    ]

    expect(toolsPhrase(reads)).toContain('3')
  })

  it('names the program a codex command ran, not the whole pipeline', () => {
    const detail = toolParts(tool('commandExecution', 'cat x | python3 -c "import sys"')).detail

    expect(detail).toContain('python3')
    expect(detail).not.toContain('import sys')
  })
})
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `npm test --prefix ui-tui -- episodeSummary --no-file-parallelism`
Expected: FAIL -- verb is `commandExecution.read` humanized wrong, and the pipeline is not shortened.

- [ ] **Step 3: Create the codex verb table**

Create `ui-tui/src/domain/codexTools.ts`:

```ts
// SPDX-License-Identifier: MIT
// Copyright (c) 2026 EverMind.
// See NOTICES.md.
//
// Codex's own tool names, and how a run of them folds.
//
// The runtime normalises most transports into Raven's vocabulary at the read
// boundary, and `OVERRIDES` in `episodeSummary.ts` is keyed by that vocabulary.
// Codex is the exception on purpose: its rows keep codex's own names so a direct
// chat reads as a codex conversation. That is why they need a table here -- with
// no entry, every codex call falls to the generic rule and a run of six reads
// stops collapsing to one line.
//
// The verb is the codex name verbatim, never a Raven synonym. Only `style` and
// `unit` are ours, and they only decide how repeats collapse.

import type { VerbRule } from './episodeSummary.js'

export const CODEX_VERBS: Record<string, VerbRule> = {
  apply_patch: { verb: 'apply_patch', unit: 'files', style: 'target' },
  update_plan: { verb: 'update_plan', unit: '', style: 'target' },
  commandExecution: { verb: 'commandExecution', unit: 'commands', style: 'target' },
  'commandExecution.read': { verb: 'commandExecution.read', unit: 'files', style: 'count' },
  'commandExecution.listFiles': { verb: 'commandExecution.listFiles', unit: 'dirs', style: 'count' },
  'commandExecution.search': { verb: 'commandExecution.search', unit: 'patterns', style: 'count' },
  webSearch: { verb: 'webSearch', unit: 'queries', style: 'target' },
  fileChange: { verb: 'fileChange', unit: 'files', style: 'target' },
  imageView: { verb: 'imageView', unit: 'images', style: 'count' },
  imageGeneration: { verb: 'imageGeneration', unit: 'images', style: 'target' },
  mcpToolCall: { verb: 'mcpToolCall', unit: 'calls', style: 'target' },
  dynamicToolCall: { verb: 'dynamicToolCall', unit: 'calls', style: 'target' },
  collabAgentToolCall: { verb: 'collabAgentToolCall', unit: 'calls', style: 'target' },
  subAgentActivity: { verb: 'subAgentActivity', unit: 'subagents', style: 'target' },
  contextCompaction: { verb: 'contextCompaction', unit: '', style: 'target' }
}

// An MCP call is named `mcp.<server>.<tool>`, one name per configured tool, so
// it cannot have a row of its own.
export const codexRule = (name: string): VerbRule | undefined =>
  CODEX_VERBS[name] ?? (name.startsWith('mcp.') ? { verb: name, unit: 'calls', style: 'target' } : undefined)
```

- [ ] **Step 4: Wire it into `episodeSummary.ts`**

Export the interface (change `interface VerbRule` to `export interface VerbRule`), add the import at the top:

```ts
import { codexRule } from './codexTools.js'
```

change `ruleFor`:

```ts
// The rule for any tool: its override if we have one, else codex's own table,
// else a generic rule built from the humanized name. Codex is consulted second
// rather than merged into OVERRIDES because the two are keyed by different
// vocabularies -- OVERRIDES by Raven's names, CODEX_VERBS by codex's.
const ruleFor = (name: string): VerbRule =>
  OVERRIDES[name] ?? codexRule(name) ?? { verb: humanize(name) || name, unit: 'calls', style: 'target' }
```

and extend the shell-command detail branch so codex's command rows get the same program naming `exec` gets:

```ts
  if (tool.name === 'exec' || tool.name === 'commandExecution') {
    return execLabel(raw)
  }
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `npm test --prefix ui-tui -- episodeSummary --no-file-parallelism`
Expected: PASS.

- [ ] **Step 6: Run the whole TUI suite and rebuild the bundle**

Run: `npm test --prefix ui-tui -- --no-file-parallelism`
Expected: PASS.

Run: `npm run build --prefix ui-tui`
`raven tui` loads the prebuilt gitignored `ui-tui/dist/entry.js`, so without this a manual check tests stale code.

- [ ] **Step 7: Cover a whole codex turn end to end**

Append to `ui-tui/src/__tests__/directEpisodes.test.ts`:

```ts
it('folds a codex turn keeping codex names on every row', () => {
  const turns = [
    { role: 'user', content: 'fix the bug', call_id: 'c1', at_ms: 1 },
    {
      role: 'assistant', content: '', call_id: 'c1', at_ms: 2,
      tool_calls: [{ id: 't1', name: 'commandExecution.read', arguments: '{"path":"/w/calc.py"}' }]
    },
    { role: 'tool', content: 'def add(a, b):', call_id: 'c1', tool_call_id: 't1', at_ms: 3 },
    {
      role: 'assistant', content: '', call_id: 'c1', at_ms: 4,
      tool_calls: [{ id: 't2', name: 'apply_patch', arguments: '{"path":"calc.py"}' }]
    },
    { role: 'tool', content: 'Success. Updated the following files:', call_id: 'c1', tool_call_id: 't2', at_ms: 5 }
  ] as never

  const msgs = foldDirectTurns(turns)
  const episodes = msgs.find(m => m.kind === 'episodes')!.episodes!
  const names = episodes.flatMap(e => e.tools.map(t => t.name))

  expect(names).toEqual(['commandExecution.read', 'apply_patch'])
  expect(episodes[1]!.tools[0]!.summary).toBe('calc.py')
})
```

Run: `npm test --prefix ui-tui -- directEpisodes --no-file-parallelism`
Expected: PASS with no change to `directEpisodes.ts` -- it is vocabulary-agnostic by
design, and this test is what pins that.

- [ ] **Step 8: Define the new domain term**

In `ui-tui/CONTEXT.md`:

```markdown
**Codex verb rule** -- an entry in `CODEX_VERBS` (`domain/codexTools.ts`) giving
one codex tool its folding style. The verb is codex's own name verbatim; only
`unit` and `style` are the TUI's. Needed because codex rows deliberately keep
codex's vocabulary instead of Raven's, so `OVERRIDES` cannot match them.
```

- [ ] **Step 9: Commit**

```bash
git add ui-tui/src/domain/codexTools.ts ui-tui/src/domain/episodeSummary.ts \
        ui-tui/src/__tests__/episodeSummary.test.ts \
        ui-tui/src/__tests__/directEpisodes.test.ts ui-tui/CONTEXT.md
git commit -m "feat(tui): fold codex tool rows under codex's own names" \
  -m "Co-authored-by: Claude (claude-opus-5[1m]) <noreply@anthropic.com>"
```

---

## Final verification

- [ ] `uv run pytest tests/test_acp_dialects.py tests/test_subagent_acp.py tests/test_subagent_tool_vocabulary.py tests/test_acp_stdio.py tests/test_subagent_direct_chat.py -v`
- [ ] `npm test --prefix ui-tui -- --no-file-parallelism`
- [ ] `npm run build --prefix ui-tui`
- [ ] `make check-large-files`
- [ ] Live check: a direct chat with the `Writer` agent that runs a command, edits a file and keeps a plan. Confirm the rows read `commandExecution`, `apply_patch <file>` and `update_plan`, and that the plan row moves rather than repeating.
