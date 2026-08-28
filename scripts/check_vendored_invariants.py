"""Fail when a vendored subagent lacks a fix this trunk already carries.

``check_vendored_subagents.py`` guards the other direction: it catches a change
landing on ``subagents/*/`` that nobody meant to make. It says nothing about a
fork that is simply *behind* -- a bug this trunk fixed months ago, still live
inside a fork, failing for users who cannot tell which raven answered them.
Nothing reported that, because the forks are third-party source with no shared
history to diff against and thousands of legitimately divergent lines, so
"is it in sync" has no answer. "Does it still have this specific defect" does.

So a fix worth carrying gets an entry here, and CI checks every fork for it.
The entry is the propagation mechanism, not a report about one: whoever fixes
something in this trunk that the forks also have adds it, and the next upstream
zip that reverts the fix fails this check instead of shipping.

Each invariant is asserted on this trunk too. An invariant only ever checked
against the forks rots silently the moment the trunk rewrites the code it
describes -- it would keep passing while describing nothing.

A fork may be exempt, but only with a reason recorded beside it: a fork whose
own design makes the trunk's fix wrong there is a different thing from one
nobody has got round to, and a bare skip cannot tell them apart.
"""

from __future__ import annotations

import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENDOR_ROOT = REPO / "subagents"


def vendored_folders() -> list[str]:
    """Every fork on disk: a directory here carrying a ``subagent.json``.

    Read from the tree rather than listed here, because a list is what the
    ``raven-design`` gap was: the folder shipped, every invariant here named the
    other four, and a fork nobody had written down was reported as holding
    everything while being checked for nothing.

    The manifest is required, unlike ``check_vendored_subagents.py``'s scan,
    which hashes any directory it finds. That script guards whatever sits in the
    tree; this one asks which agents the host can dispatch to, and a directory
    declaring no manifest is neither -- it is what a half-finished checkout or a
    stray build directory looks like, and demanding trunk fixes of it would fail
    the run on a folder no install ever offers.
    """
    return sorted(
        p.name
        for p in VENDOR_ROOT.iterdir()
        if p.is_dir() and not p.name.startswith(".") and (p / "subagent.json").is_file()
    )


@dataclass(frozen=True)
class GuardedRegistration:
    """A tool must not reach the registry unless what it needs resolves.

    Registration is the checked step, not construction: constructing the tool
    does no I/O and every fork does it unconditionally, including the ones
    holding the fix. What the fix moved was the ``register`` call.

    Expressed structurally rather than as a pattern over the text because the
    forks differ in formatting and argument lists -- the trunk passes two
    arguments on one line, ``raven-ppt`` four over five lines -- and because
    the guard is a branch, which no line-oriented pattern can see the extent of.
    """

    id: str
    why: str
    tool: str
    guards: tuple[str, ...]
    trunk: tuple[str, ...]
    forks: dict[str, tuple[str, ...]]
    exempt: dict[str, str] = field(default_factory=dict)

    def describe_guard(self) -> str:
        return " / ".join(self.guards)


INVARIANTS: tuple[GuardedRegistration, ...] = (
    GuardedRegistration(
        id="web-search-gated-on-its-key",
        why=(
            "A tool that cannot run must not be advertised: the model reaches for it, "
            "every call fails, and the error text -- naming a config file and an env var -- "
            "is relayed to whoever is on the other end of the channel. One TB run spent "
            "17 of 17 web_search calls this way before the trunk gated it."
        ),
        tool="WebSearchTool",
        guards=("api_key", "corpus_endpoint"),
        # Two registries per checkout, and the second one moved: this trunk builds a
        # sub-agent's tool set in `subagent/backends/raven_loop.py`, every fork still
        # in `subagent/manager.py`. Both have to be listed -- gating only the main
        # loop leaves the tool advertised to every nested sub-agent.
        trunk=(
            "raven/agent/loop/main.py",
            "raven/agent/subagent/backends/raven_loop.py",
        ),
        forks={
            "raven-code": (
                "subagents/raven-code/Raven-main/raven/agent/loop/main.py",
                "subagents/raven-code/Raven-main/raven/agent/subagent/manager.py",
            ),
            "raven-design": (
                "subagents/raven-design/Raven-Design/raven/agent/loop/main.py",
                "subagents/raven-design/Raven-Design/raven/agent/subagent/manager.py",
            ),
            "raven-oncall": (
                "subagents/raven-oncall/Raven-Oncall/raven/agent/loop/main.py",
                "subagents/raven-oncall/Raven-Oncall/raven/agent/subagent/manager.py",
            ),
            "raven-ppt": (
                "subagents/raven-ppt/Raven-PPT/raven/agent/loop/main.py",
                "subagents/raven-ppt/Raven-PPT/raven/agent/subagent/manager.py",
            ),
            "raven-research": (
                "subagents/raven-research/Raven-X/raven/agent/loop/main.py",
                "subagents/raven-research/Raven-X/raven/agent/subagent/manager.py",
            ),
        },
    ),
)


def _called_name(call: ast.Call) -> str | None:
    func = call.func
    if isinstance(func, ast.Attribute):
        return func.attr
    return getattr(func, "id", None)


def _negates(node: ast.expr) -> bool:
    """Whether this test reads as "the guard is absent" rather than "present".

    ``if not web_search.api_key`` and ``if web_search.api_key is None`` guard
    their *else*, not their body. Reading only for the name would let the first
    one certify the branch that registers the tool without a key -- the exact
    defect, wearing a guard's shape.
    """
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
        return True
    if isinstance(node, ast.Compare) and len(node.ops) == 1:
        op, right = node.ops[0], node.comparators[0]
        if isinstance(op, (ast.Is, ast.Eq)) and isinstance(right, ast.Constant):
            return not right.value
    return False


def _mentions(node: ast.AST, names: tuple[str, ...], *, negated: bool = False) -> bool:
    """Whether `node` requires one of `names` with the polarity asked for.

    Polarity rather than mere presence, so that the branch a test certifies is
    the branch that actually has the guard. Only ``not`` and a comparison
    against a falsey constant are read as negation; anything more indirect
    (a helper that returns the key, an early ``return`` above the call) is not
    understood, and reads as no guard at all -- a false alarm, never a silent
    pass.
    """
    if isinstance(node, ast.BoolOp):
        return any(_mentions(v, names, negated=negated) for v in node.values)
    if _negates(node):
        inner = node.operand if isinstance(node, ast.UnaryOp) else node.left
        return _mentions(inner, names, negated=not negated)
    if negated:
        return False
    for child in ast.walk(node):
        if isinstance(child, ast.Name) and child.id in names:
            return True
        if isinstance(child, ast.Attribute) and child.attr in names:
            return True
    return False


def _holders(tree: ast.AST, tool: str) -> tuple[str, ...]:
    """Local names bound to a construction of `tool`.

    Needed because the trunk's own shape binds the tool to a name and registers
    that name one line later, so the registration's argument does not itself
    name the tool.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
            continue
        if _called_name(node.value) != tool:
            continue
        for target in node.targets:
            if isinstance(target, ast.Name):
                names.add(target.id)
    return tuple(sorted(names))


def _unguarded_lines(source: str, tool: str, guards: tuple[str, ...]) -> tuple[list[int], bool]:
    """Lines of registrations of `tool` no enclosing guard covers, and whether any were found.

    An ``if`` certifies whichever of its two branches the test actually argues
    for: ``if key`` its body, ``if not key`` its else. Registering in the other
    branch is the unguarded case wearing a guard's shape, and is reported.
    """
    tree = ast.parse(source)
    referents = (tool, *_holders(tree, tool))
    unguarded: list[int] = []
    seen = False

    def visit(node: ast.AST, guarded: bool) -> None:
        nonlocal seen
        if isinstance(node, ast.Call) and _called_name(node) == "register":
            if any(_mentions(arg, referents) for arg in node.args):
                seen = True
                if not guarded:
                    unguarded.append(node.lineno)
        if isinstance(node, ast.If):
            visit(node.test, guarded)
            # The two branches are certified separately: `if key` guards its
            # body, `if not key` guards its else, and neither guards the other.
            for child in node.body:
                visit(child, guarded or _mentions(node.test, guards))
            for child in node.orelse:
                visit(child, guarded or _mentions(node.test, guards, negated=True))
            return
        for child in ast.iter_child_nodes(node):
            visit(child, guarded)

    visit(tree, False)
    return sorted(unguarded), seen


def _check(path: Path, inv: GuardedRegistration) -> str | None:
    """The failure for one file, or None when it holds the invariant."""
    rel = path.relative_to(REPO) if path.is_relative_to(REPO) else path
    try:
        source = path.read_text(encoding="utf-8")
    except OSError as exc:
        return f"cannot read {rel}: {exc}"
    try:
        unguarded, seen = _unguarded_lines(source, inv.tool, inv.guards)
    except SyntaxError as exc:
        return f"cannot parse {rel}: {exc}"
    if not seen:
        # A file that never registers the tool is not evidence of the fix, so
        # a stale path has to fail rather than pass for lack of anything to see.
        return f"{rel} registers no {inv.tool}; the path in this invariant is stale"
    if not unguarded:
        return None
    where = ", ".join(f"line {n}" for n in unguarded)
    return f"{rel}: {inv.tool} registered outside a test of {inv.describe_guard()} ({where})"


def unchecked_forks(inv: GuardedRegistration) -> list[str]:
    """Folders on disk this invariant does not name in :attr:`forks`.

    An invariant otherwise covers whichever forks its author remembered, and the
    omission is invisible: a fork absent from ``forks`` produces no target, no
    target produces no failure, and the run reports every fork holding the fix.
    ``raven-design`` sat outside the one invariant here for exactly that reason.

    Membership in ``forks`` is the whole question, exemption included: ``exempt``
    names a fork whose paths are listed and deliberately not checked, which is
    what ``test_an_exemption_carries_a_reason`` enforces and what lets a reader
    see which files the waiver covers. Reading exemption as an alternative to
    listing would let a bare name in ``exempt`` satisfy this while failing that
    test -- two guards disagreeing about one registry.

    Consulted by :func:`main`, so the executable gate is what fails. Reporting
    this only from the test suite left ``make check-vendored-invariants`` and the
    invariant half of ``make lint`` exiting 0 on the exact omission this is meant
    to catch -- a false green on the one command a contributor runs.

    Reported rather than silently checked, because whether a fork should hold a
    given fix is a judgement, and guessing it is not this function's job.
    """
    return [f for f in vendored_folders() if f not in inv.forks]


def main(argv: list[str]) -> int:
    unnamed: list[str] = []
    failures: list[str] = []
    for inv in INVARIANTS:
        unnamed += [f"[{inv.id}] {fork}" for fork in unchecked_forks(inv)]
        targets = [("trunk", inv.trunk)]
        targets += [(f, p) for f, p in sorted(inv.forks.items()) if f not in inv.exempt]
        for name, paths in targets:
            for rel in paths:
                problem = _check(REPO / rel, inv)
                if problem:
                    failures.append(f"[{inv.id}] {name}: {problem}")

    if not unnamed and not failures:
        return 0

    if unnamed:
        print("Vendored forks no invariant names:", file=sys.stderr)
        for line in unnamed:
            print(f"  {line}", file=sys.stderr)
        print(
            "\nEach line is a folder shipping under `subagents/` that the named invariant\n"
            "does not list, so it is checked for nothing while the run reports every fork\n"
            "as holding the fix. Add its paths to that invariant -- or, if the fix is wrong\n"
            "for that fork, add them and name it in `exempt` with the reason, so the waiver\n"
            "still says which files it covers.",
            file=sys.stderr,
        )

    if failures:
        print("Vendored subagents are behind this trunk:", file=sys.stderr)
        for failure in failures:
            print(f"  {failure}", file=sys.stderr)
        print(
            "\nEach line above is a fix this trunk carries and that fork does not. Apply it\n"
            "inside the fork -- minimally, in the fork's own idiom, never by copying this\n"
            "trunk's file over it -- then record the tree in the same commit:\n"
            "  python3 scripts/check_vendored_subagents.py --update\n"
            "If the fix is wrong for that fork, add it to that invariant's `exempt` with the\n"
            "reason. A trunk failure means the trunk itself moved: fix the trunk, or rewrite\n"
            "the invariant to describe where the guarantee lives now.",
            file=sys.stderr,
        )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
