"""Land Hermes USER.md entries into the native user profile file.

EverOS is not enough on its own: six host consumers read
``user_memory/profile/user.md`` directly (Curator, Personalizer, and four
Sentinel producers), and the Sentinel daily plan takes that file as its entire
input. Content that only reaches EverOS is invisible to all of them.

Each entry targets one H2 section because injection picks whole sections --
``MemoryStore.get_memory_context`` keeps the top-2 by lexical overlap. A single
large section is therefore all-or-nothing and occupies one of the two slots,
while a small section per fact aligns the selection grain with the fact grain.
Entries that land on the same heading (including a heading the seeded USER.md
template already ships, e.g. ``## Preferences``) are appended into that
section's existing body rather than replacing it.

The LLM only picks a heading; the entry text is written verbatim. On any
failure -- a raised exception, a provider-returned error response, or an
illegal heading -- the entry lands under ``## Notes``, which injection always
includes, so a misclassification costs tokens, never visibility.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from loguru import logger

from raven.memory_engine.consolidate.consolidator import _parse_user_md_sections

if TYPE_CHECKING:
    from raven.memory_engine.consolidate.consolidator import MemoryStore
    from raven.providers.base import LLMProvider

FALLBACK_HEADING = "## Notes"

_HEADING_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 /&'-]{0,48}$")
_MAX_ENTRY_CHARS_FOR_PROMPT = 1200
# A heading is at most ~50 chars; classification-sized budget, not chat-sized.
_HEADING_MAX_TOKENS = 32

_PROMPT = (
    "Pick the single best H2 section name for this user-profile fact in a "
    "Markdown profile document. Reply with the section name only -- no '##', "
    "no punctuation, no explanation.\n\n"
    "Prefer one of: Goals, Routine schedule, Preferences, Important Notes, "
    "Work Context, Topics of Interest, Basic Information.\n\n"
    "Fact:\n{entry}"
)


async def import_user_md_sections(
    entries: list[str],
    store: "MemoryStore",
    *,
    provider: "LLMProvider | None" = None,
    model: str = "",
) -> list[str]:
    """Land each entry as its own H2 section, or append it into that section's
    body if the heading already exists. Returns headings actually written.

    Idempotency is keyed on the entry's own text, not on the heading: a
    heading collision is a routine outcome (several facts can legitimately
    share a section, and the seeded USER.md template already ships headings
    like "## Preferences"), so treating "heading already exists" as "already
    imported" would silently drop content on the common cold-start path.
    """
    written: list[str] = []
    with store.locked():
        for entry in entries:
            stripped = entry.strip()
            current = store.read_long_term()
            if stripped and stripped in current:
                logger.info("hermes user.md: entry already present, skipping")
                continue
            heading = await _pick_heading(entry, provider=provider, model=model)
            existing_body = _parse_user_md_sections(current).get(heading)
            # update_section replaces a section's body wholesale, so any existing
            # body (template placeholder text or a user edit) must be folded into
            # the new body here or it is silently destroyed.
            new_body = f"{existing_body}\n\n{stripped}" if existing_body else stripped
            store.update_section(heading, new_body, at_end=False)
            written.append(heading)
    return written


async def _pick_heading(entry: str, *, provider: "LLMProvider | None", model: str) -> str:
    if provider is None:
        return FALLBACK_HEADING
    try:
        response = await provider.chat(
            messages=[{"role": "user", "content": _PROMPT.format(entry=entry[:_MAX_ENTRY_CHARS_FOR_PROMPT])}],
            model=model,
            max_tokens=_HEADING_MAX_TOKENS,
            temperature=0.0,
        )
    except Exception as exc:
        logger.warning("hermes user.md: heading pick raised {}; using {}", exc, FALLBACK_HEADING)
        return FALLBACK_HEADING
    if response.finish_reason == "error":
        logger.warning("hermes user.md: heading pick returned finish_reason=error; using {}", FALLBACK_HEADING)
        return FALLBACK_HEADING
    raw = (response.content or "").strip()
    candidate = raw.lstrip("#").strip()
    if not _HEADING_RE.match(candidate):
        logger.warning("hermes user.md: rejected heading {!r}; using {}", raw, FALLBACK_HEADING)
        return FALLBACK_HEADING
    return f"## {candidate}"
