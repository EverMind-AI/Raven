"""Gates on a built deck. Everything here returns `Finding`.

The four that can refuse a deck live here, and they are the four design doc D3
names: a claim the materials never make, a figure cited as another figure, a
filled colour bar, and a deck whose pages the pipeline cannot tell apart. Three
are provenance or comprehension failures no layout can mitigate; the fourth is
the one design tell that prose demonstrably could not stop.

Everything else a build knows about a page is a measurement, lives in
`services.measure`, and is a WARNING. `check_deck` runs both and returns one
list; the caller filters by audience and severity.
"""

from raven.ppt.contracts.sources import SourceIndex
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
from raven.ppt.services.gates.facts import (
    NumberMention,
    check_text,
    fact_findings,
    number_mentions,
)
from raven.ppt.services.gates.mapping import mapping_findings
from raven.ppt.services.gates.registry import (
    DISPATCH,
    DeckUnderReview,
    by_page,
    check_deck,
    checks,
    for_audience,
)

# Re-exported from the ingest, which owns the index and its format. The gate
# reads what the ingest wrote; keeping a second builder here is how the two
# sides drifted onto different field names in the first place.
from raven.ppt.services.ingest.facts import build_source_index, load_source_index

__all__ = [
    "BAND_MAX_HEIGHT_EMU",
    "BAND_MIN_WIDTH_FRACTION",
    "DISPATCH",
    "DeckUnderReview",
    "NumberMention",
    "RULE_MAX_EMU",
    "STRIP_MAX_SHORT_EMU",
    "STRIP_MIN_ASPECT",
    "SourceIndex",
    "TITLE_COVERAGE_SHARE",
    "band_findings",
    "build_source_index",
    "by_page",
    "check_deck",
    "check_text",
    "checks",
    "citation_findings",
    "cited_labels",
    "data_mark_ids",
    "fact_findings",
    "figure_labels",
    "for_audience",
    "holds_text",
    "load_figure_catalog",
    "load_source_index",
    "mapping_findings",
    "material_findings",
    "number_mentions",
    "shown_labels",
]
