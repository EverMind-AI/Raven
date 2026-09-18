"""One per-turn ledger file, and the turn is the unit that owns it.

The file is write-only at run time and is deleted once the appendix has read it, so
nothing downstream ever notices a stranded one: it is simply a file that accumulates
on disk forever, under a name no later turn opens and no close can reach. That is
what makes the invariant worth pinning here rather than leaving to the appendix
tests, which read the file they were handed and would pass either way.

The way it gets stranded is the loop re-running a dead turn. The flow hook treats
``ctx.iteration == 1`` as the turn boundary and opens the ledger there, and the rerun
restarts the loop's iteration count, so a turn with a rerun budget reaches that
boundary twice.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.support import ledger as ledger_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _ledger_reset(tmp_path):
    ledger_mod.set_ledger_dir(tmp_path)
    yield
    ledger_mod.close_product_ledger()
    ledger_mod.set_ledger_dir(None)


def _files(tmp_path: Path) -> list[str]:
    return sorted(f.name for f in tmp_path.iterdir())


def test_opening_the_ledger_again_releases_the_file_it_replaces(tmp_path):
    """The rerun's boundary, at the level the strand happens. The first file is
    written, so it exists; the second open replaces the path the close reads, so
    without a release the first one is beyond every close that follows."""
    first = ledger_mod.open_product_ledger("turn-1")
    assert first is not None
    ledger_mod.ledger_append({"op": "search", "query": "who won?"})
    assert _files(tmp_path) == [Path(first).name]

    second = ledger_mod.open_product_ledger("turn-1-retry")
    assert second is not None and second != first

    # The replacement is named, not yet written: a ledger file appears on its first
    # row. What has to be gone is the one the close can no longer reach.
    assert Path(first).name not in _files(tmp_path), "the first attempt's file was stranded"


def test_the_close_after_a_reopen_leaves_nothing_behind(tmp_path):
    """The whole contract, end to end: a turn that ran twice still leaves the disk
    as it found it."""
    ledger_mod.open_product_ledger("turn-1")
    ledger_mod.ledger_append({"op": "search", "query": "who won?"})
    ledger_mod.open_product_ledger("turn-1-retry")
    ledger_mod.ledger_append({"op": "search", "query": "who won?"})

    ledger_mod.close_product_ledger()

    assert _files(tmp_path) == []
    assert ledger_mod.ledger_path() is None


def test_a_reopen_writes_to_the_new_file_only(tmp_path):
    """A stranded file is the disk cost; this is the reading cost. The appendix reads
    the path the ledger is pointing at, so a rerun's rows must not land under the
    name the first attempt opened."""
    ledger_mod.open_product_ledger("turn-1")
    ledger_mod.ledger_append({"op": "search", "query": "who won?"})
    second = ledger_mod.open_product_ledger("turn-1-retry")

    ledger_mod.ledger_append({"op": "search", "query": "who won?"})

    assert ledger_mod.ledger_path() == second
    assert _files(tmp_path) == [Path(second).name]


def test_an_unconfigured_ledger_still_releases_what_it_holds(tmp_path):
    """The directory can be switched off between turns. The release has to happen
    before that is read, or the off switch becomes a leak."""
    first = ledger_mod.open_product_ledger("turn-1")
    assert first is not None
    ledger_mod.ledger_append({"op": "search", "query": "who won?"})

    ledger_mod.set_ledger_dir(None)
    assert ledger_mod.open_product_ledger("turn-1-retry") is None

    assert _files(tmp_path) == []
