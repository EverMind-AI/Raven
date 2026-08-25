"""The vendored subagent checkouts must not drift without a deliberate record.

Lives in the suite as well as in pre-commit because the two see different
things. The hook is gated on ``files: '^subagents/'`` and fires on a diff, so it
says nothing about a tree that arrived already drifted; this checks the whole
tree on every run. The GitLab side reinforces it: that pipeline's config is not
in this tree at all -- it is ``.gitlab-ci.yml`` on the orphan ``ci`` branch --
and its ``tests`` job runs pytest without pre-commit.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent


def test_no_vendored_subagent_tree_changed_without_its_hash() -> None:
    result = subprocess.run(
        [sys.executable, "scripts/check_vendored_subagents.py"],
        cwd=REPO,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr or result.stdout
