"""Ported DAG core: graph validation, placeholders, render, store.

Covers the provider-agnostic core of the sub-agent DAG subsystem (no agentscope);
render/store are exercised over a tiny in-memory duck-typed backend.
"""

from __future__ import annotations

import json
import posixpath
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest

from raven.agent.loop.bundles import SubagentWiring, ToolWiring, TurnPolicy
from raven.agent.subagent.dag_capabilities import validate_capabilities
from raven.agent.subagent.dag_graph import collect_static_graph_errors, parse_dag_spec, validate_and_order
from raven.agent.subagent.dag_reader import DagReadError, read_node, read_run
from raven.agent.subagent.dag_render import render_prompt
from raven.agent.subagent.dag_store import DagRunStore, SessionNodes, make_run_id
from raven.agent.subagent.dag_tool import _NODE_SCHEMA
from raven.agent.subagent.prompt_backend import LocalFileBackend
from raven.agent.subagent.prompt_capabilities import AgentCapabilities
from raven.agent.subagent.prompt_errors import DagValidationError
from raven.agent.subagent.prompt_paths import check_confined, split_reference
from raven.agent.subagent.prompt_placeholders import parse_placeholders
from raven.config.schema import ThirdPartyCliSubagentConfig

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


def test_skills_for_an_agent_that_cannot_take_them_is_a_notice() -> None:
    """A capability gap downgrades; a safety breach refuses.

    A playbook written on a better-equipped machine should still run here with the
    parts that work -- but the caller has to be told, or a wrong result is
    unattributable.
    """
    spec = _spec({"id": "a", "subagent": "x", "prompt_template": "go", "skills": ["research"]})
    caps = {"x": AgentCapabilities(injectable_skills=False)}

    notices = validate_capabilities(spec, caps)

    assert len(notices) == 1
    assert "cannot take injected skills" in notices[0]
    assert "'a'" in notices[0]


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
    assert set(_NODE_SCHEMA["properties"]) == {
        "id",
        "subagent",
        "node_summary",
        "prompt_template",
        "depends_on",
        "inputs",
        "instance",
    }
    # Still parseable, so a playbook's own nodes[] survives the round trip.
    spec = parse_dag_spec(
        {
            "task_summary": "run a lone node carrying skills",
            "nodes": [{"id": "a", "subagent": "x", "prompt_template": "hi", "skills": ["s"]}],
        }
    )
    assert spec.nodes[0].skills == ["s"]


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
    assert cfg.verdict_enabled is True
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
    assert tool._verdict_config.verdict_enabled is True


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

    The five settled states are the control. They are asserted here too, so a
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
