"""User-facing Chinese lives in ``raven/i18n``; everything else is English source.

``raven.i18n.t`` translates by English source text, so a module that carries
Chinese literals is bypassing the catalog. The list below is the debt: files that
still carry CJK text today. A file that loses its last CJK character has to leave
the list, a file not on it may not gain any, and the list only shrinks.

Two kinds are on the list by design rather than as debt. The onboarding language
picker shows both languages before one is chosen. Model-facing tool descriptions
(the cron tool, the decision router's classifiers, the title and task prompts)
quote a user's request in Chinese next to its English form so the model
recognises either phrasing; those quotes are recognition data, and moving them
into the catalog would show an English user's model only one of the two forms
it has to recognise.
"""

from __future__ import annotations

import re
from pathlib import Path

RAVEN = Path(__file__).resolve().parent.parent / "raven"
CJK = re.compile(r"[\u3400-\u9fff\uff00-\uffef\u3000-\u303f]")

STILL_CARRYING: frozenset[str] = frozenset(
    {
        "raven/cli/onboard_commands.py",
        "raven/core/proactive_stack.py",
        "raven/proactive_engine/schedulers/cron/tool.py",
        "raven/proactive_engine/sentinel/executor/decision_router.py",
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
