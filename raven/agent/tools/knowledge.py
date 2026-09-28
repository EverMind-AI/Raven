"""Search the knowledge bases this conversation was pointed at.

A reader picks bases in the composer; the agent decides when to reach them.
That split is the whole design. The tool takes a query and never a base, so a
model cannot read material nobody offered it, and the bases it searches come
from the turn's own binding (:mod:`raven.agent.knowledge_scope`) rather than
from an argument it could have chosen or a list it was built with -- one loop
serves every session on the process.

Withheld, not refused, where nothing was picked. A conversation with no bases
is the ordinary case, and a tool that is always offered and always answers
"you have not selected anything" spends a slot in every schema to say so.

Answers text with the document each piece came from. A hit a reader cannot
place is a hit they cannot check, and "the handbook, page 12" is what makes an
answer verifiable rather than merely confident.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from loguru import logger

from raven.agent import knowledge_scope
from raven.contracts.tool import Tool

if TYPE_CHECKING:
    from raven.knowledge import KnowledgeManager

#: How many pieces one search brings back when the call does not say. Each base
#: has its own `top_k`, which is what a search of that base alone uses; this is
#: the ceiling over a merged set, where several bases each contribute.
DEFAULT_LIMIT = 8

#: The most any one call may ask for. A model that asks for a hundred pieces is
#: asking for the whole base, and the answer would fill the context it was
#: trying to save.
MAX_LIMIT = 30

#: How much of a piece goes to the model. A chunk is a few hundred words by
#: design, but a base cut at 8k tokens has pieces that would swallow a turn.
MAX_CHARS = 2000


class KnowledgeSearchTool(Tool):
    """Find passages in the bases this conversation was pointed at."""

    name = "knowledge_search"
    description = (
        "Search the knowledge bases attached to this conversation and return the passages that "
        "answer a question, best first. The material is what the person you are talking to chose "
        "to put in front of you: their own documents, already read and indexed. Use it when the "
        "answer is more likely to be in their material than in yours -- a fact about their "
        "project, their product, their notes -- and quote the document a passage came from when "
        "you use it, so the answer can be checked."
    )

    def __init__(self, manager: "KnowledgeManager | None" = None) -> None:
        # Built lazily and kept: opening the store reads a directory and the
        # first search opens a connection, neither of which a process that
        # never searches should pay for.
        self._manager = manager

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": (
                        "What to look for, in the words a passage would be written in. This is "
                        "matched by meaning, so a question or a phrase both work; a bare keyword "
                        "gives the search less to go on."
                    ),
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_LIMIT,
                    "description": f"How many passages to return. Defaults to {DEFAULT_LIMIT}.",
                },
            },
            "required": ["query"],
        }

    def _library(self) -> "KnowledgeManager":
        if self._manager is None:
            from raven.config.paths import get_runtime_subdir
            from raven.knowledge import KnowledgeManager

            self._manager = KnowledgeManager(get_runtime_subdir("knowledge"))
        return self._manager

    async def execute(self, **kwargs: Any) -> str:
        query = str(kwargs.get("query") or "").strip()
        if not query:
            return "knowledge_search needs a query: say what to look for."

        bases = knowledge_scope.selected()
        if not bases:
            # Reachable despite the withholding: a plugin or a subagent may
            # call a tool the schema did not offer this turn.
            return "No knowledge bases are attached to this conversation, so there is nothing to search."

        limit = kwargs.get("limit")
        want = DEFAULT_LIMIT if not isinstance(limit, int) or isinstance(limit, bool) else limit
        want = max(1, min(MAX_LIMIT, want))

        try:
            found = await self._library().search(list(bases), query, top_k=want)
        except Exception as exc:  # noqa: BLE001 - the model is told, the turn goes on
            logger.warning("knowledge_search failed ({})", exc)
            return f"The search could not be run: {exc}"

        if not found.hits:
            return f"Nothing in the attached material answers {query!r}."

        named = self._names(found.hits)
        lines: list[str] = []
        for at, hit in enumerate(found.hits[:want], 1):
            where = named.get(hit.document_id) or hit.document_id
            page = getattr(hit.chunk, "metadata", {}).get("page_number")
            place = f"{where}, page {page}" if isinstance(page, int) else where
            text = (hit.chunk.text or "").strip()
            if len(text) > MAX_CHARS:
                text = f"{text[:MAX_CHARS]}..."
            lines.append(f"[{at}] {place}\n{text}")

        # Said once at the end rather than per hit: a base whose vectors could
        # not be reached answered by words, and a reader comparing two sets of
        # hits deserves to know which. It is not an error -- those hits are in
        # the list -- so it does not lead.
        if found.by_keyword:
            lines.append(
                "(Some of this was found by matching words rather than meaning, because "
                f"{len(found.by_keyword)} of the attached bases could not reach their embedding model.)"
            )
        return "\n\n".join(lines)

    def _names(self, hits: list[Any]) -> dict[str, str]:
        """What each hit's document is called, looked up once per document."""
        out: dict[str, str] = {}
        for hit in hits:
            if hit.document_id in out:
                continue
            try:
                record = self._library().get_document(hit.document_id)
            except Exception:  # noqa: BLE001 - a hit with no name is still a hit
                record = None
            out[hit.document_id] = getattr(record, "source", "") or ""
        return out


__all__ = ["DEFAULT_LIMIT", "MAX_CHARS", "MAX_LIMIT", "KnowledgeSearchTool"]
