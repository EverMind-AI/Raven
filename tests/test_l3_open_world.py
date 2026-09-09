"""The L3 constitution, pinned in both directions.

L3 = the open-world layer: what an installation carries is the installation's
business -- absent, no inner layer knows; present, no inner line changes.

Delete-direction: inner layers must hold ZERO module-level imports of any
roster member (lazy in-function imports are the allowed shape for optional
mouths like playbook's tools). Measured clean for the three representatives
on 2026-08-21; this keeps them clean.

Add-direction: a synthetic plugin no code has ever seen, dropped into a user
directory, must ride discovery -> registry -> tool factory -> a real
AgentLoop's registry and schema -> execution, with zero inner diff. Notably
the synthetic tool is exactly the L1 four-member contract core, so this also
re-proves the registry hardening composes with the plugin path.
"""

from __future__ import annotations

import ast
import textwrap
import tomllib
from pathlib import Path

import pytest

from raven.agent.loop.bundles import HostWiring, ToolWiring, TurnPolicy

REPO = Path(__file__).resolve().parent.parent


def _seated_inner() -> list[str]:
    """The seats the contract calls inner, read from the contract.

    ``pyproject.toml``'s "inner layers know no surface" is the roster. The hand
    copy this file kept had fifteen names when the contract seated thirty-one,
    so half the inner tree was never walked and the delete-direction clause
    held only where someone had remembered to look. The L4 guard reads the
    same contract with the same call.
    """
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = data["tool"]["importlinter"]["contracts"]
    inner = next(c for c in contracts if c["name"] == "inner layers know no surface")
    return sorted(m.removeprefix("raven.") for m in inner["source_modules"])


def _python_files(name: str) -> list[Path]:
    """Every .py of one seat, whether the seat is a package or a single module."""
    pkg = REPO / "raven" / name
    if pkg.is_dir():
        return [p for p in pkg.rglob("*.py") if "__pycache__" not in p.parts]
    module = REPO / "raven" / f"{name}.py"
    assert module.is_file(), f"seat {name!r} is neither a package nor a module"
    return [module]


INNER_DIRS = _seated_inner()

# Roster members with their package prefixes; inner layers may know them
# lazily (function-level) but never at module level.
ROSTER = {
    "playbook": "raven.playbook",
    "everos": "raven_everos",
    "importer": "raven.importer",
    "eval_engine": "raven.eval_engine",
    **{
        f"adapter-{n}": f"raven.channels.adapters.{n}"
        for n in (
            "dingtalk",
            "discord",
            "email",
            "feishu",
            "matrix",
            "mochat",
            "qq",
            "slack",
            "telegram",
            "wecom",
            "weixin",
            "whatsapp",
        )
    },
}

# Delete-direction debt: the assembly root's eval stack names the eval engine
# at module level. Whether that stack gets wired for real or dissolved is a
# product ruling still open, so the edge is ledgered here rather than hidden
# by leaving core off the roster -- shrink it, never grow it.
_DELETE_DIRECTION_DEBT = {"raven.eval_engine": {"core/eval_stack.py"}}


def _names_target(node: ast.stmt, target: str) -> bool:
    if isinstance(node, ast.ImportFrom):
        return bool(node.module) and node.module.startswith(target)
    if isinstance(node, ast.Import):
        return any(a.name.startswith(target) for a in node.names)
    return False


def _module_level_imports(target: str) -> list[str]:
    hits = []
    rel = target.removeprefix("raven.").replace(".", "/")
    debt = _DELETE_DIRECTION_DEBT.get(target, ())
    for d in INNER_DIRS:
        for p in _python_files(d):
            if rel in str(p) or p.relative_to(REPO / "raven").as_posix() in debt:
                continue
            try:
                tree = ast.parse(p.read_text(errors="replace"))
            except SyntaxError:
                continue
            hits.extend(f"{p}:{node.lineno}" for node in tree.body if _names_target(node, target))
    return hits


@pytest.mark.parametrize("member", sorted(ROSTER))
def test_delete_direction_zero_module_level_inner_knowledge(member: str):
    hits = _module_level_imports(ROSTER[member])
    assert hits == [], (
        f"inner layers gained module-level knowledge of L3 member {member!r}: {hits}. "
        "Optional capabilities are imported lazily or discovered, never at module level."
    )


def test_the_delete_direction_debt_is_still_debt():
    """An allowlisted edge that is no longer there is an exemption nobody
    needs; the entry comes off the ledger the moment the import goes lazy."""
    for target, files in _DELETE_DIRECTION_DEBT.items():
        for rel in files:
            tree = ast.parse((REPO / "raven" / rel).read_text(errors="replace"))
            assert any(_names_target(node, target) for node in tree.body), (
                f"{rel} no longer imports {target} at module level; drop it from the ledger"
            )


@pytest.mark.asyncio
async def test_add_direction_synthetic_plugin_rides_to_a_real_turn(tmp_path: Path):
    plug = tmp_path / "plugins" / "synth-cap"
    plug.mkdir(parents=True)
    (plug / "raven-plugin.toml").write_text(
        textwrap.dedent("""
        [plugin]
        id = "synth-cap"
        version = "0.0.1"
        display_name = "Synthetic capability"
        raven = ">=0.1"
        enabled_by_default = true
        [[plugin.contributes.tools]]
        name = "synth_echo"
        factory = "synth_cap_pkg.tools:make_tool"
    """)
    )
    pkg = plug / "synth_cap_pkg"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "tools.py").write_text(
        textwrap.dedent("""
        from raven.contracts.tool import Tool

        class SynthEchoTool(Tool):
            name = "synth_echo"
            description = "Echo for the add-direction constitution test."
            parameters = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}
            async def execute(self, **kw):
                return f"synth: {kw.get('text', '')}"
        def make_tool(ctx):
            return SynthEchoTool()
    """)
    )

    from raven.plugins.bootstrap import assemble_plugin_registry

    reg = assemble_plugin_registry(user_dir=tmp_path / "plugins", entry_points_group=None)
    assert "synth_echo" in reg.tool_names()
    tool = reg.build_tool("synth_echo", config={}, services=None)

    from raven.agent.loop.main import AgentLoop

    class _Resp:
        content = "ok"
        tool_calls: list = []
        reasoning_content = None
        thinking_blocks = None
        usage: dict = {}
        finish_reason = "stop"
        error_classification = None

        def has_tool_calls(self):
            return False

    class _Provider:
        async def chat(self, messages, **kw):
            return _Resp()

        async def chat_stream(self, messages, **kw):
            return _Resp()

    loop = AgentLoop(
        provider=_Provider(),
        workspace=tmp_path / "ws",
        model="f",
        policy=TurnPolicy(interactive=False),
        tools=ToolWiring(plugin_tools=[tool]),
    )
    assert loop.tools.get("synth_echo") is not None
    assert "synth_echo" in [d["function"]["name"] for d in loop.tools.get_definitions()]
    assert await loop.tools.execute("synth_echo", {"text": "hi"}) == "synth: hi"


# Mechanism packages must not house cargo: a self-contained capability living
# inside a mechanism package rides the wrong wheel at split time. everos was
# the one known debt and it is paid -- the backend left for
# plugins-dist/everos-memory -- so the ledger is empty and stays that way.
_MECHANISM_PACKAGES = ["plugins", "market"]
_CARGO_DEBT_ALLOWLIST: set[str] = set()


def test_the_mechanism_roster_names_packages_that_exist():
    """Both packages on the roster were renamed once (plugin -> plugins,
    plughub -> market, in 536418b1) and the roster was not, so the walk below
    iterated nothing and the ban passed vacuously for the rename's lifetime."""
    for pkg in _MECHANISM_PACKAGES:
        assert (REPO / "raven" / pkg).is_dir(), f"mechanism package {pkg!r} is not there"
    for debt in _CARGO_DEBT_ALLOWLIST:
        assert (REPO / "raven" / debt).is_dir(), f"allowlisted debt {debt!r} is not there"


def test_no_new_cargo_inside_mechanism_packages():
    offenders = []
    for pkg in _MECHANISM_PACKAGES:
        for sub in (REPO / "raven" / pkg).rglob("__init__.py"):
            subdir = sub.parent
            rel = str(subdir.relative_to(REPO / "raven"))
            if rel == pkg or "__pycache__" in rel:
                continue
            py_lines = sum(
                len(f.read_text(errors="replace").splitlines())
                for f in subdir.rglob("*.py")
                if "__pycache__" not in f.parts
            )
            if py_lines >= 300 and not any(rel.startswith(a) or a.startswith(rel) for a in _CARGO_DEBT_ALLOWLIST):
                offenders.append(f"{rel} ({py_lines} lines)")
    assert offenders == [], (
        f"new cargo appeared inside a mechanism package: {offenders}. "
        "Capabilities live on the shelf, not inside the shelf's machinery."
    )


@pytest.mark.asyncio
async def test_add_direction_the_first_real_product_rides_to_a_real_turn(tmp_path: Path, monkeypatch):
    """The synthetic ride above proves a four-member toy boards; the first real
    product's port proved that a toy passing is not a product passing -- six
    seams leaked around it, all closed since. This rides the REAL
    research-flow plugin through the same doors production uses, zero inner
    diff: extra_dirs discovery (plugins.dirs' machinery), keyed registration
    (a keyless web_search declines instead of serving an error string per
    call), the hook chain on a live turn, built-in shadowing by name, and the
    flow filing its session record at turn end.
    """
    from raven.plugins.bootstrap import assemble_plugin_registry
    from raven.plugins.context import ServiceLocator
    from raven.spine.message import ChatType, Source
    from raven.spine.turn import Origin, TurnRequest

    monkeypatch.delenv("SERPER_API_KEY", raising=False)

    class _Resp:
        content = "ok"
        tool_calls: list = []
        reasoning_content = None
        thinking_blocks = None
        usage: dict = {}
        finish_reason = "stop"
        error_classification = None

        def has_tool_calls(self):
            return False

    class _Provider:
        async def chat(self, messages, **kw):
            return _Resp()

        async def chat_with_retry(self, **kw):
            return _Resp()

        def get_default_model(self):
            return "fake/default"

    provider = _Provider()

    root = REPO / "agents" / "raven-research" / "plugins"
    reg = assemble_plugin_registry(extra_dirs=[root], entry_points_group=None)
    assert "research-flow" in reg.activated_ids()

    # The shared plugin state is keyed by workspace, so the keyless probe gets
    # its own workspace and cannot leak into the keyed boarding below.
    keyless = reg.build_tool(
        "web_search",
        config={"enabled": True},
        # The locator lends the provider the way production does -- without one the
        # plugin declines wholesale (its LLM gates would be dead), which is its own
        # behaviour, not the one under test here.
        services=ServiceLocator(workspace=tmp_path / "keyless-ws", user_id="t", agent_id="t", provider=provider),
    )
    assert keyless is None, "a keyless web_search must decline, not serve error strings"

    services = ServiceLocator(workspace=tmp_path / "ws", user_id="t", agent_id="t", provider=provider)
    slice_ = {
        "enabled": True,
        "conversation": {"enabled": False},
        "search": {"apiKey": "test-key"},
        "stateRoot": str(tmp_path / "state"),
    }
    tools = [reg.build_tool(n, config=slice_, services=services) for n in ("web_search", "web_fetch")]
    assert [t.name for t in tools if t is not None] == ["web_search", "web_fetch"], (
        "the replacement tools board once the key is in the slice"
    )
    hook = reg.build_hook("research_flow", config=slice_, services=services)
    assert hook is not None, "the flow hook boards whenever the plugin is installable"

    from raven.agent.loop.main import AgentLoop

    loop = AgentLoop(
        provider=provider,
        workspace=tmp_path / "ws",
        model="f",
        policy=TurnPolicy(interactive=False),
        tools=ToolWiring(plugin_tools=[t for t in tools if t is not None]),
        host=HostWiring(hooks=[hook]),
    )

    async def _noop(**_kw):
        return None

    loop._start_executor = _noop
    loop._connect_mcp = _noop

    assert loop.tools.get("web_search") is tools[0], "the plugin's tool shadows the built-in by name"

    reply, _ = await loop._process_message(
        TurnRequest(
            origin=Origin.USER,
            source=Source(channel="cli", chat_id="c", sender_id="u", chat_type=ChatType.DM),
            text="hello there",
            conversation="cli:ride",
        )
    )
    assert reply == "ok"
    records = list((tmp_path / "state" / "sessions").glob("*.json"))
    assert records, "the flow's turn-end frame files a session record through the real chain"


def _product_plugin_redline_strays(plugin_roots):
    """The cargo redlines, applied to product plugin trees.

    (1) never raven.spine -- a product reaches the kernel through papers;
    (2) never a private module or symbol of the trunk;
    (3) never another cargo's insides: channel adapters, a different product
        plugin's package, or a shipped plugin distribution's package.
    raven.agent's shelf is deliberately NOT forbidden: it is the shared
    service layer a product may extend (the ask_user gate subclasses the
    kernel tool by design, measured 2026-08-31).
    """
    packages = {}
    for plugin_root in plugin_roots:
        for d in sorted(plugin_root.iterdir()):
            if d.is_dir() and (d / "__init__.py").exists():
                packages[d.name] = plugin_root
    strays = []
    for plugin_root in plugin_roots:
        for p in sorted(plugin_root.rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            rel_file = p.relative_to(plugin_root.parent.parent.parent)
            for node in ast.walk(ast.parse(p.read_text(errors="replace"))):
                mods, names = [], []
                if isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    mods = [node.module]
                    names = [a.name for a in node.names]
                for m in mods:
                    top = m.split(".")[0]
                    if top == "raven":
                        if m == "raven.spine" or m.startswith("raven.spine."):
                            strays.append(f"{rel_file}:{node.lineno} imports {m} (kernel through papers only)")
                        if any(part.startswith("_") for part in m.split(".")):
                            strays.append(f"{rel_file}:{node.lineno} imports private module {m}")
                        for n in names:
                            if n.startswith("_") and n != "_":
                                strays.append(f"{rel_file}:{node.lineno} imports private symbol {m}.{n}")
                        if m.startswith("raven.channels.adapters"):
                            strays.append(f"{rel_file}:{node.lineno} reaches another cargo's insides ({m})")
                    elif top == "raven_everos":
                        strays.append(f"{rel_file}:{node.lineno} imports a shipped distribution's package ({m})")
                    elif top in packages and packages[top] != plugin_root:
                        strays.append(f"{rel_file}:{node.lineno} imports another product plugin ({m})")
    return strays


def test_product_plugins_obey_the_cargo_redlines():
    """The first product broke redline one (a private context-engine symbol)
    while every machine stayed green, because the redlines only watched the
    factory-shipped roster. Product plugin trees are cargo too; same law."""
    roots = sorted(p for p in (REPO / "agents").glob("*/plugins/*") if p.is_dir())
    assert roots, "the first product exists; an empty glob means the tree moved"
    strays = _product_plugin_redline_strays(roots)
    assert strays == [], "product plugin redline breaches:\n" + "\n".join(strays)


def test_the_product_redline_guard_bites(tmp_path: Path):
    """Mutation audit, built in: each redline turns the checker red on a
    synthetic tree, so a green real-tree run means looked-and-found-nothing."""
    plugin = tmp_path / "agents" / "prod" / "plugins" / "bad-plugin"
    pkg = plugin / "bad_pkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("")
    (pkg / "mod.py").write_text(
        "import raven.spine.turn\n"
        "from raven.context_engine.segments.render import _language_directive\n"
        "from raven.channels.adapters.telegram import adapter\n"
        "import raven_everos\n",
        encoding="utf-8",
    )
    strays = _product_plugin_redline_strays([plugin])
    assert len(strays) == 4, strays
