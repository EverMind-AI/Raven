"""Tests for ``raven agents new`` -- the scaffold command and its refusals.

The command instantiates ``raven/templates/agents_scaffold/`` into a fresh
agent folder. Pinned here: the dry-run tree, the three conflict refusals
(existing directory, roster row, reserved name), the four name derivations,
the instantiation itself (schema-valid manifest, compilable python, zero
token residue), and the two landing spots.

Every test isolates the raven home through ``RAVEN_HOME``: ``agents_root()``
takes the first existing tree, so a real home would shadow everything a test
builds (the S1 spike measured exactly that).
"""

from __future__ import annotations

import json
import os
import py_compile
import sys
import tomllib
from pathlib import Path

import pytest
from typer.testing import CliRunner

from raven.cli.commands import app

runner = CliRunner()

EXPECTED_TREE = {
    ".env.example",
    "README.md",
    "config.json",
    "install.py",
    "plugins/demo-agent-flow/demo_agent_flow/__init__.py",
    "plugins/demo-agent-flow/demo_agent_flow/plugin.py",
    "plugins/demo-agent-flow/demo_agent_flow/tools/__init__.py",
    "plugins/demo-agent-flow/demo_agent_flow/tools/hello.py",
    "plugins/demo-agent-flow/raven-plugin.toml",
    "run.py",
    "subagent.json",
}

TOKENS = ("my-agent", "My-Agent", "my_agent", "MY_AGENT")


@pytest.fixture
def raven_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("RAVEN_HOME", str(home))
    return home


# ---------------------------------------------------------------------------
# dry run
# ---------------------------------------------------------------------------


def test_dry_run_prints_the_instantiated_tree_and_writes_nothing(raven_home: Path) -> None:
    r = runner.invoke(app, ["agents", "new", "demo-agent", "--dry-run"])

    assert r.exit_code == 0, r.output
    for rel in EXPECTED_TREE:
        assert rel in r.output
    assert "dry run: nothing written" in r.output
    assert not (raven_home / "agents").exists()


# ---------------------------------------------------------------------------
# the three conflict refusals
# ---------------------------------------------------------------------------


def test_an_existing_target_directory_is_refused(raven_home: Path) -> None:
    (raven_home / "agents" / "demo-agent").mkdir(parents=True)

    r = runner.invoke(app, ["agents", "new", "demo-agent"])

    assert r.exit_code != 0
    assert "already exists" in r.output
    assert not (raven_home / "agents" / "demo-agent" / "subagent.json").exists()


def test_a_roster_row_with_the_same_name_is_refused(raven_home: Path) -> None:
    (raven_home / "config.json").write_text(
        json.dumps({"subagents": {"agents": [{"name": "Demo-Agent", "kind": "acp", "command": "demo --acp"}]}}),
        encoding="utf-8",
    )

    r = runner.invoke(app, ["agents", "new", "demo-agent"])

    assert r.exit_code != 0
    assert "roster" in r.output
    assert not (raven_home / "agents").exists()


@pytest.mark.parametrize("name", ["raven-code", "raven-research-ng", "raven"])
def test_a_reserved_name_is_refused(raven_home: Path, name: str) -> None:
    r = runner.invoke(app, ["agents", "new", name])

    assert r.exit_code != 0
    assert "choose another name" in r.output
    assert not (raven_home / "agents").exists()


def test_a_reserved_display_name_is_refused(raven_home: Path) -> None:
    r = runner.invoke(app, ["agents", "new", "fine-name", "--display", "Raven-Code"])

    assert r.exit_code != 0
    assert "choose another name" in r.output


# ---------------------------------------------------------------------------
# name derivations
# ---------------------------------------------------------------------------


def test_api_key_var_normalizes_hyphens() -> None:
    """Pins the existing behavior the scaffold leans on (the blueprint's fix-2)."""
    from raven.agent.subagent.vendored_agents import api_key_var

    assert api_key_var("my-agent") == "MY_AGENT_API_KEY"


def test_the_four_identities_derive_from_one_kebab_name() -> None:
    from raven.cli.agents_commands import _display_name, _env_prefix

    assert _display_name("my-agent") == "My-Agent"
    assert _display_name("a-b2-c") == "A-B2-C"
    assert _env_prefix("my-agent") == "MY_AGENT"


@pytest.mark.parametrize("name", ["My-Agent", "my_agent", "-lead", "trail-", "double--dash", "0day", "one.dot"])
def test_a_name_that_is_not_kebab_case_is_refused(raven_home: Path, name: str) -> None:
    r = runner.invoke(app, ["agents", "new", name])

    assert r.exit_code != 0
    assert not (raven_home / "agents").exists()


# ---------------------------------------------------------------------------
# instantiation
# ---------------------------------------------------------------------------


def test_the_generated_folder_is_valid_compilable_and_token_free(raven_home: Path) -> None:
    r = runner.invoke(app, ["agents", "new", "demo-agent", "--no-smoke"])

    assert r.exit_code == 0, r.output
    target = raven_home / "agents" / "demo-agent"
    generated = {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()}
    assert generated == EXPECTED_TREE

    from raven.config.schema import ThirdPartyAcpSubagentConfig

    row = json.loads((target / "subagent.json").read_text(encoding="utf-8"))
    validated = ThirdPartyAcpSubagentConfig.model_validate(row)
    assert validated.name == "Demo-Agent"
    assert validated.everos and validated.everos.user_id == "demo-agent"
    assert "{PYTHON}" in row["command"] and "{SUBAGENT_DIR}" in row["command"]

    for rel in generated:
        text = (target / rel).read_text(encoding="utf-8")
        for token in TOKENS:
            assert token not in rel, (rel, token)
            assert token not in text, (rel, token)

    for rel in generated:
        if rel.endswith(".py"):
            py_compile.compile(str(target / rel), doraise=True)

    config = json.loads((target / "config.json").read_text(encoding="utf-8"))
    assert config == {"plugins": {"config": {"demo-agent-flow": {"enabled": True}}}}


# ---------------------------------------------------------------------------
# landing spots
# ---------------------------------------------------------------------------


def test_the_default_landing_is_the_raven_home_agents_tree(raven_home: Path) -> None:
    r = runner.invoke(app, ["agents", "new", "demo-agent", "--no-smoke"])

    assert r.exit_code == 0, r.output
    assert (raven_home / "agents" / "demo-agent" / "run.py").is_file()


def test_here_lands_in_the_working_directory_agents_tree(raven_home: Path, tmp_path: Path, monkeypatch) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    monkeypatch.chdir(checkout)

    r = runner.invoke(app, ["agents", "new", "demo-agent", "--here", "--no-smoke"])

    assert r.exit_code == 0, r.output
    assert (checkout / "agents" / "demo-agent" / "run.py").is_file()
    assert not (raven_home / "agents").exists()


# ---------------------------------------------------------------------------
# options
# ---------------------------------------------------------------------------


def test_kind_cli_is_reserved_for_v2(raven_home: Path) -> None:
    r = runner.invoke(app, ["agents", "new", "demo-agent", "--kind", "cli"])

    assert r.exit_code != 0
    assert "v2" in r.output
    assert not (raven_home / "agents").exists()


def test_an_unknown_kind_is_refused(raven_home: Path) -> None:
    r = runner.invoke(app, ["agents", "new", "demo-agent", "--kind", "carrier-pigeon"])

    assert r.exit_code != 0
    assert not (raven_home / "agents").exists()


def test_display_overrides_the_derived_display_name(raven_home: Path) -> None:
    r = runner.invoke(app, ["agents", "new", "demo-agent", "--display", "Demo Deluxe", "--no-smoke"])

    assert r.exit_code == 0, r.output
    row = json.loads((raven_home / "agents" / "demo-agent" / "subagent.json").read_text(encoding="utf-8"))
    assert row["name"] == "Demo Deluxe"
    assert row["everos"]["userId"] == "demo-agent"


def test_register_pins_the_resolved_row_into_the_host_roster(raven_home: Path) -> None:
    r = runner.invoke(app, ["agents", "new", "demo-agent", "--register", "--no-smoke"])

    assert r.exit_code == 0, r.output
    from raven.config.update_subagents import get_agents

    rows = {row["name"]: row for row in get_agents(config_path=raven_home / "config.json")}
    row = rows["Demo-Agent"]
    target = raven_home / "agents" / "demo-agent"
    assert "{PYTHON}" not in row["command"] and "{SUBAGENT_DIR}" not in row["command"]
    assert row["command"].startswith(sys.executable)
    assert row["cwd"] == str(target)


def test_no_smoke_prints_the_skip_line_and_spawns_nothing(raven_home: Path) -> None:
    r = runner.invoke(app, ["agents", "new", "demo-agent", "--no-smoke"])

    assert r.exit_code == 0, r.output
    assert "smoke: skipped (--no-smoke)" in r.output
    assert "handshake" not in r.output


@pytest.mark.parametrize("display", ['Demo "Deluxe"', "Demo\\Agent", "Demo\nAgent", " Demo", "-Demo"])
def test_a_display_name_that_would_corrupt_the_manifest_is_refused(raven_home: Path, display: str) -> None:
    """The display is substituted verbatim into JSON, so the charset is fenced at the door."""
    r = runner.invoke(app, ["agents", "new", "demo-agent", "--display", display])

    assert r.exit_code != 0
    assert "letters, digits, spaces and hyphens" in r.output
    assert not (raven_home / "agents").exists()


# ---------------------------------------------------------------------------
# degraded worlds
# ---------------------------------------------------------------------------


def test_a_malformed_host_config_reads_as_no_roster(raven_home: Path) -> None:
    """The scaffold writes no config, so a broken one must not block the folder."""
    (raven_home / "config.json").write_text("{not json", encoding="utf-8")

    r = runner.invoke(app, ["agents", "new", "demo-agent", "--no-smoke"])

    assert r.exit_code == 0, r.output
    assert (raven_home / "agents" / "demo-agent" / "subagent.json").is_file()


def test_an_empty_template_tree_is_refused(raven_home: Path, tmp_path: Path, monkeypatch) -> None:
    empty = tmp_path / "no-templates"
    empty.mkdir()
    monkeypatch.setattr("raven.cli.agents_commands._templates_root", lambda: empty)

    r = runner.invoke(app, ["agents", "new", "demo-agent"])

    assert r.exit_code != 0
    assert "broken" in r.output


def test_cache_droppings_in_the_template_tree_are_not_scaffolded(raven_home: Path, tmp_path: Path, monkeypatch) -> None:
    """Only the stub lands (no .pyc, no .DS_Store); the doctor then rightly reds the stub tree."""
    root = tmp_path / "templates"
    (root / "__pycache__").mkdir(parents=True)
    (root / "__pycache__" / "run.cpython-312.pyc").write_text("x", encoding="utf-8")
    (root / ".DS_Store").write_text("x", encoding="utf-8")
    (root / "run.py").write_text('"""Launcher stub for my-agent."""\n', encoding="utf-8")
    monkeypatch.setattr("raven.cli.agents_commands._templates_root", lambda: root)

    r = runner.invoke(app, ["agents", "new", "demo-agent", "--no-smoke"])

    target = raven_home / "agents" / "demo-agent"
    assert {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()} == {"run.py"}
    assert r.exit_code != 0
    assert "subagent.json" in r.output


# ---------------------------------------------------------------------------
# the gold tests: new -> doctor -> discovery ready -> handshake
# ---------------------------------------------------------------------------

# The escape hatch for environments whose sandbox cannot spawn the launcher
# (the S1 spike proved an in-test spawn works on the dev boxes; a restricted
# runner sets the variable instead of deleting the test).
spawns_the_launcher = pytest.mark.skipif(
    os.environ.get("RAVEN_TESTS_NO_SPAWN") == "1",
    reason="RAVEN_TESTS_NO_SPAWN=1: this environment cannot spawn the generated launcher",
)


@spawns_the_launcher
def test_gold_new_is_discovered_ready_and_handshakes_green(raven_home: Path, monkeypatch) -> None:
    """The whole closed loop, no human in it: write, doctor, discovery, ACP initialize.

    A dummy own-key is enough for the handshake (S1: initialize spends no
    tokens and never touches the provider), so the test needs no credentials.
    """
    monkeypatch.setenv("DEMO_AGENT_API_KEY", "dummy-key-for-the-smoke")

    r = runner.invoke(app, ["agents", "new", "demo-agent"])

    assert r.exit_code == 0, r.output
    assert "subagent.json: valid (Demo-Agent, kind=acp)" in r.output
    assert "raven-plugin.toml: parsed (demo-agent-flow: 1 tool(s), 1 hook(s))" in r.output
    assert "py_compile: 6 file(s) OK" in r.output
    assert "readiness=ready" in r.output
    assert "handshake GREEN" in r.output and "protocolVersion=1" in r.output

    from raven.agent.subagent.vendored_agents import discover_product_rows, product_state

    root = raven_home / "agents"
    assert any(row.name == "Demo-Agent" and row.enabled for row in discover_product_rows(root))
    assert product_state(root)["Demo-Agent"].ready


@spawns_the_launcher
def test_gold_a_keyless_world_reports_the_fail_closed_refusal(raven_home: Path, monkeypatch) -> None:
    """No key anywhere: the launcher's loud exit is passed through as a readable
    outcome (the chain is wired; the refusal is its fail-closed design), not a
    doctor failure."""
    monkeypatch.delenv("DEMO_AGENT_API_KEY", raising=False)

    r = runner.invoke(app, ["agents", "new", "demo-agent"])

    assert r.exit_code == 0, r.output
    assert "readiness=ready" in r.output
    assert "fail-closed" in r.output
    assert "DEMO_AGENT_API_KEY is not set" in r.output
    assert "handshake GREEN" not in r.output


# ---------------------------------------------------------------------------
# doctor and smoke failure shapes
# ---------------------------------------------------------------------------


def test_doctor_names_every_broken_artifact_and_exits_nonzero(raven_home: Path, tmp_path: Path, monkeypatch) -> None:
    """A template tree whose files are wrong lands, then the doctor reds each check by name."""
    root = tmp_path / "templates"
    root.mkdir()
    (root / "subagent.json").write_text("{not json", encoding="utf-8")
    plugin = root / "plugins" / "my-agent-flow"
    plugin.mkdir(parents=True)
    (plugin / "raven-plugin.toml").write_text("[plugin\nid=", encoding="utf-8")
    (root / "run.py").write_text('"""Stub for my-agent."""\ndef broken(:\n', encoding="utf-8")
    monkeypatch.setattr("raven.cli.agents_commands._templates_root", lambda: root)

    r = runner.invoke(app, ["agents", "new", "demo-agent", "--no-smoke"])

    assert r.exit_code != 0
    assert "subagent.json: INVALID" in r.output
    assert "raven-plugin.toml: INVALID" in r.output
    assert "BROKEN" in r.output
    assert "left at" in r.output


def test_doctor_reports_a_row_that_is_listed_but_not_ready(raven_home: Path, tmp_path: Path, monkeypatch) -> None:
    """A valid manifest whose launcher file is missing: discovery lists the row disabled."""
    root = tmp_path / "templates"
    root.mkdir()
    (root / "subagent.json").write_text(
        json.dumps(
            {
                "name": "My-Agent",
                "kind": "acp",
                "command": "{PYTHON} {SUBAGENT_DIR}/run.py --acp",
                "cwd": "{SUBAGENT_DIR}",
            }
        ),
        encoding="utf-8",
    )
    plugin = root / "plugins" / "my-agent-flow"
    plugin.mkdir(parents=True)
    (plugin / "raven-plugin.toml").write_text('[plugin]\nid = "my-agent-flow"\nversion = "0.1.0"\n', encoding="utf-8")
    monkeypatch.setattr("raven.cli.agents_commands._templates_root", lambda: root)

    r = runner.invoke(app, ["agents", "new", "demo-agent", "--no-smoke"])

    assert r.exit_code != 0
    assert "NOT ready" in r.output
    assert "launcher" in r.output


def test_a_failed_smoke_exits_nonzero_and_keeps_the_folder(raven_home: Path, monkeypatch) -> None:
    monkeypatch.setattr("raven.cli.agents_commands._smoke_handshake", lambda row: ("failed", "no reply"))

    r = runner.invoke(app, ["agents", "new", "demo-agent"])

    assert r.exit_code != 0
    assert "smoke: FAILED" in r.output
    assert (raven_home / "agents" / "demo-agent" / "subagent.json").is_file()


@spawns_the_launcher
@pytest.mark.parametrize(
    ("script", "expected"),
    [
        ("print('plain text', flush=True)\nimport time\ntime.sleep(3)\n", "not JSON-RPC"),
        (
            "import json\nprint(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {}}), flush=True)\n",
            "not a legal InitializeResponse",
        ),
        ("pass\n", "no reply"),
    ],
)
def test_smoke_judges_the_three_bad_reply_shapes(tmp_path: Path, script: str, expected: str) -> None:
    """The smoke's own verdicts, against tiny stand-in launchers: garbage, bad shape, silence.

    The stand-in is a file, not a ``-c`` payload: the smoke tokenizes the
    command with ``str.split`` exactly the way discovery does, so the command
    line must stay space-free.
    """
    from types import SimpleNamespace

    from raven.cli.agents_commands import _smoke_handshake

    stub = tmp_path / "stub_launcher.py"
    stub.write_text(script, encoding="utf-8")
    row = SimpleNamespace(command=f"{sys.executable} {stub}", cwd="", ready_timeout_ms=15000)

    status, detail = _smoke_handshake(row)

    assert status == "failed"
    assert expected in detail


# ---------------------------------------------------------------------------
# the engine-wheel variant
# ---------------------------------------------------------------------------

ENGINE_AGENT_TREE = {
    ".env.example",
    "README.md",
    "config.json",
    "install.py",
    "run.py",
    "subagent.json",
}

ENGINE_TREE = {
    "pyproject.toml",
    "demo_agent_engine/__init__.py",
    "demo_agent_engine/raven-plugin.toml",
    "demo_agent_engine/plugin.py",
    "demo_agent_engine/tools/__init__.py",
    "demo_agent_engine/tools/hello.py",
}


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch) -> Path:
    work = tmp_path / "work"
    work.mkdir()
    monkeypatch.chdir(work)
    return work


def test_engine_wheel_scaffolds_both_trees_in_the_user_landing(raven_home: Path, workdir: Path) -> None:
    r = runner.invoke(app, ["agents", "new", "demo-agent", "--engine-wheel", "--no-smoke"])

    assert r.exit_code == 0, r.output
    target = raven_home / "agents" / "demo-agent"
    engine = workdir / "demo-agent-engine"
    assert {p.relative_to(target).as_posix() for p in target.rglob("*") if p.is_file()} == ENGINE_AGENT_TREE
    assert {p.relative_to(engine).as_posix() for p in engine.rglob("*") if p.is_file()} == ENGINE_TREE

    for root in (target, engine):
        # Materialized before compiling: py_compile writes bytecode, and a live
        # rglob would walk into the fresh __pycache__ and try to read it as text.
        files = [p for p in root.rglob("*") if p.is_file()]
        for index, path in enumerate(files):
            rel = path.relative_to(root).as_posix()
            text = path.read_text(encoding="utf-8")
            for token in TOKENS:
                assert token not in rel, (rel, token)
                assert token not in text, (rel, token)
            if rel.endswith(".py"):
                py_compile.compile(str(path), cfile=str(root.parent / f"scratch-{index}.pyc"), doraise=True)

    config = json.loads((target / "config.json").read_text(encoding="utf-8"))
    assert config == {"plugins": {"config": {"demo-agent-engine": {"enabled": True}}}}
    run_py = (target / "run.py").read_text(encoding="utf-8")
    assert 'ENGINE_PACKAGE = "demo_agent_engine"' in run_py
    assert 'FLOW_PLUGIN_ID = "demo-agent-engine"' in run_py
    row = json.loads((target / "subagent.json").read_text(encoding="utf-8"))
    assert row["engine"] == {"package": "demo_agent_engine", "wheel": "demo-agent-engine"}

    assert "disabled as designed" in r.output
    assert "demo-agent-engine" in r.output
    assert "pip install -e" in r.output


def test_engine_wheel_here_lands_on_the_plugins_dist_shelf(raven_home: Path, workdir: Path) -> None:
    r = runner.invoke(app, ["agents", "new", "demo-agent", "--here", "--engine-wheel", "--no-smoke"])

    assert r.exit_code == 0, r.output
    assert (workdir / "agents" / "demo-agent" / "run.py").is_file()
    engine = workdir / "plugins-dist" / "demo-agent-engine"
    assert {p.relative_to(engine).as_posix() for p in engine.rglob("*") if p.is_file()} == ENGINE_TREE
    assert not (raven_home / "agents").exists()


def test_engine_pyproject_and_manifest_parse(raven_home: Path, workdir: Path) -> None:
    r = runner.invoke(app, ["agents", "new", "demo-agent", "--engine-wheel", "--no-smoke"])

    assert r.exit_code == 0, r.output
    engine = workdir / "demo-agent-engine"
    with (engine / "pyproject.toml").open("rb") as f:
        pyproject = tomllib.load(f)
    assert pyproject["project"]["name"] == "demo-agent-engine"
    assert pyproject["project"]["entry-points"]["raven.plugins"] == {"demo-agent-engine": "demo_agent_engine"}
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["demo_agent_engine"]

    from raven.plugins.manifest import PluginManifest

    manifest = PluginManifest.from_toml_path(engine / "demo_agent_engine" / "raven-plugin.toml")
    assert manifest.id == "demo-agent-engine"
    assert [t.name for t in manifest.contributes.tools] == ["demo_agent_hello"]
    assert [h.name for h in manifest.contributes.hooks] == ["demo_agent_engine"]


def test_the_engine_row_passes_the_exported_subagent_schema(raven_home: Path, workdir: Path) -> None:
    import jsonschema

    r = runner.invoke(app, ["agents", "new", "demo-agent", "--engine-wheel", "--no-smoke"])

    assert r.exit_code == 0, r.output
    row = json.loads((raven_home / "agents" / "demo-agent" / "subagent.json").read_text(encoding="utf-8"))
    schema_path = Path(__file__).resolve().parent.parent / "schemas" / "subagent.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))

    jsonschema.Draft202012Validator(schema).validate(row)


def test_engine_readiness_disabled_until_the_wheel_is_importable(raven_home: Path, workdir: Path, monkeypatch) -> None:
    """Both halves against the real probe: sys.path injection over a find_spec
    monkeypatch, so the fact under test (importability where raven runs) is
    exercised, not faked. invalidate_caches because the injected directory was
    created after this interpreter started -- a fresh process (the real flow:
    pip install, restart the gateway) needs no such step, measured."""
    r = runner.invoke(app, ["agents", "new", "demo-agent", "--engine-wheel", "--no-smoke"])

    assert r.exit_code == 0, r.output
    from raven.agent.subagent.vendored_agents import discover_product_rows, product_state

    root = raven_home / "agents"
    row = next(row for row in discover_product_rows(root) if row.name == "Demo-Agent")
    state = product_state(root)["Demo-Agent"]
    assert not row.enabled
    assert not state.ready and state.kind == "engine"
    assert "demo-agent-engine" in state.detail

    import importlib

    monkeypatch.syspath_prepend(str(workdir / "demo-agent-engine"))
    importlib.invalidate_caches()

    row = next(row for row in discover_product_rows(root) if row.name == "Demo-Agent")
    assert row.enabled
    assert product_state(root)["Demo-Agent"].ready


def test_engine_wheel_dry_run_previews_both_trees(raven_home: Path, workdir: Path) -> None:
    r = runner.invoke(app, ["agents", "new", "demo-agent", "--engine-wheel", "--dry-run"])

    assert r.exit_code == 0, r.output
    assert r.output.count("would create") == 2
    assert "demo_agent_engine/plugin.py" in r.output
    assert "pyproject.toml" in r.output
    assert "plugins/demo-agent-flow" not in r.output
    assert not (raven_home / "agents").exists()
    assert not (workdir / "demo-agent-engine").exists()


def test_an_existing_engine_directory_is_refused(raven_home: Path, workdir: Path) -> None:
    (workdir / "demo-agent-engine").mkdir()

    r = runner.invoke(app, ["agents", "new", "demo-agent", "--engine-wheel", "--no-smoke"])

    assert r.exit_code != 0
    assert "already exists" in r.output
    assert not (raven_home / "agents").exists()


@spawns_the_launcher
def test_gold_engine_variant_reports_the_designed_disabled_state(raven_home: Path, workdir: Path) -> None:
    """The engine gold test: generate, doctor reports disabled-as-designed, and
    the smoke passes through the launcher's own engine refusal (which fires
    before any key handling, so no credentials are needed)."""
    r = runner.invoke(app, ["agents", "new", "demo-agent", "--engine-wheel"])

    assert r.exit_code == 0, r.output
    assert "disabled as designed" in r.output
    assert "fail-closed" in r.output
    assert "engine wheel is not installed" in r.output
    assert "pip install -e" in r.output
    assert "handshake GREEN" not in r.output


# ---------------------------------------------------------------------------
# the c7 hardening wave: findings from the pre-push adversarial review
# ---------------------------------------------------------------------------


def test_register_is_refused_while_the_engine_row_is_not_ready(raven_home: Path, workdir: Path) -> None:
    """H1: a pinned row overrides the discovered one wholesale and is never
    readiness-checked, so pinning a cannot-start row would put an enabled name
    on the dispatch roster."""
    r = runner.invoke(app, ["agents", "new", "demo-agent", "--engine-wheel", "--register", "--no-smoke"])

    assert r.exit_code != 0
    assert "--register refused" in r.output
    from raven.config.update_subagents import get_agents

    assert get_agents(config_path=raven_home / "config.json") == []
    assert (raven_home / "agents" / "demo-agent" / "subagent.json").is_file()


def test_a_display_collision_with_a_discovered_folder_is_refused(raven_home: Path) -> None:
    """H2: the tree is a roster without registration, so the name fence must
    read it -- not only config."""
    r1 = runner.invoke(app, ["agents", "new", "demo", "--no-smoke"])
    assert r1.exit_code == 0, r1.output

    r2 = runner.invoke(app, ["agents", "new", "demo-x", "--display", "Demo", "--no-smoke"])

    assert r2.exit_code != 0
    assert "one namespace across casings" in r2.output
    assert not (raven_home / "agents" / "demo-x").exists()


def test_a_folder_id_collision_in_another_casing_is_refused(raven_home: Path) -> None:
    r1 = runner.invoke(app, ["agents", "new", "demo", "--no-smoke"])
    assert r1.exit_code == 0, r1.output

    r2 = runner.invoke(app, ["agents", "new", "other", "--display", "demo", "--no-smoke"])

    assert r2.exit_code != 0
    assert not (raven_home / "agents" / "other").exists()


def test_the_doctor_reads_its_own_folder_not_a_same_named_one(raven_home: Path) -> None:
    """H2: pre-existing duplicates must not let the doctor misattribute --
    a broken same-named neighbor is not this scaffold's failure."""
    r1 = runner.invoke(app, ["agents", "new", "demo-x", "--display", "Alpha", "--no-smoke"])
    assert r1.exit_code == 0, r1.output
    (raven_home / "agents" / "demo-x" / "run.py").unlink()

    r2 = runner.invoke(app, ["agents", "new", "alpha", "--no-smoke"])

    assert r2.exit_code != 0
    assert "one namespace across casings" in r2.output


@pytest.mark.parametrize("name", ["nl-demo\n", "nl-demo\nx"])
def test_a_name_with_an_embedded_newline_is_refused(raven_home: Path, name: str) -> None:
    """M1: re.match with $ let a trailing newline through and created a
    newline-named directory in the real home."""
    r = runner.invoke(app, ["agents", "new", name])

    assert r.exit_code != 0
    assert not (raven_home / "agents").exists()


def test_a_display_with_a_trailing_newline_is_refused(raven_home: Path) -> None:
    r = runner.invoke(app, ["agents", "new", "nl-two", "--display", "Nl Two\n"])

    assert r.exit_code != 0
    assert not (raven_home / "agents").exists()


def test_stale_staging_residue_is_swept_and_never_discovered(raven_home: Path) -> None:
    """M2: what a SIGKILL between the last file write and the rename leaves."""
    residue = raven_home / "agents" / ".demo-two.partial-k3jx9q"
    residue.mkdir(parents=True)
    (residue / "subagent.json").write_text(
        json.dumps({"name": "Demo-Two", "kind": "acp", "command": "{PYTHON} {SUBAGENT_DIR}/run.py --acp"}),
        encoding="utf-8",
    )

    from raven.agent.subagent.vendored_agents import discover_product_rows

    assert discover_product_rows(raven_home / "agents") == []

    r = runner.invoke(app, ["agents", "new", "demo-two", "--no-smoke"])

    assert r.exit_code == 0, r.output
    assert not residue.exists()
    rows = discover_product_rows(raven_home / "agents")
    assert [row.name for row in rows] == ["Demo-Two"]


def test_a_builtin_name_in_another_casing_is_refused(raven_home: Path) -> None:
    """M3: 'RAVEN' beside the built-in 'Raven' would be an enabled impostor."""
    r = runner.invoke(app, ["agents", "new", "impostor", "--display", "RAVEN"])

    assert r.exit_code != 0
    assert "built-in" in r.output
    assert not (raven_home / "agents").exists()


def test_the_engine_declaration_round_trips_through_the_row_schema(raven_home: Path, workdir: Path) -> None:
    """L1: a pinned copy of the manifest must not silently drop the engine key."""
    r = runner.invoke(app, ["agents", "new", "demo-agent", "--engine-wheel", "--no-smoke"])
    assert r.exit_code == 0, r.output

    from raven.config.schema import ThirdPartyAcpSubagentConfig

    row = json.loads((raven_home / "agents" / "demo-agent" / "subagent.json").read_text(encoding="utf-8"))
    validated = ThirdPartyAcpSubagentConfig.model_validate(row)
    assert validated.engine is not None
    assert validated.engine.package == "demo_agent_engine"
    dumped = validated.model_dump(by_alias=True)
    assert dumped["engine"] == {"package": "demo_agent_engine", "wheel": "demo-agent-engine"}


@pytest.mark.skipif(
    os.geteuid() == 0, reason="root ignores permission bits, so the directory cannot be made unwritable"
)
def test_an_unwritable_target_refuses_cleanly(raven_home: Path, tmp_path: Path, monkeypatch) -> None:
    """L4: a traceback is not a refusal."""
    checkout = tmp_path / "frozen"
    checkout.mkdir()
    (checkout / "agents").mkdir()
    (checkout / "agents").chmod(0o555)
    monkeypatch.chdir(checkout)
    try:
        r = runner.invoke(app, ["agents", "new", "demo-agent", "--here", "--no-smoke"])
    finally:
        (checkout / "agents").chmod(0o755)

    assert r.exit_code != 0
    assert "Error:" in r.output and "cannot write" in r.output
    assert "Traceback" not in r.output


def test_the_landed_folder_is_not_mkdtemps_0700(raven_home: Path) -> None:
    """L5: the folder should match hand-made siblings, not the staging dir."""
    r = runner.invoke(app, ["agents", "new", "demo-agent", "--no-smoke"])

    assert r.exit_code == 0, r.output
    mode = (raven_home / "agents" / "demo-agent").stat().st_mode & 0o777
    assert mode == 0o755, oct(mode)


def test_engine_factories_decline_without_their_config_slice(raven_home: Path, workdir: Path) -> None:
    """H3b: an installed engine wheel activates in every raven process in the
    environment, so the factories themselves scope it to the one agent whose
    rendered config enables the slice."""
    r = runner.invoke(app, ["agents", "new", "demo-agent", "--engine-wheel", "--no-smoke"])
    assert r.exit_code == 0, r.output

    import importlib
    import sys

    sys.path.insert(0, str(workdir / "demo-agent-engine"))
    importlib.invalidate_caches()
    try:
        plugin = importlib.import_module("demo_agent_engine.plugin")
        hello = importlib.import_module("demo_agent_engine.tools.hello")

        class _Ctx:
            def __init__(self, config):
                self.config = config
                self.services = None
                self.logger = None

        assert plugin.make_hook(_Ctx({})) is None
        assert hello.make_hello(_Ctx({})) is None
        assert plugin.make_hook(_Ctx({"enabled": True})) is not None
        tool = hello.make_hello(_Ctx({"enabled": True}))
        assert tool is not None and tool.name == "demo_agent_hello"
    finally:
        sys.path.remove(str(workdir / "demo-agent-engine"))
        for mod in list(sys.modules):
            if mod.startswith("demo_agent_engine"):
                del sys.modules[mod]


@spawns_the_launcher
def test_smoke_fails_a_green_handshake_whose_plugin_activation_failed(tmp_path: Path) -> None:
    """H3c: a legal reply while the plugin registry failed to build is a broken
    agent behind a green light -- the agent runs without its own tools, hooks
    and plugin memory backend, so the smoke reds it instead of blessing it."""
    from types import SimpleNamespace

    from raven.cli.agents_commands import _smoke_handshake

    stub = tmp_path / "stub_launcher.py"
    stub.write_text(
        "import json\n"
        "import sys\n"
        "print(\n"
        "    'WARNING  plugin activation failed (tool contributed by two plugins); continuing without plugins.',\n"
        "    file=sys.stderr,\n"
        "    flush=True,\n"
        ")\n"
        "print(json.dumps({'jsonrpc': '2.0', 'id': 1, 'result': {'protocolVersion': 1}}), flush=True)\n"
        "import time\n"
        "time.sleep(3)\n",
        encoding="utf-8",
    )
    row = SimpleNamespace(command=f"{sys.executable} {stub}", cwd="", ready_timeout_ms=15000)

    status, detail = _smoke_handshake(row)

    assert status == "failed"
    assert "WITHOUT its own tools" in detail
    assert "plugin activation failed" in detail


def test_an_installed_engine_pollutes_nothing_and_two_scaffolds_no_longer_collide(
    raven_home: Path, workdir: Path, tmp_path: Path, monkeypatch
) -> None:
    """H3 end to end (the review's probe-w/probe-h scenario, in process): an
    engine wheel visible to entry-point discovery activates everywhere, but
    the derived tool names cannot collide with a sibling's, and the declined
    factories keep the host's and the sibling's turns empty of engine tools."""
    r1 = runner.invoke(app, ["agents", "new", "probe-w", "--engine-wheel", "--no-smoke"])
    assert r1.exit_code == 0, r1.output
    r2 = runner.invoke(app, ["agents", "new", "probe-h", "--no-smoke"])
    assert r2.exit_code == 0, r2.output

    # Make the engine what pip install -e makes it: importable AND registered
    # on the raven.plugins entry-point group (a dist-info beside the package).
    engine_root = workdir / "probe-w-engine"
    dist_info = engine_root / "probe_w_engine-0.1.0.dist-info"
    dist_info.mkdir()
    (dist_info / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: probe-w-engine\nVersion: 0.1.0\n", encoding="utf-8"
    )
    (dist_info / "entry_points.txt").write_text("[raven.plugins]\nprobe-w-engine = probe_w_engine\n", encoding="utf-8")

    import importlib

    monkeypatch.syspath_prepend(str(engine_root))
    importlib.invalidate_caches()

    from raven.config.raven import load_raven_config
    from raven.core.plugin_stack import build_plugin_registry, build_plugin_tools

    probe_h_plugins = raven_home / "agents" / "probe-h" / "plugins"
    sibling_config = tmp_path / "sibling-config.json"
    sibling_config.write_text(
        json.dumps({"plugins": {"dirs": [str(probe_h_plugins)], "config": {"probe-h-flow": {"enabled": True}}}}),
        encoding="utf-8",
    )
    cfg = load_raven_config(sibling_config)
    registry = build_plugin_registry(cfg)
    activated = set(registry.activated_ids())
    assert {"probe-h-flow", "probe-w-engine"} <= activated, activated

    workspace = tmp_path / "ws"
    workspace.mkdir()
    tool_names = {tool.name for tool in build_plugin_tools(workspace, cfg, registry=registry)}
    assert "probe_h_hello" in tool_names
    assert "probe_w_hello" not in tool_names

    host_config = tmp_path / "host-config.json"
    host_config.write_text("{}", encoding="utf-8")
    host_cfg = load_raven_config(host_config)
    host_registry = build_plugin_registry(host_cfg)
    assert "probe-w-engine" in set(host_registry.activated_ids())
    host_tools = {tool.name for tool in build_plugin_tools(workspace, host_cfg, registry=host_registry)}
    assert "probe_w_hello" not in host_tools


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def test_dry_run_leaves_stale_staging_residue_untouched(raven_home: Path) -> None:
    """A dry run promises zero disk mutation; sweeping residue is a mutation,
    so it belongs to the write path alone."""
    residue = raven_home / "agents" / ".victim.partial-abc123"
    residue.mkdir(parents=True)
    (residue / "half-written.txt").write_text("x", encoding="utf-8")

    r = runner.invoke(app, ["agents", "new", "demo-agent", "--dry-run"])

    assert r.exit_code == 0, r.output
    assert "dry run: nothing written" in r.output
    assert residue.is_dir() and (residue / "half-written.txt").is_file()

    r2 = runner.invoke(app, ["agents", "new", "demo-agent", "--no-smoke"])

    assert r2.exit_code == 0, r2.output
    assert not residue.exists()


def test_a_home_path_with_whitespace_is_refused_before_any_write(tmp_path: Path, monkeypatch) -> None:
    """The roster command template is split on whitespace, so a spacey landing
    can never be addressed as a command -- refuse whole rather than scaffold a
    folder the doctor then reports as a missing launcher."""
    home = tmp_path / "space y" / "home"
    home.mkdir(parents=True)
    monkeypatch.setenv("RAVEN_HOME", str(home))

    r = runner.invoke(app, ["agents", "new", "demo-agent"])

    assert r.exit_code != 0
    assert "whitespace" in r.output
    assert not (home / "agents").exists()


def test_a_spacey_working_directory_refuses_here_mode(raven_home: Path, tmp_path: Path, monkeypatch) -> None:
    checkout = tmp_path / "space y checkout"
    checkout.mkdir()
    monkeypatch.chdir(checkout)

    r = runner.invoke(app, ["agents", "new", "demo-agent", "--here"])

    assert r.exit_code != 0
    assert "whitespace" in r.output
    assert not (checkout / "agents").exists()
    assert not (raven_home / "agents").exists()


def test_an_explicit_schema_default_workspace_renders_like_an_omitted_one(raven_home: Path, monkeypatch) -> None:
    """The sentinel is the paper's WORKSPACE_DEFAULT_SENTINEL, not the literal
    'workspace': a config that carries the default explicitly must land the
    engine's Agent home exactly where an omitted value does -- outside the
    host Agent home, never as a literal path under the state root."""
    import importlib.util

    from raven.contracts.path_policy import WORKSPACE_DEFAULT_SENTINEL

    r = runner.invoke(app, ["agents", "new", "demo-agent", "--no-smoke"])
    assert r.exit_code == 0, r.output
    target = raven_home / "agents" / "demo-agent"

    monkeypatch.setenv("DEMO_AGENT_API_KEY", "dummy-key-for-render")
    spec = importlib.util.spec_from_file_location("c10_generated_run", target / "run.py")
    run = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(run)

    explicit = target / "explicit-default.json"
    config = json.loads((target / "config.json").read_text(encoding="utf-8"))
    config["agents"] = {"defaults": {"workspace": WORKSPACE_DEFAULT_SENTINEL}}
    explicit.write_text(json.dumps(config), encoding="utf-8")

    explicit_ws = json.loads(run.render_config(explicit).read_text(encoding="utf-8"))["agents"]["defaults"]["workspace"]
    omitted_ws = json.loads(run.render_config(target / "config.json").read_text(encoding="utf-8"))["agents"][
        "defaults"
    ]["workspace"]

    assert explicit_ws == omitted_ws
    host_agent_home = raven_home / "workspace"
    assert not Path(explicit_ws).is_relative_to(host_agent_home)
    assert "~" not in explicit_ws
    explicit.unlink()


# ---------------------------------------------------------------------------
# the c12 wave: one {PYTHON} resolution for guard, register and installer
# ---------------------------------------------------------------------------


@pytest.fixture
def clean_alt_python(tmp_path: Path) -> Path:
    """An existing, space-free interpreter path that is not sys.executable."""
    alt = tmp_path / "pyalt"
    alt.symlink_to(sys.executable)
    return alt


def test_a_clean_subagent_python_passes_the_gate_and_is_what_register_writes(
    raven_home: Path, clean_alt_python: Path, monkeypatch
) -> None:
    """The guard and the writer share one resolver: a clean SUBAGENT_PYTHON
    over a spacey sys.executable passes, and the pinned row carries the clean
    interpreter -- never the spacey one the guard did not check."""
    monkeypatch.setenv("SUBAGENT_PYTHON", str(clean_alt_python))
    monkeypatch.setattr(sys, "executable", "/spa cey/python")

    r = runner.invoke(app, ["agents", "new", "demo-agent", "--register", "--no-smoke"])

    assert r.exit_code == 0, r.output
    from raven.config.update_subagents import get_agents

    (row,) = get_agents(config_path=raven_home / "config.json")
    assert row["command"].startswith(str(clean_alt_python))
    assert "spa cey" not in row["command"]


def test_a_spacey_subagent_python_is_refused_even_with_a_clean_sys_executable(raven_home: Path, monkeypatch) -> None:
    monkeypatch.setenv("SUBAGENT_PYTHON", "/spa cey/python")

    r = runner.invoke(app, ["agents", "new", "demo-agent"])

    assert r.exit_code != 0
    assert "whitespace" in r.output
    assert not (raven_home / "agents").exists()


def test_the_registered_interpreter_equals_the_discovered_one(
    raven_home: Path, clean_alt_python: Path, monkeypatch
) -> None:
    """The consistency pin: one folder, one interpreter, whichever side reads it."""
    monkeypatch.setenv("SUBAGENT_PYTHON", str(clean_alt_python))

    r = runner.invoke(app, ["agents", "new", "demo-agent", "--register", "--no-smoke"])

    assert r.exit_code == 0, r.output
    from raven.agent.subagent.vendored_agents import discover_product_rows
    from raven.config.update_subagents import get_agents

    (discovered,) = discover_product_rows(raven_home / "agents")
    (registered,) = get_agents(config_path=raven_home / "config.json")
    assert registered["command"] == discovered.command
    assert registered["command"].startswith(str(clean_alt_python))


def test_the_generated_installer_honors_subagent_python_and_refuses_whitespace(
    raven_home: Path, clean_alt_python: Path, monkeypatch
) -> None:
    """The template installer resolves {PYTHON} the way discovery does, and
    refuses loudly when the resolved interpreter carries whitespace."""
    import importlib.util

    r = runner.invoke(app, ["agents", "new", "demo-agent", "--no-smoke"])
    assert r.exit_code == 0, r.output
    target = raven_home / "agents" / "demo-agent"

    spec = importlib.util.spec_from_file_location("c12_generated_install", target / "install.py")
    install = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(install)

    monkeypatch.setenv("SUBAGENT_PYTHON", str(clean_alt_python))
    assert install.main() == 0
    from raven.config.update_subagents import get_agents

    (row,) = get_agents(config_path=raven_home / "config.json")
    assert row["command"].startswith(str(clean_alt_python))
    assert row["cwd"] == str(target)

    monkeypatch.setenv("SUBAGENT_PYTHON", "/spa cey/python")
    with pytest.raises(SystemExit, match="whitespace"):
        install.main()
