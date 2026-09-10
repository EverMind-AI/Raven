"""Contract tests for the exported agent schemas under ``schemas/``.

The pydantic models are the source of truth and the schema files are
generated artifacts; the byte-equality test below is the drift guard, and it
is mutation-killable in both directions -- edit a model or hand-edit a file
on disk and it reds. The instance tests then prove the exported artifacts
validate the real manifests in this repo: every shipped ``subagent.json``
plus the scaffold template (token state), and every ``raven-plugin.toml`` on
the tree.

Named for its subject rather than a CLI module: the contract is between the
models and the files editors consume, so the ``test_cli_<module>_commands``
shape does not apply.
"""

from __future__ import annotations

import importlib.util
import json
import tomllib
from pathlib import Path

import jsonschema
import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO_ROOT / "schemas"

SUBAGENT_MANIFESTS = sorted(REPO_ROOT.glob("agents/*/subagent.json")) + [
    REPO_ROOT / "raven" / "templates" / "agents_scaffold" / "subagent.json",
]

PLUGIN_MANIFESTS = sorted(
    list(REPO_ROOT.glob("agents/*/plugins/*/raven-plugin.toml"))
    + list(REPO_ROOT.glob("plugins-dist/*/*/raven-plugin.toml"))
    + list(REPO_ROOT.glob("raven/plugins/bundled/*/raven-plugin.toml"))
    + list(REPO_ROOT.glob("raven/templates/agents_scaffold/plugins/*/raven-plugin.toml"))
)


def _fresh_export() -> dict[str, str]:
    spec = importlib.util.spec_from_file_location(
        "export_agent_schemas", REPO_ROOT / "scripts" / "export_agent_schemas.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.render_schemas()


def _validator(filename: str) -> jsonschema.Draft202012Validator:
    schema = json.loads((SCHEMAS_DIR / filename).read_text(encoding="utf-8"))
    jsonschema.Draft202012Validator.check_schema(schema)
    return jsonschema.Draft202012Validator(schema)


# ---------------------------------------------------------------------------
# the drift guard
# ---------------------------------------------------------------------------


def test_the_files_on_disk_equal_a_fresh_export_byte_for_byte() -> None:
    rendered = _fresh_export()

    assert set(rendered) == {"subagent.schema.json", "raven-plugin.schema.json"}
    for filename, content in rendered.items():
        on_disk = (SCHEMAS_DIR / filename).read_text(encoding="utf-8")
        assert on_disk == content, (
            f"schemas/{filename} does not match the models; regenerate via "
            "`uv run python scripts/export_agent_schemas.py` (never hand-edit the file)"
        )


def test_the_export_is_deterministic() -> None:
    assert _fresh_export() == _fresh_export()


# ---------------------------------------------------------------------------
# the exported artifacts validate the real manifests
# ---------------------------------------------------------------------------


def test_the_manifest_inventories_are_not_empty() -> None:
    """A glob gone stale must fail loudly, not pass on zero instances."""
    assert len(SUBAGENT_MANIFESTS) >= 6
    assert len(PLUGIN_MANIFESTS) >= 8


@pytest.mark.parametrize("path", SUBAGENT_MANIFESTS, ids=lambda p: p.parent.name)
def test_every_subagent_manifest_passes_the_subagent_schema(path: Path) -> None:
    row = json.loads(path.read_text(encoding="utf-8"))

    _validator("subagent.schema.json").validate(row)


@pytest.mark.parametrize("path", PLUGIN_MANIFESTS, ids=lambda p: p.parent.name)
def test_every_plugin_manifest_passes_the_plugin_schema(path: Path) -> None:
    with path.open("rb") as f:
        doc = tomllib.load(f)

    _validator("raven-plugin.schema.json").validate(doc)
