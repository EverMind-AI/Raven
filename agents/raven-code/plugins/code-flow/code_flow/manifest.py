"""The harness manifest: what the working directory shows since this session began.

Every field is machine-read -- git facts measured from the working directory
against the base commit the session ledger pinned at the session's first
turn -- and nothing comes from model prose. A failure to read a fact degrades
to ``status: unknown`` with a blocker that names it, never to a fabricated
ready. The report is filed under the turn's ``acp_meta`` observer stash and
reaches the host as the prompt response's ``_meta``.

Its scope is the workspace, not the session: nothing locks the directory,
and HEAD is shared by every session working in it, so ``baseCommit..HEAD``
is what the tree gained since this session's base -- by whoever wrote it.
The facts are attributed to the session (``attribution: session``) only
while no other session of this process has shared the directory during its
life; once one has (``attribution: shared``, ``sharedWith`` counting them)
the status says so and the report may not be read as this session's work.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from code_flow.gitfacts import run_git

MANIFEST_VERSION = 1
LISTING_CAP = 100

MANIFEST_FIELDS = (
    "manifestVersion",
    "sessionId",
    "scope",
    "attribution",
    "sharedWith",
    "workspaceKind",
    "repositoryRoot",
    "workdir",
    "branch",
    "baseCommit",
    "head",
    "commitsPastBase",
    "workingTree",
    "diffStat",
    "status",
    "blockers",
    "readyForIntegration",
)


def _skeleton(session_key: str, cwd: Path | str | None) -> dict[str, Any]:
    return {
        "manifestVersion": MANIFEST_VERSION,
        "sessionId": session_key,
        "scope": "workspace",
        "attribution": "session",
        "sharedWith": 0,
        "workspaceKind": "plain",
        "repositoryRoot": None,
        "workdir": None if cwd is None else str(cwd),
        "branch": None,
        "baseCommit": None,
        "head": None,
        "commitsPastBase": {"count": 0, "items": []},
        "workingTree": {"clean": False, "uncommittedCount": 0, "entries": []},
        "diffStat": "",
        "status": "unknown",
        "blockers": [],
        "readyForIntegration": False,
    }


def _ok(result) -> bool:
    return result is not None and result.returncode == 0


def build_manifest(
    session_key: str, cwd: Path | str | None, base_commit: str | None, *, shared_with: int = 0
) -> dict[str, Any]:
    """The v1 report for the working directory a session works in.

    ``shared_with`` is how many other sessions of this process have shared the
    directory during this session's life (the ledger's peers). Any number
    above zero turns the attribution to ``shared``: the git facts are still
    reported, but the status is ``shared_workspace`` and the report is never
    ready for integration, because commits past base may be another
    session's.
    """
    report = _skeleton(session_key, cwd)
    if shared_with:
        report["attribution"] = "shared"
        report["sharedWith"] = shared_with
    if cwd is None:
        report["blockers"] = ["no working directory bound to the session"]
        return report

    root = run_git(cwd, "rev-parse", "--show-toplevel")
    if root is None:
        report["blockers"] = ["git could not run"]
        return report
    if root.returncode != 0:
        report["blockers"] = ["not a git repository"]
        return report

    report["workspaceKind"] = "repository"
    report["repositoryRoot"] = root.stdout.strip()
    report["baseCommit"] = base_commit

    head = run_git(cwd, "rev-parse", "HEAD")
    branch = run_git(cwd, "rev-parse", "--abbrev-ref", "HEAD")
    status = run_git(cwd, "status", "--porcelain")
    commits = run_git(cwd, "log", "--format=%H%x1f%s", f"{base_commit}..HEAD") if base_commit else None
    stat = run_git(cwd, "diff", "--stat", base_commit) if base_commit else None

    # readyForIntegration may only rest on a complete picture: every fact the
    # report carries has to have answered, or an integration step acting on a
    # half-known report is the misread this field exists to prevent.
    failures: list[str] = []
    if not base_commit:
        failures.append("base commit")
    if not _ok(head):
        failures.append("head")
    if not _ok(branch) or not branch.stdout.strip():
        failures.append("branch")
    if not _ok(status):
        failures.append("status")
    if base_commit and not _ok(commits):
        failures.append("commits past base")
    if base_commit and not _ok(stat):
        failures.append("diff stat")
    facts_known = not failures

    if _ok(head):
        report["head"] = head.stdout.strip()
    if _ok(branch) and branch.stdout.strip():
        report["branch"] = branch.stdout.strip()

    entries = [line for line in status.stdout.splitlines() if line.strip()] if _ok(status) else []
    items: list[dict[str, str]] = []
    if _ok(commits):
        for line in commits.stdout.splitlines():
            if not line.strip():
                continue
            sha, _, subject = line.partition("\x1f")
            items.append({"sha": sha.strip(), "subject": subject})
    report["commitsPastBase"] = {"count": len(items), "items": items[:LISTING_CAP]}
    report["workingTree"] = {
        "clean": facts_known and not entries,
        "uncommittedCount": len(entries),
        "entries": entries[:LISTING_CAP],
    }
    if _ok(stat) and stat.stdout.strip():
        report["diffStat"] = stat.stdout.strip().splitlines()[-1].strip()
    elif facts_known:
        report["diffStat"] = "no diff vs base"

    if not facts_known:
        report["status"] = "unknown"
        report["blockers"] = [f"git state could not be read: {', '.join(failures)}"]
    elif shared_with:
        plural = "" if shared_with == 1 else "s"
        report["status"] = "shared_workspace"
        report["blockers"] = [
            f"the directory was shared with {shared_with} other Raven-Code session{plural} since this "
            "session's base; commits past base cannot be attributed to this session"
        ] + (["uncommitted changes must be committed"] if entries else [])
    elif entries:
        report["status"] = "needs_commit"
        report["blockers"] = ["uncommitted changes must be committed"]
    elif not items:
        report["status"] = "no_changes"
        report["blockers"] = ["no commits past base"]
    else:
        report["status"] = "ready_for_integration"
        report["readyForIntegration"] = True
    return report


__all__ = ["LISTING_CAP", "MANIFEST_FIELDS", "MANIFEST_VERSION", "build_manifest"]
