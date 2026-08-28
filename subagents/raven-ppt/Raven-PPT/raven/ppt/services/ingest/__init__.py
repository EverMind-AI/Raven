"""Material ingestion: source documents -> text + assets + a catalogue of both.

The one entry point is :func:`ingest_materials`. Everything else exported here
exists because another part of the system has to agree with this one:

* a figure-cropping tool needs the same pixel operations the ingest used;
* whatever reads the catalogue back needs the loader that wrote it.

Nothing here calls a model, and nothing here knows which route a deck is being
built by.
"""

from raven.ppt.services.ingest.assets import load_catalogue, write_catalogue
from raven.ppt.services.ingest.documents import TEXT_LAYER_CHARS_PER_PAGE, text_density
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
    READ_FILE,
    ingest_materials,
)
from raven.ppt.services.ingest.sections import MAX_PARTS, Section, index, sections, stated_chars
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
    "stated_chars",
    "CATALOGUE_FILE",
    "DETAIL_INK_MAX",
    "FIGURES_DIR",
    "FIGURE_SUFFIXES",
    "ATTACHED",
    "MANIFEST_FILE",
    "MATERIALS_FILE",
    "READ_FILE",
    "SOURCES_DIR",
    "Source",
    "TEXT_LAYER_CHARS_PER_PAGE",
    "autocrop_border",
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
    "load_catalogue",
    "segment_page_blocks",
    "sources_dir",
    "take",
    "write_source",
    "text_density",
    "write_catalogue",
]
