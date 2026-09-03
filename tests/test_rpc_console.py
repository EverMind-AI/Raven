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

import raven.home as raven_home_module
from raven.config import update_tools
from raven.contracts.tool import Tool
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
        if name == "raven.skill_hub.hub":
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
                # A character the sanitiser rewrites: the registered name is
                # mcp_data_warehouse_query, which no configured key prefixes.
                "data.warehouse": _server("https://example.invalid/mcp"),
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

    # A real registry, not a names-only stub: ownership now comes from the
    # origin index it holds, and a stub that only lists names cannot show
    # whether the index is being consulted at all.
    from raven.agent.tools.registry import ToolRegistry
    from raven.mcp.naming import MCPToolRef, tool_name

    class _Stub(Tool):
        def __init__(self, name: str) -> None:
            self._name = name

        @property
        def name(self) -> str:
            return self._name

        @property
        def description(self) -> str:
            return "stub"

        @property
        def parameters(self) -> dict:
            return {"type": "object", "properties": {}}

        async def execute(self, **kwargs):
            return ""

    registry = ToolRegistry()
    registry.register(_Stub("read_file"))
    for server, tool in [
        ("ctx7", "get_docs"),
        ("ctx7", "resolve_id"),
        ("github_enterprise", "list_repos"),
        ("brave-search", "query"),
        # The case the old prefix match got wrong: the dot is sanitised out of
        # the registered name, so no configured key prefixes it and the server
        # reported as connected with zero tools while the agent called them.
        ("data.warehouse", "query"),
    ]:
        registered = tool_name(server, tool, taken=registry)
        registry.register(_Stub(registered), origin=MCPToolRef(name=registered, server=server, tool=tool))

    loop = SimpleNamespace(context=SimpleNamespace(skills=_Catalog()), tools=registry)

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
    assert by_name["data.warehouse"]["tool_count"] == 1, "a sanitised server name lost its tools"
    assert by_name["data.warehouse"]["connected"] is True

    # Non-empty is the load-bearing half. `plugin_discovery_sources()` scans the
    # `raven.plugins` entry-point group, which the dev environment fills with
    # `everos-memory`, so discovery always has something to find -- and an empty
    # list therefore means the block raised and its `except` swallowed it, which
    # is what the stub above used to cause. A shape assertion alone would hold
    # vacuously over that empty list.
    assert result["plugins"], "plugin discovery returned nothing; it raised and was swallowed"
    assert all({"id", "display_name", "version", "enabled", "bundled"} <= set(row) for row in result["plugins"]), (
        result["plugins"]
    )

    owners = {t["name"]: t["mcp_server"] for t in result["tools"]}
    assert owners["mcp_ctx7_get_docs"] == "ctx7"
    assert owners["mcp_github_enterprise_list_repos"] == "github_enterprise"
    assert owners["mcp_brave-search_query"] == "brave-search"
    assert owners["mcp_data_warehouse_query"] == "data.warehouse"
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

    from raven.config.loader import set_config_path

    # Restored, not cleared: the global outlives this module, and after the rpc
    # rename this file collects ahead of `test_segments.py`, which reads the
    # real config. Clearing left that module reading a different one than it
    # does on its own.
    previous = raven_home_module._current_config_path
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps({"agents": {"defaults": {"workspace": str(tmp_path / "ws")}}}))
    set_config_path(cfg_path)
    yield
    raven_home_module._current_config_path = previous


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


# ---------------------------------------------------------------------------
# fs.* roots at the session's working directory, not at agent home
# ---------------------------------------------------------------------------


class _WorkdirLoop:
    """The two attributes the fs handlers read, with the resolver's contract."""

    def __init__(self, by_key: dict[str, Path], default: Path):
        self._by_key = by_key
        self._default = default
        self.workspace = default / "never-this"
        self.asked: list[str] = []

    def peek_session_workdir(self, session_key: str) -> Path:
        self.asked.append(session_key)
        return self._by_key.get(session_key, self._default)


def _loop_factory(loop):
    return lambda: loop


async def test_fs_list_roots_at_the_session_workdir(tmp_path: Path) -> None:
    """The panel lists where the session's turns actually read and write. Agent
    home is where the old code pointed, and with the default config that is
    ``~/.raven/workspace`` -- a directory the launch-directory policy never
    touches, so the tree showed a place no tool call would ever change."""
    launch = tmp_path / "proj"
    launch.mkdir()
    (launch / "notes.md").write_text("x")

    loop = _WorkdirLoop({}, launch)
    r = await console_module.fs_list({}, agent_loop_factory=_loop_factory(loop))

    assert r["root"] == str(launch.resolve())
    assert [e["name"] for e in r["entries"]] == ["notes.md"]


async def test_fs_list_asks_for_the_named_session(tmp_path: Path) -> None:
    """A session pinned to its own directory must list THAT directory: the key
    goes through to the resolver, which is where the override lives."""
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    (b / "pinned.txt").write_text("x")

    loop = _WorkdirLoop({"tui:beta": b}, a)
    r = await console_module.fs_list({"session": "tui:beta"}, agent_loop_factory=_loop_factory(loop))

    assert loop.asked == ["tui:beta"]
    assert [e["name"] for e in r["entries"]] == ["pinned.txt"]


def _agent_home(monkeypatch, home: Path):
    """Point ``_upload_root`` at ``home``. Patched on the loader module because
    the handler imports the name inside the function."""
    from raven.config import loader as config_loader

    monkeypatch.setattr(config_loader, "load_config", lambda: SimpleNamespace(workspace_path=str(home)))


async def test_fs_upload_lands_in_agent_home_not_the_session_workdir(tmp_path: Path, monkeypatch) -> None:
    """The relative path this returns rides back to ``turn.send``, which resolves
    an attachment against agent home and fences it there. Deposited in the
    session's working directory instead, the file exists and the turn still drops
    it, so the reader hands over an attachment the model never sees."""
    import base64

    home = tmp_path / "home"
    home.mkdir()
    launch = tmp_path / "proj"
    launch.mkdir()
    _agent_home(monkeypatch, home)
    loop = _WorkdirLoop({}, launch)

    r = await console_module.fs_upload(
        {"name": "pic.png", "content_b64": base64.b64encode(b"bytes").decode(), "session": "tui:x"},
        agent_loop_factory=_loop_factory(loop),
    )

    assert r["path"] == "uploads/pic.png"
    assert r["abs_path"] == str(home.resolve() / "uploads" / "pic.png")
    assert (home / "uploads" / "pic.png").read_bytes() == b"bytes"
    assert not (launch / "uploads").exists()


async def test_fs_upload_survives_a_session_workdir_that_takes_no_files(tmp_path: Path, monkeypatch) -> None:
    """The reported break: the page's engine launched by the desktop shell
    inherits ``/`` as its working directory, so an upload rooted there died on
    ``mkdir`` and reached the composer as a bare ``internal_error``."""
    import base64

    home = tmp_path / "home"
    home.mkdir()
    unwritable = tmp_path / "not-a-dir" / "sub"
    (tmp_path / "not-a-dir").write_text("x")
    _agent_home(monkeypatch, home)
    loop = _WorkdirLoop({}, unwritable)

    r = await console_module.fs_upload(
        {"name": "image.png", "content_b64": base64.b64encode(b"bytes").decode(), "session": "web:1"},
        agent_loop_factory=_loop_factory(loop),
    )

    assert (home / "uploads" / "image.png").read_bytes() == b"bytes"
    assert r["path"] == "uploads/image.png"


async def test_fs_upload_names_the_directory_it_could_not_create(tmp_path: Path, monkeypatch) -> None:
    """An unwritable agent home is a real configuration, and the page can only
    report what the error carries: unguarded, ``mkdir`` escaped as -32603 with
    the reason only in the server log."""
    import base64

    from raven.rpc.errors import ConfigValidationError

    home = tmp_path / "file" / "home"
    (tmp_path / "file").write_text("x")
    _agent_home(monkeypatch, home)

    with pytest.raises(ConfigValidationError) as excinfo:
        await console_module.fs_upload(
            {"name": "pic.png", "content_b64": base64.b64encode(b"bytes").decode()},
            agent_loop_factory=None,
        )

    assert "uploads" in str(excinfo.value)


async def test_fs_root_survives_a_loop_without_the_resolver(tmp_path: Path) -> None:
    """An older loop object has no ``peek_session_workdir``; the handler falls
    back to its workspace rather than crashing the panel."""
    ws = tmp_path / "ws"
    ws.mkdir()
    loop = SimpleNamespace(workspace=ws)

    r = await console_module.fs_list({}, agent_loop_factory=_loop_factory(loop))

    assert r["root"] == str(ws.resolve())


# ---------------------------------------------------------------------------
# fs.reveal -- the viewer's fence, then the host's file manager
# ---------------------------------------------------------------------------


async def test_fs_reveal_selects_the_file_in_the_host_file_manager(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch = tmp_path / "proj"
    launch.mkdir()
    target = launch / "report.md"
    target.write_text("# hi")
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "raven-home"))

    spawned: list[list[str]] = []
    monkeypatch.setattr("subprocess.Popen", lambda argv, **kw: spawned.append(argv))
    monkeypatch.setattr("sys.platform", "darwin")

    loop = _WorkdirLoop({}, launch)
    r = await console_module.fs_reveal({"path": "report.md"}, agent_loop_factory=_loop_factory(loop))

    assert r == {"ok": True}
    assert spawned == [["open", "-R", str(target.resolve())]]


async def test_fs_reveal_finds_an_uploaded_attachment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The page sends one string to two surfaces. A panel row is relative to the
    session's working directory; an attachment chip carries ``uploads/<name>``,
    which only agent home holds -- so rooting at the workdir alone made Reveal
    fail on the very file the viewer had just rendered from that same string."""
    home = tmp_path / "raven-home"
    (home / "workspace" / "uploads").mkdir(parents=True)
    target = home / "workspace" / "uploads" / "doc.pdf"
    target.write_bytes(b"%PDF-1.4 x")
    launch = tmp_path / "proj"
    launch.mkdir()
    monkeypatch.setenv("RAVEN_HOME", str(home))

    spawned: list[list[str]] = []
    monkeypatch.setattr("subprocess.Popen", lambda argv, **kw: spawned.append(argv))
    monkeypatch.setattr("sys.platform", "darwin")

    loop = _WorkdirLoop({}, launch)
    r = await console_module.fs_reveal(
        {"path": "uploads/doc.pdf", "session": "web:1"}, agent_loop_factory=_loop_factory(loop)
    )

    assert r == {"ok": True}
    assert spawned == [["open", "-R", str(target.resolve())]]


async def test_fs_reveal_refuses_what_the_viewer_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One fence for both: a path the page may not render must not pop a Finder
    window either, or the reveal button becomes the read the viewer denied."""
    from raven.rpc.errors import ConfigValidationError

    home = tmp_path / "raven-home"
    home.mkdir()
    secret = home / "serve.json"
    secret.write_text("{}")
    monkeypatch.setenv("RAVEN_HOME", str(home))

    spawned: list[list[str]] = []
    monkeypatch.setattr("subprocess.Popen", lambda argv, **kw: spawned.append(argv))

    loop = _WorkdirLoop({}, tmp_path)
    with pytest.raises(ConfigValidationError):
        await console_module.fs_reveal({"path": str(secret)}, agent_loop_factory=_loop_factory(loop))
    assert spawned == []


# ---------------------------------------------------------------------------
# fs.open -- the same fence, then the host's application
# ---------------------------------------------------------------------------


async def test_fs_open_hands_the_file_to_the_named_application(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    launch = tmp_path / "proj"
    launch.mkdir()
    target = launch / "deck.pptx"
    target.write_bytes(b"PK\x03\x04")
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "raven-home"))

    spawned: list[list[str]] = []
    monkeypatch.setattr("subprocess.Popen", lambda argv, **kw: spawned.append(argv))
    monkeypatch.setattr("sys.platform", "darwin")

    loop = _WorkdirLoop({}, launch)
    r = await console_module.fs_open({"path": "deck.pptx", "app": "Keynote"}, agent_loop_factory=_loop_factory(loop))

    assert r == {"ok": True, "app": "Keynote"}
    # The name is ONE argv element beside the resolved path, never concatenated.
    assert spawned == [["open", "-a", "Keynote", str(target.resolve())]]


async def test_fs_open_without_an_application_uses_the_host_default(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch = tmp_path / "proj"
    launch.mkdir()
    target = launch / "sheet.xlsx"
    target.write_bytes(b"PK\x03\x04")
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "raven-home"))

    spawned: list[list[str]] = []
    monkeypatch.setattr("subprocess.Popen", lambda argv, **kw: spawned.append(argv))
    monkeypatch.setattr("sys.platform", "darwin")

    loop = _WorkdirLoop({}, launch)
    r = await console_module.fs_open({"path": "sheet.xlsx"}, agent_loop_factory=_loop_factory(loop))

    # No `app` key at all rather than a null one: the reader picked nothing.
    assert r == {"ok": True}
    assert spawned == [["open", str(target.resolve())]]


@pytest.mark.parametrize(
    "app",
    [
        "/bin/sh",
        "../../usr/bin/python3",
        # These three start with a letter, so only the separator rule can stop
        # them -- without it the "name" would name an arbitrary binary.
        "usr/bin/sh",
        "Keynote/../../bin/sh",
        "C:\\Windows\\System32\\cmd.exe",
        "-a",
        "Keynote; rm -rf /",
        "Keynote && curl evil",
        "Keynote$(whoami)",
        "Key\nnote",
        "",
        " ",
        "K" * 65,
    ],
)
async def test_fs_open_refuses_anything_that_is_not_an_application_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, app: str
) -> None:
    """The application is a NAME. It becomes an argv element next to a resolved
    path and never reaches a shell, but a separator would name an arbitrary
    binary and a leading dash would smuggle a flag into ``open`` -- and a name
    shaped like a command is one worth refusing whatever cannot come of it."""
    from raven.rpc.errors import ConfigValidationError

    launch = tmp_path / "proj"
    launch.mkdir()
    (launch / "deck.pptx").write_bytes(b"PK\x03\x04")
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "raven-home"))

    spawned: list[list[str]] = []
    monkeypatch.setattr("subprocess.Popen", lambda argv, **kw: spawned.append(argv))
    monkeypatch.setattr("sys.platform", "darwin")

    loop = _WorkdirLoop({}, launch)
    if app.strip():
        with pytest.raises(ConfigValidationError):
            await console_module.fs_open({"path": "deck.pptx", "app": app}, agent_loop_factory=_loop_factory(loop))
        assert spawned == []
    else:
        # Blank is not a refusal, it is "no choice made" -- the host default.
        await console_module.fs_open({"path": "deck.pptx", "app": app}, agent_loop_factory=_loop_factory(loop))
        assert spawned == [["open", str((launch / "deck.pptx").resolve())]]


async def test_fs_open_accepts_the_names_real_applications_have(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch = tmp_path / "proj"
    launch.mkdir()
    (launch / "a.py").write_text("x = 1")
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "raven-home"))

    spawned: list[list[str]] = []
    monkeypatch.setattr("subprocess.Popen", lambda argv, **kw: spawned.append(argv))
    monkeypatch.setattr("sys.platform", "darwin")

    loop = _WorkdirLoop({}, launch)
    for app in ["Visual Studio Code", "Sublime Text 4", "IntelliJ IDEA CE", "Cursor", "Xcode.app"]:
        await console_module.fs_open({"path": "a.py", "app": app}, agent_loop_factory=_loop_factory(loop))
    assert [x[2] for x in spawned] == [
        "Visual Studio Code",
        "Sublime Text 4",
        "IntelliJ IDEA CE",
        "Cursor",
        "Xcode.app",
    ]


async def test_fs_open_refuses_what_the_viewer_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One fence for all three verbs. A path the page may not render must not be
    handed to an application either, or the open button becomes the read the
    viewer denied -- with a program of the caller's choosing attached."""
    from raven.rpc.errors import ConfigValidationError

    home = tmp_path / "raven-home"
    home.mkdir()
    secret = home / "serve.json"
    secret.write_text("{}")
    monkeypatch.setenv("RAVEN_HOME", str(home))

    spawned: list[list[str]] = []
    monkeypatch.setattr("subprocess.Popen", lambda argv, **kw: spawned.append(argv))

    loop = _WorkdirLoop({}, tmp_path)
    with pytest.raises(ConfigValidationError):
        await console_module.fs_open({"path": str(secret)}, agent_loop_factory=_loop_factory(loop))
    assert spawned == []


async def test_fs_open_on_linux_runs_the_application_and_falls_back_to_xdg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launch = tmp_path / "proj"
    launch.mkdir()
    target = launch / "notes.odt"
    target.write_bytes(b"PK\x03\x04")
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "raven-home"))

    spawned: list[list[str]] = []
    monkeypatch.setattr("subprocess.Popen", lambda argv, **kw: spawned.append(argv))
    monkeypatch.setattr("sys.platform", "linux")

    loop = _WorkdirLoop({}, launch)
    await console_module.fs_open({"path": "notes.odt", "app": "code"}, agent_loop_factory=_loop_factory(loop))
    await console_module.fs_open({"path": "notes.odt"}, agent_loop_factory=_loop_factory(loop))

    assert spawned == [["code", str(target.resolve())], ["xdg-open", str(target.resolve())]]


async def test_fs_open_reports_a_launcher_that_is_not_there(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing launcher is a message, not a traceback: Popen raises OSError
    and the page has to be able to say why nothing happened."""
    from raven.rpc.errors import ConfigValidationError

    launch = tmp_path / "proj"
    launch.mkdir()
    (launch / "deck.pptx").write_bytes(b"PK\x03\x04")
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "raven-home"))

    def boom(argv, **kw):
        raise OSError("no such file")

    monkeypatch.setattr("subprocess.Popen", boom)
    monkeypatch.setattr("sys.platform", "darwin")

    loop = _WorkdirLoop({}, launch)
    with pytest.raises(ConfigValidationError, match="open failed"):
        await console_module.fs_open({"path": "deck.pptx"}, agent_loop_factory=_loop_factory(loop))


# ---------------------------------------------------------------------------
# channels.status carries the field specs; channels.configure writes them
# ---------------------------------------------------------------------------


async def test_channels_status_rows_carry_their_field_specs(isolated_config: None) -> None:
    """The page's configure form is drawn from these rows: every field the
    channel takes rides along -- name, whether it is secret, whether a value is
    stored -- and never the value itself, because the row crosses the wire on
    every status poll."""
    r = await console_module.channels_status({})

    tg = next(c for c in r["channels"] if c["name"] == "telegram")
    keys = {f["key"] for f in tg["fields"]}
    assert "token" in keys
    assert "enabled" not in keys, "the switch is not a form field"
    token = next(f for f in tg["fields"] if f["key"] == "token")
    assert token["secret"] is True
    assert token["set"] is False
    assert "value" not in token


async def test_channels_configure_stores_the_field_and_status_says_so(isolated_config: None) -> None:
    import json as _json

    from raven.config.loader import get_config_path

    r = await console_module.channels_configure({"name": "telegram", "fields": {"token": "123:abc"}})
    assert r == {"applied": True}

    raw = _json.loads(get_config_path().read_text())
    assert raw["channels"]["telegram"]["token"] == "123:abc"

    status = await console_module.channels_status({})
    tg = next(c for c in status["channels"] if c["name"] == "telegram")
    token = next(f for f in tg["fields"] if f["key"] == "token")
    assert token["set"] is True
    assert "123:abc" not in _json.dumps(status), "secret values must never ride the status wire"


async def test_channels_configure_skips_blanks_and_refuses_unknown_fields(isolated_config: None) -> None:
    """A blank box means "left as is": the form sends every field it showed, so
    writing the empties would erase a stored secret on every unrelated save."""
    from raven.rpc.errors import ConfigValidationError

    r = await console_module.channels_configure({"name": "telegram", "fields": {"token": "  "}})
    assert r == {"applied": False}

    with pytest.raises(ConfigValidationError):
        await console_module.channels_configure({"name": "telegram", "fields": {"not_a_field": "x"}})
    with pytest.raises(ConfigValidationError):
        await console_module.channels_configure({"name": "telegram", "fields": {"enabled": True}})
    with pytest.raises(ConfigValidationError):
        await console_module.channels_configure({"name": "nope", "fields": {"token": "x"}})


async def test_channels_configure_connects_and_disconnects(isolated_config: None) -> None:
    """Connecting and disconnecting were both client-side flags that the next
    status poll overwrote: no RPC could write `enabled`, so the buttons moved
    nothing on disk and the row snapped back to whatever the config still said.
    """
    import json as _json

    from raven.config.loader import get_config_path

    def stored_enabled() -> bool:
        raw = _json.loads(get_config_path().read_text())
        return bool(raw["channels"]["telegram"].get("enabled"))

    async def reported_on() -> bool:
        status = await console_module.channels_status({})
        return next(c for c in status["channels"] if c["name"] == "telegram")["enabled"]

    r = await console_module.channels_configure({"name": "telegram", "fields": {"token": "123:abc"}, "enabled": True})
    assert r == {"applied": True}
    assert stored_enabled() is True
    assert await reported_on() is True

    r = await console_module.channels_configure({"name": "telegram", "fields": {}, "enabled": False})
    assert r == {"applied": True}
    assert stored_enabled() is False
    assert await reported_on() is False

    raw = _json.loads(get_config_path().read_text())
    assert raw["channels"]["telegram"]["token"] == "123:abc", "disconnecting must keep the credentials"


async def test_channels_configure_asks_the_gateway_to_start_the_adapter(isolated_config: None) -> None:
    """The switch is config; the adapter is the gateway's, in another process.
    Writing the flag alone was the whole of "connect", so a channel turned on
    here did nothing until the next launch -- for a scan-login entrance that
    meant no QR could ever be fetched and nobody could sign in from the UI.
    """
    asked: list[tuple[str, bool]] = []

    async def fake_start(name: str, *, enabled: bool = True) -> str:
        asked.append((name, enabled))
        return "started" if enabled else "stopped"

    import raven.gateway.live_probe as probe

    probe_start = probe.channel_start
    probe.channel_start = fake_start
    try:
        await console_module.channels_configure({"name": "telegram", "fields": {"token": "1:a"}, "enabled": True})
        assert asked == [("telegram", True)]
        await console_module.channels_configure({"name": "telegram", "fields": {}, "enabled": False})
        assert asked == [("telegram", True), ("telegram", False)]
        # A credential correction with no switch in it does not restart anything.
        await console_module.channels_configure({"name": "telegram", "fields": {"token": "2:b"}})
        assert asked == [("telegram", True), ("telegram", False)]
    finally:
        probe.channel_start = probe_start


async def test_channels_configure_still_applies_when_no_gateway_answers(isolated_config: None) -> None:
    """No gateway is the ordinary case for a first run: the config write stands
    and the next launch honours it, so the failure to reach one must not turn a
    successful write into an error."""
    import json as _json

    import raven.gateway.live_probe as probe
    from raven.config.loader import get_config_path

    async def boom(name: str, *, enabled: bool = True) -> str:
        raise OSError("no gateway here")

    probe_start = probe.channel_start
    probe.channel_start = boom
    try:
        r = await console_module.channels_configure({"name": "telegram", "fields": {"token": "1:a"}, "enabled": True})
    finally:
        probe.channel_start = probe_start
    assert r == {"applied": True}
    assert _json.loads(get_config_path().read_text())["channels"]["telegram"]["enabled"] is True


async def test_channels_configure_refuses_an_empty_request(isolated_config: None) -> None:
    from raven.rpc.errors import ConfigValidationError

    with pytest.raises(ConfigValidationError):
        await console_module.channels_configure({"name": "telegram", "fields": {}})
    with pytest.raises(ConfigValidationError):
        await console_module.channels_configure({"name": "telegram"})


# ---------------------------------------------------------------------------
# fs.* answers about the state dir the way the viewer does -- both directions
# ---------------------------------------------------------------------------


def _fs_loop(root):
    from types import SimpleNamespace

    return SimpleNamespace(workspace=str(root))


@pytest.mark.asyncio
async def test_fs_refuses_the_state_dir_when_the_root_sits_above_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under the launch-directory policy the root is the cwd, so started from a
    home directory the state dir is a *child* of it -- and `serve.json` holds a
    token that mints unlimited nonces, so it outlives the cookie it is not
    supposed to be worth."""
    from raven.rpc.errors import ConfigValidationError
    from raven.rpc.methods import console as console_module

    monkeypatch.setenv("HOME", str(tmp_path))
    (tmp_path / ".raven").mkdir()
    (tmp_path / ".raven" / "serve.json").write_text('{"token": "SECRET"}')
    (tmp_path / "notes.md").write_text("ordinary project file")
    loop = _fs_loop(tmp_path)

    with pytest.raises(ConfigValidationError):
        await console_module.fs_read({"path": ".raven/serve.json"}, agent_loop_factory=lambda: loop)

    ok = await console_module.fs_read({"path": "notes.md"}, agent_loop_factory=lambda: loop)
    assert ok["content"] == "ordinary project file"

    listed = await console_module.fs_list({"path": ""}, agent_loop_factory=lambda: loop)
    assert [e["name"] for e in listed["entries"]] == ["notes.md"], "the state dir must not be offered"


@pytest.mark.asyncio
async def test_fs_still_serves_the_workspace_when_it_is_inside_the_state_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The default workspace lives AT `~/.raven/workspace`, so a fence without
    the viewer's carve-out refuses every file the agent itself wrote -- the same
    asymmetry as the leak above, mirrored: the viewer renders a file the panel
    will not list."""
    from raven.rpc.methods import console as console_module

    monkeypatch.setenv("HOME", str(tmp_path))
    ws = tmp_path / ".raven" / "workspace"
    ws.mkdir(parents=True)
    (ws / "report.md").write_text("the agent wrote this")
    loop = _fs_loop(ws)

    listed = await console_module.fs_list({"path": ""}, agent_loop_factory=lambda: loop)
    assert [e["name"] for e in listed["entries"]] == ["report.md"]

    got = await console_module.fs_read({"path": "report.md"}, agent_loop_factory=lambda: loop)
    assert got["content"] == "the agent wrote this"


# ---------------------------------------------------------------------------
# deliverables.list: what a conversation handed over, after nobody was watching
# ---------------------------------------------------------------------------


def _file(tmp_path: Path, name: str) -> Path:
    """A real file for the registry to point at: `missing` is stat'd, so a
    fixture that never touched the disk would report every row as gone."""
    target = tmp_path / name
    target.write_text(name)
    return target


async def test_deliverables_list_answers_for_the_conversation_not_the_transcript(tmp_path: Path) -> None:
    """The registry is the answer after a reconnect or a compaction: a client
    reads the manifests off turn events while it is connected, and this is what
    is left when it was not."""
    from raven.agent.tools.deliverables import DeliverableStore

    store = DeliverableStore(tmp_path / "deliverables.json")
    mine = _file(tmp_path, "brief.md")
    theirs = _file(tmp_path, "other.md")
    store.register(
        path=str(mine),
        name="brief.md",
        media_type="text/markdown",
        size=12,
        conversation="tui:s1",
        title="The brief",
        description="what it is",
    )
    store.register(
        path=str(theirs),
        name="other.md",
        media_type="text/markdown",
        size=9,
        conversation="tui:s2",
    )
    loop = SimpleNamespace(deliverables=store)

    result = await console_module.deliverables_list({"session_key": "tui:s1"}, agent_loop_factory=lambda: loop)

    assert [f["name"] for f in result["files"]] == ["brief.md"], "another conversation's file is not this one's"
    file = result["files"][0]
    assert file["title"] == "The brief"
    assert file["description"] == "what it is"
    assert file["missing"] is False
    # A token URL, never a path: the download route resolves the token and the
    # path never travels in a URL.
    assert file["download_path"].startswith("/files/download?token=")
    assert str(mine) not in file["download_path"]
    assert file["created_at"]


async def test_deliverables_list_says_a_file_is_gone_rather_than_dropping_it(tmp_path: Path) -> None:
    """It is still something this conversation handed over. Saying so is more
    use than a row that quietly disappears."""
    from raven.agent.tools.deliverables import DeliverableStore

    store = DeliverableStore(tmp_path / "deliverables.json")
    target = _file(tmp_path, "gone.md")
    store.register(
        path=str(target),
        name="gone.md",
        media_type="text/markdown",
        size=4,
        conversation="tui:s1",
    )
    target.unlink()
    loop = SimpleNamespace(deliverables=store)

    result = await console_module.deliverables_list({"session_key": "tui:s1"}, agent_loop_factory=lambda: loop)

    assert [f["missing"] for f in result["files"]] == [True]


async def test_deliverables_list_is_empty_without_a_key_or_a_store() -> None:
    """A draft has no conversation to ask about, and a runtime built without the
    web channel has no registry -- neither is an error."""
    loop = SimpleNamespace(deliverables=None)

    assert (await console_module.deliverables_list({"session_key": ""}, agent_loop_factory=lambda: loop))["files"] == []
    assert (await console_module.deliverables_list({"session_key": "tui:s1"}))["files"] == []


# ---------------------------------------------------------------------------
# settings.set keeps the shell env mirror in step
# ---------------------------------------------------------------------------


@pytest.fixture
def settings_cfg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A config this suite may write, with ``~`` pointed somewhere disposable.

    ``settings.set`` now refreshes ``~/.raven/env`` for the two web keys, so an
    unisolated home would have this rewrite the developer's real one.
    """
    path = tmp_path / "config.json"
    path.write_text("{}", encoding="utf-8")
    # Two bindings, not one: ``_write_raw_key`` imports ``get_config_path``
    # inside the call, while ``update_tools`` bound it at import -- and the
    # mirror reads the keys back through the latter.
    monkeypatch.setattr("raven.config.loader.get_config_path", lambda: path)
    monkeypatch.setattr(update_tools, "get_config_path", lambda: path)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    return path


@pytest.mark.parametrize(
    ("key", "env_var"),
    [("tools.web.search.apiKey", "SERPER_API_KEY"), ("tools.web.jinaApiKey", "JINA_API_KEY")],
)
async def test_settings_set_refreshes_the_shell_env_mirror(
    settings_cfg: Path, tmp_path: Path, key: str, env_var: str
) -> None:
    """Both keys, because the settings page can write either one.

    Parametrised rather than asserted on one: the jina half has no other write
    path at all outside the wizard, so a mirror wired for Serper alone would
    look right and leave that key permanently stale.
    """
    await console_module.settings_set({"key": key, "value": "rotated-1"})

    mirror = tmp_path / ".raven" / "env"
    assert f"export {env_var}=rotated-1" in mirror.read_text(encoding="utf-8")

    await console_module.settings_set({"key": key, "value": "rotated-2"})
    body = mirror.read_text(encoding="utf-8")
    assert f"export {env_var}=rotated-2" in body
    assert "rotated-1" not in body


async def test_settings_set_of_an_unrelated_key_does_not_create_the_mirror(settings_cfg: Path, tmp_path: Path) -> None:
    await console_module.settings_set({"key": "tools.exec.timeout", "value": 42})

    assert not (tmp_path / ".raven" / "env").exists()


async def test_settings_set_writes_the_config_in_the_shared_formatting(settings_cfg: Path) -> None:
    """The bytes every ``update_*`` writer lays down: two-space indent, raw
    UTF-8 rather than ASCII escapes, no trailing newline. A save that drifted
    from this would rewrite every line of the file on the next change."""
    await console_module.settings_set({"key": "plugins.disabled", "value": ["caf\u00e9-plugin"]})

    expected = '{\n  "plugins": {\n    "disabled": [\n      "caf\u00e9-plugin"\n    ]\n  }\n}'
    assert settings_cfg.read_bytes() == expected.encode("utf-8")


@pytest.mark.asyncio
async def test_ext_list_carries_the_manager_state_and_the_authorization_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pull has to answer for a connect that started before any client.

    `mcp.status` and `oauth.pending` carry the state and the URL, and
    `WsGateway.broadcast` drops both while its socket set is empty -- which is
    every connect started at assembly time, since the page attaches after. Built
    from the registry alone, this reported a server parked at the browser step as
    `disconnected` with no URL, and the page draws its authorization action off
    exactly those two fields.
    """

    def _server(url: str) -> SimpleNamespace:
        return SimpleNamespace(enabled=True, type=None, command=None, url=url)

    cfg = SimpleNamespace(
        tools=SimpleNamespace(
            mcp_servers={
                "parked": _server("https://example.invalid/mcp"),
                # No record with the manager at all: still listed from config.
                "unknown": _server("https://example.invalid/mcp"),
            }
        )
    )
    from raven.config import loader as config_loader
    from raven.config import raven as raven_config

    monkeypatch.setattr(config_loader, "load_config", lambda: cfg)
    monkeypatch.setattr(
        raven_config,
        "load_raven_config",
        lambda: SimpleNamespace(skill_forge=None, plugins=SimpleNamespace(disabled=[])),
    )
    monkeypatch.setattr(console_module, "_hub_marker_name", lambda: None)

    from raven.mcp import oauth as oauth_module

    monkeypatch.setattr(
        oauth_module,
        "pending_url",
        lambda name: "https://auth.invalid/authorize?state=abc" if name == "parked" else None,
    )

    class _Catalog:
        def gather_all_skills(self):
            return []

    class _Manager:
        def status(self):
            return [
                {
                    "name": "parked",
                    "transport": "streamableHttp",
                    "state": "auth_required",
                    "connected": False,
                    "tool_count": 0,
                    "error": "waiting for browser authorization",
                }
            ]

    from raven.agent.tools.registry import ToolRegistry

    loop = SimpleNamespace(
        context=SimpleNamespace(skills=_Catalog()),
        tools=ToolRegistry(),
        mcp_manager_if_started=_Manager(),
    )

    result = await console_module.ext_list({}, agent_loop_factory=lambda: loop)
    by_name = {m["name"]: m for m in result["mcp"]}

    assert by_name["parked"]["state"] == "auth_required"
    assert by_name["parked"]["auth_url"] == "https://auth.invalid/authorize?state=abc"
    assert by_name["parked"]["error"] == "waiting for browser authorization"
    # Config still decides enabled: the manager's record predates a toggle.
    assert by_name["parked"]["enabled"] is True

    # A configured server the manager has never seen keeps the old reading.
    assert by_name["unknown"]["state"] == "disconnected"
    assert by_name["unknown"]["auth_url"] is None


async def test_a_language_switch_reaches_the_running_process(tmp_path, monkeypatch) -> None:
    """Writing the file is half of it: `t()` answers from a module-level
    language that only the CLI seeds, so a console switch that stopped at the
    file left every later reply in the old language until a restart."""
    from raven import i18n
    from raven.config import update as config_update

    monkeypatch.setattr(config_update, "set_language", lambda value: "en")
    monkeypatch.setattr(i18n, "_language", "en")

    result = await console_module.settings_set({"key": "language", "value": "zh"})

    assert result["applied"] is True
    assert i18n.current_language() == "zh"


async def test_a_rejected_language_changes_nothing(tmp_path, monkeypatch) -> None:
    from raven import i18n
    from raven.config import update as config_update
    from raven.rpc.errors import ConfigValidationError as RpcConfigValidationError

    monkeypatch.setattr(config_update, "set_language", lambda value: "en")
    monkeypatch.setattr(i18n, "_language", "en")

    with pytest.raises(RpcConfigValidationError):
        await console_module.settings_set({"key": "language", "value": "de"})

    assert i18n.current_language() == "en"


# ---------------------------------------------------------------------------
# ext.list reports availability, not membership
# ---------------------------------------------------------------------------


def _console_loop(workspace: Path, monkeypatch: pytest.MonkeyPatch, raw: dict):
    """A real on-disk config, and a loop assembled from it the production way.

    Both halves matter. A hand-built stand-in is what let this defect hide: an
    object carrying ``tool_names`` and nothing else answers every availability
    question by omission, which is the one thing under test. And the surface
    explains an unavailable tool by reading the config file, so the file has to
    be the same one the loop was built from or the two answer about different
    deployments.
    """
    import json

    from raven.agent.loop import AgentLoop
    from raven.config.loader import load_config
    from tests._wiring import wire
    from tests.test_tool_capabilities import _StubProvider

    for var in ("SERPER_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    cfg_path = workspace / "config.json"
    cfg_path.write_text(json.dumps(raw), encoding="utf-8")
    monkeypatch.setattr("raven.home._current_config_path", cfg_path)

    config = load_config(cfg_path)
    kw = {}
    if config.tools.web.search.api_key:
        kw["brave_api_key"] = config.tools.web.search.api_key
    loop = AgentLoop(
        provider=_StubProvider(),
        workspace=workspace,
        model="stub",
        **wire(media_config=config.effective_media_config(), **kw),
    )
    # No withheld source installed here on purpose: AgentLoop installs its own,
    # which reads the live config -- the file above -- and unions the off switch
    # with the unconfigured names. Overriding it with a disabled-only lambda was
    # enough to make the credential-gate cases pass for the wrong reason.
    return loop


async def _ext_rows(loop, monkeypatch: pytest.MonkeyPatch) -> dict[str, dict]:
    from raven.config import raven as raven_config

    monkeypatch.setattr(
        raven_config,
        "load_raven_config",
        lambda: SimpleNamespace(skill_forge=None, plugins=SimpleNamespace(disabled=[])),
    )
    monkeypatch.setattr(console_module, "_hub_marker_name", lambda: None)
    result = await console_module.ext_list({}, agent_loop_factory=lambda: loop)
    return {t["name"]: t for t in result["tools"]}


_CRED_TOOLS = (
    ("web_search", "tools.web.search.apiKey", "SERPER_API_KEY"),
    ("image_generate", "tools.media.image.apiKey", "OPENROUTER_API_KEY"),
    ("text_to_speech", "tools.media.speech.apiKey", "OPENROUTER_API_KEY"),
    ("video_generate", "tools.media.video.apiKey", "OPENROUTER_API_KEY"),
)


@pytest.mark.parametrize(("tool", "setting", "env"), _CRED_TOOLS)
async def test_an_unconfigured_tool_is_reported_unavailable_with_what_it_needs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str, setting: str, env: str
) -> None:
    """A credential-less tool is registered and withheld rather than left out,
    so membership stopped meaning the model can call it. Reporting ``enabled``
    off membership told a deployer the capability was on while the model was
    never offered it -- and said nothing about what to set.

    The setting names the *key* field: for the media family the config path is
    the model, and pointing there sends the reader to a line holding no key.
    """
    loop = _console_loop(tmp_path, monkeypatch, {})

    assert loop.tools.has(tool), "this case is about a REGISTERED tool the model is not offered"
    assert not loop.tools.offers_by_name(tool)

    row = (await _ext_rows(loop, monkeypatch))[tool]
    assert row["enabled"] is False
    assert row["needs"] == {"setting": setting, "env": env}


async def test_a_configured_tool_is_reported_available_with_nothing_owed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other direction, so the fix cannot pass by reporting everything off."""
    loop = _console_loop(
        tmp_path,
        monkeypatch,
        # The media tool's own section, not a chat OpenRouter key: a key
        # configured for chat deliberately does not enable a billed media tool.
        {"tools": {"web": {"search": {"apiKey": "sk-serper"}}, "media": {"image": {"apiKey": "sk-img"}}}},
    )

    rows = await _ext_rows(loop, monkeypatch)
    for tool in ("web_search", "image_generate"):
        assert loop.tools.offers_by_name(tool), tool
        assert rows[tool]["enabled"] is True, tool
        assert rows[tool]["needs"] is None, tool


@pytest.mark.parametrize("tool", ["web_search", "text_to_speech"])
async def test_a_switched_off_tool_is_not_reported_as_missing_a_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, tool: str
) -> None:
    """Unavailable has two unrelated causes and the surface must not merge them.

    A switched-off tool usually has its key set, so answering the operator's
    own off switch with "set this credential" points at a line already filled
    in. Only the credential gate earns a ``needs``; the switch is reported by
    ``enabled`` alone, which is what keeps the row switchable.
    """
    loop = _console_loop(
        tmp_path,
        monkeypatch,
        {
            "tools": {
                "web": {"search": {"apiKey": "sk-serper"}},
                "media": {"speech": {"apiKey": "sk-tts"}},
                "disabledTools": ["web_search", "text_to_speech"],
            }
        },
    )

    assert loop.tools.has(tool)
    assert not loop.tools.offers_by_name(tool), "the off switch has to bite for this case to mean anything"

    row = (await _ext_rows(loop, monkeypatch))[tool]
    assert row["enabled"] is False
    assert row["needs"] is None, "a configured tool the operator switched off owes no credential"


async def test_a_tool_needing_no_credential_stays_reported_available(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A plain tool owes nothing, so it must not grow a needs row from the roster."""
    loop = _console_loop(tmp_path, monkeypatch, {})

    row = (await _ext_rows(loop, monkeypatch))["read_file"]
    assert row["enabled"] is True
    assert row["needs"] is None
