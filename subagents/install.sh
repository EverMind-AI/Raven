#!/usr/bin/env bash
# One command to build the sub-agents in this directory.
#
# Per folder: build the checkout's venv and scaffold `.env` from its template.
# Both steps are idempotent, so re-running is cheap.
#
# It does not register anything. Registration needs a configured host raven, and
# on a first install this script runs before one exists - so `raven` asks about
# each folder during onboarding and writes the entries there. Run this, then
# `raven`.
#
# Usage:
#   ./install.sh                    # every folder here that ships an install.py
#   ./install.sh raven-code ...     # only these
#   ./install.sh --dry-run          # report what each step would do, change nothing
#   ./install.sh --no-sync          # skip `uv sync` (the venvs are already built)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=0
SYNC=1
FOLDERS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --no-sync) SYNC=0 ;;
        -h | --help) sed -n '2,16p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
        -*) echo "unknown option: $1 (try --help)" >&2; exit 2 ;;
        *) FOLDERS+=("${1%/}") ;;
    esac
    shift
done

if [ ${#FOLDERS[@]} -eq 0 ]; then
    for candidate in "$HERE"/*/install.py; do
        [ -f "$candidate" ] && FOLDERS+=("$(basename "$(dirname "$candidate")")")
    done
fi
[ ${#FOLDERS[@]} -gt 0 ] || { echo "no sub-agent folder found in $HERE" >&2; exit 1; }

# The checkout is the one subdirectory that is a python project. Discovered
# rather than named, because the three folders spell it differently and a new
# folder is free to spell it a fourth way.
checkout_of() {
    local found=()
    for project in "$1"/*/pyproject.toml; do
        [ -f "$project" ] && found+=("$(dirname "$project")")
    done
    [ ${#found[@]} -eq 1 ] || return 1
    printf '%s\n' "${found[0]}"
}

# The LLM a folder is tuned for, plus a cross-check. `subagent.json` records it
# for a reader; `config.json` is what actually runs. Two files naming one model
# can drift, so they are compared here rather than trusted: a mismatch is
# reported, not silently preferred one way or the other.
recommendation_of() {
    python3 - "$1" <<'PYEOF'
import json, sys
from pathlib import Path
folder = Path(sys.argv[1])
try:
    rec = json.loads((folder / "subagent.json").read_text(encoding="utf-8")).get("recommendedLlm") or {}
    cfg = json.loads((folder / "config.json").read_text(encoding="utf-8"))
except (OSError, ValueError) as exc:
    print(f"unreadable ({exc.__class__.__name__})")
    sys.exit(0)
defaults = (cfg.get("agents") or {}).get("defaults") or {}
provider = defaults.get("provider")
runs = defaults.get("model")
base = ((cfg.get("providers") or {}).get(provider) or {}).get("apiBase") or ""
print(f"{rec.get('model', 'unrecorded')} via {rec.get('apiBase') or rec.get('provider') or '?'}")
if rec.get("model") and rec["model"] != runs:
    print(f"MISMATCH subagent.json recommends {rec['model']} but config.json runs {runs}")
elif rec.get("apiBase") and base and rec["apiBase"] != base:
    print(f"MISMATCH subagent.json recommends {rec['apiBase']} but config.json uses {base}")
PYEOF
}

if [ "$SYNC" = 1 ] && [ "$DRY_RUN" = 0 ] && ! command -v uv > /dev/null; then
    echo "error: no \`uv\` on PATH; it builds the checkouts' venvs (or pass --no-sync)" >&2
    exit 1
fi

ready=()
not_built=()
failed=()

for folder in "${FOLDERS[@]}"; do
    dir="$HERE/$folder"
    echo "== $folder"

    if [ ! -f "$dir/install.py" ]; then
        echo "   no install.py in $dir"
        failed+=("$folder")
        continue
    fi

    if ! checkout="$(checkout_of "$dir")"; then
        echo "   cannot tell which subdirectory is the checkout (looked for a lone pyproject.toml)"
        failed+=("$folder")
        continue
    fi

    if [ -x "$checkout/.venv/bin/raven" ]; then
        echo "   venv: present in $(basename "$checkout")"
    elif [ "$DRY_RUN" = 1 ] || [ "$SYNC" = 0 ]; then
        echo "   venv: MISSING in $(basename "$checkout") - the agent cannot run until \`uv sync\` builds it"
    fi
    if [ "$SYNC" = 1 ] && [ "$DRY_RUN" = 0 ]; then
        echo "   venv: uv sync in $(basename "$checkout")"
        if ! (cd "$checkout" && uv sync > /dev/null); then
            echo "   uv sync failed"
            failed+=("$folder")
            continue
        fi
    fi

    if [ ! -f "$dir/.env" ] && [ "$DRY_RUN" = 0 ]; then
        cp "$dir/.env.example" "$dir/.env"
        chmod 600 "$dir/.env"
        echo "   .env: created from the template"
    fi

    rec="$(recommendation_of "$dir")"
    echo "   recommended LLM: $(printf '%s' "$rec" | head -n1)"
    if mismatch="$(printf '%s' "$rec" | sed -n 's/^MISMATCH //p')" && [ -n "$mismatch" ]; then
        echo "   ! $mismatch - fix one of them"
    fi
    # A built venv is what makes the folder offerable: onboarding declines to
    # register one that cannot start.
    if [ -x "$checkout/.venv/bin/raven" ]; then
        ready+=("$folder")
    else
        not_built+=("$folder")
    fi
done

echo "== summary"
[ ${#ready[@]} -gt 0 ] && echo "   ready:     ${ready[*]}"
[ ${#not_built[@]} -gt 0 ] && echo "   not built: ${not_built[*]}"
[ ${#failed[@]} -gt 0 ] && echo "   failed:    ${failed[*]}"
if [ ${#ready[@]} -gt 0 ] && [ "$DRY_RUN" = 0 ]; then
    echo "   now run \`raven\` - onboarding asks about each of these and registers the ones you take up"
fi
[ ${#failed[@]} -eq 0 ]
