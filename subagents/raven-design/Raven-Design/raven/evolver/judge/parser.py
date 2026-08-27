"""Parse LLM-judge raw text output into a validated ``JudgeResult``.

The judge is instructed to emit a single JSON object (spec §3.2 schema +
spec §12.4-§12.5 enums), but real LLMs occasionally wrap it in markdown
code fences, add leading "Here is the analysis:" prose, or append a
short summary after the JSON. The parser tolerates these defects:

- Strips ```json / ``` code fences (any language tag).
- Locates the first ``{`` and matches to the final balanced ``}``.
- Validates enum values against ``IssueType`` / ``PatchWhere`` / ``PatchWhy``.
- Enforces the L1/L2/L3 ↔ action-kind cross-field invariants by handing
  off to ``JudgeResult.__post_init__``.

What it does NOT tolerate:

- Truncated JSON (no closing brace).
- Multiple top-level JSON objects.
- Missing required fields.
- Invalid enum strings (raises with the offender's name).

Errors are raised as ``JudgeParseError`` with a short reason; callers
typically log + skip the offending trajectory, or feed it back to the
judge as a "retry, fix this" prompt.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from .schema import (
    ActionKind,
    IssueType,
    JudgeAction,
    JudgeResult,
    PassFailResult,
    PatchWhere,
    PatchWhy,
    ProposedComponent,
)


class JudgeParseError(ValueError):
    """Raised when LLM judge output cannot be parsed into a JudgeResult."""


# ---------------------------------------------------------------------------
# Text → JSON dict
# ---------------------------------------------------------------------------


_CODE_FENCE_RE = re.compile(r"^\s*```(?:json|JSON|jsonc)?\s*\n?|\n?\s*```\s*$", re.MULTILINE)


def _extract_json_object(raw: str) -> str:
    """Locate the first balanced ``{...}`` block in ``raw``.

    Strategy:
    1. Strip Markdown code fences.
    2. Find the first ``{``.
    3. Walk forward tracking brace depth (string-aware: ignores braces
       inside double-quoted strings, respecting backslash escapes).
    4. Return the substring [first_brace, matching_close_brace+1].

    Raises ``JudgeParseError`` if no ``{`` is found or braces never
    balance.

    This is more robust than a naive ``raw[raw.find('{'):raw.rfind('}')+1]``
    which fails when the LLM appends another ``{...}`` snippet after its
    main JSON (e.g. "Note: see {related_file} for context.").
    """
    stripped = _CODE_FENCE_RE.sub("", raw).strip()
    start = stripped.find("{")
    if start < 0:
        raise JudgeParseError("no '{' found in judge output")

    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(stripped)):
        ch = stripped[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return stripped[start : i + 1]
    raise JudgeParseError("braces never balance — JSON likely truncated")


def _require(d: dict[str, Any], key: str, what: str) -> Any:
    if key not in d:
        raise JudgeParseError(f"missing required field '{key}' in {what}")
    return d[key]


def _coerce_enum(value: Any, enum_cls: type, field_name: str) -> Any:
    """Coerce a string into an Enum, raising helpfully on miss.

    Accepts the enum value's ``value`` (string form). Rejects ints,
    enum member ``.name``, or any other shape.
    """
    if not isinstance(value, str):
        raise JudgeParseError(f"field '{field_name}' must be a string, got {type(value).__name__}")
    try:
        return enum_cls(value)
    except ValueError as exc:
        valid = [m.value for m in enum_cls]
        raise JudgeParseError(f"field '{field_name}'={value!r} not one of {valid}") from exc


def _parse_components(action_obj: dict[str, Any]) -> list[ProposedComponent]:
    """Parse the ``components`` array from a patch_proposal action.

    Backwards-compat: if the LLM emits a flat ``target_file`` +
    ``patch_summary`` pair instead of (or in addition to) ``components``,
    synthesize a single component. This keeps already-trained judge
    prompts working while we roll out the multi-component schema.
    """
    raw_components = action_obj.get("components")

    # Path A: new schema — explicit components list
    if raw_components is not None:
        if not isinstance(raw_components, list):
            raise JudgeParseError(f"proposed_action.components must be a list, got {type(raw_components).__name__}")
        if not raw_components:
            raise JudgeParseError("proposed_action.components must be non-empty for patch_proposal")
        parsed: list[ProposedComponent] = []
        for i, item in enumerate(raw_components):
            if not isinstance(item, dict):
                raise JudgeParseError(f"proposed_action.components[{i}] must be an object, got {type(item).__name__}")
            component_id = item.get("component_id") or f"comp_{i + 1}"
            if not isinstance(component_id, str) or not component_id.strip():
                raise JudgeParseError(f"proposed_action.components[{i}].component_id must be a non-empty string")
            target_file = item.get("target_file")
            if not isinstance(target_file, str) or not target_file.strip():
                raise JudgeParseError(f"proposed_action.components[{i}].target_file must be a non-empty string")
            summary = item.get("summary")
            if not isinstance(summary, str) or not summary.strip():
                raise JudgeParseError(f"proposed_action.components[{i}].summary must be a non-empty string")
            depends_on_raw = item.get("depends_on") or []
            if not isinstance(depends_on_raw, list) or not all(isinstance(x, str) for x in depends_on_raw):
                raise JudgeParseError(f"proposed_action.components[{i}].depends_on must be a list of strings")
            parsed.append(
                ProposedComponent(
                    component_id=component_id,
                    target_file=target_file,
                    summary=summary,
                    depends_on=list(depends_on_raw),
                )
            )
        return parsed

    # Path B: legacy flat schema — synthesize a single component
    target_file = action_obj.get("target_file")
    patch_summary = action_obj.get("patch_summary")
    if target_file is None or patch_summary is None:
        raise JudgeParseError(
            "proposed_action must contain either 'components' (preferred) or "
            "the legacy 'target_file' + 'patch_summary' pair"
        )
    if not isinstance(target_file, str) or not target_file.strip():
        raise JudgeParseError("proposed_action.target_file must be a non-empty string")
    if not isinstance(patch_summary, str) or not patch_summary.strip():
        raise JudgeParseError("proposed_action.patch_summary must be a non-empty string")
    return [
        ProposedComponent(
            component_id="comp_1",
            target_file=target_file,
            summary=patch_summary,
            depends_on=[],
        ),
    ]


def _parse_evidence_range(raw: Any) -> Optional[tuple[int, int]]:
    """Validate the [start, end] turn-range list."""
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)):
        raise JudgeParseError(f"evidence_turn_range must be a list/tuple, got {type(raw).__name__}")
    if len(raw) != 2:
        raise JudgeParseError(f"evidence_turn_range must have exactly 2 elements, got {len(raw)}")
    a, b = raw
    if not (isinstance(a, int) and isinstance(b, int)):
        raise JudgeParseError(f"evidence_turn_range elements must be ints, got {a!r}, {b!r}")
    if a > b:
        raise JudgeParseError(f"evidence_turn_range start ({a}) > end ({b})")
    return (a, b)


# ---------------------------------------------------------------------------
# Top-level parser
# ---------------------------------------------------------------------------


def parse_judge_output(
    raw_text: str,
    *,
    expected_trajectory_id: Optional[str] = None,
) -> JudgeResult:
    """Parse one judge response into a validated ``JudgeResult``.

    ``expected_trajectory_id`` (optional): if provided, the parsed
    ``trajectory_id`` field must match — protects against the judge
    silently mis-binding output to the wrong trajectory in a batch
    pipeline.

    The original ``raw_text`` is stored on ``JudgeResult.raw_response``
    for audit (so post-hoc inspection of misclassified trajectories
    can see exactly what the LLM said).
    """
    json_text = _extract_json_object(raw_text)
    try:
        obj = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise JudgeParseError(f"JSON decode failed: {exc}") from exc

    if not isinstance(obj, dict):
        raise JudgeParseError(f"top-level must be a JSON object, got {type(obj).__name__}")

    trajectory_id = _require(obj, "trajectory_id", "judge output")
    if not isinstance(trajectory_id, str):
        raise JudgeParseError("trajectory_id must be a string")
    if expected_trajectory_id is not None:
        # Judges occasionally echo the trajectory_id back with a wrapper-
        # path suffix copied from the task description, e.g.
        #   expected: 'swe-vanilla-500/django__django-11292'
        #   got:      'swe-vanilla-500/django__django-11292-t1-exec'
        # (the '-t1-exec' came from WRAPPER_PATH in the user message).
        # Accept the parsed id if it starts with the expected id so the
        # batch doesn't lose otherwise-valid records over a cosmetic
        # suffix. Strict mismatch (different task entirely) still raises.
        if trajectory_id != expected_trajectory_id and not trajectory_id.startswith(expected_trajectory_id):
            raise JudgeParseError(f"trajectory_id mismatch: expected {expected_trajectory_id!r}, got {trajectory_id!r}")

    issue_type = _coerce_enum(_require(obj, "issue_type", "judge output"), IssueType, "issue_type")
    confidence = _require(obj, "confidence", "judge output")
    if not isinstance(confidence, (int, float)):
        raise JudgeParseError(f"confidence must be a number, got {type(confidence).__name__}")
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise JudgeParseError(f"confidence must be in [0.0, 1.0], got {confidence}")

    signal_description = _require(obj, "signal_description", "judge output")
    if not isinstance(signal_description, str):
        raise JudgeParseError("signal_description must be a string")

    evidence_turn_range = _parse_evidence_range(obj.get("evidence_turn_range"))

    action_obj = _require(obj, "proposed_action", "judge output")
    if not isinstance(action_obj, dict):
        raise JudgeParseError("proposed_action must be an object")

    kind = _coerce_enum(_require(action_obj, "kind", "proposed_action"), ActionKind, "proposed_action.kind")
    reasoning = _require(action_obj, "reasoning", "proposed_action")
    if not isinstance(reasoning, str):
        raise JudgeParseError("proposed_action.reasoning must be a string")

    # Patch-fields: required iff kind=patch_proposal.
    patch_where = None
    patch_why = None
    patch_why_extra = None
    components: list[ProposedComponent] = []

    if kind == ActionKind.patch_proposal:
        patch_where = _coerce_enum(
            _require(action_obj, "patch_where", "proposed_action(patch_proposal)"),
            PatchWhere,
            "proposed_action.patch_where",
        )
        patch_why = _coerce_enum(
            _require(action_obj, "patch_why", "proposed_action(patch_proposal)"),
            PatchWhy,
            "proposed_action.patch_why",
        )
        components = _parse_components(action_obj)
        # patch_why=other requires a sub-name
        patch_why_extra_raw = action_obj.get("patch_why_extra")
        if patch_why == PatchWhy.other:
            if not patch_why_extra_raw or not isinstance(patch_why_extra_raw, str):
                raise JudgeParseError("patch_why='other' requires non-empty patch_why_extra string")
            patch_why_extra = patch_why_extra_raw

    try:
        action = JudgeAction(
            kind=kind,
            reasoning=reasoning,
            patch_where=patch_where,
            patch_why=patch_why,
            patch_why_extra=patch_why_extra,
            components=components,
        )
    except ValueError as exc:
        raise JudgeParseError(str(exc)) from exc

    # JudgeResult.__post_init__ enforces the cross-field invariants.
    return JudgeResult(
        trajectory_id=trajectory_id,
        issue_type=issue_type,
        confidence=confidence,
        signal_description=signal_description,
        proposed_action=action,
        evidence_turn_range=evidence_turn_range,
        raw_response=raw_text,
    )


def parse_pass_fail(raw: str, *, expected_trajectory_id: str = "") -> PassFailResult:
    """Parse a no-benchmark pass/fail verdict; raise on any defect (for retry).

    Reuses the judge's tolerant JSON extractor so the same ``SemanticNode``
    repair loop applies. Requires a boolean-ish ``passed`` field.
    """
    obj = json.loads(_extract_json_object(raw))
    if "passed" not in obj:
        raise JudgeParseError("pass/fail verdict missing required 'passed' field")
    passed = obj["passed"]
    if isinstance(passed, str):
        passed = passed.strip().lower() in ("true", "pass", "passed", "yes", "1")
    return PassFailResult(
        trajectory_id=str(obj.get("trajectory_id", expected_trajectory_id)),
        passed=bool(passed),
        reasoning=str(obj.get("reasoning", "")),
        raw_response=raw,
    )


__all__ = [
    "JudgeParseError",
    "parse_judge_output",
    "parse_pass_fail",
]
