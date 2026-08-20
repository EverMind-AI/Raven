"""Sentinel — executor stage.

Carries a Planner decision out to the world: the four delivery paths
(NudgeDispatcher / NudgeInjector / DeferManager / ProactiveSpawn) plus
the SentinelRunner tick loop that orchestrates them and the menu-
acknowledgement plumbing (DecisionRouter + DecisionConsumer +
PendingDecisionStore + ActionExecutor) that closes the loop on
TaskDiscoverer-emitted menus.

Re-exported from ``raven.proactive_engine.sentinel.__init__`` so
the canonical import paths still resolve via the parent package.
"""
