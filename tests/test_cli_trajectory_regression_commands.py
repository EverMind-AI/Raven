"""Tests for the ``raven trajectory regression`` CLI subapp."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.cli.trajectory_commands import trajectory_app

runner = CliRunner()

SAMPLE_CASE = Path(__file__).parent / "trajectories" / "sample_reproduces_recording"


def _case_copy(root: Path, name: str = "case_a") -> Path:
    case = root / name
    shutil.copytree(SAMPLE_CASE, case)
    return case


def _plain(output: str) -> str:
    """Output with whitespace collapsed: Rich wraps long problem lines (they
    embed tmp paths) at the console width, splitting asserted phrases."""
    return " ".join(output.split())


def test_registered_under_trajectory() -> None:
    """Invocations go through trajectory_app on purpose: a bare single-command
    sub-app promotes its one command to the entry point, which is not how the
    installed CLI is called."""
    r = runner.invoke(trajectory_app, ["regression", "validate", "--help"])
    assert r.exit_code == 0
    assert "validate" in r.stdout


def test_validate_single_passing_case_exits_0(tmp_path) -> None:
    case = _case_copy(tmp_path)

    r = runner.invoke(trajectory_app, ["regression", "validate", str(case)])

    assert r.exit_code == 0, r.output
    assert "case_a" in r.stdout
    assert "0 problem(s)" in r.stdout


def test_validate_single_broken_case_exits_1_and_names_the_problem(tmp_path) -> None:
    case = _case_copy(tmp_path)
    (case / "case.yaml").unlink()

    r = runner.invoke(trajectory_app, ["regression", "validate", str(case)])

    assert r.exit_code == 1
    assert "case.yaml is missing" in _plain(r.stdout)


def test_validate_all_flags_the_broken_directory_among_valid_ones(tmp_path) -> None:
    _case_copy(tmp_path, "case_a")
    _case_copy(tmp_path, "case_b")
    (tmp_path / "broken").mkdir()

    r = runner.invoke(trajectory_app, ["regression", "validate", "--all", "--root", str(tmp_path)])

    assert r.exit_code == 1
    assert "broken" in r.stdout
    assert "expect.yaml is missing" in _plain(r.stdout)
    assert "3 case(s)" in r.stdout


def test_validate_all_passing_cases_exit_0(tmp_path) -> None:
    _case_copy(tmp_path, "case_a")
    _case_copy(tmp_path, "case_b")

    r = runner.invoke(trajectory_app, ["regression", "validate", "--all", "--root", str(tmp_path)])

    assert r.exit_code == 0, r.output
    assert "2 case(s), 0 problem(s)" in r.stdout


def _mutate_spans(case: Path, mutate) -> None:
    spans_path = case / "cassette" / "spans.jsonl"
    spans = [json.loads(x) for x in spans_path.read_text(encoding="utf-8").splitlines()]
    mutate(spans)
    spans_path.write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")


def _first_ref(case: Path, suffix: str) -> str:
    spans_path = case / "cassette" / "spans.jsonl"
    for line in spans_path.read_text(encoding="utf-8").splitlines():
        for key, ref in (json.loads(line).get("attributes") or {}).items():
            if key.endswith(suffix):
                return ref
    raise AssertionError(f"no reference for {suffix}")


def test_validate_all_survives_malformed_cases_and_still_checks_the_rest(tmp_path) -> None:
    """Structurally broken cases of every known shape must be reported as
    problems, not raise and cut the --all sweep short of the last, valid
    directory."""

    def bad_attrs(spans):
        spans[0]["attributes"] = ["not", "a", "mapping"]

    def bad_span_id(spans):
        spans[0]["spanId"] = ["bad"]

    bad_yaml = _case_copy(tmp_path, "a_bad_yaml")
    (bad_yaml / "case.yaml").write_text("issue: [\n", encoding="utf-8")
    _mutate_spans(_case_copy(tmp_path, "b_bad_attrs"), bad_attrs)
    bad_payload = _case_copy(tmp_path, "c_bad_payload")
    (bad_payload / "cassette" / _first_ref(bad_payload, "tool.output.artifact_path")).write_text(
        "[1]", encoding="utf-8"
    )
    _mutate_spans(_case_copy(tmp_path, "d_bad_span_id"), bad_span_id)
    int_keys = _case_copy(tmp_path, "e_int_keys")
    (int_keys / "case.yaml").write_text("42: foo\nbadkey: bar\n", encoding="utf-8")
    _case_copy(tmp_path, "z_good")

    r = runner.invoke(trajectory_app, ["regression", "validate", "--all", "--root", str(tmp_path)])

    assert r.exit_code == 1
    plain = _plain(r.stdout)
    assert "cannot be parsed as YAML" in plain
    assert "attributes must be a mapping" in plain
    assert "must hold a JSON object" in plain
    assert "spanId must be a string" in plain
    assert "keys must be strings" in plain
    assert "z_good" in plain
    assert "6 case(s)" in plain


def test_validate_all_missing_root_exits_1(tmp_path) -> None:
    r = runner.invoke(trajectory_app, ["regression", "validate", "--all", "--root", str(tmp_path / "nope")])
    assert r.exit_code == 1
    assert "is not a directory" in _plain(r.stdout)


def test_validate_all_empty_root_exits_1(tmp_path) -> None:
    r = runner.invoke(trajectory_app, ["regression", "validate", "--all", "--root", str(tmp_path)])
    assert r.exit_code == 1
    assert "no case directories" in _plain(r.stdout)


def test_validate_requires_exactly_one_of_case_dir_or_all(tmp_path) -> None:
    case = _case_copy(tmp_path)

    both = runner.invoke(trajectory_app, ["regression", "validate", str(case), "--all"])
    neither = runner.invoke(trajectory_app, ["regression", "validate"])

    assert both.exit_code == 1
    assert neither.exit_code == 1
    assert "exactly one of CASE_DIR or --all" in _plain(both.stdout)


# ── init ──────────────────────────────────────────────────────────────

_HEX_TOKEN = "a3f9c2b7d8e64a1b9c0d2e5f7a8b3c4d5e6f7a8b9c0d1e2f"

_RECORDED_INPUT = {
    "model": "stub",
    "messages": [{"role": "system", "content": "s"}, {"role": "user", "content": "go"}],
    "tools": [],
}


@pytest.fixture
def state(tmp_path, monkeypatch):
    state = tmp_path / "traces"
    monkeypatch.setenv("RAVEN_TRACING_DIR", str(state))
    monkeypatch.setattr("raven.trajectory.bundle._default_workspace", lambda: tmp_path / "ws")
    return state


def _source_bundle(root: Path, *, attempt_id: str = "att-src", token: str | None = None) -> Path:
    """A minimal minimizable bundle: one turn, one recorded model call."""
    bundle = root / attempt_id
    (bundle / "artifacts").mkdir(parents=True)
    (bundle / "artifacts" / "turn.json").write_text(
        json.dumps({"content": "go", "channel": "cli", "chat_id": "direct"}), encoding="utf-8"
    )
    content = "done" if token is None else f"done token {token}"
    (bundle / "artifacts" / "out.json").write_text(
        json.dumps({"content": content, "finish_reason": "stop", "tool_calls": [], "usage": {}}), encoding="utf-8"
    )
    (bundle / "artifacts" / "in.json").write_text(json.dumps(_RECORDED_INPUT), encoding="utf-8")
    spans = [
        {
            "traceId": attempt_id,
            "spanId": "llm-0",
            "name": "llm.call",
            "attributes": {
                "attempt.id": attempt_id,
                "session.key": "cli:init-test",
                "llm.input.artifact_path": "artifacts/in.json",
                "llm.output.artifact_path": "artifacts/out.json",
            },
        },
        {
            "traceId": attempt_id,
            "spanId": "turn-0",
            "name": "session.turn",
            "attributes": {
                "attempt.id": attempt_id,
                "session.key": "cli:init-test",
                "turn.input.artifact_path": "artifacts/turn.json",
            },
        },
    ]
    (bundle / "spans.jsonl").write_text("".join(json.dumps(s) + "\n" for s in spans), encoding="utf-8")
    (bundle / "manifest.json").write_text(json.dumps({"format_version": 1, "attempt_id": attempt_id}), encoding="utf-8")
    return bundle


def _init(args: list[str], **kwargs):
    return runner.invoke(trajectory_app, ["regression", "init", *args], **kwargs)


def _fill_required(case_dir: Path) -> None:
    path = case_dir / "case.yaml"
    text = path.read_text(encoding="utf-8")
    for key, value in (("issue", "#1"), ("owner", "forrest"), ("why", "guards the fix"), ("re_record", "never")):
        text = text.replace(f'{key}: ""', f'{key}: "{value}"', 1)
    path.write_text(text, encoding="utf-8")


def test_init_from_bundle_dir_scaffolds_a_draft(state, tmp_path) -> None:
    from raven.trajectory.regression import load_expectation, validate_case

    bundle = _source_bundle(tmp_path)
    root = tmp_path / "cases"

    r = _init([str(bundle), "--name", "sample_case", "--root", str(root), "--yes"])

    assert r.exit_code == 0, r.output
    case = root / "sample_case"
    assert (case / "cassette" / "manifest.json").is_file()
    assert (case / "cassette" / "redaction.json").is_file()
    expectation = load_expectation(case / "expect.yaml")
    assert expectation.divergence is None and expectation.mode == "strict"
    assert 'created_from: "att-src"' in (case / "case.yaml").read_text(encoding="utf-8")
    draft_problems = validate_case(case)
    assert any("issue is required" in p for p in draft_problems)
    assert "will not pass validate" in _plain(r.stdout)

    _fill_required(case)
    v = runner.invoke(trajectory_app, ["regression", "validate", str(case)])
    assert v.exit_code == 0, v.output


def test_init_from_attempt_id_under_state(state, tmp_path) -> None:
    _source_bundle(state / "bundles")
    root = tmp_path / "cases"

    r = _init(["att-src", "--name", "sample_case", "--root", str(root), "--yes"])

    assert r.exit_code == 0, r.output
    assert (root / "sample_case" / "cassette" / "manifest.json").is_file()


def test_init_from_trajectory_report_tarball(state, tmp_path) -> None:
    from raven.trajectory.report import pack_report

    bundle = _source_bundle(tmp_path)
    tarball = pack_report(bundle, tmp_path / "report.tar.gz")
    root = tmp_path / "cases"

    r = _init([str(tarball), "--name", "sample_case", "--root", str(root), "--yes"])

    assert r.exit_code == 0, r.output
    assert (root / "sample_case" / "cassette" / "manifest.json").is_file()


def _bug_report_package(tmp_path: Path, inner_tar: Path, name: str = "rep-1") -> Path:
    import tarfile

    pkg = tmp_path / f"pkg-{name}" / name
    (pkg / "trajectory").mkdir(parents=True)
    shutil.copy(inner_tar, pkg / "trajectory" / inner_tar.name)
    (pkg / "bugreport.json").write_text(json.dumps({"report_id": name}), encoding="utf-8")
    out = tmp_path / f"{name}.tar.gz"
    with tarfile.open(out, "w:gz") as tar:
        tar.add(pkg, arcname=name)
    return out


def test_init_from_bug_report_package_two_layer_layout(state, tmp_path) -> None:
    from raven.trajectory.report import pack_report

    bundle = _source_bundle(tmp_path)
    inner = pack_report(bundle, tmp_path / "att-src.tar.gz")
    package = _bug_report_package(tmp_path, inner)
    root = tmp_path / "cases"

    r = _init([str(package), "--name", "sample_case", "--root", str(root), "--yes"])

    assert r.exit_code == 0, r.output
    assert (root / "sample_case" / "cassette" / "manifest.json").is_file()


def _evil_tarball(path: Path, *, member_name: str | None = None, symlink: bool = False) -> Path:
    import io
    import tarfile

    with tarfile.open(path, "w:gz") as tar:
        if symlink:
            info = tarfile.TarInfo("bundle/link")
            info.type = tarfile.SYMTYPE
            info.linkname = "manifest.json"
            tar.addfile(info)
        else:
            data = b"evil"
            info = tarfile.TarInfo(member_name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


@pytest.mark.parametrize(
    ("member_name", "symlink", "expected"),
    [
        ("../evil.txt", False, "escapes the extraction root"),
        ("/abs/evil.txt", False, "has an absolute name"),
        (None, True, "is not a regular file or directory"),
    ],
    ids=["traversal", "absolute", "symlink"],
)
def test_init_rejects_unsafe_outer_tar_members(state, tmp_path, member_name, symlink, expected) -> None:
    tarball = _evil_tarball(tmp_path / "evil.tar.gz", member_name=member_name, symlink=symlink)
    root = tmp_path / "cases"

    r = _init([str(tarball), "--name", "sample_case", "--root", str(root), "--yes"])

    assert r.exit_code == 1
    assert expected in _plain(r.stdout)
    assert not (root / "sample_case").exists()
    assert not (tmp_path / "evil.txt").exists()


def test_init_rejects_unsafe_inner_tar_members(state, tmp_path) -> None:
    inner = _evil_tarball(tmp_path / "inner.tar.gz", member_name="../evil.txt")
    package = _bug_report_package(tmp_path, inner, name="rep-evil")
    root = tmp_path / "cases"

    r = _init([str(package), "--name", "sample_case", "--root", str(root), "--yes"])

    assert r.exit_code == 1
    assert "escapes the extraction root" in _plain(r.stdout)
    assert not (root / "sample_case").exists()


@pytest.mark.parametrize("bad_name", ["Bad-Name", "fix_v2", "eve151", "case_2024"])
def test_init_rejects_bad_case_names(state, tmp_path, bad_name) -> None:
    bundle = _source_bundle(tmp_path)

    r = _init([str(bundle), "--name", bad_name, "--root", str(tmp_path / "cases"), "--yes"])

    assert r.exit_code == 1
    assert "case name" in _plain(r.stdout)


def test_init_rejects_existing_case_and_leaves_it_unchanged(state, tmp_path) -> None:
    bundle = _source_bundle(tmp_path)
    root = tmp_path / "cases"
    existing = root / "sample_case"
    existing.mkdir(parents=True)
    (existing / "keep.txt").write_text("keep", encoding="utf-8")

    r = _init([str(bundle), "--name", "sample_case", "--root", str(root), "--yes"])

    assert r.exit_code == 1
    assert "already exists" in _plain(r.stdout)
    assert (existing / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (existing / "cassette").exists()


def test_init_copy_failure_leaves_no_half_written_case(state, tmp_path, monkeypatch) -> None:
    bundle = _source_bundle(tmp_path)
    root = tmp_path / "cases"
    original_copytree = shutil.copytree

    def broken_copytree(src, dst, *args, **kwargs):
        # Only the publish copy fails; minimize's internal copytree calls
        # must keep working so the failure lands on the publish path.
        if Path(str(dst)).parent == root:
            raise OSError("injected copy failure")
        return original_copytree(src, dst, *args, **kwargs)

    monkeypatch.setattr(shutil, "copytree", broken_copytree)
    r = _init([str(bundle), "--name", "sample_case", "--root", str(root), "--yes"])

    assert r.exit_code == 1
    assert not (root / "sample_case").exists()
    assert root.is_dir() and list(root.iterdir()) == []


def test_init_recheck_rejects_a_case_created_while_running(state, tmp_path, monkeypatch) -> None:
    """The destination may appear between the early check and publication;
    the pre-rename re-check must refuse and leave the other case intact."""
    bundle = _source_bundle(tmp_path)
    root = tmp_path / "cases"
    dest = root / "sample_case"
    original_copytree = shutil.copytree

    def copytree_then_race(src, dst, *args, **kwargs):
        result = original_copytree(src, dst, *args, **kwargs)
        if Path(str(dst)).parent == root:
            dest.mkdir(parents=True, exist_ok=True)
            (dest / "keep.txt").write_text("keep", encoding="utf-8")
        return result

    monkeypatch.setattr(shutil, "copytree", copytree_then_race)
    r = _init([str(bundle), "--name", "sample_case", "--root", str(root), "--yes"])

    assert r.exit_code == 1
    assert "was created while init ran" in _plain(r.stdout)
    assert (dest / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert not (dest / "cassette").exists()
    assert [p.name for p in root.iterdir()] == ["sample_case"]


def test_init_yes_rejects_residual_findings(state, tmp_path) -> None:
    bundle = _source_bundle(tmp_path, token=_HEX_TOKEN)
    root = tmp_path / "cases"

    r = _init([str(bundle), "--name", "sample_case", "--root", str(root), "--yes"])

    assert r.exit_code == 1
    assert "residual finding" in _plain(r.stdout)
    assert not (root / "sample_case").exists()


def test_init_interactive_review_records_digest_and_verbatim_reason(state, tmp_path) -> None:
    import hashlib

    from raven.trajectory.regression import load_case_metadata

    bundle = _source_bundle(tmp_path, token=_HEX_TOKEN)
    root = tmp_path / "cases"

    r = _init(
        [str(bundle), "--name", "sample_case", "--root", str(root)],
        input="y\nidentifier-shaped prose, verified by hand\n",
    )

    assert r.exit_code == 0, r.output
    assert _HEX_TOKEN in r.stdout
    case = root / "sample_case"
    _fill_required(case)
    meta = load_case_metadata(case / "case.yaml")
    assert len(meta.reviewed_residuals) == 1
    entry = meta.reviewed_residuals[0]
    assert entry.sha256 == hashlib.sha256(_HEX_TOKEN.encode("utf-8")).hexdigest()
    assert entry.note == "identifier-shaped prose, verified by hand"
    v = runner.invoke(trajectory_app, ["regression", "validate", str(case)])
    assert v.exit_code == 0, v.output


def test_init_interactive_decline_cancels(state, tmp_path) -> None:
    bundle = _source_bundle(tmp_path, token=_HEX_TOKEN)
    root = tmp_path / "cases"

    r = _init([str(bundle), "--name", "sample_case", "--root", str(root)], input="n\n")

    assert r.exit_code == 1
    assert not (root / "sample_case").exists()


def test_init_empty_reason_cancels(state, tmp_path) -> None:
    bundle = _source_bundle(tmp_path, token=_HEX_TOKEN)
    root = tmp_path / "cases"

    r = _init([str(bundle), "--name", "sample_case", "--root", str(root)], input="y\n\n")

    assert r.exit_code == 1
    assert "empty reason" in _plain(r.stdout)
    assert not (root / "sample_case").exists()


def test_init_eof_during_review_cancels(state, tmp_path) -> None:
    bundle = _source_bundle(tmp_path, token=_HEX_TOKEN)
    root = tmp_path / "cases"

    r = _init([str(bundle), "--name", "sample_case", "--root", str(root)], input="y\n")

    assert r.exit_code == 1
    assert not (root / "sample_case").exists()
