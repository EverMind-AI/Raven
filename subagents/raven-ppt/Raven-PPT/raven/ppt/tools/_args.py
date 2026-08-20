"""Reading a tool's arguments as the model actually sends them.

One failure, twice in one run, is why this exists. `ppt_outline` declares `pages` as
an array of objects; a live model sent the array JSON-encoded *inside a string*:

    {"project": "…", "takeaway": "…", "pages": "[{\\"page\\":1,\\"claim\\":\\"…\\"}]"}

The tool iterated the string, called `.get` on a character, and the author got back
`AttributeError: 'str' object has no attribute 'get'` -- a Python traceback where a
sentence should have been. It sent the same shape again, so two of its calls bought
nothing.

The same double-encoding then arrived with a quote inside it -- a claim that quoted
a phrase -- and `Expecting ',' delimiter: line 1 column 919` was all the author got
back for a twelve-page outline. A hand-written JSON string breaks the same way
wherever it is written, so the repairs `_reply` already made for a model's replies
are the repairs applied here, rather than a second set that drifts from them.

Both halves of that are fixed here and neither is a schema change. A double-encoded
array is unambiguous, so it is decoded rather than refused: nothing else could have
been meant, and refusing costs a round to teach the model something the tool could
simply read. What genuinely is the wrong shape gets a sentence naming the shape
wanted, which is what a refusal is for.
"""

from __future__ import annotations

import json
from typing import Any

from raven.ppt.stages._reply import escapes_repaired, json_defect, quotes_repaired


class ArgumentError(ValueError):
    """An argument that cannot be read as the shape the tool declared."""


def as_list(value: Any, name: str) -> list[Any]:
    """`value` as a list, decoding a JSON-encoded one and wrapping a lone item.

    A bare string that is not JSON is one item, not a list of its characters --
    iterating a string is the bug this module was written for.
    """
    if value is None:
        return []
    if isinstance(value, list):
        return list(value)
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("[", "{")):
            for attempt in (text, escapes_repaired(text), quotes_repaired(text)):
                if attempt is None:
                    continue
                try:
                    decoded = json.loads(attempt)
                except ValueError:
                    continue
                return decoded if isinstance(decoded, list) else [decoded]
            raise ArgumentError(f"{name} looks like JSON and does not parse: {json_defect(text)}")
        return [value]
    if isinstance(value, dict):
        return [value]
    return [value]


def as_objects(value: Any, name: str) -> list[dict[str, Any]]:
    """`value` as a list of objects, or a refusal that says what was wanted."""
    entries = as_list(value, name)
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            raise ArgumentError(
                f"{name}[{index}] is {type(entry).__name__}, and every entry has to be an object -- "
                f"{name} is a list of objects, not a list of strings"
            )
    return entries


def as_ints(value: Any, name: str) -> list[int]:
    """`value` as a list of whole numbers, accepting the strings a model sends."""
    numbers: list[int] = []
    for entry in as_list(value, name):
        try:
            numbers.append(int(str(entry).strip()))
        except (TypeError, ValueError) as exc:
            raise ArgumentError(f"{name} holds {entry!r}, which is not a page number") from exc
    return numbers


def as_strings(value: Any, name: str) -> list[str]:
    """`value` as a list of strings, dropping the empty ones."""
    return [str(entry).strip() for entry in as_list(value, name) if str(entry).strip()]
