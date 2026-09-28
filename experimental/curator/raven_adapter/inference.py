"""Host-supplied text inference for strategy operations, with a per-turn budget."""

import asyncio
from inspect import Parameter, iscoroutinefunction, signature

from pydantic import JsonValue

from ..harness.declaration import parse_as
from .materialize import load_factory


class Inference:
    """An injected async infer(messages) callable returning model text.

    Factories may accept it as a keyword-only dependency. It accepts native
    role/content message dictionaries, with no tools or provider credentials.
    Calls are bounded per worker turn (or construction scope before the first
    turn), using the configured worker provider. Model failures and truncation
    are errors; the strategy owns parsing and validating its semantic result.
    """

    def __init__(self, provider, recorder, *, max_calls=4, timeout=90):
        self.provider, self.recorder = provider, recorder
        self.max_calls, self.timeout = max_calls, timeout
        self.turn_id = object()
        self.calls = 0

    async def __call__(self, messages):
        if self.turn_id != self.recorder.turn_id:
            self.turn_id, self.calls = self.recorder.turn_id, 0
        if self.calls >= self.max_calls:
            raise RuntimeError("strategy inference budget exhausted")
        messages = parse_as(list[dict[str, JsonValue]], messages, strict=True)
        if not messages or any(
            row.get("role") not in {"system", "user", "assistant"}
            or not isinstance(row.get("content"), str)
            or set(row) - {"role", "content"}
            for row in messages
        ):
            raise ValueError("strategy inference requires text role/content messages without tool calls")
        self.calls += 1
        self.recorder.add("strategy.inference", call=self.calls, messages=messages)
        try:
            async with asyncio.timeout(self.timeout):
                response = await self.provider.chat_with_retry(messages=messages)
            if (
                response.finish_reason in {"error", "length"}
                or response.truncated
                or response.tool_calls
                or not response.content
            ):
                raise ValueError("strategy inference returned an error, truncation, tool call or empty result")
        except Exception as exc:
            self.recorder.add("strategy.inference.error", error=f"{type(exc).__name__}: {exc}")
            raise
        return response.content


HOST_DEPENDENCIES = ("infer", "plan")


def supplied(factory, available) -> dict:
    """The keyword-only host dependencies a factory declares, taken from those this host offers here."""
    parameters = signature(factory).parameters
    kwargs = {}
    for name in HOST_DEPENDENCIES:
        if name not in parameters:
            continue
        if parameters[name].kind is not Parameter.KEYWORD_ONLY or available.get(name) is None:
            raise TypeError(f"{name} requires a host-supplied keyword-only dependency")
        kwargs[name] = available[name]
    return kwargs


def strategy_factory(reference, package, *args, protocol, infer=None, plan=None):
    """Construct an explicitly declared strategy with the existing dependencies."""
    factory = load_factory(reference.root, package)
    if iscoroutinefunction(factory):
        raise TypeError("strategy factories construct inert objects synchronously")
    kwargs = supplied(factory, {"infer": infer, "plan": plan})
    signature(factory).bind(*args, **kwargs)
    instance = factory(*args, **kwargs)
    if protocol not in type(instance).__mro__:
        raise TypeError(f"{reference.root} must return an instance explicitly inheriting {protocol.__name__}")
    return instance
