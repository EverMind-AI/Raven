"""Tests for the node verdict: the judge call, its parsing, and its refusals."""

import asyncio
import json

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
