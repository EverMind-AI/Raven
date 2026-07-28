"""Temporary CI diagnostic; removed before merge."""

from __future__ import annotations

import os

from typer.testing import CliRunner

from raven.cli.commands import app


def test_zz_ci_diag() -> None:
    from rich.console import Console

    env = {k: os.environ.get(k) for k in ("TERM", "COLORTERM", "NO_COLOR", "FORCE_COLOR", "CI", "COLUMNS", "LANG")}
    con = Console()
    r = CliRunner().invoke(app, ["doctor", "--help"])
    raise AssertionError(
        f"DIAG env={env} | console width={con.width} color={con.color_system} term={con.is_terminal} "
        f"| exit={r.exit_code} len={len(r.stdout)} exc={r.exception!r} | stdout={r.stdout[:800]!r}"
    )
