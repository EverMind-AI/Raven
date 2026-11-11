"""Judge layer (J) of the agent package validator, against a scripted provider."""

from __future__ import annotations

import asyncio
import json

import pytest

from evolver.validator.judge import JudgeFormatError, build_judge_messages, judge_case, judge_layer, parse_judgement
from evolver.validator.report import CaseRun, CaseVerdict, CriterionVerdict, FileRecord
from evolver.validator.suite import Case, Criterion
from raven.contracts.llm_provider import LLMResponse

CASE = Case(
    id="hello",
    task="Create hello.txt containing exactly 'hello validator'.",
    files={"brief.md": b"Write politely.", "logo.bin": b"\xff\x00"},
    criteria=(
        Criterion("exists", "hello.txt exists."),
        Criterion("exact", "Its content is exactly 'hello validator'."),
    ),
    reference="The file content has no trailing newline.",
    timeout_s=60,
)
RUN = CaseRun(
    case_id="hello",
    reply="Created hello.txt.",
    full_text="Let me think about files... Created hello.txt.",
    files=[FileRecord("hello.txt", 15, "0" * 64, "hello validator", False)],
    seconds=3.0,
    error=None,
)


class ScriptedProvider:
    def __init__(self, *replies: str):
        self.replies = list(replies)
        self.calls: list[dict] = []

    async def chat_with_retry(self, messages, tools=None, model=None, temperature=None, **kwargs):
        self.calls.append({"messages": messages, "model": model, "temperature": temperature})
        return LLMResponse(content=self.replies.pop(0))


def _verdict(**verdicts: str) -> str:
    return json.dumps({"criteria": [{"id": k, "verdict": v, "reason": "r"} for k, v in verdicts.items()]})


def _judge(provider, repeats: int = 1) -> CaseVerdict:
    return asyncio.run(judge_case(provider, model="judge-m", case=CASE, run=RUN, repeats=repeats))


def test_prompt_contains_task_criteria_reply_and_files_not_transcript():
    messages = build_judge_messages(CASE, RUN)
    user = messages[-1]["content"]
    assert messages[0]["role"] == "system" and "JSON" in messages[0]["content"]
    for text in (
        CASE.task,
        "exists",
        "Its content is exactly",
        CASE.reference,
        RUN.reply,
        "hello validator",
        "Write politely.",
        "logo.bin",
    ):
        assert text in user
    assert "Let me think about files" not in user


def test_truncated_delivery_is_marked():
    run = CaseRun("hello", "ok", "", [FileRecord("big.txt", 70000, "1" * 64, "x" * 10, True)], 1.0, None)
    assert "[truncated]" in build_judge_messages(CASE, run)[-1]["content"]


def test_all_pass_case_passes():
    provider = ScriptedProvider(_verdict(exists="pass", exact="pass"))
    verdict = _judge(provider)
    assert verdict.passed and verdict.error is None
    assert [c.id for c in verdict.criteria] == ["exists", "exact"]
    assert provider.calls[0]["temperature"] == 0.0 and provider.calls[0]["model"] == "judge-m"


def test_parse_accepts_prose_around_the_object():
    text = "Here is my verdict:\n" + _verdict(exists="pass", exact="fail") + "\nDone."
    assert [v.verdict for v in parse_judgement(text, ["exists", "exact"])] == ["pass", "fail"]


def test_missing_criterion_id_retries_then_errors():
    provider = ScriptedProvider(_verdict(exists="pass"), _verdict(exists="pass"))
    verdict = _judge(provider)
    assert not verdict.passed and verdict.error and "exact" in verdict.error
    assert len(provider.calls) == 2 and len(verdict.raw) == 2


def test_retry_recovers_from_one_malformed_reply():
    provider = ScriptedProvider("not json at all", _verdict(exists="pass", exact="pass"))
    assert _judge(provider).passed


@pytest.mark.parametrize(
    "reply",
    [
        json.dumps(
            {
                "criteria": [{"id": "exists", "verdict": "pass", "reason": ""}] * 2
                + [{"id": "exact", "verdict": "pass", "reason": ""}]
            }
        ),
        _verdict(exists="pass", exact="pass", extra="pass"),
        _verdict(exists="maybe", exact="pass"),
    ],
)
def test_duplicate_or_unknown_id_is_rejected(reply):
    with pytest.raises(JudgeFormatError):
        parse_judgement(reply, ["exists", "exact"])


def test_majority_of_three_and_tie_fails():
    provider = ScriptedProvider(
        _verdict(exists="pass", exact="pass"),
        _verdict(exists="pass", exact="fail"),
        _verdict(exists="pass", exact="pass"),
    )
    assert _judge(provider, repeats=3).passed
    tie = ScriptedProvider(_verdict(exists="pass", exact="pass"), _verdict(exists="pass", exact="fail"))
    verdict = _judge(tie, repeats=2)
    assert not verdict.passed
    assert {c.id: c.verdict for c in verdict.criteria}["exact"] == "fail"


def test_all_not_applicable_fails_case():
    verdict = _judge(ScriptedProvider(_verdict(exists="not_applicable", exact="not_applicable")))
    assert not verdict.passed and "no applicable criterion" in (verdict.error or "")


def test_not_applicable_beside_a_pass_still_passes():
    assert _judge(ScriptedProvider(_verdict(exists="pass", exact="not_applicable"))).passed


def _cv(case_id: str, passed: bool) -> CaseVerdict:
    return CaseVerdict(case_id, passed, [CriterionVerdict("c", "pass" if passed else "fail", "")], [], None)


def test_min_pass_rate_threshold():
    verdicts = [_cv("a", True), _cv("b", False)]
    assert not judge_layer(verdicts, 2, min_pass_rate=1.0).passed
    assert judge_layer(verdicts, 2, min_pass_rate=0.5).passed
    assert [r.rule for r in judge_layer(verdicts, 2, min_pass_rate=0.5).results] == ["J1"]


def test_unjudged_cases_count_against_the_rate():
    assert not judge_layer([_cv("a", True)], 2, min_pass_rate=1.0).passed
