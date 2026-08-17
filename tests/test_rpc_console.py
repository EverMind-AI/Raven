"""Tests for the console surface and for the guarantees the gateway rests on.

Everything here started life as a mutation that survived the suite: break the
implementation, and nothing went red. Each test below is the answer to one of
those, so the property it pins is one somebody already proved was unguarded.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.rpc.methods import console as console_module
from raven.rpc.methods.console import _SETTINGS_SIMPLE_KEYS, _hub_marker_name

# ---------------------------------------------------------------------------
# settings.set's whitelist is a security boundary, so it is pinned exactly
# ---------------------------------------------------------------------------


def test_the_settings_whitelist_holds_no_containment_control() -> None:
    """These three decide what an attacker who reaches the RPC surface can then
    do, and the surface has no confirmation step. Adding one back should take an
    argument, which is what failing this test forces."""
    for forbidden in ("tools.restrictToWorkspace", "tools.sandbox.backend", "tools.web.proxy"):
        assert forbidden not in _SETTINGS_SIMPLE_KEYS, (
            f"{forbidden} weakens containment and must not be settable over RPC without confirmation"
        )


def test_the_settings_whitelist_is_exactly_this_set() -> None:
    """Pinned wholesale, not just the denials: a key added here becomes reachable
    from every RPC client at once, so it should be a deliberate edit rather than
    a line that rode in with something else."""
    assert set(_SETTINGS_SIMPLE_KEYS) == {
        "tools.exec.timeout",
        "tools.web.search.apiKey",
        "tools.web.jinaApiKey",
        "tools.media.image.apiKey",
        "tools.deepResearch.apiKey",
        "channels.sendProgress",
        "channels.sendToolHints",
        "memory.memoryTopK",
        "agents.defaults.enablePersonalization",
        "agents.defaults.reasoningEffort",
    }


# ---------------------------------------------------------------------------
# ext.list survives without the skill market
# ---------------------------------------------------------------------------


def test_the_hub_marker_is_none_when_the_market_is_absent(monkeypatch: pytest.MonkeyPatch) -> None:
    """The market is an optional install. Its absence is a normal state, so the
    lookup answers ``None`` rather than raising -- a raise here used to cost the
    whole skill list, not just the two hub fields."""
    import builtins

    real_import = builtins.__import__

    def _no_skillhub(name, *args, **kwargs):
        if name == "raven.rpc.methods.skillhub":
            raise ImportError("market not installed")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_skillhub)
    assert _hub_marker_name() is None


# The other half of this -- that an installed market yields *its* marker name,
# so hub-installed skills are recognised -- belongs with the market itself and
# is not testable here: nothing in this tree provides skillhub.


async def test_ext_list_still_reports_skills_without_the_market(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The regression this guards: the market import sat inside the loop's try,
    so a missing market did not cost the hub flag -- it cost every skill."""

    class _Skill:
        def __init__(self, name: str) -> None:
            self.name = name
            self.description = "d"
            self.source = "builtin"
            self.always = False
            self.path = tmp_path / name / "SKILL.md"

    class _Catalog:
        def gather_all_skills(self):
            return [_Skill("alpha"), _Skill("beta")]

    # Both config loaders are stubbed: this asserts about skills, and reading
    # the developer's own ~/.raven/config.json made the test fail on any machine
    # whose config is newer than the schema on the branch under test.
    from raven.config import loader as config_loader
    from raven.config import raven as raven_config

    monkeypatch.setattr(config_loader, "load_config", lambda: SimpleNamespace(tools=SimpleNamespace(mcp_servers={})))
    monkeypatch.setattr(
        raven_config,
        "load_raven_config",
        # Both attributes the handler reads. With only `skill_forge` the
        # plugin block raised on `ec.plugins.disabled` and its `except` swallowed
        # it, so a quarter of ext.list sat unexecuted for the whole suite.
        lambda: SimpleNamespace(skill_forge=None, plugins=SimpleNamespace(disabled=[])),
    )
    monkeypatch.setattr(console_module, "_hub_marker_name", lambda: None)
    loop = SimpleNamespace(
        context=SimpleNamespace(skills=_Catalog()),
        tools=SimpleNamespace(tool_names=[], get=lambda _name: None),
    )

    result = await console_module.ext_list({}, agent_loop_factory=lambda: loop)

    assert [s["name"] for s in result["skills"]] == ["alpha", "beta"]
    assert all(s["hub"] is False and s["hub_id"] == "" for s in result["skills"])


# ---------------------------------------------------------------------------
# system.upgrade refuses outside a running gateway
# ---------------------------------------------------------------------------


async def test_system_upgrade_refuses_when_no_gateway_is_running() -> None:
    """The helper waits for *this* process to exit and relaunches it, so it is
    only meaningful inside `raven serve`. On the TUI path the guard is what turns
    a meaningless call into a typed refusal instead of a half-done upgrade."""
    from raven.cli.serve_commands import SERVE
    from raven.rpc.errors import ConfigValidationError
    from raven.rpc.methods.system import system_upgrade

    assert not SERVE.running, "precondition: no gateway armed in a unit test"

    with pytest.raises(ConfigValidationError) as excinfo:
        await system_upgrade({})
    assert excinfo.value.data["reason"] == "not_serving"


# ---------------------------------------------------------------------------
# the two RPC assemblies must not drift
# ---------------------------------------------------------------------------


async def test_the_gateway_registers_what_the_umbrella_does() -> None:
    """`raven serve` builds its dispatcher through ``build_rpc_stack``; the TUI
    builds one through the umbrella. A method registered on one and not the
    other is a client-visible difference nobody would notice until a page called
    it, so the two sets are compared directly rather than against a hand-written
    mirror.
    """
    from raven.rpc.dispatcher import Dispatcher
    from raven.rpc.methods import register_aligned_methods

    umbrella = Dispatcher()
    register_aligned_methods(umbrella)

    import raven.rpc.bootstrap as bootstrap_module

    async def _sink(_frame) -> None:
        return None

    stack = await bootstrap_module.build_rpc_stack(_sink)
    try:
        gateway_methods = set(stack.dispatcher.methods())
    finally:
        await asyncio.wait_for(stack.teardown(), timeout=30)

    missing = set(umbrella.methods()) - gateway_methods
    assert not missing, f"the gateway is missing methods the umbrella registers: {sorted(missing)}"


# ---------------------------------------------------------------------------
# cron.save: a create-then-edit round trip, which nothing covered
# ---------------------------------------------------------------------------


def _cron_loop(tmp_path: Path):
    """A loop stub carrying a real CronService over a scratch jobs.json.

    Real, not a fake: the bug this pins was a kwarg the service does not take,
    and a fake would have accepted anything it was handed.
    """
    from raven.proactive_engine.schedulers.cron.service import CronService

    return SimpleNamespace(cron_service=CronService(tmp_path / "jobs.json", allowed_channels=None))


@pytest.mark.asyncio
async def test_cron_save_creates_a_job(tmp_path: Path) -> None:
    """The plain create path -- which raised TypeError on every call, because
    `add_job` was handed a `job_id` parameter it did not have."""
    loop = _cron_loop(tmp_path)
    r = await console_module.cron_save(
        {"kind": "every", "every_seconds": 3600, "name": "hourly", "message": "ping"},
        agent_loop_factory=lambda: loop,
    )

    assert r["job"]["name"] == "hourly"
    assert r["job"]["id"]
    assert [j["name"] for j in (await console_module.cron_list({}, agent_loop_factory=lambda: loop))["jobs"]] == [
        "hourly"
    ]


@pytest.mark.asyncio
async def test_cron_save_edits_in_place_and_keeps_the_id(tmp_path: Path) -> None:
    """An edit must land on the same job: run history lives in the `cron:<id>`
    session, so a fresh id orphans it, and a second row would be a duplicate."""
    loop = _cron_loop(tmp_path)
    created = await console_module.cron_save(
        {"kind": "every", "every_seconds": 3600, "name": "hourly", "message": "ping"},
        agent_loop_factory=lambda: loop,
    )
    job_id = created["job"]["id"]

    edited = await console_module.cron_save(
        {"kind": "every", "every_seconds": 7200, "name": "two-hourly", "message": "pong", "id": job_id},
        agent_loop_factory=lambda: loop,
    )

    assert edited["job"]["id"] == job_id
    # The in-place branch is the one path that changes a schedule without going
    # through the create path's `_compute_next_run`, so it is the one that can
    # drift: drop that recompute and an edited job keeps aiming at the fire time
    # its old schedule picked -- "every 1h" changed to "every 12h" would still
    # go off at the top of the hour it was already counting down to.
    assert edited["job"]["next_run_at_ms"] != created["job"]["next_run_at_ms"], (
        "an edit must recompute the next fire from the new schedule"
    )
    jobs = (await console_module.cron_list({}, agent_loop_factory=lambda: loop))["jobs"]
    assert len(jobs) == 1
    assert jobs[0]["name"] == "two-hourly"


@pytest.mark.asyncio
async def test_a_rejected_edit_leaves_the_job_alone(tmp_path: Path) -> None:
    """The edit path used to remove the job before building its replacement, so
    an edit that failed validation deleted the job it was editing."""
    from raven.rpc.errors import ConfigValidationError

    loop = _cron_loop(tmp_path)
    created = await console_module.cron_save(
        {"kind": "every", "every_seconds": 3600, "name": "hourly", "message": "ping"},
        agent_loop_factory=lambda: loop,
    )
    job_id = created["job"]["id"]

    with pytest.raises(ConfigValidationError):
        await console_module.cron_save(
            {"kind": "at", "at_iso": "1999-01-01T00:00:00", "name": "past", "message": "x", "id": job_id},
            agent_loop_factory=lambda: loop,
        )

    jobs = (await console_module.cron_list({}, agent_loop_factory=lambda: loop))["jobs"]
    assert [j["id"] for j in jobs] == [job_id]
    assert jobs[0]["name"] == "hourly"


# ---------------------------------------------------------------------------
# ext.list reports MCP from what this branch actually knows
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_ext_list_reports_a_server_by_the_tools_it_registered(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A server whose tools are registered is connected, and its tools name it.

    This read `loop.mcp_manager`, which `AgentLoop` does not have, so `manager`
    was always None and every configured server was reported disconnected with
    zero tools -- including one the agent was calling right then.
    """

    # A stub rather than the real config: `ext.list` reads exactly one field off
    # it, and loading the machine's own config makes the test depend on whatever
    # the developer happens to have installed. The shape is what
    # `resolve_transport` reads: type, then command, then url.
    def _server(url: str) -> SimpleNamespace:
        return SimpleNamespace(enabled=True, type=None, command=None, url=url)

    cfg = SimpleNamespace(
        tools=SimpleNamespace(
            mcp_servers={
                "ctx7": _server("https://example.invalid/mcp"),
                "asleep": _server("https://example.invalid/mcp"),
                # An underscore in the name is what a split on underscores
                # cannot survive: it cannot tell where the server ends and the
                # tool begins, and both halves may contain one.
                "github_enterprise": _server("https://example.invalid/mcp"),
                "brave-search": _server("https://example.invalid/mcp"),
            }
        )
    )
    # `ext.list` imports load_config inside the function, so the name to patch
    # is the one it imports from, not a module-level alias it does not have.
    from raven.config import loader as config_loader
    from raven.config import raven as raven_config

    monkeypatch.setattr(config_loader, "load_config", lambda: cfg)
    # `ext.list` also loads the raven-level config for skill-forge settings,
    # which this assertion does not care about.
    monkeypatch.setattr(
        raven_config,
        "load_raven_config",
        # Both attributes the handler reads. With only `skill_forge` the
        # plugin block raised on `ec.plugins.disabled` and its `except` swallowed
        # it, so a quarter of ext.list sat unexecuted for the whole suite.
        lambda: SimpleNamespace(skill_forge=None, plugins=SimpleNamespace(disabled=[])),
    )
    monkeypatch.setattr(console_module, "_hub_marker_name", lambda: None)

    class _Catalog:
        def gather_all_skills(self):
            return []

    names = [
        "read_file",
        "mcp_ctx7_get_docs",
        "mcp_ctx7_resolve_id",
        "mcp_github_enterprise_list_repos",
        "mcp_brave-search_query",
    ]
    loop = SimpleNamespace(
        context=SimpleNamespace(skills=_Catalog()),
        tools=SimpleNamespace(tool_names=names, get=lambda _name: None),
    )

    result = await console_module.ext_list({}, agent_loop_factory=lambda: loop)

    by_name = {m["name"]: m for m in result["mcp"]}
    assert by_name["ctx7"]["connected"] is True
    assert by_name["ctx7"]["tool_count"] == 2
    # Declared but nothing registered yet: still listed, so an installed plugin
    # does not vanish between a restart and the first turn.
    assert by_name["asleep"]["connected"] is False
    assert by_name["asleep"]["tool_count"] == 0

    # An underscored server name: reported down while its tool was registered,
    # and its tool attributed to a server called `github` that does not exist.
    assert by_name["github_enterprise"]["connected"] is True
    assert by_name["github_enterprise"]["tool_count"] == 1
    assert by_name["brave-search"]["tool_count"] == 1

    # Non-empty is the load-bearing half. `plugin_discovery_sources()` points its
    # bundled_dir at `raven/plugin/memory/`, which is in this tree, so discovery
    # always has something to find -- and an empty list therefore means the block
    # raised and its `except` swallowed it, which is what the stub above used to
    # cause. A shape assertion alone would hold vacuously over that empty list.
    assert result["plugins"], "plugin discovery returned nothing; it raised and was swallowed"
    assert all({"id", "display_name", "version", "enabled", "bundled"} <= set(row) for row in result["plugins"]), (
        result["plugins"]
    )

    owners = {t["name"]: t["mcp_server"] for t in result["tools"]}
    assert owners["mcp_ctx7_get_docs"] == "ctx7"
    assert owners["mcp_github_enterprise_list_repos"] == "github_enterprise"
    assert owners["mcp_brave-search_query"] == "brave-search"
    assert owners["read_file"] is None


# ---------------------------------------------------------------------------
# fs.read reads a window, not the whole file
# ---------------------------------------------------------------------------


def _fs_loop(tmp_path: Path):
    return SimpleNamespace(workspace=str(tmp_path), sessions=None)


@pytest.mark.asyncio
async def test_fs_read_returns_only_the_window_it_was_asked_for(tmp_path: Path) -> None:
    """The old shape was `read_bytes()[:limit]` -- the whole file allocated to
    return a slice of it, synchronously, on the shared event loop."""
    target = tmp_path / "big.log"
    target.write_bytes(b"x" * 5000)

    r = await console_module.fs_read(
        {"path": "big.log", "max_bytes": 100}, agent_loop_factory=lambda: _fs_loop(tmp_path)
    )

    assert len(r["content"]) == 100
    assert r["truncated"] is True
    assert r["size"] == 5000


@pytest.mark.asyncio
async def test_fs_read_still_serves_the_head_of_a_file_past_the_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A huge file is the case this exists for, so it gets its window.

    The bound is on the read, not on the file: `fh.read(limit)` allocates at
    most `limit` whatever the size. Refusing by size would have made exactly the
    motivating case -- a multi-gigabyte log -- unreadable, while adding nothing
    to the allocation guarantee.
    """
    from raven.rpc import files as files_module

    monkeypatch.setattr(files_module, "MAX_VIEW_BYTES", 1000)
    target = tmp_path / "huge.log"
    target.write_bytes(b"y" * 4000)

    r = await console_module.fs_read({"path": "huge.log"}, agent_loop_factory=lambda: _fs_loop(tmp_path))

    assert len(r["content"]) == 1000, "the clamped window, not the file"
    assert r["truncated"] is True, "the signal that says there is more where this came from"
    assert r["size"] == 4000


@pytest.mark.asyncio
async def test_fs_read_clamps_a_caller_supplied_ceiling(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`max_bytes` came straight off the wire with no upper bound, so it was an
    unbounded allocation request from the caller."""
    from raven.rpc import files as files_module

    monkeypatch.setattr(files_module, "MAX_VIEW_BYTES", 500)
    target = tmp_path / "mid.log"
    target.write_bytes(b"z" * 400)

    r = await console_module.fs_read(
        {"path": "mid.log", "max_bytes": 10_000_000_000}, agent_loop_factory=lambda: _fs_loop(tmp_path)
    )

    assert len(r["content"]) == 400
    assert r["truncated"] is False


# ---------------------------------------------------------------------------
# cron.save with no loop: the handler builds its own service off the config path
# ---------------------------------------------------------------------------


@pytest.fixture
def isolated_config(tmp_path: Path):
    """A config path of our own -- the cron store hangs off its parent."""
    import json

    import raven.config.loader as loader
    from raven.config.loader import set_config_path

    # Restored, not cleared: the global outlives this module, and after the rpc
    # rename this file collects ahead of `test_segments.py`, which reads the
    # real config. Clearing left that module reading a different one than it
    # does on its own.
    previous = loader._current_config_path
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"agents": {"defaults": {"workspace": str(tmp_path / "ws")}}}))
    set_config_path(cfg_path)
    yield
    loader._current_config_path = previous


@pytest.mark.asyncio
async def test_saving_a_job_returns_it(isolated_config: None) -> None:
    """No `agent_loop_factory`, so this drives the other half of `_cron_service`:
    the branch that builds a service off `get_cron_dir()` when no loop is bound."""
    r = await console_module.cron_save({"kind": "cron", "expr": "0 9 * * *", "name": "standup", "message": "hi"})

    assert r["job"]["name"] == "standup"
    assert r["job"]["expr"] == "0 9 * * *"


@pytest.mark.asyncio
async def test_editing_a_job_keeps_its_id(isolated_config: None) -> None:
    """The id is the job's run history: it lives in the ``cron:<id>`` session, so
    an edit that mints a fresh id orphans everything the job has ever done."""
    first = await console_module.cron_save({"kind": "cron", "expr": "0 9 * * *", "name": "standup", "message": "hi"})
    job_id = first["job"]["id"]

    edited = await console_module.cron_save(
        {"id": job_id, "kind": "cron", "expr": "0 10 * * *", "name": "standup", "message": "hi there"}
    )

    assert edited["job"]["id"] == job_id
    # The stored schedule, not just the recompute its sibling test pins.
    assert edited["job"]["expr"] == "0 10 * * *"
    listed = await console_module.cron_list({})
    assert len(listed["jobs"]) == 1, "an edit must replace the job, not add a second one"
