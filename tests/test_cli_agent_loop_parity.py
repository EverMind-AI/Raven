"""Cross-entrypoint parity for the assembly-door call.

Three entrypoints used to hand-write their own ``AgentLoop(...)`` call, and a
kwarg added to one and forgotten in the others failed *silently* -- the omitted
feature simply did not exist in that surface (``third_party_subagents`` missing
from two surfaces and ``plugin_tools`` missing from the gateway both shipped
that way). ``build_runtime`` dissolved the three copies: the cargo mapping now
exists once, in ``raven/core/runtime.py``, and parity of everything derived
from config holds by construction.

What can still drift is what the entrances still own -- the transport-side
``TurnPolicy`` / ``HostWiring`` fields and the identity handles they pass to
the door. This file keeps that remainder honest, and keeps the door singular:
an ``AgentLoop(...)`` call reappearing in an entrance is the regression this
guard exists to catch now.

So every difference has to be *declared* here, with the reason. An undeclared
one turns this red. Shrinking :data:`LEDGER` (i.e. fixing a gap) is also a
deliberate edit, because a stale entry turns it red too.

The check is static (``ast`` over the call site) rather than a runtime
kwarg-capture: ``gateway()`` cannot be driven under unit test at all -- it
builds ChannelManager / Cron / Heartbeat stacks whose shutdown paths assume a
running event loop (see the note in ``test_cli_gateway_commands.py``). Reading
the source is the only way to cover all three the same way.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

import pytest

from raven.agent.loop import AgentLoop

_REPO_ROOT = Path(__file__).resolve().parent.parent

ENTRYPOINTS: dict[str, str] = {
    "agent": "raven/cli/agent_commands.py",
    "gateway": "raven/cli/gateway_commands.py",
    "tui": "raven/cli/tui_commands.py",
}


class Difference(NamedTuple):
    """One declared asymmetry: who omits the kwarg, and why that is acceptable."""

    absent_from: frozenset[str]
    reason: str


# Declared asymmetries. Every entry is a deliberate design decision, not a known
# omission: an entrypoint that simply forgot a kwarg is a bug to fix, and the
# tests below make either shape visible -- an undeclared difference fails, and so
# does an entry that no longer matches the code.
LEDGER: dict[str, Difference] = {
    "response_modifier": Difference(
        absent_from=frozenset({"tui"}),
        reason=(
            "Sentinel hook. The gateway process owns Sentinel proactivity in "
            "v0.1 and the REPL builds its own stack; the TUI deliberately wires "
            "neither (see the _build_agent_loop docstring)."
        ),
    ),
    "cron_service": Difference(
        absent_from=frozenset({"agent"}),
        reason=(
            "Upstream removed the REPL from `agent`, so that process is never a "
            "cron runner and wiring a CronService would create jobs nothing "
            "fires (see the comment at the AgentLoop call in "
            "raven/cli/agent_commands.py). Scripted reminder creation is "
            "`raven cron add` with an explicit --channel."
        ),
    ),
    "now_fn": Difference(
        absent_from=frozenset({"tui"}),
        reason=(
            "Fed from a --fake-now CLI option that only agent and gateway "
            "expose; the TUI has no such flag, so there is nothing to pass."
        ),
    ),
    "deliverables": Difference(
        absent_from=frozenset({"agent", "tui"}),
        reason=(
            "The gateway builds the store itself because the web surface needs "
            "the same handle; agent and tui take the door's default, which is "
            "the identical expression."
        ),
    ),
}


def _call_kwargs(relative_path: str, callee: str) -> set[str]:
    """Flattened keyword names passed to the ``callee(...)`` call in one file.

    The wiring bundles are transparent: what a site really passes is the
    flattened field set, so descend into each bundle constructor and collect
    its keyword names.
    """
    source = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == callee:
            # ``**kwargs`` forwarding would make the set unreadable statically;
            # no site does that today, and this asserts it stays that way.
            assert all(kw.arg for kw in node.keywords), f"{relative_path}: {callee} called with **kwargs"
            flat: set[str] = set()
            for kw in node.keywords:
                inner = kw.value
                if isinstance(inner, ast.Call) and getattr(inner.func, "id", "").endswith(("Wiring", "Policy")):
                    assert all(k.arg for k in inner.keywords), f"{relative_path}: bundle with **kwargs"
                    flat |= {k.arg for k in inner.keywords if k.arg}
                else:
                    flat.add(kw.arg)
            return flat
    raise AssertionError(f"no {callee}(...) call found in {relative_path}")


def _agent_loop_kwargs(relative_path: str) -> set[str]:
    """What one entrance passes through the assembly door."""
    source = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else getattr(node.func, "attr", None)
            assert name != "AgentLoop", (
                f"{relative_path} constructs AgentLoop directly again; entrances "
                "must assemble through raven.core.runtime.build_runtime."
            )
    return _call_kwargs(relative_path, "build_runtime")


@pytest.fixture(scope="module")
def kwargs_by_entrypoint() -> dict[str, set[str]]:
    return {name: _agent_loop_kwargs(path) for name, path in ENTRYPOINTS.items()}


def _all_kwargs(kwargs_by_entrypoint: dict[str, set[str]]) -> set[str]:
    return set().union(*kwargs_by_entrypoint.values())


@pytest.mark.parametrize("entrypoint", sorted(ENTRYPOINTS))
def test_every_entrypoint_has_a_readable_call_site(entrypoint: str) -> None:
    """The parity check is only as good as its ability to find the call."""
    assert len(_agent_loop_kwargs(ENTRYPOINTS[entrypoint])) >= 10


def test_the_door_itself_stays_rich() -> None:
    """The single ``AgentLoop(...)`` site in the door still wires the full
    cargo surface; the old per-entrance floor moves here."""
    assert len(_call_kwargs("raven/core/runtime.py", "AgentLoop")) >= 25


def test_no_undeclared_asymmetry(kwargs_by_entrypoint: dict[str, set[str]]) -> None:
    """Every kwarg is either passed by all three, or declared in LEDGER.

    This is the check that catches "added a kwarg to one entrypoint and forgot
    the other two".
    """
    undeclared: dict[str, set[str]] = {}
    for kwarg in sorted(_all_kwargs(kwargs_by_entrypoint)):
        absent = {name for name, passed in kwargs_by_entrypoint.items() if kwarg not in passed}
        if absent and kwarg not in LEDGER:
            undeclared[kwarg] = absent

    assert not undeclared, (
        "AgentLoop kwargs differ between entrypoints without a LEDGER entry: "
        + "; ".join(f"{k} missing from {sorted(v)}" for k, v in undeclared.items())
        + ". Either pass it everywhere, or declare the asymmetry with its reason."
    )


@pytest.mark.parametrize("kwarg", sorted(LEDGER))
def test_declared_asymmetry_still_matches_reality(kwarg: str, kwargs_by_entrypoint: dict[str, set[str]]) -> None:
    """A LEDGER entry must describe the code exactly.

    Fails both ways on purpose: if a gap is fixed the entry has to go (so the
    file cannot rot into a list of lies), and if the asymmetry spreads to another
    entrypoint that is a new omission, not a covered one.
    """
    absent = frozenset(name for name, passed in kwargs_by_entrypoint.items() if kwarg not in passed)

    assert absent, (
        f"{kwarg!r} is now passed by every entrypoint -- delete its LEDGER entry. "
        f"Recorded reason was: {LEDGER[kwarg].reason}"
    )
    assert absent == LEDGER[kwarg].absent_from, (
        f"{kwarg!r} is absent from {sorted(absent)} but LEDGER declares {sorted(LEDGER[kwarg].absent_from)}."
    )


def test_ledger_entries_name_real_entrypoints() -> None:
    for kwarg, difference in LEDGER.items():
        unknown = difference.absent_from - set(ENTRYPOINTS)
        assert not unknown, f"LEDGER[{kwarg!r}] names unknown entrypoints: {sorted(unknown)}"
        assert difference.absent_from, f"LEDGER[{kwarg!r}] declares no absence"
        assert difference.reason.strip(), f"LEDGER[{kwarg!r}] has no reason"


def test_the_shared_core_is_not_eroding(kwargs_by_entrypoint: dict[str, set[str]]) -> None:
    """A floor on how much the three entrypoints agree about.

    Guards the other direction from :func:`test_no_undeclared_asymmetry`: that one
    is satisfiable by declaring ever more asymmetry, this one notices when the
    shared core is what shrank.
    """
    shared = set.intersection(*kwargs_by_entrypoint.values())

    assert len(shared) >= 10, f"only {len(shared)} kwargs are passed by all three entrypoints: {sorted(shared)}"


class _StubProvider:
    """Construction-only stand-in; no turn is ever run against it here."""

    def get_default_model(self) -> str:
        return "fake/default"

    async def chat_with_retry(self, **kwargs):  # pragma: no cover - never invoked
        raise NotImplementedError


def _build_loop(tmp_path: Path) -> AgentLoop:
    """A minimal ``AgentLoop``, built only to inspect its post-construction attributes."""
    return AgentLoop(provider=_StubProvider(), workspace=tmp_path, model="fake/default")


def test_loop_holds_subagent_questions_and_memory_config(tmp_path: Path) -> None:
    loop = _build_loop(tmp_path)
    assert loop.subagent_questions_config.autofill_enabled is True
    # memory_config was a pass-through parameter with no attribute behind it;
    # the resolver needs user_id and memory_top_k from it.
    assert loop.memory_config.user_id == "default"
    assert loop.memory_config.memory_top_k == 5
