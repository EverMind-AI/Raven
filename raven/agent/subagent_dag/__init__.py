"""Sub-agent DAG orchestration — a decoupled, optional subsystem (req4).

Ported from the RavenX AgentScope layer, but kept independent of Raven's kernel:
it carries its own graph model, placeholder rendering, ready-set scheduler, and
on-disk run store, and is meant to be exposed to the main agent as an ordinary
Raven tool (``run_subagent_dag``) — NOT fused into ``raven/agent/subagent/`` and
NOT routed through ``Origin.SUBAGENT`` / the spine scheduler.

This package's core (graph / placeholders / paths / render / store / projection)
is provider-agnostic pure Python + Pydantic over a duck-typed file/exec backend;
no ``agentscope`` import appears here. The runner, node executors, the Raven tool
wrapper, and the backend adapter land on top of this core.
"""

from raven.agent.subagent_dag._capabilities import (
    AgentCapabilities,
    validate_capabilities,
)
from raven.agent.subagent_dag._errors import DagValidationError
from raven.agent.subagent_dag._graph import (
    DagNodeSpec,
    SubAgentDagSpec,
    parse_dag_spec,
    validate_and_order,
)
from raven.agent.subagent_dag._paths import RUNS_PREFIX, check_confined, split_reference
from raven.agent.subagent_dag._placeholders import (
    Placeholder,
    iter_placeholders,
    parse_placeholders,
)
from raven.agent.subagent_dag._projection import fold_dag_run_entry
from raven.agent.subagent_dag._reader import DagReadError, read_node, read_run
from raven.agent.subagent_dag._render import render_prompt
from raven.agent.subagent_dag._store import DagRunStore, SessionNodes, make_run_id, read_session_nodes

__all__ = [
    "AgentCapabilities",
    "validate_capabilities",
    "DagValidationError",
    "DagReadError",
    "read_run",
    "read_node",
    "DagNodeSpec",
    "SubAgentDagSpec",
    "parse_dag_spec",
    "validate_and_order",
    "RUNS_PREFIX",
    "check_confined",
    "split_reference",
    "Placeholder",
    "iter_placeholders",
    "parse_placeholders",
    "fold_dag_run_entry",
    "render_prompt",
    "DagRunStore",
    "SessionNodes",
    "read_session_nodes",
    "make_run_id",
]
