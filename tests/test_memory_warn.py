"""warn_memory_start_failed must surface on stderr (visible under the TUI)."""

from __future__ import annotations

import pytest

from raven.cli._memory_warn import warn_memory_start_failed


def test_warn_memory_start_failed_writes_degraded_notice_to_stderr(
    capsys: pytest.CaptureFixture[str],
) -> None:
    warn_memory_start_failed(ValueError("memory.userId='..' is not accepted"))

    captured = capsys.readouterr()
    combined = captured.err + captured.out

    assert "long-term memory is off" in combined
    assert "ValueError" in combined
