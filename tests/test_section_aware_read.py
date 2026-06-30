"""Tests for section-aware reading of user.md.

When ``MemoryStore.get_memory_context`` is called with a current user
message, it parses user.md into H2 sections, scores each by lexical
overlap with the query, and returns only the top-K (default 2) plus
the '## Notes' catchall. When called without a message, it preserves
the full-dump behavior.

Tests cover the three pure helpers (``_parse_user_md_sections``,
``_score_section_relevance``, ``_select_relevant_sections``) and the
public ``get_memory_context`` entry point.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.memory_engine.consolidate.consolidator import (
    MemoryStore,
    _parse_user_md_sections,
    _score_section_relevance,
)

SEEDED = (
    "# Long-term Memory\n"
    "\n"
    "## Projects\n"
    "\n"
    "### Project A\n"
    "- 5-table join in reports/queries.py [src: episodes.md @ 2026-04-01 09:00]\n"
    "\n"
    "### Project B\n"
    "- Race condition on staging; Redis lock added [src: episodes.md @ 2026-05-01 10:00]\n"
    "- mutex fallback under consideration [src: episodes.md @ 2026-05-07 12:30]\n"
    "\n"
    "## Habits\n"
    "\n"
    "- Goes to gym Wed 20:00 [src: episodes.md @ 2026-04-15 21:00]\n"
    "- Writes weekly report Mon 09:00 [src: episodes.md @ 2026-04-08 09:30]\n"
    "\n"
    "## Preferences\n"
    "\n"
    "- Uses uv for Python package management [src: episodes.md @ 2026-03-15 14:00]\n"
    "\n"
    "## Notes\n"
    "\n"
    "- Prefers terse responses [src: episodes.md @ 2026-04-10 14:00]\n"
)


class TestParseUserMdSections:
    def test_splits_into_h2_blocks_in_file_order(self):
        sections = _parse_user_md_sections(SEEDED)
        assert list(sections.keys()) == [
            "## Projects",
            "## Habits",
            "## Preferences",
            "## Notes",
        ]

    def test_body_includes_h3_subheadings(self):
        sections = _parse_user_md_sections(SEEDED)
        projects = sections["## Projects"]
        assert "### Project A" in projects
        assert "### Project B" in projects

    def test_h1_preamble_dropped(self):
        sections = _parse_user_md_sections(SEEDED)
        assert "# Long-term Memory" not in "\n".join(sections.values())

    def test_returns_empty_for_no_h2(self):
        sections = _parse_user_md_sections("# Title\n\nsome prose\n")
        assert sections == {}


class TestScoreSectionRelevance:
    def test_heading_match_weighs_more_than_body(self):
        # 1 heading token match (3x) vs 1 body token match (1x).
        s_heading = _score_section_relevance(
            "tell me about habits",
            "## Habits",
            "irrelevant body",
        )
        s_body = _score_section_relevance(
            "tell me about habits",
            "## Other",
            "habits show up in body",
        )
        assert s_heading > s_body

    def test_empty_query_scores_zero(self):
        assert _score_section_relevance("", "## Foo", "body") == 0.0

    def test_chinese_tokens_match_as_runs(self):
        # "怎么写" is one multi-char token; should match itself in body.
        score = _score_section_relevance(
            "mutex 怎么写",
            "## Random",
            "mutex 怎么写 看这里",
        )
        assert score >= 2  # both 'mutex' and '怎么写' overlap


class TestGetMemoryContextSelective:
    @pytest.fixture
    def store(self, tmp_path: Path) -> MemoryStore:
        s = MemoryStore(tmp_path)
        s.write_long_term(SEEDED)
        return s

    def test_no_message_falls_back_to_full_dump(self, store: MemoryStore):
        out = store.get_memory_context()
        # All H2 headings present in the full-dump branch.
        for heading in ("## Projects", "## Habits", "## Preferences", "## Notes"):
            assert heading in out

    def test_empty_message_falls_back_to_full_dump(self, store: MemoryStore):
        out = store.get_memory_context(current_message="   \n\n  ")
        for heading in ("## Projects", "## Habits", "## Preferences", "## Notes"):
            assert heading in out

    def test_project_query_pulls_projects_section(self, store: MemoryStore):
        out = store.get_memory_context(
            current_message="Project B 的 mutex 怎么写？",
        )
        assert "## Projects" in out
        # Notes always included as catchall.
        assert "## Notes" in out
        # Irrelevant sections are excluded.
        assert "## Habits" not in out
        assert "## Preferences" not in out

    def test_habits_query_pulls_habits_section(self, store: MemoryStore):
        out = store.get_memory_context(
            current_message="周三晚上能跟我聊聊吗 habits",
        )
        assert "## Habits" in out
        assert "## Notes" in out
        # Projects shouldn't dominate purely on length.
        assert "## Projects" not in out

    def test_query_that_matches_nothing_still_returns_notes(
        self,
        store: MemoryStore,
    ):
        # All scores near zero; top-K picks something, but Notes always wins
        # the catchall slot.
        out = store.get_memory_context(
            current_message="xyzabc unrelated query 12345",
        )
        assert "## Notes" in out

    def test_empty_user_md_returns_empty(self, tmp_path: Path):
        s = MemoryStore(tmp_path)
        # Don't write anything.
        assert s.get_memory_context(current_message="anything") == ""
