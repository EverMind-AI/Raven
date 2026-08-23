"""Cross-entrypoint parity for the ``AgentLoop(...)`` keyword arguments.

Three entrypoints each hand-write their own ``AgentLoop(...)`` call: ``raven
agent`` (REPL), ``raven gateway`` (long-running service behind the web UI), and
``raven tui``. Nothing makes them agree, and a kwarg added to one and forgotten
in the others fails *silently* -- the omitted feature simply does not exist in
that surface. Two real instances of exactly that:

* ``third_party_subagents`` was passed only by the gateway, so ``run_subagent_dag``
  was never registered in the TUI or the REPL and the model had no way to call it.
* ``plugin_tools`` was passed by ``agent`` and ``tui`` but not the gateway, so
  plugin-contributed tools were absent from the web UI.

Both are fixed; this file is what keeps them fixed.

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
}


def _agent_loop_kwargs(relative_path: str) -> set[str]:
    """Keyword names passed to the ``AgentLoop(...)`` call in one entrypoint."""
    source = (_REPO_ROOT / relative_path).read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name == "AgentLoop":
            # ``**kwargs`` forwarding would make the set unreadable statically;
            # no entrypoint does that today, and this asserts it stays that way.
            assert all(kw.arg for kw in node.keywords), f"{relative_path}: AgentLoop called with **kwargs"
            return {kw.arg for kw in node.keywords if kw.arg}
    raise AssertionError(f"no AgentLoop(...) call found in {relative_path}")


@pytest.fixture(scope="module")
def kwargs_by_entrypoint() -> dict[str, set[str]]:
    return {name: _agent_loop_kwargs(path) for name, path in ENTRYPOINTS.items()}


def _all_kwargs(kwargs_by_entrypoint: dict[str, set[str]]) -> set[str]:
    return set().union(*kwargs_by_entrypoint.values())


@pytest.mark.parametrize("entrypoint", sorted(ENTRYPOINTS))
def test_every_entrypoint_has_a_readable_call_site(entrypoint: str) -> None:
    """The parity check is only as good as its ability to find the call."""
    assert len(_agent_loop_kwargs(ENTRYPOINTS[entrypoint])) > 10


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

    assert len(shared) >= 25, f"only {len(shared)} kwargs are passed by all three entrypoints: {sorted(shared)}"
