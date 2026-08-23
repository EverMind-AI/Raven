"""``read_skill`` / ``use_skill`` — Skill Hub retrieval tools.

Two tools that complete the Skill Hub integration's progressive disclosure
(catalog -> body -> scripts). The discovery half (``HubSkillSource`` injecting
the ``# Skills`` catalog) already lands the candidate list each turn; these
tools are the on-demand fetch the LLM drives after seeing it.

``read_skill`` is **Hub-driven**. Local and Everos candidates already ship
their ``content`` in the ``RouterHit`` (rendered into the ``# Skills`` /
``# Active Skills`` context), so the body round-trip only matters for Hub
candidates (``hub/<slug>``) whose catalog entry is metadata-only.

``use_skill`` is **source-agnostic**. The LLM sees one fused, source-tagged
catalog, so it should not reason about provenance — it calls ``use_skill`` with
the qualified id and the tool dispatches on the ``<source>/`` prefix:

- ``local/<name>`` / ``everos/<id>`` — already materialized on disk (Everos
  skills are written to ``<workspace>/skills/everos/<id>/`` by the evolver);
  resolve the skill dir via the registry and return its ``scripts/`` path. No
  download — for these sources ``use_skill`` is effectively a no-op resolver.
- ``hub/<slug>`` — download + safely extract the zip into the workspace skill
  tree (so it becomes registry-discoverable), then return the ``scripts/`` path.

Both return a text blob (the ``Tool`` contract is ``-> str``): a markdown header
plus the ``scripts_dir`` line and the SKILL.md body when present.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from raven.agent.tools.base import Tool

if TYPE_CHECKING:
    from raven.memory_engine.skill_local.registry import SkillRegistry
    from raven.skill_hub import SkillHubClient

logger = logging.getLogger(__name__)


def _split_qualified_id(skill_id: str) -> tuple[str, str]:
    """Split ``<source>/<native_id>``. A bare id (no slash) is assumed Hub —
    that's the only source whose body/bundle is fetched remotely."""
    source, sep, native = skill_id.partition("/")
    if not sep:
        return "hub", source
    return source, native


class ReadSkillTool(Tool):
    """Fetch a candidate skill's full SKILL.md body for fine-selection."""

    def __init__(
        self,
        client: "SkillHubClient | None" = None,
        registry: "SkillRegistry | None" = None,
    ) -> None:
        self._client = client
        self._registry = registry

    @property
    def name(self) -> str:
        return "read_skill"

    @property
    def description(self) -> str:
        return (
            "Read the full SKILL.md body of a candidate skill from the "
            "'# Skills' catalog so you can decide whether it fits before using "
            "it. Pass the skill's qualified id exactly as shown in brackets in "
            "the catalog (e.g. 'hub/my-skill'). This does NOT download or run "
            "anything — it only returns the instructions. For local/everos "
            "skills the body is usually already in your context."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "skill_id": {
                    "type": "string",
                    "description": (
                        "The skill's qualified id, exactly as shown in the "
                        "'# Skills' catalog brackets (e.g. 'hub/my-skill')."
                    ),
                },
            },
            "required": ["skill_id"],
        }

    async def execute(self, skill_id: Any = None, **_: Any) -> str:
        if not skill_id or not isinstance(skill_id, str):
            return "Error: 'skill_id' is required — a skill's qualified id like 'hub/<slug>'."
        source, native = _split_qualified_id(skill_id)

        if source in ("local", "everos"):
            meta = self._registry.get(native, source=source) if self._registry else None
            if meta is None:
                return (
                    f"Error: no {source} skill {native!r} found. Its body may "
                    f"already be present in the '# Skills' context."
                )
            return f"## {meta.name}\n{meta.content}"

        if self._client is None:
            return "Error: Skill Hub is not configured; cannot read a remote skill body."
        try:
            meta = await self._client.get(native)
        except Exception as e:  # noqa: BLE001 — surface as tool error, not a crash
            return f"Error: failed to read skill {skill_id!r} from the Hub: {e}"
        body = meta.get("skill_md") or ""
        name = meta.get("name") or native
        version = meta.get("version") or ""
        tags = meta.get("tags") or meta.get("scenario_tags") or []
        head = f"## {name}" + (f" ({version})" if version else "")
        if tags:
            head += f"\ntags: {', '.join(str(t) for t in tags)}"
        return f"{head}\n\n{body}" if body else f"{head}\n[no body returned]"


class UseSkillTool(Tool):
    """Materialize a skill's bundled scripts/assets to local disk for ``exec``."""

    def __init__(
        self,
        client: "SkillHubClient | None" = None,
        registry: "SkillRegistry | None" = None,
    ) -> None:
        self._client = client
        self._registry = registry

    @property
    def name(self) -> str:
        return "use_skill"

    @property
    def description(self) -> str:
        return (
            "Make a skill's bundled scripts/assets available on local disk so "
            "you can run them via the exec tool. Pass the skill's qualified id "
            "from the '# Skills' catalog (e.g. 'local/x', 'everos/x', "
            "'hub/x'). Returns the SKILL.md body plus a 'scripts_dir' path when "
            "the skill ships runnable files. Pure-instruction skills need no "
            "scripts — just follow the body; you only need this for skills "
            "whose SKILL.md references files under scripts/."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "skill_id": {
                    "type": "string",
                    "description": (
                        "The skill's qualified id, exactly as shown in the "
                        "'# Skills' catalog brackets (e.g. 'hub/my-skill')."
                    ),
                },
            },
            "required": ["skill_id"],
        }

    async def execute(self, skill_id: Any = None, **_: Any) -> str:
        if not skill_id or not isinstance(skill_id, str):
            return "Error: 'skill_id' is required — a skill's qualified id like 'hub/<slug>'."
        source, native = _split_qualified_id(skill_id)

        if source in ("local", "everos"):
            return self._use_on_disk(source, native)
        if source == "hub":
            return await self._use_hub(native)
        return f"Error: unknown skill source {source!r} in {skill_id!r} (expected one of local/everos/hub)."

    def _use_on_disk(self, source: str, native: str) -> str:
        """Resolve an already-materialized local/everos skill dir."""
        meta = self._registry.get(native, source=source) if self._registry else None
        if meta is None:
            return (
                f"Error: no {source} skill {native!r} found on disk. If it is a "
                f"pure-instruction skill its body is already in your context."
            )
        scripts = meta.path.parent / "scripts"
        if scripts.is_dir():
            return f"## {meta.name}\nscripts_dir: {scripts}\ncached: true\n\n{meta.content}"
        return f"## {meta.name}\n(no bundled scripts — pure-instruction skill; follow the body)\n\n{meta.content}"

    async def _use_hub(self, native: str) -> str:
        """Download + extract a Hub skill, then expose its scripts dir."""
        if self._client is None:
            return "Error: Skill Hub is not configured; cannot fetch a remote skill."
        try:
            info = await self._client.install(native)
        except Exception as e:  # noqa: BLE001 — surface as tool error, not a crash
            return f"Error: failed to install skill {native!r} from the Hub: {e}"
        # Best-effort: make the freshly extracted skill visible to the
        # registry on subsequent turns. No-op if the cache isn't a scanned
        # source yet; the returned scripts_dir is usable this turn regardless.
        if self._registry is not None:
            try:
                self._registry.invalidate_source("hub")
            except Exception:  # noqa: BLE001
                pass
        scripts_dir = info.get("scripts_dir")
        body = info.get("skill_md") or ""
        name = info.get("slug") or native
        version = info.get("version") or ""
        head = f"## {name}" + (f" ({version})" if version else "")
        if scripts_dir:
            return f"{head}\nscripts_dir: {scripts_dir}\n\n{body}"
        return f"{head}\n(no bundled scripts — pure-instruction skill; follow the body)\n\n{body}"


__all__ = ["ReadSkillTool", "UseSkillTool"]
