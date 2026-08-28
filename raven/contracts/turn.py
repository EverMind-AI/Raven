"""The turn contract paper: what a submitter hands the spine, and what it
gets back.

Contract tier — frozen for every loop. Skeleton form tonight: the paper
declares the surface while the bodies still live in ``raven.spine`` (moving
them wholesale drags the seven shapes along; that is the S2 full move, not
this skeleton). Consumers may already import the contract from here; the
definitions being re-exports is invisible to them and to the ledger guard.
"""

from raven.spine.scheduler import TurnHandle
from raven.spine.turn import Origin, TurnRequest

__tier__ = "contract"
__all__ = ["TurnRequest", "TurnHandle", "Origin"]
