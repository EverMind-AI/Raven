#!/bin/bash
# Launches the TUI in a demo-local raven home, so the machine registry starts
# empty -- the cold-start ask IS the demo. Your real ~/.raven is not touched.
set -euo pipefail
cd "$(dirname "$0")"
REPO_ROOT="$(cd ../.. && pwd)"

export RAVEN_HOME="$PWD/.raven-demo-home"
mkdir -p "$RAVEN_HOME"

# The demo home needs a provider key but must NOT inherit a machine registry:
# an inherited connections.json would skip the ask this demo exists to show.
if [ ! -f "$RAVEN_HOME/config.json" ]; then
  for src in "${RAVEN_CONFIG_SOURCE:-}" "$HOME/.raven/config.json"; do
    [ -n "$src" ] && [ -f "$src" ] && cp "$src" "$RAVEN_HOME/config.json" && break
  done
  if [ ! -f "$RAVEN_HOME/config.json" ]; then
    echo "No config to seed the demo home with."
    echo "Either run 'uv run raven onboard' once with RAVEN_HOME=$RAVEN_HOME,"
    echo "or point RAVEN_CONFIG_SOURCE at a config.json that has a provider key."
    exit 1
  fi
fi
rm -f "$RAVEN_HOME/connections.json"

echo "Demo home: $RAVEN_HOME (registry empty on purpose)"
echo "Now paste the task from task.md into the TUI."
cd "$REPO_ROOT"
exec uv run raven tui
