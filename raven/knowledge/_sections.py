"""The section-metadata contract shared by the writing and reading sides.

A parser records which section a chunk came from; retrieval reads it back to
widen a hit into the passage around it. The keys live here rather than with
either side because the reading side has to work over chunks no parser of ours
produced -- everything indexed before a structured parser existed, and every
format with no headings to cut on.
"""

from __future__ import annotations

SECTION_TEXT = "section_text"
"""Metadata key: the whole section, carried on the first chunk that came from it.

Defined on the reading side rather than by whichever parser writes it, because
this module has to work over chunks no parser of ours produced -- everything
indexed before a structured parser existed, and every format that has no
headings to cut on. Expansion is a best effort over whatever the payload
happens to carry, so it cannot depend on the writer being installed.

Only present when a section had to be split, and only within
:data:`MAX_SECTION_CHARS`. A reader that wants the section back gets it verbatim
instead of reassembling it from the pieces, which cannot be done safely after
the fact: neighbouring chunks share an overlap window, and recovering where it
starts -- by matching one chunk's tail against the next one's head, or by
searching for a piece in the section text -- lands on the wrong place as soon as
the document repeats itself, which boilerplate and tables routinely do. Both
mistakes drop content silently. One stored copy costs a few KB and cannot be
wrong.
"""

MAX_SECTION_CHARS = 4000
"""Past this, a section stops being context and starts being the document, so
it is not stored for inlining and a hit keeps the chunk that matched."""

SECTION_ORDINAL = "section_ordinal"
"""Metadata key: which section of its document a chunk came from, 0-based.

The section identity. A heading path is not one -- two same-named children of one
parent share it, and a reader keying on the path alone hands a hit in the second
section the text of the first. The boundary is only knowable while the document is
being cut, so the parser records it.

Absent on every chunk indexed before the structured parser, and on documents with
no headings, where there is one section and nothing to disambiguate. Defined here
with :data:`SECTION_TEXT` for the same reason: this side has to read chunks no
parser of ours produced, so it cannot depend on the writer being installed.
"""
