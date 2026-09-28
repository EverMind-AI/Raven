"""The agency: a model plays the owner who cultivates the digital employee through the Curator.

Each round the owner plays customers from its drill cards, then steps out of the role to judge the drills and
speaks to the Curator: what went wrong, what matters most, what came back after it was fixed, and which
materials it now hands over. It reads what the Curator said its last revision changed before it speaks again.

What the owner knows is fixed: all of the scenario's materials. What varies is when each reaches the employee: all at
onboarding, as the owner decides after each round, or on a fixed partition of the materials into steps. Whatever the
plan, everything has been handed over by the review before the last round, so the last round tries an employee that
was told everything.

The owner reviews each drill against the card it played this round, with figures computed from its own materials for
that card and facts read from the delivered deck's structure (`references`); it still decides every verdict itself.

Who turns the owner's review into the Curator's requirements is `analysis`:

- `analyst` (the default): the owner only speaks, the way a real one would: a remark in its own words and the
  materials it hands over. The base Analyst (`experimental.analyst.run.analyse`) reads those words against the sessions
  and the execution records, with exactly the materials it reads for any evaluator, and writes the requirements. The
  owner still keeps a scorecard, one verdict per criterion, but only the value judge reads it (from the analysis
  record): the criteria, the cards and the references never reach the Analyst or the Curator.
- `owner`: the agency is this scenario's Analyst (`Agency.review`): the standards come from the owner, so the owner's
  review is the round's analysis. For every failed criterion it says whether what it already handed over did not take
  hold (a requirement for the Curator) or the shortfall only waits on material it still holds back (handed over
  instead).
"""

import base64
import json
import re
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from ..analyst import role
from ..analyst.activity import activity
from ..analyst.feedback import Feedback
from ..analyst.run import Limits, analyse
from ..curator.generation.context.render import tool
from ..curator.harness.declaration import schema_for
from ..iteration.exchange import exchange, messages
from ..iteration.protocols import Item, Signal
from ..requirements import Requirement
from .files import page_images, read_delivered
from .reference import Rules, references
from .scenario import Scenario
from .traveller import transcript

NAME = "submit_review"
DELIVERABLE_LIMIT = 120_000
RESEARCH_LIMIT = 40_000
FILED_LIMIT = 20_000
TASK_LIMIT = 1_500
PAGES = 24
REPORT = re.compile(
    r"\[BEGIN UNTRUSTED subagent #(?P<tag>\w+)[^\]]*\]\n?(?P<body>.*?)\n?\[END UNTRUSTED subagent #(?P=tag)\]", re.S
)
# Other marked blocks (a skill catalog, say) may quote the start of a report's marker.
OTHER_BLOCK = re.compile(
    r"\[BEGIN UNTRUSTED (?!subagent )[^\]#]*#(?P<tag>\w+)[^\]]*\].*?\[END UNTRUSTED [^\]#]*#(?P=tag)\]", re.S
)
TASK_LABEL, ANSWER_LABEL = "The task the sub-agent was given:", "What it returned as its answer:"
TAIL_LABEL = "Tail of what the sub-agent did"
LOOKS = frozenset({"read_file", "list_dir", "grep", "find", "web_search", "web_fetch", "load_skill", "read_skill"})
TEXT_FILES = frozenset({".md", ".txt", ".csv", ".json", ".yaml", ".yml"})
PRIVATE = frozenset({"sessions", "subagents", "uploads", "skills", "memory", "agent_memory", "user_memory", "decks"})
SOURCE = "agency"
REFERENCES = "references.jsonl"
Plan = Literal["all", "staged"] | tuple[tuple[str, ...], ...]
Delivery = Literal["pool", "dialog"]
Analysis = Literal["owner", "analyst"]
_PROMPT = Path(__file__).resolve().parent / "prompts" / "agency.md"
_OWNER = Path(__file__).resolve().parent / "prompts" / "owner.md"


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    result: Literal["pass", "fail", "unknown"]
    session: str | None = None
    actual: str = ""
    note: str = ""


class Shortfall(BaseModel):
    """The behavior behind failed criteria, and whether the employee already had what it needed for it."""

    model_config = ConfigDict(extra="forbid")

    criteria: list[str] = Field(min_length=1, description="Ids of the failed criteria this behavior is behind.")
    cause: Literal["not_held", "material_missing"] = Field(
        description="not_held: what you already handed over or told the trainer covers it and it still went wrong; "
        "material_missing: it is covered only by a material you still hold back."
    )
    material: str | None = Field(default=None, description="For material_missing: the withheld material covering it.")
    behavior: str = Field(min_length=1, description="The situation and what you expect, as a rule for any customer.")
    observed: str = Field(min_length=1, description="What the employee did instead, naming the drill.")
    evidence: list[str] = Field(min_length=1, description="The employee's words or files that show it, with the drill.")
    acceptance: str = Field(min_length=1, description="One statement the next drills can confirm or refute.")
    strength: Literal["must_hold", "should"] = Field(
        description="must_hold when your materials state it as holding every time; should when a miss is tolerable."
    )


class Review(BaseModel):
    """`handover` names the withheld materials the owner decides to give the employee after this round."""

    model_config = ConfigDict(extra="forbid")

    verdicts: list[Verdict] = Field(min_length=1)
    shortfalls: list[Shortfall] = Field(default_factory=list)
    remark: str = Field(min_length=1)
    handover: list[str] = Field(default_factory=list)


class Mark(Verdict):
    """A verdict on the owner's own scorecard, which only the value judge reads."""

    waits_on: str | None = Field(
        default=None, description="For a fail covered only by a material you still hold back: that material."
    )


class Spoken(BaseModel):
    """What the owner submits when an Analyst reads its words: its scorecard, its remark and its handover."""

    model_config = ConfigDict(extra="forbid")

    verdicts: list[Mark] = Field(min_length=1)
    remark: str = Field(min_length=1)
    handover: list[str] = Field(default_factory=list)


def waiting(review: Review | Spoken) -> dict[str, str]:
    """Each failed criterion the owner said only waits on a material it still holds back, with that material."""
    if isinstance(review, Spoken):
        return {verdict.id: verdict.waits_on for verdict in review.verdicts if verdict.waits_on}
    return {
        criterion: shortfall.material
        for shortfall in review.shortfalls
        if shortfall.cause == "material_missing" and shortfall.material
        for criterion in shortfall.criteria
    }


def cut(text: str, limit: int) -> str:
    """`text` up to `limit` characters; a longer one says how much was left out, so the owner never takes a cut
    document for the whole of it."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n[... {len(text) - limit} more characters not shown]"


def deliverables(exchanges) -> dict[str, str]:
    """The last version of each file a conversation handed over, a deck read slide by slide with its styling."""
    files = {}
    for turn in exchanges:
        for path in turn.execution.deliverables:
            if Path(path).is_file():
                files[Path(path).name] = cut(read_delivered(path, style=True), DELIVERABLE_LIMIT)
    return files


def looks(sessions) -> tuple[dict, list[dict]]:
    """Per drill, the page count of each deck it delivered (last version), and those pages as image message parts.

    The owner flips through the rendered pages the way a customer would; a deck that cannot be rendered says so.
    """
    shown, parts = {}, []
    for name, exchanges in sessions.items():
        decks = {
            Path(path).name: Path(path)
            for turn in exchanges
            for path in turn.execution.deliverables
            if Path(path).suffix.lower() == ".pptx" and Path(path).is_file()
        }
        for deck, path in decks.items():
            try:
                images = page_images(path)[:PAGES]
            except Exception as exc:  # noqa: BLE001 -- a deck that cannot be rendered is reported, not fatal
                shown.setdefault(name, {})[deck] = f"could not be rendered: {exc}"[:200]
                continue
            shown.setdefault(name, {})[deck] = len(images)
            parts.append({"type": "text", "text": f"[{name}] {deck}"})
            for number, image in enumerate(images, start=1):
                url = "data:image/png;base64," + base64.b64encode(image.read_bytes()).decode()
                parts += [{"type": "text", "text": f"page {number}"}, {"type": "image_url", "image_url": {"url": url}}]
    return shown, parts


def research(exchanges) -> dict[str, str]:
    """What the research colleague reported in each playbook run of a conversation, read from the run manifest."""
    found = {}
    for turn in exchanges:
        for row in turn.execution.records:
            if row["kind"] != "dag.progress" or row.get("name") != "dag_run_completed":
                continue
            for node in ((row.get("payload") or {}).get("manifest") or {}).get("files", []):
                path = Path(str(node.get("output_file") or ""))
                if "Research" in str(node.get("subagent")) and path.is_file():
                    found[str(node["node"])] = cut(path.read_text(errors="replace"), RESEARCH_LIMIT)
    return found


def partition(steps, materials, rounds: int | None = None) -> tuple[tuple[str, ...], ...]:
    """Check that `steps` split every material into disjoint, non-empty-at-onboarding steps that fit before the last round."""
    steps = tuple(tuple(step) for step in steps)
    named = [name for step in steps for name in step]
    if not steps or not steps[0]:
        raise ValueError("a partition gives something at onboarding")
    if sorted(named) != sorted(set(named)) or set(named) != set(materials):
        raise ValueError(f"a partition must give every material exactly once; materials: {sorted(materials)}")
    if rounds is not None and len(steps) > rounds:
        raise ValueError(f"a partition of {len(steps)} steps does not finish before the last of {rounds} rounds")
    return steps


def revision(plan) -> dict | None:
    """The Curator's last revision as its reply to the owner: what it understood, in the owner's own terms.

    The plan's changes name harness targets and mechanisms, which the owner knows nothing about, so they stay out.
    """
    if plan is None:
        return None
    return {"understanding": plan.understanding}


def reports(exchanges) -> list[dict]:
    """Every report a colleague (a subagent or a playbook step) sent back to the employee, with the task it had.

    Read from the model requests of the employee's turns, where each reaches it in marked blocks: a delegation's
    result names the task it was given, then what it returned as its answer (the tail of what it did is left out);
    a playbook step's account follows the note that names the step. Whatever the colleague was asked to do, this is
    what the employee had in hand. The same report repeats in every later request, so each is kept once.
    """
    found = {}
    for turn in exchanges:
        for row in turn.execution.records:
            if row.get("kind") != "provider.request":
                continue
            for message in (row.get("parameters") or {}).get("messages") or []:
                content = message.get("content")
                if not isinstance(content, str) or "[BEGIN UNTRUSTED subagent" not in content:
                    continue
                content, task, start = OTHER_BLOCK.sub("", content), "", 0
                for match in REPORT.finditer(content):
                    lead = content[start : match.start()].strip()
                    start = match.end()
                    label = lead.splitlines()[-1] if lead else ""
                    body = match.group("body").strip()
                    if TASK_LABEL in label:
                        task = body
                        continue
                    if label.startswith(TAIL_LABEL):
                        continue
                    asked = task if ANSWER_LABEL in label else lead.split("\n\n")[-1]
                    asked = asked.split("Working directory:", 1)[0].strip()
                    asked = "" if asked.startswith("[Runtime Context") else asked
                    if len(asked) > TASK_LIMIT:
                        asked = asked[: TASK_LIMIT * 2 // 3] + " [...] " + asked[-TASK_LIMIT // 3 :]
                    report = cut(body, RESEARCH_LIMIT)
                    found[report] = found.get(report) or asked
    return [{"task": task, "report": report} for report, task in found.items()]


def written(folder: Path, before: Path, *, label: str, skip=frozenset()) -> dict[str, str]:
    """Files under `folder` that are new or changed against `before`, keyed `label/relative path`.

    Text files come with their content; anything else (decks, scripts, build output) is listed by name only. Hidden
    entries are the host's and tools' own bookkeeping (Raven's `.raven` checkpoints, a deck engine's dot files), not
    something the employee filed.
    """
    files = {}
    if not Path(folder).is_dir():
        return files
    for path in sorted(Path(folder).rglob("*")):
        relative = path.relative_to(folder)
        if (
            not path.is_file()
            or skip & set(relative.parts[:1])
            or "__pycache__" in relative.parts
            or any(part.startswith(".") for part in relative.parts)
        ):
            continue
        old = Path(before) / relative
        if old.is_file() and old.read_bytes() == path.read_bytes():
            continue
        key = f"{label}/{relative.as_posix()}"
        if path.suffix.lower() in TEXT_FILES and not relative.parts[0] == "decks":
            files[key] = cut(path.read_text(errors="replace"), FILED_LIMIT)
        else:
            files[key] = f"[{path.suffix or 'file'}; content not shown]"
    return files


def back_office(exchanges) -> list[dict]:
    """What the employee did besides talking, turn by turn and in order: files written, commands, playbooks,
    delegations and deliveries, each with whether it succeeded (`ok`, with the tool's error when it failed), so
    the owner can see what happened before what and never takes a failed write for a filed ticket."""
    rows = []
    for index, turn in enumerate(exchanges, start=1):
        started, finished = [], {}
        for row in turn.execution.records:
            event = row.get("event") or {}
            if row.get("kind") != "runner.event" or row.get("event_type", "ToolEvent") != "ToolEvent":
                continue
            if event.get("phase") == "start" and event.get("name") and event["name"] not in LOOKS:
                started.append(event)
            elif event.get("phase") == "complete" and event.get("tool_call_id"):
                finished[event["tool_call_id"]] = event
        for event in started:
            arguments = event.get("arguments") or {}
            target = (
                arguments.get("path")
                or arguments.get("node_id")
                or arguments.get("name")
                or arguments.get("agent")
                or str(arguments.get("command") or "")[:120]
                or [Path(str(item.get("path") or "")).name for item in arguments.get("files") or []]
            )
            done = finished.get(event.get("tool_call_id"))
            outcome = {"ok": None} if done is None else {"ok": done.get("ok") is not False}
            if done is not None and done.get("ok") is False:
                outcome["error"] = str(done.get("result_preview") or "")[:300]
            rows.append({"turn": index, "action": event["name"], "target": target, **outcome})
    return rows


def attached(paths) -> tuple[str, ...]:
    """Uploaded files as a signal's attachments: relative to the agent home, `uploads/<material>/<file>`."""
    return tuple(f"uploads/{Path(path).parent.name}/{Path(path).name}" for path in paths)


class Agency(role.Analyst):
    """`prepare` puts the plan's opening materials in place; `evaluate` judges a round and releases what the owner hands over.

    `review` turns that judgement into the round's feedback, the way `analysis` says (see the module docstring); with
    `analyst` the base Analyst runs on `analyst_model` within `analyst_limits`.

    `reply` returns the Curator's latest plan (or None); the owner reads it as the Curator's answer to its last remark.
    `cards` returns each drill's current playing of its card (see `Traveller.card`), keyed by drill name; the owner
    reads it and the references take their trip facts from it. With `records`, each judged drill's card and references
    are appended to `references.jsonl` there. `workdirs` maps each drill to the `workdir` and `home` of the replica it
    played on (see `experimental.simulation.employee.Together`); `workdir` is the employee's own, where every replica
    started.
    `deliver` says how materials reach the employee: `dialog` hands them to the Curator the way an owner would, the
    opening materials uploaded to the employee's `uploads` folder with an onboarding message (`opening`), later ones
    uploaded the same way and also pasted into the remark; `pool` copies them straight into its skill pool, bypassing
    the Curator.
    `plan` says when materials are given: `all` at onboarding; `staged` gives the scenario's initial set, then what the
    owner chooses after each round; a partition (a tuple of disjoint steps covering every material) gives step 0 at
    onboarding and step k with the review of round k. With `rounds`, whatever is still withheld is handed over with
    the review before the last round.
    """

    def __init__(
        self,
        scenario: Scenario,
        provider,
        skills: Path,
        *,
        workdir: Path,
        plan: Plan = "staged",
        deliver: Delivery = "dialog",
        rounds: int | None = None,
        uploads: Path | None = None,
        shared: Path | None = None,
        reply=None,
        cards=None,
        records: Path | None = None,
        workdirs: dict | None = None,
        analysis: Analysis = "analyst",
        analyst_model=None,
        analyst_limits: Limits = Limits(),
        model=None,
        max_calls=4,
        timeout=180,
    ):
        if isinstance(plan, tuple):
            partition(plan, scenario.materials, rounds)
        elif plan not in ("all", "staged"):
            raise ValueError(f"unknown disclosure plan: {plan}")
        if deliver not in ("pool", "dialog") or (deliver == "dialog" and uploads is None):
            raise ValueError(f"unknown delivery {deliver!r}, or a dialog delivery without an uploads folder")
        if analysis not in ("owner", "analyst"):
            raise ValueError(f"unknown analysis {analysis!r}")
        super().__init__((), provider, model=model)
        self.scenario, self.provider, self.skills, self.workdir = scenario, provider, Path(skills), Path(workdir)
        self.deliver, self.uploads, self.uploaded = deliver, Path(uploads) if uploads else None, []
        self.shared = Path(shared) if shared else None
        self.plan, self.model, self.max_calls, self.timeout = plan, model, max_calls, timeout
        self.rounds = rounds
        self.analysis, self.analyst_model, self.analyst_limits = analysis, analyst_model, analyst_limits
        self.reply = reply or (lambda: None)
        self.cards = cards or dict
        self.records = Path(records) if records else None
        self.workdirs = workdirs if workdirs is not None else {}
        self.rules = Rules.load(scenario)
        self.released: list[str] = []
        self.reviews: list[dict] = []
        self.judged = 0

    def _reply(self) -> dict | None:
        """The Curator's reply, marked `is_new` false when no revision came since the owner last read it."""
        reply = revision(self.reply())
        if reply is None:
            return None
        shown, self._shown = getattr(self, "_shown", None), reply["understanding"]
        return {**reply, "is_new": reply["understanding"] != shown}

    def _filed(self, name) -> dict[str, str]:
        """What a drill left for colleagues: the files new or changed in its replica's workdir and home.

        A replica starts from the employee's own workdir and home, so comparing against them finds tickets however
        they were written, moved or renamed. The deck engine's `decks` folder is its build output: a delivered deck
        reaches the owner as a deliverable. A drill that played on no replica has nothing to compare, so it filed
        nothing here.
        """
        placed = self.workdirs.get(name)
        if placed is None:
            return {}
        home = self.uploads.parent if self.uploads else None
        return {
            **written(placed["workdir"], self.workdir, label="workdir", skip=frozenset({"uploads", "decks"})),
            **(written(placed["home"], home, label="home", skip=PRIVATE) if home else {}),
        }

    @property
    def chooses(self) -> bool:
        """Whether the owner decides what to hand over; on a fixed partition the plan decides."""
        return self.plan == "staged"

    def due(self, review: int) -> list[str]:
        """What the plan hands over with the review of round `review`, whatever the owner chooses."""
        if isinstance(self.plan, tuple):
            due = list(self.plan[review]) if review < len(self.plan) else []
        else:
            due = []
        if self.rounds is not None and review >= self.rounds - 1:
            due += [name for name in self.withheld if name not in due]
        return [name for name in due if name not in self.released]

    @property
    def withheld(self) -> list[str]:
        return [name for name in self.scenario.materials if name not in self.released]

    def prepare(self) -> None:
        self.scenario.withdraw(self.skills)
        if self.uploads:
            self.scenario.withdraw_uploads(self.uploads, *([self.shared] if self.shared else []))
        self.released, self.reviews, self.uploaded, self.judged = [], [], [], 0
        if isinstance(self.plan, tuple):
            opening = list(self.plan[0])
        else:
            opening = list(self.scenario.materials if self.plan == "all" else self.scenario.initial)
        if self.deliver == "dialog":
            self.uploaded = self.scenario.upload(opening, self.uploads, shared=self.shared)
            self.released.extend(opening)
        else:
            self._release(opening)

    def opening(self) -> tuple[Signal, ...]:
        """The owner's onboarding message with its uploaded files; nothing when materials went straight to the pool."""
        if self.deliver != "dialog":
            return ()
        files = "\n".join(f"- {path}" for path in self.uploaded)
        attachments = attached(self.uploaded)
        return (Signal(SOURCE, self.scenario.onboarding.replace("{files}", files), attachments=attachments),)

    def _release(self, names) -> list[str]:
        new = [name for name in names if name not in self.released]
        if self.deliver == "pool":
            self.scenario.release(new, self.skills)
        self.released.extend(new)
        return new

    def _parse(self, sessions, arguments) -> Review | Spoken:
        review = (Spoken if self.analysis == "analyst" else Review).model_validate(arguments)
        expected = [criterion.id for criterion in self.scenario.criteria]
        got = [verdict.id for verdict in review.verdicts]
        if sorted(got) != sorted(expected):
            raise ValueError(f"give exactly one verdict per criterion; expected {expected}, got {got}")
        unknown = {verdict.session for verdict in review.verdicts if verdict.session} - sessions.keys()
        if unknown:
            raise ValueError(f"verdicts name sessions that did not happen: {sorted(unknown)}")
        stray = set(review.handover) - set(self.withheld)
        if stray:
            raise ValueError(f"handover may name only withheld materials {self.withheld}; got {sorted(stray)}")
        coming = {*review.handover, *self.due(self.judged + 1)} if self.chooses else set(self.withheld)
        if isinstance(review, Spoken):
            for verdict in review.verdicts:
                if verdict.waits_on is None:
                    continue
                if verdict.result != "fail":
                    raise ValueError(f"only a failed verdict waits on a material; {verdict.id} is {verdict.result}")
                if verdict.waits_on not in self.withheld:
                    raise ValueError(
                        f"a verdict waits only on a material still withheld {self.withheld}; got {verdict.waits_on!r}"
                    )
                if verdict.waits_on not in coming:
                    raise ValueError(f"hand over {verdict.waits_on!r}, the material a verdict waits on")
            return review
        failed = {verdict.id for verdict in review.verdicts if verdict.result == "fail"}
        covered = {criterion for shortfall in review.shortfalls for criterion in shortfall.criteria}
        if failed - covered:
            raise ValueError(f"give a shortfall for every failed criterion; missing: {sorted(failed - covered)}")
        if covered - failed:
            raise ValueError(f"shortfalls name only failed criteria; these did not fail: {sorted(covered - failed)}")
        for shortfall in review.shortfalls:
            if shortfall.cause == "not_held" and shortfall.material is not None:
                raise ValueError("only a material_missing shortfall names a material")
            if shortfall.cause == "material_missing" and shortfall.material not in self.withheld:
                raise ValueError(
                    f"a missing material is one still withheld {self.withheld}; got {shortfall.material!r}"
                )
            if shortfall.cause == "material_missing" and shortfall.material not in coming:
                raise ValueError(f"hand over {shortfall.material!r}, the material a shortfall waits on")
        return review

    def _record(self, sessions, drawn, found) -> None:
        if self.records is None:
            return
        self.records.mkdir(parents=True, exist_ok=True)
        with (self.records / REFERENCES).open("a") as log:
            for name in sessions:
                card = drawn.get(name)
                row = {
                    "evaluation": self.judged + 1,
                    "drill": name,
                    "card": card.text if card else None,
                    "trip": card.trip.facts() if card and card.trip else None,
                    "references": found.get(name, {}),
                }
                log.write(json.dumps(row, ensure_ascii=False) + "\n")

    async def evaluate(self, sessions) -> Signal:
        signal, _, _ = await self._judge(sessions)
        return signal

    async def review(self, worker, sessions, *, previous_signals=(), previous_feedback=None, history=()) -> role.Review:
        """The owner's judgement of the round as the Analyst's feedback; the Curator hears the owner's own words.

        A requirement is raised for each shortfall the employee had the materials for; one that waits on material is
        set aside, the material handed over instead. The decision follows in code: curate on any requirement, stop
        when everything passed and nothing is held back, supplement when only material is handed over.
        """
        earlier = list(self.reviews)
        try:
            signal, judged, handed = await self._judge(sessions)
        except Exception as exc:
            self.record(worker, {"source": SOURCE, "error": str(exc)})
            raise
        given = tuple(handed) if self.deliver == "dialog" else ()
        if self.analysis == "analyst":
            context = {"previous_signals": previous_signals, "previous_feedback": previous_feedback, "history": history}
            return await self._analysed(worker, sessions, signal, judged, given, **context)
        # TODO: the single-layer owner analysis is kept only to reproduce runs made before the two-layer design
        # (owner speaks, base Analyst writes the requirements); it is planned for removal.
        severity = {criterion.id: criterion.severity for criterion in self.scenario.criteria}
        raised = [shortfall for shortfall in judged.shortfalls if shortfall.cause == "not_held"]
        requirements = []
        for shortfall in raised:
            failed_before = sum(bool(set(shortfall.criteria) & set(entry["failed"])) for entry in earlier)
            told_before = any(set(shortfall.criteria) & set(entry.get("raised", ())) for entry in earlier)
            firm = failed_before or any(severity.get(criterion) == "red_line" for criterion in shortfall.criteria)
            requirements.append(
                Requirement(
                    behavior=shortfall.behavior,
                    observed=shortfall.observed,
                    evidence=tuple(shortfall.evidence),
                    expectation="unmet" if told_before else "new",
                    acceptance=shortfall.acceptance,
                    strength="must_hold" if firm else shortfall.strength,
                    recurrence=failed_before,
                )
            )
        filtered = tuple(
            f"{', '.join(shortfall.criteria)}: waits on material "
            + (
                f"handed over with this review ({shortfall.material})"
                if shortfall.material in handed
                else "not given yet"
            )
            for shortfall in judged.shortfalls
            if shortfall.cause == "material_missing"
        )
        passed = all(verdict.result == "pass" for verdict in judged.verdicts)
        giving = f" With this review the owner hands over: {', '.join(given)}." if given else ""
        if requirements:
            decision = "curate"
            reason = f"{len(requirements)} behavior(s) did not hold although the employee had the owner's materials for them."
        elif passed and not self.withheld and not given:
            decision, reason = "stop", "Every criterion passed and the owner has handed over all of its materials."
        elif given:
            decision, reason = "supplement", "Nothing the employee had the materials for fell short."
        else:
            decision, reason = "continue", "Nothing the employee had the materials for fell short."
        feedback = Feedback(
            decision=decision, reason=reason + giving, requirements=tuple(requirements), filtered=filtered
        )
        self.record(
            worker,
            {
                "source": SOURCE,
                "signals": (signal,),
                "shortfalls": [shortfall.model_dump() for shortfall in judged.shortfalls],
                "requirement_criteria": [shortfall.criteria for shortfall in raised],
                "feedback": feedback,
            },
        )
        heard = (Signal(SOURCE, signal.text, attachments=signal.attachments),)
        return role.Review((signal,), feedback, tuple(activity(sessions)), heard, given)

    async def _analysed(self, worker, sessions, scorecard, judged, given, **context) -> role.Review:
        """The owner's words as the base Analyst reads them for any evaluator: its remark, whether it is satisfied and
        what it hands over, never its verdicts. The scorecard is recorded first, so a failed analysis keeps it."""
        spoken = Signal(SOURCE, scorecard.text, satisfied=scorecard.satisfied, attachments=scorecard.attachments)
        self.record(worker, {"source": SOURCE, "scorecard": scorecard, "waiting_on_material": waiting(judged)})
        try:
            feedback = await analyse(
                worker,
                self.provider,
                (spoken,),
                sessions,
                model=self.analyst_model,
                limits=self.analyst_limits,
                **context,
            )
        except Exception as exc:
            exc.signals = (spoken,)
            raise
        return role.Review((spoken,), feedback, tuple(activity(sessions)), None, given)

    async def _judge(self, sessions) -> tuple[Signal, Review | Spoken, list[str]]:
        """The owner's call: its signal, its submission and the materials it handed over with it."""
        drawn = self.cards()
        found = {
            name: references(self.rules, exchanges, getattr(drawn.get(name), "trip", None))
            for name, exchanges in sessions.items()
            if self.rules
        }
        self._record(sessions, drawn, found)
        delivered = {name: deliverables(exchanges) for name, exchanges in sessions.items()}
        researched = {name: research(exchanges) for name, exchanges in sessions.items()}
        packet = {
            "profile": self.scenario.profile,
            "criteria": [criterion.model_dump() for criterion in self.scenario.criteria],
            "materials": {name: self.scenario.text(name) for name in self.scenario.materials},
            "given_to_the_assistant": list(self.released),
            "withheld": self.withheld,
            "handing_over_now": self.due(self.judged + 1),
            "you_choose_handover": self.chooses,
            "your_earlier_reviews": self.reviews,
            "curator_reply": self._reply(),
            "cards": {name: drawn[name].text for name in sessions if name in drawn},
            "references": {name: facts for name, facts in found.items() if facts},
            "conversations": {name: transcript(exchanges) for name, exchanges in sessions.items()},
            "deliverables": {name: files for name, files in delivered.items() if files},
            "research": {name: notes for name, notes in researched.items() if notes},
            "colleague_reports": {name: told for name, exchanges in sessions.items() if (told := reports(exchanges))},
            "filed": {name: files for name in sessions if (files := self._filed(name))},
            "back_office": {name: rows for name, exchanges in sessions.items() if (rows := back_office(exchanges))},
        }
        packet["deck_pages"], pictures = looks(sessions)
        spoken = self.analysis == "analyst"
        request = messages((_OWNER if spoken else _PROMPT).read_text(), packet)
        if pictures:
            lead = {"type": "text", "text": "The pages of the delivered decks, in the order `deck_pages` lists them."}
            request.append({"role": "user", "content": [lead, *pictures]})
        _, review = await exchange(
            self.provider,
            request,
            [
                tool(
                    NAME,
                    "Submit your scorecard (one verdict per criterion), your remark to the trainer, and any materials "
                    "you now hand over.",
                    schema_for(Spoken),
                )
                if spoken
                else tool(
                    NAME,
                    "Submit one verdict per criterion, a shortfall for every failure, your remark to the trainer, and "
                    "any materials you now hand over.",
                    schema_for(Review),
                )
            ],
            submit={NAME: lambda arguments: self._parse(sessions, arguments)},
            model=self.model,
            max_calls=self.max_calls,
            timeout=self.timeout,
            label="agency",
        )
        criteria = {criterion.id: criterion for criterion in self.scenario.criteria}
        items = tuple(
            Item(v.id, v.result, v.session, expected=criteria[v.id].check, actual=v.actual, note=v.note)
            for v in review.verdicts
        )
        self.judged += 1
        handed = self._release([*(review.handover if self.chooses else ()), *self.due(self.judged)])
        text, uploaded = review.remark.strip(), []
        if self.deliver == "dialog" and handed:
            uploaded = self.scenario.upload(handed, self.uploads, shared=self.shared)
            self.uploaded.extend(uploaded)
            files = "\n".join(f"- {path}" for path in uploaded)
            note = self.scenario.handover.replace("{files}", files) if self.scenario.handover else files
            text = "\n\n---\n\n".join([text, note, *(self.scenario.document(name).strip() for name in handed)])
        entry = {
            "round": len(self.reviews) + 1,
            "remark": review.remark.strip(),
            "failed": sorted({v.id for v in review.verdicts if v.result == "fail"}),
        }
        if not spoken:
            entry["raised"] = sorted({c for s in review.shortfalls if s.cause == "not_held" for c in s.criteria})
        self.reviews.append(
            {**entry, "waiting_on_material": sorted(set(waiting(review).values())), "handed_over": handed}
        )
        signal = Signal(
            SOURCE,
            text,
            items,
            satisfied=all(item.result == "pass" for item in items),
            attachments=attached(uploaded),
        )
        return signal, review, handed
