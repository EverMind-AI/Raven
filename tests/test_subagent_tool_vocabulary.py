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


def test_an_empty_sibling_value_is_dropped() -> None:
    """The write path this replaced never sent an empty field."""
    out = normalize_row(_call("Bash", {"command": "ls", "cwd": None, "extra": ""}))
    fn = out["tool_calls"][0]["function"]
    assert json.loads(fn["arguments"]) == {"command": "ls"}


def test_a_subject_stored_under_argument_reaches_the_tools_own_key() -> None:
    """codex-acp sends no ``rawInput`` for a read, so the path lives only in
    the frame's ``locations`` and the write side stores it under ``argument``.
    """
    out = normalize_row(_call("read", {"argument": "src/a.py"}))
    fn = out["tool_calls"][0]["function"]
    assert fn["name"] == "read_file"
    assert json.loads(fn["arguments"]) == {"path": "src/a.py"}


def test_a_non_string_under_the_tools_own_key_leaves_the_payload_alone() -> None:
    """This is the openai ``web_search`` shape: nothing is promotable and
    nothing may be dropped.
    """
    out = normalize_row(_call("web_search", {"query": ["a", "b"], "n": 3}))
    fn = out["tool_calls"][0]["function"]
    assert fn["name"] == "web_search"
    assert json.loads(fn["arguments"]) == {"query": ["a", "b"], "n": 3}


def test_the_subject_is_the_first_key_on_the_wire() -> None:
    """A reader that does not know the tool takes the first string value, so
    dict equality alone would not pin this.
    """
    out = normalize_row(_call("read", {"note": "x", "filePath": "src/a.py"}))
    fn = out["tool_calls"][0]["function"]
    assert list(json.loads(fn["arguments"]))[0] == "path"


def test_a_call_without_a_function_object_passes_through() -> None:
    """A dict entry missing ``function`` has nothing to normalize; it must be
    left alone rather than gaining a ``{"name": None, "arguments": None}``
    function of its own.
    """
    call = {"id": "c1", "type": "function"}
    row = {"role": "assistant", "content": "", "tool_calls": [call]}
    out = normalize_row(row)
    assert out["tool_calls"][0] == call


def test_a_subject_under_an_unenumerated_key_still_reaches_the_tools_key() -> None:
    """The write path this replaced swept for any string as its last resort, so
    a row that used to arrive keyed must still arrive keyed."""
    out = normalize_row(_call("Bash", {"weird": "foo"}))
    fn = out["tool_calls"][0]["function"]
    assert fn["name"] == "exec"
    assert json.loads(fn["arguments"]) == {"command": "foo"}


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
