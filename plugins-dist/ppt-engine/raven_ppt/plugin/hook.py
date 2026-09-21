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
from raven.agent.hook.participant import ParticipantHook
from raven.contracts.loop_hooks import AgentHook, AgentHookContext, HookDecision
from raven.contracts.participant import Accept, AgentParticipant, Answer, Intake, Resample, StepView
from raven.utils.workspace import sync_workspace_templates
from raven_ppt.plugin import ledger, materials
from raven_ppt.services import tier
from raven_ppt.services.publish.deliver import (
    delivered_decks,
    last_refusal,
    published_digests,
    unrecorded_deliveries,
)

MATERIALS_DIRNAME = "materials"
OUT_DIRNAME = "out"
"""The two directories a deck session carries beside the ppt tools' own ``deck/``.

Named here rather than inline because the prompt text tells the model both
paths and the deck verification reads one of them; a second spelling would
have the agent publish where nothing looks. (The fork named them in its
per-session ACP layer, which does not board; this hook is their home now.)
"""


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
# The other way a deck under out/ fails to be the one the record names, and the one the
# copy nudge above misdiagnoses. A live run's author, wanting one more change in a deck
# it had already delivered, ran `python3 - # Apply the same fix to the published deck
# (out/deck.pptx) in place` and rewrote the delivery where it lay: the build directory
# and the staged copy both still held the recorded bytes, so nothing had been copied
# anywhere -- the deliverable itself had been edited outside every gate. Told apart from
# a copy because the fix is different: a copy is answered by publishing properly, this by
# putting the edit in the program.
TAMPERED_NUDGE = (
    "The deck at {names} is not the one ppt_build published: its bytes changed after publication, so the "
    "change went through no gate and no reading, and the user is holding a deck nobody checked. Editing "
    "the delivered file is never the way to change a page. Put the change in the program that draws it and "
    "run ppt_build, which republishes and re-checks; then reply with the path it returns."
)
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
        # newline="" for the reason raven-code seeds its guide that way: the
        # default would write a CRLF copy of an LF prompt on Windows, and each
        # seat is asserted equal to the packaged source byte for byte.
        target.write_text((prompts / name).read_text(encoding="utf-8"), encoding="utf-8", newline="")
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


class _DeckEngine:
    """What the ppt engine keeps for the whole process: the identity seat and
    the per-root material bookkeeping. One per plugin instance; every turn's
    participant is built over the same one."""

    def __init__(self, home: Path | None = None, *, deck_per_session: bool = True) -> None:
        self.home = home
        self.deck_per_session = deck_per_session
        self._seeded = False
        self._books: dict[str, tuple[list[tuple[str, Path]], set[str]]] = {}

    def seed_once(self) -> None:
        if self._seeded:
            return
        # First touch, not construction: the factory runs while the host is
        # still assembling, and a seat this engine never serves a turn on is a
        # home it has no business writing into.
        self._seeded = True
        if self.home is not None:
            try:
                if seeded := seed_identity(Path(self.home)):
                    logger.info("ppt-engine: seeded the deck identity into %s: %s", self.home, seeded)
            except OSError as exc:
                # The turn must run either way; a home that cannot be written
                # is loud in the log, and the next process retries because
                # nothing was marked done on disk.
                logger.warning("ppt-engine: seeding the deck identity failed: %s", exc)

    def bookkeeping(self, root: Path) -> tuple[list[tuple[str, Path]], set[str]]:
        key = str(root)
        books = self._books.get(key)
        if books is None:
            books = materials.rehydrate(root / MATERIALS_DIRNAME)
            self._books[key] = books
        return books


class PptParticipant(AgentParticipant):
    """Material staging in, deck verification out, per turn.

    One instance per turn: the marks taken when the turn began and the nudges
    it has already spent are attributes here, not keys in the hook context.
    """

    def __init__(self, engine: _DeckEngine) -> None:
        self._engine = engine
        self._deck_mtimes_before: dict[Path, float] | None = None
        self._build_marks_before: tuple[object, ...] | None = None
        self._deck_stood: bool | None = None
        self._copy_nudged = False
        self._delivered_nudged = False
        self._unfinished_nudges = 0

    def _ledger_after_compaction(self, step: StepView, root: Path) -> str | None:
        """Once per compaction, put the deck ledger under the host's summary.

        The host's summary is written for code work and paraphrases the user;
        a deck's requirements are the user's own words and its state is on
        disk (:mod:`raven_ppt.plugin.ledger`). The note lands on the last
        message before the next call, and the marker it carries is how the
        next iteration knows this summary is answered.
        """
        messages = list(step.transcript)
        index = ledger.summary_index(messages)
        if index is None:
            return None
        digest = ledger.summary_digest(messages, index)
        if ledger.ledger_stands_for(messages, digest):
            return None
        try:
            return ledger.deck_ledger(root, list(step.history), digest)
        except Exception as exc:  # the ledger is help, never a reason to stop a turn
            logger.warning("deck ledger not written after compaction: %s", exc)
            return None

    @staticmethod
    def _journal_window(root: Path, step: StepView) -> None:
        """Write the user's words the running turn has put in the window so far."""
        messages = list(step.transcript)
        ledger.journal(root, messages=messages[step.turn_base :] if messages else None, window=messages or None)

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
        if not self._engine.deck_per_session:
            return bound
        own = bound / DECKS_DIRNAME / _session_dirname(session_key)
        if bound.name == own.name and bound.parent.name == DECKS_DIRNAME:
            return bound
        own.mkdir(parents=True, exist_ok=True)
        workdir.repoint(own)
        return own

    def _take_marks(self, root: Path, *, keep: bool) -> None:
        """The turn-start facts a deck this turn publishes is told apart by.

        ``keep`` leaves marks an earlier phase already took (the iteration
        fallback for a turn that skipped the inbound phase); the inbound phase
        takes them fresh.
        """
        if keep and self._deck_mtimes_before is not None:
            return
        self._deck_mtimes_before = _decks_before(root)
        self._build_marks_before = _build_marks(root)
        # What already stands, so a turn that is not deck work is not told to build
        # one: read from the publish record rather than from the folder, because a
        # copy someone put under out/ is not a deck this project published.
        self._deck_stood = bool(_standing_decks(root))

    async def advise(self, step: StepView) -> str | None:
        """Point a turn that skipped the inbound phase at the session's deck folder,
        journal the window, and answer a compaction with the deck ledger.

        The host runs ``before_user_inbound`` for a user's turn only. A turn a
        sub-agent's late result starts -- the deck agent's own research coming back
        after the deck was delivered -- skips it, so the turn ran where the session's
        directory points, one level above the deck: a live run answered such a return
        with three ``edit_file`` calls on a path that did not exist there and a
        ``ppt_build`` that found no brief, and set out to rebuild the deck from
        nothing. The first iteration is where every turn passes.
        """
        if step.response is not None:
            # Asked before the call and after it; this participant advises before.
            return None
        bound = workdir.current()
        if bound is None:
            return None
        root = Path(bound)
        if step.iteration in (0, 1):
            root = self._own_folder(root, step.session_key)
            self._take_marks(root, keep=True)
            # The session's tier, for the deck tools that run outside the hook chain:
            # the mode overlay's deck knobs (services/tier) are written where ppt_build
            # reads them per call, so a tier switched mid-session takes effect on the
            # next build.
            tier.write_mode(root, step.mode_overlay, step.mode)
        # Every iteration, the first included: a first call that overflowed is
        # summarised and retried as iteration 1 with the summary already in the
        # window. The turn's ask_user answers are journaled at review time,
        # because the host compacts at the top of an iteration before this fires.
        self._journal_window(root, step)
        return self._ledger_after_compaction(step, root)

    async def intake(self, text: str, step: StepView) -> Answer | None:
        self._engine.seed_once()
        bound = workdir.current()
        if bound is None or not text or not text.strip():
            return None
        root = self._own_folder(Path(bound), step.session_key)
        # The user's own words, before the staging block is written over the
        # model's view of them: the session record files this turn only after
        # it ends, and a turn long enough to compact needs them before that.
        ledger.journal(root, inbound=text)
        staged, taken = self._engine.bookkeeping(root)
        try:
            declared = materials.inputs_from_prompt(text)
            wanted = materials.unique_sources(declared + materials.materials_from_prompt(text))
            staged.extend(materials.stage(root / MATERIALS_DIRNAME, materials.unstaged(wanted, staged), taken))
        except materials.StagingError as exc:
            # Fatal to the turn, not skipped, and reported as the turn's reply
            # -- the fork's shape on both of its transports.
            return Intake(text=text, reply=(f"The material could not be staged. {exc}", []))
        # Taken before the turn runs, so what this turn publishes can be told
        # apart from what an earlier turn of the same session left behind.
        self._take_marks(root, keep=False)
        standing = _standing_decks(root)
        return Intake(text=text + materials.describe(staged, root / MATERIALS_DIRNAME, root / OUT_DIRNAME, standing))

    async def review(self, step: StepView) -> Answer:
        """Send a turn back when the model stops talking before the deck is published.

        The loop ends a turn on the first reply without a tool call, whatever the
        reply says. A run that had just ingested its sources answered with a note to
        itself and the turn was over, deck unbuilt, until the delegating agent noticed
        and spawned it again. The deck's own state says whether the turn is done -- a
        publish record newer than the turn started -- so this is decided from the
        directory, not from the prose: with no deck and no question to the user in
        the reply, the iteration is rolled back and the model is told to continue.
        Bounded to `UNFINISHED_NUDGES` per turn, under the loop's own rollback cap.

        A turn that begins with a deck already on the record is not that run. The
        inbound phase tells such a turn its deck stands and hands it no compile
        instruction, but this guard knew only whether *this* turn published, so the
        plain answer to "looks good, thanks" was rolled back twice and told to build
        and publish, and only the cap let the third reply end. The turn-start fact
        rides ``_deck_stood``, and a turn that began with a deck and wrote no build of
        its own ends where it stops: the cap is an escape hatch, not an answer to a
        guard firing on the wrong turn.
        """
        if not step.tools_ran:
            # Asked before the tools run as well; this judgement waits for the
            # iteration to finish.
            return Accept()
        bound = workdir.current()
        if bound is not None:
            # The tool results of this iteration are in the window now, the ask_user
            # answer among them, and the next iteration compacts before any other
            # verb of this participant fires: an answer not journaled here can be
            # summarised out of the window unseen.
            self._journal_window(Path(bound), step)
        response = step.response
        if response is None or getattr(response, "tool_calls", None):
            return Accept()
        text = str(getattr(response, "content", None) or "").strip()
        if not text:
            # Nothing said at all is the empty-recovery's case, not this one.
            return Accept()
        if bound is None or self._deck_mtimes_before is None:
            return Accept()
        root = Path(bound)
        if not _deck_started(root):
            return Accept()
        state = root / "deck" / "state"
        published = published_digests(state)
        delivered = delivered_decks(state)
        deck, _ = materials.verified_deck(root / OUT_DIRNAME, text, self._deck_mtimes_before, published, delivered)
        unpublished = materials.unpublished_decks(root / OUT_DIRNAME, self._deck_mtimes_before, published)
        named = [path for path in unpublished if path.name in text]
        if named and not self._copy_nudged:
            # Before the hands-back test: "delivered, would you like changes?" names the
            # copy and asks a question in the same breath. And before the delivered
            # nudge: a deck published under out/ and then copied by hand to the path the
            # user asked for is a finished deck named by the wrong file, and the answer
            # is the argument that writes it there, not the out/ path. Once; a second
            # such reply falls through to the nudges below.
            self._copy_nudged = True
            listed = ", ".join(str(path) for path in named)
            # A file the publish step's own record names is not a copy of anything --
            # it is the delivery, edited after the fact. Two conditions, two fixes.
            tampered = unrecorded_deliveries(state)
            nudge = (
                TAMPERED_NUDGE.format(names=listed)
                if tampered
                else COPY_NUDGE.format(names=listed, reason=_refused_because(state))
            )
            return Resample(
                "reply names a copy the publish step never wrote",
                inject=[{"role": "user", "content": nudge}],
                note="ppt_engine: reply naming a copy the publish step never wrote rolled back (1/1)",
            )
        if deck is not None:
            if deck.name in text or self._delivered_nudged:
                return Accept()
            self._delivered_nudged = True
            return Resample(
                "reply ends the turn without naming the deck it published",
                inject=[{"role": "user", "content": DELIVERED_NUDGE.format(paths=str(deck))}],
                note="ppt_engine: reply ending a turn without naming the deck it published rolled back (1/1)",
            )
        if _hands_back(text):
            return Accept()
        if self._deck_stood and not unpublished and self._built_nothing(root):
            # The deck the user has was on the record before this turn began, and this
            # turn built nothing for the record to be missing. So there is nothing this
            # reply failed to publish, and the nudge's premise -- ending here hands the
            # user nothing -- is false. It is the same fact the inbound statement turns
            # on, carried here rather than re-read.
            return Accept(
                note="ppt_engine: the deck on the record stands and this turn built none; letting the reply end"
            )
        if self._unfinished_nudges >= UNFINISHED_NUDGES:
            return Accept(
                note=f"ppt_engine: turn ending without a deck after {self._unfinished_nudges} nudges; letting it end"
            )
        self._unfinished_nudges += 1
        return Resample(
            "reply without a deck or a question",
            inject=[{"role": "user", "content": UNFINISHED_NUDGE}],
            note=f"ppt_engine: reply without a deck or a question rolled back ({self._unfinished_nudges}/{UNFINISHED_NUDGES})",
        )

    def _built_nothing(self, root: Path) -> bool:
        """Whether this turn moved none of the marks a build moves.

        A turn whose start was never recorded cannot answer this, and the safe answer
        is no: the guard stays on rather than exempting a turn it cannot vouch for.
        """
        before = self._build_marks_before
        return before is not None and _build_marks(root) == tuple(before)

    async def outbound(self, reply: str, step: StepView) -> str | None:
        bound = workdir.current()
        before = self._deck_mtimes_before
        if bound is None or before is None:
            return None
        out_dir = Path(bound) / OUT_DIRNAME
        state = Path(bound) / "deck" / "state"
        published = published_digests(state)
        delivered = delivered_decks(state)
        deck, slides = materials.verified_deck(out_dir, reply, before, published, delivered)
        if deck is None:
            copied = materials.unpublished_decks(out_dir, before, published)
            if copied:
                names = ", ".join(path.name for path in copied)
                tampered = unrecorded_deliveries(state)
                if tampered:
                    return reply + f"\n\nThe deck under {out_dir} is not the one ppt_build published: " + tampered[0]
                return reply + (
                    f"\n\nNo deck was published this turn. {names} under {out_dir} was not written by "
                    "ppt_build, so it did not pass the checks and is not the deliverable; the deck is "
                    f"delivered only when ppt_build publishes it. {_refused_because(state)}"
                )
            if "MEDIA:" in reply:
                return reply + (
                    f"\n\nNo verifiable deck was published: nothing under {out_dir} opens as a "
                    "presentation carrying slides."
                )
            return None
        # The deck alone. The render under out/ is the engine's own preview, and
        # the web surface renders a deck it is shown by itself; a PDF copied beside
        # the deck and announced as a second MEDIA line was a second deliverable
        # the user never asked for, and the one place left that still wrote one
        # after the build stage stopped.
        return reply + f"\n\nPublished a {slides}-slide deck.\nDeck: {deck}\nMEDIA: {deck}"


def ppt_hook(home: Path | None = None, *, deck_per_session: bool = True) -> ParticipantHook:
    """The engine's seat in the hook chain: one participant per turn over one engine."""
    engine = _DeckEngine(home, deck_per_session=deck_per_session)
    return ParticipantHook("ppt_engine", lambda: PptParticipant(engine))


def _decks_before(root: Path) -> dict[Path, float]:
    """What the turn starts with: every deck under out/ and every delivery on the record."""
    return materials.deck_mtimes(root / OUT_DIRNAME, delivered_decks(root / "deck" / "state"))


def _build_marks(root: Path) -> tuple[object, ...]:
    """What "this turn built something" is decided from, read at the turn's start
    and again at its end.

    Not the decks under ``out/``. A build refused on blocking findings returns
    before it is staged or published, a draft is never published at all, and a
    build whose script failed reaches neither -- all three did the turn's work and
    left ``out/`` exactly as they found it. What a build does move is the
    candidate, the program that draws it, and the failure folder, so those are
    the fact. Sizes and mtimes rather than digests, because this only has to
    differ: a build that rewrote a file with the same bytes still ran.
    """
    deck = root / "deck"
    marks: list[object] = []
    for path in (deck / "build" / "deck.pptx", deck / "build" / "build.py"):
        try:
            stat = path.stat()
        except OSError:
            marks.append(None)
            continue
        marks.append((stat.st_size, stat.st_mtime_ns))
    try:
        marks.append(sum(1 for _ in (deck / "review" / "build_failures").iterdir()))
    except OSError:
        marks.append(0)
    return tuple(marks)


def _standing_decks(root: Path) -> list[Path]:
    """The decks the publish record already holds, read once at the turn's start.

    Both the statement the inbound phase writes and the guard `after_iteration`
    holds the turn to turn on this one fact, so they ask for it in one place: a
    turn told its deck stands and then sent back to publish one is the pair
    disagreeing.
    """
    return materials.published_decks(root / OUT_DIRNAME, published_digests(root / "deck" / "state"))


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
    "TAMPERED_NUDGE",
    "DELIVERED_NUDGE",
    "IDENTITY_SEATS",
    "MATERIALS_DIRNAME",
    "OUT_DIRNAME",
    "UNFINISHED_NUDGE",
    "UNFINISHED_NUDGES",
    "MisconfiguredEngineHook",
    "PptParticipant",
    "ppt_hook",
    "REFUSED_UNRECORDED",
    "seed_identity",
]
