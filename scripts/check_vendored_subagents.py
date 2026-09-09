"""Fail when a change to a vendored subagent checkout is not deliberate.

The trees under ``subagents/*/`` are other agents' own source, shipped as source
(see ``subagents/README.md``). They are near-copies of this repo, so a merge from
upstream can land on them by accident: git's rename detection matches an upstream
path this trunk has renamed away to the byte-identical copy inside a fork, and
applies the delta there. That happened in the v0.1.13 sync -- twelve files across
three forks, each left half-upgraded and broken at import, and invisible because
nothing in CI reads these trees.

The guard is a recorded tree hash per fork. Any change to any tracked file under
a fork moves its hash, so an accidental landing fails here; a deliberate upgrade
updates ``subagents/TREE_HASHES`` in the same commit and says so in its message.

Run with ``--update`` to rewrite the manifest after a deliberate upgrade.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
VENDOR_ROOT = REPO / "subagents"
MANIFEST = VENDOR_ROOT / "TREE_HASHES"


def _forks() -> list[str]:
    return sorted(p.name for p in VENDOR_ROOT.iterdir() if p.is_dir() and not p.name.startswith("."))


def _tracked(fork: str) -> list[str]:
    """Tracked paths only: a fork's .venv and node_modules are not its source."""
    git = shutil.which("git")
    if git is None:  # pragma: no cover - a checkout without git cannot be checked
        raise RuntimeError("git is required to list the tracked files of a vendored checkout")
    out = subprocess.run(
        [git, "-C", str(REPO), "ls-files", "-z", f"subagents/{fork}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def _hash(fork: str) -> str:
    h = hashlib.sha256()
    for rel in _tracked(fork):
        # The path goes in as well as the content: a file moved between two
        # otherwise identical trees has to change the hash.
        h.update(rel.encode())
        h.update(b"\0")
        h.update(hashlib.sha256((REPO / rel).read_bytes()).digest())
    return h.hexdigest()


def _current() -> dict[str, str]:
    return {fork: _hash(fork) for fork in _forks()}


def _recorded() -> dict[str, str]:
    if not MANIFEST.exists():
        return {}
    recorded = {}
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        fork, _, digest = line.partition(" ")
        recorded[fork] = digest.strip()
    return recorded


def main(argv: list[str]) -> int:
    current = _current()
    if "--update" in argv:
        body = [
            "# One sha256 per vendored subagent checkout, over its tracked paths",
            "# and their contents. Written by scripts/check_vendored_subagents.py",
            "# --update, and only for a deliberate upgrade of that fork.",
        ]
        body += [f"{fork} {digest}" for fork, digest in current.items()]
        MANIFEST.write_text("\n".join(body) + "\n", encoding="utf-8")
        print(f"wrote {MANIFEST.relative_to(REPO)} for {len(current)} fork(s)")
        return 0

    recorded = _recorded()
    if not recorded:
        print(f"{MANIFEST.relative_to(REPO)} is missing; run with --update to record it.", file=sys.stderr)
        return 1

    drifted = [f for f in current if recorded.get(f) != current[f]]
    gone = [f for f in recorded if f not in current]
    if not drifted and not gone:
        return 0

    print("Vendored subagent checkouts changed:", file=sys.stderr)
    for fork in drifted:
        print(f"  {fork}: recorded {recorded.get(fork, '(none)')[:12]} now {current[fork][:12]}", file=sys.stderr)
    for fork in gone:
        print(f"  {fork}: recorded but no longer present", file=sys.stderr)
    print(
        "\nThese trees are other agents' own source. A sync from upstream can land on\n"
        "them by accident -- rename detection matches a path this trunk renamed away to\n"
        "the byte-identical copy inside a fork. If that is what happened, revert it:\n"
        "  git checkout origin/main -- subagents/\n"
        "If this is a deliberate upgrade of that fork, run its own suite in its own venv,\n"
        "then record it in the same commit:\n"
        "  python3 scripts/check_vendored_subagents.py --update",
        file=sys.stderr,
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
