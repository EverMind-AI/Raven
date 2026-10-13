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

from typing import TYPE_CHECKING

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


__all__ = ["knowledge_manager", "set_manager_for_tests"]
