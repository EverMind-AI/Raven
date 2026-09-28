"""Run one experimental task with a person conversing and judging each round."""

import argparse
import asyncio
import tempfile
from pathlib import Path

from raven.config.raven import load_raven_config
from raven.core.config_stack import load_runtime_config
from raven.providers.factory import make_lazy_provider

from ..curator.generation.run import GenerationError
from ..curator.harness import Task
from ..curator.raven_adapter.inspection import Baseline
from ..curator.raven_adapter.worker import Worker, WorkerError
from .conversation import Conversation
from .exchange import ExchangeError
from .human import DONE, Human
from .run import Limits, run


async def main(args):
    config = load_runtime_config(str(args.config), str(args.home) if args.home else None)
    extensions = load_raven_config(args.config)
    root = args.state_dir or Path(tempfile.mkdtemp(prefix="raven-iteration-task-"))
    if root.exists() and any(root.iterdir()):
        raise ValueError("state-dir must be a new or empty task directory")
    baseline = Baseline(config, extensions, args.workdir, task=Task(text=args.task))
    curator_config = config.model_copy(deep=True)
    if args.curator_model:
        curator_config.agents.defaults.model = args.curator_model
        curator_config.agents.defaults.provider = "auto"
    provider = make_lazy_provider(curator_config)
    human = Human()
    async with Worker(baseline, root, timeout=args.timeout) as worker:
        print(f"Task records: {root}")
        print(f"Talk to the assistant; {DONE} ends a conversation, then leave feedback empty to finish.")
        try:
            await run(
                worker,
                provider,
                [Conversation(human, max_turns=args.turns)],
                [human],
                model=args.analyst_model,
                curator_model=args.curator_model,
                limits=Limits(max_rounds=args.rounds),
            )
        except (GenerationError, ExchangeError, WorkerError, ValueError) as exc:
            print(f"Could not complete this operation: {exc}")


def cli():
    parser = argparse.ArgumentParser(description="Iterate an experimental Curator task with a person in the loop.")
    parser.add_argument("--config", type=Path, required=True, help="Raven JSON configuration")
    parser.add_argument("--task", required=True, help="Current task goal")
    parser.add_argument("--workdir", type=Path, default=Path.cwd())
    parser.add_argument("--home", type=Path, help="Override the agent home")
    parser.add_argument("--state-dir", type=Path, help="New task directory; defaults to a temporary directory")
    parser.add_argument("--curator-model", help="Override the model used for curation")
    parser.add_argument("--analyst-model", help="Override the model used for analysis")
    parser.add_argument("--rounds", type=int, default=5, help="Maximum feedback rounds")
    parser.add_argument("--turns", type=int, default=20, help="Maximum messages per conversation")
    parser.add_argument("--timeout", type=float, default=120, help="Timeout for each worker operation in seconds")
    args = parser.parse_args()
    args.config = args.config.expanduser().resolve()
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    cli()
