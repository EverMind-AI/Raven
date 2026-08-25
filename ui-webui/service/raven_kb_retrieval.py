# -*- coding: utf-8 -*-
"""Let a session's knowledge bases reach a chat turn that runs on the gateway.

Everything up to the agent already works: the chat page attaches knowledge bases
to a session, and ``ChatService`` resolves each one and builds a ``RAGMiddleware``
out of them. It then hands that middleware to the agent class -- and in this
deployment the agent class is ``RavenGatewayAgent``, which forwards the turn to
raven over a WebSocket and drops every kwarg it does not understand, the
middleware among them. So the knowledge bases were configured, indexed and
searchable, and no chat turn could see any of it.

The middleware cannot simply be run here. Its default ``agentic`` mode works by
publishing a search tool into the agent's toolkit, and the toolkit belongs to the
AgentScope agent that gateway mode replaces -- raven runs its own loop with its
own tools and has no way to call back into this process. Its ``static`` mode,
though, is just "search on every user input and put the results in front of the
model", which is expressible over the gateway: the turn is a text field, and a
hint block prepended to it lands in the same place.

So this module runs the static path against the same knowledge handles, merging
across bases the way the middleware does, and formats the results into the hint
template it would have used. Two stages sit between: candidates are recalled
wide and narrowed by a reranker (see raven_kb_rerank), and what finally reaches
the model is a whole section rather than the matched slice (see
raven_kb_sections). Retrieval never breaks a turn: any failure returns the
user's text unchanged.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

from agentscope.middleware import RAGMiddleware
from raven_kb_rerank import rerank
from raven_kb_sections import Passage, expand

logger = logging.getLogger("uvicorn.error")

# How many candidates to recall before reranking. A reranker only improves on
# the vector order if it is given more to choose from than the caller asked for,
# and the extra candidates cost one vector scan, not one embedding call each.
# Capped because the reranker is charged per document.
_RECALL_MULTIPLIER = 5
_MIN_RECALL = 20
_MAX_RECALL = 50

# A ceiling on what retrieval may add to one turn. top_k goes up to 50 and a
# chunk can be a few thousand characters, so an unbounded hint could crowd the
# model's context out with material the user never asked about.
_MAX_CONTEXT_CHARS = 8000


def _text_of(chunk: Any) -> str:
    content = getattr(chunk, "content", None)
    return getattr(content, "text", "") or ""


def _citation(passage: Passage) -> str:
    """Name the passage as precisely as the index allows.

    The heading path turns a citation from "somewhere in this file" into the
    actual section, and lets the model quote it back. Documents indexed before
    the structured parser, and formats with no headings, fall back to the
    filename alone.
    """
    if passage.heading_path:
        return passage.source + " > " + " > ".join(passage.heading_path)
    return passage.source


_CLOSING_TAG = re.compile(r"</\s*([A-Za-z][\w-]*)\s*>")


def _envelope_closers(template: str) -> tuple[str, ...]:
    """The tag names that close the hint envelope, read off the template itself.

    Read rather than hardcoded because ``hint_template`` is accepted
    programmatically -- the dock hides it, being ``SkipJsonSchema``, but a caller
    can still override it, and delimiters that no longer match the template would
    neutralise nothing.
    """
    tail = template.split("{context}", 1)[-1]
    return tuple(dict.fromkeys(_CLOSING_TAG.findall(tail)))


def _neutralise(text: str, closers: tuple[str, ...]) -> str:
    """Stop document text from closing the envelope that contains it.

    A passage carrying the template's closing delimiters ends the hint early, and
    whatever follows lands between a closed ``</system-reminder>`` and the user's
    question -- the position where the model reads framing rather than quoted
    data. The envelope's whole job is to say "this came from a knowledge base";
    content that escapes it is no longer marked as retrieved at all. Bases are
    shareable and indexed documents are routinely not written by the user -- a
    crawled page, a downloaded report -- so the text cannot be trusted to leave
    the delimiters alone.

    Only the closing delimiters, and only the ones the template actually uses.
    Escaping ``<`` wholesale would mangle exactly the content people index:
    code samples, XML fragments, HTML. This leaves every other angle bracket
    verbatim.

    Whitespace inside the tag is tolerated because a model reading
    ``</ content >`` as a close is a coin toss, and matching only the tight form
    would leave the loose one working.
    """
    for tag in closers:
        text = re.sub(rf"</\s*{re.escape(tag)}\s*>", f"&lt;/{tag}>", text, flags=re.IGNORECASE)
    return text


def _format(passages: list[Passage], closers: tuple[str, ...]) -> str:
    """Render passages as a numbered, cited list.

    ``closers`` has no default on purpose: an empty tuple neutralises nothing, so
    a caller that forgets it gets the unescaped envelope back.
    """
    entries = [
        f"[{index}] (source: {_citation(passage)})\n{passage.text}" for index, passage in enumerate(passages, start=1)
    ]
    # Neutralise the joined text, not just each passage body: a heading is
    # document content too, so the citation line carries the same risk as the
    # passage under it.
    rendered = _neutralise("\n\n".join(entries), closers)
    if len(rendered) > _MAX_CONTEXT_CHARS:
        rendered = rendered[:_MAX_CONTEXT_CHARS] + "\n\n[truncated]"
    return rendered


class KnowledgeRetriever:
    """The static-mode half of ``RAGMiddleware``, driven from the gateway agent."""

    def __init__(self, knowledge_bases: list[Any], parameters: Any) -> None:
        self._knowledge_bases = knowledge_bases
        self._parameters = parameters

    @classmethod
    def from_middlewares(cls, middlewares: Any) -> "KnowledgeRetriever | None":
        """Pick the knowledge handles out of the middlewares built for this turn.

        Returns ``None`` when the session has no knowledge bases attached, which
        is the common case and must stay free of any retrieval work.
        """
        knowledge_bases: list[Any] = []
        parameters = None
        for middleware in middlewares or []:
            if not isinstance(middleware, RAGMiddleware):
                continue
            knowledge_bases.extend(middleware._knowledge_bases)
            parameters = parameters or middleware._parameters
        if not knowledge_bases:
            return None
        return cls(knowledge_bases, parameters or RAGMiddleware.Parameters())

    def _recall_size(self) -> int:
        return min(max(self._parameters.top_k * _RECALL_MULTIPLIER, _MIN_RECALL), _MAX_RECALL)

    async def _search(self, text: str) -> list[tuple[Any, Any]]:
        """Recall candidates from every knowledge base at once, best first.

        Mirrors the fan-out, flatten and score-merge the middleware's own
        ``_search_across`` performs, but keeps each hit paired with the
        knowledge base that produced it -- a flattened list has no way back to
        the collection holding the hit's sibling chunks, which is what section
        expansion needs.

        Returns more than ``top_k``: this is the recall stage, and the reranker
        downstream is what narrows it. Scores from bases with different
        embedding models are not strictly comparable; this sorts by raw score,
        exactly as the middleware does, which is another reason not to let this
        order be the final one.
        """
        recall = self._recall_size()
        # One base failing must not cost the others their hits. ChatService
        # already skips a base it cannot resolve so the turn runs on the rest; a
        # base that resolves and then fails to search -- a dead embedding
        # endpoint, an expired credential -- gets the same treatment here rather
        # than the opposite one. Without `return_exceptions` a single raise
        # unwinds past `augment`'s catch-all and the turn goes out bare.
        per_base = await asyncio.gather(
            *(
                base.search(
                    queries=[text],
                    top_k=recall,
                    score_threshold=self._parameters.score_threshold,
                )
                for base in self._knowledge_bases
            ),
            return_exceptions=True,
        )
        for base, hits in zip(self._knowledge_bases, per_base):
            if isinstance(hits, BaseException):
                logger.warning(
                    "knowledge: base %r dropped out of this turn: %s",
                    getattr(base, "collection", base),
                    hits,
                )
        pairs = [
            (base, hit)
            for base, hits in zip(self._knowledge_bases, per_base)
            if not isinstance(hits, BaseException)
            for hit in hits
        ]
        pairs.sort(key=lambda pair: pair[1].score, reverse=True)
        return pairs[:recall]

    async def _rerank(self, text: str, pairs: list[tuple[Any, Any]]) -> list[tuple[Any, Any]]:
        """Re-order candidates by reading each against the query, then cut to top_k.

        Runs on chunks rather than on expanded sections: the reranker scores a
        query against one passage, and a whole section dilutes the part that
        actually matched. Expansion happens afterwards, on the winners.

        A reranker that is unconfigured or unreachable returns no opinion, and
        the vector order stands -- the cut to ``top_k`` happens either way.
        """
        top_k = self._parameters.top_k
        order = await rerank(text, [_text_of(hit.chunk) for _, hit in pairs])
        if order is not None:
            pairs = [pairs[index] for index in order]
        return pairs[:top_k]

    async def augment(self, text: str) -> str:
        """Prepend the retrieved context to the user's message.

        Searches on every turn regardless of the configured mode. ``agentic`` is
        the default and cannot be honoured here -- it needs a tool in the agent's
        toolkit, which gateway mode does not own -- and treating it as "no
        retrieval" would mean a knowledge base attached through the UI silently
        did nothing. Searching anyway is the behaviour that matches what
        attaching one is asking for.

        Two more of ``Parameters`` are not honoured either, and unlike
        ``hint_template`` they are not ``SkipJsonSchema``, so they carry a title
        and a description into the dock UI and a user can set them. Written down
        here because this whole module exists to fix a knob that silently did
        nothing, and leaving two in that state undocumented invites the same
        report:

        - ``emit_hint_event`` (default on, "Show matched chunks in chat") wants a
          ``HintBlockEvent``. Nothing emits one over this transport, so the front
          end never gets it; what the user sees is the retrieved text inlined at
          the top of their own message.
        - ``persist_hint`` (default off) asks for the hint to leave the agent
          context right after the model call. Here it is part of the string
          handed to ``turn.send``, and that string is what raven persists: it
          lands in session history and in the consolidation input, so the
          passages stay in front of the model for the rest of the session and
          can be distilled into long-term memory as though the user had typed
          them. Honouring it needs a transient-context field on the gateway's
          turn protocol, which ``TurnSendParams`` (strict, no such field) does
          not have -- a protocol change, not a local fix.

        Returns ``text`` unchanged when there is nothing to add, and on any
        failure: a knowledge base whose embedding endpoint is down must not cost
        the user their turn.
        """
        if not text.strip():
            return text
        try:
            passages = await expand(await self._rerank(text, await self._search(text)))
        except Exception as exc:  # noqa: BLE001 - the turn matters more
            logger.warning("knowledge: retrieval failed, sending the turn as-is: %s", exc)
            return text

        context = _format(passages, _envelope_closers(self._parameters.hint_template))
        if not context:
            return text

        hint = self._parameters.hint_template.replace("{context}", context)
        # Context first, question last: the request is what the model should be
        # answering, and burying it under a wall of retrieved text is how a
        # retrieval-augmented turn starts answering the wrong thing.
        return f"{hint}\n\n{text}"
