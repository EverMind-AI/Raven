"""Tests for the ``playbooks.*`` RPC handlers.

Three things the page depends on and nothing else asserts:

* the list carries each graph's *shape* (a card draws a diagram per row, and a
  card that fetched its own graph would make opening the page N round trips);
* a file that will not parse comes back as a row with ``error`` set, not as a
  failed call -- the library is two directories of hand-edited text;
* the detail keeps a blank field blank. ``subagent`` / ``node_summary`` /
  ``prompt_template`` are the three an author may leave for the caller to fill,
  and a wire that omitted them would read as "no such field".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from raven.config.schema import MCPServerConfig
from raven.playbook import NodeSpec, ParamSpec, PlaybookSpec, PlaybookStore, Triggers
from raven.rpc.errors import RpcError
from raven.rpc.methods import playbooks as mod
from raven.rpc.models import METHOD_MODELS


def _spec(name: str = "competitor-scan") -> PlaybookSpec:
    return PlaybookSpec(
        name=name,
        description="research one competitor's market and tech sides, then merge",
        task_summary="research the named competitor and report what was found",
        mode="dag",
        triggers=Triggers(keywords=["competitor"]),
        params={"target": ParamSpec(type="string", required=True, description="which competitor to scan")},
        nodes=[
            NodeSpec(id="market", subagent="Raven", node_summary="the market side", prompt_template="market"),
            NodeSpec(id="tech", subagent="Raven", node_summary="the tech side", prompt_template="tech"),
            NodeSpec(
                id="merge",
                subagent="Raven",
                node_summary="merge both",
                prompt_template="merge {{ market.output }} {{ tech.output }}",
                depends_on=["market", "tech"],
                instance="w1",
                skills=["web-research"],
            ),
        ],
    )


@pytest.fixture
def library(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> PlaybookStore:
    """A two-layer store on tmp, installed as the one the handlers resolve.

    The builtin layer is pinned empty so these assertions stay true when the
    package ships builtin playbooks.
    """
    store = PlaybookStore(tmp_path / "user", builtin_root=tmp_path / "builtin")
    monkeypatch.setattr(mod, "_store", lambda: store)
    monkeypatch.setattr(mod, "_disabled", lambda: set())
    return store


async def test_list_carries_the_graph_shape(library: PlaybookStore) -> None:
    library.save(_spec())
    rows = (await mod.playbooks_list({}))["playbooks"]
    assert [r["name"] for r in rows] == ["competitor-scan"]
    row = rows[0]
    assert row["error"] == ""
    assert row["mode"] == "dag"
    assert [(n["id"], n["depends_on"]) for n in row["nodes"]] == [
        ("market", []),
        ("tech", []),
        ("merge", ["market", "tech"]),
    ]
    # Shape only: the card has no use for prompts, and the library would
    # otherwise ship every template on page open.
    assert set(row["nodes"][0]) == {"id", "depends_on"}
    METHOD_MODELS["playbooks.list"][1].model_validate({"playbooks": rows})


async def test_unreadable_file_is_a_row_not_a_failure(library: PlaybookStore) -> None:
    library.save(_spec())
    broken = library.path_for("broken-one")
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_text("not a playbook at all\n", encoding="utf-8")

    rows = {r["name"]: r for r in (await mod.playbooks_list({}))["playbooks"]}
    assert rows["competitor-scan"]["error"] == ""
    assert rows["broken-one"]["error"]
    assert rows["broken-one"]["nodes"] == []
    METHOD_MODELS["playbooks.list"][1].model_validate({"playbooks": list(rows.values())})


async def test_get_answers_one_whole_spec(library: PlaybookStore) -> None:
    library.save(_spec())
    got = (await mod.playbooks_get({"name": "competitor-scan"}))["playbook"]
    assert got["keywords"] == ["competitor"]
    assert got["params"]["target"]["required"] is True
    assert got["params"]["target"]["description"] == "which competitor to scan"
    merge = [n for n in got["nodes"] if n["id"] == "merge"][0]
    assert merge["depends_on"] == ["market", "tech"]
    assert merge["instance"] == "w1"
    assert merge["skills"] == ["web-research"]
    # Not written by the author: the key is absent, which is how the contract
    # spells it and what the typed client expects. Told apart from `[]`.
    assert "mcps" not in merge
    assert got["path"].endswith("competitor-scan/playbook.md")
    METHOD_MODELS["playbooks.get"][1].model_validate({"playbook": got})


async def test_get_answers_the_servers_the_playbook_carries(library: PlaybookStore) -> None:
    """A node's ``mcps`` entry is only a name, and the same name may be a server
    the machine configures -- a different process, reached differently. Without
    the definitions a reader of the library cannot tell which one a step reaches,
    which is what the handler's "one whole spec" contract promises.

    Reported as the file declares them: a carried server references a credential
    through ``{{ params.X }}`` and the run supplies it, so what goes out is the
    reference. Nothing here is ever a secret's value -- there is none to resolve
    at this point, and there must never be one.
    """
    spec = _spec()
    spec.params["PG_PASSWORD"] = ParamSpec(type="secret", description="the database password")
    spec.mcp_servers = {
        "local-pg": MCPServerConfig(
            command="pg-mcp",
            args=["--db", "analytics"],
            env={"PGPASSWORD": "{{ params.PG_PASSWORD }}"},
        )
    }
    spec.nodes[0].mcps = ["local-pg"]
    library.save(spec, overwrite=True)

    got = (await mod.playbooks_get({"name": "competitor-scan"}))["playbook"]

    carried = got["mcp_servers"]["local-pg"]
    assert carried["command"] == "pg-mcp"
    assert carried["args"] == ["--db", "analytics"]
    assert carried["env"] == {"PGPASSWORD": "{{ params.PG_PASSWORD }}"}
    assert carried["url"] == ""
    METHOD_MODELS["playbooks.get"][1].model_validate({"playbook": got})


async def test_get_carries_every_field_the_runtime_reads(library: PlaybookStore) -> None:
    """A partial projection is worse than none: it reads as a complete answer.

    ``resolve_transport`` consumes ``type``, grant resolution consumes ``enabled``
    and ``auth``, and a tool call consumes ``tool_timeout``. Dropping them showed
    a disabled SSE server carrying OAuth as a launchable generic http one -- every
    field a reader would use to decide whether to trust the step, wrong.

    ``oauth`` is the one thing reported as a boolean rather than passed through:
    the endpoints and any client id are the deployment's business, and a reader
    only needs to know the file carries its own.
    """
    spec = _spec()
    spec.mcp_servers = {
        "quiet": MCPServerConfig(
            type="sse",
            url="https://svc.test/sse",
            enabled=False,
            auth="oauth",
            tool_timeout=7,
        )
    }
    library.save(spec, overwrite=True)

    got = (await mod.playbooks_get({"name": "competitor-scan"}))["playbook"]

    carried = got["mcp_servers"]["quiet"]
    assert carried["type"] == "sse", "an sse server presented as http is a different protocol"
    assert carried["enabled"] is False
    assert carried["auth"] == "oauth"
    assert carried["tool_timeout"] == 7
    assert carried["has_oauth_config"] is False, "this one declares none of its own"
    METHOD_MODELS["playbooks.get"][1].model_validate({"playbook": got})


async def test_get_reports_a_self_declared_oauth_server_without_its_endpoints(library: PlaybookStore) -> None:
    """Whether one is declared, never what it is."""
    from raven.config.schema import MCPOAuthConfig

    spec = _spec()
    spec.mcp_servers = {
        # All three, because that is what the OAuth path requires before it will
        # use a declaration as written.
        "own": MCPServerConfig(
            url="https://svc.test/mcp",
            auth="oauth",
            oauth=MCPOAuthConfig(
                issuer="https://svc.test",
                authorization_endpoint="https://svc.test/authorize",
                token_endpoint="https://svc.test/token",
            ),
        ),
        # Two of the three. `_seed_for` ignores a partial document and runs
        # discovery, so calling this one self-carried would describe a server
        # that does not exist.
        "partial": MCPServerConfig(
            url="https://part.test/mcp",
            auth="oauth",
            oauth=MCPOAuthConfig(issuer="https://part.test", authorization_endpoint="https://part.test/authorize"),
        ),
    }
    library.save(spec, overwrite=True)

    carried = (await mod.playbooks_get({"name": "competitor-scan"}))["playbook"]["mcp_servers"]

    assert carried["own"]["has_oauth_config"] is True
    assert carried["partial"]["has_oauth_config"] is False, "a partial document is not a document"
    assert "svc.test/authorize" not in json.dumps(carried), "the endpoints are not the reader's business"


async def test_get_reports_the_transport_the_runtime_will_pick(library: PlaybookStore) -> None:
    """Not the raw field, and never ``null``.

    ``type`` is optional in the file and the runtime derives it -- a url ending
    ``/sse`` resolves to ``sse``, not to streamable http. Emitting the unwritten
    field verbatim put ``null`` on the wire, which the contract does not allow
    (three strings or an absent key), and left the reader to redo a guess this
    already knows the answer to. Two readers guessing separately is how the page
    came to label an sse server as http.
    """
    spec = _spec()
    spec.mcp_servers = {
        "sse-by-url": MCPServerConfig(url="https://svc.test/sse"),
        "http-by-url": MCPServerConfig(url="https://svc.test/mcp"),
        "stdio-by-command": MCPServerConfig(command="pg-mcp"),
        "declared": MCPServerConfig(type="streamableHttp", url="https://svc.test/sse"),
    }
    library.save(spec, overwrite=True)

    carried = (await mod.playbooks_get({"name": "competitor-scan"}))["playbook"]["mcp_servers"]

    assert carried["sse-by-url"]["type"] == "sse"
    assert carried["http-by-url"]["type"] == "streamableHttp"
    assert carried["stdio-by-command"]["type"] == "stdio"
    # A declared value is the author's answer and is not re-derived.
    assert carried["declared"]["type"] == "streamableHttp"
    for entry in carried.values():
        assert entry["type"] is not None, "null is not one of the shapes the contract allows"
    METHOD_MODELS["playbooks.get"][1].model_validate(
        {"playbook": {**(await mod.playbooks_get({"name": "competitor-scan"}))["playbook"]}}
    )


async def test_a_definition_with_neither_command_nor_url_never_reaches_the_detail(
    library: PlaybookStore,
) -> None:
    """Which is why the detail has no transport-less shape to report.

    ``load_playbook`` drops a definition it cannot honour and logs it, so a
    server with no command and no url is gone before this handler sees it. The
    key would be absent if one ever arrived -- ``null`` is not one of the shapes
    the contract allows -- but the load path is what makes that unreachable.
    """
    spec = _spec()
    spec.mcp_servers = {"empty": MCPServerConfig(), "real": MCPServerConfig(url="https://svc.test/mcp")}
    library.save(spec, overwrite=True)

    carried = (await mod.playbooks_get({"name": "competitor-scan"}))["playbook"]["mcp_servers"]
    assert "empty" not in carried, "an unusable definition is dropped at load, not reported"
    assert carried["real"]["type"] == "streamableHttp"


async def test_get_reports_no_carried_servers_as_an_empty_mapping(library: PlaybookStore) -> None:
    """Which is most playbooks: every ``mcps`` name resolves against the machine."""
    library.save(_spec())
    got = (await mod.playbooks_get({"name": "competitor-scan"}))["playbook"]
    assert got["mcp_servers"] == {}
    METHOD_MODELS["playbooks.get"][1].model_validate({"playbook": got})


async def test_get_reports_the_version_the_file_declares(library: PlaybookStore) -> None:
    """Read from the spec, not pinned to the constant the writer happened to use."""
    spec = _spec()
    spec.version = 7
    library.save(spec, overwrite=True)

    got = (await mod.playbooks_get({"name": "competitor-scan"}))["playbook"]
    assert got["version"] == 7
    METHOD_MODELS["playbooks.get"][1].model_validate({"playbook": got})


async def test_get_keeps_a_blank_field_blank(library: PlaybookStore) -> None:
    spec = _spec()
    # A blank is an empty string on the model; the wire keeps it that way.
    spec.nodes.append(NodeSpec(id="angle"))
    library.save(spec, overwrite=True)

    got = (await mod.playbooks_get({"name": "competitor-scan"}))["playbook"]
    angle = [n for n in got["nodes"] if n["id"] == "angle"][0]
    assert angle["subagent"] == ""
    assert angle["node_summary"] == ""
    assert angle["prompt_template"] == ""
    METHOD_MODELS["playbooks.get"][1].model_validate({"playbook": got})


async def test_get_refuses_an_unknown_name(library: PlaybookStore) -> None:
    with pytest.raises(RpcError):
        await mod.playbooks_get({"name": "nope"})
    with pytest.raises(RpcError):
        await mod.playbooks_get({})


# ── playbooks.credentials.* / playbooks.oauth.* ────────────────────────────────


def _carried_spec(name: str = "carried") -> PlaybookSpec:
    return PlaybookSpec(
        name=name,
        description="a playbook that carries its own servers",
        task_summary="prove the carried servers reach a node",
        mode="dag",
        triggers=Triggers(keywords=["carried"]),
        params={
            "PROBE_TOKEN": ParamSpec(type="secret", required=True, description="the bearer the server demands"),
            "topic": ParamSpec(type="string", required=False, description="not a secret"),
        },
        mcp_servers={
            "tokened": MCPServerConfig(
                type="streamableHttp",
                url="http://127.0.0.1:8932/mcp",
                headers={"Authorization": "Bearer {{ params.PROBE_TOKEN }}"},
            ),
            "sentry": MCPServerConfig(type="streamableHttp", url="https://mcp.sentry.dev/mcp", auth="oauth"),
            "memory": MCPServerConfig(command="npx", args=["-y", "@modelcontextprotocol/server-memory"]),
        },
        nodes=[NodeSpec(id="a", subagent="Raven", node_summary="s", prompt_template="p", mcps=["tokened", "sentry"])],
    )


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(mod, "_host_mcp_server_names", lambda: ["sentry"])
    return tmp_path / "home"


async def test_credentials_get_names_what_is_held_and_never_a_value(library: PlaybookStore, home: Path) -> None:
    from raven.playbook.credentials import set_secret_param

    library.save(_carried_spec())
    set_secret_param("carried", "PROBE_TOKEN", "s3cr3t")

    out = await mod.playbooks_credentials_get({"name": "carried"})

    assert out["params"] == [{"name": "PROBE_TOKEN", "set": True, "description": "the bearer the server demands"}]
    assert "s3cr3t" not in str(out)
    by_name = {s["name"]: s for s in out["servers"]}
    assert by_name["sentry"] == {
        "name": "sentry",
        "auth": "oauth",
        "enabled": True,
        "authorized": False,
        "shadows_host": True,
    }
    assert by_name["tokened"]["auth"] == "none" and by_name["tokened"]["shadows_host"] is False
    assert by_name["memory"]["authorized"] is False


async def test_credentials_get_reports_an_authorized_carried_oauth_server(library: PlaybookStore, home: Path) -> None:
    from raven.mcp.oauth import credentials_path
    from raven.playbook.credentials import credential_scope

    library.save(_carried_spec())
    credentials_path("sentry", scope=credential_scope("carried")).write_text(
        json.dumps({"tokens": {"access_token": "t"}})
    )
    out = await mod.playbooks_credentials_get({"name": "carried"})
    assert {s["name"]: s["authorized"] for s in out["servers"]}["sentry"] is True
    # The host's own file is untouched and unread.
    assert not credentials_path("sentry").exists()


async def test_credentials_set_and_clear_round_trip(library: PlaybookStore, home: Path) -> None:
    from raven.playbook.credentials import stored_secret_param_names

    library.save(_carried_spec())
    assert await mod.playbooks_credentials_set({"name": "carried", "param": "PROBE_TOKEN", "value": "v"}) == {
        "ok": True
    }
    assert stored_secret_param_names("carried") == {"PROBE_TOKEN"}
    assert await mod.playbooks_credentials_clear({"name": "carried", "param": "PROBE_TOKEN"}) == {"ok": True}
    assert stored_secret_param_names("carried") == frozenset()


async def test_credentials_set_refuses_a_param_the_spec_does_not_declare_secret(
    library: PlaybookStore, home: Path
) -> None:
    library.save(_carried_spec())
    with pytest.raises(RpcError, match="no secret param named topic"):
        await mod.playbooks_credentials_set({"name": "carried", "param": "topic", "value": "v"})
    with pytest.raises(RpcError, match="no secret param named nope"):
        await mod.playbooks_credentials_set({"name": "carried", "param": "nope", "value": "v"})
    with pytest.raises(RpcError, match="value is required"):
        await mod.playbooks_credentials_set({"name": "carried", "param": "PROBE_TOKEN", "value": ""})


async def test_credentials_methods_refuse_an_unknown_playbook(library: PlaybookStore, home: Path) -> None:
    with pytest.raises(RpcError, match="no playbook named ghost"):
        await mod.playbooks_credentials_get({"name": "ghost"})
    with pytest.raises(RpcError, match="name is required"):
        await mod.playbooks_credentials_get({})


async def test_oauth_authorize_refuses_a_server_that_is_not_carried_or_not_oauth(
    library: PlaybookStore, home: Path
) -> None:
    library.save(_carried_spec())
    with pytest.raises(RpcError, match="carries no MCP server named ghost"):
        await mod.playbooks_oauth_authorize({"name": "carried", "server": "ghost"})
    with pytest.raises(RpcError, match="only an oauth server"):
        await mod.playbooks_oauth_authorize({"name": "carried", "server": "tokened"})


async def test_oauth_authorize_runs_a_scoped_throwaway_manager(library: PlaybookStore, home: Path, monkeypatch) -> None:
    """The flow is driven by a manager keyed to the playbook's credential scope,
    never the host's manager, and the call answers with the parked URL."""
    from raven.mcp import manager as manager_mod

    library.save(_carried_spec())
    built: list[dict] = []

    class FakeManager:
        def __init__(self, registry, **kwargs):
            built.append(kwargs)
            self._state = None

        def status(self):
            return [{"name": "sentry", "state": self._state}] if self._state else []

        async def connect(self, name, cfg, **kwargs):
            self._state = "auth_required"
            return {"name": name, "state": "auth_required", "error": "needs authorization"}

        async def aclose(self):
            pass

    monkeypatch.setattr(manager_mod, "MCPConnectionManager", FakeManager)
    monkeypatch.setattr(
        "raven.mcp.oauth.pending_url", lambda server: "https://auth.example/consent" if server == "sentry" else None
    )
    monkeypatch.setattr("raven.market.connect.CONNECT_WAIT", 0.2)

    out = await mod.playbooks_oauth_authorize({"name": "carried", "server": "sentry"})

    assert out["server"] == "sentry"
    assert out["state"] == "auth_required"
    assert out["auth_url"] == "https://auth.example/consent"
    assert built and built[0]["credential_scope"] == "playbooks/carried"


async def test_oauth_authorize_says_why_a_connect_failed_inside_the_window(
    library: PlaybookStore, home: Path, monkeypatch
) -> None:
    """A connect that failed rather than parked must carry its reason to the
    caller. Unretrieved, the exception is only an asyncio warning on shutdown
    and the page shows a bare "error" with the cause in a log nobody reads."""
    from raven.mcp import manager as manager_mod

    library.save(_carried_spec())
    closed: list[bool] = []

    class FailingManager:
        def __init__(self, registry, **kwargs):
            pass

        def status(self):
            return []

        async def connect(self, name, cfg, **kwargs):
            raise RuntimeError("the sandbox could not start: no such image")

        async def aclose(self):
            closed.append(True)

    monkeypatch.setattr(manager_mod, "MCPConnectionManager", FailingManager)
    monkeypatch.setattr("raven.market.connect.CONNECT_WAIT", 0.2)

    from raven.rpc.dispatcher import Dispatcher

    d = Dispatcher()
    mod.register_playbooks_methods(d)
    resp = await d.dispatch(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "playbooks.oauth.authorize",
            "params": {"name": "carried", "server": "sentry"},
        }
    )

    # Through the real dispatcher, because that is the boundary that loses a
    # diagnostic: anything but this surface's own vocabulary arrives as
    # internal_error with the reason in a traceback tail the page drops.
    # Through the frame the page actually receives: the reason has to be
    # somewhere the page shows. It toasts result.error for a state that is not
    # connected, and for a raised error it toasts only the code name, so the
    # reason rides in the result.
    assert "error" not in resp, resp
    assert resp["result"]["state"] == "error"
    assert "no such image" in resp["result"]["error"], resp
    assert resp["result"]["auth_url"] is None
    assert closed, "the throwaway manager is closed even when the connect raises"


async def test_oauth_clear_drops_the_scoped_tokens_only(library: PlaybookStore, home: Path) -> None:
    from raven.mcp.oauth import credentials_path
    from raven.playbook.credentials import credential_scope

    library.save(_carried_spec())
    host = credentials_path("sentry")
    host.write_text(json.dumps({"tokens": {"access_token": "host"}}))
    scoped = credentials_path("sentry", scope=credential_scope("carried"))
    scoped.write_text(json.dumps({"tokens": {"access_token": "carried"}}))

    assert await mod.playbooks_oauth_clear({"name": "carried", "server": "sentry"}) == {"ok": True}

    assert not scoped.exists()
    assert host.exists()


def test_the_new_methods_are_declared_in_the_contract() -> None:
    for method in (
        "playbooks.credentials.get",
        "playbooks.credentials.set",
        "playbooks.credentials.clear",
        "playbooks.oauth.authorize",
        "playbooks.oauth.clear",
    ):
        assert method in METHOD_MODELS
