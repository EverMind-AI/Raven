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
from research_flow.tools.ask_user import DRAskUserTool  # noqa: E402
from research_flow.tools.web import _UNRESOLVED_REFUSAL, WebFetchTool, WebSearchTool, set_current_session  # noqa: E402

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


def test_the_proxy_reaches_both_tools(tmp_path):
    slice_ = {"enabled": True, "search": {"apiKey": "k"}, "proxy": "http://127.0.0.1:7890", **_NO_LLM_GATES}
    ctx = _ctx(tmp_path, slice_)

    assert make_web_search(ctx).proxy == "http://127.0.0.1:7890"
    assert make_web_fetch(ctx).proxy == "http://127.0.0.1:7890"


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
