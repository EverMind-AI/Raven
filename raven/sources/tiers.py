"""What the tiers mean, and which source sits in which one.

A tier is not a quality score. It is a statement about what a source's terms
allow the deployment to do with the data it hands over, and it is the reason
the registry can refuse a call before it is made rather than argue about it
afterwards.

The four tiers are deliberately coarse. A source either may be used as it
stands, may be used with its attribution carried alongside, may be used under
a licence the operator has, or may not be used at all until the operator says
why. The finer question -- what a source permits for *this* use rather than
in general -- is not a tier but a condition on the source, and lives in
:mod:`raven.sources.policy`.
"""

from __future__ import annotations

from enum import Enum

__all__ = ["SourceTier", "TIER_MEANING"]


class SourceTier(str, Enum):
    """What a source's terms allow a deployment to do with it."""

    #: Free to read and to build on, with no licence the operator has to hold.
    PUBLIC = "public"

    #: Usable, but the source's terms ask for the attribution to travel with it.
    ATTRIBUTED = "attributed"

    #: Usable under a licence the operator holds, or has to get one for.
    LICENSED = "licensed"

    #: Not usable until the operator records why this deployment may use it.
    RESTRICTED = "restricted"


TIER_MEANING: dict[SourceTier, str] = {
    SourceTier.PUBLIC: (
        "The data is free to read and to build on. Nothing has to be carried "
        "with it and no licence has to be held."
    ),
    SourceTier.ATTRIBUTED: (
        "The data may be used, and the source's terms ask for its name to "
        "travel with whatever comes out. The attribution is part of the "
        "artefact, not a note beside it."
    ),
    SourceTier.LICENSED: (
        "The data may be used under a licence. Either the operator already "
        "holds one, or the source hands it out under terms that permit the "
        "use -- which is a question about the deployment and not about the "
        "source."
    ),
    SourceTier.RESTRICTED: (
        "The source's terms do not permit the use as it stands. It may still "
        "be used, but only by an operator who has recorded why, and the "
        "record is what a reader later checks."
    ),
}
