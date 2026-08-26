"""Where a deck's files live.

Paths only. The predecessor put the path layout, template binding, outline
persistence, the figure catalogue, fingerprints and content saving on one
745-line object that both routes shared, so every route change touched it and
no route used more than half of it. Persistence moved to the services that own
each artefact; what remains is the answer to "where does this deck keep things",
which every layer needs and none should own.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


@dataclass(frozen=True)
class Project:
    """One deck under a workspace, addressed by slug."""

    workspace: Path
    slug: str

    def __post_init__(self) -> None:
        if not _SLUG.match(self.slug):
            raise ValueError(
                f"{self.slug!r} is not a usable project name: lowercase letters, digits, '-' and '_', "
                "starting with a letter or digit, up to 64 characters"
            )

    @property
    def root(self) -> Path:
        """One deck per workspace, so the slug names it rather than nesting it.

        A workspace is one task: the launcher makes a directory per spawn and the
        agent is fenced inside it. Addressing decks by slug under it let a run
        start a second one by naming it differently, which two of six measured
        runs did -- and then nothing said which of the two was the deck, least of
        all the export path that repeats the same slug.
        """
        return self.workspace / "deck"

    @property
    def sources_dir(self) -> Path:
        """The deck's own source set: every document it stands on, whatever brought
        it in. One place, because the evidence accumulates -- a directory the user
        had, a file they attached, something fetched, the request itself -- and an
        ingest that read one of those at a time let whoever ran last decide what the
        deck's evidence was."""
        return self.root / "sources"

    @property
    def ingest_dir(self) -> Path:
        return self.root / "ingest"

    @property
    def figures_dir(self) -> Path:
        return self.ingest_dir / "figures"

    @property
    def build_dir(self) -> Path:
        return self.root / "build"

    @property
    def review_dir(self) -> Path:
        return self.root / "review"

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def exports_dir(self) -> Path:
        """Where the finished deck is published, under its own name.

        Not `exports/`: raven writes a session transcript to `<workspace>/exports`
        of its own, so that directory already answers to something else.
        """
        return self.workspace / "out"

    def contains(self, path: Path) -> bool:
        """Whether a path is inside this project, resolving symlinks first.

        Every path that arrives from a model is checked through here. The build
        directory holds a program the model wrote and the export directory is
        the delivery path, so "save to the project" has to mean the project and
        not a relative walk out of it.
        """
        try:
            resolved = path.resolve()
        except OSError:
            return False
        return resolved == self.root.resolve() or self.root.resolve() in resolved.parents
