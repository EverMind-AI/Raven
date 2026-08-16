"""Segment 5 — ``# Skills``. EverOS: rewriter → router → gate → render.

Five-step pipeline:

1. **Rewriter** (optional) — one LLM call that judges ``need_retrieval``
   and rewrites the query for skill routing. ``need_retrieval=False``
   short-circuits the rest of the build to an empty segment.
2. **Router fan-out** — :class:`SkillForgeRouter` queries Local + Hub +
   Everos in parallel and RRF-fuses to ``pool_size``.
3. **Pre-gate body hydrate** — Hub hits arrive with ``content=""``
   (catalog metadata only). Parallel ``SkillHubClient.get(id)`` fills
   ``content`` so the gate can see real body excerpts. Local/Everos hits
   already carry body and are untouched.
4. **LLM gate** (optional) — one LLM call that picks 0..``max_select``
   hits from the pool. Empty result is a valid "inject nothing".
5. **Post-gate refs hydrate** — for the 0-2 selected hits, resolve
   ``{baseDir}/x`` + markdown link refs to absolute paths. Hub hits
   download + extract the zip first (via ``SkillHubClient.install`` with
   ``prefetched_meta`` from step 3 to skip the redundant ``get``).

When rewriter / gate are not wired (provider missing, config off, etc.)
the pipeline degrades gracefully — both are independent and the segment
still produces a valid result with whatever is wired.
"""

from __future__ import annotations

import asyncio
import logging
from collections import Counter
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from raven.context_engine.base import AssemblyContext, Segment
from raven.context_engine.segments import render
from raven.memory_engine.skill_forge.refs import resolve_refs
from raven.skill_hub.audit import record_install, write_install_meta
from raven.skill_hub.policy import SkillPolicy, is_blocked
from raven.tracing import semconv, trace

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from raven.memory_engine.skill_forge import SkillForgeRouter
    from raven.memory_engine.skill_forge.gate import LLMGateFilter
    from raven.memory_engine.skill_forge.rewriter import QueryRewriter
    from raven.memory_engine.skill_forge.types import RouterHit
    from raven.providers.base import LLMProvider
    from raven.skill_hub import SkillHubClient

log = logging.getLogger(__name__)


class SkillsSegmentBuilder:
    name = "skills"
    order = 5
    needs_prefix = False

    def __init__(
        self,
        router: "SkillForgeRouter | None",
        *,
        skill_top_k: int = 5,
        rewriter: "QueryRewriter | None" = None,
        gate: "LLMGateFilter | None" = None,
        gate_pool_size: int = 10,
        hub_client: "SkillHubClient | None" = None,
        get_tool_definitions: "Any | None" = None,
        min_safety: float = 0.7,
        blocklist: "Iterable[str] | None" = None,
        auto_install: str = "auto",
        install_audit_path: "Path | None" = None,
    ) -> None:
        self._router = router
        self._skill_top_k = skill_top_k
        self._rewriter = rewriter
        self._gate = gate
        # When the gate is active, the router selects ``gate_pool_size``
        # candidates (the gate then trims to ``max_select``). Without the
        # gate, ``skill_top_k`` controls direct injection size.
        self._pool_size = gate_pool_size if gate is not None else skill_top_k
        self._hub_client = hub_client
        self._get_tool_definitions = get_tool_definitions
        self._policy = SkillPolicy.create(
            min_safety=min_safety,
            blocklist=blocklist,
            auto_install=auto_install,
        )
        self._install_audit_path = install_audit_path
        self._audited_installs: set[str] = set()

    def set_provider(self, provider: "LLMProvider", model: str) -> None:
        """Hand a live ``/model`` switch down to the two LLM users in this
        segment. The router itself holds no provider."""
        if self._rewriter is not None:
            self._rewriter.set_provider(provider, model)
        if self._gate is not None:
            self._gate.set_provider(provider, model)

    @trace.instrument("skill.inject", kind="skill", detached=True, extract=semconv.skill_inject_skills)
    async def build(self, ctx: AssemblyContext) -> Segment | None:
        if self._router is None:
            return Segment(
                text="",
                meta={"injected_skill_ids": [], "skill_hits_by_source": {}},
            )

        query = ctx.current_message or ""

        # ── ① Rewriter ────────────────────────────────────────────────
        if self._rewriter is not None and query.strip():
            result = await self._rewriter.analyze(query)
            if not result.need_retrieval:
                return Segment(
                    text="",
                    meta={
                        "injected_skill_ids": [],
                        "skill_hits_by_source": {},
                        "rewriter_skipped": True,
                    },
                )
            if result.rewritten_query:
                query = result.rewritten_query

        # ── ② Router fan-out ─────────────────────────────────────────
        candidates = list(
            await self._router.select(
                query=query,
                history=ctx.session_messages,
                k=self._pool_size,
            )
        )

        # ── ②b Blocklist — hard drop across every source ──────────────
        if self._policy.blocklist:
            kept: list["RouterHit"] = []
            for c in candidates:
                if is_blocked(self._policy.blocklist, c.name, c.meta.get("skill_id")):
                    log.warning("dropping blocklisted skill from pool: %s", c.qualified_id)
                else:
                    kept.append(c)
            candidates = kept

        # ── ③ Pre-gate body hydrate (Hub only) ────────────────────────
        # Side-channel: keep the metadata dict so post-gate install can
        # pass it back into SkillHubClient.install to skip the redundant
        # get() round-trip.
        prefetched_meta: dict[str, dict[str, Any]] = {}
        candidates = await self._hydrate_bodies(candidates, prefetched_meta)

        # ── ④ LLM gate ───────────────────────────────────────────────
        if self._gate is not None and candidates:
            tools = self._collect_tool_names()
            gated = await self._gate.filter(query, candidates, tools)
        else:
            gated = candidates[: self._skill_top_k]

        # ── ⑤ Post-gate refs hydrate ─────────────────────────────────
        gated = await self._hydrate_refs(gated, prefetched_meta)

        # ── ⑥ Render ─────────────────────────────────────────────────
        body = render.render_router_skills(gated)
        meta: dict[str, Any] = {
            "injected_skill_ids": [h.qualified_id for h in gated if getattr(h, "qualified_id", None)],
            "skill_hits_by_source": dict(Counter((h.meta.get("source") or "?") for h in gated)),
        }
        text = f"# Skills\n\n{body}" if body else ""
        return Segment(text=text, meta=meta)

    async def _hydrate_bodies(
        self,
        candidates: list["RouterHit"],
        prefetched_meta: dict[str, dict[str, Any]],
    ) -> list["RouterHit"]:
        """Fill ``content`` for Hub hits via ``SkillHubClient.get`` in
        parallel. Non-Hub hits and Hub hits that already carry content
        (e.g. test stubs that prefilled it) are untouched.

        Stores the returned metadata dict in ``prefetched_meta`` keyed by
        qualified_id so the post-gate install step can reuse it.
        """
        if self._hub_client is None:
            return candidates
        targets = [(i, c) for i, c in enumerate(candidates) if c.meta.get("source") == "hub" and not c.content]
        if not targets:
            return candidates

        async def _one(c: "RouterHit") -> dict[str, Any] | None:
            try:
                return await self._hub_client.get(c.meta["id"])
            except Exception as e:
                # warning, not debug: a failed detail fetch means the
                # candidate cannot be vetted and is dropped below.
                log.warning(
                    "hub body hydrate failed for %s: %s",
                    c.meta.get("id"),
                    e,
                )
                return None

        metas = await asyncio.gather(*(_one(c) for _, c in targets))
        out = list(candidates)
        dropped: set[int] = set()
        for (i, c), m in zip(targets, metas):
            if m is None:
                # Fail closed: the policy runs on the detail metadata, so a
                # candidate whose detail fetch failed is unvetted. Leaving it
                # in the pool would let the post-gate step call install()
                # with prefetched_meta=None, whose internal re-fetch bypasses
                # SkillPolicy entirely.
                log.warning(
                    "dropping hub skill %s: detail fetch failed, cannot vet",
                    c.qualified_id,
                )
                dropped.add(i)
                continue
            # Authoritative policy check: the catalog payload omits
            # score_safety (HubSkillSource's guard no-ops there), so the
            # detail metadata fetched here is the first place the full
            # policy (safety bar + blocklist + body path lint) can run.
            refusal = self._policy.refusal_for_detail(m, c.name)
            if refusal is not None:
                log.warning("dropping hub skill %s: %s", c.qualified_id, refusal)
                dropped.add(i)
                continue
            prefetched_meta[c.qualified_id] = m
            out[i] = replace(c, content=m.get("skill_md", "") or "")
        if dropped:
            out = [c for i, c in enumerate(out) if i not in dropped]
        return out

    async def _hydrate_refs(
        self,
        gated: list["RouterHit"],
        prefetched_meta: dict[str, dict[str, Any]],
    ) -> list["RouterHit"]:
        """For each selected hit, resolve {baseDir} / markdown-link refs
        in the body to absolute paths rooted at the skill's directory.

        - Local: skill_dir already in meta, resolve in-process (no IO).
        - Hub: ``SkillHubClient.install`` to download + extract the zip,
          then resolve under the extracted dir. Cache hit on
          ``<slug>@<version>`` makes repeat installs ~50ms (stat only).
        - Everos: no bundled files; pass through unchanged.
        """
        if not gated:
            return gated

        async def _hydrate_one(h: "RouterHit") -> "RouterHit":
            source = h.meta.get("source")
            if source == "local":
                resolved, _ = resolve_refs(h.content, h.meta.get("skill_dir"))
                return replace(h, content=resolved)
            if source == "hub" and self._hub_client is not None:
                vetted = prefetched_meta.get(h.qualified_id)
                if vetted is None:
                    # Never hand install() a hub hit without vetted detail
                    # metadata — its internal re-fetch would skip SkillPolicy.
                    log.warning(
                        "skipping install for %s: no vetted detail metadata",
                        h.qualified_id,
                    )
                    return h
                skip = self._policy.install_skip_reason(h.name)
                if skip is not None:
                    # Consent skip, not a failure: the body already
                    # hydrated in step ③ still injects — only the bundle
                    # download is withheld.
                    log.info("hub auto-install skipped: %s", skip)
                    return h
                try:
                    installed = await self._hub_client.install(
                        h.meta["id"],
                        prefetched_meta=vetted,
                    )
                except Exception as e:
                    # Install failures fall back to the unresolved body
                    # already hydrated in step ③ — agent loses ref
                    # absolute-path convenience but the skill body itself
                    # still lands in the prompt.
                    log.warning(
                        "hub install for refs hydrate failed (%s): %s",
                        h.meta.get("id"),
                        e,
                    )
                    return h
                self._notify_install(installed, vetted)
                body = installed.get("skill_md", "") or h.content
                resolved, _ = resolve_refs(body, installed.get("dir"))
                new_meta = dict(h.meta)
                new_meta["skill_dir"] = installed.get("dir")
                return replace(h, content=resolved, meta=new_meta)
            # everos / unknown — body already in place, no refs to resolve.
            return h

        return list(await asyncio.gather(*(_hydrate_one(h) for h in gated)))

    def _notify_install(
        self,
        installed: dict[str, Any],
        detail_meta: dict[str, Any] | None,
    ) -> None:
        """Surface + audit an auto-inject install, once per slug@version.

        ``install()`` is called every turn for selected Hub hits (cache
        hits are cheap), so without the dedup a long-lived gateway would
        re-log and re-audit the same bundle each turn.
        """
        slug = str(installed.get("slug") or "?")
        version = str(installed.get("version") or "")
        key = f"{slug}@{version}"
        if key in self._audited_installs:
            return
        self._audited_installs.add(key)
        score = (detail_meta or {}).get("score_safety")
        log.warning(
            "installed hub skill %s (auto-injected into context, score_safety=%s)",
            key,
            score,
        )
        record_install(
            self._install_audit_path,
            slug=slug,
            version=version,
            trigger="auto_inject",
            score_safety=score,
            skill_dir=installed.get("dir"),
        )
        write_install_meta(
            installed.get("dir"),
            slug=slug,
            version=version,
            trigger="auto_inject",
        )

    def _collect_tool_names(self) -> list[str] | None:
        """Return tool names for the gate's hard-constraint block.

        ``get_tool_definitions`` is a callable injected at construction; when
        absent the gate runs without the tool-constraint hint (still
        works, just less aggressive at culling env-mismatched skills).
        """
        if self._get_tool_definitions is None:
            return None
        try:
            defs = self._get_tool_definitions()
        except Exception:
            return None
        names: list[str] = []
        for d in defs or []:
            if isinstance(d, dict):
                # OpenAI function-call schema → name lives under
                # ``function.name``; also accept a flat ``name``.
                fn = d.get("function") if isinstance(d.get("function"), dict) else None
                if fn and isinstance(fn.get("name"), str):
                    names.append(fn["name"])
                elif isinstance(d.get("name"), str):
                    names.append(d["name"])
        return names or None
