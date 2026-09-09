"""Tool classification and location extraction for the ACP surface."""

from raven.acp.tool_kinds import (
    _KINDS,  # noqa: PLC2701 -- the coverage pin below
    MAX_LOCATIONS,
    absolute_path,
    locations,
    title_for,
    tool_kind,
)


def test_every_registered_tool_name_has_been_looked_at():
    # The names AgentLoop._register_default_tools registers (plus the skill-hub
    # pair). A tool renamed or added lands on "other" silently unless this pin
    # forces the decision.
    registered = {
        "read_file",
        "write_file",
        "edit_file",
        "list_dir",
        "grep",
        "find",
        "exec",
        "web_search",
        "web_fetch",
        "message",
        "spawn",
        "ask_user",
        "cron",
        "image_generate",
        "text_to_speech",
        "video_generate",
        "use_skill",
        "read_skill",
        "tool_search",
    }
    assert registered <= set(_KINDS)


def test_kinds_are_the_schema_vocabulary():
    assert set(_KINDS.values()) <= {"read", "edit", "delete", "move", "search", "execute", "think", "fetch", "other"}
    assert tool_kind("web_search") == "search"
    assert tool_kind("web_fetch") == "fetch"
    assert tool_kind("exec") == "execute"
    assert tool_kind("mcp_github_create_issue") == "other"
    assert tool_kind(None) == "other"


def test_title_prefers_display_then_subject_never_empty():
    assert title_for("exec", {"command": "ls -la"}) == "exec: ls -la"
    assert title_for("read_file", {"path": "notes.md"}) == "read_file: notes.md"
    assert title_for("web_search", {"query": "raven"}, display="Searching raven") == "Searching raven"
    assert title_for("message", {}) == "message"
    assert title_for(None, None) == "tool"
    long = title_for("exec", {"command": "x" * 300})
    assert len(long) <= len("exec: ") + 120


def test_locations_resolve_relative_against_cwd_and_drop_unresolvable():
    found = locations({"path": "src/a.py", "file_path": "/abs/b.py"}, "/work")
    assert {"path": "/work/src/a.py"} in found
    assert {"path": "/abs/b.py"} in found
    # No cwd: the relative path is dropped, never sent relative.
    assert locations({"path": "src/a.py"}, None) == []


def test_locations_dedupe_and_cap():
    args = {"paths": [f"/f/{i}.txt" for i in range(20)] + ["/f/0.txt"]}
    found = locations(args, None)
    assert len(found) == MAX_LOCATIONS
    assert len({loc["path"] for loc in found}) == MAX_LOCATIONS


def test_absolute_path_never_resolves_symlinks_and_survives_bad_input():
    assert absolute_path("/tmp/x", None) == "/tmp/x"
    assert absolute_path("rel", None) is None
    assert absolute_path("a\0b", None) is None
