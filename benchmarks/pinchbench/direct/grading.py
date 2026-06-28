"""
PinchBench Grading Engine — adapted for raven.

Supports automated (Python code), LLM judge, and hybrid grading.
LLM judge uses Raven's own provider (OpenRouter) instead of OpenClaw.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Dict, List

from task_loader import Task

logger = logging.getLogger(__name__)

DEFAULT_JUDGE_MODEL = "anthropic/claude-sonnet-4.6"


@dataclass
class GradeResult:
    task_id: str
    score: float
    max_score: float
    grading_type: str
    breakdown: Dict[str, float]
    notes: str

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "score": self.score,
            "max_score": self.max_score,
            "grading_type": self.grading_type,
            "breakdown": self.breakdown,
            "notes": self.notes,
        }


def grade_task(
    *,
    task: Task,
    execution_result: Dict[str, Any],
    judge_model: str = DEFAULT_JUDGE_MODEL,
    judge_api_key: str = "",
    verbose: bool = False,
) -> GradeResult:
    """Grade a task using the appropriate method."""
    grading_type = task.grading_type

    if grading_type == "automated":
        return _grade_automated(task, execution_result, verbose=verbose)

    if grading_type == "llm_judge":
        return _grade_llm_judge(
            task=task,
            execution_result=execution_result,
            judge_model=judge_model,
            judge_api_key=judge_api_key,
            verbose=verbose,
        )

    if grading_type == "hybrid":
        auto_result = _grade_automated(task, execution_result, verbose=verbose)
        llm_result = _grade_llm_judge(
            task=task,
            execution_result=execution_result,
            judge_model=judge_model,
            judge_api_key=judge_api_key,
            verbose=verbose,
        )
        return _combine_grades(task, auto_result, llm_result)

    raise ValueError(f"Unknown grading type: {grading_type}")


# ---------------------------------------------------------------------------
# Automated grading
# ---------------------------------------------------------------------------

def _grade_automated(
    task: Task, execution_result: Dict[str, Any], verbose: bool = False
) -> GradeResult:
    grading_code = _extract_grading_code(task)
    if not grading_code:
        return GradeResult(
            task_id=task.task_id, score=0.0, max_score=1.0,
            grading_type="automated", breakdown={},
            notes="No automated grading code found",
        )

    namespace: Dict[str, Any] = {}
    exec(grading_code, namespace)
    grade_func = namespace.get("grade")
    if not callable(grade_func):
        return GradeResult(
            task_id=task.task_id, score=0.0, max_score=1.0,
            grading_type="automated", breakdown={},
            notes="Automated grading function missing",
        )

    scores = grade_func(
        execution_result.get("transcript", []),
        execution_result.get("workspace", ""),
    )
    if not isinstance(scores, dict):
        scores = {}

    if verbose:
        logger.info("  Automated scores: %s", scores)

    total = _average_scores(scores)
    return GradeResult(
        task_id=task.task_id, score=total, max_score=1.0,
        grading_type="automated",
        breakdown=_normalize_score_dict(scores),
        notes="",
    )


# ---------------------------------------------------------------------------
# LLM judge grading — calls OpenRouter directly via litellm
# ---------------------------------------------------------------------------

def _grade_llm_judge(
    *,
    task: Task,
    execution_result: Dict[str, Any],
    judge_model: str,
    judge_api_key: str,
    verbose: bool = False,
) -> GradeResult:
    transcript_summary = _summarize_transcript(execution_result.get("transcript", []))
    rubric = task.llm_judge_rubric or _format_grading_criteria(task)
    prompt = _build_judge_prompt(task, transcript_summary, rubric)

    if verbose:
        logger.info("  Judge prompt (first 500 chars): %s", prompt[:500])

    # Call LLM judge via litellm
    try:
        import os

        import litellm
        os.environ.setdefault("OPENROUTER_API_KEY", judge_api_key)

        response = litellm.completion(
            model=f"openrouter/{judge_model}",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.1,
            max_tokens=2000,
            api_key=judge_api_key,
            api_base="https://openrouter.ai/api/v1",
        )
        judge_text = response.choices[0].message.content or ""
    except Exception as exc:
        logger.error("LLM judge failed: %s", exc)
        return GradeResult(
            task_id=task.task_id, score=0.0, max_score=1.0,
            grading_type="llm_judge", breakdown={},
            notes=f"Judge error: {exc}",
        )

    parsed = _parse_judge_response_text(judge_text)
    normalized = _normalize_judge_response(parsed)

    if verbose:
        logger.info("  Judge raw: %s", parsed)
        logger.info("  Judge normalized: %s", normalized)

    breakdown = normalized.get("scores", {})
    total = normalized.get("total")
    notes = normalized.get("notes", "")

    return GradeResult(
        task_id=task.task_id,
        score=float(total) if total is not None else 0.0,
        max_score=1.0,
        grading_type="llm_judge",
        breakdown=_normalize_score_dict(breakdown),
        notes=str(notes) if notes else "",
    )


# ---------------------------------------------------------------------------
# Hybrid grading
# ---------------------------------------------------------------------------

def _combine_grades(
    task: Task, auto_result: GradeResult, llm_result: GradeResult
) -> GradeResult:
    weights = task.grading_weights or {"automated": 0.5, "llm_judge": 0.5}
    aw = float(weights.get("automated", 0.5))
    lw = float(weights.get("llm_judge", 0.5))
    total_w = aw + lw or 1.0
    combined = (auto_result.score * aw + llm_result.score * lw) / total_w
    breakdown = {
        **{f"automated.{k}": v for k, v in auto_result.breakdown.items()},
        **{f"llm_judge.{k}": v for k, v in llm_result.breakdown.items()},
    }
    notes = " | ".join(filter(None, [auto_result.notes, llm_result.notes]))
    return GradeResult(
        task_id=task.task_id, score=combined, max_score=1.0,
        grading_type="hybrid", breakdown=breakdown, notes=notes,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_grading_code(task: Task) -> str:
    if not task.automated_checks:
        return ""
    match = re.search(r"```python\s*(.*?)\s*```", task.automated_checks, re.DOTALL)
    return match.group(1) if match else ""


def _average_scores(scores: Dict[str, Any]) -> float:
    values = [float(v) for v in scores.values() if isinstance(v, (int, float))]
    return sum(values) / len(values) if values else 0.0


def _normalize_score_dict(scores: Dict[str, Any]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for k, v in scores.items():
        try:
            out[str(k)] = float(v)
        except (TypeError, ValueError):
            pass
    return out


def _format_grading_criteria(task: Task) -> str:
    return "\n".join(f"- {c}" for c in task.grading_criteria) if task.grading_criteria else ""


def _summarize_transcript(transcript: List[Dict[str, Any]]) -> str:
    parts: List[str] = []
    for event in transcript:
        if event.get("type") != "message":
            continue
        msg = event.get("message", {})
        role = msg.get("role")
        if role == "assistant":
            for item in msg.get("content", []):
                if item.get("type") == "toolCall":
                    parts.append(
                        f"Tool: {item.get('name')}({json.dumps(item.get('arguments', {}))})"
                    )
                elif item.get("type") == "text":
                    text = item.get("text", "")
                    if text:
                        parts.append(f"Assistant: {text[:300]}")
        elif role == "toolResult":
            content = msg.get("content", [])
            if content:
                parts.append(f"Result: {str(content[0])[:200]}")
        elif role == "user":
            content = msg.get("content", [])
            if content:
                parts.append(f"User: {str(content[0])[:200]}")
    return "\n".join(parts)


def _build_judge_prompt(task: Task, transcript_summary: str, rubric: str) -> str:
    return (
        "You are a grading function. Your ONLY job is to output a single JSON object.\n\n"
        "CRITICAL RULES:\n"
        "- Do NOT use any tools\n"
        "- Respond with ONLY a JSON object — nothing else\n\n"
        "Be a strict evaluator. Reserve 1.0 for genuinely excellent performance. "
        "An average acceptable completion should score around 0.6-0.7.\n\n"
        f"## Task\n{task.prompt}\n\n"
        f"## Expected Behavior\n{task.expected_behavior}\n\n"
        f"## Agent Transcript (summarized)\n{transcript_summary}\n\n"
        f"## Grading Rubric\n{rubric}\n\n"
        "Score each criterion from 0.0 to 1.0.\n\n"
        "Respond with ONLY this JSON structure (no markdown, no code fences, no extra text):\n"
        '{"scores": {"criterion_name": 0.0}, "total": 0.0, "notes": "brief justification"}'
    )


def _parse_judge_response_text(raw_text: str) -> Dict[str, Any]:
    if not raw_text:
        return {}

    # Try code block
    m = re.search(r"```json\s*(.*?)\s*```", raw_text, re.DOTALL)
    if m:
        try:
            parsed = json.loads(m.group(1))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    # Extract JSON objects by balanced braces
    candidates: List[str] = []
    depth = 0
    current: List[str] = []
    for ch in raw_text:
        if ch == "{":
            if depth == 0:
                current = []
            depth += 1
        if depth > 0:
            current.append(ch)
        if ch == "}":
            depth -= 1
            if depth == 0 and current:
                candidates.append("".join(current))

    for c in reversed(candidates):
        try:
            parsed = json.loads(c)
            if isinstance(parsed, dict) and "scores" in parsed:
                return parsed
        except json.JSONDecodeError:
            continue

    for c in reversed(candidates):
        try:
            parsed = json.loads(c)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue

    # Fallback: regex for total score
    sm = re.search(
        r"(?:total|overall|final)\s*(?:score)?[:\s]*(0\.\d+|1\.0+)",
        raw_text, re.IGNORECASE,
    )
    if sm:
        try:
            total = float(sm.group(1))
            if 0.0 <= total <= 1.0:
                return {"scores": {}, "total": total, "notes": "Extracted from prose"}
        except ValueError:
            pass

    return {}


def _normalize_judge_response(parsed: Dict[str, Any]) -> Dict[str, Any]:
    result: Dict[str, Any] = {"scores": {}, "total": None, "notes": ""}

    if "scores" in parsed:
        sd = parsed["scores"]
        if isinstance(sd, dict):
            for k, v in sd.items():
                if isinstance(v, dict) and "score" in v:
                    result["scores"][k] = float(v["score"])
                elif isinstance(v, (int, float)):
                    result["scores"][k] = v
    elif "criteria_scores" in parsed:
        sd = parsed["criteria_scores"]
        if isinstance(sd, dict):
            for k, v in sd.items():
                if isinstance(v, dict) and "score" in v:
                    result["scores"][k] = v["score"]
                elif isinstance(v, (int, float)):
                    result["scores"][k] = v

    if "total" in parsed and parsed["total"] is not None:
        result["total"] = float(parsed["total"])
    elif "score" in parsed and isinstance(parsed["score"], (int, float)):
        result["total"] = float(parsed["score"])
    elif result["scores"]:
        values = [v for v in result["scores"].values() if isinstance(v, (int, float))]
        if values:
            result["total"] = sum(values) / len(values)

    if "notes" in parsed:
        result["notes"] = str(parsed["notes"])
    elif "justification" in parsed:
        result["notes"] = str(parsed["justification"])

    return result
