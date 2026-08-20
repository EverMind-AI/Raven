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

from raven.agent.tools.base import ToolResult
from raven.ppt.contracts import Audience, Finding, Severity
from raven.utils.helpers import ContentPart

# Kept in the payload under the audience that can act on them, because a page
# holding three times what a slide holds comes back arranged into compartments
# however often it is redesigned -- the design pass may rearrange a page but
# never change what it says.
_AUDIENCE_KEY = {Audience.AUTHOR: "for_you", Audience.DESIGNER: "for_the_design_pass"}


def where(path: Path, workspace: Path) -> str:
    """A path an author can hand straight to `read_file`.

    Every path a tool names is relative to the workspace, because a name is not an
    address. A live run was told its source was "tarvis.md" and spent two calls
    guessing where that was -- `materials/tarvis.md`, then `materials.md`, both
    wrong, the file sitting at `ppt_projects/<slug>/sources/tarvis.md`. Two calls
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
        body["next_step"] = "; ".join(asks) + ". Then run the tool again."
    return json.dumps(body, ensure_ascii=False)


def with_images(model_text: str, blocks: Iterable[ContentPart]) -> ToolResult:
    """Attach pictures to a reply whose text already stands without them."""
    parts = list(blocks)
    return ToolResult(model_text=model_text, blocks=parts) if parts else ToolResult(model_text=model_text)


def grouped(findings: Iterable[Finding]) -> dict[str, Any]:
    """Findings arranged the way their readers need them.

    By audience first, because that decides who is being asked; severity is a
    field on each entry rather than a second level of nesting, since a reader
    filtering by it is filtering a list it already has in hand.
    """
    out: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        out.setdefault(_AUDIENCE_KEY[finding.audience], []).append(_entry(finding))
    return dict(sorted(out.items()))


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
    route's call: a filled colour bar refuses a deck on the script route, where
    prose alone never stopped it coming back, and is a warning on a route whose
    engine draws the page furniture itself.
    """
    return [f for f in findings if f.kind in kinds or f.severity is Severity.BLOCKING]
