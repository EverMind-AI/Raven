"""Export the agent-facing JSON Schemas from the pydantic models.

Two files land under the top-level ``schemas/`` directory, for editors to
consume (VS Code ``json.schemas``, taplo's schema associations) so an agent
author gets field-level validation while typing, one step before any doctor
or loader runs:

- ``subagent.schema.json`` -- an agent folder's ``subagent.json`` roster row.
  A discriminated union on ``kind`` restricted to the two kinds a folder
  manifest can declare (``acp`` / ``cli``): the same discrimination the config
  loader's ``AgentConfig`` uses, so an editor error points at the right
  variant, and restricted because discovery never materializes a folder as
  ``builtin`` or ``openai``.
- ``raven-plugin.schema.json`` -- a ``raven-plugin.toml`` file. The pydantic
  model (``PluginManifest``) describes the ``[plugin]`` table; the export
  wraps it in the one-key envelope exactly as ``from_toml_path`` unwraps it.

The schemas are generated artifacts: the models are the source of truth, and
``tests/test_agent_schemas.py`` holds the drift guard (the files on disk must
equal a fresh export byte for byte). Output is deterministic -- sorted keys,
two-space indent, ASCII, trailing newline -- so a regeneration with nothing
changed is a no-op in git.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO_ROOT / "schemas"
COMMENT = "generated, do not edit; regenerate via: uv run python scripts/export_agent_schemas.py"
JSON_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"


def _subagent_schema() -> dict[str, Any]:
    from pydantic import Field, TypeAdapter

    from raven.config.schema import ThirdPartyAcpSubagentConfig, ThirdPartyCliSubagentConfig

    row = Annotated[
        ThirdPartyAcpSubagentConfig | ThirdPartyCliSubagentConfig,
        Field(discriminator="kind"),
    ]
    # by_alias so the schema speaks the manifests' own camelCase spellings
    # (readyTimeoutMs, everos.userId), which is what the loader reads.
    schema = TypeAdapter(row).json_schema(by_alias=True)
    return {
        "$schema": JSON_SCHEMA_DIALECT,
        "$comment": COMMENT,
        "title": "subagent.json",
        "description": (
            "An agent folder's roster-row manifest, as discovered from agents/<folder>/subagent.json. "
            "Exported from raven.config.schema (ThirdPartyAcpSubagentConfig | ThirdPartyCliSubagentConfig)."
        ),
        **schema,
    }


def _plugin_schema() -> dict[str, Any]:
    from raven.plugins.manifest import PluginManifest

    inner = PluginManifest.model_json_schema()
    defs = inner.pop("$defs", {})
    return {
        "$schema": JSON_SCHEMA_DIALECT,
        "$comment": COMMENT,
        "title": "raven-plugin.toml",
        "description": (
            "A plugin manifest: everything lives under the [plugin] table, mirroring "
            "PluginManifest.from_toml_path, which unwraps that one key before validating. "
            "Exported from raven.plugins.manifest.PluginManifest."
        ),
        "type": "object",
        "required": ["plugin"],
        "properties": {"plugin": inner},
        "$defs": defs,
    }


def render_schemas() -> dict[str, str]:
    """Each schema file's exact content, keyed by filename.

    The single render used by both the writer below and the contract test's
    byte-for-byte comparison, so "what the script would write" cannot drift
    from "what the test checks".
    """
    payloads = {
        "subagent.schema.json": _subagent_schema(),
        "raven-plugin.schema.json": _plugin_schema(),
    }
    return {name: json.dumps(payload, indent=2, sort_keys=True) + "\n" for name, payload in payloads.items()}


def main() -> int:
    SCHEMAS_DIR.mkdir(parents=True, exist_ok=True)
    for name, content in sorted(render_schemas().items()):
        path = SCHEMAS_DIR / name
        changed = not path.exists() or path.read_text(encoding="utf-8") != content
        path.write_text(content, encoding="utf-8")
        print(f"{'wrote' if changed else 'unchanged'} {path.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
