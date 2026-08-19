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
from pathlib import Path
from typing import Any

from raven.agent.tools.base import Tool

_DEFAULT_MAX_ROUNDS = 8


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
                                   "their task. Read, never written: each trial works in its own "
                                   "directory of links to it.",
                },
                "command": {
                    "type": "string",
                    "description": "The one command that runs the case once, as typed on the machine. "
                                   "'{job_dir}' is the trial's working directory, '{config}' its "
                                   "config.json. Read the case's entry script with ops_exec.",
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
                    "description": f"Round count to stop at (default {_DEFAULT_MAX_ROUNDS}). A "
                                   f"backstop, not a plan.",
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
        chosen_backend = backend or ("process" if command else "")
        if chosen_backend in ("process", "openfoam") and not command:
            return (
                f"REFUSED: backend={chosen_backend!r} runs a command on the machine, and none was "
                f"given, so there is nothing to run.\n"
                f"Look at the case with ops_exec -- its entry script says how it starts -- and "
                f"pass that line as 'command'. Nothing was written."
            )
        if not chosen_backend:
            return (
                "REFUSED: this experiment does not say how a trial starts.\n"
                "Pass 'command': the one line that runs the owner's case once, the way it would be "
                "typed on that machine. Read the case with ops_exec first if you do not know it.\n"
                "Only pass backend='docker' if the work really is a container image rather than "
                "something installed on the machine. Nothing was written."
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
