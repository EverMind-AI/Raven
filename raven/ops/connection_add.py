"""The write half of the machine registry: reach a machine, then write it down.

:mod:`raven.ops.connections` reads the owner's machines and stays read-only on
purpose. This module is the one door that adds a row, and it is the same door
whichever surface knocks: the owner in a terminal (``raven ops connection
add``), or an agent that was told about a machine in conversation
(``ops_connection_add``). Until 2026-09-06 the host ran the command on the
owner's behalf before dispatching a sub-agent; that step went with the
pre-dispatch registry gate, and the write moved to the on-call product's own
plugin. It sits here now because the coding agent runs on the owner's machines
too -- it builds a case where the solver is and smoke-tests it there -- and a
second copy of the probe in a second plugin is the drift this tree already paid
for once (five byte-identical registries on one computer, 2026-08-25).

Two things keep what the owner is asked short. What a machine can say about
itself -- cores, memory, devices -- is read off it once reached, never typed.
And what the owner leaves out of the way in -- port, username, key path -- is
resolved the way their own terminal would resolve it (``ssh -G``, config
blocks included), then tried: the probe decides, and only what connected is
written. Nothing is written until the machine itself answers; an unreachable
row in the registry is a fact every later turn acts on, an absent one is a
question the loop knows to ask.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

# The reader is reached through its module at call time, not bound at import:
# where the store is gets decided per call (see ``connections.store_path``),
# and a test that stands a scratch registry in patches it there.
from raven.ops import connections
from raven.ops.connections import LOCAL, SSH, UNREADABLE, password_auth, password_path, transport_of
from raven.ops.transport import ALIAS_MARK, ASKPASS_REFUSED, ASKPASS_UNRESOLVED, TransportError
from raven.utils.atomic_io import atomic_replace, write_transaction

# What the owner has to be asked for, when the registry lists nothing that
# fits. Shared text: the on-call agent's listing reply and the coding agent's
# guide both point at it, so the questions are spelled once. The loop cannot
# fill any of it in -- the address and the key are deliberately outside what it
# can see -- and it must not guess: a guess that lands in the registry stops
# being a guess and becomes what every later turn reads as fact.
ASK_OWNER = """\
Ask the owner, in one message, and stop there until they answer:
  - is the machine this very computer, or another one reached over ssh?
  - what do they call it?
  - if another one: its address -- and port, username and private-key path
    only if they know them; left out, ssh's own config is tried and whatever
    connects is kept
Optional, only if they care to say: what is installed on it (with paths),
which directories on it hold their work, and how its budget is counted.
A machine that takes only a password is registered by the owner, not through a
tool call: point them at the Agent Hub's Machines tab, Login: Password, or at
`raven ops connection add --auth password --password-stdin --non-interactive`
in a terminal. The password is typed or piped there and never passes through
you.

With the answers, call ops_connection_add: it reaches the machine before
writing anything, and what the machine says about itself is read off it.
Until a machine is listed there is nothing to run on there. Do not look for a
way in yourself -- not with exec, not with an ssh typed from an address in the
task statement."""

# What the machine is asked about itself. Read once reached, never typed twice.
PROBE_SCRIPT = (
    "echo CORES=$(nproc 2>/dev/null || sysctl -n hw.ncpu 2>/dev/null); "
    "echo MEM=$(free -g 2>/dev/null | awk '/^Mem:/{print $2}'); "
    "echo GPU=$(nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | paste -sd'|' -); "
    "echo LIBC=$(ldd --version 2>/dev/null | head -1)"
)


def ssh_defaults(host: str, *, port: int = 0, user: str = "") -> dict[str, Any]:
    """What ssh itself would use for this host: ``port``, ``user``, ``keys``.

    ``ssh -G`` prints the resolved configuration -- ``~/.ssh/config`` blocks
    included -- so an owner who left the port, the user or the key path out
    gets the values their own ssh would pick, not a guess. ``keys`` lists only
    identity files that exist here, in ssh's own order; the caller probes each
    and keeps the one that connects. Empty when ssh is not on this computer,
    or cannot say: the same reader a password machine's prompt is resolved by
    (``transport.resolve_with_ssh``), with plain ssh's command line.
    """
    from raven.ops.transport import resolve_with_ssh

    resolved = resolve_with_ssh(["ssh"] + (["-p", str(port)] if port else []) + [f"{user}@{host}" if user else host])
    if resolved.failure:
        return {}
    out: dict[str, Any] = {"keys": []}
    if resolved.first("port").isdigit():
        out["port"] = int(resolved.first("port"))
    if resolved.first("user"):
        out["user"] = resolved.first("user")
    for path in resolved.values.get("identityfile", ()):
        if path and Path(os.path.expanduser(path)).exists():
            out["keys"].append(path)
    return out


#: ssh's own identity files, in the order it offers them: the IdentityFile
#: defaults of ssh_config(5), as ``ssh -G`` prints them on OpenSSH 8.9, 9.6
#: and 9.9 alike -- ``id_rsa`` first. The form prefills the first one that
#: exists, so this order is what decides which key an owner with two of them
#: is proposed; measured rather than assumed, since the other way round
#: proposed the key ssh itself would try second.
DEFAULT_IDENTITIES = ("id_rsa", "id_ecdsa", "id_ecdsa_sk", "id_ed25519", "id_ed25519_sk", "id_xmss", "id_dsa")


def local_keys() -> list[str]:
    """The private keys this computer has to offer, as ``~/.ssh/<name>`` paths.

    What the register form proposes where the owner would otherwise type a
    path from memory (the form's old placeholder named ``id_ed25519`` to an
    owner whose key is ``id_rsa``): ssh's own default identities that exist,
    in the order ssh tries them, then any other file in ``~/.ssh`` with a
    ``.pub`` beside it, by name. Nothing is read beyond the directory listing.
    The paths are spelled with ``~`` the way ``ssh -G`` spells them, which the
    transport hands to ssh unchanged and ssh expands itself. Empty when there
    is no ``~/.ssh`` or nothing in it looks like a key; the owner then types a
    path, or registers the machine with its password.
    """
    ssh_dir = Path(os.path.expanduser("~/.ssh"))
    try:
        names = sorted(p.name for p in ssh_dir.iterdir() if p.is_file())
    except OSError:
        return []
    present = set(names)
    found = [name for name in DEFAULT_IDENTITIES if name in present]
    found += [name[:-4] for name in names if name.endswith(".pub") and name[:-4] in present and name[:-4] not in found]
    return [f"~/.ssh/{name}" for name in found]


def probe(
    row: dict[str, Any], *, timeout: float = 30.0, isolate_key: bool = False, password: str | None = None
) -> tuple[bool, str, dict[str, Any]]:
    """Reach the machine and read what it is. ``(reached, message, detected)``.

    The reaching is the point and the reading is the bonus. A row that cannot be
    reached is not written, because an unreachable row in the registry is worse
    than an absent one: absent is a question the loop knows to ask, unreachable
    is a fact it acts on. Rides :mod:`raven.ops.transport`, the seam ``exec``'s
    machine channel reaches a machine through, so the way a machine is reached
    here is the way it will be reached afterwards.

    ``isolate_key`` is for the callers that have to say WHICH key opened the
    session rather than merely that one opened. Without it ``-i`` is a
    preference and not a restriction -- the other configured identities and
    the agent are offered too -- so the first candidate could be credited with
    a session a different key authenticated, and the registry would then hold
    a path that stops working the day that agent or config changes. The agent
    tool trying the keys ssh named, when the owner gave no path, isolates
    fully: no config read at all. A password row (``auth: password``) is reached the one
    way every later connection reaches it, through the askpass helper
    (``transport.ssh_env``), and ``isolate_key`` does not apply: there is no
    key to prove. A registration supplies its own ``password`` so overlapping
    probes do not authenticate with another registration's password file.
    """
    from raven.ops.transport import TIMED_OUT_RC, make_ssh_runner, runner_from, ssh_target

    where = "this computer" if transport_of(row) == LOCAL else f"{row.get('user')}@{row.get('host')}:{row.get('port')}"
    password_file = None
    try:
        if password_auth(row) and password is not None:
            path = password_path(str(row["id"]))
            fd, name = tempfile.mkstemp(prefix=f".{row['id']}.probe.", suffix=".password", dir=path.parent)
            password_file = Path(name)
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(password + "\n")
        if isolate_key and transport_of(row) == SSH and not password_auth(row):
            # The row read the one way every other route onto a machine reads
            # it; only the key restriction is added.
            host, port, key, user = ssh_target(row)
            runner = make_ssh_runner(host, port, key, user=user, identities_only=True, cap_seconds=timeout)
        elif password_file is not None:
            runner = runner_from(row, cap_seconds=timeout, password_file=password_file)
        else:
            runner = runner_from(row, cap_seconds=timeout)
        code, out = runner(PROBE_SCRIPT)
    except TransportError as exc:
        return False, f"could not reach {where}: {exc}", {}
    except FileNotFoundError:
        return False, f"could not reach {where}: ssh not found on this computer", {}
    finally:
        if password_file is not None:
            password_file.unlink(missing_ok=True)
    if code == TIMED_OUT_RC:
        # Reached and then silent, or never answering at all: either way a row
        # is not written on it, and the owner is told how long was waited.
        return False, f"could not reach {where}: no answer within {timeout:.0f}s", {}
    if code != 0:
        said = out.strip() or f"exit {code}"
        if password_auth(row) and ASKPASS_UNRESOLVED in said:
            # The helper waited for the row's spelling because ssh could not
            # resolve the host; the machine's prompt may name the host ssh
            # did resolve when it connected, so this is no other question.
            said += (
                "\nRaven could not learn from ssh which host this machine's password prompt names (above), so the "
                "kept password waited for the prompt at the registered address, and this prompt was not that one, so "
                "it was not sent. Check that `ssh -G` for this address works on this computer, or register the "
                "machine at the address ssh logs in to."
            )
        elif password_auth(row) and ASKPASS_REFUSED in said:
            # The helper gave ssh nothing, so the refusal that follows is no
            # verdict on the password: the machine asked another question.
            said += (
                "\nssh asked something other than this account's password (a one-time code, another account's "
                "password, or a question worded another way); the kept password answers only its own prompt, so it "
                "was not sent. A machine that asks that is registered with a key."
            )
        elif password_auth(row) and re.search(r"permission denied|password|authentication", said, re.IGNORECASE):
            # ssh says only that the login was refused; the other cause is a
            # sshd that takes no password at all, which looks the same.
            said += (
                "\nThe password was refused, or this machine does not accept password logins "
                "(PasswordAuthentication is off in its sshd)."
            )
        return False, f"could not reach {where}: {said}", {}
    return True, f"reached {where}", parse_probe(out)


def store_password(conn_id: str, password: str) -> Path:
    """Keep ``password`` for ``conn_id`` where ssh's askpass reads it: ``<home>/keys/<id>.password``, mode 0600.

    The owner's decision of 2026-10-19: the machine's password is what this
    computer logs in with, every time, and it is kept here at the protection
    a file readable by the owner alone gives -- the same the provider keys in
    ``config.json`` have -- rather than behind a keychain or a master password,
    which a service that restarts and runs at night with nobody there could
    not open. One line, newline-terminated, which is what askpass hands ssh;
    never in the registry, never on a command line, never in a log
    (``raven.config.held_secrets`` scrubs it from what a model reads, which
    is why one shorter than that scrubber's floor is refused here: that
    short, it would pass as ordinary text and a model could read it back).
    Refused, too, for an id the registry refuses (``password_path``), before
    anything is written. Removing the machine deletes the file.
    """
    return _store_password(conn_id, password)[0]


def store_registration_password(conn_id: str, password: str) -> tuple[int, int, int]:
    """Keep a registration's password and return the file identity its cleanup may remove."""
    return _store_password(conn_id, password, unregistered=True)[1]


def _password_identity(path: Path) -> tuple[int, int, int]:
    stamp = path.stat()
    return stamp.st_dev, stamp.st_ino, stamp.st_ctime_ns


def _store_password(conn_id: str, password: str, *, unregistered: bool = False) -> tuple[Path, tuple[int, int, int]]:
    from raven.config.held_secrets import PASSWORD_MIN_LEN

    if "\n" in password or "\r" in password:
        # ssh takes the askpass answer up to the first line break, and the
        # scrubber reads the file back line by line: a password with one in it
        # would reach the machine as one string, be kept as another and be
        # measured against the floor as a third. One owner for what a
        # password is, so a client that reads one from a file is held to it.
        raise ValueError(
            "a password cannot contain a line break: ssh takes the askpass answer up to the first one, so what the "
            "machine is given and what is kept here would differ; use a password on one line, or register with a key"
        )
    if len(password) < PASSWORD_MIN_LEN:
        raise ValueError(
            f"a password shorter than {PASSWORD_MIN_LEN} characters cannot be kept: that short, it would pass the "
            "scrubber as ordinary text and a model could read it back; use a longer password, or register with a key"
        )
    path = password_path(conn_id)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    with write_transaction(path):
        if unregistered:
            found = connections.read()
            if found.state == UNREADABLE:
                raise ValueError(found.detail)
            if any(str(r.get("id") or "").strip() == conn_id for r in found.rows):
                raise ValueError(f"a machine with id {conn_id!r} is already listed in the registry")
        atomic_replace(path, password + "\n", mode=0o600)
        return path, _password_identity(path)


def forget_password(conn_id: str, *, holding: str | None = None, identity: tuple[int, int, int] | None = None) -> bool:
    """Delete the password kept for ``conn_id``; whether one was deleted. An id none could be kept under has none.

    With ``holding``, only a file that still holds exactly that password is
    deleted: the file is keyed by id alone, and a registration cleaning up after
    itself must not take away what a registration of the same id that landed
    meanwhile keeps there -- the landed row would then be refused at its every
    connection, for a password it never lost. ``identity`` also distinguishes
    overlapping registrations that keep the same password before either row
    is written.
    """
    try:
        path = password_path(conn_id)
    except ValueError:
        return False
    if not path.exists():
        return False
    with write_transaction(path):
        if holding is not None or identity is not None:
            try:
                if identity is not None and _password_identity(path) != identity:
                    return False
                if holding is not None and path.read_text(encoding="utf-8") != holding + "\n":
                    return False
            except OSError:
                return False
        had = path.exists()
        path.unlink(missing_ok=True)
        return had


def forget_unowned_password(conn_id: str, password: str, *, identity: tuple[int, int, int]) -> bool:
    """After a registration that wrote no row: forget the password it kept, unless it is a row's now; whether it did.

    The file is keyed by id alone, so another registration of the same id can
    land between this one's duplicate check and its write -- most often the
    same machine submitted twice. The file is deleted only while it is this
    registration's write, still holds its password and no row of the id is
    listed: a landed row keeps what is there, whichever call wrote it, and an
    unreadable registry is left alone too. A password file nothing lists is
    harmless; a listed row without one is refused at its every connection.
    """
    path = password_path(conn_id)
    if not path.exists():
        return False
    # The identity check and unlink share the writer's lock: another store
    # must not replace the checked file before cleanup removes it.
    with write_transaction(path):
        found = connections.read()
        if found.state == UNREADABLE or any(str(r.get("id") or "").strip() == conn_id for r in found.rows):
            return False
        return forget_password(conn_id, holding=password, identity=identity)


def parse_probe(out: str) -> dict[str, Any]:
    """The row fields a probe's output names. Only what parsed cleanly."""
    seen: dict[str, str] = {}
    for line in out.splitlines():
        key, _, value = line.partition("=")
        if value.strip():
            seen[key.strip()] = value.strip()
    found: dict[str, Any] = {}
    if seen.get("CORES", "").isdigit():
        found["cores"] = int(seen["CORES"])
    if seen.get("MEM", "").isdigit() and int(seen["MEM"]) > 0:
        found["memory"] = f"{seen['MEM']} GB"
    gpu = seen.get("GPU", "")
    if gpu:
        cards = [c.strip() for c in gpu.split("|") if c.strip()]
        # The count is what admission reads (`gpus`), and the device line is
        # written as "N x <card>" when the cards match, which is the other
        # spelling the readers infer a count from. Left as a "+"-joined list,
        # the row a real two-card box probed to (2026-09-07) had no count at all,
        # and admission fell back to job count on a machine it could have gated
        # by device.
        found["gpus"] = len(cards)
        found["device"] = f"{len(cards)} x {cards[0]}" if len(set(cards)) == 1 else " + ".join(cards)
        found["kind"] = "gpu"
    elif "cores" in found:
        found["kind"] = "cpu"
    if seen.get("LIBC"):
        found["note"] = seen["LIBC"]
    return found


def alias_collision(row: dict[str, Any], rows: list[dict[str, Any]]) -> str:
    """Why registering ``row`` beside ``rows`` would let a ``Host`` alias Raven writes decide where a password
    machine logs in; empty when it would not.

    Raven writes ``Host <id>`` for every key machine (:func:`write_ssh_alias`),
    and ssh follows it for any connection to that name, case-blind. A password
    machine registered at such a name would log in at the key machine's
    address, with its password (reviewed 2026-10-20). Both orders are refused:
    a key machine whose id is a registered password machine's address, and a
    password machine at a registered key machine's id or at an alias Raven
    keeps with no machine left behind it. Connecting refuses the pair again by
    name (``transport.ssh_env``), which is what still covers a pair written
    before this check existed.
    """
    from raven.ops.transport import managed_aliases

    if transport_of(row) == LOCAL:
        return ""
    conn_id = str(row.get("id") or "").strip()
    if not password_auth(row):
        for other in rows:
            if transport_of(other) == SSH and password_auth(other):
                host = str(other.get("host") or "").strip()
                if host.lower() == conn_id.lower():
                    return (
                        f"id {conn_id!r} is the address password machine {str(other.get('id') or '?')!r} is "
                        f"registered at ({host!r}): the ssh alias Raven writes for this id would take that "
                        "machine's logins, and its password, to this machine. Pick another id"
                    )
        return ""
    host = str(row.get("host") or "").strip()
    for other in rows:
        other_id = str(other.get("id") or "").strip()
        if transport_of(other) == SSH and not password_auth(other) and other_id.lower() == host.lower():
            return (
                f"machine {conn_id!r} would log in at {host!r}, the id of key machine {other_id!r}: the ssh alias "
                f"Raven keeps for {other_id!r} would take this machine's logins, and its password, to "
                f"{other_id!r}'s address. Register {conn_id!r} at its own address, or remove {other_id!r} first"
            )
    if host.lower() in managed_aliases():
        return (
            f"machine {conn_id!r} would log in at {host!r}, the ssh alias Raven keeps in ~/.ssh/config for a "
            f"machine {host.lower()!r} that is no longer registered: ssh would follow it to that address. Take "
            f"the '{ALIAS_MARK.format(conn_id=host.lower())}' block out of ~/.ssh/config, or register "
            f"{conn_id!r} at its own address"
        )
    return ""


def write(row: dict[str, Any], *, password: str | None = None) -> Path:
    """Append one machine, keeping whatever shape the file already had.

    Refuses rather than rewrites when the file holds entries the reader skips:
    serialising the survivors would erase rows the owner wrote by hand, and a
    recoverable typo is theirs to fix, not ours to delete on the way past. A
    file that does not exist yet is started here -- the first machine an agent
    adds for an owner who never ran the command is the common case. With
    ``password``, publish the successful registration's password with its row
    under the cleanup lock, even if another attempt removed the earlier file.
    """
    path = connections.store_path()
    with write_transaction(path):
        found = connections.read()
        if found.state == UNREADABLE:
            raise ValueError(f"{found.detail}\nFix or move that file before adding to it.")
        if found.detail:
            raise ValueError(
                f"{found.detail}\nAdding a machine here would rewrite the file without them. "
                "Give those entries an id (or remove them) first, then add this machine."
            )
        rows = list(found.rows)
        if any(str(r.get("id")).strip() == row["id"] for r in rows):
            raise ValueError(f"a machine with id {row['id']!r} is already listed in {path}")
        # Judged here too, under the registry's lock: the callers ask before
        # they reach the machine, and a registration can land in between.
        if collision := alias_collision(row, rows):
            raise ValueError(collision)
        rows.append(row)
        payload = json.dumps({"connections": rows}, indent=2, ensure_ascii=False) + "\n"
        if password is None:
            atomic_replace(path, payload)
        else:
            # A newer registration may have removed its own file while this
            # probe was pending. Publish this successful login's password and
            # row together, after checking duplicates and before cleanup can run.
            with write_transaction(password_path(row["id"])):
                identity = store_registration_password(row["id"], password)
                try:
                    atomic_replace(path, payload)
                finally:
                    forget_unowned_password(row["id"], password, identity=identity)
    return path


def set_fields(conn_id: str, fields: dict[str, Any]) -> Path:
    """Change ``fields`` on the one machine listed as ``conn_id``, keeping every other row as it was.

    For what is learned about a machine after it was added -- where its Raven
    node is kept (``node_dir``), set by ``raven ops connection install-node
    --dir``. Refuses on the same grounds as :func:`write`, and when the id is
    not listed exactly once.
    """
    path = connections.store_path()
    found = connections.read()
    if found.state == UNREADABLE:
        raise ValueError(f"{found.detail}\nFix or move that file before changing it.")
    if found.detail:
        raise ValueError(
            f"{found.detail}\nChanging a machine here would rewrite the file without them. "
            "Give those entries an id (or remove them) first."
        )
    rows = list(found.rows)
    hits = [r for r in rows if str(r.get("id") or "").strip() == conn_id]
    if len(hits) != 1:
        raise ValueError(f"machine {conn_id!r} is listed {len(hits)} times in {path}; it has to be listed once")
    hits[0].update(fields)
    path.write_text(json.dumps({"connections": rows}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


#: The registry field that remembers which Raven node this computer last put on,
#: or found on, the machine: its install name, ``<version>-<digest>``. Written by
#: ``install-node`` and by the Agent Hub's list when a link comes up, read by
#: the listing to say whether the machine has this Raven's node without
#: reaching it.
NODE_FIELD = "node_install"


def write_ssh_alias(row: dict[str, Any]) -> str | None:
    """Keep a ``Host <id>`` alias in ``~/.ssh/config`` for an ssh row.

    Transfers cannot ride the exec machine channel -- rsync and scp run their
    client HERE and only address the machine -- so without an alias every
    transfer carries the raw address, and the raw address then has to live in
    the model's context (measured 2026-09-03: a task statement shipped the
    ssh string because nothing else could address the machine). With the
    alias, ``rsync ... <id>:...`` resolves inside ssh's own config and the
    context carries only the registry name.

    One managed block per id, replaced in full on re-add; everything outside
    the markers is the owner's and is never touched. Returns the alias, or
    None when there is nothing to write: a local row, or a password row --
    an ssh config has nowhere to carry a password, so scp and rsync run from
    a terminal ask for it, and an agent reaches such a machine's files through
    Raven's own file tools. A failure is reported by the caller as a warning,
    never as a refusal: the alias is a convenience beside the registry, not
    part of it.
    """
    if transport_of(row) == LOCAL or password_auth(row):
        return None
    conn_id = str(row["id"])
    mark = ALIAS_MARK.format(conn_id=conn_id)
    block = "\n".join(
        [
            mark,
            f"Host {conn_id}",
            f"  HostName {row.get('host')}",
            f"  Port {int(row.get('port') or 22)}",
            f"  User {row.get('user') or 'root'}",
            f"  IdentityFile {row.get('key') or '~/.ssh/id_rsa'}",
            mark,
        ]
    )
    path = Path(os.path.expanduser("~/.ssh/config"))
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    if mark in text:
        head, tail = _around_managed_block(text, mark, conn_id)
        text = head.rstrip("\n") + ("\n" if head.strip() else "") + tail.lstrip("\n")
    text = (text.rstrip("\n") + "\n\n" if text.strip() else "") + block + "\n"
    path.write_text(text, encoding="utf-8")
    path.chmod(0o600)
    return conn_id


def _around_managed_block(text: str, mark: str, conn_id: str) -> tuple[str, str]:
    """``text`` before and after the managed block ``mark`` delimits; refuses a block missing a mark.

    The block is the text between two marks. With one mark only -- a block
    the owner edited or truncated by hand -- there is no block to replace or
    take out, and treating the rest of the file as the block would write the
    file back without it (measured 2026-10-16: an unrelated ``Host`` entry
    after a lone mark vanished on removal). Such a file is left as it is and
    the owner is told to take the block out by hand.
    """
    if text.count(mark) != 2:
        raise ValueError(
            f"the managed 'Host {conn_id}' block in ~/.ssh/config is incomplete (one of its two marker lines is "
            "missing); take it out by hand before adding or removing the machine"
        )
    head, _, rest = text.partition(mark)
    _, _, tail = rest.partition(mark)
    return head, tail


def remove(conn_id: str) -> Path:
    """Take every row listed as ``conn_id`` out, keeping every other row as it was.

    The write half of ``raven ops connection remove`` and ``machines.remove``.
    Refuses on the grounds :func:`write` refuses on -- a file that cannot be
    read, or one holding entries the reader skips -- and when the id is not
    listed: a removal that finds nothing is a wrong id, and the caller should
    hear that rather than see it succeed. Two rows under one id are both this
    machine's (the doctor names the pair as one defect), so both go.
    """
    path = connections.store_path()
    found = connections.read()
    if found.state == UNREADABLE:
        raise ValueError(f"{found.detail}\nFix or move that file before changing it.")
    if found.detail:
        raise ValueError(
            f"{found.detail}\nRemoving a machine here would rewrite the file without them. "
            "Give those entries an id (or remove them) first."
        )
    wanted = str(conn_id or "").strip()
    kept = [r for r in found.rows if str(r.get("id") or "").strip() != wanted]
    if len(kept) == len(found.rows):
        raise ValueError(f"machine {wanted!r} is not listed in {path}")
    path.write_text(json.dumps({"connections": kept}, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def remove_ssh_alias(conn_id: str) -> bool:
    """Drop the managed ``Host <id>`` block from ``~/.ssh/config``; whether one was there.

    Only the block between this module's own marks goes (:data:`ALIAS_MARK`);
    everything else in the file is the owner's and is left as it was, as
    :func:`write_ssh_alias` leaves it. A missing file, or one without the
    block, is nothing to do; a block with one of its two marks missing is
    left as it is and refused with ``ValueError``
    (:func:`_around_managed_block`). An ``OSError`` or that refusal is the
    caller's to report as a warning: the alias is a convenience beside the
    registry, not part of it.
    """
    conn_id = str(conn_id or "").strip()
    mark = ALIAS_MARK.format(conn_id=conn_id)
    path = Path(os.path.expanduser("~/.ssh/config"))
    if not path.exists():
        return False
    text = path.read_text(encoding="utf-8")
    if mark not in text:
        return False
    head, tail = _around_managed_block(text, mark, conn_id)
    parts = [part for part in (head.rstrip("\n"), tail.lstrip("\n").rstrip("\n")) if part.strip()]
    path.write_text("\n\n".join(parts) + "\n" if parts else "", encoding="utf-8")
    return True
