"""The verify gate's counters must leave the process.

`rejected_on_elided` was cited as "identically zero across the whole tree" in the
evidence that retracted the dr@2.x elision attribution. It was not zero: the warning
emitted alongside its increment is present on 8-12.5% of the questions in every DR arm
of six separate batches. It read as zero because nothing ever wrote it down - the
counter lived in per-turn metadata and died with the subprocess, and the only trace was
a logger line in a stderr tail the launcher truncates to 1500 bytes.

So these tests pin the three properties that make the record trustworthy rather than
merely present:

* an absent ledger writes nothing at all (write-only, off by default);
* a gate that was installed but never fired is distinguishable from a gate that was
  never installed, because the second one cannot produce the `installed` line;
* an outcome that measured nothing writes null, never 0.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from raven.agent.flow.verify import DraftReviewerGate
from raven.agent.hook import AgentHookContext


class _Rejecter:
    async def chat_with_retry(self, **kwargs):
        return SimpleNamespace(
            content='{"pass": false, "unsupported_claims": ["the deciding date"], '
            '"issues": ["no source", "wrong unit"]}'
        )


class _Passer:
    async def chat_with_retry(self, **kwargs):
        return SimpleNamespace(content='{"pass": true, "unsupported_claims": [], "issues": []}')


def _ctx(*, elided: bool = False, metadata: dict | None = None) -> AgentHookContext:
    ctx = AgentHookContext(session_key="cli:test")
    ctx.iteration = 3
    ctx.response = SimpleNamespace(has_tool_calls=False, content="reasoning</think>## Answer: X")
    body = "[earlier tool output elided to fit the context window]" if elided else "docid=5 body"
    ctx.messages = [
        {"role": "user", "content": "task"},
        {"role": "tool", "name": "web_fetch", "content": body},
    ]
    if metadata is not None:
        ctx.metadata.update(metadata)
    return ctx


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_no_ledger_configured_writes_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("RAVEN_WEB_LEDGER", raising=False)
    gate = DraftReviewerGate(_Passer())
    # An unconfigured ledger must not create a file anywhere, and must not raise.
    assert list(tmp_path.iterdir()) == []
    assert gate.name == "DraftReviewerGate"


@pytest.mark.asyncio
async def test_installed_line_separates_never_fired_from_never_installed(monkeypatch, tmp_path):
    ledger = tmp_path / "l.jsonl"
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(ledger))

    gate = DraftReviewerGate(_Passer(), max_revisions=1, strict_reject_only=True)
    installed = [r for r in _read(ledger) if r["op"] == "verify_gate"]
    assert len(installed) == 1, "construction is the presence flag; without it, zero is ambiguous"
    assert installed[0]["event"] == "installed"
    assert installed[0]["strict_reject_only"] is True
    assert installed[0]["max_revisions"] == 1
    # The reviewer is named on the row: a stub provider has no default, so null here.
    assert "model" in installed[0] and installed[0]["model"] is None

    # A turn that still has tool calls is not a review; it must not add a line, or every
    # tool-calling turn would enter the ledger as an event.
    ctx = _ctx()
    ctx.response = SimpleNamespace(has_tool_calls=True, content="")
    await gate.after_iteration(ctx)
    assert [r for r in _read(ledger) if r["op"] == "verify"] == []
    # ...yet the gate is still demonstrably present.
    assert len([r for r in _read(ledger) if r["op"] == "verify_gate"]) == 1


@pytest.mark.asyncio
async def test_elision_carveout_lands_its_trigger_count(monkeypatch, tmp_path):
    ledger = tmp_path / "l.jsonl"
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(ledger))
    gate = DraftReviewerGate(_Rejecter())

    await gate.after_iteration(_ctx(elided=True))

    rows = [r for r in _read(ledger) if r["op"] == "verify"]
    assert len(rows) == 1
    row = rows[0]
    assert row["outcome"] == "degraded_elided"
    assert row["reviewer_pass"] is False
    assert row["n_unsupported_claims"] == 1
    assert row["n_issues"] == 2
    # The whole point: the number the carve-out keyed on is now on disk.
    assert row["elided_in_context"] == 1
    assert row["reviewed"] is True


@pytest.mark.asyncio
async def test_every_row_names_the_model_that_reviewed(monkeypatch, tmp_path):
    """A mode overlay can move the reviewer per session, and the appendix and the
    observers exporter read the model off these rows; a pin lands as written, and
    a null pin resolves to the provider's default rather than to nothing."""
    ledger = tmp_path / "l.jsonl"
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(ledger))

    class _PasserWithDefault(_Passer):
        def get_default_model(self):
            return "main/model"

    pinned = DraftReviewerGate(_Rejecter(), model="judge/cheap")
    await pinned.after_iteration(_ctx())
    rows = _read(ledger)
    assert [r["model"] for r in rows if r["op"] == "verify_gate"][-1] == "judge/cheap"
    assert [r["model"] for r in rows if r["op"] == "verify"][-1] == "judge/cheap"

    inherited = DraftReviewerGate(_PasserWithDefault())
    await inherited.after_iteration(_ctx())
    rows = _read(ledger)
    assert [r["model"] for r in rows if r["op"] == "verify"][-1] == "main/model"


@pytest.mark.asyncio
async def test_a_plain_reject_is_recorded_as_reject(monkeypatch, tmp_path):
    ledger = tmp_path / "l.jsonl"
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(ledger))
    gate = DraftReviewerGate(_Rejecter(), fail_open_on_elided_evidence=False)

    decision = await gate.after_iteration(_ctx(elided=False))

    assert decision.rollback is True
    rows = [r for r in _read(ledger) if r["op"] == "verify"]
    assert [r["outcome"] for r in rows] == ["reject"]
    assert rows[0]["elided_in_context"] == 0
    assert rows[0]["revisions"] == 1


@pytest.mark.asyncio
async def test_budget_spent_writes_null_not_zero(monkeypatch, tmp_path):
    ledger = tmp_path / "l.jsonl"
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(ledger))
    reviewer = _Rejecter()
    gate = DraftReviewerGate(reviewer, max_revisions=1)

    # Budget already spent: the gate accepts without reviewing.
    spent = {"verify_gate": {"revisions": 1, "reviews": 1, "fail_open": 0,
                             "passes": 0, "rejects": 1,
                             "evidence_elided_in_context": 7}}
    await gate.after_iteration(_ctx(metadata=spent))

    rows = [r for r in _read(ledger) if r["op"] == "verify"]
    assert [r["outcome"] for r in rows] == ["budget_spent"]
    row = rows[0]
    assert row["reviewed"] is False
    # No review ran, so these were not measured. 0 would be a measurement.
    assert row["n_unsupported_claims"] is None
    assert row["n_issues"] is None
    assert row["review"] is None
    # And the previous review's elision snapshot must not be carried forward as fresh.
    assert row["elided_in_context"] is None


@pytest.mark.asyncio
async def test_the_key_verdict_is_not_reused(monkeypatch, tmp_path):
    """`traj_scored.jsonl` already uses `verdict` for "the judge marked this correct"."""
    ledger = tmp_path / "l.jsonl"
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(ledger))
    gate = DraftReviewerGate(_Passer())

    await gate.after_iteration(_ctx())

    rows = _read(ledger)
    assert rows, "expected the installed line plus one review line"
    for row in rows:
        assert "verdict" not in row, f"name collision with the judge's verdict: {row}"
    assert [r["outcome"] for r in rows if r["op"] == "verify"] == ["pass"]


# --------------------------------------------------------------------------- #
# dr@2.9: the draft-1 recovery anchor is a contract, not a coincidence        #
# --------------------------------------------------------------------------- #


def test_both_revision_prompts_share_the_recovery_anchor():
    """Offline draft-1 recovery greps one sentence; two code paths can emit it.

    ``ForcedFinalizeGate``-adjacent analysis recovers the rejected draft by finding
    the injected revision turn and taking the assistant message before it (the pair
    is written by a single ``rollback_inject``, so the pairing itself cannot slip).
    The anchor it greps for is the opening sentence of the revision prompt - and
    ``verify.py`` picks between TWO prompts depending on ``evidence_round``.

    Today both happen to open with the same sentence, so recovery is complete: on
    the dr@2.7 batches it found 49+48+47+47 = 191 rejected drafts, one per item, a
    clean pairing under ``max_revisions=1``. Nothing enforces that, and
    ``evidence_round`` is now enabled on an arm - so the day someone rewrites the
    opening of the evidence-round prompt, recovery silently under-counts and the
    only symptom is "fewer rejections", which reads as an improvement.
    """
    from raven.agent.flow.verify import _EVIDENCE_ROUND_PROMPT, _REVISION_PROMPT

    anchor = "A reviewer rejected the draft above."
    assert _REVISION_PROMPT.startswith(anchor)
    assert _EVIDENCE_ROUND_PROMPT.startswith(anchor)


# --------------------------------------------------------------------------- #
# dr@3.0: the draft that ships is the one draft nobody read                   #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_the_shipping_draft_can_be_reviewed_for_the_record(monkeypatch, tmp_path):
    """``max_revisions`` capped reviews as well as revisions.

    So the last draft - the one that becomes the answer - was accepted unexamined:
    42 of 47 rejects on one batch, 28 of 28 on another. With the knob on, that draft
    gets a verdict, and the ledger row carries the counts a ``budget_spent`` row
    correctly wrote as null.
    """
    ledger = tmp_path / "l.jsonl"
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(ledger))
    gate = DraftReviewerGate(_Rejecter(), max_revisions=1, review_final_draft=True)

    spent = {"verify_gate": {"revisions": 1, "reviews": 1, "fail_open": 0,
                             "passes": 0, "rejects": 1,
                             "evidence_elided_in_context": 7}}
    decision = await gate.after_iteration(_ctx(metadata=spent))

    rows = [r for r in _read(ledger) if r["op"] == "verify"]
    assert [r["outcome"] for r in rows] == ["budget_spent_reviewed"]
    row = rows[0]
    assert row["reviewed"] is True
    assert row["reviewer_pass"] is False
    assert row["n_unsupported_claims"] == 1
    # The decision is unchanged: recorded, not acted on.
    assert decision.rollback is False
    assert decision.short_circuit_result is None


@pytest.mark.asyncio
async def test_reviewing_the_final_draft_never_sends_the_turn_back(monkeypatch, tmp_path):
    """Zero score budget is the whole premise, so it has to be asserted.

    A reviewer that rejects here must still accept: injecting feedback would add a
    re-sample past the declared revision budget, which is a distribution change
    wearing an observation's name - and it would arrive without a version bump,
    because the knob advertises itself as observation-only.
    """
    ledger = tmp_path / "l.jsonl"
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(ledger))
    gate = DraftReviewerGate(_Rejecter(), max_revisions=1, review_final_draft=True)
    meta = {"verify_gate": {"revisions": 1, "reviews": 1, "fail_open": 0,
                            "passes": 0, "rejects": 1}}
    decision = await gate.after_iteration(_ctx(metadata=meta))

    assert decision.rollback is False
    assert not decision.rollback_inject
    assert meta["verify_gate"]["revisions"] == 1


@pytest.mark.asyncio
async def test_the_knob_off_leaves_the_landed_behaviour_byte_identical(monkeypatch, tmp_path):
    """Every published batch ran the unreviewed path; it must stay reachable and unchanged."""
    ledger = tmp_path / "l.jsonl"
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(ledger))
    gate = DraftReviewerGate(_Rejecter(), max_revisions=1)
    await gate.after_iteration(_ctx(metadata={"verify_gate": {
        "revisions": 1, "reviews": 1, "fail_open": 0, "passes": 0, "rejects": 1}}))

    rows = [r for r in _read(ledger) if r["op"] == "verify"]
    assert [r["outcome"] for r in rows] == ["budget_spent"]
    assert rows[0]["reviewed"] is False


@pytest.mark.asyncio
async def test_a_stalled_attempt_lands_in_the_ledger_with_its_number(monkeypatch, tmp_path):
    """A stall used to exist only as a terminal warning line - uncountable after
    the fact, which is why the stall rate was reconstructed from message-gap
    archaeology. One row per stalled attempt makes it a statistic, and the op is
    ``verify_gate`` so the trail's outcome reader never sees it."""
    import asyncio

    ledger = tmp_path / "l.jsonl"
    monkeypatch.setenv("RAVEN_WEB_LEDGER", str(ledger))

    class _Staller:
        async def chat_with_retry(self, **kwargs):
            await asyncio.sleep(1.0)

    gate = DraftReviewerGate(_Staller(), timeout_seconds=0.12, attempt_timeout_seconds=0.05)
    await gate.after_iteration(_ctx())

    stalls = [r for r in _read(ledger) if r["op"] == "verify_gate" and r.get("event") == "stall"]
    assert stalls, "each stalled attempt writes a row"
    assert [r["attempt"] for r in stalls] == list(range(1, len(stalls) + 1))
    assert all(r["slice_s"] == 0.05 for r in stalls)
    assert [r for r in _read(ledger) if r["op"] == "verify" and r.get("outcome") == "stall"] == []
