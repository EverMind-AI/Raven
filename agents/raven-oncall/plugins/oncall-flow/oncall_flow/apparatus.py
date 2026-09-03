"""Fingerprint a campaign's apparatus, and say what moved.

The apparatus is everything a run is measured against that the run did not
produce: the campaign's declaration (``meta.json`` -- objective, budget, host,
which case) and the case the job runs out of.

**This module decides nothing about whether an edit was allowed.** Deciding that
needs domain knowledge -- fixing a wrong viscosity is the job, rewriting the
initial water column is a different problem wearing the same clothes -- and a
check that carries domain knowledge has to be rewritten for every new domain.
What it does instead is make every edit visible, whatever tool made it. That
matters because the tools that record their own edits (``ops_edit_case_dict``)
can be bypassed: an arm reaching for ``exec`` and ``sed`` leaves the audit trail
empty while the file changes underneath it.

The one place a judgement IS made is the split between the two halves, and it is
a role boundary rather than a domain fact:

  - the **case** belongs to the agent. Edits are recorded, never blocked.
  - **meta.json** is the apparatus' own declaration. An agent editing it is out of
    role in any domain, and the damage is not confined to scoring -- change
    ``remote_dir`` and the job runs somewhere nobody is watching.

Both readings are the same operation, so both live here.
"""

from __future__ import annotations

import hashlib
import json
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

BASELINE_FILE = "apparatus_baseline.json"

# Depth-limited so a case that has already been run -- time directories, a
# processor* decomposition -- does not turn one status call into tens of
# thousands of hashes. The staged case is authored, not generated, so its
# dictionaries all live within a few levels.
_MAX_DEPTH = 4


def _sha_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# Keys the loop itself writes into meta.json after declaration. The wake route
# is a campaign fact (the 2a wake design: where this campaign's wakes land),
# recorded by the scheduling tools on every live schedule -- fingerprinting it
# would make the declaration gate fire on the loop's own bookkeeping, so the
# fingerprint reads the declaration minus these keys. The fork kept addressing
# in the cron payload and could hash the raw bytes.
_LOOP_KEYS = ("wake_route",)


def meta_sha(campaign_dir: Path) -> str:
    """Fingerprint of the declaration, or "-" when there is none to read."""
    p = Path(campaign_dir) / "meta.json"
    try:
        text = p.read_text(encoding="utf-8")
    except OSError:
        return "-"
    try:
        data = json.loads(text)
    except ValueError:
        return _sha_text(text)
    if isinstance(data, dict):
        for key in _LOOP_KEYS:
            data.pop(key, None)
        text = json.dumps(data, ensure_ascii=False, sort_keys=True)
    return _sha_text(text)


def _staged_case(campaign_dir: Path) -> str:
    try:
        meta = json.loads((Path(campaign_dir) / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return str(meta.get("staged_case") or "").strip().rstrip("/")


def case_hashes(root: str, runner: Callable[[str], tuple[int, str]]) -> tuple[dict[str, str], bool]:
    """``{path: sha}`` for every file in the case, and whether the read worked.

    The second value matters as much as the first. A host that cannot answer
    yields an empty mapping, which is indistinguishable from a case whose files
    were all deleted -- and treating a network blip as a wiped apparatus would
    refuse a submit for no reason.
    """
    if not root:
        return {}, True
    q = shlex.quote(root)
    rc, out = runner(f"find {q} -maxdepth {_MAX_DEPTH} -type f -exec sha256sum {{}} + 2>/dev/null")
    if rc != 0:
        return {}, False
    table: dict[str, str] = {}
    for line in out.splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0]:
            table[parts[1].strip()] = parts[0]
    # A readable case with zero files is possible but means the path is wrong;
    # report it as unreadable so it surfaces rather than reading as "all removed".
    return table, bool(table)


def baseline_of(campaign_dir: Path, runner: Callable[[str], tuple[int, str]] | None) -> dict[str, Any]:
    """Fingerprint the apparatus as it stands right now."""
    root = _staged_case(campaign_dir)
    if root and runner is not None:
        case, readable = case_hashes(root, runner)
    else:
        case, readable = {}, True
    return {"meta_sha": meta_sha(campaign_dir), "staged_case": root, "case": case, "case_readable": readable}


def save_baseline(campaign_dir: Path, baseline: dict[str, Any]) -> None:
    (Path(campaign_dir) / BASELINE_FILE).write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_baseline(campaign_dir: Path) -> dict[str, Any] | None:
    p = Path(campaign_dir) / BASELINE_FILE
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


@dataclass(frozen=True)
class Drift:
    meta_changed: bool = False
    case_changed: list[str] = field(default_factory=list)
    case_added: list[str] = field(default_factory=list)
    case_removed: list[str] = field(default_factory=list)
    case_unreadable: bool = False

    @property
    def is_clean(self) -> bool:
        return not (self.meta_changed or self.case_changed or self.case_added or self.case_removed)

    def describe(self) -> list[str]:
        """One line per fact, operands only -- no verdict about whether it is bad."""
        out: list[str] = []
        if self.meta_changed:
            out.append("meta.json has changed since this campaign's first submit")
        for p in self.case_changed:
            out.append(f"case file changed: {p}")
        for p in self.case_added:
            out.append(f"case file added: {p}")
        for p in self.case_removed:
            out.append(f"case file removed: {p}")
        if self.case_unreadable:
            out.append("the case could not be read this time, so its files were not compared")
        return out


def compare(before: dict[str, Any] | None, now: dict[str, Any]) -> Drift:
    """What moved between the baseline and now.

    No baseline means this is the first submit: there is nothing to have drifted
    from, and reporting that as drift would refuse round 0 of every campaign.
    """
    if not before:
        return Drift(case_unreadable=not now.get("case_readable", True))
    if not now.get("case_readable", True):
        # Compare the declaration anyway -- it is read locally and a remote
        # failure says nothing about it.
        return Drift(meta_changed=before.get("meta_sha") != now.get("meta_sha"), case_unreadable=True)
    old_case: dict[str, str] = before.get("case") or {}
    new_case: dict[str, str] = now.get("case") or {}
    return Drift(
        meta_changed=before.get("meta_sha") != now.get("meta_sha"),
        case_changed=sorted(p for p in old_case.keys() & new_case.keys() if old_case[p] != new_case[p]),
        case_added=sorted(new_case.keys() - old_case.keys()),
        case_removed=sorted(old_case.keys() - new_case.keys()),
    )
