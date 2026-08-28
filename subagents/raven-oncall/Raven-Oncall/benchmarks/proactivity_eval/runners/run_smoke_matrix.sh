#!/usr/bin/env bash
# Run the full smoke matrix (2 systems × 2 modes × N=10) sequentially,
# then aggregate via pa_scorecard.py.
#
# Why sequential: LAN vLLM serves both Raven and Hermes adapters from
# the same endpoint; running in parallel can serialize at the server
# anyway and makes per-run cost estimation noisy.
#
# Usage (from repo root):
#   bash proactivity-eval/runners/run_smoke_matrix.sh [N]
# N defaults to 10.

set -euo pipefail

N="${1:-10}"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
cd "$REPO_ROOT"

OUT_DIR="${SMOKE_OUT_DIR:-proactivity-eval/output}"
mkdir -p "$OUT_DIR"

# Existing planner results (from earlier session) may already be here:
PLANNER_COLD="${PLANNER_COLD:-/tmp/pa-cold.json}"
PLANNER_WARM="${PLANNER_WARM:-/tmp/pa-warm.json}"

# Hermes source tree: optional override. If unset, adapters resolve via
# runners.config.yaml → systems.hermes_src → $HERMES_AGENT_SRC.
HERMES_SRC_FLAG=()
if [ -n "${HERMES_AGENT_SRC:-}" ]; then
    HERMES_SRC_FLAG=(--hermes-src "$HERMES_AGENT_SRC")
fi

echo "[smoke] N=$N"
echo "[smoke] output dir=$OUT_DIR"

run_ec() {
    local mode="$1"
    local out="$OUT_DIR/pa-ec-agent-${mode}-n${N}.json"
    echo "[smoke] ec agent $mode -> $out"
    uv run python proactivity-eval/runners/run.py --agent raven --benchmark pbench \
        --n "$N" --context-mode "$mode" --max-iter 200 --timeout 600 \
        --output "$out" 2>&1 | tee "${out%.json}.log" | tail -2
}

run_hermes() {
    local mode="$1"
    local out="$OUT_DIR/pa-hermes-agent-${mode}-n${N}.json"
    echo "[smoke] hermes agent $mode -> $out"
    uv run python proactivity-eval/runners/run.py --agent hermes --benchmark pbench \
        --n "$N" --context-mode "$mode" --timeout 900 \
        "${HERMES_SRC_FLAG[@]}" \
        --output "$out" 2>&1 | tee "${out%.json}.log" | tail -2
}

run_ec cold
run_ec warm
run_hermes cold
run_hermes warm

echo "[smoke] generating scorecard"
uv run python proactivity-eval/runners/pa_scorecard.py \
    --planner-cold "$PLANNER_COLD" \
    --planner-warm "$PLANNER_WARM" \
    --ec-agent-cold "$OUT_DIR/pa-ec-agent-cold-n${N}.json" \
    --ec-agent-warm "$OUT_DIR/pa-ec-agent-warm-n${N}.json" \
    --hermes-cold "$OUT_DIR/pa-hermes-agent-cold-n${N}.json" \
    --hermes-warm "$OUT_DIR/pa-hermes-agent-warm-n${N}.json" \
    --output "$OUT_DIR/pa-scorecard-smoke-n${N}.md"

echo "[smoke] done. scorecard:"
cat "$OUT_DIR/pa-scorecard-smoke-n${N}.md"
