"""Whether this process is serving as someone else's sub-agent, and what it gives up.

``RavenLoopBackend`` narrows an in-process sub-agent by construction: it registers
into an empty ``ToolRegistry`` and never adds the orchestration tools, so a
sub-agent cannot delegate the task it was handed. A ``kind: acp`` agent whose
command is ``raven acp`` gets none of that narrowing -- the child is a separate
raven process that builds its own full registry from its own config and has no way
to tell a DAG node from a user. Measured on a graph whose first node ran the
built-in ``Raven`` agent over acp: the child answered by handing the node's whole
task to a background ``spawn``, whose receipt returns before any work is done, and
ended the turn on the receipt. Twice, with a continuation in between.

The signal is an environment variable rather than a flag on the command line
because ``command`` is user-authored config: a flag would need every existing
install edited by hand and would fail silently wherever one was missed. The host
already injects ``RAVEN_HOME`` into every child it launches, so this rides a
channel that exists.

Third of three mechanisms for one rule, and the division between them is scope
rather than preference. ``RavenLoopBackend`` narrows the registry it builds, which
reaches only what it builds. ``IN_SUBAGENT_RUN``
(:mod:`raven.agent.subagent.backends.base`) is a ``ContextVar`` that
``run_subagent_dag`` refuses on, which reaches any code running inside that
backend's task -- and a ``ContextVar`` does not cross a process boundary, so it
cannot reach a child at all. This one is what the child reads about itself, and
it is the only one of the three that survives the fork/exec.
"""

from __future__ import annotations

import os

#: Set by the host on a child it launches to act as a sub-agent; read only for
#: truth, so a config ``env`` entry of "0" is the way back to a full registry
#: (that map is merged after the host's, and so wins).
SUBAGENT_ENV_VAR = "RAVEN_SUBAGENT"

#: Every tool that dispatches work to another agent, or steers a dispatch already
#: running. Withheld from a sub-agent because a node's whole point is that *this*
#: agent does the work; a sub-agent that delegates returns a receipt, and the
#: caller cannot tell that from an answer.
#:
#: ``load_playbook`` is here for a reason its name hides: a ``dag`` playbook
#: dispatches from inside the call, so it is a third route to a graph next to
#: ``spawn`` and ``run_subagent_dag``. The three ``*_dag`` controls only steer a
#: run this process started, and already report "no run_subagent_dag tool is
#: registered" when asked without one -- they are dropped so the model is not
#: offered three tools that can only answer that.
#:
#: ``message`` is deliberately absent. The in-process backend withholds it, but an
#: acp child reaches its caller over the protocol rather than through that tool.
#:
#: Read by the tests rather than by the gates. Neither gate can filter a name list:
#: both work by not building the thing at all -- one skips a block of
#: registrations, the other leaves the playbook funnel unbuilt so its two tools
#: decline their binding -- and a filter applied after construction would pay for
#: the tools it then discards. So this is the specification, and
#: ``tests/test_agent_loop_subagent_role.py`` is what holds the two gates to it,
#: in both directions: a name here that nothing registers fails as loudly as a
#: name the gates let through.
WITHHELD_FROM_SUBAGENT = frozenset(
    {
        "spawn",
        "run_subagent_dag",
        "load_playbook",
        "create_playbook",
        "cancel_dag",
        "dag_status",
        "resolve_dag_node",
    }
)


def is_subagent_process() -> bool:
    """Whether the host launched this process to serve as a sub-agent."""
    return os.environ.get(SUBAGENT_ENV_VAR, "").strip().lower() in ("1", "true", "yes", "on")


def subagent_role_env() -> dict[str, str]:
    """What the host overlays on a child it launches to answer as a sub-agent.

    Written next to the reader above rather than beside the other environment
    builders: the two are one contract, and a variable named in two files is one
    rename away from a child that is told nothing and silently keeps the full
    registry -- the failure this exists to close, and one no error reports.
    """
    return {SUBAGENT_ENV_VAR: "1"}


__all__ = ["SUBAGENT_ENV_VAR", "WITHHELD_FROM_SUBAGENT", "is_subagent_process", "subagent_role_env"]
