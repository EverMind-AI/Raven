"""Static layers of the agent package validator: folder (F), declarations (D), code (C).

Every rule has a mutant: the scaffold of ``raven agents new`` with exactly one
violation, which must fail that rule and no other. The shipped agents calibrate
the rules: a rule they fail is a wrong rule.
"""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from evolver.validator.scope import check_code, check_declarations, check_folder, other_plugin_packages
from evolver.validator.snapshot import take_snapshot, tree_digest
from tests.evolver_validator_stub import REPO, scaffold_template

Mutation = Callable[[Path], None]


@pytest.fixture(scope="module")
def template(tmp_path_factory) -> Path:
    return scaffold_template(tmp_path_factory.mktemp("scaffold"))


@pytest.fixture
def folder(template: Path, tmp_path: Path) -> Path:
    copy = tmp_path / "src" / template.name
    shutil.copytree(template, copy, symlinks=True)
    return copy


def _failed(layer) -> set[str]:
    return {r.rule for r in layer.results if r.status == "fail"}


def rm(rel: str) -> Mutation:
    return lambda root: (root / rel).unlink()


def add(rel: str, data: bytes) -> Mutation:
    def apply(root: Path) -> None:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    return apply


def symlink_to(target: str, rel: str = "notes.md") -> Mutation:
    return lambda root: os.symlink(target, root / rel)


def test_snapshot_excludes_build_output_and_records_it(folder, tmp_path):
    add("plugins/demo-agent-flow/demo_agent_flow/__pycache__/plugin.cpython-312.pyc", b"\0")(folder)
    snap = take_snapshot(folder, tmp_path / "run", engine=None)
    assert not list(snap.root.rglob("__pycache__"))
    assert snap.build_output == ("plugins/demo-agent-flow/demo_agent_flow/__pycache__/plugin.cpython-312.pyc",)
    assert snap.root == tmp_path / "run" / "package" / folder.name
    assert snap.digest == tree_digest(snap.root)


def test_snapshot_digest_tracks_content(folder, tmp_path):
    first = take_snapshot(folder, tmp_path / "a", engine=None).digest
    (folder / "README.md").write_text("changed\n", encoding="utf-8")
    assert take_snapshot(folder, tmp_path / "b", engine=None).digest != first


def test_folder_layer_passes_on_scaffold(folder, tmp_path):
    layer = check_folder(take_snapshot(folder, tmp_path / "run", engine=None))
    assert layer.layer == "F"
    assert layer.passed, [r for r in layer.results if r.status == "fail"]


@pytest.mark.parametrize(
    "mutate, rule",
    [
        (rm("run.py"), "F1"),
        (add("data.bin", b"x"), "F2"),
        (add(".env", b"A=1\n"), "F3"),
        (add(".env.example", b"DEMO_AGENT_API_KEY=sk-live-123\n"), "F3"),
        (add("modes/max.json", b'{"providers": {"x": {"apiKey": "sk-or-1"}}}'), "F3"),
        (
            add(
                "plugins/demo-agent-flow/demo_agent_flow/keys.py",
                b'TOKEN = "sk-or-v1-0123456789abcdef0123456789abcdef"\n',
            ),
            "F3",
        ),
        (add("notes.md", b"Use key sk-proj-AbCdEfGhIjKlMnOpQrStUvWx12345678 for tests.\n"), "F3"),
        (symlink_to("/etc/hostname"), "F4"),
        (add("big.md", b"x" * (1024 * 1024 + 1)), "F4"),
    ],
)
def test_folder_mutant_fails_exactly_its_rule(folder, tmp_path, mutate, rule):
    mutate(folder)
    assert _failed(check_folder(take_snapshot(folder, tmp_path / "run", engine=None))) == {rule}


def test_commented_or_empty_key_slots_are_not_credentials(folder, tmp_path):
    add(".env.example", b"# DEMO_AGENT_API_KEY=sk-example\nDEMO_AGENT_API_KEY=\n")(folder)
    assert check_folder(take_snapshot(folder, tmp_path / "run", engine=None)).passed


def test_build_output_is_advisory_f5(folder, tmp_path):
    add("plugins/demo-agent-flow/demo_agent_flow/__pycache__/x.pyc", b"\0")(folder)
    layer = check_folder(take_snapshot(folder, tmp_path / "run", engine=None))
    assert layer.passed
    assert [r.rule for r in layer.results if r.status == "advisory"] == ["F5"]


def test_scaffold_json_files_are_valid(template):
    for path in template.rglob("*.json"):
        json.loads(path.read_text(encoding="utf-8"))


def set_json(rel: str, key: str, value) -> Mutation:
    def apply(root: Path) -> None:
        path = root / rel
        data = json.loads(path.read_text(encoding="utf-8"))
        data[key] = value
        path.write_text(json.dumps(data), encoding="utf-8")

    return apply


def merge_config(extra: dict) -> Mutation:
    def merge(base: dict, more: dict) -> dict:
        for key, value in more.items():
            base[key] = merge(base.get(key, {}), value) if isinstance(value, dict) else value
        return base

    def apply(root: Path) -> None:
        path = root / "config.json"
        path.write_text(json.dumps(merge(json.loads(path.read_text(encoding="utf-8")), extra)), encoding="utf-8")

    return apply


def replace_in_manifest(old: str, new: str) -> Mutation:
    def apply(root: Path) -> None:
        (path,) = root.glob("plugins/*/raven-plugin.toml")
        text = path.read_text(encoding="utf-8")
        assert old in text
        path.write_text(text.replace(old, new, 1), encoding="utf-8")

    return apply


def test_declarations_pass_on_scaffold(folder, tmp_path):
    layer = check_declarations(take_snapshot(folder, tmp_path / "run", engine=None), shipped_root=None)
    assert layer.layer == "D"
    assert layer.passed, [r for r in layer.results if r.status == "fail"]


@pytest.mark.parametrize(
    "mutate, rule",
    [
        (set_json("subagent.json", "kind", "cli"), "D1"),
        (set_json("subagent.json", "description", "TODO: describe"), "D2"),
        (set_json("subagent.json", "owns", ""), "D2"),
        (set_json("subagent.json", "command", "/usr/bin/python3 {SUBAGENT_DIR}/run.py"), "D3"),
        (set_json("subagent.json", "cwd", "/srv/agent"), "D3"),
        (replace_in_manifest('factory = "demo_agent_flow.plugin', 'factory = "not a ref'), "D4"),
        (merge_config({"agents": {"defaults": {"maxToolIterations": "lots"}}}), "D5"),
        (add("config.json", b"{not json"), "D5"),
        (merge_config({"plugins": {"dirs": ["/x"]}}), "D6"),
        (merge_config({"plugins": {"config": {"everos-memory": {"base_url": "http://127.0.0.1:1"}}}}), "D6"),
    ],
)
def test_declaration_mutant_fails_exactly_its_rule(folder, tmp_path, mutate, rule):
    mutate(folder)
    layer = check_declarations(take_snapshot(folder, tmp_path / "run", engine=None), shipped_root=None)
    assert _failed(layer) == {rule}


def test_credential_in_config_is_d6(folder, tmp_path):
    merge_config({"providers": {"openrouter": {"apiKey": "sk-or-123"}}})(folder)
    layer = check_declarations(take_snapshot(folder, tmp_path / "run", engine=None), shipped_root=None)
    assert "D6" in _failed(layer)


def test_unprefixed_tool_name_is_advisory_d7(folder, tmp_path):
    replace_in_manifest('name = "demo_agent_hello"', 'name = "hello"')(folder)
    layer = check_declarations(take_snapshot(folder, tmp_path / "run", engine=None), shipped_root=None)
    assert layer.passed
    assert "D7" in {r.rule for r in layer.results if r.status == "advisory"}


def test_shared_memory_identity_is_advisory_d7(folder, tmp_path):
    other = tmp_path / "shipped" / "other-agent"
    other.mkdir(parents=True)
    (other / "config.json").write_text(json.dumps({"memory": {"userId": "demo-agent"}}), encoding="utf-8")
    layer = check_declarations(take_snapshot(folder, tmp_path / "run", engine=None), shipped_root=tmp_path / "shipped")
    assert layer.passed
    assert any(r.rule == "D7" and "other-agent" in r.message for r in layer.results)


def append_py(rel: str, text: str) -> Mutation:
    def apply(root: Path) -> None:
        path = root / rel
        path.write_text(path.read_text(encoding="utf-8") + "\n" + text, encoding="utf-8")

    return apply


HELLO = "plugins/demo-agent-flow/demo_agent_flow/tools/hello.py"


def _code_layer(folder: Path, run: Path):
    snap = take_snapshot(folder, run, engine=None)
    return check_code(snap, other_packages=other_plugin_packages(REPO / "agents", snap))


def test_other_plugin_packages_names_shipped_flows_not_own(folder, tmp_path):
    snap = take_snapshot(folder, tmp_path / "run", engine=None)
    others = other_plugin_packages(REPO / "agents", snap)
    assert {"code_flow", "research_flow", "oncall_flow", "raven_design", "raven_ppt"} <= others
    assert "demo_agent_flow" not in others


def test_code_layer_passes_on_scaffold(folder, tmp_path):
    layer = _code_layer(folder, tmp_path / "run")
    assert layer.layer == "C"
    assert layer.passed, [r for r in layer.results if r.status == "fail"]


@pytest.mark.parametrize(
    "mutate, rule",
    [
        (append_py(HELLO, "def broken(:\n"), "C1"),
        (append_py(HELLO, "import evolver\n"), "C2"),
        (append_py(HELLO, "from code_flow import flow\n"), "C2"),
        (append_py(HELLO, "import pathlib\npathlib.Path(__file__).with_name('x.txt').write_text('x')\n"), "C3"),
        (append_py(HELLO, "import no_such_module_xyz\n"), "C3"),
    ],
)
def test_code_mutant_fails_exactly_its_rule(folder, tmp_path, mutate, rule):
    mutate(folder)
    assert _failed(_code_layer(folder, tmp_path / "run")) == {rule}


SHIPPED = [
    ("raven-code", None),
    ("raven-research", None),
    ("raven-oncall", None),
    ("raven-design", "plugins-dist/design-engine"),
    ("raven-ppt", "plugins-dist/ppt-engine"),
]


@pytest.mark.parametrize("name, engine", SHIPPED)
def test_shipped_agent_passes_static_layers(name, engine, tmp_path):
    snap = take_snapshot(REPO / "agents" / name, tmp_path / "run", engine=engine and REPO / engine)
    for layer in (
        check_folder(snap),
        check_declarations(snap, shipped_root=REPO / "agents"),
        check_code(snap, other_packages=other_plugin_packages(REPO / "agents", snap)),
    ):
        assert layer.passed, [r for r in layer.results if r.status == "fail"]
