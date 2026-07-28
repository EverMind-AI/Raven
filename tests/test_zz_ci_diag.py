"""Temporary CI diagnostic; removed before merge."""

from __future__ import annotations

import os
import sys

from typer.testing import CliRunner

from raven.cli.commands import app


def test_zz_ci_diag() -> None:
    from rich.console import Console

    env = {k: os.environ.get(k) for k in ("TERM", "COLORTERM", "NO_COLOR", "FORCE_COLOR", "CI", "COLUMNS", "LANG")}
    con = Console()
    r = CliRunner().invoke(app, ["doctor", "--help"])
    print(f"DIAG env={env}", file=sys.stderr)
    print(f"DIAG console width={con.width} color={con.color_system} term={con.is_terminal}", file=sys.stderr)
    print(f"DIAG exit={r.exit_code} len={len(r.stdout)}", file=sys.stderr)
    print(f"DIAG stdout={r.stdout[:600]!r}", file=sys.stderr)
    if r.exception:
        print(f"DIAG exception={r.exception!r}", file=sys.stderr)
