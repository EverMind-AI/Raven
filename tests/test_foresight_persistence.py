"""Foresight persistence into user.md ## Foresight section.

Covers ``MemoryStore.append_foresight`` semantics:
- creates the section when it doesn't exist
- appends to existing section preserving prior bullets
- dedupes by (prediction, src_ts) tuple
- FIFO-caps at ``max_keep`` (default 20) when total grows past it
- end-to-end: ``annotate(enable_foresight=True)`` persists, default-off doesn't
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from raven.memory_engine.consolidate.consolidator import (
    _FORESIGHT_BULLET_RE,
    _FORESIGHT_HEADING,
    MemoryStore,
    _ensure_foresight_at_end,
    _format_foresight_bullet,
    _splice_h2_section_at_end,
)
from raven.providers.base import LLMProvider, LLMResponse, ToolCallRequest

# ---------------------------------------------------------------------------
# Pure-helper tests.


class TestForesightBulletFormat:
    def test_renders_all_fields_inline(self):
        s = _format_foresight_bullet(
            {
                "prediction": "Alex returns to Project A index work",
                "window": "1-2 days",
                "confidence": "high",
                "src_ts": "2026-05-07 10:30",
            },
            generation_ts="2026-05-07 17:32",
        )
        assert s.startswith("- Alex returns to Project A index work ")
        assert "(from 2026-05-07 17:32" in s
        assert "window: 1-2 days" in s
        assert "confidence: high" in s
        assert "src: episodes.md @ 2026-05-07 10:30)" in s

    def test_missing_fields_default_to_question_mark(self):
        s = _format_foresight_bullet(
            {"prediction": "only prediction"},
            generation_ts="2026-05-07 17:32",
        )
        assert "window: ?" in s
        assert "confidence: ?" in s
        assert "src: episodes.md @ ?" in s

    def test_round_trips_through_regex(self):
        original = {
            "prediction": "Alex revisits Project A tomorrow",
            "window": "1-2 days",
            "confidence": "high",
            "src_ts": "2026-05-07 10:30",
        }
        line = _format_foresight_bullet(original, "2026-05-07 17:32")
        m = _FORESIGHT_BULLET_RE.match(line)
        assert m is not None
        assert m.group("prediction") == "Alex revisits Project A tomorrow"
        assert m.group("gen_ts") == "2026-05-07 17:32"
        assert m.group("window") == "1-2 days"
        assert m.group("confidence") == "high"
        assert m.group("src_ts") == "2026-05-07 10:30"


# ---------------------------------------------------------------------------
# MemoryStore.append_foresight.


@pytest.fixture
def store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(
        tmp_path,
        now_fn=lambda: datetime(2026, 5, 7, 17, 32),
    )


_FS_A = {
    "prediction": "Alex returns to Project A index work tomorrow",
    "window": "1-2 days",
    "confidence": "high",
    "src_ts": "2026-05-07 10:30",
}
_FS_B = {
    "prediction": "If 60s timeout fails, Alex switches Project B to mutex",
    "window": "1-2 days",
    "confidence": "medium",
    "src_ts": "2026-05-07 12:30",
}


def test_creates_section_on_first_append(store: MemoryStore):
    """No prior user.md → ## Foresight section appears with 1 bullet."""
    written = store.append_foresight([_FS_A])
    assert written == 1
    content = store.read_long_term()
    assert _FORESIGHT_HEADING in content
    assert "Alex returns to Project A index work tomorrow" in content
    # Properly formatted bullet:
    assert "(from 2026-05-07 17:32, window: 1-2 days, confidence: high, src: episodes.md @ 2026-05-07 10:30)" in content


def test_appends_to_existing_section_preserving_prior_bullets(store: MemoryStore):
    """Existing ## Foresight bullets stay verbatim, new ones append after."""
    store.append_foresight([_FS_A])
    store.append_foresight([_FS_B])
    content = store.read_long_term()
    # Both bullets present.
    assert content.count("- Alex returns to Project A") == 1
    assert content.count("- If 60s timeout fails") == 1
    # FS_A came first, so it should appear before FS_B.
    assert content.index("Alex returns") < content.index("60s timeout")


def test_dedupes_by_prediction_and_src_ts(store: MemoryStore):
    """Same (prediction, src_ts) is skipped on second call."""
    n1 = store.append_foresight([_FS_A])
    n2 = store.append_foresight([_FS_A])  # exact duplicate
    assert n1 == 1
    assert n2 == 0  # nothing new written
    content = store.read_long_term()
    assert content.count("Alex returns") == 1  # not 2


def test_semantic_dedup_collapses_reworded_repeat_emissions(store: MemoryStore):
    """Same claim with different src_ts is dropped as semantic dup.

    A 30-day longrun showed reworded re-emissions of the same prediction
    filled the foresight cap; we collapse them via Jaccard ≥ 0.6 over
    content tokens.
    """
    store.append_foresight([_FS_A])
    later = dict(_FS_A, src_ts="2026-05-08 10:30")  # same pred, new src
    written = store.append_foresight([later])
    assert written == 0  # semantic dup → skipped
    content = store.read_long_term()
    assert content.count("Alex returns") == 1


def test_semantically_distinct_predictions_both_kept(store: MemoryStore):
    """Two predictions with different topical content both persist."""
    store.append_foresight([_FS_A])  # Project A index work
    written = store.append_foresight([_FS_B])  # 60s timeout / mutex
    assert written == 1
    content = store.read_long_term()
    assert "Alex returns" in content
    assert "mutex" in content


def test_fifo_caps_at_max_keep(store: MemoryStore):
    """When total bullets exceed max_keep, oldest are dropped first.

    Each fixture prediction uses topically distinct vocabulary so the
    semantic dedup (Jaccard ≥ 0.6) doesn't collapse siblings —
    we want to exercise the FIFO path here, not the dedup path.
    """
    distinct_predictions = [
        "Release clawtrack v1.0 after final testing",
        "Submit NeurIPS workshop paper before deadline",
        "Schedule one-on-one with team member Bob",
        "Pick up dry cleaning on Friday evening",
        "Renew passport before international trip",
        "Run Saturday morning 10K route",
        "Plan quarterly retrospective meeting agenda",
        "Reply to editor feedback on chapter 3",
    ]
    initial = [dict(_FS_A, src_ts=f"2026-05-0{i + 1} 10:00", prediction=distinct_predictions[i]) for i in range(5)]
    store.append_foresight(initial, max_keep=10)
    new = [dict(_FS_A, src_ts=f"2026-05-1{i} 10:00", prediction=distinct_predictions[5 + i]) for i in range(3)]
    store.append_foresight(new, max_keep=5)
    content = store.read_long_term()
    # Earliest 3 of the original 5 should have been dropped.
    assert distinct_predictions[0] not in content
    assert distinct_predictions[1] not in content
    assert distinct_predictions[2] not in content
    # Most recent 5 should be present.
    for kept in distinct_predictions[3:8]:
        assert kept in content


def test_empty_input_is_noop(store: MemoryStore):
    """No foresights = no write."""
    written = store.append_foresight([])
    assert written == 0
    assert store.read_long_term() == ""


def test_blank_prediction_is_skipped(store: MemoryStore):
    """Entries with empty prediction text get skipped, not written as bullets."""
    bad = {"prediction": "", "window": "x", "confidence": "low", "src_ts": "x"}
    written = store.append_foresight([_FS_A, bad])
    assert written == 1
    content = store.read_long_term()
    assert content.count("- ") == 1  # only the good one


class TestSpliceAtEndHelper:
    def test_appends_when_section_missing(self):
        content = "# Title\n\n## Projects\n\n- a\n"
        out = _splice_h2_section_at_end(content, "## Foresight", "- pred1")
        assert "## Projects" in out
        # Foresight comes after Projects
        assert out.index("## Projects") < out.index("## Foresight")

    def test_moves_section_to_end_when_already_present_in_middle(self):
        content = (
            "# Title\n\n"
            "## Projects\n\n- p\n\n"
            "## Foresight\n\n- f-old\n\n"  # in the middle
            "## Habits\n\n- h\n"
        )
        out = _splice_h2_section_at_end(content, "## Foresight", "- f-new")
        # Foresight should be moved past Habits to end.
        assert out.index("## Habits") < out.index("## Foresight")
        # Old foresight content gone.
        assert "f-old" not in out
        # New body present.
        assert "f-new" in out
        # Projects/Habits content preserved verbatim.
        assert "- p\n" in out
        assert "- h\n" in out

    def test_keeps_at_end_when_already_at_end(self):
        content = (
            "# Title\n\n"
            "## Projects\n\n- p\n\n"
            "## Foresight\n\n- f-old\n"  # already last
        )
        out = _splice_h2_section_at_end(content, "## Foresight", "- f-new")
        assert out.index("## Projects") < out.index("## Foresight")
        assert "f-old" not in out
        assert "f-new" in out


class TestEnsureForesightAtEnd:
    def test_noop_when_no_foresight(self):
        c = "# Title\n\n## Projects\n\n- p\n"
        assert _ensure_foresight_at_end(c) == c

    def test_noop_when_already_last(self):
        c = "# Title\n\n## Projects\n\n- p\n\n## Foresight\n\n- f\n"
        assert _ensure_foresight_at_end(c) == c

    def test_moves_foresight_when_in_middle(self):
        c = "# Title\n\n## Foresight\n\n- f\n\n## Projects\n\n- p\n\n## Habits\n\n- h\n"
        out = _ensure_foresight_at_end(c)
        # Now Foresight is after Habits
        assert out.index("## Habits") < out.index("## Foresight")
        # Both Projects and Habits content survive
        assert "- p\n" in out
        assert "- h\n" in out
        assert "- f" in out


def test_foresight_section_lands_at_end_after_refresh_runs(tmp_path: Path):
    """End-to-end ordering test: even if Foresight is created FIRST (when
    user.md is empty), once refresh_section subsequently appends a new
    H2 (## Projects), the next foresight write moves ## Foresight back
    to the bottom."""
    store = MemoryStore(tmp_path, now_fn=lambda: datetime(2026, 5, 7, 17, 32))
    # Simulate the case_10 sequence: annotate first (creates Foresight),
    # then refresh_section appends Projects, then a 2nd annotate hits.
    store.append_foresight([_FS_A])
    # Mimic refresh_section appending ## Projects via splicer.
    from raven.memory_engine.consolidate.consolidator import _splice_h2_section

    after_refresh = _splice_h2_section(
        store.read_long_term(),
        "## Projects",
        "- new project bullet [src: episodes.md @ 2026-05-01 09:00]",
    )
    store.write_long_term(after_refresh)
    # Sanity: at this point Foresight is BEFORE Projects (creation order).
    pre = store.read_long_term()
    assert pre.index("## Foresight") < pre.index("## Projects")

    # New foresight append → Foresight should move to bottom.
    store.append_foresight([_FS_B])
    post = store.read_long_term()
    assert post.index("## Projects") < post.index("## Foresight"), (
        "Foresight should be moved to end on subsequent append"
    )


def test_coexists_with_other_h2_sections(tmp_path: Path):
    """Adding ## Foresight doesn't disturb existing ## Projects / ## Habits."""
    store = MemoryStore(tmp_path, now_fn=lambda: datetime(2026, 5, 7, 17, 32))
    seeded = (
        "# Long-term Memory\n\n"
        "## Projects\n\n"
        "- Project B mutex landed [src: episodes.md @ 2026-05-15 09:00]\n\n"
        "## Habits\n\n"
        "- Gym Wed 20:00 [src: episodes.md @ 2026-04-15 21:00]\n"
    )
    store.write_long_term(seeded)
    store.append_foresight([_FS_A])
    content = store.read_long_term()
    # Original sections still byte-identical from heading onward
    assert "## Projects\n\n- Project B mutex landed" in content
    assert "## Habits\n\n- Gym Wed 20:00" in content
    # New section appended
    assert _FORESIGHT_HEADING in content
    assert "Alex returns to Project A" in content


# ---------------------------------------------------------------------------
# annotate() end-to-end persistence.

CASE_06_MESSAGES = [
    {"role": "user", "timestamp": "2026-05-07T09:30", "content": "想优化 Project A 月报, 30 秒太慢"},
    {"role": "user", "timestamp": "2026-05-07T10:30", "content": "Project A 先放, 去修 Project B race condition"},
]


class _FakeProvider(LLMProvider):
    def __init__(self, canned_args):
        super().__init__()
        self._canned = canned_args

    async def chat(self, **kwargs):
        return LLMResponse(
            content=None,
            tool_calls=[
                ToolCallRequest(
                    id="call_0",
                    name="annotate_conversation",
                    arguments=self._canned,
                )
            ],
        )

    def get_default_model(self):
        return "fake-model"


CANNED_WITH_FORESIGHT = {
    "episode_summary": [
        "[2026-05-07 09:30] Alex 提 Project A 月报 30s 慢 #project-a #perf",
        "[2026-05-07 10:30] Alex 暂搁 Project A, 转 Project B race #project-b #pivot #deferred",
    ],
    "foresight_hint": [
        {
            "prediction": "Alex 明日大概率回头处理 Project A 索引方案",
            "window": "1-2 days",
            "confidence": "high",
            "src_ts": "2026-05-07 10:30",
        },
    ],
}


@pytest.mark.asyncio
async def test_annotate_enable_foresight_persists_to_user_md(tmp_path: Path):
    store = MemoryStore(tmp_path, now_fn=lambda: datetime(2026, 5, 7, 17, 32))
    ok = await store.annotate(
        CASE_06_MESSAGES,
        _FakeProvider(CANNED_WITH_FORESIGHT),
        "fake-model",
        enable_foresight=True,
    )
    assert ok is True
    content = store.read_long_term()
    assert _FORESIGHT_HEADING in content
    assert "Alex 明日大概率回头处理 Project A" in content
    assert "(from 2026-05-07 17:32" in content
    assert "src: episodes.md @ 2026-05-07 10:30)" in content


@pytest.mark.asyncio
async def test_annotate_default_off_does_not_create_section(tmp_path: Path):
    """enable_foresight=False (default) → no ## Foresight section, no write."""
    store = MemoryStore(tmp_path, now_fn=lambda: datetime(2026, 5, 7, 17, 32))
    # Canned response WITHOUT foresight_hint (matches default tool schema)
    canned_no_foresight = {"episode_summary": CANNED_WITH_FORESIGHT["episode_summary"]}
    ok = await store.annotate(
        CASE_06_MESSAGES,
        _FakeProvider(canned_no_foresight),
        "fake-model",  # enable_foresight defaults to False
    )
    assert ok is True
    assert store.read_long_term() == ""  # nothing persisted
