"""Tests for the split consolidation path.

Two-stage consolidation:

- ``annotate(messages)``  — light path that ONLY appends to episodes.md.
  Does not touch user.md.  Runs on the normal token-budget trigger.
- ``refresh_section(tag)`` — heavy path that rewrites ONE H2 section of
  user.md from the tag's recent episodes.  Other H2 sections are left
  byte-identical.  Fires from ``maybe_refresh_hot_tags`` only when a
  tag's new-episode count exceeds the threshold.

Tests cover:

1. Pure-Python helpers (``_parse_episode_line``, ``_splice_h2_section``,
   ``count_tags``, ``hot_tags``).
2. ``annotate`` end-to-end on a case_06-shaped conversation, asserting
   the episode-format invariants survive in the new path (timestamp +
   #tags on every episode) AND that user.md is untouched.
3. ``refresh_section`` rewrites one section without disturbing peers.
4. ``maybe_refresh_hot_tags`` fires only above threshold and advances
   the offset state file correctly.
"""

from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from raven.memory_engine.consolidate.consolidator import (
    MemoryStore,
    _parse_episode_line,
    _splice_h2_section,
)
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest


_TAG_RE = re.compile(r"#[a-z][a-z0-9-]*")
_EVIDENCE_RE = re.compile(r"\[src: episodes\.md @ [^\]]+\]")


# ===========================================================================
# FakeProvider — multi-tool aware.
# ===========================================================================

class _FakeProvider(LLMProvider):
    """Stubs ``chat`` by looking up a canned response per tool name.

    Configure via ``set_response(tool_name, args_dict)``. Raises if a
    tool is invoked without a configured response — that's a test bug.
    """
    def __init__(self):
        super().__init__()
        self._responses: dict[str, dict[str, Any]] = {}

    def set_response(self, tool_name: str, args: dict[str, Any]) -> None:
        self._responses[tool_name] = args

    async def chat(self, **kwargs) -> LLMResponse:
        tools = kwargs.get("tools") or []
        if not tools:
            return LLMResponse(content=None)
        tool_name = tools[0]["function"]["name"]
        if tool_name not in self._responses:
            raise RuntimeError(
                f"FakeProvider received call to {tool_name!r} without canned response"
            )
        return LLMResponse(
            content=None,
            tool_calls=[ToolCallRequest(
                id=f"call_{tool_name}",
                name=tool_name,
                arguments=self._responses[tool_name],
            )],
        )

    def get_default_model(self) -> str:
        return "fake-model"


# ===========================================================================
# Pure-helper tests.
# ===========================================================================

class TestParseEpisodeLine:
    def test_extracts_timestamp_summary_and_tags(self):
        ts, summary, tags = _parse_episode_line(
            "[2026-05-07 09:30] User raised Project A perf #project-a #perf"
        )
        assert ts == "2026-05-07 09:30"
        assert tags == ["project-a", "perf"]
        assert "#" not in summary
        assert "User raised Project A perf" in summary

    def test_rejects_lines_without_timestamp(self):
        assert _parse_episode_line("no timestamp here") is None
        assert _parse_episode_line("") is None

    def test_empty_tags_is_empty_list(self):
        ts, summary, tags = _parse_episode_line(
            "[2026-05-07 09:30] event with no tags at all"
        )
        assert tags == []
        assert summary == "event with no tags at all"


class TestSpliceH2Section:
    USER_MD = (
        "# Long-term Memory\n"
        "\n"
        "## Projects\n"
        "\n"
        "- old project bullet [src: episodes.md @ 2026-05-01 10:00]\n"
        "\n"
        "## Habits\n"
        "\n"
        "- habit bullet [src: episodes.md @ 2026-05-02 09:00]\n"
        "\n"
        "## Notes\n"
        "\n"
        "- note bullet [src: episodes.md @ 2026-05-03 11:00]\n"
    )

    def test_replaces_target_section_only(self):
        new = _splice_h2_section(
            self.USER_MD, "## Projects",
            "- new project bullet [src: episodes.md @ 2026-05-19 10:00]",
        )
        assert "old project bullet" not in new
        assert "new project bullet" in new
        # other sections survive verbatim
        assert "- habit bullet [src: episodes.md @ 2026-05-02 09:00]" in new
        assert "- note bullet [src: episodes.md @ 2026-05-03 11:00]" in new

    def test_preserves_h1_and_other_h2_byte_identical(self):
        new = _splice_h2_section(self.USER_MD, "## Projects", "- new\n")
        # Hash the bytes of `## Habits` onward — should equal the original
        # from `## Habits` onward.
        orig_tail = self.USER_MD[self.USER_MD.index("## Habits"):]
        new_tail = new[new.index("## Habits"):]
        assert new_tail == orig_tail

    def test_appends_new_h2_when_heading_missing(self):
        new = _splice_h2_section(
            self.USER_MD, "## Foresight", "- predicted X [src: episodes.md @ unknown]",
        )
        assert "## Foresight" in new
        assert "predicted X" in new
        # original ## Projects/## Habits/## Notes still present
        assert "## Projects" in new
        assert "## Habits" in new
        assert "## Notes" in new


# ===========================================================================
# annotate() — light path.
# ===========================================================================

CASE_06_MESSAGES: list[dict[str, Any]] = [
    {"role": "user", "timestamp": "2026-05-07T09:30",
     "content": "想优化 Project A 月报，30 秒太慢"},
    {"role": "user", "timestamp": "2026-05-07T10:30",
     "content": "Project A 先放，去修 Project B race condition"},
    {"role": "user", "timestamp": "2026-05-07T11:30",
     "content": "Redis 锁加在 deploy/tasks.py，失败率 20%"},
    {"role": "user", "timestamp": "2026-05-07T12:30",
     "content": "锁超时 30s→60s，明天看效果"},
]

ANNOTATE_RESPONSE: dict[str, Any] = {
    "episode_summary": [
        "[2026-05-07 09:30] User raised Project A monthly report perf (30s, 5-table join) #project-a #perf #sql",
        "[2026-05-07 10:30] User deferred Project A, pivoted to Project B race condition #project-b #pivot #deferred",
        "[2026-05-07 11:30] Project B Redis lock added in deploy/tasks.py; 20% failure rate #project-b #bug",
        "[2026-05-07 12:30] Lock timeout raised 30s->60s; mutex if still failing tomorrow #project-b #decision",
    ],
    "foresight_hint": [
        {
            "prediction": "User likely returns to Project A index design tomorrow",
            "window": "1-2 days", "confidence": "high",
            "src_ts": "2026-05-07 10:30",
        },
    ],
}


@pytest.mark.asyncio
async def test_annotate_writes_tagged_episodes(tmp_path: Path):
    """Episodes land with the expected format (#tags + timestamps)."""
    store = MemoryStore(tmp_path, now_fn=lambda: datetime(2026, 5, 7, 17, 32))
    provider = _FakeProvider()
    provider.set_response("annotate_conversation", ANNOTATE_RESPONSE)

    ok = await store.annotate(CASE_06_MESSAGES, provider, "fake-model")
    assert ok is True

    episode_lines = [
        ln for ln in store.history_file.read_text(encoding="utf-8").splitlines()
        if ln.startswith("[")
    ]
    assert len(episode_lines) == 4
    assert all(_TAG_RE.search(ln) for ln in episode_lines)


@pytest.mark.asyncio
async def test_annotate_does_not_touch_user_md(tmp_path: Path):
    """Invariant: annotate is the LIGHT path. user.md stays as-is."""
    store = MemoryStore(tmp_path, now_fn=lambda: datetime(2026, 5, 7, 17, 32))
    seeded = "# Long-term Memory\n\n## Projects\n\n- existing [src: episodes.md @ 2026-04-01 10:00]\n"
    store.write_long_term(seeded)
    provider = _FakeProvider()
    provider.set_response("annotate_conversation", ANNOTATE_RESPONSE)

    ok = await store.annotate(CASE_06_MESSAGES, provider, "fake-model")
    assert ok is True
    # Byte-identical user.md.
    assert store.read_long_term() == seeded


@pytest.mark.asyncio
async def test_annotate_default_omits_foresight_slot(tmp_path: Path):
    """enable_foresight defaults to False — the tool schema sent to the LLM
    should NOT include the foresight_hint property."""
    captured: dict[str, Any] = {}

    class _CapturingProvider(LLMProvider):
        async def chat(self, **kwargs):
            captured["tools"] = kwargs.get("tools")
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(
                    id="call_0", name="annotate_conversation",
                    arguments={
                        "episode_summary": [
                            "[2026-05-07 09:30] x #project-a",
                        ],
                    },
                )],
            )

        def get_default_model(self) -> str:
            return "fake-model"

    store = MemoryStore(tmp_path)
    ok = await store.annotate(
        CASE_06_MESSAGES, _CapturingProvider(), "fake-model",
    )
    assert ok is True
    tool = captured["tools"][0]["function"]
    assert "foresight_hint" not in tool["parameters"]["properties"]
    assert "foresight_hint" not in tool["parameters"]["required"]


@pytest.mark.asyncio
async def test_annotate_with_foresight_includes_slot(tmp_path: Path):
    """enable_foresight=True — the tool schema includes foresight_hint."""
    captured: dict[str, Any] = {}

    class _CapturingProvider(LLMProvider):
        async def chat(self, **kwargs):
            captured["tools"] = kwargs.get("tools")
            return LLMResponse(
                content=None,
                tool_calls=[ToolCallRequest(
                    id="call_0", name="annotate_conversation",
                    arguments={
                        "episode_summary": [
                            "[2026-05-07 09:30] x #project-a",
                        ],
                        "foresight_hint": [],
                    },
                )],
            )

        def get_default_model(self) -> str:
            return "fake-model"

    store = MemoryStore(tmp_path)
    ok = await store.annotate(
        CASE_06_MESSAGES, _CapturingProvider(), "fake-model",
        enable_foresight=True,
    )
    assert ok is True
    tool = captured["tools"][0]["function"]
    assert "foresight_hint" in tool["parameters"]["properties"]
    assert "foresight_hint" in tool["parameters"]["required"]


@pytest.mark.asyncio
async def test_annotate_tolerates_string_episode_summary(tmp_path: Path):
    """Smaller LLMs occasionally emit a string instead of array — wrap to [str]."""
    args = dict(ANNOTATE_RESPONSE)
    args["episode_summary"] = "[2026-05-07 09:30] solo line #project-a"
    store = MemoryStore(tmp_path, now_fn=lambda: datetime(2026, 5, 7, 17, 32))
    provider = _FakeProvider()
    provider.set_response("annotate_conversation", args)

    ok = await store.annotate(CASE_06_MESSAGES, provider, "fake-model")
    assert ok is True
    text = store.history_file.read_text(encoding="utf-8")
    assert "[2026-05-07 09:30] solo line #project-a" in text


# ===========================================================================
# Tag counting + hot-tag detection.
# ===========================================================================

def _seed_episodes(store: MemoryStore, lines: list[str]) -> None:
    """Append given lines (each one paragraph) to episodes.md."""
    for ln in lines:
        store.append_history(ln)


class TestTagAccounting:
    def test_count_tags_aggregates_across_lines(self, tmp_path: Path):
        store = MemoryStore(tmp_path)
        _seed_episodes(store, [
            "[2026-05-01 09:00] foo #project-a #perf",
            "[2026-05-02 09:00] bar #project-a",
            "[2026-05-03 09:00] baz #project-b #bug",
        ])
        counts = store.count_tags()
        assert counts == {"project-a": 2, "perf": 1, "project-b": 1, "bug": 1}

    def test_hot_tags_returns_tags_at_or_above_threshold(self, tmp_path: Path):
        store = MemoryStore(tmp_path)
        _seed_episodes(store, [
            f"[2026-05-{day:02d} 09:00] e{day} #project-b"
            for day in range(1, 6)
        ] + [
            "[2026-05-06 09:00] x #project-a",
            "[2026-05-07 09:00] y #project-a",
        ])
        hot = store.hot_tags(threshold=5)
        # project-b: 5 occurrences ≥ 5 → hot. project-a: 2 < 5 → not hot.
        assert [t for t, _, _ in hot] == ["project-b"]

    def test_hot_tags_respects_existing_offset(self, tmp_path: Path):
        store = MemoryStore(tmp_path)
        _seed_episodes(store, [
            f"[2026-05-{day:02d} 09:00] e{day} #project-b" for day in range(1, 8)
        ])
        # Offset already at 6, so only 1 new episode — below threshold 5.
        store.write_tag_offsets({"project-b": 6})
        assert store.hot_tags(threshold=5) == []
        # If we lower threshold to 1, the 1 new episode lights it up.
        hot = store.hot_tags(threshold=1)
        assert [t for t, _, _ in hot] == [("project-b", 7, 6)[0]]


# ===========================================================================
# refresh_section() + maybe_refresh_hot_tags() — heavy path.
# ===========================================================================

SEEDED_USER_MD = (
    "# Long-term Memory\n"
    "\n"
    "## Projects\n"
    "\n"
    "### Project A\n"
    "- old A status [src: episodes.md @ 2026-04-01 09:00]\n"
    "\n"
    "## Habits\n"
    "\n"
    "- Goes to gym Wed 20:00 [src: episodes.md @ 2026-04-15 21:00]\n"
    "- Writes weekly report Mon 09:00 [src: episodes.md @ 2026-04-08 09:30]\n"
    "\n"
    "## Notes\n"
    "\n"
    "- Prefers terse responses [src: episodes.md @ 2026-04-10 14:00]\n"
)


def _seed_project_b_episodes(store: MemoryStore, n: int = 5) -> None:
    """N realistic #project-b episodes spread across a week."""
    seeds = [
        "[2026-05-07 09:30] Project B race condition surfaced on staging #project-b #bug",
        "[2026-05-07 10:30] Redis lock added in deploy/tasks.py #project-b #infra",
        "[2026-05-07 11:30] 20% failure rate persists after 30s lock #project-b #bug",
        "[2026-05-07 12:30] Raised timeout 30s -> 60s #project-b #decision",
        "[2026-05-08 09:00] 60s timeout: 5 of 5 staging passes #project-b #decision",
    ]
    for line in seeds[:n]:
        store.append_history(line)


@pytest.mark.asyncio
async def test_refresh_section_rewrites_target_only(tmp_path: Path):
    """The hot tag's section is replaced; ## Habits and ## Notes survive byte-identical."""
    store = MemoryStore(tmp_path, now_fn=lambda: datetime(2026, 5, 8, 10, 0))
    store.write_long_term(SEEDED_USER_MD)
    _seed_project_b_episodes(store, n=5)

    new_project_body = (
        "### Project A\n"
        "- old A status [src: episodes.md @ 2026-04-01 09:00]\n"
        "\n"
        "### Project B\n"
        "- Status: lock fix verified (5/5 passes after 60s timeout) "
        "[src: episodes.md @ 2026-05-08 09:00]\n"
        "- Root Cause: Redis lock timeout 30s < deploy step 35s "
        "[src: episodes.md @ 2026-05-07 12:30]\n"
    )
    provider = _FakeProvider()
    provider.set_response("refresh_profile_section", {
        "section_heading": "## Projects",
        "section_body": new_project_body,
    })

    ok = await store.refresh_section("project-b", provider, "fake-model")
    assert ok is True

    new_user_md = store.read_long_term()
    # Hot section updated.
    assert "Status: lock fix verified" in new_user_md
    assert "old A status" in new_user_md   # preserved inside the same H2
    # ## Habits + ## Notes byte-identical from their headings to EOF.
    habits_orig = SEEDED_USER_MD[SEEDED_USER_MD.index("## Habits"):]
    habits_new = new_user_md[new_user_md.index("## Habits"):]
    assert habits_new == habits_orig


@pytest.mark.asyncio
async def test_maybe_refresh_hot_tags_fires_only_above_threshold(tmp_path: Path):
    """Below threshold → no refresh, no LLM call, no state file write."""
    store = MemoryStore(tmp_path, now_fn=lambda: datetime(2026, 5, 8, 10, 0))
    store.write_long_term(SEEDED_USER_MD)
    _seed_project_b_episodes(store, n=3)  # only 3 — below threshold 5

    provider = _FakeProvider()
    # No response configured: any LLM call will RAISE.
    refreshed = await store.maybe_refresh_hot_tags(
        provider, "fake-model", threshold=5,
    )
    assert refreshed == 0
    # Offsets file should NOT have been written.
    assert not store._tag_offsets_path.exists()
    # user.md still byte-identical to seed.
    assert store.read_long_term() == SEEDED_USER_MD


@pytest.mark.asyncio
async def test_maybe_refresh_hot_tags_advances_offsets(tmp_path: Path):
    """A successful refresh updates the JSON offset file to current count."""
    store = MemoryStore(tmp_path, now_fn=lambda: datetime(2026, 5, 8, 10, 0))
    store.write_long_term(SEEDED_USER_MD)
    _seed_project_b_episodes(store, n=5)

    new_body = (
        "### Project B\n"
        "- post-refresh bullet [src: episodes.md @ 2026-05-08 09:00]\n"
    )
    provider = _FakeProvider()
    provider.set_response("refresh_profile_section", {
        "section_heading": "## Projects",
        "section_body": new_body,
    })

    refreshed = await store.maybe_refresh_hot_tags(
        provider, "fake-model", threshold=5,
    )
    assert refreshed == 1
    offsets = store.read_tag_offsets()
    assert offsets == {"project-b": 5}
    # Profile bullet wrote successfully.
    assert "post-refresh bullet" in store.read_long_term()
