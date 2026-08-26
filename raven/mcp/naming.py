"""Model-facing names for MCP tools.

The name a server advertises is not usable as-is: two servers may advertise the
same tool name, and neither is guaranteed to be spelled in what the providers
accept. This module is the single place that turns a ``(server, tool)`` pair
into the string the model sees.

What the providers accept -- the character set and the 64-character cap, and the
sources that were checked for it -- belongs to
:mod:`raven.providers.tool_names`, which is where the constraint comes from.
This module is one of its consumers.
"""

from __future__ import annotations

import hashlib
from collections.abc import Container
from dataclasses import dataclass

from raven.providers.tool_names import MAX_NAME_LENGTH, sanitary_form

PREFIX = "mcp"
SEPARATOR = "_"
SUFFIX_HASH_LENGTH = 12

__all__ = ["MCPToolRef", "PREFIX", "SEPARATOR", "legacy_tool_name", "spellings", "tool_name"]


def _suffix(server: str, tool: str) -> str:
    """A short tag derived from the raw pair, not from where it sits in a list.

    Deriving it from the identity rather than a running counter is what makes
    the *tag* stable: the same pair always yields the same one, whatever order
    the servers happen to connect in, and across restarts.

    Which of two colliding pairs actually receives it is a different question
    and the answer is not stable -- ``tool_name`` appends a suffix only to
    whichever pair arrives second, and ``apply_config`` gathers connects
    concurrently. :func:`spellings` is where that matters and where it is
    spelled out.
    """
    identity = f"{server}\0{tool}".encode()
    return "_" + hashlib.sha1(identity).hexdigest()[:SUFFIX_HASH_LENGTH]


def _overflows(clean_server: str, clean_tool: str) -> bool:
    """Whether this pair's plain form is too long to register as it stands.

    One function because two callers must agree: :func:`tool_name` reads it to
    decide that a suffix is mandatory, and :func:`spellings` reads it to decide
    that the plain form is therefore a spelling this pair never had.
    """
    return len(f"{PREFIX}{SEPARATOR}{clean_server}{SEPARATOR}{clean_tool}") > MAX_NAME_LENGTH


def _assemble(server: str, tool: str, suffix: str = "") -> str:
    """``mcp_<server>_<tool><suffix>``, trimmed to fit the length cap.

    The tool component is trimmed first and the server second, so a name that
    has to lose characters still says which server it came from.
    """
    head = f"{PREFIX}{SEPARATOR}{server}{SEPARATOR}"
    budget = MAX_NAME_LENGTH - len(suffix)
    if len(head) + len(tool) <= budget:
        return head + tool + suffix
    if len(head) < budget:
        return head + tool[: budget - len(head)] + suffix
    return (head + tool)[:budget] + suffix


def tool_name(server: str, tool: str, *, taken: Container[str] = frozenset()) -> str:
    """The model-facing name for one MCP tool.

    ``taken`` is the set of names already registered. A name that clashes with
    one of them, and a name that overflows :data:`MAX_NAME_LENGTH`, both get a
    suffix derived from the raw ``(server, tool)`` pair.
    """
    clean_server, clean_tool = sanitary_form(server), sanitary_form(tool)
    plain = _assemble(clean_server, clean_tool)
    if not _overflows(clean_server, clean_tool) and plain not in taken:
        return plain
    return _assemble(clean_server, clean_tool, _suffix(server, tool))


@dataclass(frozen=True)
class MCPToolRef:
    """Where one registered tool came from.

    The single record behind every question anyone asks about an MCP tool:
    which server owns it, what it is called on that server, and what the model
    sees. Kept as one value rather than three parallel lookups because they are
    one fact -- the previous shape (the manager holding a ``set[str]`` of names,
    the wrapper separately holding its own pair) let two places answer the same
    question differently, and three consumers each grew their own way of
    guessing the parts back out of the name.

    Guessing is not possible, which is the point: sanitising rewrites
    characters, the length cap truncates, and a collision appends a hash, so
    ``name`` is a one-way function of ``(server, tool)``.
    """

    name: str
    """What the model sees, and what the registry is keyed by."""
    server: str
    """The server's name as written in config -- before any sanitising."""
    tool: str
    """The tool's name as the server advertised it -- before any sanitising."""


def spellings(server: str, tool: str) -> frozenset[str]:
    """Every name this pair could ever have been registered under.

    Config outlives code. An entry naming an MCP tool may have been written
    against a spelling this build no longer produces, and the entry cannot be
    parsed back into a pair, so the only exact comparison is to generate the
    pair's spellings forward and test membership.

    At most three exist:

    1. the raw form, from before names were sanitised or capped at all;
    2. the plain sanitised form, which is what a pair gets when nothing
       collides with it -- and which a pair that overflows the cap never gets,
       because :func:`tool_name` makes the suffix mandatory for those. Offering
       it anyway would answer a config entry with a spelling no build ever
       registered, so nobody could have copied it from anywhere;
    3. the suffixed form, which is what a pair gets when something collides,
       or when it overflows.

    All of them come from the pair alone. That matters most for the third:
    which of two colliding tools actually receives the suffix depends on which
    connect commits first -- concurrent, so not even stable across restarts --
    but the *spelling* does not, so an entry naming it keeps meaning the same
    tool after the other server is removed or the race lands the other way.
    """
    clean_server, clean_tool = sanitary_form(server), sanitary_form(tool)
    out = {
        legacy_tool_name(server, tool),
        _assemble(clean_server, clean_tool, _suffix(server, tool)),
    }
    if not _overflows(clean_server, clean_tool):
        out.add(_assemble(clean_server, clean_tool))
    return frozenset(out)


def legacy_tool_name(server: str, tool: str) -> str:
    """The pre-sanitising form, kept so existing config keeps matching.

    Frozen against the formula that shipped: the wrapper built this name from
    the raw pair with no cleaning and no cap, so a ``disabled_tools`` entry
    copied from a live deploy carries the dot or slash as-is. Cleaning it here
    would reproduce a spelling that never existed and silently un-disable the
    tool the entry was written against.

    For a pair that is already clean and short this equals
    :func:`tool_name` -- which is the measure of how narrow the compatibility
    window is. Only names carrying an illegal character or overflowing the cap
    have two spellings at all.
    """
    return f"{PREFIX}{SEPARATOR}{server}{SEPARATOR}{tool}"
