"""Close native managed-delegation resolution inside an experiment-owned leaf runtime."""

from raven.agent.subagent.registry import AgentRegistry
from raven.agent.subagent.role import WITHHELD_FROM_SUBAGENT, is_subagent_process


class LeafRegistry(AgentRegistry):
    """No backend can be resolved or added, including by direct chat or a separate DAG tool."""

    def backend(self, name, *, build=None):
        raise RuntimeError("delegation is disabled by this host")

    def apply(self, configs):
        if configs:
            raise ValueError("delegation is disabled by this host")


def bind_leaf(runtime):
    if not is_subagent_process():
        raise ValueError("a leaf Harness must be assembled in an isolated native subagent process")
    if any(runtime.loop.tools.get(name) is not None for name in WITHHELD_FROM_SUBAGENT):
        raise ValueError("a leaf Harness cannot register orchestration tools")
    runtime.loop.subagents.registry = LeafRegistry()
