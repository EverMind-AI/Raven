"""Check that a Mirasim host bundle still speaks every frame the Mirasim adapter uses.

Usage: python scripts/check_mirasim_frames.py [--against OLD_SERVER_CJS] [SERVER_CJS]

The adapter in ``raven/acp/mirasim/`` drives a protocol Mirasim does not document and
changes between releases, from a bundle that is minified: identifiers are renamed on every
build, while frame and field names stay literal. So a bundle is checked by those literals --
read as text, never run:

- each frame the adapter sends is a ``case '<name>':`` label of the host's dispatcher;
- each frame it reads is a ``'type': '<name>'`` literal;
- each field it sends or reads is a quoted name somewhere in the bundle.

On its own that proves only that each name is still somewhere: most of them occur many times
-- ``stop`` is a case label of six unrelated switches -- so one renamed in the dispatcher the
adapter talks to, and kept elsewhere, passes. ``--against`` an older bundle that worked closes
most of that gap: it reports every name of the adapter's that now occurs fewer times than it
did, the shape such a rename leaves, beside the frame names that appeared or vanished (type
names and other literals that are not frame names are only counted).

With no path it checks the host this machine runs (``~/.mirasim-remote/current/server.cjs``,
present where Mirasim reaches the machine over SSH; pass the desktop app's ``server.cjs``
otherwise). Exits 1 when anything the adapter uses is missing or occurs fewer times than in the
older bundle, naming it, and 2 when a bundle cannot be read.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

SENT = (
    "setStreamCadence",
    "listClis",
    "getCatalog",
    "getCommands",
    "listSessions",
    "getHistory",
    "subscribe",
    "prompt",
    "stop",
    "recall",
    "steer",
    "interact",
    "deleteSession",
)
"""The frames ``raven/acp/mirasim`` sends; a test holds this list to the adapter's own source."""

RECEIVED = (
    "accepted",
    "error",
    "snapshot",
    "session",
    "outbox",
    "catalog",
    "commands",
    "sessions",
    "history",
    "clis",
    "sessionDeleteResult",
)
"""The frames it reads."""

TOLERATED = ("goalError",)
"""Frames it reads when they come but does not need: 0.0.465's host never sends ``goalError``."""

FIELDS = (
    # what the frames it sends carry
    "clientRef",
    "reqId",
    "sessionKey",
    "taskId",
    "ref",
    "agent",
    "workdir",
    "prompt",
    "model",
    "effort",
    "route",
    "attachments",
    "paths",
    "limit",
    "before",
    "scope",
    "refresh",
    "promptId",
    "action",
    "value",
    "coalesceMs",
    "statusMs",
    # what it reads from the answers and the queue
    "ok",
    "error",
    "message",
    "sessions",
    "turns",
    "older",
    "anchor",
    "clis",
    "installed",
    "items",
    "dispatched",
    "steered",
    "noRun",
    "routeByModel",
    "effortByModel",
    "defaultModel",
    "defaultEffort",
    "contextWindow",
    # what it reads from a session's snapshot and its patches
    "snapshot",
    "seq",
    "fromSeq",
    "patch",
    "full",
    "set",
    "lists",
    "appendText",
    "appendReasoning",
    "phase",
    "text",
    "reasoning",
    "toolCalls",
    "subAgents",
    "plan",
    "steers",
    "interactions",
    "artifacts",
    "usage",
    "contextUsedTokens",
    "contextWindowTokens",
    "contextPercent",
    "incomplete",
    "segments",
)
"""The fields it sends or reads; a test holds the ones it sends to the adapter's own source."""

DEFAULT_BUNDLE = Path.home() / ".mirasim-remote" / "current" / "server.cjs"

_LITERAL = r"'(?:[^'\\\n]|\\.)*'"
_CASE = re.compile(r"case\s*(" + _LITERAL + r"(?:\s*\+\s*" + _LITERAL + r")*)\s*:")
_TYPE = re.compile(r"'type'\s*:\s*(" + _LITERAL + r"(?:\s*\+\s*" + _LITERAL + r")*)")
_QUOTED = re.compile(_LITERAL)
_FRAME_NAME = re.compile(r"[a-z][A-Za-z0-9_]*")


def _joined(expression: str) -> str:
    """A literal the minifier split into ``'a'+'b'`` pieces, put back together."""
    return "".join(piece[1:-1] for piece in _QUOTED.findall(expression))


def vocabulary(text: str) -> tuple[set[str], set[str]]:
    """The bundle's ``case`` labels and its ``'type'`` literals.

    Each match is anchored on its keyword, so a quote elsewhere in the bundle -- inside a
    regular expression literal, say -- cannot pair with one of these and swallow it.
    """
    return {_joined(match) for match in _CASE.findall(text)}, {_joined(match) for match in _TYPE.findall(text)}


def _quoting(name: str) -> re.Pattern[str]:
    """``name`` as a quoted literal, whole or split into ``'a'+'b'`` pieces."""
    return re.compile("'" + r"(?:'\s*\+\s*')?".join(map(re.escape, name)) + "'")


def quoted(text: str, name: str) -> bool:
    """Whether ``name`` appears in ``text`` as a quoted literal, whole or split into ``'a'+'b'`` pieces."""
    return _quoting(name).search(text) is not None


def occurrences(text: str) -> dict[str, int]:
    """How many times each name the adapter uses occurs in ``text``, in the role it is checked in."""
    labels = Counter(_joined(match) for match in _CASE.findall(text))
    literals = Counter(_joined(match) for match in _TYPE.findall(text))
    counted = {f"case '{name}'": labels[name] for name in SENT}
    counted |= {f"type '{name}'": literals[name] for name in RECEIVED}
    counted |= {f"field '{name}'": len(_quoting(name).findall(text)) for name in FIELDS}
    return counted


def fewer(old: str, new: str) -> dict[str, tuple[int, int]]:
    """The names the adapter uses that occur fewer times in ``new`` than in ``old``: a rename in one place shows so."""
    before, after = occurrences(old), occurrences(new)
    return {name: (before[name], after[name]) for name in before if after[name] < before[name]}


def missing(text: str) -> dict[str, list[str]]:
    """What the adapter uses that ``text`` lacks, by kind; empty when nothing is."""
    cases, types = vocabulary(text)
    gaps = {
        "sent frames (no case label)": [name for name in SENT if name not in cases],
        "read frames (no type literal)": [name for name in RECEIVED if name not in types],
        "fields": [name for name in FIELDS if not quoted(text, name)],
    }
    return {kind: names for kind, names in gaps.items() if names}


def changes(old: str, new: str) -> dict[str, list[str]]:
    """The case labels and type literals that appeared in ``new`` or vanished from ``old``.

    Only names shaped like the host's frame names are listed; type names (``ChatMediaKind``) and
    expressions a schema puts under ``'type'`` are counted under ``other``.
    """
    old_cases, old_types = vocabulary(old)
    new_cases, new_types = vocabulary(new)
    found: dict[str, list[str]] = {}
    others = 0
    for kind, names in (
        ("case labels added", new_cases - old_cases),
        ("case labels removed", old_cases - new_cases),
        ("type literals added", new_types - old_types),
        ("type literals removed", old_types - new_types),
    ):
        found[kind] = sorted(name for name in names if _FRAME_NAME.fullmatch(name))
        others += len(names) - len(found[kind])
    found["other literals changed"] = [str(others)] if others else []
    return found


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("bundle", nargs="?", type=Path, default=DEFAULT_BUNDLE, help="the host's server.cjs")
    parser.add_argument("--against", type=Path, help="an older server.cjs to list the changes since")
    args = parser.parse_args(argv)
    try:
        text = args.bundle.read_text(encoding="utf-8", errors="replace")
        older = args.against.read_text(encoding="utf-8", errors="replace") if args.against else None
    except OSError as exc:
        hint = (
            "; no host runs here over SSH, so pass the host's server.cjs (the desktop app ships one)"
            if args.bundle == DEFAULT_BUNDLE and not DEFAULT_BUNDLE.exists()
            else ""
        )
        print(f"cannot read the bundle: {exc}{hint}", file=sys.stderr)
        return 2
    dropped: dict[str, tuple[int, int]] = {}
    if older is not None:
        for kind, names in changes(older, text).items():
            print(f"{kind}: {', '.join(names) or '-'}")
        dropped = fewer(older, text)
        for name, (before, after) in dropped.items():
            print(f"FEWER {name}: {before} before, {after} now")
    gaps = missing(text)
    for kind, names in gaps.items():
        print(f"MISSING {kind}: {', '.join(names)}")
    if not gaps:
        print(
            f"{args.bundle}: all {len(SENT)} sent frames, {len(RECEIVED)} read frames and {len(FIELDS)} fields present"
        )
    return 1 if gaps or dropped else 0


if __name__ == "__main__":
    raise SystemExit(main())
