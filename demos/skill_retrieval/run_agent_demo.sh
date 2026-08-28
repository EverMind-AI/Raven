#!/usr/bin/env bash
# Demo: feed a query into ``raven agent``, watch it discover the
# image-gen skill, follow its SKILL.md instructions, and write a PNG.
#
# Usage:
#   bash run_agent_demo.sh                              # default fox prompt
#   bash run_agent_demo.sh "render a cyberpunk skyline" # custom prompt
#
# Requires:
#   - OPENROUTER_API_KEY in env (or fallback: read from ../../raven/key.env)
#   - HTTPS_PROXY for OpenRouter access from China-mainland IPs
#     (the script defaults to the worker-internal proxy)

set -euo pipefail

DEMO_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$DEMO_DIR/../.." && pwd)"

# ── 1. resolve OpenRouter key ───────────────────────────────────────────
if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
    KEY_FILE="$REPO_ROOT/raven/key.env"
    if [[ -f "$KEY_FILE" ]]; then
        OPENROUTER_API_KEY="$(grep -v '^#' "$KEY_FILE" | head -1 | tr -d '\n')"
    fi
fi
if [[ -z "${OPENROUTER_API_KEY:-}" ]]; then
    echo "error: set OPENROUTER_API_KEY in env or write to raven/key.env" >&2
    exit 1
fi
export OPENROUTER_API_KEY

# ── 2. proxy for OpenRouter (skip if you're outside China-mainland) ────
: "${HTTPS_PROXY:=http://14.103.45.158:15002}"
: "${HTTP_PROXY:=$HTTPS_PROXY}"
export HTTPS_PROXY HTTP_PROXY

# ── 3. build a one-shot config that points the agent at this workspace ─
TMPCONFIG="$(mktemp)"
trap 'rm -f "$TMPCONFIG"' EXIT

# Inherit user defaults if a config already exists, otherwise start blank.
USER_CONFIG="$HOME/.raven/config.json"
if [[ -f "$USER_CONFIG" ]]; then
    cp "$USER_CONFIG" "$TMPCONFIG"
else
    echo '{}' > "$TMPCONFIG"
fi

python3 - "$TMPCONFIG" "$OPENROUTER_API_KEY" <<'PYINJECT'
import json, sys, pathlib
path, key = sys.argv[1], sys.argv[2]
data = json.loads(pathlib.Path(path).read_text() or "{}")
data.setdefault("agents", {}).setdefault("defaults", {})
data["agents"]["defaults"]["model"] = "openrouter/anthropic/claude-sonnet-4-5"
data["agents"]["defaults"]["provider"] = "openrouter"
data.setdefault("providers", {}).setdefault("openrouter", {})
data["providers"]["openrouter"]["apiKey"] = key
# Enable SkillForge so the dual-pool retrieval (LocalPool BM25 + optional
# mass-library dense) actually runs. Without this, ContextBuilder falls
# back to "render every skill's directory entry" — fine for the 10-skill
# demo workspace but defeats the point of showing skill-forge in action.
data["skill_forge"] = {
    "enabled": True,
    "topK": 5,
    # Reranker requires a 0.6B model + (typically) GPU; demo stays light.
    "rerankerEnabled": False,
}
pathlib.Path(path).write_text(json.dumps(data, indent=2))
PYINJECT

# ── 4. run the agent ───────────────────────────────────────────────────
PROMPT="${1:-Use the image-gen skill to generate a watercolor painting of a fox sitting in autumn leaves, save it to ./fox.png}"

cd "$REPO_ROOT"
exec uv run raven agent \
    --config "$TMPCONFIG" \
    --workspace "$DEMO_DIR" \
    --no-markdown \
    -m "$PROMPT"
