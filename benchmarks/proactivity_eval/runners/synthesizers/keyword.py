"""Keyword-based deterministic context synthesizer.

Recognizes activity category (coding / writing / research / communication /
data_analysis) via keyword matching, identifies language or topic when signals
are strong enough, and emits at most 2 candidate routines when repeated
patterns are observed.

Rules are intentionally simple and transparent — auditable in ~150 lines —
so readers can judge whether benchmark numbers reflect the Planner's real
capability or synthesizer over-fitting.

Replaceable: register additional synthesizers in synthesizers/__init__.py.
"""

from __future__ import annotations

import re
from collections import Counter

from raven.proactive_engine.sentinel.types import Routine

from .base import SynthesizedContext


CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "coding": [
        "vscode", "visual studio code", "ide", "pycharm", "intellij",
        "debugger", "breakpoint", "compile", "runtime",
        "stack overflow", "github", "gitlab", "terminal", "console",
        ".py", ".rb", ".js", ".ts", ".go", ".rs", ".java", ".cpp",
        ".php", ".sh", ".sql",
    ],
    "writing": [
        "markdown", "paragraph", "draft", "article", "blog post",
        "wrote", "writing notes", "document",
        ".md", ".doc", ".docx", ".txt",
    ],
    "research": [
        "searches", "google", "wikipedia", "paper", "documentation",
        "browser", "clicked on a link", "scrolls through", "browses",
    ],
    "communication": [
        "email", "inbox", "slack", "message", "reply",
        "compose", "chat conversation",
    ],
    "data_analysis": [
        "spreadsheet", "excel", "csv", "dataframe", "jupyter",
        "chart", "visualization",
    ],
}

LANGUAGE_HINTS: dict[str, list[str]] = {
    "Python": [".py", " python "],
    "Ruby": [".rb", " ruby "],
    "JavaScript": [".js", " javascript", " node.js"],
    "TypeScript": [".ts", " typescript"],
    "Go": [".go", " golang"],
    "PHP": [".php", " php "],
    "Java": [".java", " java "],
    "Rust": [".rs", " rust "],
    "C++": [".cpp", " c++"],
}

# Regex patterns for extracting topics from event prose.
# The inner \w\s\-' set is non-greedy and stops at common English prepositions
# that usually begin a *location* clause ("in VSCode", "at desk") — those
# belong in the environment description, not the research topic.
_STOP_PREPS = r"(?:\s+(?:in|on|at|from|using|with|via)\s+|[.,;:]|$)"

TOPIC_EXTRACTORS = [
    re.compile(
        r"search(?:es|ed|ing)?\s+(?:for|about)\s+['\"]?"
        r"([A-Za-z][\w\s\-']{2,40}?)(?=['\"]|" + _STOP_PREPS + ")",
        re.IGNORECASE,
    ),
    re.compile(
        r"research(?:es|ed|ing)?\s+(?:on|about)\s+"
        r"([A-Za-z][\w\s\-']{2,40}?)(?=" + _STOP_PREPS + ")",
        re.IGNORECASE,
    ),
    re.compile(
        r"article\s+(?:about|on)\s+([A-Za-z][\w\s\-']{2,40}?)(?=" + _STOP_PREPS + ")",
        re.IGNORECASE,
    ),
]

PROFILE_TEMPLATES: dict[str, str] = {
    "coding_with_lang": (
        "The user is a developer currently working on a {language} project. "
        "They consult documentation actively when debugging or exploring APIs."
    ),
    "coding": (
        "The user is a developer engaged in a coding session, alternating "
        "between writing code and consulting references as needed."
    ),
    "writing_with_topic": (
        "The user is writing a document on '{topic}'. They are in a "
        "content-drafting phase, alternating between writing and reference "
        "collection."
    ),
    "writing": (
        "The user is writing in a long-form text editor, drafting or "
        "revising content."
    ),
    "research_with_topic": (
        "The user is conducting research on '{topic}', collecting references "
        "from multiple sources."
    ),
    "research": (
        "The user is conducting online research, visiting multiple sources."
    ),
    "communication": (
        "The user is handling their inbox or messages, writing and reading "
        "replies."
    ),
    "data_analysis": (
        "The user is analyzing data in a spreadsheet or analytical tool."
    ),
    "general": "The user is engaged in a focused work session.",
}


class KeywordSynthesizer:
    """Keyword + regex deterministic context synthesizer."""

    name: str = "keyword"

    def __init__(
        self,
        routine_threshold: int = 3,
        min_duration_for_memory: int = 60,
    ) -> None:
        self.routine_threshold = routine_threshold
        self.min_duration_for_memory = min_duration_for_memory

    def synthesize(self, obs: list[dict]) -> SynthesizedContext:
        if not obs:
            return SynthesizedContext(user_profile=PROFILE_TEMPLATES["general"])

        text = " ".join(e.get("event", "") for e in obs).lower()
        category = self._detect_category(text)
        language = self._detect_language(text) if category == "coding" else None
        topic = self._extract_topic(obs)

        return SynthesizedContext(
            user_profile=self._render_profile(category, language, topic),
            routines=self._detect_routines(obs),
            memory_md=self._synthesize_memory(obs, category, topic, language),
        )

    # ------------------------------------------------------------------
    # Category / language / topic detection

    def _detect_category(self, text: str) -> str:
        scores = {
            cat: sum(1 for kw in kws if kw in text)
            for cat, kws in CATEGORY_KEYWORDS.items()
        }
        if not any(scores.values()):
            return "general"
        return max(scores, key=lambda c: scores[c])

    def _detect_language(self, text: str) -> str | None:
        scores = {
            lang: sum(1 for hint in hints if hint in text)
            for lang, hints in LANGUAGE_HINTS.items()
        }
        best = max(scores, key=lambda l: scores[l]) if scores else None
        if best and scores[best] > 0:
            return best
        return None

    def _extract_topic(self, obs: list[dict]) -> str | None:
        topics: list[str] = []
        for ev in obs:
            ev_text = ev.get("event", "")
            for pat in TOPIC_EXTRACTORS:
                topics.extend(m.strip().rstrip(".,;") for m in pat.findall(ev_text))
        topics = [t for t in topics if 3 <= len(t) <= 60]
        if not topics:
            return None
        most_common, _ = Counter(topics).most_common(1)[0]
        return most_common

    # ------------------------------------------------------------------
    # Profile rendering

    def _render_profile(
        self,
        category: str,
        language: str | None,
        topic: str | None,
    ) -> str:
        if category == "coding" and language:
            return PROFILE_TEMPLATES["coding_with_lang"].format(language=language)
        if category == "writing" and topic:
            return PROFILE_TEMPLATES["writing_with_topic"].format(topic=topic)
        if category == "research" and topic:
            return PROFILE_TEMPLATES["research_with_topic"].format(topic=topic)
        return PROFILE_TEMPLATES.get(category, PROFILE_TEMPLATES["general"])

    # ------------------------------------------------------------------
    # Routine detection — candidate only, never active.

    def _detect_routines(self, obs: list[dict]) -> list[Routine]:
        routines: list[Routine] = []
        events_lower = [e.get("event", "").lower() for e in obs]

        # Pattern 1: editor <-> browser alternation while working.
        editor_mentions = sum(
            1 for e in events_lower
            if any(kw in e for kw in ("vscode", "ide", "pycharm"))
        )
        browser_mentions = sum(
            1 for e in events_lower
            if any(kw in e for kw in ("google", "browser", "search", "scrolls"))
        )
        if editor_mentions >= 2 and browser_mentions >= 2:
            routines.append(Routine(
                id="synthetic-research-while-working",
                pattern="user alternates between editor and web research",
                occurrence_count=min(editor_mentions, browser_mentions),
                status="candidate",
                user_confirmed=False,
            ))

        # Pattern 2: repeated search activity (>= threshold).
        search_events = sum(
            1 for e in events_lower
            if any(kw in e for kw in ("searches", "search for", "google"))
        )
        if search_events >= self.routine_threshold:
            routines.append(Routine(
                id="synthetic-search-heavy",
                pattern=(
                    f"user has been performing repeated searches "
                    f"({search_events} in this session)"
                ),
                occurrence_count=search_events,
                status="candidate",
                user_confirmed=False,
            ))

        return routines[:2]

    # ------------------------------------------------------------------
    # Short memory note — mimics "Sentinel's last tick observation".

    def _synthesize_memory(
        self,
        obs: list[dict],
        category: str,
        topic: str | None,
        language: str | None,
    ) -> str:
        duration = self._compute_duration(obs)
        if duration < self.min_duration_for_memory:
            return ""

        focus_parts = [p for p in (language, topic) if p]
        focus = ", ".join(focus_parts) if focus_parts else category

        return (
            f"- Observed (just now, ~{duration // 60}min session): "
            f"user is in {category} mode, focus on {focus}."
        )

    @staticmethod
    def _compute_duration(obs: list[dict]) -> int:
        times: list[float] = []
        for e in obs:
            try:
                times.append(float(e.get("time", "")))
            except (TypeError, ValueError):
                continue
        if len(times) < 2:
            return 0
        return int(max(times) - min(times))


__all__ = ["KeywordSynthesizer"]
