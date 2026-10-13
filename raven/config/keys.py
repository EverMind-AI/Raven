"""Choose existing config key spellings without letting null aliases hide values."""

from collections.abc import Mapping
from typing import Any

from pydantic.alias_generators import to_camel, to_snake


def key_as_written(node: Mapping[str, Any], name: str) -> str:
    """Prefer a non-null spelling, then a present null spelling, then ``name``.

    Extension loading skips null blocks, so a null alias must not hide the
    block it reads. False, zero and other non-null values still reach the
    caller's validation rather than being replaced as if they were absent.
    """
    spellings = (name, to_camel(name), to_snake(name))
    for spelling in spellings:
        if spelling in node and node[spelling] is not None:
            return spelling
    return next((spelling for spelling in spellings if spelling in node), name)
