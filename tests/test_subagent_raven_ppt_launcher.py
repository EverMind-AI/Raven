"""Unit tests for the host-side raven-ppt launcher (subagents/raven-ppt/run.py).

The launcher turns a task and its named documents into a deck run: secrets
merged into their slots, files staged under the state root, and a prompt tail
that tells the agent what it holds. The exec itself is not unit-testable from
here, so what is tested is the boundary around it: a run that names no
material starts instead of being refused, the prompt says which case it is in,
and a declared file that cannot be copied still stops the run.
"""

import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LAUNCHER = _REPO_ROOT / "subagents" / "raven-ppt" / "run.py"


@pytest.fixture
def mod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The launcher module, repointed at tmp_path so no real checkout venv or
    state root can leak into a test. `CHECKOUT` is patched too: it is bound
    from `HERE` at import, so patching `HERE` alone would leave `main()`
    probing the real fork's venv. Nothing reaches `HOST_CONFIG` or
    `DEFAULT_CONFIG`: the main-path tests stub `render_config`, and the
    declared-file test exits in `stage` before it is called."""
    spec = importlib.util.spec_from_file_location("raven_ppt_launcher", _LAUNCHER)
    assert spec and spec.loader
    launcher = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(launcher)
    monkeypatch.setattr(launcher, "HERE", tmp_path)
    monkeypatch.setattr(launcher, "CHECKOUT", tmp_path / "Raven-PPT")
    monkeypatch.setattr(launcher, "STATE_ROOT", tmp_path / "state")
    return launcher


def _executable(tmp_path: Path) -> None:
    """The checkout's venv raven, which the launcher tests for executability."""
    raven = tmp_path / "Raven-PPT" / ".venv" / "bin" / "raven"
    raven.parent.mkdir(parents=True)
    raven.write_text("#!/bin/sh\n", encoding="utf-8")
    raven.chmod(0o755)


class _Done:
    """A child that has already finished: no output, exit code 0."""

    stdout: list[str] = []

    def wait(self) -> int:
        return 0


def test_a_run_naming_no_material_starts_instead_of_being_refused(
    mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _executable(tmp_path)
    monkeypatch.setattr(
        sys,
        "argv",
        ["run.py", "--job", "job-1", "--task", "Build a 10-page deck about whales."],
    )
    monkeypatch.setattr(mod, "render_config", lambda source: source)
    launches: list[list[str]] = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda argv, **_: launches.append(argv) or _Done())

    code = mod.main()

    assert code == 1  # the stub published no deck
    # The run must launch the tmp checkout's raven, not the real fork's: the
    # real one only exists on boxes that ran uv sync inside the vendored tree.
    assert launches and launches[0][0] == str(tmp_path / "Raven-PPT" / ".venv" / "bin" / "raven")


def test_a_declared_file_that_cannot_be_copied_still_stops_the_run(
    mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _executable(tmp_path)
    task = '```raven-ppt\n{"materials": ["/nonexistent/notes.md"]}\n```\nBuild it.'
    monkeypatch.setattr(sys, "argv", ["run.py", "--job", "job-2", "--task", task])

    with pytest.raises(SystemExit, match="cannot stage"):
        mod.main()


def test_the_material_flag_is_gone(mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["run.py", "--job", "job-3", "--task", "no material", "--material", "/abs/a.md"],
    )

    with pytest.raises(SystemExit) as exc:
        mod.main()

    assert exc.value.code == 2  # argparse rejects the unknown flag


def _task_arg(launches: list[list[str]]) -> str:
    argv = launches[0]
    return argv[argv.index("-m") + 1]


def test_the_task_passes_through_without_path_checking(mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _executable(tmp_path)
    task = (
        "Build a 10-page deck from /nonexistent/Q3.pdf and https://example.com/r.pdf. "
        "Save it as /Users/me/Desktop/q3-review.pptx"
    )
    monkeypatch.setattr(sys, "argv", ["run.py", "--job", "job-4", "--task", task])
    monkeypatch.setattr(mod, "render_config", lambda source: source)
    launches: list[list[str]] = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda argv, **_: launches.append(argv) or _Done())

    code = mod.main()

    assert code == 1  # the stub published no deck; the run itself started
    delivered = _task_arg(launches)
    assert delivered.startswith(task)  # the task arrives exactly as written
    assert "does not exist on this host" not in delivered
    assert "No material staged for this run" in delivered


def test_an_unrelated_json_block_with_a_template_key_runs(mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _executable(tmp_path)
    task = 'Match our house deck settings:\n```json\n{"template": "corporate", "theme": "dark"}\n```\nBuild 8 slides on hiring.'
    monkeypatch.setattr(sys, "argv", ["run.py", "--job", "job-9", "--task", task])
    monkeypatch.setattr(mod, "render_config", lambda source: source)
    launches: list[list[str]] = []
    monkeypatch.setattr(mod.subprocess, "Popen", lambda argv, **_: launches.append(argv) or _Done())

    code = mod.main()

    assert code == 1  # the block is not a path declaration; the run started


def test_the_template_flag_is_gone(mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["run.py", "--job", "job-5", "--task", "no template", "--template", "/abs/house.pptx"],
    )

    with pytest.raises(SystemExit) as exc:
        mod.main()

    assert exc.value.code == 2  # argparse rejects the unknown flag


def test_a_declared_template_is_refused(mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _executable(tmp_path)
    task = '```raven-ppt\n{"template": "/abs/house.pptx"}\n```\nBuild it.'
    monkeypatch.setattr(sys, "argv", ["run.py", "--job", "job-6", "--task", task])

    with pytest.raises(SystemExit, match="template"):
        mod.main()


def test_the_prompt_says_when_no_material_was_staged(mod, tmp_path: Path) -> None:
    mats = tmp_path / "materials"

    section = mod.material_section([], mats)

    assert "No material staged for this run" in section
    assert str(mats) not in section


def test_the_prompt_lists_every_staged_file_and_its_grounding_rule(mod, tmp_path: Path) -> None:
    mats = tmp_path / "materials"
    target = mats / "report.pdf"

    section = mod.material_section([("/abs/report.pdf", target)], mats)

    assert "/abs/report.pdf" in section
    assert "report.pdf" in section
    assert f"Use only files under {mats}" in section


def _config_source(tmp_path: Path) -> Path:
    """A minimal shipped `config.json`, the one input `render_config` reads."""
    source = tmp_path / "config.json"
    source.write_text(
        json.dumps({"providers": {"custom": {"apiBase": "https://x.invalid/v1"}}}),
        encoding="utf-8",
    )
    return source


class TestRenderedConfigLifetime:
    """The rendered config is one file per launcher, swept on liveness.

    It holds every secret this folder merged in, at mode 600, and the child
    derives its whole data directory from the path, so both halves of that --
    who may write it and when it may be removed -- are load-bearing. The
    manager lets one sub-agent be spawned twice concurrently, which is what
    rules out the fixed name this used to have.
    """

    def test_the_rendered_file_is_private_and_named_after_this_pid(
        self, mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("PPT_API_KEY", "k-llm")

        rendered = mod.render_config(_config_source(tmp_path))

        assert rendered.parent == mod.STATE_ROOT
        assert rendered.name == f".config.rendered.{os.getpid()}.json"
        assert (rendered.stat().st_mode & 0o777) == 0o600

    def test_a_second_concurrent_run_does_not_empty_the_first_ones_config(
        self, mod, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The regression this file name exists for.

        `os.getppid()` stands in for the other launcher because the pid has to
        be one the sweep finds alive; a made-up number would be swept as dead
        and the test would pass for the wrong reason.
        """
        monkeypatch.setenv("PPT_API_KEY", "k-llm")
        source = _config_source(tmp_path)
        first = mod.render_config(source)

        monkeypatch.setattr(mod.os, "getpid", os.getppid)
        second = mod.render_config(source)

        assert first != second
        assert first.exists(), "the concurrent run removed the file the first run is using"
        held = json.loads(first.read_text(encoding="utf-8"))
        assert held["providers"]["custom"]["apiKey"] == "k-llm"

    def test_sweep_removes_only_files_whose_pid_is_gone(self, mod) -> None:
        state = mod.STATE_ROOT
        state.mkdir(parents=True)
        dead = state / f".config.rendered.{2**22 + 12345}.json"
        alive = state / f".config.rendered.{os.getpid()}.json"
        junk = state / ".config.rendered.notapid.json"
        for path in (dead, alive, junk):
            path.write_text("{}", encoding="utf-8")

        mod.sweep_stale_renders()

        assert not dead.exists()
        assert not junk.exists()
        assert alive.exists()

    def test_the_pre_pid_fixed_name_config_is_left_alone(self, mod) -> None:
        """Not removed, though it holds keys nothing writes any more.

        One fixed name shared by every past run carries no liveness signal, and
        a server started before the rename pinned that exact path. Deleting it
        would drop that server back to the shipped defaults on its next session
        with nothing said -- the failure the pid rule exists to avoid, so the
        same rule forbids guessing here.
        """
        state = mod.STATE_ROOT
        state.mkdir(parents=True)
        legacy = state / ".config.rendered.json"
        legacy.write_text('{"providers": {"custom": {"apiKey": "k-llm"}}}', encoding="utf-8")

        mod.sweep_stale_renders()

        assert legacy.exists(), "a live pre-upgrade server may still be reading this path"

    def test_nothing_else_in_the_state_root_is_touched(self, mod) -> None:
        state = mod.STATE_ROOT
        state.mkdir(parents=True)
        keep = state / "config.json"
        keep.write_text("{}", encoding="utf-8")

        mod.sweep_stale_renders()

        assert keep.exists()
