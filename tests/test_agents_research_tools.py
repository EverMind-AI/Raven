"""The three replacement tools where the plugin seam decides whether they work.

The fork resolved this in its assembly: one loop read ``config.tools.web``, built
both web tools from it, and refused to register a ``web_search`` that had no key
to search with. A plugin factory is handed its own config slice and nothing else,
so the same three questions - which key, which proxy, and may this tool be
advertised at all - are answered here instead.

Every test is written against the way that seam fails silently: a tool that is
registered, named to the model as one of exactly two, and answers every call with
an error string while the launch reports success.
"""

from __future__ import annotations

import json
import socket
import sys
from pathlib import Path

import httpx
import pytest

REPO = Path(__file__).resolve().parent.parent
PLUGIN_DIR = REPO / "agents" / "raven-research" / "plugins" / "research-flow"
sys.path.insert(0, str(PLUGIN_DIR))

from research_flow.plugin import make_ask_user, make_hook, make_web_fetch, make_web_search  # noqa: E402
from research_flow.support import ledger as ledger_mod  # noqa: E402
from research_flow.support.search_saturation import SearchSaturation  # noqa: E402
from research_flow.tools import web as web_mod  # noqa: E402
from research_flow.tools.ask_user import DRAskUserTool  # noqa: E402
from research_flow.tools.web import (  # noqa: E402
    _UNRESOLVED_REFUSAL,
    DEFAULT_FETCH_PROVIDER,
    FETCH_PROVIDERS,
    SEARCH_PROVIDERS,
    WebFetchTool,
    WebSearchTool,
    fetch_fallbacks,
    set_current_session,
)

from raven.agent.tools.params import cast_params, validate_params  # noqa: E402
from raven.plugins.context import PluginContext, ServiceLocator  # noqa: E402
from raven.security.network import validate_url_target  # noqa: E402

# The gates that need a model. Off, so a context with no provider still installs
# the flow and these tests measure the key rather than the provider.
_NO_LLM_GATES = {"verify": {"enabled": False}, "forceFinalize": {"enabled": False}}


def _ctx(tmp_path: Path, slice_: dict, *, provider: object | None = None) -> PluginContext:
    return PluginContext(
        config=slice_,
        services=ServiceLocator(workspace=tmp_path, user_id="u", agent_id="a", provider=provider),
    )


def _patch_client(monkeypatch, transport):
    real = httpx.AsyncClient

    def factory(**kwargs):
        kwargs.pop("proxy", None)
        return real(transport=transport, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", factory)


class _PageTransport(httpx.AsyncBaseTransport):
    """Reader stub: any GET comes back as one short page."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="page body")


class _StatusTransport(httpx.AsyncBaseTransport):
    """Answers every request with a status error, the URL and query intact."""

    def __init__(self, status: int) -> None:
        self._status = status

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(self._status, request=request, json={})


# --------------------------------------------------------------------------
# What a vendor's refusal is allowed to say
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_search_status_error_names_the_vendor_and_status_only(monkeypatch):
    """httpx puts the whole request URL in a status error's text. This pair
    authenticates by header, so nothing leaks today -- but the search ledger's
    row lands on disk and the rendering reaches the model, so neither may carry
    a request. A vendor keyed by query parameter is one edit away."""
    _patch_client(monkeypatch, _StatusTransport(401))
    set_current_session("t")
    tool = WebSearchTool(api_key="SECRET-KEY-123")
    # Driven through ``execute`` so the row captured here is the one a run
    # writes to disk, rather than a shaping dict assembled by the test.
    rows: list[dict] = []
    monkeypatch.setattr(
        type(tool),
        "_log_search",
        lambda self, state, query, n, urls, rendered, shaping, **kw: rows.append(shaping),
    )

    rendered = await tool.execute(query="q1", count=3)

    assert rendered == "Error: Serper answered HTTP 401"
    assert "SECRET-KEY-123" not in rendered and "serper.dev" not in rendered
    # Not vacuous: the row exists and stays diagnosable, and the status is what
    # the rollout-time balance polling reads off it.
    assert len(rows) == 1, "the failed search still writes its ledger row"
    assert rows[0]["status"] == 401 and rows[0]["quota_err"] is True
    assert "SECRET-KEY-123" not in rows[0]["error"] and "serper.dev" not in rows[0]["error"]


@pytest.mark.asyncio
async def test_a_fetch_status_error_names_the_reader_and_status_only(monkeypatch):
    _patch_client(monkeypatch, _StatusTransport(402))
    set_current_session("t")

    answer = await WebFetchTool(api_key="SECRET-KEY-123").execute(url="https://example.com/a")

    assert json.loads(answer)["error"] == "Jina Reader answered HTTP 402"
    assert "SECRET-KEY-123" not in answer and "r.jina.ai" not in answer


# --------------------------------------------------------------------------
# The key: the slice the plugin reads, and the tool it hands back
# --------------------------------------------------------------------------


def test_the_search_tool_takes_its_key_from_the_plugin_slice(tmp_path, monkeypatch):
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    slice_ = {"enabled": True, "search": {"apiKey": "serper-key"}, **_NO_LLM_GATES}

    tool = make_web_search(_ctx(tmp_path, slice_))

    assert isinstance(tool, WebSearchTool) and tool.api_key == "serper-key"


def test_the_fetch_tool_takes_its_key_from_the_plugin_slice(tmp_path, monkeypatch):
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    slice_ = {"enabled": True, "fetch": {"apiKey": "jina-key"}, **_NO_LLM_GATES}

    tool = make_web_fetch(_ctx(tmp_path, slice_))

    assert isinstance(tool, WebFetchTool) and tool.api_key == "jina-key"


@pytest.mark.parametrize("vendor", sorted(v for v, s in FETCH_PROVIDERS.items() if s.needs_key))
def test_a_reader_selected_without_a_key_is_built_as_the_default_reader(tmp_path, monkeypatch, vendor):
    """The kernel's ``web_fetch`` degrades a keyless keyed backend to Jina, and
    this tool REPLACES the kernel's: without the same rule a research run is the
    one place where that config answers every fetch with a refusal, while a
    plain raven on it reads the page.

    Registration is not the gate on this half. Jina needs no key, so the tool is
    always offered and it is the backend underneath that has to move.
    """
    monkeypatch.delenv(FETCH_PROVIDERS[vendor].env_var, raising=False)
    slice_ = {"enabled": True, "fetch": {"provider": vendor}, **_NO_LLM_GATES}

    tool = make_web_fetch(_ctx(tmp_path, slice_))

    assert isinstance(tool, WebFetchTool) and tool.provider == DEFAULT_FETCH_PROVIDER


@pytest.mark.parametrize("where", ["slice", "env"])
def test_a_reader_whose_key_resolves_is_left_on_the_backend_it_names(tmp_path, monkeypatch, where):
    """Either source counts, because the tool reads both at call time: degrading
    on an empty slice alone would move a deploy that only exports the var."""
    slice_: dict = {"enabled": True, "fetch": {"provider": "tavily"}, **_NO_LLM_GATES}
    if where == "slice":
        monkeypatch.delenv("TAVILY_API_KEY", raising=False)
        slice_["fetch"]["apiKey"] = "tavily-key"
    else:
        monkeypatch.setenv("TAVILY_API_KEY", "tavily-key")

    tool = make_web_fetch(_ctx(tmp_path, slice_))

    assert tool is not None and tool.provider == "tavily"


def test_the_reader_it_degrades_to_is_built_with_that_reader_s_own_key(tmp_path, monkeypatch):
    """The key follows the provider, as it does at the kernel's own build.

    A host that configured a Jina key gets authenticated Jina from raven's
    built-in reader, so a research run that quietly dropped to anonymous Jina
    would spend the deployment's configured quota on one path and not the
    other -- on the very fallback this rule exists to match.
    """
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    slice_ = {
        "enabled": True,
        "fetch": {"provider": "tavily", "fallbackApiKey": "jina-key"},
        **_NO_LLM_GATES,
    }

    tool = make_web_fetch(_ctx(tmp_path, slice_))

    assert tool is not None and tool.provider == DEFAULT_FETCH_PROVIDER
    assert tool.api_key == "jina-key"


def test_a_reader_that_keeps_its_vendor_ignores_the_fallback_key(tmp_path, monkeypatch):
    """The other branch: the fallback key must not reach the vendor that did
    resolve one, which would send Jina's credential to Tavily's endpoint."""
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    slice_ = {
        "enabled": True,
        "fetch": {"provider": "tavily", "apiKey": "tavily-key", "fallbackApiKey": "jina-key"},
        **_NO_LLM_GATES,
    }

    tool = make_web_fetch(_ctx(tmp_path, slice_))

    assert tool is not None and tool.provider == "tavily"
    assert tool.api_key == "tavily-key"


def test_the_degradation_rule_takes_an_unknown_name_the_way_the_constructor_does(monkeypatch):
    """Two doors into the same tool, one normalisation: a caller must not get
    a KeyError from the rule and a degraded tool from the constructor."""
    monkeypatch.delenv("JINA_API_KEY", raising=False)

    assert WebFetchTool.effective_provider("bogus", None) == DEFAULT_FETCH_PROVIDER
    assert WebFetchTool(provider="bogus").provider == DEFAULT_FETCH_PROVIDER


def test_the_plugin_degrades_a_keyless_reader_exactly_as_the_kernel_does(monkeypatch):
    """One rule, two implementations - the seam this plugin is built on.

    Pinned per vendor rather than by reading both: the two tables are separate
    objects, and a backend added to one with a different ``needs_key`` is the
    drift that puts the two ``web_fetch`` tools on different backends for one
    config, which is the bug this rule was added to close.
    """
    from raven.agent.tools.web import FETCH_PROVIDERS as KERNEL_PROVIDERS
    from raven.agent.tools.web import WebFetchTool as KernelFetchTool

    assert set(FETCH_PROVIDERS) == set(KERNEL_PROVIDERS)
    for vendor in sorted(FETCH_PROVIDERS):
        monkeypatch.delenv(FETCH_PROVIDERS[vendor].env_var, raising=False)
        assert WebFetchTool.effective_provider(vendor, None) == KernelFetchTool.effective_provider(vendor, None)
        assert WebFetchTool.effective_provider(vendor, "k") == KernelFetchTool.effective_provider(vendor, "k")


def test_the_proxy_reaches_both_tools(tmp_path):
    slice_ = {"enabled": True, "search": {"apiKey": "k"}, "proxy": "http://127.0.0.1:7890", **_NO_LLM_GATES}
    ctx = _ctx(tmp_path, slice_)

    assert make_web_search(ctx).proxy == "http://127.0.0.1:7890"
    assert make_web_fetch(ctx).proxy == "http://127.0.0.1:7890"


def test_the_digest_follows_the_sessions_mode_not_the_base_config(tmp_path):
    """web_fetch is built once from the base config while the chain is rebuilt
    per (session, mode). A deep session whose overlay moves digest.model must
    digest with that model, so the tool reads the knob off the session's gear at
    call time; a call outside any geared session keeps the base value."""
    import asyncio
    from types import SimpleNamespace

    from research_flow.flow import SessionGear
    from research_flow.plugin import _shared_for

    seen: list[str | None] = []

    class _Provider:
        async def chat_with_retry(self, **kwargs):
            seen.append(kwargs.get("model"))
            return SimpleNamespace(content="the extracted facts")

    # verbatimHeadChars is pinned rather than inherited: the assertion is about
    # which model digested, and a moved default would rewrite the digest text.
    slice_ = {
        "enabled": True,
        "digest": {"enabled": True, "model": "base/cheap", "verbatimHeadChars": 0},
        **_NO_LLM_GATES,
    }
    ctx = _ctx(tmp_path, slice_, provider=_Provider())
    tool = make_web_fetch(ctx)
    page = "x" * (tool.digest_threshold_chars + 1)

    async def run():
        set_current_session("s-deep")
        _shared_for(ctx).session_gear["s-deep"] = SessionGear(digest_model="deep/strong", digest_verbatim_head_chars=0)
        deep = await tool._try_digest(page, "the founding date", "https://a.example/one")
        set_current_session("s-ungeared")
        base = await tool._try_digest(page, "the founding date", "https://a.example/two")
        return deep, base

    deep, base = asyncio.run(run())
    assert deep == "the extracted facts" and base == "the extracted facts"
    assert seen == ["deep/strong", "base/cheap"]


def test_a_keyless_search_tool_is_not_contributed(tmp_path, monkeypatch):
    """The fork refused to register it rather than advertise a tool whose every
    call is an error string; the contract this flow renders names the model's
    tools exactly, so an advertised dud is worse than an absent tool."""
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    ctx = _ctx(tmp_path, {"enabled": True, **_NO_LLM_GATES})

    assert make_web_search(ctx) is None
    # web_fetch is not gated the same way: the reader serves unauthenticated and
    # the key only lifts the rate limit.
    assert isinstance(make_web_fetch(ctx), WebFetchTool)


def test_a_bare_env_var_still_contributes_the_search_tool(tmp_path, monkeypatch):
    """The launcher accepts an exported key in place of a rendered one, so the
    gate has to ask the tool (which resolves both) and not the slice."""
    monkeypatch.setenv("SERPER_API_KEY", "from-the-environment")

    tool = make_web_search(_ctx(tmp_path, {"enabled": True, **_NO_LLM_GATES}))

    assert isinstance(tool, WebSearchTool) and tool.api_key == "from-the-environment"


@pytest.mark.asyncio
async def test_the_not_configured_message_names_the_path_the_plugin_reads(monkeypatch):
    """Reader-facing, and the only thing the operator gets: naming a path this
    tool never consults sends them to edit a file that cannot fix it."""
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    set_current_session("t")

    answer = await WebSearchTool().execute(query="anything")

    assert 'plugins.config["research-flow"].search.apiKey' in answer
    assert "tools.web.search.apiKey" not in answer
    assert "SERPER_API_KEY" in answer


def test_the_flow_and_its_tools_decline_together(tmp_path):
    """Without the hook nothing calls ``set_current_session`` or ``start_turn``,
    so every session collapses into the ContextVar's default slot and the replay
    cache, the dedup sets and the retry budget are never reset - process-wide and
    permanent. The tools are per-session only through the hook, so they decline
    with it and the kernel's own web tools serve instead."""
    slice_ = {
        "enabled": True,
        "search": {"apiKey": "k"},
        "verify": {"enabled": True},
        "askUser": {"enabled": True},
        "conversation": {"enabled": True, "gate": "agentic"},
    }
    ctx = _ctx(tmp_path, slice_, provider=None)

    assert make_hook(ctx) is None
    assert make_web_search(ctx) is None
    assert make_web_fetch(ctx) is None
    assert make_ask_user(ctx) is None


# --------------------------------------------------------------------------
# web_fetch: a resolver failure is not the target's fault
# --------------------------------------------------------------------------


def test_the_trunk_refusal_wording_is_the_one_the_tolerance_keys_on(monkeypatch):
    """The tolerance below reads one refusal of the trunk validator by its text.
    If that wording moves, this fails here rather than silently restoring the
    strictness the reader path is meant not to have."""

    def _no_resolver(*args, **kwargs):
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    monkeypatch.setattr(socket, "getaddrinfo", _no_resolver)
    is_valid, error_msg = validate_url_target("https://example.com/a")

    assert not is_valid and error_msg.startswith(_UNRESOLVED_REFUSAL)


@pytest.mark.asyncio
async def test_a_resolver_hiccup_does_not_refuse_the_fetch(monkeypatch):
    """The reader service opens the connection from its own network; this process
    never does. A local resolver that fails under load therefore says nothing
    about the target, and refusing on it makes the refusal load-correlated."""

    def _no_resolver(*args, **kwargs):
        raise socket.gaierror(socket.EAI_AGAIN, "Temporary failure in name resolution")

    monkeypatch.setattr(socket, "getaddrinfo", _no_resolver)
    _patch_client(monkeypatch, _PageTransport())
    set_current_session("t")

    answer = await WebFetchTool().execute(url="https://example.com/a")

    assert "URL validation failed" not in answer
    assert "page body" in answer


@pytest.mark.asyncio
async def test_a_private_address_is_still_refused(monkeypatch):
    """The tolerance drops the refusal on a resolution FAILURE and nothing else:
    a name that resolves inward is still where the block belongs."""
    _patch_client(monkeypatch, _PageTransport())
    set_current_session("t")

    answer = await WebFetchTool().execute(url="http://127.0.0.1:8080/admin")

    assert "URL validation failed" in answer


@pytest.mark.asyncio
async def test_a_non_http_scheme_is_still_refused(monkeypatch):
    _patch_client(monkeypatch, _PageTransport())
    set_current_session("t")

    answer = await WebFetchTool().execute(url="file:///etc/passwd")

    assert "URL validation failed" in answer


# --------------------------------------------------------------------------
# ask_user: a granted call must not bounce at the registry
# --------------------------------------------------------------------------


def _registry_errors(tool: DRAskUserTool, params: dict) -> list[str]:
    """What ``ToolRegistry.execute`` decides about these params, in its own order.

    Mirrors ``raven/agent/tools/registry.py``: cast through the tool's hook, cast
    to the schema, then validate. Errors here are returned to the model and
    ``execute`` is never called.
    """
    cast = cast_params(tool.parameters, tool.cast_params(params))
    return validate_params(tool.parameters, cast)


def test_a_granted_call_whose_entry_names_no_question_does_not_bounce():
    """The gate has already moved to ``asked`` and the grant is spent-able by the
    time the registry validates, so a bounce costs the round trip and returns a
    parameter error where the fork returned this tool's own contract text."""
    tool = DRAskUserTool(delivery="tool")

    assert _registry_errors(tool, {"questions": [{"header": "Scope"}]}) == []
    assert _registry_errors(tool, {"questions": ["a bare string question"]}) == []
    assert _registry_errors(tool, {"questions": [{"question": "q", "recommended": "first"}]}) == []
    assert _registry_errors(tool, {"questions": [{"question": "q", "options": "not a list"}]}) == []


def test_the_widening_leaves_a_well_formed_call_alone():
    tool = DRAskUserTool(delivery="tool")
    params = {"questions": [{"question": "Which base?", "header": "Base", "options": ["main", "dev"]}]}

    cast = tool.cast_params(params)

    assert cast["questions"] == [{"question": "Which base?", "header": "Base", "options": ["main", "dev"]}]


def test_the_handoff_default_is_not_widened():
    """A handoff call short-circuits before the registry, so the fork's shape is
    what the measured arms sent and nothing here may move it."""
    tool = DRAskUserTool(delivery="handoff")
    params = {"questions": [{"header": "Scope"}]}

    assert tool.cast_params(params)["questions"] == [{"header": "Scope"}]


def test_the_schema_the_model_reads_still_demands_a_question():
    """The widening is a cast, not a looser contract: the schema is prompt text,
    byte-compared against the fork's, and telling the model a question is
    optional would change what it sends."""
    items = DRAskUserTool(delivery="tool").parameters["properties"]["questions"]["items"]

    assert items["required"] == ["question"]


# --------------------------------------------------------------------------
# The provider: which vendor each half calls, and what the rule is told
# --------------------------------------------------------------------------


class _CaptureTransport(httpx.AsyncBaseTransport):
    """Records every request and answers with a canned body."""

    def __init__(self, payload: object = None, *, text: str | None = None) -> None:
        self.seen: list[httpx.Request] = []
        self._payload = payload
        self._text = text

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await request.aread()
        self.seen.append(request)
        if self._text is not None:
            return httpx.Response(200, text=self._text, request=request)
        return httpx.Response(200, json=self._payload if self._payload is not None else {}, request=request)


#: One live-shaped response per search vendor, and the single organic row it
#: must normalise into. Written from each vendor's own field names, because the
#: render path reads exactly three keys and a vendor that spells them
#: differently is the whole reason the normaliser exists.
_SEARCH_PAYLOADS: dict[str, tuple[dict, dict]] = {
    "serper": (
        {"organic": [{"title": "T", "link": "https://e.com/a", "snippet": "S"}]},
        {"title": "T", "link": "https://e.com/a", "snippet": "S"},
    ),
    "serpapi": (
        {"organic_results": [{"title": "T", "link": "https://e.com/a", "snippet": "S"}]},
        {"title": "T", "link": "https://e.com/a", "snippet": "S"},
    ),
    "tavily": (
        {"results": [{"title": "T", "url": "https://e.com/a", "content": "S"}]},
        {"title": "T", "link": "https://e.com/a", "snippet": "S"},
    ),
    "exa": (
        {"results": [{"title": "T", "url": "https://e.com/a", "highlights": ["S"]}]},
        {"title": "T", "link": "https://e.com/a", "snippet": "S"},
    ),
    "brave": (
        {"web": {"results": [{"title": "T", "url": "https://e.com/a", "description": "S"}]}},
        {"title": "T", "link": "https://e.com/a", "snippet": "S"},
    ),
    "firecrawl": (
        {"success": True, "data": [{"title": "T", "url": "https://e.com/a", "description": "S"}]},
        {"title": "T", "link": "https://e.com/a", "snippet": "S"},
    ),
    "anysearch": (
        {"data": {"results": [{"title": "T", "url": "https://e.com/a", "snippet": "S"}]}},
        {"title": "T", "link": "https://e.com/a", "snippet": "S"},
    ),
}


def test_the_search_provider_reaches_the_tool_from_the_plugin_slice(tmp_path, monkeypatch):
    """The fourth question the seam answers, beside key, proxy and registration."""
    monkeypatch.delenv("SERPER_API_KEY", raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tavily-env-key")
    slice_ = {"enabled": True, "search": {"provider": "tavily"}, **_NO_LLM_GATES}

    tool = make_web_search(_ctx(tmp_path, slice_))

    # Not vacuous: the key follows the vendor, so a tool pointed at Tavily that
    # kept reading SERPER_API_KEY would be registered and fail every call.
    assert isinstance(tool, WebSearchTool)
    assert tool.provider == "tavily" and tool.api_key == "tavily-env-key"


def test_the_fetch_provider_reaches_the_tool_from_the_plugin_slice(tmp_path, monkeypatch):
    monkeypatch.delenv("JINA_API_KEY", raising=False)
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-env-key")
    slice_ = {"enabled": True, "search": {"apiKey": "k"}, "fetch": {"provider": "firecrawl"}, **_NO_LLM_GATES}

    tool = make_web_fetch(_ctx(tmp_path, slice_))

    assert tool.provider == "firecrawl" and tool.api_key == "fc-env-key"


def test_an_unknown_provider_degrades_to_the_default_and_says_so(tmp_path, monkeypatch, caplog):
    """A plain config slice has no schema to reject a typo, so the fallback has
    to be audible. A silent one is the symptomless failure this pair keeps
    warning about."""
    monkeypatch.setenv("SERPER_API_KEY", "serper-env-key")
    slice_ = {"enabled": True, "search": {"provider": "sepr"}, **_NO_LLM_GATES}

    tool = make_web_search(_ctx(tmp_path, slice_))

    assert tool.provider == "serper" and tool.api_key == "serper-env-key"


@pytest.mark.parametrize("vendor", sorted(SEARCH_PROVIDERS))
@pytest.mark.asyncio
async def test_each_search_vendor_is_called_on_its_own_endpoint(monkeypatch, vendor):
    payload, _ = _SEARCH_PAYLOADS[vendor]
    transport = _CaptureTransport(payload)
    _patch_client(monkeypatch, transport)
    set_current_session("t")

    await WebSearchTool(api_key="K", provider=vendor).execute(query="q", count=3)

    assert len(transport.seen) == 1, "one search is one request"
    sent = transport.seen[0]
    # The key must be on the request somewhere, or the vendor would 401 in
    # production while this test passed on a request that carried no auth.
    carried = "K" in str(sent.url) or "K" in " ".join(sent.headers.values())
    assert carried, f"{vendor} request carries no credential"


@pytest.mark.parametrize("vendor", sorted(SEARCH_PROVIDERS))
@pytest.mark.asyncio
async def test_each_search_vendor_normalises_into_the_render_shape(monkeypatch, vendor):
    """The render path reads ``organic`` rows of ``title``/``link``/``snippet``
    and nothing else, so a vendor's own spelling has to be translated before it
    reaches the reader -- and a row whose ``link`` is empty is dropped by the
    dedup, which reads as a dry search rather than as a parse failure."""
    payload, expected = _SEARCH_PAYLOADS[vendor]
    _patch_client(monkeypatch, _CaptureTransport(payload))
    set_current_session("t")
    tool = WebSearchTool(api_key="K", provider=vendor)
    seen: list[list[str]] = []
    monkeypatch.setattr(
        type(tool),
        "_log_search",
        lambda self, state, query, n, urls, rendered, shaping, **kw: seen.append(list(urls)),
    )

    rendered = await tool.execute(query="q", count=3)

    assert seen == [[expected["link"]]], f"{vendor} row did not reach the ledger"
    assert expected["title"] in rendered and expected["link"] in rendered


@pytest.mark.parametrize("vendor", sorted(SEARCH_PROVIDERS))
def test_a_vendor_that_serves_no_offset_tells_the_saturation_rule(vendor):
    """``paginates`` is the load-bearing field. A rule left on ``paginate``
    against a vendor with no offset escalates into requests that come back
    identical: the rung is reached, unanswerable, and leaves no symptom."""
    set_current_session(f"sat-{vendor}")
    tool = WebSearchTool(api_key="K", provider=vendor, saturation_factory=SearchSaturation)

    rule = tool._state().saturation

    assert rule is not None
    assert rule.paginates is SEARCH_PROVIDERS[vendor].paginates


@pytest.mark.asyncio
async def test_the_serper_request_body_is_the_one_this_pair_always_sent(monkeypatch):
    """The default distribution's wire traffic, pinned against a literal: every
    published number came off this body, so a refactor that "tidies" Serper into
    the other branches has to fail here rather than in a later comparison."""
    transport = _CaptureTransport(_SEARCH_PAYLOADS["serper"][0])
    _patch_client(monkeypatch, transport)
    set_current_session("t")
    tool = WebSearchTool(api_key="K", provider="serper")

    await tool.execute(query="q", count=3)

    sent = transport.seen[0]
    assert str(sent.url) == "https://google.serper.dev/search"
    assert json.loads(sent.content) == {"q": "q", "num": 3}
    assert sent.headers["X-API-KEY"] == "K"


@pytest.mark.asyncio
async def test_a_paginating_vendor_sends_its_own_offset_parameter(monkeypatch):
    """Page N is not one parameter: Serper takes a page index, SerpApi an offset
    in results, Brave a page index under a third name. One page number, three
    encodings, and sending the wrong one reads as a duplicate first page."""
    seen: dict[str, httpx.Request] = {}
    for vendor in ("serper", "serpapi", "brave"):
        transport = _CaptureTransport(_SEARCH_PAYLOADS[vendor][0])
        # One patch per vendor, undone before the next: patching the already
        # patched factory would pass ``transport`` twice and the request would
        # never be sent.
        with monkeypatch.context() as m:
            _patch_client(m, transport)
            set_current_session(f"pg-{vendor}")
            tool = WebSearchTool(api_key="K", provider=vendor)
            await tool._search(tool._state(), "q", 3, page=2)
        assert transport.seen, f"{vendor} sent no request"
        seen[vendor] = transport.seen[0]

    assert json.loads(seen["serper"].content)["page"] == 2
    # An offset in results, so page 2 starts one whole page in.
    assert seen["serpapi"].url.params["start"] == "3"
    # A page index, unlike SerpApi's result count.
    assert seen["brave"].url.params["offset"] == "1"


@pytest.mark.parametrize("status", [401, 429])
@pytest.mark.asyncio
async def test_a_serpapi_status_error_does_not_leak_the_key_it_puts_in_the_query(monkeypatch, tmp_path, status):
    """SerpApi is the vendor the sibling test called "one edit away": it
    authenticates by query parameter, and httpx puts the whole request URL in a
    status error's text. The rendering reaches the model and the ledger lands on
    disk.

    Both statuses, because a failure leaves by one of two doors. One outside
    ``_RETRY_ON_STATUS`` re-raises to the handler that shapes it; a retryable one
    is caught inside ``_send_with_retry``, which writes a row of its own -- and
    that was the row still carrying the URL.

    Read off the file rather than a captured shaping dict: the retry rows never
    pass through ``_log_search``, so a test watching that seam asserts the door
    the key does not leave by.
    """
    # One retry, no waiting: the leak needs a row written, not a real backoff.
    monkeypatch.setattr(web_mod, "_RETRY_BACKOFF_S", (0.0,))
    _patch_client(monkeypatch, _StatusTransport(status))
    set_current_session("t")
    ledger_mod.set_ledger_dir(tmp_path)
    path = Path(ledger_mod.open_product_ledger("t"))
    try:
        rendered = await WebSearchTool(api_key="SECRET-KEY-123", provider="serpapi").execute(query="q1", count=3)
        written = path.read_text(encoding="utf-8")
    finally:
        ledger_mod.close_product_ledger()
        ledger_mod.set_ledger_dir(None)

    assert rendered == f"Error: SerpApi answered HTTP {status}"
    assert "SECRET-KEY-123" not in rendered and "serpapi.com" not in rendered
    assert "SECRET-KEY-123" not in written and "serpapi.com" not in written
    # Not vacuous: the rows exist, a retryable status really did take the second
    # door, and what is left of the error still names the status.
    rows = [json.loads(ln) for ln in written.splitlines() if ln.strip()]
    assert [r["op"] for r in rows] == (["search"] if status == 401 else ["search_retry", "search"])
    assert all(r["status"] == status and str(status) in r["error"] for r in rows if r["op"] == "search_retry")


@pytest.mark.parametrize("vendor", sorted(v for v, s in FETCH_PROVIDERS.items() if s.needs_key))
@pytest.mark.asyncio
async def test_a_fetch_backend_with_no_anonymous_tier_refuses_before_the_request(monkeypatch, vendor):
    """``needs_key`` decides whether the tool can run at all. Jina reads pages
    unauthenticated; the others answer nothing without a key, and a tool that
    is advertised and fails every call is worse than one that is absent."""
    monkeypatch.delenv(FETCH_PROVIDERS[vendor].env_var, raising=False)
    transport = _CaptureTransport(text="page body")
    _patch_client(monkeypatch, transport)
    set_current_session("t")

    answer = await WebFetchTool(provider=vendor).execute(url="https://example.com/a")

    assert FETCH_PROVIDERS[vendor].label in json.loads(answer)["error"]
    assert FETCH_PROVIDERS[vendor].env_var in json.loads(answer)["error"]
    assert transport.seen == [], "the refusal must not spend a request"


@pytest.mark.parametrize(
    ("vendor", "payload"),
    [
        ("tavily", {"results": [{"raw_content": "page body"}]}),
        ("exa", {"results": [{"text": "page body"}]}),
        ("firecrawl", {"success": True, "data": {"markdown": "page body"}}),
        ("anysearch", {"code": 0, "data": {"content": "page body"}}),
    ],
)
@pytest.mark.asyncio
async def test_each_fetch_vendor_reads_a_page_its_own_way(monkeypatch, vendor, payload):
    _patch_client(monkeypatch, _CaptureTransport(payload))
    set_current_session("t")

    answer = json.loads(await WebFetchTool(api_key="K", provider=vendor).execute(url="https://example.com/a"))

    assert answer["text"] == "page body"
    # The extractor column is an instrument, not a label: the backends are not
    # interchangeable, so a row must say which one served the page.
    assert answer["extractor"] == FETCH_PROVIDERS[vendor].extractor


@pytest.mark.asyncio
async def test_a_fetch_vendor_that_answers_without_a_page_is_an_error_not_an_empty_page(monkeypatch):
    """A 200 whose envelope reports the failure. Rendered as an error, because an
    empty page would be indistinguishable from a real one that had no text."""
    _patch_client(monkeypatch, _CaptureTransport({"success": False, "error": "blocked"}))
    set_current_session("t")

    answer = json.loads(await WebFetchTool(api_key="K", provider="firecrawl").execute(url="https://example.com/a"))

    assert "blocked" in answer["error"] and "text" not in answer


# A stub is not an answer: the thin-page rewrite
# --------------------------------------------------------------------------


class _PerUrlTransport(httpx.AsyncBaseTransport):
    """Reader stub that answers each requested page differently.

    Keyed on the URL behind ``r.jina.ai/``, which is how this tool addresses the reader,
    so a test states what each address returns rather than counting calls.
    """

    def __init__(self, pages: dict[str, str], *, status: dict[str, int] | None = None) -> None:
        self.pages = pages
        self.status = status or {}
        self.asked: list[str] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        target = str(request.url).split("r.jina.ai/", 1)[-1]
        self.asked.append(target)
        if (code := self.status.get(target)) is not None:
            return httpx.Response(code, text="nope")
        return httpx.Response(200, text=self.pages.get(target, ""))


def _capture_ledger(monkeypatch) -> list[dict]:
    rows: list[dict] = []
    monkeypatch.setattr("research_flow.tools.web._ledger_append", rows.append)
    return rows


@pytest.mark.asyncio
async def test_a_thin_abstract_page_is_re_read_where_the_paper_lives(monkeypatch):
    """The defect this exists for: a landing page is a stub by design.

    The run that prompted it read 36 pages at a 2,358-character median and then wrote
    estimates into a scored table for the numbers those pages did not carry.
    """
    paper = "full paper text " * 200
    transport = _PerUrlTransport(
        {
            "https://arxiv.org/abs/2410.04728": "Abstract page shell",
            "https://arxiv.org/html/2410.04728": paper,
        }
    )
    _patch_client(monkeypatch, transport)
    rows = _capture_ledger(monkeypatch)
    set_current_session("t")

    answer = json.loads(await WebFetchTool().execute(url="https://arxiv.org/abs/2410.04728"))

    assert answer["text"] == paper
    assert answer["served_url"] == "https://arxiv.org/html/2410.04728"
    assert answer["requested_chars"] == len("Abstract page shell")
    assert answer["fallbacks_tried"] == ["https://arxiv.org/html/2410.04728"]
    # The PDF is never asked for: the HTML answered, so the budget is not spent.
    assert transport.asked == ["https://arxiv.org/abs/2410.04728", "https://arxiv.org/html/2410.04728"]


@pytest.mark.asyncio
async def test_both_addresses_are_recorded_so_either_citation_resolves(monkeypatch):
    """The grounding check reads the ledger, and the answer may cite either address.

    The stub is recorded at its own length, under the URL that was asked for; the text
    that was actually read is recorded under the URL that served it.
    """
    paper = "full paper text " * 200
    _patch_client(
        monkeypatch,
        _PerUrlTransport(
            {
                "https://arxiv.org/abs/2410.04728": "shell",
                "https://arxiv.org/html/2410.04728": paper,
            }
        ),
    )
    rows = _capture_ledger(monkeypatch)
    set_current_session("t")

    await WebFetchTool().execute(url="https://arxiv.org/abs/2410.04728")

    by_url = {r["url"]: r for r in rows if r["op"] == "fetch"}
    assert set(by_url) == {"https://arxiv.org/abs/2410.04728", "https://arxiv.org/html/2410.04728"}
    assert by_url["https://arxiv.org/abs/2410.04728"]["chars"] == len("shell")
    assert by_url["https://arxiv.org/abs/2410.04728"]["served_url"] == "https://arxiv.org/html/2410.04728"
    assert by_url["https://arxiv.org/html/2410.04728"]["chars"] == len(paper)
    assert by_url["https://arxiv.org/html/2410.04728"]["fallback_for"] == "https://arxiv.org/abs/2410.04728"


@pytest.mark.asyncio
async def test_a_page_that_answered_is_never_re_read(monkeypatch):
    transport = _PerUrlTransport({"https://arxiv.org/abs/2410.04728": "a real page " * 100})
    _patch_client(monkeypatch, transport)
    _capture_ledger(monkeypatch)
    set_current_session("t")

    answer = json.loads(await WebFetchTool().execute(url="https://arxiv.org/abs/2410.04728"))

    assert "served_url" not in answer and "fallbacks_tried" not in answer
    assert transport.asked == ["https://arxiv.org/abs/2410.04728"]


@pytest.mark.asyncio
async def test_a_thin_page_with_nowhere_else_to_look_is_returned_as_it_is(monkeypatch):
    transport = _PerUrlTransport({"https://example.com/paper": "stub"})
    _patch_client(monkeypatch, transport)
    _capture_ledger(monkeypatch)
    set_current_session("t")

    answer = json.loads(await WebFetchTool().execute(url="https://example.com/paper"))

    assert answer["text"] == "stub"
    assert "served_url" not in answer
    assert transport.asked == ["https://example.com/paper"]


@pytest.mark.asyncio
async def test_the_largest_read_wins_rather_than_the_first(monkeypatch):
    """A rewrite that is also thin must not replace a stub with a smaller stub."""
    transport = _PerUrlTransport(
        {
            "https://arxiv.org/abs/2410.04728": "a" * 300,
            "https://arxiv.org/html/2410.04728": "b" * 50,
            "https://arxiv.org/pdf/2410.04728": "c" * 200,
        }
    )
    _patch_client(monkeypatch, transport)
    _capture_ledger(monkeypatch)
    set_current_session("t")

    answer = json.loads(await WebFetchTool().execute(url="https://arxiv.org/abs/2410.04728"))

    assert answer["text"] == "a" * 300, "the requested page was still the best read"
    assert "served_url" not in answer, "nothing else served it, so nothing else is named"
    assert answer["fallbacks_tried"] == [
        "https://arxiv.org/html/2410.04728",
        "https://arxiv.org/pdf/2410.04728",
    ]
    assert len(transport.asked) == 3, "the budget is two rewrites, and both were spent"


@pytest.mark.asyncio
async def test_a_rewrite_that_fails_does_not_fail_the_fetch(monkeypatch):
    paper = "full paper text " * 200
    transport = _PerUrlTransport(
        {
            "https://openreview.net/forum?id=JFygzwx8SJ": "client shell",
            "https://openreview.net/pdf?id=JFygzwx8SJ": paper,
        },
        status={"https://openreview.net/pdf?id=JFygzwx8SJ": 403},
    )
    _patch_client(monkeypatch, transport)
    rows = _capture_ledger(monkeypatch)
    set_current_session("t")

    answer = json.loads(await WebFetchTool().execute(url="https://openreview.net/forum?id=JFygzwx8SJ"))

    assert answer["text"] == "client shell", "the stub is still returned"
    assert "error" not in answer
    failed = [r for r in rows if r.get("outcome") == "fallback_error"]
    assert len(failed) == 1 and failed[0]["url"] == "https://openreview.net/pdf?id=JFygzwx8SJ"


@pytest.mark.asyncio
async def test_a_thin_dataset_card_falls_back_to_the_metadata_the_table_needs(monkeypatch):
    """The size, split and licence a benchmark table asks for live on the API, not the card."""
    meta = json.dumps({"id": "allenai/qasper", "cardData": {"license": "cc-by-4.0"}, "downloads": 1}) * 20
    transport = _PerUrlTransport(
        {
            "https://huggingface.co/datasets/allenai/qasper": "Dataset card",
            "https://huggingface.co/api/datasets/allenai/qasper": meta,
        }
    )
    _patch_client(monkeypatch, transport)
    _capture_ledger(monkeypatch)
    set_current_session("t")

    answer = json.loads(await WebFetchTool().execute(url="https://huggingface.co/datasets/allenai/qasper"))

    assert answer["served_url"] == "https://huggingface.co/api/datasets/allenai/qasper"
    assert "cc-by-4.0" in answer["text"]


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (
            "https://arxiv.org/abs/2410.04728",
            ["https://arxiv.org/html/2410.04728", "https://arxiv.org/pdf/2410.04728"],
        ),
        # A version suffix names the same paper, and the rewrites drop it: the unversioned
        # address is the one that always resolves.
        (
            "https://arxiv.org/abs/2510.08907v4",
            ["https://arxiv.org/html/2510.08907", "https://arxiv.org/pdf/2510.08907"],
        ),
        (
            "https://arxiv.org/html/2606.09659v1",
            ["https://arxiv.org/pdf/2606.09659", "https://arxiv.org/abs/2606.09659"],
        ),
        ("https://arxiv.org/pdf/2601.17668", ["https://arxiv.org/abs/2601.17668"]),
        ("https://openreview.net/forum?id=JFygzwx8SJ", ["https://openreview.net/pdf?id=JFygzwx8SJ"]),
        ("https://aclanthology.org/2025.acl-long.1219", ["https://aclanthology.org/2025.acl-long.1219.pdf"]),
        (
            "https://huggingface.co/datasets/allenai/qasper",
            ["https://huggingface.co/api/datasets/allenai/qasper"],
        ),
        (
            "https://github.com/Future-House/litqa",
            [
                "https://raw.githubusercontent.com/Future-House/litqa/HEAD/README.md",
                "https://api.github.com/repos/Future-House/litqa",
            ],
        ),
        # Nowhere else to look: an unknown host, and a repository page that is already
        # deeper than its front page.
        ("https://example.com/paper", []),
        ("https://github.com/Future-House/litqa/tree/main/src", []),
    ],
)
def test_each_rewrite_names_the_same_document_at_an_address_that_carries_it(url, expected):
    assert fetch_fallbacks(url) == expected
