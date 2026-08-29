"""User-facing Chinese lives in ``raven/i18n``; everything else is English source.

``raven.i18n.t`` translates by English source text, so a module that carries
Chinese literals is bypassing the catalog. The list below is the debt: files that
still carry CJK text today. A file that loses its last CJK character has to leave
the list, a file not on it may not gain any, and the list only shrinks.
"""

from __future__ import annotations

import re
from pathlib import Path

RAVEN = Path(__file__).resolve().parent.parent / "raven"
CJK = re.compile(r"[\u3400-\u9fff\uff00-\uffef\u3000-\u303f]")

STILL_CARRYING: frozenset[str] = frozenset(
    {
        "raven/agent/tools/tool_search.py",
        "raven/channels/adapters/weixin/channel.py",
        "raven/cli/onboard_commands.py",
        "raven/cli/tui_commands.py",
        "raven/core/proactive_stack.py",
        "raven/importer/scanners/claude_code.py",
        "raven/importer/scanners/hermes.py",
        "raven/memory_engine/consolidate/consolidator.py",
        "raven/proactive_engine/schedulers/cron/tool.py",
        "raven/proactive_engine/sentinel/attention_producers/daily_plan.py",
        "raven/proactive_engine/sentinel/executor/action_executor.py",
        "raven/proactive_engine/sentinel/executor/decision_consumer.py",
        "raven/proactive_engine/sentinel/executor/decision_router.py",
        "raven/proactive_engine/sentinel/executor/dispatcher.py",
        "raven/proactive_engine/sentinel/executor/runner.py",
        "raven/proactive_engine/sentinel/predictor/prompts.py",
        "raven/proactive_engine/sentinel/predictor/task_discoverer.py",
        "raven/proactive_engine/sentinel/trigger_policy/prompts.py",
        "raven/session/title.py",
    }
)


def _carrying() -> set[str]:
    return {
        p.relative_to(RAVEN.parent).as_posix()
        for p in RAVEN.rglob("*.py")
        if not p.relative_to(RAVEN).as_posix().startswith("i18n/") and CJK.search(p.read_text(encoding="utf-8"))
    }


def test_no_module_outside_the_catalog_gains_cjk_text() -> None:
    gained = _carrying() - STILL_CARRYING
    assert gained == set(), f"Chinese text outside raven/i18n; route it through raven.i18n.t: {sorted(gained)}"


def test_the_list_only_shrinks() -> None:
    cleared = STILL_CARRYING - _carrying()
    assert cleared == set(), f"no longer carrying CJK text; remove from STILL_CARRYING: {sorted(cleared)}"
