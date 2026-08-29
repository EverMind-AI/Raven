"""User-facing Chinese lives in ``raven/i18n``; everything else is English source.

``raven.i18n.t`` translates by English source text, so a module that carries
Chinese literals is bypassing the catalog. The list below is the debt: files that
still carry CJK text today. A file that loses its last CJK character has to leave
the list, a file not on it may not gain any, and the list only shrinks.

The three files left on it are exemptions rather than debt, and each is on it
for the same reason: the text has to carry both languages at once, so the
catalog -- which answers in one language at a time -- is the wrong home.

- The onboarding language picker shows both languages before one is chosen.
- The cron tool's description and the decision router's classifier instructions
  quote a user's request in Chinese beside its English form, because the model
  reading them has to recognise either phrasing whatever the user's language is.

Anything else that carries Chinese is debt: route the message through
``raven.i18n.t``, or, if it is language data rather than a message, put it in
``raven.i18n.zh_lexicon`` and read it back.
"""

from __future__ import annotations

import re
from pathlib import Path

RAVEN = Path(__file__).resolve().parent.parent / "raven"
CJK = re.compile(r"[\u3400-\u9fff\uff00-\uffef\u3000-\u303f]")

STILL_CARRYING: frozenset[str] = frozenset(
    {
        "raven/cli/onboard_commands.py",
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
