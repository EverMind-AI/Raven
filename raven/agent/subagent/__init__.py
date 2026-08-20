"""SubagentManager — spawns child AgentLoops for delegated tasks.

Implementation lives in ``manager.py``.

External callers should keep using:

    from raven.agent.subagent import SubagentManager
"""

from raven.agent.subagent.manager import SubagentManager

__all__ = ["SubagentManager"]
