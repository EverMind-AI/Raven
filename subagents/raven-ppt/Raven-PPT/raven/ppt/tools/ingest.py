"""`ppt_ingest`: read the materials once, and say what is in them.

Everything downstream stands on this call. The fact gate can only refuse a number
the materials never printed if the materials were read; the citation gate can only
catch a page citing Figure 4 while showing Figure 5 if the captions were extracted.
So the reply's job is to make the deck's evidence *addressable* -- every asset by
id, with the label the source gave it -- rather than to describe the reading.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from raven.agent.tools.base import Tool
from raven.ppt.contracts import Project
from raven.ppt.services.ingest import index, ingest_materials, mirror
from raven.ppt.tools import _return

# How many assets are listed in full. Past this the list stops being read and
# starts being scrolled; the catalogue file holds all of them either way.
MAX_LISTED = 40


class PptIngestTool(Tool):
    name = "ppt_ingest"
    description = (
        "Read every document in the project's materials directory once: the text, the figures, the tables "
        "and the numbers. It writes materials.md, a fact index and a figure catalogue into the project, and "
        "returns the figure ids with the caption each one carries in its source. Every later step reads what "
        "this produced -- a number on a slide is checked against this index, and a figure reference against "
        "these labels -- so run it before writing anything."
    )
    timeout_seconds = 600.0

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "project": {
                    "type": "string",
                    "description": "short lowercase slug naming this deck; every later call uses it",
                },
                "materials_dir": {
                    "type": "string",
                    "description": (
                        "a directory of the user's documents to bring into the deck's sources, relative to "
                        "the workspace. Leave it out to re-read the sources the deck already holds -- which "
                        "is what you want after editing one by hand"
                    ),
                },
            },
            "required": ["project"],
        }

    async def execute(self, project: str, materials_dir: str | None = None, **kwargs: Any) -> str:
        try:
            deck = Project(workspace=self.workspace, slug=project)
        except ValueError as exc:
            return _return.failed(str(exc))

        taken = 0
        left_behind: tuple[str, ...] = ()
        templates: tuple[str, ...] = ()
        if materials_dir:
            given = (self.workspace / materials_dir).resolve()
            if not _inside(self.workspace, given):
                return _return.failed(f"{materials_dir} is outside the workspace")
            if not given.is_dir():
                return _return.failed(f"there is no directory at {materials_dir}")
            mirrored = mirror(deck, given)
            taken, left_behind = mirrored.taken, mirrored.left_behind
            templates = mirrored.templates
        if not deck.sources_dir.is_dir() or not any(deck.sources_dir.iterdir()):
            return _return.failed(
                "this deck holds no sources to read",
                hint="name a directory of documents in materials_dir, or let ppt_prepare find them",
            )

        try:
            # Reading a PDF is CPU-bound and takes seconds per document; run it off
            # the event loop so nothing else in the turn is blocked behind it.
            outcome = await asyncio.to_thread(ingest_materials, deck.sources_dir, deck.ingest_dir)
        except FileNotFoundError as exc:
            return _return.failed(str(exc))
        except (OSError, ValueError) as exc:
            return _return.failed(f"the materials could not be read: {exc}")

        assets = [_asset(asset) for asset in outcome.assets[:MAX_LISTED]]
        payload: dict[str, Any] = {
            "project": project,
            **({"taken_from": materials_dir, "taken": taken} if materials_dir else {}),
            **({"left_behind": list(left_behind)} if left_behind else {}),
            **({"templates_here": list(templates)} if templates else {}),
            "materials": _return.where(outcome.materials_path, self.workspace),
            # Every part of it with the line it starts at, because the whole file is
            # long enough that reading it is a decision: 16k tokens on a fourteen-page
            # paper, and it is re-sent on every request for the rest of the run.
            "materials_index": index(outcome.materials_path),
            "sources": [_return.where(deck.sources_dir / name, self.workspace) for name in outcome.source_files],
            "pages": outcome.page_count,
            "characters": outcome.text_chars,
            "figures": assets,
            "figure_count": len(outcome.assets),
        }
        if len(outcome.assets) > MAX_LISTED:
            payload["figures_note"] = f"{len(outcome.assets) - MAX_LISTED} more are in {outcome.catalogue_path.name}"
        if outcome.findings:
            payload["measured"] = _return.grouped(outcome.findings)
        asks = [
            f"read what you need of {_return.where(outcome.materials_path, self.workspace)} before deciding "
            "what the deck says -- materials_index lists every part with the line it starts at, and read_file "
            "takes offset and limit. Whatever you read stays in the context and is re-sent on every later "
            "request, so read the parts the deck stands on rather than the file"
        ]
        if not outcome.assets:
            asks.append("nothing visual was extracted, so any evidence on a page has to be drawn rather than placed")
        return _return.done(asks=asks, **payload)


def _asset(asset: Any) -> dict[str, Any]:
    """One asset as the author needs to address it.

    `source_label` is the load-bearing field: it is the name the *source* gave
    this figure, and citing a figure by a number nobody extracted is how a page
    ends up referencing Figure 4 while showing Figure 5.
    """
    entry: dict[str, Any] = {
        "figure_id": asset.asset_id,
        "kind": asset.kind.value if hasattr(asset.kind, "value") else str(asset.kind),
    }
    for name in ("source_label", "caption", "source_file", "source_page"):
        value = getattr(asset, name, None)
        if value:
            entry[name] = value
    if asset.concerns:
        entry["concerns"] = list(asset.concerns)
    return entry


def _inside(workspace: Path, path: Path) -> bool:
    root = workspace.resolve()
    return path == root or root in path.parents
