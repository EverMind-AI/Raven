"""Redaction review — per-item adjudication of what redaction could not decide.

The redaction layers (see :mod:`raven.trajectory.redact`) either replace
content they can prove sensitive or flag what they merely suspect. This module
turns those signals into an explicit user decision step for the bug-report
flow:

- :func:`build_review_items` merges the residual findings of every redaction
  report into review items — one item per token **value** (replacement is a
  global by-value operation, so a per-occurrence decision could not be
  honored), plus one "confirmed sensitive" item when a private-key block was
  hit (its content is already replaced; the item asks for informed
  acknowledgment, not a content choice). Sources are translated from artifact
  file names into human terms via the bundle's span records.
- :func:`validate_review_decisions` checks a decision set without side
  effects, so interactive callers can re-ask on conflicts before anything is
  applied.
- :func:`apply_review_decisions` rewrites the redacted trees according to the
  decisions and verifies the result: tokens the user redacted are gone in
  every spelling variant, tokens the user kept are provably untouched, and
  every string that will be serialized (samples, source labels, notices) has
  the user-redacted values filtered out.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Sequence

from raven.trajectory.bugreport import BugReportError, PreparationError
from raven.trajectory.redact import (
    REDACTION_METADATA_FILE,
    KnownSecret,
    RedactionReport,
    ResidualFinding,
    _apply_exact,
    _variants,
)

KIND_CONFIRMED = "confirmed"
KIND_SUSPECTED = "suspected"

ACTION_ACKNOWLEDGED = "acknowledged"
ACTION_KEPT = "kept"
ACTION_REDACTED = "redacted"

USER_CONFIRMED_LABEL = "user-confirmed"
PRIVATE_KEY_CATEGORY = "private-key-block"
PRIVATE_KEY_PLACEHOLDER = "[REDACTED:pattern.private-key-block]"

_FIXED_LABELS = {
    "spans.jsonl": "the span log",
    "session.jsonl": "the session transcript",
    "verdicts.jsonl": "the verdict log",
    "manifest.json": "the bundle manifest",
    REDACTION_METADATA_FILE: "the redaction summary",
}
_PROBLEM_LABELS = {
    "problem.description": "your problem description",
    "problem.expected": "your expected result",
    "problem.actual": "your actual result",
    "problem.steps": "your steps to reproduce",
    "problem.severity": "your severity rating",
    "problem.reporter": "your reporter handle",
}
_ENVIRONMENT_PREFIX = "environment."


class ReviewCancelledError(BugReportError):
    """The user cancelled the report from the review screen."""


class ReviewConflictError(BugReportError):
    """Linked items (overlapping token values) received contradictory decisions."""


@dataclass
class ReviewItem:
    """One decision the user must make before the report may ship."""

    id: str
    kind: str
    category: str
    token: str
    masked_sample: str
    sources: list[dict[str, Any]] = field(default_factory=list)
    occurrences: list[dict[str, Any]] = field(default_factory=list)
    linked: list[str] = field(default_factory=list)


@dataclass
class ReviewDecision:
    item_id: str
    action: str


@dataclass
class ReviewOutcome:
    """What :func:`apply_review_decisions` produced, for the caller to merge."""

    user_secrets: list[KnownSecret] = field(default_factory=list)
    user_decisions: list[dict[str, Any]] = field(default_factory=list)
    security_notices: list[str] = field(default_factory=list)


def _item_id(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


def _ordered_spans(spans_file: Path) -> list[dict[str, Any]]:
    spans: list[dict[str, Any]] = []
    try:
        lines = spans_file.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return spans
    for line in lines:
        try:
            span = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(span, dict):
            spans.append(span)
    spans.sort(key=lambda s: s.get("startTime") if isinstance(s.get("startTime"), str) else "")
    return spans


def _artifact_labels(root: Path) -> dict[str, str]:
    """Semantic names for ``artifacts/<name>`` files, from the span records.

    Model and tool calls are numbered in start-time order, so a finding can
    say "model call #2 input" instead of the artifact file name.
    """
    spans_file = root / "spans.jsonl"
    if not spans_file.is_file():
        return {}
    labels: dict[str, str] = {}
    llm_n = tool_n = 0
    for span in _ordered_spans(spans_file):
        raw_attrs = span.get("attributes")
        attrs = raw_attrs if isinstance(raw_attrs, dict) else {}
        art_keys = [
            key
            for key, value in attrs.items()
            if isinstance(key, str) and key.endswith(".artifact_path") and isinstance(value, str) and value
        ]
        if not art_keys:
            continue
        if any(key.startswith("llm.") for key in art_keys):
            llm_n += 1
        if any(key.startswith("tool.") for key in art_keys):
            tool_n += 1
        for key in art_keys:
            prefix = key[: -len(".artifact_path")]
            if prefix == "llm.input":
                label = f"model call #{llm_n} input"
            elif prefix == "llm.output":
                label = f"model call #{llm_n} output"
            elif prefix.startswith("tool."):
                side = prefix.split(".", 1)[1]
                name = attrs.get("tool.name")
                named = f" ({name})" if isinstance(name, str) and name else ""
                label = f"tool call #{tool_n}{named} {side}"
            else:
                span_name = span.get("name")
                label = f"{span_name} {prefix}" if isinstance(span_name, str) and span_name else prefix
            labels.setdefault(attrs[key], label)
    return labels


def _source_label(rel: str, artifact_labels: dict[str, str]) -> str:
    if rel in artifact_labels:
        return artifact_labels[rel]
    if rel in _FIXED_LABELS:
        return _FIXED_LABELS[rel]
    if rel in _PROBLEM_LABELS:
        return _PROBLEM_LABELS[rel]
    if rel.startswith(_ENVIRONMENT_PREFIX):
        return f"environment summary ({rel[len(_ENVIRONMENT_PREFIX):]})"
    return rel


def _sources_from_occurrences(occurrences: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    by_file: dict[str, dict[str, Any]] = {}
    for occurrence in occurrences:
        entry = by_file.get(occurrence["file"])
        if entry is None:
            entry = {"label": occurrence["label"], "file": occurrence["file"], "count": 0}
            by_file[occurrence["file"]] = entry
            sources.append(entry)
        entry["count"] += 1
    return sources


def _placeholder_occurrences(root: Path, artifact_labels: dict[str, str]) -> list[dict[str, Any]]:
    occurrences: list[dict[str, Any]] = []
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = str(path.relative_to(root))
        if rel == REDACTION_METADATA_FILE:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        for line_no, line in enumerate(text.splitlines(), 1):
            start = line.find(PRIVATE_KEY_PLACEHOLDER)
            while start != -1:
                end = start + len(PRIVATE_KEY_PLACEHOLDER)
                occurrences.append(
                    {
                        "file": rel,
                        "line_no": line_no,
                        "line": line,
                        "start": start,
                        "end": end,
                        "label": _source_label(rel, artifact_labels),
                    }
                )
                start = line.find(PRIVATE_KEY_PLACEHOLDER, end)
    return occurrences


def build_review_items(reports: Sequence[RedactionReport]) -> list[ReviewItem]:
    """The adjudication list over the merged findings of ``reports``.

    Suspected items merge by token value across every report (a value either
    is a secret or is not, wherever it appears); linked marks value-containment
    pairs whose decisions must agree. The confirmed private-key item is built
    from the replacement placeholders — the original content no longer exists.
    """
    labels_by_report = [_artifact_labels(report.redacted_dir) for report in reports]

    items: list[ReviewItem] = []
    key_hits = sum(report.patterns.get(PRIVATE_KEY_CATEGORY, 0) for report in reports)
    if key_hits:
        occurrences: list[dict[str, Any]] = []
        for report, labels in zip(reports, labels_by_report):
            occurrences.extend(_placeholder_occurrences(report.redacted_dir, labels))
        items.append(
            ReviewItem(
                id=_item_id(f"pattern.{PRIVATE_KEY_CATEGORY}"),
                kind=KIND_CONFIRMED,
                category=PRIVATE_KEY_CATEGORY,
                token="",
                masked_sample=PRIVATE_KEY_PLACEHOLDER,
                sources=_sources_from_occurrences(occurrences),
                occurrences=occurrences,
            )
        )

    by_token: dict[str, ReviewItem] = {}
    order: list[str] = []
    for index, (report, labels) in enumerate(zip(reports, labels_by_report)):
        for finding in report.findings:
            occurrences = [{**occ, "label": _source_label(occ["file"], labels), "report": index} for occ in finding.occurrences]
            item = by_token.get(finding.token)
            if item is None:
                item = ReviewItem(
                    id=_item_id(finding.token),
                    kind=KIND_SUSPECTED,
                    category=finding.category,
                    token=finding.token,
                    masked_sample=finding.sample,
                    occurrences=occurrences,
                )
                by_token[finding.token] = item
                order.append(finding.token)
            else:
                item.occurrences.extend(occurrences)

    suspected = [by_token[token] for token in order]
    for item in suspected:
        item.occurrences.sort(key=lambda o: (o["report"], o["file"], o["line_no"], o["start"]))
        item.sources = _sources_from_occurrences(item.occurrences)
    suspected.sort(key=lambda i: (i.occurrences[0]["report"], i.occurrences[0]["file"], i.occurrences[0]["line_no"]))

    for a in suspected:
        for b in suspected:
            if a is not b and a.token in b.token:
                a.linked.append(b.id)
                # containment is what makes decisions collide: replacing the
                # contained value rewrites inside the containing one.
                if a.id not in b.linked:
                    b.linked.append(a.id)

    return items + suspected


def validate_review_decisions(items: Sequence[ReviewItem], decisions: Sequence[ReviewDecision]) -> None:
    """Check a decision set without touching anything; raise on any defect.

    ``ReviewConflictError`` marks the one user-fixable case (linked items
    deciding differently); every other defect is a caller bug and raises
    ``BugReportError``.
    """
    by_id = {item.id: item for item in items}
    seen: dict[str, str] = {}
    for decision in decisions:
        item = by_id.get(decision.item_id)
        if item is None:
            raise BugReportError(f"decision for unknown review item {decision.item_id!r}")
        if decision.item_id in seen:
            raise BugReportError(f"duplicate decision for review item {decision.item_id!r}")
        allowed = (ACTION_ACKNOWLEDGED,) if item.kind == KIND_CONFIRMED else (ACTION_KEPT, ACTION_REDACTED)
        if decision.action not in allowed:
            raise BugReportError(f"invalid action {decision.action!r} for review item {decision.item_id!r}")
        seen[decision.item_id] = decision.action
    missing = [item.id for item in items if item.id not in seen]
    if missing:
        raise BugReportError(f"missing decision(s) for review item(s): {', '.join(missing)}")

    for item in items:
        for other_id in item.linked:
            if seen[item.id] != seen[other_id]:
                raise ReviewConflictError(
                    f"linked review items {item.id} and {other_id} have overlapping values"
                    " and must share one decision (keep both or replace both)"
                )


def _token_pattern(token: str) -> re.Pattern[str]:
    variants = sorted({v for v in _variants(token) if v}, key=len, reverse=True)
    return re.compile("|".join(re.escape(v) for v in variants))


def _body_files(reports: Sequence[RedactionReport]) -> list[tuple[Path, str]]:
    files: list[tuple[Path, str]] = []
    for report in reports:
        root = report.redacted_dir
        for path in sorted(p for p in root.rglob("*") if p.is_file()):
            rel = str(path.relative_to(root))
            if rel != REDACTION_METADATA_FILE:
                files.append((path, rel))
    return files


def _write_metadata(report: RedactionReport) -> None:
    (report.redacted_dir / REDACTION_METADATA_FILE).write_text(
        json.dumps(report.metadata(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def apply_review_decisions(
    items: Sequence[ReviewItem],
    decisions: Sequence[ReviewDecision],
    *,
    reports: Sequence[RedactionReport],
) -> ReviewOutcome:
    """Rewrite the redacted trees per ``decisions`` and prove the result.

    Order is load-bearing: the kept-token baseline is counted before any
    change (finding counts use a different scan scope and would misfire);
    body text is replaced before the summaries are rewritten (a summary built
    from unfiltered findings would reintroduce replaced plaintext); and every
    string headed for serialization is filtered through the user-confirmed
    replacements. Verification failures raise ``PreparationError`` — a
    decision record that does not match the delivered bytes must never ship.
    """
    validate_review_decisions(items, decisions)
    actions = {decision.item_id: decision.action for decision in decisions}

    redacted_tokens = [item.token for item in items if actions[item.id] == ACTION_REDACTED]
    kept_tokens = [item.token for item in items if actions[item.id] == ACTION_KEPT]
    user_secrets = [KnownSecret(USER_CONFIRMED_LABEL, token) for token in redacted_tokens]

    files = _body_files(reports)
    kept_patterns = {token: _token_pattern(token) for token in kept_tokens}
    baseline: dict[str, int] = {token: 0 for token in kept_tokens}
    for path, _rel in files:
        text = path.read_text(encoding="utf-8")
        for token, pattern in kept_patterns.items():
            baseline[token] += sum(1 for _ in pattern.finditer(text))

    if user_secrets:
        for report in reports:
            counts: dict[str, int] = {}
            root = report.redacted_dir
            for path, _rel in files:
                if root not in path.parents:
                    continue
                text = path.read_text(encoding="utf-8")
                replaced = _apply_exact(text, user_secrets, counts)
                if replaced != text:
                    path.write_text(replaced, encoding="utf-8")
            if counts.get(USER_CONFIRMED_LABEL):
                report.exact[USER_CONFIRMED_LABEL] = (
                    report.exact.get(USER_CONFIRMED_LABEL, 0) + counts[USER_CONFIRMED_LABEL]
                )

    def _filter(text: str) -> str:
        return _apply_exact(text, user_secrets, {}) if user_secrets else text

    redacted_set = set(redacted_tokens)
    for report in reports:
        pruned: list[ResidualFinding] = []
        for finding in report.findings:
            if finding.token in redacted_set:
                continue
            finding.sample = _filter(finding.sample)
            pruned.append(finding)
        report.findings = pruned
        _write_metadata(report)

    summary_files = [report.redacted_dir / REDACTION_METADATA_FILE for report in reports]
    for token in redacted_tokens:
        for variant in _variants(token):
            if not variant:
                continue
            for path, rel in files:
                if variant in path.read_text(encoding="utf-8"):
                    raise PreparationError(f"user-redacted content survived in the export: {rel}")
            for path in summary_files:
                if variant in path.read_text(encoding="utf-8"):
                    raise PreparationError(
                        f"user-redacted content survived in the export: {path.name}"
                    )
    for token, pattern in kept_patterns.items():
        count = 0
        for path, _rel in files:
            count += sum(1 for _ in pattern.finditer(path.read_text(encoding="utf-8")))
        if count != baseline[token]:
            raise PreparationError(
                f"a kept token changed during review application ({baseline[token]} -> {count} occurrence(s))"
            )

    key_hits = sum(report.patterns.get(PRIVATE_KEY_CATEGORY, 0) for report in reports)
    notices = (
        [f"the original trajectory contained {key_hits} private key block(s), replaced before export"]
        if key_hits
        else []
    )

    user_decisions = [
        {
            "id": item.id,
            "category": item.category,
            "sources": [
                {"source": _filter(source["label"]), "count": source["count"]} for source in item.sources
            ],
            "masked_sample": _filter(item.masked_sample),
            "action": actions[item.id],
        }
        for item in items
    ]

    return ReviewOutcome(
        user_secrets=user_secrets,
        user_decisions=user_decisions,
        security_notices=[_filter(notice) for notice in notices],
    )


__all__ = [
    "ACTION_ACKNOWLEDGED",
    "ACTION_KEPT",
    "ACTION_REDACTED",
    "KIND_CONFIRMED",
    "KIND_SUSPECTED",
    "PRIVATE_KEY_PLACEHOLDER",
    "ReviewCancelledError",
    "ReviewConflictError",
    "ReviewDecision",
    "ReviewItem",
    "ReviewOutcome",
    "apply_review_decisions",
    "build_review_items",
    "validate_review_decisions",
]
