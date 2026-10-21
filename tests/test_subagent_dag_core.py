"""Ported DAG core: graph validation, placeholders, render, store.

Covers the provider-agnostic core of the sub-agent DAG subsystem (no agentscope);
render/store are exercised over a tiny in-memory duck-typed backend.
"""

from __future__ import annotations

import json
import os
import pathlib
import posixpath
import re
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

import pytest

from raven.agent.loop.bundles import SubagentWiring, ToolWiring, TurnPolicy
from raven.agent.subagent.dag_capabilities import validate_capabilities
from raven.agent.subagent.dag_graph import collect_static_graph_errors, parse_dag_spec, validate_and_order
from raven.agent.subagent.dag_reader import DagReadError, read_node, read_run
from raven.agent.subagent.dag_render import (
    MAYBE_MISSING_HEADING,
    MISSING_OUTPUT_TEXT,
    _upstream_memory_lines,
    render_prompt,
)
from raven.agent.subagent.dag_store import DagRunStore, SessionNodes, make_run_id
from raven.agent.subagent.dag_tool import _NODE_SCHEMA
from raven.agent.subagent.prompt_backend import LocalFileBackend
from raven.agent.subagent.prompt_capabilities import AgentCapabilities
from raven.agent.subagent.prompt_errors import DagValidationError
from raven.agent.subagent.prompt_paths import check_confined, split_reference
from raven.agent.subagent.prompt_placeholders import parse_placeholders
from raven.config.schema import ThirdPartyCliSubagentConfig
from raven.security.trust import wrap_untrusted

if TYPE_CHECKING:
    from raven.agent.subagent.dag_tool import SubAgentDagTool

# --- graph ---------------------------------------------------------------


def test_topo_order_and_parse() -> None:
    spec = parse_dag_spec(
        {
            "task_summary": "say hello and echo it downstream",
            "nodes": [
                {"id": "a", "subagent": "x", "node_summary": "say hello", "prompt_template": "hello"},
                {
                    "id": "b",
                    "subagent": "x",
                    "node_summary": "echo a's output",
                    "prompt_template": "{{ a.output }}",
                    "depends_on": ["a"],
                },
            ],
        }
    )
    assert validate_and_order(spec) == ["a", "b"]


def test_two_ids_differing_only_by_case_are_one_node_id() -> None:
    """The registry's key space is case-sensitive; the filesystem's is not.

    A node's artifacts are files named after its id, so on a case-folding
    backend (APFS by default, and NTFS) `Plan.out.md` and `plan.out.md` are one
    file and the second node's write destroys the first's output. Refused rather
    than normalised: two nodes the model named differently must not silently
    become one.
    """
    spec = parse_dag_spec(
        {
            "task_summary": "reject ids that collide when folded",
            "nodes": [
                {"id": "Plan", "subagent": "x", "node_summary": "first", "prompt_template": "one"},
                {"id": "plan", "subagent": "x", "node_summary": "second", "prompt_template": "two"},
            ],
        }
    )

    assert "duplicate node ids: ['plan']" in collect_static_graph_errors(spec.nodes)[0]


def test_ids_differing_by_more_than_case_stay_separate() -> None:
    """The control: folding must not collapse ids that are genuinely distinct."""
    spec = parse_dag_spec(
        {
            "task_summary": "keep distinct ids distinct",
            "nodes": [
                {"id": "plan_a", "subagent": "x", "node_summary": "first", "prompt_template": "one"},
                {"id": "plan_b", "subagent": "x", "node_summary": "second", "prompt_template": "two"},
            ],
        }
    )

    assert collect_static_graph_errors(spec.nodes) == []


def test_a_spawn_held_id_is_not_reported_as_a_run() -> None:
    """A spawn has no run, so its claim records the `kind` in the owner slot.

    Dropping that straight into "run '...'" names a run that does not exist and
    sends the reader looking for it.
    """
    from raven.agent.subagent.dag_store import UNRECORDED, duplicate_node_id

    spawn = duplicate_node_id("plan", "spawn", readable=False)
    assert "an earlier spawn in this conversation" in spawn
    assert "run 'spawn'" not in spawn
    assert "that run left it" not in spawn

    unrecorded = duplicate_node_id("plan", UNRECORDED, readable=False)
    assert f"run '{UNRECORDED}'" not in unrecorded

    assert "used by run 'r1'" in duplicate_node_id("plan", "r1", readable=False), "a real run still reads as one"


def test_a_node_id_colliding_by_case_with_a_claimed_one_is_refused() -> None:
    """`Plan` is taken, so `plan` is taken too -- one file backs both."""
    spec = parse_dag_spec(
        {
            "task_summary": "reuse a claimed id in another case",
            "nodes": [{"id": "plan", "subagent": "x", "node_summary": "retry", "prompt_template": "retry"}],
        }
    )
    known = SessionNodes(owner={"Plan": "r1"}, state={"Plan": "completed"}, has_output={"Plan": True})

    with pytest.raises(DagValidationError) as exc_info:
        validate_and_order(spec, session_nodes=known)

    message = str(exc_info.value)
    assert "already used by run 'r1'" in message
    assert "'Plan'" in message, "the refusal has to name the id actually taken, not the one asked for"


def test_duplicate_node_error_names_the_duplicate_ids() -> None:
    spec = parse_dag_spec(
        {
            "task_summary": "reject duplicate node ids",
            "nodes": [
                {"id": "a", "subagent": "x", "node_summary": "first", "prompt_template": "one"},
                {"id": "a", "subagent": "x", "node_summary": "second", "prompt_template": "two"},
            ],
        }
    )

    assert collect_static_graph_errors(spec.nodes)[0] == "duplicate node ids: ['a']"
    with pytest.raises(DagValidationError) as exc_info:
        validate_and_order(spec)
    assert str(exc_info.value) == "duplicate node ids: ['a']"


@pytest.mark.parametrize(
    "value",
    [None, 3, {}, {"file": ""}, {"node": ""}, {"file": "a.md", "extra": "x"}, {"file": "a", "node": "b"}],
)
def test_static_validation_rejects_every_unsupported_input_shape(value: object) -> None:
    spec = parse_dag_spec(
        {
            "task_summary": "validate one node input",
            "nodes": [
                {
                    "id": "a",
                    "subagent": "x",
                    "node_summary": "consume one input",
                    "prompt_template": "use {{ inputs.material }}",
                    "inputs": {"material": value},
                }
            ],
        }
    )

    assert collect_static_graph_errors(spec.nodes)
    with pytest.raises(DagValidationError):
        validate_and_order(spec)


def test_static_validation_can_check_an_unfilled_playbook_node_without_weakening_dispatch() -> None:
    spec = parse_dag_spec(
        {
            "task_summary": "load a fillable playbook node",
            "nodes": [
                {
                    "id": "a",
                    "subagent": "x",
                    "node_summary": "fill the prompt later",
                    "prompt_template": "",
                    "inputs": {"material": "still validate this shape"},
                }
            ],
        }
    )

    assert collect_static_graph_errors(spec.nodes, allowed_blank_fields=frozenset({"prompt_template"})) == []
    with pytest.raises(DagValidationError, match="cannot run without"):
        validate_and_order(spec)


def test_allowing_a_blank_prompt_does_not_allow_a_malformed_input() -> None:
    spec = parse_dag_spec(
        {
            "task_summary": "load a fillable playbook node",
            "nodes": [
                {
                    "id": "a",
                    "subagent": "x",
                    "node_summary": "fill the prompt later",
                    "prompt_template": "",
                    "inputs": {"material": 3},
                }
            ],
        }
    )

    errors = collect_static_graph_errors(spec.nodes, allowed_blank_fields=frozenset({"prompt_template"}))

    assert any("literal string" in error for error in errors)


@pytest.mark.parametrize("instance", ["", " ", " handle", "handle "])
def test_instance_rejects_blank_or_invisible_edge_whitespace(instance: str) -> None:
    with pytest.raises(DagValidationError):
        parse_dag_spec(
            {
                "task_summary": "reject an ambiguous instance handle",
                "nodes": [
                    {
                        "id": "a",
                        "subagent": "x",
                        "node_summary": "run one node",
                        "prompt_template": "hello",
                        "instance": instance,
                    }
                ],
            }
        )

    assert _NODE_SCHEMA["properties"]["instance"]["minLength"] == 1


@pytest.mark.parametrize("name", ["General Audit", "MiniMax M2.5", "Исследователь", "claude_code", "a"])
def test_subagent_name_takes_any_name_the_config_layer_accepts(name: str) -> None:
    # A sub-agent name is a roster key, never a path: `spawn` dispatches to
    # "General Audit" happily, so a DAG node naming the same agent must not be
    # refused for the name alone. Only the node `id` stays charset-restricted,
    # because it becomes `<id>.prompt.md`.
    spec = parse_dag_spec(
        {
            "task_summary": "run a lone node under this agent name",
            "nodes": [{"id": "a", "subagent": name, "prompt_template": "hi"}],
        }
    )
    assert spec.nodes[0].subagent == name


@pytest.mark.parametrize("name", [" ", " coder", "coder ", "\tcoder", "two\nlines"])
def test_subagent_name_rejects_edge_whitespace(name: str) -> None:
    # Edge whitespace is invisible, so " coder" would fail the roster lookup
    # against a name that looks identical to the configured one -- refused here
    # where the error can name the field instead.
    with pytest.raises(DagValidationError):
        parse_dag_spec(
            {
                "task_summary": "run a lone node under this agent name",
                "nodes": [{"id": "a", "subagent": name, "prompt_template": "hi"}],
            }
        )


@pytest.mark.parametrize("field", ["subagent", "prompt_template", "node_summary"])
def test_a_blank_required_field_parses_but_never_runs(field: str) -> None:
    """Blank has to survive parsing and be refused before dispatch.

    A playbook may leave a node's agent or prompt for the model to fill, and
    ``load_playbook`` can only report that gap if the file loads -- so the parse
    accepts it. ``validate_and_order`` is the point past which nobody can fill it
    any more, which is where it gets refused.
    """
    node = {"id": "a", "subagent": "x", "prompt_template": "hi", field: ""}
    spec = parse_dag_spec({"task_summary": "run one node with a required field left blank", "nodes": [node]})
    assert getattr(spec.nodes[0], field) == ""

    with pytest.raises(DagValidationError, match="cannot run without"):
        validate_and_order(spec)


def test_node_summary_survives_the_parse_and_reaches_the_spec() -> None:
    spec = parse_dag_spec(
        {
            "task_summary": "compare the two vendors",
            "nodes": [
                {
                    "id": "a",
                    "subagent": "x",
                    "node_summary": "read the pricing pages",
                    "prompt_template": "hello",
                }
            ],
        }
    )
    assert spec.nodes[0].node_summary == "read the pricing pages"


def test_a_long_node_summary_is_accepted_because_length_is_advisory() -> None:
    # No `max_length`: the ceiling lives in the field's schema description, and a
    # verbose summary is clipped where it is displayed rather than rejected here.
    spec = parse_dag_spec(
        {
            "task_summary": "s",
            "nodes": [{"id": "a", "subagent": "x", "node_summary": "w" * 400, "prompt_template": "hi"}],
        }
    )
    assert len(spec.nodes[0].node_summary) == 400


def test_a_graph_without_a_task_summary_is_rejected() -> None:
    with pytest.raises(DagValidationError):
        parse_dag_spec({"nodes": [{"id": "a", "subagent": "x", "node_summary": "s", "prompt_template": "hi"}]})


def test_a_blank_task_summary_is_rejected_at_parse() -> None:
    # Unlike a node's summary, the graph-level one has no gap-filling path: it is
    # written by the model through the schema or by the playbook executor, so
    # blank can be refused at the boundary.
    with pytest.raises(DagValidationError):
        parse_dag_spec(
            {
                "task_summary": "",
                "nodes": [{"id": "a", "subagent": "x", "node_summary": "s", "prompt_template": "hi"}],
            }
        )


@pytest.mark.parametrize("node_id", ["has space", "a/b", "..", "a.out", ""])
def test_node_id_keeps_its_closed_charset(node_id: str) -> None:
    # The id is a path component; widening the sub-agent charset must not have
    # widened this one with it.
    with pytest.raises(DagValidationError):
        parse_dag_spec(
            {
                "task_summary": "run a lone node whose id may be invalid",
                "nodes": [{"id": node_id, "subagent": "x", "prompt_template": "hi"}],
            }
        )


def test_cycle_rejected() -> None:
    spec = parse_dag_spec(
        {
            "task_summary": "run two nodes that depend on each other",
            "nodes": [
                {
                    "id": "a",
                    "subagent": "x",
                    "node_summary": "read b's output",
                    "prompt_template": "{{ b.output }}",
                    "depends_on": ["b"],
                },
                {
                    "id": "b",
                    "subagent": "x",
                    "node_summary": "read a's output",
                    "prompt_template": "{{ a.output }}",
                    "depends_on": ["a"],
                },
            ],
        }
    )
    with pytest.raises(DagValidationError):
        validate_and_order(spec)


def test_default_deny_undeclared_reference() -> None:
    spec = parse_dag_spec(
        {
            "task_summary": "run a node that reads another without declaring it",
            "nodes": [
                {"id": "a", "subagent": "x", "node_summary": "node a", "prompt_template": "hi"},
                {
                    "id": "b",
                    "subagent": "x",
                    "node_summary": "read a's output without declaring the dependency",
                    "prompt_template": "{{ a.output }}",
                },  # no depends_on
            ],
        }
    )
    with pytest.raises(DagValidationError):
        validate_and_order(spec)


# --- capabilities --------------------------------------------------------

_STATELESS_BOXED = {"x": AgentCapabilities(stateful=False, reads_local_files=False)}
_FULL = {"x": AgentCapabilities(stateful=True, reads_local_files=True)}


def _spec(*nodes: dict) -> object:
    return parse_dag_spec({"task_summary": "run a small capability-gated graph", "nodes": list(nodes)})


def test_reused_instance_on_a_stateless_agent_is_rejected() -> None:
    spec = _spec(
        {"id": "draft", "subagent": "x", "prompt_template": "write", "instance": "author"},
        {"id": "revise", "subagent": "x", "prompt_template": "revise", "instance": "author"},
    )
    with pytest.raises(DagValidationError) as exc:
        validate_capabilities(spec, _STATELESS_BOXED)

    message = str(exc.value)
    assert "['draft', 'revise']" in message  # both offenders named, not just the second
    assert "stateless" in message
    assert "run_subagent_dag again" in message  # the way out is a fresh call


def test_a_single_instance_handle_is_not_reuse() -> None:
    """One node carrying a handle reuses nothing, so it is at worst redundant —
    rejecting it would fail graphs that are merely verbose."""
    spec = _spec({"id": "solo", "subagent": "x", "prompt_template": "go", "instance": "author"})
    validate_capabilities(spec, _STATELESS_BOXED)


def test_reused_instance_on_a_stateful_agent_is_allowed() -> None:
    spec = _spec(
        {"id": "draft", "subagent": "x", "prompt_template": "write", "instance": "author"},
        {"id": "revise", "subagent": "x", "prompt_template": "revise", "instance": "author"},
    )
    validate_capabilities(spec, _FULL)


def test_a_handle_shared_by_two_different_agents_is_not_a_capability_error() -> None:
    """The runner groups by handle to serialize, whichever agent runs the node;
    two agents sharing one never shared a session to begin with."""
    spec = _spec(
        {"id": "a", "subagent": "x", "prompt_template": "go", "instance": "shared"},
        {"id": "b", "subagent": "y", "prompt_template": "go", "instance": "shared"},
    )
    validate_capabilities(spec, {**_STATELESS_BOXED, "y": AgentCapabilities(stateful=False)})


@pytest.mark.parametrize(
    ("template", "inputs", "depends_on", "suggested"),
    [
        ("read {{ a.output_path }}", {}, ["a"], "{{ a.output }}"),
        ("read {{ inputs.doc.path }}", {"doc": {"file": "d.md"}}, [], "{{ inputs.doc }}"),
        ("read {{ ref_path:notes.md }}", {}, [], "{{ ref:notes.md }}"),
    ],
)
def test_path_placeholders_are_rejected_for_an_agent_that_cannot_read_files(
    template: str,
    inputs: dict,
    depends_on: list[str],
    suggested: str,
) -> None:
    spec = _spec(
        {"id": "a", "subagent": "x", "prompt_template": "upstream"},
        {
            "id": "b",
            "subagent": "x",
            "prompt_template": template,
            "inputs": inputs,
            "depends_on": depends_on,
        },
    )
    with pytest.raises(DagValidationError) as exc:
        validate_capabilities(spec, _STATELESS_BOXED)

    message = str(exc.value)
    assert "no-local-files" in message
    assert suggested in message  # names the content form to switch to
    assert "run_subagent_dag again" in message


def test_a_grammar_error_surfaces_unwrapped_by_the_capability_gate() -> None:
    """A malformed placeholder is the parser's error, not the path-placeholder
    gate's. Calling ``validate_capabilities`` directly, with no earlier grammar
    pass, must not catch it and dress it in advice for an unrelated capability
    fault -- the shape a future caller with no such pass would hit too."""
    spec = _spec(
        {"id": "a", "subagent": "x", "prompt_template": "upstream"},
        {"id": "b", "subagent": "x", "prompt_template": "{{ ref: }}", "depends_on": ["a"]},
    )
    with pytest.raises(DagValidationError) as exc:
        validate_capabilities(spec, _STATELESS_BOXED)

    message = str(exc.value)
    assert message == "empty path in '{{ ref: }}'"
    assert "no-local-files" not in message
    assert "run_subagent_dag again" not in message


def test_the_capability_refusal_keeps_mains_exact_wording() -> None:
    """A genuine capability refusal, by contrast, still carries the DAG's
    advice -- and the generalized gate must land on the byte-for-byte string
    the single-function version raised, since this message is model-facing."""
    spec = _spec(
        {"id": "a", "subagent": "x", "prompt_template": "upstream"},
        {"id": "b", "subagent": "x", "prompt_template": "read {{ a.output_path }}", "depends_on": ["a"]},
    )
    with pytest.raises(DagValidationError) as exc:
        validate_capabilities(spec, _STATELESS_BOXED)

    assert str(exc.value) == (
        "node 'b' passes local file paths to sub-agent 'x', which the roster "
        "tags [no-local-files]: it cannot open them, so the path would reach "
        "it as meaningless text. Replace each with the content form "
        "({{ a.output_path }} -> use {{ a.output }}), or move the node to a "
        "sub-agent tagged [local-files]. Then call run_subagent_dag again "
        "with the corrected graph."
    )


def test_content_placeholders_are_fine_for_an_agent_that_cannot_read_files() -> None:
    spec = _spec(
        {"id": "a", "subagent": "x", "prompt_template": "upstream"},
        {
            "id": "b",
            "subagent": "x",
            "prompt_template": "{{ a.output }} {{ inputs.k }} {{ ref:notes.md }}",
            "inputs": {"k": "literal"},
            "depends_on": ["a"],
        },
    )
    validate_capabilities(spec, _STATELESS_BOXED)


def test_paths_are_fine_for_an_agent_that_can_read_files() -> None:
    spec = _spec(
        {"id": "a", "subagent": "x", "prompt_template": "upstream"},
        {"id": "b", "subagent": "x", "prompt_template": "{{ a.output_path }}", "depends_on": ["a"]},
    )
    validate_capabilities(spec, _FULL)


def test_an_agent_absent_from_the_map_is_not_gated() -> None:
    """An unknown ``subagent`` is the runner's error to raise; guessing at
    capabilities we were never told would reject a graph for the wrong reason."""
    spec = _spec(
        {"id": "a", "subagent": "unmapped", "prompt_template": "go", "instance": "h"},
        {"id": "b", "subagent": "unmapped", "prompt_template": "go", "instance": "h"},
    )
    validate_capabilities(spec, {})


# --- placeholders --------------------------------------------------------


def test_placeholder_kinds() -> None:
    phs = parse_placeholders(
        "{{ a.output }} {{ a.output_path }} {{ inputs.k }} {{ inputs.k.path }} {{ ref:x }} {{ ref_path:x }}"
    )
    kinds = [p.kind for p in phs]
    assert kinds == ["output", "output_path", "input", "input_path", "ref", "ref_path"]


# --- render (over an in-memory backend) ----------------------------------


class _FakeBackend:
    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def join_path(self, *parts: str) -> str:
        return posixpath.join(*parts)

    def abspath(self, path: str, cwd: str | None = None) -> str:
        return path if path.startswith("/") else posixpath.join(cwd or "/", path)

    async def write_file(self, path: str, data: bytes) -> None:
        self.files[path] = data

    async def read_file(self, path: str) -> bytes:
        return self.files[path]

    async def file_exists(self, path: str) -> bool:
        return path in self.files


async def test_a_submitted_graph_runs_its_nodes_in_the_configured_sandbox(tmp_path: Path) -> None:
    """``run_subagent_dag`` used to be the one dispatch route that never asked
    for an executor: every node's exec ran on the host however ``tools.sandbox``
    was set, silently -- exactly what the sandbox docs promise never happens.
    The tool now builds each node's executor from the same sandbox the spawn
    paths read."""
    from raven.agent.subagent.dag_tool import SubAgentDagTool
    from raven.agent.subagent.registry import AgentRegistry

    built: list[Path] = []
    ran: list[tuple[Path, object]] = []
    exited: list[bool] = []

    class _Vm:
        def __init__(self, workspace: Path) -> None:
            self.workspace = workspace

        async def __aenter__(self) -> "_Vm":
            return self

        async def __aexit__(self, *exc: object) -> None:
            exited.append(True)

    class _Node:
        async def run(self, task: str, *, task_id: str, workspace, executor, **kwargs) -> str:
            ran.append((Path(workspace), executor))
            return f"OUT[{task_id}]"

    def sandbox_for(workspace: Path) -> _Vm:
        built.append(workspace)
        return _Vm(workspace)

    registry = AgentRegistry(build_builtin=lambda row, narrowed: _Node())
    registry.apply([])
    tool = SubAgentDagTool(workspace=tmp_path, registry=registry, sandbox_for=sandbox_for)
    tool.set_context("cli", "direct", None)
    out = await tool.execute(
        [{"id": "a", "subagent": "Raven", "node_summary": "one", "prompt_template": "hello"}],
        task_summary="run the graph under test",
        background=False,
    )
    assert "completed" in str(out)
    assert len(built) == 1 and len(ran) == 1, "one node, one executor built for it"
    assert built[0] == ran[0][0], "the executor is built for the workdir its node runs in"
    assert isinstance(ran[0][1], _Vm), f"the node received the factory's executor, got {ran[0][1]!r}"
    assert exited == [True], "the node's executor is torn down when the node ends"


async def test_render_output_and_inputs() -> None:
    be = _FakeBackend()
    out_path = "/hist/nodes/a.out.md"
    be.files[out_path] = b"RESULT_A"

    spec = parse_dag_spec(
        {
            "task_summary": "render output and inputs for one node",
            "nodes": [
                {
                    "id": "b",
                    "subagent": "x",
                    "prompt_template": "up={{ a.output }} path={{ a.output_path }} lit={{ inputs.k }}",
                    "depends_on": ["a"],
                    "inputs": {"k": "LITERAL"},
                }
            ],
        }
    )
    node = spec.nodes[0]
    rendered = await render_prompt(node, backend=be, cwd="/w", nodes_root="/hist/nodes")
    assert "RESULT_A" in rendered
    assert f"path={out_path}" in rendered
    assert "lit=LITERAL" in rendered


async def test_injected_content_is_fenced_but_the_template_is_not() -> None:
    """A node output is sub-agent-authored and a referenced file may hold
    anything a run fetched; neither is an instruction this prompt may carry.
    The template's own words, a literal input and the ``_path`` forms are the
    author's and stay verbatim."""
    be = _FakeBackend()
    out_path = "/hist/nodes/a.out.md"
    be.files[out_path] = b"RESULT_A"
    be.files["/w/notes.md"] = b"FILE_BODY"

    spec = parse_dag_spec(
        {
            "task_summary": "fence what is injected, not what was authored",
            "nodes": [
                {
                    "id": "b",
                    "subagent": "x",
                    "prompt_template": (
                        "AUTHORED up={{ a.output }} ref={{ ref:notes.md }} fin={{ inputs.f }} "
                        "path={{ a.output_path }} lit={{ inputs.k }}"
                    ),
                    "depends_on": ["a"],
                    "inputs": {"k": "LITERAL", "f": {"file": "notes.md"}},
                }
            ],
        }
    )
    rendered = await render_prompt(spec.nodes[0], backend=be, cwd="/w", nodes_root="/hist/nodes")

    assert "[BEGIN UNTRUSTED subagent" in rendered
    # Once for the bare ref, once for the file input: both inject contents.
    assert rendered.count("[BEGIN UNTRUSTED file") == 2
    assert "RESULT_A" in rendered
    assert rendered.count("FILE_BODY") == 2
    assert "AUTHORED" in rendered
    assert f"path={out_path}" in rendered
    assert "lit=LITERAL" in rendered


# --- store ---------------------------------------------------------------


def test_make_run_id_shape() -> None:
    rid = make_run_id()
    # "<UTC-timestamp>-<8 hex>"
    assert "-" in rid and len(rid.rsplit("-", 1)[1]) == 8


async def test_store_roundtrip() -> None:
    be = _FakeBackend()
    store = DagRunStore(be, "/w", "run123", nodes_root="/w/nodes", registry_root="/w")
    await store.init('{"nodes": []}')
    p = store.output_path("a")
    await store.write_text(p, "hello out")
    assert await store.read_text(p) == "hello out"
    assert store.run_dir.endswith("run123")


def test_a_run_keeps_only_run_scoped_files_in_its_run_dir() -> None:
    """graph.json and manifest.json describe the run; the node files do not."""
    from raven.agent.subagent.dag_store import DagRunStore

    be = _FakeBackend()
    store = DagRunStore(be, "/hist/mas_dag", "run123", nodes_root="/hist/nodes", registry_root="/hist")

    assert store.run_dir == "/hist/mas_dag/run123"
    assert store.output_path("a") == "/hist/nodes/a.out.md"
    assert store.prompt_path("a") == "/hist/nodes/a.prompt.md"
    assert store.memory_path("a") == "/hist/nodes/a.memory.json"
    assert store.transcript_path("a") == "/hist/nodes/a.transcript.jsonl"
    assert store.attempt_output_path("a", 2) == "/hist/nodes/a.attempt-2.out.md"
    assert store.attempt_prompt_path("a", 2) == "/hist/nodes/a.attempt-2.prompt.md"
    assert store.attempt_transcript_path("a", 2) == "/hist/nodes/a.attempt-2.transcript.jsonl"
    assert store.registry_root == "/hist"


def test_a_node_artifact_path_needs_no_run_id() -> None:
    """A node id is unique per conversation, so it locates its own files.

    The run id was only ever a directory the id happened to sit in; keeping it
    in the signature is what forced every resolver to consult the index before
    it could name a file.
    """
    from raven.agent.subagent.dag_store import memory_path_in, output_path_in
    from raven.agent.subagent.history import nodes_root

    be = _FakeBackend()

    assert output_path_in(be, "/hist/nodes", "market_scan") == "/hist/nodes/market_scan.out.md"
    assert memory_path_in(be, "/hist/nodes", "market_scan") == "/hist/nodes/market_scan.memory.json"
    assert nodes_root(Path("/sess")) == Path("/sess/subagents/nodes")


def test_a_tally_counts_not_executed_only_when_a_node_is_in_it() -> None:
    """A run without branches carries exactly five keys, in a fixed order; `not_executed` comes last when present."""
    from raven.agent.subagent.dag_store import tally_node_statuses

    plain = tally_node_statuses(["completed", "failed", "skipped", "cancelled", "exception"])
    assert plain == {"total": 5, "completed": 1, "failed": 1, "skipped": 1, "cancelled": 1}
    assert list(plain) == ["total", "completed", "failed", "skipped", "cancelled"]
    branching = tally_node_statuses(iter(["completed", "not_executed", "not_executed"]))
    assert branching == {"total": 3, "completed": 1, "failed": 0, "skipped": 0, "cancelled": 0, "not_executed": 2}
    assert list(branching)[-1] == "not_executed"


def test_the_tally_line_names_not_executed_only_when_there_is_one() -> None:
    from raven.agent.subagent.dag_store import format_tally

    assert format_tally({"total": 2, "completed": 2, "failed": 0, "skipped": 0, "cancelled": 0}) == (
        "2 completed, 0 failed, 0 cancelled, 0 skipped (of 2)"
    )
    assert format_tally({"total": 7, "completed": 3, "failed": 1, "skipped": 0, "cancelled": 0, "not_executed": 3}) == (
        "3 completed, 1 failed, 0 cancelled, 0 skipped, 3 not executed (of 7)"
    )
    assert format_tally({}) == "0 completed, 0 failed, 0 cancelled, 0 skipped (of 0)"


# --- reader ---------------------------------------------------------------


def _seed_run(be: "_FakeBackend", run_id: str, *, finalized: bool, root: str = "/hist/mas_dag") -> None:
    """Write a two-node run dir the way DagRunStore/_finalize would."""
    rdir = f"{root}/{run_id}"
    ndir = f"{root.rsplit('/', 1)[0]}/nodes"
    graph = {
        "task_summary": "the whole graph",
        "nodes": [
            {
                "id": "a",
                "subagent": "x",
                "node_summary": "do the thing",
                "prompt_template": "do {{ inputs.k }}",
                "depends_on": [],
                "instance": None,
                "inputs": {"k": "LITERAL", "spec": {"file": "/w/spec.md"}},
            },
            {
                "id": "b",
                "subagent": "y",
                "node_summary": "then use it",
                "prompt_template": "use {{ a.output }}",
                "depends_on": ["a"],
                "instance": "h",
            },
        ],
    }
    be.files[f"{rdir}/graph.json"] = json.dumps(graph).encode()
    be.files[f"{ndir}/a.prompt.md"] = b"do LITERAL"
    be.files[f"{ndir}/a.out.md"] = b"OUTPUT_A"
    be.files[f"{ndir}/a.memory.json"] = b"{}"
    if finalized:
        manifest = {
            "a": {
                "status": "completed",
                "subagent": "x",
                "depends_on": [],
                "instance": None,
                "started_at": 1000,
                "ended_at": 3000,
                "prompt_file": f"{ndir}/a.prompt.md",
                "output_file": f"{ndir}/a.out.md",
                "error": None,
            },
            "b": {
                "status": "failed",
                "subagent": "y",
                "depends_on": ["a"],
                "instance": "h",
                "started_at": 3000,
                "ended_at": 4000,
                "prompt_file": None,
                "output_file": None,
                "error": "boom",
            },
        }
        be.files[f"{rdir}/manifest.json"] = json.dumps(manifest).encode()


async def test_read_run_rebuilds_a_finalized_manifest() -> None:
    be = _FakeBackend()
    _seed_run(be, "20260730T060242Z-6b0b89a3", finalized=True)

    run = await read_run(be, "/hist/mas_dag", "20260730T060242Z-6b0b89a3", "/hist/nodes")

    assert run["finalized"] is True
    assert run["summary"] == {"total": 2, "completed": 1, "failed": 1, "skipped": 0, "cancelled": 0}
    a, b = run["files"]
    assert (a["node"], a["status"], a["started_at"], a["ended_at"]) == ("a", "completed", 1000, 3000)
    assert a["prompt_template"] == "do {{ inputs.k }}"
    # The other half of the template: without it every `{{ inputs.k }}` in the
    # line above is a key with no visible source.
    assert a["inputs"] == {"k": "LITERAL", "spec": {"file": "/w/spec.md"}}
    assert b["inputs"] is None
    assert (b["node"], b["status"], b["error"], b["instance"]) == ("b", "failed", "boom", "h")
    assert b["depends_on"] == ["a"]
    assert run["task_summary"] == "the whole graph"
    assert (a["node_summary"], b["node_summary"]) == ("do the thing", "then use it")
    assert a["memory_file"] == "/hist/nodes/a.memory.json"
    assert b["memory_file"] is None, "a memory file that was never written reads back as absent"
    assert a["prompt_file"] == "/hist/nodes/a.prompt.md"
    assert a["output_file"] == "/hist/nodes/a.out.md"
    assert b["prompt_file"] is None, "a manifest null is trusted, and nothing was derived either"
    assert b["output_file"] is None


async def test_read_run_of_an_unfinalized_run_falls_back_to_the_graph() -> None:
    be = _FakeBackend()
    _seed_run(be, "20260730T060242Z-6b0b89a3", finalized=False)

    run = await read_run(be, "/hist/mas_dag", "20260730T060242Z-6b0b89a3", "/hist/nodes")

    # Structure survives without manifest.json; state does not, so every node
    # reads back pending and the caller is expected to overlay live state.
    assert run["finalized"] is False
    assert [f["node"] for f in run["files"]] == ["a", "b"]
    assert {f["status"] for f in run["files"]} == {"pending"}
    assert run["files"][1]["prompt_template"] == "use {{ a.output }}"
    # Structure includes what each node was handed: it lives in graph.json, which
    # is written before the first node runs, so an in-flight run has it too.
    assert run["files"][0]["inputs"] == {"k": "LITERAL", "spec": {"file": "/w/spec.md"}}
    assert run["task_summary"] == "the whole graph"
    assert run["files"][0]["node_summary"] == "do the thing"
    assert run["files"][0]["memory_file"] is not None
    assert run["files"][0]["prompt_file"] is not None
    assert run["files"][0]["output_file"] is not None


async def test_read_run_without_a_run_dir_raises() -> None:
    with pytest.raises(DagReadError):
        await read_run(_FakeBackend(), "/w", "20260730T060242Z-6b0b89a3", "/w/nodes")


@pytest.mark.parametrize("run_id", ["../../etc", "not-a-run-id", "", "20260730T060242Z-ZZZZZZZZ"])
async def test_read_run_rejects_a_malformed_run_id(run_id: str) -> None:
    with pytest.raises(DagReadError):
        await read_run(_FakeBackend(), "/w", run_id, "/w/nodes")


@pytest.mark.parametrize("node_id", ["../graph", "a/b", "a.out", ""])
async def test_read_node_rejects_a_malformed_node_id(node_id: str) -> None:
    be = _FakeBackend()
    _seed_run(be, "20260730T060242Z-6b0b89a3", finalized=True)
    with pytest.raises(DagReadError):
        await read_node(be, "/hist/mas_dag", "20260730T060242Z-6b0b89a3", node_id, "/hist/nodes")


async def test_read_node_returns_the_rendered_prompt_and_output() -> None:
    be = _FakeBackend()
    _seed_run(be, "20260730T060242Z-6b0b89a3", finalized=True)

    node = await read_node(be, "/hist/mas_dag", "20260730T060242Z-6b0b89a3", "a", "/hist/nodes")

    assert node["prompt"] == "do LITERAL"
    assert node["prompt_file"] == "/hist/nodes/a.prompt.md"
    assert node["output"] == "OUTPUT_A"
    assert node["output_file"] == "/hist/nodes/a.out.md"
    assert node["output_chars"] == 8
    assert node["output_truncated"] is False


async def test_read_node_truncates_a_long_output_and_says_so() -> None:
    be = _FakeBackend()
    run_id = "20260730T060242Z-6b0b89a3"
    _seed_run(be, run_id, finalized=True)
    be.files["/hist/nodes/a.out.md"] = b"x" * 5000

    node = await read_node(be, "/hist/mas_dag", run_id, "a", "/hist/nodes", max_output_chars=100)

    assert len(node["output"]) == 100
    assert node["output_chars"] == 5000
    assert node["output_truncated"] is True


async def test_read_node_of_a_node_that_never_ran_is_empty_not_an_error() -> None:
    be = _FakeBackend()
    _seed_run(be, "20260730T060242Z-6b0b89a3", finalized=True)

    node = await read_node(be, "/hist/mas_dag", "20260730T060242Z-6b0b89a3", "b", "/hist/nodes")

    assert node["prompt"] is None
    assert node["output"] is None
    assert node["output_chars"] == 0


def _seed_legacy_run(be: "_FakeBackend", run_id: str) -> None:
    """A run written before this session's history flattened.

    Its manifest names each artifact by an absolute path under the run dir --
    the shape every run on disk has today. Nothing of it lives at the flat node
    root, which is exactly what makes an id reused there later ambiguous.
    """
    rdir = f"/hist/mas_dag/{run_id}"
    be.files[f"{rdir}/graph.json"] = json.dumps(
        {"task_summary": "old", "nodes": [{"id": "plan", "subagent": "x", "prompt_template": "p", "depends_on": []}]}
    ).encode()
    be.files[f"{rdir}/plan.prompt.md"] = b"OLD RUN'S PROMPT"
    be.files[f"{rdir}/plan.out.md"] = b"OLD RUN'S ANSWER"
    be.files[f"{rdir}/manifest.json"] = json.dumps(
        {
            "plan": {
                "status": "completed",
                "subagent": "x",
                "depends_on": [],
                "instance": None,
                "started_at": 1,
                "ended_at": 2,
                "prompt_file": f"{rdir}/plan.prompt.md",
                "output_file": f"{rdir}/plan.out.md",
                "error": None,
            }
        }
    ).encode()


async def test_read_node_trusts_the_manifests_path_like_read_run_does() -> None:
    """A legacy run's node must not be served from a newer node holding its id.

    `read_run` trusts an explicitly named file and only derives a candidate when
    the manifest carries none. `read_node` deriving unconditionally makes the two
    readers of one `(run_id, node)` disagree, and the one `dag.node` opens serves
    the newer node's prompt and output under the old run's identity and status.
    """
    be = _FakeBackend()
    _seed_legacy_run(be, "20260730T060242Z-6b0b89a3")
    be.files["/hist/nodes/plan.out.md"] = b"A DIFFERENT, NEWER NODE"
    be.files["/hist/nodes/plan.prompt.md"] = b"A DIFFERENT, NEWER PROMPT"

    node = await read_node(be, "/hist/mas_dag", "20260730T060242Z-6b0b89a3", "plan", "/hist/nodes")
    run = await read_run(be, "/hist/mas_dag", "20260730T060242Z-6b0b89a3", "/hist/nodes")

    assert node["output"] == "OLD RUN'S ANSWER"
    assert node["prompt"] == "OLD RUN'S PROMPT"
    entry = next(f for f in run["files"] if f["node"] == "plan")
    assert node["output_file"] == entry["output_file"], "the two readers must name one file"
    assert node["prompt_file"] == entry["prompt_file"]


async def test_read_node_honours_a_manifest_that_records_the_node_with_no_output() -> None:
    """The same defect, one branch over: a recorded `None` is an answer too.

    A skipped or failed node's manifest entry carries `"output_file": null`.
    Treating that as "nothing named, derive one" reaches the flat root exactly
    like the unrecorded case does, and serves a later node's output under this
    one's identity -- for a node that provably wrote nothing.
    """
    be = _FakeBackend()
    run_id = "20260730T060242Z-6b0b89a3"
    rdir = f"/hist/mas_dag/{run_id}"
    be.files[f"{rdir}/graph.json"] = json.dumps(
        {"task_summary": "old", "nodes": [{"id": "plan", "subagent": "x", "prompt_template": "p", "depends_on": []}]}
    ).encode()
    be.files[f"{rdir}/manifest.json"] = json.dumps(
        {"plan": {"status": "skipped", "prompt_file": None, "output_file": None, "error": None}}
    ).encode()
    be.files["/hist/nodes/plan.out.md"] = b"A DIFFERENT, NEWER NODE"

    node = await read_node(be, "/hist/mas_dag", run_id, "plan", "/hist/nodes")

    assert node["output"] is None
    assert node["output_file"] is None
    assert node["status"] == "skipped"


async def test_read_node_still_derives_from_the_flat_root_when_the_manifest_names_nothing() -> None:
    """The control for the test above: deriving is right when nothing was named.

    An unfinalized run has no manifest at all, and a node that has just written
    its output is readable well before the run finalizes -- so removing the
    derivation entirely would break the live case this branch added it for.
    """
    be = _FakeBackend()
    _seed_run(be, "20260730T060242Z-6b0b89a3", finalized=False)

    node = await read_node(be, "/hist/mas_dag", "20260730T060242Z-6b0b89a3", "a", "/hist/nodes")

    assert node["output"] == "OUTPUT_A"
    assert node["output_file"] == "/hist/nodes/a.out.md"


async def test_read_node_returns_its_own_transcript() -> None:
    be = _FakeBackend()
    _seed_run(be, "20260730T060242Z-6b0b89a3", finalized=True)
    be.files["/hist/nodes/a.transcript.jsonl"] = (
        b'{"role": "user", "content": "do LITERAL"}\n{"role": "assistant", "content": "OUTPUT_A"}\n'
    )

    node = await read_node(be, "/hist/mas_dag", "20260730T060242Z-6b0b89a3", "a", "/hist/nodes")

    assert node["transcript"] == [
        {"role": "user", "content": "do LITERAL"},
        {"role": "assistant", "content": "OUTPUT_A"},
    ]


# --- replan ----------------------------------------------------------------

# A real, well-formed run id: _check_run_id runs the moment prepare_replan
# reads the old graph, well before anything this suite is actually about.
# A workdir reference that lands in the node root only reaches the node-root rule where it
# passes confinement first.
_POSIX_REFERENCE_PATHS = pytest.mark.skipif(
    os.name == "nt",
    reason=(
        "check_confined resolves a workdir reference with POSIX path rules, so on Windows it refuses "
        "every such reference as outside the roots before the node-root rule is reached"
    ),
)


_OLD_RUN_ID = "20260904T000000Z-00000001"


def _node(node_id: str, *, depends_on: list[str] | None = None, prompt: str = "do it") -> dict:
    """A minimal, schema-valid replan node dict, matching `_seed_run`'s own nodes."""
    return {
        "id": node_id,
        "subagent": "x",
        "node_summary": "a replan node",
        "prompt_template": prompt,
        "depends_on": depends_on or [],
    }


def _set_seeded_confirm(tool: "SubAgentDagTool", run_id: str, value: bool) -> None:
    """Rewrite the seeded graph.json with a top-level `confirm` key.

    `_seed_run` writes none, which parses as False; a replan inherits the
    confirmation gate from the old run's own spec, so exercising the declined
    path needs it set on the seeded graph explicitly.
    """
    path = f"{tool._run_root(None)}/{run_id}/graph.json"
    graph = json.loads(tool._backend.files[path].decode())
    graph["confirm"] = value
    tool._backend.files[path] = json.dumps(graph).encode()


def _claim_the_id_from_under_it(tool: "SubAgentDagTool", run_id: str, node_id: str) -> None:
    """Record `node_id` as already claimed by `run_id` in this session's registry.

    A fresh run's own dispatch claims its node ids into the same registry;
    seeding a claim here ahead of time is what makes that claim collide.
    """
    path = f"{tool._run_root(None).rsplit('/', 1)[0]}/nodes.json"
    registry = json.loads(tool._backend.files[path].decode()) if path in tool._backend.files else {}
    registry.setdefault("nodes", {})[node_id] = {"run_id": run_id, "status": "running"}
    registry.setdefault("runs", []).append({"run_id": run_id, "nodes": [node_id]})
    tool._backend.files[path] = json.dumps(registry).encode()


def _recording_announce(sink: list) -> Any:
    """An announcer of the shape `_final_announcer` awaits.

    A plain lambda satisfies the constructor but blows up on the `await` inside
    the announcer, so a test that means to assert "nothing was announced" fails
    with a TypeError instead of showing what got announced.
    """

    async def _announce(run_id: str, _text: str, _origin: dict) -> None:
        sink.append(run_id)

    return _announce


async def _replannable_tool(tmp_path, **kw) -> tuple["SubAgentDagTool", dict]:
    """A real graph tool over a seeded, still-unfinalized old run.

    `finalized=False` on purpose: that is the state a replan is decided in, and it
    is what makes the overlay load-bearing -- an unfinalized run's index entry
    carries no per-node status, so every node of it reads back `running`.

    `_seed_run`'s own literal `/hist/mas_dag` prefix is a convention shared with
    the direct, root-as-argument reader calls around it. A real `SubAgentDagTool`
    resolves its own root through `SessionManager` instead
    (`<workspace>/sessions/<channel>/<chat>/subagents/mas_dag`), so the seed is
    written there -- computed from the same tool, after `set_context`, before
    `prepare_replan`'s own graph.json read ever runs -- rather than at the
    literal prefix the low-level reader tests use.

    That same `_run_root` call is what makes `SessionManager` create this
    workspace's `sessions/` directory for real, which is what lets
    `_session_nodes` consult the backend at all instead of short-circuiting to
    empty. What it reads from there is still the fake backend's own
    `index.json`, seeded here as the old run claiming both seeded nodes with
    no per-node status -- again, a run that has not finalized.

    `agents=` registers "x" for real: `prepare_replan` runs the replacement
    graph through the same `_preflight` a submitted one would, which refuses
    an unregistered sub-agent name -- unlike the graph/render/store tests
    elsewhere in this file, this is the one path in it that actually checks.
    """
    from raven.agent.subagent.dag_tool import SubAgentDagTool

    be = _FakeBackend()
    tool = SubAgentDagTool(
        workspace=tmp_path,
        agents=[ThirdPartyCliSubagentConfig(name="x", command="true")],
        **kw,
    )
    tool.set_context("cli", "direct", None)
    root = tool._run_root(None)
    history = root.rsplit("/", 1)[0]
    _seed_run(be, _OLD_RUN_ID, finalized=False, root=root)
    be.files[f"{history}/nodes.json"] = json.dumps(
        {
            "nodes": {n: {"run_id": _OLD_RUN_ID} for n in ("a", "b")},
            "runs": [{"run_id": _OLD_RUN_ID, "nodes": ["a", "b"]}],
        }
    ).encode()
    tool._backend = be
    # `output_file` beside `status`: a real live read is `read_run_reconciled`,
    # whose entries carry the resolved path (or None). The overlay reads both
    # halves, because a completed node that wrote nothing is not referenceable.
    live = {
        "files": [
            {"node": "a", "status": "exception", "output_file": None},
            {"node": "b", "status": "completed", "output_file": f"{history}/nodes/b.out.md"},
        ]
    }
    return tool, live


async def test_is_awaiting_decision_reads_the_runs_own_desk(tmp_path) -> None:
    """The predicate the resolve tool consults before it pays for a replan. It has
    to answer per (run, node) off the live desk, not per run: a graph suspends one
    node at a time and the answer for a sibling is exactly the mistake this gate
    exists to make free."""
    from raven.agent.subagent.dag_adjudication import AdjudicationDesk
    from raven.agent.subagent.dag_tool import SubAgentDagTool

    tool = SubAgentDagTool(workspace=tmp_path, agents=[ThirdPartyCliSubagentConfig(name="x", command="true")])

    assert tool.is_awaiting_decision(_OLD_RUN_ID, "a") is False, "no desk means no run to answer for"

    desk = AdjudicationDesk()
    tool._desks[_OLD_RUN_ID] = desk
    assert tool.is_awaiting_decision(_OLD_RUN_ID, "a") is False, "a desk with nothing open awaits nothing"

    desk.open("a")
    assert tool.is_awaiting_decision(_OLD_RUN_ID, "a") is True
    assert tool.is_awaiting_decision(_OLD_RUN_ID, "b") is False, "one node's suspension is not another's"
    assert tool.is_awaiting_decision("20260101T000000000000Z-deadbeef", "a") is False

    desk.resolve("a", "replan", "the plan was wrong")
    assert tool.is_awaiting_decision(_OLD_RUN_ID, "a") is True, (
        "an answered but unconsumed node is still on the desk; only take/close retires it"
    )
    desk.take("a")
    assert tool.is_awaiting_decision(_OLD_RUN_ID, "a") is False


async def test_replanning_refuses_a_redeclared_id(tmp_path) -> None:
    """`b` belongs to the old run forever, whatever became of it."""
    tool, live = await _replannable_tool(tmp_path)

    out = await tool.prepare_replan(_OLD_RUN_ID, "a", [_node("b")], "the plan was wrong", None, live)

    assert isinstance(out, str)
    assert f"already used by run '{_OLD_RUN_ID}'" in out
    assert "depends_on" in out, "the refusal has to say how to reuse it instead"


async def test_replanning_allows_a_reference_to_a_completed_node_before_the_index_is_written(tmp_path) -> None:
    """The old run has not finalized, so is_readable is False for every node of it.

    This is the test the overlay exists for: without it, `b` reads back `running`
    from the index and a reference to a plainly-completed node is refused.
    """
    tool, live = await _replannable_tool(tmp_path)

    plan = await tool.prepare_replan(
        _OLD_RUN_ID,
        "a",
        [_node("fresh", depends_on=["b"], prompt="use {{ b.output }}")],
        "the plan was wrong",
        None,
        live,
    )

    assert not isinstance(plan, str), plan
    assert [n.id for n in plan.nodes] == ["fresh"]
    assert plan.from_node == "a"
    assert plan.run_id and plan.run_id != _OLD_RUN_ID, "the successor id is minted up front"


async def test_replanning_refuses_a_reference_to_a_live_completed_node_that_wrote_nothing(tmp_path) -> None:
    """The overlay carries the output half, not just the status half.

    Without it every node the overlay marks completed reads back unreadable and
    the sibling test above fails; carrying `status` alone instead makes this one
    pass a node whose output does not exist. Both halves come from the same live
    entry, which is what the finalize about to happen will write.
    """
    tool, _ = await _replannable_tool(tmp_path)
    live = {"files": [{"node": "b", "status": "completed", "output_file": None}]}

    out = await tool.prepare_replan(
        _OLD_RUN_ID, "a", [_node("fresh", depends_on=["b"], prompt="use {{ b.output }}")], "wrong", None, live
    )

    assert isinstance(out, str), out
    assert "b" in out


@_POSIX_REFERENCE_PATHS
async def test_replanning_refuses_an_absolute_path_to_a_node_that_may_not_execute(tmp_path) -> None:
    """A replacement graph is held to the rules a submitted one is, the node root by any
    spelling included: refused here, it never becomes a run that fails on a missing file."""
    tool, live = await _replannable_tool(tmp_path)
    artifacts = f"{tool._history_root()}/nodes"
    replacement = [
        {**_node("r2"), "routes": {"a": "the first holds", "b": "the second holds"}},
        {**_node("g2", depends_on=["r2"]), "when": "r2.a"},
        {
            **_node("reader2", depends_on=["r2", "g2"], prompt="{{ ref:" + artifacts + "/g2.out.md }}"),
            "when": "r2.done",
        },
    ]

    out = await tool.prepare_replan(_OLD_RUN_ID, "a", replacement, "the plan was wrong", None, live)

    assert isinstance(out, str)
    assert "of node 'g2', which may not execute" in out


async def test_replanning_refuses_a_reference_to_a_node_that_did_not_complete(tmp_path) -> None:
    tool, live = await _replannable_tool(tmp_path)
    live = {"files": [{"node": "a", "status": "exception"}, {"node": "b", "status": "running"}]}

    out = await tool.prepare_replan(_OLD_RUN_ID, "a", [_node("fresh", depends_on=["b"])], "wrong", None, live)

    assert isinstance(out, str)
    assert "b" in out


async def test_replanning_refuses_while_delegation_is_paused(tmp_path) -> None:
    tool, live = await _replannable_tool(tmp_path, is_paused=lambda: True)

    out = await tool.prepare_replan(_OLD_RUN_ID, "a", [_node("fresh")], "wrong", None, live)

    assert isinstance(out, str)
    assert "delegation is paused" in out


async def test_replanning_charges_the_dispatch_quota(tmp_path) -> None:
    charged: list[str | None] = []

    def _charge(key: str | None) -> None:
        charged.append(key)
        return None

    tool, live = await _replannable_tool(tmp_path, charge=_charge)

    await tool.prepare_replan(_OLD_RUN_ID, "a", [_node("fresh")], "wrong", None, live)

    assert len(charged) == 1, "a replan submits a new graph, which is the unit this quota counts"


async def test_a_spent_quota_refuses_the_replan(tmp_path) -> None:
    tool, live = await _replannable_tool(tmp_path, charge=lambda key: "Error: rate limit")

    out = await tool.prepare_replan(_OLD_RUN_ID, "a", [_node("fresh")], "wrong", None, live)

    assert out == "Error: rate limit"


async def test_a_declined_confirmation_refuses_the_replan(tmp_path) -> None:
    async def _ask(conversation: str, question: str) -> bool:
        return False

    tool, live = await _replannable_tool(tmp_path, ask=_ask)
    # The gate is inherited from the old run's spec, so the seeded graph has to
    # carry it -- `_seed_run` writes no `confirm` key, which parses as False.
    _set_seeded_confirm(tool, _OLD_RUN_ID, True)

    out = await tool.prepare_replan(_OLD_RUN_ID, "a", [_node("fresh")], "wrong", None, live)

    assert isinstance(out, str)
    assert "did not approve" in out
    # Pin the corrected wording: the old run is still going, not already stopped.
    assert "still running as submitted" in out
    assert "already stopped" not in out


async def test_the_link_is_recorded_on_the_old_runs_graph_json(tmp_path) -> None:
    tool, live = await _replannable_tool(tmp_path)
    plan = await tool.prepare_replan(_OLD_RUN_ID, "a", [_node("fresh")], "the plan was wrong", None, live)

    await tool.start_replan(_OLD_RUN_ID, plan)

    graph = json.loads(tool._backend.files[f"{tool._run_root(None)}/{_OLD_RUN_ID}/graph.json"].decode())
    assert graph["replan"]["run_id"] == plan.run_id
    assert graph["replan"]["from_node"] == "a"
    assert graph["replan"]["reason"] == "the plan was wrong"
    assert graph["replan"]["started"] is True
    assert graph["replan"]["decided_at"] > 0
    assert graph["nodes"], "the original graph is still there; the key is added beside it"


async def test_the_successors_spec_inherits_task_summary_and_confirm_from_the_old_run(tmp_path) -> None:
    """`_submit_replan` builds the successor's spec from the plan's own
    `task_summary`/`confirm`, not from the plan's `reason` string and a bare
    `False` standing in for fields nobody asked the old run about.
    """
    captured: dict[str, object] = {}

    tool, live = await _replannable_tool(tmp_path)
    plan = await tool.prepare_replan(_OLD_RUN_ID, "a", [_node("fresh")], "the plan was wrong", None, live)

    async def _fake_run(spec, *_args, **_kwargs):
        from raven.agent.subagent.dag_runner import DagRunResult

        captured["task_summary"] = spec.task_summary
        captured["confirm"] = spec.confirm
        return DagRunResult(run_id=plan.run_id, dir="/d")

    tool._run = _fake_run

    await tool.start_replan(_OLD_RUN_ID, plan)
    task = tool._runs.get(plan.run_id)
    assert task is not None, "the successor must have been dispatched as its own task"
    await task

    assert captured["task_summary"] == "the whole graph", "the old run's task_summary, not its replan reason"
    assert captured["confirm"] is False, "the old run's own confirm gate, not a hardcoded False"


async def test_prepare_replan_tolerates_a_graph_already_carrying_a_replan_link(tmp_path) -> None:
    """A second replan attempt on the same run must still parse its graph.json,
    which by now carries the first attempt's `replan` key -- a key `parse_dag_spec`
    rejects as unknown unless it is stripped before parsing.

    The first `start_replan` is only here to seed that key, so its successor is
    stubbed out and awaited: a real one dispatches a live background run that
    outlives the test and parks the event loop's teardown.
    """
    from raven.agent.subagent.dag_adjudication import ReplanPlan
    from raven.agent.subagent.dag_runner import DagRunResult

    tool, live = await _replannable_tool(tmp_path)
    first = await tool.prepare_replan(_OLD_RUN_ID, "a", [_node("fresh")], "first try", None, live)

    async def _fake_run(*_args, **_kwargs):
        return DagRunResult(run_id=first.run_id, dir="/d")

    tool._run = _fake_run
    await tool.start_replan(_OLD_RUN_ID, first)
    await tool._runs[first.run_id]

    graph = json.loads(tool._backend.files[f"{tool._run_root(None)}/{_OLD_RUN_ID}/graph.json"].decode())
    assert "replan" in graph, "the seed for this test: a link must already be on the graph"

    second = await tool.prepare_replan(_OLD_RUN_ID, "a", [_node("again")], "second try", None, live)

    assert isinstance(second, ReplanPlan), "must parse past the stray `replan` key rather than raising"


async def test_a_refused_submission_still_records_the_link_as_unstarted(tmp_path) -> None:
    """A dangling reason is worse than a record saying the successor never ran."""
    tool, live = await _replannable_tool(tmp_path)
    plan = await tool.prepare_replan(_OLD_RUN_ID, "a", [_node("fresh")], "wrong", None, live)
    _claim_the_id_from_under_it(tool, plan.run_id, "fresh")

    out = await tool.start_replan(_OLD_RUN_ID, plan)

    graph = json.loads(tool._backend.files[f"{tool._run_root(None)}/{_OLD_RUN_ID}/graph.json"].decode())
    assert graph["replan"]["started"] is False
    assert graph["replan"]["error"]
    assert "Error" in str(getattr(out, "model_text", out))


async def test_a_bound_foreground_replan_returns_the_successors_first_event(tmp_path) -> None:
    tool, live = await _replannable_tool(tmp_path)
    plan = await tool.prepare_replan(_OLD_RUN_ID, "a", [_node("fresh")], "wrong", None, live)

    out = await tool.start_replan(_OLD_RUN_ID, plan, bound=True)

    assert plan.run_id in str(getattr(out, "model_text", out))
    assert "started in the background" not in str(getattr(out, "model_text", out))


async def test_a_replanned_run_does_not_announce_its_own_outcome(tmp_path) -> None:
    """`_run_detached` is the announce site. There is no seam to hand it a
    `DagRunResult` directly, so `_run` is stubbed to hand one back instead --
    driving the real branch rather than adding a production-only test hook.
    """
    import asyncio

    from raven.agent.subagent.dag_runner import DagRunResult

    announced: list[str] = []
    tool, live = await _replannable_tool(tmp_path, announce=_recording_announce(announced))

    async def _fake_run(*_args, **_kwargs):
        return DagRunResult(run_id="r1", dir="/d", replanned_into="run-new")

    tool._run = _fake_run
    await tool._run_detached(object(), "r1", asyncio.Event(), object(), object(), None, frozenset(), {}, None)

    assert announced == [], "the resolve call already told the agent; a second telling is narration"


async def test_a_replanned_run_stays_silent_through_a_released_outbox_too(tmp_path) -> None:
    """The suppression above must win over the outbox branch, not just the
    `outbox is None` half of it that the sibling test exercises -- a released
    outbox stays in `_outboxes` exactly like a bound one, so `put_final` would
    reach it here too and announce the raw `DagRunResult` were the check
    ordered the other way around.
    """
    import asyncio

    from raven.agent.subagent.dag_adjudication import Outbox
    from raven.agent.subagent.dag_runner import DagRunResult

    announced: list[str] = []
    tool, live = await _replannable_tool(tmp_path, announce=_recording_announce(announced))

    async def _fake_run(*_args, **_kwargs):
        return DagRunResult(run_id="r1", dir="/d", replanned_into="run-new")

    tool._run = _fake_run
    outbox = Outbox(
        conversation="cli:direct",
        announce_report=tool._report_announcer("r1", tool._default_origin),
        announce_final=tool._final_announcer("r1", tool._default_origin),
    )
    await outbox.release(flush=True)
    assert not outbox.bound, "the released lane this test means to cover"

    await tool._run_detached(object(), "r1", asyncio.Event(), object(), object(), None, frozenset(), {}, outbox)

    assert announced == [], "released is still an outbox in `_outboxes`; a replan must silence it too"


async def test_emit_replanned_sends_the_wire_event(tmp_path) -> None:
    """The control tool's own entry point, once resolve_node has succeeded."""
    published: list[tuple[str, dict]] = []

    async def _publish(name: str, value: dict) -> None:
        published.append((name, value))

    tool, live = await _replannable_tool(tmp_path, progress_publisher=_publish)
    plan = await tool.prepare_replan(_OLD_RUN_ID, "a", [_node("fresh")], "the plan was wrong", None, live)

    await tool.emit_replanned(_OLD_RUN_ID, plan)

    assert published == [
        (
            "dag_run_replanned",
            {
                "run_id": _OLD_RUN_ID,
                "replan_run_id": plan.run_id,
                "from_node": "a",
                "reason": "the plan was wrong",
            },
        )
    ]


async def test_start_replan_no_longer_sends_the_wire_event(tmp_path) -> None:
    """Emitting moved to the control tool; this call only dispatches and records."""
    published: list[tuple[str, dict]] = []

    async def _publish(name: str, value: dict) -> None:
        published.append((name, value))

    tool, live = await _replannable_tool(tmp_path, progress_publisher=_publish)
    plan = await tool.prepare_replan(_OLD_RUN_ID, "a", [_node("fresh")], "the plan was wrong", None, live)

    await tool.start_replan(_OLD_RUN_ID, plan)

    assert published == []


# --- reference roots -----------------------------------------------------

_ROOTS = ("/work", "/home/agent")


@pytest.mark.parametrize(
    "path",
    [
        "notes.md",
        "sub/notes.md",
        "/work/notes.md",
        "/home/agent/sessions/web/s1/subagents/mas_dag/r1/a.out.md",
        "../home/agent/user_memory/facts.md",
    ],
)
def test_a_reference_landing_in_either_root_is_accepted(path: str) -> None:
    check_confined(path, what="ref", roots=_ROOTS)


@pytest.mark.parametrize("path", ["/etc/passwd", "../../etc/passwd", "/home/agent-other/x.md"])
def test_a_reference_landing_outside_every_root_is_rejected(path: str) -> None:
    with pytest.raises(DagValidationError, match="outside"):
        check_confined(path, what="ref", roots=_ROOTS)


@pytest.mark.parametrize("path", ["/work/notes.md", "../elsewhere/notes.md"])
def test_without_roots_a_reference_must_stay_relative(path: str) -> None:
    with pytest.raises(DagValidationError, match="must be relative"):
        check_confined(path, what="ref")


def test_the_nodes_prefix_cannot_escape_the_node_root() -> None:
    check_confined("@nodes/a.out.md", what="ref", roots=_ROOTS)
    with pytest.raises(DagValidationError, match="node artifacts"):
        check_confined("@nodes/../../etc/passwd", what="ref", roots=_ROOTS)
    with pytest.raises(DagValidationError, match="empty"):
        check_confined("@nodes/", what="ref", roots=_ROOTS)


def test_split_reference_names_the_root() -> None:
    assert split_reference("@nodes/a.out.md") == ("nodes", "a.out.md")
    assert split_reference("notes.md") == ("workdir", "notes.md")


@pytest.mark.parametrize("path", ["@nodes/../../secret.md", "@nodes/..", "@nodes//etc/passwd"])
def test_a_nodes_prefixed_reference_cannot_escape_the_node_root(path: str) -> None:
    """Checked lexically against its own prefix, like the runs prefix was.

    Resolution happens later and against a different root, so a prefix that
    escaped here would escape before anything compared paths.
    """
    with pytest.raises(DagValidationError):
        check_confined(path, what="ref", roots=None)


# --- render across runs --------------------------------------------------


def _one_node(template: str, **extra: object) -> object:
    return parse_dag_spec(
        {
            "task_summary": "render a single node prompt",
            "nodes": [{"id": "n", "subagent": "x", "prompt_template": template, **extra}],
        }
    ).nodes[0]


async def test_render_reaches_another_node_through_the_nodes_prefix() -> None:
    be = _FakeBackend()
    be.files["/hist/nodes/plan.out.md"] = b"EARLIER"

    node = _one_node("text={{ ref:@nodes/plan.out.md }} path={{ ref_path:@nodes/plan.out.md }}")
    rendered = await render_prompt(node, backend=be, cwd="/w", nodes_root="/hist/nodes")

    assert "EARLIER" in rendered
    assert "path=/hist/nodes/plan.out.md" in rendered


async def test_a_nodes_reference_is_refused_when_no_node_root_is_known() -> None:
    node = _one_node("{{ ref:@nodes/plan.out.md }}")
    with pytest.raises(DagValidationError, match="node artifacts"):
        await render_prompt(node, backend=_FakeBackend(), cwd="/w")


async def test_an_earlier_runs_output_resolves_without_the_registry() -> None:
    """Flat, the id is the whole address, so path resolution reads no registry.

    Readability still does -- a failed node is refused at validation. This is
    only about turning an id into a filename.
    """
    be = _FakeBackend()
    be.files["/hist/nodes/scan.out.md"] = b"WHAT SCAN FOUND"
    node = _one_node("build on {{ scan.output }}", id="new", depends_on=["scan"])

    rendered = await render_prompt(
        node,
        backend=be,
        cwd="/w",
        nodes_root="/hist/nodes",
    )

    assert "WHAT SCAN FOUND" in rendered


@pytest.mark.parametrize(
    ("template", "extra"),
    [
        ("{{ ref_path:gone.md }}", {}),
        ("{{ inputs.k.path }}", {"inputs": {"k": {"file": "gone.md"}}}),
    ],
)
async def test_a_path_placeholder_naming_a_missing_file_is_refused(template: str, extra: dict) -> None:
    node = _one_node(template, **extra)
    with pytest.raises(DagValidationError, match="is not a file it can read"):
        await render_prompt(node, backend=_FakeBackend(), cwd="/w")


# --- who may set a shared session's skills -------------------------------


def test_a_later_node_on_a_shared_instance_cannot_set_skills() -> None:
    """A resumed session keeps the skill menu it was opened with.

    So a list on the second node is read by nobody. The older rule demanded every
    member declare the *same* skills, which let the file repeat it where it had no
    effect -- reading as if each step configured its own tools. Refused instead, so
    the file cannot say something untrue.
    """
    spec = _spec(
        {"id": "open", "subagent": "x", "prompt_template": "go", "instance": "s", "skills": ["a"]},
        {
            "id": "later",
            "subagent": "x",
            "prompt_template": "go",
            "instance": "s",
            "depends_on": ["open"],
            "skills": ["b"],
        },
    )
    with pytest.raises(DagValidationError) as exc:
        validate_capabilities(spec, _FULL)

    message = str(exc.value)
    assert "['later']" in message  # the offender, not the node that may set them
    assert "keeps the skill menu it was opened with" in message
    assert "open" in message  # and where to move them


def test_the_node_that_opens_the_session_may_set_skills() -> None:
    spec = _spec(
        {"id": "open", "subagent": "x", "prompt_template": "go", "instance": "s", "skills": ["a"]},
        {"id": "later", "subagent": "x", "prompt_template": "go", "instance": "s", "depends_on": ["open"]},
    )
    assert validate_capabilities(spec, _FULL) == []


def test_a_later_node_on_a_shared_instance_may_replace_its_mcps() -> None:
    """A grant is resolved per dispatch, not stored in the session's prompt.

    Every backend rebuilds its tool list from the grant *that* node resolved, and
    an acp peer is handed the list as a replacement -- so unlike ``skills``, a
    continuation node's ``mcps`` takes effect. Refusing it here would leave a
    stateful group unable to change or clear its grant between turns, with the
    replacement path unreachable from the graph tool.
    """
    for later_mcps in (["db"], ["other"], []):
        spec = _spec(
            {"id": "open", "subagent": "x", "prompt_template": "go", "instance": "s", "mcps": ["db"]},
            {
                "id": "later",
                "subagent": "x",
                "prompt_template": "go",
                "instance": "s",
                "depends_on": ["open"],
                "mcps": later_mcps,
            },
        )
        assert validate_capabilities(spec, _FULL) == [], f"refused mcps={later_mcps!r}"


def test_mcps_alone_does_not_demand_a_dependency_chain() -> None:
    """The chain requirement exists because only the node that *opens* a session
    can set its skills, so which member opens it has to be decided. ``mcps`` has
    no such first-node semantics -- each node's list applies to its own turn --
    so an unchained group declaring only ``mcps`` is answerable as written."""
    spec = _spec(
        {"id": "a", "subagent": "x", "prompt_template": "go", "instance": "s", "mcps": ["db"]},
        {"id": "b", "subagent": "x", "prompt_template": "go", "instance": "s", "mcps": []},
    )
    assert validate_capabilities(spec, _FULL) == []


def test_skills_stays_restricted_when_mcps_travels_beside_it() -> None:
    """The two fields split here, so a node carrying both is judged on ``skills``
    alone -- neither exempted by the ``mcps`` beside it nor dragging it down."""
    spec = _spec(
        {"id": "open", "subagent": "x", "prompt_template": "go", "instance": "s", "skills": ["a"]},
        {
            "id": "later",
            "subagent": "x",
            "prompt_template": "go",
            "instance": "s",
            "depends_on": ["open"],
            "skills": ["b"],
            "mcps": ["db"],
        },
    )
    with pytest.raises(DagValidationError) as exc:
        validate_capabilities(spec, _FULL)
    message = str(exc.value)
    assert "['later']" in message
    assert "'mcps'" not in message.split("(")[0]  # the refusal names skills, not mcps


def test_an_undecided_order_is_refused_only_when_it_would_matter() -> None:
    """Which member opens the session is undecided without a chain.

    The runner serializes a shared-instance group but does not pin the order among
    nodes with no mutual dependency, so *whose* skills take effect is unanswerable.
    The check fires only when someone actually declared some -- a group that
    declares nothing is unaffected, which is what keeps this from being a new
    restriction on every graph that shares a handle.
    """
    declaring = _spec(
        {"id": "a", "subagent": "x", "prompt_template": "go", "instance": "s", "skills": ["k"]},
        {"id": "b", "subagent": "x", "prompt_template": "go", "instance": "s"},
    )
    with pytest.raises(DagValidationError, match="do not form a dependency chain"):
        validate_capabilities(declaring, _FULL)

    silent = _spec(
        {"id": "a", "subagent": "x", "prompt_template": "go", "instance": "s"},
        {"id": "b", "subagent": "x", "prompt_template": "go", "instance": "s"},
    )
    assert validate_capabilities(silent, _FULL) == []


# --- capability gaps are reported, not refused ---------------------------


def test_skills_for_an_agent_with_no_menu_are_not_a_capability_gap() -> None:
    """They used to be noticed as ignored. Now they are quoted into the node's
    prompt by `dag_skills.fold_skills`, so the capability check has nothing to
    say about them; only a name the catalog lacks is worth a line, and that
    module says it."""
    spec = _spec({"id": "a", "subagent": "x", "prompt_template": "go", "skills": ["research"]})
    caps = {"x": AgentCapabilities(injectable_skills=False)}

    assert validate_capabilities(spec, caps) == []


def test_mcp_injection_is_checked_per_agent_capability() -> None:
    spec = _spec(
        {"id": "supported", "subagent": "x", "prompt_template": "go", "mcps": ["github"]},
        {"id": "unsupported", "subagent": "y", "prompt_template": "go", "mcps": ["github"]},
    )
    capabilities = {
        "x": AgentCapabilities(injectable_mcps=True),
        "y": AgentCapabilities(injectable_mcps=False),
    }

    notices = validate_capabilities(spec, capabilities)

    assert len(notices) == 1
    assert "'unsupported'" in notices[0]
    assert "agent 'y'" in notices[0]
    assert "ignored" in notices[0]


def test_a_graph_that_asks_for_nothing_extra_gets_no_notices() -> None:
    spec = _spec({"id": "a", "subagent": "x", "prompt_template": "go"})
    assert validate_capabilities(spec, _FULL) == []


def test_a_shared_session_ordered_through_a_non_member_is_accepted() -> None:
    """The shape the shipped orchestration guide teaches, and it was refused.

    `draft(author) -> review -> revise(author)`: the two members share no direct
    edge, so head detection over direct dependencies alone called them unordered
    and rejected the graph -- asserting something false, since the topological sort
    runs `draft` first. Adding `skills` to `draft` is exactly what the guide's own
    field table invites, and that was the trigger.
    """
    spec = _spec(
        {"id": "draft", "subagent": "x", "prompt_template": "write", "instance": "author", "skills": ["w"]},
        {"id": "review", "subagent": "x", "prompt_template": "critique {{ draft.output }}", "depends_on": ["draft"]},
        {
            "id": "revise",
            "subagent": "x",
            "prompt_template": "revise {{ review.output }}",
            "depends_on": ["review"],
            "instance": "author",
        },
    )

    assert validate_capabilities(spec, _FULL) == []


def test_the_model_is_not_offered_skills_or_mcps() -> None:
    """The graph tool's own parameters carry neither field.

    They are playbook-only and the private playbook path passes them to the node
    model directly. Asserted on the advertised schema rather than on the model,
    since that is the half a re-addition would show up in.
    """
    # `routes`, `select` and `when` are the exception and the next test's subject:
    # a node's branch fields have to be writable, since the schema is the only way
    # a model can put one in a graph.
    assert set(_NODE_SCHEMA["properties"]) == {
        "id",
        "subagent",
        "node_summary",
        "prompt_template",
        "depends_on",
        "inputs",
        "instance",
        "routes",
        "select",
        "when",
    }
    # Still parseable, so a playbook's own nodes[] survives the round trip.
    spec = parse_dag_spec(
        {
            "task_summary": "run a lone node carrying skills",
            "nodes": [{"id": "a", "subagent": "x", "prompt_template": "hi", "skills": ["s"]}],
        }
    )
    assert spec.nodes[0].skills == ["s"]


def test_the_node_schema_declares_the_three_branch_fields() -> None:
    """The advertised schema is the only way a model writes a branch.

    Each property is pinned to its shape and to the rules the validator enforces,
    not just to its presence: a route name's charset, its reserved `done` and its
    case rule, the route count each `select` needs, the enum, and the guard's atoms,
    operators and depends_on rule. None is required: a graph that uses no branch
    writes none of them.
    """
    properties = _NODE_SCHEMA["properties"]
    assert not {"routes", "select", "when"} & set(_NODE_SCHEMA["required"])

    assert properties["routes"]["type"] == "object"
    assert properties["routes"]["additionalProperties"] == {"type": "string"}
    routes_doc = properties["routes"]["description"]
    assert "Raven's judge picks from its answer" in routes_doc
    assert "nothing the node writes selects a route by itself" in routes_doc
    assert '[A-Za-z0-9_-], not "done", and no two differing only in case' in routes_doc
    # The count each `select` needs, stated against the select value that needs it.
    assert 'two or more for "one", one or more for "many"' in routes_doc

    assert properties["select"]["type"] == "string"
    assert properties["select"]["enum"] == ["one", "many"]
    select_doc = properties["select"]["description"]
    assert '"one" (the default) -- exactly one' in select_doc
    assert '"many" -- any number, including none' in select_doc
    assert "Only on a node that declares routes" in select_doc

    assert properties["when"]["type"] == "string"
    when_doc = properties["when"]["description"]
    assert "Atoms <node>.<route> and <node>.done" in when_doc
    assert "not, and, or and parentheses" in when_doc
    # `r.a AND r.b` does not parse, and `r.a or r.b and r.c` reads as `r.a or (r.b and r.c)`:
    # the keywords are lowercase, and the guide body gives precedence in full.
    assert "the keywords are lowercase" in when_doc
    assert "Every decision node in depends_on must appear here" in when_doc
    # The rule is that the decision node appears, not that `.done` will do: `.done` holds
    # whichever route was selected, which is right for a merge and wrong for a branch node.
    # Both readings of the old branch-half sentence are pinned out, since either one lets
    # a branch node ride whichever route won.
    assert "if it runs whichever route is selected" not in when_doc
    assert "whose route does not matter" not in when_doc


def test_the_when_description_says_which_nodes_of_a_branch_need_its_route() -> None:
    """A node is left out with a branch only when every dependency it has in the graph
    was left out. A node of the branch that also depends on something outside it --
    the decision node itself included -- runs on the route not taken unless its own
    guard names the route, so the schema may not tell the model that the entry's guard
    covers it. The rule is the branch's own: a merge node meant to run on every route,
    like the guide's `reply`, keeps `<node>.done`, and unscoped the rule would forbid it."""
    when_doc = _NODE_SCHEMA["properties"]["when"]["description"]

    assert "A branch node needs no guard beyond naming any decision node it depends on" in when_doc
    assert (
        "a node of the branch that also depends on something outside it, the decision node included, needs "
        "the branch's route, not <node>.done"
    ) in when_doc
    assert "A merge node that should run on every route keeps <node>.done." in when_doc
    assert "one that also depends on a node outside the branch" not in when_doc
    assert "Only a branch's first node needs a guard" not in when_doc


def _guide_text() -> str:
    """The orchestration guide's own words, for rules that now live only there."""
    import raven as raven_pkg

    return (
        pathlib.Path(raven_pkg.__file__).parent / "memory_engine/skills/subagent-dag-orchestration/SKILL.md"
    ).read_text()


def test_the_guide_gives_the_operator_precedence_the_schema_leaves_to_it() -> None:
    """The schema states the operators and their lowercase spelling; the binding order
    (`not` > `and` > `or`, so `r.a or r.b and r.c` reads as `r.a or (r.b and r.c)`) is
    in the guide. A model that assumed left-to-right would read the same guard the other
    way, so the rule keeps a pin wherever it is stated."""
    guide = _guide_text()

    assert "`not` binds tightest, then `and`, then `or`" in guide


def test_the_guide_says_a_false_or_unknown_guard_leaves_the_node_not_executed() -> None:
    """`not_executed` covers both a guard that came out false and one that came out
    unknown, and the two reasons are distinguishable in the manifest. Unknown is not
    false -- an atom naming a route of a decision that never ran is unknown, not
    false -- so a guard `not risk.high` does not run the node when risk was never
    judged, and the reason says which happened."""
    guide = _guide_text()

    assert "False leaves it **not executed** with the reason" in guide
    assert "`when_false`" in guide
    assert "unknown leaves it not executed with `when_unknown`" in guide
    assert "Unknown is not false" in guide


def test_the_prompt_template_description_says_a_missing_output_has_a_stand_in() -> None:
    """A merge step reads one sentence where a branch's node was never run, and a
    `ref:@nodes/` path to a node that may not run is refused.

    Both are facts the graph's author has to design around: the first is the text
    the dependent actually receives, and the second is a refusal it earns at
    submission.
    """
    described = _NODE_SCHEMA["properties"]["prompt_template"]["description"]

    assert MISSING_OUTPUT_TEXT in described
    assert "write one titled block per output that may be missing" in described
    assert "rather than a ref:@nodes/ path, which is refused" in described
    # What "may not run" covers, which is what the refusal reads: `.done` counts, even
    # though it holds whichever route is selected.
    assert "A node may not run with a when of its own (<node>.done included)" in described
    assert "in-graph dependencies all may not run" in described


def test_the_stand_in_is_promised_only_for_a_node_of_the_same_graph() -> None:
    """The fixed sentence stands in only for a node of the graph being submitted. A
    node an earlier run did not execute is refused like one that failed, so the
    schema, the guide and the glossary list it with the refused ones rather than
    promising the sentence."""
    described = _NODE_SCHEMA["properties"]["prompt_template"]["description"]

    assert "failed, skipped, cancelled, not-executed or still-running one is refused, saying which" in described
    assert "A node of this graph that was not executed (see when) leaves no output file" in described
    assert "A node that was not executed (see when)" not in described

    earlier = _flat(_heading_span(_repo_text(_SKILL), "### Reading a task this conversation already finished"))
    assert "One that failed, was cancelled, was skipped, was not executed, or is still running keeps its id" in earlier

    glossary = _repo_text("CONTEXT.md")
    template = _flat(_glossary_entry(glossary, "**Prompt template**"))
    assert (
        "a node that failed, was skipped, was cancelled, was not executed or is still running is refused with "
        "which of those it was"
    ) in template
    node_id = _flat(_glossary_entry(glossary, "**Node id**"))
    assert "A failed, skipped, cancelled, not-executed or still-running one" in node_id


# --- upstream memory records appended to a node's prompt ---------------------


def _memory_spec(nodes: list[dict]):
    return parse_dag_spec({"task_summary": "run a small memory chain of nodes", "nodes": nodes})


_CHAIN = [
    {"id": "a", "subagent": "x", "prompt_template": "first"},
    {"id": "b", "subagent": "x", "prompt_template": "second", "depends_on": ["a"]},
    {"id": "c", "subagent": "x", "prompt_template": "third", "depends_on": ["b"]},
]


async def test_a_node_is_told_its_upstream_memory_paths() -> None:
    spec = _memory_spec(_CHAIN)
    by_id = {n.id: n for n in spec.nodes}
    rendered = await render_prompt(
        by_id["c"],
        backend=_FakeBackend(),
        cwd="/w",
        nodes_root="/hist/nodes",
        run_id="run1",
        by_id=by_id,
    )
    # Transitive: c depends on b, which depends on a.
    assert "/hist/nodes/b.memory.json" in rendered
    assert "/hist/nodes/a.memory.json" in rendered
    # Its own record is not upstream of itself.
    assert "c.memory.json" not in rendered


async def test_a_root_node_is_told_nothing() -> None:
    spec = _memory_spec(_CHAIN)
    by_id = {n.id: n for n in spec.nodes}
    rendered = await render_prompt(
        by_id["a"],
        backend=_FakeBackend(),
        cwd="/w",
        nodes_root="/hist/nodes",
        run_id="run1",
        by_id=by_id,
    )
    assert rendered == "first"


async def test_the_block_says_what_to_do_when_a_record_is_absent() -> None:
    spec = _memory_spec(_CHAIN)
    by_id = {n.id: n for n in spec.nodes}
    rendered = await render_prompt(
        by_id["b"],
        backend=_FakeBackend(),
        cwd="/w",
        nodes_root="/hist/nodes",
        run_id="run1",
        by_id=by_id,
    )
    # Written asynchronously after a node finishes, so absence is the common
    # case and the node has to be told to carry on rather than wait or fail.
    assert "may not exist yet" in rendered
    assert "proceed without it" in rendered


async def test_an_agent_that_cannot_read_local_files_is_told_nothing() -> None:
    spec = _memory_spec(_CHAIN)
    by_id = {n.id: n for n in spec.nodes}
    rendered = await render_prompt(
        by_id["c"],
        backend=_FakeBackend(),
        cwd="/w",
        nodes_root="/hist/nodes",
        run_id="run1",
        by_id=by_id,
        capabilities={"x": AgentCapabilities(reads_local_files=False)},
    )
    assert rendered == "third"
    assert "memory.json" not in rendered


async def test_a_cross_run_upstream_gets_the_same_memory_path() -> None:
    spec = _memory_spec([{"id": "d", "subagent": "x", "prompt_template": "only", "depends_on": ["earlier"]}])
    by_id = {n.id: n for n in spec.nodes}
    rendered = await render_prompt(
        by_id["d"],
        backend=_FakeBackend(),
        cwd="/w",
        nodes_root="/hist/nodes",
        run_id="run2",
        by_id=by_id,
    )
    assert "/hist/nodes/earlier.memory.json" in rendered


def _node_with_inputs(template: str, inputs: dict[str, object]) -> dict[str, object]:
    return {
        "id": "a",
        "subagent": "x",
        "node_summary": "carry material into the prompt",
        "prompt_template": template,
        "inputs": inputs,
    }


def test_a_graph_declaring_an_input_no_placeholder_names_is_refused() -> None:
    """The graph gate, so nothing is dispatched.

    A node used to validate and render with the declared material simply absent
    from its prompt: the sub-agent worked from what was left, and the run still
    read as finished. `validate_and_order` is where a graph is refused whole, so
    it is where this belongs -- the renderer would already be mid-run.
    """
    spec = parse_dag_spec(
        {
            "task_summary": "declare material and never reference it",
            "nodes": [_node_with_inputs("do the task", {"silently_lost": "important material"})],
        }
    )

    with pytest.raises(DagValidationError, match="node 'a' input 'silently_lost' is declared but never referenced"):
        validate_and_order(spec)


async def test_the_renderer_refuses_it_too_whoever_called(tmp_path: Path) -> None:
    """The same question at the second gate.

    `render_prompt` has its own interpolation loop and never reaches the shared
    renderer's, so a caller that skips validation -- which the tests here do
    routinely -- would still drop the material silently.
    """
    spec = parse_dag_spec(
        {
            "task_summary": "declare material and never reference it",
            "nodes": [_node_with_inputs("do the task", {"silently_lost": "important material"})],
        }
    )

    with pytest.raises(DagValidationError, match="declared but never referenced"):
        await render_prompt(spec.nodes[0], backend=LocalFileBackend(), cwd=str(tmp_path))


async def test_a_referenced_input_still_renders_where_it_is_named(tmp_path: Path) -> None:
    spec = parse_dag_spec(
        {
            "task_summary": "reference the material",
            "nodes": [_node_with_inputs("use {{ inputs.brief }} now", {"brief": "keep it short"})],
        }
    )

    assert validate_and_order(spec) == ["a"]
    rendered = await render_prompt(spec.nodes[0], backend=LocalFileBackend(), cwd=str(tmp_path))
    assert rendered == "use keep it short now"


def test_subagent_dag_config_defaults():
    from raven.config.raven import RavenConfig

    cfg = RavenConfig().subagent_dag
    assert cfg.verdict_model is None
    assert cfg.verdict_timeout_seconds == 180.0
    assert cfg.evidence_budget_chars == 8000
    assert cfg.adjudication_timeout_seconds == 600.0
    assert cfg.max_continuations == 2


def test_adjudication_timeout_is_capped_at_an_hour():
    import pytest as _pytest
    from pydantic import ValidationError

    from raven.config.raven import SubagentDagConfig

    assert SubagentDagConfig(adjudication_timeout_seconds=3600.0).adjudication_timeout_seconds == 3600.0
    with _pytest.raises(ValidationError):
        SubagentDagConfig(adjudication_timeout_seconds=3601.0)


def test_dag_tool_accepts_a_provider_and_verdict_config(tmp_path):
    from raven.agent.subagent.dag_tool import SubAgentDagTool
    from raven.config.raven import SubagentDagConfig

    sentinel = object()
    cfg = SubagentDagConfig(verdict_model="cheap-tier")
    tool = SubAgentDagTool(workspace=tmp_path, provider_for=lambda: sentinel, verdict_config=cfg)
    assert tool._provider_for() is sentinel
    assert tool._verdict_config.verdict_model == "cheap-tier"


def test_dag_tool_without_a_provider_still_builds(tmp_path):
    from raven.agent.subagent.dag_tool import SubAgentDagTool

    tool = SubAgentDagTool(workspace=tmp_path)
    assert tool._provider_for is None


async def test_the_judge_resolves_its_provider_at_dispatch_not_at_construction(tmp_path):
    """The loop's `provider` is a property over the running turn's binding, so a
    tool built at startup that captured its value would send every judge call
    through the default binding -- a session that switched model would never
    reach the judge, and a verdictModel from another vendor would fail open.
    """
    import raven.agent.subagent.dag_tool as tool_mod
    from raven.agent.subagent.dag_tool import SubAgentDagTool
    from raven.agent.subagent.dag_verdict import Verdict

    seen: list[object] = []

    async def _fake_judge(provider, **kwargs):
        seen.append(provider)
        return Verdict(accomplished=True)

    live = ["first"]
    tool = SubAgentDagTool(workspace=tmp_path, provider_for=lambda: live[0])
    judge_node = tool._judge_node()
    assert judge_node is not None

    class _Store:
        async def read_text(self, _path):
            return ""

        def transcript_path(self, _node_id):
            return "t"

        def prompt_path(self, _node_id):
            return "p"

    class _Node:
        id = "a"

    original = tool_mod.judge
    tool_mod.judge = _fake_judge
    try:
        await judge_node(node=_Node(), store=_Store(), output="o", error="", crashed=False)
        live[0] = "switched"
        await judge_node(node=_Node(), store=_Store(), output="o", error="", crashed=False)
    finally:
        tool_mod.judge = original

    assert seen == ["first", "switched"], f"the judge captured its provider instead of resolving it: {seen}"


def test_agent_loop_passes_its_subagent_dag_config_to_the_registered_tool(tmp_path):
    from raven.agent.loop import AgentLoop
    from raven.config.raven import SubagentDagConfig
    from raven.providers.base import LLMProvider, LLMResponse

    class _StubProvider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")

        async def chat(
            self,
            messages,
            tools=None,
            model=None,
            max_tokens=4096,
            temperature=0.7,
            reasoning_effort=None,
            tool_choice=None,
        ):
            return LLMResponse(content="stub", finish_reason="stop")

        def get_default_model(self) -> str:
            return "stub"

    non_default = SubagentDagConfig(verdict_model="cheap-tier")
    loop = AgentLoop(
        provider=_StubProvider(),
        workspace=tmp_path,
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
        subagents=SubagentWiring(subagent_dag_config=non_default),
    )

    tool = loop.tools.get("run_subagent_dag")
    assert tool is not None
    assert tool._verdict_config.verdict_model == "cheap-tier"


@pytest.mark.asyncio
async def test_the_host_side_questions_wait_their_turn_on_the_conversation(tmp_path, monkeypatch):
    """`_direct_ask` and `_confirm_graph` serialize on the conversation's question
    lock like every other asker, and the wait draws on their own budget.

    A plugin gate asks from inside a tool call and a foreground graph runs
    several of those at once, so two host-side askers -- or one and a relayed
    sub-agent holding the lock for its own question -- would otherwise evict
    each other from the broker's single pending slot. Busy past the deadline
    reads as no answer: None for the grant, a refusal for the confirm gate, and
    the broker never sees the question.
    """
    import asyncio

    from raven.acp_client.asker import question_lock
    from raven.agent.loop import AgentLoop
    from raven.providers.base import LLMProvider, LLMResponse
    from raven.rpc.question_broker import QuestionBroker

    class _StubProvider(LLMProvider):
        def __init__(self) -> None:
            super().__init__(api_key="test")

        async def chat(self, messages, tools=None, model=None, max_tokens=4096, temperature=0.7, **kwargs):
            return LLMResponse(content="stub", finish_reason="stop")

        def get_default_model(self) -> str:
            return "stub"

    sent: list[str] = []

    async def send_frame(frame: dict) -> None:
        sent.append(frame["params"]["question"])
        if frame["params"]["question"] != "slow node":
            broker.reply(frame["params"]["conversation_id"], "Continue")

    broker = QuestionBroker(send_frame, timeout_s=5.0)
    loop = AgentLoop(
        provider=_StubProvider(),
        workspace=tmp_path,
        model="stub",
        policy=TurnPolicy(max_iterations=2),
        tools=ToolWiring(restrict_to_workspace=True),
    )
    loop.tools.get("ask_user").set_broker(broker)

    # The confirm gate has no per-call budget of its own; it waits the tool's
    # default, shortened here so the busy branch is observable.
    monkeypatch.setattr("raven.agent.loop.wiring.DEFAULT_TIMEOUT_S", 0.1)
    lock = question_lock("web:sess1")
    await lock.acquire()
    try:
        assert await loop._direct_ask("which entity?", None, "web:sess1") is None
        assert await asyncio.wait_for(loop._confirm_graph("web:sess1", "Run this graph?"), timeout=2.0) is False
    finally:
        lock.release()
    assert sent == []

    assert await loop._direct_ask("which entity?", None, "web:sess1", timeout_s=1.0) == "Continue"
    assert sent == ["which entity?"]

    # One budget, not two in series: a lock had late leaves the question only
    # what is left of the round's budget. The lock is held for ~0.3s of a 0.6s
    # round, so the budget the broker is handed must be what remained, never a
    # fresh 0.6s. Asserted on the budget itself, not on wall-clock elapsed: a
    # loaded CI box stretches the hold and would blur any timing window.
    budgets: list[float] = []
    real_await = broker.await_question

    async def spy(conversation_id: str, **kwargs):
        budgets.append(kwargs["timeout_s"])
        return await real_await(conversation_id, **kwargs)

    monkeypatch.setattr(broker, "await_question", spy)

    async def hold_briefly() -> None:
        await lock.acquire()
        await asyncio.sleep(0.3)
        lock.release()

    holder = asyncio.ensure_future(hold_briefly())
    await asyncio.sleep(0)
    answer = await loop._direct_ask("slow node", None, "web:sess1", timeout_s=0.6)
    await holder
    assert answer == ""
    assert len(budgets) == 1 and 0.0 < budgets[0] <= 0.6 - 0.25, budgets


def test_every_state_unreadable_answers_gets_a_sentence_of_its_own() -> None:
    """`pending` and `exception` reach `_unreadable` only through a replan.

    Named for the states this function answers, not for the ones the overlay can
    carry: `interrupted` is a member of the second set and not the first. A run
    that is unfinalized and held by no tool any more has its nodes reconciled to
    that value (`dag_resume`), and it falls to the caller's unknown-id fallback
    here, as it did before these two branches existed. Deliberately not asserted
    either way, so adding a branch for it later needs no change to this test.

    The overlay reads that run as it stands rather than as it finalized, so it
    can hand these two through -- states a finalized manifest never records, and
    which fell past every branch here. The caller's generic fallback then said
    the id was unknown, while the uniqueness check refuses re-declaring it as
    already claimed: two messages that cannot both be acted on.

    Reached only from `prepare_replan`, and only to refuse it: every branch here
    is raised as a `DagValidationError`, which `prepare_replan` returns before
    `resolve_node` is called, so at the moment any of this is read the old run is
    still going as submitted. `pending` therefore states the node's state in the
    present and the loss as what replanning would do, which is what the assertion
    below pins -- describing a replacement as done would be false every time.

    The six settled states are the control. They are asserted here too, so a
    regression that flattens this function into one message cannot pass by
    answering only the two new ones.
    """
    from raven.agent.subagent.dag_graph import _unreadable

    def refusal(state: str) -> str:
        known = SessionNodes(owner={"prev": "run-R"}, state={"prev": state})
        out = _unreadable("n1", "'prev'", "prev", known)
        assert out is not None, f"state {state!r} fell through to the unknown-id fallback"
        return out

    assert "failed in run 'run-R'" in refusal("failed")
    assert "skipped" in refusal("skipped")
    assert "stopped mid-run" in refusal("cancelled")
    assert "has not finished writing" in refusal("running")
    assert "recorded no outcome" in refusal("unrecorded")
    assert "did not execute" in refusal("not_executed")

    # Redo, not wait: a replan that lands discards the run these two belong to,
    # so its remaining nodes never produce anything -- which is why they must not
    # take RUNNING's "re-submit once that run reports".
    pending = refusal("pending")
    assert "has not started, and this replan would discard it unrun" in pending
    assert "was replaced" not in pending, "nothing has been replaced yet when this is read"
    assert "Re-do it under a new id" in pending
    assert "re-submit once that run reports" not in pending

    exception = refusal("exception")
    assert "could not accomplish its task" in exception
    assert "Re-do it under a new id" in exception
    assert "re-submit once that run reports" not in exception

    # An id no run ever claimed is still not a state, and still the caller's to phrase.
    assert _unreadable("n1", "'prev'", "prev", SessionNodes(owner={}, state={})) is None


def test_the_guide_and_the_refusal_agree_about_a_replans_unfinished_nodes() -> None:
    """The refusal sends the model to this guide, so the two cannot disagree.

    `_validation_error` appends `read_skill(GUIDE_SKILL_ID)` because a validation
    failure is the one moment the graph shape is known to be wrong, and the guide
    injects by `description` -- its body reaches the model through that call and
    nowhere else. So the sentence the refusal gives and the rule the guide gives
    are read one after the other, in the same breath.

    The guide's rule for a node of a run still in flight is to wait for that run
    to report. That is right for a fresh graph and wrong inside a replan, which
    discards the replaced run's unfinished nodes -- which is what `pending` and
    `exception` are, and why they are refused with a new id instead. Nothing else
    in the repo catches a disagreement here: `test_living_docs` checks that cited
    paths exist, not that claims about them hold.

    Whitespace is collapsed before matching so a later rewrap of the guide cannot
    void these assertions silently.
    """
    import raven.memory_engine
    from raven.agent.subagent.dag_graph import _unreadable
    from raven.agent.subagent.dag_tool import GUIDE_SKILL_ID

    native = GUIDE_SKILL_ID.split("/", 1)[1]
    guide = Path(raven.memory_engine.__file__).parent / "skills" / native / "SKILL.md"
    body = " ".join(guide.read_text(encoding="utf-8").split())

    for state in ("pending", "exception"):
        known = SessionNodes(owner={"prev": "run-R"}, state={"prev": state})
        message = _unreadable("n1", "'prev'", "prev", known) or ""
        assert "Re-do it under a new id" in message, f"{state} no longer asks for a new id"

    assert "submit again once that run reports its result" in body, "the fresh-graph rule is the premise"
    assert "inverts when you are replanning" in body, "the guide must carry the replan exception"
    assert "ask for a new id" in body, "and must give the advice the refusal above gives"
    assert "of the four" not in body, "a closed count over a set that has since grown"


async def test_await_finalized_does_not_return_until_the_run_task_has_ended(tmp_path) -> None:
    """The wait itself, which the replan tests around it could not see.

    Every other test of this call drives a stub that records being called, so
    they pin where it sits in the sequence and nothing about what it does there.
    Replacing the body with `return` left all of them green -- and the docstring's
    whole claim is that the successor's validation needs the predecessor's
    per-node outcomes on disk first, which is exactly what returning early would
    take away.

    So this drives the real method against a real pending task and asserts the
    order of two events, not that a call happened.
    """
    import asyncio

    tool, _ = await _replannable_tool(tmp_path)
    order: list[str] = []
    release = asyncio.Event()

    async def _run_task() -> None:
        await release.wait()
        order.append("run ended")

    tool._runs["run-old"] = asyncio.create_task(_run_task())

    async def _waiter() -> None:
        await tool.await_finalized("run-old")
        order.append("await_finalized returned")

    waiter = asyncio.create_task(_waiter())
    # A real interval, not one loop turn: the point is that it is parked, and a
    # single `sleep(0)` samples before it could have finished either way.
    await asyncio.sleep(0.05)
    assert not waiter.done(), "returned while the run it was told to wait for was still going"
    assert order == []

    release.set()
    await waiter
    assert order == ["run ended", "await_finalized returned"]


async def test_await_finalized_returns_at_once_for_a_run_this_instance_never_started(tmp_path) -> None:
    """The other half of the contract: a run id absent from this instance's own
    `_runs` is not waited on, because it is either already over or being run by a
    second graph tool instance -- which is why callers resolve the owner first."""
    import asyncio

    tool, _ = await _replannable_tool(tmp_path)

    async with asyncio.timeout(1):
        await tool.await_finalized("a-run-that-was-never-started-here")


async def test_a_run_is_listed_from_init_not_from_its_finalize() -> None:
    """`session_run_ids` has to see a run that is still going.

    It is intersected with the loop's live run set to scope what the control
    tools may list and cancel, so a run recorded only at finalize is invisible
    for exactly as long as it is running -- the whole window `dag_status` and
    `dag_cancel` exist for. The run-keyed index this replaced claimed its entry
    in `init` for the same reason.
    """
    from raven.agent.subagent.dag_store import DagRunStore, read_registry

    be = _FakeBackend()
    store = DagRunStore(be, "/hist/mas_dag", "run123", nodes_root="/hist/nodes", registry_root="/hist")

    await store.init(json.dumps({"task_summary": "t", "nodes": []}), ["a", "b"])

    registry = await read_registry(be, "/hist")
    assert [r["run_id"] for r in registry["runs"]] == ["run123"]
    assert sorted(registry["nodes"]) == ["a", "b"]


async def test_finalizing_updates_the_runs_entry_rather_than_adding_a_second() -> None:
    """The entry claimed at init is the one the summary lands on."""
    from raven.agent.subagent.dag_store import DagRunStore, read_registry

    be = _FakeBackend()
    store = DagRunStore(be, "/hist/mas_dag", "run123", nodes_root="/hist/nodes", registry_root="/hist")
    await store.init(json.dumps({"task_summary": "t", "nodes": []}), ["a", "b"])

    await store.record_outcome({"a": "completed", "b": "failed"}, "1/2 completed")

    registry = await read_registry(be, "/hist")
    assert len(registry["runs"]) == 1, "one run, one entry"
    assert registry["runs"][0]["summary"] == "1/2 completed"
    assert registry["runs"][0]["nodes"] == ["a", "b"]


async def test_the_registry_maps_a_node_id_straight_to_its_owner_and_outcome() -> None:
    """Node-keyed, because every question asked of it is asked about a node.

    The run-keyed shape made every reader walk runs to answer "what became of
    id x", and two readers walked it differently.
    """
    import json

    from raven.agent.subagent.dag_store import read_session_nodes

    be = _FakeBackend()
    be.files["/hist/nodes.json"] = json.dumps(
        {
            "nodes": {
                "scan": {"kind": "dag", "run_id": "r1", "status": "completed", "has_output": True},
                "draft": {"kind": "dag", "run_id": "r1", "status": "failed", "has_output": False},
            },
            "runs": [{"run_id": "r1", "summary": "1/2 completed", "nodes": ["scan", "draft"]}],
        }
    ).encode()

    known = await read_session_nodes(be, "/hist")

    assert known.owner == {"scan": "r1", "draft": "r1"}
    assert known.state == {"scan": "completed", "draft": "failed"}
    assert known.is_readable("scan") is True
    assert known.is_readable("draft") is False


async def test_a_node_that_completed_with_no_output_is_not_readable() -> None:
    """`completed` is the run's verdict; it does not promise a file.

    Overriding a reported `completed` to `failed` would misreport the run, so
    the writer records what it knows -- whether an output file was written --
    and the reader asks that second question separately. Measured on one
    machine: 57 of 85 spawn records have an out.md, and a DAG node that
    finishes with empty output has the same shape.
    """
    import json

    from raven.agent.subagent.dag_store import read_session_nodes

    be = _FakeBackend()
    be.files["/hist/nodes.json"] = json.dumps(
        {
            "nodes": {
                "loud": {"kind": "dag", "run_id": "r1", "status": "completed", "has_output": True},
                "quiet": {"kind": "dag", "run_id": "r1", "status": "completed", "has_output": False},
            },
            "runs": [],
        }
    ).encode()

    known = await read_session_nodes(be, "/hist")

    assert known.state["quiet"] == "completed"
    assert known.has_output == {"loud": True, "quiet": False}
    assert known.is_readable("loud") is True
    assert known.is_readable("quiet") is False


async def test_a_runless_node_claims_and_finalizes_in_the_same_registry() -> None:
    """One registry, two writers.

    A spawn has no run id, so it cannot claim through `DagRunStore`, and a
    second registry beside this one would let a spawn and a graph each believe
    it holds the same node id.
    """
    from raven.agent.subagent.dag_store import (
        RUNNING,
        claim_node,
        index_guard,
        read_session_nodes,
        record_node_outcome,
    )

    be = _FakeBackend()
    async with index_guard("/hist"):
        await claim_node(be, "/hist", "market_scan", kind="spawn", started_at_ms=1000)

    known = await read_session_nodes(be, "/hist")
    assert known.owner["market_scan"] == "spawn"
    assert known.state["market_scan"] == RUNNING
    # Claimed but still running: the id is taken and nothing can be read from
    # it yet, which is the whole window a spawn sits in after it returns.
    assert known.is_readable("market_scan") is False

    async with index_guard("/hist"):
        await record_node_outcome(be, "/hist", "market_scan", status="completed", has_output=True, ended_at_ms=2000)

    known = await read_session_nodes(be, "/hist")
    assert known.owner["market_scan"] == "spawn"
    assert known.is_readable("market_scan") is True


async def test_the_backstop_claims_an_id_nothing_holds() -> None:
    """A caller reaching the manager directly leaves no claim of its own.

    Its task would then have no registry row, so no listing draws it and no
    later task can reference it.
    """
    from raven.agent.subagent.dag_store import RUNNING, ensure_node_claimed, index_guard, read_session_nodes

    be = _FakeBackend()
    async with index_guard("/hist"):
        await ensure_node_claimed(be, "/hist", "scan", kind="spawn", started_at_ms=1000)

    known = await read_session_nodes(be, "/hist")
    assert known.owner["scan"] == "spawn"
    assert known.state["scan"] == RUNNING


async def test_the_backstop_leaves_a_finished_claim_exactly_as_it_found_it() -> None:
    """Its docstring's promise, and the one that costs something to break.

    Overwriting a held id would reset a `completed` node to `running` and to no
    output -- the node stays on disk and stops being referenceable, with the
    registry saying a finished task is still going. Case-folded like every other
    claim check, so a differently-cased id counts as held too.
    """
    from raven.agent.subagent.dag_store import (
        claim_node,
        ensure_node_claimed,
        index_guard,
        read_session_nodes,
        record_node_outcome,
    )

    be = _FakeBackend()
    async with index_guard("/hist"):
        await claim_node(be, "/hist", "Scan", kind="spawn", started_at_ms=1000)
        await record_node_outcome(be, "/hist", "Scan", status="completed", has_output=True, ended_at_ms=2000)

    async with index_guard("/hist"):
        await ensure_node_claimed(be, "/hist", "Scan", kind="spawn", started_at_ms=9999)
        await ensure_node_claimed(be, "/hist", "scan", kind="spawn", started_at_ms=9999)

    known = await read_session_nodes(be, "/hist")
    assert known.state == {"Scan": "completed"}, "no second row, and the outcome untouched"
    assert known.is_readable("Scan") is True


async def test_finalizing_a_runless_node_keeps_what_the_claim_wrote() -> None:
    """The outcome merges into the entry rather than replacing it -- a write
    that dropped `kind` would make the node's owner read as UNRECORDED, and the
    duplicate-id refusal names the owner."""
    from raven.agent.subagent.dag_store import claim_node, index_guard, read_registry, record_node_outcome

    be = _FakeBackend()
    async with index_guard("/hist"):
        await claim_node(be, "/hist", "scan", kind="spawn", started_at_ms=1000)
        await record_node_outcome(be, "/hist", "scan", status="failed", has_output=False, ended_at_ms=2000)

    entry = (await read_registry(be, "/hist"))["nodes"]["scan"]
    assert entry == {
        "kind": "spawn",
        "status": "failed",
        "has_output": False,
        "started_at_ms": 1000,
        "ended_at_ms": 2000,
    }


async def test_two_callers_racing_for_one_node_id_leave_exactly_one_winner() -> None:
    """The guard is what makes read-then-claim atomic.

    Both tasks read the registry before either writes, which is the interleaving
    that let two runs pass the uniqueness check and both claim an id. Held, the
    second read sees the first claim and refuses.
    """
    import asyncio

    from raven.agent.subagent.dag_store import claim_node, index_guard, read_session_nodes

    be = _FakeBackend()
    outcomes: list[str] = []

    async def contender(tag: str, hold: float) -> None:
        async with index_guard("/hist"):
            known = await read_session_nodes(be, "/hist")
            await asyncio.sleep(hold)
            if "plan" in known.owner:
                outcomes.append(f"{tag}:refused")
                return
            await claim_node(be, "/hist", "plan", kind=tag, started_at_ms=1)
            outcomes.append(f"{tag}:claimed")

    await asyncio.gather(contender("spawn", 0.02), contender("dag", 0.0))

    assert sorted(outcomes) == ["dag:claimed", "spawn:refused"] or sorted(outcomes) == [
        "dag:refused",
        "spawn:claimed",
    ], outcomes
    assert len([o for o in outcomes if o.endswith(":claimed")]) == 1


async def test_a_damaged_registry_degrades_to_empty_and_logs_why() -> None:
    """The registry is what keeps node ids unique per conversation, so a
    corrupt file must not fail the run reading it -- but degrading silently
    would let that uniqueness guarantee lapse without anyone noticing.
    """
    from loguru import logger

    from raven.agent.subagent.dag_store import read_session_nodes

    be = _FakeBackend()
    be.files["/hist/nodes.json"] = b"{not valid json"

    lines: list[str] = []
    sink_id = logger.add(lambda m: lines.append(str(m)), level="WARNING")
    try:
        known = await read_session_nodes(be, "/hist")
    finally:
        logger.remove(sink_id)

    assert known.owner == {}
    assert known.state == {}
    assert any("Node registry" in line and "unreadable" in line for line in lines)


async def test_a_registry_whose_halves_have_the_wrong_type_degrades_per_half() -> None:
    """A well-formed object can still carry a `nodes` or `runs` of the wrong shape.

    Both guards survived a mutation sweep of this module, so both are pinned
    here. Per half rather than wholesale: a damaged `runs` list must not cost
    the caller the node claims, which are what keep ids unique -- dropping
    those silently frees every id in the conversation.
    """
    import json

    from raven.agent.subagent.dag_store import read_registry, read_session_nodes

    be = _FakeBackend()
    be.files["/hist/nodes.json"] = json.dumps(
        {"nodes": {"plan": {"run_id": "r1", "status": "completed", "has_output": True}}, "runs": "not-a-list"}
    ).encode()

    registry = await read_registry(be, "/hist")
    assert registry["runs"] == [], "a runs value that is not a list reads as no runs"
    assert (await read_session_nodes(be, "/hist")).owner == {"plan": "r1"}, "the node claims survive it"

    be.files["/hist/nodes.json"] = json.dumps({"nodes": [{"id": "plan"}], "runs": [{"run_id": "r1"}]}).encode()

    registry = await read_registry(be, "/hist")
    assert registry["nodes"] == {}, "a nodes value that is not a dict reads as no nodes"
    assert registry["runs"] == [{"run_id": "r1"}], "the run list survives it"


async def test_a_registry_whose_run_entries_are_not_objects_drops_only_those() -> None:
    """`runs` is a list of dicts; a stray scalar in it must not cost the rest."""
    import json

    from raven.agent.subagent.dag_store import read_registry

    be = _FakeBackend()
    be.files["/hist/nodes.json"] = json.dumps({"nodes": {}, "runs": [{"run_id": "r1"}, "junk", 7]}).encode()

    assert (await read_registry(be, "/hist"))["runs"] == [{"run_id": "r1"}]


async def test_a_registry_that_is_not_a_json_object_also_degrades_to_empty() -> None:
    """Valid JSON that isn't an object -- a bare list, here -- is not a shape
    the registry can be a per-node dict lookup against, so it degrades the
    same way a corrupt file does. This path has no exception to log from, so
    unlike the corrupt-file case it degrades without a warning.
    """
    from raven.agent.subagent.dag_store import read_session_nodes

    be = _FakeBackend()
    be.files["/hist/nodes.json"] = b"[1, 2, 3]"

    known = await read_session_nodes(be, "/hist")

    assert known.owner == {}
    assert known.state == {}


def test_an_invented_node_field_gets_the_closed_set_in_one_message() -> None:
    """pydantic's extra_forbidden names the field but not the set it violated,
    and the measured recovery was another guess (a node carried 'mode',
    2026-09-01). The refusal now lists every legal field once."""
    with pytest.raises(DagValidationError) as exc:
        parse_dag_spec(
            {
                "task_summary": "an invented field is named and answered",
                "nodes": [
                    {
                        "id": "a",
                        "subagent": "x",
                        "node_summary": "go",
                        "prompt_template": "go",
                        "mode": "acceptEdits",
                    }
                ],
            }
        )
    message = str(exc.value)
    assert "'nodes.0.mode'" in message
    assert "instance" in message and "depends_on" in message and "inputs" in message
    assert "task_summary, nodes, confirm" in message


def test_the_oversized_graph_hint_names_the_ref_escape_hatch() -> None:
    """The graph JSON is the one call whose size scales with prose written into
    it, and the generic "smaller form" advice sent the model toward cutting
    content (measured 2026-09-02: a graph carrying the task rules verbatim in
    every node hit the reply cap mid-write). The tool's own hint has to name
    the escape hatch that already exists."""
    from raven.agent.subagent.dag_tool import SubAgentDagTool

    hint = SubAgentDagTool.incomplete_hint.fget(None)  # type: ignore[union-attr]
    assert "{{ ref:<path> }}" in hint
    assert "carries the reference, not the text" in hint
    assert "Do not shorten prompts by dropping task rules" in hint
    prompt_doc = _NODE_SCHEMA["properties"]["prompt_template"]["description"]
    assert "A long prompt belongs in a file" in prompt_doc


async def test_an_addressed_run_is_validated_against_the_conversation_it_names(tmp_path) -> None:
    """A stint resumed from a terminal or an RPC call has no turn, and validating
    its graph against the turn's registry found none of the roles the record
    said had finished: `depends on unknown r03-planner` on the very node the
    driver had just named as done. The run is written under the addressed
    conversation, so that is the registry a dependency on an earlier run is
    checked against."""
    from raven.agent.subagent.dag_store import SessionNodes
    from raven.agent.subagent.dag_tool import SubAgentDagTool, _DagOrigin

    tool = SubAgentDagTool(workspace=tmp_path, agents=[ThirdPartyCliSubagentConfig(name="x", command="true")])
    tool.set_context("cli", "direct", None)
    asked: list[str | None] = []

    async def session_nodes(session_key: str | None = None) -> SessionNodes:
        asked.append(session_key)
        return SessionNodes()

    tool.session_nodes = session_nodes  # type: ignore[method-assign]

    await tool._execute(
        [{"id": "a", "subagent": "x", "prompt_template": "hi", "depends_on": []}],
        background=True,
        task_summary="probe",
        origin=_DagOrigin(channel="web", chat_id="default", conversation="web:the-stint"),
    )
    assert asked[:1] == ["web:the-stint"]

    asked.clear()
    await tool._execute(
        [{"id": "b", "subagent": "x", "prompt_template": "hi", "depends_on": []}],
        background=True,
        task_summary="probe",
    )
    assert asked[:1] == [None], "an unaddressed run still reads the turn's own registry"


# --- branches: routes, select and when ---------------------------------------


def _branch_node(**over: object) -> dict:
    """A schema-valid node dict; keyword arguments override or add fields."""
    base: dict = {"id": "n", "subagent": "x", "node_summary": "s", "prompt_template": "p"}
    base.update(over)
    return base


def _decision(node_id: str = "r", **over: object) -> dict:
    """A decision node with two routes, `a` and `b`."""
    return _branch_node(id=node_id, routes={"a": "the first holds", "b": "the second holds"}, **over)


def _branch_nodes(nodes: list[dict]) -> list:
    return parse_dag_spec({"task_summary": "g", "nodes": nodes}).nodes


# What a session knows once an earlier run has completed a node called `old`.
_EARLIER_RUN_NODES = SessionNodes(state={"old": "completed"}, owner={"old": "r0"}, has_output={"old": True})


def _branch_graph(nodes: list[dict], *, session_nodes: SessionNodes | None = None) -> list[str]:
    return collect_static_graph_errors(_branch_nodes(nodes), session_nodes=session_nodes)


def test_a_node_spec_carries_routes_select_and_when_and_defaults_them_off() -> None:
    plain = _branch_nodes([_branch_node(id="a")])[0]
    assert (plain.routes, plain.select, plain.when) == (None, None, None)
    decision = _branch_nodes([_branch_node(id="r", select="many", routes={"a": "x"}, when="p.q")])[0]
    assert (decision.routes, decision.select, decision.when) == ({"a": "x"}, "many", "p.q")


def test_a_plain_node_dumps_without_the_branch_fields() -> None:
    """A graph that uses no branch persists the same bytes as before the fields existed.

    Playbook files and run records dump nodes with `exclude_none=True`, so a default of
    `""` for `when` would have added a `"when": ""` key to every node of every graph.
    """
    keys = {"routes", "select", "when"}
    plain = _branch_nodes([_branch_node(id="a")])[0]
    assert not keys & set(plain.model_dump(exclude_none=True, by_alias=True))
    branching = _branch_nodes([_branch_node(id="r", select="many", routes={"a": "x"}, when="p.q")])[0]
    assert keys <= set(branching.model_dump(exclude_none=True, by_alias=True))


@pytest.mark.parametrize("when", [None, "", "   "])
def test_an_unset_or_blank_when_means_no_guard(when: str | None) -> None:
    from raven.agent.subagent.dag_graph import guards_of, may_not_execute

    nodes = _branch_nodes([_branch_node(id="a", when=when), _branch_node(id="b", depends_on=["a"], when=when)])
    assert guards_of(nodes) == {}
    assert may_not_execute(nodes, {n.id: n for n in nodes}) == frozenset()
    assert collect_static_graph_errors(nodes) == []


def test_select_accepts_only_one_or_many() -> None:
    for value in ("one", "many"):
        node = _branch_nodes([_branch_node(id="r", select=value, routes={"a": "x", "b": "y"})])[0]
        assert node.select == value
    with pytest.raises(DagValidationError):
        _branch_nodes([_branch_node(id="r", select="some", routes={"a": "x", "b": "y"})])


def test_a_decision_node_needs_two_routes_for_one_and_one_for_many() -> None:
    one = _branch_graph([_branch_node(id="r", routes={"a": "cond a"})])
    assert one == ["node 'r' declares 1 route(s); a decision node with select 'one' needs at least two routes"]
    assert _branch_graph([_branch_node(id="r", select="many", routes={"a": "cond a"})]) == []
    empty = _branch_graph([_branch_node(id="r", select="many", routes={})])
    assert empty == ["node 'r' declares 0 route(s); a decision node with select 'many' needs at least one route"]


@pytest.mark.parametrize("name", ["done", "Done", "DONE"])
def test_a_route_named_done_is_reserved_in_any_letter_case(name: str) -> None:
    """`r.Done` would sit one letter case away from `r.done`, which means the node settled;
    the refusal names the spelling the author wrote."""
    errors = _branch_graph([_branch_node(id="r", routes={name: "x", "b": "y"})])
    assert errors == [
        f"node 'r': '{name}' is reserved as a route name -- it means the node settled, whatever it selected"
    ]


def test_a_route_name_must_use_the_node_id_charset() -> None:
    errors = _branch_graph([_branch_node(id="r", routes={"a b": "x", "c": "y"})])
    assert any("route name" in e for e in errors)
    trailing_newline = _branch_graph([_branch_node(id="r", routes={"a\n": "x", "c": "y"})])
    assert any("route name" in e for e in trailing_newline)


@pytest.mark.parametrize(
    "routes",
    [{"Bug": "x", "bug": "y"}, {"bug": "x", "Bug": "y"}],
    ids=["capitalised_first", "lowercase_first"],
)
def test_two_route_names_of_one_node_may_not_differ_only_in_case(routes: dict[str, str]) -> None:
    """Whichever of the two is declared first: the collision is between the names, not their order."""
    errors = _branch_graph([_branch_node(id="r", routes=routes)])
    assert len(errors) == 1 and "differ only in case" in errors[0]


def test_a_route_condition_cannot_be_blank() -> None:
    errors = _branch_graph([_branch_node(id="r", routes={"a": "  ", "b": "y"})])
    assert len(errors) == 1 and "condition" in errors[0]


def test_select_without_routes_is_refused() -> None:
    errors = _branch_graph([_branch_node(id="n", select="one")])
    assert any("select" in e and "routes" in e for e in errors)


def test_a_guarded_node_may_name_a_decision_it_depends_on() -> None:
    assert _branch_graph([_decision(), _branch_node(id="g", depends_on=["r"], when="r.a")]) == []


def test_a_node_may_be_both_a_decision_and_guarded() -> None:
    errors = _branch_graph(
        [
            _decision(),
            _decision("r2", depends_on=["r"], when="r.a"),
            _branch_node(id="g", depends_on=["r2"], when="r2.b"),
        ]
    )
    assert errors == []


def test_a_triage_graph_with_two_decisions_and_a_guarded_merge_is_accepted() -> None:
    errors = _branch_graph(
        [
            _branch_node(
                id="triage",
                routes={"bug": "a defect", "feature": "new behaviour", "question": "no change needed"},
            ),
            _branch_node(id="risk", routes={"high": "touches auth or billing", "low": "touches neither"}),
            _branch_node(id="fix", depends_on=["triage"], when="triage.bug"),
            _branch_node(id="design", depends_on=["triage"], when="triage.feature"),
            _branch_node(
                id="deep_review",
                depends_on=["triage", "risk", "fix", "design"],
                when="(triage.bug or triage.feature) and risk.high",
            ),
            _branch_node(
                id="reply",
                depends_on=["triage", "fix", "design"],
                when="triage.done",
                prompt_template="## Fix\n{{ fix.output }}\n\n## Design\n{{ design.output }}",
            ),
        ]
    )
    assert errors == []


def test_a_guard_naming_a_node_outside_depends_on_is_refused() -> None:
    errors = _branch_graph([_decision(), _branch_node(id="g", when="r.a")])
    assert len(errors) == 1 and "depends_on" in errors[0]


def test_a_guard_naming_no_node_of_this_graph_says_so_first() -> None:
    """Neither dependency nor graph holds the name, and the earlier-run cause is the
    one to report: an author sent to fix depends_on would be fixing the wrong half."""
    errors = _branch_graph([_branch_node(id="g", when="old.risky")])
    assert errors == [
        "node 'g' guard names 'old', which is not a node of this graph -- the routes of an earlier run are not "
        "recorded, so name a decision node of this graph"
    ]


def test_a_node_of_this_graph_missing_from_depends_on_is_reported_as_that() -> None:
    errors = _branch_graph([_decision(), _branch_node(id="g", when="r.a")])
    assert errors == ["node 'g' guard names 'r', which is not in its depends_on -- a guard may only read a dependency"]


def test_a_node_id_spelled_like_a_keyword_can_be_guarded_on() -> None:
    """`not` is a legal node id, so a graph may name it -- and guard a dependent on it."""
    errors = _branch_graph(
        [
            _decision("not"),
            _branch_node(id="g", depends_on=["not"], when="not.a"),
        ]
    )
    assert errors == []


def test_a_node_named_by_several_atoms_is_reported_once() -> None:
    errors = _branch_graph([_decision(), _branch_node(id="g", when="r.a or r.b or r.done")])
    assert len(errors) == 1 and "depends_on" in errors[0]


def test_an_undeclared_route_written_twice_is_reported_once() -> None:
    errors = _branch_graph([_decision(), _branch_node(id="g", depends_on=["r"], when="r.c or r.c")])
    assert len(errors) == 1 and "'c'" in errors[0]


def test_a_guard_naming_an_undeclared_route_is_refused() -> None:
    errors = _branch_graph([_decision(), _branch_node(id="g", depends_on=["r"], when="r.c")])
    assert len(errors) == 1 and "'c'" in errors[0]


def test_an_undeclared_route_refusal_lists_the_declared_routes_in_name_order() -> None:
    errors = _branch_graph(
        [
            _branch_node(id="r", routes={"zeta": "z holds", "alpha": "a holds"}),
            _branch_node(id="g", depends_on=["r"], when="r.c"),
        ]
    )
    assert errors == ["node 'g' guard names route 'c', which node 'r' does not declare (it declares: alpha, zeta)"]


def test_a_guard_naming_a_node_with_no_routes_is_refused() -> None:
    errors = _branch_graph([_branch_node(id="plain"), _branch_node(id="g", depends_on=["plain"], when="plain.a")])
    assert len(errors) == 1 and "declares no 'routes'" in errors[0]


def test_a_decision_node_in_depends_on_must_appear_in_when() -> None:
    """The refusal offers the route first. `.done` is only for a node that runs
    whichever route is selected: offered alone, it steers a node that belongs to one
    branch onto every route, where it reads the fixed sentence for that branch."""
    errors = _branch_graph([_decision(), _branch_node(id="g", depends_on=["r"])])
    assert errors == [
        "node 'g' depends on decision node 'r', which must appear in when -- "
        "write 'r.<route>' for the route(s) it runs on, or 'r.done' if it runs whichever route is selected"
    ]


def test_a_decision_node_that_depends_on_another_decision_must_name_it_in_when() -> None:
    """A nested decision is bound like any other node: it says which route of its parent it wants."""
    errors = _branch_graph([_decision(), _decision("r2", depends_on=["r"])])
    assert errors == [
        "node 'r2' depends on decision node 'r', which must appear in when -- "
        "write 'r.<route>' for the route(s) it runs on, or 'r.done' if it runs whichever route is selected"
    ]


def test_a_guard_naming_one_decision_does_not_excuse_another_the_node_depends_on() -> None:
    errors = _branch_graph(
        [
            _decision(),
            _decision("s"),
            _branch_node(id="g", depends_on=["r", "s"], when="r.a"),
        ]
    )
    assert errors == [
        "node 'g' depends on decision node 's', which must appear in when -- "
        "write 's.<route>' for the route(s) it runs on, or 's.done' if it runs whichever route is selected"
    ]


def test_a_guarded_decision_depending_on_another_must_still_name_it() -> None:
    """`r2` has a guard of its own, and that does not stand in for naming `r`: the
    guard on `r2` is read only once `r` settles, so it cannot say which route."""
    errors = _branch_graph(
        [
            _decision(),
            _decision("r2", depends_on=["r"], when="r.a"),
            _branch_node(id="g", depends_on=["r2"]),
        ]
    )
    assert errors == [
        "node 'g' depends on decision node 'r2', which must appear in when -- "
        "write 'r2.<route>' for the route(s) it runs on, or 'r2.done' if it runs whichever route is selected"
    ]


def test_a_decision_depending_on_a_plain_node_must_still_be_named() -> None:
    """`r2` needs no guard of its own -- `p` decides nothing -- but a node that
    depends on `r2` still has to say which of its routes it runs on."""
    errors = _branch_graph(
        [
            _branch_node(id="p"),
            _decision("r2", depends_on=["p"]),
            _branch_node(id="g", depends_on=["r2"]),
        ]
    )
    assert errors == [
        "node 'g' depends on decision node 'r2', which must appear in when -- "
        "write 'r2.<route>' for the route(s) it runs on, or 'r2.done' if it runs whichever route is selected"
    ]


def test_a_node_that_declines_the_route_and_names_the_decision_is_allowed() -> None:
    """Naming the decision is the whole obligation: `.done` is a node that runs on
    every route, and it is accepted however the decision itself is guarded."""
    assert (
        _branch_graph(
            [
                _decision(),
                _decision("r2", depends_on=["r"], when="r.a"),
                _branch_node(id="g", depends_on=["r2"], when="r2.done"),
            ]
        )
        == []
    )


def test_several_unnamed_decisions_are_reported_in_name_order() -> None:
    """`validate_and_order` raises the first error, so the order must not follow the hash seed."""
    decisions = [_decision(name) for name in ("d1", "d2", "d3", "d4", "d5")]
    errors = _branch_graph([*decisions, _branch_node(id="g", depends_on=["d5", "d3", "d1", "d4", "d2"])])
    assert errors == [
        f"node 'g' depends on decision node '{name}', which must appear in when -- "
        f"write '{name}.<route>' for the route(s) it runs on, or '{name}.done' if it runs whichever route is selected"
        for name in ("d1", "d2", "d3", "d4", "d5")
    ]


def test_a_done_atom_satisfies_the_must_appear_rule() -> None:
    assert _branch_graph([_decision(), _branch_node(id="g", depends_on=["r"], when="r.done")]) == []


def test_two_routes_of_one_single_select_node_in_one_and_chain_is_refused() -> None:
    errors = _branch_graph([_decision(), _branch_node(id="g", depends_on=["r"], when="r.a and r.b")])
    assert len(errors) == 1 and "can never be true" in errors[0]


def test_a_clash_among_several_routes_lists_them_in_name_order() -> None:
    routes = {name: f"{name} holds" for name in "abcde"}
    errors = _branch_graph(
        [
            _branch_node(id="r", routes=routes),
            _branch_node(id="g", depends_on=["r"], when="r.e and r.c and r.a and r.d and r.b"),
        ]
    )
    assert errors == [
        "node 'g' guard requires ['a', 'b', 'c', 'd', 'e'] of single-select node 'r' at once, which can never be true"
    ]


def test_the_same_two_routes_under_an_or_are_fine() -> None:
    assert _branch_graph([_decision(), _branch_node(id="g", depends_on=["r"], when="r.a or r.b")]) == []


def test_two_routes_of_a_multi_select_node_may_both_hold() -> None:
    errors = _branch_graph([_decision(select="many"), _branch_node(id="g", depends_on=["r"], when="r.a and r.b")])
    assert errors == []


def test_a_right_nested_and_chain_is_flattened_before_the_check() -> None:
    errors = _branch_graph(
        [
            _decision(),
            _branch_node(id="x", routes={"y": "y holds", "z": "z holds"}),
            _branch_node(id="g", depends_on=["r", "x"], when="r.a and (x.y and r.b)"),
        ]
    )
    assert len(errors) == 1 and "can never be true" in errors[0] and "'r'" in errors[0]


def test_a_negated_and_chain_cannot_make_the_guard_impossible() -> None:
    errors = _branch_graph([_decision(), _branch_node(id="g", depends_on=["r"], when="not (r.a and r.b)")])
    assert errors == []


def test_done_beside_a_route_of_the_same_node_is_satisfiable() -> None:
    """`done` is not a route, so `r.a and r.done` holds whenever `r` selects `a`."""
    errors = _branch_graph([_decision(), _branch_node(id="g", depends_on=["r"], when="r.a and r.done")])
    assert errors == []


def test_an_undeclared_route_is_not_also_reported_as_a_clash() -> None:
    errors = _branch_graph([_decision(), _branch_node(id="g", depends_on=["r"], when="r.a and r.c")])
    assert len(errors) == 1 and "'c'" in errors[0] and "never be true" not in errors[0]


def test_not_done_can_never_be_true() -> None:
    errors = _branch_graph([_decision(), _branch_node(id="g", depends_on=["r"], when="not r.done")])
    assert errors == ["node 'g' guard negates 'r.done', which is always true, so it can never be true"]


@pytest.mark.parametrize(
    "when",
    [
        "not not r.done",
        "not not not not r.done",
        "not (not r.done and r.a)",
        "not (r.a and not r.done)",
        "not r.done or r.b",
        "r.b or not r.done",
        "(r.a and r.b) or r.done",
        "r.done or (r.a and r.b)",
        "r.done and ((r.a and r.b) or r.b)",
        "not (not r.done or r.a)",
        "not not (r.a or r.b)",
        "not (r.a and r.b)",
    ],
)
def test_satisfiable_guards_survive_negation_and_disjunction(when: str) -> None:
    assert _branch_graph([_decision(), _branch_node(id="g", depends_on=["r"], when=when)]) == []


@pytest.mark.parametrize(
    "when",
    [
        "not not not r.done",
        "not r.done and r.a",
        "r.a and not r.done",
        "not r.done or (r.a and r.b)",
        "(r.a and r.b) or not r.done",
        "not not (r.a and r.b)",
        "r.a and not not r.b",
        "not (not r.a or not r.b)",
    ],
)
def test_impossible_guards_are_still_rejected_in_boolean_context(when: str) -> None:
    errors = _branch_graph([_decision(), _branch_node(id="g", depends_on=["r"], when=when)])
    assert errors and all("can never be true" in error for error in errors)


@pytest.mark.parametrize("when", ["not r.a", "r.a and not r.b"])
def test_a_negated_route_is_an_ordinary_guard(when: str) -> None:
    """Only `not <n>.done` is dead. `not r.a` holds when `r` completed and selected something else."""
    assert _branch_graph([_decision(), _branch_node(id="g", depends_on=["r"], when=when)]) == []


def test_a_done_atom_inside_a_negated_group_is_not_refused() -> None:
    """Only `not <n>.done` itself is dead: `not (r.done and x.y)` is a live guard."""
    errors = _branch_graph(
        [
            _decision(),
            _branch_node(id="x", routes={"y": "y holds", "z": "z holds"}),
            _branch_node(id="g", depends_on=["r", "x"], when="not (r.done and x.y)"),
        ]
    )
    assert errors == []


def test_a_guard_syntax_error_is_reported() -> None:
    errors = _branch_graph([_branch_node(id="g", when="r.a and")])
    assert len(errors) == 1 and errors[0].startswith("node 'g': guard:") and "position" in errors[0]


def test_a_guard_error_position_indexes_the_text_as_written() -> None:
    errors = _branch_graph([_branch_node(id="g", when="  r.a and")])
    assert len(errors) == 1 and "position 9" in errors[0]


def test_every_guard_syntax_error_is_reported_not_just_the_first() -> None:
    errors = _branch_graph([_branch_node(id="g1", when="r.a and"), _branch_node(id="g2", when="(r.a")])
    assert [e.split(":")[0] for e in errors] == ["node 'g1'", "node 'g2'"]


def test_a_guard_that_does_not_parse_is_the_only_error_of_its_node() -> None:
    """Its atoms are unknown, so the must-appear rule has nothing sound to say."""
    errors = _branch_graph([_decision(), _branch_node(id="g", depends_on=["r"], when="r.a and")])
    assert len(errors) == 1 and "position" in errors[0]


def test_a_guard_may_not_name_a_decision_of_an_earlier_run() -> None:
    errors = _branch_graph(
        [_branch_node(id="g", depends_on=["old"], when="old.a")],
        session_nodes=_EARLIER_RUN_NODES,
    )
    assert len(errors) == 1 and "earlier run" in errors[0]


def test_guards_of_parses_every_non_blank_when_by_node_id() -> None:
    from raven.agent.subagent.dag_graph import guards_of

    nodes = _branch_nodes(
        [
            _decision(),
            _branch_node(id="g", depends_on=["r"], when="r.a or r.b"),
            _branch_node(id="blank", depends_on=["r"], when="   "),
            _branch_node(id="plain"),
        ]
    )
    guards = guards_of(nodes)
    assert set(guards) == {"g"}
    assert guards["g"].source == "r.a or r.b"
    assert [(atom.node, atom.name) for atom in guards["g"].atoms] == [("r", "a"), ("r", "b")]


def test_guards_of_lets_a_syntax_error_propagate() -> None:
    from raven.agent.subagent.dag_graph import guards_of
    from raven.agent.subagent.dag_guard import GuardError

    with pytest.raises(GuardError, match="position"):
        guards_of(_branch_nodes([_branch_node(id="g", when="r.a and")]))


def test_may_not_execute_follows_guards_down_the_graph() -> None:
    from raven.agent.subagent.dag_graph import may_not_execute

    nodes = _branch_nodes(
        [
            _decision(),
            _branch_node(id="g", depends_on=["r"], when="r.a"),
            _branch_node(id="inside", depends_on=["g"]),
            _branch_node(id="common", depends_on=["r"]),
        ]
    )
    skippable = may_not_execute(nodes, {n.id: n for n in nodes})
    assert "g" in skippable and "inside" in skippable
    assert "r" not in skippable and "common" not in skippable


def test_may_not_execute_needs_every_dependency_to_be_skippable() -> None:
    from raven.agent.subagent.dag_graph import may_not_execute

    nodes = _branch_nodes(
        [
            _decision(),
            _branch_node(id="g", depends_on=["r"], when="r.a"),
            _branch_node(id="done_guard", depends_on=["r"], when="r.done"),
            _branch_node(id="plain", depends_on=["r"]),
            _branch_node(id="mixed", depends_on=["g", "plain"]),
            _branch_node(id="blank_when", depends_on=["r"], when="   "),
            _branch_node(id="after_old_run", depends_on=["old"]),
            _branch_node(id="g_and_old", depends_on=["g", "old"]),
        ]
    )
    # `done_guard` is counted although `r.done` always holds: the set errs towards "may not run".
    # `g_and_old` is too: `old` belongs to an earlier run, so it is no dependency in this graph and
    # the only one that counts, `g`, may not execute. `after_old_run` has no dependency here at all.
    assert may_not_execute(nodes, {n.id: n for n in nodes}) == frozenset({"g", "done_guard", "g_and_old"})


def test_may_not_execute_raises_on_a_cycle() -> None:
    from raven.agent.subagent.dag_graph import may_not_execute

    nodes = _branch_nodes([_branch_node(id="p", depends_on=["q"]), _branch_node(id="q", depends_on=["p"])])
    with pytest.raises(DagValidationError, match="cycle"):
        may_not_execute(nodes, {n.id: n for n in nodes})


def _reading_graph(template: str, **over: object) -> list[str]:
    """Errors of a branching graph whose `reader` carries `template`.

    `g`, `inside` and `g_and_old` may not execute. The last also depends on `old`, a node an earlier
    run completed.
    """
    return _branch_graph(
        [
            _decision(),
            _branch_node(id="g", depends_on=["r"], when="r.a"),
            _branch_node(id="inside", depends_on=["g"]),
            _branch_node(id="g_and_old", depends_on=["g", "old"]),
            _branch_node(id="reader", depends_on=["r", "g"], when="r.b", prompt_template=template, **over),
        ],
        session_nodes=_EARLIER_RUN_NODES,
    )


@pytest.mark.parametrize(
    ("template", "target"),
    [
        ("{{ ref:@nodes/g.out.md }}", "g"),
        ("{{ ref_path:@nodes/g.out.md }}", "g"),
        ("{{ ref:@nodes/g.prompt.md }}", "g"),
        ("{{ ref:@nodes/./g.out.md }}", "g"),
        ("{{ ref:@nodes/inside.out.md }}", "inside"),
        ("{{ ref:@nodes/g_and_old.out.md }}", "g_and_old"),
    ],
)
def test_an_at_nodes_reference_to_a_node_that_may_not_execute_is_refused(template: str, target: str) -> None:
    errors = _reading_graph(template)
    assert len(errors) == 1
    assert f"of node '{target}', which may not execute in this graph" in errors[0]
    assert f"use {{{{ {target}.output }}}} instead" in errors[0]


def test_a_file_input_aimed_at_a_node_that_may_not_execute_is_refused() -> None:
    errors = _reading_graph("{{ inputs.k }}", inputs={"k": {"file": "@nodes/g.out.md"}})
    assert len(errors) == 1
    assert "node 'reader' reads input 'k' of node 'g', which may not execute" in errors[0]


@pytest.mark.parametrize("kind", ["ref", "ref_path", "input"])
@pytest.mark.parametrize("spelling", ["GATE", "gate", "gAtE"])
def test_at_nodes_case_aliases_use_the_reserved_node_identity(kind: str, spelling: str) -> None:
    path = f"@nodes/{spelling}.out.md"
    template = "{{ inputs.k }}" if kind == "input" else "{{ " + kind + ":" + path + " }}"
    inputs = {"k": {"file": path}} if kind == "input" else {}
    errors = _branch_graph(
        [
            _decision(),
            _branch_node(id="Gate", depends_on=["r"], when="r.a"),
            _branch_node(id="reader", depends_on=["r", "Gate"], when="r.done", prompt_template=template, inputs=inputs),
        ]
    )
    assert len(errors) == 1
    assert "of node 'Gate', which may not execute" in errors[0]
    assert "use {{ Gate.output }} instead" in errors[0]
    assert (
        _branch_graph(
            [
                _branch_node(id="Gate"),
                _branch_node(id="reader", depends_on=["Gate"], prompt_template=template, inputs=inputs),
            ]
        )
        == []
    )


@pytest.mark.parametrize("kind", ["ref", "ref_path", "input"])
@pytest.mark.parametrize(
    "relative",
    [
        "Gate.out.md",
        "./Gate.out.md",
        "GATE.out.md",
        "gate.out.md",
        r".\Gate.out.md",
        r"nested\..\Gate.out.md",
        "nested/Gate.out.md",
        "{drive}Gate.out.md",
        r"{drive}.\gate.out.md",
        r"{drive}nested\..\GATE.out.md",
    ],
)
async def test_at_nodes_aliases_respect_the_filesystem_and_reserved_ids(
    tmp_path: Path, kind: str, relative: str
) -> None:
    relative = relative.format(drive=tmp_path.drive or "C:")
    artifacts = tmp_path / "nodes"
    artifacts.mkdir()
    (artifacts / "nested").mkdir()
    (artifacts / "Gate.out.md").write_text("the guarded node's output", encoding="utf-8")
    backend = LocalFileBackend()
    resolved = backend.abspath(relative, cwd=str(artifacts))
    aliases_output = await backend.file_exists(resolved)
    if aliases_output:
        assert await backend.read_file(resolved) == b"the guarded node's output"
    path = f"@nodes/{relative}"
    template = "{{ inputs.k }}" if kind == "input" else "{{ " + kind + ":" + path + " }}"
    inputs = {"k": {"file": path}} if kind == "input" else {}
    errors = _branch_graph(
        [
            _decision(),
            _branch_node(id="Gate", depends_on=["r"], when="r.a"),
            _branch_node(id="reader", depends_on=["r", "Gate"], when="r.done", prompt_template=template, inputs=inputs),
        ]
    )
    if aliases_output or relative in ("GATE.out.md", "gate.out.md"):
        assert len(errors) == 1
        assert "of node 'Gate', which may not execute" in errors[0]
        assert "use {{ Gate.output }} instead" in errors[0]
    else:
        assert errors == []


@_POSIX_REFERENCE_PATHS
@pytest.mark.parametrize("kind", ["ref", "ref_path", "input"])
@pytest.mark.parametrize(
    "spelling",
    [
        "{artifacts}/Gate.out.md",
        "{artifacts}/sub/../Gate.out.md",
        "{artifacts}/GATE.prompt.md",
        "../session/subagents/nodes/Gate.out.md",
    ],
    ids=["absolute", "absolute-through-a-parent", "absolute-case-alias", "relative-out-of-the-workdir"],
)
def test_a_path_into_the_node_root_without_at_nodes_is_refused_too(tmp_path: Path, kind: str, spelling: str) -> None:
    """`@nodes/` is one spelling of the node root. The history root around it is a reference
    root of its own, so the same files can be named by absolute path, as every finish text
    prints them, or from the workdir; a reference is judged by where it lands."""
    workdir, history = tmp_path / "ws", tmp_path / "session" / "subagents"
    artifacts = history / "nodes"
    workdir.mkdir()
    artifacts.mkdir(parents=True)
    path = spelling.format(artifacts=artifacts)
    template = "{{ inputs.k }}" if kind == "input" else "{{ " + kind + ":" + path + " }}"
    inputs = {"k": {"file": path}} if kind == "input" else {}

    def errors(nodes: list[dict]) -> list[str]:
        roots = (str(workdir), str(history))
        return collect_static_graph_errors(_branch_nodes(nodes), roots=roots, nodes_root=str(artifacts))

    reader = {"id": "reader", "prompt_template": template, "inputs": inputs}
    refused = errors(
        [
            _decision(),
            _branch_node(id="Gate", depends_on=["r"], when="r.a"),
            _branch_node(**reader, depends_on=["r", "Gate"], when="r.done"),
        ]
    )
    assert len(refused) == 1
    assert "of node 'Gate', which may not execute" in refused[0]
    assert errors([_branch_node(id="Gate"), _branch_node(**reader, depends_on=["Gate"])]) == []


@_POSIX_REFERENCE_PATHS
@pytest.mark.parametrize(("link", "refused"), [(True, True), (False, False)], ids=["through-a-link", "same-name"])
def test_a_workdir_reference_is_judged_by_the_file_its_read_would_open(
    tmp_path: Path, link: bool, refused: bool
) -> None:
    """The read follows a symlink, so a link in the workdir pointing into the node root names
    that node's artifact; a file of the same name that is not one names nothing."""
    workdir, history = tmp_path / "ws", tmp_path / "session" / "subagents"
    artifacts = history / "nodes"
    workdir.mkdir()
    artifacts.mkdir(parents=True)
    if link:
        try:
            (workdir / "Gate.out.md").symlink_to(artifacts / "Gate.out.md")
        except OSError:
            pytest.skip("this host cannot create a symlink")
    nodes = [
        _decision(),
        _branch_node(id="Gate", depends_on=["r"], when="r.a"),
        _branch_node(id="reader", depends_on=["r", "Gate"], when="r.done", prompt_template="{{ ref:Gate.out.md }}"),
    ]
    errors = collect_static_graph_errors(
        _branch_nodes(nodes), roots=(str(workdir), str(history)), nodes_root=str(artifacts)
    )
    assert bool(errors) is refused, errors


@pytest.mark.parametrize(
    "template",
    [
        "{{ ref:@nodes/r.out.md }}",
        "{{ g.output }}",
        "{{ ref:g.out.md }}",
        "{{ ref:@nodes/old.out.md }}",
        "{{ ref:@nodes/sub/g.out.md }}",
    ],
)
def test_a_reference_that_cannot_land_on_a_missing_node_file_is_fine(template: str) -> None:
    assert _reading_graph(template) == []


def test_a_graph_with_duplicate_ids_gets_no_at_nodes_verdict() -> None:
    """With two nodes called `g`, no one can say which a path names: only the duplicate is reported."""
    errors = _branch_graph(
        [
            _decision(),
            _branch_node(id="g", depends_on=["r"], when="r.a"),
            _branch_node(id="g", depends_on=["r"], when="r.b"),
            _branch_node(id="reader", depends_on=["r"], when="r.a", prompt_template="{{ ref:@nodes/g.out.md }}"),
        ]
    )
    assert errors == ["duplicate node ids: ['g']"]


def test_an_at_nodes_reference_in_a_graph_with_no_guard_is_left_alone() -> None:
    reader = _branch_node(id="b", depends_on=["a"], prompt_template="{{ ref:@nodes/a.out.md }}")
    assert _branch_graph([_branch_node(id="a"), reader]) == []


def test_a_cycle_in_a_guarded_graph_is_reported_not_raised() -> None:
    errors = _branch_graph(
        [
            _decision(),
            _branch_node(id="p", depends_on=["q"]),
            _branch_node(id="q", depends_on=["p"]),
            _branch_node(id="g", depends_on=["r"], when="r.a", prompt_template="{{ ref:@nodes/p.out.md }}"),
        ]
    )
    assert errors == ["graph contains a cycle"]


def test_a_malformed_placeholder_beside_a_guard_is_reported_not_raised() -> None:
    errors = _branch_graph(
        [
            _decision(),
            _branch_node(id="g", depends_on=["r"], when="r.a"),
            _branch_node(id="bad", prompt_template="{{ ref: }}"),
        ]
    )
    assert errors == ["empty path in '{{ ref: }}'"]


# --- branches: judging a decision node ---------------------------------------

_TWO_ROUTES = {"a": "the first holds", "b": "the second holds"}


class _NoFiles:
    """The slice of a run store the judge closure reads: no prompt and no transcript."""

    async def read_text(self, _path):
        return ""

    def transcript_path(self, _node_id):
        return "t"

    def prompt_path(self, _node_id):
        return "p"


class _Canned:
    """A provider that answers every call with one report_verdict call, and records the calls."""

    def __init__(self, **report: object) -> None:
        self.calls: list[dict] = []
        self._report = report

    async def chat_with_retry(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        return SimpleNamespace(tool_calls=[SimpleNamespace(arguments=json.dumps(self._report))])


async def _run_judge(judge_node: Any, node: Any, *, crashed: bool = False) -> Any:
    return await judge_node(node=node, store=_NoFiles(), output="o", error="boom" if crashed else "", crashed=crashed)


async def test_a_graph_with_routes_is_refused_on_a_host_that_cannot_judge(tmp_path) -> None:
    from raven.agent.subagent.dag_tool import SubAgentDagTool

    tool = SubAgentDagTool(workspace=tmp_path)
    assert tool._judge_node() is None

    out = await tool.execute(task_summary="g", nodes=[_decision()], background=True)

    text = out if isinstance(out, str) else out.model_text
    assert "no provider to judge" in text
    assert "node 'r' declares routes" in text, "the refusal names the decision node"
    assert "No sub-agent was run" in text


async def test_a_refusal_names_every_decision_node_in_graph_order(tmp_path) -> None:
    from raven.agent.subagent.dag_tool import SubAgentDagTool

    tool = SubAgentDagTool(workspace=tmp_path)
    nodes = [_decision("s"), _branch_node(id="plain"), _decision("r")]

    out = await tool.execute(task_summary="g", nodes=nodes, background=True)

    text = out if isinstance(out, str) else out.model_text
    assert "nodes 's', 'r' declare routes" in text
    assert "plain" not in text, "a node that declares no routes is not named"


async def test_a_graph_with_routes_is_accepted_where_a_provider_is_bound(tmp_path) -> None:
    from raven.agent.subagent.dag_tool import SubAgentDagTool

    tool = SubAgentDagTool(
        workspace=tmp_path,
        provider_for=lambda: object(),
        agents=[ThirdPartyCliSubagentConfig(name="x", command="true")],
    )

    pre = await tool._preflight(parse_dag_spec({"task_summary": "g", "nodes": [_decision()]}))

    assert set(pre.backends) == {"r"}


async def test_a_graph_without_routes_is_not_refused_on_a_host_that_cannot_judge(tmp_path) -> None:
    from raven.agent.subagent.dag_tool import SubAgentDagTool

    tool = SubAgentDagTool(workspace=tmp_path, agents=[ThirdPartyCliSubagentConfig(name="x", command="true")])
    assert tool._judge_node() is None

    pre = await tool._preflight(parse_dag_spec({"task_summary": "g", "nodes": [_branch_node(id="a")]}))

    assert set(pre.backends) == {"a"}


async def test_a_replacement_graph_with_routes_is_refused_on_a_host_that_cannot_judge(tmp_path) -> None:
    tool, live = await _replannable_tool(tmp_path)
    fresh = {**_node("fresh"), "routes": _TWO_ROUTES}

    out = await tool.prepare_replan(_OLD_RUN_ID, "a", [fresh], "wrong", None, live)

    assert isinstance(out, str)
    assert "no provider to judge" in out


async def test_a_replacement_graph_with_routes_is_accepted_where_a_provider_is_bound(tmp_path) -> None:
    tool, live = await _replannable_tool(tmp_path, provider_for=lambda: object())
    fresh = {**_node("fresh"), "routes": _TWO_ROUTES}

    plan = await tool.prepare_replan(_OLD_RUN_ID, "a", [fresh], "wrong", None, live)

    assert not isinstance(plan, str), plan
    assert [n.id for n in plan.nodes] == ["fresh"]


@pytest.mark.parametrize(
    ("declared", "routes", "select"),
    [
        pytest.param({"routes": _TWO_ROUTES, "select": None}, _TWO_ROUTES, "one", id="decision-default-select"),
        pytest.param({"routes": _TWO_ROUTES, "select": "many"}, _TWO_ROUTES, "many", id="decision-select-many"),
        pytest.param({}, None, "one", id="bare-node-with-only-an-id"),
        pytest.param({"routes": None, "select": None}, None, "one", id="ordinary-node"),
        pytest.param({"routes": {}, "select": None}, None, "one", id="empty-routes"),
    ],
)
async def test_the_judge_hands_the_nodes_declaration_to_the_verdict_call(
    tmp_path, monkeypatch, declared, routes, select
) -> None:
    import raven.agent.subagent.dag_tool as tool_mod
    from raven.agent.subagent.dag_tool import SubAgentDagTool
    from raven.agent.subagent.dag_verdict import Verdict

    seen: list[dict] = []

    async def _fake_judge(provider, **kwargs):
        seen.append(kwargs)
        return Verdict(accomplished=True)

    monkeypatch.setattr(tool_mod, "judge", _fake_judge)
    tool = SubAgentDagTool(workspace=tmp_path, provider_for=lambda: object())

    await _run_judge(tool._judge_node(), SimpleNamespace(id="n", **declared))

    assert seen[0]["routes"] == routes
    assert seen[0]["select"] == select


async def test_a_decision_nodes_declaration_reaches_the_request_the_provider_sees(tmp_path) -> None:
    from raven.agent.subagent.dag_tool import SubAgentDagTool

    provider = _Canned(outcome="accomplished", route=["a"], route_evidence="the first holds")
    tool = SubAgentDagTool(workspace=tmp_path, provider_for=lambda: provider)
    node = SimpleNamespace(id="r", routes=_TWO_ROUTES, select="many")

    verdict = await _run_judge(tool._judge_node(), node)

    assert verdict.routes == ("a",)
    assert verdict.route_evidence == "the first holds"
    (call,) = provider.calls
    route = call["tools"][0]["function"]["parameters"]["properties"]["route"]
    assert route["type"] == "array"
    assert route["items"]["enum"] == ["a", "b"]
    assert "- a: the first holds" in call["messages"][-1]["content"]


async def test_a_crashed_decision_node_is_described_without_asking_for_a_route(tmp_path) -> None:
    from raven.agent.subagent.dag_tool import SubAgentDagTool

    provider = _Canned(outcome="not_accomplished", category="tool_failure", what_is_missing="the tool died")
    tool = SubAgentDagTool(workspace=tmp_path, provider_for=lambda: provider)
    node = SimpleNamespace(id="r", routes=_TWO_ROUTES, select=None)

    verdict = await _run_judge(tool._judge_node(), node, crashed=True)

    assert verdict.accomplished is False
    assert verdict.routes is None
    assert "route" not in json.dumps(provider.calls[0]["tools"])


async def test_a_decision_node_fails_when_no_provider_is_bound_at_call_time(tmp_path) -> None:
    from raven.agent.subagent.dag_tool import SubAgentDagTool
    from raven.agent.subagent.dag_verdict import RouteUndeterminedError

    bound: list[object | None] = [object()]
    tool = SubAgentDagTool(workspace=tmp_path, provider_for=lambda: bound[0])
    judge_node = tool._judge_node()
    assert judge_node is not None
    bound[0] = None

    with pytest.raises(RouteUndeterminedError, match="no provider is bound"):
        await _run_judge(judge_node, SimpleNamespace(id="r", routes=_TWO_ROUTES, select=None))


@pytest.mark.parametrize(
    "declared",
    [
        pytest.param({}, id="bare-node-with-only-an-id"),
        pytest.param({"routes": None, "select": None}, id="ordinary-node"),
    ],
)
async def test_an_ordinary_node_is_judged_accomplished_when_no_provider_is_bound_at_call_time(
    tmp_path, declared
) -> None:
    from raven.agent.subagent.dag_tool import SubAgentDagTool
    from raven.agent.subagent.dag_verdict import Verdict

    bound: list[object | None] = [object()]
    tool = SubAgentDagTool(workspace=tmp_path, provider_for=lambda: bound[0])
    judge_node = tool._judge_node()
    assert judge_node is not None
    bound[0] = None

    verdict = await _run_judge(judge_node, SimpleNamespace(id="n", **declared))

    assert verdict == Verdict(accomplished=True)


async def test_a_crashed_decision_node_keeps_its_own_failure_when_no_provider_is_bound(tmp_path) -> None:
    from raven.agent.subagent.dag_tool import SubAgentDagTool
    from raven.agent.subagent.dag_verdict import Verdict

    bound: list[object | None] = [object()]
    tool = SubAgentDagTool(workspace=tmp_path, provider_for=lambda: bound[0])
    judge_node = tool._judge_node()
    assert judge_node is not None
    bound[0] = None

    verdict = await _run_judge(judge_node, SimpleNamespace(id="r", routes=_TWO_ROUTES, select=None), crashed=True)

    assert verdict == Verdict(accomplished=True), "no route is read from a node that crashed, so nothing is raised"


@pytest.mark.parametrize("crashed", [False, True], ids=["returned", "crashed"])
async def test_the_judge_reads_the_follow_ups_a_continued_node_was_sent(tmp_path, crashed) -> None:
    from raven.agent.subagent.dag_tool import SubAgentDagTool

    nodes = tmp_path / "nodes"
    nodes.mkdir()
    (nodes / "n.prompt.md").write_text("push the fix to PR 918", encoding="utf-8")
    (nodes / "n.follow_ups.json").write_text(json.dumps(["open a replacement PR instead"]), encoding="utf-8")
    store = DagRunStore(
        backend=LocalFileBackend(),
        root=str(tmp_path / "runs"),
        run_id="r1",
        nodes_root=str(nodes),
        registry_root=str(tmp_path),
    )
    provider = _Canned(outcome="not_accomplished" if crashed else "accomplished")
    tool = SubAgentDagTool(workspace=tmp_path, provider_for=lambda: provider)

    await tool._judge_node()(
        node=SimpleNamespace(id="n"),
        store=store,
        output="opened PR 1022",
        error="boom" if crashed else "",
        crashed=crashed,
    )

    user = provider.calls[0]["messages"][-1]["content"]
    fenced = re.findall(r"\[BEGIN UNTRUSTED \S+ #(\w+) [^\n]*\n(.*?)\n\[END UNTRUSTED \S+ #\1\]", user, re.DOTALL)
    assert [body for _, body in fenced][:2] == ["push the fix to PR 918", "open a replacement PR instead"]


# --- branches: a missing upstream reads as a fixed sentence ------------------------

_FENCE = re.compile(r"\[BEGIN UNTRUSTED \S+ #(\w+) [^\n]*\n.*?\n\[END UNTRUSTED \S+ #\1\]", re.DOTALL)

_REFERENCES_TO_R = {
    "output": "{{ r.output }}",
    "output_path": "{{ r.output_path }}",
    "input": "{{ inputs.prev }}",
    "input_path": "{{ inputs.prev.path }}",
}


def _merge_node(template: str, *, depends_on: list[str], inputs: dict[str, object] | None = None) -> Any:
    spec = parse_dag_spec(
        {
            "task_summary": "merge what the branches left",
            "nodes": [
                {
                    "id": "merge",
                    "subagent": "x",
                    "node_summary": "merge",
                    "prompt_template": template,
                    "depends_on": depends_on,
                    "inputs": inputs or {},
                }
            ],
        }
    )
    return spec.nodes[0]


def _without_nonces(text: str) -> str:
    return re.sub(r"#[0-9a-f]{8}", "#NONCE", text)


@pytest.mark.parametrize("stale", [False, True], ids=["no-file", "stale-file"])
@pytest.mark.parametrize("form", list(_REFERENCES_TO_R))
async def test_a_not_executed_node_reads_as_the_fixed_sentence(form: str, stale: bool) -> None:
    """The status decides, not the file: a node that was not executed wrote none, and
    one that left a file under its id in an earlier run is still not this run's output."""
    be = _FakeBackend()
    if stale:
        be.files["/nodes/r.out.md"] = b"STALE_R"
    inputs = {"prev": {"node": "r"}} if form.startswith("input") else None
    node = _merge_node(f"before {_REFERENCES_TO_R[form]} after", depends_on=["r"], inputs=inputs)

    rendered = await render_prompt(node, backend=be, cwd="/w", nodes_root="/nodes", not_executed={"r"})

    assert rendered == f"before {MISSING_OUTPUT_TEXT} after"


async def test_only_a_reference_to_the_node_reads_as_the_sentence() -> None:
    """An input key, a file path and a ref can all be spelled ``r`` without naming node ``r``."""
    be = _FakeBackend()
    be.files["/nodes/a.out.md"] = b"RESULT_A"
    be.files["/w/notes.md"] = b"FILE_BODY"
    be.files["/w/r"] = b"REF_R"
    node = _merge_node(
        "{{ inputs.r }} {{ ref:r }} {{ inputs.live }} {{ inputs.f }} {{ inputs.dead }}",
        depends_on=["a", "r"],
        inputs={"r": "LITERAL", "live": {"node": "a"}, "f": {"file": "notes.md"}, "dead": {"node": "r"}},
    )

    rendered = await render_prompt(node, backend=be, cwd="/w", nodes_root="/nodes", not_executed={"r"})

    assert rendered.startswith("LITERAL ")
    assert "REF_R" in rendered and "RESULT_A" in rendered and "FILE_BODY" in rendered
    assert rendered.count(MISSING_OUTPUT_TEXT) == 1 and rendered.endswith(MISSING_OUTPUT_TEXT)


async def test_the_fixed_sentence_is_written_outside_every_untrusted_fence() -> None:
    be = _FakeBackend()
    be.files["/nodes/write.out.md"] = b"WRITTEN_DOCS"
    node = _merge_node("## Fix\n{{ fix.output }}\n\n## Docs\n{{ write.output }}", depends_on=["fix", "write"])

    rendered = await render_prompt(node, backend=be, cwd="/w", nodes_root="/nodes", not_executed={"fix"})

    fences = [m.group(0) for m in _FENCE.finditer(rendered)]
    assert len(fences) == 1 and "WRITTEN_DOCS" in fences[0], "the executed upstream is still fenced"
    assert MISSING_OUTPUT_TEXT in rendered
    assert all(MISSING_OUTPUT_TEXT not in fence for fence in fences)


async def test_an_upstream_that_writes_the_sentence_into_its_own_output_is_fenced_as_data() -> None:
    be = _FakeBackend()
    be.files["/nodes/write.out.md"] = MISSING_OUTPUT_TEXT.encode()
    node = _merge_node("## Fix\n{{ fix.output }}\n\n## Docs\n{{ write.output }}", depends_on=["fix", "write"])

    rendered = await render_prompt(node, backend=be, cwd="/w", nodes_root="/nodes", not_executed={"fix"})

    fences = [m.group(0) for m in _FENCE.finditer(rendered)]
    assert len(fences) == 1 and MISSING_OUTPUT_TEXT in fences[0], "the forged copy is inside a fence"
    assert _FENCE.sub("", rendered).count(MISSING_OUTPUT_TEXT) == 1, "the runner's own copy is not"


async def test_the_note_lists_only_the_nodes_the_template_actually_references() -> None:
    be = _FakeBackend()
    be.files["/nodes/fix.out.md"] = b"FIXED"
    node = _merge_node("## Bug fix result\n{{ fix.output }}", depends_on=["fix", "write"])

    rendered = await render_prompt(node, backend=be, cwd="/w", nodes_root="/nodes", may_not_execute={"fix", "write"})

    assert rendered.count(MAYBE_MISSING_HEADING) == 1
    body, _, note = rendered.partition(MAYBE_MISSING_HEADING)
    assert "FIXED" in body, "nothing is missing in this run and the note is appended all the same"
    assert "selects them: fix." in note
    assert "write" not in note
    assert note == (
        "\n\nSome inputs above come from steps that run only when an earlier decision selects them: fix. "
        f"A missing one reads exactly:\n{MISSING_OUTPUT_TEXT}\n"
        "Continue with the inputs you have, and do not guess what a missing one would have said."
    )


async def test_the_note_names_each_referenced_node_once_in_template_order() -> None:
    be = _FakeBackend()
    be.files["/nodes/triage.out.md"] = b"TRIAGED"
    node = _merge_node(
        "{{ write.output }}\n{{ inputs.design }}\n{{ triage.output }}\n"
        "{{ fix.output_path }}\n{{ inputs.audit_in.path }}\n{{ write.output_path }}",
        depends_on=["triage", "write", "design_doc", "fix", "audit"],
        inputs={"design": {"node": "design_doc"}, "audit_in": {"node": "audit"}},
    )

    rendered = await render_prompt(
        node,
        backend=be,
        cwd="/w",
        nodes_root="/nodes",
        not_executed={"write", "design_doc", "fix", "audit"},
        may_not_execute={"write", "design_doc", "fix", "audit", "unreferenced"},
    )

    assert "selects them: write, design_doc, fix, audit." in rendered


async def test_a_file_input_keyed_like_a_not_executed_node_still_renders_the_file() -> None:
    """The not-executed rule is about nodes: an `inputs` key that happens to be
    spelled like one is a file, and its path form still names that file."""
    be = _FakeBackend()
    be.files["/w/notes.md"] = b"THE NOTES"
    node = _merge_node(
        "{{ inputs.r }} and {{ inputs.r.path }}",
        depends_on=[],
        inputs={"r": {"file": "notes.md"}},
    )

    rendered = await render_prompt(node, backend=be, cwd="/w", nodes_root="/nodes", not_executed={"r"})

    assert MISSING_OUTPUT_TEXT not in rendered
    assert "THE NOTES" in _without_nonces(rendered)
    assert rendered.endswith("and /w/notes.md")


async def test_a_node_read_through_both_forms_is_listed_once_in_the_note() -> None:
    """`{{ x.output }}` and `{{ inputs.k }}` with `k` -> x name the same step."""
    be = _FakeBackend()
    be.files["/nodes/fix.out.md"] = b"FIXED"
    node = _merge_node(
        "{{ fix.output }} {{ inputs.fix_in }}",
        depends_on=["fix"],
        inputs={"fix_in": {"node": "fix"}},
    )

    rendered = await render_prompt(
        node,
        backend=be,
        cwd="/w",
        nodes_root="/nodes",
        may_not_execute={"fix"},
    )

    assert "selects them: fix." in rendered
    assert "selects them: fix, fix." not in rendered


async def test_a_blank_line_before_the_note_heading_leaves_one_heading() -> None:
    """A template that already ends in a blank line does not get a second one."""
    be = _FakeBackend()
    be.files["/nodes/fix.out.md"] = b"FIXED"
    node = _merge_node("{{ fix.output }}\n\n", depends_on=["fix"])

    rendered = await render_prompt(node, backend=be, cwd="/w", nodes_root="/nodes", may_not_execute={"fix"})

    assert rendered.count(MAYBE_MISSING_HEADING) == 1
    assert rendered.count(f"{MAYBE_MISSING_HEADING}\n\n") == 1


async def test_a_node_with_no_branch_note_is_unchanged() -> None:
    be = _FakeBackend()
    be.files["/nodes/a.out.md"] = b"RESULT_A"
    node = _merge_node("up: {{ a.output }}", depends_on=["a"])
    expected = _without_nonces("up: " + wrap_untrusted("RESULT_A", source="subagent"))

    plain = await render_prompt(node, backend=be, cwd="/w", nodes_root="/nodes")
    unrelated = await render_prompt(node, backend=be, cwd="/w", nodes_root="/nodes", may_not_execute={"b"})

    for rendered in (plain, unrelated):
        assert "RESULT_A" in rendered
        assert MAYBE_MISSING_HEADING not in rendered
        assert MISSING_OUTPUT_TEXT not in rendered
        assert _without_nonces(rendered) == expected


async def test_the_note_comes_after_the_template_and_before_the_memory_block() -> None:
    be = _FakeBackend()
    be.files["/nodes/fix.out.md"] = b"FIXED"
    spec = _memory_spec(
        [
            {"id": "fix", "subagent": "x", "prompt_template": "fix"},
            {"id": "merge", "subagent": "x", "prompt_template": "{{ fix.output }}", "depends_on": ["fix"]},
        ]
    )
    by_id = {n.id: n for n in spec.nodes}

    rendered = await render_prompt(
        by_id["merge"],
        backend=be,
        cwd="/w",
        nodes_root="/nodes",
        run_id="run1",
        by_id=by_id,
        may_not_execute={"fix"},
    )

    note_at = rendered.index(MAYBE_MISSING_HEADING)
    assert rendered.index("FIXED") < note_at < rendered.index("## Upstream memory records")


def test_an_unexecuted_upstream_gets_no_memory_line() -> None:
    spec = _memory_spec(
        [
            {"id": "a", "subagent": "x", "prompt_template": "a"},
            {"id": "b", "subagent": "x", "prompt_template": "b", "depends_on": ["a"]},
            {"id": "c", "subagent": "x", "prompt_template": "c", "depends_on": ["a", "b"]},
            {"id": "d", "subagent": "x", "prompt_template": "d", "depends_on": ["b"]},
        ]
    )
    by_id = {n.id: n for n in spec.nodes}

    def lines(node_id: str, not_executed: set[str]) -> list[str]:
        return _upstream_memory_lines(
            by_id[node_id],
            backend=_FakeBackend(),
            nodes_root="/nodes",
            run_id="r1",
            by_id=by_id,
            capabilities=None,
            not_executed=not_executed,
        )

    assert lines("b", {"a"}) == []
    assert lines("c", {"a"}) == ["- b: /nodes/b.memory.json"], "only the one that was not executed is left out"
    assert lines("d", {"b"}) == ["- a: /nodes/a.memory.json"], "the records beyond it are still named"


async def test_a_memory_block_leaves_out_an_upstream_that_was_not_executed() -> None:
    spec = _memory_spec(
        [
            {"id": "a", "subagent": "x", "prompt_template": "a"},
            {"id": "b", "subagent": "x", "prompt_template": "second", "depends_on": ["a"]},
        ]
    )
    by_id = {n.id: n for n in spec.nodes}
    kwargs = {"backend": _FakeBackend(), "cwd": "/w", "nodes_root": "/nodes", "run_id": "r1", "by_id": by_id}

    assert "/nodes/a.memory.json" in await render_prompt(by_id["b"], **kwargs)
    assert await render_prompt(by_id["b"], not_executed={"a"}, **kwargs) == "second"


# --- branches: what a later run and a reader make of a node that was not executed ------

_FIX_WAS_NOT_EXECUTED = SessionNodes(state={"fix": "not_executed"}, owner={"fix": "r1"}, has_output={"fix": False})


def test_referencing_a_not_executed_node_names_the_unselected_branch() -> None:
    from raven.agent.subagent.dag_graph import _unreadable

    message = _unreadable("later", "'fix'", "fix", _FIX_WAS_NOT_EXECUTED)

    assert message is not None
    assert "did not execute" in message
    assert "failed" not in message
    assert "skipped" not in message
    assert message.startswith("node 'later' references 'fix', which run 'r1' did not execute")
    assert "its branch was not selected, so it wrote no output" in message
    assert "Continue without it" in message


@pytest.mark.parametrize(
    "later",
    [
        {"depends_on": ["fix"]},
        {"prompt_template": "{{ fix.output }}"},
        {"prompt_template": "{{ inputs.p }}", "inputs": {"p": {"node": "fix"}}},
    ],
    ids=["depends_on", "output reference", "node input"],
)
def test_a_later_graph_that_reads_a_not_executed_node_is_refused_with_the_branch_sentence(later: dict) -> None:
    """Without the branch each call site fell back to a generic "unknown id" refusal for a node the session knows."""
    spec = parse_dag_spec({"task_summary": "g", "nodes": [_branch_node(id="later", **later)]})

    with pytest.raises(DagValidationError) as refusal:
        validate_and_order(spec, session_nodes=_FIX_WAS_NOT_EXECUTED)

    assert "which run 'r1' did not execute" in str(refusal.value)


def test_a_caller_with_no_graph_gets_the_same_sentence_about_a_not_executed_node() -> None:
    from raven.agent.subagent.dag_graph import check_node_refs

    with pytest.raises(DagValidationError, match="which run 'r1' did not execute"):
        check_node_refs("spawned", parse_placeholders("read {{ fix.output }}"), {}, _FIX_WAS_NOT_EXECUTED)


# What a plain node reads back with, key for key: `dag.get` returns the reader's dict as it is.
_PLAIN_NODE_KEYS = {
    "node",
    "subagent",
    "depends_on",
    "instance",
    "status",
    "started_at",
    "ended_at",
    "prompt_file",
    "output_file",
    "memory_file",
    "error",
    "prompt_template",
    "node_summary",
    "inputs",
}

_BRANCH_RUN_ID = "20261015T120000Z-0badc0de"


def _branching_nodes() -> list[dict]:
    """Two decisions (one `select: one`, one `select: many`), a guarded node, and a node inside its branch."""
    return [
        _branch_node(id="notes"),
        _branch_node(
            id="triage",
            routes={"question": "it asks how", "bug": "it reports a defect", "feature": "it wants something new"},
        ),
        _branch_node(id="scan", select="many", routes={"docs": "it touches docs", "api": "it touches the api"}),
        _branch_node(id="fix", depends_on=["triage"], when="triage.bug"),
        _branch_node(id="verify", depends_on=["fix"], prompt_template="check {{ fix.output }}"),
        _branch_node(id="reply", depends_on=["triage", "scan"], when="triage.done and scan.done"),
    ]


def _recorded(depends_on: list[str], status: str, **branched: Any) -> dict:
    """One node's manifest entry as `_finalize` writes it; `branched` is what a branch added."""
    return {
        "status": status,
        "subagent": "x",
        "depends_on": depends_on,
        "instance": None,
        "instance_auto": False,
        "started_at": 1000,
        "ended_at": 2000,
        "prompt_file": None,
        "output_file": None,
        "error": None,
        **branched,
    }


def _store_branching_run(be: "_FakeBackend", *, finalized: bool = True) -> None:
    """Write the run `_branching_nodes` describes the way `store.init` and `_finalize` write it.

    `triage` selected `question`, so `fix` was not executed and `verify`, whose only dependency it is,
    followed it; `scan` selected nothing.
    """
    nodes = _branching_nodes()
    assert _branch_graph(nodes) == [], "the stored graph is one the validator accepts"
    rdir = f"/hist/mas_dag/{_BRANCH_RUN_ID}"
    be.files[f"{rdir}/graph.json"] = (
        parse_dag_spec({"task_summary": "triage it", "nodes": nodes}).model_dump_json().encode()
    )
    if finalized:
        manifest = {
            "notes": _recorded([], "completed"),
            "triage": _recorded(
                [], "completed", selected_routes=["question"], route_evidence="the issue asks how to reproduce it"
            ),
            "scan": _recorded([], "completed", selected_routes=[]),
            "fix": _recorded(["triage"], "not_executed", reason="when_false"),
            "verify": _recorded(["fix"], "not_executed", reason="upstream_not_executed"),
            "reply": _recorded(["triage", "scan"], "completed"),
        }
        be.files[f"{rdir}/manifest.json"] = json.dumps(manifest).encode()


async def _read_branching_run(*, finalized: bool = True) -> dict:
    be = _FakeBackend()
    _store_branching_run(be, finalized=finalized)
    return await read_run(be, "/hist/mas_dag", _BRANCH_RUN_ID, "/hist/nodes")


async def test_read_run_counts_a_not_executed_node_in_its_summary() -> None:
    run = await _read_branching_run()

    assert run["summary"] == {"total": 6, "completed": 4, "failed": 0, "skipped": 0, "cancelled": 0, "not_executed": 2}


async def test_read_run_gives_each_node_the_branch_fields_it_has_and_no_others() -> None:
    """`dag.get` hands this dict over RPC as it is, so a node that has no selection, no reason and no
    guard must not carry six null keys for them."""
    run = await _read_branching_run()

    by_node = {entry["node"]: entry for entry in run["files"]}
    assert {node_id: set(entry) - _PLAIN_NODE_KEYS for node_id, entry in by_node.items()} == {
        "notes": set(),
        "triage": {"selected_routes", "route_evidence", "routes", "select"},
        "scan": {"selected_routes", "routes", "select"},
        "fix": {"reason", "when"},
        "verify": {"reason"},
        "reply": {"when"},
    }
    assert all(_PLAIN_NODE_KEYS <= set(entry) for entry in by_node.values()), "nothing the reader had is gone"


async def test_read_run_reads_the_outcome_off_the_manifest_and_the_request_off_the_graph() -> None:
    run = await _read_branching_run()

    by_node = {entry["node"]: entry for entry in run["files"]}
    assert by_node["fix"]["status"] == "not_executed"
    assert by_node["fix"]["reason"] == "when_false"
    assert by_node["verify"]["reason"] == "upstream_not_executed"
    assert by_node["triage"]["selected_routes"] == ["question"]
    assert by_node["triage"]["route_evidence"] == "the issue asks how to reproduce it"
    assert by_node["fix"]["when"] == "triage.bug"
    assert by_node["reply"]["when"] == "triage.done and scan.done"
    assert by_node["scan"]["select"] == "many"
    assert by_node["triage"]["select"] == "one", "left unset in the graph, it is the one that applies"


async def test_read_run_keeps_an_empty_selection_as_an_empty_list() -> None:
    """A `select: many` decision that chose nothing is still a decision: a falsy value is not an absent one."""
    run = await _read_branching_run()

    scan = next(entry for entry in run["files"] if entry["node"] == "scan")
    assert scan["selected_routes"] == []
    assert "route_evidence" not in scan, "no quote was recorded, so no key is invented"


async def test_read_run_names_a_decisions_routes_in_declaration_order_and_not_their_conditions() -> None:
    run = await _read_branching_run()

    by_node = {entry["node"]: entry for entry in run["files"]}
    assert by_node["triage"]["routes"] == ["question", "bug", "feature"], "declared order, which sorting would lose"
    assert by_node["scan"]["routes"] == ["docs", "api"]
    assert "it reports a defect" not in json.dumps(run), "a condition is the judge's, and no display needs it"


async def _read_one_node(node: dict) -> dict:
    """The reader's entry for one hand-written graph.json node, as a person editing the file could leave it."""
    be = _FakeBackend()
    graph = {"task_summary": "g", "nodes": [{"id": "n", "subagent": "x", **node}]}
    be.files[f"/hist/mas_dag/{_BRANCH_RUN_ID}/graph.json"] = json.dumps(graph).encode()
    return (await read_run(be, "/hist/mas_dag", _BRANCH_RUN_ID, "/hist/nodes"))["files"][0]


@pytest.mark.parametrize(
    ("node", "shown"),
    [
        ({"routes": _TWO_ROUTES}, {"routes": ["a", "b"], "select": "one"}),
        ({"routes": _TWO_ROUTES, "select": None}, {"routes": ["a", "b"], "select": "one"}),
        ({"routes": _TWO_ROUTES, "select": "one"}, {"routes": ["a", "b"], "select": "one"}),
        ({"routes": _TWO_ROUTES, "select": "many"}, {"routes": ["a", "b"], "select": "many"}),
        ({}, {}),
        ({"routes": None, "select": None}, {}),
        ({"routes": {}}, {}),
        ({"routes": {}, "select": "many"}, {}),
        ({"select": "many"}, {}),
    ],
    ids=["unset", "null", "one", "many", "plain", "plain-nulls", "empty-routes", "empty-routes-many", "stray-select"],
)
async def test_read_run_shows_routes_and_the_effective_select_only_on_a_node_that_branches(
    node: dict, shown: dict
) -> None:
    """The run-started event says `select: one` for a decision that left it unset; the snapshot says the same.

    A node branches when it has a route in `routes`, as the runner reads it, so an empty `routes` reads as none
    and a stray `select` on a node with no routes is not shown.
    """
    entry = await _read_one_node(node)

    assert {key: entry[key] for key in ("routes", "select") if key in entry} == shown


@pytest.mark.parametrize("routes", [5, ["a"], "ab", True], ids=["number", "list", "string", "bool"])
async def test_read_run_does_not_trip_over_routes_that_are_not_a_mapping(routes: object) -> None:
    """A hand-edited graph.json must not raise out of `dag.get` -- `list(5)` does -- nor be read as a decision:
    `list("ab")` would have made two routes of one string."""
    entry = await _read_one_node({"routes": routes, "select": "many"})

    assert not {"routes", "select"} & set(entry)


async def test_an_unfinalized_branching_run_still_shows_what_each_node_was_asked_to_branch_on() -> None:
    """Structure always comes from graph.json; only what happened waits for the manifest."""
    run = await _read_branching_run(finalized=False)

    by_node = {entry["node"]: entry for entry in run["files"]}
    assert {node_id: set(entry) - _PLAIN_NODE_KEYS for node_id, entry in by_node.items()} == {
        "notes": set(),
        "triage": {"routes", "select"},
        "scan": {"routes", "select"},
        "fix": {"when"},
        "verify": set(),
        "reply": {"when"},
    }
    assert {entry["status"] for entry in run["files"]} == {"pending"}
    assert run["summary"] == {"total": 6, "completed": 0, "failed": 0, "skipped": 0, "cancelled": 0}


async def test_read_run_leaves_out_a_blank_guard() -> None:
    """A blank `when` is no guard to the scheduler and to the run-started event, so the reader shows none.

    Stored the way `store.init` stores a graph: every field dumped, a blank as the model gave it. The
    guard that is real is the control, and it reads back as declared.
    """
    nodes = [
        _decision("r"),
        _branch_node(id="empty", when=""),
        _branch_node(id="spaces", when="   "),
        _branch_node(id="g", depends_on=["r"], when="  r.a "),
    ]
    assert _branch_graph(nodes) == [], "a blank guard is a valid graph"
    stored = parse_dag_spec({"task_summary": "g", "nodes": nodes}).model_dump_json()
    assert '"when":""' in stored and '"when":"   "' in stored, "the blanks reach graph.json as they were given"
    be = _FakeBackend()
    be.files[f"/hist/mas_dag/{_BRANCH_RUN_ID}/graph.json"] = stored.encode()

    run = await read_run(be, "/hist/mas_dag", _BRANCH_RUN_ID, "/hist/nodes")

    by_node = {entry["node"]: entry for entry in run["files"]}
    assert {node_id: set(entry) - _PLAIN_NODE_KEYS for node_id, entry in by_node.items()} == {
        "r": {"routes", "select"},
        "empty": set(),
        "spaces": set(),
        "g": {"when"},
    }
    assert by_node["g"]["when"] == "  r.a "


async def test_read_run_does_not_trip_over_a_guard_that_is_not_text() -> None:
    """graph.json is a file a person can edit: a guard that is not text is no guard, and reading it must not raise."""
    be = _FakeBackend()
    graph = {"task_summary": "g", "nodes": [{"id": "n", "subagent": "x", "when": 5}]}
    be.files[f"/hist/mas_dag/{_BRANCH_RUN_ID}/graph.json"] = json.dumps(graph).encode()

    run = await read_run(be, "/hist/mas_dag", _BRANCH_RUN_ID, "/hist/nodes")

    assert "when" not in run["files"][0]


@pytest.mark.parametrize("finalized", [True, False], ids=["finalized", "in-flight"])
@pytest.mark.parametrize("graph", ["hand-written", "dumped"])
async def test_a_run_without_branches_reads_back_with_exactly_the_keys_it_always_had(
    graph: str, finalized: bool
) -> None:
    """The "dumped" graph is what `store.init` really writes: every branch field present, as null."""
    be = _FakeBackend()
    run_id = "20260730T060242Z-6b0b89a3"
    _seed_run(be, run_id, finalized=finalized)
    if graph == "dumped":
        path = f"/hist/mas_dag/{run_id}/graph.json"
        be.files[path] = parse_dag_spec(json.loads(be.files[path])).model_dump_json().encode()
        assert '"routes":null' in be.files[path].decode(), "the fixture carries the nulls this test is about"

    run = await read_run(be, "/hist/mas_dag", run_id, "/hist/nodes")

    assert [set(entry) for entry in run["files"]] == [_PLAIN_NODE_KEYS, _PLAIN_NODE_KEYS]
    assert list(run["summary"]) == ["total", "completed", "failed", "skipped", "cancelled"]
    assert set(run) == {
        "run_id",
        "dir",
        "nodes_root",
        "finalized",
        "task_summary",
        "files",
        "terminal_outputs",
        "summary",
    }


def _status_blocks(text: str) -> dict[str, list[str]]:
    """`dag_status` text cut into the lines of each node, keyed by node id."""
    blocks: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("- "):
            current = blocks.setdefault(line[2:].split(" [", 1)[0], [])
        if current is not None and not line.startswith("Run dir:"):
            current.append(line)
    return blocks


async def test_dag_status_renders_a_stored_branching_run_through_the_reader() -> None:
    """The renderer's own tests feed it records shaped like the reader's; this is the reader's answer, whole."""
    from raven.agent.subagent.dag_control_tools import _render_run

    out = _render_run(await _read_branching_run())

    assert out.splitlines()[0] == (
        f"DAG run {_BRANCH_RUN_ID}: 4 completed, 0 failed, 0 cancelled, 0 skipped, 2 not executed (of 6)."
    )
    blocks = _status_blocks(out)
    assert blocks["fix"][0] == "- fix [not_executed]"
    assert "    reason: when_false" in blocks["fix"]
    assert "    reason: upstream_not_executed" in blocks["verify"]
    assert "    selected_routes: question" in blocks["triage"]
    assert "    selected_routes: (none)" in blocks["scan"]
    assert not [
        line for node_id in ("notes", "reply") for line in blocks[node_id] if "reason" in line or "selected" in line
    ]
    assert "the issue asks how to reproduce it" not in out, "the judge's quote is not model-facing text"


class _EchoAgent:
    """A node backend that answers with its own id, whatever it is asked."""

    async def run(self, task: str, *, task_id: str, **_: Any) -> str:
        return f"OUT[{task_id}]"


async def _run_a_branching_graph() -> tuple[Any, "_FakeBackend", list[tuple[str, dict]]]:
    """Run for real: a router, a dependent of it that is not executed, a `select: many` router that chooses
    nothing, and a node that runs. Returns the result, the store, and the progress events published."""
    from raven.agent.subagent.dag_runner import run_dag
    from raven.agent.subagent.dag_verdict import Verdict

    async def judge(*, node, store, output, error, crashed, output_limited=False):
        if node.id == "triage":
            return Verdict(accomplished=True, routes=("docs",), route_evidence="the issue is about the docs")
        if node.id == "scan":
            return Verdict(accomplished=True, routes=())
        return Verdict(accomplished=True)

    events: list[tuple[str, dict]] = []

    async def publish(name: str, value: dict) -> None:
        events.append((name, value))

    be = _FakeBackend()
    spec = parse_dag_spec(
        {
            "task_summary": "triage it",
            "nodes": [
                _branch_node(id="triage", routes={"docs": "a documentation gap", "bug": "a code defect"}),
                _branch_node(id="fix", depends_on=["triage"], when="triage.bug"),
                _branch_node(id="scan", select="many", routes={"a": "it touches a", "b": "it touches b"}),
                _branch_node(id="reply", depends_on=["triage", "scan"], when="triage.done and scan.done"),
            ],
        }
    )
    result = await run_dag(
        spec,
        resolve=lambda node: _EchoAgent(),
        backend=be,
        workdir="/w",
        run_root="/hist/mas_dag",
        nodes_root="/hist/nodes",
        history_root="/hist",
        judge_node=judge,
        progress_publisher=publish,
    )
    return result, be, events


def _per_node(entries: Any, keys: tuple[str, ...]) -> dict[str, dict]:
    """For each entry, those of `keys` it carries, and nothing else."""
    return {entry["node"]: {key: entry[key] for key in keys if key in entry} for entry in entries}


async def test_read_run_returns_the_outcomes_the_runner_wrote_under_the_names_it_wrote_them() -> None:
    """The reader tests above read a manifest written by hand. This one reads what `_finalize` wrote, so the
    writer's key names and the reader's cannot drift apart while each side's own tests stay green."""
    result, be, _ = await _run_a_branching_graph()
    manifest = json.loads(be.files[f"{result.dir}/manifest.json"])
    outcome_keys = ("reason", "selected_routes", "route_evidence")

    run = await read_run(be, "/hist/mas_dag", result.run_id, "/hist/nodes")

    written = _per_node([{**entry, "node": node} for node, entry in manifest.items()], outcome_keys)
    assert written == {
        "triage": {"selected_routes": ["docs"], "route_evidence": "the issue is about the docs"},
        "fix": {"reason": "when_false"},
        "scan": {"selected_routes": []},
        "reply": {},
    }, "what the runner wrote, spelled out: a writer and a reader gone quiet together must not pass"
    assert _per_node(result.files, outcome_keys) == written, "the record it returned says the same"
    assert _per_node(run["files"], outcome_keys) == written


async def test_read_run_describes_each_node_the_way_the_run_started_event_did() -> None:
    """Both say `select: one` for a decision that left it unset, and neither says anything of a node that does
    not branch: a client that read the event and one that read the snapshot must not disagree about a node."""
    result, be, events = await _run_a_branching_graph()
    started = next(value for name, value in events if name == "dag_run_started")["nodes"]
    request_keys = ("routes", "select", "when")

    run = await read_run(be, "/hist/mas_dag", result.run_id, "/hist/nodes")

    announced = {node["id"]: {key: node[key] for key in request_keys if key in node} for node in started}
    assert announced == {
        "triage": {"routes": ["docs", "bug"], "select": "one"},
        "fix": {"when": "triage.bug"},
        "scan": {"routes": ["a", "b"], "select": "many"},
        "reply": {"when": "triage.done and scan.done"},
    }
    assert _per_node(run["files"], request_keys) == announced


# --- the model-facing text: the schema's sentences, the guide, the glossary -----


def _repo_text(relative: str) -> str:
    return (Path(__file__).resolve().parent.parent / relative).read_text(encoding="utf-8")


def _heading_span(text: str, heading: str) -> str:
    """The text from a heading to the next one at the same or a higher level.

    A fenced block is skipped over: the examples these documents carry are full of
    `#`-prefixed lines -- a prompt's own headings, a shell comment -- and none of
    them is a heading of the document.
    """
    start = text.index(heading)
    level = len(heading) - len(heading.lstrip("#"))
    rest = text[start + len(heading) :]
    fenced = False
    for line in rest.splitlines():
        if line.startswith("```"):
            fenced = not fenced
            continue
        if not fenced and line.startswith("#") and len(line) - len(line.lstrip("#")) <= level:
            return rest[: rest.index(line)]
    return rest


def _glossary_entry(text: str, term: str) -> str:
    """One glossary entry, from its bold term to the next entry or heading.

    The entry is where the term opens a line -- a glossary also names its terms
    inside other entries. An entry may run over several paragraphs; what ends it is
    a blank line followed by the next bold term or a heading.
    """
    start = re.search(rf"^{re.escape(term)}", text, re.M)
    assert start is not None, f"no glossary entry opens with {term}"
    rest = text[start.end() :]
    end = re.search(r"\n\n(?=\*\*|#)", rest)
    return rest[: end.start()] if end else rest


def _flat(text: str) -> str:
    """The text with every run of whitespace as one space, so a phrase matches
    however the document happens to wrap it."""
    return " ".join(text.split())


_SKILL = "raven/memory_engine/skills/subagent-dag-orchestration/SKILL.md"


def test_the_skill_documents_conditional_branches() -> None:
    """The body a model reads before it writes a branching graph.

    Every rule pinned here is one the runtime refuses a graph for, or one a graph
    that follows the rest of the guide gets wrong without it.
    """
    section = _flat(_heading_span(_repo_text(_SKILL), "## Conditional branches"))

    for field in ("`routes`", "`select`", "`when`"):
        assert field in section
    # The guard grammar: both atoms, the three operators, the parentheses.
    for token in ("`<node>.<route>`", "`<node>.done`", "`not`", "`and`", "`or`", "parentheses"):
        assert token in section, f"the guard grammar's {token} is not explained"
    # Spaces separate tokens; inside an atom (`r . a`) the guard does not parse.
    assert "Spacing between tokens is free; an atom is one token, written without spaces." in section
    # A decision node's prompt asks for a conclusion; conditions are decidable and
    # a single-select decision has a route for everything else.
    assert "asks for the conclusion" in section
    assert "mutually exclusive and decidable" in section
    assert "catch-all" in section
    # Every decision node a node depends on is named in its `when`: the general rule,
    # and the same rule for one decision depending on another.
    assert (
        "**Every decision node in `depends_on` must appear in the node's `when`.** A node cannot depend on a "
        "decision and stay silent about it"
    ) in section
    assert "decision node that depends on another decision node" in section
    # One titled block per input that may be missing, and no `@nodes/` path to a
    # node that may not run -- where "may not run" counts `.done` too.
    assert "one titled block per input that may be" in section
    assert "## Bug fix result" in section
    assert "not** with `ref:@nodes/`" in section
    assert (
        "*may not run* when it has a `when` of its own, even `<node>.done`, which holds whichever route is "
        "selected, or when it depends on nodes of this graph that all may not run"
    ) in section
    # The fixed sentence, verbatim, is what a dependent of the same graph reads.
    assert MISSING_OUTPUT_TEXT in section
    assert "When a node reads a dependency of the same graph that was not executed" in section
    for reason in ("`when_false`", "`when_unknown`", "`upstream_not_executed`"):
        assert reason in section
    # Unknown is a value of its own, and the upstream check runs before the guard.
    assert "Unknown is not false: a route of a decision node that was never executed is *unknown*" in section
    assert (
        "that check comes first, so `<node>.done`, true whatever the decision did, cannot revive a node with "
        "no input left on that path"
    ) in section


def test_the_skill_says_which_nodes_of_a_branch_need_its_route() -> None:
    """The runner leaves a node out with a branch only when every dependency it has in
    the graph was left out. A node of the branch that also depends on something
    outside it runs on the route not taken unless its own guard names the route -- and
    `.done`, which holds whichever route is selected, does not -- so the guide may not
    say the entry's guard covers the whole branch. The rule is the branch's own: a
    merge node meant to run on every route, like the example's `reply`, keeps `.done`."""
    section = _flat(_heading_span(_repo_text(_SKILL), "## Conditional branches"))

    assert (
        "**Guard the entry node of a branch, and every node of the branch that also depends on something outside it.**"
    ) in section
    assert "a branch node needs no guard beyond naming any decision node it depends on" in section
    assert (
        "A node of the branch that also depends on something outside it, such as the decision node itself or a "
        "node that runs whichever route is selected, is not left out that way: unless its own `when` names the "
        "branch's route, it can run when the branch does not"
    ) in section
    assert "Name the route, as `<node>.<route>`, not `<node>.done`, which holds whichever route is selected" in section
    assert "since a guard may only read a dependency" in section
    assert (
        "A merge node that should run on every route, like `reply` in the example below, keeps `<node>.done`."
    ) in section
    assert "A node that also depends on something outside the branch" not in section
    assert "Only a branch's entry node needs a guard" not in section


def test_the_skill_says_when_a_route_failure_and_a_missing_judge_end_the_node() -> None:
    """Two outcomes a graph's author cannot see from the schema: a decision node
    whose route cannot be established fails, and a host with no judge refuses the
    graph outright rather than running it down no branch."""
    section = _flat(_heading_span(_repo_text(_SKILL), "## Conditional branches"))

    assert '"The route could not be determined: <reason>"' in section
    assert "A host with no model to judge with refuses" in section


def test_the_skill_names_a_node_that_prints_a_route_as_an_anti_pattern() -> None:
    """The node's own text selects nothing, which is the one thing a model reaching
    for this feature is most likely to get wrong: it will ask the step to print its
    answer as a route name unless the guide says not to."""
    anti_patterns = _flat(_heading_span(_repo_text(_SKILL), "## Anti-patterns"))

    assert "**Don't** ask a node to print a route marker" in anti_patterns
    assert "**Don't** repeat the entry guard on every node inside a branch" in anti_patterns
    # Qualified the way the rule is: a copy says nothing only where every dependency
    # lies inside the branch.
    assert "A node whose dependencies all lie inside the branch is left out with its entry" in anti_patterns
    assert (
        "The exception is a node of the branch that also depends on something outside it, the decision node "
        "included: it needs the branch's route in its own `when`"
    ) in anti_patterns
    assert "The exception is a node that also depends on something outside the branch" not in anti_patterns


def test_the_skills_resident_description_names_the_branch_in_one_graph() -> None:
    """The description rides in every system prompt and is the only part of this
    guide an ordinary turn reads, so a model that never opens the body still has to
    learn here that one graph can branch rather than only run in series."""
    import raven

    skill = Path(raven.__file__).parent / "memory_engine" / "skills" / "subagent-dag-orchestration" / "SKILL.md"
    head = skill.read_text(encoding="utf-8").split("---")[1]
    description = next(line for line in head.splitlines() if line.startswith("description: "))

    assert (
        "A step whose result decides what runs next is a decision node: give it routes, and guard what follows "
        "with when, all in one graph."
    ) in description
    # Loops are still graphs in series: a branch is a decision inside one graph, not a cycle.
    assert "Iteration is graphs in series, one graph per round, driven by you." in description


def test_the_skills_worked_example_is_a_graph_the_runtime_accepts() -> None:
    """The example is copied, so it has to be a valid submission as written -- and
    the one submission that exercises the whole feature: two decisions, a guarded
    branch off each, a merge behind both, and a merge that reads a branch it did not
    take."""
    section = _heading_span(_repo_text(_SKILL), "## Conditional branches")
    block = section.split("```json", 1)[1].split("```", 1)[0]
    arguments = json.loads(block)

    spec = parse_dag_spec(arguments)
    order = validate_and_order(spec)
    assert sorted(order) == sorted(node.id for node in spec.nodes)
    position = {node_id: index for index, node_id in enumerate(order)}
    assert all(position[dep] < position[node.id] for node in spec.nodes for dep in node.depends_on)
    by_id = {node.id: node for node in spec.nodes}
    # The prose around the example counts what it shows, and calls a guarded node a
    # node that carries a guard rather than a guard.
    prose = _flat(section)
    assert "One graph, two decisions, four guarded steps:" in prose
    assert sum(1 for node in spec.nodes if node.routes) == 2
    assert sum(1 for node in spec.nodes if node.when) == 4
    assert "`fix`, `design` and `deep_review` each open a branch, and each carries that branch's guard." in prose
    assert by_id["triage"].routes == {
        "bug": "the issue is caused by a defect in the code",
        "feature": "the issue asks for new behaviour",
        "question": "anything else: a usage question, or an issue that is neither a defect nor a request for new behaviour",
    }
    assert by_id["deep_review"].when == "(triage.bug or triage.feature) and risk.high"
    assert "{{ fix.output }}" in by_id["reply"].prompt_template
    assert "ref:@nodes/" not in by_id["reply"].prompt_template


def test_context_md_defines_the_branch_terms() -> None:
    """`CONTEXT.md` is where a new domain term is defined, and these four carry
    meanings a reader cannot guess: which node decides, what a guard may read, and
    why a node that never ran is a state of its own."""
    text = _repo_text("CONTEXT.md")

    decision = _flat(_glossary_entry(text, "**Decision node**"))
    assert "`routes`" in decision and "`select`" in decision
    guard = _flat(_glossary_entry(text, "**Guard**"))
    assert "`when`" in guard and "`<node>.done`" in guard
    assert "`not_executed`" in _glossary_entry(text, "**Not executed**")

    route = _flat(_glossary_entry(text, "**Route (DAG branch)**"))
    assert "_Avoid_" in route, "the entry has to rule out the routing-table rows and the runner's own route check"
    assert "**Routing entry**" in route
    assert "`_route_available`" in route


def test_the_glossary_keeps_every_not_executed_consequence_with_the_state() -> None:
    """Three things follow from the status, and two of them are defined where a
    reader of another entry looks: the fixed sentence is written unfenced, and the
    memory block leaves the node out."""
    text = _repo_text("CONTEXT.md")

    entry = _flat(_glossary_entry(text, "**Not executed**"))
    for reason in ("`when_false`", "`when_unknown`", "`upstream_not_executed`"):
        assert reason in entry
    assert "`skipped`" in entry

    fence = _flat(_glossary_entry(text, "**The fence rule**"))
    assert "not executed: that is the runner's own words, so it is written unfenced" in fence

    memory = _flat(_glossary_entry(text, "**Memory record**"))
    assert "A node that was not executed gets no line" in memory


def test_the_tui_glossary_says_what_the_turned_in_line_carries() -> None:
    """The panel's second line is where a person reads what a branch did; the
    glossary describes it field by field, so the description has to name the
    branch parts or the next change to it reads as free."""
    entry = _flat(_glossary_entry(_repo_text("ui-tui/CONTEXT.md"), "**DAG Panel**"))

    assert "why it was not executed, or what a decision selected" in entry
    assert "guard" in entry
    assert "route evidence" in entry
    assert "`/dag <node>`" in entry


def test_the_tasks_list_design_keeps_the_counts_and_node_fields_the_reader_emits() -> None:
    """`docs/specs/` is kept matching the code; `docs/plans/` is the archive."""
    text = _repo_text("docs/specs/2026-09-18-desk-tasks-list-design.md")

    counts = text[text.index("`counts` (") : text.index("`nodes[]`")]
    assert "`not_executed`" in counts

    task_node = text[text.index("`TaskNode`:") :]
    task_node = task_node[: task_node.index("\n\n")]
    for field in ("`reason`", "`selected_routes`", "`route_evidence`", "`routes`", "`select`", "`when`"):
        assert field in task_node


def test_the_playbook_spec_lists_the_branch_fields_and_drops_the_stale_limit() -> None:
    """A playbook's node model is the graph's node model, so the playbook file
    format has to carry the same fields -- and a branch is expressible, which the
    "cannot be expressed" list used to deny."""
    text = _repo_text("docs/specs/playbook-spec.md")

    for field, kind in (("routes", "map"), ("select", "enum"), ("when", "string")):
        assert f"| `{field}` | {kind} |" in text
    assert "skills | mcps | instance | inputs | routes | select | when" in text
    assert "conditional skipping" not in text
    assert "**Two things that cannot be expressed**: cycles" in text, "loops are still not expressible"
    # The run prefix moves the node part of every `when` atom with the ids, and only a
    # dependent that runs reads the stand-in.
    assert "`dependsOn`, `{{ id.output }}` and the node ids in `when` atoms rewritten in sync" in text
    assert 'a dependent that still runs reads the fixed "not executed" sentence in its place' in text


_DOCS_EN = "docs-site/docs/orchestration.md"
_DOCS_ZH = "docs-site/docs/orchestration.zh.md"


def _docs_section(text: str, title: str, anchor: str) -> str:
    """One second-level section of a docs page: the English heading by its words, the
    Chinese twin by the anchor it has to pin, since its own title slugifies to nothing."""
    heading = next(
        (
            line
            for line in text.splitlines()
            if line == f"## {title}" or (line.startswith("## ") and f"{{ #{anchor} }}" in line)
        ),
        None,
    )
    assert heading is not None, f"the page has no section #{anchor}"
    return _heading_span(text, heading)


def _verdict_paragraph(text: str) -> str:
    """The paragraph that introduces the verdict -- in either language, the one that bolds it."""
    start = text.index("**verdict")
    return text[text.rfind("\n\n", 0, start) + 2 : text.index("\n\n", start)]


def _paragraph_with(text: str, marker: str) -> str:
    """The first paragraph of ``text`` that contains ``marker``."""
    paragraph = next((paragraph for paragraph in text.split("\n\n") if marker in paragraph), None)
    assert paragraph is not None, f"no paragraph carries {marker}"
    return paragraph


def _code_spans(text: str) -> list[str]:
    """Every inline code span, in order: the part of a sentence both languages spell alike."""
    return re.findall(r"`[^`]+`", _flat(text))


@pytest.mark.parametrize("page", [_DOCS_EN, _DOCS_ZH], ids=["en", "zh"])
def test_the_docs_site_describes_branches(page: str) -> None:
    """The published pages are the only place a user reads what `routes`, `select`
    and `when` do, and the two languages have to say the same things.

    Both pages are checked through what they share -- the fields, the atoms, the
    statuses and the sentence are code in either language -- so this file names
    no word of the Chinese page.
    """
    text = _repo_text(page)
    raw_section = _docs_section(text, "Conditional branches", "conditional-branches")
    section = _flat(raw_section)

    for code in ("`routes`", "`select`", "`when`", "`<node>.<route>`", "`<node>.done`", "`depends_on`"):
        assert code in section
    for reason in ("`when_false`", "`when_unknown`", "`upstream_not_executed`"):
        assert reason in section
    assert "`not_executed`" in section
    assert MISSING_OUTPUT_TEXT in section, "the stand-in sentence is quoted verbatim"
    assert "`ref:@nodes/`" in section
    # The two paragraphs that state rules rather than name fields -- which decision
    # nodes a guard names and which nodes of a branch need one; what may not run and
    # how to read it -- carry the same code, in the same order, as their twin.
    twin = _DOCS_ZH if page == _DOCS_EN else _DOCS_EN
    twin_section = _docs_section(_repo_text(twin), "Conditional branches", "conditional-branches")
    for marker in ("`<node>.<route>`", "`ref:@nodes/`"):
        assert _code_spans(_paragraph_with(raw_section, marker)) == _code_spans(
            _paragraph_with(twin_section, marker)
        ), f"the paragraph with {marker} says something its twin does not"
    # A later graph gets a refusal, not the stand-in: the section points at the rule.
    assert "(#node-contract-and-data-flow)" in section

    contract = _docs_section(text, "Node contract and data flow", "node-contract-and-data-flow")
    for field in ("routes", "select", "when"):
        assert f"| `{field}` |" in contract, f"the node contract table lists `{field}`"
    statuses = _docs_section(text, "Failure handling and replanning", "failure-handling-and-replanning")
    assert "| `not_executed` |" in statuses, "the status table lists the new status"


def test_the_english_docs_page_says_who_needs_a_guard_and_who_reads_the_stand_in() -> None:
    """The rules the code-span check above holds the Chinese page to, in the English
    page's own words: which nodes of a branch need its route, that the stand-in is
    for the same graph only, and what "may not run" covers."""
    text = _repo_text(_DOCS_EN)
    section = _flat(_docs_section(text, "Conditional branches", "conditional-branches"))

    assert "Every decision node in a node's `depends_on` must appear in its `when`" in section
    assert "so a branch node needs no guard beyond naming any decision node it depends on" in section
    assert (
        "A node of the branch that also depends on something outside it, the decision node included, needs the "
        "branch's route in its own `when`, as `<node>.<route>` rather than `<node>.done`"
    ) in section
    assert "A merge node that should run on every route keeps `<node>.done`." in section
    assert "A node that also depends on a node outside the branch" not in section
    assert "Only a branch's first node needs a guard" not in section
    assert "A node of the same graph that reads one receives this sentence" in section
    assert "a later graph that names a node an earlier run did not execute is refused" in section
    assert (
        "A node may not run when it has a `when` of its own, `<node>.done` included, or when it depends on "
        "nodes of the graph that all may not run"
    ) in section

    contract = _flat(_docs_section(text, "Node contract and data flow", "node-contract-and-data-flow"))
    assert "Earlier failed, skipped, canceled, interrupted, not-executed, or still-running nodes" in contract


@pytest.mark.parametrize("page", [_DOCS_EN, _DOCS_ZH], ids=["en", "zh"])
def test_the_docs_site_no_longer_offers_to_turn_the_judge_off(page: str) -> None:
    """The switch is gone from the code and migrated out of configs, so a page that
    still offers to turn the judge off describes a build nobody can run.

    Read the same way in both languages: the verdict paragraph names the judge once
    -- the ordinary node's call that may fail or time out; the switch was the second
    mention -- and points at the branch rules for the decision node's exception, and
    the `subagentDag` defaults list the judge's timeout and no verdict switch.
    """
    text = _repo_text(page)

    paragraph = _verdict_paragraph(text)
    assert paragraph.count("judge") == 1, paragraph
    assert "(#conditional-branches)" in paragraph

    settings = text[text.index("`subagentDag`") :]
    assert "verdict" not in settings[: settings.index("180")], "the defaults no longer list a verdict switch"
    assert "verdictEnabled" not in text


def test_the_changelog_entry_names_the_feature_and_the_retired_switch() -> None:
    """`## Unreleased` is hand-kept, so nothing but this test notices an entry the
    feature should have had. The retired switch changes what an upgraded build does,
    so it is filed under Changed, apart from the feature it serves."""
    unreleased = _heading_span(_repo_text("CHANGELOG.md"), "## Unreleased")
    added = _flat(_heading_span(unreleased, "### Added"))
    changed = _flat(_heading_span(unreleased, "### Changed"))

    assert "`routes`" in added and "`when`" in added
    assert "`not_executed`" in added
    assert "a node of the same graph that reads one gets a fixed sentence" in added
    assert "`subagentDag.verdictEnabled`" not in added
    assert "`subagentDag.verdictEnabled`" in changed
    # What retiring the switch does to a config that had judgement off, said plainly;
    # the no-judge refusal is a fact of its own, not the reason the switch went.
    assert (
        "A config that turned judgement off now has every finished node judged: one more model call per node" in changed
    )
    assert "`raven playbook run` judges its nodes too" in changed
    assert "A host with no model to judge with refuses a graph that declares routes." in changed
    assert "reads exactly as it did" not in added


@pytest.mark.parametrize("encoded", [False, True])
async def test_registry_dag_node_aliases_reach_the_parser_in_canonical_form(tmp_path, monkeypatch, encoded):
    from copy import deepcopy

    from raven.agent.subagent.dag_tool import SubAgentDagTool
    from raven.agent.tools.registry import ToolRegistry

    tool = SubAgentDagTool(
        workspace=tmp_path, agents=[ThirdPartyCliSubagentConfig(name="x", command="unused-test-agent")]
    )
    captured = []

    async def execute(**params):
        captured.append(parse_dag_spec(params).model_dump())
        return "accepted graph"

    monkeypatch.setattr(tool, "execute", execute)
    registry = ToolRegistry()
    registry.register(tool)
    nodes = [
        {"id": "a", "subagent": "x", "nodeSummary": "first", "promptTemplate": "First task"},
        {"id": "b", "subagent": "x", "nodeSummary": "second", "promptTemplate": "Next task", "dependsOn": ["a"]},
    ]
    params = {"taskSummary": "two tasks", "nodes": json.dumps(nodes) if encoded else nodes}
    original = deepcopy(params)
    result = await registry.execute(tool.name, params)
    assert result == "accepted graph"
    assert captured[0]["task_summary"] == "two tasks"
    assert captured[0]["nodes"][1]["depends_on"] == ["a"]
    assert captured[0]["nodes"][0]["node_summary"] == "first"
    assert params == original


@pytest.mark.parametrize(
    "extra,graph_extra,fragment",
    [
        ({"dependsOn": ["a"], "depends_on": []}, {}, "conflicts"),
        ({"task_summary": "not a node summary"}, {}, "task_summary is not allowed"),
        ({}, {"taskSummary": "another title"}, "taskSummary conflicts with task_summary"),
    ],
)
async def test_registry_dag_aliases_do_not_overwrite_or_guess_keys(tmp_path, monkeypatch, extra, graph_extra, fragment):
    from raven.agent.subagent.dag_tool import SubAgentDagTool
    from raven.agent.tools.registry import ToolRegistry

    tool = SubAgentDagTool(
        workspace=tmp_path, agents=[ThirdPartyCliSubagentConfig(name="x", command="unused-test-agent")]
    )
    called = []

    async def execute(**params):
        called.append(params)
        return "unexpected"

    monkeypatch.setattr(tool, "execute", execute)
    registry = ToolRegistry()
    registry.register(tool)
    node = {"id": "b", "subagent": "x", "node_summary": "second", "prompt_template": "Next task", **extra}
    result = await registry.execute(tool.name, {"task_summary": "two tasks", "nodes": [node], **graph_extra})
    assert result.startswith("Error: Invalid parameters")
    assert fragment in result
    assert called == []


@pytest.mark.parametrize("field,value", [("id", "not an id"), ("subagent", " padded "), ("instance", " padded ")])
def test_dag_node_patterns_are_still_enforced_by_the_native_parser(field, value):
    from pydantic import ValidationError

    from raven.agent.subagent.dag_graph import DagNodeSpec

    valid = {"id": "node", "subagent": "x", "node_summary": "work", "prompt_template": "Task", "instance": "worker"}
    DagNodeSpec.model_validate(valid)
    with pytest.raises(ValidationError) as caught:
        DagNodeSpec.model_validate({**valid, field: value})
    assert [(error["loc"], error["type"]) for error in caught.value.errors()] == [((field,), "string_pattern_mismatch")]


def test_the_node_schema_omits_only_the_playbook_resource_fields(tmp_path):
    from raven.agent.subagent.dag_graph import DagNodeSpec
    from raven.agent.subagent.dag_tool import SubAgentDagTool

    assert set(SubAgentDagTool(workspace=tmp_path).node_schema()["properties"]) == set(DagNodeSpec.model_fields) - {
        "skills",
        "mcps",
    }


@pytest.mark.parametrize("field", ["skills", "mcps"])
@pytest.mark.parametrize("value", [None, [], ["resource"]])
async def test_registry_dag_nodes_reject_playbook_resource_fields(tmp_path, monkeypatch, field, value):
    from raven.agent.subagent.dag_tool import SubAgentDagTool
    from raven.agent.tools.registry import ToolRegistry

    tool = SubAgentDagTool(
        workspace=tmp_path, agents=[ThirdPartyCliSubagentConfig(name="x", command="unused-test-agent")]
    )
    captured = []

    async def execute(**params):
        captured.append(params)
        return "unexpected dispatch"

    monkeypatch.setattr(tool, "execute", execute)
    registry = ToolRegistry()
    registry.register(tool)
    node = {"id": "a", "subagent": "x", "node_summary": "work", "prompt_template": "Task", field: value}
    result = await registry.execute(tool.name, {"task_summary": "work", "nodes": [node]})
    assert result.startswith("Error: Invalid parameters")
    assert f"nodes[0].{field} is not allowed" in result
    assert captured == []
    assert getattr(parse_dag_spec({"task_summary": "work", "nodes": [node]}).nodes[0], field) == value
