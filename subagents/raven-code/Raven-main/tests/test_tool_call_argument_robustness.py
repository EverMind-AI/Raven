"""Tool-call arguments that arrive as invalid JSON must never reach a tool unrepaired.

Evidence base: a 500-task SWE-bench Verified run of four harnesses on
Qwen3.6-35B-A3B behind vLLM 0.23.1. The server returns HTTP 200 with tool-call
arguments that are not valid JSON on 0.17-0.25 percent of calls (truncated
objects, unescaped quotes, raw control characters, fragments of a different
tool-call syntax). `json_repair` makes such a string parse again, but a
truncated one parses into *silently incomplete* parameters -- a write_file whose
content lost its tail, or an edit_file whose old_string now matches the wrong
span. Executing that is worse than failing: the model believes it succeeded.
"""

from __future__ import annotations

import json

from raven.providers.tool_args import ToolArgsLimits, coerce_arguments, limits_from_defaults, looks_truncated

# Verbatim samples captured from the real run. They cover every parser error the
# upstream server produced: unterminated object, missing delimiter, invalid
# control character, and unterminated string.
TRUNCATED_AT_FUNCTION_HEADER = "{"
MISSING_CLOSING_BRACE = '{"cmd": "cat /testbed/django/db/backends/postgresql/client.py"'
RAW_NEWLINE_IN_KEY = '{"cd /testbed && git stash pop\n</parameter": ""}'
UNESCAPED_QUOTE_IN_KEY = '{"cd=/tmp && python3 -c "import requests; print(requests.__file__)"\n</parameter": ""}'


def test_an_unclosed_object_is_reported_as_truncated() -> None:
    assert looks_truncated(TRUNCATED_AT_FUNCTION_HEADER)
    assert looks_truncated(MISSING_CLOSING_BRACE)
    assert looks_truncated('{"todos": [{"content": "x"}')


def test_a_closed_object_is_never_reported_as_truncated() -> None:
    """The two mangled-but-closed samples must not be mistaken for truncation.

    They need resampling, not a bigger output budget, so misclassifying them
    would burn the max_tokens ladder on a problem more tokens cannot fix.
    """
    assert not looks_truncated(RAW_NEWLINE_IN_KEY)
    assert not looks_truncated(UNESCAPED_QUOTE_IN_KEY)
    assert not looks_truncated('{"cmd": "ls"}')
    assert not looks_truncated('  {"cmd": "ls"}  \n')
    assert not looks_truncated("[]")
    assert not looks_truncated("")


def test_valid_arguments_are_returned_unchanged_and_unflagged() -> None:
    parsed, flags = coerce_arguments('{"cmd": "ls -la /testbed"}')

    assert parsed == {"cmd": "ls -la /testbed"}
    assert not flags.truncated
    assert not flags.repaired


def test_a_truncated_object_is_repaired_but_stays_flagged() -> None:
    """Repair still runs so the caller can show the model what it wrote.

    The flag is what stops execution; dropping the content instead would lose
    the diagnostic value of a partially-written command.
    """
    parsed, flags = coerce_arguments(MISSING_CLOSING_BRACE)

    assert flags.truncated
    assert flags.repaired
    assert parsed["cmd"].startswith("cat /testbed")


def test_mangled_but_closed_arguments_are_flagged_repaired_only() -> None:
    for sample in (RAW_NEWLINE_IN_KEY, UNESCAPED_QUOTE_IN_KEY):
        parsed, flags = coerce_arguments(sample)

        assert flags.repaired, sample
        assert not flags.truncated, sample
        assert isinstance(parsed, dict), sample


def test_arguments_that_repair_into_a_non_object_become_an_empty_object() -> None:
    """`json_repair` answers `''` for hopeless input, and `**''` is a TypeError.

    Without this, the tool layer reports `parameters must be an object, got str`
    -- which sends the model chasing a schema problem it does not have.
    """
    parsed, flags = coerce_arguments("garbage </parameter")

    assert parsed == {}
    assert flags.repaired


def test_empty_and_whitespace_arguments_are_an_empty_object() -> None:
    for sample in ("", "   ", "\n"):
        parsed, flags = coerce_arguments(sample)

        assert parsed == {}
        assert not flags.truncated, sample


def test_a_dict_from_the_provider_is_passed_through() -> None:
    parsed, flags = coerce_arguments({"cmd": "ls"})

    assert parsed == {"cmd": "ls"}
    assert not flags.repaired
    assert not flags.truncated


def test_the_finish_reason_marks_truncation_even_when_the_object_closed() -> None:
    """A closed object can still be short: the model may have been cut mid-value.

    hermes stopped trusting `finish_reason` alone because some gateways rewrite
    "length" into "tool_calls"; we use it as an additional signal, never as the
    only one.
    """
    _, flags = coerce_arguments('{"cmd": "ls"}', finish_reason="length")

    assert flags.truncated


def test_limits_come_from_agent_defaults_and_have_safe_fallbacks() -> None:
    class _Defaults:
        max_tokens = 8192
        tool_args_truncation_max_retries = 4
        tool_args_malformed_max_resamples = 3
        tool_args_max_tokens_ceiling = 32768

    limits = limits_from_defaults(_Defaults())

    assert limits == ToolArgsLimits(
        truncation_max_retries=4,
        malformed_max_resamples=3,
        max_tokens_ceiling=32768,
    )
    assert isinstance(limits_from_defaults(None), ToolArgsLimits)
    assert isinstance(limits_from_defaults(object()), ToolArgsLimits)


def test_the_doubling_ladder_stops_at_the_ceiling() -> None:
    limits = ToolArgsLimits(truncation_max_retries=4, malformed_max_resamples=3, max_tokens_ceiling=32768)

    assert limits.next_max_tokens(8192) == 16384
    assert limits.next_max_tokens(16384) == 32768
    assert limits.next_max_tokens(32768) == 32768
    assert limits.next_max_tokens(30000) == 32768


def test_a_repaired_call_still_serializes_to_valid_json_for_the_next_request() -> None:
    """Whatever we hand back must survive the round trip into conversation history.

    codex lost 74 of 500 tasks precisely because it replayed the raw string; the
    server accepted it on the way out and rejected it on the way back in.
    """
    from raven.providers.base import ToolCallRequest

    parsed, _ = coerce_arguments(UNESCAPED_QUOTE_IN_KEY)
    payload = ToolCallRequest(id="c1", name="exec", arguments=parsed).to_openai_tool_call()

    assert isinstance(json.loads(payload["function"]["arguments"]), dict)
