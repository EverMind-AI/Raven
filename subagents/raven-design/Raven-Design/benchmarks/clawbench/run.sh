#!/usr/bin/env bash
# ClawBench for Raven — streaming process_direct runner.
#
# Usage:
#   CLAW_BENCH_ROOT=/path/to/claw-bench ./benchmarks/clawbench/run.sh --limit 80

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$SCRIPT_DIR/../.."

PYTHON="${PYTHON:-python3}"

exec "$PYTHON" benchmarks/clawbench/stream.py "$@"
