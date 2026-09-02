"""The one place a deck tool's reply is built.

The predecessor had five. `_ok()` existed, and four tools bypassed it to write
`"ok": True` into a dict by hand -- one of them computing it twice in the same
method -- so `ok: false` did not reliably carry an `error` key and a consumer
branching on `error` reached a different conclusion from one branching on `ok`.
A second near-duplicate assembled the render attachments, differing from the
first only in taking a dict where the other took the same dict re-parsed from
its own JSON.

Two rules follow from the upstream tool contract and are enforced here rather
than remembered:

`model_text` stands alone. Blocks are additive, and only providers that can
carry an image inside a `role="tool"` message ever see them -- everything else
(the sentinel, subagents, the curator, session export, any Chat Completions
transport) gets the text. So the text names the file paths and the numbers, and
never says "see the image above".

Every ask, every time. Voicing one problem when two stand reads as the only
thing wrong with the deck: one run kept re-checking numbers for seven rebuilds
because the fact ask was the only instruction it got while seventeen colour bars
stood untouched.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from raven.contracts.tool import ContentPart, ToolResult
from raven_ppt.contracts import Finding, Severity


def where(path: Path, workspace: Path) -> str:
    """A path an author can hand straight to `read_file`.

    Every path a tool names is relative to the workspace, because a name is not an
    address. A live run was told its source was "tarvis.md" and spent two calls
    guessing where that was -- `materials/tarvis.md`, then `materials.md`, both
    wrong, the file sitting at `deck/sources/tarvis.md`. Two calls
    for a string the tool already held.
    """
    try:
        return str(path.relative_to(workspace))
    except ValueError:  # outside the workspace, where the absolute path is the address
        return str(path)


def failed(error: str, **detail: Any) -> str:
    """A refusal. `ok` and `error` always travel together."""
    return json.dumps({"ok": False, "error": error, **detail}, ensure_ascii=False)


def done(*, blocking: Sequence[Finding] = (), asks: Sequence[str] = (), **payload: Any) -> str:
    """A reply whose `ok` is derived from the findings rather than asserted.

    Passing `blocking` non-empty makes this a refusal that still carries the
    payload -- the deck was built and measured, and is not published. That is a
    different thing from `failed`, which means no deck came out at all.
    """
    body: dict[str, Any] = {"ok": not blocking, **payload}
    if blocking:
        body["error"] = _one_line(blocking)
    if asks:
        body["next_step"] = "; ".join(asks) + (". Then run the tool again." if blocking else ".")
    return json.dumps(body, ensure_ascii=False)


def with_images(model_text: str, blocks: Iterable[ContentPart]) -> ToolResult:
    """Attach pictures to a reply whose text already stands without them."""
    parts = list(blocks)
    return ToolResult(model_text=model_text, blocks=parts) if parts else ToolResult(model_text=model_text)


def grouped(findings: Iterable[Finding]) -> dict[str, Any]:
    """Findings under the one key that says whose they are.

    There were two, `for_you` and `for_the_design_pass`, when a second actor
    could act on a page. There is not one any more, and the second key was read
    exactly as it was written: a live run left 25 pairs of overlapping words
    alone across eight builds because they were filed under somebody else. One
    list, addressed to the author, is the whole of it -- severity is a field on
    each entry rather than a second level of nesting, since a reader filtering
    by it is filtering a list it already has in hand.
    """
    return {"for_you": [_entry(finding) for finding in findings]} if findings else {}


def _entry(finding: Finding) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "kind": finding.kind,
        "severity": finding.severity.value,
        "problem": finding.message,
    }
    if finding.page is not None:
        entry["page"] = finding.page
    if finding.detail:
        entry["detail"] = dict(finding.detail)
    return entry


def _one_line(findings: Sequence[Finding]) -> str:
    """One sentence naming what is refused, and how many of each."""
    counts: dict[str, int] = {}
    for finding in findings:
        counts[finding.kind] = counts.get(finding.kind, 0) + 1
    parts = [f"{count} {kind}" for kind, count in sorted(counts.items())]
    return "the deck is not published while these stand: " + ", ".join(parts)


def blocking_of(findings: Iterable[Finding], kinds: frozenset[str]) -> list[Finding]:
    """The findings a profile has declared fatal.

    Severity comes from the measurement, but which kinds are fatal is the
    route's call: a page the build cannot map back to its own code is fatal on
    the script route, where a render has to be matched to what drew it, and
    means nothing on a route whose engine draws the page furniture itself.

    A route may not call fatal a kind the check itself only reports; a test
    holds the two together, because for a while nothing did.
    """
    return [f for f in findings if f.kind in kinds or f.severity is Severity.BLOCKING]
