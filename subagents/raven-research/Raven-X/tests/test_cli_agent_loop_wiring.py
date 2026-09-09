"""Every AgentLoop construction site must hand over the capabilities it owns.

Three defects found within two days of each other in 20260811 share one shape: a
capability that is **on by default** whose activation site is never reached, with
no functional symptom.

1. ``final_shape.process_appendix`` defaulted on, but the trail is computed from
   the client-side ledger and the only writer of ``RAVEN_WEB_LEDGER`` was the
   batch launcher. The one configuration that asked for an appendix was the one
   that could not get one. Symptom: no appendix.
2. ``dr_flow`` was passed only by ``agent_commands``. A gateway session - the
   surface a product integration actually uses - silently ran stock Raven.
   Symptom: none. An answer still comes back.
3. ``TokenWiseConfig.cache_optimization`` defaulted True, ``enabled`` defaulted
   True, ``max_cache_breakpoints`` defaulted 4, and ``install_from_config`` had
   no callers, so ``CacheOptimizer`` never ran. Symptom: the bill, measured at
   4.3x a comparable agent on the same questions.

In all three the *default value was correct*. So a test that checks defaults, or
a config validator, or a schema assertion, cannot catch any of them - they were
all reachability failures, not configuration failures. The only thing that
catches this class is enumerating the construction sites and requiring each one
to either wire the capability or say in writing why it does not.

That is what this file does. It is deliberately a source-level test: the failure
mode is the absence of an argument, which produces no runtime signal anywhere.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

CLI_DIR = Path(__file__).resolve().parents[1] / "raven" / "cli"

# A construction site may skip a capability only with a reason recorded here.
# Adding an entry is a deliberate act; forgetting an argument is not.
_EXEMPT: dict[tuple[str, str], str] = {
    ("tui_commands.py", "dr_flow"): (
        "The TUI front-end is pruned from this build and 'raven tui' fails before "
        "reaching a turn, so wiring the flow here would be untestable."
    ),
    ("tui_commands.py", "strategies"): (
        "Same surface, same reason: with no reachable turn there is no LLM call for a "
        "strategy to place cache breakpoints on, so wiring it would assert nothing."
    ),
}

# The config-driven set: every kwarg here carries an operator setting whose
# absence has no runtime symptom (the loop default is a working value). Found
# the hard way a fourth time on 20260826: the ACP construction site shipped
# without five of these, and the only functional trace was r.jina.ai being
# dialled unauthenticated.
_REQUIRED = (
    "dr_flow",
    "strategies",
    "context_window_authoritative",
    "jina_api_key",
    # The provider selectors and the vendor key map belong to this set for the
    # textbook reason: a site that omits one silently falls back to the default
    # backend, which is a working value and therefore leaves no runtime trace.
    "web_search_provider",
    "web_fetch_provider",
    "web_fetch_fallback",
    "web_provider_keys",
    "disabled_tools",
    "context_config",
    "skill_forge_router_config",
)


def _agent_loop_sites() -> list[tuple[str, int, set[str]]]:
    """Return (filename, lineno, kwarg names) for every ``AgentLoop(...)`` call."""
    sites: list[tuple[str, int, set[str]]] = []
    for path in sorted(CLI_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", None)
            if name != "AgentLoop":
                continue
            sites.append(
                (path.name, node.lineno, {kw.arg for kw in node.keywords if kw.arg})
            )
    return sites


def test_the_enumeration_finds_the_construction_sites_at_all():
    """Guard the guard.

    If the AST walk stops matching - AgentLoop gets aliased, wrapped in a
    factory, or the CLI package is reorganised - every assertion below passes
    over an empty list and this file becomes a green no-op. That is the failure
    mode of the gates this project has had to rewrite most often.
    """
    sites = _agent_loop_sites()
    assert len(sites) >= 3, f"expected the known CLI construction sites, found {sites}"
    assert {s[0] for s in sites} >= {"agent_commands.py", "gateway_commands.py"}


@pytest.mark.parametrize("capability", _REQUIRED)
def test_every_construction_site_wires_the_capability_or_is_exempt(capability):
    missing = [
        (fname, lineno)
        for fname, lineno, kwargs in _agent_loop_sites()
        if capability not in kwargs and (fname, capability) not in _EXEMPT
    ]
    assert not missing, (
        f"{capability} is not passed at {missing}. Either wire it, or add an entry to "
        f"_EXEMPT with the reason. A missing kwarg here has no runtime symptom."
    )


def test_the_exempt_list_cannot_rot_into_a_blanket_pass():
    """An exemption for a site that no longer exists silently widens the gate."""
    present = {(fname, cap) for fname, _, _ in _agent_loop_sites() for cap in _REQUIRED}
    stale = [key for key in _EXEMPT if key not in present]
    assert not stale, f"_EXEMPT names sites that no longer exist: {stale}"

    for key, reason in _EXEMPT.items():
        assert len(reason) > 40, f"{key} needs a real reason, got {reason!r}"


def test_the_dr_surfaces_pass_the_configured_flow_not_a_fresh_default():
    """``dr_flow=DRFlowConfig()`` would satisfy the kwarg check and drop the config.

    The enumeration above only proves an argument is present. This pins the value
    for the two surfaces that must honour the operator's config, so the gate
    cannot be satisfied by wiring a default-constructed object.
    """
    import inspect

    from raven.cli import acp_commands, agent_commands, gateway_commands

    for mod in (agent_commands, gateway_commands):
        src = inspect.getsource(mod)
        assert "dr_flow=ec_config.dr_flow" in src, (
            f"{mod.__name__} must pass the configured flow, not a fresh DRFlowConfig()"
        )

    # The ACP surface resolves the flow per session (raven/acp/modes.py): a mode
    # is the config's own flow with an overlay merged over it, so the literal
    # above is not the spelling there. What still has to hold is the same thing
    # it was pinning -- the configured flow is the source, and no
    # default-constructed one is anywhere on the path.
    acp_src = inspect.getsource(acp_commands)
    assert "ec_config.dr_flow" in acp_src, "acp_commands must derive the flow from the config"
    assert "DRFlowConfig(" not in acp_src, "acp_commands must not construct a flow of its own"
