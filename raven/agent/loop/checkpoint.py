"""Per-turn shadow-git checkpoint of the workspace (Bug2 safety net).

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
from pathlib import Path

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


# Upper bound on any single git subprocess. Without this, an NFS lock, a
# held ``.git/index.lock``, or a full disk could hang ``communicate()``
# indefinitely and brick the agent loop — violating this service's
# "never break a turn" contract. Generous enough that normal cold-init
# fits comfortably; tight enough to detect a real hang within one turn.
_GIT_TIMEOUT_SECONDS = 30.0


class CheckpointService:
    """Shadow-git working-tree snapshots, one commit per turn."""

    def __init__(self, workspace: Path, shadow_dir: str = ".raven/shadow.git") -> None:
        self._workspace = Path(workspace).expanduser().resolve()
        candidate = (self._workspace / shadow_dir).resolve()
        # Containment is a load-bearing invariant: per-workspace recovery
        # isolation (Bug2) breaks if the shadow git lands outside its
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

    async def _git(self, *args: str) -> tuple[int, str, str]:
        """Run a git command against the shadow repo. Returns (rc, out, err).

        ``core.quotePath=false`` keeps non-ASCII paths (CJK/Japanese/emoji) as
        real UTF-8 in output instead of git's default octal-escaped form —
        without this, ``edited_files`` would land in the recovery prompt as
        ``"\\346\\265\\213"`` gibberish.
        """
        cmd = (
            "git",
            f"--git-dir={self._git_dir}",
            f"--work-tree={self._workspace}",
            "-c",
            "core.quotePath=false",
            *args,
        )
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._workspace),
        )
        try:
            out, err = await asyncio.wait_for(
                proc.communicate(),
                timeout=_GIT_TIMEOUT_SECONDS,
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
                _GIT_TIMEOUT_SECONDS,
                " ".join(args[:2]),
            )
            return -1, "", "timeout"
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")

    async def _ensure_init(self) -> bool:
        """Lazily initialize the shadow repo. Idempotent; returns readiness."""
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


__all__ = ["CheckpointService"]
