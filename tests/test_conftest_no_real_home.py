"""The suite's own home, ``_no_real_raven_home`` in ``tests/conftest.py``, on every platform."""

from __future__ import annotations

import ntpath
import os
import posixpath
from pathlib import Path


def test_a_test_that_brings_no_home_of_its_own_reads_the_suite_s_home_on_windows_as_on_posix(tmp_path: Path) -> None:
    """Two of the ssh-alias CLI tests take only ``store`` and ``reached``, so their home is this fixture alone.
    It moved only ``HOME`` until it went through ``point_home``, and on Windows ``os.path.expanduser`` reads
    ``USERPROFILE`` (``ntpath``): those two wrote ``Host gpu`` into the operator's own ``~/.ssh/config``.
    Pinned with both path flavours, from a test that takes no home fixture of its own."""
    suite_home = tmp_path.with_name(f"{tmp_path.name}-home")

    assert ntpath.expanduser("~") == str(suite_home), "the Windows reading of ~"
    assert posixpath.expanduser("~") == str(suite_home), "the POSIX reading of ~"
    assert os.environ["HOME"] == os.environ["USERPROFILE"] == str(suite_home)
