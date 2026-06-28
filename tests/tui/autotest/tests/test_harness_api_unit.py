"""Unit tests for Harness API — mocks tui-use CLI subprocess calls.

Tier 1 (tui-use) selected per Day 0 spike. Harness public API contract
in specs/tui-autotest.md §S3. This file pins the mapping between
Harness methods and tui-use CLI invocations.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest

from tests.tui.autotest.runner import (
    Harness,
    HarnessError,
    SpawnError,
)


def _completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


@pytest.fixture
def mock_run():
    with patch("tests.tui.autotest.runner.subprocess.run") as m:
        yield m


class TestHarnessConstruction:
    def test_defaults(self):
        h = Harness()
        assert h.cols == 120
        assert h.rows == 40

    def test_custom_dims(self):
        h = Harness(cols=80, rows=24)
        assert h.cols == 80
        assert h.rows == 24

    def test_initial_env_overrides_empty(self):
        h = Harness()
        assert h._env_overrides == {}


class TestSpawn:
    def test_spawn_invokes_tui_use_start_with_dims(self, mock_run):
        mock_run.side_effect = [
            _completed(stdout="happy-lemur\n"),
            _completed(),
        ]
        h = Harness(cols=100, rows=30)
        h.spawn("uv run raven tui")

        start_call = mock_run.call_args_list[0]
        cmd = start_call.args[0]
        assert cmd[0] == "tui-use"
        assert cmd[1] == "start"
        assert "--cols" in cmd and cmd[cmd.index("--cols") + 1] == "100"
        assert "--rows" in cmd and cmd[cmd.index("--rows") + 1] == "30"
        assert cmd[-3:] == ["uv", "run", "raven"] or "raven" in cmd[-1] or "raven" in " ".join(cmd[-4:])

    def test_spawn_captures_session_id(self, mock_run):
        mock_run.side_effect = [
            _completed(stdout="quirky-fox\n"),
            _completed(),
        ]
        h = Harness()
        h.spawn("/bin/cat")
        assert h._session_id == "quirky-fox"

    def test_spawn_calls_use_after_start(self, mock_run):
        mock_run.side_effect = [
            _completed(stdout="sid\n"),
            _completed(),
        ]
        h = Harness()
        h.spawn("/bin/cat")
        # Second call: tui-use use sid
        use_call = mock_run.call_args_list[1]
        cmd = use_call.args[0]
        assert cmd[:3] == ["tui-use", "use", "sid"]

    def test_spawn_raises_spawn_error_on_failure(self, mock_run):
        mock_run.side_effect = [
            _completed(returncode=1, stderr="tui-use: cannot start"),
        ]
        h = Harness()
        with pytest.raises(SpawnError):
            h.spawn("nonexistent-binary")

    def test_spawn_twice_raises(self, mock_run):
        mock_run.side_effect = [
            _completed(stdout="sid1\n"),
            _completed(),
        ]
        h = Harness()
        h.spawn("/bin/cat")
        with pytest.raises(HarnessError):
            h.spawn("/bin/cat")

    def test_spawn_passes_env_overrides(self, mock_run):
        mock_run.side_effect = [
            _completed(stdout="sid\n"),
            _completed(),
        ]
        h = Harness()
        h.env_set({"FORCE_COLOR": "1", "FOO": "bar"})
        h.spawn("uv run raven tui")
        start_call = mock_run.call_args_list[0]
        # env passed as kwarg to subprocess.run
        env = start_call.kwargs.get("env")
        assert env is not None
        assert env["FORCE_COLOR"] == "1"
        assert env["FOO"] == "bar"

    def test_spawn_sets_default_force_color(self, mock_run):
        mock_run.side_effect = [_completed(stdout="sid\n"), _completed()]
        h = Harness()
        h.spawn("uv run raven tui")
        env = mock_run.call_args_list[0].kwargs.get("env")
        # FORCE_COLOR default = "1" per specs/tui-autotest.md §S5.1
        assert env["FORCE_COLOR"] == "1"
        assert env["TERM"] == "xterm-256color"


class TestType:
    def test_type_invokes_tui_use_type(self, mock_run):
        mock_run.side_effect = [_completed(stdout="sid\n"), _completed(), _completed()]
        h = Harness()
        h.spawn("/bin/cat")
        h.type("hello world")
        last_cmd = mock_run.call_args_list[-1].args[0]
        assert last_cmd[:3] == ["tui-use", "type", "hello world"]

    def test_type_before_spawn_raises(self):
        h = Harness()
        with pytest.raises(HarnessError):
            h.type("hello")


class TestPress:
    @pytest.mark.parametrize("key", ["enter", "ctrl+c", "ctrl+d", "tab", "escape", "arrow_up"])
    def test_press_invokes_tui_use_press(self, mock_run, key):
        mock_run.side_effect = [_completed(stdout="sid\n"), _completed(), _completed()]
        h = Harness()
        h.spawn("/bin/cat")
        h.press(key)
        last_cmd = mock_run.call_args_list[-1].args[0]
        assert last_cmd[:3] == ["tui-use", "press", key]

    def test_press_before_spawn_raises(self):
        h = Harness()
        with pytest.raises(HarnessError):
            h.press("enter")


class TestWait:
    def test_wait_returns_true_when_pattern_found(self, mock_run):
        spawn_calls = [_completed(stdout="sid\n"), _completed()]
        snapshot_calls = [
            _completed(stdout="─── header ───\n hello world \n─── footer ───\n"),
        ]
        mock_run.side_effect = spawn_calls + snapshot_calls
        h = Harness()
        h.spawn("/bin/cat")
        assert h.wait("hello", timeout=1.0) is True

    def test_wait_returns_true_for_regex_pattern(self, mock_run):
        spawn_calls = [_completed(stdout="sid\n"), _completed()]
        snapshot_calls = [_completed(stdout="--- agent ID: 42 ---\n")]
        mock_run.side_effect = spawn_calls + snapshot_calls
        h = Harness()
        h.spawn("/bin/cat")
        import re
        assert h.wait(re.compile(r"agent\s+ID"), timeout=1.0) is True

    def test_wait_returns_false_on_timeout(self, mock_run):
        spawn_calls = [_completed(stdout="sid\n"), _completed()]
        # Many polls all without the target — wait should timeout fast
        no_match = _completed(stdout="empty screen\n")
        mock_run.side_effect = spawn_calls + [no_match] * 100
        h = Harness()
        h.spawn("/bin/cat")
        assert h.wait("nopattern_target", timeout=0.2) is False

    def test_wait_before_spawn_raises(self):
        h = Harness()
        with pytest.raises(HarnessError):
            h.wait("anything", timeout=1.0)


class TestDump:
    def test_dump_returns_screen_rows_stripped(self, mock_run):
        spawn_calls = [_completed(stdout="sid\n"), _completed()]
        snapshot = (
            "─── sid ───\n"
            "row one\n"
            "row two trailing   \n"
            "\n"
            "─── running | cursor(0,0) ───\n"
        )
        mock_run.side_effect = spawn_calls + [_completed(stdout=snapshot)]
        h = Harness()
        h.spawn("/bin/cat")
        rows = h.dump()
        assert "row one" in rows
        assert "row two trailing" in rows  # trailing whitespace stripped
        # Banner/separator rows (those bracketed with ─── markers) excluded
        for row in rows:
            assert "───" not in row

    def test_screen_joins_with_newline(self, mock_run):
        spawn_calls = [_completed(stdout="sid\n"), _completed()]
        mock_run.side_effect = spawn_calls + [_completed(stdout="─── sid ───\nA\nB\n─── status ───\n")]
        h = Harness()
        h.spawn("/bin/cat")
        s = h.screen()
        assert "A" in s and "B" in s
        assert s == "A\nB" or s.startswith("A\nB") or "A\nB" in s


class TestExpectExit:
    def test_expect_exit_zero_match(self, mock_run):
        spawn_calls = [_completed(stdout="sid\n"), _completed()]
        info = _completed(
            stdout=(
                "Session ID: sid\n"
                "Label: t\n"
                "Command: /bin/cat\n"
                "Status: exited\n"
                "Exit Code: 0\n"
            )
        )
        mock_run.side_effect = spawn_calls + [info]
        h = Harness()
        h.spawn("/bin/cat")
        assert h.expect_exit(0, timeout=1.0) is True

    def test_expect_exit_mismatch_returns_false(self, mock_run):
        spawn_calls = [_completed(stdout="sid\n"), _completed()]
        info = _completed(stdout="Status: exited\nExit Code: 1\n")
        mock_run.side_effect = spawn_calls + [info]
        h = Harness()
        h.spawn("/bin/cat")
        assert h.expect_exit(0, timeout=1.0) is False

    def test_expect_exit_polls_until_exited(self, mock_run):
        spawn_calls = [_completed(stdout="sid\n"), _completed()]
        info_running = _completed(stdout="Status: running\n")
        info_exited = _completed(stdout="Status: exited\nExit Code: 0\n")
        mock_run.side_effect = spawn_calls + [info_running, info_running, info_exited]
        h = Harness()
        h.spawn("/bin/cat")
        assert h.expect_exit(0, timeout=2.0) is True

    def test_expect_exit_timeout_returns_false(self, mock_run):
        spawn_calls = [_completed(stdout="sid\n"), _completed()]
        info_running = _completed(stdout="Status: running\n")
        mock_run.side_effect = spawn_calls + [info_running] * 100
        h = Harness()
        h.spawn("/bin/cat")
        assert h.expect_exit(0, timeout=0.2) is False


class TestEnvSet:
    def test_env_set_before_spawn_stored(self):
        h = Harness()
        h.env_set({"FOO": "bar"})
        assert h._env_overrides["FOO"] == "bar"

    def test_env_set_merges(self):
        h = Harness()
        h.env_set({"A": "1"})
        h.env_set({"B": "2"})
        assert h._env_overrides == {"A": "1", "B": "2"}

    def test_env_set_after_spawn_raises(self, mock_run):
        mock_run.side_effect = [_completed(stdout="sid\n"), _completed()]
        h = Harness()
        h.spawn("/bin/cat")
        with pytest.raises(HarnessError):
            h.env_set({"FOO": "bar"})


class TestKill:
    def test_kill_invokes_tui_use_kill(self, mock_run):
        mock_run.side_effect = [
            _completed(stdout="sid\n"),
            _completed(),
            _completed(),
            _completed(stdout="Status: exited\nExit Code: 0\n"),
        ]
        h = Harness()
        h.spawn("/bin/cat")
        exit_code = h.kill()
        # tui-use kill called
        kill_calls = [c for c in mock_run.call_args_list if c.args[0][:2] == ["tui-use", "kill"]]
        assert len(kill_calls) >= 1
        assert exit_code == 0

    def test_kill_idempotent_when_already_exited(self, mock_run):
        mock_run.side_effect = [
            _completed(stdout="sid\n"),
            _completed(),
            _completed(),
            _completed(stdout="Status: exited\nExit Code: 0\n"),
            _completed(),
            _completed(stdout="Status: exited\nExit Code: 0\n"),
        ]
        h = Harness()
        h.spawn("/bin/cat")
        h.kill()
        h.kill()  # no raise

    def test_kill_before_spawn_noop(self):
        h = Harness()
        # Should not raise; just no-op
        result = h.kill()
        assert result == -1
