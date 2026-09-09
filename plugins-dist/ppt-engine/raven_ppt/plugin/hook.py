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
import os
import uuid
from importlib.resources import files as pkg_files
from pathlib import Path

from raven.agent import workdir
from raven.contracts.loop_hooks import AgentHook, AgentHookContext, HookDecision
from raven.utils.workspace import sync_workspace_templates
from raven_ppt.plugin import ledger, materials
from raven_ppt.services import tier
from raven_ppt.services.publish.deliver import last_refusal, published_digests, published_original

MATERIALS_DIRNAME = "materials"
OUT_DIRNAME = "out"
"""The two directories a deck session carries beside the ppt tools' own ``deck/``.

Named here rather than inline because the prompt text tells the model both
paths and the deck verification reads one of them; a second spelling would
have the agent publish where nothing looks. (The fork named them in its
per-session ACP layer, which does not board; this hook is their home now.)
"""

_METADATA_KEY = "ppt_engine"

# How many times one turn is sent back for ending before the deck was published. Two
# live runs ended with a note the model meant for itself ("append needs real content
# -- re-ingest after...") and with nothing at all; the delegating agent had to spawn
# the run again to continue it. The loop caps hook rollbacks per turn as well.
UNFINISHED_NUDGES = 2
UNFINISHED_NUDGE = (
    "The deck is not published yet: ppt_build has not returned a pptx_path this turn, so ending here "
    "hands the user nothing. Do not end the turn -- continue: build, fix what the build reports, and "
    "publish. If something stops you, say plainly what it is and what you need, and end with that."
)
# And the other way a finished turn hands the user nothing: the deck is published and
# the reply does not say so. On a live run the model's last act after publishing was a
# `cp` of the deck that the exec policy refused, and its reply was the refusal -- "the
# operation was not completed, would you like me to continue?" -- so the delegating
# agent received a question about a deck it was never told existed and the user got
# no file. The path is in the directory; the reply is sent back once to carry it.
DELIVERED_NUDGE = (
    "The deck is already delivered: {paths}. Nothing more needs doing to it, and a command that was "
    "refused after it was published changes nothing. Reply to the user with that path and what the deck "
    "holds -- how many pages and what they argue -- and end there; a question about continuing, or a "
    "note about a blocked command, is not the answer to a finished deck."
)
# And the third: a build the checks refused, copied by the model to a name of its own
# under out/ and named in the reply as the deliverable. Two live runs ended their turn
# on exactly that; the correction the announcement appends reached only the delegating
# agent, which had to adjudicate the turn and start the run again to continue it. The
# reason the build was refused is on disk, so the reply is sent back once with it, to
# the one who can act on it.
COPY_NUDGE = (
    "The reply names {names}, but ppt_build did not publish that file: it is a copy, made outside the "
    "publish step, of a build the checks refused, and it is not the deliverable -- the user would receive a "
    "deck that failed its checks. {reason} Do not end the turn: fix what the build reports, run ppt_build "
    "until it returns a pptx_path, and reply with that path. If a finding cannot be fixed, say plainly which "
    "one and why, and end with that."
)
REFUSED_UNRECORDED = "Run ppt_build again and read its findings: they are what stands in the way."
# What a reply that legitimately ends a turn without a deck says: it asks the user
# something, or it says the work cannot be done. Anything else with no deck behind it
# is a thought that leaked into the answer slot.
_HANDS_BACK = (
    "?",
    "\uff1f",
    "\u65e0\u6cd5",
    "\u4e0d\u80fd",
    "\u5931\u8d25",
    "\u9700\u8981\u4f60",
    "\u9700\u8981\u60a8",
    "\u8bf7\u63d0\u4f9b",
    "\u8bf7\u786e\u8ba4",
    "\u8bf7\u544a\u77e5",
    "cannot",
    "can't",
    "unable",
    "could not",
    "couldn't",
    "blocked",
    "refus",
    "not possible",
    "need you",
    "please provide",
    "please confirm",
)

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


DECKS_DIRNAME = "decks"


def _session_dirname(session_key: str) -> str:
    """A directory name for a session: the part after the channel, kept to safe characters."""
    tail = session_key.rpartition(":")[2] or session_key
    safe = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in tail).strip("._")
    return safe[:80] or "session"


class PptEngineHook(AgentHook):
    """Material staging in, deck verification out, per turn."""

    def __init__(self, home: Path | None = None, *, deck_per_session: bool = True) -> None:
        self._home = home
        self._deck_per_session = deck_per_session
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

    def _ledger_after_compaction(self, ctx: AgentHookContext, root: Path) -> HookDecision:
        """Once per compaction, put the deck ledger under the host's summary.

        The host's summary is written for code work and paraphrases the user;
        a deck's requirements are the user's own words and its state is on
        disk (:mod:`raven_ppt.plugin.ledger`). The note rides ``append_note``,
        so it lands on the last message before the next call, and the marker
        it carries is how the next iteration knows this summary is answered.
        """
        messages = ctx.messages or []
        index = ledger.summary_index(messages)
        if index is None:
            return HookDecision()
        digest = ledger.summary_digest(messages, index)
        if ledger.ledger_stands_for(messages, digest):
            return HookDecision()
        try:
            note = ledger.deck_ledger(root, ctx.session_history, digest)
        except Exception as exc:  # the ledger is help, never a reason to stop a turn
            logger.warning("deck ledger not written after compaction: %s", exc)
            return HookDecision()
        return HookDecision(append_note=note, notes=["deck ledger appended under the compaction summary"])

    @staticmethod
    def _journal_window(root: Path, ctx: AgentHookContext) -> None:
        """Write the user's words the running turn has put in the window so far."""
        messages = ctx.messages or []
        ledger.journal(root, messages=messages[ctx.turn_base :] if messages else None, window=messages or None)

    def _own_folder(self, bound: Path, session_key: str) -> Path:
        """Point the turn at this session's own deck folder, and say where that is.

        The host gives every session on a channel the same directory, and the
        engine fences one deck per directory: a second task in the same channel
        would build on the first task's template, sources and plan. So the turn is
        repointed to a folder of this session's own before anything is staged, the
        way the rebind_workdir grant repoints mid-turn; the enclosing bind still
        resets it at turn end, and the same session's next turn lands in the same
        folder. Idempotent: a turn already pointed there is left alone.
        """
        if not self._deck_per_session:
            return bound
        own = bound / DECKS_DIRNAME / _session_dirname(session_key)
        if bound.name == own.name and bound.parent.name == DECKS_DIRNAME:
            return bound
        own.mkdir(parents=True, exist_ok=True)
        workdir.repoint(own)
        return own

    async def before_iteration(self, ctx: AgentHookContext) -> HookDecision:
        """Point a turn that skipped the inbound phase at the session's deck folder.

        The host runs ``before_user_inbound`` for a user's turn only. A turn a
        sub-agent's late result starts -- the deck agent's own research coming back
        after the deck was delivered -- skips it, so the turn ran where the session's
        directory points, one level above the deck: a live run answered such a return
        with three ``edit_file`` calls on a path that did not exist there and a
        ``ppt_build`` that found no brief, and set out to rebuild the deck from
        nothing. The first iteration is where every turn passes.
        """
        bound = workdir.current()
        if bound is None:
            return HookDecision()
        root = Path(bound)
        if ctx.iteration in (0, 1):
            root = self._own_folder(root, ctx.session_key)
            if ctx.metadata is not None:
                # What the inbound phase would have taken, so a deck this turn publishes is
                # still told apart from an earlier turn's and announced.
                ctx.metadata.setdefault(_METADATA_KEY, {}).setdefault(
                    "deck_mtimes_before", materials.deck_mtimes(root / OUT_DIRNAME)
                )
                # The session's tier, for the deck tools that run outside the hook chain:
                # the mode overlay's deck knobs (services/tier) are written where ppt_build
                # reads them per call, so a tier switched mid-session takes effect on the
                # next build.
                tier.write_mode(root, ctx.metadata.get("mode_overlay"), ctx.metadata.get("mode"))
        # Every iteration, the first included: a first call that overflowed is
        # summarised and retried as iteration 1 with the summary already in the
        # window. The turn's ask_user answers are journaled at after_iteration,
        # because the host compacts at the top of an iteration before this fires.
        self._journal_window(root, ctx)
        return self._ledger_after_compaction(ctx, root)

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
        root = self._own_folder(Path(bound), ctx.session_key)
        # The user's own words, before the staging block is written over the
        # model's view of them: the session record files this turn only after
        # it ends, and a turn long enough to compact needs them before that.
        ledger.journal(root, inbound=text)
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

    async def after_iteration(self, ctx: AgentHookContext) -> HookDecision:
        """Send a turn back when the model stops talking before the deck is published.

        The loop ends a turn on the first reply without a tool call, whatever the
        reply says. A run that had just ingested its sources answered with a note to
        itself and the turn was over, deck unbuilt, until the delegating agent noticed
        and spawned it again. The deck's own state says whether the turn is done -- a
        publish record newer than the turn started -- so this is decided from the
        directory, not from the prose: with no deck and no question to the user in
        the reply, the iteration is rolled back and the model is told to continue.
        Bounded to `UNFINISHED_NUDGES` per turn, under the loop's own rollback cap.
        """
        bound = workdir.current()
        if bound is not None:
            # The tool results of this iteration are in the window now, the ask_user
            # answer among them, and the next iteration compacts before any other
            # phase of this hook fires: an answer not journaled here can be
            # summarised out of the window unseen.
            self._journal_window(Path(bound), ctx)
        response = ctx.response
        if response is None or getattr(response, "tool_calls", None):
            return HookDecision()
        text = str(getattr(response, "content", None) or "").strip()
        if not text:
            # Nothing said at all is the empty-recovery's case, not this one.
            return HookDecision()
        meta = (ctx.metadata or {}).get(_METADATA_KEY) if ctx.metadata is not None else None
        if bound is None or not meta or "deck_mtimes_before" not in meta:
            return HookDecision()
        root = Path(bound)
        if not _deck_started(root):
            return HookDecision()
        published = published_digests(root / "deck" / "state")
        deck, _ = materials.verified_deck(root / OUT_DIRNAME, text, meta["deck_mtimes_before"], published)
        if deck is not None:
            if deck.name in text or meta.get("delivered_nudged"):
                return HookDecision()
            meta["delivered_nudged"] = True
            pdf = deck.with_suffix(".pdf")
            paths = str(deck) + (f" (and its PDF preview {pdf})" if pdf.is_file() else "")
            return HookDecision(
                rollback=True,
                rollback_inject=[{"role": "user", "content": DELIVERED_NUDGE.format(paths=paths)}],
                notes=["ppt_engine: reply ending a turn without naming the deck it published rolled back (1/1)"],
            )
        named = [
            path
            for path in materials.unpublished_decks(root / OUT_DIRNAME, meta["deck_mtimes_before"], published)
            if path.name in text
        ]
        if named and not meta.get("copy_nudged"):
            # Before the hands-back test: "delivered, would you like changes?" names the
            # copy and asks a question in the same breath. Once; a second such reply
            # falls through to the unfinished nudges below.
            meta["copy_nudged"] = True
            nudge = COPY_NUDGE.format(
                names=", ".join(str(path) for path in named), reason=_refused_because(root / "deck" / "state")
            )
            return HookDecision(
                rollback=True,
                rollback_inject=[{"role": "user", "content": nudge}],
                notes=["ppt_engine: reply naming a copy the publish step never wrote rolled back (1/1)"],
            )
        if _hands_back(text):
            return HookDecision()
        nudged = int(meta.get("unfinished_nudges", 0))
        if nudged >= UNFINISHED_NUDGES:
            return HookDecision(notes=[f"ppt_engine: turn ending without a deck after {nudged} nudges; letting it end"])
        meta["unfinished_nudges"] = nudged + 1
        return HookDecision(
            rollback=True,
            rollback_inject=[{"role": "user", "content": UNFINISHED_NUDGE}],
            notes=[f"ppt_engine: reply without a deck or a question rolled back ({nudged + 1}/{UNFINISHED_NUDGES})"],
        )

    async def after_send(self, ctx: AgentHookContext) -> HookDecision:
        bound = workdir.current()
        before = (ctx.metadata or {}).get(_METADATA_KEY, {}).get("deck_mtimes_before")
        if bound is None or before is None:
            return HookDecision()
        out_dir = Path(bound) / OUT_DIRNAME
        reply = ctx.outbound_content or ""
        published = published_digests(Path(bound) / "deck" / "state")
        deck, slides = materials.verified_deck(out_dir, reply, before, published)
        if deck is None:
            copied = materials.unpublished_decks(out_dir, before, published)
            if copied:
                # A deck under out/ that the publish step never wrote: the model copied
                # its build there after a refused build and said it was delivered.
                names = ", ".join(path.name for path in copied)
                return HookDecision(
                    modified_content=reply
                    + (
                        f"\n\nNo deck was published this turn. {names} under {out_dir} was not written by "
                        "ppt_build, so it did not pass the checks and is not the deliverable; the deck is "
                        f"delivered only when ppt_build publishes it. {_refused_because(Path(bound) / 'deck' / 'state')}"
                    )
                )
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
        # delivered by being published. The PDF beside it is the same deck as the
        # page previews it: a .pptx is a download and nothing more on the web surface.
        announced = f"\n\nPublished a {slides}-slide deck.\nDeck: {deck}\nMEDIA: {deck}"
        # Only a preview this publish wrote. `_pdf_beside` returns None when the build
        # could not render one, and the older file it leaves in place is a picture of a
        # deck that no longer exists -- announced as "the same deck", which is worse than
        # no preview. Newer than the deck it stands for is the one test that holds
        # whether the copy happened this turn or the render was skipped.
        preview = deck.with_suffix(".pdf")
        if not _preview_of(deck, preview):
            _preview_beside_copy(Path(bound) / "deck" / "state", deck, preview)
        if _preview_of(deck, preview):
            announced += f"\nPreview (the same deck as a PDF, for viewing): {preview}\nMEDIA: {preview}"
        return HookDecision(modified_content=reply + announced)


def _preview_beside_copy(state_dir: Path, deck: Path, preview: Path) -> None:
    """Give a renamed copy of the published deck the original's PDF, under its own stem.

    The reply named a copy the model made of the published deck -- the digest check
    let it through -- and the PDF the publish wrote sits beside the original, so the
    copy would go out with no preview. Copied fresh rather than with its timestamps,
    because the copy of the deck is newer than the render and `_preview_of` reads the
    timestamps.
    """
    import shutil

    original = published_original(state_dir, deck)
    if original is None:
        return
    rendered = original.with_suffix(".pdf")
    if not _preview_of(original, rendered):
        return
    try:
        temporary = preview.parent / f".{preview.name}.{uuid.uuid4().hex}.tmp"
        shutil.copyfile(rendered, temporary)
        os.replace(temporary, preview)
    except OSError as exc:
        logger.warning("ppt-engine: the preview could not be put beside %s: %s", deck.name, exc)


def _preview_of(deck: Path, preview: Path) -> bool:
    """Whether `preview` is a picture of this very deck rather than an earlier one."""
    try:
        return preview.is_file() and preview.stat().st_mtime_ns >= deck.stat().st_mtime_ns
    except OSError:
        return False


def _refused_because(state_dir: Path) -> str:
    """The last refusal as a sentence for the model, or where to get one."""
    reason = last_refusal(state_dir)
    return f"The last build was refused: {reason}." if reason else REFUSED_UNRECORDED


def _deck_started(root: Path) -> bool:
    """Whether this workdir holds a deck in progress: state or a build script exists."""
    deck = root / "deck"
    return (deck / "state").is_dir() or (deck / "build").is_dir()


def _hands_back(text: str) -> bool:
    """Whether a reply asks the user something or says the work cannot be done."""
    lowered = text.lower()
    return any(mark in lowered for mark in _HANDS_BACK)


__all__ = [
    "COPY_NUDGE",
    "DELIVERED_NUDGE",
    "IDENTITY_SEATS",
    "MATERIALS_DIRNAME",
    "OUT_DIRNAME",
    "UNFINISHED_NUDGE",
    "UNFINISHED_NUDGES",
    "MisconfiguredEngineHook",
    "PptEngineHook",
    "REFUSED_UNRECORDED",
    "seed_identity",
]
