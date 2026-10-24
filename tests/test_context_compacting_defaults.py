"""Every default the compacting strategy runs on, pinned in one table.

The values are the ported strategy's own; a change to any of them is a
behaviour change and should read as one in review, not as a drive-by edit to
a config class.
"""

from __future__ import annotations

import pytest

from raven.agent.tools import context_retrieve
from raven.config.raven import PRUNE_LINE_MAX_CHARS, ContextConfig
from raven.context_engine import headroom

_CONTEXT = ContextConfig()
_COMPACTING = _CONTEXT.history.compacting

DEFAULTS = [
    ("history.strategy", _CONTEXT.history.strategy, "compacting"),
    ("triggerRatioMax", _COMPACTING.trigger_ratio_max, 0.90),
    ("triggerRatioMin", _COMPACTING.trigger_ratio_min, 0.80),
    ("triggerRefWindow", _COMPACTING.trigger_ref_window, 128_000),
    ("triggerSlope", _COMPACTING.trigger_slope, 0.03),
    ("retainRatio", _COMPACTING.retain_ratio, 0.40),
    ("minReclaimTokens", _COMPACTING.min_reclaim_tokens, 16_384),
    ("headKeepMessages", _COMPACTING.head_keep_messages, 2),
    ("headKeepDecays", _COMPACTING.head_keep_decays, True),
    ("tailKeepMessages", _COMPACTING.tail_keep_messages, 8),
    ("tailKeepRatio", _COMPACTING.tail_keep_ratio, 0.25),
    ("boundary", _COMPACTING.boundary, "tool_pair"),
    ("honorPinned", _COMPACTING.honor_pinned, True),
    ("prune.enabled", _COMPACTING.prune.enabled, True),
    ("prune.minChars", _COMPACTING.prune.min_chars, 2_048),
    ("prune.keepRecentPairs", _COMPACTING.prune.keep_recent_pairs, 3),
    ("prune.dedup", _COMPACTING.prune.dedup, True),
    ("prune.pruneCallArgs", _COMPACTING.prune.prune_call_args, True),
    ("prune.callArgsMaxChars", _COMPACTING.prune.call_args_max_chars, 1_024),
    ("prune line bound", PRUNE_LINE_MAX_CHARS, 160),
    ("summary.mode", _COMPACTING.summary.mode, "lazy"),
    ("summary.model", _COMPACTING.summary.model, ""),
    ("summary.fold", _COMPACTING.summary.fold, True),
    ("summary.foldMaxMessages", _COMPACTING.summary.fold_max_messages, 20),
    ("summary.refsInline", _COMPACTING.summary.refs_inline, True),
    ("summary.maxTokens", _COMPACTING.summary.max_tokens, 4_096),
    ("summary.timeoutSeconds", _COMPACTING.summary.timeout_seconds, 120.0),
    ("summary.maxInputTokens", _COMPACTING.summary.max_input_tokens, 0),
    ("summary.retryOnEmpty", _COMPACTING.summary.retry_on_empty, True),
    ("headroom.minTokens", _CONTEXT.headroom.min_tokens, 4_096),
    ("headroom.overflowBackoffTokens", _CONTEXT.headroom.overflow_backoff_tokens, 8_192),
    ("headroom floor constant", headroom.DEFAULT_MIN_REPLY_HEADROOM_TOKENS, 4_096),
    ("headroom backoff constant", headroom.DEFAULT_OVERFLOW_BACKOFF_TOKENS, 8_192),
    ("cache.turnScratch", _CONTEXT.cache.turn_scratch, True),
    ("cache.compactionBreakpoint", _CONTEXT.cache.compaction_breakpoint, False),
    ("toolResultMaxChars", _CONTEXT.tool_result_max_chars, 1_000_000),
    ("context_retrieve default budget", context_retrieve._DEFAULT_MAX_TOKENS, 4_000),
    ("context_retrieve budget ceiling", context_retrieve._MAX_MAX_TOKENS, 32_000),
    ("context_retrieve refs per call", context_retrieve._MAX_REFS, 20),
]


@pytest.mark.parametrize(("name", "actual", "expected"), DEFAULTS, ids=[row[0] for row in DEFAULTS])
def test_the_default(name: str, actual: object, expected: object) -> None:
    assert actual == expected, name
