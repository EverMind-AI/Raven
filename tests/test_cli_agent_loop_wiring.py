"""Every AgentLoop construction site must hand over the capabilities it owns.

``TokenWiseConfig.enabled``, ``.cache_optimization`` and ``.max_cache_breakpoints``
all defaulted to working values, ``CacheOptimizer`` was complete and benchmarked,
and ``install_from_config`` -- the one function that turns those flags into a
registry -- had no callers. Nothing passed ``strategies=`` to ``AgentLoop``, so
every surface ran the empty pass-through registry and the strategy never
executed once.

The default value was correct, so a test over defaults, a schema assertion or a
config validator cannot catch this: it was a reachability failure, not a
configuration one. The only thing that catches the class is enumerating the
construction sites and requiring each to either wire the capability or say in
writing why it does not.

That is what this file does. It is deliberately source-level, because the
failure mode is the absence of an argument -- which produces no runtime signal
anywhere.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

RAVEN = Path(__file__).resolve().parents[1] / "raven"

# A construction site may skip a capability only with a reason recorded here.
# Adding an entry is a deliberate act; forgetting an argument is not.
_EXEMPT: dict[tuple[str, str], str] = {
    ("trajectory/replay.py", "strategies"): (
        "Replay re-runs a recorded trajectory to compare against what was recorded. "
        "Placing live cache breakpoints would change the request being replayed, so "
        "the comparison would no longer be against the run it is reproducing."
    ),
}

# Each kwarg here carries an operator setting whose absence has no runtime
# symptom, because the loop's own default is a working value.
_REQUIRED = ("strategies",)


def _sites() -> list[tuple[str, ast.Call]]:
    found: list[tuple[str, ast.Call]] = []
    for path in sorted(RAVEN.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "AgentLoop":
                found.append((path.relative_to(RAVEN).as_posix(), node))
    return found


def test_the_enumeration_still_finds_the_construction_sites():
    """The negative assertions below all pass over an empty list."""
    names = {rel for rel, _ in _sites()}
    assert {
        "cli/agent_commands.py",
        "cli/gateway_commands.py",
        "cli/tui_commands.py",
    } <= names, f"a production surface stopped building an AgentLoop by name: {sorted(names)}"


@pytest.mark.parametrize("capability", _REQUIRED)
def test_every_construction_site_wires_every_capability(capability: str):
    missing = [
        rel
        for rel, call in _sites()
        if capability not in {kw.arg for kw in call.keywords} and (rel, capability) not in _EXEMPT
    ]
    assert not missing, (
        f"these AgentLoop sites do not pass {capability!r}: {missing}\n"
        f"Wire it, or add an entry to _EXEMPT with the reason."
    )


def test_the_probe_each_site_passes_reads_the_running_turns_binding():
    """A probe closed over the construction-time provider is wrong per session.

    ``AgentLoop.provider`` is a property over the active binding, so a session
    that switched models runs on a provider the loop was not built with. The
    strategies run inside ``use_binding``, and the answer has to follow them
    there -- otherwise a session switched onto a caching model is told it cannot
    cache, and one switched off it marks a request that cannot carry the field.
    """
    from raven.cli._token_wise_stack import caching_probe
    from raven.providers.binding import ModelBinding, use_binding
    from raven.providers.litellm_provider import LiteLLMProvider

    model = "openrouter/anthropic/claude-fable-5"
    built_with = LiteLLMProvider(api_key="k", default_model=model, provider_name="custom")
    switched_to = LiteLLMProvider(api_key="k", default_model=model, provider_name="openrouter")
    probe = caching_probe(built_with)

    assert probe(model) is False, "outside a turn, the default binding is the only answer"
    with use_binding(ModelBinding(provider=switched_to, model=model)):
        assert probe(model) is True, "inside a turn, the session's own wire decides"


def test_no_production_caller_redirects_usage_telemetry():
    """``settings.usage`` scans exactly one directory: UsageTracker's default.

    A caller passing ``telemetry_dir=`` writes rows that reader never sees --
    shipped once as ``workspace/.token_wise``, where default-on usage tracking
    reported ``calls=0`` in settings. Like the ``strategies=`` check above,
    the defect is an argument with no runtime symptom, so the only thing that
    catches the class is enumerating the call sites.
    """
    offenders = [
        rel for rel, call in _calls("install_from_config") if "telemetry_dir" in {kw.arg for kw in call.keywords}
    ]
    assert not offenders, (
        f"these install_from_config sites redirect usage telemetry away from "
        f"the directory settings.usage reads: {offenders}"
    )


def _calls(func_name: str) -> list[tuple[str, ast.Call]]:
    found: list[tuple[str, ast.Call]] = []
    for path in sorted(RAVEN.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == func_name:
                found.append((path.relative_to(RAVEN).as_posix(), node))
    return found
