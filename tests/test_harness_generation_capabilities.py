"""The checked-in harness capability document controls every generation boundary."""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from loguru import logger
from pydantic import ValidationError

from raven.agent import harness_capabilities
from raven.agent.subagent import charter
from raven.agent.subagent.charter import parse
from raven.playbook import agent_generator, agent_spec
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


@contextmanager
def _warnings() -> Iterator[list[str]]:
    """loguru's own sink, read back as text: the skew a dropped field causes is
    a log line rather than a raised error, so holding the sink is the only way
    a test can see it."""
    said: list[str] = []
    sink = logger.add(lambda message: said.append(message.record["message"]), level="WARNING")
    try:
        yield said
    finally:
        logger.remove(sink)


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
    coordinator_only, _ = _persona_spec_from_args({"coordinator": {"brief": "Handle it"}, "workers": []}, {"worker"})
    assert coordinator_only.coordinator is not None
    assert coordinator_only.delegate == []
    with pytest.raises(ValueError, match="seat must be an object"):
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


def test_a_worker_whose_catalogue_disables_tools_names_the_skew_it_drops(tmp_path, monkeypatch) -> None:
    _disabled_document(tmp_path, monkeypatch)

    with _warnings() as said:
        charter = parse({"brief": "the dispatch brief", "tools": ["read_file", "write_file"]})

    assert charter is not None
    assert charter.tools is None
    assert [line for line in said if "capability.tools" in line] == [
        "charter: 2 tool name(s) accepted from the host, but this install's catalogue "
        "disables capability.tools; the dispatch keeps the worker's own toolset"
    ]


def test_a_worker_whose_catalogue_enables_tools_names_no_skew() -> None:
    with _warnings() as said:
        charter = parse({"brief": "the dispatch brief", "tools": ["read_file"]})

    assert charter is not None
    assert charter.tools == ("read_file",)
    assert said == []


def test_a_dispatch_with_an_empty_restriction_names_the_skew_it_drops(tmp_path, monkeypatch) -> None:
    _disabled_document(tmp_path, monkeypatch)

    with _warnings() as said:
        charter = parse({"brief": "the dispatch brief", "tools": []})

    assert charter is not None
    assert charter.tools is None
    assert [line for line in said if "capability.tools" in line] == [
        "charter: 0 tool name(s) accepted from the host, but this install's catalogue "
        "disables capability.tools; the dispatch keeps the worker's own toolset"
    ]


def test_a_dispatch_that_carries_no_tools_names_no_skew(tmp_path, monkeypatch) -> None:
    _disabled_document(tmp_path, monkeypatch)

    with _warnings() as said:
        absent = parse({"brief": "the dispatch brief"})

    assert absent is not None
    assert absent.tools is None
    assert said == []


def test_an_undeclared_tools_switch_fails_the_parse_even_without_a_tools_key(tmp_path, monkeypatch) -> None:
    """The catalogue is consulted for every payload, so a build whose document
    never declared the switch refuses the dispatch rather than answering it
    without the field the document is supposed to describe."""
    document = json.loads(harness_capabilities._PATH.read_text(encoding="utf-8"))
    del document["harnessGeneration"]["capability"]["parameters"]["items"]["tools"]
    path = tmp_path / "harness_generation.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(harness_capabilities, "_PATH", path)

    with pytest.raises(RuntimeError, match="not declared: capability.parameters.tools"):
        parse({"brief": "the dispatch brief"})


def test_the_skew_count_names_the_accepted_tools_not_the_entries_sent(tmp_path, monkeypatch) -> None:
    _disabled_document(tmp_path, monkeypatch)

    with _warnings() as said:
        charter = parse({"brief": "the dispatch brief", "tools": ["read_file", 7, "", "write_file"]})

    assert charter is not None
    assert charter.tools is None
    assert [line for line in said if "capability.tools" in line] == [
        "charter: 2 tool name(s) accepted from the host, but this install's catalogue "
        "disables capability.tools; the dispatch keeps the worker's own toolset"
    ]


def test_a_non_boolean_switch_is_refused(tmp_path, monkeypatch) -> None:
    document = json.loads(harness_capabilities._PATH.read_text(encoding="utf-8"))
    document["harnessGeneration"]["memory"]["parameters"]["items"]["systemPrompt"]["enabled"] = "false"
    path = tmp_path / "harness_generation.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(harness_capabilities, "_PATH", path)

    with pytest.raises(RuntimeError, match="enabled must be a boolean"):
        persona_tool(["worker"], ["read_file"])


def test_capability_loader_refuses_a_catalogue_that_is_not_text(tmp_path, monkeypatch) -> None:
    """A catalogue in another encoding is a broken file, not a crash.

    ``UnicodeDecodeError`` is a ``ValueError`` and not one of the errors the
    loader wraps, so it used to escape the loader's own contract: the caller
    that catches the ``RuntimeError`` never saw it.
    """
    path = tmp_path / "not-text.json"
    path.write_bytes(b'{"harnessGeneration": {"memory": "\xff\xfe"}}')
    monkeypatch.setattr(harness_capabilities, "_PATH", path)

    with pytest.raises(RuntimeError, match="invalid harness generation capability file"):
        harness_capabilities.parameter_enabled("memory", "systemPrompt")


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


def test_validate_loads_the_document_and_its_checks(tmp_path, monkeypatch) -> None:
    """The up-front check is the same load the queries do, failures included."""
    harness_capabilities.validate()

    bad = tmp_path / "harness_generation.json"
    bad.write_text("{", encoding="utf-8")
    monkeypatch.setattr(harness_capabilities, "_PATH", bad)
    with pytest.raises(RuntimeError, match="invalid harness generation capability file"):
        harness_capabilities.validate()


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (("memory", "parameters", "items", "systemPrompt"), "memory.parameters.systemPrompt"),
        (("action", "parameters", "items", "checksImpl"), "action.parameters.checksImpl"),
        (("planning", "functions", "participant", "items", "advise"), "planning.functions.participant.advise"),
    ],
)
def test_validate_refuses_missing_switches_at_startup(tmp_path, monkeypatch, path, expected) -> None:
    document = json.loads(harness_capabilities._PATH.read_text(encoding="utf-8"))
    section = document["harnessGeneration"]
    for key in path[:-1]:
        section = section[key]
    del section[path[-1]]
    catalogue = tmp_path / "harness_generation.json"
    catalogue.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(harness_capabilities, "_PATH", catalogue)

    with pytest.raises(RuntimeError, match=re.escape(f"not declared: {expected}")):
        harness_capabilities.validate()


def test_enabling_an_unwired_catalog_item_fails_loudly(tmp_path, monkeypatch) -> None:
    document = json.loads(harness_capabilities._PATH.read_text(encoding="utf-8"))
    document["harnessGeneration"]["action"]["functions"]["participant"]["items"]["review"]["enabled"] = True
    path = tmp_path / "harness_generation.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    monkeypatch.setattr(harness_capabilities, "_PATH", path)

    with pytest.raises(RuntimeError, match="enabled but not wired: action.functions.participant.review"):
        persona_tool(["worker"], ["read_file"])


def test_a_misshapen_catalogue_section_names_the_file_rather_than_raising_on_its_own(tmp_path, monkeypatch) -> None:
    """Every read of this loader is promised one error that names the file. A
    section that is not an object would otherwise raise an ``AttributeError``
    from inside the walk, which says nothing about which file is broken."""
    checked_in = json.loads(harness_capabilities._PATH.read_text(encoding="utf-8"))

    def point_at(name: str, mutate) -> None:
        document = json.loads(json.dumps(checked_in))
        mutate(document["harnessGeneration"])
        path = tmp_path / name
        path.write_text(json.dumps(document), encoding="utf-8")
        monkeypatch.setattr(harness_capabilities, "_PATH", path)

    def at(module: str, *keys: str):
        def mutate(root: dict) -> None:
            section = root[module]
            for key in keys[:-1]:
                section = section[key]
            section[keys[-1]] = []

        return mutate

    def module_body(module: str):
        def mutate(root: dict) -> None:
            root[module] = []

        return mutate

    for name, mutate, expected in (
        ("module-body", module_body("memory"), "memory must be an object"),
        ("parameters", at("memory", "parameters"), "memory.parameters must be an object"),
        ("parameters-items", at("memory", "parameters", "items"), "memory.parameters.items must be an object"),
        ("functions", at("memory", "functions"), "memory.functions must be an object"),
        ("functions-kind", at("memory", "functions", "participant"), "memory.functions.participant must be an object"),
        (
            "functions-kind-items",
            at("memory", "functions", "participant", "items"),
            "memory.functions.participant.items must be an object",
        ),
        ("self-kind", at("action", "functions", "self"), "action.functions.self must be an object"),
    ):
        point_at(f"{name}.json", mutate)
        with pytest.raises(RuntimeError, match=re.escape(f"invalid harness generation capability file: {expected}")):
            harness_capabilities.parameter_enabled("memory", "systemPrompt")


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


_QUERY_SOURCES = (
    Path(agent_generator.__file__),
    Path(agent_spec.__file__),
    Path(charter.__file__),
)
_PARAMETER_QUERIES = {"parameter_enabled", "_parameter_enabled"}
_FUNCTION_QUERIES = {"function_enabled", "_function_enabled"}


def _literal_rows(node: ast.AST) -> list[tuple[str, ...]] | None:
    """The rows of a table of string constants, or ``None`` if this is not one."""
    if not isinstance(node, (ast.Tuple, ast.List)):
        return None
    rows: list[tuple[str, ...]] = []
    for element in node.elts:
        if not isinstance(element, (ast.Tuple, ast.List)):
            return None
        row: list[str] = []
        for item in element.elts:
            if not isinstance(item, ast.Constant) or not isinstance(item.value, str):
                return None
            row.append(item.value)
        rows.append(tuple(row))
    return rows


def _clause_rows(node: ast.AST) -> list[tuple[str, ...]] | None:
    """The rows a comprehension clause iterates over, or ``None`` if it is not a table of strings."""
    rows = _literal_rows(node)
    if rows is not None:
        return rows
    if not isinstance(node, (ast.Tuple, ast.List)):
        return None
    flat: list[tuple[str, ...]] = []
    for element in node.elts:
        if not isinstance(element, ast.Constant) or not isinstance(element.value, str):
            return None
        flat.append((element.value,))
    return flat


def _bound(node: ast.AST, env: dict[str, object]) -> object | None:
    """What a name bound by this expression can be.

    A string, a table of strings, or a lookup in one -- the three shapes a
    capability query can take its arguments from.
    """
    if isinstance(node, ast.Dict):
        table: dict[str, str] = {}
        for key, item in zip(node.keys, node.values):
            if not (isinstance(key, ast.Constant) and isinstance(key.value, str)):
                return None
            if not (isinstance(item, ast.Constant) and isinstance(item.value, str)):
                return None
            table[key.value] = item.value
        return table
    if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "get":
        source = env.get(node.func.value.id) if isinstance(node.func.value, ast.Name) else None
        if isinstance(source, dict):
            return ("get", source)
    return None


def _values(node: ast.AST, env: dict[str, object]) -> set[str] | None:
    """The strings this argument can be, if it is one the scan can resolve."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Name):
        bound = env.get(node.id)
        if isinstance(bound, str):
            return {bound}
    return None


def _table(node: ast.AST, env: dict[str, object]) -> dict[str, str] | None:
    """The table this argument is looked up in, if it is looked up in one."""
    if isinstance(node, ast.Name):
        bound = env.get(node.id)
        if isinstance(bound, tuple) and bound and bound[0] == "get":
            return bound[1]
    return None


def _record(call: ast.Call, env: dict[str, object], parameters: set, functions: set) -> None:
    """Read one capability query and write down what it asks for."""
    name = call.func.id if isinstance(call.func, ast.Name) else ""
    if name in _PARAMETER_QUERIES and len(call.args) == 2:
        modules = _values(call.args[0], env)
        switches = _values(call.args[1], env)
        if modules and switches:
            parameters.update((module, switch) for module in modules for switch in switches)
        return
    if name in _FUNCTION_QUERIES and len(call.args) == 3:
        kinds = _values(call.args[1], env)
        if not kinds:
            return
        table = _table(call.args[0], env)
        if table is not None:
            functions.update((module, kind, switch) for switch, module in table.items() for kind in kinds)
            return
        modules = _values(call.args[0], env)
        switches = _values(call.args[2], env)
        if modules and switches:
            functions.update((module, kind, switch) for module in modules for kind in kinds for switch in switches)


def _scan(node: ast.AST, env: dict[str, object], parameters: set, functions: set) -> None:
    """Walk the source, resolving the capability queries it makes.

    A table of constants is walked once per row, so the query inside it is
    read with that row's names; a query whose arguments come from data the
    model supplies is left alone rather than guessed at.
    """
    if isinstance(node, list):
        for item in node:
            _scan(item, env, parameters, functions)
        return
    if isinstance(node, ast.For):
        rows = _literal_rows(node.iter)
        targets = node.target.elts if isinstance(node.target, ast.Tuple) else [node.target]
        if rows is not None and all(isinstance(target, ast.Name) for target in targets):
            for row in rows:
                inner = dict(env)
                for target, value in zip(targets, row):
                    inner[target.id] = value
                _scan(node.body, inner, parameters, functions)
        else:
            _scan(node.body, env, parameters, functions)
        return
    if isinstance(node, ast.comprehension):
        _scan(node.iter, env, parameters, functions)
        rows = _clause_rows(node.iter)
        targets = node.target.elts if isinstance(node.target, ast.Tuple) else [node.target]
        if rows is not None and all(isinstance(target, ast.Name) for target in targets):
            for row in rows:
                inner = dict(env)
                for target, value in zip(targets, row):
                    inner[target.id] = value
                _scan(node.ifs, inner, parameters, functions)
        else:
            _scan(node.ifs, env, parameters, functions)
        return
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        _scan(node.body, dict(env), parameters, functions)
        return
    if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
        bound = _bound(node.value, env)
        if bound is not None:
            env[node.targets[0].id] = bound
    if isinstance(node, ast.Call):
        _record(node, env, parameters, functions)
    for child in ast.iter_child_nodes(node):
        _scan(child, env, parameters, functions)


def _query_sites() -> tuple[set[tuple[str, str]], set[tuple[str, str, str]]]:
    """The switches the generation surface reads, read out of its source."""
    parameters: set[tuple[str, str]] = set()
    functions: set[tuple[str, str, str]] = set()
    for path in _QUERY_SOURCES:
        _scan(ast.parse(path.read_text(encoding="utf-8")), {}, parameters, functions)
    return parameters, functions


def test_the_validated_switches_are_the_ones_the_generation_surface_reads() -> None:
    """The list ``validate()`` checks is hand-written; the query sites are not.

    A switch a new call site reads has to be added to that list by hand, or
    the up-front check misses the very thing it is there to catch.
    """
    parameters, functions = _query_sites()

    assert parameters and functions, "the scan found no query sites; it has stopped reading them"
    assert parameters == set(harness_capabilities._GENERATION_PARAMETERS)
    assert functions == set(harness_capabilities._GENERATION_FUNCTIONS)


_COMPREHENSION_QUERIES = """
def _functions():
    return {
        name: {"type": "string"}
        for module, name in (
            ("memory", "intake"),
            ("planning", "advise"),
        )
        if _function_enabled(module, "participant", name)
    }


def _parameters():
    return {
        name: {"type": "boolean"}
        for name in ("systemPrompt", "stopWhen")
        if _parameter_enabled("memory", name)
    }
"""


def test_the_scan_reads_a_query_point_written_in_a_comprehension() -> None:
    """A comprehension binds its own names; the scan has to bind them too.

    The surface builds its schema with a dict comprehension, so a query in one
    is read with names the scan never bound unless it binds the clause target.
    """
    parameters: set[tuple[str, str]] = set()
    functions: set[tuple[str, str, str]] = set()

    _scan(ast.parse(_COMPREHENSION_QUERIES), {}, parameters, functions)

    assert parameters == {("memory", "systemPrompt"), ("memory", "stopWhen")}
    assert functions == {
        ("memory", "participant", "intake"),
        ("planning", "participant", "advise"),
    }
