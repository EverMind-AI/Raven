"""Sanitize remembered text before it can become pet-identity evidence.

Two dispositions only. A *drop* category means the whole item is discarded: the risk of
leaking a credential, contact, private path, health or finance detail, protected
characteristic, or a transient bad day outweighs any identity signal it carries. A *strip*
category means the offending sentence is removed and the remainder may survive, because
prompt-injection text is usually appended to otherwise-useful memory.

Recalled text is data, never instructions: nothing here interprets what a memory asks for.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass

CATEGORY_CREDENTIALS = "credentials"
CATEGORY_CONTACTS = "contacts"
CATEGORY_PRIVATE_PATHS = "private-paths"
CATEGORY_SENSITIVE = "sensitive-personal-data"
CATEGORY_DEMOGRAPHICS = "demographics"
CATEGORY_INCIDENTS = "negative-incidents"
CATEGORY_INSTRUCTIONS = "embedded-instructions"
CATEGORY_FORESIGHT = "foresight"

EXCLUDED_CATEGORIES: tuple[str, ...] = (
    CATEGORY_CREDENTIALS,
    CATEGORY_CONTACTS,
    CATEGORY_PRIVATE_PATHS,
    CATEGORY_SENSITIVE,
    CATEGORY_DEMOGRAPHICS,
    CATEGORY_INCIDENTS,
    CATEGORY_INSTRUCTIONS,
    CATEGORY_FORESIGHT,
)

MIN_EVIDENCE_CHARS = 12

_DROP_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (CATEGORY_CREDENTIALS, re.compile(r"\b(?:sk|pk|rk)-[A-Za-z0-9_\-]{16,}")),
    (CATEGORY_CREDENTIALS, re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}")),
    (CATEGORY_CREDENTIALS, re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    (CATEGORY_CREDENTIALS, re.compile(r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}")),
    (
        CATEGORY_CREDENTIALS,
        re.compile(
            r"(?i)\b(?:api[_\- ]?key|secret|access[_\- ]?token|token|password|passwd|credential)\b\s*[:=]\s*\S+"
        ),
    ),
    (CATEGORY_CREDENTIALS, re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}")),
    (CATEGORY_CONTACTS, re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
    (CATEGORY_CONTACTS, re.compile(r"(?<!\w)\+?\d[\d\s\-()]{8,}\d(?!\w)")),
    (
        CATEGORY_CONTACTS,
        re.compile(
            r"(?i)\b\d{1,6}\s+[A-Za-z][A-Za-z.'-]*(?:\s+[A-Za-z][A-Za-z.'-]*){0,3}\s+"
            r"(?:street|st|avenue|ave|boulevard|blvd|road|rd|lane|ln|drive|dr|court|ct|"
            r"place|pl|way|terrace|circle|cir|highway|hwy|parkway|pkwy|square|sq)\b\.?"
        ),
    ),
    (CATEGORY_CONTACTS, re.compile(r"(?<!\d)(?:\d{1,3}\.){3}\d{1,3}(?!\d)")),
    (CATEGORY_PRIVATE_PATHS, re.compile(r"(?:/Users/|/home/|/var/folders/|/private/)\S+")),
    (CATEGORY_PRIVATE_PATHS, re.compile(r"[A-Za-z]:\\\\?[^\s\"']+")),
    (CATEGORY_PRIVATE_PATHS, re.compile(r"(?<!\w)~/\S+")),
    (
        CATEGORY_SENSITIVE,
        re.compile(
            r"(?i)\b(?:salary|compensation|bonus|net worth|bank account|credit card|iban|ssn|"
            r"social security|passport|mortgage|tax return|invoice|diagnosis|prescription|"
            r"medication|therapy|medical|clinical|lawsuit|attorney|litigation|visa status|"
            r"hiv|aids|cancer|diabetes|epilepsy|schizophrenia|bipolar disorder|leukemia|"
            r"alzheimer'?s|dementia|autism|adhd|ptsd|tumor)\b"
        ),
    ),
    (CATEGORY_SENSITIVE, re.compile(r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)")),
    (CATEGORY_SENSITIVE, re.compile(r"(?<!\d)\d{13,19}(?!\d)")),
    (
        CATEGORY_DEMOGRAPHICS,
        re.compile(
            r"(?i)(?:\b(?:is|aged)\s+\d{1,2}\s+years?\s+old\b|\bis\s+\d{1,2}\b(?=\s*(?:,|\.|$))|"
            r"\b(?:ethnicity|race|religion|religious|nationality|sexual orientation|"
            r"disability|pregnan\w+|marital status|"
            r"gay|lesbian|bisexual|homosexual|heterosexual|pansexual|asexual|queer|"
            r"transgender|cisgender|non-binary|nonbinary|genderqueer|genderfluid|"
            r"gender identity|gender dysphoria|"
            r"christian|catholic|protestant|muslim|islamic|jewish|hindu|buddhist|sikh|atheist|mormon|"
            r"divorced?|custody battle|custody dispute|marital separation)\b)"
        ),
    ),
    (
        CATEGORY_INCIDENTS,
        re.compile(
            r"(?i)\b(?:crashed|outage|regression|panicked|angry|furious|frustrated|upset|"
            r"blew up|went wrong|screwed up|missed the deadline|got rejected|was fired)\b"
        ),
    ),
)

_STRIP_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"(?i)\b(?:ignore|disregard|forget)\b[^.!?]*\b(?:instruction|instructions|prompt|prompts|rules?)\b[^.!?]*[.!?]?"
    ),
    re.compile(
        r"(?i)\b(?:you are now|act as|pretend to be|new instructions?|system prompt|override[^.!?]*rules?)\b[^.!?]*[.!?]?"
    ),
    re.compile(r"(?i)\b(?:run|execute|curl|wget|npm install|pip install|rm\s+-rf|sudo)\b[^.!?]*[.!?]?"),
    re.compile(
        r"(?i)\b(?:visit|open|navigate to|go to|fetch|download)\b[^.!?]{0,80}?https?://\S+"
        r"(?:\s+and\s+\w+\s+(?:it|this|that|them))?[.!?]?"
    ),
    re.compile(r"(?is)<\s*/?\s*(?:script|system|instructions|tool_call)\s*>[^<]*(?:<\s*/\s*\w+\s*>)?"),
)

_BARE_URL = re.compile(r"https?://\S+")
_WHITESPACE = re.compile(r"\s+")
_DEDUPE_STRIP = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class Sanitized:
    """Outcome of sanitizing one remembered item. ``text is None`` means dropped."""

    text: str | None
    removed: tuple[str, ...]


def sanitize_memory_text(text: str, *, max_chars: int = 240) -> Sanitized:
    collapsed = _WHITESPACE.sub(" ", text or "").strip()
    if not collapsed:
        return Sanitized(text=None, removed=())

    removed: list[str] = []
    for category, pattern in _DROP_PATTERNS:
        if pattern.search(collapsed):
            if category not in removed:
                removed.append(category)
    if removed:
        return Sanitized(text=None, removed=tuple(removed))

    stripped = collapsed
    for pattern in _STRIP_PATTERNS:
        replaced = pattern.sub(" ", stripped)
        if replaced != stripped:
            if CATEGORY_INSTRUCTIONS not in removed:
                removed.append(CATEGORY_INSTRUCTIONS)
            stripped = replaced

    without_urls = _BARE_URL.sub(" ", stripped)
    if without_urls != stripped:
        stripped = without_urls

    stripped = _WHITESPACE.sub(" ", stripped).strip(" ;,-")
    if len(stripped) < MIN_EVIDENCE_CHARS:
        return Sanitized(text=None, removed=tuple(removed))

    return Sanitized(text=stripped[:max_chars].rstrip(), removed=tuple(removed))


def normalize_for_dedupe(text: str) -> str:
    return _DEDUPE_STRIP.sub(" ", text.lower()).strip()


def evidence_ref(source: str, text: str) -> str:
    """Content-addressed reference. Carries provenance and a digest, never the text."""
    digest = hashlib.sha256(normalize_for_dedupe(text).encode("utf-8")).hexdigest()
    return f"{source}:sha256:{digest[:12]}"
