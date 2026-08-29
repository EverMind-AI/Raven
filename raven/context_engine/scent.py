"""The scent menu — pull-mode skill discovery's per-turn hint.

Replaces the push pipeline's per-turn LLM work (rewriter + gate) with rules
and a cheap lookup: on a *fat* turn, one ``router.select`` over the same
three sources renders a few name+description lines into the turn's user
envelope. No bodies, no system-prefix bytes, no LLM calls. The model pulls
what it wants through ``find_skill`` / ``read_skill``.

Judgement errors are engineered to be near-free in both directions: a turn
wrongly judged fat costs one sub-millisecond local lookup (plus one light
Hub query when wired); a turn wrongly judged lean costs only that turn's
menu, with ``find_skill`` as the backstop. (The menu is stripped from the
persisted session together with the runtime-context block, so it never
carries over between turns.) That asymmetry is why the classifier below is
a handful of rules and not a model.
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.i18n import zh_lexicon
from raven.security.trust import wrap_untrusted
from raven.skill_hub.policy import SkillPolicy, is_blocked, refuses_low_safety

if TYPE_CHECKING:
    from raven.memory_engine import SkillForgeRouter

_MIN_CHARS = 8
_MIN_CORE_CHARS = 4
_NOVELTY_THRESHOLD = 0.3
# Anaphora-opened turns need a much higher bar: short Chinese follow-ups are
# mostly function-word bigrams, which are novel as strings while carrying no
# new topic. Only near-total novelty (a new entity/task after the opener)
# should override the follow-up reading.
_ANAPHORA_NOVELTY = 0.8
_WINDOW_USER_MESSAGES = 2
_QUERY_WINDOW_CAP = 200
# Assembly sits on the turn's critical path; a slow remote source must cost a
# missing menu, never a stalled turn. Local BM25 answers in microseconds, so
# only a remote source can hit this.
_BUILD_TIMEOUT_S = 2.0

# Function words and interjections that carry no retrievable intent. Domain
# nouns do not belong here — this list only strips what makes a turn *lean*,
# the novelty test does the topic work.
_FUNCTION_WORDS = (
    *zh_lexicon.FUNCTION_WORDS,
    "the",
    "please",
    "just",
    "okay",
    "ok",
    "yes",
    "no",
)

# Openers that refer back to something already in the conversation; combined
# with low novelty they mark a follow-up turn.
_ANAPHORA_PREFIXES = (
    *zh_lexicon.ANAPHORA_PREFIXES,
    "and ",
    "also ",
    "then ",
    "it ",
    "that ",
)

_TASK_VERBS = (
    *zh_lexicon.TASK_VERBS,
    "review",
    "write",
    "build",
    "analyze",
    "compare",
    "fix",
    "find",
)


def _normalize(text: str) -> str:
    folded = unicodedata.normalize("NFKC", text or "").lower()
    return " ".join(folded.split())


def _strip_function_words(text: str) -> str:
    ascii_stop = {w for w in _FUNCTION_WORDS if w.isascii()}
    tokens = [t for t in text.split() if t not in ascii_stop]
    out = " ".join(tokens)
    for w in _FUNCTION_WORDS:
        if not w.isascii():
            out = out.replace(w, "")
    return re.sub(r"[\s,!?." + zh_lexicon.PUNCTUATION_MARKS + r"]+", "", out)


def _bigrams(text: str) -> set[str]:
    squeezed = re.sub(r"\s+", "", text)
    if len(squeezed) < 2:
        return {squeezed} if squeezed else set()
    return {squeezed[i : i + 2] for i in range(len(squeezed) - 1)}


def novelty(message: str, window: str) -> float:
    """Fraction of the message's character bigrams unseen in the window.

    High novelty means the turn brings new material (a topic switch is the
    typical case); a follow-up re-uses the window's vocabulary and scores
    low. Pure set arithmetic — the point is to be free, not clever.
    """
    grams = _bigrams(_normalize(message))
    if not grams:
        return 0.0
    seen = _bigrams(_normalize(window))
    return len(grams - seen) / len(grams)


def _starts_with_anaphora(text: str) -> bool:
    return text.startswith(_ANAPHORA_PREFIXES)


def _has_task_verb(text: str) -> bool:
    return any(v in text for v in _TASK_VERBS)


def recent_user_window(session_messages: list[dict[str, Any]], limit: int = _WINDOW_USER_MESSAGES) -> str:
    """The last few user messages, oldest first, joined for novelty/query use."""
    texts: list[str] = []
    for msg in reversed(session_messages):
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            texts.append(content)
            if len(texts) >= limit:
                break
    return "\n".join(reversed(texts))


def is_fat(message: str, window: str) -> bool:
    """Whether this turn warrants a fresh menu lookup."""
    t = _normalize(message)
    if len(t) < _MIN_CHARS:
        return False
    if len(_strip_function_words(t)) < _MIN_CORE_CHARS:
        return False
    nov = novelty(message, window)
    if _starts_with_anaphora(t) and nov < _ANAPHORA_NOVELTY:
        return False
    return nov >= _NOVELTY_THRESHOLD or _has_task_verb(t)


def build_scent_query(message: str, window: str) -> str:
    """The retrieval query for a fat turn.

    A fresh topic stands on its own; a task phrased against the conversation
    (a Chinese "look up its pricing again") needs the window so its referents resolve into
    retrievable words.
    """
    t = _normalize(message)
    if _starts_with_anaphora(t) or novelty(message, window) < _NOVELTY_THRESHOLD:
        return (window[-_QUERY_WINDOW_CAP:] + "\n" + message).strip()
    return message


@dataclass(frozen=True)
class ScentResult:
    """One turn's menu: the rendered text plus the qualified ids it shows.

    The ids feed ``AssembledContext.metadata["injected_skill_ids"]`` so the
    after-turn backend feedback keeps receiving the skills the model was
    offered, exactly as it did under push injection. Empty text means a
    silent turn (lean / no hits / lookup failure).
    """

    text: str = ""
    skill_ids: list[str] = field(default_factory=list)

    def __bool__(self) -> bool:
        return bool(self.text)


_SILENT = ScentResult()


class ScentMenu:
    """Builds the per-turn menu (empty ``ScentResult`` when there is nothing to say)."""

    def __init__(
        self,
        router: "SkillForgeRouter",
        *,
        policy: SkillPolicy | None = None,
        top_k: int = 3,
        desc_chars: int = 60,
    ) -> None:
        self._router = router
        self._policy = policy
        self._top_k = top_k
        self._desc_chars = desc_chars

    async def build(self, current_message: str, session_messages: list[dict[str, Any]]) -> ScentResult:
        window = recent_user_window(session_messages)
        if not is_fat(current_message, window):
            return _SILENT
        query = build_scent_query(current_message, window)
        try:
            hits = await asyncio.wait_for(
                self._router.select(query, history=[], k=self._top_k), timeout=_BUILD_TIMEOUT_S
            )
        except TimeoutError:
            logger.warning("scent menu lookup timed out after {}s; skipping this turn", _BUILD_TIMEOUT_S)
            return _SILENT
        except Exception:  # noqa: BLE001 - a scent failure must never break assembly
            logger.opt(exception=True).warning("scent menu lookup failed; skipping this turn")
            return _SILENT
        lines = []
        ids: list[str] = []
        for h in hits or []:
            if self._policy is not None and _refused_by_policy(h, self._policy):
                continue
            desc = _first_line(h.meta.get("description") or h.content)[: self._desc_chars]
            if not desc:
                continue
            lines.append(f"- {h.qualified_id}: {desc}")
            ids.append(h.qualified_id)
        if not lines:
            return _SILENT
        shown_query = query.replace("\n", " / ")[:80]
        header = f"Possibly relevant skills (matched for: {shown_query!r}):"
        footer = "(If irrelevant, ignore. Read one with read_skill(id); search differently with find_skill.)"
        return ScentResult(
            text=wrap_untrusted("\n".join([header, *lines, footer]), source="skill catalog"),
            skill_ids=ids,
        )


def _refused_by_policy(hit: Any, policy: SkillPolicy) -> bool:
    """Advertising-time policy screen over a router hit.

    Blocklisted skills must not be advertised by id at all; a catalog hit
    that already carries a ``score_safety`` below the bar is dropped too.
    Hub catalog payloads usually omit the score, so the authoritative
    safety check for those stays with ``read_skill`` / ``use_skill`` on the
    detail metadata.
    """
    meta = getattr(hit, "meta", None) or {}
    if is_blocked(policy.blocklist, getattr(hit, "name", None), meta.get("skill_id"), meta.get("slug")):
        logger.debug("scent menu: dropping blocklisted skill {}", hit.qualified_id)
        return True
    if refuses_low_safety(meta.get("score_safety"), policy.min_safety):
        logger.debug("scent menu: dropping low-safety skill {}", hit.qualified_id)
        return True
    return False


def _first_line(text: str) -> str:
    stripped = (text or "").strip()
    return stripped.splitlines()[0] if stripped else ""
