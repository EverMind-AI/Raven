"""Commands a shell command assumes and this host lacks, supplied on the command's PATH.

Models write shell for GNU userland. On macOS ``timeout`` is not installed, so
``timeout 60 some-cli ...`` -- the usual way to bound a run -- fails with
"command not found" and the turn spends a second call re-running it without.
Seen repeatedly while agents diagnosed a sub-agent that would not answer.
Only what the host is missing is supplied, and only on the PATH commands run
with; a real ``timeout`` found first always wins.
"""

from __future__ import annotations

import os
import shutil
import sys

from loguru import logger

from raven.home import raven_home

# A GNU `timeout` subset: DURATION with s/m/h/d suffixes, -s/--signal,
# -k/--kill-after, --preserve-status, --foreground; exit 124 on timeout, 125 on
# its own error, 126/127 when the command cannot be run.
_TIMEOUT = r"""
import os, signal, subprocess, sys

def seconds(text):
    units = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    text = text.strip()
    scale = units.get(text[-1:], None)
    return float(text[:-1] if scale else text) * (scale or 1)

def main(argv):
    sig, kill_after, preserve = signal.SIGTERM, None, False
    while argv and argv[0].startswith("-") and argv[0] != "-":
        opt = argv.pop(0)
        if opt in ("-s", "--signal"):
            name = argv.pop(0).upper()
            sig = int(name) if name.isdigit() else getattr(signal, name if name.startswith("SIG") else "SIG" + name)
        elif opt.startswith("--signal="):
            name = opt.split("=", 1)[1].upper()
            sig = int(name) if name.isdigit() else getattr(signal, name if name.startswith("SIG") else "SIG" + name)
        elif opt in ("-k", "--kill-after"):
            kill_after = seconds(argv.pop(0))
        elif opt.startswith("--kill-after="):
            kill_after = seconds(opt.split("=", 1)[1])
        elif opt == "--preserve-status":
            preserve = True
        elif opt in ("--foreground", "-f", "-v", "--verbose"):
            pass
        elif opt == "--":
            break
        else:
            sys.stderr.write("timeout: unknown option %s\n" % opt)
            return 125
    if len(argv) < 2:
        sys.stderr.write("usage: timeout [OPTION] DURATION COMMAND [ARG]...\n")
        return 125
    try:
        limit = seconds(argv[0])
    except ValueError:
        sys.stderr.write("timeout: invalid time interval %r\n" % argv[0])
        return 125
    try:
        child = subprocess.Popen(argv[1:])
    except FileNotFoundError:
        sys.stderr.write("timeout: failed to run command %r: No such file or directory\n" % argv[1])
        return 127
    except PermissionError:
        sys.stderr.write("timeout: failed to run command %r: Permission denied\n" % argv[1])
        return 126
    try:
        code = child.wait(timeout=limit or None)
        return code if code >= 0 else 128 - code
    except subprocess.TimeoutExpired:
        child.send_signal(sig)
        try:
            code = child.wait(timeout=kill_after)
        except subprocess.TimeoutExpired:
            child.kill()
            code = child.wait()
        # GNU reports a child it had to KILL as 137 either way, so a caller can
        # tell a forced kill from a polite timeout.
        if preserve or code == -signal.SIGKILL:
            return code if code >= 0 else 128 - code
        return 124
    except KeyboardInterrupt:
        child.send_signal(signal.SIGINT)
        return child.wait()

sys.exit(main(sys.argv[1:]))
"""

_dir: str | None = None
_checked = False


def compat_bin_dir() -> str | None:
    """The directory holding the commands this host lacks, written once per process; ``None`` when none are."""
    global _dir, _checked
    if _checked:
        return _dir
    _checked = True
    if shutil.which("timeout") is not None:
        return None
    target = raven_home() / "cache" / "compat-bin"
    script = f"#!{sys.executable}\n{_TIMEOUT}"
    try:
        target.mkdir(parents=True, exist_ok=True)
        path = target / "timeout"
        if not path.exists() or path.read_text(encoding="utf-8") != script:
            path.write_text(script, encoding="utf-8")
        path.chmod(0o755)
    except OSError as exc:
        logger.debug("compat-bin: could not write the timeout shim: {}", exc)
        return None
    _dir = str(target)
    return _dir


def with_compat(path: str) -> str:
    """``path`` with the compat directory appended, so a host command of the same name still wins."""
    extra = compat_bin_dir()
    if not extra or extra in path.split(os.pathsep):
        return path
    return f"{path}{os.pathsep}{extra}" if path else extra


__all__ = ["compat_bin_dir", "with_compat"]
