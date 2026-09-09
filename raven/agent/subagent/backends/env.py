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
"""

from __future__ import annotations

import os
import subprocess

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
    """The user's login shell if this module can drive it, else ``None``."""
    shell = os.environ.get("SHELL", "").strip()

    return shell if shell and os.path.basename(shell) in _DRIVABLE_SHELLS else None


def login_shell_env() -> dict[str, str]:
    """Return the login shell's environment, captured once per process.

    Falls back to raven's own environment when the capture fails, comes back
    empty, or ``$SHELL`` names a shell this module cannot drive, so an unusual
    login shell degrades to the previous behaviour instead of making every spawn
    fail -- or, worse, succeeding with a PATH from a shell the user never uses.
    """
    global _LOGIN_ENV, _LOGIN_ENV_FAILED
    if _LOGIN_ENV is not None:
        return dict(_LOGIN_ENV)
    if _LOGIN_ENV_FAILED:
        return dict(os.environ)
    shell = _login_shell()
    if shell is None:
        _LOGIN_ENV_FAILED = True
        logger.warning(
            "SHELL={} is unset or not one this build can drive ({}); subagents inherit raven's "
            "environment rather than a capture from the wrong shell",
            os.environ.get("SHELL", "") or "<unset>",
            ", ".join(sorted(_DRIVABLE_SHELLS)),
        )
        return dict(os.environ)
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
        _LOGIN_ENV_FAILED = True
        logger.warning("Login shell environment capture failed ({}); subagents inherit raven's", exc)
        return dict(os.environ)
    captured = {
        key: value
        for key, _, value in (entry.partition("=") for entry in proc.stdout.decode("utf-8", "replace").split("\0"))
        if key and _
    }
    if not captured or proc.returncode != 0:
        _LOGIN_ENV_FAILED = True
        # `-i` on a non-tty always writes the shell's own job-control notices
        # ("cannot set terminal process group", "no job control in this
        # shell") to stderr; those are expected and not diagnostic of the
        # failure, so they are dropped before taking the tail. The prefix is
        # the shell's own name, so it is derived rather than hardcoded.
        noise = f"{os.path.basename(shell)}: "
        stderr_lines = proc.stderr.decode("utf-8", "replace").splitlines()
        stderr_tail = "\n".join(line for line in stderr_lines if not line.startswith(noise))[-500:].strip()
        logger.warning(
            "Login shell environment capture failed (exit {}): {}; subagents inherit raven's",
            proc.returncode,
            stderr_tail or "<no stderr beyond expected job-control notices>",
        )
        return dict(os.environ)
    _LOGIN_ENV = captured
    return dict(captured)


def host_identity_env() -> dict[str, str]:
    """Return the custom raven home that identifies the spawning host.

    Read at spawn time because tests and ``raven serve`` may point one process
    at different homes between child launches.
    """
    from raven.contracts.path_policy import HOME_ENV_VAR

    home = os.environ.get(HOME_ENV_VAR, "").strip()
    return {HOME_ENV_VAR: home} if home else {}


__all__ = ["host_identity_env", "login_shell_env"]
