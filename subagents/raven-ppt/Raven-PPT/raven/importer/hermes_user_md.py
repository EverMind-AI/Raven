"""Land Hermes USER.md entries into the native user profile file.

EverOS is not enough on its own: six host consumers read
``user_memory/profile/user.md`` directly (Curator, Personalizer, and four
Sentinel producers), and the Sentinel daily plan takes that file as its entire
input. Content that only reaches EverOS is invisible to all of them.

Each entry is classified to one H2 section because injection picks whole
sections -- ``MemoryStore.get_memory_context`` keeps the top-2 by lexical
overlap -- so which section an entry lands in is what determines whether a
later turn ever sees it. In practice the classifier is biased toward the
handful of headings the seeded USER.md template already ships (e.g.
``## Preferences``), so most entries append into an existing section's body
rather than minting a new one; a new heading is created only when nothing
already on file fits.

The LLM only picks a heading; the entry text is written verbatim. On any
failure -- a raised exception, a provider-returned error response, or an
illegal heading -- the entry lands under ``## Notes``, which injection always
includes, so a misclassification costs tokens, never visibility.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from loguru import logger

from raven.memory_engine.consolidate.consolidator import parse_user_md_sections

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


@dataclass(frozen=True)
class ImportedSections:
    """Headings written, plus how many entries were already present.

    ``len()`` reflects only ``written`` -- matching the plain list of
    headings this replaces, so ``len(result)`` at the existing call site
    keeps counting what actually landed, not what was skipped.
    """

    written: tuple[str, ...]
    skipped: int

    def __len__(self) -> int:
        return len(self.written)


async def import_user_md_sections(
    entries: list[str],
    store: "MemoryStore",
    *,
    provider: "LLMProvider | None" = None,
    model: str = "",
    on_progress: Callable[[int, int], None] | None = None,
) -> ImportedSections:
    """Land each entry as its own H2 section, or append it into that section's
    body if the heading already exists. ``written`` holds one heading per
    entry actually written, so two entries sharing a section yield that
    heading twice; ``skipped`` counts entries already present verbatim in
    their target section.

    Entry text is written unchanged apart from surrounding whitespace; blank
    entries carry nothing to import and are dropped (and are not counted as
    skipped).

    Idempotency is keyed on the entry's own text against its *target
    section's* body, not the heading and not the whole file: a heading
    collision is a routine outcome (several facts can legitimately share a
    section, and the seeded USER.md template already ships headings like
    "## Preferences"), so treating "heading already exists" as "already
    imported" would silently drop content on the common cold-start path.
    Matching the whole file would go too far the other way and drop a
    distinct entry that happens to be a substring of unrelated text
    elsewhere in the profile.

    Heading classification (the LLM call) never depends on the file's current
    content, so every heading is picked before the write lock is taken --
    only the read-check-write of each section is serialized, keeping a
    classification-heavy run from stalling every other writer of user.md for
    its whole duration.
    """
    kept = [(entry, stripped) for entry, stripped in ((e, e.strip()) for e in entries) if stripped]
    # One LLM call per entry, and the caller has no other way to tell this apart
    # from a hang: on a real install three entries took 9.4s, all of it here.
    # Reported as *completed*, before each call rather than after it: counting
    # the call about to be made showed 3/3 while the third was still in flight,
    # which is the 100%-then-wait this progress line exists to remove. The close
    # after the loop is what still reaches N/N.
    headings: list[str] = []
    for index, (entry, _) in enumerate(kept):
        if on_progress is not None:
            on_progress(index, len(kept))
        headings.append(await _pick_heading(entry, provider=provider, model=model))
    if on_progress is not None and kept:
        on_progress(len(kept), len(kept))

    written: list[str] = []
    skipped = 0
    with store.locked():
        for (_, stripped), heading in zip(kept, headings):
            current = store.read_long_term()
            existing_body = parse_user_md_sections(current).get(heading, "")
            if _entry_already_in_section(stripped, existing_body):
                logger.info("hermes user.md: entry already present in {}, skipping", heading)
                skipped += 1
                continue
            # update_section replaces a section's body wholesale, so any existing
            # body (template placeholder text or a user edit) must be folded into
            # the new body here or it is silently destroyed.
            new_body = f"{existing_body}\n\n{stripped}" if existing_body else stripped
            store.update_section(heading, new_body, at_end=False)
            written.append(heading)
    return ImportedSections(written=tuple(written), skipped=skipped)


def _entry_already_in_section(entry: str, section_body: str) -> bool:
    """True iff ``entry`` is already one of the blank-line-separated
    paragraphs of ``section_body`` -- the same shape ``import_user_md_sections``
    appends entries in, so an exact match here means this entry, not merely
    text that happens to contain or be contained by it."""
    return entry in {paragraph.strip() for paragraph in section_body.split("\n\n")}


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
