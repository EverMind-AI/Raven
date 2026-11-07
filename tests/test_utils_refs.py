"""Tests for the shared ``[[ref ...]]`` grammar (raven.utils.refs)."""

import pytest

from raven.utils.refs import REF_PATTERN, SourceRef

# The grammar, spelled out rather than imported. This is the whole point of the
# module having one home: every store that mints a ref and every reader that
# parses one share this spelling, so a change to the shape has to be a
# deliberate act that breaks this line first.
CONTRACT = r"\[\[ref (?P<store>[\w.-]+)/(?P<scope>[^#\]]+)#(?P<lo>\d+)(?:-(?P<hi>\d+))?\]\]"


class TestPattern:
    def test_the_pattern_is_the_cross_subsystem_contract(self):
        assert REF_PATTERN.pattern == CONTRACT


class TestRender:
    def test_a_single_entry_renders_one_ordinal(self):
        assert SourceRef("session", "tg_42", 118, 118).render() == "[[ref session/tg_42#118]]"

    def test_a_span_renders_both_ends(self):
        assert SourceRef("shared_memory", "raven-code", 7, 9).render() == "[[ref shared_memory/raven-code#7-9]]"

    def test_what_render_produces_the_pattern_accepts(self):
        for ref in (SourceRef("checkpoint", "cli:abc", 3, 3), SourceRef("everos.v2", "a/b", 3, 12)):
            assert REF_PATTERN.fullmatch(ref.render())


class TestParse:
    @pytest.mark.parametrize(("lo", "hi"), [(1, 1), (3, 12), (118, 118)])
    def test_render_then_parse_round_trips(self, lo, hi):
        ref = SourceRef("session", "tg_42", lo, hi)
        assert SourceRef.parse(ref.render()) == ref

    def test_a_token_embedded_in_prose_is_found(self):
        assert SourceRef.parse("lesson learned\n\n[[ref shared_memory/raven-code#2]]\n") == SourceRef(
            "shared_memory", "raven-code", 2, 2
        )

    def test_a_missing_upper_end_reads_as_a_single_entry(self):
        assert SourceRef.parse("[[ref session/s#5]]").hi == 5

    def test_the_first_of_several_wins(self):
        text = "[[ref session/s#1]] then [[ref session/s#2]]"
        assert SourceRef.parse(text) == SourceRef("session", "s", 1, 1)

    def test_a_dotted_store_name_is_legal(self):
        assert SourceRef.parse("[[ref everos.v2/raven-research#4]]").store == "everos.v2"

    def test_the_scope_takes_everything_up_to_the_hash(self):
        # Store names cannot hold a slash, so the split is unambiguous and a
        # scope may be a path-like session key.
        ref = SourceRef.parse("[[ref session/cli:proj/main#6-8]]")
        assert (ref.store, ref.scope, ref.lo, ref.hi) == ("session", "cli:proj/main", 6, 8)

    @pytest.mark.parametrize(
        "text",
        [
            "",
            "no ref here",
            "[ref session/s#1]",
            "[[ref session/s]]",
            "[[ref session/s#]]",
            "[[ref /s#1]]",
            "[[ref session#1]]",
            "[[ref session/s#x]]",
        ],
    )
    def test_text_without_a_token_parses_to_none(self, text):
        assert SourceRef.parse(text) is None


class TestFindAll:
    def test_text_without_a_token_finds_nothing(self):
        assert SourceRef.find_all("nothing to see [ref session/s#1]") == []

    def test_every_token_comes_back_in_reading_order(self):
        text = "a [[ref session/s#1]] b [[ref shared_memory/raven-code#7-9]] c [[ref checkpoint/s#4]]"
        assert SourceRef.find_all(text) == [
            SourceRef("session", "s", 1, 1),
            SourceRef("shared_memory", "raven-code", 7, 9),
            SourceRef("checkpoint", "s", 4, 4),
        ]

    def test_found_refs_render_back_to_what_was_read(self):
        text = "[[ref session/s#1]] [[ref session/s#7-9]]"
        assert " ".join(ref.render() for ref in SourceRef.find_all(text)) == text

    def test_a_near_miss_beside_a_real_token_is_skipped(self):
        assert SourceRef.find_all("[[ref session/s]] [[ref session/s#2]]") == [SourceRef("session", "s", 2, 2)]
