"""Sentinel — predictor stage.

Reads global state (memory, history, active sessions, learned
routines, recent fires) and produces a richly-typed
``PlannerContext`` for the trigger-policy stage to decide on. Also
hosts the long-term-pattern detectors (RoutineLearner +
RoutineAggregator + RoutineStore) and the daily task-discovery
scanner (TaskDiscoverer + matching prompt).

Re-exported from ``raven.proactive_engine.sentinel.__init__`` so
the canonical import paths (``ContextAssembler``, ``RoutineLearner``
etc) still resolve via the parent package.
"""
