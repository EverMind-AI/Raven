"""Generator loop mechanics against a scripted provider (no real LLM)."""

import json

import pytest

import raven.playbook.generator as generator_mod
from raven.playbook import (
    PlaybookGenerationError,
    PlaybookGenerator,
    PlaybookProtocolError,
    PlaybookProviderError,
    StaticInventory,
)
from raven.playbook.prompt import emit_tool
from raven.providers.base import ErrorClassification

ROSTER = {
    "research-raven": "deep retrieval and fact-checking",
    "code-raven": "repository-level code work",
    "data-raven": "the whole data chain",
    "content-raven": "text and deliverables",
}


class ToolCall:
    def __init__(self, arguments, name="emit_playbook"):
        self.arguments = arguments
        self.name = name


class Response:
    def __init__(
        self,
        args,
        name="emit_playbook",
        *,
        finish_reason="stop",
        error_classification=None,
        content=None,
        truncated=False,
        max_tokens=None,
    ):
        self.has_tool_calls = args is not None
        self.tool_calls = [ToolCall(args, name)] if args is not None else []
        self.finish_reason = finish_reason
        self.error_classification = error_classification
        self.content = content
        self.truncated = truncated
        self.max_tokens = max_tokens


class ScriptedProvider:
    def __init__(self, payloads):
        self._payloads = list(payloads)
        self.calls = []

    async def chat_with_retry(self, messages, tools=None, model=None, tool_choice=None, **_):
        self.calls.append(messages)
        payload = self._payloads.pop(0)
        return payload if isinstance(payload, Response) else Response(payload)


GOOD_DAG = {
    "name": "weekly-feedback",
    "description": "weekly user-feedback analysis",
    "taskSummary": "pull this week's feedback and write the report",
    "mode": "dag",
    "confirm": True,
    "triggers": {"keywords": ["user feedback", "feedback weekly"]},
    "params": {"week_of": {"type": "string", "required": True, "description": "which week should be analyzed?"}},
    "nodes": [
        {
            "id": "pull",
            "subagent": "data-raven",
            "nodeSummary": "pull this week's feedback from slack",
            "promptTemplate": "pull the feedback for ${params.week_of}",
            "skills": ["sql-queries"],
        },
        {
            "id": "report",
            "subagent": "content-raven",
            "nodeSummary": "write the weekly report from the pulled feedback",
            "promptTemplate": "write the weekly report from {{ pull.output }}",
            "dependsOn": ["pull"],
        },
    ],
    "assumptions": ["the data source is slack"],
    "blockingQuestions": [],
}


def _generator(payloads, skills=("sql-queries",)):
    return (
        PlaybookGenerator(
            ScriptedProvider(payloads),
            skill_router=None,
            agent_roster=ROSTER,
            inventory=StaticInventory(mcp=["slack"]),
        ),
        None,
    )


def test_emit_tool_schema_inlines_refs_without_losing_constraints():
    schema = emit_tool()[0]["function"]["parameters"]
    encoded = json.dumps(schema)

    assert '"$defs"' not in encoded
    assert '"$ref"' not in encoded
    assert schema["properties"]["triggers"]["properties"]["keywords"]["minItems"] == 1
    assert schema["properties"]["params"]["additionalProperties"]["required"] == ["description"]
    node = schema["properties"]["nodes"]["anyOf"][0]["items"]
    assert node["required"] == ["id"]
    assert node["properties"]["dependsOn"]["items"] == {"type": "string"}
    assert "confirm" in schema["properties"]
    assert "confirm" not in node["properties"]
    assert "version" not in schema["properties"]
    assert "blockingQuestions" in schema["properties"]


async def test_happy_path_fills_code_owned_fields():
    gen, _ = _generator([GOOD_DAG])
    result = await gen.generate("weekly feedback analysis, the flow is fixed...", skills=["sql-queries"])
    assert result.spec.name == "weekly-feedback"
    assert result.spec.nodes[1].depends_on == ["pull"]
    assert result.notes == ["Assumption: the data source is slack"]


async def test_missing_required_tool_is_a_protocol_error_without_content_repair():
    gen, _ = _generator([None, GOOD_DAG])

    with pytest.raises(PlaybookProtocolError) as raised:
        await gen.generate("weekly feedback analysis")

    assert raised.value.code == "required_tool_missing"
    assert len(gen._provider.calls) == 1


async def test_truncated_response_without_tool_has_its_own_protocol_error():
    response = Response(None, truncated=True, max_tokens=512)
    gen, _ = _generator([response, GOOD_DAG])

    with pytest.raises(PlaybookProtocolError) as raised:
        await gen.generate("weekly feedback analysis")

    assert raised.value.code == "required_tool_output_truncated"
    assert len(gen._provider.calls) == 1


async def test_provider_failure_preserves_classification_without_becoming_a_missing_tool():
    classification = ErrorClassification(
        "upstream_transport_failure",
        retryable=True,
        should_fallback=True,
    )
    response = Response(
        None,
        finish_reason="error",
        error_classification=classification,
        content="upstream did not process the request",
    )
    gen, _ = _generator([response, GOOD_DAG])

    with pytest.raises(PlaybookProviderError) as raised:
        await gen.generate("weekly feedback analysis")

    assert raised.value.classification is classification
    assert raised.value.category == "upstream_transport_failure"
    assert raised.value.code == "provider_call_failed"
    assert "provider_call_failed" in str(raised.value)
    assert len(gen._provider.calls) == 1


def test_missing_required_tool_name_is_not_assumed_to_be_correct():
    from raven.playbook.generator import _required_tool_args

    response = Response(GOOD_DAG)
    del response.tool_calls[0].name

    with pytest.raises(PlaybookProtocolError) as raised:
        _required_tool_args(response)

    assert raised.value.code == "required_tool_wrong_name"


def test_wrong_required_tool_name_is_classified_separately():
    from raven.playbook.generator import _required_tool_args

    with pytest.raises(PlaybookProtocolError) as raised:
        _required_tool_args(Response(GOOD_DAG, name="other_tool"))

    assert raised.value.code == "required_tool_wrong_name"


def test_multiple_required_tool_calls_are_rejected_as_ambiguous():
    from raven.playbook.generator import _required_tool_args

    response = Response(GOOD_DAG)
    response.tool_calls.append(ToolCall(GOOD_DAG))

    with pytest.raises(PlaybookProtocolError) as raised:
        _required_tool_args(response)

    assert raised.value.code == "required_tool_multiple_calls"


def test_missing_required_tool_arguments_are_a_protocol_error():
    from raven.playbook.generator import _required_tool_args

    response = Response(GOOD_DAG)
    del response.tool_calls[0].arguments

    with pytest.raises(PlaybookProtocolError) as raised:
        _required_tool_args(response)

    assert raised.value.code == "required_tool_arguments_invalid"


async def test_invalid_required_tool_arguments_are_not_content_repaired():
    gen, _ = _generator(["{truncated", GOOD_DAG])

    with pytest.raises(PlaybookProtocolError) as raised:
        await gen.generate("weekly feedback analysis")

    assert raised.value.code == "required_tool_arguments_invalid"
    assert len(gen._provider.calls) == 1


async def test_truncated_required_tool_json_has_its_own_protocol_error():
    response = Response("{truncated", truncated=True, max_tokens=512)
    gen, _ = _generator([response, GOOD_DAG])

    with pytest.raises(PlaybookProtocolError) as raised:
        await gen.generate("weekly feedback analysis")

    assert raised.value.code == "required_tool_arguments_truncated"
    assert len(gen._provider.calls) == 1


async def test_repair_loop_feeds_errors_back():
    bad = dict(GOOD_DAG)
    bad["nodes"] = [dict(GOOD_DAG["nodes"][0], agent="ghost-agent"), GOOD_DAG["nodes"][1]]
    gen, _ = _generator([bad, GOOD_DAG])
    result = await gen.generate("weekly feedback analysis", skills=["sql-queries"])
    assert result.spec.name == "weekly-feedback"
    repair_msg = gen._provider.calls[1][-1]["content"]
    assert "ghost-agent" in repair_msg and "emit_playbook" in repair_msg


async def test_gives_up_after_budget_with_error_detail():
    bad = {"name": "x!", "mode": "dag"}
    gen, _ = _generator([bad, bad, bad, bad])
    with pytest.raises(PlaybookGenerationError):
        await gen.generate("anything")


async def test_envelope_wrapped_spec_is_unwrapped():
    gen, _ = _generator([{"playbook": GOOD_DAG}])
    result = await gen.generate("weekly feedback analysis")
    assert result.spec.name == "weekly-feedback"


async def test_a_stringified_object_field_is_decoded_not_refused():
    """Some models serialise a nested object twice; the content is still right."""
    payload = json.loads(json.dumps(GOOD_DAG))
    payload["triggers"] = json.dumps(GOOD_DAG["triggers"])
    payload["params"] = json.dumps(GOOD_DAG["params"])
    payload["nodes"] = json.dumps(GOOD_DAG["nodes"])
    # Scripted four deep so a regression ends in the loop's own error naming
    # the field, rather than the provider running out of payloads.
    gen, _ = _generator([payload] * 4)
    result = await gen.generate("weekly feedback analysis", skills=["sql-queries"])
    assert result.spec.triggers.keywords == ["user feedback", "feedback weekly"]
    assert list(result.spec.params) == ["week_of"]
    assert [n.id for n in result.spec.nodes] == ["pull", "report"]
    assert len(gen._provider.calls) == 1, "it should not have needed a repair round"


async def test_free_text_that_parses_as_json_is_left_alone():
    """`prompts` is the author's prose, not a structure to decode."""
    payload = {
        "name": "due-diligence",
        "description": "run due diligence on a target",
        "taskSummary": "due diligence sweep",
        "mode": "prompt",
        "triggers": {"keywords": ["due diligence", "diligence sweep"]},
        "prompts": '{"layer one": "a breadth scan", "layer two": "fan out by focus"}',
    }
    gen, _ = _generator([payload] * 4)
    result = await gen.generate("build me a due-diligence playbook")
    assert result.spec.prompts == payload["prompts"]


def test_the_decoded_field_set_tracks_the_contract():
    """A new object or array field on the spec must not need a second list."""
    assert set(generator_mod._STRUCTURAL_FIELDS) == {
        "triggers",
        "params",
        "nodes",
        "blockingQuestions",
        "assumptions",
    }


async def test_unknown_skills_degrade_into_notes():
    payload = json.loads(json.dumps(GOOD_DAG))
    payload["nodes"][0]["skills"] = ["ghost-skill"]
    gen, _ = _generator([payload])
    result = await gen.generate("weekly feedback analysis")
    assert any(n.startswith("Missing capability:") and "ghost-skill" in n for n in result.notes)


async def test_blocking_questions_become_notes_not_fields():
    payload = json.loads(json.dumps(GOOD_DAG))
    payload["blockingQuestions"] = ["what is the report for?"]
    gen, _ = _generator([payload])
    result = await gen.generate("weekly feedback analysis")
    assert "Open question: what is the report for?" in result.notes
    # The self-report never reaches the machine fields.
    assert "blockingQuestions" not in result.spec.block_dump()


async def test_model_proposed_triggers_go_through_the_guards():
    """The emit_playbook schema exposes ``triggers``, so the model writes the L1
    vocabulary directly. Every guard in triggers.py has to apply to what it
    writes: a stop word, an entry under the length rule and a duplicate case
    variant must not reach the index just because a model proposed them rather
    than a person.
    """
    payload = json.loads(json.dumps(GOOD_DAG))
    payload["triggers"] = {"keywords": ["help", "a", "user feedback", "user feedback", "SEO", "seo"]}
    gen, _ = _generator([payload])

    result = await gen.generate("weekly feedback analysis")

    assert result.spec.triggers.keywords == ["user feedback", "seo"]


async def test_all_junk_triggers_feed_the_repair_loop():
    """An emptied vocabulary is a repairable mistake, not a silent widening:
    keeping one of the dropped words would reinstate the entry a guard just
    rejected, and an index entry is a permanent per-message cost.
    """
    junk = json.loads(json.dumps(GOOD_DAG))
    junk["triggers"] = {"keywords": ["a", "help", "the"]}
    gen, _ = _generator([junk, GOOD_DAG])

    result = await gen.generate("weekly feedback analysis")

    assert result.spec.triggers.keywords == ["user feedback", "feedback weekly"]
    repair_msg = gen._provider.calls[1][-1]["content"]
    assert "every trigger candidate was dropped" in repair_msg


async def test_revise_keeps_the_name_and_reports_fresh_notes():
    gen, _ = _generator([GOOD_DAG])
    result = await gen.generate("weekly feedback analysis")

    edited = json.loads(json.dumps(GOOD_DAG))
    edited["name"] = "renamed-anyway"
    edited["assumptions"] = ["the report becomes daily"]
    gen2, _ = _generator([edited])
    revised = await gen2.revise(result.spec, "make the report daily")
    assert revised.spec.name == result.spec.name
    assert revised.notes == ["Assumption: the report becomes daily"]
