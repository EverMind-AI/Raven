"""How one OpenAI-compatible endpoint's ``reasoning_steps`` become turn events.

A deep-research endpoint reports its steps in a response field rather than in a
notification, so this reads the field: the step's own tool name, its payload,
and its result with the transport's wrapping removed. Sibling to
:mod:`raven.acp_client.acp_dialects`, for a transport that has no
notifications to read.

Two response shapes, one accumulator. Buffered, every step arrives whole;
streamed, a ``thinking`` step arrives as token fragments across many frames
while an action step arrives whole in one. Feeding both through
:meth:`OpenAIStepReader.feed_delta` is what makes the two paths produce one
event list -- which is the property the tests pin, and the only reason a live
view and a settled record agree.

Names and argument keys are the endpoint's own. Mapping them into raven's
vocabulary is the read boundary's job
(:mod:`raven.agent.subagent.tool_vocabulary`): presentation is recoverable from
provenance, and provenance is not recoverable from presentation.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from loguru import logger

from raven.agent.subagent.backends import turn_rows

_THINKING = "thinking"
_THOUGHT_KEY = "thought"

# Per measured type: which payload key carries the result half, and how to read
# it. A type absent here is reported as a call with no result rather than split
# on a guess -- see the module docstring.
_RESULT_KEY = {
    "web_search": "search_results",
    "fetch_url_content": "snippet",
}

# A result row is recorded only when the endpoint sent something to record. A
# result key that never arrived, and one holding null, are both unreadable: the
# call is left unpaired rather than answered. Reading null as a failure would
# invent a provider failure exactly as recording it as success invented a
# provider answer -- a failed row needs a failure the endpoint reported, which
# for the one type with a status envelope means `success: false` or a non-empty
# error. An empty list is different and is kept: it is how both measured types
# say "no matches", which is a result.
_MISSING = object()


def _text_of(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)


def _unwrap_fetch(snippet: Any) -> tuple[str, bool]:
    """``fetch_url_content``'s result: a JSON string inside the payload.

    Degrades to the raw snippet when it will not parse: a page that came back is
    worth keeping even when its envelope is not the measured one.
    """
    if not isinstance(snippet, str):
        return _text_of(snippet), True
    try:
        inner = json.loads(snippet)
    except ValueError:
        return snippet, True
    if not isinstance(inner, dict):
        return snippet, True
    ok = inner.get("success") is not False and not inner.get("error")
    return _text_of(inner.get("extracted_info") or inner.get("error") or ""), ok


def _now() -> str:
    """When this reader saw the step, which is the only clock it has.

    The endpoint stamps nothing, so a row's time is its arrival. Read here rather
    than passed in because a streamed step arrives on its own frame and a
    buffered one arrives with the whole response, and the caller cannot tell the
    difference by the time it holds the list.
    """
    return datetime.now().isoformat()


class OpenAIStepReader:
    """One call's ``reasoning_steps``, accumulated into turn events."""

    def __init__(self) -> None:
        self._events: list[dict[str, Any]] = []
        self._thought: list[str] = []
        self._thought_at: str | None = None
        self._calls = 0

    def feed_steps(self, steps: Any) -> None:
        """The buffered response's whole list, in order."""
        if not isinstance(steps, list):
            return
        for step in steps:
            self.feed_delta(step)

    def feed_delta(self, step: Any) -> None:
        """One step, whole or fragmentary.

        A ``thinking`` fragment appends to the open thought; anything else closes
        it. Nothing here raises: a step the reader cannot make sense of costs its
        own rows, never the turn.
        """
        if not isinstance(step, dict):
            return
        kind = step.get("type")
        if not isinstance(kind, str) or not kind:
            return
        if kind == _THINKING:
            piece = step.get(_THOUGHT_KEY)
            if isinstance(piece, str) and piece:
                if not self._thought:
                    self._thought_at = _now()
                self._thought.append(piece)
            return
        self._close_thought()
        payload = step.get(kind)
        try:
            self._append_action(kind, payload)
        except Exception as exc:  # noqa: BLE001 - an audit trail may not break a run
            logger.debug("openai step {!r} could not be read ({})", kind, exc)

    def events(self) -> list[dict[str, Any]]:
        """The events so far, with any open thought closed.

        Non-destructive, because a streamed run publishes on every frame: the
        open thought is appended to the answer rather than consumed, so the next
        frame still extends it.
        """
        out = list(self._events)
        if self._thought:
            out.append(turn_rows.thought("".join(self._thought), at=self._thought_at))
        return out

    def _close_thought(self) -> None:
        if self._thought:
            self._events.append(turn_rows.thought("".join(self._thought), at=self._thought_at))
            self._thought = []
            self._thought_at = None

    def _append_action(self, kind: str, payload: Any) -> None:
        self._calls += 1
        call_id = f"mi-{self._calls}"
        fields = dict(payload) if isinstance(payload, dict) else {"argument": _text_of(payload)}
        result_key = _RESULT_KEY.get(kind)
        raw_result = fields.pop(result_key, _MISSING) if result_key else _MISSING
        self._events.append(
            turn_rows.call(
                id=call_id,
                name=kind,
                arguments_json=json.dumps(fields, ensure_ascii=False, default=str),
                at=_now(),
            )
        )
        if raw_result is _MISSING:
            return
        if raw_result is None:
            logger.debug("openai step {!r} reported a null result; leaving the call unpaired", kind)
            return
        if kind == "fetch_url_content":
            text, ok = _unwrap_fetch(raw_result)
        else:
            text, ok = _text_of(raw_result), True
        self._events.append(turn_rows.result(id=call_id, text=text, ok=ok, at=_now()))


__all__ = ["OpenAIStepReader"]
