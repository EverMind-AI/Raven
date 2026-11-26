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


def object_arguments(decoded: Any, sent: Any) -> dict[str, Any]:
    """``decoded`` when it is an object; otherwise ``sent`` parked as text."""
    if isinstance(decoded, dict):
        return decoded
    return {RAW_ARGUMENTS_KEY: sent if isinstance(sent, str) else json.dumps(sent, ensure_ascii=False)}


__all__ = ["object_arguments"]
