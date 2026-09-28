"""Run the travel-agency cultivation: each round the agency plays customers from its drill cards against the
digital employee, reviews the drills and speaks to the Curator, and the loop curates the employee's Harness. With
`--analysis analyst` (the default) the owner only speaks and the base Analyst turns its words into the Curator's
requirements; with `--analysis owner` the agency is the round's Analyst (see `experimental.simulation.agency`).

The owner knows all of the scenario's materials from the start and has handed every one over by the review before the
last round; what varies is the partition of the materials into steps, which is how cultivation runs are told apart and
what mining searches over. A chain names one such plan (all at once, as the owner decides, or a scenario's named
partition), and `--partition` gives any other as JSON. The run's settings, with its command line but without any
credential or configuration path, are written to `settings.json` beside the records.

Each round plays every drill card with freshly drawn values (see `experimental.simulation.cards`) from `--seed`, so a
rule counts as held only on values the employee has not met before; arms given one seed meet the same values.
`--without` leaves materials out of the scenario altogether, for arms that differ only in what the agency owns.
Every drill runs on its own replica of the employee, made from it as the round begins and kept under `replicas/`
beside the records (see `experimental.simulation.employee.Together`), so no drill sees another's files;
`--concurrent-drills` plays that many of a round's drills at once.

The agency copies the scenario's materials into the employee's skill pool as the disclosure plan releases them;
directories there with a scenario material's name are owned by the scenario and reset at the start of a run.
"""

import argparse
import asyncio
import hashlib
import json
import random
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from raven.config.mode_catalogue import build_mode_catalogue
from raven.providers.factory import make_lazy_provider, make_resolving_provider

from ..analyst.run import Limits as AnalystLimits
from ..curator.generation.run import GenerationError
from ..curator.generation.run import Limits as CuratorLimits
from ..curator.raven_adapter.worker import WorkerError
from ..iteration.conversation import Conversation
from ..iteration.exchange import ExchangeError
from ..iteration.run import Limits, run
from . import caching
from .agency import Agency
from .attribution import write as attribute_run
from .employee import REPLICAS, Fresh, Together, hire, housed_files, replicate, skills
from .record import PLACEHOLDERS, placed
from .scenario import Scenario
from .traveller import Traveller

EFFORTS = ("low", "medium", "high")
CHAINS = {
    "documents": {"deliver": "dialog", "disclose": "all"},
    "staged": {"deliver": "dialog", "disclose": "staged"},
    "by-stage": {"deliver": "dialog", "disclose": "by-stage"},
}


def baseline(home: Path | None) -> dict[str, str]:
    """The employee's starting agent home as a fingerprint: each file's path and SHA-256, past sessions left out."""
    if home is None:
        return {}
    return {
        path.relative_to(home).as_posix(): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(Path(home).rglob("*"))
        if path.is_file() and "sessions" not in path.relative_to(home).parts and path.name != ".lock"
    }


def _digest(folder: Path) -> str:
    files = sorted(path for path in folder.rglob("*") if path.is_file() and "__pycache__" not in path.parts)
    total = hashlib.sha256()
    for path in files:
        total.update(path.relative_to(folder).as_posix().encode())
        total.update(hashlib.sha256(path.read_bytes()).digest())
    return total.hexdigest()


def starting_harness(repository: Path) -> dict:
    """Which code every harness starts from: Raven's commit, any uncommitted change to it, and each Raven-X product.

    Whatever the Curator has not touched is the starting state, so a reader can check it was the same across runs.
    """

    def git(*argv):
        executable = shutil.which("git")
        if executable is None:
            return ""
        try:
            return subprocess.run(
                [executable, *argv], cwd=repository, capture_output=True, text=True, timeout=30
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            return ""

    agents = Path(repository) / "agents"
    return {
        "raven_commit": git("rev-parse", "HEAD") or None,
        "raven_modified": bool(git("status", "--porcelain", "--", "raven", "agents")),
        "raven_diff": hashlib.sha256(git("diff", "HEAD", "--", "raven", "agents").encode()).hexdigest(),
        "products": {folder.name: _digest(folder) for folder in sorted(agents.glob("raven-*")) if folder.is_dir()},
    }


def settings(args, employee_model: str, employee_effort=None, employee_tier=None) -> dict:
    """What a reader needs to know about how this run was set up; never a credential or a local config path.

    `argv` is the command line as given, with the configuration's path replaced: a reader reproduces the run from it.
    """
    return {
        "argv": placed(args.argv, {"--config": PLACEHOLDERS["--config"]}),
        "chain": args.chain,
        "scenario": Path(args.scenario).name,
        "deliver": args.deliver,
        "disclose": [list(step) for step in args.disclose] if isinstance(args.disclose, tuple) else args.disclose,
        "analysis": args.analysis,
        "rounds": args.rounds,
        "turns": args.turns,
        "repeats": args.repeats,
        "cards": args.cards,
        "concurrent_drills": args.concurrent_drills,
        "curator_budget": {
            "calls": getattr(args, "curator_calls", None),
            "queries": getattr(args, "curator_queries", None),
        },
        "seed": args.seed,
        "without": args.without,
        "models": {
            "employee": employee_model,
            "curator": args.curator_model or employee_model,
            "analyst": args.analyst_model or args.curator_model or employee_model,
            "simulation": args.simulation_model or args.curator_model or employee_model,
            "traveller": args.traveller_model or args.simulation_model or args.curator_model or employee_model,
            "subagents": args.subagent_model,
        },
        "efforts": {
            "employee": employee_effort,
            "employee_tier": employee_tier,
            "curator": args.curator_effort,
            "traveller": args.traveller_effort,
        },
        "baseline": baseline(args.home),
        "starting_harness": starting_harness(Path(__file__).resolve().parents[2]),
        "started": time.time(),
    }


def workplace(path: Path | None) -> Path:
    """The employee's working directory: a new empty folder, never the repository or anything holding it.

    Every drill's replica copies this folder whole, so whatever sits in it reaches the employee; the repository
    holds the scenario, its drill cards and the judges' tests.
    """
    if path is None:
        return Path(tempfile.mkdtemp(prefix="raven-simulation-workdir-"))
    path, repository = Path(path).resolve(), Path(__file__).resolve().parents[2]
    if path == repository or repository.is_relative_to(path) or path.is_relative_to(repository):
        raise ValueError(f"the employee's workdir may not be the repository, inside it or above it: {path}")
    if path.exists() and any(path.iterdir()):
        raise ValueError(f"the employee's workdir must be new or empty: {path}")
    path.mkdir(parents=True, exist_ok=True)
    return path


async def main(args):
    caching.install()
    scenario = Scenario.load(args.scenario)
    if args.without:
        scenario = scenario.without(args.without)
    root = args.state_dir or Path(tempfile.mkdtemp(prefix="raven-simulation-task-"))
    if root.exists() and any(root.iterdir()):
        raise ValueError("state-dir must be a new or empty task directory")
    if isinstance(args.disclose, str) and args.disclose not in ("all", "staged"):
        if args.disclose not in scenario.plans:
            raise ValueError(f"the scenario has no plan named {args.disclose}: {sorted(scenario.plans)}")
        args.disclose = scenario.plans[args.disclose]
    employee = hire(
        scenario,
        args.config,
        workdir=workplace(args.workdir),
        root=root,
        home=args.home,
        subagent_model=args.subagent_model,
        timeout=args.timeout,
    )
    hired = housed_files(employee.baseline.config.workspace_path)
    defaults = employee.baseline.config.agents.defaults
    if not defaults.reasoning_effort:
        raise ValueError("set agents.defaults.reasoningEffort explicitly; a provider default is not a run setting")
    tier = build_mode_catalogue(employee.baseline.config).default
    (root / "settings.json").write_text(
        json.dumps(settings(args, defaults.model, defaults.reasoning_effort, tier), ensure_ascii=False, indent=2)
    )
    model_config = employee.baseline.config.model_copy(deep=True)
    overridden = args.curator_model or args.analyst_model or args.simulation_model or args.traveller_model
    if overridden:
        model_config.agents.defaults.provider = "auto"
    if args.curator_model:
        model_config.agents.defaults.model = args.curator_model
    if args.curator_effort:
        model_config.agents.defaults.reasoning_effort = args.curator_effort
    # With a role on another model, each call's vendor follows its model id: the Curator and the owner can run on a
    # gateway while the customers stay on the employee's own vendor.
    provider = make_resolving_provider(model_config) if overridden else make_lazy_provider(model_config)
    travellers, placed = {}, {}
    analyst_limits = AnalystLimits(call_timeout=args.timeout, max_calls=8)
    agency = Agency(
        scenario,
        provider,
        skills(employee),
        workdir=employee.baseline.workdir,
        plan=args.disclose,
        deliver=args.deliver,
        rounds=args.rounds,
        uploads=employee.baseline.config.workspace_path / "uploads",
        shared=employee.baseline.workdir / "uploads",
        reply=lambda: employee.last_plan,
        cards=lambda: {name: traveller.card for name, traveller in travellers.items() if traveller.card},
        records=root,
        workdirs=placed,
        analysis=args.analysis,
        analyst_model=args.analyst_model or args.curator_model,
        analyst_limits=analyst_limits,
        model=args.simulation_model,
        timeout=args.timeout,
    )
    agency.prepare()
    cards = [persona for persona in scenario.personas if not args.cards or persona.name in args.cards]
    missing = set(args.cards or ()) - {persona.name for persona in scenario.personas}
    if missing:
        raise ValueError(f"the scenario has no drill cards named {sorted(missing)}")
    for persona in cards:
        for repeat in range(1, args.repeats + 1):
            traveller = Traveller(
                persona,
                provider,
                name=f"{persona.name}-{repeat}" if args.repeats > 1 else None,
                model=args.traveller_model or args.simulation_model,
                effort=args.traveller_effort,
                timeout=args.timeout,
                seed=args.seed,
            )
            travellers[traveller.name] = traveller

    def spawn(worker, label):
        return replicate(
            worker,
            args.config,
            root / REPLICAS / label,
            subagent_model=args.subagent_model,
            timeout=args.timeout,
        )

    named = [
        (name, Fresh(Conversation(traveller, max_turns=args.turns), hired)) for name, traveller in travellers.items()
    ]
    size = args.concurrent_drills
    trials = [Together(dict(named[start : start + size]), spawn, placed) for start in range(0, len(named), size)]
    async with employee:
        print(f"Task records: {root}")
        print(
            f"Scenario: {scenario.root.name}; chain: {args.chain}; disclosure: {args.disclose}; delivery: {args.deliver}; "
            f"analysis: {args.analysis}; cards: {', '.join(card.name for card in cards)}; skills: {skills(employee)}"
        )
        try:
            await run(
                employee,
                provider,
                trials,
                agency,
                opening=agency.opening(),
                curator_model=args.curator_model,
                limits=Limits(
                    max_rounds=args.rounds,
                    analyst=analyst_limits,
                    curator=CuratorLimits(
                        call_timeout=args.timeout, max_calls=args.curator_calls, max_queries=args.curator_queries
                    ),
                ),
            )
        except (GenerationError, ExchangeError, WorkerError, ValueError) as exc:
            print(f"Could not complete this operation: {exc}")
            for row in getattr(exc, "trace", ()):
                print(f"  {row}")
        try:
            attributed = await attribute_run(root, provider, model=args.simulation_model, timeout=args.timeout)
            print(f"Attributed {attributed['interventions']} intervention reasons to the owner's rules")
        except Exception as exc:  # noqa: BLE001 -- the judge falls back to markers without an attribution
            print(f"Could not attribute interventions: {exc!r}")


def cli():
    # Without abbreviations every option in the stored command line is spelled out, so its paths can be found.
    parser = argparse.ArgumentParser(
        description="Cultivate a digital employee: a simulated agency drills it, reviews it and speaks to the Curator.",
        allow_abbrev=False,
    )
    parser.add_argument("--config", type=Path, required=True, help="Raven JSON configuration")
    parser.add_argument(
        "--scenario", type=Path, default=Path("travel_agency"), help="Scenario directory or bundled name"
    )
    parser.add_argument(
        "--workdir", type=Path, help="The employee's working directory, new or empty; defaults to a temporary one"
    )
    parser.add_argument("--home", type=Path, help="Override the agent home; its skills folder receives the materials")
    parser.add_argument("--state-dir", type=Path, help="New task directory; defaults to a temporary directory")
    parser.add_argument(
        "--disclose",
        choices=("all", "staged"),
        default="staged",
        help="Hand every material over at onboarding, or the scenario's initial set first and the rest as the owner "
        "chooses after each round",
    )
    parser.add_argument("--curator-model", help="Override the model used for curation")
    parser.add_argument(
        "--analysis",
        choices=("owner", "analyst"),
        default="analyst",
        help="Who turns the owner's review into requirements: the base Analyst reading its words, or the owner itself",
    )
    parser.add_argument("--analyst-model", help="Override the base Analyst's model (with --analysis analyst)")
    parser.add_argument("--simulation-model", help="Override the model playing the agency and its customers")
    parser.add_argument("--traveller-model", help="Override the model playing the customers only")
    parser.add_argument(
        "--curator-calls",
        type=int,
        default=48,
        help="Model calls one curation may make, shared by the root and every child harness it revises",
    )
    parser.add_argument("--curator-queries", type=int, default=72, help="Exploration tool calls one curation may make")
    parser.add_argument(
        "--curator-effort",
        choices=EFFORTS,
        default="high",
        help="Reasoning effort of the Curator, the Analyst and the agency's review (the employee keeps its own)",
    )
    parser.add_argument(
        "--traveller-effort", choices=EFFORTS, default="low", help="Reasoning effort of the simulated customers"
    )
    parser.add_argument(
        "--subagent-model",
        help="Run the employee's Research and PPT subagents on this model, through the employee's own provider and "
        "key, instead of their own",
    )
    parser.add_argument("--rounds", type=int, default=4, help="Maximum feedback rounds")
    parser.add_argument("--turns", type=int, default=8, help="Maximum customer messages per drill")
    parser.add_argument("--repeats", type=int, default=1, help="Times each drill card is played per round")
    parser.add_argument(
        "--deliver",
        choices=("pool", "dialog"),
        default="dialog",
        help="Hand materials to the Curator through the conversation (uploads, then pasted text), or copy them "
        "straight into the employee's skill pool",
    )
    parser.add_argument("--cards", nargs="+", help="Drill cards to play each round; defaults to every card")
    parser.add_argument(
        "--concurrent-drills",
        type=int,
        default=1,
        help="Play this many of a round's drills at once (each drill always runs on its own replica of the employee)",
    )
    parser.add_argument(
        "--seed", type=int, help="Seed of the values drawn for the drill cards each round; drawn at random if absent"
    )
    parser.add_argument(
        "--without", nargs="+", help="Leave these materials out of the scenario, as if the agency never had them"
    )
    parser.add_argument(
        "--chain",
        choices=sorted(CHAINS),
        help="How the owner cultivates the employee; sets delivery and disclosure, overriding --deliver and --disclose",
    )
    parser.add_argument(
        "--partition",
        type=json.loads,
        help='A partition of the materials into steps as JSON, e.g. [["service-sop", "price-list"], ["handover-ticket"]]: '
        "step 0 at onboarding, step k with the review of round k; overrides --chain and --disclose",
    )
    parser.add_argument(
        "--timeout", type=float, default=180, help="Timeout for each worker or model operation in seconds"
    )
    args = parser.parse_args()
    args.argv = sys.argv[1:]
    args.config = args.config.expanduser().resolve()
    if args.concurrent_drills < 1:
        parser.error("--concurrent-drills must be at least 1")
    if args.seed is None:
        args.seed = random.randrange(2**31)
    if args.chain:
        args.deliver, args.disclose = CHAINS[args.chain]["deliver"], CHAINS[args.chain]["disclose"]
    if args.partition is not None:
        args.chain, args.deliver = "partition", "dialog"
        args.disclose = tuple(tuple(step) for step in args.partition)
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
