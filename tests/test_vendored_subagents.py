"""The vendored subagent checkouts must not drift without a deliberate record.

Lives in the suite rather than only in pre-commit because the GitLab pipeline
runs pytest and not pre-commit, and a sync that lands on these trees reaches main
through that pipeline.
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
