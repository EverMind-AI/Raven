"""The kernel version a harness node is stamped with.

``evolver/tree/store.py`` writes ``core_version=HarnessNode.current_core_version()``
onto every node it saves, so this value is the only record of which raven a node
was grown under. It read ``raven.__core_version__``, a module that exists neither
in this tree nor in the wheel ``scripts/build_core_wheel.py`` produces, and the
read sat inside ``try/except ImportError`` -- so the except branch was the only
branch and every node recorded "unknown".
"""

from __future__ import annotations

import raven
from evolver.tree.node import HarnessNode


def test_a_node_is_stamped_with_the_installed_raven_version() -> None:
    assert HarnessNode.current_core_version() == raven.__version__


def test_the_unknown_fallback_is_not_the_normal_answer() -> None:
    """The docstring said the fallback "shouldn't happen in normal operation";
    this is that sentence as an assertion."""
    assert HarnessNode.current_core_version() != "unknown"
