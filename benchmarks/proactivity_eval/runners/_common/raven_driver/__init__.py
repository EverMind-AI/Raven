"""proactivity_eval — subprocess-driven proactivity evaluation harness.

Public API:

    from proactivity_eval import RavenDriver, AgentState
    from proactivity_eval import AgentResponse, SentinelTickResult

The package never imports the ``raven`` python module. The agent
under test is driven through its CLI in a subprocess. See
``proactivity_eval/driver.py`` for the design rationale.
"""

from __future__ import annotations

from .driver import AgentResponse, RavenDriver, SentinelTickResult
from .state import AgentState

__version__ = "0.1.0"
__all__ = [
    "AgentResponse",
    "AgentState",
    "RavenDriver",
    "SentinelTickResult",
    "__version__",
]
