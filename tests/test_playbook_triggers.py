"""The L1 vocabulary guards, and that no path can write the index around them.

An L1 entry is a standing cost: it is substring-matched against every inbound
message for as long as the playbook exists, and each hit buys a gate call. So
the guards are not advice to the author -- whoever proposes a word (a person, or
the generation model through the emit_playbook schema) goes through them.

CJK fixtures are written as unicode escapes to keep the source ASCII; they pin
that the guards handle Chinese entries (stop words, one-character entries,
full-width folding) with real CJK data.
"""

import pytest

from raven.playbook.triggers import (
    TriggerGuardError,
    find_collisions,
    guard_triggers,
    normalize,
)
from raven.playbook.types import Triggers

CJK_HELP_ME = "\u5e2e\u6211"  # bang wo: "help me" (a stop word)
CJK_BRIEFLY = "\u4e00\u4e0b"  # yi xia: "briefly" (a stop word)
CJK_DE = "\u7684"  # de: possessive particle (one character)
CJK_DUE_DILIGENCE = "\u5c3d\u8c03"  # jin diao: "due diligence"
CJK_ARTICLE = "\u6587\u7ae0"  # wen zhang: "article" (domain-generic)
CJK_SEO_TUNING = "seo \u4f18\u5316"  # seo you hua: "seo tuning"
FULL_WIDTH_SEO = "\uff33\uff25\uff2f"  # full-width letters, NFKC-folds to "seo"


def test_stop_words_are_dropped():
    assert guard_triggers([CJK_HELP_ME, CJK_SEO_TUNING]).keywords == [CJK_SEO_TUNING]
    assert guard_triggers(["help", "post a tweet"]).keywords == ["post a tweet"]


def test_entries_below_the_length_rule_are_dropped():
    # The rule counts characters ignoring spaces, so "a b" is two characters
    # and survives while a single CJK character does not.
    assert guard_triggers([CJK_DE, CJK_DUE_DILIGENCE]).keywords == [CJK_DUE_DILIGENCE]


def test_case_variants_collapse_to_one_entry():
    assert guard_triggers(["SEO", "seo", "Seo"]).keywords == ["seo"]


def test_normalization_is_applied_before_dedup():
    # Full-width and padded forms are the same entry once normalized, so the
    # index cannot end up holding three ways to spell one word.
    assert len(guard_triggers([FULL_WIDTH_SEO, " seo ", "seo"]).keywords) == 1


def test_guards_raise_when_nothing_survives():
    # Reported rather than silently widened: keeping a dropped entry so the
    # playbook still has a vocabulary would reinstate the unfit entry.
    with pytest.raises(TriggerGuardError, match="every trigger candidate was dropped"):
        guard_triggers([CJK_DE, CJK_HELP_ME, CJK_BRIEFLY])


def test_guards_accept_a_triggers_object():
    assert guard_triggers(Triggers(keywords=[CJK_DUE_DILIGENCE, CJK_HELP_ME])).keywords == [CJK_DUE_DILIGENCE]


def test_generic_filter_runs_when_negative_samples_are_given():
    everyday = [
        "is that bug fixed? also forward the article to the team",
        "read the article, let's talk tomorrow",
        "the weekly sync moves to 3pm",
    ]
    # "article" hits 2 of 3 unrelated messages, far above the default rate,
    # while "due diligence" hits none. Without a corpus neither can be
    # judged -- which is why the corpus is the load-bearing part.
    kept = guard_triggers(["article", "due diligence"], negative_samples=everyday).keywords
    assert kept == ["due diligence"]


def test_a_playbook_cannot_collide_with_itself():
    # The report exists so a human can sharpen one side of a conflict; "a
    # collides with a" offers nothing to sharpen and hides the real conflicts.
    assert find_collisions({"a": Triggers(keywords=["seo", "SEO"])}) == {}


def test_cross_playbook_collisions_are_still_reported():
    library = {
        "a": Triggers(keywords=["seo", "indexing"]),
        "b": Triggers(keywords=["SEO", "backlinks"]),
    }
    assert find_collisions(library) == {"seo": ["a", "b"]}


def test_normalize_is_idempotent():
    for raw in (FULL_WIDTH_SEO, " Seo ", "seo"):
        assert normalize(normalize(raw)) == normalize(raw)
