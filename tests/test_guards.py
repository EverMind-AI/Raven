"""Code-layer guards.

Each guard is a pure function isolated from MemoryStore. These tests
pin the contract so prompt-side regressions or stricter rules don't
silently change behavior at 30-day scale.
"""

from __future__ import annotations

from raven.memory_engine.consolidate.consolidator import (
    _PROCESS_TAGS,
    _VALID_CONFIDENCE,
    _drop_bullets_without_src,
    _foresight_token_set,
    _format_foresight_bullet,
    _is_process_only_episode,
    _is_semantic_duplicate_foresight,
    _normalize_confidence,
)

# ---------- _is_process_only_episode --------------------------------


def test_process_only_question_tag_alone_rejected():
    line = "[2026-05-12 14:00] which framework for new dashboard #question"
    assert _is_process_only_episode(line) is True


def test_process_only_habit_tag_alone_rejected():
    line = "[2026-05-12 07:30] morning routine recap #habit"
    assert _is_process_only_episode(line) is True


def test_process_only_answer_tag_alone_rejected():
    line = "[2026-05-12 09:00] explained the concept of CAP #answer"
    assert _is_process_only_episode(line) is True


def test_question_with_content_tag_kept():
    line = "[2026-05-12 14:00] which framework #question #project-dashboard"
    assert _is_process_only_episode(line) is False


def test_pure_content_tag_kept():
    line = "[2026-05-12 14:00] merged PR #142 #project-clawtrack #pr"
    assert _is_process_only_episode(line) is False


def test_episode_without_tags_kept():
    line = "[2026-05-12 14:00] free-form note without any tags"
    assert _is_process_only_episode(line) is False


def test_unparseable_line_kept():
    # No timestamp prefix — _parse_episode_line returns None.
    # Guard returns False so we don't drop unrelated freeform input.
    assert _is_process_only_episode("just a stray string with #habit") is False


def test_empty_input_kept():
    assert _is_process_only_episode("") is False


def test_uppercase_tag_not_recognized_as_tag():
    # ``_TAG_RE`` only matches lowercase kebab-case slugs (system spec).
    # ``#QUESTION`` thus produces zero parsed tags → guard treats the line
    # as untagged free-form input and returns False (lets it through).
    line = "[2026-05-12 14:00] uppercase tag test #QUESTION"
    assert _is_process_only_episode(line) is False


def test_multiple_process_tags_only_still_rejected():
    line = "[2026-05-12 14:00] mixed process tags #question #habit"
    assert _is_process_only_episode(line) is True


# ---------- _normalize_confidence -----------------------------------


def test_confidence_low_valid():
    assert _normalize_confidence("low") == "low"


def test_confidence_medium_valid():
    assert _normalize_confidence("medium") == "medium"


def test_confidence_high_valid():
    assert _normalize_confidence("high") == "high"


def test_confidence_case_insensitive():
    assert _normalize_confidence("LOW") == "low"
    assert _normalize_confidence("Medium") == "medium"


def test_confidence_strips_whitespace():
    assert _normalize_confidence("  high  ") == "high"


def test_confidence_invalid_strong_rendered_unknown():
    assert _normalize_confidence("strong") == "?"


def test_confidence_invalid_likely_rendered_unknown():
    assert _normalize_confidence("likely") == "?"


def test_confidence_empty_rendered_unknown():
    assert _normalize_confidence("") == "?"


def test_format_foresight_bullet_normalizes_bad_confidence():
    bullet = _format_foresight_bullet(
        {
            "prediction": "User will deploy on Friday",
            "window": "1-3 days",
            "confidence": "strong",
            "src_ts": "2026-05-10 14:00",
        },
        generation_ts="2026-05-10 21:00",
    )
    assert "confidence: ?" in bullet
    assert "strong" not in bullet


def test_format_foresight_bullet_keeps_valid_confidence():
    bullet = _format_foresight_bullet(
        {
            "prediction": "User will deploy on Friday",
            "window": "1-3 days",
            "confidence": "medium",
            "src_ts": "2026-05-10 14:00",
        },
        generation_ts="2026-05-10 21:00",
    )
    assert "confidence: medium" in bullet


# ---------- _foresight_token_set ------------------------------------


def test_token_set_lowercases_and_drops_short_words():
    tokens = _foresight_token_set("User runs every Saturday morning")
    # "user" is stopword; "runs" is 4 chars (included).
    assert "runs" in tokens
    assert "saturday" in tokens
    assert "morning" in tokens
    assert "user" not in tokens


def test_token_set_drops_framing_stopwords_and_s_stems():
    tokens = _foresight_token_set("User will continue daily medication reminders")
    assert "user" not in tokens
    assert "will" not in tokens
    assert "continue" not in tokens
    assert "daily" in tokens
    assert "medication" in tokens
    # ``reminders`` gets s-stemmed to ``reminder`` for plural/singular
    # collapse (see ``_stem_trailing_s``).
    assert "reminder" in tokens
    assert "reminders" not in tokens


def test_token_set_empty_for_pure_stopword_text():
    tokens = _foresight_token_set("user will continue tomorrow")
    assert tokens == frozenset()


# ---------- _is_semantic_duplicate_foresight ------------------------


def test_semantic_dup_reworded_saturday_run():
    new = "User runs every Saturday morning (recurring habit pattern)"
    existing = ["User runs every Saturday morning (recurring habit)"]
    assert _is_semantic_duplicate_foresight(new, existing) is True


def test_semantic_dup_caregiver_medication_cluster():
    # Real longrun pattern (caregiver-01, day 20 vs day 22): same claim
    # reworded slightly. With s-stemming and Jaccard ≥ 0.6 the dedup
    # collapses them.
    new = "User will set daily medication reminders for mom (amlodipine, donepezil, metoprolol) at similar times"
    existing = ["Daily medication reminders for mom (amlodipine, donepezil, metoprolol) - recurring care routine"]
    assert _is_semantic_duplicate_foresight(new, existing) is True


def test_semantic_dup_misses_heavy_morphology():
    # Documented limitation: ``remind`` vs ``reminders`` differ even
    # after s-stem (remind has no trailing s; reminders → reminder).
    # Without a real stemmer we accept this false negative; the data
    # showed near-identical wording dominates the dup cluster anyway.
    new = "User will remind mom about daily medications"
    existing = ["Daily medication reminders for mom"]
    assert _is_semantic_duplicate_foresight(new, existing) is False


def test_semantic_dup_unrelated_predictions_distinct():
    new = "User will release clawtrack v1.0 after final testing"
    existing = [
        "User runs every Saturday morning (recurring habit)",
        "User will attend NeurIPS workshop submission deadline next week",
    ]
    assert _is_semantic_duplicate_foresight(new, existing) is False


def test_semantic_dup_empty_existing_returns_false():
    assert _is_semantic_duplicate_foresight("anything", []) is False


def test_semantic_dup_empty_new_returns_false():
    assert _is_semantic_duplicate_foresight("", ["something"]) is False


def test_semantic_dup_blocks_within_batch():
    # Simulate caller appending duplicates one at a time to existing.
    existing: list[str] = []
    first = "User attends weekly boss meetings on Mondays"
    second = "User attends weekly Monday boss meetings"
    assert _is_semantic_duplicate_foresight(first, existing) is False
    existing.append(first)
    assert _is_semantic_duplicate_foresight(second, existing) is True


# ---------- _drop_bullets_without_src --------------------------------


def test_drop_keeps_bullet_with_src_link():
    body = "- **Status**: v1.0 released [src: episodes.md @ 2026-05-15 19:30]"
    cleaned, n_dropped = _drop_bullets_without_src(body)
    assert cleaned == body
    assert n_dropped == 0


def test_drop_removes_bullet_missing_src_link():
    body = "- **Status**: v1.0 released [src: episodes.md @ 2026-05-15 19:30]\n- Senior developer working remotely"
    cleaned, n_dropped = _drop_bullets_without_src(body)
    assert "- Senior developer" not in cleaned
    assert "v1.0 released" in cleaned
    assert n_dropped == 1


def test_drop_preserves_non_bullet_lines():
    body = "### clawtrack\n- **Status**: v1.0 released [src: episodes.md @ 2026-05-15 19:30]\n\nSome prose paragraph.\n"
    cleaned, n_dropped = _drop_bullets_without_src(body)
    assert "### clawtrack" in cleaned
    assert "Some prose paragraph." in cleaned
    assert n_dropped == 0


def test_drop_handles_indented_bullets():
    body = "  - **Status**: ok [src: episodes.md @ 2026-05-15 19:30]\n  - missing-src bullet"
    cleaned, n_dropped = _drop_bullets_without_src(body)
    assert "missing-src bullet" not in cleaned
    assert "ok" in cleaned
    assert n_dropped == 1


def test_drop_empty_body_noop():
    cleaned, n_dropped = _drop_bullets_without_src("")
    assert cleaned == ""
    assert n_dropped == 0


def test_drop_rejects_src_with_wrong_format():
    # Wrong file name (not episodes.md), wrong bracket style, etc.
    body = (
        "- bullet a (src: episodes.md @ 2026-05-15 19:30)\n"  # parens — wrong
        "- bullet b [src: notes.md @ 2026-05-15 19:30]\n"  # wrong file
        "- bullet c [src: episodes.md @ 2026-05-15 19:30]\n"  # correct
    )
    cleaned, n_dropped = _drop_bullets_without_src(body)
    assert "bullet a" not in cleaned
    assert "bullet b" not in cleaned
    assert "bullet c" in cleaned
    assert n_dropped == 2


# ---------- module-level constants snapshot --------------------------


def test_process_tags_constant():
    # Stored without leading '#' since ``_TAG_RE`` strips it on parse.
    assert _PROCESS_TAGS == frozenset({"question", "habit", "answer"})


def test_valid_confidence_constant():
    assert _VALID_CONFIDENCE == frozenset({"low", "medium", "high"})
