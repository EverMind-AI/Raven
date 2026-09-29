"""Per-turn shadow-git checkpoint of the workspace (a recovery safety net).

Commits the workspace to an out-of-band git repo (separate ``--git-dir``,
work-tree pointed at the real workspace) at the end of each turn. The user's
own ``.git`` is never touched. A truncated multi-file edit therefore leaves a
recoverable snapshot, and the interrupted turn's changed files can be listed
for the next turn's recovery prompt.

Scope (documented limits):
- Only filesystem state is snapshotted — not conversation state.
- Changes made via shell tools (``rm``/``mv``/``sed -i``) are captured by the
  next ``add -A`` but are not attributable to a specific tool call. This is an
  *undo stack for the working tree*, not full crash recovery.
- Granularity is per-turn (one commit per turn), matching Claude Code/Cursor.

Safety layers (defense in depth against snapshotting things the user doesn't
want stored):
1. ``info/exclude`` ships an expanded default blacklist covering common build
   artifacts, virtualenvs, IDE state, OS junk, and likely-credential paths.
2. The work-tree's own ``.gitignore`` files are honored automatically by git
   (standard ``add -A`` semantics) — so anything the user marked private in
   their own repo stays out of the shadow as well.
3. ``gc.auto`` is configured so a periodic ``git gc --auto`` keeps long-lived
   sessions from accumulating loose objects forever.

Every git invocation is best-effort: failures are logged and degrade to a
no-op (return ``None``) so the checkpoint layer can never break a turn.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import contextlib
import os
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Collection

from loguru import logger

# Committer identity baked into the shadow repo so commits don't depend on the
# user's global git config (and never touch it).
_GIT_IDENT = (
    "-c",
    "user.name=Raven",
    "-c",
    "user.email=checkpoint@raven.local",
    "-c",
    "commit.gpgsign=false",
)


# Ephemeral / risky patterns baked into the shadow ``info/exclude``. Defense
# in depth: even when the workspace has no ``.gitignore``, these never end up
# in a snapshot. Categories (kept aligned with what real projects ignore):
#
# - Self / Python caches: avoid recursion into the shadow itself + standard
#   Python build noise.
# - Build / package artifacts: typical multi-language output dirs that can be
#   GB-scale and have zero recovery value.
# - Virtual environments: same — large and re-creatable from lockfiles.
# - Credentials & dotenv: high-impact leak vectors. The user's own
#   ``.gitignore`` usually covers these; we still exclude in case it doesn't
#   (e.g. a fresh workspace that was never git-init'd).
# - Logs / OS junk / IDE state: not secrets, just noise that bloats the repo.
_DEFAULT_EXCLUDES = """\
# Raven shadow-git default excludes (see checkpoint.py).
# Layered on top of any .gitignore files in the work-tree.

# Self + Python caches
.raven/
__pycache__/
*.pyc
*.pyo

# Build / package artifacts
dist/
build/
target/
*.egg-info/
.eggs/
node_modules/
.next/
.nuxt/
out/

# Virtualenvs
# (``env/`` deliberately omitted — too easily collides with a legitimate
# project source dir; users whose env IS a virtualenv typically have it
# in their own .gitignore, which S4-A honors automatically.)
venv/
.venv/
.tox/

# Credentials & dotenv (defense in depth — usually in user's .gitignore too)
.env
.env.*
*.key
*.pem
*.crt
*.p12
.aws/credentials
secrets.yaml
secrets.yml
# Private keys are conventionally extensionless, so the patterns above miss
# them entirely — ``*.key`` / ``*.pem`` never match ``id_ed25519``.
.ssh/
.gnupg/
id_rsa
id_dsa
id_ecdsa
id_ed25519
*.ppk

# Logs
*.log
logs/

# OS junk
.DS_Store
Thumbs.db

# IDE state
.idea/
.vscode/
"""


# How often (in successful commits) to fire ``git gc --auto`` against the
# shadow repo. ``--auto`` lets git itself decide whether GC is warranted based
# on its internal heuristics (``gc.auto`` threshold etc.); we just provide the
# heartbeat. 0 disables the periodic invocation entirely.
_GC_EVERY_N_COMMITS = 50


# Upper bound on a git subprocess the turn waits behind (a staging's ``git add``
# runs on a thread nothing waits behind and has its own, below). Without this,
# an NFS lock, a held ``.git/index.lock``, or a full disk could hang
# ``communicate()`` indefinitely and brick the agent loop — violating this
# service's "never break a turn" contract. Generous enough that normal
# cold-init fits comfortably; tight enough to detect a real hang within one turn.
_GIT_TIMEOUT_SECONDS = 30.0

# How long a command waits for the tree staged in front of it before the call
# fails. Staging a tree git has seen before is a stat walk (about 0.1s on a repo
# of a few thousand files, 0.3s on seventeen thousand); the first one in a
# directory hashes and writes every file and was measured at 14s on a 390 MB
# tree, and past 30s while a turn's commit hashed the same tree beside it. This
# is only reached by a filesystem that has stopped answering.
_STAGE_WAIT_SECONDS = 120.0

# The ceiling on a staging's ``git add``. Not ``_GIT_TIMEOUT_SECONDS``: that one
# bounds a call something waits behind, and a staging runs on a thread nothing
# waits behind past ``_STAGE_WAIT_SECONDS``. Killed at 30s, the first staging of
# a large directory (tens of seconds while a turn's commit hashes the same tree)
# was started over and killed again, and every command was held back for good.
_STAGE_ADD_TIMEOUT_SECONDS = 600.0

# A command's staging index left behind by a process that is gone. Age rather
# than a liveness probe: asking whether a pid is alive terminates it on Windows.
_STAGE_INDEX_STALE_SECONDS = 7 * 24 * 3600


# The staging running on each index, across every service in this process: two
# services for one directory share the index file, so they share its one run.
_STAGING: dict[Path, "concurrent.futures.Future[str | None]"] = {}
_STAGE_LOCKS: dict[Path, threading.Lock] = {}


class StagingTimeoutError(TimeoutError):
    """The tree a command is to be measured against is still being staged.

    A :class:`TimeoutError`, which is what ``ExecTool`` catches: the tool holds
    this repo through a protocol and does not import the loop shell."""


async def _within(staging: "concurrent.futures.Future[str | None]", deadline: float) -> bool:
    """Whether ``staging`` finished by ``deadline``, waited for without owning it.

    Detached rather than awaited: a staging still running when the loop closes
    must not hold the close up, and a cancelled waiter is what lets its late
    result be dropped once the loop is gone.
    """
    waiter = asyncio.wrap_future(staging)
    try:
        done, _ = await asyncio.wait({waiter}, timeout=max(0.0, deadline - time.monotonic()))
    finally:
        if not waiter.done():
            waiter.cancel()
    return bool(done)


class CheckpointService:
    """Shadow-git working-tree snapshots, one commit per turn."""

    def __init__(self, workspace: Path, shadow_dir: str = ".raven/shadow.git") -> None:
        self._workspace = Path(workspace).expanduser().resolve()
        # The working directory is the launch directory now, so a user can
        # aim this at their home directory just by running `raven tui` from
        # it. Snapshotting a whole home is both an every-turn `add -A` over
        # everything the user owns and a copy of their secrets into a repo
        # that keeps history; the excludes above cannot be made complete
        # enough to make that safe, and the recovery value at that scope is
        # nil -- an interrupted turn edits a project, not a home. Refuse, and
        # let the caller disable the net rather than silently take the copy.
        home = Path.home().expanduser().resolve()
        if self._workspace == home or self._workspace in home.parents:
            raise ValueError(
                f"refusing to checkpoint {self._workspace} -- it is your home "
                "directory or an ancestor of it. Run raven from a project "
                "directory, or set runtime.checkpoint.policy=never."
            )
        candidate = (self._workspace / shadow_dir).resolve()
        # Containment is a load-bearing invariant: per-workspace recovery
        # isolation breaks if the shadow git lands outside its
        # workspace, since a second AgentLoop on a different workspace
        # configured with a similarly-escaping path could share the repo
        # and cross-contaminate ``edited_files``. ``..`` / absolute paths /
        # ``""`` / ``"."`` all fall into this trap; reject them with a
        # clear error rather than letting the resolved path drift silently.
        if candidate == self._workspace or not candidate.is_relative_to(self._workspace):
            raise ValueError(
                f"shadow_dir={shadow_dir!r} must resolve to a path strictly "
                f"under the workspace ({self._workspace}); got {candidate}"
            )
        self._git_dir = candidate
        self._shadow_rel = shadow_dir
        self._ready = False
        self._commit_count = 0
        self._stage_pruned = False
        self._warmed = False
        self._initializing: concurrent.futures.Future[bool] | None = None

    def covers(self, path: Path | str) -> bool:
        """Whether ``path`` lies in the work-tree this repo snapshots."""
        try:
            return Path(path).expanduser().resolve().is_relative_to(self._workspace)
        except OSError:
            return False

    async def _git(self, *args: str) -> tuple[int, str, str]:
        """Run a git command against the shadow repo. Returns (rc, out, err).

        ``core.quotePath=false`` keeps non-ASCII paths (CJK/Japanese/emoji) as
        real UTF-8 in output instead of git's default octal-escaped form —
        without this, ``edited_files`` would land in the recovery prompt as
        ``"\\346\\265\\213"`` gibberish.
        """
        rc, out, err = await self._run(args)
        return rc, out.decode(errors="replace"), err.decode(errors="replace")

    def _command(self, args: tuple[str, ...], index: Path | None) -> tuple[tuple[str, ...], dict[str, str] | None]:
        cmd = (
            "git",
            f"--git-dir={self._git_dir}",
            f"--work-tree={self._workspace}",
            "-c",
            "core.quotePath=false",
            *args,
        )
        return cmd, None if index is None else {**os.environ, "GIT_INDEX_FILE": str(index)}

    async def _run(
        self,
        args: tuple[str, ...],
        *,
        index: Path | None = None,
        stdin: bytes | None = None,
        timeout: float | None = None,
    ) -> tuple[int, bytes, bytes]:
        """``_git`` with the raw bytes, and optionally against another index."""
        if timeout is None:
            timeout = _GIT_TIMEOUT_SECONDS
        cmd, env = self._command(args, index)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._workspace),
            env=env,
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate() if stdin is None else proc.communicate(stdin),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            # NFS / index-lock / disk-full pathology: don't leak a zombie,
            # don't let the turn hang. Synthesize a non-zero rc so the
            # caller's degrade-on-failure path engages.
            try:
                proc.kill()
                await proc.wait()
            except ProcessLookupError:
                pass
            logger.debug(
                "checkpoint git timed out after {}s: {}",
                timeout,
                " ".join(args[:2]),
            )
            return -1, b"", b"timeout"
        return proc.returncode or 0, out, err

    async def _ensure_init(self) -> bool:
        """Lazily initialize the shadow repo. Idempotent; returns readiness.

        A warm-up runs this same setup on its own thread (:meth:`warm`), and two
        at once fail on the repo's config lock -- which costs the turn its
        commit when the turn ends before the warm-up's setup does. So a setup
        already running is waited for, and repeated only if it did not succeed.
        """
        if self._ready:
            return True
        running = self._initializing
        if running is not None and not running.done():
            waiter = asyncio.wrap_future(running)
            try:
                await waiter
            finally:
                if not waiter.done():
                    waiter.cancel()
            if self._ready:
                return True
        return await self._init_repo()

    async def _init_repo(self) -> bool:
        if self._ready:
            return True
        try:
            if not (self._git_dir / "HEAD").exists():
                self._git_dir.parent.mkdir(parents=True, exist_ok=True)
                rc, _, err = await self._git("init")
                if rc != 0:
                    logger.debug("checkpoint init failed: {}", err.strip())
                    return False
                # Drop a discoverability hint next to the shadow git so a user
                # who notices ``.raven/`` can identify it without grepping
                # the codebase. Best-effort — write failure here is fine.
                try:
                    notice = self._git_dir.parent / "NOTICE.txt"
                    notice.write_text(
                        "This directory is created by Raven's runtime "
                        "checkpoint feature (a per-turn safety net). It is "
                        "an out-of-band shadow git repo — your own .git is "
                        "untouched.\n\n"
                        "Safe to delete; will be recreated on next agent run. "
                        'Disable via `runtime.checkpoint.policy = "never"` '
                        "in your Raven config (typically "
                        "~/.raven/config.json, or whichever file you "
                        "passed via --config).\n",
                        encoding="utf-8",
                    )
                except OSError:
                    pass
            # Layered ignore: shadow-specific defaults + the user's own
            # .gitignore (auto-walked by git in the work-tree). Together they
            # keep build artifacts, ephemeral state, and user-marked-private
            # files (.env, secrets.yml) out of any snapshot.
            exclude = self._git_dir / "info" / "exclude"
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text(_DEFAULT_EXCLUDES, encoding="utf-8")
            # gc.auto: git's own threshold for "objects/refs are getting
            # crufty, fire a real GC". Setting it once at init lets every
            # subsequent ``git gc --auto`` consult the same threshold without
            # us passing ``-c`` on each call.
            await self._git("config", "gc.auto", "256")
            # gc.autoDetach=false: when git decides to auto-gc, run gc in the
            # foreground instead of detaching a background daemon. Detached
            # gc races with workspace cleanup (test tempdirs, agent shutdown)
            # and leaves "Directory not empty" errors when the rmtree hits a
            # gc still writing into ``objects/``. Synchronous gc is governed
            # by our _GIT_TIMEOUT_SECONDS so it can't hang the turn either.
            await self._git("config", "gc.autoDetach", "false")
            self._ready = True
            return True
        except OSError as exc:
            logger.debug("checkpoint init error: {}", exc)
            return False

    async def commit_turn(self, label: str) -> tuple[str | None, list[str]]:
        """Snapshot the current worktree as one commit.

        Returns ``(checkpoint_id, changed_files)``. When nothing changed
        since the last turn, or on any git failure, returns ``(None, [])``.
        """
        if not await self._ensure_init():
            return None, []
        try:
            rc, _, err = await self._git("add", "-A")
            if rc != 0:
                logger.debug("checkpoint add failed: {}", err.strip())
                return None, []
            # Files staged this turn = this turn's changes. Capture before commit.
            rc, out, _ = await self._git("diff", "--cached", "--name-only")
            changed = [ln for ln in out.splitlines() if ln.strip()]
            if not changed:
                return None, []  # nothing to snapshot
            rc, _, err = await self._git(*_GIT_IDENT, "commit", "-m", label)
            if rc != 0:
                logger.debug("checkpoint commit failed: {}", err.strip())
                return None, []
            rc, out, _ = await self._git("rev-parse", "--short", "HEAD")
            cid = out.strip() or None
            self._commit_count += 1
            await self._maybe_gc()
            return cid, changed
        except OSError as exc:
            logger.debug("checkpoint commit error: {}", exc)
            return None, []

    async def stage_tree(self) -> str | None:
        """The work-tree as it stands, as a tree id in the shadow repo.

        Taken just before a command runs, so what the command changed can be
        diffed afterwards against the contents it replaced -- a command names no
        file it writes, and by the time it returns the old text is gone. Staged
        into an index of its own: the per-turn commit reads the shared index as
        "what the last turn left", and moving it here would make that commit miss
        everything this turn did before the command. Nothing is committed; the
        tree is only read back within the same call.

        Staged afresh for every command, inside the command's own call, so the
        tree is the directory as it stood the moment before the command ran and
        nothing about what happened since an earlier staging has to be known. A
        staging already running (a warm-up, or another session's command in the
        same directory) is waited for first: they share one index, and a
        warm-up runs the repo setup on its thread too, where two ``git config``
        writes at once fail on the config lock.

        ``None`` when the tree cannot be staged at all (git failed), which costs
        the call its diff and nothing else. :class:`StagingTimeoutError` when it
        has not finished after :data:`_STAGE_WAIT_SECONDS`: the caller fails the
        command rather than run it unmeasured. The staging is left running
        rather than killed, because what it has hashed is what makes the next
        one fast.
        """
        index = self._stage_path()
        deadline = time.monotonic() + _STAGE_WAIT_SECONDS
        earlier = _STAGING.get(index)
        if earlier is not None and not earlier.done() and not await _within(earlier, deadline):
            raise StagingTimeoutError
        if not await self._ensure_init():
            return None
        self._prepare_index(index)
        staging = self._start_stage(index)
        if not await _within(staging, deadline):
            raise StagingTimeoutError
        return staging.result()

    async def warm(self) -> None:
        """Start a staging in the background, so the first command finds the index warm.

        The first staging in a directory the shadow repo has never indexed hashes
        every file in it, which on a large tree is seconds. Started when a session
        opens on the directory, it runs while the user types and the model writes
        its first reply instead of inside the first command. Returns at once: the
        repo's own setup (``git init``, its config, seeding the index) runs on the
        staging's thread too, so nobody waits for any of it. Once per service,
        which is once per directory per process: after that every command's own
        staging keeps the index warm.
        """
        if self._warmed:
            return
        self._warmed = True
        index = self._stage_path()
        running = _STAGING.get(index)
        if running is not None and not running.done():
            return
        staging = self._register_stage(index)
        if not self._ready:
            self._initializing = concurrent.futures.Future()
        threading.Thread(target=self._warm_up, args=(index, staging), name="raven-stage", daemon=True).start()

    def _warm_up(self, index: Path, staging: "concurrent.futures.Future[str | None]") -> None:
        # A loop of the thread's own for the repo setup: the turn's loop is not
        # to wait on it, and one that closes while the setup is still starting a
        # git process would hold its close up.
        initializing = self._initializing
        try:
            ready = asyncio.run(self._init_repo())
        except Exception as exc:  # noqa: BLE001 -- a warm-up never breaks anything
            logger.debug("checkpoint warm-up init error: {}", exc)
            ready = False
        if initializing is not None:
            initializing.set_result(ready)
        if not ready:
            staging.set_result(None)
            return
        self._prepare_index(index)
        self._stage(index, staging)

    def _start_stage(self, index: Path) -> "concurrent.futures.Future[str | None]":
        staging = self._register_stage(index)
        threading.Thread(target=self._stage, args=(index, staging), name="raven-stage", daemon=True).start()
        return staging

    @staticmethod
    def _register_stage(index: Path) -> "concurrent.futures.Future[str | None]":
        staging: concurrent.futures.Future[str | None] = concurrent.futures.Future()
        # Running from the start, so a waiter that gives up and cancels its
        # wrapper cannot cancel the staging itself: the warm-up sets its repo up
        # before it stages, and a staging cancelled in that window lost its
        # result and raised on its thread when it finished.
        staging.set_running_or_notify_cancel()
        _STAGING[index] = staging
        return staging

    def _stage(self, index: Path, result: "concurrent.futures.Future[str | None]") -> None:
        """``git add -A`` then ``git write-tree`` on the staging index, on a thread of its own.

        A thread and blocking runs rather than asyncio subprocesses: a staging
        outlives the call that started it whenever it is slow, and an asyncio
        subprocess still starting when its loop closes holds the close up for
        good (CPython 3.12, macOS). Both steps under one lock per index, because
        ``write-tree`` writes the index back too and a second staging's ``add``
        beside it fails on the index lock.
        """
        with _STAGE_LOCKS.setdefault(index, threading.Lock()):
            if self._stage_step(("add", "-A"), index, timeout=_STAGE_ADD_TIMEOUT_SECONDS) is None:
                result.set_result(None)
                return
            tree = self._stage_step(("write-tree",), index)
        result.set_result(tree or None)

    def _stage_step(self, args: tuple[str, ...], index: Path, *, timeout: float | None = None) -> str | None:
        """One git step of a staging: its output, or ``None`` when it failed."""
        if timeout is None:
            timeout = _GIT_TIMEOUT_SECONDS
        cmd, env = self._command(args, index)
        try:
            done = subprocess.run(
                cmd,
                cwd=str(self._workspace),
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            logger.debug("checkpoint stage timed out after {}s: {}", timeout, " ".join(args))
            # A killed git leaves the index lock behind, and every later stage
            # would then fail on it. The index is this process's own and the
            # stage lock is held, so no live git owns the lock.
            with contextlib.suppress(OSError):
                Path(f"{index}.lock").unlink()
            return None
        except OSError as exc:
            logger.debug("checkpoint stage error: {}", exc)
            return None
        if done.returncode != 0:
            logger.debug("checkpoint stage failed: {}", done.stderr.decode(errors="replace").strip())
            return None
        return done.stdout.decode(errors="replace").strip()

    async def read_blobs(self, tree: str, paths: Collection[str], *, max_bytes: int) -> dict[str, bytes]:
        """What each of ``paths`` held in ``tree``, keyed by the path as given.

        A path is left out when the tree never had it (new, ignored, excluded),
        when it lies outside the work-tree, or when it held more than
        ``max_bytes`` -- a caller reads a missing key as "not known", never as
        "empty".
        """
        rel_of: dict[str, str] = {}
        for path in paths:
            try:
                rel = Path(path).resolve().relative_to(self._workspace).as_posix()
            except (OSError, ValueError):
                continue
            if "\n" not in rel:
                rel_of[path] = rel
        if not rel_of:
            return {}
        wanted = list(rel_of.items())
        # A name that is not UTF-8 reaches Python surrogate-escaped; git wants
        # the bytes the filesystem holds.
        query = "".join(f"{tree}:{rel}\n" for _, rel in wanted).encode(errors="surrogateescape")
        rc, out, _ = await self._run(("cat-file", "--batch-check"), stdin=query)
        lines = out.decode(errors="replace").splitlines()
        if rc != 0 or len(lines) != len(wanted):
            return {}
        small: list[tuple[str, str]] = []
        for (path, _), line in zip(wanted, lines):
            parts = line.split()
            if len(parts) == 3 and parts[1] == "blob" and parts[2].isdigit() and int(parts[2]) <= max_bytes:
                small.append((path, parts[0]))
        if not small:
            return {}
        rc, out, _ = await self._run(("cat-file", "--batch"), stdin="".join(f"{sha}\n" for _, sha in small).encode())
        if rc != 0:
            return {}
        found: dict[str, bytes] = {}
        at = 0
        for path, _ in small:
            end = out.find(b"\n", at)
            if end < 0:
                break
            header = out[at:end].split()
            if len(header) != 3 or not header[2].isdigit():
                break
            size = int(header[2])
            found[path] = out[end + 1 : end + 1 + size]
            at = end + 1 + size + 1
        return found

    async def trackable(self, paths: Collection[str]) -> set[str]:
        """The subset of ``paths`` this repo would store, keyed by the path as given.

        By the repo's own rules -- the default excludes (credentials, ``.env``,
        keys) and the work-tree's ``.gitignore`` files -- judged on the rules
        alone, not on what an index happens to hold. The boundary a command's
        created files must respect before their contents go anywhere: the
        checkpoint keeps an ignored file out of storage, so its text must not
        reach a diff either. A path outside the work-tree is never trackable,
        and when git cannot answer nothing is.
        """
        if not await self._ensure_init():
            return set()
        rel_of: dict[str, str] = {}
        for path in paths:
            try:
                rel_of[path] = Path(path).resolve().relative_to(self._workspace).as_posix()
            except (OSError, ValueError):
                continue
        if not rel_of:
            return set()
        query = "".join(f"{rel}\0" for rel in rel_of.values()).encode(errors="surrogateescape")
        rc, out, _ = await self._run(("check-ignore", "--no-index", "-z", "--stdin"), stdin=query)
        # 0: some are ignored, 1: none are; anything else is git failing to say.
        if rc not in (0, 1):
            return set()
        ignored = set(out.decode(errors="surrogateescape").split("\0")) - {""}
        return {path for path, rel in rel_of.items() if rel not in ignored}

    def _stage_path(self) -> Path:
        """This process's staging index.

        Per process because two processes can run commands in one directory and
        an index is a single file. Seeded from the shared one (``_prepare_index``)
        so a first staging finds git's stat cache already warm from the last
        turn's commit instead of hashing the whole tree again.
        """
        return self._git_dir / f"exec-{os.getpid()}.index"

    def _prepare_index(self, index: Path) -> None:
        if not self._stage_pruned:
            self._stage_pruned = True
            now = time.time()
            for other in self._git_dir.glob("exec-*.index"):
                with contextlib.suppress(OSError):
                    if now - other.stat().st_mtime > _STAGE_INDEX_STALE_SECONDS:
                        other.unlink()
        shared = self._git_dir / "index"
        if not index.exists() and shared.exists():
            with contextlib.suppress(OSError):
                shutil.copyfile(shared, index)

    async def _maybe_gc(self) -> None:
        """Periodic ``git gc --auto`` so long-lived sessions don't accumulate
        loose objects forever. ``--auto`` is a no-op below ``gc.auto`` (256
        loose objects by default), so the cost in steady state is one cheap
        rev-list count, not a real repack.

        ``_commit_count`` is per-instance and resets when CheckpointService
        is re-constructed (e.g. fresh AgentLoop start). The 50-commit
        heartbeat is therefore a hint, not a guarantee — git's own
        ``gc.auto=256`` threshold (set at init) is the load-bearing safety
        net that catches accumulated loose objects across process restarts.
        """
        if _GC_EVERY_N_COMMITS <= 0:
            return
        if self._commit_count % _GC_EVERY_N_COMMITS != 0:
            return
        rc, _, err = await self._git("gc", "--auto")
        if rc != 0:
            logger.debug("checkpoint gc failed: {}", err.strip())


__all__ = ["CheckpointService", "StagingTimeoutError"]
