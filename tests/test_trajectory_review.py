"""Data-layer tests for the redaction review flow (build / validate / apply)."""

from __future__ import annotations

import json

import pytest

from raven.trajectory import bugreport as breport
from raven.trajectory import redact as tredact
from raven.trajectory import review as treview

TOKEN_A = "qT7zK9mP4vX2sW8dQ5nRfJ3y"
TOKEN_B = "Zx4cV7bN1mQ9kL6pT2wYhG8s"
TOKEN_INNER = "Hj5tR8uE3iO7pA1sD4fGk9lZ"
TOKEN_OUTER = TOKEN_INNER + "W6xC2v"


def _report(tree, *, patterns=None):
    """A RedactionReport over a hand-rolled redacted tree, scanned for real."""
    report = tredact.RedactionReport(bundle_dir=tree, redacted_dir=tree.resolve())
    if patterns:
        report.patterns.update(patterns)
    report.findings = tredact.scan_residuals(tree)
    return report


def _write(tree, name, text):
    tree.mkdir(parents=True, exist_ok=True)
    (tree / name).write_text(text, encoding="utf-8")


def _decisions(items, **actions_by_kind):
    """One decision per item: confirmed acknowledged, suspected per mapping."""
    decisions = []
    for item in items:
        if item.kind == treview.KIND_CONFIRMED:
            decisions.append(treview.ReviewDecision(item.id, treview.ACTION_ACKNOWLEDGED))
        else:
            decisions.append(treview.ReviewDecision(item.id, actions_by_kind[item.token]))
    return decisions


# ── build_review_items ────────────────────────────────────────────────


def test_build_merges_same_token_across_reports(tmp_path):
    tree_a = tmp_path / "trajectory"
    tree_b = tmp_path / "problem"
    _write(tree_a, "spans.jsonl", f"one {TOKEN_A} here")
    _write(tree_b, "problem.description", f"two {TOKEN_A} there")

    items = treview.build_review_items([_report(tree_a), _report(tree_b)])

    assert len(items) == 1
    item = items[0]
    assert item.kind == treview.KIND_SUSPECTED
    assert item.token == TOKEN_A
    assert item.id == treview._item_id(TOKEN_A)
    assert [(s["label"], s["count"]) for s in item.sources] == [
        ("the span log", 1),
        ("your problem description", 1),
    ]
    assert len(item.occurrences) == 2


def test_build_same_mask_different_values_stay_separate(tmp_path):
    twin = TOKEN_A[:4] + "H6cV1bN8mQ3kL5pT" + TOKEN_A[-4:]
    tree = tmp_path / "trajectory"
    _write(tree, "spans.jsonl", f"{TOKEN_A}\n{twin}")

    items = treview.build_review_items([_report(tree)])

    assert len(items) == 2
    assert {i.token for i in items} == {TOKEN_A, twin}
    assert len({i.id for i in items}) == 2
    masks = {i.masked_sample.split()[0][:4] for i in items}
    assert masks == {TOKEN_A[:4]}


def test_build_multiple_findings_in_one_file(tmp_path):
    tree = tmp_path / "trajectory"
    _write(tree, "spans.jsonl", f"{TOKEN_A}\n{TOKEN_B}")

    items = treview.build_review_items([_report(tree)])

    assert [i.token for i in items] == [TOKEN_A, TOKEN_B]
    assert all(i.sources[0]["file"] == "spans.jsonl" for i in items)


def test_build_semantic_labels_from_spans(tmp_path):
    tree = tmp_path / "trajectory"
    spans = [
        {
            "name": "llm.call",
            "startTime": "2026-01-01T00:00:01Z",
            "attributes": {"llm.input.artifact_path": "artifacts/a1.json"},
        },
        {
            "name": "llm.call",
            "startTime": "2026-01-01T00:00:02Z",
            "attributes": {
                "llm.input.artifact_path": "artifacts/a2.json",
                "llm.output.artifact_path": "artifacts/a3.json",
            },
        },
        {
            "name": "tool.call",
            "startTime": "2026-01-01T00:00:03Z",
            "attributes": {"tool.name": "search", "tool.output.artifact_path": "artifacts/t1.json"},
        },
        {
            "name": "agent.turn",
            "startTime": "2026-01-01T00:00:04Z",
            "attributes": {"turn.output.artifact_path": "artifacts/u1.json"},
        },
    ]
    _write(tree, "spans.jsonl", "".join(json.dumps(s) + "\n" for s in spans))
    (tree / "artifacts").mkdir()
    _write(tree / "artifacts", "a2.json", TOKEN_A)
    _write(tree / "artifacts", "a3.json", TOKEN_B)
    _write(tree / "artifacts", "t1.json", TOKEN_INNER)
    _write(tree / "artifacts", "u1.json", TOKEN_OUTER.replace(TOKEN_INNER, "Vb2nM7cX4zQ8wE1rT5yK"))

    items = treview.build_review_items([_report(tree)])

    labels = {i.token: i.sources[0]["label"] for i in items}
    assert labels[TOKEN_A] == "model call #2 input"
    assert labels[TOKEN_B] == "model call #2 output"
    assert labels[TOKEN_INNER] == "tool call #1 (search) output"
    assert list(labels.values())[3] == "agent.turn turn.output"


def test_build_confirmed_item_from_placeholders(tmp_path):
    tree = tmp_path / "trajectory"
    _write(tree, "spans.jsonl", f"before {treview.PRIVATE_KEY_PLACEHOLDER} after")

    items = treview.build_review_items([_report(tree, patterns={"private-key-block": 1})])

    assert len(items) == 1
    item = items[0]
    assert item.kind == treview.KIND_CONFIRMED
    assert item.token == ""
    assert item.masked_sample == treview.PRIVATE_KEY_PLACEHOLDER
    assert item.sources == [{"label": "the span log", "file": "spans.jsonl", "count": 1}]
    occurrence = item.occurrences[0]
    assert occurrence["line"][occurrence["start"] : occurrence["end"]] == treview.PRIVATE_KEY_PLACEHOLDER


def test_build_marks_value_containment_as_linked(tmp_path):
    tree = tmp_path / "trajectory"
    _write(tree, "spans.jsonl", f"{TOKEN_INNER} and {TOKEN_OUTER} and {TOKEN_A}")

    items = treview.build_review_items([_report(tree)])

    by_token = {i.token: i for i in items}
    assert by_token[TOKEN_INNER].linked == [by_token[TOKEN_OUTER].id]
    assert by_token[TOKEN_OUTER].linked == [by_token[TOKEN_INNER].id]
    assert by_token[TOKEN_A].linked == []


# ── validate_review_decisions ─────────────────────────────────────────


def _one_item_tree(tmp_path):
    tree = tmp_path / "trajectory"
    _write(tree, "spans.jsonl", TOKEN_A)
    return treview.build_review_items([_report(tree)])


def test_validate_rejects_missing_duplicate_unknown_and_invalid(tmp_path):
    (item,) = _one_item_tree(tmp_path)

    with pytest.raises(breport.BugReportError, match="missing decision"):
        treview.validate_review_decisions([item], [])
    with pytest.raises(breport.BugReportError, match="duplicate"):
        treview.validate_review_decisions(
            [item],
            [
                treview.ReviewDecision(item.id, treview.ACTION_KEPT),
                treview.ReviewDecision(item.id, treview.ACTION_KEPT),
            ],
        )
    with pytest.raises(breport.BugReportError, match="unknown"):
        treview.validate_review_decisions([item], [treview.ReviewDecision("nope", treview.ACTION_KEPT)])
    with pytest.raises(breport.BugReportError, match="invalid action"):
        treview.validate_review_decisions([item], [treview.ReviewDecision(item.id, "acknowledged")])


def test_validate_conflicting_linked_decisions(tmp_path):
    tree = tmp_path / "trajectory"
    _write(tree, "spans.jsonl", f"{TOKEN_INNER} {TOKEN_OUTER}")
    items = treview.build_review_items([_report(tree)])

    with pytest.raises(treview.ReviewConflictError):
        treview.validate_review_decisions(
            items,
            _decisions(items, **{TOKEN_INNER: treview.ACTION_KEPT, TOKEN_OUTER: treview.ACTION_REDACTED}),
        )


# ── apply_review_decisions ────────────────────────────────────────────


def test_apply_mixed_decisions_replace_and_keep(tmp_path):
    tree = tmp_path / "trajectory"
    escaped_b = json.dumps(TOKEN_B)[1:-1]
    _write(tree, "spans.jsonl", f"keep {TOKEN_A} drop {TOKEN_B}\nescaped {escaped_b} again")
    report = _report(tree)
    items = treview.build_review_items([report])

    outcome = treview.apply_review_decisions(
        items,
        _decisions(items, **{TOKEN_A: treview.ACTION_KEPT, TOKEN_B: treview.ACTION_REDACTED}),
        reports=[report],
    )

    text = (tree / "spans.jsonl").read_text(encoding="utf-8")
    assert TOKEN_B not in text and escaped_b not in text
    assert text.count("[REDACTED:user-confirmed]") == 2
    assert TOKEN_A in text
    assert report.exact["user-confirmed"] == 2
    assert [f.token for f in report.findings] == [TOKEN_A]
    assert outcome.user_secrets == [tredact.KnownSecret("user-confirmed", TOKEN_B)]
    actions = {d["id"]: d["action"] for d in outcome.user_decisions}
    items_by_token = {i.token: i for i in items}
    assert actions[items_by_token[TOKEN_A].id] == "kept"
    assert actions[items_by_token[TOKEN_B].id] == "redacted"


def test_apply_adjacent_kept_and_redacted_do_not_leak_via_summary(tmp_path):
    """The review-verified trap: samples copy neighbors verbatim, so the
    rewritten summary must not carry the redacted neighbor's plaintext."""
    tree = tmp_path / "trajectory"
    _write(tree, "spans.jsonl", f"{TOKEN_A} {TOKEN_B}")
    report = _report(tree)
    items = treview.build_review_items([report])

    outcome = treview.apply_review_decisions(
        items,
        _decisions(items, **{TOKEN_A: treview.ACTION_KEPT, TOKEN_B: treview.ACTION_REDACTED}),
        reports=[report],
    )

    summary = (tree / tredact.REDACTION_METADATA_FILE).read_text(encoding="utf-8")
    assert TOKEN_B not in summary
    (entry,) = json.loads(summary)["residual_findings"]
    assert entry["sample"].startswith(TOKEN_A[:4])
    assert "[REDACTED:user-confirmed]" in entry["sample"]
    decisions = {d["action"]: d for d in outcome.user_decisions}
    assert TOKEN_B not in json.dumps(decisions)
    assert decisions["kept"]["masked_sample"].startswith(TOKEN_A[:4])


def test_apply_adjacent_tokens_both_kept_pass_verification(tmp_path):
    """Full-keep must not be rejected by the count check even though each
    token's plaintext also appears inside the other's summary window."""
    tree = tmp_path / "trajectory"
    _write(tree, "spans.jsonl", f"{TOKEN_A} {TOKEN_B}")
    report = _report(tree)
    items = treview.build_review_items([report])

    outcome = treview.apply_review_decisions(
        items,
        _decisions(items, **{TOKEN_A: treview.ACTION_KEPT, TOKEN_B: treview.ACTION_KEPT}),
        reports=[report],
    )

    assert outcome.user_secrets == []
    text = (tree / "spans.jsonl").read_text(encoding="utf-8")
    assert TOKEN_A in text and TOKEN_B in text


def test_apply_kept_token_with_checksum_context_occurrence(tmp_path):
    """finding.count skips checksum contexts; the baseline must not."""
    tree = tmp_path / "trajectory"
    checksum_line = f'{{"tool.output.artifact_sha1": "{TOKEN_A}"}}'
    _write(tree, "spans.jsonl", f"plain {TOKEN_A}\n{checksum_line}")
    report = _report(tree)
    items = treview.build_review_items([report])
    assert items[0].occurrences[0]["line_no"] == 1 and len(items[0].occurrences) == 1

    treview.apply_review_decisions(items, _decisions(items, **{TOKEN_A: treview.ACTION_KEPT}), reports=[report])

    assert (tree / "spans.jsonl").read_text(encoding="utf-8").count(TOKEN_A) == 2


def test_apply_containment_pair_same_decision(tmp_path):
    tree = tmp_path / "trajectory"
    _write(tree, "spans.jsonl", f"{TOKEN_INNER} {TOKEN_OUTER}")
    report = _report(tree)
    items = treview.build_review_items([report])

    treview.apply_review_decisions(
        items,
        _decisions(items, **{TOKEN_INNER: treview.ACTION_REDACTED, TOKEN_OUTER: treview.ACTION_REDACTED}),
        reports=[report],
    )

    text = (tree / "spans.jsonl").read_text(encoding="utf-8")
    assert TOKEN_INNER not in text and TOKEN_OUTER not in text
    summary = (tree / tredact.REDACTION_METADATA_FILE).read_text(encoding="utf-8")
    assert TOKEN_INNER not in summary and json.loads(summary)["residual_findings"] == []


def test_apply_filters_source_labels(tmp_path):
    tree = tmp_path / "trajectory"
    span = {
        "name": "tool.call",
        "startTime": "2026-01-01T00:00:01Z",
        "attributes": {"tool.name": TOKEN_A, "tool.output.artifact_path": "artifacts/t1.json"},
    }
    _write(tree, "spans.jsonl", json.dumps(span) + "\n")
    (tree / "artifacts").mkdir()
    _write(tree / "artifacts", "t1.json", f"{TOKEN_A} used here")
    report = _report(tree)
    items = treview.build_review_items([report])
    assert TOKEN_A in items[0].sources[0]["label"]

    outcome = treview.apply_review_decisions(
        items, _decisions(items, **{TOKEN_A: treview.ACTION_REDACTED}), reports=[report]
    )

    (decision,) = outcome.user_decisions
    assert TOKEN_A not in json.dumps(decision)
    assert "[REDACTED:user-confirmed]" in decision["sources"][0]["source"]


def test_apply_acknowledges_private_key_and_emits_notice(tmp_path):
    tree = tmp_path / "trajectory"
    _write(tree, "spans.jsonl", treview.PRIVATE_KEY_PLACEHOLDER)
    report = _report(tree, patterns={"private-key-block": 2})
    items = treview.build_review_items([report])

    outcome = treview.apply_review_decisions(items, _decisions(items), reports=[report])

    assert outcome.security_notices == [
        "the original trajectory contained 2 private key block(s), replaced before export"
    ]
    (decision,) = outcome.user_decisions
    assert decision["action"] == "acknowledged"
    assert decision["category"] == "private-key-block"


def test_apply_renames_paths_carrying_redacted_tokens(tmp_path):
    """Tar member names come from the tree, so a redacted value in a file name
    must be renamed with the same replacement its references got."""
    tree = tmp_path / "trajectory"
    (tree / "artifacts").mkdir(parents=True)
    _write(tree / "artifacts", f"{TOKEN_A}.json", "clean content")
    _write(tree, "spans.jsonl", f'{{"tool.output.artifact_path": "artifacts/{TOKEN_A}.json"}}')
    report = _report(tree)
    items = treview.build_review_items([report])

    treview.apply_review_decisions(items, _decisions(items, **{TOKEN_A: treview.ACTION_REDACTED}), reports=[report])

    assert not (tree / "artifacts" / f"{TOKEN_A}.json").exists()
    renamed = tree / "artifacts" / "[REDACTED:user-confirmed].json"
    assert renamed.exists() and renamed.read_text(encoding="utf-8") == "clean content"
    spans = (tree / "spans.jsonl").read_text(encoding="utf-8")
    assert '"artifacts/[REDACTED:user-confirmed].json"' in spans
    rels = [str(p.relative_to(tree)) for p in tree.rglob("*") if p.is_file()]
    assert not any(TOKEN_A in rel for rel in rels)


def test_apply_rename_collision_aborts(tmp_path):
    tree = tmp_path / "trajectory"
    (tree / "artifacts").mkdir(parents=True)
    _write(tree / "artifacts", f"{TOKEN_A}.json", "clean content")
    _write(tree / "artifacts", "[REDACTED:user-confirmed].json", "already here")
    _write(tree, "spans.jsonl", f'{{"tool.output.artifact_path": "artifacts/{TOKEN_A}.json"}}')
    report = _report(tree)
    items = treview.build_review_items([report])

    with pytest.raises(breport.PreparationError, match="collides"):
        treview.apply_review_decisions(items, _decisions(items, **{TOKEN_A: treview.ACTION_REDACTED}), reports=[report])


def test_apply_renamed_file_updates_kept_finding_source(tmp_path):
    tree = tmp_path / "trajectory"
    (tree / "artifacts").mkdir(parents=True)
    _write(tree / "artifacts", f"{TOKEN_B}.json", f"keep {TOKEN_A}")
    _write(tree, "spans.jsonl", f'{{"tool.output.artifact_path": "artifacts/{TOKEN_B}.json"}}')
    report = _report(tree)
    items = treview.build_review_items([report])

    treview.apply_review_decisions(
        items,
        _decisions(items, **{TOKEN_A: treview.ACTION_KEPT, TOKEN_B: treview.ACTION_REDACTED}),
        reports=[report],
    )

    renamed = tree / "artifacts" / "[REDACTED:user-confirmed].json"
    assert renamed.read_text(encoding="utf-8") == f"keep {TOKEN_A}"
    (entry,) = json.loads((tree / tredact.REDACTION_METADATA_FILE).read_text(encoding="utf-8"))["residual_findings"]
    assert entry["file"] == "artifacts/[REDACTED:user-confirmed].json"
    assert TOKEN_B not in json.dumps(entry)


def test_apply_conflicting_decisions_touch_nothing(tmp_path):
    tree = tmp_path / "trajectory"
    _write(tree, "spans.jsonl", f"{TOKEN_INNER} {TOKEN_OUTER}")
    report = _report(tree)
    items = treview.build_review_items([report])
    before = (tree / "spans.jsonl").read_text(encoding="utf-8")

    with pytest.raises(treview.ReviewConflictError):
        treview.apply_review_decisions(
            items,
            _decisions(items, **{TOKEN_INNER: treview.ACTION_REDACTED, TOKEN_OUTER: treview.ACTION_KEPT}),
            reports=[report],
        )

    assert (tree / "spans.jsonl").read_text(encoding="utf-8") == before
    assert not (tree / tredact.REDACTION_METADATA_FILE).exists()
