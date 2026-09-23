"""Tripwires for the two root installers (install.sh and install.ps1).

Both installers resolve the latest release from the release page redirect
and derive every other URL from that one directory: the wheel, the locked
constraints and raven-plugins.txt, the list of plugin wheels the release
ships beside raven. The list is what they hand to `uv tool install
--with-requirements`; nothing in either script knows a plugin's name or
guesses one from an asset list, and neither script calls the GitHub API.

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
hanging on it.

Both launches obey the probe rule: the scripts are served from main and install
the latest release, which can predate a subcommand main already knows about
(`raven web` shipped after 0.1.13 did, and the install ended in "No such
command 'web'"). So every `raven <sub>` call is preceded by `raven <sub>
--help`, the probe's failure branch finishes on the one command every release
has, and neither the stop nor the page's own exit code becomes the script's.
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


def test_the_installers_read_the_release_plugin_list_and_never_the_api() -> None:
    """The release writes what it ships; the installer reads it. Grepping the
    API's JSON for asset names decided on the user's machine what a complete
    install was, said nothing when an asset was missing, and ran against a
    60-requests-per-hour quota one office shares."""
    for script in (INSTALL_SH, INSTALL_PS1):
        text = script.read_text(encoding="utf-8")
        assert "api.github.com" not in text, script.name
        assert "raven-plugins.txt" in text, script.name
        assert "--with-requirements" in text, script.name
        assert "everos_memory" not in text, script.name
    sh = INSTALL_SH.read_text(encoding="utf-8")
    assert "releases/latest" in sh and "redirect_url" in sh
    # Without a list the release installs raven alone and says so, as 0.1.13 did.
    assert "carries no plugin list" in sh
    assert "carries no plugin list" in INSTALL_PS1.read_text(encoding="utf-8")


def test_the_optional_capability_steps_exist_and_are_skippable() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    for fn in ("install_browser", "install_office"):
        assert f"{fn}() {{" in text
    assert '[ -n "${RAVEN_MINIMAL:-}" ] || install_browser' in text
    assert '[ -n "${RAVEN_MINIMAL:-}" ] || install_office' in text


def test_the_install_ends_on_a_running_page() -> None:
    """install.sh finishes in the product: it clears a resident gateway an
    earlier install left behind, then holds the terminal on a fresh one so the
    browser opens on the build that just landed. RAVEN_NO_LAUNCH=1 skips the
    launch, so CI and Dockerfiles get a script that returns."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "launch_web() {" in text
    assert "web --stop" in text
    assert "web --foreground" in text
    assert '  [ -n "${RAVEN_NO_LAUNCH:-}" ] || launch_web\n' in text


def test_the_launch_invokes_raven_by_absolute_path() -> None:
    """`uv tool update-shell` only fixes future shells, so this one's PATH may
    still not carry the shim -- a bare `raven web` here would not be found."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    launch = text[text.index("launch_web() {") :]
    assert 'bin="$(uv tool dir --bin 2>/dev/null || true)/raven"' in launch
    assert '"$bin" web --foreground' in launch


def _unprobed_subcommands(text: str, call: str) -> list[str]:
    """Subcommands the script calls without an earlier `<call> <sub> --help`."""
    probed: set[str] = set()
    unprobed: list[str] = []
    for line in text.splitlines():
        for m in re.finditer(call + r" ([a-z][a-z-]*)(.*)$", line):
            sub, rest = m.group(1), m.group(2)
            if "--help" in rest:
                probed.add(sub)
            elif sub not in probed:
                unprobed.append(sub)
    return unprobed


def test_the_launch_probes_before_calling_a_subcommand_the_release_may_lack() -> None:
    """The script is served from main and installs the latest release, which
    can predate a subcommand main already knows about: `raven web` shipped
    after 0.1.13 did, and every one-line install ended in "No such command
    'web'" with exit 2. So every `"$bin" <sub>` call is preceded by a `"$bin"
    <sub> --help` probe whose failure branch ends the install on the one
    command every release has."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert '"$bin" web --help' in text
    assert _unprobed_subcommands(text, r'"\$bin"') == []
    launch = text[text.index("launch_web() {") :]
    assert "Raven installed." in launch


@pytest.mark.skipif(sys.platform == "win32" or shutil.which("sh") is None, reason="POSIX sh only")
def test_launch_web_answers_each_release_shape_it_exists_for(tmp_path: Path) -> None:
    """The text pins cannot tell a probe that gates the launch from one that is
    merely present. Two fake ravens stand in: the shape of the latest release,
    which answers `web` the way typer does (usage on stderr, exit 2), and the
    shape of main, which has it. Every call the fakes receive is logged, so the
    assertions read what reached the product, not what the script printed."""
    body = re.search(r"^launch_web\(\) \{.*?^\}$", INSTALL_SH.read_text(encoding="utf-8"), re.S | re.M)
    assert body is not None
    harness = tmp_path / "harness.sh"
    harness.write_text(
        "ok() { printf 'OK %s\\n' \"$1\"; }\n"
        "warn() { printf 'WARN %s\\n' \"$1\" >&2; }\n" + body.group(0) + "\nlaunch_web\n",
        encoding="utf-8",
    )

    # One pair of fake executables for every case: macOS scans a freshly written
    # executable on its first run, so a fresh pair per case is what made this
    # test idle. The shape of the fake raven is chosen per run through the env.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "calls.log"
    # `uv tool dir --bin` names the shim directory; the fake answers with ours.
    (bin_dir / "uv").write_text(f"#!/bin/sh\nprintf '%s' '{bin_dir}'\n", encoding="utf-8")
    (bin_dir / "raven").write_text(
        "#!/bin/sh\n"
        f"echo \"$*\" >> '{log}'\n"
        'case "$FAKE_SHAPE:$1:$2" in\n'
        # The latest release: typer answers an unknown command with usage and exit 2.
        '  without-web:web:*) echo "Usage: raven [OPTIONS] COMMAND [ARGS]..." >&2; '
        "echo \"No such command 'web'.\" >&2; exit 2 ;;\n"
        "  stop-fails:web:--stop) exit 1 ;;\n"
        "  interrupted:web:--foreground) exit 130 ;;\n"
        "esac\n"
        "exit 0\n",
        encoding="utf-8",
    )
    for exe in ("uv", "raven"):
        (bin_dir / exe).chmod(0o755)

    def run(shape: str) -> tuple[int, str, str, list[str]]:
        log.unlink(missing_ok=True)
        r = subprocess.run(
            ["sh", str(harness)],
            capture_output=True,
            text=True,
            check=False,
            env={"PATH": f"{bin_dir}:/usr/bin:/bin", "HOME": str(tmp_path), "FAKE_SHAPE": shape},
        )
        calls = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
        return r.returncode, r.stdout, r.stderr, calls

    code, out, _err, calls = run("without-web")
    assert code == 0, "a release without `web` must still end the install cleanly"
    assert "run: raven" in out, "the fallback names the command every release has"
    assert calls == ["web --help"], "only the probe may reach a release without `web`"

    code, _out, _err, calls = run("with-web")
    assert code == 0
    assert calls == ["web --help", "web --stop", "web --foreground"], "a release with `web` gets the launch"

    code, _out, err, calls = run("stop-fails")
    assert code == 0
    assert calls[-1] == "web --foreground", "a failed --stop is a warning, not the end of the launch"
    assert "could not stop" in err

    code, _out, err, calls = run("interrupted")
    assert code == 0, "the page's own exit code is not the script's"
    assert calls[-1] == "web --foreground"
    assert "130" in err and "raven web" in err


def test_the_path_hint_precedes_the_launch() -> None:
    """`uv tool update-shell` only fixes future shells. The page holds this one
    until Ctrl-C, so the hint about the shim's directory has to land before
    the launch, and RAVEN_NO_LAUNCH runs must still get it."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    main = text[text.index("main() {") :]
    hint = main.index("open a new terminal, or run: export PATH=")
    assert hint < main.index("|| launch_web")


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
    assert "sudo apt-get install -y libreoffice fonts-noto-cjk < /dev/tty" in text


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


def test_the_ci_gate_installs_the_latest_release_the_way_users_do() -> None:
    """The text pins above cannot catch the class of defect that shipped: a
    script on main calling something the latest release lacks. Only a real
    install of that release with this script can, so CI does one -- piped, as
    `curl | sh` and `irm | iex` arrive, which is what selects remote mode; run
    as a file, the script would detect the checkout and install it editable
    instead, and the gate would be measuring the wrong thing."""
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = workflow[workflow.index("  installer:") :]
    job = job[: job.index("\n  windows-upgrade:")]
    assert "cat install.sh | sh" in job
    assert "Get-Content install.ps1 -Raw | Invoke-Expression" in job
    assert 'RAVEN_NO_LAUNCH: "1"' in job
    assert 'raven.exe" --version' in job and '"$UV_TOOL_BIN_DIR/raven" --version' in job


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
    assert "    if (-not $env:RAVEN_NO_LAUNCH) { Start-Web $uv }\n" in text


def test_the_windows_launch_warns_instead_of_exiting_on_a_non_zero_page() -> None:
    """Under `irm | iex` the installer runs in the reader's own interactive
    PowerShell, and Ctrl-C -- the ordinary way to end a foreground page --
    returns non-zero. Exiting on that would close the window they are standing
    in, so the launch reports instead. install.sh does the same since the
    probe rule landed: the page's exit code is never the script's."""
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


def test_the_windows_launch_probes_before_calling_web() -> None:
    """Same rule as install.sh. The message the old unguarded `--stop` printed
    on a release without `web` ("could not clear the gateway a previous install
    left running") described a gateway that never existed."""
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert _unprobed_subcommands(text, r"& \$bin") == []
    launch = text[text.index("function Start-Web") :]
    assert "& $bin web --help" in launch
    assert "Raven installed." in launch
    assert "could not clear the gateway" not in text


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


def _sh_function(text: str, name: str) -> str:
    """One shell function lifted out of install.sh, to be run on its own."""
    match = re.search(rf"^{name}\(\) \{{$.*?^\}}$", text, re.MULTILINE | re.DOTALL)
    assert match, f"{name} is not defined in install.sh"
    return match.group(0)


def test_the_font_check_recognises_the_file_the_fallback_writes(tmp_path: Path) -> None:
    """A second install has to be a no-op. The check runs before the download, so
    a filename it cannot recognise makes every install fetch the same 8MB again.

    Run with fc-list hidden, because that is the path where it matters: a host
    that has fontconfig answers from it and never reaches the filename check.
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    name = re.search(r'^HAN_FONT_NAME="([^"]+)"$', text, re.MULTILINE)
    assert name, "install.sh no longer names the font file it installs"
    installed = name.group(1)

    home = tmp_path / "home"
    fonts = home / ".local" / "share" / "fonts"
    fonts.mkdir(parents=True)
    (fonts / installed).write_bytes(b"OTTO placeholder")

    script = "\n".join(
        [
            "set -eu",
            'have() { case "$1" in fc-list) return 1 ;; *) command -v "$1" >/dev/null 2>&1 ;; esac; }',
            "NODE_OS=linux",
            f'HAN_FONT_NAME="{installed}"',
            _sh_function(text, "han_font_dir"),
            _sh_function(text, "have_han_font"),
            "have_han_font",
        ]
    )
    done = subprocess.run(  # noqa: S603 - /bin/sh with a script this test built
        ["/bin/sh", "-c", script],
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        check=False,
    )
    assert done.returncode == 0, (
        f"{installed} sits in the user font directory and the check did not see it, "
        f"so every later install downloads it again: {done.stderr}"
    )


def test_the_font_check_says_no_on_a_host_that_has_none(tmp_path: Path) -> None:
    """The other half: an empty directory must not report a font, or the install
    skips the step that is the whole point of it."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    home = tmp_path / "home"
    (home / ".local" / "share" / "fonts").mkdir(parents=True)

    script = "\n".join(
        [
            "set -eu",
            'have() { case "$1" in fc-list) return 1 ;; *) command -v "$1" >/dev/null 2>&1 ;; esac; }',
            "NODE_OS=linux",
            'HAN_FONT_NAME="NotoSansSC-Regular.otf"',
            _sh_function(text, "han_font_dir"),
            _sh_function(text, "have_han_font"),
            "have_han_font",
        ]
    )
    done = subprocess.run(  # noqa: S603 - /bin/sh with a script this test built
        ["/bin/sh", "-c", script],
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")},
        check=False,
    )
    assert done.returncode != 0, "an empty font directory reported a font"


def _han_font_check(text: str, *, node_os: str, home: Path, macos_root: Path, fc_list: Path | None) -> int:
    """Run install.sh's `have_han_font` alone, against directories this test owns.

    `fc-list` is a real executable on PATH rather than a shell function, because
    a name with a hyphen is not a function name POSIX sh will accept -- and
    because what is under test is whether the check consults it at all.
    """
    path = os.environ.get("PATH", "/usr/bin:/bin")
    if fc_list is not None:
        fc_list.parent.mkdir(parents=True, exist_ok=True)
        fc_list.write_text("#!/bin/sh\nprintf 'Noto Sans CJK SC\\n'\n", encoding="utf-8")
        fc_list.chmod(0o755)
        path = f"{fc_list.parent}{os.pathsep}{path}"
    script = "\n".join(
        [
            "set -eu",
            'have() { command -v "$1" >/dev/null 2>&1; }',
            f"NODE_OS={node_os}",
            'HAN_FONT_NAME="NotoSansSC-Regular.otf"',
            f'MACOS_FONT_ROOT="{macos_root}"',
            _sh_function(text, "han_font_dir"),
            _sh_function(text, "have_han_font"),
            "have_han_font",
        ]
    )
    return subprocess.run(  # noqa: S603 - /bin/sh with a script this test built
        ["/bin/sh", "-c", script],
        capture_output=True,
        text=True,
        env={"HOME": str(home), "PATH": path},
        check=False,
    ).returncode


def test_a_mac_needs_no_font_because_it_already_ships_one(tmp_path: Path) -> None:
    """Nothing is missing on a Mac: what kept LibreOffice from drawing Chinese
    was an unconfigured fontconfig, which raven now configures per conversion.
    Fetching 8MB here would fix nothing and would still be fetched on every
    install, because the file it writes is not what the renderer was short of.
    """
    text = INSTALL_SH.read_text(encoding="utf-8")
    home = tmp_path / "home"
    (home / "Library" / "Fonts").mkdir(parents=True)
    root = tmp_path / "System" / "Library" / "Fonts"
    (root / "Supplemental").mkdir(parents=True)
    (root / "Supplemental" / "Arial Unicode.ttf").write_bytes(b"true placeholder")

    assert _han_font_check(text, node_os="darwin", home=home, macos_root=root, fc_list=None) == 0


def test_a_mac_is_not_talked_out_of_its_own_faces_by_homebrews_fc_list(tmp_path: Path) -> None:
    """fc-list arrives with plenty of brew formulae and describes a configuration
    the converter never reads, so its answer says nothing about this Mac either
    way. Consulting it is how a Mac stripped of its own faces gets told it has
    one, and the deck then renders as boxes with nothing reporting it."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    home = tmp_path / "home"
    (home / "Library" / "Fonts").mkdir(parents=True)
    bare = tmp_path / "System" / "Library" / "Fonts"
    bare.mkdir(parents=True)

    fc_list = tmp_path / "bin" / "fc-list"
    assert _han_font_check(text, node_os="darwin", home=home, macos_root=bare, fc_list=fc_list) != 0


def test_the_installer_and_the_renderer_agree_on_what_a_mac_already_has() -> None:
    """Two answers to one question, and a disagreement is silent both ways: a
    face the installer counts but the renderer does not leaves a Mac with no
    download and no Chinese, and one the renderer counts but the installer does
    not fetches 8MB that were never needed."""
    from raven.utils import fonts

    text = INSTALL_SH.read_text(encoding="utf-8")
    checked = re.findall(r'\[ -f "\$MACOS_FONT_ROOT(/[^"]+)" \]', text)
    assert checked, "install.sh no longer checks any of the faces macOS ships"

    root = "/System/Library/Fonts"
    assert [f"{root}{suffix}" for suffix in checked] == [
        path for path in fonts._SYSTEM_HAN_FACES if path.startswith(root)
    ]
