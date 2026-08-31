"""The L4 constitution, pinned.

L4 = the entrance layer: whoever calls the kernel. Three runnable clauses:
1. The kernel has zero knowledge of its callers -- no inner package imports
   any surface package, ever.
2. A NEW entrance needs nothing but inward imports: a ~25-line surface builds
   a Scheduler, submits a TurnRequest, and receives deliverables -- without
   touching or knowing cli/rpc/acp.
3. Every turn produces EXACTLY one terminal event (the L0 handover: consumers
   key their per-turn release on it; zero would leak the slot, two would test
   release idempotence in anger).
"""

from __future__ import annotations

import ast
import tempfile
import tomllib
from pathlib import Path

import pytest

from raven.agent.loop.bundles import TurnPolicy

REPO = Path(__file__).resolve().parent.parent


def _seated_inner() -> list[str]:
    """The packages the contract seats as inner, read from the contract.

    ``pyproject.toml``'s "inner layers know no surface" is the roster, and this
    file used to keep a second copy of it. The copy drifted twice without
    failing anything -- ``i18n`` and ``observability`` were seated in the
    contract and never added here -- so the guard silently stopped covering the
    packages it had just been told about. Reading the roster is the fix; the
    kernel-closure test does the same with the same tomllib call.
    """
    data = tomllib.loads((REPO / "pyproject.toml").read_text(encoding="utf-8"))
    contracts = data["tool"]["importlinter"]["contracts"]
    inner = next(c for c in contracts if c["name"] == "inner layers know no surface")
    return sorted(m.removeprefix("raven.") for m in inner["source_modules"])


INNER_DIRS = _seated_inner()


def _python_files(name: str) -> list[Path]:
    """Every .py of one seat, whether the seat is a package or a single module."""
    pkg = REPO / "raven" / name
    if pkg.is_dir():
        return [p for p in pkg.rglob("*.py") if "__pycache__" not in p.parts]
    module = REPO / "raven" / f"{name}.py"
    assert module.is_file(), f"seat {name!r} is neither a package nor a module"
    return [module]


SURFACES = ("raven.cli", "raven.rpc", "raven.acp")


def test_kernel_and_organs_know_no_surface():
    offenders = []
    for d in INNER_DIRS:
        for p in _python_files(d):
            try:
                tree = ast.parse(p.read_text(errors="replace"))
            except SyntaxError:
                continue
            for node in tree.body:
                mods = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    mods = [node.module]
                elif isinstance(node, ast.Import):
                    mods = [a.name for a in node.names]
                for m in mods:
                    if any(m == s or m.startswith(s + ".") for s in SURFACES):
                        offenders.append(f"{p.relative_to(REPO)}:{node.lineno} -> {m}")
    # Debt allowlist: EMPTY, and may only shrink — it emptied when ask_user /
    # deep_research were re-typed against the QuestionResponder paper (tool
    # side), so no inner module names the concrete broker machine at rpc.
    assert offenders == [], f"an inner layer imports a surface (callers must stay unknown to the called): {offenders}"


class _Resp:
    content = "fifth entrance says hi"
    tool_calls: list = []
    reasoning_content = None
    thinking_blocks = None
    usage: dict = {}
    finish_reason = "stop"
    error_classification = None

    has_tool_calls = False


class _Provider:
    async def chat(self, messages, **kw):
        return _Resp()

    async def chat_with_retry(self, messages, **kw):
        return _Resp()

    async def chat_stream(self, messages, **kw):
        return _Resp()


@pytest.mark.asyncio
async def test_a_new_entrance_needs_only_inward_imports():
    """The whole fifth entrance, inline. It imports spine + agent and nothing
    from any existing surface; if this ever needs a cli/rpc import to work,
    the entrance layer has grown a hidden dependency."""
    from raven.agent.loop.main import AgentLoop
    from raven.agent.spine_runner import AgentTurnRunner
    from raven.spine.message import ChatType, Source
    from raven.spine.scheduler import OriginPools, Scheduler
    from raven.spine.turn import Origin, TurnRequest

    loop = AgentLoop(
        provider=_Provider(), workspace=Path(tempfile.mkdtemp()), model="f", policy=TurnPolicy(interactive=False)
    )
    received: list = []

    async def sink(ev):
        received.append(type(ev).__name__)

    sched = Scheduler(runner=AgentTurnRunner(loop, stream=False), pools=OriginPools(user=2, system=2), sink=sink)
    req = TurnRequest(
        origin=Origin.USER,
        source=Source(channel="fifth", chat_id="c1", sender_id="u", chat_type=ChatType.DM),
        text="hello from the fifth entrance",
    )
    outcome = await sched.submit(req).result()

    assert outcome is not None
    assert "Text" in received, "the entrance must receive the answer"
    terminals = [e for e in received if e in ("TurnEnded", "TurnFailed")]
    assert len(terminals) == 1, (
        f"exactly one terminal event per turn, got {terminals} "
        "(zero leaks the consumer's slot; two would double-release)"
    )


def test_every_seat_the_contract_names_is_something_the_guard_can_walk() -> None:
    """A seat naming a path that does not exist would be scanned as nothing, and
    a guard that scans nothing passes."""
    seats = _seated_inner()

    assert len(seats) >= 28, seats
    for name in seats:
        assert _python_files(name), name


def _acp_rpc_facade_strays(pkg_root):
    """Every import of ``raven.rpc`` from the acp surface that is not on the
    facade roster, as ``file:line`` strays.

    The surfaces-law contracts pin the zero directions; this pins the one
    directed edge that legitimately remains -- acp hosts an rpc stack over its
    translator -- to the single module built for hosting. A second rpc import
    appearing under raven/acp means someone reached past the facade into the
    sibling surface's insides, which is the exact disease the law retired.
    """
    import ast

    facade = {"raven.rpc.bootstrap"}
    strays = []
    for p in sorted(pkg_root.rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            mods = []
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                mods = [node.module]
            for m in mods:
                if (m == "raven.rpc" or m.startswith("raven.rpc.")) and m not in facade:
                    strays.append(f"{p.relative_to(pkg_root.parent.parent)}:{node.lineno} -> {m}")
    return strays


def test_acp_reaches_rpc_only_through_the_bootstrap_facade():
    strays = _acp_rpc_facade_strays(REPO / "raven" / "acp")
    assert strays == [], (
        "acp reached past the rpc facade; host through raven.rpc.bootstrap or "
        "move the shared piece inward, and widen this roster only with the "
        "reason in the diff:\n" + "\n".join(strays)
    )


def test_the_facade_roster_guard_bites(tmp_path):
    """Mutation audit, built in: a synthetic reach past the facade turns the
    checker red, so a green run means looked-and-found-nothing."""
    pkg = tmp_path / "raven" / "acp"
    pkg.mkdir(parents=True)
    (pkg / "sneaky.py").write_text(
        "from raven.rpc.bootstrap import build_rpc_stack\nfrom raven.rpc.dispatcher import Dispatcher\n",
        encoding="utf-8",
    )
    strays = _acp_rpc_facade_strays(pkg)
    assert len(strays) == 1 and "dispatcher" in strays[0], strays
