# Raven Benchmarks

This directory holds **evaluation harnesses** that are deliberately decoupled
from the runtime package. They are not imported by `raven/` and are
excluded from the wheel build — keep it that way.

Use this area for reproducible evaluation work: capability suites, agent
comparisons, context stress tests, and proactivity runs that should not ship as
part of the end-user CLI package.

## Layout

```
benchmarks/
├── appworld/           AppWorld agent benchmark + evolver plugin
│   ├── agent_cli.py       One-task subject agent (drives AgentLoop)
│   ├── batch.py           Batch scorer: N tasks x K trials, resumable
│   └── evolve/            raven.evolver BenchBundle plugin (entry.py)
│                          + designer/diagnosis/sandbox/precheck glue
│
├── pinchbench/         Context / AgentLoop capability benchmark
│   ├── tasks/             23 task_*.md cards (YAML frontmatter + sections)
│   ├── direct/            Drives AgentLoop.process_direct() per task
│   ├── bot_runner/        Drives full gateway + channel path per task
│   ├── assets/            Task-specific workspace files
│   └── results/           Run outputs (gitignored)
│
├── clawbench/          ClawBench streaming benchmark adapter
│   ├── stream.py          Drives AgentLoop.process_direct() across one session
│   ├── run.sh             Shell wrapper
│   └── README.md          Setup and run instructions
│
├── proactivity_eval/   Sentinel / Proactive Engine benchmark
│   ├── data/              5 sub-benchmarks: cases / pbench / longrun /
│   │                      simulation / timeline + reward_data
│   └── runners/           run.py (unified entry) + judge.py +
│                          *_scorecard.py + per-agent adapters
│
└── README.md           This file
```

`skill_evals/` will land here in Phase 1 once `raven/skill_forge/evals/`
is folded into `memory_engine/skill/`.

## Running

### Model and tool configuration

The benchmark runners can use the normal `~/.raven/config.json`, or the
environment overrides below. Never commit real keys.

For an OpenAI-compatible gateway using OpenRouter-style environment names:

```bash
export OPENROUTER_API_KEY="..."
export OPENROUTER_API_BASE="https://openrouter.ai/api/v1"
export RAVEN_BENCH_PROVIDER="custom"
export RAVEN_BENCH_MODEL="deepseek-v4-flash"
```

Optional web tools:

```bash
export SERPER_API_KEY="..."
export JINA_API_KEY="..."
```

Equivalent `~/.raven/config.json`:

```json
{
  "agents": {
    "defaults": {
      "provider": "custom",
      "model": "deepseek-v4-flash",
      "maxToolIterations": 40,
      "contextWindowTokens": 65536
    }
  },
  "providers": {
    "custom": {
      "apiKey": "YOUR_API_KEY",
      "apiBase": "YOUR_OPENAI_COMPATIBLE_API_BASE"
    }
  },
  "tools": {
    "web": {
      "jinaApiKey": "YOUR_JINA_KEY",
      "search": {
        "apiKey": "YOUR_SERPER_KEY"
      }
    }
  }
}
```

PinchBench (Direct mode):
```bash
./benchmarks/pinchbench/direct/run.sh \
    --model deepseek-v4-flash \
    --provider custom \
    --api-base "$OPENROUTER_API_BASE" \
    --api-key "$OPENROUTER_API_KEY" \
    --suite task_00_sanity
```

PinchBench (Bot mode):
```bash
./benchmarks/pinchbench/bot_runner/run.sh --suite automated-only
```

ClawBench (first 80 tasks, one streaming session):
```bash
git clone https://github.com/claw-bench/claw-bench ../claw-bench
export CLAW_BENCH_ROOT="$PWD/../claw-bench"

./benchmarks/clawbench/run.sh \
    --clawbench-root "$CLAW_BENCH_ROOT" \
    --limit 80 \
    --session-id clawbench-stream-raven-80 \
    --max-iterations 40
```

ClawBench with Curator context engine:
```bash
./benchmarks/clawbench/run.sh \
    --clawbench-root "$CLAW_BENCH_ROOT" \
    --limit 80 \
    --session-id clawbench-stream-raven-curator-80 \
    --context-engine curator \
    --curator-model deepseek-v4-flash \
    --max-iterations 40
```

Proactivity Eval (longrun, 30-day persona):
```bash
uv run python benchmarks/proactivity_eval/runners/run.py \
    --agent raven --mode sentinel --benchmark longrun \
    --persona dev-01
```

## Relation to runtime

The runtime (`raven/`) **never statically imports from `benchmarks/`** — this
is the "independent eval track" principle. The reverse is allowed and
expected: benchmarks import `raven.agent`, `raven.providers`, etc. directly.

One scoped exception: `raven.evolver` loads its bench *plugins* from here by
registry name at launch (`benchmarks.appworld.evolve.entry:build`), inserting
the subject repo root on `sys.path` first. It is lazy, opt-in, and only works
from a repo checkout — evolution needs the git repo as its subject anyway, so
nothing in the installed wheel depends on this directory.

Two unit tests under `tests/` reach into `benchmarks/proactivity_eval/runners/`
via `sys.path` injection because they exercise the synthesizer / prompt-loader
code that lives there (it is benchmark code, not runtime code, and there is
no production substitute to test against).
