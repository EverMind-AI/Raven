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

import contextlib
import http.server
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

INSTALL_SH = Path(__file__).resolve().parents[1] / "install.sh"
INSTALL_PS1 = Path(__file__).resolve().parents[1] / "install.ps1"

# Tests that run install.sh code under sh. Git Bash can run them on Windows, but
# not as a Linux or Mac host would: its curl sits outside /usr/bin and the
# script's paths mix separators with Windows ones, so a harness there fails or
# passes for the wrong reason.
POSIX_SH_ONLY = pytest.mark.skipif(sys.platform == "win32" or shutil.which("sh") is None, reason="POSIX sh only")


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


@POSIX_SH_ONLY
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
    started_in = tmp_path / "pwd.log"
    # `uv tool dir --bin` names the shim directory; the fake answers with ours.
    (bin_dir / "uv").write_text(f"#!/bin/sh\nprintf '%s' '{bin_dir}'\n", encoding="utf-8")
    (bin_dir / "raven").write_text(
        "#!/bin/sh\n"
        f"echo \"$*\" >> '{log}'\n"
        f"[ \"$1:$2\" = web:--foreground ] && pwd >> '{started_in}'\n"
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
        started_in.unlink(missing_ok=True)
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
    # pytest runs this from the repository root, which is exactly the source
    # checkout the launch must not start the page from.
    assert started_in.read_text(encoding="utf-8").strip() == str(tmp_path), "the page starts from HOME"

    code, _out, err, calls = run("stop-fails")
    assert code == 0
    assert calls[-1] == "web --foreground", "a failed --stop is a warning, not the end of the launch"
    assert "could not stop" in err

    code, _out, err, calls = run("interrupted")
    assert code == 0, "the page's own exit code is not the script's"
    assert calls[-1] == "web --foreground"
    assert "130" in err and "raven web" in err


def test_the_launch_leaves_the_working_directory_before_starting_the_page() -> None:
    """`raven web` starts its engine as `python -m raven`, which puts the working
    directory first on sys.path -- so an install run from inside a source
    checkout would bring the release up on that checkout's raven. Both
    installers move to the home directory first. The PowerShell one moves back
    afterwards: under `irm | iex` it runs in the caller's own shell."""
    sh = INSTALL_SH.read_text(encoding="utf-8")
    launch = sh[sh.index("launch_web() {") :]
    assert launch.index('cd "${HOME:-/}"') < launch.index('"$bin" web --stop')
    ps1 = INSTALL_PS1.read_text(encoding="utf-8")
    start = ps1[ps1.index("function Start-Web") :]
    assert start.index("Push-Location $HOME") < start.index("& $bin web --stop")
    assert start.index("Pop-Location") > start.index("& $bin web --foreground")


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
    """The page bundle imports i18n/messages.json and ui-shared/*.json, so a
    catalogue-only change is a page change -- watching ui-web alone would miss
    it. The pin is the whole call: a substring pin keeps passing when a source
    is appended, which is how the shared catalogues were missed."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert 'is_stale "$src/ui-web/dist/index.html" "$src/ui-web" "$src/i18n" "$src/ui-shared"' in text


def test_the_staleness_walk_prunes_what_the_build_itself_writes() -> None:
    """node_modules is rewritten by this script's own `npm ci` and dist holds
    the artifact being judged: left in the walk, either makes every artifact
    permanently stale and every re-run a full rebuild."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    walk = text[text.index("is_stale() {") : text.index("build_web_assets() {")]
    assert r"\( -name node_modules -o -name dist -o -name .modern \) -prune -o" in walk


@POSIX_SH_ONLY
def test_is_stale_answers_each_case_it_exists_for(tmp_path: Path) -> None:
    """The text pins above cannot tell a working mtime comparison from a broken
    one, and this is the half that decides whether a rebuild happens at all."""
    body = re.search(r"^is_stale\(\) \{.*?^\}$", INSTALL_SH.read_text(encoding="utf-8"), re.S | re.M)
    assert body is not None
    harness = tmp_path / "harness.sh"
    harness.write_text(
        body.group(0) + '\nif is_stale "$@"; then echo STALE; else echo FRESH; fi\n',
        encoding="utf-8",
    )

    web, catalog, shared = tmp_path / "ui-web", tmp_path / "i18n", tmp_path / "ui-shared"
    (web / "src").mkdir(parents=True)
    (web / "node_modules").mkdir()
    (web / ".modern").mkdir()
    (web / "dist").mkdir()
    catalog.mkdir()
    shared.mkdir()
    artifact = web / "dist" / "index.html"

    # An explicit clock, not sleeps: every step stamps a second further out than
    # the last, so the comparison under test is the only thing being measured.
    epoch = time.time()

    def stamp(path: Path, tick: int, body: str = "x") -> None:
        path.write_text(body, encoding="utf-8")
        os.utime(path, (epoch + tick, epoch + tick))

    def verdict() -> str:
        r = subprocess.run(
            ["sh", str(harness), str(artifact), str(web), str(catalog), str(shared)],
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
    stamp(shared / "verbs.json", 7)
    assert verdict() == "STALE", "a touched shared catalogue must rebuild"

    stamp(artifact, 8)
    for junk in (web / "node_modules" / "x", web / ".modern" / "modern.iife.js", web / "dist" / "extra.css"):
        stamp(junk, 9)
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


@POSIX_SH_ONLY
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


# --- The private Node runtime is verified before it is extracted -------------
# provision_private_node downloads a binary the installer then runs, so every
# way of failing to verify it has to end where a mismatch does: nothing is
# extracted. Each case runs the real function against a fake nodejs.org that
# publishes one release and answers only that release's exact URLs.

_NODE_STEP = (
    "sha256_of",
    "mirrors_allowed",
    "connection_failed",
    "reachable",
    "choose_node_dist",
    "latest_node_v22",
    "provision_private_node",
)
_NODE_SETTINGS = ("MIN_NODE_MAJOR", "NODE_DIST", "NODE_MIRROR", "NODE_FROM")
_NODE_PKG = "node-v22.99.0-linux-x64"
# The same package as .tar.xz sits first: a lookup that matches a prefix
# instead of the whole name reads the wrong digest.
_OTHER_ENTRIES = f"{'1' * 64}  {_NODE_PKG}.tar.xz\n{'2' * 64}  node-v22.99.0-darwin-arm64.tar.gz\n"


def _have(hidden: tuple[str, ...] = ()) -> str:
    """A shell `have` that reports the named tools as missing."""
    if not hidden:
        return 'have() { command -v "$1" >/dev/null 2>&1; }\n'
    return f'have() {{ case "$1" in {"|".join(hidden)}) return 1 ;; esac; command -v "$1" >/dev/null 2>&1; }}\n'


def _node_release(tmp_path: Path) -> tuple[Path, str]:
    """Publish v22.99.0 the way nodejs.org lays a release out; return the
    directory serving it and the tarball's digest."""
    import hashlib
    import io
    import tarfile

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.json").write_text('[{"version":"v22.99.0","lts":"Jod"}]', encoding="utf-8")
    node = b"#!/bin/sh\necho v22.99.0\n"
    entry = tarfile.TarInfo(f"{_NODE_PKG}/bin/node")
    entry.size, entry.mode, entry.mtime = len(node), 0o755, int(time.time())
    with tarfile.open(dist / "node.tar.gz", "w:gz") as tar:
        tar.addfile(entry, io.BytesIO(node))
    return dist, hashlib.sha256((dist / "node.tar.gz").read_bytes()).hexdigest()


def _fake_curl(
    bin_dir: Path,
    served: dict[str, Path],
    *,
    down: tuple[str, ...] = (),
    codes: dict[str, int] | None = None,
    tunnels: dict[str, tuple[str, int]] | None = None,
    log: Path | None = None,
) -> None:
    """A curl that answers each URL in ``served`` from its file and every other
    URL with an HTTP error (exit 22), never connects to a URL under ``down``
    (exit 7, the code for a connection that was refused or timed out), and
    ends with the given code for a URL under a prefix in ``codes``. A URL
    under a prefix in ``tunnels`` gets a proxy's refusal of the tunnel: the
    status it answered, as %{http_connect} writes it, and curl's exit code."""
    refused = "".join(f"  '{prefix}'*) exit {code} ;;\n" for prefix, code in (codes or {}).items())
    # The status is written only where %{http_connect} asks for it, as curl does.
    refused += "".join(
        f"  '{prefix}'*) case \"$wout\" in *http_connect*) printf '%s' {status} ;; esac; exit {code} ;;\n"
        for prefix, (status, code) in (tunnels or {}).items()
    )
    refused += "".join(f"  '{prefix}'*) exit 7 ;;\n" for prefix in down)
    answers = "".join(f"  '{url}') src='{path}' ;;\n" for url, path in served.items())
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "curl").write_text(
        "#!/bin/sh\n"
        'out=""\nwout=""\n'
        'while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2 ;; -w) wout="$2"; shift 2 ;; '
        'http*|file*) url="$1"; shift ;; *) shift ;; esac; done\n'
        + (f"echo \"$url\" >> '{log}'\n" if log is not None else "")
        + 'case "$url" in\n'
        + refused
        + "  *) ;;\nesac\n"
        + 'case "$url" in\n'
        + answers
        + "  *) exit 22 ;;\n"
        "esac\n"
        '[ -f "$src" ] || exit 22\n'
        'if [ -n "$out" ]; then cp "$src" "$out"; else cat "$src"; fi\n',
        encoding="utf-8",
    )
    (bin_dir / "curl").chmod(0o755)


def _sh_functions(names: tuple[str, ...], settings: tuple[str, ...] = ()) -> str:
    """The named functions and top-level assignments, as install.sh spells them."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    lines = []
    for name in settings:
        match = re.search(rf"^{name}=.*$", text, re.M)
        assert match is not None, name
        lines.append(match.group(0))
    for name in names:
        match = re.search(rf"^{name}\(\) \{{.*?^\}}$", text, re.S | re.M)
        assert match is not None, name
        lines.append(match.group(0))
    return "\n".join(lines) + "\n"


def _without_the_machines_etc(functions: str, tmp_path: Path) -> str:
    """``functions`` with the last place uv reads a machine uv.toml from moved
    under ``tmp_path``: a /etc/uv/uv.toml on the box running the suite would
    otherwise decide these cases."""
    read_last = "${XDG_CONFIG_DIRS:-/etc/xdg} /etc"
    assert functions.count(read_last) == 1, "uv_config_files no longer reads /etc where this expects"
    return functions.replace(read_last, f"${{XDG_CONFIG_DIRS:-/etc/xdg}} {shlex.quote(str(tmp_path / 'etc'))}")


_SH_PREAMBLE = (
    "set -eu\n"
    "info() { printf 'INFO %s\\n' \"$1\"; }\n"
    "ok() { printf 'OK %s\\n' \"$1\"; }\n"
    "warn() { printf 'WARN %s\\n' \"$1\" >&2; }\n"
    "die() { printf 'DIE %s\\n' \"$1\" >&2; exit 1; }\n"
)


def _run_node_step(
    tmp_path: Path,
    dist: Path,
    listing: str | None,
    *,
    hidden: tuple[str, ...] = (),
    base: str = "https://nodejs.org/dist",
    down: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
):
    """Run provision_private_node with `listing` as the release's SHASUMS256.txt
    (None: the file cannot be fetched), the release served under `base` and no
    connection reaching a URL under `down`. Returns the result, the runtime
    directory, and the directory its downloads are staged in."""
    if listing is not None:
        (dist / "SHASUMS256.txt").write_text(listing, encoding="utf-8")
    harness = tmp_path / "node-step.sh"
    harness.write_text(
        _SH_PREAMBLE + _have(hidden) + _sh_functions(_NODE_STEP, _NODE_SETTINGS) + "provision_private_node\n",
        encoding="utf-8",
    )
    bin_dir = tmp_path / "bin"
    served = {
        f"{base}/index.json": dist / "index.json",
        f"{base}/v22.99.0/SHASUMS256.txt": dist / "SHASUMS256.txt",
        f"{base}/v22.99.0/{_NODE_PKG}.tar.gz": dist / "node.tar.gz",
    }
    _fake_curl(bin_dir, served, down=down)
    runtime, staging = tmp_path / "runtime", tmp_path / "staging"
    staging.mkdir()
    result = subprocess.run(
        ["sh", str(harness)],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "TMPDIR": str(staging),
            "NODE_RUNTIME_DIR": str(runtime),
            "NODE_OS": "linux",
            "NODE_ARCH": "x64",
            **(env or {}),
        },
    )
    return result, runtime, staging


@POSIX_SH_ONLY
def test_a_node_download_that_matches_its_published_digest_is_installed(tmp_path: Path) -> None:
    dist, digest = _node_release(tmp_path)
    result, runtime, staging = _run_node_step(tmp_path, dist, _OTHER_ENTRIES + f"{digest}  {_NODE_PKG}.tar.gz\n")

    assert result.returncode == 0, result.stderr
    node = runtime / _NODE_PKG / "bin" / "node"
    assert node.is_file() and os.access(node, os.X_OK)
    assert list(staging.iterdir()) == [], "the download is not left behind"


@POSIX_SH_ONLY
@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("digest-mismatch", "mismatch"),
        ("checksums-unreachable", "SHASUMS256.txt"),
        ("package-not-listed", f"{_NODE_PKG}.tar.gz"),
        ("no-hash-tool", "sha256sum"),
    ],
)
def test_a_node_download_that_cannot_be_verified_is_never_extracted(tmp_path: Path, case: str, reason: str) -> None:
    """A missing checksum file, a missing entry, and a missing hash tool each
    leave the download unverified, and an unverified binary is one the
    installer must not run. Each stops the install with its own reason."""
    dist, digest = _node_release(tmp_path)
    listing = {
        "digest-mismatch": _OTHER_ENTRIES + f"{'0' * 64}  {_NODE_PKG}.tar.gz\n",
        "checksums-unreachable": None,
        "package-not-listed": _OTHER_ENTRIES,
        "no-hash-tool": _OTHER_ENTRIES + f"{digest}  {_NODE_PKG}.tar.gz\n",
    }[case]
    hidden = ("sha256sum", "shasum") if case == "no-hash-tool" else ()
    result, runtime, staging = _run_node_step(tmp_path, dist, listing, hidden=hidden)

    assert result.returncode != 0 and "DIE " in result.stderr, result.stdout + result.stderr
    assert reason in result.stderr
    assert list(runtime.iterdir()) == [], "nothing unverified is extracted"
    assert list(staging.iterdir()) == [], "the download is not left behind"


# The Windows twin runs the real Install-PrivateNode and the real Fail, parsed
# out of install.ps1, against the same kind of fake release. Fail raises under
# the script's ErrorActionPreference, so a Fail inside a try is caught by that
# try's own catch -- the mismatch case is the one that sees it.

_PS1_PKG = "node-v22.99.0-win-x64"

# Parses install.ps1 and hands back the named functions as written there. The
# caller defines them with Invoke-Expression at script scope: one defined inside
# a function would be gone when that function returned.
_PS1_DEFINE = r"""
$ast = [System.Management.Automation.Language.Parser]::ParseFile($Ps1, [ref]$null, [ref]$null)
function Get-Install([string[]]$Names) {
    foreach ($name in $Names) {
        $fn = $ast.Find({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq $name }, $true)
        if (-not $fn) { throw "install.ps1 defines no $name" }
        $fn.Extent.Text
    }
}
"""

# A web that answers `$Served` (URL -> file under $WebRoot), refuses every
# connection to a URL under `$Down`, fails the certificate check of a URL under
# `$Untrusted` (as PowerShell 7 reports it), and answers anything else with a
# 404.
_PS1_FAKE_WEB = r"""
function New-Refusal([string]$Uri) {
    [System.Net.Http.HttpRequestException]::new("No connection could be made to $Uri")
}
function New-NotFound([string]$Uri) {
    $failure = [System.Exception]::new("404 Not Found: $Uri")
    $failure | Add-Member -NotePropertyName Response -NotePropertyValue "404"
    $failure
}
function New-Untrusted([string]$Uri) {
    [System.Net.Http.HttpRequestException]::new([System.Net.Http.HttpRequestError]::SecureConnectionError, "The SSL connection could not be established: $Uri", $null, $null)
}
function Invoke-WebRequest {
    param([Parameter(Position = 0)][string]$Uri, [string]$OutFile, [switch]$UseBasicParsing, [string]$Method, [int]$TimeoutSec, [switch]$SkipCertificateCheck)
    foreach ($prefix in $Down) { if ($prefix -and $Uri.StartsWith($prefix)) { throw (New-Refusal $Uri) } }
    foreach ($prefix in $Untrusted) { if ($prefix -and $Uri.StartsWith($prefix) -and -not $SkipCertificateCheck) { throw (New-Untrusted $Uri) } }
    $file = $Served[$Uri]
    if (-not $file -or -not (Test-Path (Join-Path $WebRoot $file))) { throw (New-NotFound $Uri) }
    if ($OutFile) { Copy-Item (Join-Path $WebRoot $file) $OutFile; return }
    [pscustomobject]@{ Content = (Get-Content -Raw (Join-Path $WebRoot $file)) }
}
"""

_PS1_NODE_STEP = r"""
foreach ($text in (Get-Install @("Fail", "Install-PrivateNode", "Get-NodeDist", "Test-MirrorsAllowed", "Test-Reachable", "Get-TunnelStatus"))) {
    Invoke-Expression $text
}
function Write-Info([string]$Message) { }
function Write-Ok([string]$Message) { }
function Write-Warn([string]$Message) { [Console]::Out.WriteLine("WARN $Message") }
function Get-NodeArch { "x64" }
function Get-LatestNodeV22([string]$From) { "v22.99.0" }
function Test-NodeOk([string]$Path) { Test-Path $Path }
function Add-ProcessPath([string]$PathToAdd) { }
$Served = @{
    "$Base/index.json" = "index.json"
    "$Base/v22.99.0/node-v22.99.0-win-x64.zip" = "node.zip"
    "$Base/v22.99.0/SHASUMS256.txt" = "SHASUMS256.txt"
}
$Down = @($DownList -split ",")
$NodeRuntimeDir = $Runtime
$null = Install-PrivateNode
"""


def _ps1_node_release(tmp_path: Path) -> tuple[Path, str]:
    import hashlib
    import zipfile

    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.json").write_text('[{"version":"v22.99.0","lts":"Jod"}]', encoding="utf-8")
    with zipfile.ZipFile(dist / "node.zip", "w") as archive:
        archive.writestr(f"{_PS1_PKG}/node.exe", "a node.exe that is not really one")
    return dist, hashlib.sha256((dist / "node.zip").read_bytes()).hexdigest()


def _ps1_settings(*names: str) -> str:
    text = INSTALL_PS1.read_text(encoding="utf-8")
    lines = []
    for name in names:
        match = re.search(rf"^\${name} = .*$", text, re.M)
        assert match is not None, name
        lines.append(match.group(0))
    return "\n".join(lines) + "\n"


def _run_ps1(
    tmp_path: Path, name: str, body: str, *, env: dict[str, str] | None = None, shell: str = "pwsh", **params: str
):
    """Run `body` under `shell` with install.ps1's functions within reach, each
    of `params` handed in as a string parameter of the same name."""
    harness = tmp_path / f"{name}.ps1"
    names = ["Ps1", *params]
    harness.write_text(
        "param(" + ", ".join(f"[string]${n}" for n in names) + ")\n" + _PS1_DEFINE + body,
        encoding="utf-8",
    )
    staging = tmp_path / "staging"
    staging.mkdir(exist_ok=True)
    argv = [shutil.which(shell), "-NoProfile", "-NonInteractive", "-File", str(harness), "-Ps1", str(INSTALL_PS1)]
    for key, value in params.items():
        argv += [f"-{key}", value]
    return subprocess.run(
        argv,
        capture_output=True,
        # A localized pwsh writes its own messages in the console code page.
        encoding="utf-8",
        errors="replace",
        check=False,
        env={
            "PATH": os.environ.get("PATH", ""),
            # A trimmed environment loses the system directory and the executable
            # extensions with it: without PATHEXT the child pwsh reads a bare
            # powershell.exe as a document and refuses to run it in a pipeline.
            "PATHEXT": os.environ.get("PATHEXT", ""),
            "SYSTEMROOT": os.environ.get("SYSTEMROOT", os.environ.get("WINDIR", "")),
            "WINDIR": os.environ.get("WINDIR", os.environ.get("SYSTEMROOT", "")),
            "PSModulePath": os.environ.get("PSModulePath", ""),
            "HOME": str(tmp_path),
            "TMPDIR": str(staging),
            "TMP": str(staging),
            "TEMP": str(staging),
            # pwsh phones home on startup unless told not to; the suite stays offline.
            "POWERSHELL_TELEMETRY_OPTOUT": "1",
            "POWERSHELL_UPDATECHECK": "Off",
            **(env or {}),
        },
    )


def _run_ps1_node_step(
    tmp_path: Path,
    dist: Path,
    listing: str | None,
    *,
    base: str = "https://nodejs.org/dist",
    down: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
):
    if listing is not None:
        # LF as published: a Windows text write would end every line in a carriage return.
        (dist / "SHASUMS256.txt").write_bytes(listing.encode("utf-8"))
    runtime, staging = tmp_path / "runtime", tmp_path / "staging"
    runtime.mkdir()
    staging.mkdir()
    settings = _ps1_settings("ErrorActionPreference", "MinNodeMajor", "NodeDist", "NodeMirror")
    result = _run_ps1(
        tmp_path,
        "node-step",
        settings + _PS1_FAKE_WEB + _PS1_NODE_STEP,
        env=env,
        WebRoot=str(dist),
        Runtime=str(runtime),
        Base=base,
        DownList=",".join(down),
    )
    return result, runtime, staging


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
def test_a_windows_node_download_that_matches_its_published_digest_is_installed(tmp_path: Path) -> None:
    dist, digest = _ps1_node_release(tmp_path)
    result, runtime, staging = _run_ps1_node_step(tmp_path, dist, f"{digest}  {_PS1_PKG}.zip\n")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (runtime / _PS1_PKG / "node.exe").is_file()
    assert list(staging.iterdir()) == [], "the download is not left behind"


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
@pytest.mark.parametrize(
    ("case", "reason"),
    [
        ("digest-mismatch", "mismatch"),
        ("checksums-unreachable", "SHASUMS256.txt"),
        ("package-not-listed", f"{_PS1_PKG}.zip"),
    ],
)
def test_a_windows_node_download_that_cannot_be_verified_is_never_extracted(
    tmp_path: Path, case: str, reason: str
) -> None:
    dist, digest = _ps1_node_release(tmp_path)
    listing = {
        "digest-mismatch": f"{digest}  {_PS1_PKG}.7z\n{'0' * 64}  {_PS1_PKG}.zip\n",
        "checksums-unreachable": None,
        "package-not-listed": f"{digest}  {_PS1_PKG}.7z\n",
    }[case]
    result, runtime, staging = _run_ps1_node_step(tmp_path, dist, listing)

    assert result.returncode != 0, result.stdout + result.stderr
    assert reason in result.stdout + result.stderr
    assert list(runtime.iterdir()) == [], "nothing unverified is extracted"
    assert list(staging.iterdir()) == [], "the download is not left behind"


# --- Node from the mirror when nodejs.org cannot be reached ------------------
# The release, its index and its checksums all come from the one host that
# answered: the check then catches a corrupt or truncated download, which is
# what it ever caught, since the tarball and the checksums shared an origin.

_NODE_MIRROR = "https://cdn.npmmirror.com/binaries/node"


@POSIX_SH_ONLY
def test_node_comes_from_the_mirror_when_nodejs_org_cannot_be_reached(tmp_path: Path) -> None:
    dist, digest = _node_release(tmp_path)
    result, runtime, staging = _run_node_step(
        tmp_path, dist, f"{digest}  {_NODE_PKG}.tar.gz\n", base=_NODE_MIRROR, down=("https://nodejs.org/",)
    )

    assert result.returncode == 0, result.stderr
    assert (runtime / _NODE_PKG / "bin" / "node").is_file()
    assert "npmmirror.com" in result.stderr and "RAVEN_NO_MIRROR" in result.stderr, "the switch is announced"
    assert list(staging.iterdir()) == []


@POSIX_SH_ONLY
def test_the_opt_out_keeps_node_on_nodejs_org(tmp_path: Path) -> None:
    dist, digest = _node_release(tmp_path)
    result, runtime, _staging = _run_node_step(
        tmp_path,
        dist,
        f"{digest}  {_NODE_PKG}.tar.gz\n",
        base=_NODE_MIRROR,
        down=("https://nodejs.org/",),
        env={"RAVEN_NO_MIRROR": "1"},
    )

    assert result.returncode != 0 and "DIE " in result.stderr
    assert list(runtime.iterdir()) == []


@POSIX_SH_ONLY
def test_a_nodejs_org_that_answers_is_never_swapped_for_the_mirror(tmp_path: Path) -> None:
    """Served from nodejs.org, which answers; the mirror serves nothing here, so
    a run that asked it would fail."""
    dist, digest = _node_release(tmp_path)
    result, runtime, _staging = _run_node_step(tmp_path, dist, f"{digest}  {_NODE_PKG}.tar.gz\n")

    assert result.returncode == 0, result.stderr
    assert "npmmirror" not in result.stderr


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
def test_a_windows_node_comes_from_the_mirror_when_nodejs_org_cannot_be_reached(tmp_path: Path) -> None:
    dist, digest = _ps1_node_release(tmp_path)
    result, runtime, staging = _run_ps1_node_step(
        tmp_path, dist, f"{digest}  {_PS1_PKG}.zip\n", base=_NODE_MIRROR, down=("https://nodejs.org/",)
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert (runtime / _PS1_PKG / "node.exe").is_file()
    assert "npmmirror.com" in result.stdout and "RAVEN_NO_MIRROR" in result.stdout, "the switch is announced"
    assert list(staging.iterdir()) == []


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
def test_the_windows_opt_out_keeps_node_on_nodejs_org(tmp_path: Path) -> None:
    dist, digest = _ps1_node_release(tmp_path)
    result, runtime, _staging = _run_ps1_node_step(
        tmp_path,
        dist,
        f"{digest}  {_PS1_PKG}.zip\n",
        base=_NODE_MIRROR,
        down=("https://nodejs.org/",),
        env={"RAVEN_NO_MIRROR": "1"},
    )

    assert result.returncode != 0, result.stdout + result.stderr
    assert list(runtime.iterdir()) == []


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


def _installer_job() -> str:
    """The installer job's text in ci.yml, up to the job after it."""
    workflow = (Path(__file__).resolve().parents[1] / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    job = workflow[workflow.index("  installer:") :]
    return job[: job.index("\n  windows-upgrade:")]


def _step_value(step: str, pattern: str) -> str:
    found = re.search(pattern, step)
    assert found, f"{pattern!r} matches nothing in the step {step.splitlines()[0]!r}"
    return found.group(1)


def test_the_ci_gate_installs_the_latest_release_the_way_users_do() -> None:
    """The text pins above cannot catch the class of defect that shipped: a
    script on main calling something the latest release lacks. Only a real
    install of that release with this script can, so CI does one -- piped, as
    `curl | sh` and `irm | iex` arrive, which is what selects remote mode; run
    as a file, the script would detect the checkout and install it editable
    instead, and the gate would be measuring the wrong thing."""
    job = _installer_job()
    assert "cat install.sh | sh" in job
    assert "Get-Content install.ps1 -Raw | Invoke-Expression" in job
    assert 'RAVEN_NO_LAUNCH: "1"' in job
    assert 'raven.exe" --version' in job and '"$UV_TOOL_BIN_DIR/raven" --version' in job


def test_the_ci_gate_runs_the_windows_install_under_both_powershells() -> None:
    """Windows PowerShell 5.1 is the shell every Windows ships with, pwsh is the
    one CI reaches for, and their web cmdlets fail differently. A release
    lookup that only worked the pwsh way passed this gate while every install
    from the stock shell failed, so the gate runs both -- each into its own
    tool, bin and home directories. The version check runs the raven.exe in
    UV_TOOL_BIN_DIR, so a bin directory both installs shared would let one
    shell's install answer for the other's."""
    steps = [
        step
        for step in _installer_job().split("\n      - ")
        if "Get-Content install.ps1 -Raw | Invoke-Expression" in step
    ]
    assert sorted(_step_value(step, r"\n        shell: (\S+)") for step in steps) == ["powershell", "pwsh"]
    for name in ("UV_TOOL_DIR", "UV_TOOL_BIN_DIR", "RAVEN_HOME"):
        values = {_step_value(step, rf"\n          {name}: (.+)") for step in steps}
        assert len(values) == 2, f"both PowerShell installs share one {name}"


def test_the_windows_installer_is_where_this_tripwire_thinks_it_is() -> None:
    assert INSTALL_PS1.is_file()


def test_the_windows_release_lookup_reads_the_redirect_in_both_powershells() -> None:
    """The lookup stops at the release page redirect and reads its Location.
    Windows PowerShell 5.1 returns that redirect as the response and reports
    the exceeded redirect count as an error with no response attached, so the
    error has to be ignored: raised and caught instead, it left the catch
    nothing to read, and every install from the stock shell ended in "Could not
    resolve the latest Raven release wheel". pwsh raises whatever -ErrorAction
    says, so the catch still reads the exception's response."""
    text = INSTALL_PS1.read_text(encoding="utf-8")
    lookup = text[text.index("function Resolve-RavenLatestVersion") : text.index("function Resolve-RavenWheel")]
    probe = next(line for line in lookup.splitlines() if "-MaximumRedirection 0" in line)
    assert "-ErrorAction Ignore" in probe
    assert "$target = [string]$response.Headers.Location" in lookup
    assert "$failed = $_.Exception.Response" in lookup


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
    sources, and give the page every shared catalogue it inlines -- the pin is
    each source directory by name, since membership is what was missed."""
    text = INSTALL_PS1.read_text(encoding="utf-8")
    assert "function Test-AssetStale" in text
    assert "$needTui = Test-AssetStale" in text
    assert "$needPage = Test-AssetStale" in text
    assert '(Join-Path $ScriptDir "i18n")' in text
    assert '(Join-Path $ScriptDir "ui-shared")' in text
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


# --- the Chinese-font step --------------------------------------------------
# Every case below is a box on a rendered page when it goes wrong, and none of
# them raises: the conversion succeeds, the PDF is well formed, and only whoever
# looks at the picture finds out. So each branch runs for real, against files.

_FONT_STEP = (
    "sha256_of",
    "mirrors_allowed",
    "linux_has_han_face",
    "install_linux_cjk_font",
    "install_cjk_fonts",
)


def _font_step_harness(tmp_path: Path, *, office: bool = True, hidden: tuple[str, ...] = (), **overrides: str) -> Path:
    text = INSTALL_SH.read_text(encoding="utf-8")
    bodies = []
    for name in _FONT_STEP:
        match = re.search(rf"^{name}\(\) \{{.*?^\}}$", text, re.S | re.M)
        assert match is not None, name
        bodies.append(match.group(0))
    names = ("HAN_FONT_NAME",)
    settings = [re.search(rf"^{name}=.*$", text, re.M).group(0) for name in names]
    settings += [f"{name}='{value}'" for name, value in overrides.items()]
    harness = tmp_path / "font-step.sh"
    harness.write_text(
        "info() { :; }\n"
        "ok() { printf 'OK %s\\n' \"$1\"; }\n"
        "warn() { printf 'WARN %s\\n' \"$1\" >&2; }\n"
        + _have(hidden if office else (*hidden, "soffice", "libreoffice"))
        + "\n".join(settings)
        + "\n"
        + "\n".join(bodies)
        + "\ninstall_cjk_fonts\n",
        encoding="utf-8",
    )
    return harness


def _run_font_step(tmp_path: Path, harness: Path, *, os_name: str, fc_list: str = "", tools: tuple[str, ...] = ()):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    (bin_dir / "fc-list").write_text(f"#!/bin/sh\nprintf '%s' '{fc_list}'\n", encoding="utf-8")
    (bin_dir / "fc-cache").write_text(f"#!/bin/sh\necho \"fc-cache $*\" >> '{log}'\n", encoding="utf-8")
    for tool in ("fc-list", "fc-cache"):
        (bin_dir / tool).chmod(0o755)
    for tool in tools:
        (bin_dir / tool).write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        (bin_dir / tool).chmod(0o755)
    home = tmp_path / "home"
    cached = home / ".raven" / "cache" / "pdf-preview" / "old-render.pdf"
    cached.parent.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(b"%PDF drawn without a Han face")
    result = subprocess.run(
        ["sh", str(harness)],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(home),
            "RAVEN_HOME": str(home / ".raven"),
            "XDG_DATA_HOME": str(home / "share"),
            "NODE_OS": os_name,
            "RAVEN_MACOS_APPS": str(tmp_path / "Applications"),
        },
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
    return result, calls, cached, home


def _published_face(tmp_path: Path, payload: bytes = b"OTTO a face that is not really one") -> dict[str, str]:
    """The face where GitHub publishes it, and the same bytes on the mirror."""
    import hashlib

    source = tmp_path / "upstream" / "NotoSansSC-Regular.otf"
    mirror = tmp_path / "mirror" / "NotoSansSC-Regular.otf"
    for path in (source, mirror):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    return {
        "HAN_FONT_URL": source.as_uri(),
        "HAN_FONT_MIRROR_URL": mirror.as_uri(),
        "HAN_FONT_SHA256": hashlib.sha256(payload).hexdigest(),
        "HAN_FONT_BYTES": str(len(payload)),
    }


def test_the_font_step_is_skippable_and_runs_before_the_launch() -> None:
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert "install_cjk_fonts() {" in text
    assert '[ -n "${RAVEN_MINIMAL:-}" ] || install_cjk_fonts' in text
    main_body = text[text.index("main() {") :]
    assert main_body.index("install_cjk_fonts") < main_body.index("launch_web")


@POSIX_SH_ONLY
def test_a_linux_host_with_a_han_face_downloads_nothing_and_keeps_its_previews(tmp_path: Path) -> None:
    """LibreOffice reads the same fontconfig as fc-list on Linux, so a Chinese
    family listed there is one a page is drawn with."""
    harness = _font_step_harness(tmp_path, **_published_face(tmp_path))
    result, calls, cached, home = _run_font_step(
        tmp_path, harness, os_name="linux", fc_list="Noto Sans CJK SC\n", tools=("soffice",)
    )

    assert result.returncode == 0, result.stderr
    assert not (home / "share" / "fonts").exists()
    assert cached.is_file(), "previews drawn with a Han face are not thrown away"
    assert calls == []


@POSIX_SH_ONLY
def test_a_linux_host_without_one_gets_the_face_and_loses_its_stale_previews(tmp_path: Path) -> None:
    harness = _font_step_harness(tmp_path, **_published_face(tmp_path))
    result, calls, cached, home = _run_font_step(tmp_path, harness, os_name="linux", tools=("soffice",))

    installed = home / "share" / "fonts" / "NotoSansSC-Regular.otf"
    assert result.returncode == 0, result.stderr
    assert installed.is_file()
    assert calls == [f"fc-cache -f {installed.parent}"], "fontconfig only sees a new face once its cache is rebuilt"
    assert not cached.exists(), "a preview cached before the face existed shows boxes forever"


@POSIX_SH_ONLY
def test_a_download_that_does_not_match_its_digest_is_not_installed(tmp_path: Path) -> None:
    """A truncated or substituted OTF still parses and draws nothing, which is
    exactly the failure the step is for -- so a mismatch installs nothing."""
    pins = _published_face(tmp_path)
    pins["HAN_FONT_SHA256"] = "0" * 64
    harness = _font_step_harness(tmp_path, **pins)
    result, _calls, cached, home = _run_font_step(tmp_path, harness, os_name="linux", tools=("soffice",))

    fonts_dir = home / "share" / "fonts"
    assert result.returncode == 0, "a failed download is a warning, not the end of the install"
    assert list(fonts_dir.iterdir()) == [], "neither the face nor its partial download is left behind"
    assert "package: fonts-noto-cjk" in result.stderr, "the hint names the package, not one distro's command"
    assert "apt-get" not in result.stderr
    assert cached.is_file()


@POSIX_SH_ONLY
def test_a_download_that_cannot_be_hashed_is_not_installed(tmp_path: Path) -> None:
    """With neither sha256sum nor shasum there is no digest to compare, and an
    unchecked download is the truncated face this step exists to keep out."""
    harness = _font_step_harness(tmp_path, hidden=("sha256sum", "shasum"), **_published_face(tmp_path))
    result, _calls, cached, home = _run_font_step(tmp_path, harness, os_name="linux", tools=("soffice",))

    assert result.returncode == 0, "a skipped face is a warning, not the end of the install"
    assert list((home / "share" / "fonts").iterdir()) == [], "an unchecked face is not installed"
    assert cached.is_file()


@POSIX_SH_ONLY
def test_a_mac_is_left_alone_by_the_font_step(tmp_path: Path) -> None:
    """A Mac already ships Han faces, and raven links them into LibreOffice's
    profiles itself (raven/utils/office.py): no download, no password, and no
    file written outside the user's home."""
    harness = _font_step_harness(tmp_path, **_published_face(tmp_path))
    result, calls, cached, home = _run_font_step(tmp_path, harness, os_name="darwin")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "" and result.stderr == ""
    assert calls == []
    assert not (home / "share" / "fonts").exists()
    assert cached.is_file()
    assert "/usr/local/etc/fonts" not in INSTALL_SH.read_text(encoding="utf-8")


# --- LibreOffice on a Mac without Homebrew ------------------------------------

_OFFICE_STEP = ("sha256_of", "mirrors_allowed", "write_soffice_launcher", "install_libreoffice_dmg", "install_office")


def _office_step_harness(tmp_path: Path, **overrides: str) -> Path:
    text = INSTALL_SH.read_text(encoding="utf-8")
    bodies = [re.search(rf"^{name}\(\) \{{.*?^\}}$", text, re.S | re.M).group(0) for name in _OFFICE_STEP]
    names = ("LO_VERSION", "LO_BUILD", "LO_SHA256_ARM64", "LO_SHA256_X64", "LO_MIRROR", "MACOS_APPS")
    settings = [re.search(rf"^{name}=.*$", text, re.M).group(0) for name in names]
    settings += [f"{name}='{value}'" for name, value in overrides.items()]
    harness = tmp_path / "office-step.sh"
    harness.write_text(
        "set -eu\n"
        "info() { :; }\n"
        "ok() { printf 'OK %s\\n' \"$1\"; }\n"
        "warn() { printf 'WARN %s\\n' \"$1\" >&2; }\n"
        # The host running the suite may have its own soffice; this Mac has none.
        'have() { case "$1" in soffice|libreoffice|brew) return 1 ;; esac; command -v "$1" >/dev/null 2>&1; }\n'
        + "\n".join(settings)
        + "\n"
        + "\n".join(bodies)
        + "\ninstall_office\n",
        encoding="utf-8",
    )
    return harness


def _run_office_step(
    tmp_path: Path,
    harness: Path,
    dmg: bytes = b"a dmg that is not really one",
    *,
    down: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    log = tmp_path / "calls.log"
    payload = tmp_path / "payload.dmg"
    payload.write_bytes(dmg)
    (bin_dir / "curl").write_text(
        "#!/bin/sh\n"
        'while [ $# -gt 0 ]; do case "$1" in -o) out="$2"; shift 2 ;; http*) url="$1"; shift ;; *) shift ;; esac; done\n'
        f"echo \"curl $url\" >> '{log}'\n"
        + "".join(f"case \"$url\" in '{prefix}'*) exit 7 ;; esac\n" for prefix in down)
        + f"cp '{payload}' \"$out\"\n",
        encoding="utf-8",
    )
    (bin_dir / "hdiutil").write_text(
        "#!/bin/sh\n"
        f"echo \"hdiutil $1\" >> '{log}'\n"
        '[ "$1" = attach ] || exit 0\n'
        'while [ $# -gt 0 ]; do case "$1" in -mountpoint) mnt="$2"; shift 2 ;; *) shift ;; esac; done\n'
        'mkdir -p "$mnt/LibreOffice.app/Contents/MacOS"\n'
        "printf '#!/bin/sh\\n' > \"$mnt/LibreOffice.app/Contents/MacOS/soffice\"\n"
        'chmod +x "$mnt/LibreOffice.app/Contents/MacOS/soffice"\n',
        encoding="utf-8",
    )
    (bin_dir / "ditto").write_text('#!/bin/sh\ncp -R "$1" "$2"\n', encoding="utf-8")
    for tool in ("curl", "hdiutil", "ditto"):
        (bin_dir / tool).chmod(0o755)
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    apps = tmp_path / "Applications"
    apps.mkdir(exist_ok=True)
    result = subprocess.run(
        ["sh", str(harness)],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(home),
            "TMPDIR": str(tmp_path),
            "NODE_OS": "darwin",
            "NODE_ARCH": "arm64",
            "RAVEN_MACOS_APPS": str(apps),
            **(env or {}),
        },
    )
    calls = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
    return result, calls, apps, home


@POSIX_SH_ONLY
def test_a_mac_without_homebrew_still_gets_libreoffice(tmp_path: Path) -> None:
    """Deck preview is the render-truth capability, and it was off on every Mac
    without Homebrew: the script only warned. The release dmg is fetched,
    verified, and copied in, with a launcher on PATH for the model's own soffice."""
    import hashlib

    dmg = b"the release dmg"
    harness = _office_step_harness(tmp_path, LO_SHA256_ARM64=hashlib.sha256(dmg).hexdigest())
    result, calls, apps, home = _run_office_step(tmp_path, harness, dmg)

    assert result.returncode == 0, result.stderr
    app = apps / "LibreOffice.app"
    assert (app / "Contents" / "MacOS" / "soffice").is_file()
    assert calls[0] == (
        "curl https://download.documentfoundation.org/libreoffice/stable/26.8.0/mac/aarch64/"
        "LibreOffice_26.8.0_MacOS_aarch64.dmg"
    )
    assert calls[1:] == ["hdiutil attach", "hdiutil detach"], "the image is detached again"
    launcher = home / ".local" / "bin" / "soffice"
    assert f'exec "{app}/Contents/MacOS/soffice" "$@"' in launcher.read_text(encoding="utf-8")
    assert os.access(launcher, os.X_OK)
    assert [p.name for p in tmp_path.glob("raven-libreoffice.*")] == [], "the download does not outlive the step"


@POSIX_SH_ONLY
def test_a_dmg_that_does_not_match_its_digest_is_never_mounted(tmp_path: Path) -> None:
    """An app copied from an image nobody verified is the one thing worse than
    no preview. Every source is tried, then the step gives up and says so."""
    harness = _office_step_harness(tmp_path, LO_SHA256_ARM64="0" * 64)
    result, calls, apps, home = _run_office_step(tmp_path, harness)

    assert result.returncode == 0, "a failed download is a warning, not the end of the install"
    assert [call.split("/")[2] for call in calls] == [
        "download.documentfoundation.org",
        "downloadarchive.documentfoundation.org",
        "mirrors.tuna.tsinghua.edu.cn",
    ]
    assert not (apps / "LibreOffice.app").exists()
    assert not (home / ".local" / "bin" / "soffice").exists()
    assert "libreoffice.org" in result.stderr


@POSIX_SH_ONLY
def test_a_dmg_comes_from_the_mirror_when_libreoffice_org_does_not_deliver(tmp_path: Path) -> None:
    """The digest is pinned, so a third source cannot change what is installed."""
    import hashlib

    dmg = b"the release dmg"
    harness = _office_step_harness(tmp_path, LO_SHA256_ARM64=hashlib.sha256(dmg).hexdigest())
    result, calls, apps, _home = _run_office_step(
        tmp_path, harness, dmg, down=("https://download.documentfoundation.org/", "https://downloadarchive.")
    )

    assert result.returncode == 0, result.stderr
    assert (apps / "LibreOffice.app" / "Contents" / "MacOS" / "soffice").is_file()
    assert calls[2] == (
        "curl https://mirrors.tuna.tsinghua.edu.cn/libreoffice/libreoffice/stable/26.8.0/mac/aarch64/"
        "LibreOffice_26.8.0_MacOS_aarch64.dmg"
    )


@POSIX_SH_ONLY
def test_the_opt_out_keeps_libreoffice_on_its_own_hosts(tmp_path: Path) -> None:
    import hashlib

    dmg = b"the release dmg"
    harness = _office_step_harness(tmp_path, LO_SHA256_ARM64=hashlib.sha256(dmg).hexdigest())
    _result, calls, apps, _home = _run_office_step(
        tmp_path,
        harness,
        dmg,
        down=("https://download.documentfoundation.org/", "https://downloadarchive."),
        env={"RAVEN_NO_MIRROR": "1"},
    )

    assert not (apps / "LibreOffice.app").exists()
    assert not any("tuna" in call for call in calls)


@POSIX_SH_ONLY
def test_the_face_comes_from_the_mirror_when_github_does_not_deliver(tmp_path: Path) -> None:
    """Pinned by size and digest, so a second source cannot change what is installed."""
    pins = _published_face(tmp_path)
    pins["HAN_FONT_URL"] = (tmp_path / "nowhere" / "NotoSansSC-Regular.otf").as_uri()
    harness = _font_step_harness(tmp_path, **pins)
    result, _calls, _cached, home = _run_font_step(tmp_path, harness, os_name="linux", tools=("soffice",))

    assert result.returncode == 0, result.stderr
    assert (home / "share" / "fonts" / "NotoSansSC-Regular.otf").is_file()


@POSIX_SH_ONLY
def test_the_opt_out_keeps_the_face_on_github(tmp_path: Path) -> None:
    pins = _published_face(tmp_path)
    pins["HAN_FONT_URL"] = (tmp_path / "nowhere" / "NotoSansSC-Regular.otf").as_uri()
    harness = _font_step_harness(tmp_path, RAVEN_NO_MIRROR="1", **pins)
    _result, _calls, _cached, home = _run_font_step(tmp_path, harness, os_name="linux", tools=("soffice",))

    assert not (home / "share" / "fonts" / "NotoSansSC-Regular.otf").exists()


@POSIX_SH_ONLY
def test_an_app_already_in_applications_gets_a_launcher_and_no_second_copy(tmp_path: Path) -> None:
    """The libreoffice.org dmg puts nothing on PATH. Downloading again would
    copy a second bundle over the one the user installed."""
    harness = _office_step_harness(tmp_path)
    existing = tmp_path / "Applications" / "LibreOffice.app" / "Contents" / "MacOS" / "soffice"
    existing.parent.mkdir(parents=True)
    existing.write_text("#!/bin/sh\n# theirs\n", encoding="utf-8")
    existing.chmod(0o755)
    result, calls, _apps, home = _run_office_step(tmp_path, harness)

    assert result.returncode == 0, result.stderr
    assert calls == [], "nothing is downloaded or mounted"
    assert "theirs" in existing.read_text(encoding="utf-8")
    assert (home / ".local" / "bin" / "soffice").is_file()


@POSIX_SH_ONLY
def test_a_launcher_that_cannot_be_written_does_not_abort_the_install(tmp_path: Path) -> None:
    """The step is best effort: an existing app whose launcher cannot be written
    (here ~/.local is a file, which blocks root as well) is a warning, and the
    installer carries on under set -e."""
    harness = _office_step_harness(tmp_path)
    existing = tmp_path / "Applications" / "LibreOffice.app" / "Contents" / "MacOS" / "soffice"
    existing.parent.mkdir(parents=True)
    existing.write_text("#!/bin/sh\n", encoding="utf-8")
    existing.chmod(0o755)
    home = tmp_path / "home"
    home.mkdir()
    (home / ".local").write_text("not a directory", encoding="utf-8")
    harness.write_text(harness.read_text(encoding="utf-8") + "echo STEP_FINISHED\n", encoding="utf-8")

    result, calls, _apps, _home = _run_office_step(tmp_path, harness)

    assert result.returncode == 0, result.stderr
    assert "STEP_FINISHED" in result.stdout, "set -e must not abort the installer here"
    assert "Could not write ~/.local/bin/soffice" in result.stderr
    assert calls == []


@POSIX_SH_ONLY
def test_a_linux_host_without_libreoffice_downloads_no_font(tmp_path: Path) -> None:
    """The LibreOffice offer declined, or a distro without apt: nothing will
    render, so the 8 MB face would be fetched for nobody."""
    harness = _font_step_harness(tmp_path, office=False, **_published_face(tmp_path))
    result, calls, cached, home = _run_font_step(tmp_path, harness, os_name="linux")

    assert result.returncode == 0, result.stderr
    assert result.stdout == "" and result.stderr == ""
    assert not (home / "share" / "fonts").exists()
    assert calls == []
    assert cached.is_file()


@POSIX_SH_ONLY
def test_the_pinned_face_by_name_counts_as_a_han_face(tmp_path: Path) -> None:
    """A host with the fontconfig library but no fc-list binary: the file this
    step installs, found by its own name, means a second run downloads nothing."""
    harness = _font_step_harness(tmp_path, **_published_face(tmp_path))
    fonts = tmp_path / "home" / "share" / "fonts"
    fonts.mkdir(parents=True)
    (fonts / "NotoSansSC-Regular.otf").write_bytes(b"already here")
    result, calls, cached, _home = _run_font_step(tmp_path, harness, os_name="linux", tools=("soffice",))

    assert result.returncode == 0, result.stderr
    assert (fonts / "NotoSansSC-Regular.otf").read_bytes() == b"already here"
    assert calls == []
    assert cached.is_file()


def test_the_helpers_are_defined_before_the_sections_that_use_them() -> None:
    """The file reads top-down; sha256_of is first called from the Node step,
    so it lives with the other helpers."""
    text = INSTALL_SH.read_text(encoding="utf-8")
    assert text.index("sha256_of() {") < text.index("install_libreoffice_dmg() {")
    assert text.index("sha256_of() {") < text.index("# --- 0. platform detection")


# --- A download host that cannot be reached ------------------------------------
# From mainland China huggingface.co and GitHub time out more often than they
# answer, and astral.sh, PyPI, nodejs.org or the npm registry can fail the same
# way. Each installer then fetches from a mirror -- and only then: an HTTP error
# is the host's answer, a host the user chose is kept, and RAVEN_NO_MIRROR=1
# keeps every download where it was.

_SH_HOSTS = (
    "NODE_DIST",
    "NODE_MIRROR",
    "PYPI_INDEX",
    "PYPI_MIRROR",
    "PYTHON_DOWNLOADS",
    "PYTHON_MIRROR",
    "NPM_REGISTRY",
    "NPM_MIRROR",
    "PLAYWRIGHT_MIRROR",
    "LO_MIRROR",
    "HAN_FONT_MIRROR_URL",
)
_PS1_HOSTS = (
    "NodeDist",
    "NodeMirror",
    "PypiIndex",
    "PypiMirror",
    "PythonDownloads",
    "PythonMirror",
    "NpmRegistry",
    "NpmMirror",
    "PlaywrightMirror",
)


def _sh_value(name: str) -> str:
    values = re.findall(rf'^{name}="([^"]*)"$', INSTALL_SH.read_text(encoding="utf-8"), re.M)
    assert len(values) == 1, f"install.sh assigns {name} {len(values)} times"
    return values[0]


def _ps1_value(name: str) -> str:
    values = re.findall(rf'^\${name} = "([^"]*)"$', INSTALL_PS1.read_text(encoding="utf-8"), re.M)
    assert len(values) == 1, f"install.ps1 assigns ${name} {len(values)} times"
    return values[0]


@pytest.mark.parametrize(
    ("script", "name"), [(INSTALL_SH, name) for name in _SH_HOSTS] + [(INSTALL_PS1, name) for name in _PS1_HOSTS]
)
def test_each_host_a_download_falls_back_from_or_to_is_spelled_out_once(script: Path, name: str) -> None:
    """In its assignment and nowhere else, so moving a host is one edit, and a
    hard-coded copy that a refactor brings back fails here instead of drifting.
    Comments may name a host they explain, and one host can be a prefix of
    another's, so both are left out of the count."""
    is_sh = script == INSTALL_SH
    value = _sh_value(name) if is_sh else _ps1_value(name)
    names = _SH_HOSTS if is_sh else _PS1_HOSTS
    others = [rf"^{other}=" if is_sh else rf"^\${other} = " for other in names if other != name]
    code = [
        line
        for line in script.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("#") and not any(re.match(other, line) for other in others)
    ]
    assert "\n".join(code).count(value) == 1, f"{value} is spelled out somewhere besides {name} in {script.name}"


def test_the_installers_fall_back_to_the_hosts_the_runtime_does() -> None:
    from raven.utils import mirrors

    same = {
        ("PYPI_INDEX", "PypiIndex"): mirrors.PYPI_DEFAULT_INDEX,
        ("PYPI_MIRROR", "PypiMirror"): mirrors.PYPI_INDEX,
        ("PYTHON_DOWNLOADS", "PythonDownloads"): mirrors.PYTHON_DEFAULT_DOWNLOADS,
        ("PYTHON_MIRROR", "PythonMirror"): mirrors.PYTHON_DOWNLOADS,
        ("NPM_REGISTRY", "NpmRegistry"): mirrors.NPM_DEFAULT_REGISTRY,
        ("NPM_MIRROR", "NpmMirror"): mirrors.NPM_REGISTRY,
    }
    for (sh_name, ps1_name), value in same.items():
        assert _sh_value(sh_name) == value, sh_name
        assert _ps1_value(ps1_name) == value, ps1_name
    for sh_name, ps1_name in (
        ("NODE_DIST", "NodeDist"),
        ("NODE_MIRROR", "NodeMirror"),
        ("PLAYWRIGHT_MIRROR", "PlaywrightMirror"),
    ):
        assert _sh_value(sh_name) == _ps1_value(ps1_name), sh_name


# --- One rule for a host that answered -------------------------------------------
# Five places decide whether a download's host could be reached before a
# fallback moves it: the runtime (urllib for the launches it starts, httpx for
# the files it fetches), the upgrade helper, install.sh (curl) and install.ps1
# (PowerShell). Each runs here with the client it really uses, against the same
# loopback hosts and proxies, and has to answer every case alike.


@contextlib.contextmanager
def _listening(handle: Callable[[socket.socket], None]) -> Iterator[str]:
    """Hand every connection to 127.0.0.1 on a port of its own to ``handle``;
    yield ``host:port``."""
    listener = socket.create_server(("127.0.0.1", 0))
    listener.settimeout(0.2)
    stopped = threading.Event()

    def serve(client: socket.socket) -> None:
        with client, contextlib.suppress(OSError):
            handle(client)

    def accept() -> None:
        while not stopped.is_set():
            try:
                client, _ = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            threading.Thread(target=serve, args=(client,), daemon=True).start()

    acceptor = threading.Thread(target=accept, daemon=True)
    acceptor.start()
    try:
        yield f"127.0.0.1:{listener.getsockname()[1]}"
    finally:
        stopped.set()
        acceptor.join()
        listener.close()


def _read_request(client: socket.socket) -> None:
    head = b""
    while b"\r\n\r\n" not in head:
        chunk = client.recv(4096)
        if not chunk:
            return
        head += chunk


def _tunnel_refused(status: int, reason: str) -> Callable[[socket.socket], None]:
    """An HTTP proxy that answers every request for a tunnel with ``status``."""

    def handle(client: socket.socket) -> None:
        _read_request(client)
        extra = 'Proxy-Authenticate: Basic realm="corp"\r\n' if status == 407 else ""
        client.sendall(f"HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\n{extra}\r\n".encode("ascii"))

    return handle


def _tunnel_then_closed(client: socket.socket) -> None:
    """An HTTP proxy that opens every tunnel and then drops it, the way one
    that answers at once and dials afterwards reports a host it cannot reach."""
    _read_request(client)
    client.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")


def _socks_refused(reply: int) -> Callable[[socket.socket], None]:
    """A SOCKS5 proxy that answers every CONNECT with ``reply``."""

    def handle(client: socket.socket) -> None:
        greeting = client.recv(2)
        if len(greeting) < 2 or greeting[0] != 5:
            return
        client.recv(greeting[1])
        client.sendall(b"\x05\x00")
        head = client.recv(4)
        if len(head) < 4:
            return
        # The address asked for: IPv4, a name of the length that leads it, or IPv6; then the port.
        length = 4 if head[3] == 1 else 16 if head[3] == 4 else client.recv(1)[0]
        client.recv(length + 2)
        client.sendall(bytes([5, reply, 0, 1, 0, 0, 0, 0, 0, 0]))

    return handle


def _closed_at_once(client: socket.socket) -> None:
    """A host that takes the connection and closes it before TLS begins."""


class _AnswerHead(http.server.BaseHTTPRequestHandler):
    status = 200

    def do_HEAD(self) -> None:  # noqa: N802 - the name http.server dispatches on
        self.send_response(self.status)
        self.end_headers()

    def log_message(self, *_args: object) -> None:
        return None


class _AnswerHeadWithAnError(_AnswerHead):
    status = 503


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


#: Each case's id, then whether a host that answers like that has answered.
_REACHABILITY = {
    # Direct.
    "an-http-error": True,
    "a-certificate-that-fails": True,
    "refused": False,
    "closed-before-tls": False,
    # Through an HTTP proxy.
    "a-tunnel-to-a-certificate-that-fails": True,
    "tunnel-refused-403": True,
    "tunnel-refused-407": True,
    "tunnel-refused-500": False,
    "tunnel-refused-502": False,
    "tunnel-refused-503": False,
    "tunnel-refused-504": False,
    "tunnel-closed-before-tls": False,
    "proxy-not-listening": False,
    # Through a SOCKS proxy.
    "socks-refused-by-its-rules": False,
    "socks-host-unreachable": False,
}


@pytest.fixture(scope="module")
def reachability_cases(tmp_path_factory: pytest.TempPathFactory) -> Iterator[dict[str, tuple[str, str]]]:
    """Each case of :data:`_REACHABILITY`: the URL asked about, and the proxy
    it is asked through, "" for none."""
    from tests._tls import connect_proxy, https_endpoint, private_ca

    far = "https://pypi.example.invalid/simple/"
    with contextlib.ExitStack() as stack:

        def proxy(handle: Callable[[socket.socket], None], scheme: str = "http") -> str:
            return f"{scheme}://{stack.enter_context(_listening(handle))}"

        failing = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _AnswerHeadWithAnError)
        threading.Thread(target=failing.serve_forever, daemon=True).start()
        stack.callback(failing.server_close)
        stack.callback(failing.shutdown)
        refused_certificate = stack.enter_context(
            https_endpoint(private_ca(tmp_path_factory.mktemp("ca")).server, _AnswerHead)
        )
        reasons = {403: "Forbidden", 407: "Proxy Authentication Required", 500: "Internal Server Error"}
        reasons |= {502: "Bad Gateway", 503: "Service Unavailable", 504: "Gateway Timeout"}
        cases = {
            "an-http-error": (f"http://127.0.0.1:{failing.server_address[1]}/", ""),
            "a-certificate-that-fails": (f"{refused_certificate}/", ""),
            "refused": (f"https://127.0.0.1:{_closed_port()}/", ""),
            "closed-before-tls": (f"https://{stack.enter_context(_listening(_closed_at_once))}/", ""),
            "a-tunnel-to-a-certificate-that-fails": (f"{refused_certificate}/", stack.enter_context(connect_proxy())),
            **{
                f"tunnel-refused-{status}": (far, proxy(_tunnel_refused(status, reason)))
                for status, reason in reasons.items()
            },
            "tunnel-closed-before-tls": (far, proxy(_tunnel_then_closed)),
            "proxy-not-listening": (far, f"http://127.0.0.1:{_closed_port()}"),
            # socks5h: curl looks a name up itself under socks5.
            "socks-refused-by-its-rules": (far, proxy(_socks_refused(2), "socks5h")),
            "socks-host-unreachable": (far, proxy(_socks_refused(4), "socks5h")),
        }
        assert cases.keys() == _REACHABILITY.keys()
        yield cases


def _proxy_env(proxy: str) -> dict[str, str]:
    """The variables that send every client through ``proxy``, or past every proxy."""
    names = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy")
    if not proxy:
        # A variable of their own, so no client reads the system's proxy instead.
        return {**dict.fromkeys(names, ""), "NO_PROXY": "*", "no_proxy": "*"}
    return {**dict.fromkeys(names, proxy), "NO_PROXY": "", "no_proxy": ""}


def _python_verdicts(
    cases: dict[str, tuple[str, str]], monkeypatch: pytest.MonkeyPatch, answers: Callable[[str], bool]
) -> dict[str, bool]:
    import urllib.request

    said = {}
    for name, (url, proxy) in cases.items():
        with monkeypatch.context() as patch:
            for key, value in _proxy_env(proxy).items():
                patch.setenv(key, value)
            # urlopen keeps the proxies it read first, for the whole process.
            patch.setattr(urllib.request, "_opener", None)
            said[name] = answers(url)
    return said


def _runtime_launches(cases: dict[str, tuple[str, str]], monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    from raven.utils import mirrors

    return _python_verdicts(cases, monkeypatch, mirrors._answers)


def _runtime_downloads(cases: dict[str, tuple[str, str]], monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    import httpx

    from raven.utils import mirrors

    def answers(url: str) -> bool:
        try:
            with httpx.Client(timeout=5) as client:
                client.head(url)
        except Exception as exc:  # noqa: BLE001 - what is raised is the question
            return not mirrors.unreachable(exc)
        return True

    return _python_verdicts(cases, monkeypatch, answers)


def _upgrade_helper(cases: dict[str, tuple[str, str]], monkeypatch: pytest.MonkeyPatch) -> dict[str, bool]:
    from raven.updates import upgrade

    namespace: dict[str, object] = {"__name__": "raven_upgrade_helper_test"}
    exec(upgrade._UPGRADE_HELPER_SOURCE, namespace)
    answers = namespace["answers"]
    assert callable(answers)
    return _python_verdicts(cases, monkeypatch, answers)


def _install_sh(cases: dict[str, tuple[str, str]], tmp_path: Path) -> dict[str, bool]:
    if sys.platform == "win32" or shutil.which("sh") is None or shutil.which("curl") is None:
        pytest.skip("POSIX sh and curl")
    body = ""
    for name, (url, proxy) in cases.items():
        settings = " ".join(f"export {key}={shlex.quote(value)};" for key, value in _proxy_env(proxy).items())
        body += f"( {settings} if reachable {shlex.quote(url)}; then echo {name}=True; else echo {name}=False; fi )\n"
    harness = tmp_path / "reachable.sh"
    harness.write_text(_SH_PREAMBLE + _sh_functions(("connection_failed", "reachable")) + body, encoding="utf-8")
    result = subprocess.run(
        ["sh", str(harness)], capture_output=True, text=True, check=False, env={"PATH": os.environ["PATH"]}
    )
    return {name: value == "True" for name, value in (line.split("=", 1) for line in result.stdout.split())}


_PS1_TABLE_STEP = r"""
foreach ($text in (Get-Install @("Test-Reachable", "Get-TunnelStatus"))) { Invoke-Expression $text }
foreach ($line in (Get-Content $CasesFile)) {
    $name, $url, $proxy = $line -split " "
    # .NET has no socks5h, and hands the name to a SOCKS5 proxy under socks5.
    $proxy = $proxy -replace "^socks5h:", "socks5:"
    [System.Net.Http.HttpClient]::DefaultProxy = if ($proxy) { [System.Net.WebProxy]::new($proxy) } else { [System.Net.WebProxy]::new() }
    [Console]::Out.WriteLine("$name=$(Test-Reachable $url)")
}
"""


def _install_ps1(cases: dict[str, tuple[str, str]], tmp_path: Path) -> dict[str, bool]:
    if shutil.which("pwsh") is None:
        pytest.skip("needs PowerShell")
    listed = tmp_path / "cases.txt"
    lines = [f"{name} {url} {proxy}".rstrip() for name, (url, proxy) in cases.items()]
    listed.write_text("\n".join(lines) + "\n", encoding="utf-8")
    step = _ps1_settings("ErrorActionPreference") + _PS1_TABLE_STEP
    result = _run_ps1(tmp_path, "reachable-table", step, CasesFile=str(listed))
    said = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert said.keys() == cases.keys(), result.stdout + result.stderr
    return {name: value == "True" for name, value in said.items()}


@pytest.mark.slow
@pytest.mark.parametrize(
    "decider",
    ["runtime-launches", "runtime-downloads", "upgrade-helper", "install.sh", "install.ps1"],
)
def test_every_copy_of_the_rule_answers_each_case_alike(
    reachability_cases: dict[str, tuple[str, str]], decider: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A host that answered -- with an HTTP error, or with a certificate that
    fails -- keeps the download, and so does a proxy's refusal of the tunnel:
    a 403 for a host its rules block, a 407 for credentials it was not given.
    The mirror would be asked through that same proxy, and would only route
    around its rules. A host no connection reached moves it, and so does a
    proxy's 5xx, which says the proxy could not reach the host either, and a
    SOCKS proxy that could not connect, whatever its reason: PowerShell has
    that only in an exception's text."""
    if decider == "runtime-launches":
        said = _runtime_launches(reachability_cases, monkeypatch)
    elif decider == "runtime-downloads":
        said = _runtime_downloads(reachability_cases, monkeypatch)
    elif decider == "upgrade-helper":
        said = _upgrade_helper(reachability_cases, monkeypatch)
    elif decider == "install.sh":
        said = _install_sh(reachability_cases, tmp_path)
    else:
        said = _install_ps1(reachability_cases, tmp_path)

    assert said == _REACHABILITY


def test_both_installers_say_how_to_keep_every_download_where_it_was() -> None:
    for script in (INSTALL_SH, INSTALL_PS1):
        header = script.read_text(encoding="utf-8").split("\n\n", 1)[0]
        assert "RAVEN_NO_MIRROR=1" in header, script.name


def test_the_quick_start_says_so_too() -> None:
    """Whoever needs the switch reads the quick start before the script."""
    docs = INSTALL_SH.parent / "docs-site" / "docs"
    for page in ("quick-start.md", "quick-start.zh.md"):
        assert "RAVEN_NO_MIRROR=1" in (docs / page).read_text(encoding="utf-8"), page


@POSIX_SH_ONLY
@pytest.mark.parametrize(
    ("value", "allowed"),
    [("", True), ("0", True), ("no", True), ("1", False), ("TRUE", False), (" yes ", False), ("on", False)],
)
def test_the_opt_out_reads_the_way_ravens_own_do(tmp_path: Path, value: str, allowed: bool) -> None:
    """The installer and the `raven resources` it starts read one variable, so
    they have to agree on what a value means."""
    harness = tmp_path / "opt-out.sh"
    harness.write_text(
        _SH_PREAMBLE + _sh_functions(("mirrors_allowed",)) + "if mirrors_allowed; then echo ON; else echo OFF; fi\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["sh", str(harness)],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin:/bin", "RAVEN_NO_MIRROR": value},
    )

    assert result.stdout.strip() == ("ON" if allowed else "OFF")


@POSIX_SH_ONLY
@pytest.mark.parametrize(
    ("code", "reached"),
    [
        (0, True),
        (22, True),
        (60, True),
        (6, False),
        (7, False),
        (28, False),
        (35, False),
        (52, False),
        (56, False),
        (97, False),
    ],
    ids=["answered", "http-error", "certificate", "no-name", "refused", "timed-out", "tls", "empty", "reset", "proxy"],
)
def test_only_a_connection_that_never_completed_is_unreachable(tmp_path: Path, code: int, reached: bool) -> None:
    """An HTTP error, and a certificate that fails verification, are answers:
    the host is there, and a mirror would only repeat or hide what it said."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "curl").write_text(f"#!/bin/sh\nexit {code}\n", encoding="utf-8")
    (bin_dir / "curl").chmod(0o755)
    harness = tmp_path / "reachable.sh"
    harness.write_text(
        _SH_PREAMBLE
        + _sh_functions(("connection_failed", "reachable"))
        + "if reachable https://example.org/; then echo YES; else echo NO; fi\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["sh", str(harness)], capture_output=True, text=True, check=False, env={"PATH": f"{bin_dir}:/usr/bin:/bin"}
    )

    assert result.stdout.strip() == ("YES" if reached else "NO"), result.stderr


@POSIX_SH_ONLY
@pytest.mark.parametrize(
    ("code", "tunnel", "reached"),
    [(56, "403", True), (7, "403", True), (7, "407", True), (56, "502", False), (7, "504", False), (35, "200", False)],
    ids=["refused-curl-7", "refused-curl-8", "credentials", "bad-gateway", "gateway-timeout", "opened-then-dropped"],
)
def test_a_proxys_refusal_of_the_tunnel_is_read_from_its_status(
    tmp_path: Path, code: int, tunnel: str, reached: bool
) -> None:
    """curl 7 ends a refused tunnel with 56, curl 8 with 7, so its exit code
    cannot tell a refusal from a connection that never completed; the status
    the proxy answered with can (%{http_connect})."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # The status is written only where %{http_connect} asks for it, as curl does.
    (bin_dir / "curl").write_text(
        f'#!/bin/sh\ncase "$*" in *http_connect*) printf {tunnel} ;; esac\nexit {code}\n', encoding="utf-8"
    )
    (bin_dir / "curl").chmod(0o755)
    harness = tmp_path / "reachable.sh"
    harness.write_text(
        _SH_PREAMBLE
        + _sh_functions(("connection_failed", "reachable"))
        + "if reachable https://example.org/; then echo YES; else echo NO; fi\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["sh", str(harness)], capture_output=True, text=True, check=False, env={"PATH": f"{bin_dir}:/usr/bin:/bin"}
    )

    assert result.stdout.strip() == ("YES" if reached else "NO"), result.stderr


@POSIX_SH_ONLY
def test_a_host_is_reached_where_its_redirects_end(tmp_path: Path) -> None:
    """GitHub answers a release file's address with a redirect to the host that
    serves it, and from mainland China that second host is the one that times
    out; the address answering is not the file being reachable."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    # Answers at once, unless asked to follow where it points, which times out.
    (bin_dir / "curl").write_text(
        '#!/bin/sh\nfor arg in "$@"; do case "$arg" in --location|-[a-zA-Z]*L*) exit 28 ;; esac; done\nexit 0\n',
        encoding="utf-8",
    )
    (bin_dir / "curl").chmod(0o755)
    harness = tmp_path / "reachable.sh"
    harness.write_text(
        _SH_PREAMBLE
        + _sh_functions(("connection_failed", "reachable"))
        + "if reachable https://github.com/o/r/releases/latest/download/f; then echo YES; else echo NO; fi\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["sh", str(harness)], capture_output=True, text=True, check=False, env={"PATH": f"{bin_dir}:/usr/bin:/bin"}
    )

    assert result.stdout.strip() == "NO", result.stderr


_PS1_REACHABLE_STEP = r"""
foreach ($text in (Get-Install @("Test-Reachable", "Get-TunnelStatus"))) { Invoke-Expression $text }
function New-Failure($Kind) {
    switch ($Kind) {
        "refused" { [System.Net.Http.HttpRequestException]::new([System.Net.Http.HttpRequestError]::ConnectionError, "refused", $null, $null) }
        "tls" { [System.Net.Http.HttpRequestException]::new([System.Net.Http.HttpRequestError]::SecureConnectionError, "The SSL connection could not be established", $null, $null) }
        "trust" { [System.Net.WebException]::new("Could not establish trust relationship", [System.Net.WebExceptionStatus]::TrustFailure) }
        "refused-5.1" { [System.Net.WebException]::new("Unable to connect", [System.Net.WebExceptionStatus]::ConnectFailure) }
        "tunnel-403" { [System.Net.Http.HttpRequestException]::new([System.Net.Http.HttpRequestError]::ProxyTunnelError, "The proxy tunnel request failed with status code '403'.", $null, [System.Net.HttpStatusCode]::Forbidden) }
        "tunnel-503" { [System.Net.Http.HttpRequestException]::new([System.Net.Http.HttpRequestError]::ProxyTunnelError, "The proxy tunnel request failed with status code '503'.", $null, [System.Net.HttpStatusCode]::ServiceUnavailable) }
        "socks" { [System.Net.Http.HttpRequestException]::new([System.Net.Http.HttpRequestError]::ProxyTunnelError, "An error occurred while establishing a connection to the proxy tunnel.", $null, $null) }
    }
}
# What Windows PowerShell raises for an answer: the response, from wherever it
# came -- the proxy's own address when the proxy refused the tunnel.
function New-Answer([int]$Status, [string]$From) {
    $failure = [System.Exception]::new("($Status)")
    $failure | Add-Member -NotePropertyName Response -NotePropertyValue ([pscustomobject]@{ StatusCode = $Status; ResponseUri = [uri]$From })
    $failure
}
[System.Net.WebRequest]::DefaultWebProxy = [System.Net.WebProxy]::new("http://proxy.test:3128", $false, @("direct-502-5-1\.test"))
# PowerShell 7 reports a refused certificate as it does any failed handshake;
# only asking again without the check tells the two apart.
function Invoke-WebRequest {
    param([Parameter(Position = 0)][string]$Uri, [switch]$UseBasicParsing, [string]$Method, [int]$TimeoutSec, [switch]$SkipCertificateCheck)
    $hostName = ([Uri]$Uri).Host
    if ($hostName -eq "refused.test") { throw (New-Failure "refused") }
    if ($hostName -eq "missing.test") {
        $failure = [System.Exception]::new("404 Not Found")
        $failure | Add-Member -NotePropertyName Response -NotePropertyValue "404"
        throw $failure
    }
    if ($hostName -eq "re-signed.test" -and -not $SkipCertificateCheck) { throw (New-Failure "tls") }
    if ($hostName -eq "no-tls.test") { throw (New-Failure "tls") }
    if ($hostName -eq "re-signed-5-1.test") { throw (New-Failure "trust") }
    if ($hostName -eq "refused-5-1.test") { throw (New-Failure "refused-5.1") }
    if ($hostName -in @("tunnel-403", "tunnel-503", "socks")) { throw (New-Failure $hostName) }
    if ($hostName -eq "tunnel-403-5-1.test") { throw (New-Answer 403 "http://proxy.test:3128/") }
    if ($hostName -eq "tunnel-502-5-1.test") { throw (New-Answer 502 "http://proxy.test:3128/") }
    if ($hostName -eq "error-502-5-1.test") { throw (New-Answer 502 $Uri) }
    if ($hostName -eq "direct-502-5-1.test") { throw (New-Answer 502 $Uri) }
}
$names = @("answers", "refused", "missing", "re-signed", "no-tls", "re-signed-5-1", "refused-5-1")
$names += @("tunnel-403", "tunnel-503", "socks", "tunnel-403-5-1", "tunnel-502-5-1", "error-502-5-1", "direct-502-5-1")
foreach ($name in $names) {
    $url = if ($name -in @("tunnel-403", "tunnel-503", "socks")) { "https://$name/" } else { "https://$name.test/" }
    [Console]::Out.WriteLine("$name=$(Test-Reachable $url)")
}
"""


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
def test_the_windows_installer_draws_the_same_line(tmp_path: Path) -> None:
    """The line install.sh draws with curl's exit codes, drawn from what
    PowerShell raises: Windows PowerShell names a refused certificate outright
    (TrustFailure); PowerShell 7 calls it a failed handshake, like a TLS that
    never got going, until the host is asked again without the check.

    A proxy's refusal of the tunnel is read from its status. PowerShell 7
    names it (ProxyTunnelError, with the status beside it, and none from a
    SOCKS proxy); Windows PowerShell, which no CI job runs, hands the proxy's
    response back as it would the host's, and only where it came from tells
    the two apart (measured with 5.1 through a loopback proxy)."""
    result = _run_ps1(tmp_path, "reachable-step", _ps1_settings("ErrorActionPreference") + _PS1_REACHABLE_STEP)

    said = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert said == {
        "answers": "True",
        "refused": "False",
        "missing": "True",
        "re-signed": "True",
        "no-tls": "False",
        "re-signed-5-1": "True",
        "refused-5-1": "False",
        "tunnel-403": "True",
        "tunnel-503": "False",
        "socks": "False",
        "tunnel-403-5-1": "True",
        "tunnel-502-5-1": "False",
        "error-502-5-1": "True",
        "direct-502-5-1": "True",
    }, result.stdout + result.stderr


# --- uv itself ------------------------------------------------------------------

_UV_PAGE = "https://pypi.tuna.tsinghua.edu.cn/simple/uv/"
_UV_LINUX_TAG = "manylinux_2_17_x86_64.manylinux2014_x86_64"


def _uv_wheel(path: Path, version: str, *, suffix: str = "") -> str:
    """A uv wheel as PyPI carries one: the binaries under ``*.data/scripts``.
    Returns its sha256, as an index publishes it beside the link."""
    import hashlib
    import zipfile

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as wheel:
        for exe in ("uv", "uvx"):
            entry = zipfile.ZipInfo(f"uv-{version}.data/scripts/{exe}{suffix}")
            entry.external_attr = 0o755 << 16
            wheel.writestr(entry, f"#!/bin/sh\necho {exe} {version}\n")
        wheel.writestr(f"uv-{version}.dist-info/METADATA", "Metadata-Version: 2.1\nName: uv\n")
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _uv_index(tmp_path: Path, tag: str, *, suffix: str = "", tamper: bool = False) -> tuple[Path, dict[str, Path]]:
    """An index page in TUNA's shape: links relative to the page, a sha256 on
    each, sorted by name -- so 0.9.9 comes after 0.10.0. Returns the page and
    what each absolute file URL serves."""
    web = tmp_path / "web"
    files = {"0.10.0": web / "new.whl", "0.9.9": web / "old.whl"}
    links, served = [], {}
    for version, path in files.items():
        digest = _uv_wheel(path, version, suffix=suffix)
        if tamper:
            digest = "0" * 64
        rel = f"../../packages/aa/{version}/uv-{version}-py3-none-{tag}.whl"
        links.append(f'<a href="{rel}#sha256={digest}">uv-{version}</a>')
        served[f"https://pypi.tuna.tsinghua.edu.cn/packages/aa/{version}/uv-{version}-py3-none-{tag}.whl"] = path
    page = web / "index.html"
    page.write_text("\n".join(links) + "\n", encoding="utf-8")
    return page, served


def _served_from(index: str, page: Path, served: dict[str, Path]) -> dict[str, Path]:
    """What :func:`_uv_index` serves, moved from TUNA to ``index``: its uv page
    and the wheels its relative links lead to."""
    host = "/".join(index.split("/")[:3])
    moved = {url.replace("https://pypi.tuna.tsinghua.edu.cn", host): path for url, path in served.items()}
    return {f"{index.rstrip('/')}/uv/": page, **moved}


def _fake_unzip(bin_dir: Path) -> None:
    """unzip, for a host without one: the image this suite runs in ships none."""
    (bin_dir / "unzip").write_text(
        "#!/bin/sh\n"
        'while [ $# -gt 0 ]; do case "$1" in -d) dir="$2"; shift 2 ;; -*) shift ;; *) zip="$1"; shift ;; esac; done\n'
        f"exec {shlex.quote(sys.executable)} -c "
        '"import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])" "$zip" "$dir"\n',
        encoding="utf-8",
    )
    (bin_dir / "unzip").chmod(0o755)


_UV_STEP = (
    "sha256_of",
    "mirrors_allowed",
    "connection_failed",
    "reachable",
    "resolve_href",
    "install_uv_from_mirror",
    "uv_config_files",
    "uv_config_text",
    "uv_config_has",
    "uv_config_index",
    "uv_index_chosen",
    "uv_named_indexes",
    "uv_host_chosen",
    "uv_unreachable",
    "ensure_uv",
)
_UV_SETTINGS = ("PYPI_MIRROR", "UV_INSTALLER", "PYTHON_DOWNLOADS", "UV_GITHUB_RELEASE")
_UV_INSTALLER_URL = "https://astral.sh/uv/install.sh"


def _run_uv_step(
    tmp_path: Path,
    served: dict[str, Path],
    *,
    env: dict[str, str] | None = None,
    down: tuple[str, ...] = ("https://astral.sh/",),
    codes: dict[str, int] | None = None,
    tunnels: dict[str, tuple[str, int]] | None = None,
    uv_toml: str | None = None,
    machine_uv_toml: str | None = None,
):
    bin_dir = tmp_path / "bin"
    log = tmp_path / "curl.log"
    _fake_curl(bin_dir, served, down=down, codes=codes, tunnels=tunnels, log=log)
    _fake_unzip(bin_dir)
    for root, text in (("config", uv_toml), ("xdg", machine_uv_toml)):
        if text is not None:
            (tmp_path / root / "uv").mkdir(parents=True)
            (tmp_path / root / "uv" / "uv.toml").write_text(text, encoding="utf-8")
    harness = tmp_path / "uv-step.sh"
    # uv counts only where this step puts it: a uv the machine already has
    # (/usr/bin/uv, say) would end the step before any of it ran.
    have = 'have() { case "$1" in uv) [ -x "$HOME/.local/bin/uv" ] ;; *) command -v "$1" >/dev/null 2>&1 ;; esac; }\n'
    functions = _without_the_machines_etc(_sh_functions(_UV_STEP, _UV_SETTINGS), tmp_path)
    harness.write_text(_SH_PREAMBLE + have + functions + "ensure_uv\n", encoding="utf-8")
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    result = subprocess.run(
        ["sh", str(harness)],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{bin_dir}:/usr/bin:/bin",
            "HOME": str(home),
            "TMPDIR": str(tmp_path),
            "NODE_OS": "linux",
            "NODE_ARCH": "x64",
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_CONFIG_DIRS": str(tmp_path / "xdg"),
            **(env or {}),
        },
    )
    asked = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
    return result, home / ".local" / "bin", asked


@POSIX_SH_ONLY
def test_uv_comes_from_the_newest_wheel_on_the_mirror_when_astral_sh_cannot_be_reached(tmp_path: Path) -> None:
    """uv publishes the binary its installer fetches to PyPI too, as a wheel,
    and a wheel is a zip."""
    page, served = _uv_index(tmp_path, _UV_LINUX_TAG)

    result, bin_dir, _asked = _run_uv_step(tmp_path, {_UV_PAGE: page, **served})

    assert result.returncode == 0, result.stdout + result.stderr
    ran = subprocess.run([str(bin_dir / "uv")], capture_output=True, text=True, check=False)
    assert ran.stdout.strip() == "uv 0.10.0", "the newest release, not the last one listed"
    assert os.access(bin_dir / "uvx", os.X_OK)
    assert "pypi.tuna.tsinghua.edu.cn" in result.stderr and "RAVEN_NO_MIRROR" in result.stderr


@POSIX_SH_ONLY
def test_a_uv_wheel_that_does_not_match_its_published_digest_is_not_installed(tmp_path: Path) -> None:
    page, served = _uv_index(tmp_path, _UV_LINUX_TAG, tamper=True)

    result, bin_dir, _asked = _run_uv_step(tmp_path, {_UV_PAGE: page, **served})

    assert result.returncode != 0 and "DIE " in result.stderr
    assert not (bin_dir / "uv").exists()


_THEIRS = "https://pypi.example.internal/simple"
# Each way a user names the index uv reads its packages from: the environment,
# the user's uv.toml, the machine's.
_NAMED_INDEXES = [
    ({"UV_DEFAULT_INDEX": _THEIRS}, None, None),
    ({"UV_INDEX_URL": _THEIRS}, None, None),
    ({"UV_INDEX": f"corp={_THEIRS}"}, None, None),
    ({"UV_INDEX": _THEIRS}, None, None),
    ({}, f'index-url = "{_THEIRS}"\n', None),
    ({}, None, f'[[index]]\nname = "corp"\nurl = "{_THEIRS}"\ndefault = true\n'),
    ({}, f"index = [{{ url = '{_THEIRS}', default = true }}]\n", None),
    ({}, f'"index-url" = "{_THEIRS}"\n', None),
    ({}, None, f'[["index"]]\n"url" = "{_THEIRS}"\n"default" = true\n'),
    ({}, "\ufeff" + f'index-url = "{_THEIRS}"\n', None),
    ({}, None, "\ufeff" + f'[["index"]]\n"url" = "{_THEIRS}"\n"default" = true\n'),
]
_NAMED_INDEX_IDS = [
    "default-index",
    "index-url",
    "named-index",
    "index",
    "uv-toml-index-url",
    "machines-default-table",
    "inline-default",
    "quoted-index-url",
    "quoted-default-table",
    "uv-toml-with-a-mark",
    "machines-default-table-with-a-mark",
]


@POSIX_SH_ONLY
@pytest.mark.parametrize(("env", "uv_toml", "machine_uv_toml"), _NAMED_INDEXES, ids=_NAMED_INDEX_IDS)
def test_an_index_the_user_named_is_where_uv_comes_from(
    tmp_path: Path, env: dict[str, str], uv_toml: str | None, machine_uv_toml: str | None
) -> None:
    """The fallback reads the index the way uv would: from the environment, or
    else from the uv.toml uv reads, in each form a default index takes there."""
    page, served = _uv_index(tmp_path, _UV_LINUX_TAG)

    result, bin_dir, asked = _run_uv_step(
        tmp_path, _served_from(_THEIRS, page, served), env=env, uv_toml=uv_toml, machine_uv_toml=machine_uv_toml
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert os.access(bin_dir / "uv", os.X_OK)
    assert f"{_THEIRS}/uv/" in asked and not any("tuna" in url for url in asked)


@POSIX_SH_ONLY
@pytest.mark.parametrize("extra_lists_uv", [False, True], ids=["extra-lists-none", "extra-lists-uv"])
def test_the_named_indexes_are_asked_in_uvs_order(tmp_path: Path, extra_lists_uv: bool) -> None:
    """UV_INDEX's entries before the default, as uv asks them: the first that
    lists uv is where it comes from, and one that lists none hands over to the
    next."""
    page, served = _uv_index(tmp_path, _UV_LINUX_TAG)
    extra = "https://extra.example.internal/simple"
    empty = tmp_path / "web" / "empty.html"
    empty.write_text("<html></html>\n", encoding="utf-8")
    first = _served_from(extra, page, served) if extra_lists_uv else {f"{extra}/uv/": empty}

    result, _bin_dir, asked = _run_uv_step(
        tmp_path,
        {**_served_from(_THEIRS, page, served), **first},
        env={"UV_INDEX": f"extra={extra}", "UV_DEFAULT_INDEX": _THEIRS},
    )

    assert result.returncode == 0, result.stdout + result.stderr
    if extra_lists_uv:
        assert f"{extra}/uv/" in asked and f"{_THEIRS}/uv/" not in asked
    else:
        assert asked.index(f"{extra}/uv/") < asked.index(f"{_THEIRS}/uv/")
    assert not any("tuna" in url for url in asked)


@POSIX_SH_ONLY
def test_the_uv_toml_uv_config_file_names_is_where_the_index_is_read(tmp_path: Path) -> None:
    page, served = _uv_index(tmp_path, _UV_LINUX_TAG)
    named = tmp_path / "named.toml"
    named.write_text(f'index-url = "{_THEIRS}"\n', encoding="utf-8")

    result, _bin_dir, asked = _run_uv_step(
        tmp_path,
        _served_from(_THEIRS, page, served),
        env={"UV_CONFIG_FILE": str(named)},
        uv_toml='index-url = "https://other.example.internal/simple"\n',
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert not any("other.example" in url or "tuna" in url for url in asked)


#: A default index whose address spans lines: uv reads it, and the patterns
#: the installers read it with do not.
_UNREADABLE_INDEX = '[[index]]\nurl = """\n' + _THEIRS + '\n"""\ndefault = true\n'


@POSIX_SH_ONLY
def test_an_index_a_uv_toml_names_unreadably_keeps_the_mirror_away(tmp_path: Path) -> None:
    """A default index whose address this cannot read is still the user's
    choice: uv does not come from the mirror, and the warning says how to hand
    the index over."""
    page, served = _uv_index(tmp_path, _UV_LINUX_TAG)

    result, bin_dir, asked = _run_uv_step(tmp_path, {_UV_PAGE: page, **served}, uv_toml=_UNREADABLE_INDEX)

    assert result.returncode != 0 and "DIE " in result.stderr
    assert not (bin_dir / "uv").exists()
    assert not any("tuna" in url for url in asked)
    assert "UV_DEFAULT_INDEX" in result.stderr


@POSIX_SH_ONLY
def test_the_opt_out_leaves_uv_to_its_own_installer(tmp_path: Path) -> None:
    page, served = _uv_index(tmp_path, _UV_LINUX_TAG)

    result, _bin_dir, asked = _run_uv_step(tmp_path, {_UV_PAGE: page, **served}, env={"RAVEN_NO_MIRROR": "1"})

    assert result.returncode != 0 and "DIE " in result.stderr
    assert asked == [_UV_INSTALLER_URL]


def _uv_installer(tmp_path: Path, *, works: bool) -> Path:
    """uv's installer as astral.sh serves it: one that puts uv where uv's own
    does, or one that ends the way it does when its download failed."""
    script = tmp_path / "web" / "install.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    if works:
        body = (
            'mkdir -p "$HOME/.local/bin"\n'
            "printf '#!/bin/sh\\necho uv 0.13.4\\n' > \"$HOME/.local/bin/uv\"\n"
            'chmod +x "$HOME/.local/bin/uv"\n'
        )
    else:
        body = "echo 'downloading uv 0.13.4'\nexit 1\n"
    script.write_text(body, encoding="utf-8")
    return script


@POSIX_SH_ONLY
@pytest.mark.parametrize(
    ("code", "mirrored"),
    [(22, False), (60, False), (97, True)],
    ids=["http-error", "certificate", "proxy-could-not-connect"],
)
def test_uv_comes_from_the_mirror_only_when_its_installer_could_not_be_reached(
    tmp_path: Path, code: int, mirrored: bool
) -> None:
    """astral.sh's answer -- an HTTP error, a certificate that fails
    verification -- is final, and installing uv from another host would hide
    it; a proxy that could not connect is no answer."""
    page, served = _uv_index(tmp_path, _UV_LINUX_TAG)

    result, bin_dir, asked = _run_uv_step(
        tmp_path, {_UV_PAGE: page, **served}, down=(), codes={"https://astral.sh/": code}
    )

    assert (bin_dir / "uv").exists() is mirrored, result.stdout + result.stderr
    assert any("tuna" in url for url in asked) is mirrored
    assert (result.returncode == 0) is mirrored


_UV_HOSTS_DOWN = [
    (("https://releases.astral.sh/", "https://github.com/"), True),
    (("https://releases.astral.sh/",), False),
    ((), False),
]
_UV_HOSTS_DOWN_IDS = ["both-unreachable", "github-answered", "both-answered"]


@POSIX_SH_ONLY
@pytest.mark.parametrize(("down", "mirrored"), _UV_HOSTS_DOWN, ids=_UV_HOSTS_DOWN_IDS)
def test_an_installer_that_left_no_uv_hands_over_to_the_mirror_only_when_its_hosts_cannot_be_reached(
    tmp_path: Path, down: tuple[str, ...], mirrored: bool
) -> None:
    """The installer does not say why it failed, so the hosts it downloads uv
    from -- releases.astral.sh, then GitHub's release files -- are asked, and
    the mirror stands in only when neither can be reached."""
    page, served = _uv_index(tmp_path, _UV_LINUX_TAG)
    installer = _uv_installer(tmp_path, works=False)

    result, bin_dir, asked = _run_uv_step(tmp_path, {_UV_INSTALLER_URL: installer, _UV_PAGE: page, **served}, down=down)

    assert (bin_dir / "uv").exists() is mirrored, result.stdout + result.stderr
    assert any("tuna" in url for url in asked) is mirrored
    assert "https://releases.astral.sh/" in asked


@POSIX_SH_ONLY
@pytest.mark.parametrize(
    ("tunnel", "mirrored"), [(("403", 56), False), (("407", 7), False), (("502", 56), True)], ids=["403", "407", "502"]
)
def test_a_proxy_that_refused_the_tunnel_to_astral_sh_answered_for_it(
    tmp_path: Path, tunnel: tuple[str, int], mirrored: bool
) -> None:
    """Fetching uv's installer through a proxy that will not open a tunnel to
    astral.sh: a refusal is final, as astral.sh's own error would be; a 5xx
    says the proxy could not reach it either."""
    page, served = _uv_index(tmp_path, _UV_LINUX_TAG)

    result, bin_dir, _asked = _run_uv_step(
        tmp_path, {_UV_PAGE: page, **served}, down=(), tunnels={"https://astral.sh/": tunnel}
    )

    assert (bin_dir / "uv").exists() is mirrored, result.stdout + result.stderr


@POSIX_SH_ONLY
def test_an_installer_that_works_is_all_uv_takes(tmp_path: Path) -> None:
    installer = _uv_installer(tmp_path, works=True)

    result, bin_dir, asked = _run_uv_step(tmp_path, {_UV_INSTALLER_URL: installer}, down=())

    assert result.returncode == 0, result.stdout + result.stderr
    assert os.access(bin_dir / "uv", os.X_OK)
    assert asked == [_UV_INSTALLER_URL], "no host is asked about once uv is in place"


# What uv's installers read, in this order and before their own two hosts, for
# where to download uv from (as of 0.13.4).
_UV_HOST_SETTINGS = [
    "UV_DOWNLOAD_URL",
    "INSTALLER_DOWNLOAD_URL",
    "UV_INSTALLER_GHE_BASE_URL",
    "UV_INSTALLER_GITHUB_BASE_URL",
]


@POSIX_SH_ONLY
@pytest.mark.parametrize("name", _UV_HOST_SETTINGS)
@pytest.mark.parametrize("installer", ["left-no-uv", "unreachable"])
def test_a_host_the_user_chose_for_uvs_installer_is_never_replaced(tmp_path: Path, name: str, installer: str) -> None:
    """uv's two hosts out of reach, as on a network that blocks them, and the
    host the user named for uv in their place: its answer, or its silence, is
    final, the same with no installer to run."""
    page, served = _uv_index(tmp_path, _UV_LINUX_TAG)
    if installer == "left-no-uv":
        served[_UV_INSTALLER_URL] = _uv_installer(tmp_path, works=False)
    down = ("https://releases.astral.sh/", "https://github.com/")
    if installer == "unreachable":
        down += ("https://astral.sh/",)

    result, bin_dir, asked = _run_uv_step(
        tmp_path, {_UV_PAGE: page, **served}, env={name: "https://uv.example.internal"}, down=down
    )

    assert result.returncode != 0 and "DIE " in result.stderr, result.stdout + result.stderr
    assert not (bin_dir / "uv").exists()
    assert not any("tuna" in url for url in asked)


_PS1_UV_STEP = r"""
$names = @(
    "Find-Uv", "Install-UvFromMirror", "Test-MirrorsAllowed", "Get-UvNamedIndexes", "Get-UvConfigIndex",
    "Get-UvConfigFiles", "Get-UvConfigText", "Get-UvConfigLines", "Test-UvConfigHas", "Test-UvIndexChosen"
)
foreach ($text in (Get-Install $names)) { Invoke-Expression $text }
function Write-Info([string]$Message) { [Console]::Out.WriteLine("INFO $Message") }
function Write-Warn([string]$Message) { [Console]::Out.WriteLine("WARN $Message") }
# The machine's own uv is not the one this step installs.
function Get-Command {
    param([Parameter(Position = 0)]$Name)
    if ($Name -ne "uv") { Microsoft.PowerShell.Core\Get-Command @PSBoundParameters @args }
}
$Served = @{}
foreach ($pair in ($ServedList -split ";")) {
    if ($pair) { $url, $file = $pair -split "=", 2; $Served[$url] = $file }
}
$Down = @("https://astral.sh/")
$uv = Install-UvFromMirror
[Console]::Out.WriteLine("FOUND $uv")
"""


def _run_ps1_uv_step(
    tmp_path: Path,
    page: Path,
    served: dict[str, Path],
    *,
    env: dict[str, str] | None = None,
    uv_tomls: dict[str, str] | None = None,
):
    """Install-UvFromMirror; ``uv_tomls`` as in :func:`_run_ps1_package_step`."""
    for root, text in (uv_tomls or {}).items():
        (tmp_path / root / "uv").mkdir(parents=True)
        (tmp_path / root / "uv" / "uv.toml").write_text(text, encoding="utf-8")
    result = _run_ps1(
        tmp_path,
        "uv-step",
        _ps1_settings("ErrorActionPreference", "PypiMirror") + _PS1_FAKE_WEB + _PS1_UV_STEP,
        env={
            "USERPROFILE": str(tmp_path / "home"),
            "HOME": str(tmp_path / "home"),
            "APPDATA": str(tmp_path / "appdata"),
            "PROGRAMDATA": str(tmp_path / "programdata"),
            **(env or {}),
        },
        WebRoot=str(page.parent),
        ServedList=";".join(f"{url}={path.name}" for url, path in served.items()),
    )
    return result, tmp_path / "home" / ".local" / "bin"


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
def test_a_windows_uv_comes_from_the_newest_wheel_on_the_mirror(tmp_path: Path) -> None:
    page, served = _uv_index(tmp_path, "win_amd64", suffix=".exe")

    result, bin_dir = _run_ps1_uv_step(tmp_path, page, {_UV_PAGE: page, **served})

    uv = bin_dir / "uv.exe"
    assert uv.is_file(), result.stdout + result.stderr
    assert "uv 0.10.0" in uv.read_text(encoding="utf-8"), "the newest release, not the last one listed"
    assert (bin_dir / "uvx.exe").is_file()
    assert "pypi.tuna.tsinghua.edu.cn" in result.stdout and "RAVEN_NO_MIRROR" in result.stdout


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
def test_a_windows_uv_wheel_that_does_not_match_its_published_digest_is_not_installed(tmp_path: Path) -> None:
    page, served = _uv_index(tmp_path, "win_amd64", suffix=".exe", tamper=True)

    result, bin_dir = _run_ps1_uv_step(tmp_path, page, {_UV_PAGE: page, **served})

    assert not (bin_dir / "uv.exe").exists(), result.stdout + result.stderr
    assert "does not match" in result.stdout


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
@pytest.mark.parametrize(
    ("env", "uv_tomls"),
    [
        ({"UV_DEFAULT_INDEX": _THEIRS}, None),
        ({"UV_INDEX": f"corp={_THEIRS}"}, None),
        ({}, {"appdata": f'index-url = "{_THEIRS}"\n'}),
        ({}, {"programdata": f'[[index]]\nname = "corp"\nurl = "{_THEIRS}"\ndefault = true\n'}),
        ({}, {"appdata": f"index = [{{ url = '{_THEIRS}', default = true }}]\n"}),
        ({}, {"appdata": f'"index-url" = "{_THEIRS}"\n'}),
        ({}, {"programdata": f'[["index"]]\n"url" = "{_THEIRS}"\n"default" = true\n'}),
        ({}, {"appdata": "\ufeff" + f'index-url = "{_THEIRS}"\n'}),
    ],
    ids=[
        "default-index",
        "named-index",
        "uv-toml-index-url",
        "machines-default-table",
        "inline-default",
        "quoted-index-url",
        "quoted-default-table",
        "uv-toml-with-a-mark",
    ],
)
def test_a_windows_uv_comes_from_the_index_the_user_named(
    tmp_path: Path, env: dict[str, str], uv_tomls: dict[str, str] | None
) -> None:
    page, served = _uv_index(tmp_path, "win_amd64", suffix=".exe")

    result, bin_dir = _run_ps1_uv_step(tmp_path, page, _served_from(_THEIRS, page, served), env=env, uv_tomls=uv_tomls)

    assert (bin_dir / "uv.exe").is_file(), result.stdout + result.stderr
    assert f"{_THEIRS}/uv/" in result.stdout and "tuna" not in result.stdout


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
@pytest.mark.parametrize("extra_lists_uv", [False, True], ids=["extra-lists-none", "extra-lists-uv"])
def test_a_windows_uv_asks_the_named_indexes_in_uvs_order(tmp_path: Path, extra_lists_uv: bool) -> None:
    page, served = _uv_index(tmp_path, "win_amd64", suffix=".exe")
    extra = "https://extra.example.internal/simple"
    empty = page.parent / "empty.html"
    empty.write_text("<html></html>\n", encoding="utf-8")
    first = _served_from(extra, page, served) if extra_lists_uv else {f"{extra}/uv/": empty}

    result, bin_dir = _run_ps1_uv_step(
        tmp_path,
        page,
        {**_served_from(_THEIRS, page, served), **first},
        env={"UV_INDEX": f"extra={extra}", "UV_DEFAULT_INDEX": _THEIRS},
    )

    assert (bin_dir / "uv.exe").is_file(), result.stdout + result.stderr
    if extra_lists_uv:
        assert f"{extra}/uv/" in result.stdout and f"{_THEIRS}/uv/" not in result.stdout
    else:
        assert result.stdout.index(f"{extra}/uv/") < result.stdout.index(f"{_THEIRS}/uv/")
    assert "tuna" not in result.stdout


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
def test_a_windows_uv_toml_index_read_unreadably_keeps_the_mirror_away(tmp_path: Path) -> None:
    page, served = _uv_index(tmp_path, "win_amd64", suffix=".exe")

    result, bin_dir = _run_ps1_uv_step(
        tmp_path,
        page,
        {_UV_PAGE: page, **served},
        uv_tomls={"appdata": _UNREADABLE_INDEX},
    )

    assert not (bin_dir / "uv.exe").exists(), result.stdout + result.stderr
    assert "tuna" not in result.stdout and "UV_DEFAULT_INDEX" in result.stdout


_PS1_ENSURE_UV_STEP = r"""
$names = @("Ensure-Uv", "Test-UvHostChosen", "Test-UvUnreachable", "Test-Reachable", "Get-TunnelStatus", "Test-MirrorsAllowed")
foreach ($text in (Get-Install $names)) { Invoke-Expression $text }
function Write-Info([string]$Message) { }
function Write-Ok([string]$Message) { }
function Write-Warn([string]$Message) { [Console]::Out.WriteLine("WARN $Message") }
function Fail([string]$Message) { [Console]::Out.WriteLine("FAIL $Message"); exit 3 }
function Add-ProcessPath([string]$PathToAdd) { }
# uv in place, whoever put it there, is the marker file.
function Find-Uv { if (Test-Path $Marker) { "C:\fake\uv.exe" } }
function Install-UvFromMirror {
    New-Item -ItemType File -Path $Marker -Force | Out-Null
    [Console]::Out.WriteLine("MIRROR")
    Find-Uv
}
# The installer's own PowerShell is Windows PowerShell; away from Windows, this one.
if ($IsWindows -eq $false) { $UvInstallShell = (Get-Process -Id $PID).Path }
$Served = @{}
foreach ($pair in ($ServedList -split ";")) {
    if ($pair) { $url, $file = $pair -split "=", 2; $Served[$url] = $file }
}
$Down = @($DownList -split ",")
$Untrusted = @($UntrustedList -split ",")
$uv = Ensure-Uv
[Console]::Out.WriteLine("FOUND $uv")
[Console]::Out.WriteLine("COUNT $(@($uv).Count)")
"""

_PS1_UV_INSTALLER_URL = "https://astral.sh/uv/install.ps1"
# uv's installer, the two ways it ends: having put uv in place, printing what it
# did, or the way it ends when its download failed (`catch { ...; exit 1 }`, as
# of 0.13.4).
_PS1_UV_INSTALLER_WORKS = "Write-Output 'downloading uv 0.13.4'\nNew-Item -ItemType File -Path '<MARKER>' | Out-Null\n"
_PS1_UV_INSTALLER_FAILS = "Write-Output 'downloading uv 0.13.4'\ntry { throw 'no binary' } catch { exit 1 }\n"


def _run_ps1_ensure_uv_step(
    tmp_path: Path,
    *,
    installer: str | None = None,
    down: tuple[str, ...] = (),
    untrusted: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
):
    """Ensure-Uv on a machine with no uv. ``installer`` is what astral.sh serves
    for uv's installer; with none, astral.sh answers with a 404."""
    web = tmp_path / "web"
    web.mkdir(exist_ok=True)
    marker = tmp_path / "uv-installed"
    served = {}
    if installer is not None:
        (web / "install.ps1").write_text(installer.replace("<MARKER>", str(marker)), encoding="utf-8")
        served[_PS1_UV_INSTALLER_URL] = "install.ps1"
    result = _run_ps1(
        tmp_path,
        "ensure-uv-step",
        _ps1_settings("ErrorActionPreference", "UvInstaller", "UvInstallShell", "UvGithubRelease", "PythonDownloads")
        + _PS1_FAKE_WEB
        + _PS1_ENSURE_UV_STEP,
        env=env,
        WebRoot=str(web),
        ServedList=";".join(f"{url}={name}" for url, name in served.items()),
        DownList=",".join(down),
        UntrustedList=",".join(untrusted),
        Marker=str(marker),
    )
    return result, result.stdout.splitlines()


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
def test_a_windows_uv_installer_that_works_hands_back_only_where_uv_is(tmp_path: Path) -> None:
    """uv's installer, run in a PowerShell of its own, prints what it did on
    that process's stdout. Left in this function's output it comes back
    beside the path, and the caller runs the lot as the uv command. It still
    reaches the reader's console."""
    result, lines = _run_ps1_ensure_uv_step(tmp_path, installer=_PS1_UV_INSTALLER_WORKS)

    assert "FOUND C:\\fake\\uv.exe" in lines and "COUNT 1" in lines, result.stdout + result.stderr
    assert "downloading uv 0.13.4" in lines, "the installer's own report still reaches the reader"
    assert "MIRROR" not in lines


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
@pytest.mark.parametrize(("down", "mirrored"), _UV_HOSTS_DOWN, ids=_UV_HOSTS_DOWN_IDS)
def test_a_windows_uv_installer_that_exits_hands_over_to_the_mirror_only_when_its_hosts_cannot_be_reached(
    tmp_path: Path, down: tuple[str, ...], mirrored: bool
) -> None:
    """uv's installer ends a failed download with `exit 1`. Run inside this
    script it would end the install with it -- and under `irm | iex` the
    reader's whole session -- before the mirror is asked. It does not say why
    it failed, so the hosts it downloads uv from are asked."""
    result, lines = _run_ps1_ensure_uv_step(tmp_path, installer=_PS1_UV_INSTALLER_FAILS, down=down)

    assert ("MIRROR" in lines) is mirrored, result.stdout + result.stderr
    assert ("FOUND C:\\fake\\uv.exe" in lines) is mirrored
    assert any(line.startswith("WARN uv's installer did not finish") for line in lines)


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
@pytest.mark.parametrize(
    ("down", "untrusted", "mirrored"),
    [(("https://astral.sh/",), (), True), ((), (), False), ((), ("https://astral.sh/",), False)],
    ids=["unreachable", "http-error", "certificate"],
)
def test_a_windows_uv_comes_from_the_mirror_only_when_its_installer_could_not_be_reached(
    tmp_path: Path, down: tuple[str, ...], untrusted: tuple[str, ...], mirrored: bool
) -> None:
    """astral.sh's answer -- an HTTP error, a certificate that fails
    verification -- is final, and installing uv from another host would hide
    it."""
    result, lines = _run_ps1_ensure_uv_step(
        tmp_path,
        installer=_PS1_UV_INSTALLER_WORKS if untrusted else None,
        down=down,
        untrusted=untrusted,
    )

    assert ("MIRROR" in lines) is mirrored, result.stdout + result.stderr
    assert ("FOUND C:\\fake\\uv.exe" in lines) is mirrored


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
def test_the_windows_opt_out_leaves_uv_to_its_own_installer(tmp_path: Path) -> None:
    result, lines = _run_ps1_ensure_uv_step(tmp_path, down=("https://astral.sh/",), env={"RAVEN_NO_MIRROR": "1"})

    assert "MIRROR" not in lines, result.stdout + result.stderr
    assert any(line.startswith("FAIL uv could not be installed") for line in lines)


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
@pytest.mark.parametrize("name", _UV_HOST_SETTINGS)
@pytest.mark.parametrize("installer", ["left-no-uv", "unreachable"])
def test_a_host_the_user_chose_for_the_windows_uv_installer_is_never_replaced(
    tmp_path: Path, name: str, installer: str
) -> None:
    down = ("https://releases.astral.sh/", "https://github.com/")
    if installer == "unreachable":
        down += ("https://astral.sh/",)

    result, lines = _run_ps1_ensure_uv_step(
        tmp_path,
        installer=_PS1_UV_INSTALLER_FAILS if installer == "left-no-uv" else None,
        down=down,
        env={name: "https://uv.example.internal"},
    )

    assert "MIRROR" not in lines, result.stdout + result.stderr
    assert any(line.startswith("FAIL uv could not be installed") for line in lines)


def test_the_windows_installer_runs_uvs_installer_in_windows_powershell() -> None:
    """As uv documents it (`powershell -ExecutionPolicy ByPass -c ...`):
    Windows PowerShell is on every Windows, whichever PowerShell runs this
    script."""
    assert _ps1_settings("UvInstallShell").strip() == '$UvInstallShell = "powershell.exe"'


# --- PyPI and the Python uv fetches ----------------------------------------------

_PACKAGE_STEP = (
    "mirrors_allowed",
    "connection_failed",
    "reachable",
    "use_mirror",
    "uv_config_files",
    "uv_config_text",
    "uv_config_has",
    "uv_index_chosen",
    "use_package_mirrors",
)
_PACKAGE_SETTINGS = ("PYPI_INDEX", "PYPI_MIRROR", "PYTHON_DOWNLOADS", "PYTHON_MIRROR", "MIRROR_EXPORTS")


def _run_package_step(
    tmp_path: Path,
    *,
    down: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
    uv_toml: str | None = None,
    machine_uv_toml: str | None = None,
):
    log = tmp_path / "probes.log"
    _fake_curl(tmp_path / "bin", {}, down=down, log=log)
    for root, text in (("config", uv_toml), ("xdg", machine_uv_toml)):
        if text is not None:
            (tmp_path / root / "uv").mkdir(parents=True)
            (tmp_path / root / "uv" / "uv.toml").write_text(text, encoding="utf-8")
    harness = tmp_path / "package-step.sh"
    harness.write_text(
        _SH_PREAMBLE
        + _without_the_machines_etc(_sh_functions(_PACKAGE_STEP, _PACKAGE_SETTINGS), tmp_path)
        + "use_package_mirrors\n"
        'echo "INDEX=${UV_DEFAULT_INDEX:-}"\n'
        'echo "PYTHON=${UV_PYTHON_INSTALL_MIRROR:-}"\n'
        'echo "EXPORTED=$MIRROR_EXPORTS"\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["sh", str(harness)],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin",
            "HOME": str(tmp_path),
            "XDG_CONFIG_HOME": str(tmp_path / "config"),
            "XDG_CONFIG_DIRS": f"{tmp_path / 'nowhere'}:{tmp_path / 'xdg'}",
            **(env or {}),
        },
    )
    said = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    probes = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
    return result, said, probes


@POSIX_SH_ONLY
def test_uv_installs_from_the_pypi_mirror_when_pypi_cannot_be_reached(tmp_path: Path) -> None:
    result, said, _probes = _run_package_step(tmp_path, down=("https://pypi.org/",))

    assert said["INDEX"] == "https://pypi.tuna.tsinghua.edu.cn/simple"
    assert said["PYTHON"] == "", "releases.astral.sh answered"
    assert "UV_DEFAULT_INDEX" in said["EXPORTED"], "remembered, so the page starts without it"
    assert "RAVEN_NO_MIRROR" in result.stderr


@POSIX_SH_ONLY
def test_uv_fetches_its_python_from_the_mirror_when_releases_astral_sh_cannot_be_reached(tmp_path: Path) -> None:
    result, said, _probes = _run_package_step(tmp_path, down=("https://pypi.org/", "https://releases.astral.sh/"))

    assert said["PYTHON"] == "https://registry.npmmirror.com/-/binary/python-build-standalone"
    assert any("releases.astral.sh" in line and "RAVEN_NO_MIRROR" in line for line in result.stderr.splitlines())


@POSIX_SH_ONLY
def test_hosts_that_answer_are_left_alone(tmp_path: Path) -> None:
    _result, said, probes = _run_package_step(tmp_path)

    assert said["INDEX"] == "" and said["PYTHON"] == ""
    assert probes == ["https://pypi.org/simple/", "https://releases.astral.sh/"]


@POSIX_SH_ONLY
@pytest.mark.parametrize("name", ["UV_DEFAULT_INDEX", "UV_INDEX_URL", "UV_INDEX"])
def test_an_index_the_user_named_is_kept(tmp_path: Path, name: str) -> None:
    _result, said, probes = _run_package_step(
        tmp_path, down=("https://pypi.org/",), env={name: "https://pypi.example.internal/simple"}
    )

    assert "tuna" not in said["INDEX"]
    assert "https://pypi.org/simple/" not in probes, "a choice already made is not second-guessed"


@POSIX_SH_ONLY
@pytest.mark.parametrize(
    "text",
    [
        'index-url = "https://pypi.example.internal/simple"\n',
        '[[index]]\nurl = "https://pypi.example.internal/simple"\ndefault = true\n',
        'index = [{ url = "https://pypi.example.internal/simple", default = true }]\n',
        'index = [\n  { name = "corp", url = "https://pypi.example.internal/simple", default = true },\n]\n',
        '"index-url" = "https://pypi.example.internal/simple"\n',
        '"default" = true\n',
        "\ufeff" + 'index-url = "https://pypi.example.internal/simple"\n',
    ],
    ids=[
        "index-url",
        "default-index",
        "inline-default-index",
        "inline-default-index-in-a-list",
        "quoted-index-url",
        "quoted-default",
        "a-mark",
    ],
)
def test_an_index_in_the_users_uv_toml_is_kept(tmp_path: Path, text: str) -> None:
    _result, said, _probes = _run_package_step(tmp_path, down=("https://pypi.org/",), uv_toml=text)

    assert said["INDEX"] == ""


@POSIX_SH_ONLY
def test_an_index_that_is_not_the_default_leaves_pypi_to_the_mirror(tmp_path: Path) -> None:
    """uv still asks PyPI for whatever such an index does not carry."""
    _result, said, _probes = _run_package_step(
        tmp_path,
        down=("https://pypi.org/",),
        uv_toml='index = [{ url = "https://pypi.example.internal/simple", explicit = true }]\n',
    )

    assert said["INDEX"] == "https://pypi.tuna.tsinghua.edu.cn/simple"


@POSIX_SH_ONLY
def test_an_index_in_the_machines_uv_toml_is_kept(tmp_path: Path) -> None:
    """uv reads the machine's uv.toml from each of XDG_CONFIG_DIRS before /etc."""
    _result, said, _probes = _run_package_step(
        tmp_path, down=("https://pypi.org/",), machine_uv_toml='index-url = "https://pypi.example.internal/simple"\n'
    )

    assert said["INDEX"] == ""


@POSIX_SH_ONLY
@pytest.mark.parametrize(
    ("named", "users", "index"),
    [
        ('index-url = "https://pypi.example.internal/simple"\n', None, ""),
        ("", 'index-url = "https://pypi.example.internal/simple"\n', "https://pypi.tuna.tsinghua.edu.cn/simple"),
    ],
    ids=["named-file-chooses", "users-file-unread"],
)
def test_a_uv_toml_uv_config_file_names_is_read_alone(
    tmp_path: Path, named: str, users: str | None, index: str
) -> None:
    """uv reads the file UV_CONFIG_FILE names instead of the user's and the machine's."""
    path = tmp_path / "named.toml"
    path.write_text(named, encoding="utf-8")
    _result, said, _probes = _run_package_step(
        tmp_path, down=("https://pypi.org/",), env={"UV_CONFIG_FILE": str(path)}, uv_toml=users
    )

    assert said["INDEX"] == index


# Each form in which uv is told where a Python comes from: a mirror of the
# builds, a list of downloads of the user's own, a copy of releases.astral.sh.
_PYTHON_SOURCES = [
    ({"UV_PYTHON_INSTALL_MIRROR": "https://python.example.internal"}, None),
    ({"UV_PYTHON_DOWNLOADS_JSON_URL": "https://python.example.internal/downloads.json"}, None),
    ({"UV_ASTRAL_MIRROR_URL": "https://astral.example.internal"}, None),
    ({}, 'python-install-mirror = "https://python.example.internal"\n'),
    ({}, 'python-downloads-json-url = "https://python.example.internal/downloads.json"\n'),
]
_PYTHON_SOURCE_IDS = ["mirror", "downloads-list", "astral-copy", "uv-toml-mirror", "uv-toml-downloads-list"]


@POSIX_SH_ONLY
@pytest.mark.parametrize(("env", "uv_toml"), _PYTHON_SOURCES, ids=_PYTHON_SOURCE_IDS)
def test_a_python_source_the_user_chose_is_kept(tmp_path: Path, env: dict[str, str], uv_toml: str | None) -> None:
    _result, said, probes = _run_package_step(tmp_path, down=("https://releases.astral.sh/",), env=env, uv_toml=uv_toml)

    assert said["PYTHON"] == env.get("UV_PYTHON_INSTALL_MIRROR", "")
    assert "https://releases.astral.sh/" not in probes, "a choice already made is not second-guessed"


@POSIX_SH_ONLY
def test_the_opt_out_keeps_pypi(tmp_path: Path) -> None:
    _result, said, probes = _run_package_step(
        tmp_path, down=("https://pypi.org/", "https://releases.astral.sh/"), env={"RAVEN_NO_MIRROR": "1"}
    )

    assert said["INDEX"] == "" and said["PYTHON"] == ""
    assert probes == []


def test_both_installers_choose_the_package_hosts_before_the_first_install() -> None:
    sh = INSTALL_SH.read_text(encoding="utf-8")
    body = sh[sh.index("install_raven() {") :]
    assert body.index("use_package_mirrors") < body.index("uv tool install")
    ps1 = INSTALL_PS1.read_text(encoding="utf-8")
    body = ps1[ps1.index("function Install-Raven") :]
    assert body.index("Use-PackageMirrors") < body.index("tool install")


_PS1_PACKAGE_STEP = r"""
$names = @(
    "Test-MirrorsAllowed", "Test-Reachable", "Get-TunnelStatus", "Set-MirrorEnv", "Restore-MirrorEnv",
    "Get-UvConfigFiles", "Get-UvConfigText", "Get-UvConfigLines", "Test-UvConfigHas", "Get-UvConfigIndex",
    "Test-UvIndexChosen", "Use-PackageMirrors"
)
foreach ($text in (Get-Install $names)) { Invoke-Expression $text }
function Write-Warn([string]$Message) { [Console]::Out.WriteLine("WARN $Message") }
$Served = @{}
$Down = @($DownList -split ",")
$named = [string](Get-UvConfigIndex)
[Console]::Out.WriteLine("CONFIG=$named")
Use-PackageMirrors
[Console]::Out.WriteLine("INDEX=$env:UV_DEFAULT_INDEX")
[Console]::Out.WriteLine("PYTHON=$env:UV_PYTHON_INSTALL_MIRROR")
Restore-MirrorEnv
[Console]::Out.WriteLine("RESTORED=$env:UV_DEFAULT_INDEX$env:UV_PYTHON_INSTALL_MIRROR")
"""


def _run_ps1_package_step(
    tmp_path: Path,
    *,
    down: tuple[str, ...] = (),
    env: dict[str, str] | None = None,
    uv_tomls: dict[str, str] | None = None,
    shell: str = "pwsh",
):
    dist = tmp_path / "dist"
    dist.mkdir(exist_ok=True)
    # A bare root is uv's own location under tmp_path ("appdata" and
    # "programdata" are the user's and the machine's, any other root a file
    # UV_CONFIG_FILE can name). An absolute root is the file itself.
    for root, text in (uv_tomls or {}).items():
        given = Path(root)
        target = given if given.is_absolute() else tmp_path / given / "uv" / "uv.toml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    settings = _ps1_settings("ErrorActionPreference", "PypiIndex", "PypiMirror", "PythonDownloads", "PythonMirror")
    result = _run_ps1(
        tmp_path,
        "package-step",
        settings + "$script:MirrorSaved = @{}\n" + _PS1_FAKE_WEB + _PS1_PACKAGE_STEP,
        env={"APPDATA": str(tmp_path / "appdata"), "PROGRAMDATA": str(tmp_path / "programdata"), **(env or {})},
        WebRoot=str(dist),
        DownList=",".join(down),
        shell=shell,
    )
    return result, dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
def test_a_windows_install_takes_packages_and_pythons_from_the_mirrors_and_gives_the_session_back(
    tmp_path: Path,
) -> None:
    result, said = _run_ps1_package_step(tmp_path, down=("https://pypi.org/", "https://releases.astral.sh/"))

    assert said["INDEX"] == "https://pypi.tuna.tsinghua.edu.cn/simple", result.stdout + result.stderr
    assert said["PYTHON"] == "https://registry.npmmirror.com/-/binary/python-build-standalone"
    assert said["RESTORED"] == "", "the session that ran `irm | iex` keeps its own environment"
    assert "RAVEN_NO_MIRROR" in result.stdout
    assert any("releases.astral.sh" in line and "RAVEN_NO_MIRROR" in line for line in result.stdout.splitlines())


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
def test_a_windows_install_keeps_an_index_the_user_named(tmp_path: Path) -> None:
    _result, said = _run_ps1_package_step(
        tmp_path, down=("https://pypi.org/",), env={"UV_INDEX_URL": "https://pypi.example.internal/simple"}
    )

    assert said["INDEX"] == ""


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
@pytest.mark.parametrize("root", ["appdata", "programdata", "named"])
def test_a_windows_install_keeps_an_index_in_a_uv_toml(tmp_path: Path, root: str) -> None:
    """uv on Windows reads the user's uv.toml under APPDATA and the machine's
    under PROGRAMDATA, or only the file UV_CONFIG_FILE names."""
    named = {"UV_CONFIG_FILE": str(tmp_path / "named" / "uv" / "uv.toml")} if root == "named" else None
    _result, said = _run_ps1_package_step(
        tmp_path,
        down=("https://pypi.org/",),
        env=named,
        uv_tomls={root: 'index-url = "https://pypi.example.internal/simple"\n'},
    )

    assert said["INDEX"] == ""


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
@pytest.mark.parametrize(
    "text",
    [
        'index = [{ url = "https://pypi.example.internal/simple", default = true }]\n',
        'index = [\n  { name = "corp", url = "https://pypi.example.internal/simple", default = true },\n]\n',
    ],
    ids=["inline-default-index", "inline-default-index-in-a-list"],
)
def test_a_windows_install_keeps_an_inline_default_index(tmp_path: Path, text: str) -> None:
    _result, said = _run_ps1_package_step(tmp_path, down=("https://pypi.org/",), uv_tomls={"appdata": text})

    assert said["INDEX"] == ""


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
@pytest.mark.parametrize(
    "text",
    [
        '"index-url" = "https://pypi.example.internal/simple"\n',
        '[["index"]]\n"url" = "https://pypi.example.internal/simple"\n"default" = true\n',
        "\ufeff" + 'index-url = "https://pypi.example.internal/simple"\n',
        "\ufeff" + '[["index"]]\n"url" = "https://pypi.example.internal/simple"\n"default" = true\n',
    ],
    ids=["quoted-index-url", "quoted-default-table", "a-mark", "a-mark-and-quoted-keys"],
)
def test_a_windows_install_keeps_an_index_a_uv_toml_names_in_any_form(tmp_path: Path, text: str) -> None:
    """uv reads a quoted key and a file with a byte-order mark; the installer
    reads them too, or the mirror replaces the user's index."""
    _result, said = _run_ps1_package_step(tmp_path, down=("https://pypi.org/",), uv_tomls={"appdata": text})

    assert said["INDEX"] == ""


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
def test_a_windows_install_reads_a_uv_config_file_with_brackets(tmp_path: Path) -> None:
    """Test-Path and Get-Content treat brackets as wildcards; uv does not."""
    chosen = tmp_path / "selected [config] data"
    named = {"UV_CONFIG_FILE": str(chosen / "uv.toml")}
    _result, said = _run_ps1_package_step(
        tmp_path,
        down=("https://pypi.org/",),
        env=named,
        uv_tomls={str(chosen / "uv.toml"): 'index-url = "https://pypi.example.internal/simple"\n'},
    )

    assert said["INDEX"] == ""


@pytest.mark.slow
@pytest.mark.parametrize("shell", ["pwsh", "powershell"])
def test_a_windows_install_decodes_a_uv_config_file_as_utf8(tmp_path: Path, shell: str) -> None:
    """Windows PowerShell 5.1 reads BOM-less files through the ANSI code page
    unless told otherwise, which corrupts a non-ASCII index URL. The value
    reader is asserted directly, because recognizing the ASCII key alone
    cannot tell an intact URL from a mis-decoded one."""
    if shutil.which(shell) is None:
        pytest.skip(f"needs {shell}")
    chosen = tmp_path / "utf8"
    chosen.mkdir()
    url = "https://pypi.example.internal/caf\u00e9/simple"
    (chosen / "uv.toml").write_bytes(f'index-url = "{url}"\n'.encode("utf-8"))
    result, said = _run_ps1_package_step(
        tmp_path,
        down=("https://pypi.org/",),
        env={"UV_CONFIG_FILE": str(chosen / "uv.toml")},
        shell=shell,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert said["CONFIG"] == url
    assert said["INDEX"] == ""


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
def test_a_windows_install_reads_only_the_uv_toml_uv_config_file_names(tmp_path: Path) -> None:
    _result, said = _run_ps1_package_step(
        tmp_path,
        down=("https://pypi.org/",),
        env={"UV_CONFIG_FILE": str(tmp_path / "named" / "uv" / "uv.toml")},
        uv_tomls={"named": "", "appdata": 'index-url = "https://pypi.example.internal/simple"\n'},
    )

    assert said["INDEX"] == "https://pypi.tuna.tsinghua.edu.cn/simple"


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
@pytest.mark.parametrize(("env", "uv_toml"), _PYTHON_SOURCES, ids=_PYTHON_SOURCE_IDS)
def test_a_windows_install_keeps_a_python_source_the_user_chose(
    tmp_path: Path, env: dict[str, str], uv_toml: str | None
) -> None:
    uv_tomls = {"appdata": uv_toml} if uv_toml is not None else None
    _result, said = _run_ps1_package_step(tmp_path, down=("https://releases.astral.sh/",), env=env, uv_tomls=uv_tomls)

    assert said["PYTHON"] == env.get("UV_PYTHON_INSTALL_MIRROR", "")


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
def test_a_windows_install_whose_hosts_answer_is_left_alone(tmp_path: Path) -> None:
    _result, said = _run_ps1_package_step(tmp_path)

    assert said["INDEX"] == "" and said["PYTHON"] == ""


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
def test_the_windows_opt_out_keeps_pypi(tmp_path: Path) -> None:
    _result, said = _run_ps1_package_step(tmp_path, down=("https://pypi.org/",), env={"RAVEN_NO_MIRROR": "1"})

    assert said["INDEX"] == ""


# --- The npm registry a source build reads -------------------------------------

_NPM_STEP = (
    "mirrors_allowed",
    "connection_failed",
    "reachable",
    "npm_registry_answers",
    "use_npm_mirror",
    "npm_run",
)
_NPM_SETTINGS = ("NPM_REGISTRY", "NPM_MIRROR", "NPM_ANSWERS")


def _run_npm_step(
    tmp_path: Path,
    *,
    down: tuple[str, ...] = (),
    configured: str | None = "https://registry.npmjs.org/",
    env: dict[str, str] | None = None,
    build_npmrc: str | None = None,
    caller_npmrc: str | None = None,
):
    """``configured`` is what npm answers `config get registry` with; None is an
    npm that fails, after printing the default all the same. The stand-in
    answers from the .npmrc of the directory it runs in, as npm does."""
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    answer = "echo 'https://registry.npmjs.org/'; exit 1" if configured is None else f"echo '{configured}'"
    (node_dir / "npm").write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2 $3" = "config get registry" ]; then\n'
        '  if [ -f .npmrc ] && grep -q "^registry=" .npmrc; then\n'
        '    sed -n "s/^registry=//p" .npmrc\n'
        f"  else\n    {answer}\n  fi\n"
        "fi\n",
        encoding="utf-8",
    )
    (node_dir / "npm").chmod(0o755)
    _fake_curl(tmp_path / "bin", {}, down=down)
    build = tmp_path / "build"
    build.mkdir(exist_ok=True)
    if build_npmrc is not None:
        (build / ".npmrc").write_text(build_npmrc, encoding="utf-8")
    caller = tmp_path / "caller"
    caller.mkdir(exist_ok=True)
    if caller_npmrc is not None:
        (caller / ".npmrc").write_text(caller_npmrc, encoding="utf-8")
    harness = tmp_path / "npm-step.sh"
    harness.write_text(
        _SH_PREAMBLE
        + _sh_functions(_NPM_STEP, _NPM_SETTINGS)
        + f"registry=\"$(use_npm_mirror '{node_dir}' '{build}')\"\n"
        + 'echo "REGISTRY=$registry"\n'
        + f"npm_run \"$registry\" '{node_dir}' '{build}' sh -c 'echo \"INSIDE=${{npm_config_registry:-}}\"'\n"
        + 'echo "AFTER=${npm_config_registry:-}"\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["sh", str(harness)],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(caller),
        env={"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin", **(env or {})},
    )
    return result, dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


@POSIX_SH_ONLY
def test_a_source_build_installs_from_the_npm_mirror_when_the_registry_cannot_be_reached(tmp_path: Path) -> None:
    result, said = _run_npm_step(tmp_path, down=("https://registry.npmjs.org/",))

    assert said["REGISTRY"] == "https://registry.npmmirror.com"
    assert said["INSIDE"] == "https://registry.npmmirror.com", "the install reads the mirror"
    assert said["AFTER"] == "", "and nothing of it stays with the installer"
    assert "RAVEN_NO_MIRROR" in result.stderr


@POSIX_SH_ONLY
def test_a_registry_npm_is_configured_with_is_kept(tmp_path: Path) -> None:
    _result, said = _run_npm_step(
        tmp_path, down=("https://registry.npmjs.org/",), configured="https://registry.example.internal/"
    )

    assert said["REGISTRY"] == ""


@POSIX_SH_ONLY
def test_a_registry_that_answers_is_kept(tmp_path: Path) -> None:
    _result, said = _run_npm_step(tmp_path)

    assert said["REGISTRY"] == ""


@POSIX_SH_ONLY
def test_an_npm_that_cannot_say_which_registry_it_reads_is_left_alone(tmp_path: Path) -> None:
    """Not knowing is not the same as npm's default: the choice may be one the
    user made where this script cannot see it."""
    _result, said = _run_npm_step(tmp_path, down=("https://registry.npmjs.org/",), configured=None)

    assert said["REGISTRY"] == ""


@POSIX_SH_ONLY
def test_the_opt_out_keeps_the_registry(tmp_path: Path) -> None:
    _result, said = _run_npm_step(tmp_path, down=("https://registry.npmjs.org/",), env={"RAVEN_NO_MIRROR": "1"})

    assert said["REGISTRY"] == ""


@POSIX_SH_ONLY
def test_the_two_builds_do_not_override_each_other_s_choice(tmp_path: Path) -> None:
    """Each build asks npm in its own directory, and each keeps the registry
    its own project names: the bundle and the page are two projects."""
    node_dir = tmp_path / "node"
    node_dir.mkdir()
    (node_dir / "npm").write_text(
        "#!/bin/sh\n"
        'if [ "$1 $2 $3" = "config get registry" ]; then\n'
        '  if [ -f .npmrc ] && grep -q "^registry=" .npmrc; then\n'
        '    sed -n "s/^registry=//p" .npmrc\n'
        "  else\n    echo 'https://registry.npmjs.org/'\n  fi\n"
        "fi\n",
        encoding="utf-8",
    )
    (node_dir / "npm").chmod(0o755)
    _fake_curl(tmp_path / "bin", {}, down=("https://registry.npmjs.org/",))
    tui = tmp_path / "ui-tui"
    web = tmp_path / "ui-web"
    tui.mkdir()
    web.mkdir()
    (web / ".npmrc").write_text("registry=https://registry.example.internal/\n", encoding="utf-8")
    harness = tmp_path / "npm-two.sh"
    harness.write_text(
        _SH_PREAMBLE
        + _sh_functions(_NPM_STEP, _NPM_SETTINGS)
        + f"tui=\"$(use_npm_mirror '{node_dir}' '{tui}')\"\n"
        + f"web=\"$(use_npm_mirror '{node_dir}' '{web}')\"\n"
        + 'echo "TUI=$tui"\n'
        + 'echo "WEB=$web"\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        ["sh", str(harness)],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": f"{tmp_path / 'bin'}:/usr/bin:/bin"},
    )
    said = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)

    assert said["TUI"] == "https://registry.npmmirror.com", "the build that names no registry takes the mirror"
    assert said["WEB"] == "", "and the one that names a registry keeps it"


@POSIX_SH_ONLY
def test_the_registry_is_asked_where_the_install_runs(tmp_path: Path) -> None:
    """npm reads the .npmrc of the project it runs in, so the question is
    asked there: a registry named beside the install is kept."""
    _result, said = _run_npm_step(
        tmp_path,
        down=("https://registry.npmjs.org/",),
        build_npmrc="registry=https://registry.example.internal/\n",
    )

    assert said["REGISTRY"] == "", "the registry the project names is kept"
    assert said["INSIDE"] == ""


@POSIX_SH_ONLY
def test_a_registry_beside_the_installer_is_not_the_projects(tmp_path: Path) -> None:
    """The other way round: started from a project whose .npmrc names a
    registry, the install goes to npmjs, and the fallback stands."""
    _result, said = _run_npm_step(
        tmp_path,
        down=("https://registry.npmjs.org/",),
        caller_npmrc="registry=https://registry.example.internal/\n",
    )

    assert said["REGISTRY"] == "https://registry.npmmirror.com"


def test_both_installers_choose_the_registry_before_npm_ci() -> None:
    sh = INSTALL_SH.read_text(encoding="utf-8")
    body = sh[sh.index("build_web_assets() {") :]
    assert body.index("use_npm_mirror") < body.index("npm ci")
    ps1 = INSTALL_PS1.read_text(encoding="utf-8")
    body = ps1[ps1.index("function Build-WebAssets") :]
    assert body.index("Use-NpmMirror (Get-NpmRegistry $npm)") < body.index("& $npm.Source ci")


def test_both_installers_ask_npm_where_they_install() -> None:
    """npm reads the .npmrc of the project it runs in, so the question is
    asked inside the directory the install runs in."""
    sh = INSTALL_SH.read_text(encoding="utf-8")
    assert 'use_npm_mirror "$node_dir" "$src/ui-tui"' in sh
    assert 'use_npm_mirror "$2" "$1/ui-web"' in sh
    ps1 = INSTALL_PS1.read_text(encoding="utf-8")
    body = ps1[ps1.index("function Build-WebAssets") :]
    for directory in ("ui-tui", "ui-web"):
        block = body[body.index(f'Push-Location (Join-Path $ScriptDir "{directory}")') :]
        assert block.index("Use-NpmMirror (Get-NpmRegistry $npm)") > block.index("Push-Location"), directory


_PS1_NPM_STEP = r"""
foreach ($text in (Get-Install @("Test-MirrorsAllowed", "Test-Reachable", "Get-TunnelStatus", "Set-MirrorEnv", "Test-NpmRegistryReachable", "Use-NpmMirror", "Restore-NpmMirror"))) {
    Invoke-Expression $text
}
function Write-Warn([string]$Message) { [Console]::Out.WriteLine("WARN $Message") }
$script:NpmAnswers = $null
$Served = @{}
$Down = @($DownList -split ",")
Use-NpmMirror $(if ($Configured -eq "<cannot say>") { $null } else { $Configured })
[Console]::Out.WriteLine("REGISTRY=$env:npm_config_registry")
Restore-NpmMirror
[Console]::Out.WriteLine("AFTER=$env:npm_config_registry")
"""


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
@pytest.mark.parametrize(
    ("down", "configured", "expected"),
    [
        (("https://registry.npmjs.org/",), "https://registry.npmjs.org/", "https://registry.npmmirror.com"),
        (("https://registry.npmjs.org/",), "https://registry.example.internal/", ""),
        ((), "https://registry.npmjs.org/", ""),
        (("https://registry.npmjs.org/",), "<cannot say>", ""),
    ],
    ids=["unreachable", "theirs", "answers", "npm-cannot-say"],
)
def test_a_windows_source_build_takes_the_npm_mirror_only_when_it_must(
    tmp_path: Path, down: tuple[str, ...], configured: str, expected: str
) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    result = _run_ps1(
        tmp_path,
        "npm-step",
        _ps1_settings("ErrorActionPreference", "NpmRegistry", "NpmMirror")
        + "$script:MirrorSaved = @{}\n"
        + _PS1_FAKE_WEB
        + _PS1_NPM_STEP,
        env={"npm_config_registry": "", "NPM_CONFIG_REGISTRY": ""},
        WebRoot=str(dist),
        DownList=",".join(down),
        Configured=configured,
    )

    said = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert said["REGISTRY"] == expected, result.stdout + result.stderr
    assert said["AFTER"] == "", "the fallback is put back after the build"


_PS1_NPM_DIR_STEP = r"""
foreach ($text in (Get-Install @("Test-MirrorsAllowed", "Test-Reachable", "Get-TunnelStatus", "Set-MirrorEnv", "Test-NpmRegistryReachable", "Use-NpmMirror", "Restore-NpmMirror", "Get-NpmRegistry"))) {
    Invoke-Expression $text
}
function Write-Warn([string]$Message) { [Console]::Out.WriteLine("WARN $Message") }
$script:NpmAnswers = $null
$Served = @{}
$Down = @($DownList -split ",")
$npm = [pscustomobject]@{ Source = $NpmPath }
Push-Location $CallerDir
$outside = Get-NpmRegistry $npm
Pop-Location
Push-Location $BuildDir
Use-NpmMirror (Get-NpmRegistry $npm)
[Console]::Out.WriteLine("OUTSIDE=$outside")
[Console]::Out.WriteLine("REGISTRY=$env:npm_config_registry")
Restore-NpmMirror
[Console]::Out.WriteLine("AFTER=$env:npm_config_registry")
Pop-Location
"""


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
@pytest.mark.parametrize(
    ("build_npmrc", "caller_npmrc", "expected"),
    [
        ("registry=https://registry.example.internal/\n", None, ""),
        (None, "registry=https://registry.example.internal/\n", "https://registry.npmmirror.com"),
    ],
    ids=["the-projects-own", "the-installers-own"],
)
def test_a_windows_source_build_asks_npm_where_the_install_runs(
    tmp_path: Path, build_npmrc: str | None, caller_npmrc: str | None, expected: str
) -> None:
    """npm reads the .npmrc of the project it runs in, so the question is
    asked there: a registry named beside the install is kept, and one beside
    the installer is not the project's."""
    dist = tmp_path / "dist"
    dist.mkdir()
    build = tmp_path / "build"
    build.mkdir()
    if build_npmrc is not None:
        (build / ".npmrc").write_text(build_npmrc, encoding="utf-8")
    caller = tmp_path / "caller"
    caller.mkdir()
    if caller_npmrc is not None:
        (caller / ".npmrc").write_text(caller_npmrc, encoding="utf-8")
    npm = tmp_path / "npm.ps1"
    npm.write_text(
        "if (\"$args\" -eq 'config get registry') {\n"
        "  if (Test-Path .npmrc) { (Get-Content .npmrc | Where-Object { $_ -like 'registry=*' }) -replace '^registry=', '' }\n"
        "  else { 'https://registry.npmjs.org/' }\n"
        "}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    result = _run_ps1(
        tmp_path,
        "npm-dir-step",
        _ps1_settings("ErrorActionPreference", "NpmRegistry", "NpmMirror")
        + "$script:MirrorSaved = @{}\n"
        + _PS1_FAKE_WEB
        + _PS1_NPM_DIR_STEP,
        env={"npm_config_registry": "", "NPM_CONFIG_REGISTRY": ""},
        WebRoot=str(dist),
        DownList="https://registry.npmjs.org/",
        BuildDir=str(build),
        CallerDir=str(caller),
        NpmPath=str(npm),
    )

    said = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert said["REGISTRY"] == expected, result.stdout + result.stderr
    if expected:
        assert said["OUTSIDE"] == "https://registry.example.internal/", "another directory's registry"


_PS1_NPM_TWO_STEP = r"""
foreach ($text in (Get-Install @("Test-MirrorsAllowed", "Test-Reachable", "Get-TunnelStatus", "Set-MirrorEnv", "Test-NpmRegistryReachable", "Use-NpmMirror", "Restore-NpmMirror", "Get-NpmRegistry"))) {
    Invoke-Expression $text
}
function Write-Warn([string]$Message) { [Console]::Out.WriteLine("WARN $Message") }
$script:NpmAnswers = $null
$Served = @{}
$Down = @($DownList -split ",")
$npm = [pscustomobject]@{ Source = $NpmPath }
Push-Location $FirstDir
Use-NpmMirror (Get-NpmRegistry $npm)
[Console]::Out.WriteLine("FIRST=$env:npm_config_registry")
Restore-NpmMirror
Pop-Location
Push-Location $SecondDir
Use-NpmMirror (Get-NpmRegistry $npm)
[Console]::Out.WriteLine("SECOND=$env:npm_config_registry")
Restore-NpmMirror
Pop-Location
"""


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
def test_a_windows_source_build_keeps_the_two_builds_apart(tmp_path: Path) -> None:
    """Each build asks npm in its own directory, and each keeps the registry
    its own project names: the bundle and the page are two projects."""
    dist = tmp_path / "dist"
    dist.mkdir()
    first = tmp_path / "ui-tui"
    second = tmp_path / "ui-web"
    first.mkdir()
    second.mkdir()
    (second / ".npmrc").write_text("registry=https://registry.example.internal/\n", encoding="utf-8")
    npm = tmp_path / "npm.ps1"
    npm.write_text(
        "if (\"$args\" -eq 'config get registry') {\n"
        "  if (Test-Path .npmrc) { (Get-Content .npmrc | Where-Object { $_ -like 'registry=*' }) -replace '^registry=', '' }\n"
        "  else { 'https://registry.npmjs.org/' }\n"
        "}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    result = _run_ps1(
        tmp_path,
        "npm-two-step",
        _ps1_settings("ErrorActionPreference", "NpmRegistry", "NpmMirror")
        + "$script:MirrorSaved = @{}\n"
        + _PS1_FAKE_WEB
        + _PS1_NPM_TWO_STEP,
        env={"npm_config_registry": "", "NPM_CONFIG_REGISTRY": ""},
        WebRoot=str(dist),
        DownList="https://registry.npmjs.org/",
        FirstDir=str(first),
        SecondDir=str(second),
        NpmPath=str(npm),
    )

    said = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    assert said["FIRST"] == "https://registry.npmmirror.com", result.stdout + result.stderr
    assert said["SECOND"] == "", "the build that names a registry keeps it"


_PS1_NPM_REGISTRY_STEP = r"""
foreach ($text in (Get-Install @("Get-NpmRegistry"))) { Invoke-Expression $text }
$registry = Get-NpmRegistry ([pscustomobject]@{ Source = $FakeNpm })
[Console]::Out.WriteLine("REGISTRY=$(if ($null -eq $registry) { '<cannot say>' } else { $registry })")
"""


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
@pytest.mark.parametrize(
    ("answer", "said"),
    [
        ("'https://registry.npmjs.org/'; exit 0", "https://registry.npmjs.org/"),
        ("'https://registry.example.internal/'; exit 0", "https://registry.example.internal/"),
        ("'https://registry.npmjs.org/'; exit 1", "<cannot say>"),
        ("throw 'npm is broken'", "<cannot say>"),
    ],
    ids=["default", "theirs", "npm-fails", "npm-throws"],
)
def test_a_windows_source_build_asks_npm_which_registry_it_reads(tmp_path: Path, answer: str, said: str) -> None:
    """An npm that fails cannot say, even when it printed the default first.
    The stand-in is a script, as the npm.ps1 PowerShell finds first is."""
    fake = tmp_path / "npm.ps1"
    fake.write_text(f"if (\"$args\" -eq 'config get registry') {{ {answer} }}\n", encoding="utf-8")
    result = _run_ps1(
        tmp_path,
        "npm-registry-step",
        _ps1_settings("ErrorActionPreference") + _PS1_NPM_REGISTRY_STEP,
        FakeNpm=str(fake),
    )

    assert f"REGISTRY={said}" in result.stdout, result.stdout + result.stderr


# --- Chromium -------------------------------------------------------------------
# Playwright already tries its own hosts in turn and reports only that none of
# them delivered, so the one retry comes after the whole download failed.


_PLAYWRIGHT_HOSTS = ("https://cdn.playwright.dev/", "https://playwright.download.prss.microsoft.com/")


def _run_browser_step(
    tmp_path: Path,
    *,
    first_fails: bool = True,
    env: dict[str, str] | None = None,
    down: tuple[str, ...] = _PLAYWRIGHT_HOSTS,
):
    """A playwright whose download succeeds only from the mirror (or always,
    with `first_fails` off); every host it was pointed at lands in the log.
    Its own hosts cannot be reached unless ``down`` says otherwise."""
    tools = tmp_path / "tools"
    python = tools / "raven" / "bin" / "python"
    python.parent.mkdir(parents=True)
    log = tmp_path / "playwright.log"
    python.write_text(
        "#!/bin/sh\n"
        '[ "$1" = -c ] && exit 0\n'
        f"echo \"${{PLAYWRIGHT_DOWNLOAD_HOST:-default}}\" >> '{log}'\n"
        + (
            '[ "${PLAYWRIGHT_DOWNLOAD_HOST:-}" = https://cdn.npmmirror.com/binaries/playwright ]\n'
            if first_fails
            else "exit 0\n"
        ),
        encoding="utf-8",
    )
    python.chmod(0o755)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uv").write_text(f"#!/bin/sh\n[ \"$1 $2\" = 'tool dir' ] && echo '{tools}'\n", encoding="utf-8")
    (bin_dir / "uv").chmod(0o755)
    _fake_curl(bin_dir, {}, down=down)
    harness = tmp_path / "browser-step.sh"
    harness.write_text(
        _SH_PREAMBLE
        + _sh_functions(
            ("mirrors_allowed", "connection_failed", "reachable", "playwright_answers", "install_browser"),
            ("PLAYWRIGHT_MIRROR", "PLAYWRIGHT_HOSTS"),
        )
        + "install_browser\n",
        encoding="utf-8",
    )
    result = subprocess.run(
        ["sh", str(harness)],
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": f"{bin_dir}:/usr/bin:/bin", **(env or {})},
    )
    hosts = log.read_text(encoding="utf-8").splitlines() if log.is_file() else []
    return result, hosts


@POSIX_SH_ONLY
def test_chromium_is_retried_from_the_mirror_when_playwrights_hosts_fail(tmp_path: Path) -> None:
    result, hosts = _run_browser_step(tmp_path)

    assert result.returncode == 0, result.stderr
    assert hosts == ["default", "https://cdn.npmmirror.com/binaries/playwright"]
    assert "Chromium download failed" not in result.stderr
    assert "RAVEN_NO_MIRROR" in result.stderr


@POSIX_SH_ONLY
def test_a_chromium_that_downloads_is_not_retried(tmp_path: Path) -> None:
    _result, hosts = _run_browser_step(tmp_path, first_fails=False)

    assert hosts == ["default"]


@POSIX_SH_ONLY
def test_a_download_host_the_user_named_is_kept(tmp_path: Path) -> None:
    result, hosts = _run_browser_step(tmp_path, env={"PLAYWRIGHT_DOWNLOAD_HOST": "https://playwright.example.internal"})

    assert hosts == ["https://playwright.example.internal"]
    assert "Chromium download failed" in result.stderr


@POSIX_SH_ONLY
def test_the_opt_out_tries_playwrights_hosts_only(tmp_path: Path) -> None:
    result, hosts = _run_browser_step(tmp_path, env={"RAVEN_NO_MIRROR": "1"})

    assert hosts == ["default"]
    assert "Chromium download failed" in result.stderr


@POSIX_SH_ONLY
@pytest.mark.parametrize("down", [(), _PLAYWRIGHT_HOSTS[:1]], ids=["both-answered", "the-second-answered"])
def test_a_chromium_download_that_reached_playwrights_hosts_is_not_retried(
    tmp_path: Path, down: tuple[str, ...]
) -> None:
    """playwright does not say why it failed, so its hosts are asked: one that
    answered had its say, and the mirror stands in only for hosts that cannot
    be reached."""
    result, hosts = _run_browser_step(tmp_path, down=down)

    assert hosts == ["default"]
    assert "Chromium download failed" in result.stderr


_PS1_BROWSER_STEP = r"""
$names = @("Test-MirrorsAllowed", "Test-Reachable", "Get-TunnelStatus", "Set-MirrorEnv", "Install-Chromium")
foreach ($text in (Get-Install $names)) { Invoke-Expression $text }
function Write-Warn([string]$Message) { [Console]::Out.WriteLine("WARN $Message") }
$Served = @{}
$Down = @($DownList -split ",")
$ok = Install-Chromium $Python
[Console]::Out.WriteLine("OK=$ok")
"""


def _run_ps1_browser_step(tmp_path: Path, python: Path, *, down: tuple[str, ...] = _PLAYWRIGHT_HOSTS):
    """Install-Chromium with ``python`` as the install's own; Playwright's
    hosts cannot be reached unless ``down`` says otherwise."""
    result = _run_ps1(
        tmp_path,
        "browser-step",
        _ps1_settings("ErrorActionPreference", "PlaywrightMirror", "PlaywrightHosts")
        + "$script:MirrorSaved = @{}\n"
        + _PS1_FAKE_WEB
        + _PS1_BROWSER_STEP,
        env={"PLAYWRIGHT_DOWNLOAD_HOST": "", "PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST": ""},
        Python=str(python),
        WebRoot=str(tmp_path),
        DownList=",".join(down),
    )
    return result, result.stdout.splitlines()


def _ps1_playwright(tmp_path: Path, *, mirror_works: bool = True, says: bool = False) -> tuple[Path, Path]:
    """A python whose playwright download fails from its own hosts, and works
    from the mirror unless ``mirror_works`` is off; every host it was pointed at
    lands in the log, and with ``says`` it reports each attempt on stdout, as
    playwright does."""
    log = tmp_path / "playwright.log"
    python = tmp_path / "python.ps1"
    python.write_text(
        "$asked = if ($env:PLAYWRIGHT_DOWNLOAD_HOST) { $env:PLAYWRIGHT_DOWNLOAD_HOST } else { 'default' }\n"
        f"Add-Content -Path '{log}' -Value $asked\n"
        + ('Write-Output "Downloading Chromium from $asked"\n' if says else "")
        + ("if ($env:PLAYWRIGHT_DOWNLOAD_HOST) { exit 0 } else { exit 1 }\n" if mirror_works else "exit 1\n"),
        encoding="utf-8",
    )
    return python, log


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
def test_a_windows_chromium_is_retried_from_the_mirror_when_playwrights_hosts_fail(tmp_path: Path) -> None:
    python, log = _ps1_playwright(tmp_path)

    result, lines = _run_ps1_browser_step(tmp_path, python)

    assert "OK=True" in lines, result.stdout + result.stderr
    assert log.read_text(encoding="utf-8").split() == ["default", "https://cdn.npmmirror.com/binaries/playwright"]


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
@pytest.mark.parametrize("down", [(), _PLAYWRIGHT_HOSTS[:1]], ids=["both-answered", "the-second-answered"])
def test_a_windows_chromium_download_that_reached_playwrights_hosts_is_not_retried(
    tmp_path: Path, down: tuple[str, ...]
) -> None:
    """playwright does not say why it failed, so its hosts are asked: one that
    answered had its say, and the mirror stands in only for hosts that cannot
    be reached."""
    python, log = _ps1_playwright(tmp_path)

    result, lines = _run_ps1_browser_step(tmp_path, python, down=down)

    assert "OK=False" in lines, result.stdout + result.stderr
    assert log.read_text(encoding="utf-8").split() == ["default"]


@pytest.mark.slow
@pytest.mark.skipif(shutil.which("pwsh") is None, reason="needs PowerShell")
@pytest.mark.parametrize(("mirror_works", "said"), [(True, "OK=True"), (False, "OK=False")])
def test_a_windows_chromium_download_shows_its_progress_and_still_reports_how_it_went(
    tmp_path: Path, mirror_works: bool, said: str
) -> None:
    """playwright reports its download on stdout. Left in the function's output
    it comes back beside the true or false, which then reads as true either
    way, so a failed download would never be reported."""
    python, _log = _ps1_playwright(tmp_path, mirror_works=mirror_works, says=True)

    result, lines = _run_ps1_browser_step(tmp_path, python)

    assert said in lines, result.stdout + result.stderr
    assert "Downloading Chromium from default" in lines, "playwright's own report still reaches the reader"


# --- What the page starts with ---------------------------------------------------


def test_the_page_starts_with_the_users_own_environment() -> None:
    """The variables a fallback set are for this install's own downloads; the
    runtime decides for itself, and the page is started after they are gone."""
    sh = INSTALL_SH.read_text(encoding="utf-8")
    main = sh[sh.index("main() {") :]
    assert main.index("unset") < main.index("launch_web")
    ps1 = INSTALL_PS1.read_text(encoding="utf-8")
    main = ps1[ps1.index("function Main") :]
    assert main.index("Restore-MirrorEnv") < main.index("Start-Web")
