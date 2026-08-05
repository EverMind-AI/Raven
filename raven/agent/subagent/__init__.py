"""SubagentManager — spawns child AgentLoops for delegated tasks.

Implementation lives in ``manager.py``.

External callers should keep using:

    from raven.agent.subagent import SubagentManager
"""

from raven.agent.subagent.manager import SubagentManager
from raven.agent.subagent.presets import (
    THIRD_PARTY_SUBAGENT_PRESETS,
    third_party_subagent_preset,
    third_party_subagent_presets,
)

__all__ = [
    "SubagentManager",
    "THIRD_PARTY_SUBAGENT_PRESETS",
    "third_party_subagent_preset",
    "third_party_subagent_presets",
]
