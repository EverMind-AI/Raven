"""The provider half of the LLM relay: a LiteLLM provider whose model calls the host makes.

See :mod:`raven.providers.relay` for the offer and the transport; this module is
what the provider factory builds in a process that has one.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

from raven.contracts.llm_provider import ChatDelta, GenerationSettings, LLMResponse
from raven.providers import relay_wire
from raven.providers.base import LLMProvider
from raven.providers.binding import active_binding
from raven.providers.litellm_shape import LiteLLMShape
from raven.providers.relay import (
    RelayFaultError,
    RelayUnavailableError,
    active,
    connect,
    give_up,
    read_answer,
    request_frame,
)


class RelayProvider(LiteLLMShape):
    """A LiteLLM provider whose model calls the host makes.

    Constructed with exactly the arguments a :class:`LiteLLMProvider` takes, and
    answering every question from the same shared code; what it sends to the
    host is that construction, as it stands at the moment of the call (callers
    adjust ``generation``, ``model_overrides`` and the cache flag after building
    a provider), plus the call's own arguments.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._local: LLMProvider | None = None

    # Answered by the provider built here once the host could not be reached:
    # it is the one sending then. Built from this construction, it answers the
    # same, but a wrapper asks the provider that has the wire.
    def wire_model_id(self, model: str) -> str:
        return self._local.wire_model_id(model) if self._local is not None else super().wire_model_id(model)

    def can_serve(self, model: str) -> bool:
        return self._local.can_serve(model) if self._local is not None else super().can_serve(model)

    def emits_unparsed_reasoning(self) -> bool:
        return self._local.emits_unparsed_reasoning() if self._local is not None else super().emits_unparsed_reasoning()

    def spec(self) -> dict[str, Any]:
        """What the host builds its provider from: this one's construction, as it stands now."""
        return {
            "api_key": self.api_key,
            "api_base": self.api_base,
            "default_model": self.default_model,
            "extra_headers": dict(self.extra_headers or {}),
            "provider_name": self._provider_name or None,
            "disable_auto_cache_control": bool(self.disable_auto_cache_control),
            "extra_body": dict(self.extra_body or {}) or None,
            "model_overrides": dict(self.model_overrides or {}) or None,
            "tls_verify": bool(self.tls_verify),
            "unparsed_reasoning": self._unparsed_reasoning,
            "generation": relay_wire.encode(getattr(self, "generation", None) or GenerationSettings()),
        }

    def _locally(self) -> LLMProvider:
        """The provider this process builds for itself once the host cannot be reached.

        Kept in step with this one on every call, for the same reason the spec
        is read at the call: callers adjust these after building a provider.
        """
        if self._local is None:
            from raven.providers.litellm_provider import LiteLLMProvider

            spec = self.spec()
            spec.pop("generation")
            self._local = LiteLLMProvider(**spec)
        local = self._local
        local.generation = getattr(self, "generation", None) or GenerationSettings()
        for attr in ("model_overrides", "disable_auto_cache_control", "extra_body", "extra_headers"):
            setattr(local, attr, getattr(self, attr))
        return local

    def _call(self, **args: Any) -> dict[str, Any]:
        # The running turn's window travels with the call: the host sizes the
        # output ceiling under it, as this process would have.
        bound = active_binding()
        return {
            "spec": self.spec(),
            "args": relay_wire.encode(args, sentinel=self._SENTINEL),
            "window": bound.context_window if bound is not None else None,
        }

    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: int | None = None,
        temperature: float = 0.7,
        reasoning_effort: str | None = None,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> LLMResponse:
        args = {
            "messages": messages,
            "tools": tools,
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "reasoning_effort": reasoning_effort,
            "tool_choice": tool_choice,
        }
        if active():
            try:
                reader, writer = await connect()
            except RelayUnavailableError as exc:
                give_up(exc)
            else:
                try:
                    await relay_wire.write_frame(writer, request_frame("chat", **self._call(**args)))
                    return read_answer(await relay_wire.read_frame(reader))
                finally:
                    writer.close()
        return await self._locally().chat(**args)

    async def chat_stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        model: str | None = None,
        max_tokens: object = LLMProvider._SENTINEL,
        temperature: object = LLMProvider._SENTINEL,
        reasoning_effort: object = LLMProvider._SENTINEL,
        tool_choice: str | dict[str, Any] | None = None,
    ) -> AsyncIterator[ChatDelta]:
        args = {
            "messages": messages,
            "tools": tools,
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "reasoning_effort": reasoning_effort,
            "tool_choice": tool_choice,
        }
        if active():
            try:
                reader, writer = await connect()
            except RelayUnavailableError as exc:
                give_up(exc)
            else:
                # Closing the connection is how a stream abandoned here -- a
                # cancelled turn, a consumer that stopped reading -- is called off
                # on the host: its reader sees the end and cancels the call.
                try:
                    await relay_wire.write_frame(writer, request_frame("stream", **self._call(**args)))
                    while True:
                        frame = await relay_wire.read_frame(reader)
                        if frame is not None and "delta" in frame:
                            yield relay_wire.decode(frame["delta"])
                            continue
                        if frame is not None and frame.get("done"):
                            return
                        read_answer(frame)
                        raise RelayFaultError("the host ended a stream with an unknown frame")
                finally:
                    writer.close()
        async for delta in self._locally().chat_stream(**args):
            yield delta


def litellm_provider(*args: Any, **kwargs: Any) -> LiteLLMShape:
    """A LiteLLM-routed provider: relayed to the host when this process has a relay, else direct.

    Takes :class:`LiteLLMProvider`'s arguments. The direct class is imported only
    when it is the one returned, which is what keeps a relayed process free of
    LiteLLM.
    """
    if active():
        return RelayProvider(*args, **kwargs)
    from raven.providers.litellm_provider import LiteLLMProvider

    return LiteLLMProvider(*args, **kwargs)


def is_litellm_transport(provider: Any) -> bool:
    """Whether ``provider`` sends through LiteLLM, here or on the host, without importing LiteLLM.

    Both classes stand on :class:`LiteLLMShape`, so the question is answered
    from the module that does not import it.
    """
    return isinstance(provider, LiteLLMShape)
