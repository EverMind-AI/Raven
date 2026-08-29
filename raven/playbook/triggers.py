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

import json
import unicodedata
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.playbook.llm_result import ProviderResponseError, RequiredToolError, required_tool_arguments
from raven.playbook.types import Triggers

if TYPE_CHECKING:
    from raven.contracts.llm_provider import LLMProvider

_EXPAND_TOOL_NAME = "emit_triggers"

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

_EXPAND_PROMPT = """\
You are building the trigger vocabulary for a task template (playbook).
When a user message contains one of these words/phrases, the playbook
becomes a match candidate.

Playbook intent: {description}
The user's original wording: {source_input}
Existing seed entries: {seeds}

Core method: imagine how 20 different users would open their mouth when
they want THIS task's result -- they name the outcome they want ("post a
tweet", "see what users are complaining about", "compare A and B"), not
the task's formal name ("content production pipeline"). Draw the entries
from those openings.

Expand along five axes into 20-40 keywords (words and phrases share the
one list):
1. Action phrases (most important): short verb+object pairs -- two or
   three character combinations are the most common way users phrase a
   request. For Chinese action phrases also emit classifier-inserted
   variants: Chinese often inserts a classifier between verb and object
   ("\u53d1\u63a8" -> also list "\u53d1\u6761\u63a8" and "\u53d1\u4e2a\u63a8"), and substring matching
   cannot bridge the insertion, so list every variant explicitly;
2. Synonyms and near-synonyms (at most one hypernym level up, prefer none);
3. Colloquial phrasings (including emotional wording -- complaints,
   praise -- when relevant to the task);
4. Chinese/English pairs (users mix English terms, e.g. twitter/tweet
   next to their Chinese equivalents);
5. Strongly indicative scenario words.

Hard requirements:
- No mechanism words (playbook, workflow, template, pipeline, automation
  and their Chinese equivalents): they describe how the system works, can
  appear in any task's wording, and point at no particular task;
- Only strongly specific entries; everyday high-frequency words like
  "article", "report", "data" are never acceptable;
- Phrases must read like fragments of real user speech, not full
  sentences;
- All lowercase."""


def normalize(text: str) -> str:
    """The one normalization both vocabulary and query go through.

    NFKC folds full-width forms, lowercase folds case, whitespace collapses
    to single spaces. Chinese is substring-matched without segmentation, so
    no tokenizer is involved anywhere.
    """
    folded = unicodedata.normalize("NFKC", text).lower()
    return " ".join(folded.split())


def _expand_tool() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": _EXPAND_TOOL_NAME,
                "description": "Submit the expanded trigger vocabulary.",
                "parameters": {
                    "type": "object",
                    "properties": {"keywords": {"type": "array", "items": {"type": "string"}}},
                    "required": ["keywords"],
                },
            },
        }
    ]


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


async def _expand_once(
    provider: "LLMProvider",
    description: str,
    source_input: str,
    seeds: list[str],
    model: str | None,
) -> list[str] | None:
    response = await provider.chat_with_retry(
        messages=[
            {
                "role": "user",
                "content": _EXPAND_PROMPT.format(
                    description=description,
                    source_input=source_input or "(none)",
                    seeds=json.dumps(seeds, ensure_ascii=False),
                ),
            }
        ],
        tools=_expand_tool(),
        model=model,
        tool_choice={"type": "function", "function": {"name": _EXPAND_TOOL_NAME}},
    )
    try:
        args = required_tool_arguments(response, _EXPAND_TOOL_NAME)
    except (ProviderResponseError, RequiredToolError) as exc:
        logger.warning("trigger expansion failed: {}", exc)
        return None
    return [*(args.get("keywords") or []), *(args.get("phrases") or [])]


async def expand_triggers(
    provider: "LLMProvider",
    *,
    description: str,
    source_input: str = "",
    seeds: Triggers | None = None,
    negative_samples: list[str] | None = None,
    max_hit_rate: float = _DEFAULT_MAX_HIT_RATE,
    rounds: int = 2,
    model: str | None = None,
) -> Triggers:
    """Expansion calls plus the guard pipeline; returns the vocabulary
    ready for human review.

    ``rounds`` samples the expansion more than once and unions the results:
    a single sample is high-variance (a word present in one run vanishes in
    the next), and recall lost at L1 is unrecoverable downstream, while an
    extra entry only costs a filtered candidate. Union first, guard after.
    Seeds are kept unless a guard drops them -- the guards outrank the
    author, because a generic seed hurts the same as a generic expansion."""
    seed_words = list(seeds.keywords) if seeds is not None else []
    raw: list[str] = []
    for _ in range(max(1, rounds)):
        expanded = await _expand_once(provider, description, source_input, seed_words, model)
        if expanded is None:
            break
        raw += expanded
    return guard_triggers(
        seed_words + raw,
        negative_samples=negative_samples,
        max_hit_rate=max_hit_rate,
        what=description[:60],
    )


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
    Callers that want expansion as well go through :func:`expand_triggers`,
    which ends here.

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
