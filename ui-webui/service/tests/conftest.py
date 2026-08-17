"""Make ``service/`` importable so these tests can import the agent modules.

Run from the repo root with the ``ravenx`` env (the one that has agentscope):

    conda run -n ravenx python -m pytest ui-webui/service/tests -q

The repo-root pytest config sets ``testpaths = ["tests"]``, so this directory is
not collected by ``uv run pytest`` — its imports need agentscope, which the uv
env does not carry.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
