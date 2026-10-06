"""One shared interpretation of a stored launch-command string.

A subagent row stores its launch as one string (``command``), because a user
edits it in config and reads it back the same way. On POSIX that string is
tokenised the way ``shlex`` always has (POSIX mode: backslashes escape, quotes
both group and escape). On native Windows it is tokenised by the same
backslash rules as the Win32 ``CommandLineToArgvW`` parser that
``CreateProcess`` passes straight through, because that is the parser the
spawned process will apply.

Without this module every caller reached for ``shlex.split`` (POSIX mode) on
any host, so on Windows the interpreter and launcher paths -- which carry
backslashes like ``C:\\Users\\...`` -- lost them to shlex's escape processing
and the spawn rewrote the command itself.
"""

import os
import shlex
import shutil


def _split_windows(command: str) -> list[str]:
    """Tokenise one Windows command line by the CommandLineToArgvW rules.

    ``token_started`` tracks a token the quotes opened but no character has
    filled yet, so a quoted-but-empty argument (``""``) survives as a token
    the way the spawning host's parser would deliver it.
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


def command_argv(command: str) -> list[str]:
    """Turn a stored launch-command string into an argv list for spawning.

    POSIX mode shlex on a POSIX host, the CommandLineToArgvW rules on Windows.
    Raises ``ValueError`` on an unbalanced quote (shlex's "No closing
    quotation", or the Windows half's own check), the same class callers catch
    today.
    """
    command = command.strip()
    if os.name == "nt":
        return _split_windows(command)
    return shlex.split(command)


def _resolve_executable_windows(exe: str) -> str:
    """The full path of a bare executable name on Windows, with extension.

    The process-launch API finds ``npx.cmd`` only when it is handed a full
    path (no extension search, no suffix search); ``shutil.which`` is the one
    place that applies the PATHEXT lookup the shell would have done. A bare
    name with no extension becomes the resolved path; anything already
    carrying a path or extension is returned unchanged so the spawn does not
    differ from the string the operator wrote.
    """
    root, ext = os.path.splitext(exe)
    if ext or not root or os.sep in exe or (os.altsep and os.altsep in exe):
        return exe
    return shutil.which(exe) or exe


def command_quote(value: str) -> str:
    """Quote one argv token for the platform this command will spawn on.

    The string a command template is built from is split again by
    ``command_argv`` before spawning, so a token carrying a space must reach
    that split already quoted the way that host's parser reads -- ``shlex.quote``
    on POSIX, the surrounding-double-quote (with internal ``"`` escaped as
    ``\\"``) on Windows. Producing this platform's quoting is what makes a
    template round-trip back into the same argv it was built from.
    """
    if os.name == "nt":
        return '"' + value.replace('"', '\\"') + '"'
    return shlex.quote(value)


def launch_argv(command: str) -> list[str]:
    """The argv to spawn for a stored launch-command string.

    ``command_argv`` plus, on Windows, the same executable resolution a
    bare-name lookup needs. POSIX callers get ``command_argv`` unchanged,
    because the shell the spawn falls back to does its own PATH lookup there.
    """
    argv = command_argv(command)
    if argv and os.name == "nt":
        argv[0] = _resolve_executable_windows(argv[0])
    return argv
