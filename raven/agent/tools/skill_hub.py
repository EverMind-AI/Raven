"""``read_skill`` / ``use_skill`` — Skill Hub retrieval tools.

Two tools that complete the Skill Hub integration's progressive disclosure
(catalog -> body -> scripts). The discovery half (``HubSkillSource`` injecting
the ``# Skills`` catalog) already lands the candidate list each turn; these
tools are the on-demand fetch the LLM drives after seeing it.

``read_skill`` is the **body** route, for every source. A Hub candidate's
catalog entry is metadata-only; a local skill declaring ``inject: description``
is advertised by its digest entry alone and is excluded from BM25 routing, so
for both the body exists nowhere in context until this tool fetches it. Local
and Everos ids resolve straight from the registry, which is why this tool
registers wherever the registry does and needs no Hub endpoint.

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
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from raven.contracts.tool import Tool
from raven.skill_hub.audit import record_install, write_install_meta
from raven.skill_hub.policy import SkillPolicy, is_blocked, refuses_low_safety

__all__ = ["FindSkillTool", "ReadSkillTool", "UseSkillTool"]

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

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


def _lookup_on_disk(registry: "SkillRegistry | None", source: str, native: str):
    """Resolve a router-namespaced id against the physical-layer registry.

    The two carry different vocabularies and must be translated, not passed
    through: ``local`` is a router namespace spanning every on-disk layer
    (``workspace`` / ``builtin`` / ``external`` / ``mirror/*``) and is never
    itself a layer, so it resolves to the layer-priority winner. ``everos`` is
    both a namespace and a layer, and keeps its exact compound-key lookup.
    """
    if registry is None:
        return None
    if source == "local":
        return registry.get(native)
    return registry.get(native, source=source)


class ReadSkillTool(Tool):
    """Fetch a candidate skill's full SKILL.md body for fine-selection.

    Policy runs here too: for an instruction-only skill the body *is* the
    payload, so a blocklisted or low-safety skill must be refused at the
    read, not just at install time (``use_skill``). Under pull discovery
    this tool is the first place a Hub skill's detail metadata — the only
    payload carrying ``score_safety`` — is fetched, which makes it the
    authoritative enforcement point for the remote sources.
    """

    def __init__(
        self,
        client: "SkillHubClient | None" = None,
        registry: "SkillRegistry | None" = None,
        *,
        min_safety: float = 0.7,
        blocklist: "Iterable[str] | None" = None,
    ) -> None:
        self._client = client
        self._registry = registry
        self._policy = SkillPolicy.create(min_safety=min_safety, blocklist=blocklist)

    @property
    def name(self) -> str:
        return "read_skill"

    @property
    def description(self) -> str:
        return (
            "Read a skill's full SKILL.md body — its actual instructions. Pass "
            "the qualified id exactly as shown in the skill menu, a find_skill "
            "result, or the '# Skills' / '# Active Skills' context (e.g. "
            "'local/my-skill', 'hub/my-skill'). This does "
            "NOT download or run anything. Call it for any skill listed by "
            "description alone, whether to judge if it fits or because you have "
            "decided to follow it; a skill whose body is already shown in "
            "context needs no fetch."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "skill_id": {
                    "type": "string",
                    "description": (
                        "The skill's qualified id, with its '<source>/' prefix, "
                        "exactly as the context shows it (e.g. 'local/my-skill', "
                        "'hub/my-skill')."
                    ),
                },
            },
            "required": ["skill_id"],
        }

    async def execute(self, skill_id: Any = None, **_: Any) -> str:
        if not skill_id or not isinstance(skill_id, str):
            return "Error: 'skill_id' is required — a skill's qualified id like 'local/<name>' or 'hub/<slug>'."
        source, native = _split_qualified_id(skill_id)

        if is_blocked(self._policy.blocklist, native):
            return f"Error: skill {native!r} is on the operator blocklist (skillForge.blocklist) and cannot be read."

        if source in ("local", "everos"):
            meta = _lookup_on_disk(self._registry, source, native)
            if meta is None:
                return (
                    f"Error: no {source} skill {native!r} found. Its body may "
                    f"already be present in the '# Skills' context."
                )
            if is_blocked(self._policy.blocklist, meta.name):
                return f"Error: skill {meta.name!r} is on the operator blocklist (skillForge.blocklist) and cannot be read."
            return f"## {meta.name}\n{meta.content}"

        if self._client is None:
            # A bare id (no '<source>/') lands here, so name the local form:
            # this tool now serves on-disk skills too, and a dropped prefix is
            # the likelier mistake than a genuine Hub read without a Hub.
            return (
                f"Error: cannot read {skill_id!r} — Skill Hub is not configured. Only on-disk skills are "
                f"readable here; pass the qualified id with its prefix, e.g. 'local/{native}'."
            )
        try:
            meta = await self._client.get(native)
        except Exception as e:  # noqa: BLE001 — surface as tool error, not a crash
            return f"Error: failed to read skill {skill_id!r} from the Hub: {e}"
        refusal = self._policy.refusal_for_detail(meta, native)
        if refusal is not None:
            return f"Error: refusing to read hub skill: {refusal}."
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
        *,
        min_safety: float = 0.7,
        blocklist: "Iterable[str] | None" = None,
        auto_install: str = "auto",
        install_audit_path: "Path | None" = None,
    ) -> None:
        self._client = client
        self._registry = registry
        self._policy = SkillPolicy.create(
            min_safety=min_safety,
            blocklist=blocklist,
            auto_install=auto_install,
        )
        self._install_audit_path = install_audit_path

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

        if is_blocked(self._policy.blocklist, native):
            return f"Error: skill {native!r} is on the operator blocklist (skillForge.blocklist) and cannot be used."

        if source in ("local", "everos"):
            return self._use_on_disk(source, native)
        if source == "hub":
            return await self._use_hub(native)
        return f"Error: unknown skill source {source!r} in {skill_id!r} (expected one of local/everos/hub)."

    def _use_on_disk(self, source: str, native: str) -> str:
        """Resolve an already-materialized local/everos skill dir."""
        meta = _lookup_on_disk(self._registry, source, native)
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
        """Policy-check, then download + extract a Hub skill.

        The detail metadata is fetched first because it is the only
        payload that carries ``score_safety`` (catalog search omits it);
        on pass it doubles as ``prefetched_meta`` so install() skips the
        redundant round-trip.
        """
        if self._client is None:
            return "Error: Skill Hub is not configured; cannot fetch a remote skill."
        try:
            meta = await self._client.get(native)
        except Exception as e:  # noqa: BLE001 — surface as tool error, not a crash
            return f"Error: failed to install skill {native!r} from the Hub: {e}"

        slug = str(meta.get("slug") or meta.get("name") or native)
        score = meta.get("score_safety")
        refusal = self._policy.refusal_for_detail(meta, native)
        if refusal is not None:
            return f"Error: refusing to install hub skill: {refusal}."

        skip = await self._policy.install_skip_reason(slug)
        if skip is not None:
            logger.info("use_skill install skipped: %s", skip)
            return f"Skill install skipped: {skip}. Use read_skill to view the skill body."

        try:
            info = await self._client.install(native, prefetched_meta=meta)
        except Exception as e:  # noqa: BLE001 — surface as tool error, not a crash
            return f"Error: failed to install skill {native!r} from the Hub: {e}"
        record_install(
            self._install_audit_path,
            slug=str(info.get("slug") or slug),
            version=str(info.get("version") or ""),
            trigger="use_skill",
            score_safety=score,
            skill_dir=info.get("dir"),
        )
        write_install_meta(
            info.get("dir"),
            slug=str(info.get("slug") or slug),
            version=str(info.get("version") or ""),
            trigger="use_skill",
        )
        logger.warning(
            "installed hub skill %s@%s via use_skill (score_safety=%s)",
            info.get("slug") or slug,
            info.get("version") or "",
            score,
        )
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


_LIBRARY_BLURB = (
    "The skill library (SkillHub) holds 110k+ vetted methodology skills across "
    "16 categories: DEV, FRONTEND-UI, DEVOPS-INFRA, TESTING, SECURITY, DATA, "
    "AI-ML, AUTH, DOC-PROC, WRITING, MULTIMEDIA, COMMS, WORKFLOW, PRODUCTIVITY, "
    "META, OTHER."
)


class FindSkillTool(Tool):
    """Search the skill library with an intent-bearing query (pull discovery).

    The counterpart of the scent menu: the menu whispers what might exist,
    this tool is how the model searches on its own terms. The query the model
    writes mid-task carries the task's actual intent, which is exactly what
    the retrieval stack (and the Hub reranker behind it) works best on.
    """

    def __init__(
        self,
        get_router: Callable[[], Any],
        *,
        hub_wired: bool = False,
        min_safety: float = 0.7,
        blocklist: "Iterable[str] | None" = None,
    ) -> None:
        self._get_router = get_router
        self._hub_wired = hub_wired
        self._policy = SkillPolicy.create(min_safety=min_safety, blocklist=blocklist)

    @property
    def name(self) -> str:
        return "find_skill"

    @property
    def description(self) -> str:
        # The blurb must describe what is actually wired: without a Hub
        # endpoint the search covers only the locally installed pool, and
        # promising a 110k-skill library would send the model chasing
        # results that cannot exist on this deployment.
        scope = f"{_LIBRARY_BLURB} " if self._hub_wired else "It searches the locally installed skill pool. "
        return (
            f"Search the skill library for reusable methodology. {scope}"
            "Facing an unfamiliar kind of task, search before improvising -- "
            "there is usually an established approach. Also search when the "
            "same approach has failed twice in a row, or when a task has run "
            "many steps without converging. Write the query from the task's "
            "actual intent (e.g. 'weekly aggregation SQL patterns'), include a "
            "category word to narrow. Returns matching skills as id + "
            "description; read one with read_skill(id)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "What you are trying to do, in retrieval-friendly words.",
                },
            },
            "required": ["query"],
        }

    async def execute(self, query: Any = None, **_: Any) -> str:
        if not query or not isinstance(query, str) or not query.strip():
            return "Error: 'query' is required — describe what you are trying to do."
        router = self._get_router()
        if router is None:
            return "Error: skill retrieval is not available in this deployment."
        try:
            hits = await router.select(query.strip(), history=[], k=8)
        except Exception as exc:  # noqa: BLE001 - a search failure must not kill the turn
            return f"Error: skill search failed ({exc})."
        lines = []
        for h in hits:
            # Same advertising-time screen as the scent menu: blocklisted
            # skills are never named, and a catalog hit already carrying a
            # low score_safety is dropped (the authoritative hub check runs
            # on the detail metadata in read_skill / use_skill).
            meta = getattr(h, "meta", None) or {}
            if is_blocked(self._policy.blocklist, getattr(h, "name", None), meta.get("skill_id"), meta.get("slug")):
                continue
            if refuses_low_safety(meta.get("score_safety"), self._policy.min_safety):
                continue
            desc = ((meta.get("description") or h.content or "").strip().splitlines() or [""])[0][:120]
            lines.append(f"- {h.qualified_id}: {desc}")
        if not lines:
            return "No matching skills found. Try different wording, or proceed without one."
        return "Matching skills (read one with read_skill(id)):\n" + "\n".join(lines)
