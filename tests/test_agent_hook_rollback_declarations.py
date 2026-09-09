"""A tree whose hook can roll back declares it on the class the loop sees.

``AgentHook.rolls_back_iterations`` is read off the hook object the loop holds,
once per turn, before any per-session state is resolved. A hook that delegates
its phases to a per-session chain therefore cannot have the flag derived for it:
the chain does not exist yet at the moment of the read. ``ResearchFlowHook`` is
that shape -- its six rollback sites live in ``gates/``, behind a composite it
builds per session -- so the declaration is the only thing standing between a
rolled-back draft and the reader, and nothing local to ``flow.py`` hints that it
is needed. This file is that hint.

The producing set is derived rather than listed: every ``HookDecision`` built
with ``rollback=True`` is found by AST, so a tree that grows a rollback has to
appear in ``REGISTERED_HOOK`` before this file passes. AST rather than imports on
purpose -- ``raven_design`` and ``raven_ppt`` are editable installs rooted at the
main checkout, so importing them from a worktree reads the main checkout's code
instead of the revision under test.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent

#: Tree root -> (module holding the contributed hook, its class name). The keys
#: are checked against the derived producing set, so this table cannot go quietly
#: stale: a new rollback in an unlisted tree fails as an unmapped root.
REGISTERED_HOOK: dict[str, tuple[str, str]] = {
    "agents/raven-research/plugins/research-flow": ("research_flow/flow.py", "ResearchFlowHook"),
    "plugins-dist/design-engine": ("raven_design/plugin/hook.py", "DesignEngineHook"),
    "plugins-dist/ppt-engine": ("raven_ppt/plugin/hook.py", "PptEngineHook"),
}

SEARCH_ROOTS = ("agents", "plugins-dist", "raven", "evolver")


def _rollback_producers() -> dict[Path, int]:
    """Files building ``HookDecision(rollback=True)``, mapped to a hit count.

    Tests are excluded: a test fake rolls back to drive the loop and is not a
    contributed hook. Docstrings naming ``rollback=True`` are not calls and so
    never reach here, which is why this is AST and not a grep.
    """
    found: dict[Path, int] = {}
    for root in SEARCH_ROOTS:
        base = REPO / root
        if not base.is_dir():
            continue
        for path in base.rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                if name != "HookDecision":
                    continue
                for kw in node.keywords:
                    if kw.arg == "rollback" and isinstance(kw.value, ast.Constant) and kw.value.value is True:
                        found[path] = found.get(path, 0) + 1
    return found


def _declares_flag(module: Path, class_name: str) -> bool:
    tree = ast.parse(module.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef) or node.name != class_name:
            continue
        for stmt in node.body:
            targets = [stmt.target] if isinstance(stmt, ast.AnnAssign) else getattr(stmt, "targets", [])
            if not any(isinstance(t, ast.Name) and t.id == "rolls_back_iterations" for t in targets):
                continue
            value = stmt.value
            if isinstance(value, ast.Constant) and value.value is True:
                return True
    return False


@pytest.fixture(scope="module")
def producers() -> dict[Path, int]:
    found = _rollback_producers()
    assert found, "no HookDecision(rollback=True) found anywhere -- the AST walk is broken"
    return found


def test_the_walk_sees_the_trees_that_are_known_to_roll_back(producers):
    """Guards the derivation itself: a walk that silently found nothing, or
    stopped seeing a tree, would let every assertion below pass vacuously."""
    roots = {
        root for path in producers for root in REGISTERED_HOOK if str(path.relative_to(REPO)).startswith(root + "/")
    }
    assert roots == set(REGISTERED_HOOK), (
        "the derived producing trees no longer match the mapped ones; "
        f"derived={sorted(roots)} mapped={sorted(REGISTERED_HOOK)}"
    )


def test_every_rollback_lives_in_a_tree_whose_hook_is_mapped(producers):
    """The half that catches a *new* engine: an unmapped tree that rolls back has
    no declaration this file can check, which is exactly the state to refuse."""
    unmapped = sorted(
        str(path.relative_to(REPO))
        for path in producers
        if not any(str(path.relative_to(REPO)).startswith(root + "/") for root in REGISTERED_HOOK)
    )
    assert not unmapped, (
        "these files roll back from a tree with no entry in REGISTERED_HOOK, so "
        "nothing checks that the loop is told to hold their drafts: " + repr(unmapped)
    )


@pytest.mark.parametrize("root", sorted(REGISTERED_HOOK))
def test_a_tree_that_rolls_back_declares_it_on_its_contributed_hook(root):
    rel_module, class_name = REGISTERED_HOOK[root]
    module = REPO / root / rel_module
    assert module.is_file(), f"{root}: {rel_module} is gone -- update REGISTERED_HOOK"
    assert _declares_flag(module, class_name), (
        f"{class_name} ({root}/{rel_module}) can roll back an iteration but does not "
        "declare rolls_back_iterations = True, so the loop streams a draft it may "
        "then retract"
    )
