"""One shared interpretation of a stored launch-command string.

A subagent row stores its launch as one string (``command``), because a user
edits it in config and reads it back the same way. Two readers need it, and
they want different tokenisers:

- to **start** it, the split must match the parser the spawn applies --
  ``shlex`` on POSIX, the Win32 ``CommandLineToArgvW`` backslash rules on
  Windows. POSIX shlex on a Windows drive path eats the backslashes, so
  ``C:\\Users\\..`` reached ``CreateProcess`` as ``C:Users..``.
- to **judge** it (does the launcher it names exist?), the split must keep
  each token's shape so ``_is_absolute_path`` can read a Windows drive-letter
  path as absolute on any host -- the shape judgment is what fail-closes a
  Windows-shaped product on a POSIX scan, which POSIX shlex would destroy.

Without this module every caller reached for ``shlex.split`` (POSIX mode) on
any host, so the launch arm mangled Windows paths and the shape arm could not
see a quoted Windows path as one token.
"""

import os
import re
import shlex


def _split_windows(command: str) -> list[str]:
    """Tokenise one Windows command line by the CommandLineToArgvW rules.

    Narrower than the real parser in two places, neither of them a spelling
    ``command_quote`` produces: a ``""`` pair inside quotes, which Windows reads
    as a literal ``"``, yields nothing here, and an unclosed quote raises where
    Windows reads the rest of the line as one token.
    """
    argv: list[str] = []
    token: list[str] = []
    token_started = False
    in_quotes = False
    i = 0
    length = len(command)
    while i < length:
        char = command[i]
        if char == "\\":
            run = 0
            while i < length and command[i] == "\\":
                run += 1
                i += 1
            if i < length and command[i] == '"':
                token.append("\\" * (run // 2))
                if run % 2:
                    token.append('"')
                    i += 1
                else:
                    in_quotes = not in_quotes
                    token_started = True
                    i += 1
            else:
                token.append("\\" * run)
        elif char == '"':
            in_quotes = not in_quotes
            token_started = True
            i += 1
        elif char in " \t" and not in_quotes:
            if token_started:
                argv.append("".join(token))
                token = []
                token_started = False
            i += 1
        else:
            token.append(char)
            token_started = True
            i += 1
    if in_quotes:
        raise ValueError("unbalanced quotes in command")
    if token_started:
        argv.append("".join(token))
    return argv


def _split_shape(command: str) -> list[str]:
    """Read host quoting while preserving absolute paths from the other host.

    Windows drive and UNC paths retain Windows backslash rules on POSIX, and
    a single-quoted POSIX absolute path stays grouped on Windows. Other tokens
    follow the host's escape rules, so a literal quote in an argument cannot
    swallow the launcher that follows it. Unclosed groups consume the rest
    of the command; syntax validation belongs to the launch parser.
    """
    argv: list[str] = []
    token: list[str] = []
    token_started = False
    quote: str | None = None
    windows = os.name == "nt"
    windows_token = windows
    i = 0
    while i < len(command):
        char = command[i]
        if not token and quote != "'" and re.match(r"(?:[A-Za-z]:[\\/]|\\\\)", command[i:]):
            windows_token = True
        if char == "\\" and quote != "'":
            token_started = True
            if windows_token:
                start = i
                while i < len(command) and command[i] == "\\":
                    i += 1
                run = i - start
                if i < len(command) and command[i] == '"':
                    token.append("\\" * (run // 2))
                    if run % 2:
                        token.append('"')
                    else:
                        quote = None if quote else '"'
                    i += 1
                else:
                    token.append("\\" * run)
                continue
            if i + 1 < len(command) and (quote is None or command[i + 1] in '\\"'):
                i += 1
                token.append(command[i])
            else:
                token.append(char)
        elif char == quote:
            quote = None
        elif quote is None and (
            char == '"'
            or (
                char == "'"
                and (
                    not windows_token
                    or (not token_started and command[i + 1 :].startswith("/") and "'" in command[i + 1 :])
                )
            )
        ):
            quote = char
            token_started = True
        elif char in " \t" and quote is None:
            if token_started:
                argv.append("".join(token))
                token = []
                token_started = False
                windows_token = windows
        else:
            token.append(char)
            token_started = True
        i += 1
    if token_started:
        argv.append("".join(token))
    return argv


def command_argv(command: str) -> list[str]:
    """Turn a stored launch-command string into an argv list for spawning.

    POSIX mode shlex on a POSIX host, the CommandLineToArgvW rules on Windows.
    Raises ``ValueError`` on an unbalanced double quote (shlex's "No closing
    quotation", or the Windows half's own check), the same class callers catch
    today.
    """
    command = command.strip()
    if os.name == "nt":
        return _split_windows(command)
    return shlex.split(command)


def command_tokens(command: str) -> list[str]:
    """The tokeniser for judging a command's paths, shape-preserving on any host."""
    return _split_shape(command)


def resolve_subagent_command(template: str, *, python: str, subagent_dir: str, quote: bool = True) -> str:
    """Substitute ``{PYTHON}`` and ``{SUBAGENT_DIR}`` in a manifest field.

    The one rule every producer of a roster row agrees on: a field that is
    split back into argv before spawning (``command`` / ``resumeCommand``, the
    default ``quote=True``) gets each path token that names the interpreter or
    the agent root quoted the way that host's parser reads, so a spaced one
    survives instead of being refused. A field the spawn reads as a single
    path (``cwd``, ``quote=False``) is substituted unquoted.

    The quoting happens at replacement: each value is quoted with
    :func:`command_quote` and the template's placeholders swapped for the
    quoted spelling, so a spaced value reads as one token to the parser.
    The shipped manifests use ``{SUBAGENT_DIR}/run.py`` -- a forward slash --
    so the quoted root and its launcher stay one token on both hosts.
    Discovery (``vendored_agents``), ``raven agents new --register`` and each
    folder's ``install.py`` all write through this, so the three agree.
    """
    directory, interpreter = subagent_dir, python
    if quote:
        directory = command_quote(directory)
        interpreter = command_quote(interpreter)
    return template.replace("{SUBAGENT_DIR}", directory).replace("{PYTHON}", interpreter)


def command_quote(value: str) -> str:
    """Quote one argv token for the platform this command will spawn on.

    The string a command template is built from is split again before
    spawning, so a token carrying a space must reach that split already quoted
    the way that host's parser reads -- ``shlex.quote`` on POSIX, surrounding
    double quotes on Windows. There a ``"`` inside the token is escaped as
    ``\\"``, and any run of backslashes in front of a quote, the closing one
    included, is doubled: CommandLineToArgvW halves a backslash run only where
    a quote follows it, so a trailing ``\\`` left single would escape the
    closing quote. Producing this platform's quoting is what makes a template
    round-trip back into the same argv it was built from.
    """
    if os.name == "nt":
        escaped = re.sub(r'(\\*)"', r'\1\1\\"', value)
        return '"' + re.sub(r"(\\+)\Z", r"\1\1", escaped) + '"'
    return shlex.quote(value)
