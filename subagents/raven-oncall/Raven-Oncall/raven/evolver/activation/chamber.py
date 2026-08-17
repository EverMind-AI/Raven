"""Pre-flight chamber: replay activation specs over recorded trajectories.

Corpus note (design section 8): agent trajectories from the uv-contaminated
runs are VALID behavioral data — the contamination was verifier-side. Default
corpus = every trial dir handed in via roots; the report records provenance.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from raven.evolver.activation.spec import ActivationSpec, evaluate_spec

SESSION_GLOB = "**/sessions/tb2-task.jsonl"


@dataclass
class Corpus:
    trajectories: list[list[dict]]
    provenance: list[str] = field(default_factory=list)


@dataclass
class ChamberReport:
    node_id: str
    spec_kind: str
    reachable_count: int
    corpus_size: int
    provenance: list[str]

    @property
    def verdict(self) -> str:
        return "PASS" if self.reachable_count > 0 else "BLOCK"

    def to_dict(self) -> dict:
        return {
            "node_id": self.node_id,
            "spec_kind": self.spec_kind,
            "reachable_count": self.reachable_count,
            "corpus_size": self.corpus_size,
            "provenance": self.provenance,
            "verdict": self.verdict,
        }


def load_corpus(roots: list[Path]) -> Corpus:
    trajectories: list[list[dict]] = []
    provenance: list[str] = []
    for root in roots:
        provenance.append(str(root))
        for session in sorted(Path(root).glob(SESSION_GLOB)):
            traj = []
            try:
                for line in session.open():
                    try:
                        traj.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            except OSError:
                continue
            if traj:
                trajectories.append(traj)
    return Corpus(trajectories=trajectories, provenance=provenance)


def run_chamber(node_id: str, spec: ActivationSpec, corpus: Corpus) -> ChamberReport:
    count = evaluate_spec(spec, corpus.trajectories)
    return ChamberReport(
        node_id=node_id,
        spec_kind=spec.kind,
        reachable_count=count,
        corpus_size=len(corpus.trajectories),
        provenance=corpus.provenance,
    )
