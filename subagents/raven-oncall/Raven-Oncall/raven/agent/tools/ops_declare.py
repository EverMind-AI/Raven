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
from pathlib import Path
from typing import Any

from raven.agent.tools.base import Tool

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


class OpsDeclareTool(Tool):
    @property
    def name(self) -> str:
        return "ops_declare"

    @property
    def description(self) -> str:
        return (
            "Declare an experiment ONCE, before any of it runs: which machine, which case, "
            "how one trial starts, what is optimised, where it starts, what it may spend. "
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
                    "description": "What this experiment is for, in the owner's terms.",
                },
                "metric": {
                    "type": "string",
                    "description": "The one number rounds are judged by, e.g. 'residual'.",
                },
                "goal": {
                    "type": "string",
                    "enum": ["max", "min"],
                    "description": "Whether that number should be as large or as small as possible. "
                                   "Never guessed from the name: for a loss the best value is the "
                                   "smallest, so a guess hands over the worst trial.",
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
                                   "Read the entry script first with exec(machine=...).",
                },
                "backend": {
                    "type": "string",
                    "enum": ["process", "docker", "openfoam"],
                    "description": "'process' runs the command on the machine, which anything the "
                                   "owner installed needs; 'docker' runs an image holding none of "
                                   "their software. Defaults to 'process' with a command.",
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
                    "description": "Total compute this may spend. Only the owner knows it -- nothing "
                                   "in the case implies one. Leave out when they did not say: no "
                                   "budget is a real answer, not zero, and spend is reported anyway.",
                },
                "budget_unit": {
                    "type": "string",
                    "description": "Unit of budget_total, e.g. 'minute', 'gpu-minute'.",
                },
                "budget_overlap": {
                    "type": "string",
                    "enum": ["additive", "shared"],
                    "description": "'additive' when concurrent trials each spend the budget; 'shared' "
                                   "when they occupy one device and it counts once.",
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
                "remote_dir": {
                    "type": "string",
                    "description": "Where trial directories go: writable, and not inside the case.",
                },
                "image": {
                    "type": "string",
                    "description": "Container image, for backend='docker' only.",
                },
            },
            "required": ["campaign", "objective", "metric", "goal"],
        }

    async def execute(
        self,
        campaign: str,
        objective: str,
        metric: str,
        goal: str,
        connection: str = "",
        staged_case: str = "",
        command: str = "",
        backend: str = "",
        seed_config: dict | None = None,
        budget_total: float | None = None,
        budget_unit: str = "",
        budget_overlap: str = "",
        max_rounds: int = _DEFAULT_MAX_ROUNDS,
        remote_dir: str = "",
        image: str = "",
        **kwargs: Any,
    ) -> str:
        from raven.agent.tools.ops import _ops_home, _slug
        from raven.ops.connections import describe as conn_describe, get as conn_get

        name = str(campaign).strip()
        if not name:
            return "REFUSED: an experiment needs a name. Nothing was written."
        cdir = _ops_home() / _slug(name)
        meta_file = cdir / "meta.json"
        if meta_file.exists():
            # The declaration is what every later round is checked against, so a
            # second one is not an update: it would move the target under results
            # that were already recorded. The apparatus gate says the same thing
            # from the other side, and refuses the submit.
            return (
                f"REFUSED: '{name}' is already declared, and a declaration is what its rounds "
                f"are checked against -- rewriting it would move the target under results that "
                f"are already recorded.\n"
                f"Read it with ops_tune_status(campaign='{name}'). If something in it is wrong, "
                f"say so with ops_ask_owner rather than declaring over it. If this is different "
                f"work, give it a different name. Nothing was written."
            )
        if goal not in ("max", "min"):
            return (
                f"REFUSED: goal must be 'max' or 'min', not {goal!r}. Which direction is better "
                f"cannot be read off a metric's name, and getting it wrong hands over the worst "
                f"trial. Nothing was written."
            )
        if not connection:
            return (
                "REFUSED: this experiment has no machine to run on.\n"
                "Call ops_connections, pick the one the work calls for, and pass its id. It is "
                "the one thing a campaign cannot be given later.\n" + conn_describe()
            )
        if conn_get(connection) is None:
            return (
                f"REFUSED: there is no connection with id {connection!r}.\n{conn_describe()}"
            )
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
                f"Look at the case with exec(machine=...) -- its entry script says how it starts -- and "
                f"pass that line as 'command'. Nothing was written."
            )
        if not chosen_backend:
            return (
                "REFUSED: this experiment does not say how a trial starts.\n"
                "Pass 'command': the one line that runs the owner's case once, the way it would be "
                "typed on that machine. Read the case with exec(machine=...) first if you do not know it.\n"
                "Only pass backend='docker' if the work really is a container image rather than "
                "something installed on the machine. Nothing was written."
            )

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
        meta: dict[str, Any] = {
            "backend": chosen_backend,
            "connection": connection,
            "objective": {"metric": metric, "direction": goal},
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
                }

        cdir.mkdir(parents=True, exist_ok=True)
        meta_file.write_text(json.dumps(meta, indent=2), encoding="utf-8")
        return self._readback(name, meta, cdir)

    @staticmethod
    def _readback(name: str, meta: dict[str, Any], cdir: Path) -> str:
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
        lines.append(f"  starts with  {meta.get('command') or meta.get('image')}")
        lines.append(f"  target       {obj['metric']}, as {obj['direction']} as possible")
        seed = meta.get("seed_config")
        if seed:
            shown = " ".join(f"{k}={v}" for k, v in sorted(seed.items()))
            lines.append(f"  round 0 runs {shown}")
        else:
            lines.append(
                "  round 0 runs whatever you submit -- no starting point was declared, so "
                "nothing can show a later round changed something on purpose"
            )
        budget = meta.get("budget")
        if budget:
            lines.append(f"  budget       {budget['total']:g} {budget['unit']} ({budget['overlap']})")
        else:
            lines.append(
                "  budget       none -- nothing will stop this on the total. Report what a "
                "round costs once you know it, so the owner can set one if they want."
            )
        lines.append(f"  stops after  {meta['max_rounds']} rounds")
        lines.append(
            f"Submit round 0 with ops_submit(campaign='{name}', round=0, configs=[...]). "
            f"To change any of the above, it has to be now: from the first submit on, this file "
            f"is what the campaign is checked against and rewriting it is refused."
        )
        return "\n".join(lines)


def display_name_or_id(conn_id: str) -> str:
    from raven.ops.connections import display_name

    return display_name(conn_id) or conn_id
