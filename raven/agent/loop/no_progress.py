"""When a tool call that keeps working is still getting nowhere.

``failure_streak`` catches the model repeating a call that fails. This catches
the other shape: a call that succeeds every time and answers the same thing
every time. The failure streak cannot see it -- a success resets that counter to
zero -- and one live run spent an hour reading one file 127 times, exit 0 on
every call, ending the turn no further along than it started.

Counted per turn rather than consecutively. The run that motivated this
alternated two spellings of one read, a plain read and the same read behind a
checksum, so no two neighbouring calls were identical and a consecutive streak
would have stayed at one forever. What decides is how many times one exact call
has already given one exact answer in this turn, not whether those times were
adjacent -- at the cost that two spellings each reach the threshold on their
own, so an alternating pair takes twice the calls to fire.
"""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Any


def no_progress_key(tool: str, arguments: Any, result: str, blocks: Any = None) -> str:
    """One exact call together with its exact answer, as a dict key.

    The answer is half the key on purpose: a poll whose answer changes is making
    progress, and only an unchanging one is the case this counts. Reading the raw
    result is what makes that true -- the untrusted fence carries a random nonce,
    so a key built after fencing would never match itself.

    ``blocks`` is the other half of the answer, and leaving it out was wrong.
    ``model_text`` is not everything the model receives: an image read answers
    with stable metadata -- path, dimensions, a token estimate -- while the
    pixels ride in the blocks. A render-edit-inspect pass redrawing a slide at
    one size therefore reads identical text and different pictures, which is
    progress; keyed on the text alone it counts as a repeat and the nudge fires
    on the eighth read, in exactly the workflow this guard exists to help.
    """
    material = json.dumps(arguments, sort_keys=True, ensure_ascii=False, default=str)
    picture = json.dumps(blocks, sort_keys=True, ensure_ascii=False, default=str) if blocks else ""
    return sha256(f"{tool}\x00{material}\x00{result}\x00{picture}".encode()).hexdigest()


def no_progress_nudge(tool: str, n: int) -> str:
    """Injected when one call has answered identically N times in a turn.

    Points at the expectation rather than at the tool: the call is working, so
    "try a different tool" is the wrong advice. What has gone wrong is upstream
    of the read -- either the answer was never the one that settles the
    question, or something else is writing what the model keeps reading.
    """
    return (
        f"[loop] `{tool}` has returned the same result for the same arguments {n} times in this turn. "
        "Calling it again will not answer differently. Stop re-reading and change something: if the "
        "answer contradicts what you expect, the expectation is what to check -- something other than "
        "this call may be writing what you are reading."
    )


__all__ = ["no_progress_key", "no_progress_nudge"]
