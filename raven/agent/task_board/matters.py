"""Which groups a turn may have moved, and the check that its update answered for each.

A post-turn update tends to write one step per turn, on the matter the turn is known to be
about, and to fold every other matter into it. Code therefore lists the groups the turn may
have moved -- its candidates -- and the update has to say of each, in ``matters``, whether the
turn moved it, quoting the turn's own words for every one it did.
"""

from __future__ import annotations

import re
from collections.abc import Collection, Mapping, Sequence
from typing import Any

from raven.agent.task_board.board import Board, Problem, TaskGroup

_W = "0-9A-Za-z_"
# ASCII-only boundaries on purpose: a Chinese character right beside a token ends it, the way
# a space does, since Chinese prose runs a PR number into the words around it. Every repeat is
# bounded: a title is the model's text, and an unbounded one rescans the rest of a long run
# from every start in it.
_ANCHOR = re.compile(
    r"(?P<ref>#\d{1,10})(?!\d)"
    r"|`(?P<code>[^`\n]{3,100})`"
    rf"|(?P<path>[{_W}.-]{{0,100}}[{_W}-]/[{_W}./-]{{1,100}})"
    rf"|(?P<file>[{_W}-]{{2,60}}\.[A-Za-z]{{2,5}})(?![{_W}])"
    rf"|(?<![{_W}.])(?P<version>v?\d{{1,10}}(?:\.\d{{1,10}}){{1,4}})(?![{_W}])"
    rf"|(?<![{_W}.#])(?P<number>\d{{3,20}})(?![{_W}.])"
)
_WORD_CHAR = re.compile(f"[{_W}]")
_LINK = re.compile(r"\[([^\]\n]{0,200})\]\([^)\s]{0,2000}\)")
_ELISION = re.compile(r"\.\.\.|\u2026")
_SHORTEST_PIECE = 4
_SHAPE = 'must be a list of {"group": "<id>", "moved": true|false, "evidence": "<quote>"} entries, one per listed group'


def anchors(title: str) -> list[str]:
    """The tokens of ``title`` that identify a matter rather than describe it, in title order.

    An issue or PR reference, a code span, a path, a file name, a version, a number of three or
    more digits. Plain words are never anchors: "fix" and "review" name half a board.
    """
    found: list[str] = []
    for match in _ANCHOR.finditer(title):
        token = next(value for value in match.groupdict().values() if value)
        if token not in found:
            found.append(token)
    return found


def _names(text: str, token: str) -> bool:
    token = token.casefold()
    before = r"(?<![0-9a-z_])" if _WORD_CHAR.match(token[0]) else ""
    after = r"(?![0-9a-z_])" if _WORD_CHAR.match(token[-1]) else ""
    return re.search(before + re.escape(token) + after, text) is not None


def _tokens(group: TaskGroup) -> list[str]:
    return [group.id, *(link.id for step in group.steps for link in step.tasks), *anchors(group.title)]


def candidates(board: Board, owners: Sequence[str], text: str) -> list[str]:
    """The groups a turn's update must answer for: ``owners`` first, as given, then every other
    group on ``board``, open or completed, that ``text`` names by id, by the id of a Task its
    steps link, or by a title anchor, in board order."""
    folded = text.casefold()
    listed = list(dict.fromkeys(owners))
    for group in board.groups:
        if group.id not in listed and any(_names(folded, token) for token in _tokens(group)):
            listed.append(group.id)
    return listed


def normalized(text: str) -> str:
    """``text`` the way a quote is checked against it: a markdown link read as its text, emphasis
    and code backticks gone, whitespace collapsed, case folded."""
    text = _LINK.sub(r"\1", text).replace("*", "").replace("`", "")
    return re.sub(r"\s+", " ", text).strip().casefold()


def quoted(evidence: str, words: str) -> bool:
    """Whether ``evidence`` is found word for word in ``words`` (already :func:`normalized`).

    An elision (``...``) splits it into pieces, each found on its own; a piece shorter than four
    characters is left out, and evidence with no piece left is not a quote.
    """
    pieces = [piece for piece in (normalized(p) for p in _ELISION.split(evidence)) if len(piece) >= _SHORTEST_PIECE]
    return bool(pieces) and all(piece in words for piece in pieces)


def _well_formed(matters: Any) -> bool:
    return isinstance(matters, list) and all(
        isinstance(entry, Mapping) and isinstance(entry.get("group"), str) and isinstance(entry.get("moved"), bool)
        for entry in matters
    )


def matters_problems(
    args: Mapping[str, Any], candidates: Sequence[str], known: Collection[str], words: str
) -> list[Problem]:
    """What the update's ``matters`` gets wrong against its own steps and the turn's ``words``.

    Every candidate is answered; a group answered moved has a step; a step on a ``known`` group,
    listed or not, is answered moved; and each moved answer's evidence is the turn's own words.
    A step that opens a new group answers for nothing. An answer repeated for one group: the
    last one counts.
    """
    matters = args.get("matters")
    if not _well_formed(matters):
        return [Problem(_SHAPE, "matters")]
    said = {entry["group"]: entry for entry in matters}
    steps = args.get("steps")
    stepped = [s.get("group") for s in steps if isinstance(s, Mapping)] if isinstance(steps, list) else []
    text = normalized(words)
    out = [Problem(f"answer {g} -- did the turn move it?", "matters") for g in candidates if g not in said]
    for group, entry in said.items():
        if not entry["moved"]:
            continue
        if group in known and group not in stepped:
            out.append(Problem(f"matters says the turn moved {group}, but no step is on it"))
        evidence = entry.get("evidence")
        if not isinstance(evidence, str) or not quoted(evidence, text):
            out.append(
                Problem(
                    f"the evidence for {group} is not in this turn's message or reply; quote this turn's own words,"
                    " or answer moved: false",
                    "matters",
                )
            )
    for group in dict.fromkeys(stepped):
        if group in known and not said.get(group, {}).get("moved"):
            out.append(
                Problem(
                    f"{group} has a step, but matters does not say the turn moved it; add "
                    f'{{"group": "{group}", "moved": true, "evidence": "<quote>"}} with this turn\'s words,'
                    " or drop the step"
                )
            )
    return out
