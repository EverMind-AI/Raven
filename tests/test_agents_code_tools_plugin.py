"""code-flow's tool face (code_flow/tools/): the fork's tools as same-name replacements.

Pins the manifest rows, the D6 admission from the slice, the fork's schema
spelling with the host's names accepted as aliases, the two pieces of conduct
the file tools carry (read-before-edit, the Python syntax verdict), the
``occurrence`` edit, ``glob`` and ``todo``, and that a plugin tool
registered after the built-in replaces it in the registry the loop reads.
"""

import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from raven.agent import workdir
from raven.agent.tools.filesystem import ReadFileTool as TrunkReadFileTool
from raven.agent.tools.registry import ToolRegistry
from raven.contracts.tool import ToolResult
from raven.plugins import DiscoveredPlugin, ManifestOrigin, PluginManifest, PluginRegistry
from raven.plugins.context import PluginContext, ServiceLocator

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-code" / "plugins" / "code-flow"
sys.path.insert(0, str(PLUGIN_DIR))
from code_flow.tools import plugin as factories  # noqa: E402
from code_flow.tools.filesystem import EditFileTool, ReadFileTool, ReadLedger, WriteFileTool  # noqa: E402
from code_flow.tools.read_state import ReadSessions, owner_for  # noqa: E402
from code_flow.tools.search import GlobTool, expand_braces, is_notice  # noqa: E402

MANIFEST_PATH = PLUGIN_DIR / "raven-plugin.toml"
FACE = ("read_file", "write_file", "edit_file", "list_dir", "glob", "todo", "exec")


def _ctx(tmp_path: Path, tools: dict) -> PluginContext:
    """A plugin context carrying the flow slice whose ``tools`` section is the
    product's own tool face."""
    return PluginContext(
        config={"enabled": True, "tools": tools},
        services=ServiceLocator(workspace=tmp_path, user_id="u", agent_id="a"),
    )


def trunk_read_file(workspace: Path):
    from raven.agent.tools.filesystem import ReadFileTool as TrunkRead

    return TrunkRead(workspace=workspace)


def registry_execute(reg: ToolRegistry, name: str, params: dict):
    return reg.execute(name, params)


def _run(coro):
    return asyncio.get_event_loop_policy().new_event_loop().run_until_complete(coro)


def _text(result) -> str:
    return result.model_text if isinstance(result, ToolResult) else str(result)


@pytest.fixture(autouse=True)
def _fresh_ledger(tmp_path):
    reads = ReadSessions(owner_for(_ctx(tmp_path, {"enabled": True})))
    reads.bind("cli:unit")
    yield
    reads.unbind()


@pytest.fixture()
def registry(tmp_path):
    ctx = _ctx(tmp_path, {"enabled": True})
    reg = ToolRegistry()
    for make in (
        factories.make_read_file,
        factories.make_write_file,
        factories.make_edit_file,
        factories.make_list_dir,
        factories.make_glob,
        factories.make_todo,
        factories.make_exec,
    ):
        reg.register(make(ctx))
    return reg


# --- manifest and admission ----------------------------------------------------


def test_the_manifest_contributes_the_seven_tools_beside_the_flows_own_seats():
    """One plugin per product, the way every sibling ships: the tool face is a
    subpackage of code-flow, not a plugin of its own."""
    mf = PluginManifest.from_toml_path(MANIFEST_PATH)
    assert mf.id == "code-flow"
    assert [t.name for t in mf.contributes.tools] == list(FACE)
    assert [h.name for h in mf.contributes.hooks] == ["code_flow"]
    assert mf.contributes.tool_gates == []
    assert [f.factory for f in mf.contributes.tools] == [f"code_flow.tools.plugin:make_{name}" for name in FACE]


def test_the_registry_builds_every_tool_from_the_real_factory_strings(tmp_path):
    reg = PluginRegistry()
    mf = PluginManifest.from_toml_path(MANIFEST_PATH)
    reg.activate([DiscoveredPlugin(manifest=mf, source=ManifestOrigin.USER, location=MANIFEST_PATH)])
    assert reg.tool_names() == sorted(FACE)
    services = ServiceLocator(workspace=tmp_path, user_id="u", agent_id="a")
    slice_ = {"enabled": True, "tools": {"enabled": True}}
    for name in FACE:
        tool = reg.build_tool(name, config=slice_, services=services)
        assert tool is not None and tool.name == name


@pytest.mark.parametrize("config", [{}, {"enabled": True}, {"enabled": True, "tools": {"enabled": False}}])
def test_a_slice_that_does_not_ask_for_the_face_contributes_nothing(tmp_path, config):
    """The flow and the face have one switch each: a product can keep the flow
    and hand the tools back to the host."""
    ctx = PluginContext(config=config, services=ServiceLocator(workspace=tmp_path, user_id="u", agent_id="a"))
    for make in (
        factories.make_read_file,
        factories.make_write_file,
        factories.make_edit_file,
        factories.make_list_dir,
        factories.make_glob,
        factories.make_todo,
        factories.make_exec,
    ):
        assert make(ctx) is None


# --- the fork's schema, the host's names as aliases ----------------------------


def test_the_schemas_carry_the_forks_spelling(registry):
    read = registry.get("read_file").parameters
    assert read["required"] == ["file_path"] and "path" not in read["properties"]
    edit = registry.get("edit_file").parameters
    assert edit["required"] == ["file_path", "old_string", "new_string"]
    assert "occurrence" in edit["properties"] and "old_text" not in edit["properties"]
    write = registry.get("write_file").parameters
    assert write["required"] == ["file_path", "content"]
    assert registry.get("glob").name == "glob"
    assert registry.get("find") is None


def test_the_hosts_spelling_still_runs_through_the_aliases(registry, tmp_path):
    target = tmp_path / "a.txt"
    target.write_text("alpha beta\n")
    assert "1| alpha beta" in _text(_run(registry.execute("read_file", {"path": str(target)})))
    result = _run(registry.execute("edit_file", {"path": str(target), "old_text": "beta", "new_text": "gamma"}))
    assert "Successfully edited" in _text(result)
    assert target.read_text() == "alpha gamma\n"


def test_the_forks_spelling_wins_when_a_call_carries_both(tmp_path):
    tool = EditFileTool(workspace=tmp_path)
    cast = tool.cast_params({"path": "legacy.py", "file_path": "fork.py", "old_text": "a", "old_string": "b"})
    assert cast == {"file_path": "fork.py", "old_string": "b"}


# --- read before edit ---------------------------------------------------------------


def test_editing_a_file_nobody_read_is_refused_until_it_is_read(registry, tmp_path):
    target = tmp_path / "b.txt"
    target.write_text("one\n")
    refused = _text(
        _run(registry.execute("edit_file", {"file_path": str(target), "old_string": "one", "new_string": "two"}))
    )
    assert refused.startswith("Error") and "have not read" in refused
    assert target.read_text() == "one\n"
    _run(registry.execute("read_file", {"file_path": str(target)}))
    accepted = _run(registry.execute("edit_file", {"file_path": str(target), "old_string": "one", "new_string": "two"}))
    assert "Successfully edited" in _text(accepted)
    assert target.read_text() == "two\n"


def test_a_write_counts_as_having_read_what_now_stands(registry, tmp_path):
    target = tmp_path / "c.txt"
    _run(registry.execute("write_file", {"file_path": str(target), "content": "x = 1\n"}))
    result = _run(registry.execute("edit_file", {"file_path": str(target), "old_string": "1", "new_string": "2"}))
    assert "Successfully edited" in _text(result)


def test_the_read_rule_is_a_switch(tmp_path):
    ctx = _ctx(tmp_path, {"enabled": True, "requireReadBeforeEdit": False})
    tool = factories.make_edit_file(ctx)
    target = tmp_path / "d.txt"
    target.write_text("k\n")
    assert "Successfully edited" in _text(_run(tool.execute(file_path=str(target), old_string="k", new_string="v")))


# --- the syntax verdict ---------------------------------------------------------------


def test_a_write_that_breaks_python_says_so_in_the_same_result(registry, tmp_path):
    target = tmp_path / "e.py"
    result = _text(_run(registry.execute("write_file", {"file_path": str(target), "content": "def f(:\n"})))
    assert "Successfully wrote" in result
    assert "syntax error" in result and "line 1" in result


def test_an_edit_that_breaks_python_says_so_and_a_clean_one_stays_quiet(registry, tmp_path):
    target = tmp_path / "f.py"
    target.write_text("x = 1\n")
    _run(registry.execute("read_file", {"file_path": str(target)}))
    broken = _text(
        _run(registry.execute("edit_file", {"file_path": str(target), "old_string": "1", "new_string": "("}))
    )
    assert "syntax error" in broken
    fixed = _text(_run(registry.execute("edit_file", {"file_path": str(target), "old_string": "(", "new_string": "2"})))
    assert "Successfully edited" in fixed and "syntax error" not in fixed


def test_the_verdict_is_python_only_and_a_switch(tmp_path):
    ctx = _ctx(tmp_path, {"enabled": True})
    write = factories.make_write_file(ctx)
    assert "syntax" not in _text(_run(write.execute(file_path=str(tmp_path / "g.txt"), content="def f(:\n")))
    quiet = factories.make_write_file(_ctx(tmp_path, {"enabled": True, "pythonSyntaxNote": False}))
    assert "syntax" not in _text(_run(quiet.execute(file_path=str(tmp_path / "h.py"), content="def f(:\n")))


# --- occurrence -----------------------------------------------------------------------


def test_occurrence_replaces_exactly_the_nth_match(registry, tmp_path):
    target = tmp_path / "i.txt"
    target.write_text("a\na\na\n")
    _run(registry.execute("read_file", {"file_path": str(target)}))
    result = _run(
        registry.execute("edit_file", {"file_path": str(target), "old_string": "a", "new_string": "b", "occurrence": 2})
    )
    assert "occurrence 2 of 3" in _text(result) and "line 2" in _text(result)
    assert target.read_text() == "a\nb\na\n"
    direct = _run(
        registry.get("edit_file").execute(file_path=str(target), old_string="a", new_string="c", occurrence=2)
    )
    assert isinstance(direct, ToolResult) and direct.file_change is not None and direct.diff
    assert target.read_text() == "a\nb\nc\n"


@pytest.mark.parametrize(
    ("content", "old", "occurrence", "expected"),
    [
        ("aaaa bb aa", "aa", 1, "Xaa bb aa"),
        ("aaaa bb aa", "aa", 2, "aaX bb aa"),
        ("aaaa bb aa", "aa", 3, "aaaa bb X"),
        ("ababa aba", "aba", 2, "ababa X"),
        ("aa\r\naa\r\n", "aa\r\n", 2, "aa\r\nX"),
    ],
)
def test_occurrence_counts_non_overlapping_matches(registry, tmp_path, content, old, occurrence, expected):
    target = tmp_path / "matches.txt"
    target.write_bytes(content.encode())
    _run(registry.execute("read_file", {"file_path": str(target)}))
    result = _run(
        registry.execute(
            "edit_file",
            {"file_path": str(target), "old_string": old, "new_string": "X", "occurrence": occurrence},
        )
    )
    assert not _text(result).startswith("Error")
    assert target.read_bytes() == expected.encode()


def test_occurrence_description_states_its_matching_rules(registry):
    tool = registry.get("edit_file")
    description = tool.parameters["properties"]["occurrence"]["description"]
    assert "non-overlapping" in description and "1-based" in description
    assert "exact" in description and "replace_all" in description
    assert "non-overlapping" in tool.description


def test_occurrence_past_the_count_and_with_replace_all_are_refused(registry, tmp_path):
    target = tmp_path / "j.txt"
    target.write_text("a\na\n")
    _run(registry.execute("read_file", {"file_path": str(target)}))
    too_far = _text(
        _run(
            registry.execute(
                "edit_file", {"file_path": str(target), "old_string": "a", "new_string": "b", "occurrence": 3}
            )
        )
    )
    assert too_far.startswith("Error") and "matches only 2" in too_far
    both = _text(
        _run(
            registry.execute(
                "edit_file",
                {"file_path": str(target), "old_string": "a", "new_string": "b", "occurrence": 1, "replace_all": True},
            )
        )
    )
    assert both.startswith("Error") and "mutually exclusive" in both
    assert target.read_text() == "a\na\n"


def test_several_matches_without_a_selector_are_still_refused(registry, tmp_path):
    target = tmp_path / "k.txt"
    target.write_text("a\na\n")
    _run(registry.execute("read_file", {"file_path": str(target)}))
    result = _text(
        _run(registry.execute("edit_file", {"file_path": str(target), "old_string": "a", "new_string": "b"}))
    )
    assert "appears 2 times" in result and "old_string" in result and "old_text" not in result
    assert target.read_text() == "a\na\n"


# --- glob ------------------------------------------------------------------------------


def test_glob_finds_by_pattern_and_expands_a_brace_alternation(tmp_path):
    (tmp_path / "one.py").write_text("")
    (tmp_path / "two.txt").write_text("")
    (tmp_path / "three.md").write_text("")
    tool = GlobTool(workspace=tmp_path)
    with workdir.bind(tmp_path):
        py = _text(_run(tool.execute(pattern="*.py")))
        assert "one.py" in py and "two.txt" not in py
        both = _text(_run(tool.execute(pattern="*.{py,txt}")))
    assert "one.py" in both and "two.txt" in both and "three.md" not in both


def test_brace_expansion_rules():
    assert expand_braces("*.{a,b}") == ["*.a", "*.b"]
    assert expand_braces("src/{a,b}/*.{ts,tsx}") == ["src/a/*.ts", "src/a/*.tsx", "src/b/*.ts", "src/b/*.tsx"]
    assert expand_braces("*.py") == ["*.py"]


# --- the replacement in the registry -----------------------------------------------------


def test_a_plugin_tool_registered_after_the_builtin_replaces_it(tmp_path):
    reg = ToolRegistry()
    reg.register(TrunkReadFileTool(workspace=tmp_path))
    assert reg.get("read_file").parameters["required"] == ["path"]
    reg.register(ReadFileTool(workspace=tmp_path))
    assert reg.get("read_file").parameters["required"] == ["file_path"]
    assert isinstance(reg.get("read_file"), ReadFileTool)


def test_the_ledger_is_keyed_by_resolved_path(tmp_path):
    ledger = ReadLedger()
    target = tmp_path / "m.txt"
    target.write_text("z\n")
    read = ReadFileTool(workspace=tmp_path, ledger=ledger)
    edit = EditFileTool(workspace=tmp_path, ledger=ledger)
    with workdir.bind(tmp_path):
        _run(read.execute(file_path="m.txt"))
        assert ledger.has_read(target)
        assert "Successfully edited" in _text(_run(edit.execute(file_path=str(target), old_string="z", new_string="w")))


def test_write_file_still_reports_the_diff_the_ui_renders(tmp_path):
    tool = WriteFileTool(workspace=tmp_path)
    target = tmp_path / "n.py"
    target.write_text("a = 1\n")
    result = _run(tool.execute(file_path=str(target), content="a = 2\n"))
    assert isinstance(result, ToolResult) and result.file_change is not None


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


# --- what the adversarial audit of 2026-09-08 found, each pinned -------------


def test_an_occurrence_edit_keeps_the_files_own_line_endings(registry, tmp_path):
    """A CRLF file edited through ``occurrence`` used to come back with every
    line changed: the path read and wrote through text mode, which normalises
    to LF, so a one-line edit arrived as a whole-file diff (and a grader diffing
    the working tree sees a rewrite). Trunk's own edit path reads and writes
    bytes for exactly this reason; so does this one now."""
    target = tmp_path / "crlf.txt"
    target.write_bytes(b"a\r\nb\r\na\r\n")
    _run(registry.execute("read_file", {"file_path": str(target)}))
    result = _run(
        registry.execute("edit_file", {"file_path": str(target), "old_string": "a", "new_string": "Z", "occurrence": 2})
    )
    assert "occurrence 2 of 2" in _text(result)
    assert target.read_bytes() == b"a\r\nb\r\nZ\r\n"


def test_an_error_report_quotes_the_file_verbatim(registry, tmp_path):
    """The respelling of trunk's parameter names must not reach quoted file
    content. It used to be a blanket replacement over the whole result, so a
    file that really contains ``old_text`` was shown to the model as containing
    ``old_string`` -- and the model then copied an excerpt that could never
    match anything."""
    target = tmp_path / "tool.py"
    target.write_text("def execute(self, path, old_text, new_text):\n    value = old_text\n    return path\n")
    _run(registry.execute("read_file", {"file_path": str(target)}))
    near_miss = "def execute(self, path, old_text, new_text):\n    value = old_text\n    return other\n"
    report = _text(
        _run(
            registry.execute(
                "edit_file",
                {"file_path": str(target), "old_string": near_miss, "new_string": "x"},
            )
        )
    )
    sentence, _, excerpt = report.partition("\n")
    assert sentence.startswith("Error") and "old_string" in sentence, "the sentence names the parameter shown"
    assert "old_text" in excerpt, "the quoted file keeps its own spelling"
    assert "old_string" not in excerpt, "and nothing rewrites the file's text"


def test_a_long_line_is_cut_the_way_the_description_promises(registry, tmp_path):
    """Trunk numbers a line whole, so one minified line can overrun the
    whole-result budget by itself: the read came back with NO body and a note
    telling the model to continue from the same offset, which it can do
    forever. The description promised truncation; now it happens."""
    target = tmp_path / "bundle.js"
    target.write_text("x" * 200_000 + "\nlast\n")
    body = _text(_run(registry.execute("read_file", {"file_path": str(target)})))
    assert "line truncated to 2000 chars" in body
    assert "1| " in body and "2| last" in body


def test_a_read_that_failed_does_not_license_an_edit(registry, tmp_path):
    """A model probing for a file that does not exist yet used to have that
    failed read recorded as having seen it; once something else created the
    file, the edit went straight through on content nobody had read."""
    target = tmp_path / "later.txt"
    missing = _text(_run(registry.execute("read_file", {"file_path": str(target)})))
    assert missing.startswith("Error")
    target.write_text("created by something else\n")
    refused = _text(
        _run(registry.execute("edit_file", {"file_path": str(target), "old_string": "created", "new_string": "x"}))
    )
    assert refused.startswith("Error") and "have not read" in refused


def test_a_file_that_changed_since_the_read_is_refused_as_stale(registry, tmp_path):
    """The fork's third state: a formatter, a build step or a checkout between
    the read and the edit means the text being edited is no longer there."""
    import os

    target = tmp_path / "moving.txt"
    target.write_text("one\n")
    _run(registry.execute("read_file", {"file_path": str(target)}))
    target.write_text("two\n")
    os.utime(target, (1_000_000, 1_000_000))
    stale = _text(
        _run(registry.execute("edit_file", {"file_path": str(target), "old_string": "two", "new_string": "three"}))
    )
    assert stale.startswith("Error") and "changed since you last read it" in stale
    _run(registry.execute("read_file", {"file_path": str(target)}))
    assert "Successfully edited" in _text(
        _run(registry.execute("edit_file", {"file_path": str(target), "old_string": "two", "new_string": "three"}))
    )


def test_the_write_ledger_does_not_depend_on_the_syntax_verdict(tmp_path):
    """Two separate knobs: turning the Python verdict off used to leave the
    model unable to edit a file it had just written, because the ledger entry
    sat after the verdict's early return."""
    ctx = _ctx(tmp_path, {"enabled": True, "pythonSyntaxNote": False})
    write = factories.make_write_file(ctx)
    edit = factories.make_edit_file(ctx)
    target = tmp_path / "quiet.py"
    assert "Successfully wrote" in _text(_run(write.execute(file_path=str(target), content="x = 1\n")))
    assert "Successfully edited" in _text(_run(edit.execute(file_path=str(target), old_string="1", new_string="2")))


def test_the_workspace_fence_follows_the_products_answer(tmp_path):
    """These tools replace the host's own by name, and the host fences its own
    on ``tools.restrictToWorkspace``. Passing no ``allowed_dirs`` meant the
    fence was OFF whatever the product asked for -- half the face fenced, half
    not, with no warning. The launcher renders the answer into the slice."""
    outside = tmp_path / "outside.txt"
    outside.write_text("secret\n")
    inside = tmp_path / "ws"
    inside.mkdir()
    unfenced = factories.make_read_file(_ctx(inside, {"enabled": True}))
    assert "secret" in _text(_run(unfenced.execute(file_path=str(outside))))
    fenced = factories.make_read_file(_ctx(inside, {"enabled": True, "restrictToWorkspace": True}))
    refused = _text(_run(fenced.execute(file_path=str(outside))))
    assert refused.startswith("Error") and "outside allowed" in refused


def test_a_zero_or_negative_occurrence_is_refused_in_code(tmp_path):
    """The schema's ``minimum`` stops the model, but a caller reaching the tool
    directly used to get a write at a negative index reported as success."""
    tool = factories.make_edit_file(_ctx(tmp_path, {"enabled": True, "requireReadBeforeEdit": False}))
    target = tmp_path / "guard.txt"
    target.write_text("aaa\n")
    for bad in (0, -1):
        refused = _text(_run(tool.execute(file_path=str(target), old_string="a", new_string="Z", occurrence=bad)))
        assert refused.startswith("Error") and "1 or greater" in refused
    assert target.read_text() == "aaa\n"


# --- glob: the merge that used to paste the engine's notices into the listing --


def test_glob_merges_only_paths_and_honours_the_callers_limit(tmp_path):
    """Every alternative used to spend the caller's ``limit`` again, and each
    engine notice ("No files found…", "(showing first N of M results)") was
    pasted into the listing as though it were a filename -- which the model
    then tried to read."""
    for name in ("a.py", "b.py", "c.ts", "d.ts"):
        (tmp_path / name).write_text("")
    tool = GlobTool(workspace=tmp_path)
    with workdir.bind(tmp_path):
        both = _text(_run(tool.execute(pattern="*.{py,ts}")))
        assert sorted(both.splitlines()) == ["a.py", "b.py", "c.ts", "d.ts"]
        missing = _text(_run(tool.execute(pattern="*.{py,zzz}")))
        assert sorted(missing.splitlines()) == ["a.py", "b.py"], "a barren alternative adds no line"
        capped = _text(_run(tool.execute(pattern="*.{py,ts}", limit=3)))
        lines = capped.splitlines()
        assert len(lines) == 4 and len([ln for ln in lines if is_notice(ln)]) == 1
        assert len([ln for ln in lines if not is_notice(ln)]) == 3, "the caller's limit is the merged total"
        # A single pattern is the engine's own answer, verbatim.
        assert "No files found matching pattern" in _text(_run(tool.execute(pattern="*.nope")))
        # A merged one is this tool's, and names the pattern it was asked for.
        assert "No files found matching pattern: *.{nope,zzz}" == _text(_run(tool.execute(pattern="*.{nope,zzz}")))


def test_glob_preserves_a_child_truncation_when_other_patterns_are_empty(tmp_path):
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("")
    tool = GlobTool(workspace=tmp_path)
    out = _text(_run(tool.execute(pattern="*.{py,ts}", limit=1)))
    assert "PARTIAL" in out and "raise limit" in out
    assert len([line for line in out.splitlines() if not is_notice(line)]) == 1


@pytest.mark.parametrize("kind", ["missing", "not_directory", "outside"])
def test_glob_preserves_child_search_errors(tmp_path, kind):
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "outside" if kind == "outside" else root / kind
    if kind == "not_directory":
        target.write_text("file")
    elif kind == "outside":
        target.mkdir()
    tool = GlobTool(workspace=root, allowed_dirs=(root,))
    single = _text(_run(tool.execute(pattern="*.py", path=str(target))))
    merged = _text(_run(tool.execute(pattern="*.{py,ts}", path=str(target))))
    assert single.startswith("Error:") and merged == single


def test_glob_merges_by_recency_before_applying_the_total_limit(tmp_path):
    for timestamp, name in enumerate(("old.py", "middle.py", "new.ts"), start=100):
        path = tmp_path / name
        path.write_text("")
        os.utime(path, (timestamp, timestamp))
    tool = GlobTool(workspace=tmp_path)
    out = _text(_run(tool.execute(pattern="*.{py,ts}", limit=2)))
    assert out.splitlines()[:2] == ["new.ts", "middle.py"]
    assert "PARTIAL" in out


def test_glob_keeps_paths_that_look_like_notice_prefixes_and_deduplicates(tmp_path):
    names = ["ErrorHandler.py", "No files found matching pattern.py", "other.py"]
    for name in names:
        (tmp_path / name).write_text("")
    tool = GlobTool(workspace=tmp_path)
    out = _text(_run(tool.execute(pattern="{*.py,Error*}", limit=3)))
    assert sorted(out.splitlines()) == sorted(names)
    assert "PARTIAL" not in out


def test_nested_braces_expand_by_pairing_and_a_runaway_pattern_is_refused(tmp_path):
    """Scanning for the first ``}`` produced garbage patterns (``*.a}``) and
    silently dropped whole alternatives -- a listing that omits half the
    alternatives reads exactly like a tree that lacks them."""
    assert expand_braces("*.{a,b}") == ["*.a", "*.b"]
    assert expand_braces("*.{a,{b,c}}") == ["*.a", "*.b", "*.c"]
    assert expand_braces("src/{a,b}/*.{ts,tsx}") == ["src/a/*.ts", "src/a/*.tsx", "src/b/*.ts", "src/b/*.tsx"]
    assert expand_braces("*.py") == ["*.py"]
    with pytest.raises(ValueError):
        expand_braces("*.{a,b")
    with pytest.raises(ValueError):
        expand_braces("{a,b,c,d,e}{1,2,3,4,5}")
    (tmp_path / "one.py").write_text("")
    tool = GlobTool(workspace=tmp_path)
    with workdir.bind(tmp_path):
        refused = _text(_run(tool.execute(pattern="{a,b,c,d,e}{1,2,3,4,5}")))
        assert refused.startswith("Error") and "separate calls" in refused
        assert _text(_run(tool.execute(pattern="*.{py,{txt,md}}"))).splitlines() == ["one.py"]


# --- the boundary the same-name contribution stands on ------------------------


def test_the_registered_instance_is_this_products_under_every_shared_name(tmp_path):
    """The guarantee the whole face rests on: where a name is shared with the
    host, the object the registry dispatches to is THIS product's.

    Checked by identity, not by schema: a change that registers the plugin
    tools before the built-ins instead of after would leave every name in
    place and every schema silently reverted to the host's spelling."""
    from raven.agent.tools import file_search as trunk_search
    from raven.agent.tools import filesystem as trunk_fs

    ctx = _ctx(tmp_path, {"enabled": True})
    reg = ToolRegistry()
    # The host's own order: built-ins first, plugin contributions last.
    for cls in (
        trunk_fs.ReadFileTool,
        trunk_fs.WriteFileTool,
        trunk_fs.EditFileTool,
        trunk_fs.ListDirTool,
        trunk_search.GrepTool,
        trunk_search.FindTool,
    ):
        reg.register(cls(workspace=tmp_path))
    for make in (
        factories.make_read_file,
        factories.make_write_file,
        factories.make_edit_file,
        factories.make_list_dir,
        factories.make_glob,
        factories.make_todo,
        factories.make_exec,
    ):
        reg.register(make(ctx))
    for name in ("read_file", "write_file", "edit_file", "list_dir"):
        served = reg.get(name)
        assert type(served).__module__.startswith("code_flow.tools"), name
        assert not isinstance(served, (trunk_fs.ReadFileTool,)) or type(served) is not trunk_fs.ReadFileTool
    # Names the host does not use come from here too, and the host's own
    # spelling of the pathname tool is still registered until the launcher
    # withholds it -- that withholding is the launcher's test, not this one.
    assert type(reg.get("glob")).__module__.startswith("code_flow.tools")
    assert type(reg.get("todo")).__module__.startswith("code_flow.tools")
    assert type(reg.get("grep")).__module__.startswith("raven."), "a name we do not serve stays the host's"
    assert reg.get("find") is not None, "trunk's spelling is withheld by config, not by this registry"


def test_the_replacement_answers_the_call_not_just_the_schema(tmp_path):
    """Dispatch, not advertising: the served instance is the one that runs, so
    a call in the fork's spelling reaches this product's behaviour (the read
    ledger) rather than the host's tool, which has none."""
    ctx = _ctx(tmp_path, {"enabled": True})
    reg = ToolRegistry()
    reg.register(trunk_read_file(tmp_path))
    reg.register(factories.make_read_file(ctx))
    reg.register(factories.make_edit_file(ctx))
    target = tmp_path / "b.txt"
    target.write_text("one\n")
    # The host's tool has no read-before-edit rule; ours does, and it is ours
    # that answers.
    refused = _text(
        _run(registry_execute(reg, "edit_file", {"file_path": str(target), "old_string": "one", "new_string": "two"}))
    )
    assert refused.startswith("Error") and "have not read" in refused
    _run(registry_execute(reg, "read_file", {"file_path": str(target)}))
    assert "Successfully edited" in _text(
        _run(registry_execute(reg, "edit_file", {"file_path": str(target), "old_string": "one", "new_string": "two"}))
    )


def test_the_face_declines_as_a_whole_and_the_host_keeps_serving(tmp_path):
    """A product that hands the tools back gets the host's, unchanged: the
    factories decline, nothing is registered over the built-ins, and the
    schema the model sees is the host's own."""
    from raven.agent.tools import filesystem as trunk_fs

    ctx = PluginContext(
        config={"enabled": True, "tools": {"enabled": False}},
        services=ServiceLocator(workspace=tmp_path, user_id="u", agent_id="a"),
    )
    reg = ToolRegistry()
    reg.register(trunk_fs.ReadFileTool(workspace=tmp_path))
    contributed = [
        make(ctx)
        for make in (
            factories.make_read_file,
            factories.make_write_file,
            factories.make_edit_file,
            factories.make_list_dir,
            factories.make_glob,
            factories.make_todo,
            factories.make_exec,
        )
    ]
    assert contributed == [None] * 7
    assert type(reg.get("read_file")) is trunk_fs.ReadFileTool
    assert reg.get("read_file").parameters["required"] == ["path"]


def test_a_malformed_slice_declines_rather_than_serving_half_a_face(tmp_path):
    """Half a face is worse than none: the guide and the conduct teach one
    spelling, so a slice that does not parse must take the whole face down."""
    ctx = PluginContext(
        config={"enabled": True, "tools": {"enabled": "yes please"}},
        services=ServiceLocator(workspace=tmp_path, user_id="u", agent_id="a"),
    )
    assert factories.make_read_file(ctx) is None
    assert factories.make_glob(ctx) is None
