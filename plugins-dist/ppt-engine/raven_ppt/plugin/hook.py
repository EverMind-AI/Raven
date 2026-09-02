"""The deck run's turn frame: material in before the model reads, deck out after.

The fork ran this around its transport (fork ``raven/acp/methods.py``
``_session_prompt`` / ``_stage_for`` / ``_report_deck``); here it is one
contributed hook on the loop's own phases, which fire inside the turn's
workdir bind (ppt verdict, feature 5):

* ``before_user_inbound`` -- stage what the task text names into
  ``<workdir>/materials/``, then rewrite the model's view of the inbound text
  with the staging block. The session record keeps the user's own words; a
  staging failure short-circuits the turn with the fork's own sentence, because
  a deck built from part of its material is wrong in a way nothing downstream
  can see. The turn's before-snapshot of ``<workdir>/out`` rides
  ``ctx.metadata`` to the send fire.
* ``after_send`` -- find the deck this turn actually published (archive opens,
  carries slides, written this turn) and append the fork's three announcement
  lines; a reply that claimed ``MEDIA:`` while nothing verifiable was written
  gets the fork's correction instead. A turn that published nothing and claimed
  nothing passes untouched -- a follow-up that only answered a question did not
  fail.

Staging bookkeeping is per working directory, rehydrated from the copies
themselves on first touch, so a session reopened on an existing directory
neither re-lists nor overwrites what an earlier process staged -- the fork's
``rehydrate`` semantics, keyed the way this host addresses sessions.

Identity delivery (the exec-swap wave, landing the seat pw2a named): the
fork seeded its three drifted identity prompts into every session's workspace
(fork ``acp/engine.py:112-116`` -> ``sync_workspace_templates``), and its
per-session ContextBuilder read them back from there. The trunk host pools
one loop whose bootstrap seats live in the AGENT HOME workspace
(``agent_memory/profile/soul.md`` / ``agent_memory/profile/agent.md`` /
``TOOLS.md`` -- context builder and per-turn segments both read exactly
these), so seeding ``workdir.current()`` would feed a directory nothing
reads. The first ``before_user_inbound`` therefore seeds the HOME the
locator granted this plugin (``ServiceLocator.workspace``): the three
carried prompts from this wheel's package data first, write-if-missing, then
the host's own ``sync_workspace_templates`` for the rest of the template
set, exactly the fork's per-session order collapsed to once per home -- a
later turn, or an operator's in-place edit, is never overwritten.
"""

from __future__ import annotations

import logging
from importlib.resources import files as pkg_files
from pathlib import Path

from raven.agent import workdir
from raven.contracts.loop_hooks import AgentHook, AgentHookContext, HookDecision
from raven.utils.workspace import sync_workspace_templates
from raven_ppt.plugin import materials

MATERIALS_DIRNAME = "materials"
OUT_DIRNAME = "out"
"""The two directories a deck session carries beside the ppt tools' own ``deck/``.

Named here rather than inline because the prompt text tells the model both
paths and the deck verification reads one of them; a second spelling would
have the agent publish where nothing looks. (The fork named them in its
per-session ACP layer, which does not board; this hook is their home now.)
"""

_METADATA_KEY = "ppt_engine"

logger = logging.getLogger(__name__)

#: Where each carried prompt lands in the agent home: the exact seats the
#: host's context builder reads (builder BOOTSTRAP_FILES and the per-turn
#: segment renderer name these three paths and no others).
IDENTITY_SEATS = (
    ("SOUL.md", Path("agent_memory") / "profile" / "soul.md"),
    ("AGENTS.md", Path("agent_memory") / "profile" / "agent.md"),
    ("TOOLS.md", Path("TOOLS.md")),
)


def seed_identity(home: Path) -> list[str]:
    """Seed the deck identity into ``home``, write-if-missing, and report what landed.

    The three drifted prompts first -- so the host's template sync below finds
    them present and leaves them alone -- then the host's own sync for the
    rest of the template set (USER/HEARTBEAT and the L4 stubs), which is the
    fork's per-session ``sync_workspace_templates(session.root)`` collapsed to
    once per home. Write-if-missing on every file: an operator's in-place edit
    outlives every later launch, the fork's own contract.
    """
    seeded: list[str] = []
    prompts = pkg_files("raven_ppt") / "prompts"
    for name, seat in IDENTITY_SEATS:
        target = home / seat
        if target.exists():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((prompts / name).read_text(encoding="utf-8"), encoding="utf-8")
        seeded.append(str(seat))
    seeded.extend(sync_workspace_templates(home, silent=True))
    return seeded


_MALFORMED_SLICE_ERROR = 'the ppt-engine config slice is malformed; fix plugins.config["ppt-engine"]'


class MisconfiguredEngineHook(AgentHook):
    """Fail-closed sentinel cast when the config slice cannot be parsed.

    A raising factory is logged and SKIPPED by the lenient stack builder
    (raven/core/plugin_stack.py), so letting the parse error escape would boot
    a deck product with zero deck tools and no hook under a config that says
    the engine is on -- the silent degradation the code-flow MisconfiguredGate
    doctrine exists to prevent. This sentinel takes the hook's seat instead:
    every turn is answered with the config fix named, and the deploy stays
    loud until someone repairs the slice. Deliberately cast even when
    ``enabled`` was meant to be false: an unparseable slice proves nothing
    about intent, and closed-and-loud beats open-and-quiet. The tool
    factories decline alongside (the code-flow division: the gate seat closes
    and speaks, no tool face is cast).
    """

    def __init__(self, error: str) -> None:
        self._error = error

    @property
    def name(self) -> str:
        return "ppt_engine"

    async def before_user_inbound(self, ctx: AgentHookContext) -> HookDecision:
        return HookDecision(short_circuit_result=(f"{_MALFORMED_SLICE_ERROR}: {self._error}", []))


class PptEngineHook(AgentHook):
    """Material staging in, deck verification out, per turn."""

    def __init__(self, home: Path | None = None) -> None:
        self._home = home
        self._seeded = False
        self._books: dict[str, tuple[list[tuple[str, Path]], set[str]]] = {}

    @property
    def name(self) -> str:
        return "ppt_engine"

    def _bookkeeping(self, root: Path) -> tuple[list[tuple[str, Path]], set[str]]:
        key = str(root)
        books = self._books.get(key)
        if books is None:
            books = materials.rehydrate(root / MATERIALS_DIRNAME)
            self._books[key] = books
        return books

    async def before_user_inbound(self, ctx: AgentHookContext) -> HookDecision:
        if not self._seeded:
            # First touch, not construction: the factory runs while the host
            # is still assembling, and a seat this instance never serves a
            # turn on is a home it has no business writing into.
            self._seeded = True
            if self._home is not None:
                try:
                    if seeded := seed_identity(Path(self._home)):
                        logger.info("ppt-engine: seeded the deck identity into %s: %s", self._home, seeded)
                except OSError as exc:
                    # The turn must run either way; a home that cannot be
                    # written is loud in the log, and the next process
                    # retries because nothing was marked done on disk.
                    logger.warning("ppt-engine: seeding the deck identity failed: %s", exc)
        bound = workdir.current()
        text = ctx.inbound_content
        if bound is None or not text or not text.strip():
            return HookDecision()
        root = Path(bound)
        staged, taken = self._bookkeeping(root)
        try:
            declared = materials.inputs_from_prompt(text)
            wanted = materials.unique_sources(declared + materials.materials_from_prompt(text))
            staged.extend(materials.stage(root / MATERIALS_DIRNAME, materials.unstaged(wanted, staged), taken))
        except materials.StagingError as exc:
            # Fatal to the turn, not skipped, and reported as the turn's reply
            # -- the fork's shape on both of its transports.
            return HookDecision(short_circuit_result=(f"The material could not be staged. {exc}", []))
        # Taken before the turn runs, so what this turn publishes can be told
        # apart from what an earlier turn of the same session left behind.
        ctx.metadata.setdefault(_METADATA_KEY, {})["deck_mtimes_before"] = materials.deck_mtimes(root / OUT_DIRNAME)
        return HookDecision(
            modified_content=text + materials.describe(staged, root / MATERIALS_DIRNAME, root / OUT_DIRNAME)
        )

    async def after_send(self, ctx: AgentHookContext) -> HookDecision:
        bound = workdir.current()
        before = (ctx.metadata or {}).get(_METADATA_KEY, {}).get("deck_mtimes_before")
        if bound is None or before is None:
            return HookDecision()
        out_dir = Path(bound) / OUT_DIRNAME
        reply = ctx.outbound_content or ""
        deck, slides = materials.verified_deck(out_dir, reply, before)
        if deck is None:
            if "MEDIA:" in reply:
                return HookDecision(
                    modified_content=reply
                    + (
                        f"\n\nNo verifiable deck was published: nothing under {out_dir} opens as a "
                        "presentation carrying slides."
                    )
                )
            return HookDecision()
        # The fork's three lines, minus the delivery copy: the working directory
        # IS where the delegating conversation looks, so the deck is already
        # delivered by being published.
        return HookDecision(
            modified_content=reply + f"\n\nPublished a {slides}-slide deck.\nDeck: {deck}\nMEDIA: {deck}"
        )


__all__ = [
    "IDENTITY_SEATS",
    "MATERIALS_DIRNAME",
    "OUT_DIRNAME",
    "MisconfiguredEngineHook",
    "PptEngineHook",
    "seed_identity",
]
