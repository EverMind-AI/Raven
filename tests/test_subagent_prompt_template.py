"""The prompt-template layer shared by the spawn and DAG delegation surfaces."""

from __future__ import annotations

from pathlib import Path

import pytest

from raven.agent.subagent.prompt_backend import LocalFileBackend
from raven.agent.subagent.prompt_capabilities import check_path_placeholders
from raven.agent.subagent.prompt_errors import DagValidationError
from raven.agent.subagent.prompt_paths import check_confined, within
from raven.agent.subagent.prompt_placeholders import parse_placeholders
from raven.agent.subagent.prompt_render import read_text, render_template


async def test_render_inlines_a_ref_and_an_input(tmp_path: Path) -> None:
    (tmp_path / "plan.md").write_text("ship it", encoding="utf-8")
    (tmp_path / "notes.md").write_text("be careful", encoding="utf-8")

    rendered = await render_template(
        "plan: {{ ref:plan.md }} / note: {{ inputs.n }} / lit: {{ inputs.k }}",
        {"n": {"file": "notes.md"}, "k": "verbatim"},
        backend=LocalFileBackend(),
        cwd=str(tmp_path),
        runs_root=None,
        roots=(str(tmp_path),),
    )

    # Both file reads are fenced and the literal is not: the author typed the
    # literal here, and a file's contents are not their words. The fence mints a
    # fresh nonce per call, so the shape is asserted rather than the text.
    lines = rendered.splitlines()
    assert lines[0].startswith("plan: [BEGIN UNTRUSTED file ")
    assert lines[1] == "ship it"
    assert lines[2].startswith("[END UNTRUSTED file ")
    assert " / note: [BEGIN UNTRUSTED file " in lines[2]
    assert lines[3] == "be careful"
    assert lines[4].startswith("[END UNTRUSTED file ")
    assert lines[4].endswith("] / lit: verbatim")


async def test_render_gives_a_path_for_the_path_forms(tmp_path: Path) -> None:
    (tmp_path / "plan.md").write_text("ship it", encoding="utf-8")

    rendered = await render_template(
        "{{ ref_path:plan.md }}",
        {},
        backend=LocalFileBackend(),
        cwd=str(tmp_path),
        runs_root=None,
        roots=(str(tmp_path),),
    )

    assert rendered == str(tmp_path / "plan.md")


async def test_render_refuses_a_path_form_naming_a_missing_file(tmp_path: Path) -> None:
    with pytest.raises(DagValidationError, match="is not a file it can read"):
        await render_template(
            "{{ ref_path:gone.md }}",
            {},
            backend=LocalFileBackend(),
            cwd=str(tmp_path),
            runs_root=None,
            roots=(str(tmp_path),),
        )


async def test_a_ref_naming_a_directory_is_refused_rather_than_raised(tmp_path: Path) -> None:
    """A directory passed the old existence check and reached ``read_bytes``,
    which raised ``IsADirectoryError`` out of the tool -- an unshaped traceback
    carrying a host path, where every other mistake in a reference comes back as
    advice the model can act on."""
    (tmp_path / "notes").mkdir()

    with pytest.raises(DagValidationError, match="is not a file it can read"):
        await render_template(
            "{{ ref:notes }}",
            {},
            backend=LocalFileBackend(),
            cwd=str(tmp_path),
            runs_root=None,
            roots=(str(tmp_path),),
        )


def test_confinement_rejects_an_escaping_ref(tmp_path: Path) -> None:
    with pytest.raises(DagValidationError, match="outside the session workdir"):
        check_confined("../secrets.md", what="ref", roots=(str(tmp_path),))


def test_a_symlink_out_of_every_root_is_refused(tmp_path: Path) -> None:
    """The escape this guard missed while it compared strings.

    Nothing exotic is needed to place the link: git carries absolute symlinks,
    so a repository cloned into the working directory is enough, and the host
    model does not have to be complicit. The lexical check saw a path inside the
    root and the read followed the link to wherever it pointed.
    """
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    (work / "notes.md").symlink_to(outside / "secret.txt")

    with pytest.raises(DagValidationError, match="outside the session workdir"):
        check_confined("notes.md", what="ref", roots=(str(work),))


def test_a_symlinked_directory_cannot_be_walked_out_of_a_root(tmp_path: Path) -> None:
    """One link on a directory would otherwise open everything beneath it."""
    outside = tmp_path / "outside" / "deep"
    outside.mkdir(parents=True)
    (outside / "secret.txt").write_text("TOP SECRET", encoding="utf-8")
    work = tmp_path / "work"
    work.mkdir()
    (work / "escape").symlink_to(tmp_path / "outside")

    with pytest.raises(DagValidationError, match="outside the session workdir"):
        check_confined("escape/deep/secret.txt", what="ref", roots=(str(work),))


def test_a_symlink_that_stays_inside_a_root_is_allowed(tmp_path: Path) -> None:
    """The rule is about where a link goes, not that a link was used."""
    work = tmp_path / "work"
    (work / "real").mkdir(parents=True)
    (work / "real" / "plan.md").write_text("ship it", encoding="utf-8")
    (work / "plan.md").symlink_to(work / "real" / "plan.md")

    check_confined("plan.md", what="ref", roots=(str(work),))


def test_a_root_reached_through_a_symlink_still_holds_its_own_files(tmp_path: Path) -> None:
    """Both sides go through realpath, so a root that is itself behind a link --
    a bind mount, a home directory that is one, /tmp on a mac -- does not refuse
    everything under it. Resolving only the reference would do exactly that.
    """
    real = tmp_path / "real"
    real.mkdir()
    (real / "plan.md").write_text("ship it", encoding="utf-8")
    linked = tmp_path / "linked"
    linked.symlink_to(real)

    check_confined("plan.md", what="ref", roots=(str(linked),))


def test_within_is_textual_and_prefix_exact() -> None:
    assert within("/a/b/c", "/a/b")
    assert within("/a/b", "/a/b")
    assert not within("/a/bc", "/a/b")


def test_the_grammar_travels_with_the_layer() -> None:
    kinds = [ph.kind for ph in parse_placeholders("{{ ref:a }} {{ inputs.b }} {{ inputs.b.path }} {{ c.output }}")]

    assert kinds == ["ref", "input", "input_path", "output"]


async def test_a_node_reference_is_refused_with_the_file_alternative(tmp_path: Path) -> None:
    """Spec D3: this layer has no graph, so a node reference must not resolve.

    It must not pass through as text either -- that would send the sub-agent a
    literal `{{ plan.output }}`. The refusal names the form to use instead.
    """
    with pytest.raises(DagValidationError, match="only run_subagent_dag can resolve"):
        await render_template(
            "{{ plan.output }}",
            {},
            backend=LocalFileBackend(),
            cwd=str(tmp_path),
            runs_root=None,
            roots=(str(tmp_path),),
        )


def test_the_gate_takes_parsed_placeholders_so_grammar_is_not_its_job() -> None:
    """The gate used to parse its own template, so it could catch a grammar
    error raised mid-parse and re-label it as a capability refusal. Taking
    placeholders already parsed makes that unreachable from inside this
    function -- a malformed template is the caller's own parse step to raise.
    """
    placeholders = parse_placeholders("read {{ a.output_path }}")

    with pytest.raises(DagValidationError) as exc:
        check_path_placeholders(placeholders, "x", reads_local_files=False)

    assert str(exc.value) == (
        "this task passes local file paths to sub-agent 'x', which the "
        "roster tags [no-local-files]: it cannot open them, so the path "
        "would reach it as meaningless text. Replace each with the content "
        "form ({{ a.output_path }} -> use {{ a.output }})."
    )


async def test_a_node_shaped_input_is_refused_too(tmp_path: Path) -> None:
    with pytest.raises(DagValidationError, match="only run_subagent_dag can resolve"):
        await render_template(
            "{{ inputs.up }}",
            {"up": {"node": "plan"}},
            backend=LocalFileBackend(),
            cwd=str(tmp_path),
            runs_root=None,
            roots=(str(tmp_path),),
        )


async def test_an_unknown_brace_body_survives_rendering(tmp_path: Path) -> None:
    rendered = await render_template(
        "why does {{ item.name }} not render?",
        {},
        backend=LocalFileBackend(),
        cwd=str(tmp_path),
        runs_root=None,
        roots=(str(tmp_path),),
    )

    assert rendered == "why does {{ item.name }} not render?"


def test_only_the_shape_matching_body_is_a_placeholder() -> None:
    found = parse_placeholders("{{ item.name }} {{ ref:a.md }} {{ 5 + 5 }}")

    assert [(ph.kind, ph.name) for ph in found] == [("ref", "a.md")]


async def test_a_missing_input_key_is_refused(tmp_path: Path) -> None:
    """A key the task never defined is a typo, not a value.

    Substituting `str(None)` put the literal text "None" in front of the
    sub-agent, which reads as an answer rather than as the mistake it is.
    """
    with pytest.raises(DagValidationError, match="input 'pln' is not defined"):
        await render_template(
            "{{ inputs.pln }}",
            {},
            backend=LocalFileBackend(),
            cwd=str(tmp_path),
            runs_root=None,
            roots=(str(tmp_path),),
        )


async def test_a_defined_input_key_still_renders_its_literal(tmp_path: Path) -> None:
    rendered = await render_template(
        "{{ inputs.k }}",
        {"k": "verbatim"},
        backend=LocalFileBackend(),
        cwd=str(tmp_path),
        runs_root=None,
        roots=(str(tmp_path),),
    )

    assert rendered == "verbatim"


async def test_an_explicitly_null_input_is_refused_too(tmp_path: Path) -> None:
    """`{"k": None}` carries no usable value either, and `str(None)` would put
    the same misleading text in the prompt."""
    with pytest.raises(DagValidationError, match="input 'k' is not defined"):
        await render_template(
            "{{ inputs.k }}",
            {"k": None},
            backend=LocalFileBackend(),
            cwd=str(tmp_path),
            runs_root=None,
            roots=(str(tmp_path),),
        )


def test_an_empty_ref_body_is_still_refused() -> None:
    with pytest.raises(DagValidationError, match="empty path"):
        parse_placeholders("{{ ref: }}")


async def test_the_read_leaves_the_fence_to_whoever_asked_for_it(tmp_path: Path) -> None:
    """The fence follows the kind of reference, which only the caller knows, so
    the read hands back the bytes and each caller wraps what it got with the
    source its placeholder names."""
    history = tmp_path / "subagents"
    (history / "spawn" / "c1").mkdir(parents=True)
    (history / "spawn" / "c1" / "out.md").write_text("upstream said this", encoding="utf-8")

    text = await read_text(
        LocalFileBackend(),
        str(history / "spawn" / "c1" / "out.md"),
        str(tmp_path),
        None,
        what="{{ ref:... }}",
    )

    assert text == "upstream said this"


async def test_a_workspace_file_is_fenced_too(tmp_path: Path) -> None:
    """The rule this replaces keyed the fence on the sub-agent history root, so
    a file read from the working directory came through bare -- and a checkout
    someone put in the working directory is exactly where text that must not be
    read as instructions arrives. Asserted structurally, since the fence mints a
    fresh nonce each call."""
    (tmp_path / "spec.md").write_text("user wrote this", encoding="utf-8")

    rendered = await render_template(
        "{{ ref:spec.md }}",
        {},
        backend=LocalFileBackend(),
        cwd=str(tmp_path),
        runs_root=None,
        roots=(str(tmp_path),),
    )

    lines = rendered.splitlines()
    assert lines[0].startswith("[BEGIN UNTRUSTED file ")
    assert lines[1] == "user wrote this"
    assert lines[2].startswith("[END UNTRUSTED file ")


async def test_a_declared_input_no_placeholder_names_is_refused(tmp_path: Path) -> None:
    """Material arrives only where a placeholder puts it, so a declared key with
    no placeholder would not arrive at all -- silently, which is the failure the
    parameter exists to prevent. Refused before the first read: a call this
    wrong must not leave a file opened or a sub-agent dispatched.
    """
    (tmp_path / "notes.md").write_text("be careful", encoding="utf-8")

    with pytest.raises(DagValidationError, match="declared but never referenced"):
        await render_template(
            "do the thing",
            {"n": {"file": "notes.md"}},
            backend=LocalFileBackend(),
            cwd=str(tmp_path),
            runs_root=None,
            roots=(str(tmp_path),),
        )


async def test_the_refusal_names_every_stranded_key(tmp_path: Path) -> None:
    with pytest.raises(DagValidationError, match="inputs 'a', 'b' are declared"):
        await render_template(
            "{{ inputs.c }}",
            {"a": "x", "b": "y", "c": "z"},
            backend=LocalFileBackend(),
            cwd=str(tmp_path),
            runs_root=None,
            roots=(str(tmp_path),),
        )


async def test_a_path_form_counts_as_referencing_its_key(tmp_path: Path) -> None:
    """The two forms name the same key, so either one is a reference."""
    (tmp_path / "notes.md").write_text("be careful", encoding="utf-8")

    rendered = await render_template(
        "{{ inputs.n.path }}",
        {"n": {"file": "notes.md"}},
        backend=LocalFileBackend(),
        cwd=str(tmp_path),
        runs_root=None,
        roots=(str(tmp_path),),
    )

    assert rendered == str(tmp_path / "notes.md")


async def test_an_input_object_naming_neither_a_file_nor_a_node_is_refused(tmp_path: Path) -> None:
    """It rendered as its own repr before -- a sub-agent handed `{'nope': 1}` as
    though that were the material it was told to work from."""
    with pytest.raises(DagValidationError, match="neither a file nor a node"):
        await render_template(
            "{{ inputs.n }}",
            {"n": {"nope": 1}},
            backend=LocalFileBackend(),
            cwd=str(tmp_path),
            runs_root=None,
            roots=(str(tmp_path),),
        )
