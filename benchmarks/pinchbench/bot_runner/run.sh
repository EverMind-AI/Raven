#!/usr/bin/env bash
# PinchBench for Raven — Bot Mode runner
#
# This runner runs a full bot (AgentLoop run_turn) for each task,
# testing the complete turn flow through the spine.
#
# Usage:
#   ./benchmarks/pinchbench/bot_runner/run.sh                                    # Run all tasks
#   ./benchmarks/pinchbench/bot_runner/run.sh --model anthropic/claude-sonnet-4  # Specify model
#   ./benchmarks/pinchbench/bot_runner/run.sh --suite task_00_sanity             # Single task
#   ./benchmarks/pinchbench/bot_runner/run.sh --suite automated-only             # Automated-only
#   ./benchmarks/pinchbench/bot_runner/run.sh --verbose                          # Verbose output

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# cd to project root: bot_runner -> pinchbench -> benchmarks -> project root
cd "$SCRIPT_DIR/../../.."

echo "=================================================="
echo "  PinchBench for Raven (BOT MODE)"
echo "=================================================="

# Use anaconda Python 3.13 (Raven requires >=3.11)
PYTHON="${PYTHON:-$HOME/anaconda3/bin/python3}"
if [ ! -x "$PYTHON" ]; then
    PYTHON="python3"
fi

# Ensure yaml is available
"$PYTHON" -c "import yaml" 2>/dev/null || "$PYTHON" -m pip install pyyaml -q

exec "$PYTHON" benchmarks/pinchbench/bot_runner/benchmark.py "$@"
