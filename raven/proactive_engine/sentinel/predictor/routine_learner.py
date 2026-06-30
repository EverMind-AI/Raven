"""RoutineLearner — extract recurring user-activity patterns from HISTORY.md.

Fills the ``routines`` field of PlannerContext so the Planner can reason
about what the user habitually does (e.g., "every Tuesday evening writes a
RedNote post", "every morning checks GitHub notifications").

Design (v1 — deterministic, no LLM):
-----------------------------------
We bin HISTORY.md entries by ``(day_of_week, hour_slot)`` and extract
dominant keywords per bin. A bin with ≥ min_occurrences entries over the
learning window becomes a **candidate** Routine; routines stay as
"candidate" until a user-facing confirmation step upgrades them to
"active" (handled by a later component — RoutineLearner never sets
``status="active"`` on its own).

HISTORY.md line format is flexible — we accept any line that starts
with an ISO-8601-ish timestamp. Lines that don't parse are ignored.

Known limitations (v2 candidates):
- No LLM semantic grouping ("check email" and "inbox review" live in
  separate bins)
- Keyword extraction is naive (stopword list + token frequency)
- Bin granularity is fixed (day-of-week × hour); calendar-day patterns
  not detected
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterable

from loguru import logger

from raven.proactive_engine.sentinel.types import Routine

# Default half-life for ``learn_with_decay``. 14d → entries 14 days old
# count half, 28 days a quarter — fresh habits dominate without erasing
# stale ones entirely.
DEFAULT_DECAY_HALF_LIFE_DAYS = 14

# Intentionally small stopword set — perfect coverage is not the v1 goal;
# we want domain words to survive.
_STOPWORDS = {
    # English
    "the",
    "a",
    "an",
    "of",
    "and",
    "or",
    "to",
    "in",
    "on",
    "at",
    "for",
    "with",
    "by",
    "from",
    "is",
    "was",
    "are",
    "were",
    "be",
    "been",
    "this",
    "that",
    "these",
    "those",
    "it",
    "its",
    "they",
    "them",
    "user",
    "users",
    "do",
    "did",
    "does",
    "can",
    "will",
    "has",
    "have",
    "had",
    "not",
    "no",
    "yes",
    "just",
    "about",
    "as",
    "if",
    "then",
    # Chinese (common functional words)
    "的",
    "了",
    "在",
    "是",
    "我",
    "有",
    "和",
    "就",
    "不",
    "人",
    "都",
    "一",
    "也",
    "要",
    "去",
    "会",
    "着",
    "到",
    "上",
    "下",
    "说",
    "用户",
    "他",
    "她",
}

# Patterns we recognize at the start of a HISTORY.md line.
_TIMESTAMP_RE = re.compile(
    r"^\s*[\[\(]?"
    r"(?P<ts>\d{4}[-/]\d{1,2}[-/]\d{1,2}[T ]\d{1,2}:\d{2}(?::\d{2})?)"
    r"[\]\)]?\s*(?P<content>.*)$"
)

_TIMESTAMP_FORMATS = (
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%dT%H:%M",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
    "%Y/%m/%d %H:%M",
)


@dataclass
class _Entry:
    ts: datetime
    content: str


def parse_history_entries(raw: str) -> list[_Entry]:
    """Scan HISTORY.md-style text for ``[timestamp] content`` lines.

    Returns chronologically-ordered entries. Lines without a parseable
    timestamp are skipped (no warning — HISTORY.md may have prose headers).
    """
    entries: list[_Entry] = []
    for line in raw.splitlines():
        m = _TIMESTAMP_RE.match(line)
        if not m:
            continue
        ts_str = m.group("ts")
        content = (m.group("content") or "").strip()
        ts = _parse_ts(ts_str)
        if ts is None:
            continue
        entries.append(_Entry(ts=ts, content=content))
    entries.sort(key=lambda e: e.ts)
    return entries


def _parse_ts(ts_str: str) -> datetime | None:
    # Accept either 'T' or space between date and time.
    candidates = [ts_str]
    if "T" in ts_str:
        candidates.append(ts_str.replace("T", " "))
    else:
        candidates.append(ts_str.replace(" ", "T"))
    for cand in candidates:
        for fmt in _TIMESTAMP_FORMATS:
            try:
                return datetime.strptime(cand, fmt)
            except ValueError:
                continue
    return None


def _extract_keywords(content: str, max_keywords: int = 5) -> list[str]:
    """Tokenize + drop stopwords + frequency-rank. Intentionally dumb.

    Handles Chinese by treating any non-space run as a token candidate
    (CJK characters such as "week" or "check-in" will surface naturally);
    tokens length >= 2 unless purely CJK.
    """
    if not content:
        return []
    cleaned = content.lower()
    tokens = re.split(r"[\s,.'\";:!?()\[\]/\\|@#*]+", cleaned)
    kept: list[str] = []
    for tok in tokens:
        tok = tok.strip()
        if not tok or tok in _STOPWORDS:
            continue
        if tok.isdigit():
            continue
        if len(tok) < 2 and not re.search(r"[一-鿿]", tok):
            continue  # drop single ASCII chars; keep single CJK
        kept.append(tok)
    if not kept:
        return []
    ranked = Counter(kept).most_common(max_keywords)
    return [t for t, _ in ranked]


@dataclass
class _Bin:
    day_of_week: int  # 0=Monday ... 6=Sunday
    hour_slot: tuple[int, int]  # (start_hour, end_hour) exclusive; typical 3h slot
    entries: list[_Entry]

    @property
    def occurrence_count(self) -> int:
        return len(self.entries)

    @property
    def last_ts(self) -> datetime | None:
        return max((e.ts for e in self.entries), default=None)


class RoutineLearner:
    """Deterministic routine detector over HISTORY.md text.

    Caller owns the HISTORY.md source (read from MemoryStore in production,
    mocked in tests). RoutineLearner doesn't touch disk.
    """

    def __init__(
        self,
        *,
        min_occurrences: int = 3,
        hour_slot_size: int = 3,  # 24 / 3 = 8 slots per day
        learning_window_days: int = 60,
        min_history_entries: int = 0,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        if hour_slot_size < 1 or hour_slot_size > 24 or 24 % hour_slot_size != 0:
            raise ValueError("hour_slot_size must divide 24 evenly")
        self.min_occurrences = min_occurrences
        self.hour_slot_size = hour_slot_size
        self.learning_window_days = learning_window_days
        self.min_history_entries = max(0, min_history_entries)
        self._now_fn = now_fn or datetime.now

    def learn(self, history_md: str) -> list[Routine]:
        """Parse HISTORY.md text → list of candidate Routines.

        Returns empty list if no patterns meet ``min_occurrences``.
        """
        entries = parse_history_entries(history_md)
        if not entries:
            return []

        cutoff = self._now_fn() - timedelta(days=self.learning_window_days)
        entries = [e for e in entries if e.ts >= cutoff]
        if not entries:
            return []
        if len(entries) < self.min_history_entries:
            return []

        bins = self._bin(entries)
        routines: list[Routine] = []
        for key, b in bins.items():
            if b.occurrence_count < self.min_occurrences:
                continue
            keywords = self._keywords_across(b.entries)
            pattern = self._format_pattern(b, keywords)
            rid = self._routine_id(b, keywords)
            routines.append(
                Routine(
                    id=rid,
                    pattern=pattern,
                    keywords=keywords,
                    day_of_week=b.day_of_week,
                    time_slot=b.hour_slot,
                    status="candidate",
                    occurrence_count=b.occurrence_count,
                    last_triggered=(b.last_ts.isoformat() if b.last_ts else None),
                    user_confirmed=False,
                )
            )
        # Most frequent first, then most recent — stable ordering.
        routines.sort(
            key=lambda r: (r.occurrence_count, r.last_triggered or ""),
            reverse=True,
        )
        log_fn = logger.debug if routines else logger.trace
        log_fn(
            "RoutineLearner produced {} candidate routines from {} history entries",
            len(routines),
            len(entries),
        )
        return routines

    def learn_with_decay(
        self,
        history_md: str,
        *,
        half_life_days: float = DEFAULT_DECAY_HALF_LIFE_DAYS,
    ) -> list[Routine]:
        """Like ``learn`` but populates ``Routine.weight`` with a recency-
        decayed occurrence sum and uses TF-IDF (across bins) for keyword
        ranking.

        Weight formula::

            weight = sum_over_entries( 0.5 ** (days_ago / half_life_days) )

        - Today's entry contributes ~1.0
        - 14 days ago contributes ~0.5
        - 28 days ago contributes ~0.25

        TF-IDF is computed bin-internally as ``term_frequency * log(N / df)``
        where ``df`` is the count of bins containing the term and ``N`` is
        the total bin count. This downweights generic words ("work",
        "user") that appear everywhere and lifts bin-specific words
        ("standup", "running") that distinguish a routine.
        """
        entries = parse_history_entries(history_md)
        if not entries:
            return []
        now = self._now_fn()
        cutoff = now - timedelta(days=self.learning_window_days)
        entries = [e for e in entries if e.ts >= cutoff]
        if not entries:
            return []
        if len(entries) < self.min_history_entries:
            return []

        bins = self._bin(entries)
        # Cross-bin DF for TF-IDF — how many bins contain each term.
        df: Counter[str] = Counter()
        bin_term_freqs: dict[tuple[int, int], Counter[str]] = {}
        for key, b in bins.items():
            tf: Counter[str] = Counter()
            for e in b.entries:
                for kw in _extract_keywords(e.content, max_keywords=10):
                    tf[kw] += 1
            bin_term_freqs[key] = tf
            for term in tf:
                df[term] += 1

        n_bins = max(1, len(bins))

        routines: list[Routine] = []
        for key, b in bins.items():
            if b.occurrence_count < self.min_occurrences:
                continue
            keywords = self._tfidf_keywords(
                bin_term_freqs[key],
                df,
                n_bins,
                top_k=5,
            )
            pattern = self._format_pattern(b, keywords)
            rid = self._routine_id(b, keywords)
            weight = sum(_decay_factor(now=now, ts=e.ts, half_life_days=half_life_days) for e in b.entries)
            routines.append(
                Routine(
                    id=rid,
                    pattern=pattern,
                    keywords=keywords,
                    day_of_week=b.day_of_week,
                    time_slot=b.hour_slot,
                    status="candidate",
                    occurrence_count=b.occurrence_count,
                    last_triggered=(b.last_ts.isoformat() if b.last_ts else None),
                    user_confirmed=False,
                    weight=weight,
                )
            )
        # Fresh-and-frequent first.
        routines.sort(
            key=lambda r: (r.weight, r.occurrence_count, r.last_triggered or ""),
            reverse=True,
        )
        log_fn = logger.debug if routines else logger.trace
        log_fn(
            "RoutineLearner.learn_with_decay produced {} candidates (half_life={}d) from {} entries across {} bins",
            len(routines),
            half_life_days,
            len(entries),
            n_bins,
        )
        return routines

    @staticmethod
    def _tfidf_keywords(
        bin_tf: Counter[str],
        cross_bin_df: Counter[str],
        n_bins: int,
        *,
        top_k: int = 5,
    ) -> list[str]:
        """Rank a single bin's terms by ``tf * log(n_bins / df)``. Falls
        back to plain ``most_common`` ranking when only one bin exists
        (TF-IDF degenerate case)."""
        if n_bins <= 1:
            return [t for t, _ in bin_tf.most_common(top_k)]
        scored: list[tuple[str, float]] = []
        for term, tf in bin_tf.items():
            df = max(1, cross_bin_df.get(term, 1))
            idf = math.log(n_bins / df)
            scored.append((term, tf * idf))
        scored.sort(key=lambda kv: (kv[1], kv[0]), reverse=True)
        return [term for term, score in scored[:top_k] if score > 0]

    def learn_from_file(self, path: Path | str) -> list[Routine]:
        """Convenience — read a HISTORY.md file and delegate to ``learn``."""
        p = Path(path)
        if not p.exists():
            return []
        try:
            return self.learn(p.read_text(encoding="utf-8"))
        except OSError:
            return []

    # ------------------------------------------------------------------
    # Internals

    def _bin(self, entries: Iterable[_Entry]) -> dict[tuple[int, int], _Bin]:
        bins: dict[tuple[int, int], _Bin] = {}
        for e in entries:
            dow = e.ts.weekday()
            slot_start = (e.ts.hour // self.hour_slot_size) * self.hour_slot_size
            slot_end = slot_start + self.hour_slot_size
            key = (dow, slot_start)
            if key not in bins:
                bins[key] = _Bin(day_of_week=dow, hour_slot=(slot_start, slot_end), entries=[])
            bins[key].entries.append(e)
        return bins

    @staticmethod
    def _keywords_across(entries: list[_Entry]) -> list[str]:
        """Top keywords aggregated across an entire bin's content."""
        counter: Counter[str] = Counter()
        for e in entries:
            for kw in _extract_keywords(e.content, max_keywords=8):
                counter[kw] += 1
        return [k for k, _ in counter.most_common(5)]

    @staticmethod
    def _format_pattern(b: _Bin, keywords: list[str]) -> str:
        dow_names = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
        slot = f"{b.hour_slot[0]:02d}:00-{b.hour_slot[1]:02d}:00"
        kw_part = " · ".join(keywords) if keywords else "(no dominant keywords)"
        return f"{dow_names[b.day_of_week]} {slot} — {kw_part}"

    @staticmethod
    def _routine_id(b: _Bin, keywords: list[str]) -> str:
        kw_tag = keywords[0] if keywords else "none"
        # Strip non-alphanum so the id is safe to use in paths and URLs.
        kw_tag = re.sub(r"[^A-Za-z0-9\-]", "", kw_tag)[:16] or "none"
        return f"dow{b.day_of_week}-h{b.hour_slot[0]:02d}-{kw_tag}"


def _decay_factor(*, now: datetime, ts: datetime, half_life_days: float) -> float:
    """``0.5 ** (days_ago / half_life_days)``. Future timestamps clamp to
    1.0 so a parsing glitch can't make weights blow up."""
    if half_life_days <= 0:
        return 1.0
    delta_seconds = (now - ts).total_seconds()
    if delta_seconds <= 0:
        return 1.0
    days_ago = delta_seconds / 86400.0
    return 0.5 ** (days_ago / half_life_days)


__all__ = [
    "RoutineLearner",
    "parse_history_entries",
    "DEFAULT_DECAY_HALF_LIFE_DAYS",
]
