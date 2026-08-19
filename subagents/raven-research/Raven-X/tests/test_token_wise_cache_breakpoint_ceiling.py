"""Anthropic allows four ephemeral breakpoints per request. Exceeding it is a 400.

Two independent pieces of this tree write ``cache_control``:
``LiteLLMProvider._apply_cache_control`` (system message + last tool) and
``CacheOptimizer`` (tools tail, system tail, and a rolling message tail). They
were never both live, because the strategy stack was never installed, so nothing
ever had to make them stand down for each other.

Now that the stack is wired, the provider's injection is switched off whenever
CacheOptimizer is registered. Measured here: the two writers turn out to be
idempotent with each other, so that standdown is defensive rather than required -
see the test that says so, which was written to assert the opposite.

Everything in this area fails without a local symptom: too many breakpoints is a
400 from a paid endpoint, too few is a larger bill and nothing else. So these
tests count markers on the wire shape rather than trusting any comment, including
the ones this change added.
"""

from __future__ import annotations

import asyncio
from typing import Any

from raven.token_wise.cache_optimizer import CacheOptimizer

_CLAUDE = "anthropic/claude-sonnet-4.5"
_SGLANG = "student"


def _count_markers(messages: list[dict[str, Any]], tools: list[dict] | None) -> int:
    n = 0
    for msg in messages:
        content = msg.get("content")
        if isinstance(content, list):
            n += sum(1 for block in content if isinstance(block, dict) and "cache_control" in block)
    for tool in tools or []:
        if "cache_control" in tool:
            n += 1
    return n


def _conversation(n_messages: int) -> list[dict[str, Any]]:
    msgs: list[dict[str, Any]] = [{"role": "system", "content": "you are a research agent"}]
    for i in range(n_messages):
        msgs.append({"role": "user" if i % 2 == 0 else "assistant", "content": f"m{i}"})
    return msgs


def _run(opt: CacheOptimizer, messages, tools, model):
    return asyncio.run(opt.before_llm_call(messages, tools, model))


def test_the_optimizer_never_exceeds_the_ceiling():
    for n in (1, 2, 5, 40):
        for tools in (None, [{"name": "web_search"}, {"name": "web_fetch"}]):
            msgs, new_tools, _ = _run(CacheOptimizer(), _conversation(n), tools, _CLAUDE)
            used = _count_markers(msgs, new_tools)
            assert used <= 4, f"{used} breakpoints at n={n}, tools={bool(tools)}"


def test_the_two_writers_do_not_stack_so_the_standdown_is_defensive_not_required():
    """Written to prove the opposite of this, and the measurement said otherwise.

    ``PLAN.md`` 133 and the first version of this test both assumed the two writers
    would sum past the ceiling and that ``disable_auto_cache_control`` was therefore
    mandatory. It is not. The provider marks the system message and ``tools[-1]``;
    CacheOptimizer's first two breakpoints are the same two places, and both writers
    mark by replacing a block that may already carry the key, so applying both is
    idempotent and the count stays at four.

    The standdown is kept anyway - one owner for placement, and no redundant
    deepcopy of the tool schemas on every call - but it is defensive, not
    load-bearing, and the comment at the wiring site says so. This test is what
    stops that claim from drifting back: if a future change to either writer makes
    them disjoint, the count rises and this fails.
    """
    tools = [{"name": "web_search"}, {"name": "web_fetch"}]
    msgs, new_tools, _ = _run(CacheOptimizer(), _conversation(6), tools, _CLAUDE)
    assert _count_markers(msgs, new_tools) == 4

    from raven.providers.litellm_provider import LiteLLMProvider

    provider = LiteLLMProvider(api_key="k", default_model=_CLAUDE, provider_name="anthropic")
    both_msgs, both_tools = provider._apply_cache_control(msgs, new_tools)
    assert _count_markers(both_msgs, both_tools) == 4, (
        "the two writers now stack - the standdown has become load-bearing, and the "
        "ceiling is a 400 from a paid endpoint rather than a local failure"
    )


def test_a_non_caching_model_gets_no_markers_at_all():
    """The published arms all run this path: model 'student' on the local SGLang
    endpoint. Wiring the stack must be a measured no-op there, not merely a
    believed one - every reading in the version ladder was taken on it."""
    tools = [{"name": "web_search"}]
    msgs, new_tools, _ = _run(CacheOptimizer(), _conversation(10), tools, _SGLANG)
    assert _count_markers(msgs, new_tools) == 0


def test_the_injected_probe_overrides_the_model_string_in_both_directions():
    """Why the probe exists: the model string cannot see which endpoint terminates.

    ``custom`` is a gateway spec declaring no caching and it is how this build
    reaches SGLang - yet a Claude-looking model string routed through it resolves
    True by model lookup alone. Both directions are asserted because a probe that
    is ignored, and a probe that is always obeyed, fail this test differently.
    """
    tools = [{"name": "web_search"}]

    off = CacheOptimizer(supports_caching=lambda _model: False)
    msgs, new_tools, _ = _run(off, _conversation(6), tools, _CLAUDE)
    assert _count_markers(msgs, new_tools) == 0, "probe said no; markers were written anyway"

    on = CacheOptimizer(supports_caching=lambda _model: True)
    msgs, new_tools, _ = _run(on, _conversation(6), tools, _SGLANG)
    assert _count_markers(msgs, new_tools) == 4, "probe said yes; markers were withheld"


def test_the_rolling_tail_moves_with_the_conversation():
    """The layer-2 defect in one assertion.

    The provider's injection marks the system message and the last tool, so
    ``cached_tokens`` sits at the size of the fixed prefix and never grows - which
    is what the wire capture showed: 1677 on every call of a four-turn exchange.
    A rolling tail must put at least one breakpoint inside the newest part of the
    conversation, so the cached prefix grows as the conversation does.
    """
    tools = [{"name": "web_search"}]
    msgs, _, _ = _run(CacheOptimizer(), _conversation(20), tools, _CLAUDE)

    marked = [
        i
        for i, m in enumerate(msgs)
        if isinstance(m.get("content"), list)
        and any("cache_control" in b for b in m["content"] if isinstance(b, dict))
    ]
    assert marked, "no message-level breakpoint at all - only the fixed prefix is cached"
    assert max(marked) >= len(msgs) - 2, (
        f"newest breakpoint at {max(marked)} of {len(msgs)} messages: the tail is not rolling"
    )
