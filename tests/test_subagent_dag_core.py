"""Ported DAG core (req4/P3): graph validation, placeholders, render, store.

Covers the provider-agnostic core of raven.agent.subagent_dag (no agentscope);
render/store are exercised over a tiny in-memory duck-typed backend.
"""

from __future__ import annotations

import json
import posixpath

import pytest

from raven.agent.subagent_dag import (
    AgentCapabilities,
    DagReadError,
    DagRunStore,
    DagValidationError,
    SessionNodes,
    check_confined,
    make_run_id,
    parse_dag_spec,
    parse_placeholders,
    read_node,
    read_run,
    render_prompt,
    split_reference,
    validate_and_order,
    validate_capabilities,
)
from raven.agent.subagent_dag.tool import _NODE_SCHEMA

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


@pytest.mark.parametrize("name", ["General Audit", "MiniMax M2.5", "研究员", "claude_code", "a"])
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
    out_path = "/hist/mas_dag/run/a.out.md"
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
    rendered = await render_prompt(node, backend=be, cwd="/w", output_paths={"a": out_path})
    assert "RESULT_A" in rendered
    assert f"path={out_path}" in rendered
    assert "lit=LITERAL" in rendered


async def test_injected_content_is_fenced_but_the_template_is_not() -> None:
    """A node output is sub-agent-authored and a referenced file may hold
    anything a run fetched; neither is an instruction this prompt may carry.
    The template's own words, a literal input and the ``_path`` forms are the
    author's and stay verbatim."""
    be = _FakeBackend()
    out_path = "/hist/mas_dag/run/a.out.md"
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
    rendered = await render_prompt(spec.nodes[0], backend=be, cwd="/w", output_paths={"a": out_path})

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
    store = DagRunStore(be, "/w", "run123")
    await store.init('{"nodes": []}')
    p = store.output_path("a")
    await store.write_text(p, "hello out")
    assert await store.read_text(p) == "hello out"
    assert store.run_dir.endswith("run123")


# --- reader ---------------------------------------------------------------


def _seed_run(be: "_FakeBackend", run_id: str, *, finalized: bool) -> None:
    """Write a two-node run dir the way DagRunStore/_finalize would."""
    rdir = f"/hist/mas_dag/{run_id}"
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
    be.files[f"{rdir}/a.prompt.md"] = b"do LITERAL"
    be.files[f"{rdir}/a.out.md"] = b"OUTPUT_A"
    be.files[f"{rdir}/a.memory.json"] = b"{}"
    if finalized:
        manifest = {
            "a": {
                "status": "completed",
                "subagent": "x",
                "depends_on": [],
                "instance": None,
                "started_at": 1000,
                "ended_at": 3000,
                "prompt_file": f"{rdir}/a.prompt.md",
                "output_file": f"{rdir}/a.out.md",
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

    run = await read_run(be, "/hist/mas_dag", "20260730T060242Z-6b0b89a3")

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
    assert a["memory_file"] == "/hist/mas_dag/20260730T060242Z-6b0b89a3/a.memory.json"
    assert b["memory_file"] is None, "a memory file that was never written reads back as absent"


async def test_read_run_of_an_unfinalized_run_falls_back_to_the_graph() -> None:
    be = _FakeBackend()
    _seed_run(be, "20260730T060242Z-6b0b89a3", finalized=False)

    run = await read_run(be, "/hist/mas_dag", "20260730T060242Z-6b0b89a3")

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


async def test_read_run_without_a_run_dir_raises() -> None:
    with pytest.raises(DagReadError):
        await read_run(_FakeBackend(), "/w", "20260730T060242Z-6b0b89a3")


@pytest.mark.parametrize("run_id", ["../../etc", "not-a-run-id", "", "20260730T060242Z-ZZZZZZZZ"])
async def test_read_run_rejects_a_malformed_run_id(run_id: str) -> None:
    with pytest.raises(DagReadError):
        await read_run(_FakeBackend(), "/w", run_id)


@pytest.mark.parametrize("node_id", ["../graph", "a/b", "a.out", ""])
async def test_read_node_rejects_a_malformed_node_id(node_id: str) -> None:
    be = _FakeBackend()
    _seed_run(be, "20260730T060242Z-6b0b89a3", finalized=True)
    with pytest.raises(DagReadError):
        await read_node(be, "/hist/mas_dag", "20260730T060242Z-6b0b89a3", node_id)


async def test_read_node_returns_the_rendered_prompt_and_output() -> None:
    be = _FakeBackend()
    _seed_run(be, "20260730T060242Z-6b0b89a3", finalized=True)

    node = await read_node(be, "/hist/mas_dag", "20260730T060242Z-6b0b89a3", "a")

    assert node["prompt"] == "do LITERAL"
    assert node["output"] == "OUTPUT_A"
    assert node["output_chars"] == 8
    assert node["output_truncated"] is False


async def test_read_node_truncates_a_long_output_and_says_so() -> None:
    be = _FakeBackend()
    run_id = "20260730T060242Z-6b0b89a3"
    _seed_run(be, run_id, finalized=True)
    be.files[f"/hist/mas_dag/{run_id}/a.out.md"] = b"x" * 5000

    node = await read_node(be, "/hist/mas_dag", run_id, "a", max_output_chars=100)

    assert len(node["output"]) == 100
    assert node["output_chars"] == 5000
    assert node["output_truncated"] is True


async def test_read_node_of_a_node_that_never_ran_is_empty_not_an_error() -> None:
    be = _FakeBackend()
    _seed_run(be, "20260730T060242Z-6b0b89a3", finalized=True)

    node = await read_node(be, "/hist/mas_dag", "20260730T060242Z-6b0b89a3", "b")

    assert node["prompt"] is None
    assert node["output"] is None
    assert node["output_chars"] == 0


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


def test_the_runs_prefix_cannot_escape_the_run_history() -> None:
    check_confined("@runs/r1/a.out.md", what="ref", roots=_ROOTS)
    with pytest.raises(DagValidationError, match="DAG run history"):
        check_confined("@runs/../../etc/passwd", what="ref", roots=_ROOTS)
    with pytest.raises(DagValidationError, match="empty"):
        check_confined("@runs/", what="ref", roots=_ROOTS)


def test_split_reference_names_the_root() -> None:
    assert split_reference("@runs/r1/a.out.md") == ("runs", "r1/a.out.md")
    assert split_reference("notes.md") == ("workdir", "notes.md")


# --- render across runs --------------------------------------------------


def _one_node(template: str, **extra: object) -> object:
    return parse_dag_spec(
        {
            "task_summary": "render a single node prompt",
            "nodes": [{"id": "n", "subagent": "x", "prompt_template": template, **extra}],
        }
    ).nodes[0]


async def test_render_reaches_an_earlier_run_through_the_runs_prefix() -> None:
    be = _FakeBackend()
    be.files["/hist/mas_dag/r1/plan.out.md"] = b"EARLIER"

    node = _one_node("text={{ ref:@runs/r1/plan.out.md }} path={{ ref_path:@runs/r1/plan.out.md }}")
    rendered = await render_prompt(node, backend=be, cwd="/w", output_paths={}, runs_root="/hist/mas_dag")

    assert "EARLIER" in rendered
    assert "path=/hist/mas_dag/r1/plan.out.md" in rendered


async def test_a_runs_reference_is_refused_when_no_history_root_is_known() -> None:
    node = _one_node("{{ ref:@runs/r1/plan.out.md }}")
    with pytest.raises(DagValidationError, match="run history"):
        await render_prompt(node, backend=_FakeBackend(), cwd="/w", output_paths={})


@pytest.mark.parametrize(
    ("template", "extra"),
    [
        ("{{ ref_path:gone.md }}", {}),
        ("{{ inputs.k.path }}", {"inputs": {"k": {"file": "gone.md"}}}),
    ],
)
async def test_a_path_placeholder_naming_a_missing_file_is_refused(template: str, extra: dict) -> None:
    node = _one_node(template, **extra)
    with pytest.raises(DagValidationError, match="does not exist"):
        await render_prompt(node, backend=_FakeBackend(), cwd="/w", output_paths={})


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


def test_declaring_mcps_always_downgrades_while_the_wiring_is_absent() -> None:
    """Honest about the implementation, not about the declaration.

    A built-in agent's row says it *could* take mcp servers, so reading the notice
    off `injectable_mcps` would report success for something nothing implements.
    One explicit flag drives it, and flipping that flag is what stops the notice.
    """
    spec = _spec({"id": "a", "subagent": "x", "prompt_template": "go", "mcps": ["github"]})

    notices = validate_capabilities(spec, {"x": AgentCapabilities(injectable_mcps=True)})

    assert len(notices) == 1
    assert "not implemented" in notices[0]


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

    They are playbook-only: the engine folds ``skills`` into the step's prompt
    and reports ``mcps`` as unhonoured, so a model composing a graph has one
    thing to write per step. The node model still accepts them, because a
    playbook file is parsed into it -- what changed is what the model is told
    exists. Asserted on the advertised schema rather than on the model, since
    that is the half a re-addition would show up in.
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
        output_paths={},
        runs_root="/hist/mas_dag",
        run_id="run1",
        by_id=by_id,
    )
    # Transitive: c depends on b, which depends on a.
    assert "/hist/mas_dag/run1/b.memory.json" in rendered
    assert "/hist/mas_dag/run1/a.memory.json" in rendered
    # Its own record is not upstream of itself.
    assert "c.memory.json" not in rendered


async def test_a_root_node_is_told_nothing() -> None:
    spec = _memory_spec(_CHAIN)
    by_id = {n.id: n for n in spec.nodes}
    rendered = await render_prompt(
        by_id["a"],
        backend=_FakeBackend(),
        cwd="/w",
        output_paths={},
        runs_root="/hist/mas_dag",
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
        output_paths={},
        runs_root="/hist/mas_dag",
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
        output_paths={},
        runs_root="/hist/mas_dag",
        run_id="run1",
        by_id=by_id,
        capabilities={"x": AgentCapabilities(reads_local_files=False)},
    )
    assert rendered == "third"
    assert "memory.json" not in rendered


async def test_a_cross_run_upstream_is_named_with_its_own_run() -> None:
    spec = _memory_spec([{"id": "d", "subagent": "x", "prompt_template": "only", "depends_on": ["earlier"]}])
    by_id = {n.id: n for n in spec.nodes}
    rendered = await render_prompt(
        by_id["d"],
        backend=_FakeBackend(),
        cwd="/w",
        output_paths={},
        runs_root="/hist/mas_dag",
        run_id="run2",
        by_id=by_id,
        session_nodes=SessionNodes(owner={"earlier": "run1"}, state={"earlier": "completed"}),
    )
    assert "/hist/mas_dag/run1/earlier.memory.json" in rendered
