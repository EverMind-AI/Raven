"""Memory system for persistent agent memory."""

from __future__ import annotations

import asyncio
import json
import re
import sys
import weakref
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterator

from loguru import logger

from raven.utils.helpers import ensure_dir, estimate_message_tokens, estimate_prompt_tokens_chain

if TYPE_CHECKING:
    from raven.providers.base import LLMProvider
    from raven.session.manager import Session, SessionManager


_SAVE_MEMORY_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "save_memory",
            "description": (
                "Persist this conversation's consolidation across 3 memory pillars: "
                "profile_update (stable facts → user.md), episode_summary (timestamped "
                "events → episodes.md), foresight_hint (predicted future intents → "
                "behaviors.md). All 3 slots must be present; episode_summary and "
                "foresight_hint may be empty arrays."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "profile_update": {
                        "type": "string",
                        "description": (
                            "Full updated user.md as markdown. Include all existing "
                            "facts plus new ones. EVERY bullet MUST end with "
                            "'[src: episodes.md @ YYYY-MM-DD HH:MM]' linking to the "
                            "source episode timestamp. For pre-existing bullets that "
                            "lack a source, write '[src: episodes.md @ unknown]' "
                            "rather than fabricate a timestamp. Return the input "
                            "unchanged if no new facts emerged."
                        ),
                    },
                    "episode_summary": {
                        "type": "array",
                        "description": (
                            "One entry per distinct event in this conversation. "
                            "Each entry is a SINGLE LINE (no newlines), formatted "
                            "exactly as: '[YYYY-MM-DD HH:MM] <one-line summary, "
                            "<=100 chars> #tag1 #tag2'. Tags: 1-4 short kebab-case "
                            "nouns drawn from {#project-<slug>, #perf, #bug, #habit, "
                            "#task, #decision, #blocker, #deferred, #question, "
                            "#answer, #pivot, #infra, #sql, #ml}. Order entries by "
                            "their conversation timestamp. Empty array only if the "
                            "conversation produced no substantive event."
                        ),
                        "items": {"type": "string"},
                    },
                    "foresight_hint": {
                        "type": "array",
                        "description": (
                            "Predictions / deferred intents inferred from this "
                            "conversation. Fill ONLY when the user (a) explicitly "
                            "defers a task, (b) expresses a recurring habit with a "
                            "clear time anchor, or (c) commits to a specific future "
                            "action. Empty array if no such signal."
                        ),
                        "items": {
                            "type": "object",
                            "required": [
                                "prediction", "window", "confidence", "src_ts",
                            ],
                            "properties": {
                                "prediction": {
                                    "type": "string",
                                    "description": "One-line natural-language prediction, <=120 chars.",
                                },
                                "window": {
                                    "type": "string",
                                    "description": (
                                        "When the prediction applies — e.g. '1-2 days', "
                                        "'next Monday 09:00', 'recurring weekly'."
                                    ),
                                },
                                "confidence": {
                                    "type": "string",
                                    "enum": ["low", "medium", "high"],
                                },
                                "src_ts": {
                                    "type": "string",
                                    "description": (
                                        "Timestamp 'YYYY-MM-DD HH:MM' of the episode "
                                        "that triggered this prediction."
                                    ),
                                },
                            },
                        },
                    },
                },
                "required": [
                    "profile_update", "episode_summary", "foresight_hint",
                ],
            },
        },
    }
]


def _ensure_text(value: Any) -> str:
    """Normalize tool-call payload values to text for file storage."""
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


_HISTORY_TS_RE = re.compile(r"^\s*\[(\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2})")
_HISTORY_TS_FORMATS = (
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M",
)


def _parse_history_paragraph_ts_ms(paragraph: str) -> int | None:
    """Pull the leading ``[YYYY-MM-DD HH:MM]`` timestamp from a HISTORY.md
    paragraph and return it as epoch ms (local-time interpretation).
    Returns None if the paragraph has no parseable stamp."""
    m = _HISTORY_TS_RE.match(paragraph)
    if not m:
        return None
    raw = m.group(1)
    for fmt in _HISTORY_TS_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
        except ValueError:
            continue
        return int(dt.timestamp() * 1000)
    return None


def _normalize_save_memory_args(args: Any) -> dict[str, Any] | None:
    """Normalize provider tool-call arguments to the expected dict shape."""
    if isinstance(args, str):
        args = json.loads(args)
    if isinstance(args, list):
        return args[0] if args and isinstance(args[0], dict) else None
    return args if isinstance(args, dict) else None


# Split consolidation: episode/foresight annotation (light, every
# trigger) vs profile-section refresh (heavy, only on tag heat).
# Foresight emission is opt-in — default off keeps the tool schema
# minimal and tokens cheap.


def _build_annotate_tool(*, enable_foresight: bool) -> list[dict]:
    """Construct the ``annotate_conversation`` tool.

    The ``foresight_hint`` slot is included only when ``enable_foresight``
    is True. With the flag off, the LLM isn't asked for predictions at
    all — saves tokens and simplifies the prompt.
    """
    properties: dict[str, dict] = {
        "episode_summary": {
            "type": "array",
            "description": (
                "One entry per distinct event. Each entry is a SINGLE LINE "
                "(no newlines), formatted exactly as:\n"
                "  '[YYYY-MM-DD HH:MM] <summary, <=100 chars> #tag1 #tag2'\n\n"
                "SUMMARY — must include concrete identifiers (file paths, "
                "function names, PR/issue numbers, percentages, time/size "
                "values). Generic descriptions waste a slot.\n"
                "  GOOD: 'PR #1287 merged: require_auth(scope) replaces 6 "
                "sites in api/views/+middleware/'\n"
                "  BAD:  'User worked on auth refactor'\n\n"
                "TAGS — 1-4 tags per entry, kebab-case. Two CLASSES:\n"
                "  (A) CONTENT tags — name WHAT the episode is about. "
                "Every episode MUST carry at least one content tag. "
                "Use existing slugs from the 'tags you've recently used' "
                "list (see prompt) before inventing new ones — DO NOT "
                "split one project across multiple slugs like "
                "#project-clawtrack-release / -docs / -cli; pick ONE "
                "stable slug per project. New project tags follow "
                "'#project-<work-slug>' where slug names the WORK, not "
                "the codebase. Other content tags: {#perf, #bug, "
                "#decision, #blocker, #deferred, #pivot, #pr, #review, "
                "#rfc, #design, #infra, #sql, #ml}.\n"
                "  (B) PROCESS tags — {#question, #habit, #answer} "
                "describe HOW the user is interacting, not WHAT about. "
                "They are SUFFIXES only — NEVER the primary tag. "
                "An episode tagged ONLY '#question' or ONLY '#habit' is "
                "INVALID and will be rejected. Always pair with at "
                "least one content tag.\n"
                "  AVOID '#task' entirely — it's meaningless filler.\n\n"
                "Order entries by conversation timestamp. Empty array only "
                "when the chunk produced no substantive event."
            ),
            "items": {"type": "string"},
        },
    }
    required: list[str] = ["episode_summary"]
    description = (
        "Annotate this conversation chunk for episodic memory. Produces "
        "tagged episode lines. Does NOT update the user profile — that "
        "happens separately via refresh_profile_section when tag "
        "frequency warrants a focused rewrite."
    )
    if enable_foresight:
        properties["foresight_hint"] = {
            "type": "array",
            "description": (
                "Predictions / behavioral patterns inferred from this "
                "conversation. Fill when ANY of these signals present:\n"
                "(a) User explicitly defers a task ('I'll come back to "
                "X tomorrow', 'next sprint').\n"
                "(b) A recurring pattern visible across 2+ episodes "
                "(e.g. Saturday runs across multiple weeks → predict "
                "next Saturday run; Sunday-night planning → predict "
                "next Sunday planning). Look back at the 'tags you've "
                "recently used' list — if it shows recurring habits, "
                "emit them as foresight.\n"
                "(c) User commits to a specific future action with "
                "time anchor ('I'll write RFC tomorrow', "
                "'release Monday 9am').\n"
                "(d) An upcoming dated event mentioned in conversation "
                "('birthday 5/25', 'demo next Friday', 'deadline EOM').\n"
                "Empty array ONLY if none of (a)-(d) signals present. "
                "Default lean: emit foresight when reasonable — a "
                "low-confidence prediction is more useful than no "
                "prediction. Aim for 1-3 entries per substantive "
                "annotate call."
            ),
            "items": {
                "type": "object",
                "required": [
                    "prediction", "window", "confidence", "src_ts",
                ],
                "properties": {
                    "prediction": {"type": "string"},
                    "window": {"type": "string"},
                    "confidence": {
                        "type": "string",
                        "enum": ["low", "medium", "high"],
                    },
                    "src_ts": {"type": "string"},
                },
            },
        }
        required.append("foresight_hint")
        description = (
            "Annotate this conversation chunk for episodic memory. "
            "Produces tagged episode lines and foresight predictions. "
            "Does NOT update the user profile — that happens separately "
            "via refresh_profile_section when tag frequency warrants a "
            "focused rewrite."
        )
    return [{
        "type": "function",
        "function": {
            "name": "annotate_conversation",
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }]


_REFRESH_SECTION_TOOL = [
    {
        "type": "function",
        "function": {
            "name": "refresh_profile_section",
            "description": (
                "Rewrite ONE H2 section of user.md given recent episodes "
                "tagged with a specific topic. Other H2 sections are left "
                "untouched by the splicer — do not include their content."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "section_heading": {
                        "type": "string",
                        "description": (
                            "Exact H2 heading line to replace, e.g. "
                            "'## Projects' or '## Habits'. Must include "
                            "the leading '## '. If the topic naturally fits "
                            "inside an existing H2 (e.g. tag #project-b -> "
                            "'## Projects'), use that existing heading and "
                            "structure project-specific content under H3 in "
                            "the body. Only create a new H2 if no existing "
                            "section fits."
                        ),
                    },
                    "section_body": {
                        "type": "string",
                        "description": (
                            "New markdown body for this section, NOT "
                            "including the heading line itself. Every bullet "
                            "MUST end with '[src: episodes.md @ "
                            "YYYY-MM-DD HH:MM]'. H3/H4 sub-headings are "
                            "allowed within the body."
                        ),
                    },
                },
                "required": ["section_heading", "section_body"],
            },
        },
    }
]


_EPISODE_LINE_RE = re.compile(
    r"^\s*\[(\d{4}-\d{2}-\d{2}[T ]\d{1,2}:\d{2})\]\s+(.*?)\s*$"
)
_TAG_RE = re.compile(r"#([a-z][a-z0-9-]*)")


def _parse_episode_line(line: str) -> tuple[str, str, list[str]] | None:
    """Split an episodes.md line into (timestamp, summary, tags).

    Returns None for lines that don't match the
    ``[YYYY-MM-DD HH:MM] <summary> #tag #tag`` shape. Tag tokens are
    stripped from the returned summary.
    """
    m = _EPISODE_LINE_RE.match(line)
    if not m:
        return None
    ts, body = m.group(1), m.group(2)
    tags = _TAG_RE.findall(body)
    summary = _TAG_RE.sub("", body).strip()
    return ts, summary, tags


# Code-layer guards.
#
# Each of these enforces a rule the prompt already states but the LLM
# does not reliably follow at 30-day scale. Kept as pure functions so
# they can be unit-tested in isolation and reasoned about without the
# rest of MemoryStore.

# Stored without the leading '#' since ``_TAG_RE`` already strips it
# when parsing episode lines.
_PROCESS_TAGS: frozenset[str] = frozenset({"question", "habit", "answer"})
_VALID_CONFIDENCE: frozenset[str] = frozenset({"low", "medium", "high"})
_SRC_LINK_RE = re.compile(
    r"\[src:\s+episodes\.md\s+@\s+\d{4}-\d{2}-\d{2}\s+\d{1,2}:\d{2}\]"
)
# Tokens stripped before semantic dedup of foresight predictions —
# common subjects/auxiliaries/framing words that carry no topical content.
_FORESIGHT_DEDUP_STOPWORDS: frozenset[str] = frozenset({
    "user", "will", "may", "might", "likely", "again",
    "today", "tomorrow", "next", "this", "that",
    "the", "for", "with", "from", "and", "are", "has", "have",
    "continue", "recurring", "pattern", "habit",
})
_FORESIGHT_TOKEN_RE = re.compile(r"[a-zA-Z]{4,}|[一-鿿]{2,}")
# Jaccard threshold for "same claim, reworded". 0.6 chosen empirically:
# catches recurring-habit clusters like "Saturday run x4" or "daily
# medication reminders x6" while leaving obviously-distinct predictions
# alone.
_FORESIGHT_SEMANTIC_DUP_JACCARD: float = 0.6


def _stem_trailing_s(token: str) -> str:
    """Cheap plural→singular: trim trailing single 's' from tokens ≥5 chars
    that don't end in 'ss'.

    Handles ``reminders → reminder``, ``meetings → meeting``,
    ``mondays → monday`` so Jaccard catches sibling-form duplicates
    without pulling in a real stemmer (nltk PorterStemmer is overkill
    for the volume we process). Does NOT touch ``boss``, ``class``,
    ``-ing`` / ``-ed`` forms — those misses are acceptable given the
    failure mode (occasional missed dedup) vs. the cost (heavyweight
    morphology + extra dep).
    """
    if len(token) >= 5 and token.endswith("s") and not token.endswith("ss"):
        return token[:-1]
    return token


def _is_process_only_episode(line: str) -> bool:
    """True iff the episode's only tags are #question/#habit/#answer.

    Prompt declares these episodes INVALID (process tags must accompany
    a content tag). At 30-day scale the LLM still emits them ~5% of the
    time; this filter drops them at the annotate-writeback boundary so
    they never reach episodes.md or trigger refresh_section.

    Unparseable / untagged lines fall through (return False) so we don't
    accidentally suppress unrelated freeform notes.
    """
    parsed = _parse_episode_line(line)
    if not parsed:
        return False
    _, _, tags = parsed
    if not tags:
        return False
    return {t.lower() for t in tags}.issubset(_PROCESS_TAGS)


def _normalize_confidence(value: str) -> str:
    """Return value if in {low, medium, high}, else '?'.

    LLM occasionally emits 'strong' / 'likely' / 'definite' instead of
    the prompt-specified enum; rendering '?' makes the deviation visible
    in user.md rather than silently persisting bad values.
    """
    v = (value or "").strip().lower()
    return v if v in _VALID_CONFIDENCE else "?"


def _foresight_token_set(prediction: str) -> frozenset[str]:
    """Tokenize a foresight prediction for semantic-dedup comparison.

    English words ≥4 chars + CJK runs ≥2 chars, lowercased, minus
    high-frequency framing words ('user will ...', 'recurring habit',
    etc.) that carry no topical content. Each surviving token is then
    s-stemmed (``_stem_trailing_s``) so plural/singular siblings collapse
    to one form (``reminders``/``reminder``, ``meetings``/``meeting``).
    """
    raw = _FORESIGHT_TOKEN_RE.findall(prediction.lower())
    return frozenset(
        _stem_trailing_s(t)
        for t in raw
        if t not in _FORESIGHT_DEDUP_STOPWORDS
    )


def _is_semantic_duplicate_foresight(
    new_pred: str, existing_preds: list[str],
) -> bool:
    """True iff ``new_pred`` overlaps any existing prediction by Jaccard
    >= threshold over content tokens.

    The (prediction, src_ts) dedupe in append_foresight is exact-string;
    it lets through reworded re-emissions of the same semantic claim
    ('User runs every Saturday morning' vs '...morning (recurring
    habit)'). This Jaccard check catches those.
    """
    new_tokens = _foresight_token_set(new_pred)
    if not new_tokens:
        return False
    for ex in existing_preds:
        ex_tokens = _foresight_token_set(ex)
        if not ex_tokens:
            continue
        union = new_tokens | ex_tokens
        if not union:
            continue
        jaccard = len(new_tokens & ex_tokens) / len(union)
        if jaccard >= _FORESIGHT_SEMANTIC_DUP_JACCARD:
            return True
    return False


def _drop_bullets_without_src(body: str) -> tuple[str, int]:
    """Strip profile bullets that lack a ``[src: episodes.md @ ts]`` link.

    Non-bullet lines (blank, headings, prose) are preserved verbatim.
    Bullets without the evidence link are dropped — the prompt mandates
    every profile bullet cite its source episode timestamp.

    Returns (cleaned_body, n_dropped).
    """
    kept: list[str] = []
    dropped = 0
    for line in body.splitlines():
        stripped = line.lstrip()
        if not stripped.startswith("- "):
            kept.append(line)
            continue
        if _SRC_LINK_RE.search(line):
            kept.append(line)
        else:
            dropped += 1
    return "\n".join(kept), dropped


# Foresight predictions persist to user.md ## Foresight section.
# Bullet format (single line, mirrors the [src:]-style evidence link used
# by profile bullets):
#   - <prediction> (from <gen_ts>, window: <range>, confidence: <level>, src: episodes.md @ <ep_ts>)
# ``from`` = wall time when annotate() emitted this prediction; ``src`` =
# the episode timestamp that triggered it (LLM-provided src_ts).
_FORESIGHT_HEADING = "## Foresight"
_FORESIGHT_BULLET_RE = re.compile(
    r"^-\s+(?P<prediction>.+?)\s+"
    r"\(from\s+(?P<gen_ts>[^,]+),\s+"
    r"window:\s+(?P<window>[^,]+),\s+"
    r"confidence:\s+(?P<confidence>[^,]+),\s+"
    r"src:\s+episodes\.md\s+@\s+(?P<src_ts>.+?)\)\s*$"
)


def _format_foresight_bullet(entry: dict[str, Any], generation_ts: str) -> str:
    """Render a foresight dict as a single user.md bullet line.

    Tolerates missing/blank fields (substitutes ``?``) so partial LLM
    output still writes a usable record we can review later.
    """
    pred = (entry.get("prediction") or "").strip() or "?"
    window = (entry.get("window") or "").strip() or "?"
    # Enforce {low|medium|high}; off-enum values render as '?'.
    confidence = _normalize_confidence(entry.get("confidence") or "")
    src_ts = (entry.get("src_ts") or "").strip() or "?"
    return (
        f"- {pred} "
        f"(from {generation_ts}, window: {window}, "
        f"confidence: {confidence}, src: episodes.md @ {src_ts})"
    )


_RELEVANCE_TOKEN_RE = re.compile(r"\w{2,}", re.UNICODE)


def _tokenize_for_relevance(text: str) -> set[str]:
    """Lowercased set of 2+ char alphanumeric/CJK runs. Lightweight stand-in
    for proper tokenization — Chinese runs become single multi-char tokens
    (e.g. a 3-character phrase is one token), English splits on whitespace + punctuation."""
    return {t.lower() for t in _RELEVANCE_TOKEN_RE.findall(text)}


def _parse_user_md_sections(content: str) -> dict[str, str]:
    """Return ``{H2_heading_line: body}`` for every H2 section in ``content``.

    The H1 preamble (text before the first H2) is dropped. Body is the
    text between the heading and the next H2 (or EOF) with leading and
    trailing blank lines stripped. Order of insertion matches order in
    the source file.
    """
    lines = content.splitlines()
    sections: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for line in lines:
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buf).strip("\n")
            current = line.strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        sections[current] = "\n".join(buf).strip("\n")
    return sections


def _score_section_relevance(query: str, heading: str, body: str) -> float:
    """Lexical-overlap relevance: how much query vocabulary appears in
    heading or body. Heading hits weighted 3x because section titles are
    short and intentional, e.g. asking about "Projects" should reliably
    pull '## Projects'."""
    q_tokens = _tokenize_for_relevance(query)
    if not q_tokens:
        return 0.0
    heading_hits = len(q_tokens & _tokenize_for_relevance(heading))
    body_hits = len(q_tokens & _tokenize_for_relevance(body))
    return heading_hits * 3.0 + body_hits


def _splice_h2_section_at_end(content: str, heading: str, new_body: str) -> str:
    """Like ``_splice_h2_section`` but guarantees the named section is at
    the **end** of the file, regardless of its current position.

    Behavior:
    - If the section doesn't exist: appended at end (same as
      ``_splice_h2_section``'s fallback path).
    - If the section already exists anywhere: first removed from that
      position, then appended at end with the new body.

    Used by ``append_foresight`` so the auto-managed ## Foresight pillar
    stays visually at the bottom of user.md no matter what order
    refresh_section appended ## Projects / ## Habits / etc.
    """
    lines = content.splitlines()
    target = heading.strip()

    h_idx = None
    for i, ln in enumerate(lines):
        if ln.strip() == target:
            h_idx = i
            break

    if h_idx is not None:
        # Find next H2 boundary (or EOF) to delimit this section.
        next_h2 = None
        for i in range(h_idx + 1, len(lines)):
            if lines[i].startswith("## "):
                next_h2 = i
                break
        if next_h2 is not None:
            lines = lines[:h_idx] + lines[next_h2:]
        else:
            lines = lines[:h_idx]

    content_without = "\n".join(lines).rstrip("\n")
    body = new_body.strip("\n")
    if not content_without:
        return f"{target}\n\n{body}\n"
    return f"{content_without}\n\n{target}\n\n{body}\n"


def _ensure_foresight_at_end(content: str) -> str:
    """If ``## Foresight`` exists in ``content`` but isn't the last H2
    section, move it to the end. Idempotent — returns ``content``
    unchanged when Foresight is absent or already last.

    Called by ``refresh_section``'s writer after any non-Foresight H2
    splice so the auto-managed Foresight pillar can't get visually
    buried by a freshly-appended ``## Projects`` / ``## Habits`` etc.
    """
    sections = _parse_user_md_sections(content)
    if _FORESIGHT_HEADING not in sections:
        return content
    h2_order = list(sections.keys())
    if h2_order and h2_order[-1] == _FORESIGHT_HEADING:
        return content
    body = sections[_FORESIGHT_HEADING]
    return _splice_h2_section_at_end(content, _FORESIGHT_HEADING, body)


def _splice_h2_section(content: str, heading: str, new_body: str) -> str:
    """Return a copy of ``content`` with the body of the H2 section
    identified by ``heading`` replaced by ``new_body``.

    The body is everything from the line after the heading up to (but
    not including) the next H2 — or EOF if heading is the last H2.
    H1 preamble and other H2 sections are preserved byte-for-byte.

    If the heading isn't found, ``heading`` + ``new_body`` is appended
    as a fresh section at end of file.
    """
    lines = content.splitlines()
    target = heading.strip()

    h_idx = None
    for i, ln in enumerate(lines):
        if ln.strip() == target:
            h_idx = i
            break

    if h_idx is None:
        sep = "\n\n" if content and not content.endswith("\n\n") else ""
        return (
            content.rstrip("\n") + sep + "\n" + target + "\n\n"
            + new_body.strip("\n") + "\n"
        )

    # Find next H2 (lines starting with "## " but not "### " etc.).
    next_h2 = None
    for i in range(h_idx + 1, len(lines)):
        if lines[i].startswith("## "):
            next_h2 = i
            break

    before = lines[: h_idx + 1]
    after = lines[next_h2:] if next_h2 is not None else []
    body_lines = new_body.strip("\n").splitlines()

    pieces = list(before) + [""] + body_lines + [""]
    if after:
        pieces.extend(after)
    return "\n".join(pieces).rstrip("\n") + "\n"


class MemoryStore:
    """Two-layer memory: MEMORY.md (long-term facts) + HISTORY.md (grep-searchable log)."""

    def __init__(
        self, workspace: Path,
        now_fn: Callable[[], datetime] | None = None,
    ):
        # User profile + episodic log live under the ``user_memory``
        # pillar. ``memory_dir`` is an alias of ``memory_file.parent``
        # for callsites that derive sibling paths (lock file below).
        self.memory_file = ensure_dir(workspace / "user_memory" / "profile") / "user.md"
        self.history_file = ensure_dir(workspace / "user_memory" / "episodic") / "episodes.md"
        self.memory_dir = self.memory_file.parent
        # Sibling lock file (`MEMORY.md.lock`). Shared across all processes
        # that write MEMORY.md so Personalizer + MemoryConsolidator +
        # SentinelMemoryWriter serialize on the same fcntl. POSIX-only —
        # falls through to no-op on win32.
        self.memory_lock_path = self.memory_file.with_suffix(
            self.memory_file.suffix + ".lock"
        )

        # attention.md + behaviors.md siblings at user_memory root.
        # Independent fcntl locks: sentinel writes attention.md frequently
        # and shouldn't contend with Personalizer/Consolidator on user.md.
        user_memory_root = ensure_dir(workspace / "user_memory")
        self.attention_file = user_memory_root / "attention.md"
        self.behaviors_file = user_memory_root / "behaviors.md"
        self.behaviors_offsets_path = (
            user_memory_root / ".behaviors_offsets.json"
        )
        # Sidecar JSON for ``## Recent stance log`` — stance_log's source
        # of truth, distinct from the attention.md section that just
        # renders it. Lets the producer mutate state without holding the
        # attention.md write lock (which it can't, since compute_body
        # runs in Phase 1 outside the lock).
        self.stance_log_path = user_memory_root / ".stance_log.json"
        self.attention_lock_path = self.attention_file.with_suffix(
            self.attention_file.suffix + ".lock"
        )
        self.behaviors_lock_path = self.behaviors_file.with_suffix(
            self.behaviors_file.suffix + ".lock"
        )
        self.stance_log_lock_path = (
            user_memory_root / ".stance_log.json.lock"
        )

        # Used by ``consolidate`` to inject ``Current Time:`` into the
        # consolidator-LLM prompt — without this, the LLM's
        # summary-paragraph timestamps fall back to wall clock even when
        # session entries it sees were tagged with a fake clock.
        self._now_fn = now_fn or datetime.now

    @contextmanager
    def locked(self) -> Iterator[None]:
        """Hold an exclusive fcntl lock on MEMORY.md so concurrent writers
        across REPL + gateway processes don't clobber each other.

        Usage:
            with memory.locked():
                cur = memory.read_long_term()
                memory.write_long_term(cur + "...")
        """
        yield from self._fcntl_locked(self.memory_lock_path)

    @contextmanager
    def locked_attention(self) -> Iterator[None]:
        """Exclusive lock on ``attention.md.lock`` — independent of the
        user.md lock pool so sentinel writers don't block on Personalizer
        or MemoryConsolidator."""
        yield from self._fcntl_locked(self.attention_lock_path)

    @contextmanager
    def locked_behaviors(self) -> Iterator[None]:
        """Exclusive lock on ``behaviors.md.lock`` — separate from user.md
        and attention.md so the idle-triggered extractor's slow LLM call
        doesn't block hot-path writers on either of the other files."""
        yield from self._fcntl_locked(self.behaviors_lock_path)

    @contextmanager
    def locked_stance_log(self) -> Iterator[None]:
        """Exclusive lock on the stance-log sidecar JSON. Held during
        read-merge-write in StanceLogProducer so concurrent ticks
        (eval harness + gateway, REPL + gateway) don't resurrect
        FIFO-trimmed entries via lost-update races."""
        yield from self._fcntl_locked(self.stance_log_lock_path)

    def _fcntl_locked(self, lock_path: Path) -> Iterator[None]:
        if sys.platform == "win32":
            yield
            return
        # `import` here so non-POSIX import doesn't fail at module load
        import fcntl
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a") as fh:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)

    def read_long_term(self) -> str:
        if self.memory_file.exists():
            return self.memory_file.read_text(encoding="utf-8")
        return ""

    def write_long_term(self, content: str) -> None:
        self.memory_file.write_text(content, encoding="utf-8")

    def _safe_write_long_term(self, new_content: str, expected_prev: str) -> bool:
        """Compare-and-set write under the lock. Used by consolidate() to avoid
        clobbering a concurrent writer's update during the LLM call.

        Returns True if the write happened; False if MEMORY.md changed under
        us (we lose the race; caller should log + move on).
        """
        with self.locked():
            current = self.read_long_term()
            if current != expected_prev:
                logger.info(
                    "MemoryStore: consolidate skipped write — concurrent "
                    "modification detected (lost race with another writer); "
                    "next consolidation will fold our turn back in"
                )
                return False
            self.write_long_term(new_content)
            return True

    def append_history(self, entry: str) -> None:
        with open(self.history_file, "a", encoding="utf-8") as f:
            f.write(entry.rstrip() + "\n\n")

    _FORESIGHT_MAX_KEEP_DEFAULT = 20

    def append_foresight(
        self,
        foresights: list[dict[str, Any]],
        *,
        max_keep: int | None = None,
    ) -> int:
        """Persist LLM-emitted foresight predictions to ``## Foresight`` in
        user.md.

        - Creates the section if it doesn't exist (appended at end of file
          via ``_splice_h2_section`` fallback).
        - **Dedupes** by ``(prediction text, src_ts)`` — same prediction
          from same source episode is skipped.
        - **FIFO caps** total bullet count at ``max_keep`` (default 20).
          Older entries drop off the front so the section stays scannable.
        - Atomic under the memory lock; safe vs. concurrent
          consolidator/Personalizer/SentinelMemoryWriter.

        Returns the number of new entries actually written (post-dedupe).
        Empty input or all-dupes returns 0 without touching the file.
        """
        if not foresights:
            return 0
        cap = max_keep if max_keep is not None else self._FORESIGHT_MAX_KEEP_DEFAULT
        gen_ts = self._now_fn().strftime("%Y-%m-%d %H:%M")

        with self.locked():
            current = self.read_long_term()
            sections = _parse_user_md_sections(current)

            # Existing bullets in ## Foresight section, preserved verbatim
            # so we don't churn formatting on rewrite.
            existing_bullets: list[str] = []
            if _FORESIGHT_HEADING in sections:
                for line in sections[_FORESIGHT_HEADING].splitlines():
                    if line.lstrip().startswith("-"):
                        existing_bullets.append(line.rstrip())

            existing_keys: set[tuple[str, str]] = set()
            existing_predictions: list[str] = []
            for line in existing_bullets:
                m = _FORESIGHT_BULLET_RE.match(line)
                if m:
                    pred_text = m.group("prediction").strip()
                    existing_keys.add(
                        (pred_text, m.group("src_ts").strip())
                    )
                    existing_predictions.append(pred_text)

            new_bullets: list[str] = []
            written = 0
            semantic_skipped = 0
            for fs in foresights:
                pred = (fs.get("prediction") or "").strip()
                src_ts = (fs.get("src_ts") or "").strip()
                if not pred:
                    continue
                key = (pred, src_ts)
                if key in existing_keys:
                    continue
                # Semantic dedup. The (prediction, src_ts) key is
                # exact-string; LLM produces reworded re-emissions of the
                # same claim from different episodes that all slip
                # through. Block them via Jaccard over content tokens
                # (also blocks dupes within this very batch).
                if _is_semantic_duplicate_foresight(pred, existing_predictions):
                    semantic_skipped += 1
                    continue
                new_bullets.append(_format_foresight_bullet(fs, gen_ts))
                existing_keys.add(key)
                existing_predictions.append(pred)
                written += 1
            if semantic_skipped:
                logger.info(
                    "append_foresight: skipped {} semantic-duplicate "
                    "prediction(s)",
                    semantic_skipped,
                )

            if written == 0:
                return 0

            all_bullets = existing_bullets + new_bullets
            # FIFO: keep most recent ``cap`` entries (drop oldest)
            if len(all_bullets) > cap:
                all_bullets = all_bullets[-cap:]

            body = "\n".join(all_bullets)
            base = current or "# Long-term Memory\n"
            # Always-at-end variant: if ## Foresight already exists in the
            # middle of the file (e.g. because annotate ran before any
            # refresh_section created ## Projects), this moves it to the
            # bottom so the visual order is "stable profile sections first,
            # auto-managed Foresight last".
            new_content = _splice_h2_section_at_end(
                base, _FORESIGHT_HEADING, body,
            )
            if new_content != current:
                self.write_long_term(new_content)
            return written

    def read_history_since(self, since_ms: int) -> str:
        """Return HISTORY.md entries whose leading ``[YYYY-MM-DD HH:MM]``
        timestamp is ``>= since_ms``.

        Entries are blank-line separated paragraphs (per ``append_history``)
        and the consolidator prompt instructs the LLM to start each one with
        a ``[YYYY-MM-DD HH:MM]`` stamp. We split on ``\\n\\n``, parse the
        leading stamp, and keep paragraphs newer than ``since_ms``.

        Paragraphs with a malformed or missing stamp are dropped — they
        can't be reliably anchored in time. Returns "" if the file is
        missing.
        """
        if not self.history_file.exists():
            return ""
        try:
            raw = self.history_file.read_text(encoding="utf-8")
        except OSError:
            return ""

        kept: list[str] = []
        for paragraph in raw.split("\n\n"):
            stripped = paragraph.strip()
            if not stripped:
                continue
            ts_ms = _parse_history_paragraph_ts_ms(stripped)
            if ts_ms is None or ts_ms < since_ms:
                continue
            kept.append(stripped)
        return "\n\n".join(kept)

    # Helpers used by Sentinel + the (future) Personalizer write
    # path. Both relocated / added here so MemoryStore is the single
    # owner of MEMORY.md / HISTORY.md mutation logic — direct callers
    # (raven-core Sentinel, Personalizer, ContextBuilder) import
    # MemoryStore directly, no Protocol-level indirection needed.

    def read_history_tail(self, lines: int) -> str:
        """Return the last ``lines`` non-blank lines of HISTORY.md.

        ``lines <= 0`` returns every non-blank line. Missing file
        returns ``""``. Originally lived on the (since-deleted)
        ``DefaultMemoryEngine`` facade; now part of MemoryStore's
        public surface.
        """
        if not self.history_file.exists():
            return ""
        try:
            raw = self.history_file.read_text(encoding="utf-8")
        except OSError:
            return ""
        non_blank = [line for line in raw.splitlines() if line.strip()]
        if lines <= 0:
            return "\n".join(non_blank)
        return "\n".join(non_blank[-lines:])

    def update_section(
        self,
        heading: str,
        body: str,
        *,
        at_end: bool = True,
    ) -> None:
        """Replace (or insert) one H2 section in MEMORY.md.

        Must be called inside :meth:`locked` — this method does not
        acquire the lock itself so callers can group multiple section
        updates under one lock when needed (Sentinel currently writes
        a single section per turn so the common case is::

            with store.locked():
                store.update_section("## Sentinel Observations", body)

        ``at_end=True`` (default) routes through the existing
        :func:`_splice_h2_section_at_end` helper which guarantees the
        named section ends at file-end after the write — matches
        Sentinel + Foresight semantics where heading order is
        meaningful. ``at_end=False`` preserves the existing position
        via :func:`_splice_h2_section`.
        """
        current = self.read_long_term()
        if at_end:
            new = _splice_h2_section_at_end(current, heading, body)
        else:
            new = _splice_h2_section(current, heading, body)
        self.write_long_term(new)

    # Section-aware read.
    _SECTION_READ_TOP_K = 2
    _NOTES_HEADING_PREFIX = "## Notes"

    def get_memory_context(
        self, current_message: str | None = None,
    ) -> str:
        """Return the memory block to embed in the agent's system prompt.

        ``current_message=None`` (or empty) → full user.md dump. Useful
        for cold-start sessions or when the agent is being pinged without
        a user query.

        ``current_message`` provided → parse user.md into H2 sections,
        score each by lexical overlap with the query, return top-K (2 by
        default) plus '## Notes' as catchall. Sections kept appear in
        their original file order so the prompt reads naturally.

        Falls back to full dump when section parsing yields nothing.
        """
        long_term = self.read_long_term()
        if not long_term:
            return ""
        if not current_message or not current_message.strip():
            return f"## Long-term Memory\n{long_term}"
        sections = _parse_user_md_sections(long_term)
        if not sections:
            return f"## Long-term Memory\n{long_term}"
        selected = self._select_relevant_sections(
            current_message, sections, top_k=self._SECTION_READ_TOP_K,
        )
        if not selected:
            return f"## Long-term Memory\n{long_term}"
        body = "\n\n".join(
            f"{heading}\n\n{section_body}".rstrip()
            for heading, section_body in selected.items()
        )
        return f"## Long-term Memory\n\n{body}\n"

    @classmethod
    def _select_relevant_sections(
        cls,
        query: str,
        sections: dict[str, str],
        top_k: int = 2,
    ) -> dict[str, str]:
        """Score each section, keep top-K of those with score > 0 plus
        '## Notes' as a catchall. Sections scoring 0 are NOT included as
        filler — otherwise tied-0 sections leak into the prompt.
        Returned dict preserves source file order for predictable
        rendering.
        """
        scored = [
            (heading, body, _score_section_relevance(query, heading, body))
            for heading, body in sections.items()
        ]
        scored.sort(key=lambda x: x[2], reverse=True)
        keep_keys: set[str] = {
            h for h, _, score in scored[:top_k] if score > 0
        }
        for heading in sections:
            if heading.startswith(cls._NOTES_HEADING_PREFIX):
                keep_keys.add(heading)
        return {h: b for h, b in sections.items() if h in keep_keys}

    @staticmethod
    def _format_messages(messages: list[dict]) -> str:
        lines = []
        for message in messages:
            if not message.get("content"):
                continue
            tools = f" [tools: {', '.join(message['tools_used'])}]" if message.get("tools_used") else ""
            lines.append(
                f"[{message.get('timestamp', '?')[:16]}] {message['role'].upper()}{tools}: {message['content']}"
            )
        return "\n".join(lines)

    async def annotate(
        self,
        messages: list[dict],
        provider: LLMProvider,
        model: str,
        *,
        enable_foresight: bool = False,
    ) -> bool:
        """Light path: annotate the conversation chunk only.

        Produces:
        - episodes.md entries (single line, with #tags)
        - foresight_hint persisted to user.md ## Foresight when
          ``enable_foresight`` is True

        Does NOT touch user.md profile sections. Those are refreshed
        separately via ``refresh_section`` when a tag accumulates enough
        new events — the heavy path triggered by
        ``maybe_refresh_hot_tags``.

        ``enable_foresight=False`` (default) keeps the tool schema small:
        the LLM isn't asked for predictions at all.
        """
        if not messages:
            return True

        now_str = self._now_fn().strftime("%Y-%m-%d %H:%M (%A)")
        if enable_foresight:
            slot_lines = (
                "- episode_summary: ARRAY of single-line entries "
                "\"[YYYY-MM-DD HH:MM] <summary, <=100 chars> #tag1 #tag2\".\n"
                "- foresight_hint: ARRAY of predictions; [] when no deferred / "
                "recurring signal."
            )
            example_tail = (
                "\nforesight_hint:\n"
                "  - {{\"prediction\": \"User will revisit WebSocket leak "
                "fix after load test next week\", \"window\": \"5-7 days\", "
                "\"confidence\": \"medium\", \"src_ts\": "
                "\"2024-11-08 14:20\"}}\n"
                "  - {{\"prediction\": \"User runs every Saturday morning "
                "(recurring habit, 3+ observations)\", \"window\": "
                "\"recurring weekly\", \"confidence\": \"high\", "
                "\"src_ts\": \"2024-11-09 10:00\"}}\n"
                "  - {{\"prediction\": \"Q4 retrospective scheduled for "
                "next Friday\", \"window\": \"5 days\", \"confidence\": "
                "\"high\", \"src_ts\": \"2024-11-09 14:00\"}}\n"
                "(Again: FORMAT examples from an unrelated domain. "
                "Produce predictions only for the conversation above.)\n"
            )
            sys_line = (
                "You are a conversation annotator. Call "
                "annotate_conversation exactly once with both slots filled "
                "(foresight_hint may be [])."
            )
        else:
            slot_lines = (
                "- episode_summary: ARRAY of single-line entries "
                "\"[YYYY-MM-DD HH:MM] <summary, <=100 chars> #tag1 #tag2\"."
            )
            example_tail = ""
            sys_line = (
                "You are a conversation annotator. Call "
                "annotate_conversation exactly once with episode_summary "
                "filled."
            )
        # Feed the LLM its recently-used project slugs so it reuses
        # them instead of inventing new variants every call (e.g.
        # #project-clawtrack-release vs ...-cli vs ...-coverage).
        recent_tags = self.recent_project_tags(days=14, limit=12)
        if recent_tags:
            tag_history_lines = "\n".join(
                f"  - #{tag} ({n}x in last 14 days)" for tag, n in recent_tags
            )
            tag_history_block = (
                "\n## Project tags you've recently used — REUSE these "
                "slugs when describing the same project; do NOT invent "
                "new variants:\n" + tag_history_lines + "\n"
            )
        else:
            tag_history_block = ""

        prompt = f"""Annotate this conversation chunk. Call annotate_conversation with:

{slot_lines}

## Critical rules

1. **Each episode summary must include specific identifiers** — file
   names, function names, PR numbers, percentages, durations. Avoid
   vague verbs like "worked on" / "discussed" / "planned"; describe the
   concrete artifact, decision, or finding.
2. **Reuse project slugs across calls**. If a project slug already
   exists in the "tags you've recently used" list below, use it
   verbatim. Splitting one project into multiple slugs
   (#project-clawtrack-release / -cli / -docs) destroys the tag-based
   refresh trigger — pick ONE stable slug per project.
3. **Tag the WORK, not the codebase**: `#project-<work-slug>` where the
   slug names the topic. Use `#project-auth-refactor` not
   `#project-backend-api`.
4. **Process tags can't stand alone**. `#question`, `#habit`, `#answer`
   describe HOW the user is talking, not WHAT about. Every episode
   needs at least one CONTENT tag (a `#project-*` or one of {{#perf,
   #bug, #decision, #blocker, #deferred, #pivot, #pr, #review, #rfc,
   #design, #infra, #sql, #ml}}) IN ADDITION to any process tag.
5. **Avoid the generic #task tag**.
{tag_history_block}
## Current Time
{now_str}

## Conversation to Annotate
{self._format_messages(messages)}

## Output shape example
The examples below are from an UNRELATED domain (websocket / DB / feature-flag work). They demonstrate the FORMAT only. DO NOT copy any of their text or topics — generate entries that describe the actual conversation above.

episode_summary:
  - "[2024-11-08 14:20] Identified memory leak in WebSocketManager.broadcast(); ~200MB growth/hour under load #project-ws-stability #perf #bug"
  - "[2024-11-09 10:00] Migrated user_sessions from MyISAM to InnoDB (~12M rows, 4h offline window) #project-db-migration #infra #decision"
  - "[2024-11-11 16:30] Feature flag 'dark-mode-v2' ramped 10%->50% after 24h of steady metrics #project-feature-flag-rollout #pr #decision"
{example_tail}"""

        try:
            response = await provider.chat_with_retry(
                messages=[
                    {"role": "system", "content": sys_line},
                    {"role": "user", "content": prompt},
                ],
                tools=_build_annotate_tool(enable_foresight=enable_foresight),
                model=model,
                tool_choice="required",
            )

            if not response.has_tool_calls:
                logger.warning("annotate: LLM did not call annotate_conversation")
                return False

            args = _normalize_save_memory_args(response.tool_calls[0].arguments)
            if args is None:
                logger.warning("annotate: unexpected tool arguments")
                return False

            episodes = args.get("episode_summary") or []
            if isinstance(episodes, str):
                episodes = [episodes]
            n_written = 0
            n_dropped_process_only = 0
            for ep in episodes:
                line = _ensure_text(ep).strip()
                if not line:
                    continue
                # Drop process-only episodes at the boundary. Prompt
                # declares #question / #habit / #answer can't stand
                # alone; LLM still emits them ~5% of the time. Letting
                # them through pollutes refresh_section: their tag heat
                # triggers a refresh that has no content tag to anchor
                # to, and the LLM ends up writing freeform speculation
                # into ## Projects / ## Notes.
                if _is_process_only_episode(line):
                    n_dropped_process_only += 1
                    continue
                self.append_history(line)
                n_written += 1
            if n_dropped_process_only:
                logger.info(
                    "annotate: dropped {} process-only episode(s)",
                    n_dropped_process_only,
                )

            if enable_foresight:
                foresights = args.get("foresight_hint") or []
                if foresights:
                    # Persist to user.md ## Foresight section under the
                    # memory lock; do it in a thread so the async event
                    # loop isn't blocked on file I/O.
                    written = await asyncio.to_thread(
                        self.append_foresight, foresights,
                    )
                    logger.info(
                        "annotate: {} foresight hint(s) emitted → "
                        "user.md ## Foresight ({} written, {} deduped/skipped)",
                        len(foresights), written, len(foresights) - written,
                    )

            logger.info(
                "annotate done for {} messages -> {} episode(s)",
                len(messages), n_written,
            )
            return True
        except Exception:
            logger.exception("annotate failed")
            return False

    # -------------------------------------------------------------------
    # Heavy path — tag-frequency-triggered profile section refresh.
    # -------------------------------------------------------------------

    @property
    def _tag_offsets_path(self) -> Path:
        """``.consolidation_offsets.json`` next to episodes.md. Records the
        episode count at the last refresh for each tag — next time we
        only act on the delta."""
        return self.history_file.parent / ".consolidation_offsets.json"

    def read_tag_offsets(self) -> dict[str, int]:
        """Per-tag episode count at last section refresh. Missing file or
        bad JSON → empty dict (treated as 'never refreshed')."""
        p = self._tag_offsets_path
        if not p.exists():
            return {}
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            logger.warning("tag offsets: failed to parse {}; starting fresh", p)
            return {}
        return {k: int(v) for k, v in data.items() if isinstance(v, (int, float))}

    def write_tag_offsets(self, offsets: dict[str, int]) -> None:
        """Atomic write of the offsets file (tmp + rename)."""
        p = self._tag_offsets_path
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps(offsets, indent=2, sort_keys=True, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(p)

    def count_tags(self) -> dict[str, int]:
        """Total occurrence count for each tag across all of episodes.md."""
        if not self.history_file.exists():
            return {}
        counts: dict[str, int] = {}
        for line in self.history_file.read_text(encoding="utf-8").splitlines():
            parsed = _parse_episode_line(line)
            if not parsed:
                continue
            _, _, tags = parsed
            for t in tags:
                counts[t] = counts.get(t, 0) + 1
        return counts

    def recent_project_tags(
        self, *, days: int = 14, limit: int = 12,
    ) -> list[tuple[str, int]]:
        """Return up-to-``limit`` ``(project-tag, count)`` pairs seen in
        episodes.md within the last ``days``, sorted by frequency.

        Used by ``annotate()`` to seed the prompt with "slugs you've
        already used" so the LLM reuses them instead of inventing new
        variants for the same project — prevents one project being
        split across multiple #project-*-cli / -docs / -release slugs.
        """
        from datetime import timedelta
        if not self.history_file.exists():
            return []
        cutoff = self._now_fn() - timedelta(days=days)
        counts: dict[str, int] = {}
        for line in self.history_file.read_text(encoding="utf-8").splitlines():
            parsed = _parse_episode_line(line)
            if not parsed:
                continue
            ts, _, tags = parsed
            ts_norm = ts.replace("T", " ")
            try:
                dt = datetime.strptime(ts_norm, "%Y-%m-%d %H:%M")
            except ValueError:
                continue
            if dt < cutoff:
                continue
            for t in tags:
                if t.startswith("project-"):
                    counts[t] = counts.get(t, 0) + 1
        return sorted(counts.items(), key=lambda kv: -kv[1])[:limit]

    def hot_tags(self, threshold: int) -> list[tuple[str, int, int]]:
        """Tags where ``current_count - last_offset >= threshold``.

        Returns ``[(tag, current_count, previous_offset), ...]`` sorted
        by delta descending so the hottest tag refreshes first.
        """
        counts = self.count_tags()
        offsets = self.read_tag_offsets()
        hot: list[tuple[str, int, int]] = []
        for tag, current in counts.items():
            prev = offsets.get(tag, 0)
            if current - prev >= threshold:
                hot.append((tag, current, prev))
        hot.sort(key=lambda x: x[1] - x[2], reverse=True)
        return hot

    def _episodes_for_tag(
        self, tag: str, max_episodes: int = 50,
    ) -> list[str]:
        """Most recent up-to-N episode lines carrying the given tag, in
        chronological order. Pulls from the tail of episodes.md."""
        if not self.history_file.exists():
            return []
        matches: list[str] = []
        for line in reversed(
            self.history_file.read_text(encoding="utf-8").splitlines()
        ):
            stripped = line.strip()
            if not stripped:
                continue
            parsed = _parse_episode_line(stripped)
            if not parsed:
                continue
            _, _, tags = parsed
            if tag in tags:
                matches.append(stripped)
                if len(matches) >= max_episodes:
                    break
        matches.reverse()
        return matches

    async def refresh_section(
        self,
        tag: str,
        provider: LLMProvider,
        model: str,
        max_episodes: int = 50,
    ) -> bool:
        """Heavy path: rewrite ONE H2 section of user.md scoped to
        ``tag``'s recent episodes.

        The LLM picks the target H2 (or invents one if none fit) and
        emits ``{section_heading, section_body}``. The splicer replaces
        that section's body and leaves all other sections byte-identical.
        """
        relevant = self._episodes_for_tag(tag, max_episodes)
        if not relevant:
            logger.debug("refresh_section({}): no matching episodes", tag)
            return True

        current_profile = self.read_long_term()
        now_str = self._now_fn().strftime("%Y-%m-%d %H:%M (%A)")
        episodes_block = "\n".join(relevant)
        prompt = f"""Update ONE H2 section of user.md based on the recent
#{tag} episodes below. user.md is a PROFILE SNAPSHOT (current state per
topic), NOT an event log — episodes.md already keeps the event log.

<principles>
1. Explicit Evidence Required — only place a fact in user.md if you can
   cite an episode timestamp. No speculation, no inference from titles.
2. Quality Over Quantity — 5 accurate bullets > 15 noisy ones.
   An empty section is OK.
3. Inertia — existing bullets are correct unless a NEW episode
   contradicts them. UPDATE bullets in place rather than rewrite.
4. Reject Events — one-off events, emotional states ("anxiety",
   "frustration"), and transient process work ("in middle of debugging X")
   do NOT belong in user.md — they're already in episodes.md.
5. Profile Snapshot, Not Diary — every bullet answers "what is true
   about this user right now?", not "what happened on day X?".
6. Abstraction, Not Enumeration — a profile bullet captures a PATTERN,
   not a list of instances. When N episodes share a theme, write ONE
   bullet describing the abstraction; do NOT comma-list the instances
   inside the bullet.
   ✓ "Spends commute/break time on child-related research"
   ✗ "Researches English materials, breakfast recipes, sunscreen,
      dental care, vaccines, parent-child games, homework, time
      management"
   The 9-item enumeration above defeats the snapshot — each instance
   already lives in episodes.md; user.md only needs the theme.
</principles>

<section_schemas>
Each H2 section follows a semi-structured convention. Use **Field**:
prefix for required slots; bullets without prefix are ad-hoc additions.

## Identity (≤ 5 bullets — stable role/personal facts)
  - **Name**: ...
  - **Role**: ...
  - **Stack**: ...
  - **Location**: ...
  - **Key relations**: <name + role, e.g. "周晓棠 (girlfriend)">

## Preferences (≤ 5 bullets — working style / tools / quiet hours)
  - **Communication**: terse | verbose | mixed; emoji-friendly Y/N
  - **Tools**: <comma-separated preferences>
  - **Quiet hours**: <when not to interrupt>
  - ad-hoc preference bullets allowed

## Projects → ### <project-name> (each H3 has 4-6 bullets)
  - **Type**: work | side project | learning | personal
  - **Status**: <one-line current state>
  - **Recent work**: <2-3 descriptive items, NOT per-day events>
  - **Next**: <upcoming actions>
  - Optional: **Stack**, **Stakeholders**, **Deadline**

## Habits (≤ 6 bullets — recurring patterns, ≥ 2 observations to qualify)
  - **<pattern>** (confirmed by N obs; freq: weekly | daily | sporadic)
    Example: "Saturday morning run (confirmed by 4+ obs; freq: weekly)"

## Notes (≤ 8 bullets — important specific facts)
  - <birthday / deadline / preferred X / similar facts>

## Foresight — AUTO-MANAGED by a different path. DO NOT TARGET; do NOT
rewrite its contents.
</section_schemas>

<triage_each_episode>
Before deciding what to write, classify each new episode:

KEEP (write into user.md) if it represents:
  - identity / role / relationship fact → ## Identity
  - working preference confirmed → ## Preferences
  - project state change (status, deliverable, decision) → ## Projects
  - recurring pattern with ≥ 2 observations → ## Habits
  - dated commitment / deadline / specific fact → ## Notes

REJECT (stays in episodes.md only, do NOT add to user.md) if it's:
  - one-off event ("ran today", "had lunch", "PR merged" — the PR-merged
    detail goes in episodes.md; the project's Status field captures the
    end state, not the per-event)
  - emotional state ("anxious", "frustrated", "excited")
  - transient process ("in middle of debugging X", "testing Y")
  - in-progress detail that resolves soon
  - already covered by an existing bullet without new info
  - just a question the user asked
</triage_each_episode>

<update_protocol>
For each episode that PASSES triage, follow this order STRICTLY:

1. UPDATE first — find an existing bullet on the same subject; refine it
   in place to reflect the latest evidence.
   Example: existing "**Status**: pre-release testing"
            + new "PR #1287 merged: v1.0 released"
            → "**Status**: v1.0 released (5/15), gathering feedback"

2. CONSOLIDATE second — merge related bullets in the same section.
   Example: "**Recent work**: CLI bug" + new "doc generation broken"
            → "**Recent work**: CLI bug + doc generation broken"

   ANTI-PATTERN — DO NOT enumerate. When N episodes share a THEME but
   each adds a different specific instance, do NOT comma-list every
   instance inside the bullet.
   Existing "Researches child topics" + new episode "researched vaccines":
     ✗ bad:  "Researches child topics including English, breakfast,
              vaccines, dental care, sunscreen, ..."
     ✓ good: leave the bullet UNCHANGED — the theme is already captured;
             the specific vaccine instance lives in episodes.md.
   Apply this whenever you find yourself reaching for "including", "such
   as", "e.g.", or a comma-list of nouns inside one bullet.

3. REMOVE third — drop bullets obsoleted by new evidence.
   Example: "**Status**: pre-release anxiety" → DROP once "released" lands.

4. APPEND last — only if truly new topic AND under the section cap.

After processing, respect section caps (see <section_schemas>).
If you'd exceed a cap, CONSOLIDATE harder.
</update_protocol>

## Current Time
{now_str}

## Current user.md (UPDATE/CONSOLIDATE/REJECT — don't just append)
{current_profile or '(empty)'}

## Recent episodes tagged #{tag} ({len(relevant)} entries — fold into
the matching section after triage)
{episodes_block}

## Output
section_heading: the H2 line you're updating (verbatim; for project
   work use `## Projects` — the H3 sub-section goes inside section_body).
section_body: full new content for that H2, every bullet ending with
   `[src: episodes.md @ <ts>]`. Use the LATEST relevant ts when merging.
"""
        try:
            response = await provider.chat_with_retry(
                messages=[
                    {"role": "system", "content": (
                        "You maintain a structured user profile in user.md, "
                        "NOT an event log. Follow the <principles>, "
                        "<section_schemas>, <triage_each_episode>, and "
                        "<update_protocol> blocks in the user message. "
                        "Prefer UPDATE over APPEND; respect per-section "
                        "size caps; reject events / emotions / transient "
                        "process work that already lives in episodes.md."
                    )},
                    {"role": "user", "content": prompt},
                ],
                tools=_REFRESH_SECTION_TOOL,
                model=model,
                tool_choice="required",
            )
            if not response.has_tool_calls:
                logger.warning("refresh_section({}): LLM did not call tool", tag)
                return False
            args = _normalize_save_memory_args(response.tool_calls[0].arguments)
            if (args is None
                    or "section_heading" not in args
                    or "section_body" not in args):
                logger.warning("refresh_section({}): unexpected tool args", tag)
                return False
            heading = _ensure_text(args["section_heading"]).strip()
            body = _ensure_text(args["section_body"])
            if not heading.startswith("## "):
                logger.warning(
                    "refresh_section({}): bad heading {!r}", tag, heading,
                )
                return False

            # Drop profile bullets missing the
            # ``[src: episodes.md @ ts]`` evidence link. Skip ## Foresight
            # (auto-managed by append_foresight; uses paren-style src that
            # the bracket regex wouldn't match — applying the filter there
            # would wipe the section).
            if heading != _FORESIGHT_HEADING:
                body, n_src_dropped = _drop_bullets_without_src(body)
                if n_src_dropped:
                    logger.warning(
                        "refresh_section({}): dropped {} bullet(s) "
                        "missing [src:] link in section {!r}",
                        tag, n_src_dropped, heading,
                    )

            # Soft observability: warn if section grew past the
            # schema's hard caps (Projects/Habits 6, Notes 8, others 5).
            # Don't truncate — that risks dropping useful content; just
            # surface drift so the prompt can be re-tuned if it persists.
            n_bullets = sum(
                1 for ln in body.splitlines() if ln.lstrip().startswith("-")
            )
            if n_bullets > 15:
                logger.warning(
                    "refresh_section({}): LLM produced {} bullets (>15) for "
                    "section {!r} — schema cap violated, profile may be "
                    "diary-style. Check episodes.md / prompt drift.",
                    tag, n_bullets, heading,
                )

            await asyncio.to_thread(
                self._splice_section_and_write, heading, body, current_profile,
            )
            logger.info(
                "refresh_section({}): section {!r} updated using {} episode(s) "
                "-> {} bullets",
                tag, heading, len(relevant), n_bullets,
            )
            return True
        except Exception:
            logger.exception("refresh_section({}) failed", tag)
            return False

    def _splice_section_and_write(
        self, heading: str, new_body: str, expected_prev: str,
    ) -> bool:
        """CAS write: splice ``new_body`` under ``heading`` in user.md only
        if file still matches ``expected_prev`` (no concurrent writer).
        Returns True on write."""
        with self.locked():
            current = self.read_long_term()
            if current != expected_prev:
                logger.info(
                    "refresh_section: concurrent modification detected; "
                    "skipping write (will retry next round)"
                )
                return False
            new_content = _splice_h2_section(current, heading, new_body)
            # Keep auto-managed ## Foresight at the bottom of user.md
            # regardless of where refresh_section's splice landed the new
            # section. Idempotent — no-op when Foresight is absent or
            # already last.
            new_content = _ensure_foresight_at_end(new_content)
            if new_content != current:
                self.write_long_term(new_content)
            return True

    async def maybe_refresh_hot_tags(
        self,
        provider: LLMProvider,
        model: str,
        threshold: int = 5,
    ) -> int:
        """Scan episodes.md, refresh any tag whose new-episode count since
        last refresh meets ``threshold``. Refreshes are serial (hottest
        first) so we don't race on user.md.

        Returns the number of sections actually refreshed.
        """
        hot = self.hot_tags(threshold)
        if not hot:
            return 0
        offsets = self.read_tag_offsets()
        refreshed = 0
        for tag, current_count, _prev in hot:
            ok = await self.refresh_section(tag, provider, model)
            if ok:
                offsets[tag] = current_count
                self.write_tag_offsets(offsets)
                refreshed += 1
            else:
                # Don't advance offset on failure — next round retries.
                logger.warning(
                    "maybe_refresh_hot_tags: tag {!r} refresh failed; "
                    "offset not advanced", tag,
                )
        return refreshed


class MemoryConsolidator:
    """Owns consolidation policy, locking, and session offset updates."""

    _MAX_CONSOLIDATION_ROUNDS = 5

    # A tag triggers a profile-section refresh once this many new
    # episodes have accumulated since its last refresh. Below threshold,
    # episodes still accumulate but user.md stays untouched.
    _REFRESH_HOT_TAG_THRESHOLD = 5

    def __init__(
        self,
        workspace: Path,
        provider: LLMProvider,
        model: str,
        sessions: SessionManager,
        context_window_tokens: int,
        build_messages: Callable[..., list[dict[str, Any]]],
        get_tool_definitions: Callable[[], list[dict[str, Any]]],
        now_fn: Callable[[], datetime] | None = None,
        *,
        enable_foresight: bool = False,
    ):
        self.store = MemoryStore(workspace, now_fn=now_fn)
        self.provider = provider
        self.model = model
        self.sessions = sessions
        self.context_window_tokens = context_window_tokens
        self._build_messages = build_messages
        self._get_tool_definitions = get_tool_definitions
        # When True, annotate() asks the LLM for foresight predictions
        # alongside episodes and persists them to user.md ## Foresight.
        # Off by default.
        self.enable_foresight = enable_foresight
        self._locks: weakref.WeakValueDictionary[str, asyncio.Lock] = weakref.WeakValueDictionary()

    def get_lock(self, session_key: str) -> asyncio.Lock:
        """Return the shared consolidation lock for one session."""
        return self._locks.setdefault(session_key, asyncio.Lock())

    async def consolidate_messages(self, messages: list[dict[str, object]]) -> bool:
        """Light path: annotate a selected message chunk into episodes.md.

        Profile rewrites are NOT done here — they happen via
        :meth:`maybe_refresh_hot_tags` after annotation rounds finish, so
        the LLM only sees one tag's worth of relevant context at a time.
        """
        return await self.store.annotate(
            messages, self.provider, self.model,
            enable_foresight=self.enable_foresight,
        )

    async def maybe_refresh_hot_tags(self) -> int:
        """Refresh any profile sections whose backing tag has heated up
        since the last refresh. Returns the number of sections rewritten."""
        return await self.store.maybe_refresh_hot_tags(
            self.provider, self.model,
            threshold=self._REFRESH_HOT_TAG_THRESHOLD,
        )

    def pick_consolidation_boundary(
        self,
        session: Session,
        tokens_to_remove: int,
    ) -> tuple[int, int] | None:
        """Pick a user-turn boundary that removes enough old prompt tokens."""
        start = session.last_consolidated
        if start >= len(session.messages) or tokens_to_remove <= 0:
            return None

        removed_tokens = 0
        last_boundary: tuple[int, int] | None = None
        for idx in range(start, len(session.messages)):
            message = session.messages[idx]
            if idx > start and message.get("role") == "user":
                last_boundary = (idx, removed_tokens)
                if removed_tokens >= tokens_to_remove:
                    return last_boundary
            removed_tokens += estimate_message_tokens(message)

        return last_boundary

    def estimate_session_prompt_tokens(self, session: Session) -> tuple[int, str]:
        """Estimate current prompt size for the normal session history view."""
        history = session.get_history(max_messages=0)
        channel, chat_id = (session.key.split(":", 1) if ":" in session.key else (None, None))
        probe_messages = self._build_messages(
            history=history,
            current_message="[token-probe]",
            channel=channel,
            chat_id=chat_id,
        )
        return estimate_prompt_tokens_chain(
            self.provider,
            self.model,
            probe_messages,
            self._get_tool_definitions(),
        )

    async def archive_unconsolidated(self, session: Session) -> bool:
        """Archive the full unconsolidated tail for /new-style session rollover.

        Annotates the tail into episodes.md, then runs one round of hot-tag
        section refresh so the profile reflects the just-closed session.
        """
        lock = self.get_lock(session.key)
        async with lock:
            snapshot = session.messages[session.last_consolidated:]
            if not snapshot:
                return True
            ok = await self.consolidate_messages(snapshot)
            if ok:
                await self.maybe_refresh_hot_tags()
            return ok

    async def maybe_consolidate_by_tokens(self, session: Session) -> None:
        """Loop: archive old messages until prompt fits within half the context window."""
        if not session.messages or self.context_window_tokens <= 0:
            return

        lock = self.get_lock(session.key)
        async with lock:
            target = self.context_window_tokens // 2
            estimated, source = self.estimate_session_prompt_tokens(session)
            if estimated <= 0:
                return
            if estimated < self.context_window_tokens:
                logger.debug(
                    "Token consolidation idle {}: {}/{} via {}",
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                )
                return

            chunks_annotated = 0
            for round_num in range(self._MAX_CONSOLIDATION_ROUNDS):
                if estimated <= target:
                    break

                boundary = self.pick_consolidation_boundary(session, max(1, estimated - target))
                if boundary is None:
                    logger.debug(
                        "Token consolidation: no safe boundary for {} (round {})",
                        session.key,
                        round_num,
                    )
                    break

                end_idx = boundary[0]
                chunk = session.messages[session.last_consolidated:end_idx]
                if not chunk:
                    break

                logger.info(
                    "Token consolidation round {} for {}: {}/{} via {}, chunk={} msgs",
                    round_num,
                    session.key,
                    estimated,
                    self.context_window_tokens,
                    source,
                    len(chunk),
                )
                if not await self.consolidate_messages(chunk):
                    break
                chunks_annotated += 1
                session.last_consolidated = end_idx
                self.sessions.save(session)

                estimated, source = self.estimate_session_prompt_tokens(session)
                if estimated <= 0:
                    break

            # If at least one chunk was annotated, give hot tags a chance to
            # refresh their corresponding profile sections. Runs at most once
            # per ``maybe_consolidate_by_tokens`` invocation regardless of
            # how many annotation rounds fired.
            if chunks_annotated:
                await self.maybe_refresh_hot_tags()
