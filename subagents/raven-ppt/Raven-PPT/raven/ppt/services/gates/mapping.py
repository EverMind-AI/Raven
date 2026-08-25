"""Whether the pipeline can tell one page of a deck from another.

Every later stage works one page at a time: the design pass is handed a page's
render and the part of the author's input that drew it, and can neither review
nor repair a page it cannot locate. So a deck whose pages cannot be told apart
is refused -- not because anything is wrong with it, but because nothing after
this point can do anything with it, and a deck that silently skips the design
pass ships with every defect the pass exists to find. One did, with copy running
off seven of its cards, every one of them measured and reported to nobody.
"""

from __future__ import annotations

from raven.ppt.contracts.build import BuildOutcome
from raven.ppt.contracts.findings import Audience, Finding, Severity

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
            audience=Audience.AUTHOR,
            message=_MESSAGE,
            detail={"pages": outcome.pages, "mapped": len(outcome.sources), "distinct_starts": len(starts)},
        )
    ]
