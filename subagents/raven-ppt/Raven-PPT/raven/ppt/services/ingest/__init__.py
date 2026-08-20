"""Material ingestion: source documents -> text + assets + a checkable index.

The one entry point is :func:`ingest_materials`. Everything else exported here
exists because another part of the system has to agree with this one:

* the fact gate must tokenise and normalise exactly as the index did, so it
  takes ``canonical_number`` and the regexes from here rather than restating
  them -- two copies of those rules drifting by one suffix is a gate that
  rejects a number the source plainly prints;
* a figure-cropping tool needs the same pixel operations the ingest used;
* whatever reads the catalogue back needs the loader that wrote it.

Nothing here calls a model, and nothing here knows which route a deck is being
built by.
"""

from raven.ppt.services.ingest.assets import load_catalogue, write_catalogue
from raven.ppt.services.ingest.documents import TEXT_LAYER_CHARS_PER_PAGE, text_density
from raven.ppt.services.ingest.facts import (
    CAPS_PHRASE_RE,
    ENTITY_RE,
    NUMBER_RE,
    build_source_index,
    canonical_number,
    iter_numbers,
    load_source_index,
    normalise,
    write_source_index,
)
from raven.ppt.services.ingest.images import (
    DETAIL_INK_MAX,
    FIGURE_SUFFIXES,
    autocrop_border,
    contain_scale,
    crop_figure,
    detect_panels,
    edge_cut_share,
    estimate_panels,
    ink_ratio,
    interior_ink_ratio,
    segment_page_blocks,
)
from raven.ppt.services.ingest.pipeline import (
    CATALOGUE_FILE,
    FIGURES_DIR,
    MANIFEST_FILE,
    MATERIALS_FILE,
    SOURCE_INDEX_FILE,
    ingest_materials,
)
from raven.ppt.services.ingest.sections import MAX_PARTS, Section, index, sections
from raven.ppt.services.ingest.sources import (
    ATTACHED,
    FETCH,
    MIRROR,
    REQUEST,
    SOURCES_DIR,
    Source,
    fetched,
    held,
    manifest_path,
    mirror,
    receive,
    sources_dir,
    take,
    write_source,
)

__all__ = [
    "MAX_PARTS",
    "Section",
    "index",
    "sections",
    "CAPS_PHRASE_RE",
    "CATALOGUE_FILE",
    "DETAIL_INK_MAX",
    "ENTITY_RE",
    "FIGURES_DIR",
    "FIGURE_SUFFIXES",
    "ATTACHED",
    "MANIFEST_FILE",
    "MATERIALS_FILE",
    "NUMBER_RE",
    "SOURCES_DIR",
    "SOURCE_INDEX_FILE",
    "Source",
    "TEXT_LAYER_CHARS_PER_PAGE",
    "autocrop_border",
    "build_source_index",
    "canonical_number",
    "contain_scale",
    "crop_figure",
    "detect_panels",
    "edge_cut_share",
    "estimate_panels",
    "ingest_materials",
    "FETCH",
    "MIRROR",
    "REQUEST",
    "fetched",
    "held",
    "ink_ratio",
    "interior_ink_ratio",
    "manifest_path",
    "mirror",
    "receive",
    "iter_numbers",
    "load_catalogue",
    "load_source_index",
    "normalise",
    "segment_page_blocks",
    "sources_dir",
    "take",
    "write_source",
    "text_density",
    "write_catalogue",
    "write_source_index",
]
