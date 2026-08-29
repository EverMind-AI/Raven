"""Offline trigger expansion -- build a playbook's L1 match vocabulary.

Callers may expand a description, the user's original wording and author seeds
through an LLM, then pass the candidates through the same guards used for
model-generated and hand-written vocabularies:

- **rule filter** -- too-short entries and bare function words;
- **generic-word filter** -- when the caller supplies a representative negative
  corpus, candidates matching too many unrelated messages are removed.

Generic entries can crowd specific playbooks out of the top-K descriptions the
main model sees. They never dispatch work or cause a per-message LLM call.

Cross-playbook collision checking is a separate pure function over the
whole library (`find_collisions`), run at load/compile time by the caller.
"""

from __future__ import annotations

import unicodedata
from typing import TYPE_CHECKING

from loguru import logger

from raven.playbook.types import Triggers

if TYPE_CHECKING:
    pass


_MIN_KEYWORD_CHARS = 2
_DEFAULT_MAX_HIT_RATE = 0.02

# Bare function words that survive the length rule but carry no task signal.
# Domain-generic words ("article", "report") are NOT listed here on purpose --
# the negative-sample filter judges those from data, not from anyone's
# intuition. Chinese entries are written as unicode escapes to keep the
# source ASCII; each carries its pinyin and meaning.
_STOPWORDS = {
    "\u5e2e\u6211",  # bang wo: "help me"
    "\u7ed9\u6211",  # gei wo: "give me"
    "\u6211\u8981",  # wo yao: "I want"
    "\u6211\u60f3",  # wo xiang: "I'd like"
    "\u4e00\u4e2a",  # yi ge: "a/one"
    "\u4e00\u4e0b",  # yi xia: "briefly"
    "\u4e00\u4efd",  # yi fen: "a copy of"
    "\u8fd9\u4e2a",  # zhe ge: "this"
    "\u90a3\u4e2a",  # na ge: "that"
    "\u4ec0\u4e48",  # shen me: "what"
    "\u600e\u4e48",  # zen me: "how"
    "\u53ef\u4ee5",  # ke yi: "can/may"
    "\u9700\u8981",  # xu yao: "need"
    "\u9ebb\u70e6",  # ma fan: "please/trouble you"
    "the",
    "and",
    "for",
    "with",
    "make",
    "help",
    # Mechanism words: they describe how the system works, not any one task,
    # so they appear in requests for every playbook and trigger all of them.
    "playbook",
    "workflow",
    "pipeline",
    "template",
    "framework",
    "process",
    "\u6a21\u677f",  # mu ban: "template"
    "\u6d41\u7a0b",  # liu cheng: "process/flow"
    "\u65b9\u6848",  # fang an: "plan/scheme"
    "\u81ea\u52a8\u5316",  # zi dong hua: "automation"
    "\u5de5\u4f5c\u6d41",  # gong zuo liu: "workflow"
}


def normalize(text: str) -> str:
    """The one normalization both vocabulary and query go through.

    NFKC folds full-width forms, lowercase folds case, whitespace collapses
    to single spaces. Chinese is substring-matched without segmentation, so
    no tokenizer is involved anywhere.
    """
    folded = unicodedata.normalize("NFKC", text).lower()
    return " ".join(folded.split())


def _rule_filter(entries: list[str], min_chars: int) -> list[str]:
    seen: set[str] = set()
    kept: list[str] = []
    for raw in entries:
        entry = normalize(str(raw))
        if len(entry.replace(" ", "")) < min_chars or entry in _STOPWORDS or entry in seen:
            continue
        seen.add(entry)
        kept.append(entry)
    return kept


def generic_hit_rate(entry: str, negative_samples: list[str]) -> float:
    """Fraction of unrelated messages this entry would (mis)match."""
    if not negative_samples:
        return 0.0
    hits = sum(1 for msg in negative_samples if entry in msg)
    return hits / len(negative_samples)


def _generic_filter(
    entries: list[str], negative_samples: list[str], max_hit_rate: float
) -> tuple[list[str], list[str]]:
    normalized_samples = [normalize(m) for m in negative_samples]
    kept, dropped = [], []
    for entry in entries:
        if generic_hit_rate(entry, normalized_samples) > max_hit_rate:
            dropped.append(entry)
        else:
            kept.append(entry)
    return kept, dropped


class TriggerGuardError(ValueError):
    """Every candidate was dropped, so there is no vocabulary to index."""


def guard_triggers(
    entries: "list[str] | Triggers",
    *,
    negative_samples: list[str] | None = None,
    max_hit_rate: float = _DEFAULT_MAX_HIT_RATE,
    what: str = "",
) -> Triggers:
    """Run the guard pipeline over a vocabulary, without expansion calls.

    Public because the guards must apply to *every* path that fills the L1
    index, not only to expansion: a vocabulary proposed by a model or typed by
    an author reaches the same index and carries the same standing cost, so
    "the guards outrank the author" has to hold for whoever proposes the words.

    Raises:
        `TriggerGuardError`: nothing survived. Reported rather than silently
            widened, because the alternative -- keeping a dropped entry so the
            playbook still has one -- reinstates exactly the entry a guard just
            judged unfit.
    """
    raw = list(entries.keywords) if isinstance(entries, Triggers) else [str(e) for e in entries]
    keywords = _rule_filter(raw, _MIN_KEYWORD_CHARS)
    if negative_samples:
        keywords, dropped = _generic_filter(keywords, negative_samples, max_hit_rate)
        if dropped:
            logger.info("trigger guards dropped generic entries: {}", dropped)
    else:
        logger.debug(
            "trigger guards for {!r}: optional generic-word filter skipped without negative samples",
            what or raw[:3],
        )
    if not keywords:
        raise TriggerGuardError(
            f"every trigger candidate was dropped by the guards (from {raw!r}); "
            "propose words that are specific to this task and at least "
            f"{_MIN_KEYWORD_CHARS} characters long"
        )
    return Triggers(keywords=keywords)


def find_collisions(library: dict[str, Triggers]) -> dict[str, list[str]]:
    """Map each vocabulary entry claimed by 2+ playbooks to the claimants.

    A collision is a warning, not an error: overlapping candidates can be shown
    to the model together. The report lets a human sharpen one side's vocabulary,
    which is why a playbook must not be able
    to collide with itself: ``["seo", "SEO"]`` normalizes to one entry claimed
    twice, and a report saying "a collides with a" gives the reader nothing to
    sharpen while burying the cross-playbook conflicts that do."""
    owners: dict[str, list[str]] = {}
    for pid, trig in library.items():
        for entry in sorted({normalize(e) for e in trig.keywords}):
            owners.setdefault(entry, []).append(pid)
    return {entry: pids for entry, pids in owners.items() if len(pids) > 1}
