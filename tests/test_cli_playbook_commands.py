"""CLI tests for ``raven playbook``.

Each subcommand is covered against a temp two-layer library: a tmp user layer
via ``playbooks.dir`` and a tmp builtin layer via the module constant. The
generator and the run machinery are faked at the seams the commands import
through (``raven.playbook`` attributes, ``make_provider``), so no test needs
a provider or spawns anything.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app
from raven.config.loader import set_config_path
from raven.playbook import ExecutionPlan, GeneratedPlaybook, NodeSpec, ParamSpec, PlaybookSpec, Triggers

runner = CliRunner()


def _write_md(root: Path, name: str, description: str) -> None:
    target = root / name
    target.mkdir(parents=True)
    (target / "playbook.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nbody\n\n"
        "```yaml playbook-spec\n"
        "version: 1\nmode: prompt\nconfirm: true\n"
        "taskSummary: research the named topic and report back\n"
        f"triggers:\n  keywords: [{name}]\n"
        "prompts: one research node\n"
        "```\n",
        encoding="utf-8",
    )


@pytest.fixture
def library(tmp_path: Path, monkeypatch):
    """A config file wired to tmp layers; returns the paths for seeding."""
    user_root = tmp_path / "user"
    builtin_root = tmp_path / "builtin"
    user_root.mkdir()
    builtin_root.mkdir()
    monkeypatch.setattr("raven.playbook.store.BUILTIN_ROOT", builtin_root)
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"playbooks": {"dir": str(user_root)}}), encoding="utf-8")
    set_config_path(cfg)
    yield {"user": user_root, "builtin": builtin_root, "config": cfg}
    set_config_path(None)  # type: ignore[arg-type]


def _disabled_in(cfg: Path) -> list[str]:
    return json.loads(cfg.read_text(encoding="utf-8")).get("playbooks", {}).get("disabled", [])


def test_playbook_help_lists_all_subcommands():
    r = runner.invoke(app, ["playbook", "--help"])
    assert r.exit_code == 0
    for sub in ("list", "get", "validate", "create", "enable", "disable", "run", "delete", "secret", "auth"):
        assert sub in r.stdout, f"missing subcommand in --help: {sub}"


def test_list_shows_both_layers_with_origin_and_state(library):
    _write_md(library["builtin"], "briefing", "the shipped one")
    _write_md(library["user"], "mine", "the user one")
    library["config"].write_text(
        json.dumps({"playbooks": {"dir": str(library["user"]), "disabled": ["mine"]}}), encoding="utf-8"
    )

    r = runner.invoke(app, ["playbook", "list"])
    assert r.exit_code == 0
    assert "briefing" in r.stdout and "builtin" in r.stdout
    assert "mine" in r.stdout and "user" in r.stdout
    assert "disabled" in r.stdout


def test_get_prints_the_raw_file(library):
    _write_md(library["user"], "mine", "the user one")
    r = runner.invoke(app, ["playbook", "get", "mine"])
    assert r.exit_code == 0
    assert "```yaml playbook-spec" in r.stdout
    assert "description: the user one" in r.stdout


def test_get_unknown_name_fails(library):
    r = runner.invoke(app, ["playbook", "get", "ghost"])
    assert r.exit_code == 1


def test_validate_accepts_a_good_file_by_path_and_by_name(library):
    _write_md(library["user"], "mine", "fine")
    by_name = runner.invoke(app, ["playbook", "validate", "mine"])
    assert by_name.exit_code == 0, by_name.stdout
    by_path = runner.invoke(app, ["playbook", "validate", str(library["user"] / "mine" / "playbook.md")])
    assert by_path.exit_code == 0, by_path.stdout
    assert "OK" in by_path.stdout


def test_validate_reports_errors_with_the_file_position(library):
    target = library["user"] / "broken"
    target.mkdir()
    (target / "playbook.md").write_text(
        "---\nname: broken\ndescription: bad mode\n---\n\nbody\n\n"
        "```yaml playbook-spec\n"
        "version: 1\nmode: nonsense\nconfirm: true\n"
        "triggers:\n  keywords: [broken]\n"
        "prompts: whatever\n"
        "```\n",
        encoding="utf-8",
    )
    r = runner.invoke(app, ["playbook", "validate", "broken"])
    assert r.exit_code == 1
    # The full path, not its suffix: a soft-wrap regression folds the line at
    # a position that depends on the tmp path's length, so a suffix check
    # only catches it when the fold happens to land inside those characters.
    assert str(target / "playbook.md") in r.stdout
    assert "mode" in r.stdout


class _FakeGenerator:
    def __init__(self, *args, **kwargs):
        pass

    async def generate(self, user_input: str, skills=None) -> GeneratedPlaybook:
        spec = PlaybookSpec(
            name="placeholder",
            description="generated from: " + user_input.splitlines()[0][:40],
            task_summary="scan the named target and report findings",
            mode="dag",
            triggers=Triggers(keywords=["weekly scan"]),
            nodes=[
                NodeSpec(
                    id="scan",
                    subagent="research-raven",
                    node_summary="scan the target",
                    prompt_template="scan ${params.target}",
                )
            ],
            params={"target": ParamSpec(required=True, description="what to scan?")},
        )
        return GeneratedPlaybook(spec=spec, notes=["Assumption: weekly cadence", "Missing capability: mcp[fs]"])


def test_create_lands_in_the_user_layer_usable(library, monkeypatch):
    """Both creation entries land the same way. Switching the name off here would
    make a playbook the user just created immediately invisible to a running
    agent, since the deny list is read live -- and the undo for that is the
    command this one would be telling them to run."""
    monkeypatch.setattr("raven.providers.factory.make_provider", lambda config: object())
    monkeypatch.setattr("raven.playbook.PlaybookGenerator", _FakeGenerator)

    r = runner.invoke(app, ["playbook", "create", "weekly-scan", "--input", "scan competitors weekly"])
    assert r.exit_code == 0, r.stdout
    assert (library["user"] / "weekly-scan" / "playbook.md").exists()
    assert _disabled_in(library["config"]) == []
    text = (library["user"] / "weekly-scan" / "playbook.md").read_text(encoding="utf-8")
    assert "name: weekly-scan" in text
    assert "Assumption: weekly cadence" in text
    assert "Usable now" in r.stdout
    assert "disable weekly-scan" in r.stdout
    # The bracketed capability name is the diagnostic: Rich must not eat it.
    assert "mcp[fs]" in r.output


def test_create_refuses_an_existing_name(library, monkeypatch):
    monkeypatch.setattr("raven.providers.factory.make_provider", lambda config: object())
    monkeypatch.setattr("raven.playbook.PlaybookGenerator", _FakeGenerator)
    _write_md(library["builtin"], "weekly-scan", "already shipped")

    r = runner.invoke(app, ["playbook", "create", "weekly-scan", "--input", "whatever"])
    assert r.exit_code == 1


def test_enable_and_disable_edit_the_config_list(library):
    _write_md(library["builtin"], "briefing", "the shipped one")

    r = runner.invoke(app, ["playbook", "disable", "briefing"])
    assert r.exit_code == 0
    assert _disabled_in(library["config"]) == ["briefing"]

    r = runner.invoke(app, ["playbook", "enable", "briefing"])
    assert r.exit_code == 0
    assert _disabled_in(library["config"]) == []


def test_enable_unknown_name_fails(library):
    r = runner.invoke(app, ["playbook", "enable", "ghost"])
    assert r.exit_code == 1


class _FakeRuntime:
    """Captures construction and answers ``load`` without any machinery."""

    last = None

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        type(self).last = self

    async def load(self, name, params, fills=None, *, allow_disabled=False) -> ExecutionPlan | None:
        self.ran = (name, params, fills, allow_disabled)
        return ExecutionPlan(kind="dag", reply="graph finished: all nodes ok")


class _FakeProvider:
    def get_default_model(self) -> str:
        return "fake-model"


def test_run_executes_by_name_with_kv_params(library, monkeypatch):
    _write_md(library["user"], "mine", "the user one")
    monkeypatch.setattr("raven.providers.factory.make_provider", lambda config: _FakeProvider())
    monkeypatch.setattr("raven.playbook.PlaybookRuntime", _FakeRuntime)

    r = runner.invoke(app, ["playbook", "run", "mine", "target=acme"])
    assert r.exit_code == 0, r.stdout
    assert "graph finished" in r.stdout
    # `allow_disabled`: this entry is the user's own hand, and disabling only
    # takes a playbook out of what the model is offered.
    assert _FakeRuntime.last.ran == ("mine", {"target": "acme"}, {}, True)
    # The explicit entry runs synchronously: the executor is built foreground.
    assert _FakeRuntime.last.kwargs["executor"]._background is False
    # And it composes a prompt-mode graph itself -- there is no model in the room
    # to hand the guidance to, so without this the CLI could not run one at all.
    assert _FakeRuntime.last.kwargs["executor"]._compose_prompt_mode is True


def test_run_takes_fills_for_a_field_the_playbook_left_blank(library, monkeypatch):
    """The CLI's answer to a playbook that expects values at run time.

    Without ``--fill`` such a playbook simply cannot run here: nobody is present
    to write the missing prompt, and dispatching a blank step would hand a
    sub-agent nothing to do.
    """
    _write_md(library["user"], "mine", "the user one")
    monkeypatch.setattr("raven.providers.factory.make_provider", lambda config: _FakeProvider())
    monkeypatch.setattr("raven.playbook.PlaybookRuntime", _FakeRuntime)

    r = runner.invoke(
        app,
        ["playbook", "run", "mine", "--fill", "draft.promptTemplate=write it up"],
    )
    assert r.exit_code == 0, r.stdout
    assert _FakeRuntime.last.ran[2] == {"draft": {"promptTemplate": "write it up"}}


def test_run_rejects_a_malformed_fill(library):
    _write_md(library["user"], "mine", "the user one")
    r = runner.invoke(app, ["playbook", "run", "mine", "--fill", "no-dot-here=x"])
    assert r.exit_code == 1


def test_run_rejects_malformed_params(library):
    _write_md(library["user"], "mine", "the user one")
    r = runner.invoke(app, ["playbook", "run", "mine", "not-a-pair"])
    assert r.exit_code == 1


def test_delete_refuses_builtin_and_points_at_disable(library):
    _write_md(library["builtin"], "briefing", "the shipped one")
    r = runner.invoke(app, ["playbook", "delete", "briefing", "--yes"])
    assert r.exit_code == 1
    assert (library["builtin"] / "briefing" / "playbook.md").exists()


def test_delete_removes_user_playbook_and_its_switch_entry(library):
    _write_md(library["user"], "mine", "the user one")
    runner.invoke(app, ["playbook", "disable", "mine"])
    assert _disabled_in(library["config"]) == ["mine"]

    r = runner.invoke(app, ["playbook", "delete", "mine", "--yes"])
    assert r.exit_code == 0, r.stdout
    assert not (library["user"] / "mine").exists()
    assert _disabled_in(library["config"]) == []


def test_delete_unshadows_the_builtin(library):
    _write_md(library["builtin"], "briefing", "the shipped one")
    _write_md(library["user"], "briefing", "my override")

    r = runner.invoke(app, ["playbook", "delete", "briefing", "--yes"])
    assert r.exit_code == 0, r.stdout
    assert "builtin of the same name" in r.stdout
    assert not (library["user"] / "briefing").exists()
    assert (library["builtin"] / "briefing" / "playbook.md").exists()


def test_create_refuses_a_non_kebab_name(library, monkeypatch):
    monkeypatch.setattr("raven.providers.factory.make_provider", lambda config: object())
    monkeypatch.setattr("raven.playbook.PlaybookGenerator", _FakeGenerator)
    r = runner.invoke(app, ["playbook", "create", "../escape", "--input", "whatever"])
    assert r.exit_code == 1
    assert not (library["user"].parent / "escape").exists()
    # The refusal must show the actual pattern: NAME_RE contains square
    # brackets, which Rich reads as markup and swallows unless escaped --
    # live regression saw the message render as "kebab-case (^*$)".
    assert "[a-z0-9]" in r.output


def test_list_keeps_brackets_in_hand_placed_names_and_diagnostics(library):
    """Names on the read side are whatever directory holds a playbook.md --
    not NAME_RE-filtered -- and a broken row's cell carries a pydantic
    message whose brackets are the diagnostic. Rich table cells parse
    markup, so both must be escaped or they render silently truncated.

    The bracket content must open with [a-z#/@] or Rich's tag regex never
    fires: "weird[fs]" pins the escape, "weird[1]" would pin nothing."""
    target = library["user"] / "weird[fs]"
    target.mkdir()
    (target / "playbook.md").write_text(
        '---\nname: "weird[fs]"\ndescription: hand placed\n---\n\nbody\n\n'
        "```yaml playbook-spec\nversion: 1\nmode: prompt\nconfirm: true\n"
        "triggers:\n  keywords: [weird]\nprompts: x\n```\n",
        encoding="utf-8",
    )
    # A wide console keeps every cell on one physical line, so the counts
    # below cannot be split by wrapping.
    # A readable playbook whose description carries brackets: the loadable
    # path escapes the description cell, and nothing else pins that escape.
    _write_md(library["user"], "bracket-desc", "mounts [fs] volumes safely")
    r = runner.invoke(app, ["playbook", "list"], env={"COLUMNS": "300"})
    assert r.exit_code == 0
    assert "[fs] volumes" in r.output
    # The diagnostic cell: unescaped, the pattern renders as '^*$'.
    assert "[a-z0-9]" in r.output
    # The name cell: the diagnostic also carries the name (input_value=...),
    # so presence alone cannot pin add_row's escape -- the count can. Escaped
    # it appears twice (name cell + diagnostic); with add_row unescaped, once.
    assert r.output.count("weird[fs]") == 2


# ------------------------------------------------- run: the MCP pre-flight


def _write_dag_md(root: Path, name: str) -> None:
    """A dag playbook that ships its own MCP server and names a host one too."""
    target = root / name
    target.mkdir(parents=True)
    (target / "playbook.md").write_text(
        f"---\nname: {name}\ndescription: audit the analytics database\n---\n\nbody\n\n"
        "```yaml playbook-spec\n"
        "version: 1\nmode: dag\nconfirm: false\n"
        "taskSummary: run the analytics audit and report back\n"
        f"triggers:\n  keywords: [{name}]\n"
        "params:\n"
        "  PG_PASSWORD:\n    type: secret\n    description: the analytics database password\n"
        "mcpServers:\n"
        "  local-pg:\n"
        "    command: pg-mcp\n"
        "    args: ['--db', 'analytics']\n"
        "    env:\n      PGPASSWORD: '{{ params.PG_PASSWORD }}'\n"
        "nodes:\n"
        "  - id: audit\n    subagent: raven\n    nodeSummary: audit every table\n"
        "    promptTemplate: audit every table\n    mcps: [local-pg, deepwiki]\n"
        "```\n",
        encoding="utf-8",
    )


def _connected(names):
    from raven.mcp.client import Connected

    class _Caps:
        resources = None
        prompts = None
        tools = object()

    return Connected(names=list(names), session=object(), capabilities=_Caps())


def _capture_source(monkeypatch) -> dict:
    """Record what the run hands the sub-agent registry as its MCP source."""
    from raven.agent.subagent.manager import SubagentManager

    seen: dict = {}
    original = SubagentManager.set_mcp_source

    def recording(self, source):
        if source is not None:
            seen["source"] = source
        return original(self, source)

    monkeypatch.setattr(SubagentManager, "set_mcp_source", recording)
    return seen


def test_run_wires_the_playbooks_own_servers_over_the_hosts(library, monkeypatch):
    """Two holes, one test: this entry wired no MCP source at all (every declared
    server resolved to "not connected on the host"), and a playbook's ``mcps``
    was a bare local short name that only worked where the receiving machine
    happened to have a server of that name.
    """
    _write_dag_md(library["user"], "audit")
    library["config"].write_text(
        json.dumps(
            {
                "playbooks": {"dir": str(library["user"])},
                "tools": {"mcpServers": {"deepwiki": {"url": "https://deepwiki.test/mcp"}}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("raven.providers.factory.make_provider", lambda config: _FakeProvider())
    monkeypatch.setattr("raven.playbook.PlaybookRuntime", _FakeRuntime)
    seen = _capture_source(monkeypatch)

    dialled = []

    async def record(name, cfg, registry, stack, executor=None, http_auth=None):
        dialled.append(name)
        return _connected([])

    with patch("raven.mcp.manager.connect_mcp_server", new=record):
        r = runner.invoke(app, ["playbook", "run", "audit", "PG_PASSWORD=hunter2"])

    assert r.exit_code == 0, r.stdout
    assert sorted(dialled) == ["deepwiki", "local-pg"]

    source = seen["source"]
    # The playbook's own definition is resolvable, with the value filled in on
    # this side only.
    assert source.server("local-pg").config.command == "pg-mcp"
    assert source.server("local-pg").config.env == {"PGPASSWORD": "hunter2"}
    # And the host's config is still the fallback, not replaced.
    assert source.server("deepwiki").config.url == "https://deepwiki.test/mcp"

    # The value reached neither the file nor the terminal.
    assert "{{ params.PG_PASSWORD }}" in (library["user"] / "audit" / "playbook.md").read_text(encoding="utf-8")
    assert "hunter2" not in r.stdout


def test_run_reports_a_server_waiting_on_authorization_and_does_not_wait_for_it(library, monkeypatch):
    """The pre-flight is the last moment a person is around to be told. It says
    so and moves on; it must not hold the run open on a browser click."""
    import asyncio
    import time

    _write_dag_md(library["user"], "audit")
    library["config"].write_text(
        json.dumps(
            {
                "playbooks": {"dir": str(library["user"])},
                "tools": {"mcpServers": {"deepwiki": {"url": "https://deepwiki.test/mcp", "auth": "oauth"}}},
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("raven.providers.factory.make_provider", lambda config: _FakeProvider())
    monkeypatch.setattr("raven.playbook.PlaybookRuntime", _FakeRuntime)

    from raven.mcp import oauth as mcp_oauth

    captured: dict = {}

    async def fake_provider_for(server, cfg, notify=None, interactive=False, can_park=True, scope=None):
        captured["notify"] = notify
        return None

    monkeypatch.setattr(mcp_oauth, "provider_for", fake_provider_for)
    released = asyncio.Event()

    async def parks(name, cfg, registry, stack, executor=None, http_auth=None):
        if name == "deepwiki":
            captured["notify"]("oauth.pending", {"server": name, "url": "https://idp.test/a"})
            await released.wait()
        return _connected([])

    started = time.monotonic()
    with patch("raven.mcp.manager.connect_mcp_server", new=parks):
        r = runner.invoke(app, ["playbook", "run", "audit", "PG_PASSWORD=hunter2"])
    elapsed = time.monotonic() - started

    assert r.exit_code == 0, r.stdout
    assert elapsed < 10, f"the run waited {elapsed:.1f}s on a browser authorization"
    # CliRunner folds stderr into one stream, which is where err_console writes.
    assert "deepwiki" in r.output and "auth_required" in r.output
    assert "raven plugin auth deepwiki" in r.output


# ── secret / auth: the machine-held half of a carried server's credential ────


def _write_carried_md(root: Path, name: str) -> None:
    target = root / name
    target.mkdir(parents=True)
    (target / "playbook.md").write_text(
        f"---\nname: {name}\ndescription: carries a tokened server\n---\n\nbody\n\n"
        "```yaml playbook-spec\n"
        "version: 1\nmode: dag\nconfirm: false\n"
        "taskSummary: reach the carried server\n"
        f"triggers:\n  keywords: [{name}]\n"
        "params:\n  PROBE_TOKEN:\n    type: secret\n    required: true\n    description: the bearer\n"
        "  topic:\n    type: string\n    description: plain\n"
        "mcpServers:\n  tokened:\n    type: streamableHttp\n    url: http://127.0.0.1:8932/mcp\n"
        "    headers:\n      Authorization: Bearer {{ params.PROBE_TOKEN }}\n"
        "  sentry:\n    type: streamableHttp\n    url: https://mcp.sentry.dev/mcp\n    auth: oauth\n"
        "nodes:\n- id: a\n  subagent: Raven\n  nodeSummary: s\n  promptTemplate: p\n  mcps: [tokened]\n"
        "```\n",
        encoding="utf-8",
    )


@pytest.fixture
def carried(library, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    _write_carried_md(library["user"], "carried")
    return library


def test_secret_set_with_value_stores_it_under_the_playbook(carried):
    from raven.playbook.credentials import stored_secret_param_names

    r = runner.invoke(app, ["playbook", "secret", "set", "carried", "PROBE_TOKEN", "--value", "s3cr3t"])
    assert r.exit_code == 0, r.output
    assert "stored PROBE_TOKEN" in r.output
    assert stored_secret_param_names("carried") == {"PROBE_TOKEN"}


def test_secret_set_prompts_without_echo_when_no_value_is_given(carried):
    from raven.playbook.credentials import stored_secret_param_names

    r = runner.invoke(app, ["playbook", "secret", "set", "carried", "PROBE_TOKEN"], input="typed\n")
    assert r.exit_code == 0, r.output
    assert "typed" not in r.output.replace("PROBE_TOKEN", "")
    assert stored_secret_param_names("carried") == {"PROBE_TOKEN"}


def test_secret_set_refuses_a_param_that_is_not_secret(carried):
    r = runner.invoke(app, ["playbook", "secret", "set", "carried", "topic", "--value", "x"])
    assert r.exit_code == 1
    assert "no secret param named topic" in r.output


def test_secret_clear_forgets_it(carried):
    from raven.playbook.credentials import stored_secret_param_names

    runner.invoke(app, ["playbook", "secret", "set", "carried", "PROBE_TOKEN", "--value", "v"])
    r = runner.invoke(app, ["playbook", "secret", "clear", "carried", "PROBE_TOKEN"])
    assert r.exit_code == 0, r.output
    assert stored_secret_param_names("carried") == frozenset()


def test_auth_refuses_a_server_the_playbook_does_not_carry_or_that_is_not_oauth(carried):
    r = runner.invoke(app, ["playbook", "auth", "carried", "ghost"])
    assert r.exit_code == 1
    assert "carries no MCP server named ghost" in r.output
    r = runner.invoke(app, ["playbook", "auth", "carried", "tokened"])
    assert r.exit_code == 1
    assert "only an oauth server" in r.output


def test_auth_drives_a_manager_scoped_to_the_playbook(carried, monkeypatch):
    from raven.mcp import manager as manager_mod

    built = []

    class FakeManager:
        def __init__(self, registry, **kwargs):
            built.append(kwargs)

        async def connect(self, name, cfg, **kwargs):
            built[0]["connect_kwargs"] = kwargs
            return {"name": name, "state": "connected", "tool_count": 9}

        async def aclose(self):
            pass

    monkeypatch.setattr(manager_mod, "MCPConnectionManager", FakeManager)
    r = runner.invoke(app, ["playbook", "auth", "carried", "sentry"])
    assert r.exit_code == 0, r.output
    assert "authorized" in r.output and "9 tools" in r.output
    assert built[0]["credential_scope"] == "playbooks/carried"
    # A person ran this command and is at the terminal: the flow opens their
    # browser instead of parking on a URL nobody is shown.
    assert built[0]["connect_kwargs"].get("interactive") is True


def test_run_reads_a_stored_secret_and_scopes_the_carried_servers_credentials(library, monkeypatch, tmp_path):
    """The pre-flight dials before the executor runs, so what the executor would
    merge later is too late: the stored secret has to be in the definition the
    pre-flight renders, and the carried server has to dial under the playbook's
    credential scope while the host's server keeps the host's."""
    monkeypatch.setenv("RAVEN_HOME", str(tmp_path / "home"))
    from raven.mcp import manager as manager_mod
    from raven.playbook.credentials import set_secret_param

    _write_dag_md(library["user"], "audit")
    library["config"].write_text(
        json.dumps(
            {
                "playbooks": {"dir": str(library["user"])},
                "tools": {"mcpServers": {"deepwiki": {"url": "https://deepwiki.test/mcp"}}},
            }
        ),
        encoding="utf-8",
    )
    set_secret_param("audit", "PG_PASSWORD", "from-the-store")
    monkeypatch.setattr("raven.providers.factory.make_provider", lambda config: _FakeProvider())
    monkeypatch.setattr("raven.playbook.PlaybookRuntime", _FakeRuntime)
    seen = _capture_source(monkeypatch)

    scopes: dict = {}
    real = manager_mod.MCPConnectionManager

    class Recording(real):
        def __init__(self, *args, **kwargs):
            scopes["fn"] = kwargs.get("credential_scope")
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(manager_mod, "MCPConnectionManager", Recording)

    async def connect(name, cfg, registry, stack, executor=None, http_auth=None):
        return _connected([])

    with patch("raven.mcp.manager.connect_mcp_server", new=connect):
        r = runner.invoke(app, ["playbook", "run", "audit"])

    assert r.exit_code == 0, r.stdout
    assert seen["source"].server("local-pg").config.env == {"PGPASSWORD": "from-the-store"}
    assert seen["source"].server("local-pg").scope == "playbooks/audit"
    assert seen["source"].server("deepwiki").scope is None
    assert "from-the-store" not in r.stdout
    assert scopes["fn"]("local-pg") == "playbooks/audit"
    assert scopes["fn"]("deepwiki") is None


@pytest.fixture
def stints(tmp_path: Path):
    """A config whose workspace is tmp, so the CLI looks for stints under it."""
    cfg = tmp_path / "stint-config.json"
    cfg.write_text(json.dumps({"agents": {"defaults": {"workspace": str(tmp_path)}}}), encoding="utf-8")
    set_config_path(cfg)
    yield tmp_path
    set_config_path(None)  # type: ignore[arg-type]


GROUP = "-Users-admin-workspace-ledger"
"""A project-slugged session group, as a conversation launched in one has."""


def _plan_on_disk(
    workspace: Path,
    *,
    status: str = "running",
    questions: list | None = None,
    session_key: str = "",
    stint_id: str = "stint-20260917T000000Z",
    group: str = "",
):
    """A stint where the CLI looks for one, without running anything to make it.

    ``group`` places it under a project-slugged directory, which is where a
    conversation launched in a project really keeps its stints -- and is not the
    directory a process with no project derives from the same session key.
    """
    from raven.agent.subagent.history import dag_root
    from raven.session.manager import SessionManager
    from raven.stint.record import STINTS_DIRNAME, StintRecord, StintStore

    home = workspace / "sessions" / group / session_key.partition(":")[2] if group else None
    store = StintStore(dag_root(home or SessionManager(workspace).session_dir(session_key)) / STINTS_DIRNAME)
    record = StintRecord(
        stint_id=stint_id,
        playbook="game-dev",
        spec={"mode": "stint", "name": "game-dev"},
        workdir=str(workspace / "tree"),
        branch=f"stint/{stint_id}",
        round_index=2,
        status=status,
        questions=questions or [],
        origin={"session_key": session_key} if session_key else {},
    )
    entry = record.open_round(1, "run-a")
    entry.status = "completed"
    entry.verify = [{"name": "build", "status": "failed", "command": "make", "returncode": 1, "duration_sec": 1.0}]
    entry.violations = ["developer wrote 1 path(s) it may not write: reports/qa.md"]
    record.open_round(2, "run-b")
    store.write(record)
    return store, record


class TestWhoHoldsTheStintOpen:
    """A stint dispatches each round in the background and opens the next from a
    callback on the finished run's own task. On a gateway the host outlives the
    turn; from a terminal the command *is* the host, and returning closes the
    loop out from under the round -- the receipt says it started, the record
    says running, and nothing ever ran."""

    async def test_the_hold_returns_once_this_process_holds_nothing(self) -> None:
        from raven.cli.playbook_commands import hold_until_the_stints_end

        class _Driver:
            @staticmethod
            def holding() -> bool:
                return False

        await hold_until_the_stints_end(_Driver(), every_sec=0.01)

    async def test_the_hold_waits_while_a_round_is_still_going_here(self) -> None:
        """The wait is the whole point: without it the command returns between
        the receipt and the first round. Asked of the driver and not of the
        records: a record another process is beating for is live and not this
        terminal's to wait on, and holding on it hung the refusal it had just
        printed."""
        from raven.cli.playbook_commands import hold_until_the_stints_end

        class _Driver:
            def __init__(self) -> None:
                self.asked = 0

            def holding(self) -> bool:
                self.asked += 1
                return self.asked < 3

        driver = _Driver()
        await hold_until_the_stints_end(driver, every_sec=0.01)

        assert driver.asked >= 3, "it stopped waiting while a round was still going here"


class TestTheCommandsWeTellPeopleToRun:
    """Every `raven playbook stint(s) <verb>` this build prints, against what it
    registered.

    Shipped once wrong in both directions: the verbs moved from a top-level
    group to `stints`, and the messages that tell an operator how to extend or
    take up a stint kept naming a group that answers `No such command`. A person
    reaching for the one thing the message told them to reach for is exactly the
    moment not to be wrong, and nothing else checks the string against the app.
    """

    @staticmethod
    def _registered() -> dict[str, set[str]]:
        from raven.cli.playbook_commands import playbook_app

        found: dict[str, set[str]] = {}
        for group in playbook_app.registered_groups:
            app = group.typer_instance
            found[group.name] = {c.name or "" for c in app.registered_commands} | {
                g.name or "" for g in app.registered_groups
            }
        return found

    def test_every_command_a_message_names_is_one_this_build_answers(self) -> None:
        import re
        import subprocess
        from pathlib import Path

        registered = self._registered()
        root = Path(__file__).resolve().parent.parent
        files = subprocess.run(
            ["git", "ls-files", "raven", "docs"], capture_output=True, text=True, cwd=root, check=True
        ).stdout.split()

        wrong: list[str] = []
        for name in files:
            path = root / name
            if path.suffix not in {".py", ".md"}:
                continue
            for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                for match in re.finditer(r"raven playbook (stints?)\s+([a-z_]+)", line):
                    group, verb = match.group(1), match.group(2)
                    if verb not in registered.get(group, set()):
                        wrong.append(f"{name}:{number}: {match.group(0)}")

        assert wrong == [], "these name a command `raven playbook --help` does not offer:\n" + "\n".join(wrong)


class TestPlanCommands:
    @staticmethod
    def _went_quiet(store, stint_id: str, seconds: float = 3600.0) -> None:
        """Backdate the stamp, the way a host that died leaves it."""
        import time

        path = store.path_for(stint_id)
        data = json.loads(path.read_text(encoding="utf-8"))
        data["touched_at_ms"] = int((time.time() - seconds) * 1000)
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def test_the_list_stops_reporting_a_corpse_as_work_in_progress(self, stints: Path) -> None:
        """A host that dies mid-round writes nothing on its way out. Left alone
        the file says `running` for ever, and a person reads the list and waits
        for a notification nobody is going to send."""
        store, record = _plan_on_disk(stints, status="running")
        self._went_quiet(store, record.stint_id)

        result = runner.invoke(app, ["playbook", "stints", "list"])

        assert result.exit_code == 0, result.output
        assert store.read(record.stint_id).status == "interrupted"
        assert "interrupted" in result.output, "the column said `running` about a stint nothing is advancing"

    def test_the_list_leaves_a_stint_something_is_still_beating_for(self, stints: Path) -> None:
        """The stamp is the whole reason this is safe to do on a read: a stint
        another process is working looks exactly like one that died, until you
        read when it was last touched."""
        store, record = _plan_on_disk(stints, status="running")

        result = runner.invoke(app, ["playbook", "stints", "list"])

        assert result.exit_code == 0, result.output
        assert store.read(record.stint_id).status == "running"

    def test_plan_list_names_every_plan_and_where_it_got_to(self, stints: Path) -> None:
        _plan_on_disk(stints)

        result = runner.invoke(app, ["playbook", "stints", "list"])

        assert result.exit_code == 0, result.output
        assert "stint-20260917T000000Z" in result.output
        assert "game-dev" in result.output
        assert "running" in result.output

    def test_a_plan_started_in_a_conversation_is_found_from_a_terminal(self, stints: Path) -> None:
        """A stint lives beside the conversation that started it, and the person
        asking here has a terminal instead of one. Reading a single session's
        store reported a running stint as nothing having run at all."""
        _plan_on_disk(stints, session_key="local:default", stint_id="stint-from-the-tui")

        listed = runner.invoke(app, ["playbook", "stints", "list"])
        opened = runner.invoke(app, ["playbook", "stints", "get", "stint-from-the-tui"])

        assert listed.exit_code == 0, listed.output
        assert "stint-from-the-tui" in listed.output
        assert opened.exit_code == 0, opened.output
        assert "stint/stint-from-the-tui" in opened.output

    def test_a_plan_is_written_back_to_the_conversation_that_holds_it(self, stints: Path) -> None:
        store, _ = _plan_on_disk(stints, session_key="local:default", stint_id="stint-from-the-tui")

        result = runner.invoke(app, ["playbook", "stints", "stop", "stint-from-the-tui"])

        assert result.exit_code == 0, result.output
        assert store.read("stint-from-the-tui").status == "stopped"

    def test_plan_list_says_so_when_nothing_has_run(self, stints: Path) -> None:
        result = runner.invoke(app, ["playbook", "stints", "list"])

        assert result.exit_code == 0
        assert "No stints" in result.output

    def test_plan_get_shows_the_checks_and_what_was_undone(self, stints: Path) -> None:
        _plan_on_disk(stints)

        result = runner.invoke(app, ["playbook", "stints", "get", "stint-20260917T000000Z"])

        assert result.exit_code == 0, result.output
        assert "build=failed" in result.output
        assert "may not write" in result.output
        assert "stint/stint-20260917T000000Z" in result.output

    def test_an_unknown_plan_is_an_error_not_an_empty_report(self, stints: Path) -> None:
        result = runner.invoke(app, ["playbook", "stints", "get", "stint-nope"])

        assert result.exit_code == 1
        assert "No stint" in result.output

    def test_stopping_a_plan_lets_the_round_in_flight_finish(self, stints: Path) -> None:
        store, _ = _plan_on_disk(stints)

        result = runner.invoke(app, ["playbook", "stints", "stop", "stint-20260917T000000Z"])

        assert result.exit_code == 0, result.output
        assert "finishes and reports" in result.output
        assert store.read("stint-20260917T000000Z").status == "stopped"

    def test_stopping_a_finished_plan_says_so_rather_than_pretending(self, stints: Path) -> None:
        _plan_on_disk(stints, status="finished")

        result = runner.invoke(app, ["playbook", "stints", "stop", "stint-20260917T000000Z"])

        assert result.exit_code == 0
        assert "already finished" in result.output

    def test_an_answer_is_kept_where_the_next_round_reads_it(self, stints: Path) -> None:
        store, _ = _plan_on_disk(
            stints, questions=[{"round": 1, "role": "planner", "text": "which of the two?", "answer": ""}]
        )

        result = runner.invoke(
            app, ["playbook", "stints", "answer", "stint-20260917T000000Z", "-q", "0", "-t", "the second one"]
        )

        assert result.exit_code == 0, result.output
        [question] = store.read("stint-20260917T000000Z").questions
        assert question["answer"] == "the second one"
        assert question["answered_at"]

    def test_answering_a_question_that_is_not_there_is_refused(self, stints: Path) -> None:
        _plan_on_disk(stints)

        result = runner.invoke(app, ["playbook", "stints", "answer", "stint-20260917T000000Z", "-q", "3", "-t", "x"])

        assert result.exit_code == 1
        assert "no question 3" in result.output

    def test_extend_hands_the_count_and_the_plan_s_own_session_to_the_driver(self, stints: Path, monkeypatch) -> None:
        """The session the stint was started in, not this terminal's absence of
        one: its earlier rounds' nodes are in that conversation."""
        _plan_on_disk(stints, status="finished", session_key="tui:abc", group=GROUP)
        driver = _CapturingDriver()
        monkeypatch.setattr("raven.cli.playbook_commands._stint_driver", driver.built_for)

        result = runner.invoke(app, ["playbook", "stints", "extend", "stint-20260917T000000Z", "--rounds", "3"])

        assert result.exit_code == 0, result.output
        assert driver.calls == [("extend", "stint-20260917T000000Z", 3, "tui:abc")]
        # The conversation the stint was found in, not one derived from its key:
        # deriving lands under `sessions/<channel>/`, where nothing has written,
        # and the round would be opened somewhere the stint is not.
        assert driver.home == stints / "sessions" / GROUP / "abc"

    def test_resume_takes_up_a_paused_plan(self, stints: Path, monkeypatch) -> None:
        """`stint pause` tells the person to take it up with `stint resume`. A
        guard here that turned a paused stint away made that instruction false."""
        _plan_on_disk(stints, status="paused", session_key="tui:abc")
        driver = _CapturingDriver()
        monkeypatch.setattr("raven.cli.playbook_commands._stint_driver", driver.built_for)

        result = runner.invoke(app, ["playbook", "stints", "resume", "stint-20260917T000000Z"])

        assert result.exit_code == 0, result.output
        assert driver.calls == [("resume", "stint-20260917T000000Z", "tui:abc")]

    def test_resume_goes_to_the_raven_serving_the_page_when_one_is_up(self, stints: Path, monkeypatch) -> None:
        """A round runs in the process that opens it. Opened here it ran in the
        terminal -- and when the terminal was a model's shell tool, the tool's
        timeout killed it with the round half-dispatched (2026-09-20). The page's
        raven has the driver and the conversation the stint reports to."""
        _plan_on_disk(stints, status="interrupted", session_key="tui:abc")
        driver = _CapturingDriver()
        monkeypatch.setattr("raven.cli.playbook_commands._stint_driver", driver.built_for)
        sent: list[tuple] = []
        monkeypatch.setattr("raven.cli._hosted_rpc.hosted_page", lambda: (18792, "tok"))
        monkeypatch.setattr(
            "raven.cli._hosted_rpc.call",
            lambda port, token, method, params, **_: (
                sent.append((port, method, params))
                or {"reply": "Stint stint-20260917T000000Z is running round 2 again."}
            ),
        )

        result = runner.invoke(app, ["playbook", "stints", "resume", "stint-20260917T000000Z"])

        assert result.exit_code == 0, result.output
        assert sent == [(18792, "playbooks.stints.resume", {"stint_id": "stint-20260917T000000Z"})]
        assert driver.calls == [], "no second engine in this terminal"
        assert "running round 2 again" in result.output and "port 18792" in result.output

    def test_extend_goes_to_the_page_s_raven_too(self, stints: Path, monkeypatch) -> None:
        _plan_on_disk(stints, status="finished", session_key="tui:abc", group=GROUP)
        driver = _CapturingDriver()
        monkeypatch.setattr("raven.cli.playbook_commands._stint_driver", driver.built_for)
        sent: list[tuple] = []
        monkeypatch.setattr("raven.cli._hosted_rpc.hosted_page", lambda: (18792, "tok"))
        monkeypatch.setattr(
            "raven.cli._hosted_rpc.call", lambda port, token, method, params, **_: sent.append((method, params)) or {}
        )

        result = runner.invoke(app, ["playbook", "stints", "extend", "stint-20260917T000000Z", "--rounds", "3"])

        assert result.exit_code == 0, result.output
        assert sent == [("playbooks.stints.extend", {"stint_id": "stint-20260917T000000Z", "rounds": 3})]
        assert driver.calls == []

    def test_a_refusal_from_the_page_s_raven_is_the_terminal_s_answer(self, stints: Path, monkeypatch) -> None:
        _plan_on_disk(stints, status="interrupted", session_key="tui:abc")
        monkeypatch.setattr("raven.cli._hosted_rpc.hosted_page", lambda: (18792, "tok"))

        def refuse(*_a, **_k):
            raise RuntimeError("no stint stint-20260917T000000Z on this machine")

        monkeypatch.setattr("raven.cli._hosted_rpc.call", refuse)

        result = runner.invoke(app, ["playbook", "stints", "resume", "stint-20260917T000000Z"])

        assert result.exit_code == 1
        assert "no stint" in result.output

    def test_resume_says_so_for_a_plan_that_is_over(self, stints: Path, monkeypatch) -> None:
        _plan_on_disk(stints, status="finished")
        driver = _CapturingDriver()
        monkeypatch.setattr("raven.cli.playbook_commands._stint_driver", driver.built_for)

        result = runner.invoke(app, ["playbook", "stints", "resume", "stint-20260917T000000Z"])

        assert result.exit_code == 0
        assert "nothing left to take up" in result.output
        assert driver.calls == []


@pytest.fixture(autouse=True)
def _no_page_is_served(monkeypatch):
    """The relay asks whether a raven serves the page; these tests are about the
    terminal's own driver unless they say otherwise, so on this machine's real
    serve.json the answer has to be no."""
    monkeypatch.setattr("raven.cli._hosted_rpc.hosted_page", lambda: None)


class _CapturingDriver:
    """The rounds driver at the seam the stint commands build it through."""

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.home: Path | None = None

    def built_for(self, _config, home: Path) -> "_CapturingDriver":
        self.home = home
        return self

    async def extend(self, stint_id: str, rounds: int, session_key: str | None) -> str:
        self.calls.append(("extend", stint_id, rounds, session_key))
        return f"{stint_id} may now run more rounds."

    async def resume(self, stint_id: str, session_key: str | None) -> str:
        self.calls.append(("resume", stint_id, session_key))
        return f"{stint_id} taken up again."

    def holding(self) -> bool:
        """What the command asks to decide whether to hold the terminal open.

        Nothing held, so these tests exercise the answer rather than the wait:
        a double that reported a round in flight would hold the runner for ever.
        """
        return False


class _SweepingDriver(_CapturingDriver):
    """A driver that takes one stint up and holds a round of it for two asks."""

    def __init__(self) -> None:
        super().__init__()
        self.asked = 0

    async def sweep(self, session_key: str | None) -> list[str]:
        self.calls.append(("sweep", session_key))
        return ["stint-20260917T000000Z"]

    def holding(self) -> bool:
        self.asked += 1
        return self.asked <= 2


class TestSweepHoldsForWhatItTookUp:
    def test_sweep_stays_until_the_rounds_it_opened_are_done(self, stints: Path, monkeypatch) -> None:
        """Rounds are dispatched onto the loop the verb ran on, and `asyncio.run`
        per home closed that loop under them: the receipt said "took up", the
        record said running, nothing ran."""
        _plan_on_disk(stints, status="running", session_key="tui:abc")
        driver = _SweepingDriver()
        monkeypatch.setattr("raven.cli.playbook_commands._stint_driver", driver.built_for)
        monkeypatch.setattr("raven.cli.playbook_commands.asyncio.sleep", _no_sleep)

        result = runner.invoke(app, ["playbook", "stints", "sweep"])

        assert result.exit_code == 0, result.output
        assert ("sweep", None) in driver.calls
        assert "took up stint-20260917T000000Z" in result.output
        assert "Holding this terminal" in result.output
        assert driver.asked >= 3, "it returned while the round it took up was still going"

    def test_a_refusal_does_not_hold_the_terminal(self, stints: Path, monkeypatch) -> None:
        """`resume` of a stint another raven is beating for is refused, and the
        record is still live -- somebody else's. Holding on that hung the
        refusal, under a banner telling the person to run the command it was
        blocking."""
        _plan_on_disk(stints, status="running", session_key="tui:abc")
        driver = _CapturingDriver()
        monkeypatch.setattr("raven.cli.playbook_commands._stint_driver", driver.built_for)

        result = runner.invoke(app, ["playbook", "stints", "resume", "stint-20260917T000000Z"])

        assert result.exit_code == 0, result.output
        assert "taken up again" in result.output
        assert "Holding this terminal" not in result.output


async def _no_sleep(_seconds: float) -> None:
    return None


class TestTheTerminalIsAskedBeforeAStintRuns:
    """The graph tool built here had no route to a person, and its gate approves
    when it has nobody to ask -- without rendering the text that names the round
    budget and every command. Thirty rounds of shell from a file on one absent
    yes is the case the gate exists for."""

    async def test_with_a_terminal_the_question_is_shown_and_the_answer_is_theirs(self, monkeypatch) -> None:
        from raven.cli import playbook_commands as pc

        shown: list[str] = []
        monkeypatch.setattr(pc.sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(pc.console, "print", lambda text, **_kw: shown.append(str(text)))
        monkeypatch.setattr(pc.typer, "confirm", lambda _prompt, default=False: True)

        assert await pc.ask_at_the_terminal("conv", "Run 3 rounds of `make test`?") is True
        assert any("make test" in line for line in shown)

        monkeypatch.setattr(pc.typer, "confirm", lambda _prompt, default=False: False)
        assert await pc.ask_at_the_terminal("conv", "Run it?") is False

    async def test_with_no_terminal_the_answer_is_no_and_says_so(self, monkeypatch) -> None:
        from raven.cli import playbook_commands as pc

        said: list[str] = []
        monkeypatch.setattr(pc.sys.stdin, "isatty", lambda: False)
        monkeypatch.setattr(pc.err_console, "print", lambda text, **_kw: said.append(str(text)))

        assert await pc.ask_at_the_terminal("conv", "Run it?") is False
        assert any("no terminal to ask at" in line for line in said)


class TestScaffoldingAStint:
    """`playbook new-stint` -- the one mode a model may not write for you.

    The generator is held to `dag` and `prompt` because a stint's `verify` lines
    are shell that runs here every round under a single approval. That left the
    only way to make one being to copy an existing file and discover the schema
    a validation error at a time.
    """

    @staticmethod
    def _home(tmp_path, monkeypatch) -> Path:
        monkeypatch.setenv("RAVEN_HOME", str(tmp_path))
        return tmp_path / "workspace" / "playbooks"

    def test_what_it_writes_validates_as_written(self, tmp_path, monkeypatch) -> None:
        """A skeleton that does not load would report where you started rather
        than what you changed."""
        from raven.playbook.store import PlaybookStore
        from raven.playbook.validate import validate_structure

        root = self._home(tmp_path, monkeypatch)
        written = runner.invoke(app, ["playbook", "new-stint", "qa-loop"])
        assert written.exit_code == 0, written.output

        spec = PlaybookStore(root).load("qa-loop")
        assert spec.mode == "stint"
        assert validate_structure(spec, known_agents=[role.name for role in spec.roles or []]) == []

    def test_the_comments_are_the_point_and_they_survive(self, tmp_path, monkeypatch) -> None:
        """Written as a file rather than saved through the serializer: a round
        trip through the model keeps the fields and drops every comment."""
        root = self._home(tmp_path, monkeypatch)
        runner.invoke(app, ["playbook", "new-stint", "qa-loop"])

        body = (root / "qa-loop" / "playbook.md").read_text(encoding="utf-8")
        assert "isolation: branch" in body, "the choice a run cannot be un-made is explained"
        assert "refused at load" in body, "and so is the trap that refuses one"
        assert body.count("#") > 20

    def test_it_is_usable_without_an_enabling_step(self, tmp_path, monkeypatch) -> None:
        self._home(tmp_path, monkeypatch)
        runner.invoke(app, ["playbook", "new-stint", "qa-loop"])

        listed = runner.invoke(app, ["playbook", "list"])

        assert "qa-loop" in listed.output
        assert "disabled" not in listed.output

    def test_a_name_the_library_already_has_is_refused(self, tmp_path, monkeypatch) -> None:
        self._home(tmp_path, monkeypatch)
        runner.invoke(app, ["playbook", "new-stint", "qa-loop"])

        again = runner.invoke(app, ["playbook", "new-stint", "qa-loop"])

        assert again.exit_code == 1
        assert "already exists" in again.output

    def test_a_name_that_could_not_be_a_directory_is_refused(self, tmp_path, monkeypatch) -> None:
        self._home(tmp_path, monkeypatch)

        result = runner.invoke(app, ["playbook", "new-stint", "QA Loop"])

        assert result.exit_code == 1
        assert "kebab-case" in result.output
