"""Layer J: a judge model decides each acceptance criterion of each case.

The judge sees what the host sees -- the final reply and the delivered files -- never
the agent's streamed transcript. Its answer must cover every criterion id exactly
once; a malformed answer gets one retry and then fails the case as a judge error.
With several votes the per-criterion verdict is the majority, and a tie fails. A
case passes when no criterion fails and at least one passes.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from evolver.validator.report import CaseRun, CaseVerdict, CriterionVerdict, LayerResult, RuleResult
from evolver.validator.suite import Case

PROMPT_PATH = Path(__file__).with_name("judge_prompt.md")
INPUT_TEXT_CAP = 8192
VERDICTS = ("pass", "fail", "not_applicable")


class JudgeFormatError(ValueError):
    """The judge's answer does not have the required shape."""


def _input_section(case: Case) -> str:
    if not case.files:
        return "(none)"
    parts = []
    for rel, data in sorted(case.files.items()):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            parts.append(f"- {rel} ({len(data)} bytes, binary)")
            continue
        shown = text[:INPUT_TEXT_CAP] + ("\n[truncated]" if len(text) > INPUT_TEXT_CAP else "")
        parts.append(f"- {rel} ({len(data)} bytes)\n```\n{shown}\n```")
    return "\n".join(parts)


def _delivered_section(run: CaseRun) -> str:
    if not run.files:
        return "(none)"
    parts = []
    for record in run.files:
        if record.text is None:
            parts.append(f"## {record.path} ({record.size} bytes, binary, sha256 {record.sha256})")
            continue
        marker = "\n[truncated]" if record.truncated else ""
        parts.append(f"## {record.path} ({record.size} bytes)\n```\n{record.text}{marker}\n```")
    return "\n".join(parts)


def build_judge_messages(case: Case, run: CaseRun) -> list[dict[str, Any]]:
    criteria = "\n".join(f"- {c.id}: {c.statement}" for c in case.criteria)
    user = (
        f"# Task\n{case.task}\n\n"
        f"# Input files\n{_input_section(case)}\n\n"
        f"# Reference notes\n{case.reference or '(none)'}\n\n"
        f"# Criteria\n{criteria}\n\n"
        f"# Final reply\n{run.reply}\n\n"
        f"# Delivered files\n{_delivered_section(run)}\n"
    )
    return [
        {"role": "system", "content": PROMPT_PATH.read_text(encoding="utf-8")},
        {"role": "user", "content": user},
    ]


def _first_object(text: str) -> dict:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            value, _end = decoder.raw_decode(text, index)
        except ValueError:
            continue
        if isinstance(value, dict) and "criteria" in value:
            return value
    raise JudgeFormatError("no JSON object with 'criteria' in the judge's answer")


def parse_judgement(text: str, criterion_ids: list[str]) -> list[CriterionVerdict]:
    rows = _first_object(text or "").get("criteria")
    if not isinstance(rows, list):
        raise JudgeFormatError("'criteria' is not a list")
    found: dict[str, CriterionVerdict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise JudgeFormatError("a criterion verdict is not an object")
        cid, verdict = row.get("id"), row.get("verdict")
        if cid not in criterion_ids:
            raise JudgeFormatError(f"unknown criterion id {cid!r}")
        if cid in found:
            raise JudgeFormatError(f"criterion {cid!r} judged twice")
        if verdict not in VERDICTS:
            raise JudgeFormatError(f"criterion {cid!r} has verdict {verdict!r}")
        found[cid] = CriterionVerdict(cid, verdict, str(row.get("reason") or ""))
    missing = [cid for cid in criterion_ids if cid not in found]
    if missing:
        raise JudgeFormatError(f"criteria not judged: {', '.join(missing)}")
    return [found[cid] for cid in criterion_ids]


def combine(votes: list[list[CriterionVerdict]]) -> list[CriterionVerdict]:
    combined = []
    for column in zip(*votes, strict=True):
        counts = Counter(v.verdict for v in column)
        ranked = counts.most_common()
        verdict = ranked[0][0]
        if len(ranked) > 1 and ranked[1][1] == ranked[0][1]:
            verdict = "fail"
        reason = next((v.reason for v in column if v.verdict == verdict), column[0].reason)
        combined.append(CriterionVerdict(column[0].id, verdict, reason))
    return combined


async def _one_vote(provider: Any, model: str, messages: list[dict], ids: list[str], raw: list[str]):
    error: JudgeFormatError | None = None
    for _attempt in range(2):
        response = await provider.chat_with_retry(messages, model=model, temperature=0.0)
        text = getattr(response, "content", None) or ""
        raw.append(text)
        try:
            return parse_judgement(text, ids)
        except JudgeFormatError as exc:
            error = exc
    raise error or JudgeFormatError("no answer")


async def judge_case(provider: Any, *, model: str, case: Case, run: CaseRun, repeats: int) -> CaseVerdict:
    messages = build_judge_messages(case, run)
    ids = [c.id for c in case.criteria]
    raw: list[str] = []
    votes = []
    for _vote in range(max(1, repeats)):
        try:
            votes.append(await _one_vote(provider, model, messages, ids, raw))
        except JudgeFormatError as exc:
            return CaseVerdict(case.id, False, [], raw, f"judge answer malformed after a retry: {exc}")
    criteria = combine(votes)
    verdicts = {c.verdict for c in criteria}
    if "fail" in verdicts:
        return CaseVerdict(case.id, False, criteria, raw, None)
    if "pass" not in verdicts:
        return CaseVerdict(case.id, False, criteria, raw, "no applicable criterion")
    return CaseVerdict(case.id, True, criteria, raw, None)


def judge_layer(verdicts: list[CaseVerdict], total: int, *, min_pass_rate: float) -> LayerResult:
    passed = sum(1 for v in verdicts if v.passed)
    rate = passed / total if total else 0.0
    status = "pass" if total and rate >= min_pass_rate else "fail"
    return LayerResult("J", [RuleResult("J1", status, f"{passed}/{total} cases passed (required {min_pass_rate:.2f})")])
