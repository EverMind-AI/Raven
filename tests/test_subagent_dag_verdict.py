"""Tests for the node verdict: the judge call, its parsing, and its refusals."""

import asyncio
import json
import re

from raven.agent.subagent.dag_verdict import (
    CATEGORIES,
    Verdict,
    describe_failure,
    extract_verdict,
    judge,
    tail,
    verdict_tool_schema,
)


class _Call:
    def __init__(self, arguments):
        self.arguments = arguments


class _Response:
    def __init__(self, tool_calls):
        self.tool_calls = tool_calls


class _Provider:
    """Returns a canned response, or raises, and records what it was asked."""

    def __init__(self, response=None, error=None):
        self._response = response
        self._error = error
        self.calls = []

    async def chat_with_retry(self, **kwargs):
        self.calls.append(kwargs)
        if self._error is not None:
            raise self._error
        return self._response


def _accomplished():
    return _Response([_Call(json.dumps({"outcome": "accomplished"}))])


def _not_accomplished():
    return _Response(
        [
            _Call(
                json.dumps(
                    {
                        "outcome": "not_accomplished",
                        "category": "missing_credential",
                        "what_is_missing": "an API token for the billing endpoint",
                        "evidence": "401 Unauthorized",
                    }
                )
            )
        ]
    )


def test_tail_keeps_the_end_not_the_start():
    assert tail("abcdefghij", 4) == "ghij"


def test_tail_returns_short_text_whole():
    assert tail("abc", 10) == "abc"


def test_extract_verdict_reads_an_accomplished_call():
    verdict = extract_verdict(_accomplished())
    assert verdict == Verdict(accomplished=True)


def test_extract_verdict_reads_the_exception_fields():
    verdict = extract_verdict(_not_accomplished())
    assert verdict.accomplished is False
    assert verdict.category == "missing_credential"
    assert verdict.what_is_missing == "an API token for the billing endpoint"
    assert verdict.evidence == "401 Unauthorized"


def test_extract_verdict_accepts_dict_arguments():
    verdict = extract_verdict(_Response([_Call({"outcome": "accomplished"})]))
    assert verdict == Verdict(accomplished=True)


def test_extract_verdict_returns_none_without_a_tool_call():
    assert extract_verdict(_Response([])) is None


def test_extract_verdict_returns_none_on_unparseable_arguments():
    assert extract_verdict(_Response([_Call("{not json")])) is None


def test_extract_verdict_rejects_an_unknown_category():
    response = _Response([_Call(json.dumps({"outcome": "not_accomplished", "category": "made_up"}))])
    assert extract_verdict(response).category == "other"


def test_verdict_tool_schema_names_every_category():
    schema = verdict_tool_schema()
    enum = schema[0]["function"]["parameters"]["properties"]["category"]["enum"]
    assert tuple(enum) == CATEGORIES


async def test_judge_returns_the_models_verdict():
    provider = _Provider(_not_accomplished())
    verdict = await judge(provider, prompt="p", output="o", evidence="e", evidence_complete=True)
    assert verdict.accomplished is False
    assert verdict.category == "missing_credential"
    assert verdict.what_is_missing == "an API token for the billing endpoint"
    assert verdict.evidence == "401 Unauthorized"


async def test_judge_fails_open_when_the_call_raises():
    provider = _Provider(error=RuntimeError("provider down"))
    verdict = await judge(provider, prompt="p", output="o", evidence="e", evidence_complete=True)
    assert verdict.accomplished is True


async def test_judge_fails_open_when_the_model_skips_the_tool():
    provider = _Provider(_Response([]))
    verdict = await judge(provider, prompt="p", output="o", evidence="e", evidence_complete=True)
    assert verdict.accomplished is True


async def test_judge_fails_open_on_timeout():
    class _Slow:
        async def chat_with_retry(self, **kwargs):
            await asyncio.sleep(1)

    verdict = await judge(_Slow(), prompt="p", output="o", evidence="e", evidence_complete=True, timeout_s=0.01)
    assert verdict.accomplished is True


async def test_judge_fences_the_node_output():
    provider = _Provider(_accomplished())
    await judge(provider, prompt="p", output="INJECTED", evidence="e", evidence_complete=True)
    sent = json.dumps(provider.calls[0]["messages"])
    assert "INJECTED" in sent
    assert "[BEGIN UNTRUSTED subagent" in sent
    assert "[END UNTRUSTED subagent" in sent


async def test_judge_passes_the_model_override():
    provider = _Provider(_accomplished())
    await judge(provider, prompt="p", output="o", evidence="e", evidence_complete=True, model="cheap-tier")
    assert provider.calls[0]["model"] == "cheap-tier"


async def test_judge_says_when_the_evidence_is_incomplete():
    provider = _Provider(_accomplished())
    await judge(provider, prompt="p", output="o", evidence="", evidence_complete=False)
    sent = json.dumps(provider.calls[0]["messages"])
    assert "no per-step transcript" in sent


async def test_describe_failure_never_reports_accomplished():
    provider = _Provider(_accomplished())
    verdict = await describe_failure(provider, prompt="p", error="boom", evidence="e", evidence_complete=True)
    assert verdict.accomplished is False


async def test_describe_failure_keeps_the_raw_error_when_the_call_fails():
    provider = _Provider(error=RuntimeError("provider down"))
    verdict = await describe_failure(provider, prompt="p", error="boom", evidence="e", evidence_complete=True)
    assert verdict.accomplished is False
    assert verdict.what_is_missing == "boom"
    assert verdict.category == "other"


async def test_describe_failure_uses_the_models_categorisation():
    provider = _Provider(_not_accomplished())
    verdict = await describe_failure(provider, prompt="p", error="boom", evidence="e", evidence_complete=True)
    assert verdict.accomplished is False
    assert verdict.category == "missing_credential"
    assert verdict.what_is_missing == "an API token for the billing endpoint"
    assert verdict.evidence == "401 Unauthorized"


async def test_judge_is_told_a_negative_finding_can_accomplish_a_task():
    provider = _Provider(_accomplished())
    await judge(provider, prompt="p", output="o", evidence="e", evidence_complete=True)
    instruction = provider.calls[0]["messages"][0]["content"]
    assert "A negative finding can accomplish a task" in instruction
    assert "never actually investigated" in instruction


def test_verdict_tool_schema_separates_a_negative_answer_from_undone_work():
    outcome = verdict_tool_schema()[0]["function"]["parameters"]["properties"]["outcome"]
    assert "could not be done is 'not_accomplished'" in outcome["description"]
    assert "the answer is negative is 'accomplished'" in outcome["description"]


async def test_judge_is_told_when_the_node_ran_out_of_output_budget():
    provider = _Provider(_accomplished())
    await judge(provider, prompt="p", output="", evidence="e", evidence_complete=True, output_limited=True)
    sent = json.dumps(provider.calls[0]["messages"])
    assert "output token limit" in sent


async def test_judge_says_nothing_about_an_output_limit_that_did_not_happen():
    provider = _Provider(_accomplished())
    await judge(provider, prompt="p", output="o", evidence="e", evidence_complete=True)
    sent = json.dumps(provider.calls[0]["messages"])
    assert "output token limit" not in sent


async def test_judge_reads_the_output_limit_fact_outside_the_untrusted_fence():
    provider = _Provider(_accomplished())
    await judge(provider, prompt="p", output="", evidence="e", evidence_complete=True, output_limited=True)
    body = provider.calls[0]["messages"][1]["content"]
    unfenced = re.sub(r"\[BEGIN UNTRUSTED .*?\[END UNTRUSTED [^\]]*\]", "", body, flags=re.DOTALL)
    assert "output token limit" in unfenced


async def test_judge_is_told_an_output_limit_does_not_excuse_unfinished_work():
    provider = _Provider(_accomplished())
    await judge(provider, prompt="p", output="", evidence="e", evidence_complete=True, output_limited=True)
    instruction = provider.calls[0]["messages"][0]["content"]
    assert "excusing work that is not there" in instruction


def test_output_limit_is_a_verdict_category():
    assert "output_limit" in CATEGORIES


def test_extract_verdict_reads_the_output_limit_category():
    response = _Response([_Call(json.dumps({"outcome": "not_accomplished", "category": "output_limit"}))])
    assert extract_verdict(response).category == "output_limit"


async def test_describe_failure_is_told_when_the_node_ran_out_of_output_budget():
    """A crashed node can also have been cut, and naming why it crashed is
    exactly this call's job."""
    provider = _Provider(_not_accomplished())
    await describe_failure(
        provider, prompt="p", error="boom", evidence="e", evidence_complete=True, output_limited=True
    )
    sent = json.dumps(provider.calls[0]["messages"])
    assert "output token limit" in sent


async def test_describe_failure_says_nothing_about_an_output_limit_that_did_not_happen():
    provider = _Provider(_not_accomplished())
    await describe_failure(provider, prompt="p", error="boom", evidence="e", evidence_complete=True)
    sent = json.dumps(provider.calls[0]["messages"])
    assert "output token limit" not in sent
