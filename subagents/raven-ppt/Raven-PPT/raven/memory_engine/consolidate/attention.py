"""Parser / renderer for ``user_memory/attention.md``.

attention.md is sentinel/cron's projection of derived, time-sensitive
observations about the user — habits, predictions, pending decisions,
proactive ledger — into a single human-readable markdown file. Section
order is fixed by ``ATTENTION_SECTIONS`` (14 H2s including the
diagnostic). Each section's body is opaque to the parser; the
single-writer-per-H2 convention keeps concurrent producers from
stepping on each other under the shared ``attention.md.lock``.

Distinct from user.md (stable profile) and episodes.md (event log):
attention.md is **derived state** that's recomputed by sentinel/cron
producers each tick.
"""

from __future__ import annotations

import re

# Canonical H2 sections (English titles).
ATTENTION_SECTIONS: tuple[str, ...] = (
    "## User overrides",
    "## Recent stance log (30d)",
    "## Pending proposals",
    "## Rejected proposals (cooldown)",
    "## Recent proactive decisions (14d)",
    "## Cross-project behavior patterns (14d)",
    "## Active threads",
    "## Currently focused on",
    "## Predicted next 3 days",
    # Daily Planning output — list of fires the agent intends to deliver
    # today. Re-generated once per day at the first tick after 06:00.
    # Planner reads this to bias its tick decisions toward the planned
    # cadence (avoiding cross-topic conflicts + DND windows globally).
    "## 今日 fire 计划",
    "## Project rhythm (last 7 days)",
    "## Recently abandoned, worth resuming",
    "## Archived patterns",
    # Diagnostic — NudgePolicy state + per-topic acceptance rollup.
    # Sits at the bottom so the foreground user-facing sections stay
    # visually grouped at the top.
    "## Sentinel Observations (auto)",
)

# Maps Chinese H2 aliases to canonical English.
ATTENTION_ALIASES: dict[str, str] = {
    "## 用户指令": "## User overrides",
    "## 活跃话题": "## Active threads",
    "## 下一步预测": "## Predicted next 3 days",
    "## 最近放弃": "## Recently abandoned, worth resuming",
    "## 项目节奏": "## Project rhythm (last 7 days)",
    "## 当前聚焦": "## Currently focused on",
    "## 跨项目活跃话题(14天)": "## Cross-project behavior patterns (14d)",
    "## 值得续作的已放弃": "## Recently abandoned, worth resuming",
    "## 最近主动决策(14天)": "## Recent proactive decisions (14d)",
    "## 未来3日预测": "## Predicted next 3 days",
    "## 项目节奏(7天)": "## Project rhythm (last 7 days)",
    "## 近期立场日志(30天)": "## Recent stance log (30d)",
    "## 待处理提议": "## Pending proposals",
    "## 已拒绝提议(冷却中)": "## Rejected proposals (cooldown)",
    "## 已归档模式": "## Archived patterns",
}


_H2_RE = re.compile(r"^(## [^\n]+)$", re.MULTILINE)


def parse_attention(text: str) -> dict[str, str]:
    """Split ``text`` into ``{canonical_h2: body}`` in document order.

    Aliases in ``ATTENTION_ALIASES`` are normalized to canonical English
    titles. Sections whose H2 is not in ``ATTENTION_SECTIONS`` or an
    alias are still preserved under their original heading (forward-
    compat for ad-hoc sections added before this parser knows them).

    Body strings include all content up to but not including the next
    H2 line, with trailing whitespace stripped.
    """
    if not text:
        return {}
    matches = list(_H2_RE.finditer(text))
    if not matches:
        return {}
    out: dict[str, str] = {}
    for i, m in enumerate(matches):
        heading = m.group(1).strip()
        canonical = ATTENTION_ALIASES.get(heading, heading)
        body_start = m.end()
        body_end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        body = text[body_start:body_end].strip("\n")
        out[canonical] = body
    return out


def render_attention(
    sections: dict[str, str],
    *,
    include_empty: bool = False,
) -> str:
    """Render ``sections`` back to markdown in canonical section order.

    Unknown sections (not in ``ATTENTION_SECTIONS``) are appended after
    the canonical ones in insertion order. Empty bodies are skipped
    unless ``include_empty`` is set (useful for cold-start scaffolding).
    """
    parts: list[str] = []
    seen: set[str] = set()
    for h2 in ATTENTION_SECTIONS:
        body = sections.get(h2, "").strip()
        seen.add(h2)
        if not body and not include_empty:
            continue
        parts.append(h2)
        if body:
            parts.append(body)
        parts.append("")
    for h2, body in sections.items():
        if h2 in seen:
            continue
        body = body.strip()
        if not body and not include_empty:
            continue
        parts.append(h2)
        if body:
            parts.append(body)
        parts.append("")
    text = "\n".join(parts).rstrip()
    return text + "\n" if text else ""


def upsert_section(text: str, h2: str, body: str) -> str:
    """Replace ``h2``'s body in ``text`` with ``body``, or append the
    section if absent. Preserves all other sections verbatim.

    Single entry point for ``AttentionUpdater._splice_and_write`` to
    splice each producer's output into the merged file under one lock
    acquisition.
    """
    sections = parse_attention(text)
    sections[h2] = body.strip()
    return render_attention(sections)


__all__ = [
    "ATTENTION_SECTIONS",
    "ATTENTION_ALIASES",
    "parse_attention",
    "render_attention",
    "upsert_section",
]
