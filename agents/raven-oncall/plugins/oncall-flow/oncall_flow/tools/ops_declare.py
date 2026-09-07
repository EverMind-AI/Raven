"""OpsDeclareTool: write a campaign's declaration, once, before anything runs.

``meta.json`` has always been this declaration -- what the target is, which
machine and which case the work runs on, how a trial starts, what the budget is,
where it starts from -- and the apparatus gate already refuses a submit if it
changes. What was missing was the front door: every campaign here was set up by
hand, so an agent given a task and a path had nowhere to record what it worked
out. Measured 2026-08-18: an arm chose the right machine, read the owner's run
script, and then submitted with an empty address and a container image that held
none of the owner's software, because ``ops_submit`` had no field for any of it.

Splitting this out of ``ops_submit`` is what keeps that tool from growing: of its
twenty parameters, sixteen were read once at round 0 and overridden from the
campaign on every round after. It also puts a gap between deciding how to run and
spending the first minute of compute -- which is where the owner gets to look.
"""

from __future__ import annotations

import json
import re
import shlex
from datetime import datetime
from pathlib import Path
from typing import Any

from oncall_flow import actions as ops_actions
from oncall_flow import readings as ops_readings
from oncall_flow.budget import COMPUTE, LOOKS, WALL_CLOCK
from raven.contracts.tool import Tool

_METERS = frozenset({COMPUTE, WALL_CLOCK, LOOKS})

# A runaway stop, not a plan. It was 8, and on 2026-08-18 a campaign ended
# because of it with 15% of its budget unspent and its question unanswered -- the
# number that decided when an experiment was over had been picked by nobody, for
# every domain at once. Rounds are the wrong unit for that: eight of them is half
# an hour of FEA and nine hours of CFD. What may be spent is the budget, and when
# the work is done is the loop's own call; this only catches a loop that stopped
# deciding.
_DEFAULT_MAX_ROUNDS = 50


# What a command template may refer to. Each is something the campaign knows and
# the trial does not: where this round's directory is, where its config landed,
# which case it came from, and where rounds are kept.
_PLACEHOLDERS = frozenset({"job_dir", "config", "staged_case", "remote_dir"})


def _round_dir_inside_case(remote_dir: str, staged_case: str) -> tuple[str, str] | None:
    """``(case, rounds)`` when rounds would land inside the case, else None."""
    if not remote_dir or not staged_case:
        return None
    case = Path(staged_case).expanduser()
    rounds = Path(remote_dir).expanduser()
    if rounds == case or case in rounds.parents:
        return str(case), str(rounds)
    return None


# Commands whose whole job is to put a copy of something somewhere else. Only the
# first word of a step is checked against this, so a "cp" inside a filename or a
# message is not one of them.
_COPIERS = frozenset({"cp", "rsync", "scp", "install", "cpio", "tar"})


def _copies_the_case(command: str, staged_case: str) -> str | None:
    """The step that copies the case into the round's directory, or None.

    Every file of the case is already linked into the round's directory before the
    command runs, so a copy has the same file on both sides and stops. Refused
    here because saying so did not work: both the ``command`` and ``staged_case``
    descriptions said the case was already there, and the declaration measured on
    2026-08-19 at 16:06 -- twenty minutes after those words went in -- copied it in
    anyway. What a trial copies within its own directory is untouched.
    """
    if not command:
        return None
    case = str(staged_case or "").rstrip("/")
    for step in re.split(r"&&|\|\||[;\n|]", command):
        step = step.strip()
        if not step:
            continue
        try:
            words = shlex.split(step)
        except ValueError:  # unbalanced quotes, e.g. a heredoc; leave it alone
            words = step.split()
        if not words or Path(words[0]).name not in _COPIERS:
            continue
        if "{staged_case}" in step or (case and case in step):
            return step
    return None


_NPROC = re.compile(r"--nproc[_-]per[_-]node(?:=|\s+)(\d+)")


def _whole(value: object) -> int | None:
    """A positive whole number, or None for anything else (including bool)."""
    if isinstance(value, bool) or value is None:
        return None
    try:
        n = int(value)
    except (TypeError, ValueError):
        return None
    return n if n >= 1 and float(value) == n else None


def _resources_words(resources: dict) -> str:
    parts = []
    if resources.get("gpus_per_job"):
        parts.append(f"{resources['gpus_per_job']} device(s)")
    if resources.get("cores_per_job"):
        parts.append(f"{resources['cores_per_job']} core(s)")
    if resources.get("memory_per_job_gb"):
        parts.append(f"{resources['memory_per_job_gb']} GB")
    return ", ".join(parts) or "1 unit"


def _resources_for(
    row: dict,
    machine: str,
    command: str,
    *,
    gpus_per_job: int | None,
    cores_per_job: int | None,
    memory_per_job_gb: int | None,
) -> tuple[str, dict]:
    """(refusal, resources) for what one job of this campaign holds.

    Owner's rulings, 2026-09-03: a job declares how much of a machine it holds,
    the machine declares how much it has, and the gate admits by what is free.
    The number is said here, once, because cards per job is a property of how
    the job starts and every trial starts the same way. Checked at declare so a
    campaign that can never run is never created. A row that hands out nothing
    countable (no gpus, no device count, no cores) keeps the job-count gate and
    is asked for nothing; so is a campaign that starts no trial.
    """
    from oncall_flow.connections import capacity, resource_unit

    unit = resource_unit(row)
    if not command:
        return "", {}
    if not unit:
        # A row that hands out nothing countable keeps the job-count gate. On a
        # GPU row with no device count that gate can still stand for one card
        # per job (a campaign from before this field is read as one), so a
        # declaration of 1 passes through; more than one cannot be held to a
        # count the row does not have, and the migration table makes that the
        # blocking case (reviewed 2026-09-07).
        n = _whole(gpus_per_job)
        if n is not None and n > 1 and str(row.get("kind") or "").strip().lower() == "gpu":
            return (
                f"REFUSED: this declaration says one job holds {n} devices, but the row for {machine} does "
                "not say how many devices it has, so nothing can hold that number to a count and the gate "
                "would admit by job count alone. Fix the row first: write 'gpus: N' (or a device line "
                "'N x <card>'; `raven ops connection add` probes and writes both), then declare again. "
                "Nothing was written.",
                {},
            )
        return "", {}
    cap = capacity(row)
    res: dict = {}
    if unit == "gpus":
        n = _whole(gpus_per_job)
        if n is None:
            return (
                f"REFUSED: {machine} hands out devices ({cap['gpus']} of them), and this declaration does "
                "not say how many one job holds. Pass gpus_per_job -- read it off how the job starts "
                "(torchrun --nproc_per_node, or the length of the CUDA_VISIBLE_DEVICES list the code "
                "side used); a single-card run is 1. Nothing was written.",
                {},
            )
        if n > cap["gpus"]:
            return (
                f"REFUSED: {machine} has {cap['gpus']} device(s); one job holding {n} can never start here. "
                "Declare it on a machine with that many, or split the work. Nothing was written.",
                {},
            )
        if "CUDA_VISIBLE_DEVICES=" in command:
            return (
                "REFUSED: the command assigns devices itself (CUDA_VISIBLE_DEVICES=...). The system picks "
                "the cards a job holds and exports that variable before the command runs; a template that "
                "sets it too puts two jobs on one card. Take it out of the command. Nothing was written.",
                {},
            )
        m = _NPROC.search(command)
        if m and int(m.group(1)) != n:
            return (
                f"REFUSED: the command starts {m.group(1)} process(es) per node (--nproc_per_node) and "
                f"gpus_per_job says {n}. They have to agree -- the launcher exports exactly gpus_per_job "
                "devices. Nothing was written.",
                {},
            )
        res["gpus_per_job"] = n
    else:
        n = _whole(cores_per_job)
        if n is None:
            return (
                f"REFUSED: {machine} hands out cores ({cap['cores']} of them), and this declaration does "
                "not say how many one job holds. Pass cores_per_job -- read it off how the job starts "
                "(mpirun -np, decomposeParDict numberOfSubdomains); a serial run is 1. Nothing was written.",
                {},
            )
        if n > cap["cores"]:
            return (
                f"REFUSED: {machine} has {cap['cores']} core(s); one job holding {n} can never start here. "
                "Declare it on a machine with that many, or split the work. Nothing was written.",
                {},
            )
        res["cores_per_job"] = n
    if memory_per_job_gb is not None:
        mem = _whole(memory_per_job_gb)
        if mem is None:
            return (
                f"REFUSED: memory_per_job_gb must be a whole number of GB, not {memory_per_job_gb!r}. "
                "Nothing was written.",
                {},
            )
        if cap.get("memory_gb") and mem > cap["memory_gb"]:
            return (
                f"REFUSED: {machine} has {cap['memory_gb']} GB of memory; one job holding {mem} GB can "
                "never start here. Nothing was written.",
                {},
            )
        res["memory_per_job_gb"] = mem
    return "", res


def _device_key_note(meta: dict) -> str:
    """A config that names a card is telling the system something it does not read."""
    res = meta.get("resources") if isinstance(meta.get("resources"), dict) else {}
    seed = meta.get("seed_config") if isinstance(meta.get("seed_config"), dict) else {}
    if res.get("gpus_per_job") and any(k in seed for k in ("gpu", "CUDA_VISIBLE_DEVICES")):
        return (
            "seed_config carries a device key ('gpu' / 'CUDA_VISIBLE_DEVICES'); the system does not read "
            "it -- devices are assigned at submit and exported to the job"
        )
    return ""


def _unknown_placeholders(command: str) -> set[str]:
    """Names in ``command`` that nothing will fill in."""
    import string

    if not command:
        return set()
    try:
        names = {name for _, name, _, _ in string.Formatter().parse(command) if name}
    except ValueError:  # an unbalanced brace; the backend will say so plainly
        return set()
    return {n.split(".")[0].split("[")[0] for n in names} - _PLACEHOLDERS


OPTIMIZE = "optimize"
CONDITION = "condition"
COMPLETE = "complete"
_KINDS = (OPTIMIZE, CONDITION, COMPLETE)

# Phrases that say a number should go as far as it can. Deliberately narrow, in
# the same discipline as ops_finish's comparative net: it can only add a warning,
# never let a declaration through, and it is not a substitute for the field.
# The fork also matched a handful of Chinese phrasings; the repo's language
# rule keeps source strings English, and this net can only add a warning --
# an objective worded another way simply is not caught (module docstring
# discipline: never a substitute for the declared field).
_OPTIMISING = (
    "as small as possible",
    "as large as possible",
    "as fast as possible",
    "as low as possible",
    "as high as possible",
    "as short as possible",
    "minimise",
    "minimize",
    "maximise",
    "maximize",
    "the fastest",
    "the shortest",
)


def _optimising_words(text: str) -> list[str]:
    lowered = (text or "").lower()
    return [w for w in _OPTIMISING if w in lowered]


def _objective_refusal(kind: str, metric: str, goal: str, condition: str) -> str | None:
    """Why this target cannot be declared as stated, or None.

    The field exists because ``metric`` and ``goal`` were required of every
    campaign, and not every campaign has a number to rank rounds by. Measured
    2026-08-21 across seven campaigns an arm declared for itself: three invented
    one to get past the requirement -- two CFD cases whose task was "run to
    endTime and let the results hold up" declared ``completion max``, and a
    limit-load search declared ``total_load max``, which is the load it chooses
    for each round rather than anything the solver reports, so the campaign's
    best trial was permanently None. The other four had a real number and got it
    right, which is the point: reading the owner's intent is what the loop is
    good at, and being forced to state it in a shape the task does not have is
    what produced the fiction.

    So each shape is asked only for what it actually has, and refused when that
    is missing.
    """
    kind = (kind or "").strip().lower()
    if kind not in _KINDS:
        return (
            f"REFUSED: objective_kind must be one of {', '.join(_KINDS)}, not {kind!r}.\n"
            "  optimize  one number to push as far as it goes -- needs metric and goal\n"
            "  condition something outside has to become true, then you act -- needs condition\n"
            "  complete  it has to run to its own end and the result has to hold up; no number\n"
            "            ranks one round above another\n"
            "Nothing was written."
        )
    if kind == OPTIMIZE:
        if not metric:
            return (
                "REFUSED: an optimize campaign is judged by one number, and none was named.\n"
                "Pass metric -- a number the RUN REPORTS, not one you set: an input you choose "
                "for each round cannot rank the rounds that chose it, and a campaign declared "
                "that way has no best trial at all.\n"
                "If the work is 'run it to the end and let the result hold up', that is "
                "objective_kind='complete' and needs no metric. Nothing was written."
            )
        if goal not in ("max", "min"):
            return (
                f"REFUSED: goal must be 'max' or 'min', not {goal!r}. Which direction is better "
                f"cannot be read off a metric's name, and getting it wrong hands over the worst "
                f"trial. Nothing was written."
            )
    if kind == CONDITION and not condition.strip():
        return (
            "REFUSED: a condition campaign waits for something to become true, and this does "
            "not say what.\n"
            "Pass condition, in terms of something you can read: 'VOLT is 10% below what it was "
            "when this was declared', not 'the price drops a lot'. A wake turn remembers nothing, "
            "so what it compares against has to be written down here. Nothing was written."
        )
    return None


def _only_adds_readings(previous: dict[str, Any] | None, now: dict[str, Any]) -> bool:
    """Whether this second declaration changes nothing but the readings table.

    The rule it excuses is right for what it was written about: a declaration is
    what recorded results are read against, so moving it under them leaves them
    saying they were produced under conditions that no longer exist. Adding a
    reading does not do that. Nothing recorded changes meaning, and redefining an
    existing one is refused separately -- what a new reading gives is a column
    that starts from here.

    And it has to be allowed after results exist, because that is when it becomes
    possible. Measured 2026-08-17: which line of job.dat holds the penetration
    was worked out by sending 74 commands at the FIRST ROUND'S output. Requiring
    the table up front would ask for something the case does not tell you until
    it has run once.
    """
    if not previous:
        return False
    settled = {k: v for k, v in (previous or {}).items() if k != "readings"}
    proposed = {k: v for k, v in now.items() if k != "readings"}
    if settled != proposed:
        return False
    was = {r.name: r.command for r in _readings_of(previous)}
    now_named = {r.name: r.command for r in _readings_of(now)}
    return all(now_named.get(k) == v for k, v in was.items())


def _readings_of(meta: dict[str, Any]):
    return ops_readings.declared(meta or {})


def _cannot_amend(cdir: Path, name: str) -> str | None:
    """Why this campaign's declaration may not be rewritten, or None if it may.

    A second declaration under the same name used to be refused outright, with the
    reason that rewriting it "would move the target under results that are already
    recorded". That reason is exactly right, and it is why the first two checks
    below exist -- but it was being given when there were no results at all, and
    then it is simply false. Measured 2026-08-21: an arm declared an OpenFOAM case
    with ``source ...`` in the command, round 0 died on it (``sh: source: not
    found``), and with nothing recorded and no compute spent the task was still
    unrecoverable. It filed a failure with 150 core-minutes unspent.

    So the question is not how many times a name has been declared, but whether
    anything depends on the declaration yet:

      concluded   the campaign was delivered; the report states the conditions
      succeeded   a result exists, and it means what the declaration said
      in flight   a round is running against this declaration right now, and
                  amending mid-flight would leave the ledger half one setup and
                  half another

    None of those, and a second declaration is just getting the preparation right.
    """
    from oncall_flow.backend import JobStatus
    from oncall_flow.ledger import Ledger

    if (cdir / "concluded.json").exists():
        return (
            f"REFUSED: '{name}' has already been concluded, and its report states the "
            f"conditions the result was produced under. Changing the setup behind a "
            f"delivered report would leave the two disagreeing.\n"
            f"Declare the follow-up under a different name. Nothing was written."
        )
    ledger_file = cdir / "ledger.json"
    if ledger_file.exists():
        try:
            # Every record in this file belongs to this campaign -- the file lives
            # in its directory -- and reading it that way does not depend on each
            # record carrying a campaign field, which an older one may not.
            records = Ledger(str(ledger_file)).all()
        except Exception:  # noqa: BLE001 -- an unreadable ledger is not permission to rewrite
            return (
                f"REFUSED: '{name}' is already declared and its ledger could not be read, "
                f"so whether anything depends on this declaration is unknown. Declare "
                f"under a different name. Nothing was written."
            )
        if any(r.status is JobStatus.SUCCEEDED for r in records):
            return (
                f"REFUSED: '{name}' already has results, and a declaration is what they "
                f"are read against -- rewriting it would leave them saying they were "
                f"produced under conditions that no longer exist.\n"
                f"Read it with ops_tune_status(campaign='{name}'). If this is different "
                f"work, give it a different name. Nothing was written."
            )
        live = [r for r in records if not r.status.is_terminal]
        if live:
            return (
                f"REFUSED: '{name}' has {len(live)} round(s) running against the current "
                f"declaration. Amending now would leave the ledger half under one setup "
                f"and half under another.\n"
                f"Wait for them to finish, or end them with ops_kill, and declare again. "
                f"Nothing was written."
            )
    return None


def _log_amendment(cdir: Path, before: dict[str, Any] | None, after: dict[str, Any]) -> None:
    """Record what the second declaration changed, field by field."""
    try:
        from oncall_flow.instrument import log_event

        old = before or {}
        changed = {
            k: {"was": old.get(k), "now": after.get(k)}
            for k in sorted(set(old) | set(after))
            if old.get(k) != after.get(k)
        }
        log_event(cdir, "declaration_amended", changed=changed)
    except Exception:  # noqa: BLE001 -- the trail must not cost the amendment
        pass


class OpsDeclareTool(Tool):
    @property
    def name(self) -> str:
        return "ops_declare"

    @property
    def description(self) -> str:
        return (
            "Declare a campaign ONCE, before any of it runs. A campaign is anything the owner "
            "wants stayed with over time -- work to run and watch, OR something outside to keep "
            "an eye on and act on when it changes: a price, a disk filling up, a queue, somebody "
            "else's job. The second kind runs nothing and has no case; it still belongs here, "
            "because what it needs is exactly what this gives -- a starting value recorded at the "
            "moment you were asked (a later wake cannot reconstruct it), a budget that can be "
            "counted in looks, a record of every reading and every action, and a wake that "
            "carries all of it back to you. A cron job and a file of your own do the same "
            "arithmetic with none of that.\n"
            "What to declare: which machine, what the target is, what to read and when, what may "
            "be done, what it may spend, and -- for work that is run -- which case and how one "
            "trial starts. The target has three shapes and objective_kind says which: a number "
            "to optimise, a condition to watch for, or work that has to run to its end and hold "
            "up. "
            "Nothing runs and no compute is spent, so a mistake costs nothing here and is "
            "worth checking before the first submit -- from then on every round is checked "
            "against this and it cannot be rewritten. Work it out rather than ask: the machine "
            "from what the task needs, the command and starting values from the case's own "
            "scripts, the target from what the owner asked to know."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "campaign": {
                    "type": "string",
                    "description": "Short name for this experiment, e.g. 'beam-limit-load'.",
                },
                "objective": {
                    "type": "string",
                    "description": "What this campaign is for, in the owner's terms.",
                },
                "objective_kind": {
                    "type": "string",
                    "enum": ["optimize", "condition", "complete"],
                    "description": "Which shape the target has. 'optimize' -- one number to push as "
                    "far as it will go ('get the L2 error as small as possible'); "
                    "needs metric and goal. 'condition' -- something outside has to "
                    "become true, and then you act ('if VOLT drops more than 10%, buy "
                    "3 shares'); needs condition. 'complete' -- the work has to run "
                    "to its own end and the result has to hold up, with no number "
                    "ranking one round above another ('get the dam-break case to "
                    "endTime with usable results'). Read it off what the owner asked "
                    "for, the same way you read whether a number should be large or "
                    "small.",
                },
                "metric": {
                    "type": "string",
                    "description": "For objective_kind='optimize': the one number rounds are judged "
                    "by, e.g. 'residual'. It has to be a number the run REPORTS, not "
                    "one you set -- an input you choose cannot rank the rounds that "
                    "chose it. A campaign with no such number is 'complete' or "
                    "'condition'; leave this out there rather than inventing one.",
                },
                "goal": {
                    "type": "string",
                    "enum": ["max", "min"],
                    "description": "Whether that number should be as large or as small as possible. "
                    "Never guessed from the name: for a loss the best value is the "
                    "smallest, so a guess hands over the worst trial. Goes with metric.",
                },
                "condition": {
                    "type": "string",
                    "description": "For objective_kind='condition': what has to become true, said in "
                    "terms of something you can read -- 'VOLT is 10% below what it "
                    "was when this was declared', not 'the price drops a lot'. For "
                    "'complete' it is what counts as finished, and is worth writing "
                    "even though it is not required there.",
                },
                "connection": {
                    "type": "string",
                    "description": "Machine id from ops_connections. Say which you picked and why.",
                },
                "staged_case": {
                    "type": "string",
                    "description": "The directory on that machine holding the owner's case, from "
                    "their task. Read, never written: each round gets its own "
                    "directory already filled with links to every file in here, so "
                    "output lands there and this stays untouched. Nothing needs "
                    "copying.",
                },
                "command": {
                    "type": "string",
                    "description": "The one command that runs the case once, as typed on the "
                    "machine. The case is ALREADY in the round's directory before "
                    "this runs -- every file of it, linked, costing nothing -- so "
                    "do not copy it in; the command is usually just its entry "
                    "script. Four things are filled in and nothing else: "
                    "'{job_dir}' this round's own directory (also the working "
                    "directory), '{config}' its config.json, '{staged_case}' the "
                    "case it came from, '{remote_dir}' where rounds are kept. "
                    "Read the entry script first with ops_exec(machine=...).",
                },
                "backend": {
                    "type": "string",
                    "enum": ["process", "docker", "openfoam"],
                    "description": "'process' runs the command on the machine, which anything the "
                    "owner installed needs; 'docker' runs an image holding none of "
                    "their software. Defaults to 'process' with a command.",
                },
                "gpus_per_job": {
                    "type": "integer",
                    "description": (
                        "How many devices one job holds, on a machine that hands out devices. Read off how "
                        "the job starts (torchrun --nproc_per_node, the length of the CUDA_VISIBLE_DEVICES "
                        "list the code side used); the system assigns the ids and exports the variable, the "
                        "command must not. A config may override with gpus_needed."
                    ),
                },
                "cores_per_job": {
                    "type": "integer",
                    "description": (
                        "How many cores one job holds, on a CPU machine (mpirun -np, decomposeParDict "
                        "numberOfSubdomains). A config may override with cores_needed."
                    ),
                },
                "memory_per_job_gb": {
                    "type": "integer",
                    "description": (
                        "Optional: memory one job holds, in GB, checked against the machine's memory when "
                        "declared. A config may override with memory_needed_gb."
                    ),
                },
                "seed_config": {
                    "type": "object",
                    "description": "The configuration round 0 must run. Take it from the case: the "
                    "entry script's defaults are the owner's starting point, and a "
                    "parameter with NO default is usually what the experiment "
                    "searches over.",
                },
                "budget_total": {
                    "type": "number",
                    "description": "Total this may spend. Only the owner knows it -- nothing "
                    "in the case implies one. Leave out when they did not say: no "
                    "budget is a real answer, not zero, and spend is reported anyway.",
                },
                "budget_unit": {
                    "type": "string",
                    "description": "Unit of budget_total, e.g. 'core-minute', 'gpu-minute', 'minute', 'look'.",
                },
                "budget_meter": {
                    "type": "string",
                    "enum": ["compute", "wall-clock", "look"],
                    "description": "What is being spent, which decides who can measure it. 'compute' "
                    "is machine time and the host reads it. 'wall-clock' is how long "
                    "the watch stays open -- use it when the work is waiting rather "
                    "than computing, since the host would answer zero for a watch "
                    "that ran no jobs. 'look' counts the times you come back and "
                    "read: each one is a full wake, so a day watched once a minute "
                    "is 1440 of them. Never guessed from the unit: a 'minute' is a "
                    "core-minute on a solver and a wall-clock minute on a watch. "
                    "Defaults to 'compute'.",
                },
                "budget_overlap": {
                    "type": "string",
                    "enum": ["additive", "shared"],
                    "description": "For compute only: 'additive' when concurrent trials each spend "
                    "the budget; 'shared' when they occupy one device and it counts once.",
                },
                "max_rounds": {
                    "type": "integer",
                    "description": f"Hard stop on the number of rounds (default "
                    f"{_DEFAULT_MAX_ROUNDS}). This catches a loop that has stopped "
                    f"deciding; it is not how many rounds the work should take. "
                    f"When the work is done is your call, and the budget is what "
                    f"says how much may be spent -- do not treat this as a target "
                    f"to run up to.",
                },
                "actions": {
                    "type": "array",
                    "description": "Things this campaign may DO when what it is watching calls for "
                    "it -- one row each: {name, command, repeat}. Only for work that "
                    "acts on the world: placing an order, applying, raising an "
                    "alert. Starting a trial is 'command' above and does not belong "
                    "here. Written down now because this table is the list of what "
                    "may be done: an action not in it cannot be taken, and the line "
                    "cannot be composed later when nobody is looking. 'repeat' says "
                    "what doing it twice does -- 'harmful' (an order, an email: the "
                    "second identical call is refused) or 'safe' (a like, a read "
                    "mark). Use {placeholders} for what changes per call and pass "
                    "them as ops_submit(values=...).",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "command": {"type": "string"},
                            "repeat": {"type": "string", "enum": ["harmful", "safe"]},
                        },
                        "required": ["name", "command", "repeat"],
                    },
                },
                "readings": {
                    "type": "array",
                    "description": "The numbers this campaign watches -- one row each: "
                    "{name, command, when}. 'command' PRINTS the value and nothing "
                    "else; it is run again and again, so it must not change "
                    "anything. 'when' is at_declare (once, now -- the starting "
                    "point a later value is compared against), after_trial (when a "
                    "round finishes), during_trial (while one runs -- progress that "
                    "cannot be seen afterwards), or each_wake (the current state of "
                    "whatever you are watching). {job_dir} is filled in for the two "
                    "per-trial ones. Include what you must SEE as well as what you "
                    "optimise: a hard constraint you never read is one you cannot "
                    "report on. You need not have them all now -- declare again "
                    "later to add more; an existing one cannot be redefined.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "command": {"type": "string"},
                            "when": {
                                "type": "string",
                                "enum": ["at_declare", "after_trial", "during_trial", "each_wake"],
                            },
                        },
                        "required": ["name", "command", "when"],
                    },
                },
                "remote_dir": {
                    "type": "string",
                    "description": "Where trial directories go: writable, and not inside the case.",
                },
                "image": {
                    "type": "string",
                    "description": "Container image, for backend='docker' only.",
                },
            },
            "required": ["campaign", "objective", "objective_kind"],
        }

    async def execute(
        self,
        campaign: str,
        objective: str,
        objective_kind: str = "",
        metric: str = "",
        goal: str = "",
        condition: str = "",
        connection: str = "",
        staged_case: str = "",
        command: str = "",
        backend: str = "",
        seed_config: dict | None = None,
        gpus_per_job: int | None = None,
        cores_per_job: int | None = None,
        memory_per_job_gb: int | None = None,
        budget_total: float | None = None,
        budget_unit: str = "",
        budget_meter: str = "",
        budget_overlap: str = "",
        max_rounds: int = _DEFAULT_MAX_ROUNDS,
        actions: list | None = None,
        readings: list | None = None,
        remote_dir: str = "",
        image: str = "",
        **kwargs: Any,
    ) -> str:
        from oncall_flow.connections import describe as conn_describe
        from oncall_flow.connections import get as conn_get
        from oncall_flow.tools.ops import _ops_home, _slug

        name = str(campaign).strip()
        if not name:
            return "REFUSED: an experiment needs a name. Nothing was written."
        cdir = _ops_home() / _slug(name)
        meta_file = cdir / "meta.json"
        amending = meta_file.exists()
        previous: dict[str, Any] | None = None
        if amending:
            try:
                previous = json.loads(meta_file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                previous = None
        reading_problem = ops_readings.table_problem(readings, ops_readings.declared(previous or {}))
        if reading_problem:
            return reading_problem
        action_problem = ops_actions.table_problem(actions, ops_actions.declared(previous or {}))
        if action_problem:
            return action_problem
        kind_refusal = _objective_refusal(objective_kind, metric, goal, condition)
        if kind_refusal:
            return kind_refusal
        # Settled here, above the checks that differ by shape: a campaign that
        # watches has no trial to start, and the refusals below have to know that
        # before they ask it how one starts.
        kind = objective_kind.strip().lower()
        if not connection:
            return (
                "REFUSED: this experiment has no machine to run on.\n"
                "Call ops_connections, pick the one the work calls for, and pass its id. It is "
                "the one thing a campaign cannot be given later.\n" + conn_describe()
            )
        if conn_get(connection) is None:
            return f"REFUSED: there is no connection with id {connection!r}.\n{conn_describe()}"
        resource_refusal, resources = _resources_for(
            conn_get(connection) or {},
            display_name_or_id(connection),
            command,
            gpus_per_job=gpus_per_job,
            cores_per_job=cores_per_job,
            memory_per_job_gb=memory_per_job_gb,
        )
        if resource_refusal:
            return resource_refusal
        unknown = _unknown_placeholders(command)
        if unknown:
            # Caught here rather than at the first submit, where it surfaced as a
            # bare KeyError. Measured 2026-08-19: a command using {staged_case}
            # got "KeyError 'staged_case' -- this is a fault inside the tool
            # itself, do not work around it", which is true and left the loop with
            # nothing it was allowed to do. It retried the same call three times
            # and the campaign never ran a round.
            return (
                f"REFUSED: the command uses {', '.join(sorted(unknown))}, and nothing fills "
                f"those in.\n"
                "What gets expanded, and nothing else:\n"
                "  {job_dir}      this round's own directory, where it should write\n"
                "  {config}       that directory's config.json, holding this round's values\n"
                "  {staged_case}  the case you named, already linked into this round\n"
                "  {remote_dir}   where round directories are kept\n"
                "Anything else has to be written out in full. Nothing was written."
            )
        copying = _copies_the_case(command, staged_case)
        if copying:
            return (
                f"REFUSED: this copies the case in, and the case is already there.\n"
                f"  the copying  {copying}\n"
                f"Before your command runs, this round's directory is filled with a link to "
                f"every file in the case -- all of it, costing nothing -- so both sides of "
                f"that copy are the same file and it stops with 'are the same file'. Drop it: "
                f"the command is usually just the entry script, run in the round's own "
                f"directory. Nothing was written."
            )
        chosen_backend = backend or ("process" if command else "")
        if chosen_backend == "docker" and str(conn_get(connection).get("transport") or "") == "local":
            # A container on the machine raven itself runs on is not wired up: the
            # docker path still syncs its trial directory over rsync-through-ssh,
            # and there is no such thing here. Refused rather than left to fail at
            # the staging step, where it would read as a network problem.
            return (
                f"REFUSED: {display_name_or_id(connection)} is the machine raven runs on, and "
                f"the container path is not available there yet.\n"
                f"Anything installed on that machine runs with backend='process' and a command. "
                f"Nothing was written."
            )
        if chosen_backend in ("process", "openfoam") and not command:
            return (
                f"REFUSED: backend={chosen_backend!r} runs a command on the machine, and none was "
                f"given, so there is nothing to run.\n"
                f"Look at the case with ops_exec(machine=...) -- its entry script says how it starts -- and "
                f"pass that line as 'command'. Nothing was written."
            )
        if not chosen_backend and kind != CONDITION:
            return (
                "REFUSED: this experiment does not say how a trial starts.\n"
                "Pass 'command': the one line that runs the owner's case once, the way it would be "
                "typed on that machine. Read the case with ops_exec(machine=...) first if you do not know it.\n"
                "Only pass backend='docker' if the work really is a container image rather than "
                "something installed on the machine. Nothing was written."
            )
        if not chosen_backend:
            # A watch runs nothing, so there is no trial to start and nothing to
            # ask for here. Requiring it produced exactly the fiction it was
            # meant to prevent: measured 2026-08-21, a campaign watching a feed
            # was refused for having no command and declared
            # ``echo "condition watch - no trial to run"`` to get past this line.
            # What such a campaign does instead of running a trial is its actions
            # table, and that is checked where an action is taken.
            chosen_backend = "process"

        inside = _round_dir_inside_case(remote_dir, staged_case)
        if inside:
            # Measured 2026-08-19: an arm declared remote_dir as
            # "<staged_case>/runs" and left job.inp and config.json inside the
            # owner's case. Two things go wrong, and the second is the worse one:
            # the case stops being read-only, and the tree of links each round is
            # built from then contains the round directories themselves, so every
            # round links in the one before it.
            #
            # The write-set probe does not catch this. It asks which FILES in the
            # case changed, and nothing here changes a file -- a new directory
            # appears beside them.
            return (
                f"REFUSED: rounds would be written inside the owner's case.\n"
                f"  case   {inside[0]}\n"
                f"  rounds {inside[1]}\n"
                f"Each round gets a directory filled with links to the case, so a round "
                f"directory inside it would be linked into the next round, and the case "
                f"would stop being something you only read. Put rounds beside the case or "
                f"anywhere else writable. Nothing was written."
            )
        # Only what this shape has. An absent metric is what tells every later
        # reader there is no ranking to do -- writing an empty one would leave
        # them looking for a number the campaign never had.
        target: dict[str, Any] = {"kind": kind}
        if metric:
            target["metric"] = metric
        if goal in ("max", "min"):
            target["direction"] = goal
        if condition.strip():
            target["condition"] = condition.strip()
        meta: dict[str, Any] = {
            "backend": chosen_backend,
            "connection": connection,
            "objective": target,
            "objective_words": objective,
            "max_rounds": int(max_rounds),
        }
        if command:
            meta["command"] = command
        if staged_case:
            meta["staged_case"] = staged_case.rstrip("/")
        if remote_dir:
            meta["remote_dir"] = remote_dir.rstrip("/")
        if image:
            meta["image"] = image
        if isinstance(seed_config, dict) and seed_config:
            meta["seed_config"] = seed_config
        if resources:
            meta["resources"] = resources
        table = ops_readings.merge(ops_readings.declared(previous or {}), readings)
        if table:
            meta["readings"] = table
        acts = ops_actions.merge(ops_actions.declared(previous or {}), actions)
        if acts:
            meta["actions"] = acts
        if budget_total is not None:
            try:
                total = float(budget_total)
            except (TypeError, ValueError):
                total = 0.0
            if total > 0:
                meta["budget"] = {
                    "unit": budget_unit or "unit",
                    "total": total,
                    "overlap": budget_overlap if budget_overlap in ("additive", "shared") else "shared",
                    "meter": budget_meter if budget_meter in _METERS else "compute",
                }

        # When the watch opened, which is what a wall-clock budget is spent
        # against. Carried across an amendment rather than restamped: getting the
        # preparation right on a second try is the same watch, and restamping
        # would hand back the time already spent.
        opened = str((previous or {}).get("declared_at") or "").strip()
        meta["declared_at"] = opened or datetime.now().isoformat(timespec="seconds")
        # The wake route is the loop's own bookkeeping (where this campaign's
        # wakes land, written by the scheduling tools); an amendment carries it
        # rather than dropping it, or a declared-again watch would strand its
        # pending wake with no route for the resident watcher to re-raise on.
        if isinstance((previous or {}).get("wake_route"), dict):
            meta.setdefault("wake_route", previous["wake_route"])
        if amending and not _only_adds_readings(previous, meta):
            blocked = _cannot_amend(cdir, name)
            if blocked:
                return blocked
        cdir.mkdir(parents=True, exist_ok=True)
        meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        if amending:
            # The failed rounds from before stay in the ledger, and they were run
            # under the old setup -- this event is the line between the two, so a
            # reader months later can tell which rounds belong to which.
            _log_amendment(cdir, previous, meta)
        # The one thing this tool runs, and the reason it may: a starting point
        # exists only if it is taken now. A wake turn is a cold start, so "down
        # 10% from where it started" is answerable against a record and nothing
        # else -- and by the time the loop notices it needs the number, the
        # world has moved. Reads only (the table refuses anything that changes
        # something), and a failure is reported rather than raised: a
        # declaration is still worth having with a starting point it could not
        # obtain, provided it says so.
        started = await self._take_baseline(meta, cdir)
        return self._readback(name, meta, cdir, started)

    @staticmethod
    async def _take_baseline(meta: dict[str, Any], cdir: Path) -> list[dict[str, Any]]:
        """Take the starting values that have not been taken yet. Never raises.

        Every each_wake reading is taken here as well as the at_declare ones, and
        recorded as the starting point, because that is what it is: the value at
        the moment the campaign was asked to watch. Leaving it to a later look
        would leave the loop to notice it needs the number, and by then the world
        has moved. It also removes the only sensible reason to declare the same
        quantity twice -- which the table cannot hold, since one name is one row.
        """
        try:
            already = {
                str(row.get("name")) for row in ops_readings.read(cdir) if row.get("when") == ops_readings.AT_DECLARE
            }
            due = [
                r
                for r in ops_readings.declared(meta)
                if r.when in (ops_readings.AT_DECLARE, ops_readings.EACH_WAKE) and r.name not in already
            ]
            if not due:
                return []
            return await ops_readings.take(
                # Taken as, and recorded as, the starting point -- whatever the
                # row says about when it is taken from here on.
                {
                    **meta,
                    "readings": [{"name": r.name, "command": r.command, "when": ops_readings.AT_DECLARE} for r in due],
                },
                cdir,
                ops_readings.AT_DECLARE,
            )
        except Exception:  # noqa: BLE001 -- the declaration must not fail over a reading
            return []

    @staticmethod
    def _readback(name: str, meta: dict[str, Any], cdir: Path, started: list[dict[str, Any]] | None = None) -> str:
        """The declaration in the owner's terms, plus what is not settled.

        Printed rather than a bare acknowledgement because this is the last point
        before compute is spent, and every line of it is something the owner can
        check in seconds -- which machine, which case, the command, the starting
        values. What is missing is printed too: a budget nobody set is a fact about
        the experiment, not a gap to fill with a default.
        """
        obj = meta["objective"]
        lines = [
            f"Declared '{name}'. Nothing has run yet and no compute has been spent.",
            f"  machine      {display_name_or_id(meta['connection'])}",
        ]
        if meta.get("staged_case"):
            lines.append(f"  case         {meta['staged_case']}  (read, never written)")
        watching = str(obj.get("kind")) == CONDITION
        # The same field, and it means a different thing on a watch: not how a
        # trial starts but what to do once the condition holds. Written down here
        # rather than composed at the moment of acting, so an order that says buy
        # cannot become one that says sell without the owner having seen it.
        starts = meta.get("command") or meta.get("image")
        if starts:
            lines.append(f"  {'acts with' if watching else 'starts with'}    {starts}")
        elif watching:
            # A watch with no action declared is a legitimate campaign -- half of
            # them only have to tell the owner -- so this states the fact rather
            # than printing None, and says where an action would go if one is
            # needed when the condition holds.
            lines.append(
                "  acts with    nothing -- this campaign only watches and reports. If something "
                "has to be DONE when the condition holds, declare it again with an actions table."
            )
        if meta.get("resources"):
            lines.append(f"  holds        {_resources_words(meta['resources'])} per job")
        if _device_key_note(meta):
            lines.append(f"  note         {_device_key_note(meta)}")
        lines.extend(_target_lines(obj, meta.get("objective_words") or ""))
        seed = meta.get("seed_config")
        if seed:
            shown = " ".join(f"{k}={v}" for k, v in sorted(seed.items()))
            lines.append(f"  round 0 runs {shown}")
        elif not watching:
            # A watch has no round 0 to seed: what it starts from is the reading
            # it takes now, and that line would send it looking for a config.
            lines.append(
                "  round 0 runs whatever you submit -- no starting point was declared, so "
                "nothing can show a later round changed something on purpose"
            )
        budget = meta.get("budget")
        if budget:
            meter = str(budget.get("meter") or COMPUTE)
            how = {
                COMPUTE: f"machine time, {budget['overlap']}",
                WALL_CLOCK: "how long the watch stays open",
                LOOKS: "one per time you come back and read",
            }.get(meter, meter)
            lines.append(f"  budget       {budget['total']:g} {budget['unit']} ({how})")
        else:
            lines.append(
                "  budget       none -- nothing will stop this on the total. Report what a "
                "round costs once you know it, so the owner can set one if they want."
            )
        lines.append(f"  stops after  {meta['max_rounds']} rounds")
        lines.extend(_action_lines(meta))
        lines.extend(_reading_lines(meta, started or []))
        # What to do next differs by shape: a condition campaign's first move is
        # to look, not to spend a round. Saying "submit round 0" to one of those
        # names a step it has no reason to take.
        nxt = (
            f"Read what you are watching, then arrange the next look with ops_check_later(campaign='{name}', ...)."
            if str(obj.get("kind")) == CONDITION
            else f"Submit round 0 with ops_submit(campaign='{name}', round=0, configs=[...])."
        )
        lines.append(
            f"{nxt} "
            f"To change any of the above, it has to be now: from the first submit on, this file "
            f"is what the campaign is checked against and rewriting it is refused."
        )
        return "\n".join(lines)


def _action_lines(meta: dict[str, Any]) -> list[str]:
    """The action table, verbatim. This is the list the owner is being shown.

    Printed in full, command and all, because that is the whole point of having
    declared it: the line that will be sent is on screen now, before anything can
    send it, rather than assembled on some later wake with nobody watching.
    """
    table = ops_actions.declared(meta)
    if not table:
        return []
    out = ["  may do"]
    for action in sorted(table, key=lambda a: a.name):
        repeat = (
            "doing it twice does it twice -- the second identical call is refused"
            if action.repeat_is_harmful
            else "safe to repeat"
        )
        out.append(f"    {action.name:<20} {action.command}")
        out.append(f"    {'':<20} ({repeat})")
    return out


def _reading_lines(meta: dict[str, Any], started: list[dict[str, Any]]) -> list[str]:
    """The readings table, and whatever the starting ones just came back with.

    The starting value is printed as it was read, with no comparison drawn: what
    it means for the value to be 248.42 is the reading the loop is here to do. A
    reading that failed is printed too, because a missing starting point is the
    one thing that quietly makes a relative claim unanswerable later.
    """
    table = ops_readings.declared(meta)
    if not table:
        return [
            "  reads        nothing -- no readings declared, so nothing but the trial's own "
            "metrics will be on the record. Declare again to add some."
        ]
    out = ["  reads"]
    taken = {str(row.get("name")): row for row in started}
    for r in sorted(table, key=lambda x: (x.when, x.name)):
        note = ""
        row = taken.get(r.name)
        if row is not None:
            note = f"  -> now {row['value']!r}" if "value" in row else f"  -> COULD NOT READ: {row.get('error')}"
        out.append(f"    {r.name:<20} {r.when:<13} {r.command}{note}")
    return out


def _target_lines(obj: dict[str, Any], words: str) -> list[str]:
    """The target as the owner can check it, in the shape it was declared in.

    The warning at the end is the second net under an easy mistake, and it is
    only a warning: a task can say "as fast as possible" about a run that still
    has to finish before speed means anything, and refusing that would be worse
    than printing the two side by side and letting whoever reads decide.
    """
    kind = str(obj.get("kind") or "")
    out: list[str] = []
    if kind == OPTIMIZE:
        out.append(f"  target       {obj.get('metric')}, as {obj.get('direction')} as possible")
    elif kind == CONDITION:
        out.append(f"  watching for {obj.get('condition')}")
        out.append("  target       act when that holds; until then, looking is the work")
    else:
        out.append("  target       run it to its own end and let the result hold up")
        if obj.get("condition"):
            out.append(f"  finished when {obj['condition']}")
        if obj.get("metric"):
            out.append(
                f"  also reading  {obj['metric']}, as {obj.get('direction') or 'declared'} as "
                f"possible -- recorded, not what says the work is done"
            )
    if kind != OPTIMIZE:
        pushing = _optimising_words(words)
        if pushing:
            out.append(
                f"  NOTE         the objective says {', '.join(sorted(set(pushing)))!s}, which "
                f"reads like a number to push as far as it goes. If there is one the run "
                f"reports, declare optimize instead -- this campaign will not rank its rounds."
            )
    return out


def display_name_or_id(conn_id: str) -> str:
    from oncall_flow.connections import display_name

    return display_name(conn_id) or conn_id
