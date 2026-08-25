"""Pydantic v2 models for the web channel's ``raven.*`` JSON-RPC dialect.

These models are the Python-side mirror of ``rpc-schema/openrpc-web.json``,
exactly as :mod:`raven.rpc.models` mirrors ``rpc-schema/openrpc.json`` for the
terminal dialect. The two contracts are deliberately separate files: the
terminal schema is what ui-tui generates its TypeScript client from, and mixing
forty browser-only methods into it would hand the TUI a vocabulary it must
never speak. Drift between this module and the web schema is caught in CI by
``tests/test_web_rpc_schema_match.py``; that every declared method has a
handler (and every ``raven.*`` handler a declaration) is caught by
``tests/test_web_rpc_registration.py``.

Two honesty rules, matching how the handlers in
:mod:`raven.web_rpc.methods_config` actually behave:

- Every params field is optional. The web dispatcher never validates params
  against these models (neither does the terminal one), and every handler
  reads its params with ``.get`` and a default -- so a required flag here
  would document a rejection that does not exist.
- A payload the handler passes through from config or another subsystem
  (channel config blobs, sub-agent entries, hub catalog items, DAG run state)
  is typed as a free-form object rather than given invented structure.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    """Base class for all web RPC models -- forbids extra fields by default."""

    model_config = ConfigDict(extra="forbid")


# Same convention as raven.rpc.models: the schema side declares ``JsonValue``
# as the union of all JSON types, the Python side uses ``Any``.
JsonValue = Any


# ---------------------------------------------------------------------------
# raven.subagents.*
# ---------------------------------------------------------------------------


class WebProbeLastTest(_Strict):
    """One remembered ``raven.subagents.test`` verdict (TestStateStore)."""

    ok: bool
    detail: str
    tested_at_ms: int = Field(alias="testedAtMs")


class WebProbeResult(_Strict):
    """Wire shape of ``ProbeResult.to_wire``."""

    name: str
    source: str
    kind: str
    status: str
    detail: str
    target: str
    elapsed_ms: int = Field(alias="elapsedMs")
    last_test: WebProbeLastTest | None = Field(default=None, alias="lastTest")


class WebTestResult(_Strict):
    """Wire shape of ``TestResult.to_wire``."""

    name: str
    source: str
    kind: str | None = None
    ok: bool
    detail: str
    reply: str | None = None
    elapsed_ms: int = Field(alias="elapsedMs")


class RavenSubagentsListParams(_Strict):
    pass


class RavenSubagentsListResult(_Strict):
    agents: list[dict[str, JsonValue]] = Field(
        ..., description="Configured third-party sub-agent entries, validated config dicts (by alias)."
    )


class RavenSubagentsSetParams(_Strict):
    agents: list[dict[str, JsonValue]] | None = Field(
        default=None, description="Full replacement list of third-party sub-agent config entries."
    )


class RavenSubagentsSetResult(_Strict):
    ok: bool
    count: int


class RavenSubagentsPresetsParams(_Strict):
    pass


class RavenSubagentsPresetsResult(_Strict):
    presets: list[dict[str, JsonValue]]


class RavenSubagentsProbeParams(_Strict):
    pass


class RavenSubagentsProbeResult(_Strict):
    results: list[WebProbeResult]


class RavenSubagentsTestParams(_Strict):
    name: str | None = Field(default=None, description="Which configured entry or preset to test, by name only.")
    source: str | None = Field(default=None, description="'config' (default) or 'preset'.")


class RavenSubagentsTestResult(_Strict):
    result: WebTestResult


class RavenSubagentsInstancesParams(_Strict):
    session_key: str | None = None


class RavenSubagentsInstancesResult(_Strict):
    instances: list[dict[str, JsonValue]] = Field(
        ..., description="Registry rows, reconciled against what is still alive on the agent loop."
    )


class RavenSubagentsInstancesDeleteParams(_Strict):
    session_key: str | None = None


class RavenSubagentsInstancesDeleteResult(_Strict):
    removed: int


class RavenSubagentsInstancesCancelParams(_Strict):
    session_key: str | None = None
    agent: str | None = None
    handle: str | None = None


class RavenSubagentsInstancesCancelResult(_Strict):
    cancelled: bool


class RavenSubagentsDagCancelParams(_Strict):
    run_id: str | None = None


class RavenSubagentsDagCancelResult(_Strict):
    cancelled: bool


class RavenSubagentsDagGetParams(_Strict):
    run_id: str | None = None
    session_key: str | None = None


class RavenSubagentsDagGetResult(_Strict):
    run: dict[str, JsonValue] = Field(..., description="One DAG run's structure and per-node state, from its run dir.")


class RavenSubagentsDagNodeParams(_Strict):
    run_id: str | None = None
    node: str | None = None
    max_output_chars: int | None = None
    session_key: str | None = None


class RavenSubagentsDagNodeResult(_Strict):
    node: dict[str, JsonValue] = Field(..., description="One node's rendered prompt and (truncated) output.")


# ---------------------------------------------------------------------------
# raven.session.*
# ---------------------------------------------------------------------------


class RavenSessionModelGetParams(_Strict):
    session_key: str | None = None


class RavenSessionModelGetResult(_Strict):
    model: str | None = Field(..., description="The session's own override, or null when unset.")
    default: str | None = Field(..., description="agents.defaults.model, what an unset session routes on.")


class RavenSessionModelSetParams(_Strict):
    session_key: str | None = None
    model: str | None = Field(default=None, description="Model id to pin, or null to clear the override.")


class RavenSessionModelSetResult(_Strict):
    ok: bool
    model: str | None = None


class RavenSessionWorkdirGetParams(_Strict):
    session_key: str | None = None


class RavenSessionWorkdirGetResult(_Strict):
    workdir: str | None = Field(..., description="The session's own override, or null when unset.")
    default: str = Field(..., description="The directory the next turn would run in without an override.")


class RavenSessionWorkdirSetParams(_Strict):
    session_key: str | None = None
    workdir: str | None = Field(default=None, description="Absolute directory to pin, or null to clear the override.")


class RavenSessionWorkdirSetResult(_Strict):
    ok: bool
    workdir: str | None = None


# ---------------------------------------------------------------------------
# raven.channels.* / raven.gateway.restart
# ---------------------------------------------------------------------------


class WebChannelEntry(_Strict):
    name: str
    enabled: bool
    specs: dict[str, JsonValue] = Field(..., description="Reflected field specs, dotted-path -> spec.")
    config: dict[str, JsonValue] = Field(..., description="Current config, dotted-path -> value, secrets redacted.")


class RavenChannelsListParams(_Strict):
    pass


class RavenChannelsListResult(_Strict):
    channels: list[WebChannelEntry]


class RavenChannelsSetParams(_Strict):
    name: str | None = None
    fields: dict[str, JsonValue] | None = None


class RavenChannelsSetResult(_Strict):
    ok: bool
    restart_required: bool


class RavenChannelsQrParams(_Strict):
    name: str | None = None


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


class RavenChannelsQrResult(_Strict):
    qr: str | None = Field(..., description="Login QR as a PNG data URI, or null when none is pending.")
    qr_text: str | None = Field(..., description="Raw scan payload when the gateway cannot rasterise a PNG.")
    connected: bool
    running: bool
    rebind: WebRebindState | None = Field(None, description="Null for a channel that does not offer rebinding.")


class RavenChannelsRebindParams(_Strict):
    name: str | None = None


class RavenChannelsRebindResult(_Strict):
    """``started: False`` with a reason is a state the page draws, not an error.

    A rebind keeps the paired account until a new scan is confirmed, so refusing
    ("not running", "already rebinding", "unsupported") costs the caller nothing
    and needs no error path.
    """

    started: bool
    reason: str = Field(..., description="'' when started; else not_running | already_rebinding | unsupported.")
    phase: str
    refreshes: int | None = None
    max_refreshes: int | None = None
    code_age_s: float | None = None
    detail: str | None = None


class RavenChannelsRebindCancelParams(_Strict):
    name: str | None = None


class RavenChannelsRebindCancelResult(_Strict):
    phase: str
    refreshes: int | None = None
    max_refreshes: int | None = None
    code_age_s: float | None = None
    detail: str | None = None


class WebChannelLive(_Strict):
    running: bool
    connected: bool | None = Field(
        ..., description="Three-valued: null means the channel does not report a pairing at all."
    )
    qr_login: bool


class RavenChannelsLiveParams(_Strict):
    pass


class RavenChannelsLiveResult(_Strict):
    channels: dict[str, WebChannelLive]


class RavenGatewayRestartParams(_Strict):
    pass


class RavenGatewayRestartResult(_Strict):
    ok: bool


# ---------------------------------------------------------------------------
# raven.tools.*
# ---------------------------------------------------------------------------


class WebToolEntry(_Strict):
    """One configurable tool row.

    Two shapes share this model: the web-search row carries ``maxResults``, and
    a media row carries the model/base fields plus ``configured`` (a media tool
    the user never configured stays off even with a chat key present). The
    variant fields are optional rather than split into two models because the
    page renders one list from both.
    """

    kind: str
    tool: str
    registered: bool
    setting_path: str = Field(alias="settingPath")
    env_key: str = Field(alias="envKey")
    api_key: str = Field(alias="apiKey")
    key_source: str = Field(alias="keySource")
    max_results: int | None = Field(default=None, alias="maxResults")
    configured: bool | None = None
    api_base: str | None = Field(default=None, alias="apiBase")
    default_api_base: str | None = Field(default=None, alias="defaultApiBase")
    model: str | None = None
    default_model: str | None = Field(default=None, alias="defaultModel")


class RavenToolsListParams(_Strict):
    pass


class RavenToolsListResult(_Strict):
    tools: list[WebToolEntry]


class RavenToolsSetParams(_Strict):
    kind: str | None = None
    fields: dict[str, JsonValue] | None = None


class RavenToolsSetResult(_Strict):
    ok: bool
    restart_required: bool


# ---------------------------------------------------------------------------
# raven.skills.*
# ---------------------------------------------------------------------------


class WebSkillEntry(_Strict):
    id: str
    name: str
    source: str
    description: str
    path: str


class RavenSkillsGetParams(_Strict):
    pass


class RavenSkillsGetResult(_Strict):
    skillforge: dict[str, JsonValue]


class RavenSkillsSetParams(_Strict):
    fields: dict[str, JsonValue] | None = None


class RavenSkillsSetResult(_Strict):
    ok: bool
    restart_required: bool


class RavenSkillsListParams(_Strict):
    pass


class RavenSkillsListResult(_Strict):
    skills: list[WebSkillEntry]


class RavenSkillsBodyParams(_Strict):
    name: str | None = None
    id: str | None = None
    source: str | None = None


class RavenSkillsBodyResult(_Strict):
    """Union of the local-catalog shape and the hub-fetch shape.

    A local/installed skill carries path/description/tags/files; a hub catalog
    item that was never installed carries version. ``skillMd`` and ``name`` are
    the only fields both shapes always produce.
    """

    skill_md: str = Field(alias="skillMd")
    name: str
    version: str | None = None
    source: str | None = None
    path: str | None = None
    description: str | None = None
    category: str | None = None
    license: str | None = None
    tags: list[str] | None = None
    source_url: str | None = None
    files: list[str] | None = None


class RavenSkillsHubTestParams(_Strict):
    endpoint: str | None = None
    api_key: str | None = Field(default=None, alias="apiKey")


class RavenSkillsHubTestResult(_Strict):
    ok: bool
    detail: str


class RavenSkillsHubSearchParams(_Strict):
    q: str | None = None
    category: str | None = None
    sort: str | None = None
    limit: int | None = None


class RavenSkillsHubSearchResult(_Strict):
    items: list[dict[str, JsonValue]] = Field(..., description="Hub catalog entries, passed through as-is.")


class RavenSkillsHubInstallParams(_Strict):
    id: str | None = None


class RavenSkillsHubInstallResult(_Strict):
    ok: bool
    slug: str | None = None
    version: str | None = None
    dir: str | None = None


class RavenSkillsRemoveParams(_Strict):
    name: str | None = None
    id: str | None = None
    source: str | None = None


class RavenSkillsRemoveResult(_Strict):
    ok: bool
    removed: str = Field(..., description="The skill folder that was deleted.")


class RavenSkillsZipParams(_Strict):
    name: str | None = None
    id: str | None = None
    source: str | None = None


class RavenSkillsZipResult(_Strict):
    filename: str
    b64: str = Field(..., description="The zipped skill folder, base64-encoded.")


# ---------------------------------------------------------------------------
# raven.everos.*
# ---------------------------------------------------------------------------


class WebEverosProvider(_Strict):
    name: str
    label: str
    label_zh: str
    base_url: str
    supports: list[str]
    rerank_provider: str | None = None
    rerank_base_url: str | None = None
    chat_models: list[str]
    has_credential: bool


class RavenEverosGetParams(_Strict):
    pass


class RavenEverosGetResult(_Strict):
    everos: dict[str, JsonValue] = Field(..., description="Writable everos.toml sections, api_key redacted.")
    backend: str | None = Field(..., description="memory.backend actually in force, or null (native memory).")


class RavenEverosSetParams(_Strict):
    section: str | None = None
    fields: dict[str, JsonValue] | None = None
    reuse_key_from: str | None = Field(default=None, description="Sibling section whose stored key to copy.")
    credential_provider: str | None = Field(
        default=None, description="Provider slug whose Credentials-page key to copy."
    )


class RavenEverosSetResult(_Strict):
    ok: bool
    restart_required: bool


class RavenEverosClearParams(_Strict):
    section: str | None = None


class RavenEverosClearResult(_Strict):
    ok: bool
    restart_required: bool


class RavenEverosTestParams(_Strict):
    section: str | None = None
    fields: dict[str, JsonValue] | None = None
    reuse_key_from: str | None = None
    credential_provider: str | None = None


class RavenEverosTestResult(_Strict):
    ok: bool
    detail: str


class RavenEverosProvidersParams(_Strict):
    pass


class RavenEverosProvidersResult(_Strict):
    providers: list[WebEverosProvider]


class RavenEverosModelsParams(_Strict):
    section: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    provider_name: str | None = None
    reuse_key_from: str | None = None
    credential_provider: str | None = None


class RavenEverosModelsResult(_Strict):
    models: list[str]


# ---------------------------------------------------------------------------
# raven.mcp.*
# ---------------------------------------------------------------------------


class RavenMcpListParams(_Strict):
    pass


class RavenMcpListResult(_Strict):
    servers: list[dict[str, JsonValue]] = Field(
        ..., description="Configured entries with live state merged in (connected, tools)."
    )
    connected: bool


class RavenMcpSetParams(_Strict):
    servers: list[dict[str, JsonValue]] | None = None


class RavenMcpSetResult(_Strict):
    ok: bool
    restart_required: bool


# ---------------------------------------------------------------------------
# raven.cron.*
# ---------------------------------------------------------------------------


class WebCronSchedule(_Strict):
    kind: str
    expr: str | None = None
    every_ms: int | None = None
    at_ms: int | None = None
    tz: str | None = None


class WebCronJob(_Strict):
    """Wire shape of ``_serialize_cron_job``."""

    id: str
    name: str
    enabled: bool
    schedule: WebCronSchedule
    message: str
    channel: str | None = None
    to: str | None = None
    next_run_at_ms: int | None = None


class RavenCronListParams(_Strict):
    pass


class RavenCronListResult(_Strict):
    jobs: list[WebCronJob]


class WebCronScheduleParams(_Strict):
    kind: str | None = Field(default=None, description="'at', 'every', or 'cron' (default).")
    at_ms: int | None = None
    every_ms: int | None = None
    expr: str | None = None
    tz: str | None = None


class RavenCronAddParams(_Strict):
    name: str | None = None
    schedule: WebCronScheduleParams | None = None
    message: str | None = None
    deliver: bool | None = None
    channel: str | None = None
    to: str | None = None


class RavenCronAddResult(_Strict):
    job: WebCronJob


class RavenCronRemoveParams(_Strict):
    job_id: str | None = None


class RavenCronRemoveResult(_Strict):
    removed: bool


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

WEB_METHOD_MODELS: dict[str, tuple[type[BaseModel], type[BaseModel]]] = {
    # raven.subagents.*
    "raven.subagents.list": (RavenSubagentsListParams, RavenSubagentsListResult),
    "raven.subagents.set": (RavenSubagentsSetParams, RavenSubagentsSetResult),
    "raven.subagents.presets": (RavenSubagentsPresetsParams, RavenSubagentsPresetsResult),
    "raven.subagents.probe": (RavenSubagentsProbeParams, RavenSubagentsProbeResult),
    "raven.subagents.test": (RavenSubagentsTestParams, RavenSubagentsTestResult),
    "raven.subagents.instances": (RavenSubagentsInstancesParams, RavenSubagentsInstancesResult),
    "raven.subagents.instances.delete": (RavenSubagentsInstancesDeleteParams, RavenSubagentsInstancesDeleteResult),
    "raven.subagents.instances.cancel": (RavenSubagentsInstancesCancelParams, RavenSubagentsInstancesCancelResult),
    "raven.subagents.dag.cancel": (RavenSubagentsDagCancelParams, RavenSubagentsDagCancelResult),
    "raven.subagents.dag.get": (RavenSubagentsDagGetParams, RavenSubagentsDagGetResult),
    "raven.subagents.dag.node": (RavenSubagentsDagNodeParams, RavenSubagentsDagNodeResult),
    # raven.session.*
    "raven.session.model.get": (RavenSessionModelGetParams, RavenSessionModelGetResult),
    "raven.session.model.set": (RavenSessionModelSetParams, RavenSessionModelSetResult),
    "raven.session.workdir.get": (RavenSessionWorkdirGetParams, RavenSessionWorkdirGetResult),
    "raven.session.workdir.set": (RavenSessionWorkdirSetParams, RavenSessionWorkdirSetResult),
    # raven.channels.* / raven.gateway.*
    "raven.channels.list": (RavenChannelsListParams, RavenChannelsListResult),
    "raven.channels.set": (RavenChannelsSetParams, RavenChannelsSetResult),
    "raven.channels.qr": (RavenChannelsQrParams, RavenChannelsQrResult),
    "raven.channels.rebind": (RavenChannelsRebindParams, RavenChannelsRebindResult),
    "raven.channels.rebind.cancel": (
        RavenChannelsRebindCancelParams,
        RavenChannelsRebindCancelResult,
    ),
    "raven.channels.live": (RavenChannelsLiveParams, RavenChannelsLiveResult),
    "raven.gateway.restart": (RavenGatewayRestartParams, RavenGatewayRestartResult),
    # raven.skills.*
    "raven.tools.list": (RavenToolsListParams, RavenToolsListResult),
    "raven.tools.set": (RavenToolsSetParams, RavenToolsSetResult),
    "raven.skills.get": (RavenSkillsGetParams, RavenSkillsGetResult),
    "raven.skills.set": (RavenSkillsSetParams, RavenSkillsSetResult),
    "raven.skills.list": (RavenSkillsListParams, RavenSkillsListResult),
    "raven.skills.body": (RavenSkillsBodyParams, RavenSkillsBodyResult),
    "raven.skills.hub.test": (RavenSkillsHubTestParams, RavenSkillsHubTestResult),
    "raven.skills.hub.search": (RavenSkillsHubSearchParams, RavenSkillsHubSearchResult),
    "raven.skills.hub.install": (RavenSkillsHubInstallParams, RavenSkillsHubInstallResult),
    "raven.skills.remove": (RavenSkillsRemoveParams, RavenSkillsRemoveResult),
    "raven.skills.zip": (RavenSkillsZipParams, RavenSkillsZipResult),
    # raven.everos.*
    "raven.everos.get": (RavenEverosGetParams, RavenEverosGetResult),
    "raven.everos.set": (RavenEverosSetParams, RavenEverosSetResult),
    "raven.everos.clear": (RavenEverosClearParams, RavenEverosClearResult),
    "raven.everos.test": (RavenEverosTestParams, RavenEverosTestResult),
    "raven.everos.providers": (RavenEverosProvidersParams, RavenEverosProvidersResult),
    "raven.everos.models": (RavenEverosModelsParams, RavenEverosModelsResult),
    # raven.mcp.*
    "raven.mcp.list": (RavenMcpListParams, RavenMcpListResult),
    "raven.mcp.set": (RavenMcpSetParams, RavenMcpSetResult),
    # raven.cron.*
    "raven.cron.list": (RavenCronListParams, RavenCronListResult),
    "raven.cron.add": (RavenCronAddParams, RavenCronAddResult),
    "raven.cron.remove": (RavenCronRemoveParams, RavenCronRemoveResult),
}

__all__ = ["WEB_METHOD_MODELS", "JsonValue"]
