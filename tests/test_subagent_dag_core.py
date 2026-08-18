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

# --- graph ---------------------------------------------------------------


def test_topo_order_and_parse() -> None:
    spec = parse_dag_spec(
        {
            "nodes": [
                {"id": "a", "subagent": "x", "prompt_template": "hello"},
                {"id": "b", "subagent": "x", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
            ]
        }
    )
    assert validate_and_order(spec) == ["a", "b"]


@pytest.mark.parametrize("name", ["General Audit", "MiniMax M2.5", "研究员", "claude_code", "a"])
def test_subagent_name_takes_any_name_the_config_layer_accepts(name: str) -> None:
    # A sub-agent name is a roster key, never a path: `spawn` dispatches to
    # "General Audit" happily, so a DAG node naming the same agent must not be
    # refused for the name alone. Only the node `id` stays charset-restricted,
    # because it becomes `<id>.prompt.md`.
    spec = parse_dag_spec({"nodes": [{"id": "a", "subagent": name, "prompt_template": "hi"}]})
    assert spec.nodes[0].subagent == name


@pytest.mark.parametrize("name", ["", " ", " coder", "coder ", "\tcoder", "two\nlines"])
def test_subagent_name_rejects_blank_and_edge_whitespace(name: str) -> None:
    # Edge whitespace is invisible, so " coder" would fail the roster lookup
    # against a name that looks identical to the configured one -- refused here
    # where the error can name the field instead.
    with pytest.raises(DagValidationError):
        parse_dag_spec({"nodes": [{"id": "a", "subagent": name, "prompt_template": "hi"}]})


@pytest.mark.parametrize("node_id", ["has space", "a/b", "..", "a.out", ""])
def test_node_id_keeps_its_closed_charset(node_id: str) -> None:
    # The id is a path component; widening the sub-agent charset must not have
    # widened this one with it.
    with pytest.raises(DagValidationError):
        parse_dag_spec({"nodes": [{"id": node_id, "subagent": "x", "prompt_template": "hi"}]})


def test_cycle_rejected() -> None:
    spec = parse_dag_spec(
        {
            "nodes": [
                {"id": "a", "subagent": "x", "prompt_template": "{{ b.output }}", "depends_on": ["b"]},
                {"id": "b", "subagent": "x", "prompt_template": "{{ a.output }}", "depends_on": ["a"]},
            ]
        }
    )
    with pytest.raises(DagValidationError):
        validate_and_order(spec)


def test_default_deny_undeclared_reference() -> None:
    spec = parse_dag_spec(
        {
            "nodes": [
                {"id": "a", "subagent": "x", "prompt_template": "hi"},
                {"id": "b", "subagent": "x", "prompt_template": "{{ a.output }}"},  # no depends_on
            ]
        }
    )
    with pytest.raises(DagValidationError):
        validate_and_order(spec)


# --- capabilities --------------------------------------------------------

_STATELESS_BOXED = {"x": AgentCapabilities(stateful=False, reads_local_files=False)}
_FULL = {"x": AgentCapabilities(stateful=True, reads_local_files=True)}


def _spec(*nodes: dict) -> object:
    return parse_dag_spec({"nodes": list(nodes)})


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
            "nodes": [
                {
                    "id": "b",
                    "subagent": "x",
                    "prompt_template": "up={{ a.output }} path={{ a.output_path }} lit={{ inputs.k }}",
                    "depends_on": ["a"],
                    "inputs": {"k": "LITERAL"},
                }
            ]
        }
    )
    node = spec.nodes[0]
    rendered = await render_prompt(node, backend=be, cwd="/w", output_paths={"a": out_path})
    assert "up=RESULT_A" in rendered
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
        "nodes": [
            {"id": "a", "subagent": "x", "prompt_template": "do {{ inputs.k }}", "depends_on": [], "instance": None},
            {"id": "b", "subagent": "y", "prompt_template": "use {{ a.output }}", "depends_on": ["a"], "instance": "h"},
        ]
    }
    be.files[f"{rdir}/graph.json"] = json.dumps(graph).encode()
    be.files[f"{rdir}/a.prompt.md"] = b"do LITERAL"
    be.files[f"{rdir}/a.out.md"] = b"OUTPUT_A"
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
    assert (b["node"], b["status"], b["error"], b["instance"]) == ("b", "failed", "boom", "h")
    assert b["depends_on"] == ["a"]


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
    return parse_dag_spec({"nodes": [{"id": "n", "subagent": "x", "prompt_template": template, **extra}]}).nodes[0]


async def test_render_reaches_an_earlier_run_through_the_runs_prefix() -> None:
    be = _FakeBackend()
    be.files["/hist/mas_dag/r1/plan.out.md"] = b"EARLIER"

    node = _one_node("text={{ ref:@runs/r1/plan.out.md }} path={{ ref_path:@runs/r1/plan.out.md }}")
    rendered = await render_prompt(node, backend=be, cwd="/w", output_paths={}, runs_root="/hist/mas_dag")

    assert "text=EARLIER" in rendered
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
