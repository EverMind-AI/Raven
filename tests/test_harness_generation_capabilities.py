"""The checked-in harness capability document controls every generation boundary."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from raven.agent import harness_capabilities
from raven.agent.subagent.charter import parse
from raven.playbook.agent_generator import (
    _choice_profile,
    _persona_spec_from_args,
    _profile_payload,
    _task_spec_from_args,
    _tool_inventory,
    emit_tool,
    persona_tool,
    task_tool,
)
from raven.playbook.agent_spec import AgentPlaybookSpec


def _disabled_document(tmp_path, monkeypatch):
    document = json.loads(harness_capabilities._PATH.read_text(encoding="utf-8"))
    root = document["harnessGeneration"]
    for module, name in (
        ("memory", "systemPrompt"),
        ("memory", "stopWhen"),
        ("capability", "tools"),
        ("action", "checks"),
    ):
        root[module]["parameters"]["items"][name]["enabled"] = False
    for module, name in (("memory", "intake"), ("planning", "advise"), ("action", "judge"), ("action", "salvage")):
        root[module]["functions"]["participant"]["items"][name]["enabled"] = False
    path = tmp_path / "harness_generation.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(harness_capabilities, "_PATH", path)


def test_checked_in_document_exposes_only_enabled_harness_fields() -> None:
    properties = persona_tool(["worker"], ["read_file"])[0]["function"]["parameters"]["properties"]["workers"]["items"][
        "properties"
    ]

    assert {"systemPrompt", "stopWhen", "tools", "checks", "functions"} <= properties.keys()
    assert "code" not in properties
    assert set(properties["functions"]["properties"]) == {"intake", "advise", "judge", "salvage"}
    assert "reasoningEffort" not in properties


def test_disabling_fields_removes_them_from_generation(tmp_path, monkeypatch) -> None:
    _disabled_document(tmp_path, monkeypatch)

    properties = persona_tool(["worker"], ["read_file"])[0]["function"]["parameters"]["properties"]["workers"]["items"][
        "properties"
    ]

    assert not ({"systemPrompt", "stopWhen", "tools", "checks", "functions"} & properties.keys())
    assert {"as", "agent", "brief", "timeoutSeconds"} <= properties.keys()


def test_task_schema_and_compatibility_alias_respect_disabled_tools(tmp_path, monkeypatch) -> None:
    _disabled_document(tmp_path, monkeypatch)

    schema = task_tool(["worker"], ["read_file"])
    worker = schema[0]["function"]["parameters"]["properties"]["workers"]["items"]

    assert "tools" not in worker["properties"]
    assert emit_tool(["worker"], ["read_file"], {"worker": {}}, {"old": "ignored"}) == schema


def test_generator_profiles_and_tool_inventory_normalize_safe_inputs() -> None:
    class Choice:
        id = "max"
        label = "Maximum"
        description = "Use deeper reasoning"

    assert _choice_profile({"value": "fast", "name": "Fast"}) == {"id": "fast", "name": "Fast"}
    assert _choice_profile(Choice()) == {
        "id": "max",
        "name": "Maximum",
        "description": "Use deeper reasoning",
    }
    assert _profile_payload("  owns current web research  ") == {"description": "owns current web research"}
    assert _tool_inventory(
        [
            "write_file",
            {"function": {"name": "read_file", "description": "Read a file"}},
            {"function": []},
            object(),
        ]
    ) == [
        {"name": "read_file", "description": "Read a file"},
        {"name": "write_file", "description": ""},
    ]


@pytest.mark.parametrize(
    ("worker", "message"),
    [
        ("worker", "worker must be an object"),
        ({"agent": "worker", "prompt": "do it", "brief": "forbidden"}, "field.*not allowed"),
        ({"agent": "missing", "prompt": "do it"}, "unknown agent"),
        ({"agent": "worker", "prompt": ""}, "prompt must be non-empty"),
        ({"agent": "worker", "prompt": "do it", "tools": "read_file"}, "tools must be an array"),
        ({"agent": "worker", "prompt": "do it", "tools": ["missing"]}, "unknown tool"),
    ],
)
def test_task_generator_rejects_unusable_workers(worker, message) -> None:
    with pytest.raises(ValueError, match=message):
        _task_spec_from_args(
            {"workers": [worker]},
            {"worker"},
            {"read_file"},
        )


def test_task_generator_requires_workers_and_rejects_disabled_tools(tmp_path, monkeypatch) -> None:
    with pytest.raises(ValueError, match="workers must be a non-empty list"):
        _task_spec_from_args({"workers": []}, {"worker"})

    _disabled_document(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="disabled harness field.*tools"):
        _task_spec_from_args(
            {"workers": [{"agent": "worker", "prompt": "do it", "tools": ["read_file"]}]},
            {"worker"},
            {"read_file"},
        )


def test_persona_generator_assembles_judge_rules_and_known_tools() -> None:
    judge = "def judge(name, params, prior):\n    return []"
    spec, _ = _persona_spec_from_args(
        {
            "workers": [
                {
                    "as": "watcher",
                    "agent": "worker",
                    "brief": "Watch booking deadlines",
                    "tools": ["web_search"],
                    "checks": [{"tool": "web_search"}],
                    "functions": {"judge": judge},
                }
            ]
        },
        {"worker"},
        {"web_search"},
    )

    playbook = spec.delegate[0].playbook
    assert playbook is not None
    assert playbook.capability.tools == ["web_search"]
    assert playbook.action.checks.code == judge
    assert [rule.tool for rule in playbook.action.checks.rules] == ["web_search"]


def test_persona_generator_requires_object_workers_and_known_tools() -> None:
    with pytest.raises(ValueError, match="workers must be a non-empty list"):
        _persona_spec_from_args({"workers": []}, {"worker"})
    with pytest.raises(ValueError, match="worker must be an object"):
        _persona_spec_from_args({"workers": ["worker"]}, {"worker"})
    with pytest.raises(ValueError, match="unknown tool"):
        _persona_spec_from_args(
            {
                "workers": [
                    {
                        "as": "watcher",
                        "agent": "worker",
                        "brief": "Watch booking deadlines",
                        "tools": ["missing"],
                    }
                ]
            },
            {"worker"},
            {"web_search"},
        )


def test_disabling_fields_rejects_generated_and_stored_values(tmp_path, monkeypatch) -> None:
    _disabled_document(tmp_path, monkeypatch)
    worker = {
        "agent": "worker",
        "systemPrompt": "special instructions",
        "stopWhen": "done",
        "tools": ["read_file"],
        "checks": [{"tool": "read_file"}],
        "functions": {"judge": "def judge(name, params, prior):\n    return []"},
    }

    with pytest.raises(ValueError, match="disabled harness field"):
        _persona_spec_from_args({"workers": [worker]}, {"worker"})
    with pytest.raises(ValidationError, match="disabled harness field"):
        AgentPlaybookSpec.model_validate(
            {
                "delegate": [
                    {
                        "name": "worker",
                        "playbook": {
                            "memory": {"systemPrompt": "special instructions"},
                            "stopWhen": "done",
                            "capability": {"tools": ["read_file"]},
                            "action": {
                                "checks": {
                                    "rules": [{"tool": "read_file"}],
                                    "code": "def judge(name, params, prior):\n    return []",
                                }
                            },
                        },
                    }
                ]
            }
        )


def test_disabling_fields_drops_them_at_the_worker_boundary(tmp_path, monkeypatch) -> None:
    _disabled_document(tmp_path, monkeypatch)

    charter = parse(
        {
            "brief": "the dispatch brief",
            "instructionAddendum": "generated instructions",
            "stopWhen": "done",
            "tools": ["read_file"],
            "checks": [{"tool": "read_file"}],
            "code": "def judge(name, params, prior):\n    return []",
        }
    )

    assert charter is not None
    assert charter.prompt == "the dispatch brief"
    assert charter.instruction_addendum == ""
    assert charter.task_brief == "the dispatch brief"
    assert charter.stop_when == ""
    assert charter.tools is None
    assert charter.checks == ()
    assert charter.code == ""


def test_a_non_boolean_switch_is_refused(tmp_path, monkeypatch) -> None:
    document = json.loads(harness_capabilities._PATH.read_text(encoding="utf-8"))
    document["harnessGeneration"]["memory"]["parameters"]["items"]["systemPrompt"]["enabled"] = "false"
    path = tmp_path / "harness_generation.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(harness_capabilities, "_PATH", path)

    with pytest.raises(RuntimeError, match="enabled must be a boolean"):
        persona_tool(["worker"], ["read_file"])


def test_capability_loader_refuses_broken_and_undeclared_catalog_entries(tmp_path, monkeypatch) -> None:
    checked_in = json.loads(harness_capabilities._PATH.read_text(encoding="utf-8"))

    def point_at(name: str, document) -> None:
        path = tmp_path / name
        body = document if isinstance(document, str) else json.dumps(document)
        path.write_text(body, encoding="utf-8")
        monkeypatch.setattr(harness_capabilities, "_PATH", path)

    point_at("bad-json.json", "{")
    with pytest.raises(RuntimeError, match="invalid harness generation capability file"):
        harness_capabilities.parameter_enabled("memory", "systemPrompt")

    point_at("bad-root.json", {"harnessGeneration": []})
    with pytest.raises(RuntimeError, match="harnessGeneration must be an object"):
        harness_capabilities.parameter_enabled("memory", "systemPrompt")

    missing_parameter = json.loads(json.dumps(checked_in))
    del missing_parameter["harnessGeneration"]["memory"]["parameters"]["items"]["systemPrompt"]
    point_at("missing-parameter.json", missing_parameter)
    with pytest.raises(RuntimeError, match="not declared: memory.parameters.systemPrompt"):
        harness_capabilities.parameter_enabled("memory", "systemPrompt")

    missing_function = json.loads(json.dumps(checked_in))
    del missing_function["harnessGeneration"]["memory"]["functions"]["participant"]["items"]["intake"]
    point_at("missing-function.json", missing_function)
    with pytest.raises(RuntimeError, match="not declared: memory.functions.participant.intake"):
        harness_capabilities.function_enabled("memory", "participant", "intake")

    unwired_parameter = json.loads(json.dumps(checked_in))
    unwired_parameter["harnessGeneration"]["memory"]["parameters"]["items"]["temperature"] = {"enabled": True}
    point_at("unwired-parameter.json", unwired_parameter)
    with pytest.raises(RuntimeError, match="enabled but not wired: memory.parameters.temperature"):
        harness_capabilities.parameter_enabled("memory", "systemPrompt")


def test_enabling_an_unwired_catalog_item_fails_loudly(tmp_path, monkeypatch) -> None:
    document = json.loads(harness_capabilities._PATH.read_text(encoding="utf-8"))
    document["harnessGeneration"]["action"]["functions"]["participant"]["items"]["review"]["enabled"] = True
    path = tmp_path / "harness_generation.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(harness_capabilities, "_PATH", path)

    with pytest.raises(RuntimeError, match="enabled but not wired: action.functions.participant.review"):
        persona_tool(["worker"], ["read_file"])


@pytest.mark.parametrize(
    ("functions", "message"),
    [
        ([], "functions must be an object"),
        ({"archive": "def archive(step):\n    return None"}, "unknown generated participant function: archive"),
        ({"intake": ""}, "functions.intake must be non-empty Python source"),
        ({"intake": "x" * 8001}, "functions.intake exceeds 8000 characters"),
    ],
)
def test_generator_refuses_malformed_function_payloads(functions, message) -> None:
    with pytest.raises(ValueError, match=message):
        _persona_spec_from_args({"workers": [{"agent": "worker", "functions": functions}]}, {"worker"})


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"code": "def judge(name, params, prior):\n    return []"}, "Persona worker field.*code"),
        ({"tools": "read_file"}, "tools must be an array"),
        ({"checks": {}}, "checks must be an array"),
        ({"timeoutSeconds": 0}, "timeoutSeconds must be a positive integer"),
    ],
)
def test_persona_generator_refuses_fields_it_cannot_preserve(extra, message) -> None:
    worker = {"as": "worker", "agent": "worker", "brief": "do it", **extra}
    with pytest.raises(ValueError, match=message):
        _persona_spec_from_args({"workers": [worker]}, {"worker"})


@pytest.mark.parametrize(
    ("playbook", "message"),
    [
        (
            {"memory": {"functions": {"archive": "def archive(step):\n    return None"}}},
            "unknown generated participant function: memory.archive",
        ),
        ({"memory": {"functions": {"intake": ""}}}, "generated participant function is empty: memory.intake"),
        ({"action": {"checks": {"impl": "custom"}}}, "disabled harness field.*action.checksImpl"),
    ],
)
def test_stored_playbook_refuses_unknown_empty_and_disabled_fields(playbook, message) -> None:
    with pytest.raises(ValidationError, match=message):
        AgentPlaybookSpec.model_validate({"delegate": [{"name": "worker", "playbook": playbook}]})


@pytest.mark.asyncio
async def test_enabled_functions_generate_bind_and_execute_through_module_composers() -> None:
    from raven.agent.harness.participants import compose_advice, compose_intake, compose_salvage
    from raven.agent.subagent.charter import charter_participants, charter_scope
    from raven.contracts.participant import StepView
    from raven.playbook.agent_generator import build_payload

    sources = {
        "intake": ("def intake(text, step):\n    return {'text': text.upper(), 'note': step.get('phase')}"),
        "advise": ("def advise(step):\n    return 'iteration=' + str(step.get('iteration'))"),
        "salvage": ("def salvage(step):\n    return 'salvaged:' + step.get('question', '')"),
    }
    spec, briefs = _persona_spec_from_args(
        {"workers": [{"as": "worker", "agent": "worker", "brief": "do it", "functions": sources}]},
        {"worker"},
    )
    playbook = spec.delegate[0].playbook
    assert playbook is not None
    payload = build_payload(briefs["worker"], playbook)
    assert payload is not None and payload["functions"] == sources
    charter = parse(payload)
    assert charter is not None and dict(charter.functions) == sources

    step = StepView(
        session_key="s",
        iteration=3,
        response=None,
        transcript=({"role": "user", "content": "original"},),
        history=(),
        turn_base=0,
        question="recover me",
        rollbacks=0,
        mode=None,
        mode_overlay=None,
        phase="user_inbound",
    )
    with charter_scope(charter):
        participants = charter_participants()
        assert len(participants) == 1
        intake = await compose_intake("hello", step, participants)
        assert intake is not None and intake.text == "HELLO" and intake.note == "user_inbound"
        assert await compose_advice(step, participants) == "iteration=3"
        assert await compose_salvage(step, participants) == "salvaged:recover me"


def test_generated_function_signature_is_validated_before_payload_building() -> None:
    with pytest.raises(ValueError, match="must have signature intake\\(text, step\\)"):
        _persona_spec_from_args(
            {"workers": [{"agent": "worker", "functions": {"intake": "def intake(step):\n    return None"}}]},
            {"worker"},
        )


def test_disabled_generated_function_is_dropped_at_worker_boundary(tmp_path, monkeypatch) -> None:
    _disabled_document(tmp_path, monkeypatch)
    charter = parse(
        {
            "brief": "still present",
            "functions": {
                "intake": "def intake(text, step):\n    return {'text': text}",
                "advise": "def advise(step):\n    return 'x'",
                "salvage": "def salvage(step):\n    return 'x'",
            },
        }
    )
    assert charter is not None
    assert charter.functions == ()


def test_disabling_one_function_removes_rejects_and_drops_only_that_function(tmp_path, monkeypatch) -> None:
    document = json.loads(harness_capabilities._PATH.read_text(encoding="utf-8"))
    document["harnessGeneration"]["planning"]["functions"]["participant"]["items"]["advise"]["enabled"] = False
    path = tmp_path / "harness_generation.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(harness_capabilities, "_PATH", path)

    properties = persona_tool(["worker"], ["read_file"])[0]["function"]["parameters"]["properties"]["workers"]["items"][
        "properties"
    ]
    assert set(properties["functions"]["properties"]) == {"intake", "judge", "salvage"}
    with pytest.raises(ValueError, match="disabled harness field.*functions.advise"):
        _persona_spec_from_args(
            {"workers": [{"agent": "worker", "functions": {"advise": "def advise(step):\n    return 'x'"}}]},
            {"worker"},
        )

    with pytest.raises(ValidationError, match="disabled harness field.*planning.functions.advise"):
        AgentPlaybookSpec.model_validate(
            {"delegate": [{"name": "worker", "playbook": {"planning": {"functions": {"advise": "x"}}}}]}
        )

    charter = parse(
        {
            "functions": {
                "intake": "def intake(text, step):\n    return {'text': text}",
                "advise": "def advise(step):\n    return 'x'",
                "salvage": "def salvage(step):\n    return 'x'",
            }
        }
    )
    assert charter is not None
    assert set(dict(charter.functions)) == {"intake", "salvage"}
