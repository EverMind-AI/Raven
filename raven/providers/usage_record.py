"""Account every model call once, at the provider seam.

The usage file (``<raven home>/telemetry/usage-*.jsonl``) is written by
``UsageTracker``, and the tracker only hears the calls somebody hands it. For a
long time that was the turn loop and image generation alone, while some forty
other callers -- the heartbeat, the sentinel, memory consolidation, session
titles, the permission judge, the playbook planner, the in-process sub-agent's
own loop -- called a provider directly and were never billed anywhere.

So the recording happens here instead, around the calls themselves:
:func:`instrument` wraps every provider class's ``chat``, ``chat_with_retry``
and ``chat_stream`` (``raven.providers.base.LLMProvider`` applies it to each
subclass), and whatever sink :func:`install` was given hears each call once.

Once, in three senses:

- **Nested wrappers record at the outermost entry.** ``ResolvingProvider``,
  ``PerModelProvider`` and ``LazyProvider`` delegate to an inner provider, and
  the base retry ladder calls ``chat`` for every attempt; the outermost call
  marks the context as owned, and every inner entry passes straight through.
  The outermost call is the one whose answer the caller received, so a retried
  or fallen-back call is one row, under the response that came back -- the same
  unit the turn loop has always recorded.
- **A caller that records the call itself says so.** A caller that makes one
  call and records it can run it inside :func:`recorded_by_caller` and nothing
  here records it a second time. The turn loop does *not* do that: one Action
  decision may make several calls (best-of-n, a critic pass), and a claim over
  the whole decision would lose every call but the one returned. The seam
  records each of them instead, and the loop checks :func:`hears` and skips its
  own row when that would copy one of them.
- **A stream is one row, recorded when it is exhausted.** Usage and the finish
  reason may arrive on different deltas, so neither alone is the trigger. The
  wrapper nearest the wire owns the stream: it marks the deltas it passes on,
  and a wrapper that finds the first delta already marked is yielding an inner
  provider's stream and records nothing. A stream abandoned before its end is
  not recorded -- its usage never arrived.

A call that failed before reaching a model -- an error response carrying no
usage -- is not recorded: nothing was spent, and a row of zeros would read as a
cheap call. A call that raised has no response to record.

Until :func:`install` is called nothing is recorded, which is what an entry
point that assembles no runtime (``raven doctor``, a unit test) wants. The sink
is process-wide by the same necessity as ``prompt_cache.set_ttl``: providers are
rebuilt on a model switch, so nothing fixed at one construction survives.
"""

from __future__ import annotations

import functools
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, AsyncIterator, Awaitable, Callable, Iterator

from loguru import logger

Sink = Callable[[dict[str, Any], Any], Awaitable[None]]

_sink: Sink | None = None

# True while an outer frame accounts for the model call being made: an outer
# provider entry, or a caller that records the call itself.
_owned: ContextVar[bool] = ContextVar("llm_call_owned", default=False)

_RECORDED_MARK = "_usage_recorded"
_WRAPPED_MARK = "__usage_recorded_wrapper__"


def install(sink: Sink | None) -> None:
    """Send every unowned model call to ``sink``; ``None`` stops recording.

    ``sink`` takes the same ``(response, usage)`` pair a TokenStrategy's
    ``after_llm_call`` does, so a ``StrategyRegistry`` method is one.
    """
    global _sink
    _sink = sink


def hears(registry: Any) -> bool:
    """Whether the calls this module records reach ``registry``.

    True when the installed sink is that registry's own ``after_llm_call``,
    which is what the runtime assembly installs for the loop it builds.
    """
    return _sink is not None and _sink == getattr(registry, "after_llm_call", None)


@contextmanager
def recorded_by_caller() -> Iterator[None]:
    """Mark the model calls made inside this block as recorded by the caller."""
    token = _owned.set(True)
    try:
        yield
    finally:
        _owned.reset(token)


def _snapshot(usage: dict[str, Any] | None, model: str | None) -> Any:
    from raven.contracts.token_strategy import UsageSnapshot
    from raven.providers.usage import normalize_usage

    counts = normalize_usage(usage)
    counts.pop("total_tokens")
    # The session is left to the recorder, which reads the one the running turn
    # bound (``usage_context``) -- the same fallback the loop's own rows use.
    return UsageSnapshot(model=model or "", **counts)


async def _record(response: dict[str, Any], model: str | None) -> None:
    sink = _sink
    if sink is None:
        return
    usage = response.get("usage")
    if response.get("finish_reason") == "error" and not usage:
        return
    try:
        await sink(response, _snapshot(usage, model))
    except Exception as exc:  # noqa: BLE001 - accounting must never fail the call it accounts
        logger.warning("usage record failed for model={}: {}", model, exc)


def _model_of(provider: Any, args: tuple[Any, ...], kwargs: dict[str, Any]) -> str | None:
    model = kwargs.get("model")
    if model is None and len(args) >= 3:
        model = args[2]
    if model:
        return str(model)
    try:
        return provider.get_default_model()
    except Exception:  # noqa: BLE001 - a name for the row, not a reason to fail
        return None


def _wrap_call(method: Callable[..., Awaitable[Any]]) -> Callable[..., Awaitable[Any]]:
    @functools.wraps(method)
    async def call(self: Any, *args: Any, **kwargs: Any) -> Any:
        if _owned.get():
            return await method(self, *args, **kwargs)
        token = _owned.set(True)
        try:
            response = await method(self, *args, **kwargs)
        finally:
            _owned.reset(token)
        await _record(
            {
                "content": getattr(response, "content", None),
                "finish_reason": getattr(response, "finish_reason", None),
                "usage": getattr(response, "usage", None),
            },
            _model_of(self, (self, *args), kwargs),
        )
        return response

    setattr(call, _WRAPPED_MARK, True)
    return call


def _mark(delta: Any) -> None:
    # Best effort: a stream of something that takes no attribute (a test double
    # yielding strings) carries no usage either, and accounting must never
    # fail the call it accounts.
    try:
        object.__setattr__(delta, _RECORDED_MARK, True)
    except (AttributeError, TypeError):
        pass


def _wrap_stream(method: Callable[..., AsyncIterator[Any]]) -> Callable[..., AsyncIterator[Any]]:
    @functools.wraps(method)
    async def stream(self: Any, *args: Any, **kwargs: Any) -> AsyncIterator[Any]:
        # The owned flag is read, never set, in here: an async generator's body
        # runs in whichever context steps it, so a value set across a yield
        # would leak into the consumer and could not be reset once it closed.
        # The mark on the deltas is what keeps a wrapper from recording twice.
        model = _model_of(self, (self, *args), kwargs)
        deltas = method(self, *args, **kwargs)
        owner: bool | None = None
        usage: dict[str, Any] | None = None
        finish: str | None = None
        try:
            async for delta in deltas:
                if owner is None:
                    owner = not getattr(delta, _RECORDED_MARK, False)
                if owner:
                    _mark(delta)
                    usage = getattr(delta, "usage", None) or usage
                    finish = getattr(delta, "finish_reason", None) or finish
                yield delta
        finally:
            await deltas.aclose()
        # Reached only when the stream ran to its end, in the context of the
        # consumer's last step -- which is where a caller's own claim is read.
        if owner and (usage or finish) and not _owned.get():
            await _record({"content": None, "finish_reason": finish, "usage": usage}, model)

    setattr(stream, _WRAPPED_MARK, True)
    return stream


def instrument(cls: type) -> None:
    """Wrap the model-call methods ``cls`` itself defines. Idempotent."""
    for name, wrap in (("chat", _wrap_call), ("chat_with_retry", _wrap_call), ("chat_stream", _wrap_stream)):
        method = cls.__dict__.get(name)
        if method is None or getattr(method, _WRAPPED_MARK, False) or getattr(method, "__isabstractmethod__", False):
            continue
        setattr(cls, name, wrap(method))


__all__ = ["hears", "install", "instrument", "recorded_by_caller"]
