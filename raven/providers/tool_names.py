"""What a tool name may be, and what to do with one that is not.

The constraint is the providers', which is why it lives here rather than beside
any one of the subsystems that has to satisfy it. Verified against three
independent sources, because getting the character set wrong is silent rather
than loud:

- Anthropic Messages API: ``name`` must match ``^[a-zA-Z0-9_-]{1,64}$``.
- OpenAI: "a-z, A-Z, 0-9, or contain underscores and dashes, maximum length 64".
- litellm, the path raven actually takes, rewrites only names that are already
  illegal and truncates at 128 -- so a 100-char legal name passes litellm and is
  rejected by Anthropic. The 64 cap has to be enforced by us.

Two directions, and they must not share a function. Outbound -- assembling a
name to send, which is :mod:`raven.mcp.naming` -- may substitute an illegal
character, because it is constructing a name and the result is what it then
registers. Inbound -- reading a name a model sent back -- may not: substituting
there invents a name nobody declared. ``sanitary_form(" read_file")`` is
``"_read_file"`` and ``sanitary_form("read file")`` is ``"read_file"``, and
either one used on a received name answers "that tool does not exist" with a
guess.
"""

from __future__ import annotations

import re
from typing import Any

from loguru import logger

#: The cap every provider in the docstring above enforces.
MAX_NAME_LENGTH = 64

_ILLEGAL = re.compile(r"[^A-Za-z0-9_-]")


def _clean(value: str) -> str:
    """Replace what no provider accepts, and nothing else.

    The dash stays: every provider takes it, so a server named ``bcp-search``
    keeps the name it already registers today and no config entry written
    against it goes stale.
    """
    return _ILLEGAL.sub("_", value)


def sanitary_form(value: str) -> str:
    """What ``value`` becomes on the way into a tool name.

    The public face of :func:`_clean`, for the one caller that has to *show* the
    rewrite rather than perform it: a warning that names only the original tells
    the reader a name is wrong without telling them what to look for instead.

    Outbound only -- see this module's docstring for why a received name must
    never be put through it.
    """
    return _clean(value)


def is_sanitary(value: str) -> bool:
    """Whether this string survives :func:`_clean` unchanged.

    Exposed so a caller can warn about a name *before* it silently becomes a
    different one, which is the only point at which anyone can act on it.
    """
    return not _ILLEGAL.search(value)


def normalized_tool_name(raw: Any) -> Any:
    """The registry key for a tool name an upstream sent.

    Surrounding whitespace comes off and nothing else does. Whitespace is
    outside the character set above, so no name this repo ever sent can carry
    any -- which makes removing it a fact about the name rather than a guess
    about intent. Interior spaces and case differences stay, and still miss:
    ``read_file`` and ``Read_File`` can both be registered at once, so folding
    them would answer "that tool does not exist" with a guess, and that answer
    is only worth something while it is certain.

    Asked at the parse exits rather than at the registry lookup because the name
    outlives the lookup. It is written back into the history by
    ``ToolCallRequest.to_openai_tool_call``, where a bad one is replayed to the
    model every turn afterwards, and it keys the failure streak, the tool events
    and the logs -- each of which would otherwise need an allowance of its own,
    and one of them would be missed.

    A name that is not a string is handed back untouched. That is a different
    fault with a different owner, and raising would take down the parse of a
    response that may be otherwise fine.
    """
    if not isinstance(raw, str):
        return raw
    name = raw.strip()
    if name != raw:
        # Said out loud rather than cleaned up quietly: an upstream that keeps
        # sending these has a defect, and a silent repair is how it stays
        # invisible to us for as long as it keeps working.
        logger.warning("upstream sent a tool name with surrounding whitespace: {!r} -> {!r}", raw, name)
    return name


__all__ = ["MAX_NAME_LENGTH", "is_sanitary", "normalized_tool_name", "sanitary_form"]
