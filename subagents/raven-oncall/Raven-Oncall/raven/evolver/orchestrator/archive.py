"""GSME archive — one gated elite per (WHERE x WHY) cell, plus recombination.

The paper's Gated Semantic MAP-Elites has three parts: a categorical archive
keyed on the pathology a patch addresses (one elite per cell, entered only
after the gates), quality-biased selection (each round extends the current
best harness — the loop's greedy parent selection), and cross-cell
recombination (the best harness is stacked with elites from OTHER pathology
cells). The loop always had the middle part; this module adds the other two:

- :meth:`GsmeArchive.consider` observes every gated outcome. A candidate whose
  full-train confirm beat the FIXED vanilla mean (the paper's navigation bar —
  deliberately vanilla, not the possibly-ratcheted promotion baseline, so an
  independently-good mechanism is banked even when it loses to the current
  champion) enters/replaces its cell's elite.
- :meth:`GsmeArchive.eligible_elites` proposes recombination targets for a
  parent: elites from cells NOT already stacked into the parent's lineage,
  skipping pairings already tried and pairs whose edits touch the same files
  (full-file-bytes stacking would silently clobber one side of a same-file
  overlap, mismeasuring the stack — such pairs are skipped, not merged).

The archive never decides credit: a recombinant goes through the exact same
apply -> screen/confirm -> gate pipeline as a designed candidate. It only
steers which combinations get measured, so label noise in WHY degrades
coverage, never the quality of what is ultimately promoted (same argument as
the paper's descriptor-noise analysis).

State persists to one JSON (``config.archive_path``) after every round, so a
resumed run keeps its elites, lineage metadata, and attempted pairings.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from raven.evolver.orchestrator.gates.policy import CandidateOutcome
from raven.evolver.tree.node import AppliedPatch, HarnessNode, PatchWhy


@dataclass(frozen=True)
class CellElite:
    """The gated elite of one (WHERE x WHY) cell.

    ``files`` / ``deletions`` are the repo-relative paths the elite's edit
    changed vs its own parent commit — together with ``git_commit_sha`` they
    are all a recombiner needs to re-materialise the mechanism onto a new
    parent (the bytes are read back from the commit, so this record is
    resume-safe without storing file contents).
    """

    cell: str
    node_id: str
    git_commit_sha: str
    score: float
    round_index: int
    why: str
    where: str
    credited: bool = False
    files: tuple[str, ...] = ()
    deletions: tuple[str, ...] = ()
    focused_task_ids: tuple[str, ...] = ()
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "cell": self.cell,
            "node_id": self.node_id,
            "git_commit_sha": self.git_commit_sha,
            "score": self.score,
            "round_index": self.round_index,
            "why": self.why,
            "where": self.where,
            "credited": self.credited,
            "files": list(self.files),
            "deletions": list(self.deletions),
            "focused_task_ids": list(self.focused_task_ids),
            "summary": self.summary,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CellElite":
        return cls(
            cell=d["cell"],
            node_id=d["node_id"],
            git_commit_sha=d["git_commit_sha"],
            score=float(d["score"]),
            round_index=int(d["round_index"]),
            why=d["why"],
            where=d.get("where", "edit"),
            credited=bool(d.get("credited", False)),
            files=tuple(d.get("files") or ()),
            deletions=tuple(d.get("deletions") or ()),
            focused_task_ids=tuple(d.get("focused_task_ids") or ()),
            summary=d.get("summary", ""),
        )


@dataclass
class RecombinantCandidate:
    """A cross-cell recombination candidate: an elite's edit re-materialised
    onto the current parent.

    Duck-types the bench candidate contract the wired lines share
    (``files`` bytes / ``deletions`` / ``why`` / ``focused_task_ids`` /
    ``summary``), so ``files_of`` / ``deletions_of`` / ``focused_source`` /
    ``outcome_hook`` all consume it unchanged and it flows through the standard
    apply -> gate pipeline like any designed candidate. ``elite_node_id`` marks
    it for pairing bookkeeping and the audit trail.
    """

    files: dict[str, bytes]
    why: str
    cell: str
    elite_node_id: str
    focused_task_ids: list[str] = field(default_factory=list)
    summary: str = ""
    deletions: list[str] = field(default_factory=list)
    # Inherited from the elite's bytes: a beacon-carrying mechanism keeps its
    # Gate-b attribution when re-stacked (the code is byte-identical).
    has_beacon: bool = False


# Path -> lever heuristics for the mechanical WHERE binding (paper App. A:
# the four levers over the harness's edit surfaces). Module-level so a bench
# with an exotic layout can monkeypatch/extend; unknowns default to runtime
# (a code edit is a control-flow change until proven otherwise).
_KNOWLEDGE_MARKERS = ("skills/", "skill/", "everos/", "memory")
_CONFIG_SUFFIXES = (".yaml", ".yml", ".json", ".toml", ".ini", ".cfg")
_PROMPT_SUFFIXES = (".md", ".txt", ".prompt")


def _lever_of_path(path: str) -> str:
    p = path.replace("\\", "/").lower()
    if any(m in p for m in _KNOWLEDGE_MARKERS):
        return "knowledge"
    if p.endswith(_CONFIG_SUFFIXES):
        return "config"
    if p.endswith(_PROMPT_SUFFIXES):
        return "prompt"
    return "runtime"


def bind_where(paths) -> str:
    """Mechanically bind the WHERE lever from the files a patch touches.

    The paper's WHERE is read off the patch artifact, never self-declared —
    that keeps the archive's WHERE axis noise-free while WHY carries all the
    modeling error. A patch spanning several levers is its own ``"mixed"``
    coordinate (stacking it wholesale is still meaningful); no paths at all
    (a metadata-only candidate) binds to ``"edit"``, the unknown lever.
    """
    levers = {_lever_of_path(p) for p in paths}
    if not levers:
        return "edit"
    if len(levers) == 1:
        return levers.pop()
    return "mixed"


def cell_of(cand: Any) -> Optional[tuple[str, str]]:
    """The (WHERE-lever, WHY) coordinate of a candidate, or None when it has
    no WHY. WHERE is bound mechanically from the touched files (:func:`bind_where`);
    a driver-declared ``patch_where`` stays on the node ledger for audit but
    never decides the archive coordinate."""
    if isinstance(cand, AppliedPatch):
        why = cand.patch_why_extra if cand.patch_why == PatchWhy.other else cand.patch_why.value
        paths = [c.target_file for c in cand.components]
        return bind_where(paths), str(why)
    why = getattr(cand, "why", None)
    if why:
        changed, deleted = _touched_paths(cand)
        return bind_where(list(changed) + list(deleted)), str(why)
    return None


def _touched_paths(cand: Any) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """(changed, deleted) repo-relative paths of a candidate's edit."""
    if isinstance(cand, AppliedPatch):
        return tuple(c.target_file for c in cand.components), ()
    files = getattr(cand, "files", None)
    changed = tuple(sorted(files)) if isinstance(files, dict) else ()
    deleted = tuple(getattr(cand, "deletions", None) or ())
    return changed, deleted


def describe_candidate(cand: Any) -> Optional[dict]:
    """JSON-safe candidate metadata for the node ledger.

    The wired bench candidates are not :class:`AppliedPatch`, so a node record
    would otherwise carry ``patch: null`` and lose the WHERE/WHY/touched-files/
    activation info the ledger is supposed to hold. Returns None for a
    candidate with no coordinate (nothing to record).
    """
    coord = cell_of(cand)
    if coord is None:
        return None
    where, why = coord
    changed, deleted = _touched_paths(cand)
    d: dict[str, Any] = {
        "why": why,
        "where": where,
        "files": list(changed),
        "deletions": list(deleted),
        "summary": str(getattr(cand, "summary", "") or "")[:300],
        "has_beacon": bool(getattr(cand, "has_beacon", False)),
    }
    spec = getattr(cand, "activation_spec", None)
    if isinstance(spec, dict):
        d["activation_spec"] = spec
    elite_id = getattr(cand, "elite_node_id", None)
    if elite_id:
        d["recombination_of"] = elite_id
    return d


class GsmeArchive:
    """The persistent GSME state: cell elites, lineage metadata, pairings.

    ``node_meta`` records, for every PROMOTED node, which cells its lineage has
    stacked and which files that lineage touched — the exclusion sets
    :meth:`eligible_elites` filters recombination proposals with. ``pairings``
    records every (parent, elite) recombination already attempted (whatever
    its outcome), so a patience streak does not re-propose the same pair every
    round. A parent with no metadata (the root, or a pre-archive journal)
    just gets an empty lineage: redundant proposals are then caught by the
    pairing record and, failing that, measured and rejected by the gate.
    """

    def __init__(self, path: str | Path):
        self._path = Path(path)
        self._cells: dict[str, CellElite] = {}
        self._node_meta: dict[str, dict[str, list[str]]] = {}
        self._pairings: dict[str, dict[str, str]] = {}
        self._load()

    # ---- observation -------------------------------------------------------

    def consider(
        self,
        *,
        parent_id: str,
        node: HarnessNode,
        cand: Any,
        outcome: CandidateOutcome,
        vanilla_train_mean: float,
        round_index: int,
    ) -> None:
        """Observe one gated outcome: record pairings, lineage, and elites."""
        elite_id = getattr(cand, "elite_node_id", None)
        if elite_id:
            self.record_pairing(parent_id, elite_id, outcome.status.value)

        coord = cell_of(cand)
        if coord is None:
            return
        where, why = coord
        key = f"{where}::{why}"
        changed, deleted = _touched_paths(cand)

        if outcome.promoted:
            pmeta = self._node_meta.get(parent_id, {})
            self._node_meta[node.node_id] = {
                "cells": sorted(set(pmeta.get("cells", [])) | {key}),
                "files": sorted(set(pmeta.get("files", [])) | set(changed) | set(deleted)),
            }

        # Navigation bar (paper Alg. 1): full-train confirm beat VANILLA. A
        # screen-pruned or vanilla-losing candidate never enters the archive.
        if not outcome.confirm_evals or outcome.score <= vanilla_train_mean:
            return
        prev = self._cells.get(key)
        if prev is not None and prev.score >= outcome.score:
            return
        self._cells[key] = CellElite(
            cell=key,
            node_id=node.node_id,
            git_commit_sha=node.git_commit_sha,
            score=outcome.score,
            round_index=round_index,
            why=why,
            where=where,
            credited=bool(outcome.paired and outcome.paired.credited_2sigma),
            files=changed,
            deletions=deleted,
            focused_task_ids=tuple(getattr(cand, "focused_task_ids", None) or ()),
            summary=str(getattr(cand, "summary", "") or "")[:300],
        )

    def record_pairing(self, parent_id: str, elite_node_id: str, status: str) -> None:
        self._pairings.setdefault(parent_id, {})[elite_node_id] = status

    # ---- recombination proposals -------------------------------------------

    def eligible_elites(self, parent_id: str, *, limit: int = 1) -> list[CellElite]:
        """Elites worth stacking onto ``parent_id``, best score first.

        Excluded: cells already in the parent's lineage, the parent itself,
        pairings already attempted, and elites whose edit overlaps a file the
        lineage already changed (byte-level stacking cannot merge same-file
        edits, only replace — an overlapping "stack" would silently drop one
        mechanism and measure a lie).
        """
        if limit <= 0:
            return []
        meta = self._node_meta.get(parent_id, {})
        lineage_cells = set(meta.get("cells", []))
        lineage_files = set(meta.get("files", []))
        tried = self._pairings.get(parent_id, {})
        out: list[CellElite] = []
        ranked = sorted(self._cells.items(), key=lambda kv: (-kv[1].score, kv[0]))
        for key, elite in ranked:
            if key in lineage_cells:
                continue
            if elite.node_id == parent_id or elite.node_id in tried:
                continue
            if lineage_files & (set(elite.files) | set(elite.deletions)):
                continue
            out.append(elite)
            if len(out) >= limit:
                break
        return out

    # ---- reporting ----------------------------------------------------------

    def summary_text(self) -> str:
        """One line per cell elite — injectable into a design prompt so the
        driver knows which mechanisms are already banked."""
        if not self._cells:
            return ""
        lines = ["ARCHIVE (verified elites, one per failure cell):"]
        for key in sorted(self._cells):
            e = self._cells[key]
            cred = " credited" if e.credited else ""
            lines.append(
                f"- {key}: {e.node_id} score={e.score:.3f}{cred} r{e.round_index}"
                + (f" — {e.summary}" if e.summary else "")
            )
        return "\n".join(lines)

    @property
    def cells(self) -> dict[str, CellElite]:
        return dict(self._cells)

    # ---- persistence ---------------------------------------------------------

    def save(self) -> None:
        """Persist to ``path`` (best-effort, atomic tmp+rename)."""
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                {
                    "cells": {k: e.to_dict() for k, e in self._cells.items()},
                    "node_meta": self._node_meta,
                    "pairings": self._pairings,
                },
                indent=2,
            )
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(payload)
            tmp.replace(self._path)
        except OSError:
            pass

    def _load(self) -> None:
        try:
            if not self._path.exists():
                return
            d = json.loads(self._path.read_text())
            self._cells = {k: CellElite.from_dict(v) for k, v in (d.get("cells") or {}).items()}
            self._node_meta = {
                k: {"cells": list(v.get("cells", [])), "files": list(v.get("files", []))}
                for k, v in (d.get("node_meta") or {}).items()
            }
            self._pairings = {k: dict(v) for k, v in (d.get("pairings") or {}).items()}
        except (OSError, ValueError, KeyError):
            self._cells, self._node_meta, self._pairings = {}, {}, {}


__all__ = [
    "CellElite",
    "GsmeArchive",
    "RecombinantCandidate",
    "bind_where",
    "cell_of",
    "describe_candidate",
]
