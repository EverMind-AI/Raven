"""Runtime identity and readiness for clients using the local terminal gateway."""

import os

from raven.contracts.terminal import RuntimeInfo
from raven.rpc.methods.system import _raven_version


async def runtime_status(params: dict) -> dict:
    return {
        "runtime": {
            **RuntimeInfo().model_dump(by_alias=True),
            "state": "ready",
            "environment": os.environ.get("RAVEN_ENVIRONMENT", "local"),
            "appVersion": _raven_version(),
            "capabilities": ["terminal.binary-stream.v1"],
        },
        "graph": {"state": "ready"},
    }
