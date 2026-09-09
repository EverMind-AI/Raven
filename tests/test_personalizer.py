"""The personalizer, driven directly rather than through a turn.

Every method is written to fail neutrally: the agent loop calls these between a
user's message and the model's answer, so a bad LLM reply or a provider outage
has to cost a clarification, never the turn. That is the property these pin --
along with the two shapes the extraction prompt is allowed to answer in, and
what reaches user.md when it answers well.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent.personalizer.personalizer import Personalizer
from raven.memory_engine.consolidate.consolidator import MemoryStore


class _Provider:
    """Answers every chat with the next queued reply, or raises the queued error."""

    def __init__(self, *replies: str | Exception) -> None:
        self._replies = list(replies)
        self.calls: list[dict] = []

    async def chat(self, **kwargs):
        self.calls.append(kwargs)
        reply = self._replies.pop(0) if self._replies else ""
        if isinstance(reply, Exception):
            raise reply
        return SimpleNamespace(content=reply)


def _personalizer(tmp_path: Path, *replies: str | Exception) -> Personalizer:
    return Personalizer(MemoryStore(tmp_path), _Provider(*replies), "stub-model")


# ── reading a model's answer ────────────────────────────────────────────────


class TestReadingTheModelsAnswer:
    def test_json_wrapped_in_prose_is_still_read(self, tmp_path: Path) -> None:
        p = _personalizer(tmp_path)
        assert p._parse_json('Sure! {"a": 1} hope that helps', {"a": 0}) == {"a": 1}

    def test_an_answer_with_no_object_is_the_fallback(self, tmp_path: Path) -> None:
        p = _personalizer(tmp_path)
        assert p._parse_json("I could not decide.", {"needs_clarification": False}) == {"needs_clarification": False}

    def test_a_broken_object_is_the_fallback_not_an_exception(self, tmp_path: Path) -> None:
        p = _personalizer(tmp_path)
        assert p._parse_json('{"a": }', {"a": 0}) == {"a": 0}


# ── the two shapes a fact list arrives in ───────────────────────────────────


class TestGroupingFacts:
    def test_a_category_chooses_the_section(self) -> None:
        grouped = Personalizer._group_facts_by_category(
            [
                {"text": "prefers Go", "category": "preference"},
                {"text": "asks before nudging", "category": "proactivity"},
            ]
        )
        assert grouped == {
            "Preferences": ["prefers Go"],
            "Proactivity Preferences": ["asks before nudging"],
        }

    def test_a_category_nobody_maps_falls_back_to_the_section_the_caller_named(self) -> None:
        grouped = Personalizer._group_facts_by_category(
            [{"text": "runs on a laptop", "category": "hardware"}], legacy_section="Context"
        )
        assert grouped == {"Context": ["runs on a laptop"]}

    def test_the_legacy_shape_is_a_flat_list_under_one_section(self) -> None:
        grouped = Personalizer._group_facts_by_category(["prefers Go", "  "], legacy_section="Context")
        assert grouped == {"Context": ["prefers Go"]}

    def test_items_that_carry_no_text_are_dropped(self) -> None:
        assert Personalizer._group_facts_by_category([{"text": "   "}, None, 7, []]) == {}
        assert Personalizer._group_facts_by_category([]) == {}
        assert Personalizer._group_facts_by_category(None) == {}


# ── what the prompt is told about the conversation ──────────────────────────


class TestFormattingHistory:
    def test_only_the_last_few_messages_travel(self) -> None:
        history = [{"role": "user", "content": f"m{i}"} for i in range(6)]
        assert Personalizer._format_history(history) == "USER: m2\nUSER: m3\nUSER: m4\nUSER: m5"

    def test_a_long_message_is_truncated(self) -> None:
        out = Personalizer._format_history([{"role": "user", "content": "x" * 250}])
        assert out == "USER: " + "x" * 200 + "..."

    def test_nothing_usable_reads_as_no_prior_context(self) -> None:
        assert Personalizer._format_history([{"role": "user", "content": "   "}]) == "(no prior context)"
        assert Personalizer._format_history([]) == "(no prior context)"


# ── writing to user.md ──────────────────────────────────────────────────────


class TestWritingASection:
    def test_facts_land_under_a_header_that_is_already_there(self, tmp_path: Path) -> None:
        p = _personalizer(tmp_path)
        p.memory.write_long_term("# Me\n\n## Preferences\n- likes tea\n\n## Other\n- keep\n")

        p._append_to_memory_section("Preferences", ["prefers Go"])

        assert p.memory.read_long_term() == ("# Me\n\n## Preferences\n- prefers Go\n- likes tea\n\n## Other\n- keep\n")

    def test_a_missing_header_is_created_at_the_end(self, tmp_path: Path) -> None:
        p = _personalizer(tmp_path)
        p.memory.write_long_term("# Me\n\n## Other\n- keep\n")

        p._append_to_memory_section("Proactivity Preferences", ["asks first", "quiet at night"])

        assert p.memory.read_long_term().endswith("## Proactivity Preferences\n- asks first\n- quiet at night\n")
        assert "## Other" in p.memory.read_long_term()


# ── the steps the loop calls ────────────────────────────────────────────────


class TestTheStepsTheLoopCalls:
    async def test_classify_reads_the_models_verdict(self, tmp_path: Path) -> None:
        p = _personalizer(tmp_path, 'Thinking... {"needs_clarification": true, "domain": "code"}')

        assert await p.classify("write me a parser") == {"needs_clarification": True, "domain": "code"}

    async def test_a_provider_that_raises_costs_the_clarification_not_the_turn(self, tmp_path: Path) -> None:
        p = _personalizer(tmp_path, RuntimeError("provider down"))

        assert await p.classify("write me a parser") == {"needs_clarification": False, "domain": ""}

    async def test_an_answer_with_no_reusable_preference_writes_nothing(self, tmp_path: Path) -> None:
        p = _personalizer(tmp_path, '{"facts": [], "section": "Preferences"}')
        p.memory.write_long_term("# Me\n")

        assert await p.extract_and_store_preference("do it", "which language?", "whatever") is False
        assert p.memory.read_long_term() == "# Me\n"

    async def test_an_extracted_preference_reaches_the_section_it_named(self, tmp_path: Path) -> None:
        p = _personalizer(tmp_path, '{"facts": ["prefers Go"], "section": "Preferences"}')
        p.memory.write_long_term("# Me\n")

        assert await p.extract_and_store_preference("do it", "which language?", "Go") is True
        assert "## Preferences\n- prefers Go" in p.memory.read_long_term()

    async def test_the_history_reaches_the_prompt(self, tmp_path: Path) -> None:
        provider = _Provider('{"needs_clarification": false, "domain": ""}')
        p = Personalizer(MemoryStore(tmp_path), provider, "stub-model")

        await p.classify("and now?", history=[{"role": "user", "content": "earlier thing"}])

        prompt = provider.calls[0]["messages"][0]["content"]
        assert "USER: earlier thing" in prompt
        assert "and now?" in prompt


@pytest.mark.parametrize("bad", ["", "no json here", "{", "}{"])
async def test_no_shape_of_answer_makes_a_step_raise(tmp_path: Path, bad: str) -> None:
    p = _personalizer(tmp_path, bad, bad)

    assert await p.classify("x") == {"needs_clarification": False, "domain": ""}
    assert await p.extract_and_store_preference("x", "q", "a") is False
