"""User rules and default tiers: which standing one tool call has.

The user's ``permissions.tools`` node maps a tool name to a tier, or -- for
``exec`` -- to a table of command patterns each mapping to a tier. Several
matching rules resolve to the strictest (deny > ask > allow), never to the one
written last. A call no rule speaks about falls to the tool's default tier:
read-only tools run, everything else asks -- with one exception, and
``DEFAULT_ALLOW_TOOLS`` says why it is there: ``deliver_files`` hands the user
a file they asked for, which is not a read and is not an effect they approve.
``exec`` is the one tool whose default reads its argument: a command every
segment of which only reads (``READ_ONLY_COMMANDS`` and its three companions)
allows, everything else asks.

Exec pattern matching is prefix-by-token on the raw command, deliberately
without wrapper stripping: ``git *`` must not allow ``sudo git push``. A
compound command allows only when every segment allows. Segments come from the
shell lexer, so punctuation inside quotes stays text; what the lexer hands back
as its own token is shell syntax, and three kinds of it narrow what a rule may
say. Command and process substitution, backticks and heredocs run something
the token view cannot see, so the whole command answers only to a ``*`` rule.
A redirection is lifted out before matching and its target judged on its own:
a relative path without ``..`` lands under whatever the cwd is and changes
nothing, ``/dev/null`` and an fd dup write nothing, and any other target -- an
absolute path, ``~``, a variable -- again leaves only ``*`` rules. ``$VAR``
inside a word stays a word: expansion splits words, it cannot make a command
boundary. The builtin classifier (``raven.permissions.builtin``) keeps its own,
stripping view of the same command; a rule here can never lift what it rules.

The same token view says what a session grant remembers and what prefix the
approval prompt may suggest (``exec_approval_shape``), and what a prefix typed
into that prompt may look like before it reaches the config
(``validate_exec_pattern``).
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from raven.contracts.permissions import Tier

_STRICTNESS = {Tier.DENY: 2, Tier.ASK: 1, Tier.ALLOW: 0}

# Tools whose worst case is reading what the agent may already read, plus
# deliver_files: its recipient is the user themself, and it is the only route a
# finished artifact has to them. Asking there cost the work rather than guarding
# it -- the request expires while the agent waits, and the reply that follows
# hands over a path instead, which reaches nobody. Everything absent from this
# set defaults to asking, unknown (MCP) tools included.
DEFAULT_ALLOW_TOOLS: frozenset[str] = frozenset(
    {
        "read_file",
        "list_dir",
        "grep",
        "find",
        "glob",
        "tool_search",
        "tool_call",
        "web_search",
        "web_fetch",
        "deliver_files",
        "message",
        "ask_user",
        "read_skill",
        "use_skill",
        "load_playbook",
        "list_mcp_resources",
        "list_mcp_resource_templates",
        "read_mcp_resource",
        "list_mcp_prompts",
        "get_mcp_prompt",
    }
)


# Shell commands whose worst case is reading what the agent may already read
# through `read_file`. This is the default an absent rule falls to, not a
# platform ruling: a user `ask` or `deny` rule still outranks it.
#
# Four shapes, because reading only is not a property of a bare name: `whoami`
# reads only without an argument, `sort` writes with `-o`, and `git` reads only
# on its query subcommands. A name absent from all four asks -- `awk`, `sed`,
# `xargs` and `env` are absent on purpose, each being a way to run something
# else or to write a file from inside an expression this module does not parse.
READ_ONLY_COMMANDS: frozenset[str] = frozenset(
    {
        "arch",
        "basename",
        "cal",
        "cat",
        "cd",
        "cmp",
        "column",
        "comm",
        "cut",
        "df",
        "diff",
        "dirname",
        "du",
        "echo",
        "expand",
        "expr",
        "false",
        "file",
        "fmt",
        "fold",
        "free",
        "grep",
        "egrep",
        "fgrep",
        "groups",
        "head",
        "hexdump",
        "id",
        "jq",
        "locale",
        "ls",
        "lsof",
        "md5sum",
        "netstat",
        "nl",
        "nproc",
        "numfmt",
        "od",
        "paste",
        "pgrep",
        "pr",
        "ps",
        "readlink",
        "realpath",
        "rev",
        "rg",
        "sha1sum",
        "sha256sum",
        "ss",
        "stat",
        "strings",
        "tac",
        "tail",
        "tput",
        "tr",
        "tree",
        "true",
        "type",
        "uname",
        "unexpand",
        "uniq",
        "uptime",
        "wc",
        "which",
        "find",
        "sort",
        "date",
        "printf",
        "test",
        "base64",
        "fd",
        "fdfind",
    }
)

# With an argument these set rather than show: `hostname x`, `ifconfig eth0 down`.
READ_ONLY_ZERO_ARG: frozenset[str] = frozenset({"alias", "history", "hostname", "ifconfig", "pwd", "whoami"})

# The flags that turn each reader into a writer. Absent from the map means no
# flag does.
READ_ONLY_BANNED_FLAGS: dict[str, tuple[str, ...]] = {
    "base64": ("-o", "--output"),
    "date": ("-s", "--set"),
    "fd": ("-x", "--exec", "-X", "--exec-batch"),
    "fdfind": ("-x", "--exec", "-X", "--exec-batch"),
    "find": (
        "-delete",
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-fls",
        "-fprint",
        "-fprint0",
        "-fprintf",
        "-files0-from",
    ),
    "rg": ("--pre",),
    "file": ("-C", "--compile"),
    "sort": ("-o", "--output"),
    "test": ("-a", "-o", "-R", "-v"),
    "tree": ("-o",),
}

# A positional argument that does not start with this prefix is not a query:
# `date +%s` formats, `date 0910` sets the clock.
READ_ONLY_POSITIONAL_PREFIX: dict[str, str] = {"date": "+"}

# The git queries whose name alone does not settle it. `git branch x` creates
# and `git tag x` creates, so those two take one positional and none of their
# mutating flags; `git remote remove x` removes and `git reflog expire` deletes,
# so those two take a second positional only from their query forms.
_GIT_QUERY_FLAGS_BANNED: dict[str, tuple[str, ...]] = {
    "branch": (
        "-d",
        "-D",
        "-m",
        "-M",
        "-c",
        "-C",
        "-f",
        "-u",
        "--delete",
        "--move",
        "--copy",
        "--force",
        "--set-upstream-to",
        "--unset-upstream",
        "--edit-description",
        "--track",
        "--no-track",
    ),
    "tag": (
        "-d",
        "-a",
        "-s",
        "-f",
        "-m",
        "-F",
        "-u",
        "--delete",
        "--annotate",
        "--sign",
        "--force",
        "--message",
        "--file",
    ),
}
_GIT_SECOND_POSITIONAL_QUERIES: dict[str, frozenset[str]] = {
    "remote": frozenset({"get-url", "show"}),
    "reflog": frozenset({"show"}),
}
# Across every git query: the options that name a file to write or a program to
# run. The diff options (`--output`, `--ext-diff`, `--textconv`) reach diff, log,
# show, shortlog and rev-list alike; `--upload-pack`/`--receive-pack`/`--exec`
# hand ls-remote and friends a program; `-O` opens grep hits in a pager or
# editor; `--filters`/`--textconv` on cat-file run configured filters. One list
# for the whole family, so a query gained later is covered by construction.
_GIT_PROGRAM_OR_OUTPUT_OPTIONS: tuple[str, ...] = (
    "--output",
    "--output-directory",
    "--ext-diff",
    "--textconv",
    "--filters",
    "--upload-pack",
    "--receive-pack",
    "--exec",
    "-O",
    "--open-files-in-pager",
)
# Options git accepts before the subcommand; `-c` and `--exec-path` change what
# runs, so only these are let through, and only in front.
_GIT_GLOBAL_OPTIONS_ALLOWED: frozenset[str] = frozenset({"--no-pager", "-P"})
_GIT_GLOBAL_OPTIONS_WITH_VALUE: frozenset[str] = frozenset({"-C"})

# `printf %s -flag` is fine; `printf -v x` assigns. No flag of it is needed for
# reading, so the whole shape is refused rather than enumerated.
READ_ONLY_NO_FLAGS: frozenset[str] = frozenset({"printf"})

# Options whose VALUE is a program to run, as a class rather than a list per
# command: `sort --compress-program`, `rg --hostname-bin`, `rg --pre`,
# `find -exec`. A per-command denylist is only ever as complete as the man
# pages someone read, and every one of these turns a reader into an executor.
_OPTION_NAMES_A_PROGRAM = re.compile(r"^--?(pre|exec[a-z-]*|[a-z][a-z0-9-]*-(program|bin|cmd|command|filter|pager))$")

# Filters whose extra positional is an output file: `uniq IN OUT` writes OUT.
# The number is how many positionals only read.
READ_ONLY_MAX_POSITIONAL: dict[str, int] = {"uniq": 1}

READ_ONLY_SUBCOMMANDS: dict[str, frozenset[str]] = {
    "docker": frozenset({"images", "inspect", "logs", "ps"}),
    "git": frozenset(
        {
            "blame",
            "branch",
            "cat-file",
            "check-ignore",
            "describe",
            "diff",
            "for-each-ref",
            "grep",
            "log",
            "ls-files",
            "ls-remote",
            "ls-tree",
            "merge-base",
            "reflog",
            "remote",
            "rev-list",
            "rev-parse",
            "shortlog",
            "show",
            "status",
            "tag",
        }
    ),
}

# Punctuation the lexer hands back as its own token rather than folding into a
# word. `$` is absent on purpose: `$VAR` stays part of its word, because word
# splitting cannot introduce a command boundary while `$(...)` can -- and the
# `(` is what says so.
_PUNCTUATION = ";&|<>()`\n"
_WHITESPACE = " \t\r"
_SEPARATORS = frozenset(";&|\n")
_INPUT_REDIRECTIONS = frozenset({"<", "<&"})


@dataclass(frozen=True)
class _Lexed:
    """One command as the shell would run it: word tokens per segment, and each
    redirection as (operator, target) with ``None`` for an fd dup like 2>&1."""

    segments: tuple[tuple[str, ...], ...]
    redirections: tuple[tuple[str, str | None], ...]


def lex_command(command: str) -> _Lexed | None:
    """Segment one shell command, or ``None`` when the shell would run something
    this view cannot see: substitution, a backtick, a heredoc, an unbalanced
    quote, a redirection with nothing after it.

    The scan is by hand rather than by ``shlex`` because only the quoting says
    whether a `;` is an operator or an argument, and no ``shlex`` setting keeps
    both: posix mode strips the quotes before the caller sees the token, and
    ``punctuation_chars`` is documented as wanting posix mode. The shell runs
    ``find . ';' cat -delete`` as ONE command with `;` as an argument, so a
    character the user quoted must never become a boundary.

    A digit glued to a redirection (``2>``) stays in its segment as the word
    ``2``; prefix matching and the read-only list are indifferent to a trailing
    argument, and an exact rule that misses because of it asks, which is the
    safe way to be wrong.
    """
    segments: list[list[str]] = []
    redirections: list[tuple[str, str | None]] = []
    words: list[str] = []
    buffer: list[str] = []
    pending: str | None = None

    def close_word() -> bool:
        """Finish the word being accumulated; False when it cannot be read."""
        nonlocal pending
        if not buffer:
            return True
        word = _unquote("".join(buffer))
        buffer.clear()
        if word is None:
            return False
        if pending is not None:
            redirections.append((pending, None if "&" in pending and word.isdigit() else word))
            pending = None
        else:
            words.append(word)
        return True

    index, size, quote = 0, len(command), ""
    while index < size:
        char = command[index]
        if quote:
            if quote == '"':
                if char == "\\" and index + 1 < size:
                    buffer.append(char)
                    index += 1
                    buffer.append(command[index])
                    index += 1
                    continue
                # Double quotes stop word splitting, not substitution: `"$(x)"`
                # and a backtick both still run x. Only single quotes and a
                # backslash make them literal.
                if char == "`" or (char == "$" and index + 1 < size and command[index + 1] == "("):
                    return None
            buffer.append(char)
            if char == quote:
                quote = ""
            index += 1
            continue
        if char in "'\"":
            quote = char
            buffer.append(char)
            index += 1
            continue
        if char == "\\" and index + 1 < size:
            buffer.append(char)
            index += 1
            buffer.append(command[index])
            index += 1
            continue
        if char in _WHITESPACE:
            if not close_word():
                return None
            index += 1
            continue
        if char not in _PUNCTUATION:
            buffer.append(char)
            index += 1
            continue
        if not close_word():
            return None
        run = index
        while index < size and command[index] in _PUNCTUATION:
            index += 1
        operator = command[run:index]
        # Substitution, a backtick and a heredoc all run something this view
        # cannot see, so the command answers only to whole-command rules.
        if any(ch in operator for ch in "()`") or "<<" in operator:
            return None
        if all(ch in _SEPARATORS for ch in operator):
            if pending is not None:
                return None
            segments.append(words)
            words = []
            continue
        if pending is not None:
            return None
        pending = operator
    if quote or not close_word() or pending is not None:
        return None
    segments.append(words)
    return _Lexed(
        segments=tuple(tuple(segment) for segment in segments if segment),
        redirections=tuple(redirections),
    )


def _unquote(token: str) -> str | None:
    """One non-posix token as the shell would pass it, or None when this view
    cannot resolve it to a single word (an escape, or mismatched quotes)."""
    if not any(ch in token for ch in "\"'\\"):
        return token
    try:
        parts = shlex.split(token)
    except ValueError:
        return None
    return parts[0] if len(parts) == 1 else None


def _redirection_stays_home(operator: str, target: str | None) -> bool:
    """Whether a redirection can only touch the cwd or nothing at all.

    A relative path without ``..`` resolves under whatever the cwd is, so it
    needs no cwd to judge; ``/dev/null`` is the one absolute target that writes
    nothing; an fd dup names no file. Everything else -- an absolute path, a
    home path, a variable -- may reach anywhere.
    """
    if target is None or target == "/dev/null":
        return True
    if target.startswith(("/", "~", "$")):
        return False
    return ".." not in PurePosixPath(target).parts


def _redirection_writes_nothing(operator: str, target: str | None) -> bool:
    return operator in _INPUT_REDIRECTIONS or target is None or target == "/dev/null"


def _segment_reads_only_tokens(tokens: tuple[str, ...]) -> bool:
    if not tokens:
        return False
    name, args = tokens[0], tokens[1:]
    # A path names a program this list never vetted; the bare name is the claim,
    # and wrappers are not stripped -- `sudo cat` is not `cat`.
    if "/" in name:
        return False
    if name in READ_ONLY_SUBCOMMANDS:
        if name == "git":
            args = _git_args_after_global_options(args)
            if args is None:
                return False
        positional = [arg for arg in args if not arg.startswith("-")]
        if not positional or positional[0] not in READ_ONLY_SUBCOMMANDS[name]:
            return False
        return name != "git" or _git_query_only_lists(positional, args)
    if name in READ_ONLY_ZERO_ARG:
        return not args
    if name not in READ_ONLY_COMMANDS:
        return False
    if name in READ_ONLY_NO_FLAGS:
        return not any(arg.startswith("-") for arg in args)
    prefix = READ_ONLY_POSITIONAL_PREFIX.get(name)
    if prefix is not None and any(not arg.startswith(("-", prefix)) for arg in args):
        return False
    limit = READ_ONLY_MAX_POSITIONAL.get(name)
    if limit is not None and len([arg for arg in args if not arg.startswith("-")]) > limit:
        return False
    if any(_OPTION_NAMES_A_PROGRAM.match(arg.split("=", 1)[0]) for arg in args):
        return False
    return not any(_flag_hits(arg, flag) for arg in args for flag in READ_ONLY_BANNED_FLAGS.get(name, ()))


def _flag_hits(arg: str, flag: str) -> bool:
    """`-o`, `-oFILE`, `--output`, `--output=FILE`; never `--all` for `-a`."""
    if arg == flag or arg.startswith(f"{flag}="):
        return True
    return len(flag) == 2 and flag[1] != "-" and arg.startswith(flag) and not arg.startswith("--")


def _git_args_after_global_options(args: tuple[str, ...]) -> tuple[str, ...] | None:
    """The arguments from the subcommand on, or None when an option in front of
    it is not one of the few known to change nothing about what runs."""
    rest = list(args)
    while rest and rest[0].startswith("-"):
        option = rest.pop(0)
        if option in _GIT_GLOBAL_OPTIONS_WITH_VALUE:
            if not rest:
                return None
            rest.pop(0)
        elif option not in _GIT_GLOBAL_OPTIONS_ALLOWED:
            return None
    return tuple(rest)


def _git_option_hits(arg: str, flag: str) -> bool:
    """`_flag_hits`, plus git's own rule that an unambiguous prefix of a long
    option spells it: `--upl=x` is `--upload-pack=x`. Any long option that is a
    prefix of a refused one is refused; git would have either taken it as that
    option or rejected it as ambiguous, and neither is a read."""
    if _flag_hits(arg, flag):
        return True
    if not arg.startswith("--") or not flag.startswith("--"):
        return False
    # Any length: git 2.43 takes a one-letter prefix too. A bare `--` (end of
    # options) has no name and is not an option.
    name = arg[2:].split("=", 1)[0]
    return bool(name) and flag[2:].startswith(name)


def _git_query_only_lists(positional: list[str], args: tuple[str, ...]) -> bool:
    sub = positional[0]
    banned = _GIT_PROGRAM_OR_OUTPUT_OPTIONS + _GIT_QUERY_FLAGS_BANNED.get(sub, ())
    if any(_git_option_hits(arg, flag) for arg in args for flag in banned):
        return False
    if sub in ("branch", "tag"):
        return len(positional) == 1
    if sub in _GIT_SECOND_POSITIONAL_QUERIES:
        return len(positional) == 1 or positional[1] in _GIT_SECOND_POSITIONAL_QUERIES[sub]
    return True


def exec_reads_only(command: str) -> bool:
    """Whether every segment of one shell command only reads.

    Reading from a file is still reading, so input redirection is fine
    wherever it points; output may go only to /dev/null or another fd.
    """
    lexed = lex_command(command)
    if lexed is None or not lexed.segments:
        return False
    if not all(_redirection_writes_nothing(op, target) for op, target in lexed.redirections):
        return False
    return all(_segment_reads_only_tokens(segment) for segment in lexed.segments)


def _strictest(tiers: "list[Tier]") -> Tier | None:
    if not tiers:
        return None
    return max(tiers, key=lambda t: _STRICTNESS[t])


def default_tier(tool_name: str, params: dict[str, Any] | None = None) -> Tier:
    if tool_name in DEFAULT_ALLOW_TOOLS:
        return Tier.ALLOW
    if tool_name == "exec" and params is not None:
        command = params.get("command")
        machine = params.get("machine")
        # A command on a registered machine reads that machine's files, which
        # this list never spoke for.
        if isinstance(command, str) and not (isinstance(machine, str) and machine.strip()):
            return Tier.ALLOW if exec_reads_only(command) else Tier.ASK
    return Tier.ASK


def _pattern_tiers(table: dict[str, str]) -> list[tuple[str, list[str] | None, Tier]]:
    """Each rule as (raw pattern, prefix tokens or None for ``*``, tier)."""
    rules: list[tuple[str, list[str] | None, Tier]] = []
    for pattern, tier_name in table.items():
        try:
            tier = Tier(tier_name)
        except ValueError:
            continue
        if pattern.strip() == "*":
            rules.append((pattern, None, tier))
            continue
        try:
            tokens = shlex.split(pattern)
        except ValueError:
            continue
        rules.append((pattern, tokens, tier))
    return rules


def _segment_tier(tokens: list[str], rules: list[tuple[str, list[str] | None, Tier]]) -> Tier | None:
    """Strictest among the specific patterns that match; ``*`` answers only when
    none does -- it is the table's own fallback, and letting it compete would
    make ``{"*": "ask", "git *": "allow"}`` unable to allow anything."""
    matched: list[Tier] = []
    fallback: Tier | None = None
    for _, prefix, tier in rules:
        if prefix is None:
            fallback = tier if fallback is None else _strictest([fallback, tier])
            continue
        if prefix and prefix[-1] == "*":
            head = prefix[:-1]
            if len(tokens) >= len(head) and tokens[: len(head)] == head:
                matched.append(tier)
        elif tokens == prefix:
            matched.append(tier)
    if matched:
        return _strictest(matched)
    return fallback


def _reaches_out(lexed: _Lexed) -> bool:
    """A redirection past the cwd: only a ``*`` rule may speak for the command."""
    return not all(_redirection_stays_home(op, target) for op, target in lexed.redirections)


def exec_rule_tier(command: str, table: dict[str, str]) -> Tier | None:
    """The user's tier for one shell command, or None when no rule speaks.

    Deny wins on any segment; allow requires every segment to allow. A segment
    no rule speaks about leaves the command unresolved unless another segment
    already denied -- half-covered is not covered.
    """
    rules = _pattern_tiers(table)
    if not rules:
        return None
    lexed = lex_command(command)
    if lexed is None or _reaches_out(lexed):
        return _strictest([t for _, pref, t in rules if pref is None])
    segment_tiers = [_segment_tier(list(segment), rules) for segment in lexed.segments]
    if not segment_tiers:
        return None
    if any(t is Tier.DENY for t in segment_tiers):
        return Tier.DENY
    if any(t is None for t in segment_tiers):
        return None
    return _strictest([t for t in segment_tiers if t is not None])


# Programs that run whatever follows them. A prefix rule starting with one is a
# rule for everything, so the prompt never suggests it and a pattern typed
# into the prompt may not start with it; a rule written into the config by hand
# is the user's own call and is matched like any other.
_WRAPPERS: frozenset[str] = frozenset(
    {
        "bash",
        "builtin",
        "chrt",
        "cmd",
        "command",
        "csh",
        "dash",
        "doas",
        "env",
        "fish",
        "flock",
        "ionice",
        "ksh",
        "ltrace",
        "nice",
        "noglob",
        "nohup",
        "nsenter",
        "pkexec",
        "powershell",
        "pwsh",
        "runuser",
        "script",
        "setsid",
        "sh",
        "stdbuf",
        "strace",
        "su",
        "sudo",
        "taskset",
        "time",
        "timeout",
        "tcsh",
        "unshare",
        "watch",
        "xargs",
        "zsh",
    }
)

# Runners of a project's own scripts: `uv run x` is any entrypoint in the
# project env, `npm run x` any package.json script, `make x` any recipe. A
# prefix over one of these is arbitrary code execution wearing a two-word
# name, so it is never suggested -- the most common prefix in real traffic
# (uv run, 402 of 19995 calls) and the one that would hand over the most.
_RUNNER_HEADS: frozenset[str] = frozenset({"bunx", "make", "npx"})

# CLIs whose second token names a thing, not an action: `gh pr` is every pull
# request operation, `glab mr list` and `glab mr merge` share it. The prefix
# for these has to carry the verb, so it is three tokens or nothing.
_NOUN_FIRST: frozenset[str] = frozenset({"aws", "az", "docker", "gcloud", "gh", "glab", "helm", "kubectl", "terraform"})

# A prefix is worth saving only where the second token names a subcommand, so
# only programs built that way are suggested for. Elsewhere it names a file, a
# host or a URL -- `hostname newname *` and `touch report.txt *` are rules that
# match one call and then sit in the config forever. Measured against real
# traffic this costs nothing: every prefix that recurred came from this shape.
# A program absent here is not refused, it is only never suggested; the config
# file is where an unusual rule belongs.
_SUBCOMMAND_CLIS: frozenset[str] = frozenset(
    {
        "apt",
        "apt-get",
        "aws",
        "az",
        "brew",
        "bun",
        "cargo",
        "docker",
        "gcloud",
        "gh",
        "git",
        "glab",
        "go",
        "gradle",
        "helm",
        "hg",
        "just",
        "kubectl",
        "mvn",
        "nix",
        "npm",
        "pip",
        "pip3",
        "pnpm",
        "poetry",
        "pyenv",
        "rustup",
        "systemctl",
        "terraform",
        "tmux",
        "uv",
        "yarn",
        "brew",
        "conda",
        "deno",
        "dotnet",
        "flatpak",
        "gem",
        "podman",
        "snap",
        "swift",
    }
)

# git is both shapes: `git push` is a verb, `git remote add` is a noun and a
# verb. These take the verb too, so `git remote add *` cannot be answered with
# `git remote remove`.
_GIT_NOUN_SUBCOMMANDS: frozenset[str] = frozenset(
    {
        "bisect",
        "bundle",
        "lfs",
        "maintenance",
        "notes",
        "reflog",
        "remote",
        "sparse-checkout",
        "stash",
        "submodule",
        "worktree",
    }
)

# Their next token is a name or a config key, not a verb: three tokens would
# pin one branch or one key and buy nothing, while two would answer
# `git branch -D`, `git tag -d`, or a `git config core.pager` that runs a
# program. Nothing here is safe to suggest; the config file is the place.
_GIT_NEVER_SUGGEST: frozenset[str] = frozenset({"branch", "config", "tag"})
_RUNNERS: frozenset[tuple[str, str]] = frozenset({("npm", "run"), ("pnpm", "run"), ("uv", "run"), ("yarn", "run")})

# What a second token has to look like to be a command family rather than one
# resource: `log`, `push`, `rev-parse` are families and a rule over them is
# reused; `scripts/check.py`, `-la`, `analyze.R` are one call each.
_FAMILY_NAME = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


@dataclass(frozen=True)
class ExecApprovalShape:
    """What the approval prompt for one command has to know.

    ``ask_segments`` are the segments no allow rule and no read-only listing
    covers, as text -- the part of the command a session grant remembers; the
    whole command when it could not be segmented. ``suggested_pattern`` is the
    prefix rule the prompt may offer to persist, empty when there is nothing
    safe to suggest.
    """

    ask_segments: tuple[str, ...]
    suggested_pattern: str = ""


def _suggest_pattern(segment: str) -> str:
    try:
        tokens = shlex.split(segment)
    except ValueError:
        return ""
    if len(tokens) < 2 or _ASSIGNMENT.match(tokens[0]):
        return ""
    head, sub = tokens[0], tokens[1]
    if "/" in head or head in _WRAPPERS or head in _RUNNER_HEADS:
        return ""
    if not _FAMILY_NAME.match(sub) or (head, sub) in _RUNNERS:
        return ""
    if head not in _SUBCOMMAND_CLIS:
        return ""
    if head == "git" and sub in _GIT_NEVER_SUGGEST:
        return ""
    # A prefix whose own bare form only reads would cover the very thing that
    # made this call ask: `git diff --output=x` asks because of the flag, and
    # `git diff *` allows every later `--output`. The command family is not
    # what needs approving here, so there is nothing safe to suggest.
    if exec_reads_only(" ".join(tokens[: _prefix_length(head, sub, tokens)])):
        return ""
    length = _prefix_length(head, sub, tokens)
    if length == 3 and (len(tokens) < 3 or not _FAMILY_NAME.match(tokens[2])):
        return ""
    return " ".join(tokens[:length]) + " *"


def _prefix_length(head: str, sub: str, tokens: list[str]) -> int:
    """How many tokens a suggestion for this command would carry."""
    return 3 if head in _NOUN_FIRST or (head == "git" and sub in _GIT_NOUN_SUBCOMMANDS) else 2


def exec_approval_shape(command: str, table: dict[str, str]) -> ExecApprovalShape:
    lexed = lex_command(command)
    if lexed is None or _reaches_out(lexed):
        return ExecApprovalShape(ask_segments=(command,))
    rules = _pattern_tiers(table)
    asking = tuple(
        shlex.join(segment)
        for segment in lexed.segments
        if _segment_tier(list(segment), rules) is not Tier.ALLOW and not _segment_reads_only_tokens(segment)
    )
    if not asking:
        return ExecApprovalShape(ask_segments=(command,))
    return ExecApprovalShape(
        ask_segments=asking,
        suggested_pattern=_suggest_pattern(asking[0]) if len(asking) == 1 else "",
    )


def validate_exec_pattern(pattern: str) -> str | None:
    """Why a pattern typed into the approval prompt may not become a rule, or
    None when it may. Shell punctuation never matches a prefix rule and would
    only sit in the table as noise; a wrapper at the head is a rule for
    everything."""
    if any(char in pattern for char in _PUNCTUATION + "$\n"):
        return "a prefix rule cannot contain shell punctuation"
    try:
        tokens = shlex.split(pattern)
    except ValueError:
        return "unbalanced quotes"
    if not tokens or tokens == ["*"]:
        return "a rule needs a command in front of the *"
    if tokens[0] in _WRAPPERS:
        return f"a rule starting with {tokens[0]} would allow anything run through it"
    return None


def user_tier(tool_name: str, params: dict[str, Any], tools_node: dict[str, Any]) -> Tier | None:
    """The user's tier for this call, or None when their config has no rule."""
    entry = tools_node.get(tool_name)
    if entry is None:
        return None
    if isinstance(entry, str):
        try:
            return Tier(entry)
        except ValueError:
            return None
    if isinstance(entry, dict):
        command = params.get("command")
        if tool_name == "exec" and isinstance(command, str):
            return exec_rule_tier(command, entry)
        return None
    return None


__all__ = [
    "DEFAULT_ALLOW_TOOLS",
    "READ_ONLY_COMMANDS",
    "ExecApprovalShape",
    "default_tier",
    "exec_approval_shape",
    "exec_reads_only",
    "exec_rule_tier",
    "lex_command",
    "user_tier",
    "validate_exec_pattern",
]
