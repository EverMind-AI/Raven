"""``context_retrieve`` -- the way back from a ref to the text it stands for.

A ``[[ref ...]]`` in the window is a promise: the thing it names is one call
away, verbatim. This tool is that call. Without it a ref is decoration, and the
pruner that mints refs would just be deleting output with a citation attached.

Only the ``session`` store resolves here, and only for the session the turn
belongs to. That second half is the part worth stating: a ref is a string the
model can compose, so a tool that resolved whatever scope it was handed would
read another conversation's log on request. The scope is checked against the
session's current :class:`~raven.contracts.context.SessionRevision`, which also
makes a ref minted before a ``/new`` or an undo a miss rather than a wrong
answer.

What comes back is byte-for-byte what the tool returned, up to the caller's
token budget -- the budget is reported when it binds, so a partial answer is
never mistaken for a whole one. A span written before the log kept results
whole is reported as truncated instead: those bytes were never stored and no
amount of asking will produce them.

Read-only: the session is looked up with ``SessionManager.peek`` and nothing
here writes to it or to its file.
"""

from __future__ import annotations

import json
import re
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from raven.contracts.context import SessionRevision
from raven.contracts.tool import Tool
from raven.session.refs import SESSION_REF_STORE, belongs_to, is_truncated
from raven.utils.refs import SourceRef
from raven.utils.tokens import estimate_message_tokens

if TYPE_CHECKING:
    from raven.session.manager import SessionManager

_DEFAULT_MAX_TOKENS = 4_000
_MIN_MAX_TOKENS = 256
_MAX_MAX_TOKENS = 32_000
_SNIPPET_HEAD_CHARS = 800
_SNIPPET_TAIL_CHARS = 400
_MAX_REFS = 20
"""Refs answered in one call. A span already carries many messages, and the
budget is shared, so a longer list only splits the same allowance further."""


class ContextRetrieveTool(Tool):
    """Reads back the messages a ref names, from this session's log."""

    def __init__(self, sessions: SessionManager) -> None:
        self._sessions = sessions
        self._session_key: ContextVar[str | None] = ContextVar("context_retrieve_session_key", default=None)

    def set_context(self, channel: str, chat_id: str, session_key: str | None = None) -> None:
        """Bind the turn's session (turn-local, like every other routed tool)."""
        self._session_key.set(session_key or f"{channel}:{chat_id}")

    @property
    def name(self) -> str:
        return "context_retrieve"

    @property
    def description(self) -> str:
        return (
            "Read back the verbatim text behind [[ref ...]] tags in a compacted summary or a "
            "pruned tool result, when the one-line record is not enough. Pass the tags as written, "
            'e.g. refs=["[[ref session/<scope>#118]]"].'
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "minItems": 1,
                    "description": "The [[ref ...]] tags.",
                },
                "mode": {
                    "type": "string",
                    "enum": ["exact", "snippet"],
                    "description": "exact (default): the text; snippet: each entry's opening and closing.",
                },
                "max_tokens": {
                    "type": "integer",
                    "minimum": _MIN_MAX_TOKENS,
                    "maximum": _MAX_MAX_TOKENS,
                    "description": f"Budget for the reply (default {_DEFAULT_MAX_TOKENS}).",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "exact mode: start this many characters in (a cut reply names next_offset).",
                },
            },
            "required": ["refs"],
        }

    async def execute(self, **kwargs: Any) -> str:
        refs = kwargs.get("refs") or []
        if isinstance(refs, str):
            # A list the model serialised into one string is still the list it
            # meant; measured, a model asked this way did exactly that.
            try:
                decoded = json.loads(refs)
            except ValueError:
                decoded = None
            refs = decoded if isinstance(decoded, list) else [refs]
        if not isinstance(refs, list) or not refs:
            return "Error: refs must be a non-empty list of [[ref ...]] tags."
        mode = kwargs.get("mode") or "exact"
        if mode not in ("exact", "snippet"):
            return f"Error: unknown mode {mode!r}; expected 'exact' or 'snippet'."
        budget = _clamp_budget(kwargs.get("max_tokens"))
        offset = _clamp_offset(kwargs.get("offset"))

        session_key = self._session_key.get()
        if not session_key:
            return "Error: context_retrieve has no session bound; nothing to retrieve from."
        session = self._sessions.peek(session_key)
        if session is None:
            return f"Error: session {session_key!r} has no stored log."
        # Both off the same object, read once: the revision is what says which
        # messages the ordinals of an accepted ref name.
        messages = session.messages
        revision = session.revision

        # Every tag in every string: a model that pastes a line holding two refs
        # asked for both.
        wanted: list[SourceRef | str] = []
        for raw in refs:
            found = SourceRef.find_all(str(raw)) or SourceRef.find_all(_bracketed(str(raw)))
            wanted.extend(found or [str(raw)])
        answered = wanted[:_MAX_REFS]

        sections: list[str] = []
        for item in answered:
            section, spent = self._resolve(item, messages, revision, mode=mode, budget=budget, offset=offset)
            sections.append(section)
            budget -= spent
            if budget <= 0:
                remaining = len(answered) - len(sections)
                if remaining > 0:
                    sections.append(
                        f"[{remaining} more ref(s) not read: the token budget for this call is spent. "
                        "Ask again for them, or raise max_tokens.]"
                    )
                break
        if len(wanted) > _MAX_REFS:
            sections.append(
                f"[{len(wanted) - _MAX_REFS} ref(s) beyond the {_MAX_REFS} this call answers were ignored.]"
            )
        return "\n\n".join(sections)

    # ------------------------------------------------------------------

    def _resolve(
        self,
        item: SourceRef | str,
        messages: Sequence[Mapping[str, Any]],
        revision: SessionRevision,
        *,
        mode: str,
        budget: int,
        offset: int = 0,
    ) -> tuple[str, int]:
        """One ref: its entries and what reading them cost."""
        if isinstance(item, str):
            return (f"[{item!r} is not a ref; expected the [[ref store/scope#n]] form]", 0)
        ref = item
        if ref.store != SESSION_REF_STORE:
            return (f"[{ref.render()}: the {ref.store!r} store is not retrievable here]", 0)
        if not belongs_to(ref, revision):
            log_id, _, generation = ref.scope.partition("@")
            if log_id == revision.log_id and generation.isdigit():
                if int(generation) < revision.generation:
                    return (
                        f"[{ref.render()}: minted before this conversation's log was rewritten (an undo, "
                        "or another copy of the session saving over it); the entries it named may now "
                        "hold other messages, so it is not read back]",
                        0,
                    )
                return (
                    f"[{ref.render()}: names generation {generation} of this conversation's log, which is "
                    f"at generation {revision.generation} here; no stored copy of that generation can "
                    "be read back]",
                    0,
                )
            return (
                f"[{ref.render()}: not part of this conversation's log. A ref from before a /new, "
                "or from another session, names nothing that can be read back]",
                0,
            )
        if not 0 <= ref.lo <= ref.hi < len(messages):
            return (f"[{ref.render()}: out of range; this log holds {len(messages)} message(s)]", 0)

        rendered: list[str] = []
        spent = 0
        for index in range(ref.lo, ref.hi + 1):
            if spent >= budget:
                rendered.append(f"[entries {index}-{ref.hi} not read: the token budget for this call is spent]")
                break
            entry, cost = _render_entry(
                SourceRef(ref.store, ref.scope, index, index),
                messages[index],
                mode=mode,
                budget=budget - spent,
                offset=offset,
            )
            rendered.append(entry)
            spent += cost
        return ("\n\n".join(rendered), spent)


def _clamp_offset(raw: Any) -> int:
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return 0


def _clamp_budget(raw: Any) -> int:
    try:
        asked = int(raw)
    except (TypeError, ValueError):
        return _DEFAULT_MAX_TOKENS
    return max(_MIN_MAX_TOKENS, min(_MAX_MAX_TOKENS, asked))


def _render_entry(
    ref: SourceRef,
    message: Mapping[str, Any],
    *,
    mode: str,
    budget: int,
    offset: int = 0,
) -> tuple[str, int]:
    """One message, headed by what it is and how much of it is here.

    ``offset`` (exact mode) starts the body that many characters in, so an entry
    larger than one call's budget is read in turns; a cut answer names the
    ``next_offset`` to ask for.
    """
    body = _body(message.get("content"))
    header = [ref.render(), f"role={message.get('role', '?')}"]
    if name := message.get("name"):
        header.append(f"name={name}")
    header.append(f"chars={len(body):,}")
    if is_truncated(message.get("content")):
        # Written under the old 16000-character cap. The rest was never stored,
        # so saying so is the only honest answer.
        header.append("truncated=true")

    start = min(offset, len(body)) if mode == "exact" else 0
    if start:
        header.append(f"offset={start:,}")
    rest = body[start:]
    shown = rest if mode == "exact" else _snippet(body)
    whole = f"{' '.join(header)}\n{shown}"
    cost = _tokens(whole)
    if cost <= budget and shown == rest:
        return whole, cost
    # The header that will head a cut body, at its longest, is what the body
    # has to leave room for.
    widest = [*header, f"shown={len(body):,}/{len(body):,} chars", f"next_offset={len(body):,}"]
    shown = _fit(shown, budget - _tokens(f"{' '.join(widest)}\n"))
    labels = [*header, f"shown={len(shown):,}/{len(body):,} chars"]
    if mode == "exact" and start + len(shown) < len(body):
        labels.append(f"next_offset={start + len(shown)}")
    text = f"{' '.join(labels)}\n{shown}"
    return text, _tokens(text)


def _tokens(text: str) -> int:
    return estimate_message_tokens({"role": "tool", "content": text})


def _body(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return json.dumps(content, ensure_ascii=False)


def _snippet(body: str) -> str:
    if len(body) <= _SNIPPET_HEAD_CHARS + _SNIPPET_TAIL_CHARS:
        return body
    elided = len(body) - _SNIPPET_HEAD_CHARS - _SNIPPET_TAIL_CHARS
    return f"{body[:_SNIPPET_HEAD_CHARS]}\n\n[... {elided:,} chars, ask again with mode=exact ...]\n\n{body[-_SNIPPET_TAIL_CHARS:]}"


def _fit(body: str, budget: int) -> str:
    """``body`` cut to at most ``budget`` tokens.

    The characters-per-token ratio is taken from this body rather than assumed,
    so a Chinese stack trace and an English one are both cut where they
    actually reach the budget; the estimate is then checked, because a cut can
    land the count a little over.
    """
    if budget <= 0 or not body:
        return ""
    cost = _tokens(body)
    if cost <= budget:
        return body
    per_token = max(1.0, len(body) / max(1, cost))
    keep = int(budget * per_token)
    while keep > 0 and (over := _tokens(body[:keep]) - budget) > 0:
        keep -= max(1, int(over * per_token))
    return body[: max(0, keep)]


_BARE_REF = re.compile(r"(?:\bref\s+)?(?P<body>[\w.-]+/[^#\]\s\[]+#\d+(?:-\d+)?)")


def _bracketed(raw: str) -> str:
    """``raw`` with every bare ``store/scope#n`` respelled as a full tag.

    A model that copies the address out of a tag and leaves the brackets behind
    named the entry it wants; refusing it on the spelling cost one measured run
    the answer, because the model took the refusal as "not retrievable".
    """
    return _BARE_REF.sub(lambda m: f"[[ref {m['body']}]]", raw)


__all__ = ["ContextRetrieveTool"]
