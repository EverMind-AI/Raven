"""Status-bar patterns the e2e tests read turn state from.

Mirrored from ``ui-tui/src/content/verbs.ts``; kept in one place so a verb-pool
edit there has a single place to land rather than one copy per test file.
"""

from __future__ import annotations

import re

# Idle turn state (word-bounded so "readiness" won't match).
READY_RE = re.compile(r"\bready\b", re.IGNORECASE)

# Working-state verbs shown while a turn is in flight, mirroring VERBS.
WORKING_RE = re.compile(
    r"\b(pondering|contemplating|musing|cogitating|ruminating|deliberating|"
    r"mulling|reflecting|processing|reasoning|analyzing|computing|"
    r"synthesizing|formulating|brainstorming)…",
    re.IGNORECASE,
)
