"""Independent views: a question on whether something is good is researched beyond its makers.

One recommendation run read eight pages, every one the products' own sites, and
stopped; the comparison arm's "community" section cited six links it never opened.
Pinned here: the contract rule and the reviewer's rubric appear only when the flag is
on, the rule is numbered last, and the rubric leaves the elision carve-out the last word.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.config import FlowConfig  # noqa: E402
from research_flow.gates.verify import _ELISION_RUBRIC, _INDEPENDENT_VIEWS_RUBRIC, DraftReviewerGate  # noqa: E402
from research_flow.prompts import render_identity_and_contract  # noqa: E402


class _Provider:
    async def chat_with_retry(self, **kwargs):  # pragma: no cover - never called here
        raise AssertionError("no call expected")


def test_the_rule_is_said_only_when_asked_and_numbered_last():
    cfg = FlowConfig(enabled=True)
    _, off = render_identity_and_contract(cfg)
    cfg.independent_views = True
    _, on = render_identity_and_contract(cfg)
    assert on.startswith(off.rstrip())
    added = on[len(off.rstrip()) :].strip()
    last = max(int(line.split(".")[0]) for line in off.splitlines() if line[:1].isdigit())
    assert added.startswith(f"{last + 1}. When the question asks whether something is good")
    words = " ".join(added.split())
    assert "the makers' own pages" in words and "rather than filling the gap from search listings" in words


def test_the_reviewer_reads_the_rubric_only_when_asked_and_elision_stays_last():
    off = DraftReviewerGate(_Provider())._reviewer_system
    on = DraftReviewerGate(_Provider(), independent_views=True)._reviewer_system
    assert _INDEPENDENT_VIEWS_RUBRIC not in off and _INDEPENDENT_VIEWS_RUBRIC in on
    assert on.endswith(_ELISION_RUBRIC)
