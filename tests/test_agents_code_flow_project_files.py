"""Repository instructions enter system through the product hook without rewriting the query."""

import os
import sys
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent import workdir
from raven.contracts.loop_hooks import AgentHookContext
from raven.plugins.context import PluginContext, ServiceLocator
from raven.providers.binding import ModelBinding, use_binding
from raven.providers.prompt_cache import STABLE_PREFIX_KEY
from raven.utils.tokens import estimate_prompt_tokens

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-code" / "plugins" / "code-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from code_flow.flow import (  # noqa: E402
    PROJECT_FILE_MAX_CHARS,
    PROJECT_FILES,
    PROJECT_INSTRUCTIONS_HEAD,
    CodeFlowHook,
    make_flow_hook,
    project_instructions,
)
from code_flow.sessions import SessionLedger  # noqa: E402

TASK = "fix the failing test in tests/test_units.py"


def _hook(names=PROJECT_FILES) -> CodeFlowHook:
    return CodeFlowHook(SessionLedger(), project_files=list(names))


async def _system(hook: CodeFlowHook, cwd: Path | None, text: str = TASK, key: str = "s1") -> str:
    inbound = AgentHookContext(session_key=key, inbound_content=text)
    ctx = AgentHookContext(
        session_key=key,
        messages=[{"role": "system", "content": ""}, {"role": "user", "content": text}],
        context_window_tokens=8192,
    )

    async def build():
        assert (await hook.before_user_inbound(inbound)).modified_content is None
        assert inbound.inbound_content == text
        decision = await hook.before_iteration(ctx)
        assert decision.short_circuit_result is None
        assert ctx.messages[-1] == {"role": "user", "content": text}
        return ctx.messages[0]["content"]

    if cwd is None:
        return await build()
    with workdir.bind(cwd):
        return await build()


def _checkout(tmp_path: Path, **files: str) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    for name, text in files.items():
        (root / name).write_text(text, encoding="utf-8")
    return root


@pytest.mark.asyncio
async def test_the_named_files_enter_system_in_the_slices_order(tmp_path):
    root = _checkout(tmp_path, **{"AGENTS.md": "run the tests with make test", "CLAUDE.md": "the build needs node 20"})
    text = await _system(_hook(), root)
    assert text
    assert text.startswith(PROJECT_INSTRUCTIONS_HEAD)
    assert text.index("## AGENTS.md") < text.index("make test") < text.index("## CLAUDE.md") < text.index("node 20")
    assert TASK not in text, "the query is not part of system instructions"
    assert "## CONTEXT.md" not in text, "a missing name leaves no empty heading"


@pytest.mark.asyncio
async def test_nothing_bound_reads_nothing(tmp_path):
    """Unbound, these names would resolve against the agent's own home."""
    text = await _system(_hook(), None)
    assert text == ""


@pytest.mark.asyncio
async def test_an_empty_list_reads_nothing(tmp_path):
    root = _checkout(tmp_path, **{"AGENTS.md": "run the tests with make test"})
    text = await _system(_hook([]), root)
    assert text == ""


@pytest.mark.asyncio
async def test_a_checkout_without_the_files_passes_the_task_through(tmp_path):
    root = _checkout(tmp_path, **{"README.md": "hello"})
    text = await _system(_hook(), root)
    assert text == ""


def test_a_file_is_capped_and_says_so(tmp_path):
    root = _checkout(tmp_path, **{"AGENTS.md": "x" * (PROJECT_FILE_MAX_CHARS + 5_000)})
    text = project_instructions(root, ["AGENTS.md"])
    assert len(text) < PROJECT_FILE_MAX_CHARS + len(PROJECT_INSTRUCTIONS_HEAD) + 200
    assert "truncated" in text


def test_a_directory_or_a_broken_link_at_the_name_is_skipped(tmp_path):
    root = tmp_path / "checkout"
    (root / "AGENTS.md").mkdir(parents=True)
    os.symlink(str(tmp_path / "nowhere"), str(root / "CLAUDE.md"))
    (root / "CONTEXT.md").write_text("the terms of art", encoding="utf-8")
    text = project_instructions(root, PROJECT_FILES)
    assert "## CONTEXT.md" in text
    assert "## AGENTS.md" not in text
    assert "## CLAUDE.md" not in text


def test_the_files_are_not_fenced_as_untrusted(tmp_path):
    """A data fence would tell the model to ignore what it is handed to follow."""
    root = _checkout(tmp_path, **{"AGENTS.md": "run the tests with make test"})
    assert "UNTRUSTED" not in project_instructions(root, ["AGENTS.md"])


@pytest.mark.parametrize("escape", ["absolute", "parent", "symlink", "directory_symlink"])
def test_instruction_paths_cannot_read_outside_the_bound_directory(tmp_path, escape):
    root = _checkout(tmp_path, **{"AGENTS.md": "LOCAL_RULE"})
    outside = tmp_path / "host.md"
    outside.write_text("HOST_FILE_MUST_NOT_REACH_THE_MODEL")
    if escape == "absolute":
        name = str(outside)
    elif escape == "parent":
        name = "../host.md"
    elif escape == "symlink":
        (root / "CLAUDE.md").symlink_to(outside)
        name = "CLAUDE.md"
    else:
        (root / "linked").symlink_to(tmp_path, target_is_directory=True)
        name = "linked/host.md"
    text = project_instructions(root, [name, "AGENTS.md"])
    assert "HOST_FILE_MUST_NOT_REACH_THE_MODEL" not in text
    assert "LOCAL_RULE" in text


def test_contained_aliases_and_a_symlinked_working_directory_are_supported(tmp_path):
    root = _checkout(tmp_path, **{"AGENTS.md": "LOCAL_RULE"})
    (root / "CLAUDE.md").symlink_to("AGENTS.md")
    (root / "nested").mkdir()
    alias = tmp_path / "bound"
    alias.symlink_to(root, target_is_directory=True)
    text = project_instructions(alias, ["nested/../AGENTS.md", "CLAUDE.md", str(root / "AGENTS.md")])
    assert text.count("LOCAL_RULE") == 1


def test_a_symlink_loop_does_not_prevent_reading_other_instruction_files(tmp_path):
    root = _checkout(tmp_path, **{"AGENTS.md": "LOCAL_RULE"})
    (root / "CLAUDE.md").symlink_to("CLAUDE.md")
    assert "LOCAL_RULE" in project_instructions(root, ["CLAUDE.md", "AGENTS.md"])


@pytest.mark.asyncio
async def test_repository_instructions_and_concurrency_enter_system_together(tmp_path):
    root = _checkout(tmp_path, **{"AGENTS.md": "run the tests with make test"})
    ledger = SessionLedger()
    first = CodeFlowHook(ledger, project_files=PROJECT_FILES)
    second = CodeFlowHook(ledger, project_files=PROJECT_FILES)
    await _system(first, root, key="s1")
    text = await _system(second, root, key="s2")
    assert text is not None
    assert text.index(PROJECT_INSTRUCTIONS_HEAD) < text.index("# Workspace concurrency")
    assert TASK not in text


def _ctx(tmp_path: Path, config: dict) -> PluginContext:
    return PluginContext(
        config=config,
        services=ServiceLocator(workspace=tmp_path / "home", user_id="u", agent_id="a"),
    )


@pytest.mark.asyncio
async def test_the_factory_reads_the_names_from_the_slice(tmp_path):
    root = _checkout(tmp_path, **{"AGENTS.md": "run the tests with make test"})
    named = make_flow_hook(_ctx(tmp_path, {"enabled": True, "projectFiles": ["AGENTS.md"]}))
    unnamed = make_flow_hook(_ctx(tmp_path, {"enabled": True}))
    assert "make test" in (await _system(named, root))
    assert (await _system(unnamed, root)) == ""


@pytest.mark.asyncio
async def test_flow_disabled_contributes_no_system_sections(tmp_path):
    root = _checkout(tmp_path, **{"AGENTS.md": "run tests"})
    hook = CodeFlowHook(SessionLedger(), project_files=PROJECT_FILES, flow_enabled=False)
    assert await _system(hook, root) == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("blocks", [False, True])
async def test_iteration_hook_preserves_the_host_prefix_and_never_duplicates_its_addition(tmp_path, blocks):
    root = _checkout(tmp_path, **{"AGENTS.md": "REPOSITORY_RULE"})
    base = [{"type": "text", "text": "HOST_PREFIX"}] if blocks else "HOST_PREFIX"
    ctx = AgentHookContext(
        session_key="s1",
        messages=[{"role": "system", "content": base, STABLE_PREFIX_KEY: 4}, {"role": "user", "content": TASK}],
    )
    hook = _hook()
    with workdir.bind(root):
        for _ in range(3):
            assert (await hook.before_iteration(ctx)).short_circuit_result is None
            assert str(ctx.messages[0]["content"]).count("REPOSITORY_RULE") == 1
            assert ctx.messages[0][STABLE_PREFIX_KEY] == 4
            assert ctx.messages[1:] == [{"role": "user", "content": TASK}]
            ctx.messages = deepcopy(ctx.messages)
        ctx.messages[0]["content"] = deepcopy(base)
        assert (await hook.before_iteration(ctx)).short_circuit_result is None
        assert str(ctx.messages[0]["content"]).count("REPOSITORY_RULE") == 1


@pytest.mark.asyncio
async def test_hook_truncates_only_its_addition_and_reserves_tools_and_the_actual_reply_ceiling(tmp_path):
    root = _checkout(tmp_path, **{"AGENTS.md": "repository rule " * 6000})
    provider = SimpleNamespace(generation=SimpleNamespace(max_tokens=512))
    ctx = AgentHookContext(
        session_key="s1",
        messages=[{"role": "system", "content": "HOST_PREFIX"}, {"role": "user", "content": "history " * 400}],
        tools=[{"type": "function", "function": {"name": "test", "description": "tool schema " * 150}}],
        context_window_tokens=100_000,
    )
    original_history = deepcopy(ctx.messages[1:])
    hook = _hook()
    with workdir.bind(root), use_binding(ModelBinding(provider, "test-model", configured_window=2048)):
        decision = await hook.before_iteration(ctx)
    assert decision.short_circuit_result is None
    assert "Repository instructions truncated" in ctx.messages[0]["content"]
    assert ctx.messages[0]["content"].startswith("HOST_PREFIX")
    assert ctx.messages[1:] == original_history
    assert estimate_prompt_tokens(ctx.messages, ctx.tools) + 512 <= 2048


@pytest.mark.asyncio
async def test_hook_reports_when_even_the_instruction_notice_cannot_fit(tmp_path):
    root = _checkout(tmp_path, **{"AGENTS.md": "repository rule"})
    provider = SimpleNamespace(generation=SimpleNamespace(max_tokens=512))
    ctx = AgentHookContext(
        session_key="s1",
        messages=[{"role": "system", "content": "HOST_PREFIX"}, {"role": "user", "content": "history " * 2000}],
    )
    original = deepcopy(ctx.messages)
    with workdir.bind(root), use_binding(ModelBinding(provider, "test-model", configured_window=1024)):
        decision = await _hook().before_iteration(ctx)
    assert "could not fit repository instructions" in decision.short_circuit_result
    assert ctx.messages == original


@pytest.mark.asyncio
async def test_repeated_injection_does_not_remove_matching_text_from_the_host_prefix(tmp_path):
    root = _checkout(tmp_path, **{"AGENTS.md": "REPOSITORY_RULE"})
    base = "HOST_PREFIX\n\n" + project_instructions(root, PROJECT_FILES) + "\nHOST_SUFFIX"
    ctx = AgentHookContext(
        session_key="s1", messages=[{"role": "system", "content": base}, {"role": "user", "content": TASK}]
    )
    hook = _hook()
    with workdir.bind(root):
        for _ in range(2):
            await hook.before_iteration(ctx)
            assert ctx.messages[0]["content"].startswith(base)
            assert ctx.messages[0]["content"].count("REPOSITORY_RULE") == 2


@pytest.mark.asyncio
async def test_pending_checklist_restore_is_counted_before_sizing_repository_instructions(tmp_path):
    from code_flow.tools.todo import TodoStore, normalize_todos

    root = _checkout(tmp_path, **{"AGENTS.md": "repository rule " * 6000})
    store = TodoStore(tmp_path / "home")
    store.bind("s1", root)
    store.replace(normalize_todos([{"content": "work item " * 200, "status": "pending"}]))
    provider = SimpleNamespace(generation=SimpleNamespace(max_tokens=512))
    ctx = AgentHookContext(
        session_key="s1", messages=[{"role": "system", "content": "HOST_PREFIX"}, {"role": "user", "content": TASK}]
    )
    hook = CodeFlowHook(SessionLedger(), project_files=PROJECT_FILES, todos=store)
    with workdir.bind(root), use_binding(ModelBinding(provider, "test-model", configured_window=2048)):
        decision = await hook.before_iteration(ctx)
    assert decision.short_circuit_result is None
    assert decision.append_note
    assert "truncated" in ctx.messages[0]["content"]
    ctx.messages[-1]["content"] += "\n\n" + decision.append_note
    assert estimate_prompt_tokens(ctx.messages, ctx.tools) + 512 <= 2048


@pytest.mark.asyncio
async def test_a_checklist_binding_failure_does_not_suppress_repository_instructions(tmp_path, monkeypatch):
    from code_flow.tools.todo import TodoStore

    root = _checkout(tmp_path, **{"AGENTS.md": "REPOSITORY_RULE"})
    store = TodoStore(tmp_path / "home")

    def fail(*args):
        raise OSError("checklist unavailable")

    monkeypatch.setattr(store, "bind", fail)
    hook = CodeFlowHook(SessionLedger(), project_files=PROJECT_FILES, todos=store)
    assert "REPOSITORY_RULE" in await _system(hook, root)
