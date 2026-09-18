"""Tripwires for the two root installers (install.sh and install.ps1).

install.sh builds uv option pairs in scalar variables and expands them
unquoted (POSIX sh has no arrays), so a requirement carried that way must
stay a single word: word splitting hands uv each space-separated piece as
its own argument, uv exits 2, and the retry ladder silently degrades to a
bare install with the memory plugin and channel extras dropped. The quoted
positional requirement legitimately keeps the spaced PEP 508 form, so the
spaced spelling looks natural and keeps getting reintroduced -- this pin
turns that red in CI instead.

The optional capability steps (the chromium download, the LibreOffice offer)
get pins of their own: they stay skippable via RAVEN_MINIMAL, they read the
interactive answer and sudo's password prompt from /dev/tty (stdin is the
script itself under `curl | sh`), and they stay above install.sh's closing
launch -- that call holds the terminal until Ctrl-C, so anything placed after
it would not run at all on a normal install.

install.ps1 mirrors the capability steps for Windows (Install-Browser,
Install-Office) and the closing launch (Start-Web): same decisions, same
degrade-loudly warns, with the /dev/tty gate traded for a console gate --
under `irm | iex` Read-Host still reads the console, but CI has none, so the
guard must make a non-interactive run skip the winget offer cleanly instead of
hanging on it. The one deliberate divergence is the launch's exit handling,
pinned below.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).resolve().parents[1] / "install.sh"
INSTALL_PS1 = Path(__file__).resolve().parents[1] / "install.ps1"


def test_the_installer_is_where_this_tripwire_thinks_it_is() -> None:
    assert INSTALL_SH.is_file()


def test_the_everos_with_requirement_stays_one_word() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "everos-memory@$everos_url" in text
    assert "everos-memory @ " not in text


def test_the_optional_capability_steps_exist_and_are_skippable() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    for fn in ("install_browser", "install_office"):
        assert f"{fn}() {{" in text
    assert '[ -n "${RAVEN_MINIMAL:-}" ] || install_browser' in text
    assert '[ -n "${RAVEN_MINIMAL:-}" ] || install_office' in text


def test_the_install_ends_on_a_running_page() -> None:
    """install.sh finishes in the product: it clears a resident gateway an
    earlier install left behind, then holds the terminal on a fresh one so the
    browser opens on the build that just landed."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "launch_web() {" in text
    assert "web --stop" in text
    assert "web --foreground" in text
    assert "  launch_web\n" in text


def test_the_launch_invokes_raven_by_absolute_path() -> None:
    """`uv tool update-shell` only fixes future shells, so this one's PATH may
    still not carry the shim -- a bare `raven web` here would not be found."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    launch = text[text.index("launch_web() {") :]
    assert 'bin="$(uv tool dir --bin 2>/dev/null || true)/raven"' in launch
    assert '"$bin" web --foreground' in launch


def test_the_web_assets_rebuild_when_the_frontend_moved_on() -> None:
    """An editable install relinks Python and nothing else. Built once and then
    only ever checked for existence, the TUI bundle and the served page keep
    serving whatever the tree held at first install, which reads as a frontend
    that ignores your edits -- so both artifacts are compared against their
    sources, not merely tested for presence."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "is_stale() {" in text
    assert 'if is_stale "$src/ui-tui/dist/entry.js" "$src/ui-tui"; then need_tui=1; fi' in text
    assert '-newer "$artifact"' in text
    assert '[ -f "$src/ui-tui/dist/entry.js" ] || need_tui=1' not in text


def test_the_page_staleness_covers_the_shared_catalogue() -> None:
    """ui-web/build.py inlines i18n/messages.json, so a catalogue-only change
    is a page change -- watching ui-web alone would miss it."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert 'is_stale "$src/ui-web/dist/index.html" "$src/ui-web" "$src/i18n"' in text


def test_the_staleness_walk_prunes_what_the_build_itself_writes() -> None:
    """node_modules is rewritten by this script's own `npm ci` and dist holds
    the artifact being judged: left in the walk, either makes every artifact
    permanently stale and every re-run a full rebuild."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    walk = text[text.index("is_stale() {") : text.index("build_web_assets() {")]
    assert r"\( -name node_modules -o -name dist -o -name .modern \) -prune -o" in walk


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("sh") is None, reason="POSIX sh only")
def test_is_stale_answers_each_case_it_exists_for(tmp_path: Path) -> None:
    """The text pins above cannot tell a working mtime comparison from a broken
    one, and this is the half that decides whether a rebuild happens at all."""
    body = re.search(r"^is_stale\(\) \{.*?^\}$", INSTALL_SH.read_text(encoding="utf-8"), re.S | re.M)
    assert body is not None
    harness = tmp_path / "harness.sh"
    harness.write_text(
        body.group(0) + '\nif is_stale "$1" "$2" "$3"; then echo STALE; else echo FRESH; fi\n',
        encoding="utf-8",
    )

    web, catalog = tmp_path / "ui-web", tmp_path / "i18n"
    (web / "src").mkdir(parents=True)
    (web / "node_modules").mkdir()
    (web / ".modern").mkdir()
    (web / "dist").mkdir()
    catalog.mkdir()
    artifact = web / "dist" / "index.html"

    # An explicit clock, not sleeps: every step stamps a second further out than
    # the last, so the comparison under test is the only thing being measured.
    epoch = time.time()

    def stamp(path: Path, tick: int, body: str = "x") -> None:
        path.write_text(body, encoding="utf-8")
        os.utime(path, (epoch + tick, epoch + tick))

    def verdict() -> str:
        r = subprocess.run(
            ["sh", str(harness), str(artifact), str(web), str(catalog)],
            capture_output=True,
            text=True,
            check=True,
        )
        return r.stdout.strip()

    stamp(web / "src" / "page.html", 1)
    stamp(catalog / "messages.json", 1)
    assert verdict() == "STALE", "a missing artifact must build"

    stamp(artifact, 2)
    assert verdict() == "FRESH", "an artifact newer than every source must not rebuild"

    stamp(web / "src" / "page.html", 3)
    assert verdict() == "STALE", "a touched page source must rebuild"

    stamp(artifact, 4)
    stamp(catalog / "messages.json", 5)
    assert verdict() == "STALE", "a touched catalogue must rebuild"

    stamp(artifact, 6)
    for junk in (web / "node_modules" / "x", web / ".modern" / "modern.iife.js", web / "dist" / "extra.css"):
        stamp(junk, 7)
    assert verdict() == "FRESH", "the build's own output must not count as a source change"


def test_a_node_without_npm_fetches_the_runtime_that_carries_one() -> None:
    """Debian and Ubuntu package node and npm separately, so a system node >= 22
    satisfies ensure_node and the build then finds no npm with no private
    runtime ever fetched. The official tarball carries both, so that case
    provisions rather than skipping the TUI and page builds."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "provision_private_node() {" in text
    probe = text[text.index("resolve_node_dir() {") : text.index("build_web_assets() {")]
    assert "provision_private_node" in probe


def test_the_build_time_node_fetch_cannot_fail_the_install() -> None:
    """ensure_node dies on a failed download because raven needs node at all.
    This second fetch is for npm only: the node already present still runs
    `raven tui`, so its failure is a skipped build. provision_private_node dies
    internally, so the build-time caller must subshell it."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    probe = text[text.index("resolve_node_dir() {") : text.index("build_web_assets() {")]
    assert "if ( provision_private_node ); then" in probe
    assert 'blocker="Found node but not npm"' in probe


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("sh") is None, reason="POSIX sh only")
def test_resolve_node_dir_answers_each_case_it_exists_for(tmp_path: Path) -> None:
    """The text pins cannot tell a working fallback from one that never fires."""
    source = INSTALL_SH.read_text(encoding="utf-8")
    bodies = []
    for name in ("private_node_bin", "resolve_node_dir"):
        body = re.search(rf"^{name}\(\) \{{.*?^\}}$", source, re.S | re.M)
        assert body is not None, name
        bodies.append(body.group(0))

    harness = tmp_path / "harness.sh"
    harness.write_text(
        "info() { :; }\n"
        # Stands in for the download: writes the runtime layout private_node_bin
        # looks for, or fails the way a dead network would.
        "provision_private_node() {\n"
        '  [ "$PROVISION_OK" = 1 ] || { echo "download failed" >&2; exit 1; }\n'
        '  mkdir -p "$NODE_RUNTIME_DIR/node-v22.20.0-x/bin"\n'
        "  for exe in node npm; do\n"
        "    printf '#!/bin/sh\\necho ok\\n' > \"$NODE_RUNTIME_DIR/node-v22.20.0-x/bin/$exe\"\n"
        '    chmod +x "$NODE_RUNTIME_DIR/node-v22.20.0-x/bin/$exe"\n'
        "  done\n"
        "}\n" + "\n".join(bodies) + "\n"
        "resolve_node_dir >/dev/null 2>&1\n"
        'printf \'%s|%s\' "${node_dir:-}" "${blocker:-}"\n',
        encoding="utf-8",
    )

    def verdict(*, system_node: bool, system_npm: bool, provision_ok: bool) -> tuple[str, str]:
        case = tmp_path / f"case-{system_node}-{system_npm}-{provision_ok}"
        sysbin, runtime = case / "sysbin", case / "runtime"
        sysbin.mkdir(parents=True)
        runtime.mkdir(parents=True)
        for name, wanted in (("node", system_node), ("npm", system_npm)):
            if wanted:
                exe = sysbin / name
                exe.write_text("#!/bin/sh\necho ok\n", encoding="utf-8")
                exe.chmod(0o755)
        r = subprocess.run(
            ["sh", str(harness)],
            capture_output=True,
            text=True,
            check=True,
            env={
                "PATH": f"{sysbin}:/usr/bin:/bin",
                "NODE_RUNTIME_DIR": str(runtime),
                "PROVISION_OK": "1" if provision_ok else "0",
                "HOME": str(case),
            },
        )
        node_dir, _, blocker = r.stdout.partition("|")
        return node_dir, blocker

    node_dir, blocker = verdict(system_node=True, system_npm=True, provision_ok=True)
    assert node_dir.endswith("sysbin") and not blocker, "a system node with npm is used as is"

    node_dir, blocker = verdict(system_node=True, system_npm=False, provision_ok=True)
    assert "node-v22.20.0-x" in node_dir and not blocker, "node without npm provisions a runtime"

    node_dir, blocker = verdict(system_node=True, system_npm=False, provision_ok=False)
    assert not node_dir and blocker == "Found node but not npm", "a failed fetch warns, never aborts"

    node_dir, blocker = verdict(system_node=False, system_npm=False, provision_ok=True)
    assert not node_dir and blocker == "No usable node found", "ensure_node owns the no-node case"


def test_the_office_prompt_and_sudo_both_read_the_tty() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "read -r answer < /dev/tty" in text
    assert "sudo apt-get install -y libreoffice < /dev/tty" in text


def test_a_failed_tty_read_declines_instead_of_defaulting_yes() -> None:
    """Ctrl-D and a tty lost after the gate are not consent: with a default-yes
    prompt, read's failure branch must return, never fall through to sudo."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "read -r answer < /dev/tty || {" in text
    assert "no answer read" in text


def test_the_tty_gate_probes_openability_not_existence() -> None:
    """/dev/tty can exist with no controlling terminal (CI, cron, `docker run
    -t` without -i), where a read on it errors or hangs -- the gate must open
    the node, not stat it."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert ": < /dev/tty" in text
    assert "[ ! -e /dev/tty ]" not in text


def test_the_office_offer_defaults_to_yes_in_both_installers() -> None:
    """Enter means install, and only an explicit no declines -- including the
    spelled-out word, which the bare `n|N)` arm used to fall through to yes."""
    sh = INSTALL_SH.read_text(encoding="utf-8")
    assert "[Y/n]" in sh
    assert "n|N|[nN][oO])" in sh
    ps1 = INSTALL_PS1.read_text(encoding="utf-8")
    assert "[Y/n]" in ps1
    assert '-match "^[nN]"' in ps1


def test_the_capability_steps_stay_above_the_closing_launch() -> None:
    """launch_web holds the terminal until Ctrl-C, so a capability step placed
    after it would never run."""
    closing = INSTALL_SH.read_text(encoding="utf-8")
    closing = closing[closing.index("launch_web() {") :].lower()
    assert "playwright" not in closing
    assert "libreoffice" not in closing


def test_the_windows_installer_is_where_this_tripwire_thinks_it_is() -> None:
    assert INSTALL_PS1.is_file()


def test_the_windows_capability_steps_exist_and_are_skippable() -> None:
    text = INSTALL_PS1.read_text(encoding="utf-8")
    for fn in ("Install-Browser", "Install-Office"):
        assert f"function {fn}" in text
    assert "if (-not $env:RAVEN_MINIMAL) { Install-Browser $uv }" in text
    assert "if (-not $env:RAVEN_MINIMAL) { Install-Office }" in text


def test_the_windows_install_ends_on_a_running_page() -> None:
    """install.ps1 ends the same way install.sh does, by absolute path for the
    same reason: this session's PATH may not carry the shim yet."""
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "function Start-Web" in text
    assert "web --stop" in text
    assert "    & $bin web --foreground" in text
    assert "    Start-Web $uv\n" in text


def test_the_windows_launch_warns_instead_of_exiting_on_a_non_zero_page() -> None:
    """Under `irm | iex` the installer runs in the reader's own interactive
    PowerShell, and Ctrl-C -- the ordinary way to end a foreground page --
    returns non-zero. Exiting on that would close the window they are standing
    in, so the Windows launch reports where the POSIX one propagates."""
    text = INSTALL_PS1.read_text(encoding="utf-8")
    launch = text[text.index("function Start-Web") :]
    assert "exit $LASTEXITCODE" not in launch
    assert "the page ended with exit code $LASTEXITCODE" in launch


def test_the_windows_launch_puts_the_shim_on_path_before_holding_the_session() -> None:
    """The page holds the session until Ctrl-C, so a PATH fix placed after it
    would only land once the reader had already stopped the page."""
    text = INSTALL_PS1.read_text(encoding="utf-8")
    main = text[text.index("function Main") :]
    assert main.index("Add-ProcessPath") < main.index("Start-Web $uv")


def test_the_windows_office_offer_needs_a_real_console() -> None:
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "[Environment]::UserInteractive -and -not [Console]::IsInputRedirected" in text


def test_the_windows_web_assets_rebuild_when_the_frontend_moved_on() -> None:
    """Same decision as install.sh: compare the artifacts against their
    sources, and give the page the shared catalogue it inlines."""
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "function Test-AssetStale" in text
    assert "$needTui = Test-AssetStale" in text
    assert "$needPage = Test-AssetStale" in text
    assert '(Join-Path $ScriptDir "i18n")' in text
    assert "$needTui = -not (Test-Path" not in text


def test_the_windows_staleness_walk_prunes_what_the_build_itself_writes() -> None:
    text = INSTALL_PS1.read_text(encoding="utf-8")
    walk = text[text.index("function Test-AssetStale") : text.index("function Build-WebAssets")]
    assert '$pruned = @("node_modules", "dist", ".modern")' in walk
    assert "$pruned -contains $_.Name" in walk


def test_the_windows_node_without_npm_fetches_the_runtime_that_carries_one() -> None:
    """Same decision as install.sh, with try/catch standing in for the subshell:
    the fetch raises on failure and the build-time caller must not abort."""
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "function Install-PrivateNode" in text
    assert "return Install-PrivateNode" in text
    build = text[text.index("function Build-WebAssets") :]
    assert "try { $privateNode = Install-PrivateNode } catch { $privateNode = $null }" in build


def test_the_windows_capability_steps_stay_above_the_closing_launch() -> None:
    """Start-Web holds the session until Ctrl-C, so a capability step placed
    after it would never run."""
    closing = INSTALL_PS1.read_text(encoding="utf-8")
    closing = closing[closing.index("function Start-Web") :].lower()
    assert "playwright" not in closing
    assert "libreoffice" not in closing
