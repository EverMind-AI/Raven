# Proactivity Eval Runners

Harness for driving two benchmarks against Raven / Hermes / OpenClaw:

- **pbench** — ProactiveAgent reward_data (one-shot help-or-skip decisions
  over 120 stratified records).
- **longrun** — 30-day LLM-simulator trajectories over 6 personas.

## Quick start

```bash
# pbench smoke (10 stratified records, cold context)
uv run python benchmarks/proactivity_eval/runners/run.py \
    --agent raven --benchmark pbench --n 10 --context-mode cold \
    --output benchmarks/proactivity_eval/output/pbench-smoke.json

# longrun single-persona smoke (1 simulated day)
uv run python benchmarks/proactivity_eval/runners/run.py \
    --agent raven --benchmark longrun --case parent-01 --day-limit 1

# pbench full set (120 records)
uv run python benchmarks/proactivity_eval/runners/run.py \
    --agent raven --benchmark pbench --n 120 --context-mode cold \
    --output benchmarks/proactivity_eval/output/pbench-n120.json
```

## Layout

- `run.py` — unified entry point; `--benchmark {pbench|longrun}`
- `_common/` — shared driver/backend/agent helpers
- `_common/drivers/{pbench,longrun}.py` — per-benchmark driver
- `agents/{raven,hermes,openclaw}/` — per-agent config + adapter glue
- `benchmarks/{pbench,longrun}/` — per-benchmark config
- `prompts/{raven,hermes,openclaw}_agent.yaml` — pbench system/user templates
- `pa_scorecard.py` — pbench aggregator (precision/recall/F1)
- `longrun_scorecard.py` — longrun aggregator (Type A/B/C rubrics)
- `generate_longrun_fixtures.py`, `render_longrun_usecase.py` — longrun tooling
- `retry_failed.py` — pbench retry helper
- `run_smoke_matrix.sh` — pbench smoke matrix (EC × OC × cold/warm)
- `run_longrun_layer1.sh` / `scorecard_longrun_layer1.py` — longrun layer-1 driver

## Config

- `runners.config.yaml` — `systems.{raven,hermes,openclaw}_src`, default
  provider/model. Copy to `runners.config.local.yaml` for personal
  overrides (gitignored).
- Per-agent YAML: `agents/<name>/<name>.yaml`
- Per-benchmark YAML: `benchmarks/<name>/<name>.yaml`
