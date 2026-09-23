"""Run one command on a machine the owner registered.

``exec`` runs on the computer the loop itself lives on. The owner's code and
their cases live on the machines in the connection registry, which may be a
different box or may be this one -- and the credentials for a different box
belong to the connection rather than to the loop. So "what is in that
directory / what did the solver print / how big is that output" is answered
here, by naming the machine, and ``exec`` routes to this the moment it is
given one.

This channel exists so the address never has to: with no ``machine``
parameter, every remote look is a raw ``ssh -p <port> root@<ip> ...`` typed
from an address the task statement had to carry -- measured 2026-09-03, a
whole field run where the registry's own projection (id and capabilities, no
host, no port, no key) was bypassed on every call because the raw address sat
in context. The registry resolves the id here, below the model.

Two things this refuses that a plain shell would not:

**Anything that outlives the call.** ``nohup``, a trailing ``&``, ``screen``,
``tmux``, ``at``, ``crontab``: on a registered machine, work that keeps
running belongs to a governed job runner (the on-call agent's ``ops_submit``),
which is what makes its budget enforceable, its state survive a restart, and
its results arrive when they are due. On the loop's own computer there is no
ledger to escape, so the plain shell keeps that freedom.

**Anything slower than a look.** Every command is capped, and the cap is
enforced on the machine itself so a kill reaches both ends -- a cap that only
killed the local ssh client left the remote process running (measured
2026-09-02: an orphaned training run held a GPU at 100% after its local
caller died). Something that needs longer is a job, and a job has a different
home.

The refusal above is a hint, not the fence. The fence is that the command runs
in a process group of its own and the group is stopped when the call ends,
whether the shell finished or the cap fired: reviewed 2026-09-07, a command
that backgrounded something with output redirected (``python train.py > log
2>&1 & echo ok``) slipped past the old ``&``-at-the-end guard, and a remote
``timeout`` wrapper had nothing left to kill once the shell's foreground half
had returned -- the call reported exit 0 in under a second while the child ran
on unbounded. Now the wrapper sweeps the group afterwards and says so in the
output, so a detach that was not refused is still not a detach.
"""

from __future__ import annotations

import asyncio
import re
import shlex
from pathlib import PurePosixPath
from typing import Any

# Long enough to tail a large log or hash a directory; far short of any solver
# round measured on these campaigns (16 to 30 minutes). That gap is the boundary.
_TIMEOUT_S = 60
_MAX_BYTES = 20000

# Shapes whose only purpose is to outlive the call. Matched loosely on purpose:
# this is here to stop the casual reach for a background job, and it says which
# door does that job instead.
_DETACH = (
    (r"\bnohup\b", "nohup"),
    # A single & that is not && (a chain), >& / <& / &> (redirections) or |&
    # (a pipe). Anywhere, not only at the end: `sleep 300 & echo started`,
    # `(sleep 300 &)` and `bash -c 'sleep 300 &'` all background something
    # (reviewed 2026-09-07). A literal & inside quotes is refused too; the
    # wrapper below is what actually holds the line, so this can stay loose.
    (r"(?<![&<>|])&(?![&>])", "a backgrounding &"),
    (r"\bsetsid\b", "setsid"),
    (r"\bdisown\b", "disown"),
    (r"\bscreen\b", "screen"),
    (r"\btmux\b", "tmux"),
    (r"\bat\s+(now|\d)", "at"),
    (r"\bcrontab\b", "crontab"),
    (r"\bsystemd-run\b", "systemd-run"),
)


def _detaching(command: str) -> str | None:
    for pattern, name in _DETACH:
        if re.search(pattern, command):
            return name
    return None


def machines_registered() -> bool:
    """Whether this install has any connection to offer the channel for.

    Read per schema build rather than cached: the registry can be written
    mid-session, and the loop rebuilds the tool schema every turn.
    """
    try:
        from raven.ops.connections import load

        return bool(load())
    except Exception:  # noqa: BLE001 -- a malformed registry must leave the plain shell working
        return False


# What punctuation_chars hands back as its own token, split by what the token
# does to the command. A separator ends it, so the words after one belong to the
# next command and not to this ssh.
_COMMAND_SEPARATORS = frozenset({";", "&", "&&", "|", "||", "|&", "(", ")"})

# A redirection does not end the command: `ssh 2>/dev/null -p 58717 root@host`
# is a single ssh. Reading one as a terminator -- or, worse, reading `&>` as a
# word and taking it for the destination -- lost the registered host that came
# after it (reviewed 2026-09-20). The operator and the file it names are
# stepped over instead, and this ssh's own words keep being read.
_REDIRECTIONS = frozenset({"<", ">", ">>", "<<", "<<<", "<&", ">&", "&>", "&>>", ">|", "<>"})

# An -o option's name and its value are separated by an equals sign or by
# whitespace; OpenSSH honours both spellings.
_OPTION_SPLIT = re.compile(r"\s*=\s*|\s+")

# ssh(1)'s own getopt string, copied from the OpenSSH_9.9p2 binary rather than
# listed from memory: a letter followed by ':' takes a value. The hand-kept set
# this replaces had lost `B` (bind interface), so `ssh -B lo -p 58717 host`
# read `lo` as the destination (reviewed 2026-09-21). OpenSSH_8.9 differs in
# one letter -- `P` takes no value there -- and the current release is read.
_SSH_OPTSTRING = "1246ab:c:e:fgi:kl:m:no:p:qstvxAB:CD:E:F:GI:J:KL:MNO:P:Q:R:S:TVw:W:XYy"
_SSH_VALUE_FLAGS = frozenset(ch for i, ch in enumerate(_SSH_OPTSTRING) if _SSH_OPTSTRING[i + 1 : i + 2] == ":")


def _read_option_group(word: str, tokens: list[str], index: int) -> tuple[int, str | None]:
    """Read one ``-xyz`` option group the way getopt does. ``(index, port)``.

    Letters are read in turn until one takes a value; the rest of the group is
    that value, or the next word when nothing is left (``-p58717``, ``-p
    58717``, ``-vp 58717``, ``-vvvp58717`` all name port 58717). Reading only a
    two-character ``-p`` skipped ``-vp`` as a group with no argument and took
    its port for the destination (reviewed 2026-09-21). ``port`` is the value
    the group gives the port -- from ``-p`` or from an ``-o`` port option -- or
    None; ``index`` is past whatever the group consumed.
    """
    letters = word[1:]
    for offset, letter in enumerate(letters):
        if letter not in _SSH_VALUE_FLAGS:
            continue
        value = letters[offset + 1 :]
        if not value and index < len(tokens):
            value = tokens[index]
            index += 1
        if letter == "p":
            return index, value
        if letter == "o":
            # `ssh -G` prints `port 58717` for -o Port=58717 and for
            # -o "Port 58717" alike. Splitting only on the equals sign dropped
            # the spaced spelling's value and left the port at 22, so a machine
            # registered on another port went unrecognised (reviewed 2026-09-20).
            pair = _OPTION_SPLIT.split(value.strip(), maxsplit=1)
            return index, (pair[1].strip() if len(pair) == 2 and pair[0].lower() == "port" else None)
        return index, None
    return index, None


def _ssh_destinations(command: str) -> list[tuple[str, int]]:
    """Every host and port an ``ssh`` word in a shell command would connect to.

    Empty when no token runs the ssh client, or when the line cannot be
    tokenised at all. Every ssh in the line is collected, not the first: a
    compound line reaches each of its commands, so stopping at one destination
    lets ``ssh <unregistered>; ssh <registered>`` through on the strength of
    the half that was allowed.

    Tokenised with ``punctuation_chars`` so that unspaced operators separate
    words the way a shell reads them -- ``true&&ssh`` is two commands, and
    plain splitting hands back one token that is neither.

    The executable may be written ``ssh``, ``/usr/bin/ssh`` or ``\\ssh`` (a
    backslash suppresses alias lookup and still runs the client), and all three
    reach the far side.

    The port is read from ``-p`` and from an ``-o`` port option in either of the
    spellings OpenSSH honours -- ``-o Port=58717`` and ``-o "Port 58717"`` --
    because the registry holds several machines at one address on different
    ports: the address alone picks whichever row is listed first and names the
    wrong machine. Repeats keep the first value, as ssh does.

    Options are read the way ssh reads them: a ``-xyz`` group letter by letter
    (``-vp 58717``), and on past the destination until the first word that is
    not an option (``ssh root@h -p 58717 true`` is port 58717, ``ssh root@h
    true -p 58717`` is 22) or a ``--``.
    """
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
    except ValueError:
        # An unbalanced quote is not a shell line this can read; the shell will
        # reject it too, so nothing reaches a machine either way.
        return []

    found: list[tuple[str, int]] = []
    index = 0
    while index < len(tokens):
        if PurePosixPath(tokens[index].lstrip("\\")).name != "ssh":
            index += 1
            continue
        index += 1
        port = 22
        port_set = False
        destination: str | None = None
        options_open = True
        options_ended = False
        while index < len(tokens):
            word = tokens[index]
            if word in _COMMAND_SEPARATORS or PurePosixPath(word.lstrip("\\")).name == "ssh":
                # The command ended, or the next one began; either way this
                # ssh's arguments are over and the token is left for the outer
                # loop to read.
                break
            following = tokens[index + 1] if index + 1 < len(tokens) else None
            if word in _REDIRECTIONS:
                # The operator and the file it names; the file is skipped only
                # when there is one, so a redirection left dangling before a
                # separator does not swallow the separator.
                index += 1
                if following is not None and following not in _COMMAND_SEPARATORS and following not in _REDIRECTIONS:
                    index += 1
                continue
            if word.isdigit() and following in _REDIRECTIONS:
                # `2>&1` arrives as the three tokens 2, >& and 1, so a bare file
                # descriptor can stand in front of the operator. Without this
                # the digit was taken for the destination and the real host,
                # further along the line, was never read. The tokens do not say
                # whether a space separated the digit from the operator, so a
                # destination that is itself a bare integer is read as a
                # descriptor here -- a shape no registry row has, since an
                # address carries dots or letters.
                index += 1
                continue
            index += 1
            if not options_open:
                # The remote command; nothing in it is this ssh's to read.
                continue
            if word == "--" and not options_ended:
                # getopt's end of options. Before the destination it makes the
                # next word the host whatever it looks like; after it, the rest
                # is the remote command: `ssh -G root@h -- -p 58717` prints 22.
                options_ended = True
                if destination is not None:
                    options_open = False
                continue
            if word.startswith("-") and len(word) > 1 and not options_ended:
                index, value = _read_option_group(word, tokens, index)
                # First obtained value wins, which is ssh's own rule for every
                # option: `ssh -G -p 2222 -o Port=58717 host` prints 2222, and
                # reversing the two prints 58717. Overwriting instead read
                # `-p 58717 -p 22` as port 22 and let a command that really
                # reaches the registered machine past the guard (reviewed
                # 2026-09-21).
                if value is not None and value.isdigit() and not port_set:
                    port = int(value)
                    port_set = True
                continue
            if destination is None:
                destination = word.rsplit("@", 1)[-1].strip("[]").lower()
                # OpenSSH re-enters its option loop once it has the host, so
                # `ssh root@h -p 58717 true` connects on 58717; stopping at the
                # destination read it as 22 and let the line past the guard
                # (reviewed 2026-09-21). A `--` already seen means no re-entry.
                options_open = not options_ended
                continue
            # The first non-option word after the host starts the remote
            # command, and ssh stops reading options there: `ssh -G root@h
            # true -p 58717` prints 22.
            options_open = False
        if destination:
            found.append((destination, port))
    return found


def raw_ssh_target(command: str) -> dict[str, Any] | None:
    """The registered machine a plain-shell command reaches over its own ssh.

    ``None`` when the command runs no ssh client, or names a destination the
    registry does not know, or the registry cannot be read: the plain shell
    keeps working for everything that is not the bypass this looks for.

    Host and port are both matched, and the host as a whole word rather than a
    substring -- ``203.0.113.70`` is not ``203.0.113.7``, and a machine the
    registry does not hold must still be reachable from here.

    The bypass is measured, not hypothetical. Two field runs on 2026-09-14
    put the machine's address in the task statement, and the coding nodes
    typed ``ssh -p <port> root@<ip> '... &'`` from the local shell 58 times to
    start GPU work: the look here is capped at 60 s and on-call's job runner
    was not theirs to call, so the address was the path of least resistance,
    and the ledger never saw the runs. Only ssh is matched -- ``scp`` and
    ``rsync`` move files and start nothing on the far side.
    """
    destinations = _ssh_destinations(command)
    if not destinations:
        return None
    try:
        from raven.ops.connections import load

        rows = load()
    except Exception:  # noqa: BLE001 -- a malformed registry must leave the plain shell working
        return None
    for host, port in destinations:
        for row in rows:
            row_host = str(row.get("host") or "").strip().lower()
            if not row_host or row_host != host:
                continue
            try:
                row_port = int(row.get("port") or 22)
            except (TypeError, ValueError):
                row_port = 22
            if row_port == port:
                return row
    return None


def _runner_for_connection(conn_id: str, *, cap_seconds: float | None = None):
    """A command runner for a machine named by id, plus its row.

    Returns ``(None, message)`` when the id is unknown or unreachable, so the
    caller can hand the reason back rather than a traceback.
    """
    from raven.ops.connections import load, shown

    # Looked up here rather than through a registry-side ``get``: main keeps
    # the registry's readers to what the CLI and this lane need (0fd38ef8),
    # and the model-facing rendering of the list lives with the caller.
    rows = load()
    wanted = str(conn_id or "").strip()
    row = next((r for r in rows if str(r.get("id") or "").strip() == wanted), None)
    if row is None:
        listing = "\n".join(
            f"  {r.get('display_name') or r.get('id')}   (id {r.get('id')})"
            + "".join(f"   {k} {v}" for k, v in shown(r).items() if k not in ("id", "display_name"))
            for r in rows
        )
        return None, (
            f"No connection with id {conn_id!r}.\n"
            + (f"Connections this instance can run on:\n{listing}" if rows else "No connection is registered.")
        )
    from raven.ops.transport import TransportError, runner_from

    try:
        runner = runner_from(row, cap_seconds=cap_seconds)
    except TransportError as exc:
        return None, f"Cannot reach {conn_id!r}: {exc}"
    return runner, row


# The look itself, run through bash on the machine. `$1` is the command.
#
# `set -m` puts the command's shell in a process group of its own (job control
# does that for a backgrounded job, no `setsid` binary needed -- macOS has
# none), so everything it starts can be signalled as one group: the children a
# plain `&` leaves behind stay in that group, and `setsid`/`nohup`/`disown`
# are refused before this runs. A watchdog subshell stops the group at the cap
# and the exit code becomes 124, the code GNU `timeout` uses, so the caller
# reads one code for a cap kill however the machine is reached. After the
# shell returns, anything still alive in the group is stopped and the output
# says so: a look ends when the call ends. `exec 2>/dev/null` hides the
# wrapper's own job-control chatter ("Terminated: 15"); the command's stderr
# was merged into stdout before that, inside its own shell.
#
# The watchdog is a group of its own as well, and holds neither stdout nor
# stderr: a `sleep` left inside it would otherwise keep the output pipe open
# after the command was done, and ssh (or the local runner) waits for that
# pipe to close -- reviewed 2026-09-07, a swept look took the whole cap to
# return for exactly that reason.
_LOOK = """exec 2>/dev/null
set -m
bash -c "$1" 2>&1 &
pid=$!
( sleep {cap}; kill -TERM -- -$pid; sleep 5; kill -KILL -- -$pid ) >/dev/null 2>&1 &
wd=$!
set +m
wait $pid; rc=$?
[ $rc -ge 128 ] && [ $SECONDS -ge {cap} ] && rc=124
if kill -0 -- -$pid; then
  echo {note}
  kill -TERM -- -$pid; sleep 1; kill -KILL -- -$pid
fi
kill -KILL -- -$wd
exit $rc
"""

_SWEPT_NOTE = (
    "[raven: this command left work running in the background; it was stopped, because a look "
    "on a registered machine ends when the call ends. Work that has to keep running there is a "
    "job for the on-call agent's ops_submit.]"
)


def look_script(command: str) -> str:
    """The bash line that runs ``command`` on a machine as one capped, swept group."""
    script = _LOOK.format(cap=_TIMEOUT_S, note=shlex.quote(_SWEPT_NOTE))
    return f"bash -c {shlex.quote(script)} raven-look {shlex.quote(command)}"


async def run_on_machine(command: str, *, connection: str, cwd: str | None = None) -> str:
    """Run one command on a registered machine and return what it said."""
    command = (command or "").strip()
    if not command:
        return "No command given."
    detached = _detaching(command)
    if detached:
        # Refused before anything is sent, so a refusal never half-runs.
        return (
            f"Refusing this command: it uses {detached}, which is a way to leave work "
            "running after this call returns. On a registered machine that work belongs "
            "to a governed job runner -- the on-call agent's ops_submit -- which makes "
            "its compute budget enforceable, its state survive a restart, and its "
            "results arrive when they are due. This channel is for looking at the "
            f"machine; anything it runs is killed after {_TIMEOUT_S}s, and anything it leaves "
            "running in the background is stopped when the call returns. Nothing was run."
        )

    # The runner's own cap is a backstop behind the wrapper's: the wrapper
    # fires first, keeps the output and reports 124; the runner's group kill is
    # for a wrapper that could not run at all (a machine with no bash).
    runner, row = _runner_for_connection(connection, cap_seconds=_TIMEOUT_S + 15)
    if runner is None:
        return row  # the message explaining why not

    where = (cwd or ".").rstrip("/") or "/"
    # bash, not sh: /bin/sh is dash on these hosts, and shell gets written the
    # way bash is written -- `[[ ]]`, brace expansion, arrays. The cap and the
    # sweep are the wrapper's own (see _LOOK), not GNU `timeout`: that binary
    # is coreutils, macOS does not ship it, and a local connection is most
    # likely to BE a mac -- measured 2026-08-19, the first local look came back
    # `exit 127, timeout: command not found`.
    from raven.ops.transport import TIMED_OUT_RC

    body = look_script(command)
    # to_thread: the runner is a synchronous subprocess.run, and calling it
    # bare from this async def would freeze the whole event loop for up to
    # _TIMEOUT_S per look. Measured 2026-08-27 on the fork: two back-to-back
    # remote probes blocked frame reading long enough that a concurrent
    # `session/new` sat unparsed for the host's full 120s readyTimeoutMs and
    # the second instance was declared dead.
    rc, out = await asyncio.to_thread(runner, f"cd {shlex.quote(where)} 2>/dev/null || exit 66; {body}")
    text = out if len(out) <= _MAX_BYTES else out[:_MAX_BYTES] + f"\n...[truncated, {len(out)} bytes total]"
    head = f"on {_where(row)} in {where} (exit {rc})"
    if rc == 66:
        return f"{head}: no such directory. Nothing was run."
    if rc == TIMED_OUT_RC:
        return (
            f"{head}: killed at the {_TIMEOUT_S}s limit -- the host answered, the "
            f"command did not finish. Anything that takes longer is a job, and a job "
            f"belongs to the on-call agent's ops_submit.\n{text}"
        )
    return f"{head}\n{text}" if text.strip() else f"{head}, no output"


def _where(row: dict[str, Any]) -> str:
    return str(row.get("display_name") or row.get("id") or "the machine")
