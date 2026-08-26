"""Gates on a built deck. Everything here returns `Finding`.

What a deck can be refused over is what it was agreed to be and what it credits:
`citation` (a figure cited as another figure), `page_budget` and `language` (the
two halves of the brief a built file can be held to), and `house_style` (a deck in
somebody else's colours). The list this docstring used to give was design doc D3's
and outlived it in two places: there has never been a check on a claim the materials
never make -- no stage builds an index of the numbers in them -- and `band` was
downgraded to a warning after three misfires (D17).

Everything else here reports. `band` and `page_mapping` are readings of a built
file, not verdicts on it; `thin_material` and the three `coverage` rows say what
could not be checked rather than what is wrong. Measurements of the rendered page
live in `services.measure` and are warnings for the reason D2 gives. `check_deck`
runs both packages and returns one list; a route's `blocking_kinds` decides which
of them stop publication, and every one of them is the author's to answer.
"""

from raven.ppt.services.gates.bands import (
    BAND_MAX_HEIGHT_EMU,
    BAND_MIN_WIDTH_FRACTION,
    RULE_MAX_EMU,
    STRIP_MAX_SHORT_EMU,
    STRIP_MIN_ASPECT,
    TITLE_COVERAGE_SHARE,
    band_findings,
    data_mark_ids,
    holds_text,
)
from raven.ppt.services.gates.brief import material_findings
from raven.ppt.services.gates.citations import (
    citation_findings,
    cited_labels,
    figure_labels,
    load_figure_catalog,
    shown_labels,
)
from raven.ppt.services.gates.mapping import mapping_findings
from raven.ppt.services.gates.registry import (
    DISPATCH,
    DeckUnderReview,
    by_page,
    check_deck,
    checks,
)

__all__ = [
    "BAND_MAX_HEIGHT_EMU",
    "BAND_MIN_WIDTH_FRACTION",
    "DISPATCH",
    "DeckUnderReview",
    "RULE_MAX_EMU",
    "STRIP_MAX_SHORT_EMU",
    "STRIP_MIN_ASPECT",
    "TITLE_COVERAGE_SHARE",
    "band_findings",
    "by_page",
    "check_deck",
    "checks",
    "citation_findings",
    "cited_labels",
    "data_mark_ids",
    "figure_labels",
    "holds_text",
    "load_figure_catalog",
    "mapping_findings",
    "material_findings",
    "shown_labels",
]
