"""Reading each ACP adapter's tool calls into raven's own vocabulary.

Every payload here is a frame copied from a live journal (claude-agent-acp
0.66.0, codex-acp 1.1.14), trimmed only of fields none of this reads. What the
tests assert is the thing the renderer needs: a raven tool name, the subject to
show beside it, and output with no transport wrapping left on it.
"""

from __future__ import annotations

import json

from raven.agent.subagent.acp_dialects import AcpDialect, ClaudeCodeDialect, CodexDialect, dialect_for


def test_the_dialect_is_chosen_from_the_agents_own_handshake() -> None:
    """``agentInfo.name`` names the adapter; config never has to."""
    claude = dialect_for({"agentInfo": {"name": "@agentclientprotocol/claude-agent-acp", "version": "0.66.0"}})
    codex = dialect_for({"agentInfo": {"name": "@agentclientprotocol/codex-acp", "version": "1.1.14"}})

    assert isinstance(claude, ClaudeCodeDialect)
    assert isinstance(codex, CodexDialect)


def test_an_unmeasured_adapter_gets_the_spec_and_not_a_guess() -> None:
    """OpenCode reported no tool call in any capture, so it has no dialect.

    The base class reads only fields the protocol requires, which is why an
    adapter nobody has measured works without a file of its own.
    """
    assert type(dialect_for({"agentInfo": {"name": "OpenCode", "version": "1.18.16"}})) is AcpDialect
    assert type(dialect_for(None)) is AcpDialect
    assert type(dialect_for({})) is AcpDialect


def test_the_spec_kind_names_the_tool_not_the_title() -> None:
    """The title means different things per adapter; ``kind`` does not.

    Measured: the same ``kind: "execute"`` arrives titled "Terminal" from
    claude-acp and titled with the whole shell pipeline from codex-acp. Reading
    the title put a 100-character command where a verb belongs.
    """
    spec = AcpDialect()

    assert spec.tool_name({"kind": "execute", "title": "Terminal"}) == "exec"
    assert spec.tool_name({"kind": "read", "title": "Read file '/tmp/a'"}) == "read_file"
    assert spec.tool_name({"kind": "search"}) == "grep"
    assert spec.tool_name({"kind": "fetch"}) == "web_fetch"
    # No kind at all: unclassified rather than guessed from the title.
    assert spec.tool_name({"title": "Terminal"}) == "tool_call"


def test_the_subject_falls_back_to_locations_when_there_is_no_input() -> None:
    """codex-acp's ``read`` carries the path nowhere else.

    Its opening frame has no ``rawInput`` key whatsoever; ``locations`` is the
    spec's own answer and the only place the path exists.
    """
    update = {
        "sessionUpdate": "tool_call",
        "toolCallId": "exec-ae18",
        "status": "in_progress",
        "kind": "read",
        "title": "Read file '/repo/bridge",
        "locations": [{"path": "/repo/bridge/tsconfig.json"}],
    }
    call = CodexDialect().call(update)

    assert call.name == "read_file"
    assert call.subject == "/repo/bridge/tsconfig.json"
    assert json.loads(call.arguments_json()) == {"path": "/repo/bridge/tsconfig.json"}


def test_a_call_with_no_argument_anywhere_still_shows_its_title() -> None:
    """An ACP ``think`` or ``other`` call is titled and carries no input."""
    call = AcpDialect().call({"kind": "think", "title": "Considering the plan", "toolCallId": "x"})

    assert call.argument == ""
    assert call.subject == "Considering the plan"
    assert json.loads(call.arguments_json()) == {"argument": "Considering the plan"}


def test_the_title_never_outlives_a_real_argument() -> None:
    """The subject is resolved at read time, not frozen when the call opens.

    claude-agent-acp opens every call with ``rawInput: {}``; baking the title in
    as the argument made "Terminal" a real value that the later frame could no
    longer replace.
    """
    dialect = ClaudeCodeDialect()
    opening = dialect.call(
        {
            "_meta": {"claudeCode": {"toolName": "Bash"}},
            "toolCallId": "toolu_019",
            "sessionUpdate": "tool_call",
            "rawInput": {},
            "status": "pending",
            "title": "Terminal",
            "kind": "execute",
            "content": [],
        }
    )

    assert opening.argument == ""
    assert opening.subject == "Terminal"

    revising = {
        "_meta": {"claudeCode": {"toolName": "Bash"}},
        "toolCallId": "toolu_019",
        "sessionUpdate": "tool_call_update",
        "kind": "execute",
        "title": "Terminal",
        "rawInput": {"command": "find . -name '*.json'", "description": "Find json files"},
    }
    assert dialect.revises_call(revising) is True
    revised = dialect.call(revising)
    assert revised.name == "exec"
    assert revised.subject == "find . -name '*.json'"


def test_an_empty_raw_input_is_not_a_revision() -> None:
    """Every adapter measured sends one on the opening frame."""
    assert AcpDialect().revises_call({"rawInput": {}}) is False
    assert AcpDialect().revises_call({"toolCallId": "t1", "status": "completed"}) is False
    assert AcpDialect().revises_call({"locations": [{"path": "/a"}]}) is True


def test_claude_names_the_tool_it_actually_ran() -> None:
    """``kind`` cannot tell Glob from Grep; ``_meta.claudeCode.toolName`` can."""
    dialect = ClaudeCodeDialect()

    assert dialect.tool_name({"_meta": {"claudeCode": {"toolName": "Glob"}}, "kind": "search"}) == "find"
    assert dialect.tool_name({"_meta": {"claudeCode": {"toolName": "Grep"}}, "kind": "search"}) == "grep"
    # A name this table has never seen degrades to the spec answer.
    assert dialect.tool_name({"_meta": {"claudeCode": {"toolName": "SomethingNew"}}, "kind": "execute"}) == "exec"


def test_claude_results_lose_the_fence_the_adapter_added() -> None:
    """It sends the output twice: plain in ``rawOutput``, fenced in ``content``.

    The fence is for a markdown client. A transcript row is not one, and the
    literal ```` ```console ```` was ending up in the rendered output.
    """
    update = {
        "toolCallId": "toolu_019",
        "sessionUpdate": "tool_call_update",
        "status": "completed",
        "rawOutput": "./i18n/messages.json\n---i18n dirs---\n./i18n",
        "content": [
            {
                "type": "content",
                "content": {"type": "text", "text": "```console\n./i18n/messages.json\n---i18n dirs---\n./i18n\n```"},
            }
        ],
    }
    result = ClaudeCodeDialect().result(update)

    assert result.ok is True
    assert result.text == "./i18n/messages.json\n---i18n dirs---\n./i18n"
    assert "```" not in result.text


def test_claude_unfences_content_when_that_is_all_it_sent() -> None:
    """A result with no rawOutput still must not keep its fence."""
    update = {
        "status": "completed",
        "content": [{"type": "content", "content": {"type": "text", "text": "```\n1\thello\n```"}}],
    }

    assert ClaudeCodeDialect().result(update).text == "1\thello"


def test_a_fence_inside_a_result_is_left_alone() -> None:
    """Only a fence wrapping the whole payload is the adapter's."""
    text = "see below:\n```python\nprint(1)\n```\nthat is all"
    update = {"status": "completed", "content": [{"type": "content", "content": {"type": "text", "text": text}}]}

    assert ClaudeCodeDialect().result(update).text == text


def test_codex_results_are_unwrapped_from_their_envelope() -> None:
    """It sends no ``content`` at all, only an object under ``rawOutput``.

    Serialising that object is what put ``{"formatted_output": ...}`` in the
    transcript where the command's output belonged.
    """
    update = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "exec-3ace",
        "status": "completed",
        "rawOutput": {"formatted_output": "/repo/ui-tui\n../bridge\n", "exit_code": 0},
    }
    result = CodexDialect().result(update)

    assert result.ok is True
    assert result.text == "/repo/ui-tui\n../bridge\n"


def test_codex_reports_a_failed_command_through_its_exit_code() -> None:
    """The frame's own status is ``completed`` for a command that exited non-zero.

    The *call* completed; the command did not. Reading the status alone marked
    every failed command in the transcript as a success.
    """
    update = {
        "status": "completed",
        "rawOutput": {"formatted_output": "ls: /nope: No such file or directory\n", "exit_code": 2},
    }
    result = CodexDialect().result(update)

    assert result.ok is False
    assert result.text == "ls: /nope: No such file or directory\n"


def test_codex_says_so_when_a_command_printed_nothing() -> None:
    """An empty formatted_output is the real answer, so the exit code stands in."""
    result = CodexDialect().result({"status": "completed", "rawOutput": {"formatted_output": "", "exit_code": 0}})

    assert result.ok is True
    assert result.text == "(no output, exit 0)"


def test_codex_prefers_the_command_over_the_titles_truncation_of_it() -> None:
    """The title is clipped to about 100 characters on the wire."""
    command = (
        "pwd && find . -maxdepth 2 -type d -name 'bridge' -print && find .. -maxdepth 2 -type d -name 'bridge' -print"
    )
    update = {
        "sessionUpdate": "tool_call",
        "toolCallId": "exec-3ace",
        "kind": "execute",
        "title": command[:100],
        "rawInput": {"command": command, "cwd": "/repo/ui-tui"},
    }
    call = CodexDialect().call(update)

    assert call.name == "exec"
    assert call.subject == command
    assert json.loads(call.arguments_json()) == {"command": command, "cwd": "/repo/ui-tui"}


def test_the_subject_is_renamed_onto_ravens_key_and_not_repeated() -> None:
    """``{"path": x}``, never ``{"path": x, "filePath": x}``."""
    call = AcpDialect().call({"kind": "read", "rawInput": {"filePath": "/w/note.txt", "limit": 20}})

    assert json.loads(call.arguments_json()) == {"path": "/w/note.txt", "limit": 20}


def test_an_unclassified_call_keeps_the_adapters_own_field_names() -> None:
    """Inventing a generic key beside them would name the subject twice."""
    call = AcpDialect().call({"title": "whatever", "rawInput": {"filePath": "/w/note.txt"}})

    assert call.name == "tool_call"
    assert json.loads(call.arguments_json()) == {"filePath": "/w/note.txt"}


def test_a_call_labels_itself_with_the_verb_and_the_target() -> None:
    """Either half alone loses it: eighteen bare ``exec``, or a command as a title."""
    dialect = AcpDialect()

    assert dialect.call({"kind": "read", "rawInput": {"path": "src/a.py"}}).label == "read_file src/a.py"
    assert dialect.call({"kind": "think", "title": ""}).label == "think"
    assert dialect.call({"title": ""}).label == "tool_call"

    long_pipeline = "find . -type f " + "-o -name '*.py' " * 20
    label = dialect.call({"kind": "execute", "rawInput": {"command": long_pipeline}}).label
    assert len(label) <= 120
    assert label.startswith("exec find . -type f")
