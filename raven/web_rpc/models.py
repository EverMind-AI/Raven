"""Pydantic v2 models for the gateway control plane's ``raven.*`` dialect.

These models are the Python-side mirror of ``rpc-schema/openrpc-web.json``,
exactly as :mod:`raven.rpc.models` mirrors ``rpc-schema/openrpc.json`` for the
terminal dialect. The two contracts are deliberately separate files: the
terminal schema is what the frontends generate their TypeScript clients from,
and this one is the control plane's own three-method vocabulary (the forty
config-admin methods that once shared it died with ui-webui -- C7 ruling).
Drift between this module and the web schema is caught in CI by
``tests/test_web_rpc_schema_match.py``; that every declared method has a
handler (and every ``raven.*`` handler a declaration) is caught by
``tests/test_web_rpc_registration.py``.

One honesty rule, matching how the handlers in
:mod:`raven.web_rpc.methods_config` actually behave: every params field is
optional -- the dispatcher never validates params against these models, and
every handler reads its params with ``.get`` and a default, so a required
flag here would document a rejection that does not exist.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    """Base class for all web RPC models -- forbids extra fields by default."""

    model_config = ConfigDict(extra="forbid")


class WebRebindState(_Strict):
    """How a QR channel's rebind is going, carried on the QR poll the client
    already makes.

    ``code_age_s`` is an age rather than a deadline because the two sides do not
    share a clock; the client compares it against ``max_refreshes`` behaviour it
    can see, not against a timestamp it has to trust.
    """

    phase: str = Field(
        ...,
        description="idle | waiting | scanned | confirmed | failed | cancelled.",
    )
    refreshes: int = Field(..., description="Codes reissued so far, against max_refreshes.")
    max_refreshes: int
    code_age_s: float | None = Field(
        ..., description="Seconds since the current code was issued; null when none is up."
    )
    detail: str = Field(..., description="Why it failed, when it did: expired | no_token | error.")



class WebChannelLive(_Strict):
    running: bool
    connected: bool | None = Field(
        ..., description="Three-valued: null means the channel does not report a pairing at all."
    )
    qr_login: bool



class RavenChannelsQrParams(_Strict):
    name: str | None = None



class RavenChannelsQrResult(_Strict):
    qr: str | None = Field(..., description="Login QR as a PNG data URI, or null when none is pending.")
    qr_text: str | None = Field(..., description="Raw scan payload when the gateway cannot rasterise a PNG.")
    connected: bool
    running: bool
    rebind: WebRebindState | None = Field(None, description="Null for a channel that does not offer rebinding.")



class RavenChannelsStartParams(_Strict):
    name: str | None = None
    enabled: bool | None = Field(None, description="False stops the adapter; anything else starts it.")



class RavenChannelsStartResult(_Strict):
    outcome: str = Field(
        ...,
        description=(
            "started | already | stopped | absent | disabled | deny_all | missing_dep | unknown | no_manager."
        ),
    )



class RavenChannelsLiveParams(_Strict):
    pass



class RavenChannelsLiveResult(_Strict):
    channels: dict[str, WebChannelLive]



WEB_METHOD_MODELS: dict[str, tuple[type[BaseModel], type[BaseModel]]] = {
    "raven.channels.qr": (RavenChannelsQrParams, RavenChannelsQrResult),
    "raven.channels.start": (RavenChannelsStartParams, RavenChannelsStartResult),
    "raven.channels.live": (RavenChannelsLiveParams, RavenChannelsLiveResult),
}

__all__ = ["WEB_METHOD_MODELS"]
