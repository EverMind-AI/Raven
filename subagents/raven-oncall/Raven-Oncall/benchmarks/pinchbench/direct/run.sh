#!/usr/bin/env bash
# PinchBench for Raven — Direct Mode (process_direct) runner
#
# Usage:
#   ./benchmarks/pinchbench/direct/run.sh                                          # Run all tasks
#   ./benchmarks/pinchbench/direct/run.sh --model deepseek-v4-flash                # Specify model
#   ./benchmarks/pinchbench/direct/run.sh --provider custom --api-base "$OPENROUTER_API_BASE"
#   ./benchmarks/pinchbench/direct/run.sh --suite task_00_sanity                   # Single task
#   ./benchmarks/pinchbench/direct/run.sh --suite automated-only                   # Automated-only
#   ./benchmarks/pinchbench/direct/run.sh --verbose                                # Verbose output

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# cd to project root: direct -> pinchbench -> benchmarks -> project root
cd "$SCRIPT_DIR/../../.."

echo "=================================================="
echo "  PinchBench for Raven (DIRECT MODE)"
echo "=================================================="

# Use anaconda Python 3.13 (Raven requires >=3.11)
PYTHON="${PYTHON:-$HOME/anaconda3/bin/python3}"
if [ ! -x "$PYTHON" ]; then
    PYTHON="python3"
fi

# Ensure yaml is available
"$PYTHON" -c "import yaml" 2>/dev/null || "$PYTHON" -m pip install pyyaml -q

exec "$PYTHON" benchmarks/pinchbench/direct/benchmark.py "$@"
