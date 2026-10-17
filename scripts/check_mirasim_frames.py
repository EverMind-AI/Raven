"""Check that a Mirasim host bundle still speaks every frame the Mirasim adapter uses.

Usage: python scripts/check_mirasim_frames.py [--against OLD_SERVER_CJS] [SERVER_CJS]

The adapter in ``raven/acp/mirasim/`` drives a protocol Mirasim does not document and
changes between releases, from a bundle that is minified: identifiers are renamed on every
build, while frame and field names stay literal. So a bundle is checked by those literals --
read as text, never run:

- each frame the adapter sends is a ``case '<name>':`` label of the host's dispatcher;
- each frame it reads is a ``'type': '<name>'`` literal;
- each field it reads is a quoted name somewhere in the bundle.

With no path it checks the host this machine runs (``~/.mirasim-remote/current/server.cjs``).
``--against`` also lists the case labels and type literals that appeared or vanished since an
older bundle, which is where a rename shows first. Exits 1 when anything the adapter uses is
missing, naming it.
"""

from __future__ import annotations

import argparse
import re
import sys
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
    "clientRef",
    "reqId",
    "sessionKey",
    "taskId",
    "seq",
    "fromSeq",
    "patch",
    "full",
    "set",
    "appendText",
    "appendReasoning",
    "phase",
    "text",
    "reasoning",
    "toolCalls",
    "plan",
    "steers",
    "interactions",
    "artifacts",
    "usage",
    "contextUsedTokens",
    "contextPercent",
    "incomplete",
    "dispatched",
    "steered",
    "noRun",
    "promptId",
    "installed",
    "routeByModel",
    "effortByModel",
    "defaultModel",
    "defaultEffort",
    "contextWindow",
    "segments",
    "older",
    "before",
    "attachments",
    "paths",
    "route",
)
"""The fields it reads or sends."""

DEFAULT_BUNDLE = Path.home() / ".mirasim-remote" / "current" / "server.cjs"

_LITERAL = r"'(?:[^'\\\n]|\\.)*'"
_CASE = re.compile(r"case\s*(" + _LITERAL + r"(?:\s*\+\s*" + _LITERAL + r")*)\s*:")
_TYPE = re.compile(r"'type'\s*:\s*(" + _LITERAL + r"(?:\s*\+\s*" + _LITERAL + r")*)")
_QUOTED = re.compile(_LITERAL)


def _joined(expression: str) -> str:
    """A literal the minifier split into ``'a'+'b'`` pieces, put back together."""
    return "".join(piece[1:-1] for piece in _QUOTED.findall(expression))


def vocabulary(text: str) -> tuple[set[str], set[str]]:
    """The bundle's ``case`` labels and its ``'type'`` literals.

    Each match is anchored on its keyword, so a quote elsewhere in the bundle -- inside a
    regular expression literal, say -- cannot pair with one of these and swallow it.
    """
    return {_joined(match) for match in _CASE.findall(text)}, {_joined(match) for match in _TYPE.findall(text)}


def quoted(text: str, name: str) -> bool:
    """Whether ``name`` appears in ``text`` as a quoted literal, whole or split into ``'a'+'b'`` pieces."""
    pattern = "'" + r"(?:'\s*\+\s*')?".join(map(re.escape, name)) + "'"
    return re.search(pattern, text) is not None


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
    """The case labels and type literals that appeared in ``new`` or vanished from ``old``."""
    old_cases, old_types = vocabulary(old)
    new_cases, new_types = vocabulary(new)
    return {
        "case labels added": sorted(new_cases - old_cases),
        "case labels removed": sorted(old_cases - new_cases),
        "type literals added": sorted(new_types - old_types),
        "type literals removed": sorted(old_types - new_types),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("bundle", nargs="?", type=Path, default=DEFAULT_BUNDLE, help="the host's server.cjs")
    parser.add_argument("--against", type=Path, help="an older server.cjs to list the changes since")
    args = parser.parse_args(argv)
    try:
        text = args.bundle.read_text(encoding="utf-8", errors="replace")
        older = args.against.read_text(encoding="utf-8", errors="replace") if args.against else None
    except OSError as exc:
        print(f"cannot read the bundle: {exc}", file=sys.stderr)
        return 2
    if older is not None:
        for kind, names in changes(older, text).items():
            print(f"{kind} ({len(names)}): {', '.join(names) or '-'}")
    gaps = missing(text)
    for kind, names in gaps.items():
        print(f"MISSING {kind}: {', '.join(names)}")
    if not gaps:
        print(
            f"{args.bundle}: all {len(SENT)} sent frames, {len(RECEIVED)} read frames and {len(FIELDS)} fields present"
        )
    return 1 if gaps else 0


if __name__ == "__main__":
    raise SystemExit(main())
