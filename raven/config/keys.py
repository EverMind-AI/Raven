"""Choose the config key spelling the loaders read, so a write updates it in place."""

from collections.abc import Mapping
from typing import Any

from pydantic.alias_generators import to_camel, to_snake


def key_as_written(node: Mapping[str, Any], name: str, *, top_level: bool) -> str:
    """The spelling of ``name`` already in ``node`` that the loader reads, else ``name``.

    The config models accept every key as camelCase or snake_case, so a write
    has to land on the spelling being read or it grows a second key beside it.
    Which spelling is read depends on where the key sits:

    - Below the top level pydantic reads every key. It takes the camelCase
      alias whenever it is present, null or not, and the snake_case name only
      when the alias is absent, so the first spelling present is the one read.
      ``name`` is tried first, so a key present exactly as written wins; a
      schema field is passed as its alias, the spelling pydantic tries first.
    - At the top level ``load_raven_config`` drops a null extension block
      before pydantic sees it, so a non-null spelling is the one read there and
      a null one only when nothing else is present. False, zero and other
      non-null values still count, so they reach the caller's validation.
    """
    spellings = (name, to_camel(name), to_snake(name))
    if top_level:
        for spelling in spellings:
            if node.get(spelling) is not None:
                return spelling
    return next((spelling for spelling in spellings if spelling in node), name)
