"""``contextWindowAuthoritative``: the configured window wins, both directions.

The knob exists because one field served two intents. Off (the default) the
model catalog beats the configured number and the config is a *fallback*; on,
the operator's number is a *declaration*. The failure of not having it is not a
mislabelled field: on ``eval_web_dr33_dsv4f_20260817`` the catalog resolved 1M,
so ``_fit_request`` returned at its first branch on all 3,484 arm-items, all
five overflow counters read 0, and overflow prevention - the flow's primary
mechanism since dr@1.4 - could not fire at all.

The trap this file also pins: ``deepseek/deepseek-v4-flash-0731`` is currently
*unknown* to the catalog while the un-suffixed id resolves to 1,000,000. So a
config naming the suffixed id gets its declared window honoured **by the
catalog's ignorance**, and would silently lose it the day the catalog learns the
id. ``test_the_suffixed_id_must_not_rely_on_catalog_ignorance`` is there so that
day is a red test rather than a working point that quietly moved.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.agent.loop.main import AgentLoop
from raven.config import load_config

_CONFIGS = Path(__file__).resolve().parents[2] / "pipeline" / "configs"


def _window(model: str, configured: int, authoritative: bool) -> int:
    """Reproduce __init__'s resolution without building a whole loop."""
    from raven.providers.rates import resolve_context_window

    loop = AgentLoop.__new__(AgentLoop)
    loop.model = model
    loop._window_is_authoritative = authoritative
    loop.context_window_tokens = (
        configured if authoritative else (resolve_context_window(model) or configured)
    )
    return loop.context_window_tokens


def test_off_the_catalog_still_wins():
    """The default must stay byte-identical to the old behaviour."""
    from raven.providers.rates import resolve_context_window

    known = "deepseek/deepseek-v4-flash"
    catalog = resolve_context_window(known)
    if not catalog:
        pytest.skip("catalog unavailable; this assertion needs a resolvable id")
    assert _window(known, 131_072, authoritative=False) == catalog
    assert catalog != 131_072


def test_on_the_configured_number_wins():
    from raven.providers.rates import resolve_context_window

    known = "deepseek/deepseek-v4-flash"
    if not resolve_context_window(known):
        pytest.skip("catalog unavailable; this assertion needs a resolvable id")
    assert _window(known, 131_072, authoritative=True) == 131_072


def test_window_for_agrees_with_the_pin():
    """The per-call override path must not read the catalog behind the pin's
    back: the trimmer reading one number while the budget note reads another is
    the 'N copies of one number' divergence __init__ folded away."""
    loop = AgentLoop.__new__(AgentLoop)
    loop.model = "deepseek/deepseek-v4-flash"
    loop._window_is_authoritative = True
    loop.context_window_tokens = 131_072
    assert AgentLoop._window_for(loop) == 131_072
    assert AgentLoop._window_for(loop, "anthropic/claude-opus-5") == 131_072


def test_missing_attribute_falls_back_not_crashes():
    """``_window_for`` is reached by objects built in tests and older paths that
    never set the attribute. Absent must read as 'not authoritative', never as an
    AttributeError inside a hot loop."""
    loop = AgentLoop.__new__(AgentLoop)
    loop.model = "student"
    loop.context_window_tokens = 65_536
    assert AgentLoop._window_for(loop) == 65_536


@pytest.mark.parametrize("name", ["dr34_web_dsv4f0731_base128", "dr34_web_dsv4f0731_dr128"])
def test_the_launch_pair_pins_the_same_window_on_both_arms(name):
    """A window that differs across arms is an arm-correlated variable, which is
    why this pin lives on agents.defaults and not on drFlow: the latter cannot
    reach the anchor at all."""
    p = _CONFIGS / f"{name}.json"
    if not p.exists():
        pytest.skip("launch pair not present in this checkout")
    cfg = load_config(p)
    assert cfg.agents.defaults.context_window_authoritative is True
    assert cfg.agents.defaults.context_window_tokens == 131_072
    assert cfg.agents.defaults.model == "deepseek/deepseek-v4-flash-0731"


def test_the_suffixed_id_must_not_rely_on_catalog_ignorance():
    """If this fails, the catalog learned the -0731 id. That is fine - the pin
    still holds because contextWindowAuthoritative is set - but the note in this
    module about 'honoured by ignorance' is then stale and should be deleted
    rather than left as a claim nobody rechecked."""
    from raven.providers.rates import resolve_context_window

    resolved = resolve_context_window("deepseek/deepseek-v4-flash-0731")
    assert _window("deepseek/deepseek-v4-flash-0731", 131_072, authoritative=True) == 131_072
    if resolved:
        pytest.fail(
            f"catalog now resolves -0731 to {resolved}; update this module's docstring "
            "(the pin is unaffected, the explanation is not)"
        )


def test_the_launch_pair_pins_one_upstream_and_forbids_fallback():
    """Each OpenRouter upstream holds its own prompt cache, so an unpinned
    policy shows up as cache_read collapses with no local cause. Measured on the
    dsv4f batch: collapses accounted for 87.7% of the anchor's wasted prompt
    tokens and 62.2% of the treated arm's, at a 14.7s / 21.8s median gap - far
    too short to be idle expiry.

    ``streamlake/fp8``. NOT the first-party ``deepseek`` endpoint: it returns
    404 "No endpoints found" for this account under every name form tried. The
    pick between third-party fp8 upstreams is thin and the measurement said so:
    at realistic prefix sizes with a unique per-run nonce, twelve calls each,
    StreamLake ran 0 81 84 86 0 78 80 82 84 85 93 93 and DeepInfra ran
    0 81 84 86 88 89 90 91 92 0 0 93 - both reach ~93%, both collapse once,
    StreamLake has one warm zero-hit against DeepInfra's two. Within noise at
    n=12; StreamLake also happens to be the upstream this tree's own routing
    note names. The previous config excluded deepinfra for a reason nobody
    recorded, which is a second, weaker argument for not picking it.

    ⚠️ What the same measurement REFUTES: pinning does not remove collapses.
    Pinned collapse rate ran ~1 in 12 calls (~8%), against 12.7% (treated) and
    6.2% (anchor) measured UNPINNED on eval_web_dr33_dsv4f_20260817 - the same
    order of magnitude. An OpenRouter provider pin does not pin the upstream's
    own replica, and each replica holds its own cache. So this pin is worth
    having because it removes a variable and makes ``serving_upstream``
    checkable, NOT because a saving has been established. Do not budget on
    one.

    ★ 20260825: this test used to assert ``order == ["deepinfra/fp8"]`` - a single
    named vendor. Both halves were wrong. The vendor was stale (the decision above
    says StreamLake; the assertion still named the one it had moved off), and the
    shape was wrong: the batch shipped ``["streamlake/fp8", "gmicloud/fp8",
    "deepinfra/fp8"]`` - an ordered preference list with fallbacks off - because a
    single upstream could not carry the concurrency. A test that hard-codes an
    operational choice goes red on every legitimate change of it, and a gate that
    cries wolf trains everyone to ignore its red. So this now asserts the
    INVARIANTS, and the vendor list stays an operations decision:

      1. ``allow_fallbacks`` is off - routing stays inside the named list.
      2. The two arms' routing is byte-identical. This is the load-bearing one:
         a different upstream per arm is an arm-correlated variable with no
         symptom, and each upstream holds its own prompt cache.
      3. The order is explicit and non-empty - never ``auto``.

    ⚠️ And note what the widening costs, since nothing else records it: with three
    upstreams and ``serving_upstream`` never having written a row (0 out of 67,693
    before 2026-08-25), the dr@3.4 batch cannot say which upstream served any call.
    The pin's stated purpose - remove a variable, make the upstream checkable -
    was not achieved on that batch by either half.
    """
    routings = {}
    for name in ("dr34_web_dsv4f0731_base128", "dr34_web_dsv4f0731_dr128"):
        p = _CONFIGS / f"{name}.json"
        if not p.exists():
            pytest.skip("launch pair not present in this checkout")
        routing = (json.loads(p.read_text())["providers"]["openrouter"] or {})["routing"]
        assert routing["allow_fallbacks"] is False, f"{name}: fallbacks must stay off"
        order = routing["order"]
        assert isinstance(order, list) and order, f"{name}: order must be explicit, never auto"
        routings[name] = routing
    a, b = routings.values()
    assert a == b, "both arms must route identically - a per-arm upstream is arm-correlated"
