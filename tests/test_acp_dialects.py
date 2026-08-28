"""Reading each ACP adapter's tool calls in the transport's own vocabulary.

Every payload here is a frame copied from a live journal (claude-agent-acp
0.66.0, codex-acp 1.1.14), trimmed only of fields none of this reads. What the
tests assert is what the record needs: the transport's own tool name, the
subject to show beside it, and output with no transport wrapping left on it.
Naming that call in raven's vocabulary belongs to the read boundary, and to
``test_subagent_tool_vocabulary.py``.
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

    assert spec.tool_name({"kind": "execute", "title": "Terminal"}) == "execute"
    assert spec.tool_name({"kind": "read", "title": "Read file '/tmp/a'"}) == "read"
    assert spec.tool_name({"kind": "search"}) == "search"
    assert spec.tool_name({"kind": "fetch"}) == "fetch"
    # No kind at all: unclassified rather than guessed from the title.
    assert spec.tool_name({"title": "Terminal"}) == "tool_call"


def test_the_subject_falls_back_to_locations_when_there_is_no_input() -> None:
    """codex-acp's ``read`` carries the path nowhere else.

    Its opening frame has no ``rawInput`` key whatsoever; ``locations`` is the
    spec's own answer and the only place the path exists. ``subject_field``
    reads it there and keys it ``path`` -- ``arguments_json``'s own literal
    ``argument`` key is the fallback for a subject with no field of its own,
    and this one has one.
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

    assert call.name == "commandExecution.read"
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
    assert revised.name == "Bash"
    assert revised.subject == "find . -name '*.json'"


def test_an_empty_raw_input_is_not_a_revision() -> None:
    """Every adapter measured sends one on the opening frame."""
    assert AcpDialect().revises_call({"rawInput": {}}) is False
    assert AcpDialect().revises_call({"toolCallId": "t1", "status": "completed"}) is False
    assert AcpDialect().revises_call({"locations": [{"path": "/a"}]}) is True


def test_claude_names_the_tool_it_actually_ran() -> None:
    """``kind`` cannot tell Glob from Grep; ``_meta.claudeCode.toolName`` can."""
    dialect = ClaudeCodeDialect()

    assert dialect.tool_name({"_meta": {"claudeCode": {"toolName": "Glob"}}, "kind": "search"}) == "Glob"
    assert dialect.tool_name({"_meta": {"claudeCode": {"toolName": "Grep"}}, "kind": "search"}) == "Grep"
    # No table to fall out of: a name nobody has measured is reported as sent.
    named = {"_meta": {"claudeCode": {"toolName": "SomethingNew"}}, "kind": "execute"}
    assert dialect.tool_name(named) == "SomethingNew"


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

    assert call.name == "commandExecution"
    assert call.subject == command
    assert json.loads(call.arguments_json()) == {"command": command, "cwd": "/repo/ui-tui"}


def test_the_arguments_keep_every_field_the_adapter_sent() -> None:
    """The subject is not re-keyed here any more, and nothing beside it is lost."""
    call = AcpDialect().call({"kind": "read", "rawInput": {"filePath": "/w/note.txt", "limit": 20}})

    assert json.loads(call.arguments_json()) == {"filePath": "/w/note.txt", "limit": 20}


def test_an_unclassified_call_keeps_the_adapters_own_field_names() -> None:
    """Inventing a generic key beside them would name the subject twice."""
    call = AcpDialect().call({"title": "whatever", "rawInput": {"filePath": "/w/note.txt"}})

    assert call.name == "tool_call"
    assert json.loads(call.arguments_json()) == {"filePath": "/w/note.txt"}


def test_a_call_labels_itself_with_the_verb_and_the_target() -> None:
    """Either half alone loses it: eighteen bare ``execute``, or a command as a title."""
    dialect = AcpDialect()

    assert dialect.call({"kind": "read", "rawInput": {"path": "src/a.py"}}).label == "read src/a.py"
    assert dialect.call({"kind": "think", "title": ""}).label == "think"
    assert dialect.call({"title": ""}).label == "tool_call"

    long_pipeline = "find . -type f " + "-o -name '*.py' " * 20
    label = dialect.call({"kind": "execute", "rawInput": {"command": long_pipeline}}).label
    assert len(label) <= 120
    assert label.startswith("execute find . -type f")


def test_the_base_dialect_reports_the_specs_own_kind() -> None:
    """codex-acp sends no tool name of its own, so ``kind`` is the finest the
    transport offers. Stored verbatim; the read boundary maps it."""
    assert AcpDialect().tool_name({"kind": "execute"}) == "execute"
    assert AcpDialect().tool_name({"kind": "read"}) == "read"


def test_a_kindless_update_is_the_no_name_case() -> None:
    assert AcpDialect().tool_name({}) == "tool_call"


def test_a_raven_adapter_states_the_tool_name_on_its_meta() -> None:
    """The finest grain there is, and the one that survives a stale kind table.

    ``glob`` is filed under no kind by a raven build that renamed ``find``, so
    the kind alone reads "other" for a call the adapter can name exactly.
    """
    update = {"kind": "other", "title": "glob: src/**/*.ts", "_meta": {"raven.toolName": "glob"}}

    assert AcpDialect().tool_name(update) == "glob"


def test_a_kind_that_names_nothing_falls_back_to_the_title() -> None:
    """The measured raven-code build: ``glob`` is absent from its kind table.

    ``other`` is the one kind worth going behind, because it is the one that
    says nothing. Its title is ``"<name>: <subject>"``, so the name is there.
    """
    spec = AcpDialect()

    assert spec.tool_name({"kind": "other", "title": "glob: src/**/*.ts"}) == "glob"
    assert spec.tool_name({"kind": "other", "title": "todowrite"}) == "todowrite"


def test_a_specific_kind_is_never_second_guessed_by_a_title() -> None:
    """Nine of the ten kinds say something; only ``other`` does not."""
    spec = AcpDialect()

    assert spec.tool_name({"kind": "execute", "title": "Terminal"}) == "execute"
    assert spec.tool_name({"kind": "read", "title": "Read: /tmp/a"}) == "read"


def test_a_prose_title_leaves_an_unspecific_call_unspecific() -> None:
    """A title is written for a person, and most adapters spend it on prose.

    Guessing a tool name out of a sentence is worse than the generic kind: the
    row would name a tool nothing ever ran.
    """
    spec = AcpDialect()

    assert spec.tool_name({"kind": "other", "title": "Ran the tests"}) == "other"
    assert spec.tool_name({"kind": "other", "title": "Generating image (1024x1024)"}) == "other"
    assert spec.tool_name({"kind": "other"}) == "other"


def test_the_claude_dialect_reports_claudes_own_tool_name() -> None:
    update = {"kind": "search", "_meta": {"claudeCode": {"toolName": "Glob"}}}

    assert ClaudeCodeDialect().tool_name(update) == "Glob"


def test_the_claude_plan_row_carries_claude_codes_own_name() -> None:
    """`TodoWrite` never reaches a tool_call: the adapter's `shouldEmitToolCall`
    excludes it and routes its state to a `plan` frame. The row that frame
    becomes is named for the tool that produced it.
    """
    assert ClaudeCodeDialect().plan_tool_name == "TodoWrite"


def test_arguments_keep_the_adapters_own_spelling() -> None:
    """No key is renamed at write time any more."""
    call = AcpDialect().call({"toolCallId": "t1", "kind": "read", "rawInput": {"filePath": "src/a.py"}})

    assert json.loads(call.arguments_json()) == {"filePath": "src/a.py"}


from tests import acp_frames


def test_a_claude_frame_without_kind_is_still_named_by_its_meta() -> None:
    """The opening frame carries `kind`; 146 of 350 captured tool frames do not,
    and `_meta.claudeCode.toolName` is the only name on those.
    """
    assert ClaudeCodeDialect().tool_name(acp_frames.CLAUDE_UPDATE_WITHOUT_KIND) == "Bash"


def test_a_claude_bash_call_keeps_both_its_command_and_its_description() -> None:
    """The row shows the description and the expanded block shows the command,
    so both have to survive into the stored arguments.
    """
    call = ClaudeCodeDialect().call(acp_frames.CLAUDE_BASH_WITH_DESCRIPTION)
    assert call.name == "Bash"
    assert call.argument == "cd /repo/ui-tui && ls i18n"
    assert json.loads(call.arguments_json())["description"] == "Locate messages.json and i18n dirs"


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


def test_codex_prefers_the_parsed_command_over_the_bash_argument() -> None:
    """`rawInput.command` is the quoted bash argument; `commandActions` is clean."""
    assert CodexDialect().permission_command(acp_frames.CODEX_READ_PERMISSION) == "sed -n '1,200p' calc.py"


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
        codex.tool_name(
            {
                "kind": "execute",
                "_meta": {"is_mcp_tool_call": True},
                "rawInput": {"server": "fs", "tool": "read", "arguments": {}},
            }
        )
        == "mcp.fs.read"
    )
    assert (
        codex.tool_name(
            {"kind": "other", "title": "Start subagent docs", "_meta": {"codex": {"subagent": {"path": "a/docs"}}}}
        )
        == "subAgentActivity"
    )
    assert (
        codex.tool_name(
            {"kind": "other", "title": "handoff", "_meta": {"codex": {"collaboration": {"tool": "handoff"}}}}
        )
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
    assert codex.subject_field(
        {"kind": "read", "title": "View Image /tmp/a.png", "rawInput": {"path": "/tmp/a.png"}}
    ) == ("path", "/tmp/a.png")


def test_a_codex_call_puts_its_subject_first_in_the_arguments() -> None:
    codex = CodexDialect()

    call = codex.call(acp_frames.CODEX_WEBSEARCH_DONE)
    fields = json.loads(call.arguments_json())

    assert next(iter(fields)) == "query"
    assert fields["query"].startswith("site:developers.openai.com/codex Codex overview")
    # The adapter's own fields survive beside it; the record keeps what it sent.
    assert fields["type"] == "webSearch"


def test_codex_reads_the_patched_files_out_of_the_envelope() -> None:
    """`apply_patch` names itself as the command; the files are in the result."""
    codex = CodexDialect()

    assert codex.subject_from_result(acp_frames.CODEX_PATCH_DONE, name="apply_patch") == "calc.py"
    assert codex.subject_from_result(acp_frames.CODEX_READ_DONE, name="apply_patch") is None


def test_codex_takes_an_images_path_but_never_its_prompt() -> None:
    """`savedPath` is a path; `revisedPrompt` is prose and would be keyed as one.

    Synthetic -- no capture reached image generation. Shape from the adapter's
    `imageGenerationRawOutput`.
    """
    codex = CodexDialect()
    raw = {"status": "completed", "revisedPrompt": "a red bicycle", "savedPath": "/w/bike.png"}

    assert codex.subject_from_result({"rawOutput": raw}, name="imageGeneration") == "/w/bike.png"
    assert (
        codex.subject_from_result(
            {"rawOutput": {k: v for k, v in raw.items() if k != "savedPath"}}, name="imageGeneration"
        )
        is None
    )


def test_codex_results_lose_the_terminals_crlf() -> None:
    codex = CodexDialect()

    text = codex.result(acp_frames.CODEX_PATCH_DONE).text

    assert "\r" not in text
    assert "*** Update File: calc.py" in text


def test_codex_reads_the_paths_a_native_file_change_touched() -> None:
    """A native `fileChange` frame carries no `rawInput`; its diff blocks do.

    Synthetic -- `fileChange` never fired in either capture. Shape from the
    adapter's `createFileChangeUpdate` and its `createAddFileContent` /
    `createUpdateFileContent` / `createDeleteFileContent` diff-block builders,
    each of which stamps its own `path` beside `type: "diff"`.
    """
    update = {
        "sessionUpdate": "tool_call",
        "toolCallId": "edit-1",
        "kind": "edit",
        "title": "Editing files",
        "status": "completed",
        "content": [
            {"type": "diff", "oldText": None, "newText": "a\n", "path": "src/a.py", "_meta": {"kind": "add"}},
            {"type": "diff", "oldText": "b\n", "newText": None, "path": "src/b.py", "_meta": {"kind": "delete"}},
        ],
    }
    codex = CodexDialect()

    assert codex.subject_field(update) == ("path", "src/a.py, src/b.py")
    assert codex.call(update).subject == "src/a.py, src/b.py"


def test_codex_reads_a_single_file_changes_path() -> None:
    """One diff block is the common case; the join must not add a stray comma."""
    update = {
        "kind": "edit",
        "title": "Editing files",
        "content": [
            {"type": "diff", "oldText": "old\n", "newText": "new\n", "path": "notes.txt", "_meta": {"kind": "update"}}
        ],
    }

    assert CodexDialect().subject_field(update) == ("path", "notes.txt")


def test_a_file_change_with_no_path_anywhere_keeps_the_truthful_key() -> None:
    """No diff block carries a usable path: the key must still be `path`.

    Falling through to the generic tail is the defect this guards against --
    it read `argument` off the fixed title instead.
    """
    update = {
        "kind": "edit",
        "title": "Editing files",
        "content": [{"type": "diff", "oldText": "x", "newText": "y"}],
    }

    assert CodexDialect().subject_field(update) == ("path", "")


def test_claude_code_merges_a_question_with_its_custom_box() -> None:
    from raven.agent.acp.elicitation import fields
    from raven.agent.subagent.acp_dialects.claude_code import ClaudeCodeDialect

    schema = {
        "type": "object",
        "properties": {
            "question_0": {
                "type": "string",
                "title": "Which backend?",
                "oneOf": [{"const": "redis"}, {"const": "memcached"}],
            },
            "question_0_custom": {"type": "string", "title": "Other"},
            "question_1": {
                "type": "string",
                "title": "Which TTL?",
                "oneOf": [{"const": "60"}, {"const": "600"}],
            },
            "question_1_custom": {"type": "string", "title": "Other"},
        },
    }
    merged = ClaudeCodeDialect().pair_fields(fields(schema))
    assert [f.name for f in merged] == ["question_0", "question_1"]
    assert [f.custom_name for f in merged] == ["question_0_custom", "question_1_custom"]
    assert merged[0].options == ["redis", "memcached"]


def test_claude_code_leaves_an_unpaired_custom_field_alone() -> None:
    from raven.agent.acp.elicitation import fields
    from raven.agent.subagent.acp_dialects.claude_code import ClaudeCodeDialect

    schema = {"type": "object", "properties": {"notes_custom": {"type": "string"}}}
    merged = ClaudeCodeDialect().pair_fields(fields(schema))
    assert [f.name for f in merged] == ["notes_custom"]
    assert merged[0].custom_name is None


def test_the_default_dialect_pairs_nothing() -> None:
    from raven.agent.acp.elicitation import fields
    from raven.agent.subagent.acp_dialects.base import AcpDialect

    schema = {
        "type": "object",
        "properties": {"a": {"type": "string", "enum": ["x"]}, "a_custom": {"type": "string"}},
    }
    assert [f.name for f in AcpDialect().pair_fields(fields(schema))] == ["a", "a_custom"]


def test_claude_code_merges_a_custom_box_written_before_its_question() -> None:
    """`properties` order is the adapter's own, not a contract this can lean on.

    Folding decided while walking the properties in order cannot see a sibling
    written ahead of its question: that one survives as a question of its own,
    so the user is asked the same thing twice and the free-text box arrives as
    a standalone prompt -- the exact shape the merge exists to prevent.
    """
    from raven.agent.acp.elicitation import fields

    schema = {
        "type": "object",
        "properties": {
            "question_0_custom": {"type": "string", "title": "Other"},
            "question_0": {
                "type": "string",
                "title": "Which backend?",
                "oneOf": [{"const": "redis"}, {"const": "memcached"}],
            },
        },
    }
    merged = ClaudeCodeDialect().pair_fields(fields(schema))
    assert [f.name for f in merged] == ["question_0"]
    assert merged[0].custom_name == "question_0_custom"


def test_claude_code_pairs_by_the_adapters_marker_not_by_the_spelling() -> None:
    """The adapter states the pairing outright, so the spelling need not match.

    `_meta._askUserQuestionCustomAnswer.questionId` names the property the
    free-text half belongs to; a sibling named anything at all still folds.
    """
    from raven.agent.acp.elicitation import fields

    schema = {
        "type": "object",
        "properties": {
            "pick": {"type": "string", "title": "Backend", "oneOf": [{"const": "redis"}, {"const": "memcached"}]},
            "free_form": {
                "type": "string",
                "title": "Other",
                "_meta": {"_askUserQuestionCustomAnswer": {"questionId": "pick", "isCustomAnswer": True}},
            },
        },
    }
    merged = ClaudeCodeDialect().pair_fields(fields(schema))
    assert [f.name for f in merged] == ["pick"]
    assert merged[0].custom_name == "free_form"


def test_claude_code_leaves_an_unmarked_lookalike_as_a_question_of_its_own() -> None:
    """A marked request has said which siblings are pairs; the rest are not.

    `<name>_custom` also spells an independent free-text property that merely
    shares a prefix, and folding that one loses a question the schema asked:
    it is never put to the user, and its answer only ever surfaces if the enum
    half happens to be answered off-enum.
    """
    from raven.agent.acp.elicitation import fields

    schema = {
        "type": "object",
        "properties": {
            "question_0": {"type": "string", "title": "Backend", "oneOf": [{"const": "redis"}]},
            "question_0_custom": {
                "type": "string",
                "title": "Other",
                "_meta": {"_askUserQuestionCustomAnswer": {"questionId": "question_0", "isCustomAnswer": True}},
            },
            "shipping_method": {"type": "string", "title": "Shipping", "oneOf": [{"const": "air"}]},
            "shipping_method_custom": {
                "type": "string",
                "title": "Custom shipping instructions",
                "description": "Anything the carrier needs to know.",
            },
        },
    }
    merged = ClaudeCodeDialect().pair_fields(fields(schema))
    assert [f.name for f in merged] == ["question_0", "shipping_method", "shipping_method_custom"]
    assert [f.custom_name for f in merged] == ["question_0_custom", None, None]


def test_claude_code_does_not_fold_a_custom_box_the_schema_requires() -> None:
    """Folding keeps only the survivor's `required`, so a demanded key would go.

    An on-enum answer would then be accepted as content missing a key the
    `requestedSchema` lists as required. Two questions is the lesser cost.
    """
    from raven.agent.acp.elicitation import fields

    schema = {
        "type": "object",
        "required": ["backend_custom"],
        "properties": {
            "backend": {"type": "string", "title": "Backend", "oneOf": [{"const": "redis"}, {"const": "memcached"}]},
            "backend_custom": {"type": "string", "title": "Other"},
        },
    }
    merged = ClaudeCodeDialect().pair_fields(fields(schema))
    assert [f.name for f in merged] == ["backend", "backend_custom"]
    assert [f.custom_name for f in merged] == [None, None]
