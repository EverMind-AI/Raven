"""The L1 vocabulary guards, and that no path can write the index around them.

An L1 entry is a standing cost: it is substring-matched against every inbound
message for as long as the playbook exists, and each hit buys a gate call. So
the guards are not advice to the author -- whoever proposes a word (a person, or
the generation model through the emit_playbook schema) goes through them.
"""

import pytest

from raven.memory_engine.playbook.triggers import (
    TriggerGuardError,
    find_collisions,
    guard_triggers,
    normalize,
)
from raven.memory_engine.playbook.types import Triggers


def test_stop_words_are_dropped():
    assert guard_triggers(["帮我", "seo 优化"]).keywords == ["seo 优化"]


def test_entries_below_the_length_rule_are_dropped():
    # "的" is one character; the rule counts characters ignoring spaces, so
    # "a b" is two and survives while "的" does not.
    assert guard_triggers(["的", "尽调"]).keywords == ["尽调"]


def test_case_variants_collapse_to_one_entry():
    assert guard_triggers(["SEO", "seo", "Seo"]).keywords == ["seo"]


def test_normalization_is_applied_before_dedup():
    # Full-width and padded forms are the same entry once normalized, so the
    # index cannot end up holding three ways to spell one word.
    assert len(guard_triggers(["ＳＥＯ", " seo ", "seo"]).keywords) == 1


def test_guards_raise_when_nothing_survives():
    # Reported rather than silently widened: keeping a dropped entry so the
    # playbook still has a vocabulary would reinstate the unfit entry.
    with pytest.raises(TriggerGuardError, match="every trigger candidate was dropped"):
        guard_triggers(["的", "帮我", "一下"])


def test_guards_accept_a_triggers_object():
    assert guard_triggers(Triggers(keywords=["尽调", "帮我"])).keywords == ["尽调"]


def test_generic_filter_runs_when_negative_samples_are_given():
    everyday = [
        "这个 bug 修了吗，顺便把文章转给团队",
        "文章我看完了，明天聊",
        "周会挪到下午三点",
    ]
    # "文章" hits 2 of 3 unrelated messages, far above the default rate, while
    # "尽调" hits none. Without a corpus neither can be judged -- which is why
    # the corpus is the load-bearing part of the pipeline.
    kept = guard_triggers(["文章", "尽调"], negative_samples=everyday).keywords
    assert kept == ["尽调"]


def test_a_playbook_cannot_collide_with_itself():
    # The report exists so a human can sharpen one side of a conflict; "a
    # collides with a" offers nothing to sharpen and hides the real conflicts.
    assert find_collisions({"a": Triggers(keywords=["seo", "SEO"])}) == {}


def test_cross_playbook_collisions_are_still_reported():
    library = {
        "a": Triggers(keywords=["seo", "收录"]),
        "b": Triggers(keywords=["SEO", "外链"]),
    }
    assert find_collisions(library) == {"seo": ["a", "b"]}


def test_normalize_is_idempotent():
    for raw in ("ＳＥＯ", " Seo ", "seo"):
        assert normalize(normalize(raw)) == normalize(raw)
