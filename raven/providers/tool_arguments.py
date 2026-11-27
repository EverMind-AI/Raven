"""Tool-call arguments as every reader of a call takes them: a mapping.

A provider exit decodes the arguments a model sent - strictly, then with repair. What
decodes to anything but an object (a string, a list, a number, null) is no set of
arguments, so it is parked as the text that was sent, under ``RAW_ARGUMENTS_KEY``,
where the tool registry quotes it back to the model instead of a reader failing on it.
"""

from __future__ import annotations

import json
from typing import Any

from raven.contracts.tool import RAW_ARGUMENTS_KEY


def object_arguments(decoded: Any, sent: Any) -> tuple[dict[str, Any], bool]:
    """``decoded`` when it is an object; otherwise ``sent`` parked as text.

    The flag says whether it was parked: a parked call is one whose arguments did not
    arrive as a set, which the caller must record as a repair so the registry refuses it
    rather than running the tool on the parked key.
    """
    if isinstance(decoded, dict):
        return decoded, False
    return {RAW_ARGUMENTS_KEY: sent if isinstance(sent, str) else json.dumps(sent, ensure_ascii=False)}, True


__all__ = ["object_arguments"]
