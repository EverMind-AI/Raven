# Sub-Agent handle & Codex parsing refinements — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the Claude Code "Invalid session ID" bug, add structured Codex `--json` transcript parsing, and instruct the agent to pick semantic instance handles.

**Architecture:** Three additive refinements to `src/agentscope/subagent/`. (1) Mint canonical UUIDs (`str(uuid.uuid4())`) for provisioned session ids. (2) A new orthogonal `transcript_format` config field selects regex (`"text"`, default) vs a new `codex_jsonl` structured parser for id + reply extraction. (3) Prompt-only guidance for semantic handles. Every change defaults to today's behavior, so existing configs are byte-identical.

**Tech Stack:** Python 3.11+, Pydantic v2, `unittest`/`pytest`; React + TypeScript (Vite) frontend.

## Global Constraints

- **Encapsulation:** internal modules/classes/functions are `_`-prefixed; public surface is via each subpackage's `__init__.py`.
- **Lazy imports:** third-party libs imported at point of use (not relevant here — only stdlib `json`/`uuid`/`re`).
- **Docstrings:** English, strict `Args:`/`Returns:`/`Raises:` template with backtick-typed params.
- **Style/CI:** black (line length **79**), flake8, pylint, mypy, docstring checks. Run `pre-commit run --files <changed files>` before every commit; fix the code, never disable checks.
- **Tests:** assert against the **whole** data structure (whole-tuple / whole-dict), not field-by-field; use `AnyString`/`AnyValue` from `tests/utils.py` for nondeterministic fields. Backend tests run with `conda run -n ravenx python -m pytest tests/<file> -v`.
- **Backward compatibility:** `transcript_format` defaults to `"text"`; every existing config, preset, and dump keeps working. The new config field is inserted **after `output_pattern` and before `cwd`** (matters for whole-dict `model_dump()` ordering).
- **Whole-dict dumps live in BOTH `tests/subagent_config_test.py` AND `tests/subagent_factory_test.py`** — any field add must update both (documented plan gap from the id-mechanisms feature).
- **Canonical UUID:** provisioned/stateless session ids use `str(uuid.uuid4())` (8-4-4-4-12, with dashes). The internal registry handle default and the temp-prompt filename stay `.hex`.
- **Codex reply rule:** the reply is the `item.text` of the **last** `item.completed` event whose `item.type == "agent_message"`.
- **Commits:** `git add` only the files named in the task (never `git add -A`; never touch `.webapp_logs/*.pid`). End every commit message with `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.

---

### Task 1: Canonical UUID for provisioned session ids (Change 2)

**Files:**
- Modify: `src/agentscope/subagent/_tool.py` (the two `uuid.uuid4().hex` in `call()`, ~lines 287-290)
- Test: `tests/subagent_tool_test.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: no signature change. Behavior: a provisioned stateful create passes a canonical UUID to `{agent_id}`.

- [ ] **Step 1: Write the failing test**

Add to `class CliSubAgentToolStatefulTest` in `tests/subagent_tool_test.py`:

```python
    def test_provisioned_create_mints_canonical_uuid(self) -> None:
        """The minted provisioned session id is a canonical UUID."""
        tool, backend = self._make_tool()
        chunks = asyncio.run(
            _collect(tool.call(prompt="draft", instance="writer")),
        )
        aid = chunks[-1].metadata["agent_id"]
        self.assertRegex(
            aid,
            r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
            r"[0-9a-f]{4}-[0-9a-f]{12}$",
        )
        # It is exactly the token passed to --session-id.
        self.assertEqual(backend.calls[0][-1], aid)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n ravenx python -m pytest tests/subagent_tool_test.py::CliSubAgentToolStatefulTest::test_provisioned_create_mints_canonical_uuid -v`
Expected: FAIL — the current `uuid.uuid4().hex` is 32 hex chars with no dashes, so `assertRegex` fails.

- [ ] **Step 3: Write minimal implementation**

In `src/agentscope/subagent/_tool.py`, inside `call()`, replace the create/stateless id minting. Change:

```python
            else:
                created = True
                action = "create"
                agent_id = (
                    None if self._id_source == "derived" else uuid.uuid4().hex
                )
        else:
            agent_id = uuid.uuid4().hex
```

to:

```python
            else:
                created = True
                action = "create"
                agent_id = (
                    None
                    if self._id_source == "derived"
                    else str(uuid.uuid4())
                )
        else:
            agent_id = str(uuid.uuid4())
```

Leave the handle default (`handle = instance or uuid.uuid4().hex`) and the temp-prompt filename (`_write_temp_prompt`) unchanged — they never reach a CLI as a session id.

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n ravenx python -m pytest tests/subagent_tool_test.py -v`
Expected: PASS (new test + all pre-existing tool tests; `test_create_then_resume` captures `aid` dynamically, so it is unaffected).

- [ ] **Step 5: Pre-commit + commit**

```bash
pre-commit run --files src/agentscope/subagent/_tool.py tests/subagent_tool_test.py
git add src/agentscope/subagent/_tool.py tests/subagent_tool_test.py
git commit -m "$(cat <<'EOF'
fix(subagent): mint canonical UUID session ids for provisioned creates

Claude Code's --session-id rejects uuid4().hex (no dashes). Use
str(uuid.uuid4()).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: `transcript_format` config field + validation (Change 3a)

**Files:**
- Modify: `src/agentscope/subagent/_base.py`
- Test: `tests/subagent_config_test.py`, `tests/subagent_factory_test.py`

**Interfaces:**
- Produces: `CliSubAgentConfig.transcript_format: Literal["text", "codex_jsonl"] = "text"`. Validation: `codex_jsonl` forbids both regex patterns; a `derived` prototype requires `session_id_pattern` **only** when `transcript_format == "text"`.

- [ ] **Step 1: Write the failing tests**

Add to `class CliSubAgentConfigTest` in `tests/subagent_config_test.py`:

```python
    def test_transcript_format_defaults_to_text(self) -> None:
        """The transcript_format defaults to 'text'."""
        config = CliSubAgentConfig(
            name="cc",
            description="d",
            command="claude -p {prompt}",
        )
        self.assertEqual(config.transcript_format, "text")

    def test_codex_jsonl_forbids_session_id_pattern(self) -> None:
        """codex_jsonl parses the id structurally; a pattern is rejected."""
        with self.assertRaises(ValidationError):
            CliSubAgentConfig(
                name="codex",
                description="d",
                command="codex exec --json {prompt}",
                resume_command="codex exec --json resume {agent_id} {prompt}",
                id_source="derived",
                transcript_format="codex_jsonl",
                session_id_pattern=r"id:(\w+)",
            )

    def test_codex_jsonl_forbids_output_pattern(self) -> None:
        """codex_jsonl parses the reply structurally; a pattern is rejected."""
        with self.assertRaises(ValidationError):
            CliSubAgentConfig(
                name="codex",
                description="d",
                command="codex exec --json {prompt}",
                resume_command="codex exec --json resume {agent_id} {prompt}",
                id_source="derived",
                transcript_format="codex_jsonl",
                output_pattern=r"(?s)x(.*?)y",
            )

    def test_derived_codex_jsonl_needs_no_pattern(self) -> None:
        """A derived codex_jsonl prototype is valid without any pattern."""
        config = CliSubAgentConfig(
            name="codex",
            description="d",
            command="codex exec --json {prompt}",
            resume_command="codex exec --json resume {agent_id} {prompt}",
            id_source="derived",
            transcript_format="codex_jsonl",
        )
        self.assertEqual(config.transcript_format, "codex_jsonl")
        self.assertIsNone(config.session_id_pattern)
```

Then update the existing whole-dict assertion in `test_defaults_and_dump` — insert `"transcript_format": "text",` immediately after the `"output_pattern": None,` line and before `"cwd": None,`:

```python
                "output_pattern": None,
                "transcript_format": "text",
                "cwd": None,
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n ravenx python -m pytest tests/subagent_config_test.py -v`
Expected: FAIL — `transcript_format` is not a field yet (new tests error / dump mismatch).

- [ ] **Step 3: Write minimal implementation**

In `src/agentscope/subagent/_base.py`, add the field immediately after the `output_pattern` field block (before `cwd`):

```python
    transcript_format: Literal["text", "codex_jsonl"] = Field(
        default="text",
        description=(
            "How the sub-agent's stdout transcript is parsed. 'text' "
            "(default): use `session_id_pattern` / `output_pattern` "
            "regexes. 'codex_jsonl': parse a Codex `exec --json` JSONL "
            "stream structurally for the session id and reply; the regex "
            "fields must then be unset."
        ),
    )
    """The transcript parsing strategy."""
```

Add a new validator (place it after `_validate_patterns`):

```python
    @model_validator(mode="after")
    def _validate_transcript_format(self) -> "CliSubAgentConfig":
        """codex_jsonl parses structurally; regex fields must be unset.

        Returns:
            `CliSubAgentConfig`:
                The validated config instance.
        """
        if self.transcript_format == "codex_jsonl":
            if self.session_id_pattern is not None:
                raise ValueError(
                    "session_id_pattern must be unset when "
                    "transcript_format is 'codex_jsonl' (the id is parsed "
                    "from the JSONL stream)",
                )
            if self.output_pattern is not None:
                raise ValueError(
                    "output_pattern must be unset when transcript_format "
                    "is 'codex_jsonl' (the reply is parsed from the JSONL "
                    "stream)",
                )
        return self
```

In `_validate_stateful`, relax the derived-branch pattern requirement. Change:

```python
            if not self.session_id_pattern:
                raise ValueError(
                    "derived prototype requires a session_id_pattern",
                )
```

to:

```python
            if (
                self.transcript_format == "text"
                and not self.session_id_pattern
            ):
                raise ValueError(
                    "derived prototype with 'text' transcript_format "
                    "requires a session_id_pattern",
                )
```

(`Literal` is already imported at `_base.py:6`.)

- [ ] **Step 4: Update the factory whole-dict dumps**

In `tests/subagent_factory_test.py`, in BOTH `test_from_dict_round_trip` and `test_stateful_round_trip`, insert `"transcript_format": "text",` after the `"output_pattern": None,` line and before `"cwd": None,`:

```python
                "output_pattern": None,
                "transcript_format": "text",
                "cwd": None,
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `conda run -n ravenx python -m pytest tests/subagent_config_test.py tests/subagent_factory_test.py tests/subagent_presets_test.py -v`
Expected: PASS. (The Codex preset is still `text`/regex at this point — Task 6 changes it — so the preset tests remain green.)

- [ ] **Step 6: Pre-commit + commit**

```bash
pre-commit run --files src/agentscope/subagent/_base.py tests/subagent_config_test.py tests/subagent_factory_test.py
git add src/agentscope/subagent/_base.py tests/subagent_config_test.py tests/subagent_factory_test.py
git commit -m "$(cat <<'EOF'
feat(subagent): add transcript_format config field + validation

'text' (default, regex) vs 'codex_jsonl' (structured). codex_jsonl
forbids the regex fields; derived needs a session_id_pattern only for
'text'.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: Codex JSONL parser (Change 3b)

**Files:**
- Create: `src/agentscope/subagent/_transcript.py`
- Test: `tests/subagent_transcript_test.py`

**Interfaces:**
- Produces: `parse_codex_jsonl(stdout: str) -> tuple[str | None, str | None]` returning `(thread_id, reply_text)`.

- [ ] **Step 1: Write the failing tests**

Create `tests/subagent_transcript_test.py`:

```python
# -*- coding: utf-8 -*-
"""Tests for the Codex JSONL transcript parser."""

import unittest

from agentscope.subagent._transcript import parse_codex_jsonl

_SAMPLE = "\n".join(
    [
        '{"type":"thread.started",'
        '"thread_id":"019f75ec-c86f-7ee2-8d05-c8f3b2c08a38"}',
        '{"type":"turn.started"}',
        '{"type":"item.completed","item":{"id":"item_0",'
        '"type":"agent_message","text":"I will read the file."}}',
        '{"type":"item.started","item":{"id":"item_1",'
        '"type":"command_execution","command":"ls","status":"in_progress"}}',
        '{"type":"item.completed","item":{"id":"item_1",'
        '"type":"command_execution","aggregated_output":"boom",'
        '"exit_code":1,"status":"failed"}}',
        '{"type":"item.completed","item":{"id":"item_4",'
        '"type":"agent_message","text":"FINAL REPLY"}}',
        '{"type":"turn.completed","usage":{"input_tokens":1}}',
    ],
)


class ParseCodexJsonlTest(unittest.TestCase):
    """Validate structured extraction from a Codex --json transcript."""

    def test_extracts_thread_id_and_last_agent_message(self) -> None:
        """The thread_id and the final agent_message text are returned."""
        self.assertEqual(
            parse_codex_jsonl(_SAMPLE),
            ("019f75ec-c86f-7ee2-8d05-c8f3b2c08a38", "FINAL REPLY"),
        )

    def test_last_agent_message_wins(self) -> None:
        """With multiple agent_messages, the last one is the reply."""
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"t-1"}',
                '{"type":"item.completed","item":'
                '{"type":"agent_message","text":"first"}}',
                '{"type":"item.completed","item":'
                '{"type":"agent_message","text":"second"}}',
            ],
        )
        self.assertEqual(parse_codex_jsonl(stdout), ("t-1", "second"))

    def test_command_execution_items_are_skipped(self) -> None:
        """A trailing command_execution does not become the reply."""
        stdout = "\n".join(
            [
                '{"type":"thread.started","thread_id":"t-1"}',
                '{"type":"item.completed","item":'
                '{"type":"agent_message","text":"the answer"}}',
                '{"type":"item.completed","item":'
                '{"type":"command_execution","aggregated_output":"x"}}',
            ],
        )
        self.assertEqual(parse_codex_jsonl(stdout), ("t-1", "the answer"))

    def test_missing_thread_started_yields_none_id(self) -> None:
        """No thread.started → thread_id is None, reply still parsed."""
        stdout = (
            '{"type":"item.completed","item":'
            '{"type":"agent_message","text":"hi"}}'
        )
        self.assertEqual(parse_codex_jsonl(stdout), (None, "hi"))

    def test_non_json_lines_are_tolerated(self) -> None:
        """Interspersed non-JSON banner lines are skipped."""
        stdout = "\n".join(
            [
                "OpenAI Codex v0.144.5",
                '{"type":"thread.started","thread_id":"t-9"}',
                "some banner noise",
                '{"type":"item.completed","item":'
                '{"type":"agent_message","text":"done"}}',
            ],
        )
        self.assertEqual(parse_codex_jsonl(stdout), ("t-9", "done"))

    def test_empty_stdout_yields_none_none(self) -> None:
        """Empty stdout → (None, None)."""
        self.assertEqual(parse_codex_jsonl(""), (None, None))
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n ravenx python -m pytest tests/subagent_transcript_test.py -v`
Expected: FAIL with `ModuleNotFoundError: agentscope.subagent._transcript`.

- [ ] **Step 3: Write minimal implementation**

Create `src/agentscope/subagent/_transcript.py`:

```python
# -*- coding: utf-8 -*-
"""Structured parsing of CLI sub-agent transcripts (Codex JSONL)."""

import json


def parse_codex_jsonl(stdout: str) -> tuple[str | None, str | None]:
    """Parse a Codex ``exec --json`` transcript.

    The transcript is one JSON object per line. The session id is the
    ``thread_id`` of the first ``thread.started`` event; the reply is the
    ``item.text`` of the last completed ``agent_message`` item. Lines that
    are not JSON objects, and objects missing the expected fields, are
    tolerated (they contribute nothing).

    Args:
        stdout (`str`):
            The raw stdout of a Codex ``exec --json`` run.

    Returns:
        `tuple[str | None, str | None]`:
            ``(thread_id, reply_text)``. Either element is ``None`` when the
            corresponding event is absent or malformed.
    """
    thread_id: str | None = None
    reply: str | None = None
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(obj, dict):
            continue
        event_type = obj.get("type")
        if event_type == "thread.started" and thread_id is None:
            candidate = obj.get("thread_id")
            if isinstance(candidate, str):
                thread_id = candidate
        elif event_type == "item.completed":
            item = obj.get("item")
            if (
                isinstance(item, dict)
                and item.get("type") == "agent_message"
                and isinstance(item.get("text"), str)
            ):
                reply = item["text"]
    return thread_id, reply
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n ravenx python -m pytest tests/subagent_transcript_test.py -v`
Expected: PASS (6 tests).

- [ ] **Step 5: Pre-commit + commit**

```bash
pre-commit run --files src/agentscope/subagent/_transcript.py tests/subagent_transcript_test.py
git add src/agentscope/subagent/_transcript.py tests/subagent_transcript_test.py
git commit -m "$(cat <<'EOF'
feat(subagent): add parse_codex_jsonl for Codex --json transcripts

Extracts thread_id (first thread.started) and the last agent_message
text; tolerates non-JSON and missing fields.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: Tool runtime `transcript_format` support (Change 3c)

**Files:**
- Modify: `src/agentscope/subagent/_tool.py` (`__init__` + `call()`)
- Test: `tests/subagent_tool_test.py`

**Interfaces:**
- Consumes: `parse_codex_jsonl` (Task 3).
- Produces: `CliSubAgentTool.__init__(..., transcript_format: str = "text", ...)` stored as `self._transcript_format`. When `codex_jsonl`, the derived-create id and the reply come from `parse_codex_jsonl`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/subagent_tool_test.py` (after `CliSubAgentToolDerivedTest`), a JSONL sample and a new test class:

```python
_CODEX_JSONL = (
    b'{"type":"thread.started",'
    b'"thread_id":"019f75ec-c86f-7ee2-8d05-c8f3b2c08a38"}\n'
    b'{"type":"turn.started"}\n'
    b'{"type":"item.completed","item":{"id":"item_0",'
    b'"type":"command_execution","aggregated_output":"x"}}\n'
    b'{"type":"item.completed","item":{"id":"item_4",'
    b'"type":"agent_message","text":"FINAL REPLY"}}\n'
    b'{"type":"turn.completed","usage":{"input_tokens":1}}\n'
)


class CliSubAgentToolCodexJsonlTest(unittest.TestCase):
    """codex_jsonl transcript parsing drives id + reply extraction."""

    def _make_tool(
        self,
        backend: _CannedBackend,
        registry: _MemRegistry,
    ) -> CliSubAgentTool:
        """Build a derived codex_jsonl stateful tool."""
        return CliSubAgentTool(
            name="codex",
            description="d",
            command="codex exec --json {prompt}",
            resume_command="codex exec --json resume {agent_id} {prompt}",
            id_source="derived",
            transcript_format="codex_jsonl",
            backend=backend,
            registry=registry,
        )

    def test_create_parses_id_and_reply_then_resumes(self) -> None:
        """Create parses thread_id + reply; resume reuses the id."""
        backend = _CannedBackend(stdout=_CODEX_JSONL)
        registry = _MemRegistry()
        tool = self._make_tool(backend, registry)

        first = asyncio.run(
            _collect(tool.call(prompt="draft", instance="w")),
        )
        second = asyncio.run(
            _collect(tool.call(prompt="revise", instance="w")),
        )

        aid = "019f75ec-c86f-7ee2-8d05-c8f3b2c08a38"
        self.assertEqual(first[-1].content[0].text, "FINAL REPLY")
        self.assertEqual(
            first[-1].metadata,
            {"instance": "w", "agent_id": aid, "action": "create"},
        )
        self.assertEqual(registry.store, {"w": aid})
        self.assertEqual(
            backend.calls,
            [
                ["codex", "exec", "--json", "draft"],
                ["codex", "exec", "--json", "resume", aid, "revise"],
            ],
        )
        self.assertEqual(
            second[-1].metadata,
            {"instance": "w", "agent_id": aid, "action": "resume"},
        )

    def test_parse_miss_warns_and_does_not_commit(self) -> None:
        """No thread.started → warning, no persistence, reply preserved."""
        backend = _CannedBackend(
            stdout=(
                b'{"type":"item.completed","item":'
                b'{"type":"agent_message","text":"HELLO"}}\n'
            ),
        )
        registry = _MemRegistry()
        tool = self._make_tool(backend, registry)

        chunks = asyncio.run(
            _collect(tool.call(prompt="draft", instance="w")),
        )
        text = chunks[-1].content[0].text
        self.assertTrue(text.startswith("HELLO"))
        self.assertIn("not resumable", text)
        self.assertEqual(registry.store, {})
        self.assertEqual(chunks[-1].metadata["agent_id"], None)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n ravenx python -m pytest tests/subagent_tool_test.py::CliSubAgentToolCodexJsonlTest -v`
Expected: FAIL — `CliSubAgentTool.__init__` has no `transcript_format` keyword (`TypeError`).

- [ ] **Step 3: Write minimal implementation**

In `src/agentscope/subagent/_tool.py`:

Add the import near the top (after the `..tool` imports):

```python
from ._transcript import parse_codex_jsonl
```

Add the constructor parameter. In `__init__`, insert `transcript_format` in the signature right after `output_pattern: str | None = None,`:

```python
        output_pattern: str | None = None,
        transcript_format: str = "text",
        cwd: str | None = None,
```

Document it in the `Args:` block (after the `output_pattern` entry):

```python
            transcript_format (`str`, defaults to `"text"`):
                Transcript parsing strategy: ``"text"`` (regex via
                ``session_id_pattern`` / ``output_pattern``) or
                ``"codex_jsonl"`` (parse a Codex ``exec --json`` stream).
```

Store it (after `self._output_re = ...`):

```python
        self._transcript_format = transcript_format
```

In `call()`, replace the success extraction block. Change the current steps 1 and 3:

```python
        # Success. 1) Parse the CLI-minted id for a derived create.
        if created and self._id_source == "derived":
            if self._session_id_re is not None:
                match = self._session_id_re.search(stdout)
                if match is not None:
                    agent_id = match.group(1)
```

to:

```python
        # Success. Parse a structured JSONL transcript once, if configured.
        jsonl_id: str | None = None
        jsonl_reply: str | None = None
        if self._transcript_format == "codex_jsonl":
            jsonl_id, jsonl_reply = parse_codex_jsonl(stdout)

        # 1) Determine the CLI-minted id for a derived create.
        if created and self._id_source == "derived":
            if self._transcript_format == "codex_jsonl":
                if jsonl_id is not None:
                    agent_id = jsonl_id
            elif self._session_id_re is not None:
                match = self._session_id_re.search(stdout)
                if match is not None:
                    agent_id = match.group(1)
```

And change the reply-extraction step:

```python
        # 3) Extract the reply, or fall back to the raw combined output.
        combined = stdout
        if stderr:
            combined = f"{combined}\n{stderr}" if combined else stderr
        if self._output_re is not None:
            match = self._output_re.search(stdout)
            output = match.group(1).strip() if match is not None else combined
        else:
            output = combined
```

to:

```python
        # 3) Extract the reply, or fall back to the raw combined output.
        combined = stdout
        if stderr:
            combined = f"{combined}\n{stderr}" if combined else stderr
        if self._transcript_format == "codex_jsonl":
            output = (
                jsonl_reply.strip() if jsonl_reply is not None else combined
            )
        elif self._output_re is not None:
            match = self._output_re.search(stdout)
            output = match.group(1).strip() if match is not None else combined
        else:
            output = combined
```

Leave steps 2 (deferred commit), 4 (parse-miss warning), and 5 (output_file + truncation) unchanged — they are format-independent.

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n ravenx python -m pytest tests/subagent_tool_test.py -v`
Expected: PASS (new codex_jsonl class + all pre-existing tool tests, including the `text`/regex `CliSubAgentToolDerivedTest`).

- [ ] **Step 5: Pre-commit + commit**

```bash
pre-commit run --files src/agentscope/subagent/_tool.py tests/subagent_tool_test.py
git add src/agentscope/subagent/_tool.py tests/subagent_tool_test.py
git commit -m "$(cat <<'EOF'
feat(subagent): wire codex_jsonl transcript parsing into the tool

When transcript_format is codex_jsonl, derive the session id and reply
from parse_codex_jsonl instead of the regexes.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Factory forwards `transcript_format` (Change 3d)

**Files:**
- Modify: `src/agentscope/subagent/_agent_tools.py`
- Test: `tests/subagent_agent_tools_test.py`

**Interfaces:**
- Consumes: `CliSubAgentTool(..., transcript_format=...)` (Task 4), `CliSubAgentConfig.transcript_format` (Task 2).

- [ ] **Step 1: Write the failing test**

In `tests/subagent_agent_tools_test.py`, extend the `_record` helper to carry `transcript_format`. Change its signature + `data` dict:

```python
def _record(
    subagent_id: str,
    name: str,
    command: str,
    cwd: str | None = None,
    resume_command: str | None = None,
    id_source: str = "provisioned",
    session_id_pattern: str | None = None,
    output_pattern: str | None = None,
    transcript_format: str = "text",
) -> SubAgentRecord:
    """Build a sub-agent record."""
    return SubAgentRecord(
        id=subagent_id,
        user_id="u1",
        data={
            "id": subagent_id,
            "type": "cli_subagent",
            "name": name,
            "description": "d",
            "command": command,
            "resume_command": resume_command,
            "id_source": id_source,
            "session_id_pattern": session_id_pattern,
            "output_pattern": output_pattern,
            "transcript_format": transcript_format,
            "cwd": cwd,
            "env": None,
            "timeout": 600,
        },
    )
```

Add a test to `class MakeSubAgentToolFactoryTest`:

```python
    async def test_transcript_format_is_forwarded(self) -> None:
        """A codex_jsonl config forwards transcript_format to the tool."""
        storage = _FakeStorage(
            [
                _record(
                    "sa-1",
                    "codex",
                    "codex exec --json {prompt}",
                    resume_command=(
                        "codex exec --json resume {agent_id} {prompt}"
                    ),
                    id_source="derived",
                    transcript_format="codex_jsonl",
                ),
            ],
        )
        wm = _FakeWorkspaceManager(None)
        factory = make_subagent_tool_factory(storage, wm)
        tools = await factory("u1", "a1", "s1")
        cli = [t for t in tools if isinstance(t, CliSubAgentTool)]
        self.assertEqual(len(cli), 1)
        self.assertEqual(cli[0]._transcript_format, "codex_jsonl")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `conda run -n ravenx python -m pytest tests/subagent_agent_tools_test.py::MakeSubAgentToolFactoryTest::test_transcript_format_is_forwarded -v`
Expected: FAIL — the factory does not pass `transcript_format`, so the tool defaults to `"text"` and the assertion `== "codex_jsonl"` fails.

- [ ] **Step 3: Write minimal implementation**

In `src/agentscope/subagent/_agent_tools.py`, in the `CliSubAgentTool(...)` construction, add the argument right after `output_pattern=config.output_pattern,`:

```python
                    output_pattern=config.output_pattern,
                    transcript_format=config.transcript_format,
                    cwd=config.cwd or session_workdir,
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n ravenx python -m pytest tests/subagent_agent_tools_test.py -v`
Expected: PASS (new test + all pre-existing factory tests).

- [ ] **Step 5: Pre-commit + commit**

```bash
pre-commit run --files src/agentscope/subagent/_agent_tools.py tests/subagent_agent_tools_test.py
git add src/agentscope/subagent/_agent_tools.py tests/subagent_agent_tools_test.py
git commit -m "$(cat <<'EOF'
feat(subagent): factory forwards transcript_format to the CLI tool

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: Codex preset → `--json` + `codex_jsonl` (Change 3e)

**Files:**
- Modify: `src/agentscope/subagent/_presets.py`
- Test: `tests/subagent_presets_test.py`

**Interfaces:**
- Consumes: `parse_codex_jsonl` (Task 3), the `transcript_format` field + validation (Task 2). Depends on Task 4 runtime being present so an instantiated Codex instance actually parses JSONL.

- [ ] **Step 1: Rewrite the failing test**

In `tests/subagent_presets_test.py`, replace the `import re` line + `_CODEX_SAMPLE` text constant with a JSONL sample and a parser import, and replace `test_codex_patterns_match_real_output` with a JSONL-format test. The new top-of-file:

```python
# -*- coding: utf-8 -*-
"""Tests for the built-in CLI sub-agent presets."""

import unittest

from agentscope.subagent import (
    CliSubAgentConfig,
    SubAgentFactory,
    list_subagent_presets,
)
from agentscope.subagent._transcript import parse_codex_jsonl

_CODEX_JSONL_SAMPLE = "\n".join(
    [
        '{"type":"thread.started",'
        '"thread_id":"019f75ec-c86f-7ee2-8d05-c8f3b2c08a38"}',
        '{"type":"item.completed","item":{"id":"item_0",'
        '"type":"agent_message","text":"FINAL REPLY"}}',
        '{"type":"turn.completed","usage":{"input_tokens":1}}',
    ],
)
```

Replace the `test_codex_patterns_match_real_output` method with:

```python
    def test_codex_preset_uses_json_and_jsonl_format(self) -> None:
        """The Codex preset uses --json and structured JSONL parsing."""
        codex = next(
            p for p in list_subagent_presets() if p["preset_id"] == "codex"
        )
        data = codex["data"]
        self.assertEqual(data["id_source"], "derived")
        self.assertEqual(data["transcript_format"], "codex_jsonl")
        self.assertIsNone(data["session_id_pattern"])
        self.assertIsNone(data["output_pattern"])
        self.assertIn("--json", data["command"])
        self.assertIn("--json", data["resume_command"])
        self.assertEqual(
            parse_codex_jsonl(_CODEX_JSONL_SAMPLE),
            ("019f75ec-c86f-7ee2-8d05-c8f3b2c08a38", "FINAL REPLY"),
        )
```

(Keep `test_preset_ids_and_labels` and `test_each_preset_round_trips_through_factory` unchanged.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n ravenx python -m pytest tests/subagent_presets_test.py -v`
Expected: FAIL — the preset still carries regex patterns / no `--json` / `transcript_format` absent (KeyError or assertion failure).

- [ ] **Step 3: Write minimal implementation**

Rewrite `src/agentscope/subagent/_presets.py`. Remove the two pattern constants (`_CODEX_SESSION_ID_PATTERN`, `_CODEX_OUTPUT_PATTERN`), add `--json` to both command constants, and update both preset payloads. The full file:

```python
# -*- coding: utf-8 -*-
"""Built-in CLI sub-agent presets (Claude Code, Codex).

Each preset is a ready-to-instantiate ``CliSubAgentConfig`` payload
(without an ``id``, which the create endpoint mints). The frontend's
"Add from preset" control POSTs a preset's ``data`` to
``POST /subagent/``.
"""

_CODEX_COMMAND = (
    "codex -a never exec -s workspace-write "
    "-c 'sandbox_workspace_write.network_access=true' --json {prompt}"
)
_CODEX_RESUME_COMMAND = (
    "codex -a never exec -s workspace-write "
    "-c 'sandbox_workspace_write.network_access=true' "
    "--json resume {agent_id} {prompt}"
)


def list_subagent_presets() -> list[dict]:
    """Return the built-in CLI sub-agent presets.

    Returns:
        `list[dict]`:
            One dict per preset with keys ``preset_id`` (stable id),
            ``label`` (human name), and ``data`` (a valid
            ``CliSubAgentConfig`` payload without an ``id``).
    """
    return [
        {
            "preset_id": "claude_code",
            "label": "Claude Code",
            "data": {
                "type": "cli_subagent",
                "name": "claude_code",
                "description": (
                    "Delegate a coding task to Claude Code. Stateful: a "
                    "new instance handle starts a fresh session; reusing "
                    "a handle resumes it."
                ),
                "command": (
                    "claude -p {prompt} --permission-mode auto "
                    "--session-id {agent_id}"
                ),
                "resume_command": (
                    "claude -p {prompt} --permission-mode auto "
                    "--resume {agent_id}"
                ),
                "id_source": "provisioned",
                "transcript_format": "text",
                "session_id_pattern": None,
                "output_pattern": None,
                "cwd": None,
                "env": None,
                "timeout": 600,
            },
        },
        {
            "preset_id": "codex",
            "label": "Codex",
            "data": {
                "type": "cli_subagent",
                "name": "codex",
                "description": (
                    "Delegate a coding task to OpenAI Codex. Stateful: "
                    "the session id and reply are parsed from the "
                    "`exec --json` JSONL stream and the id is reused on "
                    "resume."
                ),
                "command": _CODEX_COMMAND,
                "resume_command": _CODEX_RESUME_COMMAND,
                "id_source": "derived",
                "transcript_format": "codex_jsonl",
                "session_id_pattern": None,
                "output_pattern": None,
                "cwd": None,
                "env": None,
                "timeout": 600,
            },
        },
    ]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n ravenx python -m pytest tests/subagent_presets_test.py tests/subagent_router_test.py -v`
Expected: PASS. (The router test only asserts `preset_id`s and `codex["data"]["id_source"] == "derived"`, both unchanged.)

- [ ] **Step 5: Pre-commit + commit**

```bash
pre-commit run --files src/agentscope/subagent/_presets.py tests/subagent_presets_test.py
git add src/agentscope/subagent/_presets.py tests/subagent_presets_test.py
git commit -m "$(cat <<'EOF'
feat(subagent): Codex preset uses `exec --json` + codex_jsonl parsing

Adds --json to both commands and switches to structured transcript
parsing (no regex patterns).

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 7: Semantic instance-handle guidance (Change 1)

**Files:**
- Modify: `src/agentscope/subagent/_tool.py` (`_build_input_schema` instance description)
- Modify: `src/agentscope/subagent/_dag/_tool.py` (`_NODE_SCHEMA["instance"]` description + `_DEFAULT_DESCRIPTION`)
- Test: `tests/subagent_tool_test.py`, `tests/subagent_dag_tool_test.py`

**Interfaces:** none (description text only).

- [ ] **Step 1: Write the failing tests**

Add to `class CliSubAgentToolStatefulTest` in `tests/subagent_tool_test.py`:

```python
    def test_instance_description_encourages_semantic_name(self) -> None:
        """The stateful instance handle prompts for a semantic name."""
        tool, _ = self._make_tool()
        desc = tool.input_schema["properties"]["instance"]["description"]
        self.assertIn("researcher", desc)
```

Add to `class SubAgentDagToolTest` in `tests/subagent_dag_tool_test.py`:

```python
    async def test_node_instance_description_is_semantic(self) -> None:
        """The DAG node's instance handle prompts for a semantic name."""
        with tempfile.TemporaryDirectory() as workdir:
            tool = SubAgentDagTool(
                subagents={"s": _FakeSubAgent("s")},
                backend=LocalBackend(),
                workdir=workdir,
            )
            node_schema = tool.input_schema["properties"]["nodes"]["items"]
            desc = node_schema["properties"]["instance"]["description"]
            self.assertIn("researcher", desc)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `conda run -n ravenx python -m pytest tests/subagent_tool_test.py::CliSubAgentToolStatefulTest::test_instance_description_encourages_semantic_name tests/subagent_dag_tool_test.py::SubAgentDagToolTest::test_node_instance_description_is_semantic -v`
Expected: FAIL — the current descriptions do not contain "researcher".

- [ ] **Step 3: Write minimal implementation**

In `src/agentscope/subagent/_tool.py`, `_build_input_schema`, replace the `instance` property description:

```python
            properties["instance"] = {
                "type": "string",
                "description": (
                    "A stable handle for this sub-agent instance. Reuse "
                    "the same handle to continue the same conversation "
                    "(context is preserved); use a new handle to start a "
                    "fresh instance."
                ),
            }
```

with:

```python
            properties["instance"] = {
                "type": "string",
                "description": (
                    "A stable handle for this sub-agent instance. Choose a "
                    "short, semantic name that reflects the instance's "
                    "role (e.g. `researcher`, `db-migrator`), not a random "
                    "string. Reuse the same handle to continue the same "
                    "conversation (context is preserved); use a new handle "
                    "to start a fresh instance."
                ),
            }
```

In `src/agentscope/subagent/_dag/_tool.py`, replace the `instance` entry in `_NODE_SCHEMA`:

```python
        "instance": {
            "type": "string",
            "description": "Optional stateful sub-agent handle.",
        },
```

with:

```python
        "instance": {
            "type": "string",
            "description": (
                "Optional stateful sub-agent handle. Choose a short, "
                "semantic name reflecting the instance's role (e.g. "
                "`researcher`); reuse it across nodes to continue one "
                "conversation."
            ),
        },
```

And in the same file, update the `instance` sentence inside `_DEFAULT_DESCRIPTION`. Change:

```python
    "ids, optional 'inputs', and an optional 'instance' (a stable handle "
    "reusing the same stateful sub-agent conversation across nodes). "
```

to:

```python
    "ids, optional 'inputs', and an optional 'instance' (a stable, "
    "semantic handle - e.g. 'researcher' - reusing the same stateful "
    "sub-agent conversation across nodes). "
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `conda run -n ravenx python -m pytest tests/subagent_tool_test.py tests/subagent_dag_tool_test.py -v`
Expected: PASS (new assertions + all pre-existing tests).

- [ ] **Step 5: Pre-commit + commit**

```bash
pre-commit run --files src/agentscope/subagent/_tool.py src/agentscope/subagent/_dag/_tool.py tests/subagent_tool_test.py tests/subagent_dag_tool_test.py
git add src/agentscope/subagent/_tool.py src/agentscope/subagent/_dag/_tool.py tests/subagent_tool_test.py tests/subagent_dag_tool_test.py
git commit -m "$(cat <<'EOF'
feat(subagent): guide the agent to choose semantic instance handles

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: Frontend `transcript_format` carry-through (Change 3.7)

**Files:**
- Modify: `examples/web_ui/frontend/src/api/types.ts`
- Modify: `examples/web_ui/frontend/src/pages/subagent/index.tsx`

**Interfaces:**
- Produces: `SubAgentData.transcript_format?: 'text' | 'codex_jsonl'`; the form carries it invisibly through `FormState`/`toForm`/`toPayload`.

- [ ] **Step 1: Add the type field**

In `examples/web_ui/frontend/src/api/types.ts`, add to `SubAgentData` right after the `output_pattern?` line:

```typescript
	output_pattern?: string | null;
	transcript_format?: 'text' | 'codex_jsonl';
	cwd?: string | null;
```

- [ ] **Step 2: Carry it through the form**

In `examples/web_ui/frontend/src/pages/subagent/index.tsx`:

`FormState` — add after `output_pattern: string;`:

```typescript
	session_id_pattern: string;
	output_pattern: string;
	transcript_format: 'text' | 'codex_jsonl';
}
```

`EMPTY_FORM` — add after `output_pattern: '',`:

```typescript
	session_id_pattern: '',
	output_pattern: '',
	transcript_format: 'text',
};
```

`toForm` return — add after `output_pattern: view.data.output_pattern ?? '',`:

```typescript
		session_id_pattern: view.data.session_id_pattern ?? '',
		output_pattern: view.data.output_pattern ?? '',
		transcript_format: view.data.transcript_format ?? 'text',
	};
```

`toPayload` return — add after `output_pattern: form.output_pattern.trim() || null,`:

```typescript
		session_id_pattern: form.session_id_pattern.trim() || null,
		output_pattern: form.output_pattern.trim() || null,
		transcript_format: form.transcript_format,
		cwd: form.cwd.trim() || null,
```

(No new rendered input; no new i18n key. Add-from-preset POSTs raw `preset.data`, so it already carries the field.)

- [ ] **Step 3: Build, lint, and i18n parity**

```bash
pnpm -C examples/web_ui/frontend build
pnpm -C examples/web_ui/frontend lint
node -e "const en=require('./examples/web_ui/frontend/src/i18n/locales/en.json');const zh=require('./examples/web_ui/frontend/src/i18n/locales/zh.json');const flat=(o,p='')=>Object.entries(o).flatMap(([k,v])=>typeof v==='object'&&v?flat(v,p+k+'.'):[p+k]);const a=new Set(flat(en)),b=new Set(flat(zh));const miss=[...a].filter(x=>!b.has(x)).concat([...b].filter(x=>!a.has(x)));if(miss.length){console.error('i18n key mismatch:',miss);process.exit(1)}console.log('i18n parity OK')"
```

Expected: build succeeds (tsc + vite), eslint clean, `i18n parity OK`.

- [ ] **Step 4: Commit**

```bash
git add examples/web_ui/frontend/src/api/types.ts examples/web_ui/frontend/src/pages/subagent/index.tsx
git commit -m "$(cat <<'EOF'
feat(web-ui): carry transcript_format through the sub-agent form

Invisible carry (same as id_source/session_id_pattern/output_pattern) so
an edit->save of a Codex prototype does not drop the field.

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Final verification (after all tasks)

- [ ] Run the whole sub-agent backend suite:

```bash
conda run -n ravenx python -m pytest tests/subagent_config_test.py tests/subagent_factory_test.py tests/subagent_transcript_test.py tests/subagent_tool_test.py tests/subagent_agent_tools_test.py tests/subagent_presets_test.py tests/subagent_router_test.py tests/subagent_dag_tool_test.py -v
```

Expected: all green.

- [ ] Dispatch the final whole-branch review (superpowers:requesting-code-review) against `git merge-base main HEAD`.

## Notes for the executor

- Tasks 3 → 4 → 5 → 6 are ordered by dependency (parser → runtime → wiring → preset). Task 2 must precede Tasks 5 and 6 (config field + validation). Tasks 1, 7, 8 are independent and may run any time, but keep the numeric order for clean review.
- Do NOT stage `.webapp_logs/backend.pid` / `frontend.pid` (running services). Never `git add -A`.
- If `SubAgentRecord` is not importable in a test env, it is `from agentscope.app.storage import SubAgentRecord` (already used by `tests/subagent_agent_tools_test.py`).
