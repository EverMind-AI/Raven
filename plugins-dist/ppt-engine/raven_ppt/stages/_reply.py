"""Reading a JSON reply that a model wrote by hand.

None of this is defensive programming in the abstract. Sixteen percent of one
model's replies failed to parse here, and a third of them failed for one specific
reason: a block of python carries `\\d` and `\\(` long before its author remembers
it is shipping them inside a JSON string. Each failure cost a page its whole edit,
and the error said only that parsing had failed.
"""

from __future__ import annotations

import json
import re

# A backslash that JSON does not allow, and -- first alternative -- a legal
# doubled backslash, matched only so it is stepped over. Without that branch the
# scan starts inside a legal `\\` pair: in `"b\\c"` the first backslash is fine,
# the second then looks like a fresh escape before `c`, and doubling it produces
# `\\\c`, which is more broken than what arrived. Doubling only genuinely illegal
# escapes is lossless -- a legal escape means what it says, and an illegal one can
# only have been meant literally.
_BAD_ESCAPE = re.compile(r'\\\\|\\(?![\\"/bfnrtu]|u[0-9a-fA-F]{4})')


def dedent_fence(text: str) -> str:
    """Strip a code fence the reply may have wrapped a code block in."""
    stripped = text.strip("\n")
    if stripped.startswith("```"):
        stripped = stripped.split("\n", 1)[-1]
        if stripped.rstrip().endswith("```"):
            stripped = stripped.rstrip()[: -len("```")].rstrip("\n")
    return stripped + "\n"


def loads_maybe_fenced(text: str) -> dict | None:
    """Parse a JSON object that may have arrived wrapped in a code fence."""
    candidate = _unfenced(text)
    for attempt in (candidate, escapes_repaired(candidate), quotes_repaired(candidate)):
        if attempt is None:
            continue
        try:
            parsed = json.loads(attempt)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def json_defect(text: str) -> str:
    """Where and how a reply failed to parse, for the error that reports it.

    The decoder already knows the position and the reason; saying them beats
    leaving the defect to be guessed at from a two-hundred-character tail.
    """
    candidate = _unfenced(text)
    try:
        json.loads(candidate)
    except json.JSONDecodeError as exc:
        near = candidate[max(0, exc.pos - 60) : exc.pos + 30].replace("\n", " ")
        return f"{exc.msg} at character {exc.pos} of {len(candidate)}, near `{near}`"
    except (TypeError, ValueError) as exc:
        return str(exc)
    return "the JSON parsed but was not an object"


def escapes_repaired(candidate: str) -> str | None:
    """The same text with JSON-illegal backslash escapes doubled, or None."""
    # Both cases emit exactly two backslashes: a legal pair passes through
    # unchanged, and a lone illegal backslash is doubled. A function rather than
    # a replacement template, so the replacement text is taken literally.
    repaired = _BAD_ESCAPE.sub(lambda _: "\\\\", candidate)
    return repaired if repaired != candidate else None


def quotes_repaired(candidate: str) -> str | None:
    """The same text with stray quotes inside strings escaped, or None if none were.

    The failure this answers, from a live run: an intake reply wrote a question about
    a product by name -- `"question": "这里的 "Evermind" 指的是哪款产品？"` -- and the
    quotes around the name closed the string three characters in. Fifty seconds of
    intake was thrown away for it.

    A quote inside a string is decided by what follows it: a real closing quote is
    followed by whitespace and then one of `,:}]` or the end of the text, and anything
    else means the model meant the character. Structural quotes therefore pass through
    untouched, and the repair only ever adds a backslash.
    """
    out: list[str] = []
    in_string = False
    changed = False
    index = 0
    while index < len(candidate):
        char = candidate[index]
        if not in_string:
            out.append(char)
            if char == '"':
                in_string = True
            index += 1
            continue
        if char == "\\":
            out.append(candidate[index : index + 2])
            index += 2
            continue
        if char == '"':
            if _closes_string(candidate, index):
                in_string = False
                out.append(char)
            else:
                out.append('\\"')
                changed = True
            index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out) if changed else None


def _closes_string(candidate: str, index: int) -> bool:
    rest = candidate[index + 1 :].lstrip()
    return not rest or rest[0] in ",:}]"


def _unfenced(text: str) -> str:
    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.split("\n", 1)[-1].rsplit("```", 1)[0]
    return candidate
