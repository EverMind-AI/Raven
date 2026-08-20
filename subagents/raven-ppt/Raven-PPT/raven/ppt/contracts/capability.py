"""What a route lets the model write.

The hard rule -- the model never writes a physical quantity -- used to live in
prose: a line in the development guide, a sentence in each prompt, and a habit
during review. Prose cannot be checked, and a schema field is added by whoever
is in a hurry. Here it is a declaration that the profile registry carries and
the test suite reads, so a route that grows a font-size field fails a test
rather than shipping.

`normalized_regions` is the one deliberate opening. A 0..1000 grid region or a
point on a 160x90 vector canvas describes *composition intent*, not finished
geometry: the engine still owns the mapping to EMU, the safe-area inset, the
type scale, text fitting and the overlap check. Font size and typeface are not
part of the opening and never become one -- those are the two knobs whose
misuse the engine cannot detect after the fact, because a page set at 9pt is
perfectly well-formed.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Capabilities:
    """The emissions a profile permits. Absent means forbidden."""

    raw_script: bool = False
    normalized_regions: bool = False
    background_prompt: bool = False
    # Held at False in every profile. Present so the prohibition is a field the
    # suite can assert on rather than an absence nobody notices filling in.
    physical_geometry: bool = False
    font_size: bool = False
