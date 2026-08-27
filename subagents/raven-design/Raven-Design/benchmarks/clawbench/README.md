# ClawBench Streaming Runner

This directory contains an Raven-only ClawBench runner. It executes tasks
sequentially in one persistent Raven session:

1. prepare task 1 workspace;
2. call `AgentLoop.process_direct()`;
3. grade the workspace with ClawBench's verifier;
4. repeat for task 2 without clearing the Raven session.

The ClawBench dataset is not vendored here. Clone it separately and point the
runner at the checkout.

## Setup

```bash
git clone https://github.com/claw-bench/claw-bench ../claw-bench
export CLAW_BENCH_ROOT="$PWD/../claw-bench"
```

Install Raven normally, then configure a model. The benchmark runners use
OpenRouter-style environment names for any OpenAI-compatible gateway:

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

You can also put the same values in `~/.raven/config.json`:

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

Do not commit real keys.

## Run

Smoke test one task:

```bash
./benchmarks/clawbench/run.sh \
  --clawbench-root "$CLAW_BENCH_ROOT" \
  --task cal-001 \
  --session-id clawbench-smoke
```

Run the first 80 tasks as one streaming session:

```bash
./benchmarks/clawbench/run.sh \
  --clawbench-root "$CLAW_BENCH_ROOT" \
  --limit 80 \
  --session-id clawbench-stream-raven-80 \
  --max-iterations 40
```

Run with Curator enabled:

```bash
./benchmarks/clawbench/run.sh \
  --clawbench-root "$CLAW_BENCH_ROOT" \
  --limit 80 \
  --session-id clawbench-stream-raven-curator-80 \
  --context-engine curator \
  --curator-model deepseek-v4-flash \
  --max-iterations 40
```

Useful filters:

```bash
./benchmarks/clawbench/run.sh --domain data-analysis --limit 5
./benchmarks/clawbench/run.sh --level L4 --limit 3
./benchmarks/clawbench/run.sh --task cal-001,code-001,xdom-014
```

## Outputs

By default outputs are written under `benchmarks/clawbench/results/`:

- `run_<timestamp>/workspaces/` — per-task workspaces;
- `run_<timestamp>/transcripts/` — prompts, final responses, errors;
- `run_<timestamp>/partial_results.json` — updated after each task;
- `raven_clawbench_stream_<timestamp>.json` — final summary;
- `raven_clawbench_stream_<timestamp>.tokens.csv` — per-task token records;
- `results.md` — live markdown table.

Token columns use provider-reported `response.usage`. `context_used` is the
final model call's prompt plus completion tokens for the task; it is not the
sum of all task tokens.
