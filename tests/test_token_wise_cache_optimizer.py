"""Tests for raven.token_wise.cache_optimizer.CacheOptimizer."""

from __future__ import annotations

import copy

import pytest

from raven.token_wise.cache_optimizer import CacheOptimizer

# Anthropic models support cache_control per the provider registry.
ANTHROPIC_MODEL = "anthropic/claude-sonnet-4-5"
# A model that does NOT support prompt caching → strategy must be a no-op.
NON_CACHE_MODEL = "deepseek/deepseek-chat"


def _system_msg(text: str = "You are helpful.") -> dict:
    return {"role": "system", "content": text}


def _user_msg(text: str = "hello") -> dict:
    return {"role": "user", "content": text}


def _assistant_msg(text: str = "hi back") -> dict:
    return {"role": "assistant", "content": text}


def _tool(name: str = "search") -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": "x",
            "parameters": {"type": "object", "properties": {}},
        },
    }


def _has_cache_control(block: dict) -> bool:
    return isinstance(block, dict) and block.get("cache_control") == {"type": "ephemeral"}


def _content_blocks_with_cache(msg: dict) -> int:
    content = msg.get("content")
    if isinstance(content, list):
        return sum(1 for b in content if _has_cache_control(b))
    return 0


def _count_breakpoints(messages: list[dict], tools: list[dict] | None) -> int:
    n = sum(_content_blocks_with_cache(m) for m in messages)
    if tools:
        n += sum(1 for t in tools if _has_cache_control(t))
    return n


# -----------------------------------------------------------------------------
# Behaviour
# -----------------------------------------------------------------------------


def test_constructor_validates_max_breakpoints():
    with pytest.raises(ValueError):
        CacheOptimizer(max_breakpoints=0)


async def test_noop_for_non_cache_models():
    opt = CacheOptimizer()
    msgs = [_system_msg(), _user_msg()]
    tools = [_tool()]
    out_m, out_t, out_model = await opt.before_llm_call(msgs, tools, NON_CACHE_MODEL)
    assert out_m is msgs
    assert out_t is tools
    assert out_model == NON_CACHE_MODEL


async def test_noop_when_model_empty():
    opt = CacheOptimizer()
    msgs = [_user_msg()]
    out_m, _, _ = await opt.before_llm_call(msgs, None, "")
    assert out_m is msgs


async def test_marks_tools_and_system_for_cache_capable_model():
    opt = CacheOptimizer()
    msgs = [_system_msg(), _user_msg()]
    tools = [_tool("a"), _tool("b")]
    out_m, out_t, _ = await opt.before_llm_call(msgs, tools, ANTHROPIC_MODEL)

    # Last tool marked.
    assert _has_cache_control(out_t[-1])
    # Earlier tool NOT marked.
    assert not _has_cache_control(out_t[0])
    # System content has cache_control on a content block.
    sys_msg = next(m for m in out_m if m["role"] == "system")
    assert _content_blocks_with_cache(sys_msg) == 1


async def test_does_not_mutate_inputs():
    """Original messages and tools must remain pristine."""
    opt = CacheOptimizer()
    msgs = [_system_msg(), _user_msg(), _assistant_msg(), _user_msg("again")]
    tools = [_tool("a"), _tool("b")]
    msgs_before = copy.deepcopy(msgs)
    tools_before = copy.deepcopy(tools)

    await opt.before_llm_call(msgs, tools, ANTHROPIC_MODEL)

    assert msgs == msgs_before, "input messages were mutated"
    assert tools == tools_before, "input tools were mutated"


async def test_string_content_is_wrapped_into_text_block():
    opt = CacheOptimizer()
    msgs = [_system_msg("plain string system")]
    out_m, _, _ = await opt.before_llm_call(msgs, None, ANTHROPIC_MODEL)
    sys = out_m[0]
    assert isinstance(sys["content"], list)
    assert sys["content"][-1]["type"] == "text"
    assert sys["content"][-1]["text"] == "plain string system"
    assert sys["content"][-1]["cache_control"] == {"type": "ephemeral"}


async def test_list_content_marks_only_last_block():
    opt = CacheOptimizer()
    msgs = [{
        "role": "system",
        "content": [
            {"type": "text", "text": "first"},
            {"type": "text", "text": "second"},
            {"type": "text", "text": "third"},
        ],
    }]
    out_m, _, _ = await opt.before_llm_call(msgs, None, ANTHROPIC_MODEL)
    sys = out_m[0]
    blocks = sys["content"]
    assert "cache_control" not in blocks[0]
    assert "cache_control" not in blocks[1]
    assert _has_cache_control(blocks[2])


async def test_long_history_uses_more_breakpoints():
    """A long conversation should consume up to 4 breakpoints."""
    opt = CacheOptimizer(max_breakpoints=4)
    msgs = [_system_msg()]
    # 5 prior turns to give the optimizer somewhere to place breakpoints.
    for i in range(5):
        msgs.append(_user_msg(f"q{i}"))
        msgs.append(_assistant_msg(f"a{i}"))
    msgs.append(_user_msg("current question"))
    tools = [_tool()]
    out_m, out_t, _ = await opt.before_llm_call(msgs, tools, ANTHROPIC_MODEL)
    n = _count_breakpoints(out_m, out_t)
    # tools(1) + system(1) + before-current-user(1) + mid(1) = 4
    assert n == 4


async def test_max_breakpoints_is_respected():
    """With max_breakpoints=2 we never place more than 2 markers."""
    opt = CacheOptimizer(max_breakpoints=2)
    msgs = [_system_msg()]
    for i in range(5):
        msgs.append(_user_msg(f"q{i}"))
        msgs.append(_assistant_msg(f"a{i}"))
    msgs.append(_user_msg("current"))
    tools = [_tool()]
    out_m, out_t, _ = await opt.before_llm_call(msgs, tools, ANTHROPIC_MODEL)
    n = _count_breakpoints(out_m, out_t)
    # With budget=2 and tools present: tools(1) + system(1) = 2, no room for rolling tail.
    assert n == 2


async def test_short_conversation_uses_fewer_breakpoints():
    """[system, user] with no tools → system + user (rolling tail marks the user msg too)."""
    opt = CacheOptimizer(max_breakpoints=4)
    msgs = [_system_msg(), _user_msg("hi")]
    out_m, out_t, _ = await opt.before_llm_call(msgs, None, ANTHROPIC_MODEL)
    # No tools: system(1) + rolling tail marks the one non-system msg(1) = 2
    n = _count_breakpoints(out_m, out_t)
    assert n == 2


async def test_no_system_no_tools_no_history():
    """Edge case: just one user message. Rolling tail marks it for next-call cache."""
    opt = CacheOptimizer()
    msgs = [_user_msg("hi")]
    out_m, out_t, _ = await opt.before_llm_call(msgs, None, ANTHROPIC_MODEL)
    # One non-system msg → 1 rolling breakpoint (prepares cache for next iteration).
    assert _count_breakpoints(out_m, out_t) == 1


async def test_only_tools_no_messages():
    opt = CacheOptimizer()
    tools = [_tool()]
    out_m, out_t, _ = await opt.before_llm_call([], tools, ANTHROPIC_MODEL)
    assert out_m == []
    assert _has_cache_control(out_t[-1])


async def test_tools_unchanged_when_no_tools_supplied():
    opt = CacheOptimizer()
    msgs = [_system_msg(), _user_msg()]
    out_m, out_t, _ = await opt.before_llm_call(msgs, None, ANTHROPIC_MODEL)
    assert out_t is None


async def test_idempotent_repeated_application():
    """Running the optimizer twice must not duplicate breakpoints."""
    opt = CacheOptimizer()
    msgs = [_system_msg(), _user_msg("hi")]
    once_m, _, _ = await opt.before_llm_call(msgs, None, ANTHROPIC_MODEL)
    twice_m, _, _ = await opt.before_llm_call(once_m, None, ANTHROPIC_MODEL)
    # Same cache count; the marker is overwritten not duplicated.
    assert _count_breakpoints(once_m, None) == _count_breakpoints(twice_m, None)
