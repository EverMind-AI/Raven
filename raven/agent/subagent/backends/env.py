"""The environment a spawned CLI subagent runs under.

A third-party CLI agent is a program the user also runs from their own terminal,
so it should see that terminal's environment rather than raven's. Raven's own
process environment carries its launcher's PATH ordering and whatever the editor
injected, which is enough to make a child resolve the wrong interpreter: raven's
PATH can list a conda bin ahead of /usr/local/bin, and openclaw hard-exits on an
unsupported node with no way to override the check. The capture runs the user's
own login shell from a minimal base (not raven's environment) so both leaks
close: the profile's PATH wins, and an editor-injected variable like
CLAUDE_CODE_SSE_PORT is actually absent rather than merely overlaid.

*Which* shell is `$SHELL`, not a hardcoded one, and that choice is load-bearing.
Running `bash` on a host whose login shell is zsh walks `~/.bash_profile` and
never reads the `~/.zshrc` where that user's PATH additions live -- yet `bash`
exists, `env -0` prints, and the exit status is 0, so the capture is not treated
as a failure and the fallbacks below never fire. The result is a PATH that
resolves nothing the user installed, which reads downstream as "agent not
installed" for agents that work fine in their terminal.

Only shells this module knows how to drive are driven: `bash` and `zsh` both
read profile-then-rc under `-lic`. For anything else -- fish, which prints a
greeting under `-i` that would corrupt the first parsed variable, or an unset
`$SHELL` -- raven's own environment is returned instead. Substituting a shell
the user does not use would produce a confidently wrong PATH; falling back is
merely no better than not capturing at all.

The shell is both login (`-l`) and interactive (`-i`), and neither flag alone
is enough. `-l` is what walks the profile chain (`/etc/profile`,
`~/.bash_profile`, `~/.zprofile`, `~/.profile`) where installers for bun, nvm,
and cargo write their PATH exports; `-ic` alone skips that chain entirely and
only reads `/etc/bash.bashrc` and `~/.bashrc`. `-i` is needed on top because
most distro `~/.bashrc` files open with a `[ -z "$PS1" ] && return` guard that a
non-interactive shell never gets past, so anything placed below it -- proxy
settings, nvm/conda init -- never runs either. Only `-lic` together runs the
same profile + rc chain the user's own terminal does.

Known limitation: if the login shell's profile prints a banner to stdout, that
banner corrupts the first parsed variable. This is not worked around here.

Windows has no login shell to ask: explorer.exe holds the interactive
environment, and installers refresh a terminal opened afterwards by
broadcasting WM_SETTINGCHANGE. `_capture_windows` reads the persisted User
and Machine stores that broadcast refreshes, merging them over raven's
inherited environment so a child keeps the process environment a service
already had and only the installer-refreshed entries -- Path above all --
are rebuilt from the store that is actually current.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

from loguru import logger

_LOGIN_ENV: dict[str, str] | None = None
_LOGIN_ENV_FAILED = False

# bash -lic is given a deliberately minimal base rather than raven's environment:
# inheriting it would only overlay the profile on top, leaving the very variables
# this exists to drop (an editor's CLAUDE_CODE_SSE_PORT, a launcher's NODE_OPTIONS)
# in place. HOME is what lets bash find the profile at all; the bootstrap PATH only
# has to be good enough to resolve `env`, since the profile rewrites PATH anyway.
_CAPTURE_BASE_KEYS = ("HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "LC_ALL")
_BOOTSTRAP_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

# Shells whose `-lic` reads the same profile + rc chain an interactive session
# does, and which print nothing of their own on the way. Keyed by basename so a
# homebrew or nix `$SHELL` path still matches; the path itself is what gets run.
_DRIVABLE_SHELLS = frozenset({"bash", "zsh"})


def _login_shell() -> str | None:
    r"""The user's login shell if this module can drive it, else ``None``.

    ``basename`` alone leaves a Windows-shaped bash unread: git-bash exports
    ``SHELL=C:\Program Files\Git\usr\bin\bash.exe`` with backslashes, and
    POSIX ``basename`` sees no separator. Both flavors of path are split so
    that spelling drives the shell it names rather than falling back and
    losing git-bash's profile-installed PATH entries on a POSIX host that is
    not Windows.
    """
    shell = os.environ.get("SHELL", "").strip()
    if not shell:
        return None
    name = shell.replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().endswith(".exe"):
        name = name[:-4]
    return shell if name in _DRIVABLE_SHELLS else None


def login_shell_env() -> dict[str, str]:
    """Return the login shell's environment, captured once per process.

    Falls back to raven's own environment when the capture fails, comes back
    empty, or ``$SHELL`` names a shell this module cannot drive, so an unusual
    login shell degrades to the previous behaviour instead of making every spawn
    fail -- or, worse, succeeding with a PATH from a shell the user never uses.

    Once per process is what makes it cheap, and also what makes it go stale;
    `refresh_login_shell_env` is the way to take it again without a restart.
    """
    global _LOGIN_ENV, _LOGIN_ENV_FAILED
    if _LOGIN_ENV is not None:
        return dict(_LOGIN_ENV)
    if _LOGIN_ENV_FAILED:
        return dict(os.environ)
    captured = (
        _capture_windows(consequence="subagents inherit raven's environment")
        if sys.platform == "win32"
        else _capture(consequence="subagents inherit raven's environment")
    )
    if captured is None:
        _LOGIN_ENV_FAILED = True
        return dict(os.environ)
    _LOGIN_ENV = captured
    return dict(captured)


def refresh_login_shell_env() -> bool:
    """Capture the login shell's environment again, and swap it in if that worked.

    The memo in `login_shell_env` lasts as long as the process, so an agent
    installed after the gateway started stays invisible to every probe and every
    spawn until a restart: its installer appends a PATH line to ``~/.zshrc``
    (Kimi Code's does, measured 2026-09-24), the user's own terminal runs the
    agent, and the page's "Check again" kept answering "still not found" from
    the PATH the gateway read at start. This is what that button calls.

    Swapped in whole, and only on success. The memo is read without a lock by
    probes and spawns on other threads, so it must never be empty in between: a
    reader that found it empty would capture again itself, and the MCP grant
    path does that read on the event loop. A failed refresh keeps what was
    there rather than trading a working capture for raven's own environment,
    and a process whose earlier capture failed gets another try: the memo is
    read before the failure flag, so a landed capture is what every later read
    returns. Returns whether a capture landed.
    """
    global _LOGIN_ENV
    captured = _capture(consequence="keeping the environment captured earlier")
    if captured is None:
        return False
    _LOGIN_ENV = captured
    return True


def _capture_windows(*, consequence: str) -> dict[str, str] | None:
    """The environment a freshly opened Windows terminal would start with.

    Windows has no login shell to ask: explorer.exe holds the interactive
    environment, and installers refresh a terminal opened afterwards by
    broadcasting WM_SETTINGCHANGE (measured 2026-10-08: a `uv tool install`
    made an agent resolvable from every new terminal minutes before the
    running gateway, a service with a frozen `os.environ`, could see it --
    and the WebUI probe answered "not on the login shell PATH" while the
    file sat on disk).

    Rather than round-trip a PowerShell (which needs quoting, base64, and
    a WM broadcast), read the persisted stores the shell itself reassembles
    a terminal from: HKCU\\Environment for the user's own variables and
    HKLM\\...\\Environment for the machine's. Both are merged over raven's
    inherited ``os.environ`` so a child keeps the process environment a
    service already had (proxy, temp, SystemRoot, conda) and only the
    installer-refreshed entries -- ``Path`` above all -- are rebuilt from
    the store that is actually current.

    ``None`` when neither store can be read, which is the "capture failed"
    signal the caller falls back on. A store that reads but has no ``Path``
    is still a valid capture: the probe of an absent agent then names the
    registry it was looked up in, which is the same sentence the POSIX arm
    gives for a profile with no PATH export.
    """
    import winreg

    def _read_store(hive: int, subkey: str) -> dict[str, str]:
        try:
            with winreg.OpenKey(hive, subkey) as key:
                entries = {}
                for i in range(winreg.QueryInfoKey(key)[1]):
                    name, value, _vtype = winreg.EnumValue(key, i)
                    if name and isinstance(value, str):
                        entries[name] = value
                return entries
        except OSError as exc:
            logger.warning(
                "Windows login environment capture could not read {} ({}); {}",
                subkey,
                exc,
                consequence,
            )
            return {}

    machine = _read_store(winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment")
    user = _read_store(winreg.HKEY_CURRENT_USER, r"Environment")
    if not machine and not user:
        return None

    machine_path = next((v for k, v in machine.items() if k.lower() == "path"), "")
    user_path = next((v for k, v in user.items() if k.lower() == "path"), "")
    # Expand %VAR% against the same machine-then-user precedence Windows
    # applies when it assembles a terminal's environment -- registry Path
    # entries hold unexpanded references, and passing them on raw hands the
    # child a PATH whose entries never existed (the gateway's own measured
    # log shows a launcher script dying on `%USERPROFILE%/go/bin`).
    context: dict[str, str] = {}
    for source in (os.environ, machine, user):
        context.update({k.lower(): v for k, v in source.items()})
    context.pop("path", None)
    machine_path = _expand_windows(machine_path, context)
    user_path = _expand_windows(user_path, context)

    # Windows environment names are case-insensitive, so the merge keys on the
    # fold: os.environ < machine < user, with one entry per name -- a child's
    # env block carrying both `Path` and `PATH` resolves unpredictably.
    merged: dict[str, str] = {}
    known: dict[str, str] = {}
    for source in (os.environ, machine, user):
        for name, value in source.items():
            low = name.lower()
            if low in known:
                merged.pop(known[low], None)
            known[low] = name
            merged[name] = value
    if machine_path or user_path:
        merged.pop(known.get("path", ""), None)
        # Union rather than replace: the machine+user stores are the PATH a
        # *new* terminal gets, but raven's own launcher's bin (MinGit/usr/bin,
        # conda) reached this process's PATH without touching the stores, and
        # a child spawned from the gateway needs every one of them.
        live = os.environ.get("PATH", "")
        joined = [p for p in (machine_path, user_path) if p]
        if live:
            joined.append(live)
        merged["Path"] = ";".join(joined)
    return merged


def _expand_windows(value: str, context: dict[str, str], _depth: int = 0) -> str:
    r"""``%VAR%`` references in a registry value, folded the way Windows folds them.

    The registry is what a terminal's Path is built from, so the references
    resolve against the case-folded registry context (machine-then-user
    shadowing the process's inherited env) rather than ``os.environ`` --
    a literal ``%USERPROFILE%\go\bin`` would otherwise reach a child as a
    directory named exactly that.
    """
    if not value or "%" not in value or _depth > 16:
        return value

    expanded = re.sub(r"%([^%]+)%", lambda m: context.get(m.group(1).lower(), m.group(0)), value)
    return value if expanded == value else _expand_windows(expanded, context, _depth + 1)


def _capture(*, consequence: str) -> dict[str, str] | None:
    """One run of the login shell, parsed; ``None`` when it did not work, after saying why.

    ``consequence`` is what the caller does about a failure, for the log line:
    the first capture falls back to raven's own environment, a refresh keeps the
    one it already had.
    """
    shell = _login_shell()
    if shell is None:
        logger.warning(
            "SHELL={} is unset or not one this build can drive ({}); {} rather than a capture from the wrong shell",
            os.environ.get("SHELL", "") or "<unset>",
            ", ".join(sorted(_DRIVABLE_SHELLS)),
            consequence,
        )
        return None
    capture_base = {key: os.environ[key] for key in _CAPTURE_BASE_KEYS if key in os.environ}
    capture_base["PATH"] = _BOOTSTRAP_PATH
    try:
        proc = subprocess.run(
            [shell, "-lic", "env -0"],
            env=capture_base,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
            # `-i` makes the shell initialize job control, and it reaches
            # /dev/tty for that even with all three stdio streams redirected away
            # from the terminal. It then tcsetpgrp's the terminal to its own
            # process group and exits without restoring it, leaving raven in a
            # background group: the next keystroke in `raven tui` raises SIGTTIN
            # and stops the whole job. A new session gives it no controlling
            # terminal to take.
            start_new_session=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("Login shell environment capture failed ({}); {}", exc, consequence)
        return None
    captured = {
        key: value
        for key, _, value in (entry.partition("=") for entry in proc.stdout.decode("utf-8", "replace").split("\0"))
        if key and _
    }
    if not captured or proc.returncode != 0:
        # `-i` on a non-tty always writes the shell's own job-control notices
        # ("cannot set terminal process group", "no job control in this
        # shell") to stderr; those are expected and not diagnostic of the
        # failure, so they are dropped before taking the tail. The prefix is
        # the shell's own name, so it is derived rather than hardcoded.
        noise = f"{os.path.basename(shell)}: "
        stderr_lines = proc.stderr.decode("utf-8", "replace").splitlines()
        stderr_tail = "\n".join(line for line in stderr_lines if not line.startswith(noise))[-500:].strip()
        logger.warning(
            "Login shell environment capture failed (exit {}): {}; {}",
            proc.returncode,
            stderr_tail or "<no stderr beyond expected job-control notices>",
            consequence,
        )
        return None
    return captured


def host_identity_env() -> dict[str, str]:
    """Return the custom raven home that identifies the spawning host.

    Read at spawn time because tests and ``raven serve`` may point one process
    at different homes between child launches.
    """
    from raven.contracts.path_policy import HOME_ENV_VAR

    home = os.environ.get(HOME_ENV_VAR, "").strip()
    return {HOME_ENV_VAR: home} if home else {}


__all__ = ["host_identity_env", "login_shell_env", "refresh_login_shell_env"]
