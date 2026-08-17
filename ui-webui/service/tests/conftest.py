"""Make ``service/`` importable so these tests can import the agent modules.

Run from the repo root, on the interpreter ``raven`` itself runs on -- the uv
tool env, which is also what serves the gateway and this service:

    "$(dirname "$(readlink -f "$(command -v raven)")")/python" \
        -m pytest ui-webui/service/tests -q

That env carries pytest and pytest-asyncio only when it was installed with the
service's dev requirements, which is how ``ui-webui/CLAUDE.md`` prescribes it:

    uv tool install --force --editable . \
        --with-requirements ui-webui/service/requirements-dev.txt

Without them the run aborts on ``Unknown config option: asyncio_mode`` before
collecting anything -- the repo root sets that option and pytest reads the root
config for this directory too. An env missing the plugin fails there rather than
skipping the async tests, so it reads as a broken suite; see
``requirements-dev.txt``, which says the same thing next to the pins.

``uv run pytest`` does not collect this directory: the root config sets
``testpaths = ["tests"]``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
