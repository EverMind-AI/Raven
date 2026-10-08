"""The suite's refusal to run when default text I/O is not UTF-8."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from tests.conftest import _is_utf8_codec

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("encoding", ["utf-8", "UTF-8", "utf8", "cp65001"])
def test_utf8_is_accepted_by_codec_not_by_spelling(encoding: str) -> None:
    """``cp65001`` is what Python reports on Windows with the system-wide UTF-8
    option on, where every default open() already decodes UTF-8."""
    assert _is_utf8_codec(encoding)


@pytest.mark.parametrize("encoding", ["GBK", "cp936", "cp1252", "ANSI_X3.4-1968"])
def test_a_legacy_code_page_is_refused(encoding: str) -> None:
    assert not _is_utf8_codec(encoding)


def test_a_non_utf8_interpreter_stops_at_conftest_with_the_fix_named() -> None:
    """The C locale without coercion or UTF-8 mode is ASCII on Linux, which is
    enough to build an interpreter the guard has to refuse."""
    env = {**os.environ, "LC_ALL": "C", "PYTHONCOERCECLOCALE": "0", "PYTHONUTF8": "0"}
    probe = subprocess.run(
        [sys.executable, "-c", "import locale; print(locale.getpreferredencoding(False))"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    if _is_utf8_codec(probe.stdout.strip()):
        pytest.skip("the C locale is UTF-8 on this platform")
    result = subprocess.run(
        [sys.executable, "-c", "import tests.conftest"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "RuntimeError" in result.stderr
    assert "PYTHONUTF8=1" in result.stderr
