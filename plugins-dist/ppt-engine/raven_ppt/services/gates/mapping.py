"""Whether the pipeline can tell one page of a deck from another.

Review works one page at a time: a page's render comes back beside the part of
the author's input that drew it, and neither the author nor a finding can be
pointed at a page nothing can locate. So a deck whose pages cannot be told apart
is refused -- not because anything is wrong with it, but because nothing after
this point can do anything with it. One shipped that way, with copy running off
seven of its cards, every one of them measured and reported to nobody.
"""

from __future__ import annotations

from raven_ppt.contracts.build import BuildOutcome
from raven_ppt.contracts.findings import Finding, Severity

_MESSAGE = (
    "the pages cannot be told apart in the program that drew them: every page came from the same line of "
    "it, which happens when the file hands the work to another file or draws every page from one loop. "
    "Draw them here instead -- shared helpers first, then one block per page:\n"
    "    def new_slide(): ...        # helpers, used by every page\n"
    "    def title(sl, text): ...\n"
    "    # SLIDE 1\n"
    "    sl = new_slide()\n"
    "    title(sl, 'Unified video segmentation')\n"
    "    body(sl, [...])             # this page's own composition\n"
    "    # SLIDE 2\n"
    "    sl = new_slide()\n"
    "    ...\n"
    "Helpers are welcome and shared furniture is the point; what has to be here is the call that creates "
    "each page, because the build reads it to match a render back to the code that drew it"
)


def mapping_findings(outcome: BuildOutcome | None) -> list[Finding]:
    """Whether every page of a built deck traces back to its own block.

    A one-page deck is exempt: there is nothing to tell apart, and a shim that
    draws a single page from a single line is not the failure this describes.
    """
    if outcome is None or not outcome.ok or outcome.pages < 2:
        return []
    numbered = {source.page for source in outcome.sources}
    starts = {source.first_line for source in outcome.sources}
    if numbered == set(range(1, outcome.pages + 1)) and len(starts) == len(outcome.sources):
        return []
    return [
        Finding(
            kind="page_mapping",
            severity=Severity.WARNING,
            message=_MESSAGE,
            detail={"pages": outcome.pages, "mapped": len(outcome.sources), "distinct_starts": len(starts)},
        )
    ]
