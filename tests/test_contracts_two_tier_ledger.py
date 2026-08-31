"""Two-tier admission ledger — the S2 guard, prototyped with its own bite-tests.

contracts/ carries two promise tiers (ruled 2026-08-26):
  - contract tier:      frozen for every loop (every paper stamped "contract";
                        the turn contract is spine's, raven/spine/turn.py, and
                        the re-export paper that once stood in for it is gone)
  - factory_loop tier:  versioned with the factory loop (loop_hooks trio); each
                        such module must carry the versioning marker in its
                        docstring so nobody mistakes it for a cross-loop promise.

The checker is written as a pure function over a package directory, so this
file both IS the future guard and PROVES it bites (the bad-fixture tests are
the built-in mutation audit).
"""

from pathlib import Path

FACTORY_MARKER = "Versioned with the factory loop"


def check_ledger(pkg_dir: Path, ledger: dict[str, set[str]]) -> list[str]:
    """Return violations: unlisted exports, unknown tiers, missing markers."""
    import ast

    violations: list[str] = []
    seen: dict[str, set[str]] = {tier: set() for tier in ledger}
    for py in sorted(pkg_dir.glob("*.py")):
        if py.name == "__init__.py":
            continue
        tree = ast.parse(py.read_text())
        tier = None
        exports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == "__tier__":
                        tier = ast.literal_eval(node.value)
                    if isinstance(tgt, ast.Name) and tgt.id == "__all__":
                        exports = set(ast.literal_eval(node.value))
        if tier is None:
            violations.append(f"{py.name}: no __tier__ declared")
            continue
        if tier not in ledger:
            violations.append(f"{py.name}: unknown tier {tier!r}")
            continue
        doc = ast.get_docstring(tree) or ""
        if tier == "factory_loop" and FACTORY_MARKER not in doc:
            violations.append(f"{py.name}: factory_loop module lacks marker {FACTORY_MARKER!r}")
        for sym in exports:
            if sym not in ledger[tier]:
                violations.append(f"{py.name}: {sym} exported but not in the {tier} ledger")
            seen[tier].add(sym)
    for tier, admitted in ledger.items():
        for missing in admitted - seen[tier]:
            violations.append(f"ledger lists {missing} ({tier}) but no module exports it")
    return violations


LEDGER = {
    "contract": {
        # Contract tier: the shapes every shelf implements against.
        "ApprovalResponder",
        "Asker",
        "AssembledContext",
        "TokenBudget",
        "AssembledPrefix",
        "AssemblyContext",
        "Channel",
        "ChannelSpec",
        "CONFIG_FILENAME",
        "DEFAULT_HOME_DIRNAME",
        "HOME_ENV_VAR",
        "WORKSPACE_DEFAULT_SENTINEL",
        "ContextEngine",
        "ContentPart",
        "Continuation",
        "ErrorClassification",
        "FileChange",
        "GenerationSettings",
        "ImagePart",
        "ImageURL",
        "LLMProvider",
        "LLMResponse",
        "Memory",
        "MemoryBackend",
        "ProviderHTTPError",
        "QuestionResponder",
        "RAW_ARGUMENTS_KEY",
        "RunMeta",
        "SKIPPED_AFTER_BLOCKED_CALL",
        "Segment",
        "SegmentBuilder",
        "ChatDelta",
        "SubagentActionAbortedError",
        "SubagentBackend",
        "SubagentNoAnswerError",
        "SupportsApprovalTurn",
        "SupportsDirectAsk",
        "SupportsLogin",
        "TextPart",
        "TokenStrategy",
        "Tool",
        "ToolCallRequest",
        "ToolOutput",
        "ToolResult",
        "TruncationInfo",
        "TurnContext",
        "UsageSnapshot",
        # The plugin contribution surface (plugin_surface.py): shapes a plugin
        # implements against from outside this repository.
        "BindDeclinedError",
        "RuntimeHandles",
        "ServiceLocator",
    },
    "factory_loop": {"AgentHook", "AgentHookContext", "FACTORY_LOOP_SURFACE_VERSION", "HookDecision"},
}

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "raven" / "contracts"


def test_real_contracts_package_passes_the_ledger():
    assert check_ledger(CONTRACTS_DIR, LEDGER) == []


def test_ledger_bites_a_sneaked_symbol(tmp_path):
    import shutil

    work = tmp_path / "contracts"
    shutil.copytree(CONTRACTS_DIR, work, ignore=shutil.ignore_patterns("__pycache__"))
    p = work / "channel.py"
    src = p.read_text()
    assert '"ChannelSpec",' in src
    p.write_text(
        src.replace('"ChannelSpec",', '"ChannelSpec",\n    "SneakedIn",', 1) + "\nclass SneakedIn:\n    pass\n"
    )
    violations = check_ledger(work, LEDGER)
    assert any("SneakedIn" in v for v in violations), violations


def test_ledger_bites_missing_factory_marker(tmp_path):
    import shutil

    work = tmp_path / "contracts"
    shutil.copytree(CONTRACTS_DIR, work, ignore=shutil.ignore_patterns("__pycache__"))
    p = work / "loop_hooks.py"
    p.write_text(p.read_text().replace("Versioned with the factory loop", "versioned, loosely"))
    violations = check_ledger(work, LEDGER)
    assert any("marker" in v for v in violations), violations


# ---------------------------------------------------------------------------
# A paper describes, it does not do: no machinery imports
# ---------------------------------------------------------------------------

# What a paper may import besides the stdlib: pydantic (shapes are models),
# typing_extensions, the other papers, and the spine (L0 shapes such as
# Capabilities). Anything else -- providers, tracing, loguru, a shelf -- is
# machinery, and a paper that needs it has a body that belongs elsewhere.
PAPER_IMPORT_ROOTS = ("raven.contracts", "raven.spine", "pydantic", "typing_extensions")


def check_imports(pkg_dir: Path) -> list[str]:
    """Return every import of machinery in the papers; ``if TYPE_CHECKING:``
    blocks are annotation-only and exempt."""
    import ast
    import sys

    violations: list[str] = []
    for py in sorted(pkg_dir.glob("*.py")):
        tree = ast.parse(py.read_text())
        guarded: set[int] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.If) and isinstance(node.test, ast.Name) and node.test.id == "TYPE_CHECKING":
                guarded.update(id(sub) for sub in ast.walk(node))
        for node in ast.walk(tree):
            if id(node) in guarded:
                continue
            if isinstance(node, ast.Import):
                mods = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            else:
                continue
            for mod in mods:
                if mod.split(".")[0] in sys.stdlib_module_names:
                    continue
                if any(mod == root or mod.startswith(root + ".") for root in PAPER_IMPORT_ROOTS):
                    continue
                violations.append(f"{py.name}:{node.lineno} imports {mod}")
    return violations


def test_papers_import_no_machinery():
    assert check_imports(CONTRACTS_DIR) == []


def test_import_guard_bites_machinery_and_spares_type_checking(tmp_path):
    pkg = tmp_path / "contracts"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("")
    (pkg / "leaky.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "from raven.providers.base import LLMProvider\n"
        "def f():\n"
        "    from loguru import logger\n"
        "if TYPE_CHECKING:\n"
        "    from raven.context_engine.curator import TurnContext\n"
        "__tier__ = 'contract'\n__all__ = []\n"
    )
    got = check_imports(pkg)
    assert got == ["leaky.py:2 imports raven.providers.base", "leaky.py:4 imports loguru"], got


# ---------------------------------------------------------------------------
# The contract tier is versioned: its shape moves only with a version bump
# ---------------------------------------------------------------------------

PINNED_CONTRACT_SURFACE = ("3", "1d14d6c682e77e98c7e2fc23246835af348c376906d61b7a007699e3d4da2455")


def contract_surface_digest(pkg_dir: Path) -> str:
    """Digest of the contract tier's declared surface: exported signatures,
    fields and constants, with prose and function bodies stripped so only a
    shape change moves it."""
    import ast
    import hashlib

    class SurfaceOnly(ast.NodeTransformer):
        def visit_FunctionDef(self, node):
            node.body = [ast.Pass()]
            return node

        visit_AsyncFunctionDef = visit_FunctionDef  # noqa: N815

        def visit_ClassDef(self, node):
            self.generic_visit(node)
            node.body = [
                n for n in node.body if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))
            ] or [ast.Pass()]
            return node

    chunks: list[str] = []
    for py in sorted(pkg_dir.glob("*.py")):
        if py.name == "__init__.py":
            continue
        tree = ast.parse(py.read_text())
        tier = None
        exports: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name) and tgt.id == "__tier__":
                        tier = ast.literal_eval(node.value)
                    if isinstance(tgt, ast.Name) and tgt.id == "__all__":
                        exports = set(ast.literal_eval(node.value))
        if tier != "contract":
            continue
        for node in tree.body:
            name = getattr(node, "name", None)
            if name is None and isinstance(node, ast.Assign) and len(node.targets) == 1:
                tgt = node.targets[0]
                name = tgt.id if isinstance(tgt, ast.Name) else None
            if name is None and isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name = node.target.id
            if name in exports:
                chunks.append(f"{py.name}:{ast.dump(SurfaceOnly().visit(node))}")
    assert chunks, "no contract-tier surface found; a digest of nothing pins nothing"
    return hashlib.sha256("\n".join(chunks).encode()).hexdigest()


def test_the_version_pin_matches_the_papers():
    from raven.contracts import CONTRACTS_VERSION

    version, digest = PINNED_CONTRACT_SURFACE
    assert CONTRACTS_VERSION == version, (
        f"CONTRACTS_VERSION is {CONTRACTS_VERSION!r} but the pin says {version!r}; move them together"
    )
    got = contract_surface_digest(CONTRACTS_DIR)
    assert got == digest, (
        "the contract tier's declared surface moved: bump CONTRACTS_VERSION in "
        "raven/contracts/__init__.py and repin PINNED_CONTRACT_SURFACE to the new "
        f"digest {got!r}. (A digest shift right after a Python upgrade with no "
        "paper edits is the AST render changing, not the papers -- repin without "
        "a bump.)"
    )


def test_the_digest_moves_when_a_signature_changes(tmp_path):
    import shutil

    work = tmp_path / "contracts"
    shutil.copytree(CONTRACTS_DIR, work, ignore=shutil.ignore_patterns("__pycache__"))
    p = work / "token_strategy.py"
    src = p.read_text()
    assert "model: str" in src
    p.write_text(src.replace("model: str", "model_name: str", 1))
    assert contract_surface_digest(work) != contract_surface_digest(CONTRACTS_DIR)


def test_the_digest_ignores_prose(tmp_path):
    import shutil

    work = tmp_path / "contracts"
    shutil.copytree(CONTRACTS_DIR, work, ignore=shutil.ignore_patterns("__pycache__"))
    p = work / "token_strategy.py"
    src = p.read_text()
    marker = "Convention: ``input_tokens`` is *fresh* (non-cached) prompt tokens."
    assert marker in src
    p.write_text(src.replace(marker, "Rule: input tokens count only the fresh prompt.", 1))
    assert contract_surface_digest(work) == contract_surface_digest(CONTRACTS_DIR)
