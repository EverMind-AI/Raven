"""Plugins cast gates over the tool registry: manifest, registry, stack, throat.

The fifth contribution kind (paper: contracts/tool_gate.py). Pinned here: the
manifest entry (a gate without a usable name is refused at the door), registry
activation with cross-plugin conflict detection, the lenient stack builder with
its identity stamp, and the throat semantics -- gates are cast at construction
in (name, contributed_by) order, a verdict replaces exactly one validated call
with no retry hint appended, a raising gate refuses its call (fail-closed), and
a gate's bind failure fails the whole assembly instead of quietly unregistering
(dropping a gate is failing open). The absence pin -- a registry built with no
gates is byte-identical to today's -- lives with the execute-boundary tests
(test_tool_registry_execute.py).
"""

from __future__ import annotations

import sys
import textwrap
import types
from pathlib import Path

import pytest
from pydantic import ValidationError

from raven.agent import workdir
from raven.agent.loop import AgentLoop
from raven.agent.loop.bundles import HostWiring, ToolWiring, TurnPolicy
from raven.agent.tools.registry import ToolRegistry
from raven.contracts.llm_provider import LLMResponse
from raven.contracts.tool import Tool
from raven.contracts.tool_gate import ToolGate
from raven.plugins import (
    Contributes,
    DiscoveredPlugin,
    ManifestOrigin,
    PluginConflictError,
    PluginManifest,
    PluginRegistry,
    ToolGateContribution,
)
from raven.providers.base import LLMProvider

_INJECTED: set[str] = set()


def _install_module(name: str, attrs: dict[str, object]) -> None:
    mod = types.ModuleType(name)
    for k, v in attrs.items():
        setattr(mod, k, v)
    sys.modules[name] = mod
    _INJECTED.add(name)


@pytest.fixture(autouse=True)
def _cleanup_modules():
    yield
    for name in _INJECTED:
        sys.modules.pop(name, None)
    _INJECTED.clear()


def _discovered(plugin_id: str, gates: list[tuple[str, str]]) -> DiscoveredPlugin:
    mf = PluginManifest(
        id=plugin_id,
        version="0.1.0",
        enabled_by_default=True,
        contributes=Contributes(tool_gates=[ToolGateContribution(name=n, factory=f) for n, f in gates]),
    )
    return DiscoveredPlugin(manifest=mf, source=ManifestOrigin.USER, location=None)


class _Gate:
    """A paper-shaped gate: named, awaitable adjudicate, optional fixed verdict."""

    def __init__(
        self,
        name: str,
        *,
        contributed_by: str | None = None,
        verdict: str | None = None,
        log: list | None = None,
    ) -> None:
        self.name = name
        if contributed_by is not None:
            self.contributed_by = contributed_by
        self._verdict = verdict
        self.log = log if log is not None else []

    async def adjudicate(self, name, params, *, session_workdir):
        self.log.append((self.name, name, session_workdir))
        return self._verdict


class _Probe(Tool):
    """A registered tool that records whether it ran."""

    def __init__(self, name: str = "gate_probe") -> None:
        self._name = name
        self.ran = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def description(self) -> str:
        return "records dispatch"

    @property
    def parameters(self) -> dict:
        return {"type": "object", "properties": {}, "required": []}

    async def execute(self, **kwargs) -> str:
        self.ran += 1
        return "probe ran"


class TestManifestToolGates:
    def test_parses_a_tool_gates_contribution(self) -> None:
        mf = PluginManifest.from_toml_str(
            textwrap.dedent(
                """
                [plugin]
                id = "code-flow"
                version = "0.1.0"
                [[plugin.contributes.tool_gates]]
                name = "write_gate"
                factory = "code_flow.gate:make_write_gate"
                """
            )
        )
        assert [g.name for g in mf.contributes.tool_gates] == ["write_gate"]

    def test_default_tool_gates_empty(self) -> None:
        assert PluginManifest(id="p", version="0.1.0").contributes.tool_gates == []

    def test_bad_factory_ref_rejected(self) -> None:
        with pytest.raises(ValidationError):
            ToolGateContribution(name="g", factory="not-a-ref")

    def test_a_gate_without_a_usable_name_is_a_manifest_error(self) -> None:
        """The owner ruling: no runtime name fallback -- the door refuses."""
        with pytest.raises(ValidationError, match="name"):
            PluginManifest.from_toml_str(
                textwrap.dedent(
                    """
                    [plugin]
                    id = "p"
                    version = "0.1.0"
                    [[plugin.contributes.tool_gates]]
                    factory = "m:make"
                    """
                )
            )
        with pytest.raises(ValidationError, match="name"):
            ToolGateContribution(name="", factory="m:make")

    def test_duplicate_tool_gate_names_rejected(self) -> None:
        with pytest.raises(ValidationError, match="duplicate tool_gate name"):
            PluginManifest(
                id="p",
                version="0.1.0",
                contributes=Contributes(
                    tool_gates=[
                        ToolGateContribution(name="dup", factory="m:a"),
                        ToolGateContribution(name="dup", factory="m:b"),
                    ]
                ),
            )


class TestRegistryToolGates:
    def test_activates_and_builds(self) -> None:
        seen = {}

        def make(ctx):
            seen["config"] = ctx.config
            return "gate-instance"

        _install_module("_pg_a", {"make": make})
        reg = PluginRegistry()
        reg.activate([_discovered("alpha", [("write_gate", "_pg_a:make")])])
        assert reg.tool_gate_names() == ["write_gate"]
        assert reg.tool_gate_plugin_id("write_gate") == "alpha"
        from raven.plugins import ServiceLocator

        built = reg.build_tool_gate(
            "write_gate",
            config={"mode": "strict"},
            services=ServiceLocator(workspace=Path("/w"), user_id="u", agent_id="a"),
        )
        assert built == "gate-instance"
        assert seen["config"] == {"mode": "strict"}

    def test_cross_plugin_name_conflict_raises(self) -> None:
        _install_module("_pg_b", {"make": lambda ctx: None})
        reg = PluginRegistry()
        with pytest.raises(PluginConflictError, match="tool_gate 'same'"):
            reg.activate([_discovered("one", [("same", "_pg_b:make")]), _discovered("two", [("same", "_pg_b:make")])])


class TestBuildPluginToolGates:
    def _config(self, plugin_config: dict | None = None):
        from raven.config.raven import PluginsConfig, RavenConfig

        return RavenConfig(plugins=PluginsConfig(config=dict(plugin_config or {})))

    def test_builds_gates_with_the_plugins_slice_and_stamps_identity(self, tmp_path: Path) -> None:
        from raven.core.plugin_stack import build_plugin_tool_gates

        def make(ctx):
            return _Gate(f"gate::{ctx.config.get('flag')}")

        _install_module("_pg_c", {"make": make})
        reg = PluginRegistry()
        reg.activate([_discovered("myplugin", [("g1", "_pg_c:make")])])
        built = build_plugin_tool_gates(tmp_path, self._config({"myplugin": {"flag": "on"}}), registry=reg)
        assert [g.name for g in built] == ["gate::on"]
        assert built[0].contributed_by == "myplugin"

    def test_failing_and_declining_factories_are_skipped(self, tmp_path: Path) -> None:
        """Declining (a factory returning None) is the sanctioned opt-out:
        no policy configured means no gate at all."""
        from raven.core.plugin_stack import build_plugin_tool_gates

        def boom(ctx):
            raise RuntimeError("nope")

        _install_module("_pg_d", {"boom": boom, "decline": lambda ctx: None})
        reg = PluginRegistry()
        reg.activate([_discovered("p", [("a", "_pg_d:boom"), ("b", "_pg_d:decline")])])
        assert build_plugin_tool_gates(tmp_path, self._config(), registry=reg) == []

    @pytest.mark.parametrize("fake", [object(), "gate-as-str"])
    def test_a_gate_that_cannot_carry_the_stamp_still_boards(self, tmp_path: Path, fake) -> None:
        from raven.core.plugin_stack import build_plugin_tool_gates

        def make(ctx):
            return fake

        _install_module("_pg_e", {"make": make})
        reg = PluginRegistry()
        reg.activate([_discovered("p", [("g", "_pg_e:make")])])
        built = build_plugin_tool_gates(tmp_path, self._config(), registry=reg)
        assert built == [fake]
        assert not hasattr(built[0], "contributed_by")


class TestRegistryAdjudication:
    def test_gates_are_ordered_by_name_then_contributor(self) -> None:
        b2 = _Gate("b", contributed_by="ymir")
        b1 = _Gate("b", contributed_by="xene")
        a = _Gate("a", contributed_by="zeta")
        reg = ToolRegistry(tool_gates=[b2, b1, a])
        assert [(g.name, g.contributed_by) for g in reg.tool_gates] == [
            ("a", "zeta"),
            ("b", "xene"),
            ("b", "ymir"),
        ]

    @pytest.mark.asyncio
    async def test_a_verdict_replaces_one_call_and_other_calls_run(self) -> None:
        class _Selective(_Gate):
            async def adjudicate(self, name, params, *, session_workdir):
                self.log.append((self.name, name, session_workdir))
                return "the gate's answer" if name == "gate_probe" else None

        probe, free = _Probe(), _Probe("free_probe")
        reg = ToolRegistry(tool_gates=[_Selective("selective")])
        reg.register(probe)
        reg.register(free)

        gated = await reg.execute("gate_probe", {})
        assert gated == "the gate's answer"
        assert probe.ran == 0, "a verdict means the call never runs"

        passed = await reg.execute("free_probe", {})
        assert passed == "probe ran"
        assert free.ran == 1, "the batch-mate is untouched"

    @pytest.mark.asyncio
    async def test_none_waves_the_call_through_to_the_tool(self) -> None:
        gate = _Gate("waves", verdict=None)
        probe = _Probe()
        reg = ToolRegistry(tool_gates=[gate])
        reg.register(probe)
        assert await reg.execute("gate_probe", {}) == "probe ran"
        assert probe.ran == 1
        assert [entry[1] for entry in gate.log] == ["gate_probe"]

    @pytest.mark.asyncio
    async def test_the_first_non_none_verdict_wins_in_name_order(self) -> None:
        log: list = []
        first = _Gate("a_first", verdict="a's verdict", log=log)
        second = _Gate("b_second", verdict="b's verdict", log=log)
        reg = ToolRegistry(tool_gates=[second, first])
        reg.register(_Probe())
        assert await reg.execute("gate_probe", {}) == "a's verdict"
        assert [entry[0] for entry in log] == ["a_first"], "the later gate is never consulted"

    @pytest.mark.asyncio
    async def test_a_verdict_is_returned_verbatim_with_no_retry_hint(self) -> None:
        reg = ToolRegistry(tool_gates=[_Gate("g", verdict="Error: this call needs an approval first")])
        reg.register(_Probe())
        result = await reg.execute("gate_probe", {})
        assert result == "Error: this call needs an approval first"
        assert "try a different approach" not in result

    @pytest.mark.asyncio
    async def test_a_raising_gate_refuses_the_call_fail_closed(self) -> None:
        class _Broken(_Gate):
            async def adjudicate(self, name, params, *, session_workdir):
                raise RuntimeError("policy store unreachable")

        probe = _Probe()
        reg = ToolRegistry(tool_gates=[_Broken("guard")])
        reg.register(probe)
        result = await reg.execute("gate_probe", {})
        assert result == "Error: tool call 'gate_probe' was refused by gate guard: policy store unreachable"
        assert probe.ran == 0, "failing open would make a gate's bugs silent permission grants"

    @pytest.mark.asyncio
    async def test_validation_still_precedes_adjudication(self) -> None:
        class _Strict(_Probe):
            @property
            def parameters(self) -> dict:
                return {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}

        gate = _Gate("g", verdict="never reached")
        reg = ToolRegistry(tool_gates=[gate])
        reg.register(_Strict())
        result = await reg.execute("gate_probe", {})
        assert "Invalid parameters" in result
        assert gate.log == [], "the paper says post-validation, pre-dispatch"

    @pytest.mark.asyncio
    async def test_the_bound_workdir_is_passed_explicitly(self, tmp_path: Path) -> None:
        gate = _Gate("g", verdict=None)
        reg = ToolRegistry(tool_gates=[gate])
        reg.register(_Probe())
        with workdir.bind(tmp_path):
            await reg.execute("gate_probe", {})
        await reg.execute("gate_probe", {})
        assert [entry[2] for entry in gate.log] == [tmp_path, None]


class _Provider(LLMProvider):
    def __init__(self) -> None:
        super().__init__(api_key="test")

    async def chat(
        self,
        messages,
        tools=None,
        model=None,
        max_tokens=4096,
        temperature=0.7,
        reasoning_effort=None,
        tool_choice=None,
    ):
        return LLMResponse(content="ok", finish_reason="stop")

    def get_default_model(self) -> str:
        return "fake/default"


def _loop(tmp_path: Path, gates: list | None = None) -> AgentLoop:
    (tmp_path / "home").mkdir(exist_ok=True)
    return AgentLoop(
        provider=_Provider(),
        workspace=tmp_path / "home",
        model="fake/model",
        policy=TurnPolicy(max_iterations=2),
        host=HostWiring(),
        tools=ToolWiring(restrict_to_workspace=True, plugin_tool_gates=gates),
    )


class TestLoopCastsGates:
    def test_cast_at_construction_in_order_and_never_after(self, tmp_path: Path) -> None:
        loop = _loop(tmp_path, gates=[_Gate("b"), _Gate("a")])
        assert [g.name for g in loop.tools.tool_gates] == ["a", "b"]
        with pytest.raises(AttributeError):
            loop.tools.tool_gates = ()  # type: ignore[misc]

    def test_a_binding_gate_receives_the_loops_own_handles(self, tmp_path: Path) -> None:
        class _Binding(_Gate):
            bound: list = []

            def bind_runtime(self, handles) -> None:
                _Binding.bound.append(handles)

        _Binding.bound = []
        gate = _Binding("g", contributed_by="plug")
        _loop(tmp_path, gates=[gate])
        assert len(_Binding.bound) == 1
        handles = _Binding.bound[0]
        assert callable(handles.direct_ask)
        assert callable(handles.rebind_workdir)
        assert handles.session_dir is not None

    @pytest.mark.parametrize("boom", [RuntimeError("half-bound"), None])
    def test_a_gate_that_fails_to_bind_fails_the_assembly(self, tmp_path: Path, boom) -> None:
        """Loud by design, declines included: quietly unregistering a gate
        (the tools' path) would fail open."""
        from raven.plugins.context import BindDeclinedError

        exc = boom if boom is not None else BindDeclinedError("no grant for me")

        class _Refusing(_Gate):
            def bind_runtime(self, handles) -> None:
                raise exc

        with pytest.raises(type(exc)):
            _loop(tmp_path, gates=[_Refusing("g")])

    @pytest.mark.asyncio
    async def test_direct_ask_answers_none_with_no_asking_transport(self, tmp_path: Path) -> None:
        loop = _loop(tmp_path)
        handles = loop.mint_runtime_handles("plug")
        assert await handles.direct_ask("proceed?", ["Yes", "No"], "conv-1") is None

    @pytest.mark.asyncio
    async def test_direct_ask_waits_its_turn_on_the_conversation_and_gives_up_at_the_deadline(
        self, tmp_path: Path
    ) -> None:
        """The grant is a third route to the broker's single pending slot, beside the
        confirm gate and the relayed ``Asker``. Like them it holds the conversation's
        question lock across the round trip -- ``ask_direct`` takes none itself -- so
        a gate asking from inside one node's tool call cannot evict the question a
        sibling node (or a relayed sub-agent) already has pending. A conversation
        still busy at the deadline answers None, the gate's no-channel path, and the
        broker never sees the question."""
        import asyncio

        from raven.acp_client.asker import question_lock

        class _Broker:
            default_timeout_s = 600.0

            def __init__(self) -> None:
                self.asked: list[str] = []

            async def await_question(self, cid: str, *, prompt: str, choices: list[str], **kwargs) -> str:
                self.asked.append(prompt)
                return "Yes"

        loop = _loop(tmp_path)
        broker = _Broker()
        loop.tools.get("ask_user").set_broker(broker)  # type: ignore[union-attr]
        handles = loop.mint_runtime_handles("plug")

        lock = question_lock("conv-1")
        await lock.acquire()
        try:
            assert await handles.direct_ask("proceed?", ["Yes", "No"], "conv-1", 0.1) is None
        finally:
            lock.release()
        assert broker.asked == []

        async def release_soon() -> None:
            await asyncio.sleep(0.02)
            lock.release()

        await lock.acquire()
        answer, _ = await asyncio.gather(handles.direct_ask("proceed?", ["Yes", "No"], "conv-1", 1.0), release_soon())
        assert answer == "Yes"
        assert broker.asked == ["proceed?"]

    def test_rebind_workdir_persists_repoints_and_reads_back(self, tmp_path: Path) -> None:
        from raven.agent.workdir import WorkdirPolicy, WorkdirResolver

        loop = _loop(tmp_path)
        target = tmp_path / "project"
        target.mkdir()
        handles = loop.mint_runtime_handles("plug")

        with workdir.bind(tmp_path / "old"):
            got = handles.rebind_workdir("web:abc", target)
            assert got == target.resolve()
            assert workdir.current() == target.resolve(), "the very next call resolves the new root"
        assert workdir.current() is None, "the turn-end reset still runs"

        assert loop.sessions.get_or_create("web:abc").metadata["workdir"] == str(target.resolve())
        resolver = WorkdirResolver(
            WorkdirPolicy.PER_CHANNEL,
            agent_home=tmp_path / "home",
            session_root=tmp_path / "chanwork",
            sessions=loop.sessions,
        )
        assert resolver.resolve("web:abc", create=False) == target.resolve()

    def test_rebind_workdir_keeps_the_validation_refusals(self, tmp_path: Path) -> None:
        loop = _loop(tmp_path)
        handles = loop.mint_runtime_handles("plug")
        with pytest.raises(ValueError, match="user_memory"):
            handles.rebind_workdir("web:abc", tmp_path / "home" / "user_memory" / "x")


DEMO_GATE = """
class DemoGate:
    name = "demo_gate"

    async def adjudicate(self, name, params, *, session_workdir):
        if name == "gate_probe":
            return "gated by demo"
        return None

def make(ctx):
    return DemoGate()
"""


@pytest.mark.asyncio
async def test_a_plugin_gate_is_cast_over_the_built_runtimes_registry(tmp_path: Path, monkeypatch) -> None:
    """End to end: a plugin directory's gate rides discovery -> activation ->
    build -> cast, satisfies the paper's face, and adjudicates a live call."""
    from raven.config.raven import RavenConfig
    from raven.config.schema import Config
    from raven.core import plugin_stack, runtime

    plug = tmp_path / "plugins" / "demogate"
    plug.mkdir(parents=True)
    plug.joinpath("raven-plugin.toml").write_text(
        '[plugin]\nid = "demogate"\nversion = "1.0"\nenabled_by_default = true\n'
        "[[plugin.contributes.tool_gates]]\n"
        'name = "demo_gate"\nfactory = "demogate_mod:make"\n'
    )
    plug.joinpath("demogate_mod.py").write_text(DEMO_GATE)
    _INJECTED.add("demogate_mod")
    monkeypatch.setattr(
        plugin_stack,
        "plugin_discovery_sources",
        lambda: {
            "bundled_dir": tmp_path / "none",
            "user_dir": tmp_path / "plugins",
            "project_dir": tmp_path / "none",
            "entry_points_group": None,
        },
    )
    monkeypatch.setattr(runtime.token_wise_stack, "install_from_config", lambda *a, **k: None)
    monkeypatch.setattr(runtime.token_wise_stack, "caching_probe", lambda *a, **k: False)

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "ws")
    rt = runtime.build_runtime(config, RavenConfig(), provider=_Provider())
    try:
        gates = rt.loop.tools.tool_gates
        assert [g.name for g in gates] == ["demo_gate"]
        assert gates[0].contributed_by == "demogate"
        assert isinstance(gates[0], ToolGate)
        rt.loop.tools.register(_Probe())
        assert await rt.loop.tools.execute("gate_probe", {}) == "gated by demo"
    finally:
        rt.discard()
