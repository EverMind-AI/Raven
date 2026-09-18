"""The kernel's size budget, pinned to the numbers the L0 booklet froze.

raven/spine is the frozen kernel. Frozen means the promise is not broken while
the implementation may change, and additions are safe -- but only inside a
budget, or "frozen" stops meaning anything as the tree around it grows. The L0
booklet ruled four clauses and measured each true by hand on the two trees it
compared (1,347 and 1,355 lines; this branch measured 1,360 when the guard
landed):

1. the package holds at most LINE_CEILING lines of Python;
2. it imports no other raven package -- every import is its own, the stdlib,
   or an allowed third-party name, in-function imports included;
3. the third-party names are limited to THIRD_PARTY_ALLOWED (tiktoken lives in
   utils, not here);
4. no file carries a TODO, FIXME or HACK marker.

The two numbers were set tight on purpose. Loose (5,000 lines, four third-party
packages) would let the kernel grow to three and a half times its size before
anything went red, which three years on is no guard at all; tight means adding
anything to the kernel passes one explicit review. The booklet also names the
signal that a threshold was set wrong: loose, and the assertion never once
fails; tight, and someone starts bumping the number in passing -- the moment
that happens the guard has lost its authority. So a change to a constant below
is a reviewed change to this file, never a drive-by, and this docstring is
where the reviewer reads why the number is what it is.

Clause 2 overlaps `the kernel stands alone` (import-linter) and
tests/test_kernel_closure.py, which ask about the whole kernel set; this file
asks the narrower question the booklet asked, of spine alone.

One addendum (2026-08-31) extends clause 1's discipline to raven/contracts:
the papers are additions-safe by design, but additions inside a budget --
1,922 lines when the ceiling landed -- so a new paper passes the same
explicit review a kernel line does, in the same file the reviewer already
reads for why the numbers are what they are.

The contracts ceiling moved once, 2,500 -> 2,520 (2026-09-09), and this is
the review. The 578 lines of headroom it landed with were spent, and the
change that found the end of them adds exactly one line to the papers: an
``answered`` flag on ``ApprovalOutcome``, so that a permission request nobody
saw stops being reported to a model as one a human refused. Its prose fits
the docstring that was already there, so the field is the whole cost.

Twenty rather than one. A ceiling a single line under the count is a ceiling
that fails on the next field, which is how a guard turns into a formality
somebody edits on the way past -- the failure mode this docstring names. The
alternative on the table was to shorten an unrelated paper's prose by a line,
which buys the same room while hiding that the papers grew.

It moved again, 2,520 -> 2,730 (2026-09-10), and this is that review. The
change is a whole paper rather than a field: contracts/harness.py, 198 lines,
declaring the four strategy roles the agent loop delegates to (Memory,
Planning, Capability, Action) and the carriers between them. It is the one
addition the four-module seam needs -- the roles were carved out of code the
loop already ran, so no behaviour moves with them -- and no amount of trimming
fits it under the old number: the overage alone is 193 lines against a file of
198, which would leave four Protocol signatures and nothing saying why they
are the four.

The paper is the whole of it. Without it the package stands at 2,515, which is
five lines under the number it has been passing on, so nothing here is a
ceiling absorbing somebody else's growth.

Seventeen lines of headroom, and deliberately not more. The smallest paper in
the package is 37 lines, so the slack cannot absorb a second one: the next
paper trips this gate and lands in this docstring the way this one did, while
a field or a docstring line on an existing paper still fits without a review
nobody would learn anything from.

It moved again, 2,730 -> 2,791, and this is that review. Two additions, both
about a call the loop could not describe once it was over. A failed model call
had no shape anything could carry: the loop knew a provider had raised and the
record kept a turn that simply stopped, so a run read back afterwards could not
say which call failed or what the provider said. ``CallRecord`` is that shape,
50 lines, with the two fields on the provider paper that hand it over. The
second is a single field, ``first_byte_timeout`` on ``GenerationSettings``,
with the prose for why a stream that never started is not a stream that
stopped: 9 lines.

The count is 2,771, and the twenty above it is the same headroom this docstring
argued for the first time, not room set aside to spend.

2,791 -> 2,960 (2026-09-14): the memory seam grew two declared shapes.
``contracts/memory.py`` gained ``health()`` with ``HealthCheck`` / ``BackendHealth``
(what ``raven doctor`` and ``raven import`` read instead of the plugin's
internals), and ``contracts/onboard.py`` is a new paper (``OnboardUI`` /
``OnboardStep`` / ``StepOutcome``, the screen a memory plugin contributes to
``raven onboard``). Measured at 2,938 on top of the ``CallRecord`` round
above; the headroom is the same twenty-odd lines as above, for the same reason.

2,960 -> 2,990 (2026-09-15): two grants the seam round left implicit. A
memory backend gained ``delete()``, which the memory browser needs in order to
stop deleting a derived index row behind a source of truth that keeps the text;
and ``ServiceLocator`` gained the host's embedding block, so the knowledge base
and the memory backend read one endpoint rather than a copy each. Measured at
2,969; the headroom is the same twenty-odd lines, for the same reason.

2,990 -> 3,040 (2026-09-15): ``recall_session`` joins the memory paper,
so the host can read back what a sub-agent left behind through the contract
rather than that backend's HTTP API, and ``store``'s two metadata conventions
are written down where a plugin author reads them. Measured at 3,013.

3,040 -> 3,100 (2026-09-17), and this is that review. No new paper: thirty-
nine lines onto two that were already here, both spent saying where a
dispatch's playbook is read rather than adding anything it can say.

``ActionModule`` gains ``judge`` (28 lines with its prose). The judgement a
playbook carries was read by a module-level helper inside the tool registry,
which put "what the agent does next" in two places -- the role for deciding it
and a private function for vetting it. The method moves the reading to the
role and leaves the refusing where it was: ``ToolRegistry.execute`` still
decides what a refusal does, so a replaced role withholds nothing it could not
already withhold. The prose is most of the 28: two callers and two moments is
the part a reader gets wrong.

``TurnContext`` and ``AssemblyContext`` gain ``task_brief`` and
``task_done_when``, 11 lines. The identity segment used to reach into the
dispatch layer's ContextVar to find out what this turn was asked to do, which
decided "what does this turn show its model" somewhere the Memory role could
not see it. Memory fills the two strings now and the segment renders them.

Measured at 3,072, and the twenty-eight above it is the headroom this
docstring has argued for since the first bump: a ceiling the next field trips
is a ceiling somebody edits on the way past.

3,100 -> 3,200 (2026-09-17), and this is that review. The Memory role gains
its mid-turn seat: ``shrink``, with ``WindowPressure`` (the five reasons a
window is asked to get smaller), ``WindowState`` (one turn's readings and
retry budgets, held by the shell because the role outlives the turn) and
``ShrinkResult``, 84 lines of which the prose is most. The five recoveries the
loop ran inline -- proactive compaction, the standing image window, overflow,
a picture refused in a tool result, pictures refused for size -- now go
through this one method, so the policy half moved onto the role while the
retry mechanism stayed in the shell; the six ``continue`` statements did not
move. ``REASONING_EFFORT_LADDER`` (11 lines with its prose) also lands in
``llm_provider.py``: the loop's empty-response descent and the window's head
summary both read it, and neither package may import the other.

Measured at 3,170, thirty over the count rather than one, for the reason the
first bump gave.

3,200 -> 3,410 (2026-09-18), and this is that review. The change is a whole
paper: contracts/participant.py, 191 lines, the nine verbs a sub-agent
implements instead of the six hook phases (``AgentParticipant``, its ``StepView``
of fourteen read-only fields, the ``Intake`` and ``Verdict`` it answers with,
the per-turn ``ParticipantFactory``, and a diagnostic trail so a gate's one-line
findings still reach the loop's notes). It sits beside ``loop_hooks`` rather
than replacing it: the phases remain the loop's timing contract, this is the
judgement contract, and an adapter seats one in the other. Surveyed before it
was written -- 42 real hook implementations across five plugins read fourteen
context fields and used six decision fields, and every one of them fits one of
the verbs -- so the paper is sized to what exists, not to what might.
Factory-loop tier, so the contract-tier digest and ``CONTRACTS_VERSION`` do
not move with it.

Measured at 3,379, thirty-one over the count, for the reason given above.

3,410 -> 3,440 (2026-09-18), when the participants were seated on the roles:
``MemoryModule.intake``, ``PlanningModule.advise`` and ``ActionModule.review``
/ ``salvage`` -- the four verbs a participant answers that belong to a role rather
than to the seat, each taking this turn's participants so a replaced role decides
what a plugin's judgement does. 28 lines of protocol and prose; the
composition rules themselves live in the harness, not here.

Measured at 3,406, thirty-four over the count, for the reason given above.

And once more, 3,440 -> 3,470, for the closeout of the participant contract
(2026-09-18). Four changes, all of them narrowing what the paper claims rather
than widening it: ``observe`` is gone, because a verb with no return value
decides nothing and can only keep the state a participant is forbidden to
keep; ``outbound`` says on its own docstring that it is the one verb with no
module seat and therefore the one a generated participant cannot be handed;
``StepView`` gains ``phase``, so a verb asked at two moments reads which one it
is instead of inferring it from whichever other field happens to be set, and
``tools_ran`` becomes a property derived from it so the two cannot disagree;
and the four delegating role methods are named apart from the verbs they ask
(``read_inbound`` asks ``intake``, ``guide`` asks ``advise``, ``judge_step``
asks ``review``, ``rescue`` asks ``salvage``), so a reader of either name knows
which layer they are on.

Measured at 3,444. The headroom is the twenty-odd this docstring has argued
for since the first bump, not room set aside to spend.


And once more, 3,470 -> 3,500 (2026-09-18), for the three verbs the
seam applied without a seat. ``system_addendum``, ``archive`` and
``select_tools`` were rendered straight onto the hook decision, so each was the
one judgement in the set with nowhere to compose two participants, nowhere to
vet what came back, and nothing a replacement role could decide. They join the
roles the other four sit on: ``MemoryModule.ask_system_addendum`` and
``ask_archive``, and ``CapabilityModule.ask_select_tools``. The tool seat is
deliberately not the point where narrowing takes effect -- ``select`` still is, and the
registry still adjudicates every call -- so a participant narrows after the
product has spoken and never instead of it. 47 lines of protocol and prose.

Measured at 3,477.


And once more, 3,500 -> 3,540 (2026-09-18), for the verb that lets a
dispatch's own judgements be a participant rather than something a role reads
for itself. ``AgentParticipant.judge`` is the one synchronous verb -- it runs before
every tool dispatch and ahead of the permission gate -- and it was already pure
data, a list of sentences. Adding it is what makes the participant list the only
route an external judgement takes: a Charter's ``checks`` and ``code`` answer it
through a thin shell, and a judgement generated for one dispatch can join the
same list without a second way in. ``ActionModule.judge`` takes the list; the
merge is a veto, so the first participant that refuses decides and the rest are
not asked. 30 lines of protocol and prose.

Measured at 3,515.

"""

from __future__ import annotations

import ast
import re
import sys
from pathlib import Path

LINE_CEILING = 2_000
CONTRACTS_LINE_CEILING = 3_540
THIRD_PARTY_ALLOWED = frozenset({"loguru"})
DEBT_MARKER = re.compile(r"\b(TODO|FIXME|HACK)\b")

REPO = Path(__file__).resolve().parent.parent
KERNEL_PACKAGE = "raven.spine"
SPINE = REPO / "raven" / "spine"
CONTRACTS = REPO / "raven" / "contracts"


def _spine_files() -> list[Path]:
    files = sorted(p for p in SPINE.rglob("*.py") if "__pycache__" not in p.parts)
    assert files, "raven/spine has no Python files; a budget measured on nothing is met by nothing"
    return files


def _imports() -> list[tuple[str, str]]:
    """Every import in the package as (module, where), in-function ones
    included; a relative import counts as the package itself."""
    found = []
    for p in _spine_files():
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""] if node.level == 0 else [KERNEL_PACKAGE]
            else:
                continue
            found += [(m, f"{p.relative_to(REPO)}:{node.lineno} -> {m}") for m in mods]
    assert found, "raven/spine imports nothing; the walk found no statements to judge"
    return found


def test_the_kernel_stays_under_its_line_ceiling() -> None:
    lines = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in _spine_files())

    assert 0 < lines <= LINE_CEILING, (
        f"raven/spine is {lines} lines against a ceiling of {LINE_CEILING}; "
        "growing the kernel is a reviewed change to this ceiling, not a bump in passing"
    )


def test_the_kernel_imports_no_other_raven_package() -> None:
    strays = [
        where
        for mod, where in _imports()
        if mod.split(".")[0] == "raven" and mod != KERNEL_PACKAGE and not mod.startswith(KERNEL_PACKAGE + ".")
    ]

    assert strays == [], f"the kernel reaches outside itself: {strays}"


def test_the_kernel_names_no_third_party_beyond_the_allowed() -> None:
    beyond = sorted(
        where
        for mod, where in _imports()
        if (root := mod.split(".")[0]) not in sys.stdlib_module_names
        and root != "raven"
        and root not in THIRD_PARTY_ALLOWED
    )

    assert beyond == [], f"the kernel took on a dependency outside {sorted(THIRD_PARTY_ALLOWED)}: {beyond}"


def test_the_kernel_carries_no_debt_markers() -> None:
    marked = [
        f"{p.relative_to(REPO)}:{n}"
        for p in _spine_files()
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
        if DEBT_MARKER.search(line)
    ]

    assert marked == [], f"the frozen kernel carries deferred work: {marked}"


def test_the_papers_stay_under_their_line_ceiling() -> None:
    files = sorted(p for p in CONTRACTS.rglob("*.py") if "__pycache__" not in p.parts)
    assert files, "raven/contracts has no Python files; a budget measured on nothing is met by nothing"
    lines = sum(len(p.read_text(encoding="utf-8").splitlines()) for p in files)

    assert 0 < lines <= CONTRACTS_LINE_CEILING, (
        f"raven/contracts is {lines} lines against a ceiling of {CONTRACTS_LINE_CEILING}; "
        "a new paper is a reviewed change to this ceiling, not a bump in passing"
    )
