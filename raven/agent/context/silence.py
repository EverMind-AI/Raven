"""The markers a model ends a turn with to say "nothing to report", and the rules that read them.

Silence is something the model chooses, by answering with a marker. It is not:

* an empty reply -- a call that returned no text is a failure for the empty-response recovery to
  handle, so every matcher here answers False for blank text;
* a failed turn -- an error has no answer to withhold, so callers ask only about turns that
  completed, and a marker never hides a failure;
* a marker inside a report -- prose that merely mentions one is delivered like any other prose.

The rule depends on who the turn is for, and ``is_silence`` is the one place that picks it, so the
lanes cannot drift apart. A turn a person started is never taught a marker, so it honours only the
bracketed ``[SILENT]`` the runtime teaches, as the whole reply: a one-word answer such as ``Silent.``
or ``No reply.`` is the person's answer. A turn from a proactive origin (cron, sub-agent, sentinel,
heartbeat) is told to answer ``[SILENT]`` when nothing is new, and models reliably vary the spelling
and wrap it in a short note on why they stayed quiet, so it reads the whole marker set by two rules:
the strict one (``is_exact_silence``), a reply that is exactly a marker, and the loose one
(``is_loose_silence``), where the bracketed ``[SILENT]`` opening the reply, or any marker alone on
its first or last line, counts too.
"""

import unicodedata

from raven.spine.turn import PROACTIVE_ORIGINS, Origin

SILENT_MARKER = "[SILENT]"
SILENCE_MARKERS = frozenset({SILENT_MARKER, "SILENT", "NO_REPLY", "NO REPLY"})
SILENCE_MAX_CHARS = 64

SILENCE_INSTRUCTION = (
    'SILENT: If there is genuinely nothing new to report, respond with exactly "[SILENT]" (nothing else) '
    "to suppress delivery. Never combine [SILENT] with content — either report your findings normally, "
    "or say [SILENT] and nothing more."
)


def _canonical_silence_candidate(text: str) -> str:
    return " ".join(text.strip().upper().split())


def _strip_edge_silence_punctuation(text: str) -> str:
    """Strip stray edge punctuation without erasing marker structure.

    Models sometimes emit ``.NO_REPLY`` or ``*NO_REPLY*`` instead of the exact marker. Square
    brackets stay structural, so a malformed ``[SILENT`` does not become ``SILENT``.
    """
    start = 0
    end = len(text)
    while start < end and text[start] not in "[]" and unicodedata.category(text[start]).startswith("P"):
        start += 1
    while end > start and text[end - 1] not in "[]" and unicodedata.category(text[end - 1]).startswith("P"):
        end -= 1
    return text[start:end].strip()


def _canonical_silence_candidates(text: str) -> tuple[str, ...]:
    exact = _canonical_silence_candidate(text)
    stripped = _strip_edge_silence_punctuation(text.strip())
    if stripped == text.strip():
        return (exact,)
    fallback = _canonical_silence_candidate(stripped)
    return (exact, fallback)


def is_exact_silence(text: object) -> bool:
    """The strict rule: True only when ``text`` is exactly a marker.

    Case, edge whitespace and stray edge punctuation are tolerated. Blank text is not silence, and
    prose that merely mentions a marker is delivered normally.
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False
    if len(stripped) > SILENCE_MAX_CHARS:
        return False
    return any(candidate in SILENCE_MARKERS for candidate in _canonical_silence_candidates(stripped))


def is_loose_silence(text: object) -> bool:
    """The loose rule: a marker may come with a note beside it.

    Silence when a marker is the whole reply, sits on its own first or last line, or the bracketed
    ``[SILENT]`` opens the reply (``[SILENT] No changes detected``). Only the bracketed form may
    open a line of prose, so a bare word as in ``Silent retry succeeded`` is not swallowed, and a
    marker buried mid-sentence in a real report is delivered.
    """
    if not isinstance(text, str):
        return False
    stripped = text.strip()
    if not stripped:
        return False

    def _is_token(line: str) -> bool:
        return _canonical_silence_candidate(line) in SILENCE_MARKERS

    if _is_token(stripped):
        return True
    lines = [ln for ln in stripped.splitlines() if ln.strip()]
    if lines and (_is_token(lines[0]) or _is_token(lines[-1])):
        return True
    return stripped.upper().startswith(SILENT_MARKER)


def is_silence(text: object, origin: Origin | None) -> bool:
    """Whether ``text`` is silence under the rules ``origin`` picks.

    A turn from a proactive origin reads the loose rule and the strict one: the loose matcher takes a
    whole-reply marker as written, so ``NO_REPLY.`` needs the strict rule's forgiveness of edge
    punctuation. Every other origin, and none, honours only ``[SILENT]`` with whitespace around it,
    so an origin this table does not know delivers a reply rather than swallowing it.
    """
    if origin in PROACTIVE_ORIGINS:
        return is_loose_silence(text) or is_exact_silence(text)
    return isinstance(text, str) and text.strip() == SILENT_MARKER
