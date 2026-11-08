"""The process's one knowledge manager.

Here rather than beside either of its callers, because both of them need the
same one and neither owns it. The gateway's handlers reach it to answer the
page; the agent's `knowledge_search` tool reaches it to answer a turn. Built
in two places it was two managers, and that is not a tidiness point:

* a `RecordStore` reads `records.json` once, when it is constructed, and never
  again -- so the second manager holds a snapshot of the moment it was first
  used, and a base the page created after that does not exist for the tool.
  The reader attaches it, asks, and is told nothing in the attached material
  answers;
* constructing one runs `_requeue_interrupted`, which rewrites any document
  that is mid-index back to `pending` on disk. Built while the first manager
  was indexing, the second undid it.

Lazily rather than at import time: constructing it resolves and creates a
directory, and a deployment whose page never opens the knowledge tab and whose
agent never searches one should not grow one.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from raven.knowledge._manager import KnowledgeManager

_manager: "KnowledgeManager | None" = None


def knowledge_manager() -> "KnowledgeManager":
    """The manager every caller in this process shares."""
    global _manager
    if _manager is None:
        # From the module rather than the package: the package imports this
        # one, so reaching back through it -- even lazily -- puts both inside
        # an import cycle, which `test_import_cycle_budget` counts and caps.
        from raven.config.paths import get_runtime_subdir
        from raven.knowledge._manager import KnowledgeManager

        _manager = KnowledgeManager(get_runtime_subdir("knowledge"))
    return _manager


def set_manager_for_tests(manager: "KnowledgeManager | None") -> None:
    """Point every caller at a manager on a tmp_path, and back again."""
    global _manager
    _manager = manager


#: How long after boot the queue is drained. Long enough that a gateway coming
#: up answers its first page and its first turn before it starts embedding, and
#: short enough that a reader who restarted to unstick a document sees it move
#: while they are still looking at it.
DRAIN_PENDING_AFTER_S = 20.0


def drain_pending_in_background(*, delay_s: float = DRAIN_PENDING_AFTER_S) -> "Any":
    """Finish indexing whatever is queued, once the gateway has settled.

    A document reaches the queue two ways and neither of them had anything to
    take it out again. `RecordStore._requeue_interrupted` puts back what a
    restart interrupted, and `KnowledgeManager.index_document` puts back what a
    cancelled RPC interrupted -- both so the document can be retried, and both
    writing into a queue whose only other caller is a model switch on one base.
    The row read `Queued` and stayed there, which is the same complaint the
    requeue was written to answer, one status further along.

    Never raises, and answers None rather than failing: this is a boot path,
    and a queue that cannot be drained is smaller than a gateway that does not
    start. Sequential, because `index_pending` is -- each document is an
    embedding round trip, and a boot that fired a base's worth at once would
    be the same endpoint asked to do everything at the moment it is least able
    to.
    """
    import asyncio

    async def drain() -> None:
        try:
            await asyncio.sleep(delay_s)
            tried = await knowledge_manager().index_pending()
            if tried:
                from loguru import logger

                logger.info("knowledge: indexed {} document(s) left in the queue", tried)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a boot path answers None
            from loguru import logger

            logger.warning("knowledge: the queue could not be drained ({})", exc)

    try:
        return asyncio.get_running_loop().create_task(drain())
    except RuntimeError:
        # No loop: a CLI process, or a test importing this. Nothing to drain
        # into, and nothing to report.
        return None


__all__ = [
    "DRAIN_PENDING_AFTER_S",
    "drain_pending_in_background",
    "knowledge_manager",
    "set_manager_for_tests",
]
