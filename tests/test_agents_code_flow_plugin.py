"""The code-flow plugin's wiring: manifest, factories, grants, release, throat.

New tests (not ported): the fork armed its gate through the loader's own code
paths, so nothing in its suite pins the plugin-shaped wiring the trunk uses --
the manifest rows, the D6 factory admission from the rendered config slice
(the scaffold owns the workspaceGate spellings), the bind-time adoption of the
direct_ask/rebind_workdir grants onto the fork's hook faces, the session-key
capture that replaces the fork's cid hook, the release observer riding the
session-events paper, and the gate adjudicating through the trunk registry's
own throat. The ported fork suites cover the gate's SEMANTICS; this file
covers its SEAT.
"""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-code" / "plugins" / "code-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from code_flow import gate as wg  # noqa: E402
from code_flow.flow import CodeFlowHook, make_flow_hook  # noqa: E402
from code_flow.gate import (  # noqa: E402
    WorkspaceGate,
    WorkspaceReleaseObserver,
    make_release_observer,
    make_write_gate,
)

from raven.contracts.loop_hooks import AgentHookContext  # noqa: E402
from raven.contracts.session_events import SessionObserver  # noqa: E402
from raven.contracts.tool_gate import ToolGate  # noqa: E402
from raven.plugins import DiscoveredPlugin, ManifestOrigin, PluginManifest, PluginRegistry  # noqa: E402
from raven.plugins.context import PluginContext, ServiceLocator  # noqa: E402

MANIFEST_PATH = PLUGIN_DIR / "raven-plugin.toml"


def make_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(path), "config", "user.name", "t"], check=True)
    (path / "a.py").write_text("x = 1\n")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(path), "commit", "-qm", "init"], check=True)


def ctx_for(tmp_path: Path, config: dict) -> PluginContext:
    return PluginContext(
        config=config,
        services=ServiceLocator(workspace=tmp_path / "home", user_id="u", agent_id="a"),
    )


def armed_slice(tmp_path: Path) -> dict:
    """The slice exactly as the scaffold renders it (run.py render_config)."""
    return {
        "enabled": True,
        "workspaceGate": {
            "allocBase": str(tmp_path / "state" / "acp"),
            "reposRoot": str(tmp_path / "state" / "repos"),
            "stateBucket": "acp",
        },
    }


# --- the manifest and its factories -------------------------------------------


def test_the_manifest_declares_gate_observer_and_hook():
    mf = PluginManifest.from_toml_path(MANIFEST_PATH)
    assert mf.id == "code-flow"
    assert [g.name for g in mf.contributes.tool_gates] == ["write_gate"]
    assert [o.name for o in mf.contributes.session_observers] == ["workspace_release"]
    assert [h.name for h in mf.contributes.hooks] == ["code_flow"]
    assert mf.contributes.tools == [], "no tool rows this wave: the workbench boards with the exec swap"


def test_the_registry_builds_the_gate_from_the_real_factory_strings(tmp_path):
    """``location`` is the manifest path, the discovery convention
    (discover.py sets location=manifest_path; activation appends
    location.parent to sys.path) -- so the registry's own importability
    machinery is what serves the factory strings here, not this test
    module's path setup."""
    reg = PluginRegistry()
    mf = PluginManifest.from_toml_path(MANIFEST_PATH)
    reg.activate([DiscoveredPlugin(manifest=mf, source=ManifestOrigin.USER, location=MANIFEST_PATH)])
    assert reg.tool_gate_names() == ["write_gate"]
    assert reg.session_observer_names() == ["workspace_release"]
    built = reg.build_tool_gate(
        "write_gate",
        config=armed_slice(tmp_path),
        services=ServiceLocator(workspace=tmp_path / "home", user_id="u", agent_id="a"),
    )
    assert isinstance(built, WorkspaceGate)
    observer = reg.build_session_observer(
        "workspace_release",
        config=armed_slice(tmp_path),
        services=ServiceLocator(workspace=tmp_path / "home", user_id="u", agent_id="a"),
    )
    assert isinstance(observer, WorkspaceReleaseObserver)


def test_the_gate_satisfies_the_tool_gate_paper(tmp_path):
    gate = make_write_gate(ctx_for(tmp_path, armed_slice(tmp_path)))
    assert isinstance(gate, ToolGate)
    assert gate.name == "write_gate"


def test_the_observer_satisfies_the_session_events_paper(tmp_path):
    observer = make_release_observer(ctx_for(tmp_path, armed_slice(tmp_path)))
    assert isinstance(observer, SessionObserver)


def test_an_absent_slice_casts_no_surface_at_all(tmp_path):
    """D6: no config means no gate, no observer, no hook -- the fork's
    build_workspace_gate-returns-None contract (fork workspace_gate.py:1241-1253)."""
    assert make_write_gate(ctx_for(tmp_path, {})) is None
    assert make_release_observer(ctx_for(tmp_path, {})) is None
    assert make_flow_hook(ctx_for(tmp_path, {})) is None


def test_a_disabled_slice_casts_no_surface_even_when_armed(tmp_path):
    off = dict(armed_slice(tmp_path), enabled=False)
    assert make_write_gate(ctx_for(tmp_path, off)) is None
    assert make_release_observer(ctx_for(tmp_path, off)) is None
    assert make_flow_hook(ctx_for(tmp_path, off)) is None


def test_enabled_without_the_gate_arming_builds_hook_but_no_gate(tmp_path):
    """The flow can be on while the gate is unarmed (no workspaceGate render):
    the gate and the observer decline, exactly as the fork's unarmed env did."""
    on_only = {"enabled": True}
    assert make_write_gate(ctx_for(tmp_path, on_only)) is None
    assert make_release_observer(ctx_for(tmp_path, on_only)) is None
    assert isinstance(make_flow_hook(ctx_for(tmp_path, on_only)), CodeFlowHook)


MALFORMED_SLICE = {"enabled": True, "workspaceGate": "not-a-table"}


def test_a_malformed_slice_casts_a_fail_closed_gate_over_writes(tmp_path):
    """A raising factory is logged and SKIPPED by the lenient stack builder,
    so a pydantic error escaping make_write_gate = an ungated deploy under a
    config that says enabled:true. The factory answers with the fail-closed
    sentinel instead: writes are refused and the refusal names the slice to
    fix."""
    gate = make_write_gate(ctx_for(tmp_path, MALFORMED_SLICE))
    assert gate is not None and isinstance(gate, ToolGate)
    assert gate.name == "write_gate"
    verdict = asyncio.run(gate.adjudicate("write_file", {"path": "b.py"}, session_workdir=None))
    assert verdict is not None and "WRITE BLOCKED" in verdict
    assert "plugins.config['code-flow']" in verdict
    # The other two factories decline on the same error: an absent observer
    # or hook leaks toward keeping work, the safe direction.
    assert make_release_observer(ctx_for(tmp_path, MALFORMED_SLICE)) is None
    assert make_flow_hook(ctx_for(tmp_path, MALFORMED_SLICE)) is None


def test_a_malformed_slice_still_lets_reads_flow(tmp_path):
    """Fail closed, not fail shut: reads cannot misdirect a write, so the
    sentinel classifies with the gate's own rules and waves them through."""
    gate = make_write_gate(ctx_for(tmp_path, MALFORMED_SLICE))
    assert asyncio.run(gate.adjudicate("read_file", {"path": "a.py"}, session_workdir=None)) is None
    assert asyncio.run(gate.adjudicate("exec", {"command": "git status"}, session_workdir=None)) is None
    blocked = asyncio.run(gate.adjudicate("exec", {"command": "rm -rf build"}, session_workdir=None))
    assert blocked is not None and "WRITE BLOCKED" in blocked
    assert asyncio.run(gate.adjudicate("mcp_files_delete_file", {}, session_workdir=None)) is not None


def test_the_rendered_spellings_arm_the_gates_roots(tmp_path):
    gate = make_write_gate(ctx_for(tmp_path, armed_slice(tmp_path)))
    assert gate._alloc_base == tmp_path / "state" / "acp"
    assert gate.repos_root == tmp_path / "state" / "repos"
    assert gate.workspace == tmp_path / "home"


# --- the grants ---------------------------------------------------------------


def test_bind_runtime_routes_direct_ask_through_the_session_key(tmp_path):
    gate = make_write_gate(ctx_for(tmp_path, armed_slice(tmp_path)))
    asked = []

    async def fake_direct_ask(prompt, choices, conversation_id, timeout_s=None):
        asked.append((prompt, choices, conversation_id, timeout_s))
        return "1"

    gate.bind_runtime(SimpleNamespace(direct_ask=fake_direct_ask, rebind_workdir=None))
    gate.cid = lambda: "acp:sess-1"
    answer = asyncio.run(gate.ask("proceed?", ["worktree", "readonly"], ""))
    assert answer == "1"
    assert asked == [("proceed?", ["worktree", "readonly"], "acp:sess-1", None)]


def test_direct_ask_answers_none_without_a_session_key(tmp_path):
    """The fork's ask hook answered None with no conversation id (fork
    loop/main.py:1679-1681); the pending protocol then takes over."""
    gate = make_write_gate(ctx_for(tmp_path, armed_slice(tmp_path)))

    async def fake_direct_ask(prompt, choices, conversation_id, timeout_s=None):
        raise AssertionError("must not be called without a key")

    gate.bind_runtime(SimpleNamespace(direct_ask=fake_direct_ask, rebind_workdir=None))
    gate.cid = lambda: ""
    assert asyncio.run(gate.ask("proceed?", ["worktree"], "")) is None


def test_bind_runtime_routes_rebind_through_the_session_key(tmp_path):
    gate = make_write_gate(ctx_for(tmp_path, armed_slice(tmp_path)))
    rebinds = []
    gate.bind_runtime(
        SimpleNamespace(direct_ask=None, rebind_workdir=lambda key, target: rebinds.append((key, target)))
    )
    gate.cid = lambda: "acp:sess-2"
    gate.rebind(tmp_path / "wt")
    assert rebinds == [("acp:sess-2", tmp_path / "wt")]
    # The CLI layout persists no session: without a key the grant is not called.
    gate.cid = lambda: ""
    gate.rebind(tmp_path / "elsewhere")
    assert len(rebinds) == 1


def test_absent_grants_leave_the_pending_protocol_in_charge(tmp_path):
    gate = make_write_gate(ctx_for(tmp_path, armed_slice(tmp_path)))
    gate.bind_runtime(SimpleNamespace(direct_ask=None, rebind_workdir=None))
    assert gate.ask is None
    assert gate.rebind is None


# --- the session key (the fork's cid hook, now the turn-frame hook) ------------


def test_the_hook_captures_the_session_key_for_the_gates_identity(tmp_path):
    hook = make_flow_hook(ctx_for(tmp_path, {"enabled": True}))
    gate = make_write_gate(ctx_for(tmp_path, armed_slice(tmp_path)))

    async def turn():
        await hook.before_iteration(AgentHookContext(session_key="web:alpha"))
        return gate._identity(), gate._raw_key()

    identity, raw = asyncio.run(turn())
    assert identity == "acp-web_alpha"
    assert raw == "web:alpha"


def test_an_uncaptured_turn_falls_back_to_the_unkeyed_identity(tmp_path):
    gate = make_write_gate(ctx_for(tmp_path, armed_slice(tmp_path)))

    async def turn():
        return gate._identity()

    assert asyncio.run(turn()) == "acp-unkeyed"


# --- release on retirement ------------------------------------------------------


def _bind_primary(repo: Path, base: Path, repos: Path, session: str) -> None:
    gate = wg.WorkspaceGate(repo, repos, alloc_base=base)
    gate.cid = lambda: session
    assert asyncio.run(gate.adjudicate("write_file", {"path": "b.py"}, session_workdir=None)) is None


def _bind_worktree(repo: Path, base: Path, repos: Path, session: str) -> dict:
    gate = wg.WorkspaceGate(repo, repos, alloc_base=base)
    gate.cid = lambda: session

    async def approve(prompt, choices, default):
        return "worktree"

    gate.ask = approve
    (repo / "wip.txt").write_text("uncommitted\n")
    verdict = asyncio.run(gate.adjudicate("write_file", {"path": "b.py"}, session_workdir=None))
    assert verdict and "worktree" in verdict.lower()
    record = base / "allocations" / f"{wg._safe_segment(wg._identity_for_session(session))}.json"
    return json.loads(record.read_text())


@pytest.mark.parametrize("removed", [True, False])
def test_the_observer_releases_primary_on_every_delete_request(tmp_path, removed):
    """Fork parity: the delete REQUEST frees the workspace (the fork's ACP
    delete path called release_for_session unconditionally, fork
    acp/methods.py:469-480) -- a cache-only session can still hold an
    allocation, so the removal outcome does not filter the release."""
    base, repos = tmp_path / "state" / "acp", tmp_path / "state" / "repos"
    repo = tmp_path / "repo"
    make_repo(repo)
    _bind_primary(repo, base, repos, "acp:gone")
    repo_id, _ = wg.repo_identity(repo)
    assert (repos / repo_id / "primary.json").exists()

    observer = make_release_observer(ctx_for(tmp_path, armed_slice(tmp_path)))
    observer.on_session_deleted("acp:gone", removed)

    assert not (repos / repo_id / "primary.json").exists()
    record = base / "allocations" / f"{wg._safe_segment(wg._identity_for_session('acp:gone'))}.json"
    assert not record.exists()


def test_the_observer_removes_an_empty_handed_worktree(tmp_path):
    base, repos = tmp_path / "state" / "acp", tmp_path / "state" / "repos"
    repo = tmp_path / "repo"
    make_repo(repo)
    alloc = _bind_worktree(repo, base, repos, "acp:empty")
    wt = Path(alloc["boundWorkdir"])
    assert wt.is_dir()

    observer = make_release_observer(ctx_for(tmp_path, armed_slice(tmp_path)))
    observer.on_session_deleted("acp:empty", True)

    assert not wt.exists(), "an empty-handed worktree is removed"
    record = base / "allocations" / f"{wg._safe_segment(wg._identity_for_session('acp:empty'))}.json"
    assert not record.exists()


def test_the_observer_keeps_a_worktree_with_content(tmp_path):
    base, repos = tmp_path / "state" / "acp", tmp_path / "state" / "repos"
    repo = tmp_path / "repo"
    make_repo(repo)
    alloc = _bind_worktree(repo, base, repos, "acp:kept")
    wt = Path(alloc["boundWorkdir"])
    (wt / "work.py").write_text("real work\n")

    observer = make_release_observer(ctx_for(tmp_path, armed_slice(tmp_path)))
    observer.on_session_deleted("acp:kept", True)

    assert wt.is_dir() and (wt / "work.py").read_text() == "real work\n"
    record = base / "allocations" / f"{wg._safe_segment(wg._identity_for_session('acp:kept'))}.json"
    assert json.loads(record.read_text())["state"] == "released_kept_worktree"


def test_the_observer_keeps_a_worktree_with_only_ignored_content(tmp_path):
    """Ported from fork tests/test_gate_release.py:110
    (test_release_keeps_a_worktree_with_ignored_content), ahead of the step-4
    equivalence package: it pins the ``--ignored=matching`` flag in
    _worktree_is_clean -- without it a release would delete worktrees holding
    ignored-only user files (an .env, a generated result)."""
    base, repos = tmp_path / "state" / "acp", tmp_path / "state" / "repos"
    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / ".gitignore").write_text("ignored-result.txt\n")
    subprocess.run(["git", "-C", str(repo), "add", ".gitignore"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", "ignore generated result"], check=True)
    alloc = _bind_worktree(repo, base, repos, "acp:ignored")
    wt = Path(alloc["boundWorkdir"])
    (wt / "ignored-result.txt").write_text("keep me\n")

    observer = make_release_observer(ctx_for(tmp_path, armed_slice(tmp_path)))
    observer.on_session_deleted("acp:ignored", True)

    assert wt.exists()
    assert (wt / "ignored-result.txt").read_text() == "keep me\n"
    record = base / "allocations" / f"{wg._safe_segment(wg._identity_for_session('acp:ignored'))}.json"
    assert json.loads(record.read_text())["state"] == "released_kept_worktree"


def test_a_session_holding_nothing_is_the_observers_own_noop(tmp_path):
    observer = make_release_observer(ctx_for(tmp_path, armed_slice(tmp_path)))
    observer.on_session_deleted("acp:never-allocated", True)


# --- the trunk registry's throat -------------------------------------------------


@pytest.mark.asyncio
async def test_the_gate_adjudicates_through_the_trunk_registry(tmp_path):
    """Full chain on the trunk seam: the hook captures the key, the registry
    passes the bound workdir explicitly, the gate blocks the first write on a
    dirty tree with the pending protocol, and the probe never runs."""
    from raven.agent import workdir
    from raven.agent.tools.registry import ToolRegistry
    from raven.contracts.tool import Tool

    class _WriteProbe(Tool):
        def __init__(self):
            self.ran = 0

        @property
        def name(self):
            return "write_file"

        @property
        def description(self):
            return "records dispatch"

        @property
        def parameters(self):
            return {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

        async def execute(self, **kwargs):
            self.ran += 1
            return "wrote"

    repo = tmp_path / "repo"
    make_repo(repo)
    (repo / "wip.txt").write_text("uncommitted\n")

    hook = make_flow_hook(ctx_for(tmp_path, {"enabled": True}))
    gate = make_write_gate(ctx_for(tmp_path, armed_slice(tmp_path)))
    probe = _WriteProbe()
    registry = ToolRegistry(tool_gates=[gate])
    registry.register(probe)

    await hook.before_iteration(AgentHookContext(session_key="web:turn-1"))
    with workdir.bind(repo):
        result = await registry.execute("write_file", {"path": "b.py"})

    assert "WRITE BLOCKED" in str(result)
    assert probe.ran == 0, "a verdict means the call never runs"
    record = (
        tmp_path / "state" / "acp" / "allocations" / f"{wg._safe_segment(wg._identity_for_session('web:turn-1'))}.json"
    )
    assert json.loads(record.read_text())["state"] == "awaiting_user"
