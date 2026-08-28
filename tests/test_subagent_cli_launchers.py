"""Terminal states of the CLI-shaped sub-agent launchers (`subagents/*/run.py`).

Three of the four launchers answer one question per process and report the
outcome as an exit code plus a line on stdout -- the only two channels the host
reads (`raven/agent/subagent/backends/cli_agent.py`). This file pins what those
two carry, because until now a run the launcher had killed and a run the agent
had finished without an answer were indistinguishable in both.

`raven-research` is absent on purpose: since the 2026-08-25 swap it serves ACP
over stdio (`os.execv` at the end of its `main`), so it has no terminal state of
its own to report and nothing here applies to it. Its own launcher tests are in
`tests/test_subagent_raven_launcher.py`.
"""

import importlib.util
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

# The three launchers whose main() shares one shape: run the agent under a
# subprocess timeout, then read the answer back out of the transcript.
_ONE_SHOT = ("raven-code", "raven-design", "raven-oncall")


def _load(folder: str):
    path = _REPO_ROOT / "subagents" / folder / "run.py"
    spec = importlib.util.spec_from_file_location(f"{folder.replace('-', '_')}_launcher", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Exited:
    """What `subprocess.run` hands back, reduced to what the launcher reads."""

    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


def _harness(
    folder: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    timed_out: bool,
    returncode: int = 0,
    answer: str | None,
):
    """One launcher, wired so only its terminal-state branches are exercised.

    Everything before the agent call is stubbed to something valid and dull: a
    checkout with a `raven` binary in it, a rendered config, a workspace. The
    agent call itself either returns `returncode` or raises `TimeoutExpired`,
    and the transcript either yields `answer` or is absent -- which is the whole
    input space the branches under test read.
    """
    module = _load(folder)

    checkout = tmp_path / "checkout"
    (checkout / ".venv" / "bin").mkdir(parents=True)
    (checkout / ".venv" / "bin" / "raven").write_text("", encoding="utf-8")

    workspace = tmp_path / "ws"
    workspace.mkdir()
    config = tmp_path / "rendered.json"
    config.write_text(
        json.dumps({"agents": {"defaults": {"workspace": str(workspace)}}}),
        encoding="utf-8",
    )

    if hasattr(module, "state_root"):
        monkeypatch.setattr(module, "state_root", lambda: tmp_path / "state")
        monkeypatch.setattr(module, "render_config", lambda source, state_dir: config)
    else:
        monkeypatch.setattr(module, "RUN_ROOT", tmp_path / "runs")
        monkeypatch.setattr(module, "render_config", lambda source: config)
    if hasattr(module, "resolve_workspace"):
        monkeypatch.setattr(module, "resolve_workspace", lambda state_dir, override: workspace)
    # Named but never created when the run produced nothing: "the transcript is
    # missing" is one of the two inputs these branches key on.
    transcript = tmp_path / "transcript.jsonl"
    if answer is not None:
        transcript.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(module, "session_file", lambda *a, **k: transcript)
    monkeypatch.setattr(module, "count_lines", lambda path: 0)
    monkeypatch.setattr(module, "extract_answer", lambda path, skip_lines=0: answer)
    for absent in ("pending_wakes", "campaign_state"):
        if hasattr(module, absent):
            monkeypatch.setattr(module, absent, lambda config: [])

    def fake_run(argv, **kwargs):
        if timed_out:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kwargs.get("timeout") or 1)
        return _Exited(returncode)

    monkeypatch.setattr(module.subprocess, "run", fake_run)
    return module


def _invoke(module, monkeypatch: pytest.MonkeyPatch, *extra: str) -> int:
    argv = ["run.py", "--task", "do the thing", "--checkout", str(module.Path.cwd()), "--timeout", "5"]
    monkeypatch.setattr(sys, "argv", [*argv, *extra])
    return module.main()


@pytest.fixture
def run_launcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    """Drive one launcher's `main` and hand back its exit code and stdout."""

    def go(folder: str, *extra: str, timed_out: bool, returncode: int = 0, answer: str | None):
        module = _harness(folder, tmp_path, monkeypatch, timed_out=timed_out, returncode=returncode, answer=answer)
        monkeypatch.chdir(tmp_path / "checkout")
        code = _invoke(module, monkeypatch, *extra)
        return code, capsys.readouterr().out, module

    return go


@pytest.mark.parametrize("folder", _ONE_SHOT)
def test_a_killed_run_reports_a_timeout_not_a_missing_transcript(folder, run_launcher):
    """The defect: a kill was reported as the agent having said nothing.

    `TimeoutExpired` set the exit code to None and then fell through into the
    same branch a clean exit with no answer lands in, so the reply named the
    symptom the kill had caused -- no transcript, no answer committed -- and
    never the kill. A caller reading that has no reason to retry with a longer
    deadline, which is the one thing it should do.
    """
    code, out, _ = run_launcher(folder, timed_out=True, answer=None)

    assert code == 124
    assert out.startswith("TIMEOUT:")
    assert "killed after" in out
    assert "FAILED" not in out
    assert "no answer was committed" not in out


@pytest.mark.parametrize("folder", _ONE_SHOT)
def test_a_kill_after_the_answer_landed_still_says_it_was_killed(folder, run_launcher):
    """The other half, and the one that reported a clean success.

    These launchers pass `--wait-skill-extract`, so the agent process routinely
    outlives the answer it has already committed: a deadline can land after the
    transcript holds a real reply. The answer is genuine and is still returned,
    but the run did not finish, and returning it with exit 0 and no further
    word was a timed-out run reporting as a completed one.
    """
    code, out, _ = run_launcher(folder, timed_out=True, answer="the real answer")

    assert code == 0
    assert "the real answer" in out
    assert "timed out" in out
    assert "killed after" in out


@pytest.mark.parametrize("folder", _ONE_SHOT)
def test_a_clean_exit_with_no_answer_is_still_a_plain_failure(folder, run_launcher):
    """The branch the kill used to borrow. It must keep its own wording."""
    code, out, _ = run_launcher(folder, timed_out=False, answer=None)

    assert code == 1
    assert out.startswith("FAILED:")
    assert "no answer was committed" in out
    assert "TIMEOUT" not in out


@pytest.mark.parametrize("folder", _ONE_SHOT)
def test_a_finished_run_answers_with_nothing_added(folder, run_launcher):
    """The path everything else here must not disturb."""
    code, out, _ = run_launcher(folder, timed_out=False, answer="the real answer")

    assert code == 0
    assert "the real answer" in out
    assert "timed out" not in out
    assert "TIMEOUT" not in out


@pytest.mark.parametrize("folder", _ONE_SHOT)
def test_keep_going_survives_a_kill_without_claiming_an_answer(folder, run_launcher):
    """`--keep-going` opts out of failing, not out of being told what happened.

    The reply here stands in for an answer that was never committed, so it must
    not also carry the line that qualifies a committed one -- saying a run was
    killed "after this answer was committed" when nothing was is the same class
    of false report this whole file exists to close.
    """
    code, out, _ = run_launcher(folder, "--keep-going", timed_out=True, answer=None)

    assert code == 0
    assert "killed after" in out
    assert "after this answer was committed" not in out


class _FiredTimer:
    """A `threading.Timer` that expires the moment it is started.

    The watchdog is what sets `timed_out`, and driving it by the clock would
    mean either a real wait or a race. Firing on `start()` reaches the same
    branch deterministically: the deadline landing before the read loop is
    exactly the shape being tested.
    """

    def __init__(self, interval, function, *args, **kwargs) -> None:
        self._function = function
        self.daemon = False

    def start(self) -> None:
        self._function()

    def cancel(self) -> None:
        pass


class _Streamed:
    """A `Popen` reduced to the three members the ppt launcher touches."""

    def __init__(self, lines: list[str], returncode: int) -> None:
        self.stdout = iter(lines)
        self._returncode = returncode
        self.killed = False

    def wait(self) -> int:
        return self._returncode

    def kill(self) -> None:
        self.killed = True


@pytest.fixture
def run_ppt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture):
    """Drive the ppt launcher's `main`, with only its outcome branches live."""

    def go(*, timed_out: bool, deck: bool, returncode: int = 0):
        module = _load("raven-ppt")

        checkout = tmp_path / "Raven-PPT"
        (checkout / ".venv" / "bin").mkdir(parents=True)
        binary = checkout / ".venv" / "bin" / "raven"
        binary.write_text("", encoding="utf-8")
        binary.chmod(0o755)
        monkeypatch.setattr(module, "CHECKOUT", checkout)

        root = tmp_path / "job"
        out_dir = root / "out"
        out_dir.mkdir(parents=True)
        config = tmp_path / "rendered.json"
        config.write_text("{}", encoding="utf-8")

        monkeypatch.setattr(module, "stage", lambda job, materials: (root, root, out_dir, []))
        monkeypatch.setattr(module, "render_config", lambda source: config)
        monkeypatch.setattr(module, "deck_mtimes", lambda out: {})
        published = out_dir / "deck.pptx"
        monkeypatch.setattr(module, "verified_deck", lambda out, tail, before: (published, 12) if deck else (None, 0))
        monkeypatch.setattr(module, "deliver", lambda deck_path, dest: deck_path)
        if timed_out:
            monkeypatch.setattr(module.threading, "Timer", _FiredTimer)
        monkeypatch.setattr(module.subprocess, "Popen", lambda *a, **k: _Streamed([], returncode))

        monkeypatch.setattr(
            sys,
            "argv",
            # No source material: `main` no longer takes a `--material` flag, and
            # a task that names none is a supported run rather than a refusal.
            # The outcome branches under test are the same either way.
            ["run.py", "--job", "j", "--task", "make a deck", "--no-deliver"],
        )
        code = module.main()
        return code, capsys.readouterr().out

    return go


def test_a_killed_ppt_run_reports_a_timeout_not_an_unpublished_deck(run_ppt):
    """The ppt shape of the same defect.

    Its watchdog already recorded `timed_out`, but the reply read "no verifiable
    deck was published" whether the agent had run out of things to say or been
    killed mid-render. The two want different responses and only one of them is
    "give it longer".
    """
    code, out = run_ppt(timed_out=True, deck=False)

    assert code == 124
    assert out.startswith("TIMEOUT:")
    assert "killed after" in out
    assert "FAILED" not in out


def test_a_ppt_deck_that_outlived_the_kill_is_delivered_and_declared(run_ppt):
    """A verified deck is a real artifact, so this stays a success.

    What it must not stay is silent: the run was killed on the way, and
    announcing the deck with no mention of that is the same clean-success report
    the one-shot launchers were making.
    """
    code, out = run_ppt(timed_out=True, deck=True)

    assert code == 0
    assert "Published a 12-slide deck." in out
    assert "Timed out" in out
    assert "killed after" in out


def test_a_ppt_run_that_published_nothing_on_its_own_is_still_a_plain_failure(run_ppt):
    code, out = run_ppt(timed_out=False, deck=False)

    assert code == 1
    assert out.startswith("FAILED:")
    assert "TIMEOUT" not in out


def test_a_finished_ppt_run_announces_the_deck_and_nothing_else(run_ppt):
    code, out = run_ppt(timed_out=False, deck=True)

    assert code == 0
    assert "Published a 12-slide deck." in out
    assert "Timed out" not in out
    assert "TIMEOUT" not in out


# Reserved by POSIX for the swapper/scheduler and never allocated to a user
# process, so `os.kill` on it reports gone rather than aiming at whatever real
# process a recycled number happens to name on a busy machine.
_NEVER_A_LAUNCHER = 2**22


class TestRenderedConfigSweep:
    """`raven-code`'s sweep of rendered configs a dead launcher left behind.

    That file is a copy of the config with every secret from the folder's
    `.env` merged in, at mode 600. The one-shot path removes it in a `finally`;
    the `--acp` path cannot, because the host tears an ACP server down by
    SIGKILLing its process group (`raven/agent/acp/client.py:272`) and no
    `finally` runs under SIGKILL. The sweep on the next launch is the only
    thing that ever removes it, and it was keyed on age -- a rule written for a
    stranding that used to happen only when something went wrong, and under ACP
    happens every session.
    """

    @pytest.fixture
    def sweep(self, tmp_path: Path):
        module = _load("raven-code")
        state = tmp_path / "state"
        state.mkdir()

        def write(pid: int, *, age_s: float = 0.0) -> Path:
            path = state / f".config.rendered.{pid}.json"
            path.write_text('{"providers": {"custom": {"apiKey": "secret"}}}', encoding="utf-8")
            if age_s:
                stamp = path.stat().st_mtime - age_s
                os.utime(path, (stamp, stamp))
            return path

        return module, state, write

    def test_a_config_left_by_a_dead_launcher_goes(self, sweep):
        """Fresh on disk, and still garbage: age would have kept it for a day."""
        module, state, write = sweep
        stranded = write(_NEVER_A_LAUNCHER)

        module.sweep_stale_renders(state)

        assert not stranded.exists()

    def test_a_config_held_by_a_live_launcher_stays_however_old_it_is(self, sweep):
        """Why age was the wrong test rather than merely a slow one.

        A served ACP session legitimately outlives any fixed cutoff, and taking
        its config out from under it is a worse failure than leaving a file
        behind. This is the case a liveness-plus-age rule would break, which is
        why there is no age fallback.
        """
        module, state, write = sweep
        held = write(os.getpid(), age_s=90 * 86400)

        module.sweep_stale_renders(state)

        assert held.exists()

    def test_a_name_with_no_readable_pid_goes(self, sweep):
        """Nothing can be holding a file whose name names no process."""
        module, state, write = sweep
        malformed = state / ".config.rendered.not-a-pid.json"
        malformed.write_text("{}", encoding="utf-8")

        module.sweep_stale_renders(state)

        assert not malformed.exists()

    def test_everything_else_in_the_directory_is_untouched(self, sweep):
        """The glob is all that separates these from the launcher's own logs."""
        module, state, write = sweep
        log = state / "launcher.log"
        log.write_text("log", encoding="utf-8")
        mine = write(os.getpid())

        module.sweep_stale_renders(state)

        assert log.exists()
        assert mine.exists()
