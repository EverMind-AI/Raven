"""Every shipped config is the flow we measured, minus a written-down allowlist.

## Why this exists

A 2026-08-28 audit of the product surface found the shipped
`profiles/student_sglang.json` differing from its same-backend benchmark arm on
**eleven drFlow knobs**, all in the same direction: weaker. All three anti-spin
backstops off, both reviewer rubrics off, SERP shaping off. The assembly it
actually built carried two of the five observers.

The cause was not carelessness. Every gate in this repository is scoped to "does
this change the next batch's distribution", and a shipped config **takes part in
no batch** - so it was structurally invisible to all of them. Fifteen enabled
templates passed the version-sync gate; the product config passed by inheriting
class defaults, which were all off.

This test is the patch for that seam, and it deliberately lives in `tests/`
rather than in the evaluation harness: a check over there only runs when whoever
writes the next launcher remembers to invoke it, and forgetting to invoke it is
the disease itself. Here it runs with `make test`.

## The criterion

The reference is `profiles/student_sglang_web_dr.json`, the live-web treatment
arm that actually produced published numbers. Every shipped config with
`drFlow.enabled` - the product config and everything in `examples/` - is compared
against it field by field on **effective values**. Any difference not on an
allowlist fails, and every allowlist entry carries a reason.

Effective values, not written keys: `populate_by_name=True` means a config
written in snake_case turns the knob on just as well, while a check reading
camelCase keys stays green. This repository has paid for that mistake twice.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.config import load_raven_config

_ROOT = Path(__file__).resolve().parents[1]

#: The reference: the live-web treatment arm that actually produced numbers.
REFERENCE = _ROOT / "profiles" / "student_sglang_web_dr.json"

#: Behaviour-inert. ``version`` is a label; a reading recovers its build from
#: ``dr_segment_sha``, never from the label.
INERT = {"version"}

#: Differences allowed on every shipped config. Prefix match; the value is the reason.
UNIVERSAL: dict[str, str] = {
    "final_shape.": (
        "Answer SHAPE, not research behaviour. The reason a benchmark arm pins these "
        "off is hard: they all live under DRFlowConfig, and the anchor arm "
        "(drFlow.enabled=false) makes build_dr_flow return None, so it is "
        "structurally unable to receive any of them. Switching them on for a "
        "treatment arm would hand one side an answer-extraction advantage unrelated "
        "to research quality - on one 302-question batch only 15.6% of anchor answers "
        "carried any marker, so the answer had to be recovered from prose. Unifying "
        "them would not make the setup cleaner; it would break the instrument."
    ),
    "think_closing_tag_required": (
        "Splits by BACKEND, not by bench-vs-product: all three sglang configs set it "
        "true, both channel-separated backends set it false. Comparing it across "
        "backends counts a backend difference as a design difference."
    ),
    "verify.review_final_draft": (
        "★ 20260901 (Framework). The one flow field a shipped config may raise alone, "
        "because it cannot change the answer: verify.py's budget_spent_reviewed branch "
        "returns the same accepting decision as budget_spent, nothing enters history, "
        "and the score budget is zero by construction. What it adds is a verdict on the "
        "draft that actually ships - the one draft nobody read, 42 of 47 rejects on one "
        "batch. A bench arm turning it on would spend one extra review per question for "
        "a field that does not score, which is compute the matched-compute column has to "
        "carry; the product surface has no such column."
    ),
    # ``max_iterations`` was excused here as "a bound" until 2026-09-02. It is not
    # behaviour-inert: reaching the cap marks the turn interrupted and runs
    # exhaustion synthesis, a control-flow ending the measured arm never took at
    # its own cap - so a hard question could produce a different answer while this
    # file stayed green. A config that lowers it now carries its own PER_CONFIG
    # reason naming that window.
}

#: Knobs a specific config legitimately turns on to demonstrate a feature.
PER_CONFIG: dict[str, dict[str, str]] = {
    "dr_ask_user.json": {
        "ask_user.": "demonstrating ask_user is this config's entire purpose",
        "conversation.": "a clarify handoff needs a next turn, which only the conversation surface has",
        "tools_allowlist": "build_dr_flow widens the allowlist itself when ask_user is on",
        "max_iterations": (
            "a demo turn is capped at 20 iterations to stay watchable. NOT "
            "behaviour-inert: a question still researching at the cap ends on the "
            "exhaustion path where the measured arm ran on - accepted for a demo, "
            "with the window named rather than excused as a bound"
        ),
    },
    "dr_multi_turn.json": {
        "conversation.": "demonstrating multi-turn is this config's entire purpose",
        "max_iterations": (
            "same 20-iteration demo cap and the same named exhaustion window as "
            "dr_ask_user.json"
        ),
    },
    "dr_shallow.json": {
        "budget_note.warn_ratio": (
            "the profile's one depth knob: at maxIterations 20 it moves the converge "
            "push from turn 16 to turn 12 - see the five-delta table in "
            "examples/README.md"
        ),
        "conversation.": (
            "it is dr_multi_turn.json plus the priced gates, so it keeps the "
            "conversation surface that profile demonstrates"
        ),
        "fetch_gate.enabled": (
            "priced hygiene breaker: withholds web_search after k searches with no "
            "page opened - it cuts a spiral, not depth, which is why a product "
            "profile carries it while the measured arm predates it"
        ),
        "search.saturation.": (
            "priced hygiene at k=5 (33% of searches removed at <=0.66pp), and "
            "'widen' broadens the query family instead of stopping - the measured "
            "arm ran without the ladder entirely"
        ),
        "max_iterations": (
            "the 20-iteration cap is this profile's depth promise (the five-delta "
            "table in examples/README.md prices the whole profile around it). NOT "
            "behaviour-inert: a question still researching at the cap ends on the "
            "exhaustion path where the measured arm ran on - that trade IS the "
            "shallow profile"
        ),
    },
}


def _flat(model, prefix: str = "") -> dict[str, object]:
    """Flatten a pydantic model's EFFECTIVE values."""
    out: dict[str, object] = {}
    for name in type(model).model_fields:
        value = getattr(model, name)
        if hasattr(type(value), "model_fields"):
            out.update(_flat(value, prefix + name + "."))
        else:
            out[prefix + name] = value
    return out


def _excused(path: str, config_name: str) -> str | None:
    """The reason this difference is allowed, or None."""
    for key, why in UNIVERSAL.items():
        if path == key or path.startswith(key):
            return why
    for key, why in PER_CONFIG.get(config_name, {}).items():
        if path == key or path.startswith(key):
            return why
    return None


def _shipped() -> list[Path]:
    # ★ 20260901 (Framework): ``product_demo.json`` added. It is the demo-only profile
    # the root CLAUDE.md has required since 20260828 and nobody had built, and a config
    # that exists to be shown to people is exactly the kind that must not drift
    # unnoticed. Its one difference from the shipped config - ``reportDepth`` - is
    # already excused by the ``final_shape.`` prefix above, so adding it here costs
    # nothing today and reddens the moment someone edits its research behaviour.
    out = [_ROOT / "profiles" / "student_sglang.json",
           _ROOT / "profiles" / "product_demo.json"]
    out += sorted((_ROOT / "examples").glob("*.json"))
    return out


def _drift(cfg_path: Path, ref_flat: dict[str, object]) -> list[str]:
    """Unexcused differences, in readable form."""
    cfg = load_raven_config(cfg_path)
    if not cfg.dr_flow.enabled:
        return []
    got = _flat(cfg.dr_flow)
    bad = []
    for key in sorted(got):
        if key in INERT:
            continue
        if got[key] == ref_flat.get(key):
            continue
        if _excused(key, cfg_path.name) is None:
            bad.append(f"{key}: shipped={got[key]!r} measured={ref_flat.get(key)!r}")
    return bad


@pytest.fixture(scope="module")
def reference() -> dict[str, object]:
    assert REFERENCE.exists(), f"reference arm missing: {REFERENCE}"
    return _flat(load_raven_config(REFERENCE).dr_flow)


@pytest.mark.parametrize("cfg_path", _shipped(), ids=lambda p: p.name)
def test_a_shipped_config_does_not_drift_from_the_measured_flow(cfg_path, reference):
    """What ships has to be what we measured.

    A design that is sound should serve a benchmark and a real user alike, so a
    difference in RESEARCH BEHAVIOUR has only two possible causes: the shipped side
    is missing a mechanism we already measured and kept, or it is running a
    combination nobody has ever tested. Both belong here, in red.
    """
    drift = _drift(cfg_path, reference)
    assert not drift, (
        f"{cfg_path.name} drifts from the measured arm {REFERENCE.name} on research "
        f"behaviour:\n  " + "\n  ".join(drift)
        + "\n\nEither set it to the measured value, or add it to UNIVERSAL / "
        "PER_CONFIG together with a REASON."
    )


def test_the_check_can_actually_fail(tmp_path, reference):
    """The other direction: a config with altered research behaviour must be caught.

    Without this, the test above would also pass if the flattener returned an empty
    dict - and an empty comparison set is output-identical to having no differences,
    which is a shape this repository has been burned by before.
    """
    import json

    doc = json.loads(REFERENCE.read_text(encoding="utf-8"))
    doc["drFlow"].setdefault("spinBreaker", {})["enabled"] = False   # drop a backstop
    doc["drFlow"].setdefault("digest", {})["verbatimHeadChars"] = 0  # drop the verbatim head
    broken = tmp_path / "broken.json"
    broken.write_text(json.dumps(doc), encoding="utf-8")

    drift = _drift(broken, reference)
    assert any("spin_breaker.enabled" in d for d in drift), drift
    assert any("digest.verbatim_head_chars" in d for d in drift), drift

    # And an allowlisted field must NOT be caught: a check that reddens on
    # everything is as useless as one that reddens on nothing.
    doc2 = json.loads(REFERENCE.read_text(encoding="utf-8"))
    doc2["drFlow"].setdefault("finalShape", {})["reportStructure"] = True
    shaped = tmp_path / "shaped.json"
    shaped.write_text(json.dumps(doc2), encoding="utf-8")
    assert _drift(shaped, reference) == []


def test_every_allowlist_entry_names_something_real(reference):
    """Allowlist paths must be real DRFlowConfig fields (or field prefixes).

    A typo'd excuse silently waves through a genuine difference, and is
    indistinguishable in the output from a correct one.
    """
    known = set(reference)
    tables = [(UNIVERSAL, "UNIVERSAL")]
    tables += [(v, f"PER_CONFIG[{k}]") for k, v in PER_CONFIG.items()]
    for table, label in tables:
        for path in table:
            hit = path in known or any(k.startswith(path) for k in known)
            assert hit, f"{label} names {path!r}, which is not a DRFlowConfig field"


def test_the_reference_is_a_measured_arm_not_a_product_config(reference):
    """The reference has to be an arm that ran, not one picked for convenience.

    Using a product config as the reference makes the whole file circular: agreement
    would only show that we copied ourselves.
    """
    ref = load_raven_config(REFERENCE)
    assert ref.dr_flow.enabled
    # A measured arm pins every answer-shape switch off. That is its fingerprint as
    # the arm the numbers came from.
    assert ref.dr_flow.final_shape.report_structure is False
    assert ref.dr_flow.final_shape.require_marker is False
    assert ref.dr_flow.final_shape.process_appendix is False
