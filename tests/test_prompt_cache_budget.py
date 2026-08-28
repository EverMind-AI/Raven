"""Four breakpoints, one owner.

Anthropic accepts four ``cache_control`` blocks per request. Two components can
place them -- the request builder in ``LiteLLMProvider`` and the ``CacheOptimizer``
strategy -- and their placements overlap but do not coincide: together they come
to five, which the API refuses. So exactly one of them places marks on any given
request, and which one is a fact about the installed config rather than about
whichever provider object the pool happened to build last.

These pin the count, the ownership, and that the owner still covers the stable
head of the system message -- the one mark in the set that survives a turn.
"""

from __future__ import annotations

import pytest

from raven.config.raven import TokenWiseConfig
from raven.providers import prompt_cache
from raven.token_wise.cache_optimizer import CacheOptimizer

MODEL = "openrouter/anthropic/claude-fable-5"
_HEAD = "IDENTITY\n\n---\n\nBOOTSTRAP\n\n---\n\n"
_SYSTEM = _HEAD + "RECALL for this turn"


@pytest.fixture(autouse=True)
def _restore_placement():
    yield
    prompt_cache.set_ttl(None)


def _messages(*, declare_boundary: bool = True) -> list[dict]:
    system: dict = {"role": "system", "content": _SYSTEM}
    if declare_boundary:
        system[prompt_cache.STABLE_PREFIX_KEY] = len(_HEAD)
    return [
        system,
        {"role": "user", "content": "do the thing"},
        {"role": "assistant", "content": "looking"},
        {"role": "user", "content": "result: 42"},
    ]


def _tools() -> list[dict]:
    return [{"type": "function", "function": {"name": n}} for n in ("alpha", "beta")]


def _count(messages, tools) -> int:
    n = 0
    for m in messages:
        content = m.get("content")
        if isinstance(content, list):
            n += sum(1 for b in content if isinstance(b, dict) and "cache_control" in b)
        elif "cache_control" in m:
            n += 1
    return n + sum(1 for t in (tools or []) if "cache_control" in t)


def _provider():
    from raven.providers.litellm_provider import LiteLLMProvider

    return LiteLLMProvider(
        api_key="k", api_base="https://openrouter.ai/api/v1", provider_name="openrouter", default_model=MODEL
    )


class TestTheBudgetHolds:
    async def test_the_strategy_alone_places_four(self):
        messages, tools, _ = await CacheOptimizer(max_breakpoints=4).before_llm_call(_messages(), _tools(), MODEL)

        assert _count(messages, tools) == 4

    def test_the_provider_alone_places_three(self):
        messages, tools = _provider()._apply_cache_control(_messages(), _tools())

        assert _count(messages, tools) == 3

    async def test_together_they_would_exceed_the_limit(self):
        """The reason ownership is a switch and not a preference."""
        messages, tools, _ = await CacheOptimizer(max_breakpoints=4).before_llm_call(_messages(), _tools(), MODEL)

        both, both_tools = _provider()._apply_cache_control(messages, tools)

        assert _count(both, both_tools) == 5, "over Anthropic's limit, which is why only one places marks"


class TestWhoOwnsPlacement:
    """Ownership is a fact about one request, not about the process."""

    async def test_a_caller_with_no_strategy_in_front_of_it_still_gets_marks(self):
        """The regression this replaced a process-wide switch to avoid.

        The Curator, subagents, Sentinel, the session titler and the memory
        consolidator all reach a provider directly. A switch set when the
        strategies were installed silenced the provider for them too, and the
        strategy that was to place the marks instead never sees their requests.
        """
        from raven.cli._token_wise_stack import install_from_config

        install_from_config(TokenWiseConfig(cache_optimization=True))

        messages, tools = _messages(), _tools()
        assert prompt_cache.marks_already_placed(messages) is False
        marked, marked_tools = _provider()._apply_cache_control(messages, tools)

        assert _count(marked, marked_tools) == 3

    async def test_a_request_the_strategy_marked_says_so(self):
        messages, _, _ = await CacheOptimizer(max_breakpoints=4).before_llm_call(_messages(), _tools(), MODEL)

        assert prompt_cache.marks_already_placed(messages) is True

    async def test_a_request_the_strategy_declined_to_mark_does_not(self):
        """A model the strategy will not mark is one the provider still handles."""
        messages, _, _ = await CacheOptimizer(max_breakpoints=4).before_llm_call(
            _messages(), _tools(), "openai/gpt-4o-mini"
        )

        assert prompt_cache.marks_already_placed(messages) is False

    async def test_the_stamp_keeps_the_two_placers_from_summing(self):
        messages, tools, _ = await CacheOptimizer(max_breakpoints=4).before_llm_call(_messages(), _tools(), MODEL)
        before = _count(messages, tools)

        if not prompt_cache.marks_already_placed(messages):  # what the provider asks
            messages, tools = _provider()._apply_cache_control(messages, tools)

        assert _count(messages, tools) == before <= prompt_cache.MAX_BREAKPOINTS

    def test_the_stamp_never_reaches_the_wire(self):
        stamped = prompt_cache.claim_marks(_messages())

        sanitized = _provider()._sanitize_messages(stamped, frozenset())

        assert all(prompt_cache.MARKS_PLACED_KEY not in m for m in sanitized)

    def test_stripping_a_request_also_takes_back_its_claim(self):
        """A request stripped of its breakpoints is not one whose marks are placed."""
        stamped = prompt_cache.claim_marks(_messages())

        stripped, _ = prompt_cache.strip(stamped, None)

        assert prompt_cache.marks_already_placed(stripped) is False


class TestTheOwnerCoversTheStableHead:
    async def test_the_strategy_marks_the_head_not_the_end(self):
        """The head is the only mark here that survives into the next turn."""
        messages, _, _ = await CacheOptimizer(max_breakpoints=4).before_llm_call(_messages(), _tools(), MODEL)

        blocks = messages[0]["content"]
        assert [i for i, b in enumerate(blocks) if "cache_control" in b] == [0]
        assert blocks[0]["text"] == _HEAD
        assert "".join(b["text"] for b in blocks) == _SYSTEM, "the model must read the same bytes"

    async def test_with_no_boundary_declared_it_falls_back_to_the_end(self):
        messages, _, _ = await CacheOptimizer(max_breakpoints=4).before_llm_call(
            _messages(declare_boundary=False), _tools(), MODEL
        )

        blocks = messages[0]["content"]
        assert [i for i, b in enumerate(blocks) if "cache_control" in b] == [len(blocks) - 1]


class TestTheLifetimeArrivesWithTheConfig:
    def test_an_hour_reaches_every_mark(self):
        from raven.cli._token_wise_stack import install_from_config

        install_from_config(TokenWiseConfig(cache_ttl="1h"))

        assert prompt_cache.cache_control() == {"type": "ephemeral", "ttl": "1h"}

    def test_a_lifetime_the_block_cannot_name_costs_the_default_not_the_startup(self):
        """A config surprise must not be what stops the agent coming up."""
        from raven.cli._token_wise_stack import install_from_config

        cfg = TokenWiseConfig()
        object.__setattr__(cfg, "__dict__", {**cfg.__dict__, "cache_ttl": "7 fortnights"})

        install_from_config(cfg)

        assert prompt_cache.cache_control() == {"type": "ephemeral"}


class TestEveryFieldFallsBackRatherThanRaises:
    """Why `_setting` exists, one branch at a time.

    The first version of this wiring let the strategy setters refuse a value
    they did not recognise, and a hundred tests that build their config as a
    `MagicMock` stopped being able to start an agent at all. In production the
    same shape reaches here from a hand-edited file. These fields tune an
    optimisation: a value this layer cannot use has to cost the default.
    """

    @staticmethod
    def _cfg(**overrides):
        cfg = TokenWiseConfig()
        object.__setattr__(cfg, "__dict__", {**cfg.__dict__, **overrides})
        return cfg

    def test_a_value_of_the_wrong_type_takes_the_default(self):
        from raven.cli._token_wise_stack import _setting

        assert _setting(self._cfg(max_cache_breakpoints=object()), "max_cache_breakpoints", 4, int) == 4

    def test_a_true_is_not_a_count_of_one(self):
        """`bool` is an `int`, so an unguarded numeric check would take it."""
        from raven.cli._token_wise_stack import _setting

        assert _setting(self._cfg(max_cache_breakpoints=True), "max_cache_breakpoints", 4, int) == 4

    def test_a_value_below_the_floor_takes_the_default(self):
        from raven.cli._token_wise_stack import _setting

        assert _setting(self._cfg(max_cache_breakpoints=0), "max_cache_breakpoints", 4, int, floor=1) == 4

    def test_a_field_that_is_not_there_takes_the_default(self):
        from raven.cli._token_wise_stack import _setting

        assert _setting(object(), "cache_ttl", "5m", str, allowed=("5m", "1h")) == "5m"

    def test_a_usable_value_survives(self):
        from raven.cli._token_wise_stack import _setting

        assert _setting(self._cfg(max_cache_breakpoints=2), "max_cache_breakpoints", 4, int, floor=1) == 2

    def test_a_breakpoint_count_nobody_can_use_still_installs_the_strategy(self):
        """The optimisation degrades; the agent does not fail to come up."""
        from raven.cli._token_wise_stack import install_from_config

        registry = install_from_config(self._cfg(max_cache_breakpoints=0))

        assert any(s.name == "cache_optimizer" for s in registry.strategies)


class TestTheCeilingTheVendorSets:
    """`maxCacheBreakpoints` above 4 was inert until this config got callers."""

    def _cfg(self, **kw):
        return TokenWiseConfig(**kw)

    @pytest.mark.parametrize("configured", [5, 8, 99])
    def test_a_count_over_the_limit_falls_back_to_the_limit(self, configured):
        from raven.cli._token_wise_stack import install_from_config

        registry = install_from_config(self._cfg(max_cache_breakpoints=configured))

        optimizer = next(s for s in registry.strategies if s.name == "cache_optimizer")
        assert optimizer.max_breakpoints == prompt_cache.MAX_BREAKPOINTS

    def test_the_limit_itself_is_kept(self):
        from raven.cli._token_wise_stack import install_from_config

        registry = install_from_config(self._cfg(max_cache_breakpoints=4))

        optimizer = next(s for s in registry.strategies if s.name == "cache_optimizer")
        assert optimizer.max_breakpoints == 4

    async def test_no_configured_count_can_put_a_fifth_mark_on_the_wire(self):
        """The vendor refuses a fifth outright; nothing below may produce one."""
        from raven.cli._token_wise_stack import install_from_config

        registry = install_from_config(self._cfg(max_cache_breakpoints=99))
        messages, tools, _ = await registry.before_llm_call(_messages(), _tools(), MODEL)

        assert _count(messages, tools) <= prompt_cache.MAX_BREAKPOINTS


class TestStripPutsTheSplitBack:
    """A wire that cannot carry the marks cannot carry the shape either."""

    def test_the_stable_prefix_split_becomes_the_string_it_came_from(self):
        split = [
            {"type": "text", "text": _HEAD, "cache_control": {"type": "ephemeral"}},
            {"type": "text", "text": "RECALL for this turn"},
        ]
        message = {"role": "system", "content": split, prompt_cache.STABLE_PREFIX_KEY: len(_HEAD)}

        stripped, _ = prompt_cache.strip([message], None)

        assert stripped[0]["content"] == _SYSTEM

    def test_a_split_that_no_longer_declares_its_boundary_is_left_alone(self):
        """Without the boundary nothing says the two blocks were ever one."""
        split = [{"type": "text", "text": _HEAD}, {"type": "text", "text": "RECALL for this turn"}]

        stripped, _ = prompt_cache.strip([{"role": "system", "content": split}], None)

        assert stripped[0]["content"] == split

    def test_a_boundary_that_does_not_land_on_the_seam_is_left_alone(self):
        split = [{"type": "text", "text": _HEAD}, {"type": "text", "text": "RECALL for this turn"}]
        message = {"role": "system", "content": split, prompt_cache.STABLE_PREFIX_KEY: len(_HEAD) - 1}

        stripped, _ = prompt_cache.strip([message], None)

        assert stripped[0]["content"] == split


class TestTheLifetimeIsPricedAsItIsBilled:
    """`cacheTtl: 1h` costs more to write; a cost estimate has to say so."""

    def test_the_default_lifetime_writes_at_the_measured_rate(self):
        prompt_cache.set_ttl(None)

        assert prompt_cache.write_rate_multiplier() == 1.25

    def test_the_hour_writes_at_its_own(self):
        prompt_cache.set_ttl("1h")

        assert prompt_cache.write_rate_multiplier() == 2.0

    def test_an_hour_long_write_costs_more_than_a_five_minute_one(self):
        from raven.token_wise.pricing import estimate_cost_usd

        prompt_cache.set_ttl(None)
        cheap = estimate_cost_usd(MODEL, 0, 0, cache_write_tokens=100_000)
        prompt_cache.set_ttl("1h")
        dear = estimate_cost_usd(MODEL, 0, 0, cache_write_tokens=100_000)

        assert cheap is not None and dear is not None
        assert dear == pytest.approx(cheap * (2.0 / 1.25))
