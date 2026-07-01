"""Generate a fake mass-library DB for development.

Drops a SQLite file at the given path containing N synthetic skills with
random unit-vector embeddings. The point is to give SkillService something
real to attach to before the *actual* mass library DB is built — pipeline
plumbing can be tested end-to-end while the encoding side is still TBD.

Usage::

    uv run python scripts/build_test_mass_library.py \\
        --db ~/.raven_test/mass.db \\
        --count 100 \\
        --dim 1024 \\
        --model fake-test-model

Then point ``skill_forge.mass_library_db`` at the same path.
"""

from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

import numpy as np

# Allow ``python scripts/...`` from a checkout root without ``pip install -e``.
_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from raven.skill_forge.store import SqliteStore
from raven.skill_forge.types import SkillMeta

_CATEGORIES = [
    "DOC",
    "CODING",
    "DATA",
    "DEVOPS",
    "TESTING",
    "SECURITY",
    "FRONTEND",
    "BACKEND",
    "DOMAIN",
    "META",
    "SCIENCE",
]
_VERBS = ["generate", "parse", "render", "analyze", "transform", "fetch"]
_NOUNS = ["pdf", "csv", "report", "image", "text", "config", "graph"]


def _fake_skill(rng: random.Random, idx: int) -> SkillMeta:
    name = f"mock_skill_{idx:04d}"
    verb = rng.choice(_VERBS)
    noun = rng.choice(_NOUNS)
    cat = rng.choice(_CATEGORIES)
    desc = f"{verb} {noun} ({cat.lower()})"
    body = f"# {name}\n\nUse this skill to {verb} a {noun}.\n"
    return SkillMeta(
        id=f"mock/{name}",
        name=name,
        description=desc,
        path=Path(f"sqlite://mock/{name}"),
        content=body,
        source="mock",
        raw_frontmatter={"category": cat},
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--db", type=Path, required=True, help="Output SQLite file (will be created/overwritten).")
    ap.add_argument("--count", type=int, default=100, help="Number of fake skills to generate. (default: 100)")
    ap.add_argument("--dim", type=int, default=1024, help="Embedding dimension. (default: 1024)")
    ap.add_argument(
        "--model",
        type=str,
        default="fake-test-model",
        help="String written into ``embedding_model`` column. "
        "Must match RetrievalConfig.embedding_model when "
        "SkillService loads the DB.",
    )
    ap.add_argument("--seed", type=int, default=42, help="Random seed for reproducibility. (default: 42)")
    args = ap.parse_args(argv)

    if args.db.exists():
        args.db.unlink()  # fresh build each run

    rng = random.Random(args.seed)
    np_rng = np.random.default_rng(args.seed)

    with SqliteStore(args.db) as store:
        for i in range(args.count):
            meta = _fake_skill(rng, i)
            v = np_rng.standard_normal(args.dim, dtype=np.float32)
            v /= np.linalg.norm(v) + 1e-9
            store.upsert(meta)
            store.set_embedding(
                meta.name,
                meta.source,
                v.tobytes(),
                args.model,
                args.dim,
                "float32",
            )

    print(f"Wrote {args.count} skills × {args.dim}-dim embeddings to {args.db}")
    print(
        f"Set ``skill_forge.mass_library_db = '{args.db}'`` and "
        f"``skill_forge.embedding_model = '{args.model}'`` in your config."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
