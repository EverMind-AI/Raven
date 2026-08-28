"""Two-tier admission ledger — the S2 guard, prototyped with its own bite-tests.

contracts/ will carry two promise tiers (ruled 2026-08-26):
  - contract tier:      frozen for every loop (turn contract + the seven shapes)
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
        "TurnRequest", "TurnHandle", "Origin",
        # The seven shapes, moved in whole by the S2 full move:
        "AssembledPrefix",
        "AssemblyContext",
        "Channel",
        "ChannelSpec",
        "ContextEngine",
        "Continuation",
        "ErrorClassification",
        "FileChange",
        "GenerationSettings",
        "LLMProvider",
        "LLMResponse",
        "Memory",
        "MemoryBackend",
        "ProviderHTTPError",
        "RunMeta",
        "SKIPPED_AFTER_BLOCKED_CALL",
        "Segment",
        "SegmentBuilder",
        "StreamDelta",
        "SubagentActionAbortedError",
        "SubagentBackend",
        "SubagentNoAnswerError",
        "SupportsLogin",
        "TokenStrategy",
        "Tool",
        "ToolCallRequest",
        "ToolOutput",
        "ToolResult",
        "TruncationInfo",
        "UsageSnapshot",
        "capability_violations",
        "format_llm_error",
        "parse_llm_error",
        "send_max_tokens",
    },
    "factory_loop": {"AgentHook", "AgentHookContext", "HookDecision"},
}

CONTRACTS_DIR = Path(__file__).resolve().parent.parent / "raven" / "contracts"


def test_real_contracts_package_passes_the_ledger():
    assert check_ledger(CONTRACTS_DIR, LEDGER) == []


def test_ledger_bites_a_sneaked_symbol(tmp_path):
    import shutil

    work = tmp_path / "contracts"
    shutil.copytree(CONTRACTS_DIR, work, ignore=shutil.ignore_patterns("__pycache__"))
    p = work / "turn.py"
    p.write_text(p.read_text().replace(
        '__all__ = ["TurnRequest", "TurnHandle", "Origin"]',
        '__all__ = ["TurnRequest", "TurnHandle", "Origin", "SneakedIn"]',
    ) + "\nclass SneakedIn:\n    pass\n")
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
