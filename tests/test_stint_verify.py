"""Runtime checks: what the runtime can learn without a model, and what it cannot."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.stint.bootstrap import detect_checks
from raven.stint.checks import checks_path, remember_check, resolve_checks
from raven.stint.verify import (
    CheckResult,
    CheckSpec,
    CheckSummary,
    Display,
    build_status,
    curated_env,
    parse_check_spec,
    render_checks,
    resolve_display,
    run_check,
    run_checks,
    save_results,
    start_display,
)


def test_a_check_is_a_name_and_a_command() -> None:
    spec = parse_check_spec("import=godot --headless --import")
    assert spec.name == "import"
    assert spec.command == "godot --headless --import"
    assert spec.needs_display is False


def test_a_check_that_needs_a_display_says_so_in_its_name() -> None:
    spec = parse_check_spec("clean_boot!display=godot -- --clean-boot-test")
    assert spec.name == "clean_boot"
    assert spec.needs_display is True


@pytest.mark.parametrize("value", ["no-equals-sign", "=command", "name="])
def test_a_malformed_check_is_an_error(value: str) -> None:
    with pytest.raises(ValueError):
        parse_check_spec(value)


def test_a_godot_tree_affords_an_import_and_a_boot(tmp_path) -> None:
    (tmp_path / "project").mkdir()
    (tmp_path / "project" / "project.godot").write_text("", encoding="utf-8")
    names = [spec.name for spec in detect_checks(tmp_path)]
    assert names[:2] == ["import", "clean_boot"]
    assert next(spec for spec in detect_checks(tmp_path) if spec.name == "clean_boot").needs_display


def test_the_projects_own_scripts_are_the_checks_that_settle_a_feel_gate(tmp_path) -> None:
    (tmp_path / "tools").mkdir()
    (tmp_path / "tools" / "assert_feel.py").write_text("", encoding="utf-8")
    (tmp_path / "tools" / "image_thresholds.py").write_text("", encoding="utf-8")
    names = [spec.name for spec in detect_checks(tmp_path)]
    assert "feel_assert" in names
    assert "image_thresholds" in names


def test_a_tree_that_affords_nothing_yields_no_checks(tmp_path) -> None:
    assert detect_checks(tmp_path) == []


def test_a_named_display_is_taken_as_given() -> None:
    assert resolve_display(":99").name == ":99"
    assert resolve_display(":99").available


def test_a_check_needing_a_display_is_skipped_rather_than_failed(tmp_path) -> None:
    result = run_check(
        CheckSpec(name="boot", command="true", needs_display=True),
        cwd=tmp_path,
        log_dir=tmp_path / "logs",
        display=Display(provider="none"),
    )
    assert result.status == "skipped"
    assert "no display" in result.skipped_reason
    assert result.ok is False


def test_a_check_that_runs_records_its_status_and_its_output(tmp_path) -> None:
    ok = run_check(CheckSpec(name="ok", command="echo hello"), cwd=tmp_path, log_dir=tmp_path / "logs")
    bad = run_check(CheckSpec(name="bad", command="exit 3"), cwd=tmp_path, log_dir=tmp_path / "logs")
    assert ok.status == "ok" and "hello" in ok.stdout_tail
    assert bad.status == "failed" and bad.returncode == 3
    assert build_status([ok, bad]) == "failing"
    assert build_status([ok]) == "passing"
    assert build_status([]) == "none"


def test_a_check_that_never_returns_is_a_timeout(tmp_path) -> None:
    result = run_check(
        CheckSpec(name="hang", command="sleep 30", timeout_sec=0.5), cwd=tmp_path, log_dir=tmp_path / "logs"
    )
    assert result.status == "timeout"
    assert result.timed_out


def test_the_seed_reaches_the_command_as_an_environment_variable(tmp_path) -> None:
    result = run_check(
        CheckSpec(name="seed", command="echo $STINT_SEED"), cwd=tmp_path, log_dir=tmp_path / "logs", seed="42"
    )
    assert result.stdout_tail.strip() == "42"


def test_a_check_sees_the_environment_it_was_given_and_no_other(tmp_path, monkeypatch) -> None:
    """The curated environment replaces the host's rather than being added to it.

    It is a subset of `os.environ`, so overlaying it onto a full copy removed
    nothing: the check kept every key the curation exists to drop, and a check's
    output tail travels into the next role's prompt and into the stint record.
    """
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-not-for-a-check")
    result = run_check(
        CheckSpec(name="env", command="echo [$ANTHROPIC_API_KEY]"),
        cwd=tmp_path,
        log_dir=tmp_path / "logs",
        env=curated_env(),
    )
    assert result.stdout_tail.strip() == "[]", result.stdout_tail
    assert "sk-not-for-a-check" not in result.stdout_tail


def test_a_check_given_no_environment_still_runs_under_the_hosts(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("RAVEN_CHECK_MARKER", "present")
    result = run_check(
        CheckSpec(name="env", command="echo $RAVEN_CHECK_MARKER"), cwd=tmp_path, log_dir=tmp_path / "logs"
    )
    assert result.stdout_tail.strip() == "present"


def test_results_are_saved_with_the_display_they_ran_under(tmp_path) -> None:
    results = run_checks([CheckSpec(name="ok", command="true")], cwd=tmp_path, log_dir=tmp_path / "logs")
    path = tmp_path / "checks.json"
    save_results(results, path, display=Display(name=":99", provider="xvfb"))
    saved = json.loads(path.read_text())
    assert saved["status"] == "passing"
    assert saved["display"]["name"] == ":99"
    assert "| check | status | detail |" in render_checks(results)


def test_a_timed_out_check_takes_its_grandchildren_with_it(tmp_path) -> None:
    """A check is a shell line, so the work is the shell's *child* and the shell
    is what `subprocess.run` kills on timeout. What was left running went on
    spending the machine and went on writing into the log file whose tail is
    reported as the result -- so the tail of a timed-out check could change
    after the check was over.
    """
    import os
    import time

    marker = tmp_path / "still-here"
    # The sleeper writes only after the timeout has passed, so a survivor is the
    # only thing that could create the file.
    child = f"sleep 1.5; echo alive > {marker}"
    result = run_check(
        CheckSpec(name="slow", command=f"sh -c '{child}' & wait", timeout_sec=0.4),
        cwd=tmp_path,
        log_dir=tmp_path / "logs",
    )

    assert result.status == "timeout"
    time.sleep(2.0)
    assert not marker.exists(), "the shell was killed and its child was not"
    assert os.path.isfile(result.log_path), "the log is still where the result says"


def test_a_checks_summary_survives_the_trip_through_a_run_record() -> None:
    summary = CheckSummary(name="import", status="failed", detail="exit 1 in 12s")

    assert CheckSummary.from_dict(summary.to_dict()) == summary
    assert CheckSummary.from_dict({}) == CheckSummary(name="", status="", detail="")


def test_a_check_spec_is_recorded_with_everything_needed_to_run_it_again() -> None:
    spec = parse_check_spec("clean_boot!display=godot -- --clean-boot-test", timeout_sec=30.0)

    assert spec.to_dict() == {
        "name": "clean_boot",
        "command": "godot -- --clean-boot-test",
        "timeout_sec": 30.0,
        "needs_display": True,
        "seedable": False,
    }


def test_what_a_check_is_reported_as_says_why_there_is_no_exit_code() -> None:
    """A skip and a timeout both have no exit code, and a role told only "failed"
    for either one goes looking for a bug that is not there."""
    skipped = CheckResult(
        name="boot", command="godot", returncode=None, duration_sec=0.0, skipped_reason="no display on this machine"
    )
    timed_out = CheckResult(name="suite", command="pytest", returncode=None, duration_sec=1800.0, timed_out=True)
    failed = CheckResult(name="import", command="godot --import", returncode=1, duration_sec=12.0)

    assert skipped.summary() == CheckSummary(name="boot", status="skipped", detail="no display on this machine")
    assert timed_out.summary().detail == "no result after 1800s"
    assert failed.summary().detail == "exit 1 in 12s"


def test_a_stored_status_is_worked_out_again_rather_than_believed() -> None:
    """The status is derived from the numbers beside it, and a stored one that
    disagreed with them would be the one that got read."""
    stored = CheckResult(name="import", command="godot --import", returncode=1, duration_sec=3.0).to_dict()
    stored["status"] = "ok"

    again = CheckResult.from_dict(stored)

    assert again.status == "failed"
    assert again.returncode == 1
    assert again.command == "godot --import"


def test_a_display_travels_to_a_check_as_the_variable_an_engine_reads() -> None:
    assert Display(name=":99", provider="xvfb").env() == {"DISPLAY": ":99"}
    assert Display(provider="none").env() == {}
    assert Display(provider="none").to_dict() == {
        "name": "",
        "provider": "none",
        "started": False,
        "available": False,
    }


def test_a_display_the_environment_already_has_is_the_one_a_check_draws_on(monkeypatch) -> None:
    monkeypatch.setenv("DISPLAY", ":0")

    resolved = resolve_display(None)

    assert (resolved.name, resolved.provider) == (":0", "inherited")
    assert resolve_display(":99").provider == "given", "an asked-for display beats the inherited one"


def test_a_machine_that_draws_natively_is_never_asked_for_an_xvfb(monkeypatch) -> None:
    """macOS has no Xvfb and no headless GPU path, so a stint there records that
    it had no display rather than pretending a frame was rendered."""
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("raven.stint.verify.platform.system", lambda: "Darwin")

    assert resolve_display(None, start_xvfb=True) == Display(provider="native")


def test_an_xvfb_is_only_offered_where_there_is_one_to_start(monkeypatch) -> None:
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.setattr("raven.stint.verify.platform.system", lambda: "Linux")
    monkeypatch.setattr("raven.stint.verify.shutil.which", lambda name: "/usr/bin/Xvfb")

    assert resolve_display(None, start_xvfb=True).provider == "xvfb"
    assert resolve_display(None).provider == "none", "nobody asked for one"

    monkeypatch.setattr("raven.stint.verify.shutil.which", lambda name: None)
    assert resolve_display(None, start_xvfb=True).provider == "none"


def test_a_display_that_is_already_there_starts_nothing() -> None:
    for display in (
        Display(name=":0", provider="given"),
        Display(provider="none"),
        Display(name=":99", provider="xvfb", started=True),
    ):
        assert start_display(display) == (display, None)


def test_an_xvfb_this_runtime_starts_comes_back_with_the_display_it_serves(monkeypatch) -> None:
    """The caller is the only one that can stop it, so the process has to come
    back rather than be left to outlive the run."""
    spawned: list[tuple[list[str], bool]] = []

    class _Process:
        pid = 4242

    def _popen(args, **kwargs):
        spawned.append((args, kwargs["start_new_session"]))
        return _Process()

    monkeypatch.setattr("raven.stint.verify.subprocess.Popen", _popen)
    monkeypatch.setattr("raven.stint.verify.time.sleep", lambda seconds: None)

    display, process = start_display(Display(provider="xvfb"), screen="800x600x24", number=77)

    assert display == Display(name=":77", provider="xvfb", started=True)
    assert isinstance(process, _Process)
    assert spawned == [(["Xvfb", ":77", "-screen", "0", "800x600x24"], True)]


def test_the_display_a_check_draws_on_reaches_the_command(tmp_path) -> None:
    result = run_check(
        CheckSpec(name="where", command="echo $DISPLAY"),
        cwd=tmp_path,
        log_dir=tmp_path / "logs",
        display=Display(name=":99", provider="xvfb"),
    )

    assert result.stdout_tail.strip() == ":99"


def test_a_check_that_removed_its_own_log_reports_no_output_rather_than_raising(tmp_path) -> None:
    """The result is what the round records. Losing the round because a check
    tidied up after itself would be the worse of the two outcomes."""
    log_dir = tmp_path / "logs"

    result = run_check(
        CheckSpec(name="tidy", command=f"echo hello; rm -f {log_dir}/tidy.out {log_dir}/tidy.err"),
        cwd=tmp_path,
        log_dir=log_dir,
    )

    assert result.status == "ok"
    assert result.stdout_tail == ""
    assert result.stderr_tail == ""


def test_a_check_still_times_out_where_there_are_no_process_groups(tmp_path, monkeypatch) -> None:
    """Not every platform hands out a group to end, and the timeout is still a
    timeout there -- the group is how much can be cleaned up, not whether."""

    def _no_groups(pid: int) -> int:
        raise OSError("no process groups on this platform")

    monkeypatch.setattr("raven.stint.verify.os.getpgid", _no_groups)

    result = run_check(
        CheckSpec(name="hang", command="sleep 2", timeout_sec=0.3), cwd=tmp_path, log_dir=tmp_path / "logs"
    )

    assert result.status == "timeout"
    assert result.returncode is None


def test_a_tree_with_no_checks_says_so_rather_than_showing_an_empty_table() -> None:
    assert render_checks([]) == "(no runtime checks were configured for this tree)"


def test_a_pass_where_every_check_was_skipped_is_not_a_pass() -> None:
    """Reading "not measured" as "passing" is how a gate nobody measured gets
    signed off."""
    skipped = CheckResult(name="boot", command="godot", returncode=None, duration_sec=0.0, skipped_reason="no display")
    ok = CheckResult(name="import", command="godot --import", returncode=0, duration_sec=1.0)

    assert build_status([skipped]) == "skipped"
    assert build_status([skipped, ok]) == "passing"


class TestWhatThisProjectRuns:
    """A check declared by description, answered once by the project.

    What travels in a playbook is the requirement -- "the source compiles" --
    and not the command, because a file carrying literal shell is a file that
    runs something on whoever opens it, and `uv run pytest` is nothing on a
    project that uses npm. The answer is the project's, written down where a
    person can read and change it.
    """

    @staticmethod
    def _entry(name: str, **over):
        from raven.playbook.stint_spec import VerifyEntry

        return VerifyEntry(name=name, **over)

    def test_a_playbook_that_names_its_command_asks_nobody(self, tmp_path: Path) -> None:
        specs, missing = resolve_checks(tmp_path, "verifier-loop", [self._entry("build", run="make")])

        assert [(s.name, s.command) for s in specs] == [("build", "make")]
        assert missing == []
        assert not checks_path(tmp_path).exists(), "a file that answers itself writes nothing down"

    def test_a_description_with_no_answer_yet_comes_back_as_a_name(self, tmp_path: Path) -> None:
        """Returned rather than skipped: a declared check that silently does not
        run is a gate the round reports and nobody measured."""
        specs, missing = resolve_checks(
            tmp_path, "verifier-loop", [self._entry("tests", description="the suite passes")]
        )

        assert specs == []
        assert missing == ["tests"]

    def test_the_answer_is_written_down_and_read_back(self, tmp_path: Path) -> None:
        entry = self._entry("tests", description="the suite passes")
        remember_check(tmp_path, "verifier-loop", "tests", "uv run pytest -q")

        specs, missing = resolve_checks(tmp_path, "verifier-loop", [entry])

        assert [(s.name, s.command) for s in specs] == [("tests", "uv run pytest -q")]
        assert missing == []

    def test_another_playbook_s_gate_of_the_same_name_is_a_different_question(self, tmp_path: Path) -> None:
        """Keyed by both: two playbooks may each want a `build` and mean
        different things, and the second must not inherit the first's answer."""
        entry = self._entry("build", description="the source compiles")
        remember_check(tmp_path, "verifier-loop", "build", "make")

        assert resolve_checks(tmp_path, "verifier-loop", [entry])[1] == []
        assert resolve_checks(tmp_path, "other-loop", [entry])[1] == ["build"]

    def test_a_person_s_answer_outlives_the_run_that_asked(self, tmp_path: Path) -> None:
        """Resolved once for the project, not worked out per run: a command
        re-derived every time can change between two rounds of one stint with
        nobody having decided that it should."""
        entry = self._entry("tests", description="the suite passes")
        remember_check(tmp_path, "verifier-loop", "tests", "uv run pytest -q")

        first = resolve_checks(tmp_path, "verifier-loop", [entry])[0][0].command
        second = resolve_checks(tmp_path, "verifier-loop", [entry])[0][0].command

        assert first == second == "uv run pytest -q"
