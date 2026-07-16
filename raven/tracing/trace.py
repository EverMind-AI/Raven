"""Public instrumentation API — the standard adopters call.

See ``docs/TRACING_STANDARD_API.md``. A thin facade over ``context`` / ``spans`` /
``store``: open a span on enter, finalize + emit on exit. Nesting is automatic
via contextvars (a span opened inside another becomes its child).

Guarantees (so an adopter is safe):
- **No-op when disabled** — yields a no-op handle, no I/O.
- **Never breaks the caller** — swallows tracing-internal failures; re-raises the
  application's own exception (after recording ``status=ERROR``).
- **Import-safe** — importing and calling never requires config to be present.
"""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any, Iterator

from . import config
from . import context as _ctx
from . import spans as _spans

_log = logging.getLogger("raven.tracing")

# name domain -> coarse kind (span.type). Custom nodes pass kind= explicitly.
_KIND_BY_DOMAIN = {
    "session": "session",
    "llm": "model",
    "tool": "tool",
    "subagent": "subagent",
    "skill": "skill",
    "memory": "memory",
    "plugin": "plugin",
    "tracing": "plugin",
}


def _derive_kind(name: str) -> str:
    domain = name.split(".", 1)[0]
    return _KIND_BY_DOMAIN.get(domain, domain or "internal")


class Span:
    """Handle for an open span. Every method is no-throw and returns ``self``."""

    __slots__ = (
        "name",
        "kind",
        "trace_id",
        "span_id",
        "_parent",
        "_start",
        "_attrs",
        "_events",
        "_status_code",
        "_status_message",
        "_session_key",
        "_channel",
        "_chat_id",
        "_perf0",
        "_cancelled",
        "_source",
    )

    def __init__(self, name, kind, *, trace_id, span_id, parent, session_key, channel, chat_id, start, source=None):
        self.name = name
        self.kind = kind
        self.trace_id = trace_id
        self.span_id = span_id
        self._parent = parent
        self._session_key = session_key
        self._channel = channel
        self._chat_id = chat_id
        self._start = start
        self._source = source
        self._perf0 = time.monotonic()
        self._cancelled = False
        self._attrs: dict[str, Any] = {}
        self._events: list[dict[str, Any]] = []
        self._status_code = "OK"
        self._status_message = ""

    def set(self, attributes: dict[str, Any] | None = None, **kw) -> "Span":
        """Merge attributes onto the span.

        Standard keys are fully-qualified and dotted (``llm.provider``,
        ``tool.name`` — see the semantic conventions doc), so pass them as a
        mapping: ``s.set({"llm.provider": p})``. Bare keywords (``s.set(foo=1)``)
        are accepted for convenience but are stored verbatim (no auto-namespacing).
        """
        if attributes:
            self._attrs.update(attributes)
        if kw:
            self._attrs.update(kw)
        return self

    def artifact(self, key: str, payload: Any, *, kind: str = "json") -> "Span":
        """Persist a large payload out-of-line; attach ``<key>.artifact_*`` + preview."""
        try:
            meta = {"traceId": self.trace_id, "sessionKey": self._session_key}
            art = _spans.persist_artifact(key, meta, payload, label=key)
            self._attrs.update(_spans.artifact_attributes(key, art))
        except Exception:  # noqa: BLE001 — tracing must never break the host
            _log.debug("tracing: artifact(%s) failed", key, exc_info=True)
        return self

    def event(self, name: str) -> "Span":
        self._events.append({"time": _spans.now_iso(), "name": name})
        return self

    def error(self, exc: BaseException | str) -> "Span":
        self._status_code = "ERROR"
        self._status_message = repr(exc) if isinstance(exc, BaseException) else str(exc)
        return self

    def retype(self, name: str, kind: str | None = None) -> "Span":
        """Change the span's name/kind at runtime (before emit).

        For spans whose true type is only known after the call — e.g. a ``tool.call``
        that turns out to be a ``skill.read`` (a ``use_skill`` / ``read_skill`` tool,
        or a ``read_file`` of a SKILL.md). Nesting is unaffected (span_id is fixed).
        """
        self.name = name
        if kind:
            self.kind = kind
        return self

    @property
    def invocation_source(self) -> str | None:
        """Name of the nearest enclosing non-model span — the operation this span
        runs on behalf of (``None`` at the root). Lets a model call self-label its
        purpose without the extractor walking the tree."""
        return self._source

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self._perf0) * 1000)

    def cancel(self) -> "Span":
        """Drop this span — nothing is emitted on close. For conditional spans
        (e.g. skill.inject only when something was actually injected)."""
        self._cancelled = True
        return self

    def checkpoint(self) -> "Span":
        """Emit the span's current (in-progress) state now, without closing it.

        Same ``span_id`` as the final emit — the viewer dedups by id and keeps
        the last write. Used by long root spans (a turn) so mid-flight children
        already have a root to group under while the turn is still open.
        """
        try:
            _spans.emit(
                _spans.build_span(
                    self.name,
                    self.kind,
                    trace_id=self.trace_id,
                    span_id=self.span_id,
                    parent_span_id=self._parent,
                    session_key=self._session_key,
                    channel=self._channel,
                    chat_id=self._chat_id,
                    start_time=self._start,
                    end_time=_spans.now_iso(),
                    status_code=self._status_code,
                    status_message=self._status_message,
                    attributes=self._attrs,
                    events=self._events,
                )
            )
        except Exception:  # noqa: BLE001
            _log.debug("tracing: checkpoint(%s) failed", self.name, exc_info=True)
        return self


class _NoopSpan:
    """Returned when tracing is disabled or an internal open failed."""

    trace_id = ""
    span_id = ""
    name = ""
    invocation_source = None

    def set(self, *_a, **_k):
        return self

    def artifact(self, *_a, **_k):
        return self

    def event(self, *_a):
        return self

    def error(self, *_a):
        return self

    def retype(self, *_a, **_k):
        return self

    def elapsed_ms(self):
        return 0

    def checkpoint(self, *_a, **_k):
        return self

    def cancel(self, *_a, **_k):
        return self


@contextlib.contextmanager
def span(
    name: str,
    attributes: dict[str, Any] | None = None,
    *,
    kind: str | None = None,
    session_key: str | None = None,
    channel: str | None = None,
    chat_id: str | None = None,
    detached: bool = False,
    **kw,
) -> Iterator[Any]:
    """Open a span for ``name`` (``<domain>.<verb>``). Yields a :class:`Span` handle.

    ``attributes`` is a mapping of fully-qualified dotted keys (the standard form,
    e.g. ``{"llm.provider": p, "llm.model": m}``); ``**kw`` accepts bare keys for
    convenience. Children opened inside the ``with`` block nest under this span.
    On exception the span is marked ``ERROR`` and the exception re-raised unchanged.
    """
    if not config.enabled():
        yield _NoopSpan()
        return

    try:
        cur = _ctx.current()
        trace_id = cur.trace_id if cur else _ctx.new_trace_id()
        parent = cur.parent_span_id if cur else None
        # Passed session identity seeds a root span (the turn); otherwise inherit
        # from the active context so children carry the turn's identity.
        session_key = session_key if session_key is not None else (cur.session_key if cur else None)
        channel = channel if channel is not None else (cur.channel if cur else None)
        chat_id = chat_id if chat_id is not None else (cur.chat_id if cur else None)
        span_id = _ctx.new_span_id()
        handle = Span(
            name,
            kind or _derive_kind(name),
            trace_id=trace_id,
            span_id=span_id,
            parent=parent,
            session_key=session_key,
            channel=channel,
            chat_id=chat_id,
            start=_spans.now_iso(),
            source=cur.source if cur else None,
        )
        handle.set(attributes, **kw)
        # A detached span is a leaf marker: it does NOT become the active parent,
        # so work done inside it attaches to ITS parent, not to it. Required for
        # cancellable spans (e.g. skill.inject) — otherwise a child that opened
        # before the cancel would dangle off a span that never gets emitted.
        token = (
            None
            if detached
            else _ctx.push(
                trace_id=trace_id,
                span_id=span_id,
                name=handle.name,
                kind=handle.kind,
                session_key=session_key,
                channel=channel,
                chat_id=chat_id,
            )
        )
    except Exception:  # noqa: BLE001 — open must never break the host
        _log.debug("tracing: span(%s) open failed", name, exc_info=True)
        yield _NoopSpan()
        return

    try:
        yield handle
    except Exception as exc:
        handle.error(exc)
        raise
    finally:
        try:
            if token is not None:
                _ctx.reset(token)
            if not handle._cancelled:
                _spans.emit(
                    _spans.build_span(
                        handle.name,
                        handle.kind,
                        trace_id=handle.trace_id,
                        span_id=handle.span_id,
                        parent_span_id=handle._parent,
                        session_key=handle._session_key,
                        channel=handle._channel,
                        chat_id=handle._chat_id,
                        start_time=handle._start,
                        end_time=_spans.now_iso(),
                        status_code=handle._status_code,
                        status_message=handle._status_message,
                        attributes=handle._attrs,
                        events=handle._events,
                    )
                )
        except Exception:  # noqa: BLE001
            _log.debug("tracing: span(%s) emit failed", name, exc_info=True)


def instrument(name: str, *, kind: str | None = None, detached: bool = False, seed=None, on_open=None, extract=None):
    """Decorator: wrap an async method so each call emits a ``name`` span.

    The adopter's integration surface — annotate a method, leave its body
    untouched::

        @trace.instrument("llm.call", extract=semconv.llm_call)
        async def chat_with_retry(self, ...): ...

    Hooks (all optional, all get ``bound_args`` = the call's arguments by name):

    - ``seed(bound) -> dict`` — returns ``session_key`` / ``channel`` / ``chat_id``
      to open the span with, seeding a *root* span (a turn) whose identity every
      child inherits.
    - ``on_open(span, bound)`` — runs right after open, before the body. Used to
      record input + ``span.checkpoint()`` an in-progress root for live viewing.
    - ``extract(span, bound, result, exc)`` — runs in ``finally`` (so input is
      captured even on error); fills final attributes/artifacts. ``result`` is the
      return (``None`` on error); ``exc`` is the exception (``None`` on success).

    All tracing work is no-throw and no-op when disabled — the wrapped method's
    behavior is never altered.
    """
    import functools
    import inspect

    def decorate(func):
        sig = inspect.signature(func)

        def _bind(args, kwargs):
            b = sig.bind(*args, **kwargs)
            b.apply_defaults()
            return b.arguments

        def _seed(args, kwargs) -> dict:
            if seed is None:
                return {}
            try:
                return seed(_bind(args, kwargs)) or {}
            except Exception:  # noqa: BLE001
                _log.debug("tracing: seed for %s failed", name, exc_info=True)
                return {}

        def _open(s, args, kwargs) -> None:
            if on_open is not None:
                try:
                    on_open(s, _bind(args, kwargs))
                except Exception:  # noqa: BLE001
                    _log.debug("tracing: on_open for %s failed", name, exc_info=True)

        def _close(s, args, kwargs, result, exc) -> None:
            if extract is not None:
                try:
                    extract(s, _bind(args, kwargs), result, exc)
                except Exception:  # noqa: BLE001 — extraction must not break the host
                    _log.debug("tracing: extract for %s failed", name, exc_info=True)

        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def awrapper(*args, **kwargs):
                if not config.enabled():
                    return await func(*args, **kwargs)
                with span(name, kind=kind, detached=detached, **_seed(args, kwargs)) as s:
                    _open(s, args, kwargs)
                    result = exc = None
                    try:
                        result = await func(*args, **kwargs)
                        return result
                    except BaseException as e:  # noqa: BLE001 — record + re-raise
                        exc = e
                        raise
                    finally:
                        _close(s, args, kwargs, result, exc)

            return awrapper

        @functools.wraps(func)
        def swrapper(*args, **kwargs):
            if not config.enabled():
                return func(*args, **kwargs)
            with span(name, kind=kind, detached=detached, **_seed(args, kwargs)) as s:
                _open(s, args, kwargs)
                result = exc = None
                try:
                    result = func(*args, **kwargs)
                    return result
                except BaseException as e:  # noqa: BLE001 — record + re-raise
                    exc = e
                    raise
                finally:
                    _close(s, args, kwargs, result, exc)

        return swrapper

    return decorate


def current() -> Any | None:
    """The active trace context (or None). Exposed for adopters that need it."""
    return _ctx.current()


def enabled() -> bool:
    return config.enabled()
