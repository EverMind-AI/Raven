"""The one store the process shares, so a surface and a tool see the same records.

Opening the database per call would mean a connection per call, and a
connection is a file handle and a lock. Worse, it would mean two handles
writing the same file, which the store's lock exists to prevent. So the
store is built once per process and handed out, the way the knowledge
manager is.

The accessor imports the module rather than the package, because the package
imports this one -- reaching back through it puts both inside an import
cycle, which the cycle budget counts and caps.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from raven.records.store import RecordStore

__all__ = ["records_store", "set_store_for_tests"]


_store: "RecordStore | None" = None


def records_store(db_path: Path | None = None) -> "RecordStore":
    """The shared store, built on first use."""
    global _store
    if _store is None:
        from raven.records.store import RecordStore

        _store = RecordStore(db_path)
    return _store


def set_store_for_tests(store: "RecordStore | None") -> None:
    """Put a store in place, or take the shared one away."""
    global _store
    _store = store
