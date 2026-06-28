# SPDX-License-Identifier: MIT
# Copyright (c) 2026 EverMind.
# See NOTICES.md.

"""Tests for `raven tui` after the hermes UI shell fork.

Adds coverage on top of `test_cli_tui_bootstrap.py` that is specific to the
post-fork state (full hermes ui-tui/ vendored, GatewayClientStub in place).

These tests should pass whether or not the dist/entry.js bundle has been
freshly built — they exercise the Python-side launcher logic, not the Ink
runtime.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from raven.cli.commands import app
from raven.cli.tui_commands import _UI_TUI_DIR

runner = CliRunner()


def test_ui_tui_dir_resolves_under_repo_root():
    """Sanity: the launcher's _UI_TUI_DIR points to the vendored ui-tui tree."""
    assert _UI_TUI_DIR.name == "ui-tui"
    assert (_UI_TUI_DIR / "package.json").exists(), (
        f"After fork, ui-tui/package.json should exist at {_UI_TUI_DIR}"
    )
    # Post-fork sanity — vendored hermes-ink package present.
    assert (_UI_TUI_DIR / "packages" / "hermes-ink" / "package.json").exists(), (
        "Vendored @hermes/ink package must be present after fork."
    )
    # GatewayClientStub source present.
    assert (_UI_TUI_DIR / "src" / "gatewayClientStub.ts").exists(), (
        "GatewayClientStub source must exist post-fork."
    )


def test_check_exits_zero_when_node_ok_and_run_succeeds(monkeypatch):
    """--check returns 0 when node found, version OK, and child spawned."""
    monkeypatch.setattr(
        "raven.cli.tui_commands.find_node",
        lambda: ("/usr/bin/node", (22, 5, 0)),
    )
    monkeypatch.setattr(
        "raven.cli.tui_commands.run_subprocess",
        lambda *_a, **_kw: 0,
    )
    result = runner.invoke(app, ["tui", "--check"])
    assert result.exit_code == 0, result.output


def test_check_exits_two_when_dist_missing_and_no_dev(monkeypatch, tmp_path):
    """--check without --dev requires dist/entry.js (i.e. `npm run build` must
    have run). When missing, exit code is 2 with a helpful 'npm run build' hint.
    """
    # Point _UI_TUI_DIR at an empty tmp dir so dist/entry.js is missing.
    monkeypatch.setattr(
        "raven.cli.tui_commands._UI_TUI_DIR",
        tmp_path / "ui-tui-fake",
    )
    monkeypatch.setattr(
        "raven.cli.tui_commands.find_node",
        lambda: ("/usr/bin/node", (22, 5, 0)),
    )
    # The fake dir doesn't exist either, but we want to hit the dist-missing
    # branch specifically — so create the parent dir but NOT dist/entry.js.
    fake_dir = tmp_path / "ui-tui-fake"
    fake_dir.mkdir()
    result = runner.invoke(app, ["tui"])
    assert result.exit_code == 2, result.output


def test_dev_mode_uses_rpc_socket_post_fork(monkeypatch, tmp_path):
    """Interactive `--dev` must spawn via run_subprocess_with_rpc, not the
    plain run_subprocess. entry.tsx requires RAVEN_RPC_SOCKET,
    so a plain spawn exits 2 ("spawn via parent"). This guards the regression
    where `--dev` was left on the pre-RPC plain-spawn branch. `--watch` is
    dropped because watch restarts are incompatible with the one-shot RPC
    handshake (a restart drops the accepted socket connection).
    """
    fake_bin = tmp_path / "fake-node" / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "node").write_text("#!/bin/sh\necho ok\n")
    (fake_bin / "npx").write_text("#!/bin/sh\necho ok\n")

    monkeypatch.setattr(
        "raven.cli.tui_commands.find_node",
        lambda: (str(fake_bin / "node"), (22, 5, 0)),
    )

    captured: dict[str, object] = {}

    def fake_run_subprocess_with_rpc(binary, args, cwd, **_kw):
        captured["args"] = args
        return 0

    def fail_plain(*_a, **_kw):
        raise AssertionError("interactive --dev must not use plain run_subprocess")

    monkeypatch.setattr(
        "raven.cli.tui_commands.run_subprocess_with_rpc",
        fake_run_subprocess_with_rpc,
    )
    monkeypatch.setattr(
        "raven.cli.tui_commands.run_subprocess",
        fail_plain,
    )

    result = runner.invoke(app, ["tui", "--dev"])
    assert result.exit_code == 0, result.output
    args = captured["args"]
    assert "tsx" in args, f"--dev should invoke tsx, got args={args!r}"
    assert "src/entry.tsx" in args, f"--dev should run src/entry.tsx, got args={args!r}"
    assert "--watch" not in args, f"--dev must drop --watch (RPC is one-shot), got args={args!r}"


def test_dev_check_mode_keeps_plain_spawn(monkeypatch, tmp_path):
    """`--dev --check` stays on the plain run_subprocess: entry.tsx short-
    circuits on RAVEN_TUI_CHECK before the socket guard, so the smoke path
    needs no RPC server. Opening one would be wasted setup."""
    fake_bin = tmp_path / "fake-node" / "bin"
    fake_bin.mkdir(parents=True)
    (fake_bin / "node").write_text("#!/bin/sh\necho ok\n")
    (fake_bin / "npx").write_text("#!/bin/sh\necho ok\n")

    monkeypatch.setattr(
        "raven.cli.tui_commands.find_node",
        lambda: (str(fake_bin / "node"), (22, 5, 0)),
    )

    captured: dict[str, object] = {}

    def fake_run_subprocess(binary, args, cwd, **_kw):
        captured["args"] = args
        return 0

    def fail_rpc(*_a, **_kw):
        raise AssertionError("--dev --check must not open an RPC socket")

    monkeypatch.setattr(
        "raven.cli.tui_commands.run_subprocess",
        fake_run_subprocess,
    )
    monkeypatch.setattr(
        "raven.cli.tui_commands.run_subprocess_with_rpc",
        fail_rpc,
    )

    result = runner.invoke(app, ["tui", "--dev", "--check"])
    assert result.exit_code == 0, result.output
    args = captured["args"]
    assert "tsx" in args, f"--dev --check should invoke tsx, got args={args!r}"
    assert "src/entry.tsx" in args, f"--dev --check should run src/entry.tsx, got args={args!r}"


def test_check_sets_raven_tui_check_env_var(monkeypatch):
    """`--check` must propagate RAVEN_TUI_CHECK=1 into the environment so
    the Node child (ui-tui/src/entry.tsx) takes the early-exit smoke path
    instead of starting the Ink UI. Without this, `--check` would block the
    terminal until the user hit Ctrl+C — a real bug reported during
    manual smoke that this test guards against regression."""
    monkeypatch.delenv("RAVEN_TUI_CHECK", raising=False)
    monkeypatch.setattr(
        "raven.cli.tui_commands.find_node",
        lambda: ("/usr/bin/node", (22, 5, 0)),
    )

    captured_env: dict[str, str] = {}

    def fake_run_subprocess(*_a, **_kw):
        # Snapshot env at call time.
        import os as _os

        captured_env.update(_os.environ)
        return 0

    monkeypatch.setattr(
        "raven.cli.tui_commands.run_subprocess",
        fake_run_subprocess,
    )

    result = runner.invoke(app, ["tui", "--check"])
    assert result.exit_code == 0, result.output
    assert captured_env.get("RAVEN_TUI_CHECK") == "1", (
        "--check must export RAVEN_TUI_CHECK=1 so the node child can take "
        "the early-exit smoke path; missing means the smoke test hangs."
    )


def test_packages_hermes_ink_dist_gitignored_but_buildable(tmp_path: Path):
    """The vendored @hermes/ink ships an esbuild-built dist/ that is NOT in git
    history (gitignored). Sanity: source entry exists and is buildable.

    We do NOT actually build it here (slow + needs npm); we just confirm the
    source layout expected by `npm run build --prefix packages/hermes-ink`.
    """
    pkg = _UI_TUI_DIR / "packages" / "hermes-ink"
    assert (pkg / "src" / "entry-exports.ts").exists(), (
        "hermes-ink src/entry-exports.ts (esbuild entry) must exist."
    )
    assert (pkg / "package.json").exists()
