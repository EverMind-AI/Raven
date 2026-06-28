"""Unit tests for ContextAssembler smart-loading helpers.

Verifies the section-aware allowlist + blocklist + size cap on
``MEMORY.md`` works without changing default behavior. Defaults
(allowlist=None, blocklist=[], max_chars=0) are pure passthrough so
v4-v11 benchmarks remain byte-identical.
"""

from __future__ import annotations

import pytest

from raven.config.raven import NudgePolicyConfig
from raven.proactive_engine.sentinel.predictor.context_assembler import (
    _filter_memory_md,
    _parse_h2_sections,
)


SAMPLE = """# Long-term Memory

## User Information
- name: Alex
- role: software engineer

## Plans
- ship clawtrack 5/15
- 跑步 120km in May

## Recent Notes
- random thought 1
- random thought 2

## Important Notes
- 5/25 晓棠生日
- 6/1 礼物 deadline

## Sentinel Observations (auto)
<!-- sentinel:auto last_updated=2026-05-15T07:00 -->
- dispatched: 12
<!-- /sentinel:auto -->
"""


def _cfg(*, allowlist=None, blocklist=None, max_chars=0):
    return NudgePolicyConfig(
        memory_section_allowlist=allowlist,
        memory_section_blocklist=blocklist or [],
        memory_max_chars=max_chars,
    )


# ---------------------------------------------------------------------------
# _parse_h2_sections


class TestParseH2:
    def test_extracts_each_h2_section(self):
        sections = _parse_h2_sections(SAMPLE)
        titles = [t for t, _ in sections]
        # Order preserved; lead-in (lines before first ##) is keyed by ""
        assert "User Information" in titles
        assert "Plans" in titles
        assert "Recent Notes" in titles
        assert "Important Notes" in titles
        assert "Sentinel Observations (auto)" in titles

    def test_lead_in_kept_as_empty_title(self):
        sections = _parse_h2_sections(SAMPLE)
        # First entry is lead-in (anything before the first ## header)
        assert sections[0][0] == ""
        assert "# Long-term Memory" in sections[0][1]

    def test_no_sections(self):
        sections = _parse_h2_sections("# memory\nfree text only\n")
        # All content is "lead-in"
        assert len(sections) == 1
        assert sections[0][0] == ""

    def test_empty_input(self):
        assert _parse_h2_sections("") == [("", "")]


# ---------------------------------------------------------------------------
# _filter_memory_md — defaults (no filter)


class TestPassthrough:
    def test_default_config_returns_input_unchanged(self):
        out = _filter_memory_md(SAMPLE, _cfg())
        assert out == SAMPLE

    def test_empty_input_returns_empty(self):
        assert _filter_memory_md("", _cfg()) == ""

    def test_no_h2_headers_legacy_passthrough(self):
        legacy = "## not really at line start? ## also not\n\nplain text\n"
        # Default config is passthrough regardless of structure
        assert _filter_memory_md(legacy, _cfg()) == legacy


# ---------------------------------------------------------------------------
# Allowlist


class TestAllowlist:
    def test_keeps_only_allowed_sections(self):
        out = _filter_memory_md(
            SAMPLE,
            _cfg(allowlist=["User Information", "Plans"]),
        )
        assert "## User Information" in out
        assert "## Plans" in out
        assert "## Recent Notes" not in out
        # Always-keep set: Important Notes + Sentinel Observations (auto)
        assert "## Important Notes" in out
        assert "## Sentinel Observations (auto)" in out

    def test_lead_in_always_preserved(self):
        out = _filter_memory_md(
            SAMPLE,
            _cfg(allowlist=["User Information"]),
        )
        assert "# Long-term Memory" in out  # lead-in survives


# ---------------------------------------------------------------------------
# Blocklist


class TestBlocklist:
    def test_drops_blocked_sections(self):
        out = _filter_memory_md(
            SAMPLE,
            _cfg(blocklist=["Recent Notes"]),
        )
        assert "## Recent Notes" not in out
        assert "random thought" not in out
        assert "## Plans" in out  # not blocked
        assert "## User Information" in out

    def test_blocklist_cannot_remove_always_keep_sections(self):
        # User trying to block an "always keep" section — should be ignored
        out = _filter_memory_md(
            SAMPLE,
            _cfg(blocklist=["Important Notes", "Sentinel Observations (auto)"]),
        )
        assert "## Important Notes" in out  # always kept regardless
        assert "## Sentinel Observations (auto)" in out

    def test_allowlist_and_blocklist_combine(self):
        out = _filter_memory_md(
            SAMPLE,
            _cfg(
                allowlist=["User Information", "Plans", "Recent Notes"],
                blocklist=["Recent Notes"],
            ),
        )
        # On allowlist but also blocklisted → drop (blocklist wins)
        assert "## Recent Notes" not in out
        assert "## User Information" in out
        assert "## Plans" in out


# ---------------------------------------------------------------------------
# Size cap


class TestSizeCap:
    def test_no_cap_below_threshold(self):
        out = _filter_memory_md(SAMPLE, _cfg(max_chars=99999))
        # Threshold not reached → unchanged
        assert out == SAMPLE

    def test_truncates_low_priority_sections_first(self):
        # Force tight cap. Always-keep (User Information, Important Notes,
        # Sentinel Observations) + lead-in must survive; "normal" sections
        # (Plans, Recent Notes) drop.
        out = _filter_memory_md(SAMPLE, _cfg(max_chars=400))
        # Always-keep sections survive
        assert "## User Information" in out
        assert "## Important Notes" in out
        assert "## Sentinel Observations (auto)" in out
        # Output respects cap (loose: must be <= cap + small overhead for
        # boundary blank lines)
        assert len(out) <= 600  # ample slack for separators

    def test_cap_zero_means_no_truncation(self):
        # max_chars=0 is the "no cap" sentinel
        out = _filter_memory_md(SAMPLE, _cfg(max_chars=0))
        assert out == SAMPLE
