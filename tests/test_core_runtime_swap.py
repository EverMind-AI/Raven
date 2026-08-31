"""The assembly door swaps a part without the loop knowing.

``build_runtime`` derives the memory backend from ``memory.backend`` through
the plugin registry; a backend that ships as a plugin in a user directory
boards the loop with no code in the inner layers changed, and a turn reaches
it through the same seams the bundled one uses. This is the swap the
assembly container promises, exercised end to end on a fake provider.

The other organs board by instance: the provider, session manager, router
and provider pool handed to ``build_runtime`` are the objects the loop runs
on, identity-checked here and, where a turn can show it, reached by one.
The token_wise organ has no instance socket and is pinned as what it is, a
config-driven one; the two organs with no socket at all are pinned as built
by the shell, so the gap is a visible fact rather than a silent one.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.providers.base import LLMProvider, LLMResponse
from raven.spine import ChatType, Origin, Source, TurnRequest

DEMO_BACKEND = """
class DemoBackend:
    calls: list[str] = []

    def __init__(self, ctx):
        self.ctx = ctx

    async def recall(self, query, *, user_id=None, agent_id=None, top_k=5):
        DemoBackend.calls.append("recall")
        return []

    async def store(self, session_id, messages, *, metadata=None):
        DemoBackend.calls.append("store")

    async def feedback(self, signals):
        DemoBackend.calls.append("feedback")

    async def start(self):
        DemoBackend.calls.append("start")

    async def stop(self):
        DemoBackend.calls.append("stop")


def make(ctx):
    return DemoBackend(ctx)
"""


class _Provider(LLMProvider):
    """One fixed reply; the retry and classification machinery comes from the base."""

    def __init__(self) -> None:
        super().__init__(api_key="test")
        self.chats: list[list[dict]] = []

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
        self.chats.append(messages)
        return LLMResponse(content="ok", finish_reason="stop")

    def get_default_model(self) -> str:
        return "fake/default"


def _install_demo_plugin(root: Path) -> Path:
    plug = root / "plugins" / "swapdemo"
    plug.mkdir(parents=True)
    plug.joinpath("raven-plugin.toml").write_text(
        '[plugin]\nid = "swapdemo"\nversion = "1.0"\nenabled_by_default = true\n'
        "[[plugin.contributes.memory_backends]]\n"
        'name = "swapdemo"\nfactory = "swapdemo_backend:make"\n'
    )
    plug.joinpath("swapdemo_backend.py").write_text(DEMO_BACKEND)
    return root / "plugins"


@pytest.mark.asyncio
async def test_a_plugin_backend_boards_through_the_door_and_sees_the_turn(tmp_path: Path, monkeypatch) -> None:
    from raven.config.raven import RavenConfig
    from raven.config.schema import Config
    from raven.core import plugin_stack, runtime

    user_dir = _install_demo_plugin(tmp_path)
    monkeypatch.setattr(
        plugin_stack,
        "plugin_discovery_sources",
        lambda: {
            "bundled_dir": tmp_path / "none",
            "user_dir": user_dir,
            "project_dir": tmp_path / "none",
            "entry_points_group": None,
        },
    )
    monkeypatch.setattr(runtime.token_wise_stack, "install_from_config", lambda *a, **k: None)
    monkeypatch.setattr(runtime.token_wise_stack, "caching_probe", lambda *a, **k: False)

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "ws")
    ec_config = RavenConfig(memory={"backend": "swapdemo"})

    rt = runtime.build_runtime(config, ec_config, provider=_Provider())

    assert type(rt.backend).__name__ == "DemoBackend"
    assert rt.loop.backend is rt.backend
    calls = type(rt.backend).calls
    calls.clear()

    await rt.loop._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
            text="remember that the sky is blue",
        ),
        session_key="s1",
    )
    drain = getattr(rt.loop, "drain_backend_stores", None)
    if drain is not None:
        await drain()

    assert "recall" in calls, calls
    assert "store" in calls, calls


def _quiet_plugins(tmp_path: Path, monkeypatch) -> None:
    """No plugin boards: every discovery source points at an empty directory."""
    from raven.core import plugin_stack

    monkeypatch.setattr(
        plugin_stack,
        "plugin_discovery_sources",
        lambda: {
            "bundled_dir": tmp_path / "none",
            "user_dir": tmp_path / "none",
            "project_dir": tmp_path / "none",
            "entry_points_group": None,
        },
    )


def _configs(tmp_path: Path, **ec_fields):
    from raven.config.raven import RavenConfig
    from raven.config.schema import Config

    config = Config()
    config.agents.defaults.workspace = str(tmp_path / "ws")
    return config, RavenConfig(**ec_fields)


def _turn(text: str) -> TurnRequest:
    return TurnRequest(
        origin=Origin.USER,
        source=Source(channel="test", chat_id="c1", sender_id="user", chat_type=ChatType.DM),
        text=text,
    )


class _Router:
    """Answers every prompt with no pick, which is what a real router says when
    its data is missing; the loop then stays on the default model."""

    def __init__(self) -> None:
        self.asked: list[str] = []

    async def select_model_chain(self, prompt: str) -> tuple[str | None, list[str]]:
        self.asked.append(prompt)
        return None, []


@pytest.mark.asyncio
async def test_the_provider_socket_binds_the_object_the_door_was_handed(tmp_path: Path, monkeypatch) -> None:
    """``provider=`` is an identity socket: the loop's default binding holds the
    very object, and a turn's chat call lands on it carrying the turn's text."""
    from raven.core import runtime

    _quiet_plugins(tmp_path, monkeypatch)
    config, ec_config = _configs(tmp_path)
    provider = _Provider()

    rt = runtime.build_runtime(config, ec_config, provider=provider)

    assert rt.loop.provider is provider
    assert rt.loop.default_binding.provider is provider

    await rt.loop._process_message(_turn("say ok"), session_key="test:c1")

    assert provider.chats, "the turn's chat call never reached the substitute"
    assert "say ok" in str(provider.chats[0])


@pytest.mark.asyncio
async def test_the_session_socket_is_where_the_turn_is_written(tmp_path: Path, monkeypatch) -> None:
    """``session_manager=`` is an identity socket: ``loop.sessions`` is the very
    object, and a turn's transcript lands under its root rather than under the
    workspace the loop would have opened a manager on by itself."""
    from raven.core import runtime
    from raven.session.manager import SessionManager

    _quiet_plugins(tmp_path, monkeypatch)
    config, ec_config = _configs(tmp_path)
    sessions = SessionManager(tmp_path / "elsewhere")

    rt = runtime.build_runtime(config, ec_config, provider=_Provider(), session_manager=sessions)

    assert rt.loop.sessions is sessions

    await rt.loop._process_message(_turn("hello"), session_key="test:c1")

    assert sessions.session_path("test:c1").exists()
    assert not (tmp_path / "ws" / "sessions").exists()


@pytest.mark.asyncio
async def test_the_routing_socket_is_asked_before_the_turn_is_assembled(tmp_path: Path, monkeypatch) -> None:
    """``router=`` is an identity socket: ``loop.router`` is the very object, and
    a turn on a session with no model of its own asks it for a chain once."""
    from raven.core import runtime

    _quiet_plugins(tmp_path, monkeypatch)
    config, ec_config = _configs(tmp_path)
    router = _Router()

    rt = runtime.build_runtime(config, ec_config, provider=_Provider(), router=router)

    assert rt.loop.router is router

    await rt.loop._process_message(_turn("route me"), session_key="test:c1")

    assert len(router.asked) == 1, router.asked
    assert "route me" in router.asked[0]


def test_the_provider_pool_socket_reaches_the_loop(tmp_path: Path, monkeypatch) -> None:
    """``provider_pool=`` is an identity socket: the pool a session's model switch
    resolves through is the one the door was handed, not the one the door
    derives over the config loader when the socket is left empty."""
    from raven.core import runtime
    from raven.providers.pool import ProviderPool

    _quiet_plugins(tmp_path, monkeypatch)
    config, ec_config = _configs(tmp_path)
    pool = ProviderPool(config)

    rt = runtime.build_runtime(config, ec_config, provider=_Provider(), provider_pool=pool)

    assert rt.loop.provider_pool is pool


def test_the_token_wise_socket_takes_the_strategy_the_config_names(tmp_path: Path, monkeypatch) -> None:
    """token_wise is substitutable by configuration, not by instance: the door has
    no ``strategies=`` parameter and ``token_wise_stack.install_from_config`` is
    the only builder, so what a caller can do is name the strategies. A config
    with cache optimisation on and usage tracking off yields a registry holding
    exactly one CacheOptimizer, and the loop runs on that registry, not a copy."""
    from raven.core import runtime
    from raven.token_wise.cache_optimizer import CacheOptimizer

    _quiet_plugins(tmp_path, monkeypatch)
    config, ec_config = _configs(
        tmp_path, token_wise={"enabled": True, "cache_optimization": True, "usage_tracking": False}
    )

    rt = runtime.build_runtime(config, ec_config, provider=_Provider())

    assert rt.loop.strategies is rt.strategies
    assert [type(s) for s in rt.loop.strategies.strategies] == [CacheOptimizer]


@pytest.mark.asyncio
async def test_the_context_engine_socket_binds_the_instance_the_door_was_handed(tmp_path: Path, monkeypatch) -> None:
    """The seventh organ pair closes: a built context engine rides the bundle
    through the door, and the turn's prompt is whatever it assembled -- the
    shell builds nothing of its own."""
    from raven.contracts.assembled import AssembledContext
    from raven.core import runtime

    class _StubEngine:
        owns_compaction = False

        def __init__(self) -> None:
            self.assembled_for: list[str] = []

        def set_provider(self, *a, **k) -> None:
            return None

        async def assemble(self, session_key, session_messages, budget, *, turn):
            self.assembled_for.append(session_key)
            return AssembledContext(messages=[{"role": "user", "content": "marker-from-the-stub-engine"}])

        async def after_turn(self, *a, **k) -> None:
            return None

    _quiet_plugins(tmp_path, monkeypatch)
    config, ec_config = _configs(tmp_path)
    stub = _StubEngine()
    provider = _Provider()

    rt = runtime.build_runtime(config, ec_config, provider=provider, context_engine=stub)

    assert rt.loop.context_engine is stub

    await rt.loop._process_message(_turn("does not matter: the stub decides"), session_key="test:c1")

    assert stub.assembled_for, "the turn never asked the substitute engine"
    assert "marker-from-the-stub-engine" in str(provider.chats[0])


@pytest.mark.asyncio
async def test_the_executor_socket_binds_the_instance_the_door_was_handed(tmp_path: Path, monkeypatch) -> None:
    """A built executor rides the door beside ``sandbox_config``, its config
    twin, and is the one the exec tool holds -- mount-root and home-volume
    derivation stay the builder's business."""
    from raven.core import runtime

    class _StubExecutor:
        is_sandboxed = False
        supports_process_spawning = True

    _quiet_plugins(tmp_path, monkeypatch)
    config, ec_config = _configs(tmp_path)
    stub = _StubExecutor()

    rt = runtime.build_runtime(config, ec_config, provider=_Provider(), executor=stub)

    assert rt.loop._executor is stub
    exec_tool = rt.loop.tools.get("exec")
    assert exec_tool is not None and exec_tool._executor is stub


def test_a_generation_is_sealed_after_construction():
    """FREEZE's machine: composition changes go through the swap path, never setattr."""
    from dataclasses import FrozenInstanceError

    from raven.core.runtime import RavenRuntime

    rt = RavenRuntime(loop=object(), plugin_registry=None, backend=None, strategies=None, deliverables=None)
    with pytest.raises(FrozenInstanceError):
        rt.loop = object()  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        rt.backend = object()  # type: ignore[misc]


REPO = Path(__file__).resolve().parent.parent

# The generation doors and the manager vocabulary around them; the law lives
# on the RavenRuntime paper (core/runtime.py). A spelled reach outside the
# organ homes must come from a declared operator.
_GENERATION_DOORS = {
    "apply_agents",
    "apply_mcp_config",
    "set_default_binding",
    "mcp_manager",
    "mcp_executor_provider",
}

# module path -> the doors it operates. Widening this roster is a ruling,
# not a convenience: the reason belongs in the same diff.
_DOOR_OPERATORS = {
    "raven/market/connect.py": {"apply_mcp_config", "mcp_manager", "mcp_executor_provider"},
    "raven/rpc/methods/reload.py": {"apply_mcp_config"},
    "raven/rpc/methods/subagents.py": {"apply_agents"},
    "raven/rpc/methods/config.py": {"set_default_binding"},
    "raven/agent/tools/plughub.py": {"mcp_manager"},
}

# The organs' own homes: a door's implementation, and the loop's own use of it.
_ORGAN_HOMES = ("raven/agent/loop/", "raven/mcp/", "raven/agent/subagent/")


def _door_strays(root: Path) -> list[str]:
    """Every spelling of a generation-door reach outside the organ homes and
    the operator roster, as ``file:line -> door``. Attribute spellings and the
    ``getattr``/``hasattr`` string forms count alike -- the string form is how
    the degrade-gracefully callers probe, and a guard that missed it would
    certify a hole.
    """
    import ast

    strays: list[str] = []
    for p in sorted((root / "raven").rglob("*.py")):
        rel = p.relative_to(root).as_posix()
        if "__pycache__" in p.parts or rel.startswith(_ORGAN_HOMES):
            continue
        allowed = _DOOR_OPERATORS.get(rel, frozenset())
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            doors: list[str] = []
            if isinstance(node, ast.Attribute) and node.attr in _GENERATION_DOORS:
                doors.append(node.attr)
            elif (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id in ("getattr", "hasattr")
                and len(node.args) >= 2
                and isinstance(node.args[1], ast.Constant)
                and node.args[1].value in _GENERATION_DOORS
            ):
                doors.append(node.args[1].value)
            strays.extend(f"{rel}:{node.lineno} -> {door}" for door in doors if door not in allowed)
    return strays


def test_the_generation_doors_have_exactly_their_declared_operators():
    strays = _door_strays(REPO)
    assert strays == [], (
        "a generation-door reach appeared outside the declared operators; the law "
        "lives on the RavenRuntime paper (core/runtime.py) -- route the need "
        "through a declared door and add the operator to _DOOR_OPERATORS with the "
        "reason in the diff, or take the change through a generation swap:\n" + "\n".join(strays)
    )


def test_the_door_roster_guard_bites(tmp_path):
    """Mutation audit, built in: a synthetic in-place reach turns the checker
    red, so a green run means looked-and-found-nothing."""
    pkg = tmp_path / "raven" / "sneaky"
    pkg.mkdir(parents=True)
    (pkg / "grab.py").write_text(
        "async def grab(loop):\n    await loop.apply_mcp_config({})\n    return getattr(loop, 'mcp_manager', None)\n",
        encoding="utf-8",
    )
    strays = _door_strays(tmp_path)
    assert len(strays) == 2 and all("grab.py" in s for s in strays), strays
