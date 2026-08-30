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

from raven.agent.loop.bundles import ToolWiring, TurnPolicy

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
    "everos": "raven.plugins.memory.everos",
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
# inside a mechanism package rides the wrong wheel at split time. everos is
# the one known debt (move scheduled for the S stage); this allowlist is that
# debt's ledger -- shrink it, never grow it.
_MECHANISM_PACKAGES = ["plugins", "market"]
_CARGO_DEBT_ALLOWLIST = {"plugins/memory/everos"}


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
