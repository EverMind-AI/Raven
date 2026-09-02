"""The reply envelope, and the two invariants the predecessor lost."""

from __future__ import annotations

import json

from raven.contracts.tool import ToolResult
from raven.utils.images import image_block
from raven_ppt.contracts import Finding, Severity
from raven_ppt.tools import _return


def _finding(**kw) -> Finding:
    base = {"kind": "band", "severity": Severity.BLOCKING, "message": "a filled colour bar on page 3", "page": 3}
    return Finding(**{**base, **kw})


def test_a_refusal_always_carries_both_ok_and_error() -> None:
    body = json.loads(_return.failed("no build script yet", path="/w/build.py"))
    assert body["ok"] is False
    assert body["error"] == "no build script yet"
    assert body["path"] == "/w/build.py"


def test_ok_is_derived_from_the_findings_not_asserted() -> None:
    clean = json.loads(_return.done(pages=18))
    assert clean["ok"] is True and "error" not in clean

    refused = json.loads(_return.done(blocking=[_finding()], pages=18))
    assert refused["ok"] is False
    # The predecessor could produce ok:false with no error key at all, so a
    # consumer branching on `error` disagreed with one branching on `ok`.
    assert "error" in refused
    assert refused["pages"] == 18


def test_the_refusal_line_counts_every_kind_that_stands() -> None:
    findings = [_finding(), _finding(page=5), _finding(kind="fact", message="48.3 is not in the sources", page=7)]
    body = json.loads(_return.done(blocking=findings))
    assert "2 band" in body["error"]
    assert "1 fact" in body["error"]


def test_every_ask_is_voiced_not_only_the_first() -> None:
    """One run rebuilt seven times re-checking numbers because the fact ask was
    the only instruction it got while seventeen colour bars stood untouched."""
    body = json.loads(_return.done(asks=["correct 1 unanchored value", "remove 17 filled colour bars"]))
    assert "correct 1 unanchored value" in body["next_step"]
    assert "remove 17 filled colour bars" in body["next_step"]


def test_every_finding_is_the_author_s() -> None:
    """There was a second bucket, `for_the_design_pass`, and it was read exactly as
    written: 25 pairs of overlapping words stood through eight builds of one live run
    because they were filed under somebody else. That actor is gone and `type_floor`,
    which used to be its share, comes back with the rest."""
    findings = [
        _finding(kind="density", severity=Severity.WARNING, message="295 words"),
        _finding(kind="type_floor", severity=Severity.WARNING, message="11.5pt body"),
    ]
    grouped = _return.grouped(findings)
    assert list(grouped) == ["for_you"]
    assert [f["kind"] for f in grouped["for_you"]] == ["density", "type_floor"]


def test_nothing_measured_is_no_bucket_at_all() -> None:
    """Rather than an empty list, which reads as a check that ran and found nothing
    when it is a deck nothing was measured on."""
    assert _return.grouped([]) == {}


def test_a_grouped_entry_keeps_the_sentence_the_model_acts_on() -> None:
    entry = _return.grouped([_finding()])["for_you"][0]
    assert entry["problem"] == "a filled colour bar on page 3"
    assert entry["severity"] == "blocking"
    assert entry["page"] == 3


def test_pictures_ride_along_and_the_text_still_stands_alone() -> None:
    result = _return.with_images('{"ok": true, "pptx_path": "/w/deck.pptx"}', [image_block("data:image/png;base64,A")])
    assert isinstance(result, ToolResult)
    assert result.blocks is not None and len(result.blocks) == 1
    # Upstream contract: only providers that can carry an image inside a tool
    # result ever look at blocks, so the text has to name the file itself.
    assert "/w/deck.pptx" in result.model_text


def test_no_pictures_means_no_empty_block_list() -> None:
    result = _return.with_images('{"ok": true}', [])
    assert result.blocks is None


def test_a_route_decides_which_kinds_are_fatal() -> None:
    warning_band = _finding(severity=Severity.WARNING)
    assert _return.blocking_of([warning_band], frozenset({"band"})) == [warning_band]
    assert _return.blocking_of([warning_band], frozenset({"fact"})) == []
    # A measurement that says BLOCKING is fatal on every route.
    assert _return.blocking_of([_finding(kind="fact")], frozenset()) != []
