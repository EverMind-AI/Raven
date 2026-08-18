#!/usr/bin/env bash
# One command to install the sub-agents in this directory.
#
# Per folder: build the checkout's venv, scaffold `.env` from its template, then
# register the entry in the host raven's config. Every step is idempotent, so
# re-running after filling in a key is the normal way to finish an install.
#
# Usage:
#   ./install.sh                    # every folder here that ships an install.py
#   ./install.sh raven-code ...     # only these
#   ./install.sh --dry-run          # report what each step would do, change nothing
#   ./install.sh --no-sync          # skip `uv sync` (the venvs are already built)
#   ./install.sh --config PATH      # write that config file instead of the host raven's
#
# Restart raven (or the gateway) afterwards: the write lands in the config file,
# and a running raven holds the roster it read at startup.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=0
SYNC=1
CONFIG=""
FOLDERS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --no-sync) SYNC=0 ;;
        # Absolute, because install.py is invoked with each folder as cwd: a
        # relative path would resolve inside the folder, once per folder.
        --config)
            case "${2:?--config needs a path}" in
                /*) CONFIG="$2" ;;
                *) CONFIG="$PWD/$2" ;;
            esac
            shift
            ;;
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

# CODE_API_KEY for raven-code, RESEARCH_API_KEY for raven-research: the folder
# name without its `raven-` prefix, upper-cased.
prefix_of() {
    printf '%s' "${1#raven-}" | tr '[:lower:]-' '[:upper:]_'
}

if [ "$SYNC" = 1 ] && [ "$DRY_RUN" = 0 ] && ! command -v uv > /dev/null; then
    echo "error: no \`uv\` on PATH; it builds the checkouts' venvs (or pass --no-sync)" >&2
    exit 1
fi

registered=()
needs_key=()
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

    prefix="$(prefix_of "$folder")"
    var="${prefix}_API_KEY"
    # The environment wins over the file, matching what the launcher reads.
    key="${!var-}"
    if [ -z "$key" ] && [ -f "$dir/.env" ]; then
        # First non-empty wins, matching env_value() in run.py and install.py. A
        # last-match read disagrees with them on a file that sets the key twice.
        key="$(sed -n "s/^[[:space:]]*${var}=//p" "$dir/.env" | grep -m1 '[^[:space:]]' | tr -d '[:space:]' || true)"
    fi
    if [ -z "$key" ]; then
        echo "   $var is empty - fill in $folder/.env, then re-run"
        needs_key+=("$folder")
        continue
    fi
    echo "   $var: set"

    args=()
    [ "$DRY_RUN" = 1 ] && args+=(--dry-run)
    [ -n "$CONFIG" ] && args+=(--config "$CONFIG")
    # ${args[@]+...} because expanding an empty array under `set -u` is an
    # unbound-variable error before bash 4.4, which is the stock /bin/bash on macOS.
    if ! output="$(cd "$dir" && python3 install.py ${args[@]+"${args[@]}"})"; then
        failed+=("$folder")
        continue
    fi
    # The entry itself is the first thing install.py prints; the caller wants the
    # verdict, and `--dry-run` has nothing else to say.
    printf '%s\n' "$output" | sed -n '/^}/,$p' | tail -n +2 | sed 's/^/   /'
    registered+=("$folder")
done

echo "== summary"
[ ${#registered[@]} -gt 0 ] && echo "   done:       ${registered[*]}"
[ ${#needs_key[@]} -gt 0 ] && echo "   needs a key: ${needs_key[*]}"
[ ${#failed[@]} -gt 0 ] && echo "   failed:      ${failed[*]}"
if [ ${#registered[@]} -gt 0 ] && [ "$DRY_RUN" = 0 ]; then
    echo "   restart raven (or the gateway) to pick these up"
fi
[ ${#needs_key[@]} -eq 0 ] && [ ${#failed[@]} -eq 0 ]
