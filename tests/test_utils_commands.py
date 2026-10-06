"""The one interpretation of a stored launch-command string (raven/utils/commands.py).

A subagent row keeps its launch as one string, and where it is split decides
whether the spawn sees the command the operator wrote. The Windows arm exists
because POSIX-mode shlex eats the backslashes every drive-letter path carries
-- ``C:\\Users\\..\\python.exe`` reached ``CreateProcess`` as
``C:Users..python.exe``. These pin both arms from any host by patching the
module's own ``os.name`` read, the way the shapes the vendored tests use let a
drive-letter path be judged on POSIX.
"""

from __future__ import annotations

import pytest

import raven.utils.commands as cmd


def _as(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cmd.os, "name", name)


class TestCommandArgv:
    def test_posix_splits_on_whitespace_and_unescapes_nothing(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _as("posix", monkeypatch)
        assert cmd.command_argv("npx -y pkg --flag") == ["npx", "-y", "pkg", "--flag"]

    def test_windows_keeps_a_backslash_path_one_token(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The interpreter path survives: POSIX shlex would strip every ``\\``."""
        _as("nt", monkeypatch)
        assert cmd.command_argv(r"C:\Users\livxu\python.exe C:\raven\run.py") == [
            r"C:\Users\livxu\python.exe",
            r"C:\raven\run.py",
        ]

    def test_windows_groups_a_quoted_path_with_a_space(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _as("nt", monkeypatch)
        assert cmd.command_argv(r'"C:\Program Files\Python312\python.exe" run.py') == [
            r"C:\Program Files\Python312\python.exe",
            "run.py",
        ]

    def test_windows_halves_backslashes_before_a_closing_quote(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CommandLineToArgvW's one escape: ``\\\"`` ends in a literal quote."""
        _as("nt", monkeypatch)
        assert cmd.command_argv(r'"C:\Program Files\\" x') == ["C:\\Program Files\\", "x"]

    def test_unbalanced_quotes_raise_valueerror_on_both(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for name in ("posix", "nt"):
            _as(name, monkeypatch)
            with pytest.raises(ValueError):
                cmd.command_argv('run "unterminated')


class TestCommandTokens:
    """The shape-preserving split the launcher existence probes judge with."""

    def test_groups_a_double_quoted_windows_path(self) -> None:
        assert cmd.command_tokens(r'"C:\Program Files\app\run.exe" --go') == [
            r"C:\Program Files\app\run.exe",
            "--go",
        ]

    def test_groups_a_single_quoted_posix_path(self) -> None:
        """``shlex.quote`` output must stay one token to the shape probe."""
        assert cmd.command_tokens("python '/tmp/raven agents(1)/raven-code/run.py' --acp") == [
            "python",
            "/tmp/raven agents(1)/raven-code/run.py",
            "--acp",
        ]

    def test_keeps_a_backslash_path_whole_on_any_host(self) -> None:
        assert cmd.command_tokens(r"C:\gone\python.exe --run") == [r"C:\gone\python.exe", "--run"]


class TestCommandQuote:
    def test_windows_wraps_a_spaced_path_and_escapes_inner_quotes(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _as("nt", monkeypatch)
        assert cmd.command_quote(r"C:\Program Files\py.exe") == r'"C:\Program Files\py.exe"'

    def test_a_quoted_windows_token_round_trips(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """What a producer quotes must come back through ``command_argv`` as one token."""
        _as("nt", monkeypatch)
        quoted = cmd.command_quote(r"C:\Program Files\Python312\python.exe")
        (first,) = cmd.command_argv(f"{quoted} run.py")[:1]
        assert first == r"C:\Program Files\Python312\python.exe"

    def test_posix_uses_shlex_quoting(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _as("posix", monkeypatch)
        assert cmd.command_quote("/opt/my dir/py") == "'/opt/my dir/py'"


class TestLaunchArgvResolution:
    def test_posix_leaves_argv_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _as("posix", monkeypatch)
        assert cmd.launch_argv("npx -y pkg") == ["npx", "-y", "pkg"]

    def test_windows_resolves_a_bare_extensionless_name(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """CreateProcess needs the full path of ``npx.cmd``; ``shutil.which`` finds it."""
        _as("nt", monkeypatch)
        monkeypatch.setattr(cmd.shutil, "which", lambda exe: rf"C:\Tools\{exe}.cmd")
        assert cmd.launch_argv("npx -y pkg")[0] == r"C:\Tools\npx.cmd"

    def test_windows_leaves_a_path_or_extension_alone(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _as("nt", monkeypatch)
        monkeypatch.setattr(cmd.shutil, "which", lambda exe: None)
        assert cmd.launch_argv(r"C:\Tools\node.exe run.js")[0] == r"C:\Tools\node.exe"
        assert cmd.launch_argv("npx.cmd -y pkg")[0] == "npx.cmd"
