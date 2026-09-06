"""Live-run questions about a graph, asked of a whole agent loop.

Two graph tools exist by design: the one on the model's tool table, and the
playbook engine's private one (a ``mode: dag`` playbook is dispatched by the
engine, not by the model). ``AgentLoop.dag_tools`` is what puts them behind one
accessor, and the consumers that ask a live-run question go through here rather
than each carrying its own copy of the same fallback: the two ``subagents.*``
instance readers, the web RPC's cancel, and ``read_run_reconciled`` on both
surfaces that read a run back. That last one was missed on the first pass -- it
took liveness off the tool it was handed, which is the registered instance, so a
backgrounded playbook run reopened from history came back with every unfinished
node overlaid ``interrupted``. No count is stated here: a number in a docstring
goes stale the moment a fifth caller appears, and this one did.

They arrive as a duck-typed ``loop`` because the RPC layers hold whatever the
factory gave them, including a test double with no such method. Both answers are
advisory: what they decide is whether a row draws as running and whether a stop
button reports anything, so a loop that cannot answer must degrade rather than
raise -- these are invoked lazily, only for a row that still reads running, so
an exception here would surface from a branch most callers never reach.
"""

from typing import Any

__all__ = ["cancel_run", "live_run_ids", "owning_tool", "resolve_node"]


def _registered_tool(loop: Any) -> Any:
    """The graph tool on the model's table, when this object has one.

    The fallback for a ``loop`` that is not an ``AgentLoop`` -- a test double, or
    a host built before the accessor existed. It answers for the registered
    instance only, which is exactly the half that was wrong for a playbook's run,
    so it is a floor rather than the intended path.
    """
    tools = getattr(loop, "tools", None)
    getter = getattr(tools, "get", None)
    return getter("run_subagent_dag") if getter is not None else None


def owning_tool(loop: Any, run_id: str) -> Any:
    """Whichever graph tool instance currently holds ``run_id`` live.

    A replan control step calls several tool methods in sequence
    (``prepare_replan``, ``is_foreground``, ``emit_replanned``,
    ``await_finalized``, ``start_replan``), and each one only answers correctly
    from the instance whose ``_dispatch`` actually created this run's task,
    desk and outbox -- the registered tool has none of that bookkeeping for a
    run the playbook engine's private instance is running. Falling back to
    ``_registered_tool`` when no instance claims the run any more matches every
    other helper here: the run has already ended in both, or ``loop`` is a test
    double with no ``dag_tools``.
    """
    getter = getattr(loop, "dag_tools", None)
    tools = getter() if getter is not None else []
    for tool in tools:
        fn = getattr(tool, "active_run_ids", None)
        try:
            if fn is not None and run_id in fn():
                return tool
        except Exception:  # noqa: BLE001 - liveness is advisory, never fatal
            continue
    return _registered_tool(loop)


def live_run_ids(loop: Any) -> set[str]:
    """Run ids in flight across every graph tool this loop owns."""
    fn = getattr(loop, "active_dag_run_ids", None)
    if fn is None:
        tool = _registered_tool(loop)
        fn = getattr(tool, "active_run_ids", None) if tool is not None else None
        if fn is None:
            return set()
    try:
        return set(fn())
    except Exception:  # noqa: BLE001 - liveness is advisory, never fatal
        return set()


def cancel_run(loop: Any, run_id: str) -> bool:
    """Stop one run, whichever graph tool owns it. False when nothing did."""
    fn = getattr(loop, "cancel_dag_run", None)
    if fn is None:
        tool = _registered_tool(loop)
        fn = getattr(tool, "request_cancel", None) if tool is not None else None
        if fn is None:
            return False
    try:
        return bool(fn(run_id))
    except Exception:  # noqa: BLE001 - a failed stop is reported, not raised
        return False


def resolve_node(loop: Any, run_id: str, node_id: str, decision: str, message: str | None, plan: Any = None) -> bool:
    """Answer one suspended node, whichever graph tool owns its run."""
    fn = getattr(loop, "resolve_dag_node", None)
    if fn is None:
        tool = _registered_tool(loop)
        fn = getattr(tool, "resolve_node", None) if tool is not None else None
        if fn is None:
            return False
    try:
        return bool(fn(run_id, node_id, decision, message, plan))
    except Exception:  # noqa: BLE001 - a failed answer is reported, not raised
        return False
