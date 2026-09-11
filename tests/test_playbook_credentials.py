"""The machine-held credentials of one playbook's carried servers (raven/playbook/credentials.py).

Scoped by playbook, read back only for params the spec declares ``secret``,
written 0600. The OAuth half reuses ``FileTokenStorage``'s file under the same
scope, so a carried server and a host server of one name never share a file.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from raven.config.schema import MCPServerConfig
from raven.mcp.oauth import credentials_path, has_stored_tokens
from raven.playbook import NodeSpec, ParamSpec, PlaybookSpec, Triggers
from raven.playbook.credentials import (
    clear_oauth_tokens,
    clear_secret_param,
    credential_scope,
    has_oauth_tokens,
    playbook_credentials_dir,
    set_secret_param,
    stored_secret_param_names,
    stored_secret_params,
)


@pytest.fixture(autouse=True)
def _home(tmp_path, monkeypatch):
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
    return tmp_path


def _spec(**params: ParamSpec) -> PlaybookSpec:
    return PlaybookSpec(
        name="competitor-scan",
        description="d",
        task_summary="t",
        version=1,
        mode="dag",
        confirm=False,
        triggers=Triggers(keywords=["scan"]),
        params=params,
        mcp_servers={"local-pg": MCPServerConfig(command="pg-mcp", env={"PGPASSWORD": "{{ params.PG_PASSWORD }}"})},
        nodes=[NodeSpec(id="a", subagent="x", node_summary="s", prompt_template="p", mcps=["local-pg"])],
    )


def test_a_set_secret_is_read_back_for_the_param_that_declares_it():
    spec = _spec(PG_PASSWORD=ParamSpec(type="secret", required=True, description="db password"))
    set_secret_param(spec.name, "PG_PASSWORD", "hunter2")

    assert stored_secret_params(spec) == {"PG_PASSWORD": "hunter2"}
    assert stored_secret_param_names(spec.name) == {"PG_PASSWORD"}


def test_a_stored_value_is_ignored_for_a_param_that_is_not_secret():
    spec = _spec(PG_PASSWORD=ParamSpec(type="string", required=True, description="not a secret"))
    set_secret_param(spec.name, "PG_PASSWORD", "hunter2")

    assert stored_secret_params(spec) == {}


def test_a_stored_value_for_an_undeclared_name_is_ignored():
    spec = _spec(PG_PASSWORD=ParamSpec(type="secret", required=True, description="db password"))
    set_secret_param(spec.name, "OTHER", "x")

    assert stored_secret_params(spec) == {}
    # Names-only view still lists it: the page shows what is stored, the run
    # decides what it may use.
    assert stored_secret_param_names(spec.name) == {"OTHER"}


def test_the_params_file_is_private_and_lives_under_the_playbook_scope(_home):
    set_secret_param("competitor-scan", "PG_PASSWORD", "hunter2")
    path = playbook_credentials_dir("competitor-scan") / "params.json"

    assert path.is_file()
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert path.parent == _home / "credentials" / "playbooks" / "competitor-scan"
    assert json.loads(path.read_text()) == {"PG_PASSWORD": "hunter2"}


def test_clearing_removes_only_that_param():
    set_secret_param("competitor-scan", "A", "1")
    set_secret_param("competitor-scan", "B", "2")
    clear_secret_param("competitor-scan", "A")

    assert stored_secret_param_names("competitor-scan") == {"B"}
    clear_secret_param("competitor-scan", "nope")
    assert stored_secret_param_names("competitor-scan") == {"B"}


def test_setting_an_empty_value_clears_it():
    set_secret_param("competitor-scan", "A", "1")
    set_secret_param("competitor-scan", "A", "")
    assert stored_secret_param_names("competitor-scan") == frozenset()


def test_two_playbooks_do_not_see_each_others_values():
    set_secret_param("alpha", "TOKEN", "a")
    set_secret_param("beta", "TOKEN", "b")
    assert stored_secret_param_names("alpha") == {"TOKEN"}
    spec_b = _spec(TOKEN=ParamSpec(type="secret", description="t"))
    spec_b = spec_b.model_copy(update={"name": "beta"})
    assert stored_secret_params(spec_b) == {"TOKEN": "b"}


def test_a_playbook_name_cannot_escape_the_credentials_root(_home):
    d = playbook_credentials_dir("../../etc")
    assert (_home / "credentials" / "playbooks") in d.parents


def test_oauth_tokens_are_scoped_to_the_playbook_not_the_bare_name(_home):
    # The host's own sentry is authorized; the carried sentry is not. Neither
    # reads the other's file.
    credentials_path("sentry").write_text(json.dumps({"tokens": {"access_token": "host"}}))
    assert has_stored_tokens("sentry") is True
    assert has_oauth_tokens("sentry", "competitor-scan") is False

    scoped = credentials_path("sentry", scope=credential_scope("competitor-scan"))
    scoped.write_text(json.dumps({"tokens": {"access_token": "carried"}}))
    assert has_oauth_tokens("sentry", "competitor-scan") is True
    assert scoped == _home / "credentials" / "playbooks" / "competitor-scan" / "mcp" / "sentry.json"

    clear_oauth_tokens("sentry", "competitor-scan")
    assert has_oauth_tokens("sentry", "competitor-scan") is False
    assert has_stored_tokens("sentry") is True


def test_tokens_without_an_access_token_do_not_count(_home):
    scoped = credentials_path("sentry", scope=credential_scope("competitor-scan"))
    scoped.write_text(json.dumps({"client_info": {"client_id": "c"}, "tokens": {}}))
    assert has_oauth_tokens("sentry", "competitor-scan") is False
    scoped.write_text("not json")
    assert has_oauth_tokens("sentry", "competitor-scan") is False


def test_a_server_name_that_is_a_path_reaches_no_credential_file(tmp_path, monkeypatch):
    # A playbook is distributed data. ``../../mcp/host`` under a playbook scope
    # would resolve to the host's own token file, so the rule holds on every
    # read and write: a server name is a filename, never a path.
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
    from raven.mcp.oauth import credentials_path

    host_file = credentials_path("host")
    host_file.write_text(json.dumps({"tokens": {"access_token": "host-token"}}))

    assert has_oauth_tokens("../../mcp/host", "portable") is False
    with pytest.raises(ValueError):
        clear_oauth_tokens("../../mcp/host", "portable")
    with pytest.raises(ValueError):
        credentials_path("..", scope="playbooks/portable")
    assert host_file.exists()


def test_a_playbook_carrying_a_path_like_server_name_is_refused():
    from pydantic import ValidationError

    from raven.playbook.types import PlaybookSpec

    def spec(server: str):
        return PlaybookSpec.model_validate(
            {
                "name": "portable",
                "description": "carries one server",
                "version": 1,
                "mode": "dag",
                "confirm": False,
                "taskSummary": "reach the carried server",
                "triggers": {"keywords": ["portable"]},
                "mcpServers": {server: {"type": "streamableHttp", "url": "https://mcp.example.test/mcp"}},
                "nodes": [
                    {
                        "id": "a",
                        "subagent": "raven",
                        "nodeSummary": "reach it",
                        "promptTemplate": "call it",
                        "mcps": [server],
                    }
                ],
            }
        )

    assert "my.server-1" in spec("my.server-1").mcp_servers
    for bad in ("../../mcp/host", "..", ".hidden", "a/b"):
        with pytest.raises(ValidationError, match="mcpServers name"):
            spec(bad)
