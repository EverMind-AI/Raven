#!/usr/bin/env bash
# One command to build the sub-agents in this directory.
#
# Per folder: build the checkout's venv, scaffold `.env` from its template, and
# report the LLM it is tuned for and the EverOS identity it remembers under.
# Both writes are idempotent, so re-running is cheap.
#
# It registers nothing. A folder on disk is what puts an agent on the table, so
# `raven` discovers these on every start and onboarding only asks whose LLM each
# one spends. Run this, then `raven`.
#
# It does report the config rows that shadow a folder. An entry an older
# `install.py` wrote outranks the folder's own manifest, so a manifest a `git
# pull` updated never reaches the roster. `--prune-stale` deletes those entries
# after backing the previous list up beside this script. With no host raven to
# read the config through - a first install, before one exists - the whole step
# is skipped rather than failed.
#
# Usage:
#   ./install.sh                    # every folder here that ships an install.py
#   ./install.sh raven-code ...     # only these
#   ./install.sh --dry-run          # report what each step would do, change nothing
#   ./install.sh --no-sync          # skip `uv sync` (the venvs are already built)
#   ./install.sh --prune-stale      # also delete the config rows that shadow a folder
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DRY_RUN=0
SYNC=1
PRUNE_STALE=0
PRUNE_FAILED=0
FOLDERS=()

while [ $# -gt 0 ]; do
    case "$1" in
        --dry-run) DRY_RUN=1 ;;
        --no-sync) SYNC=0 ;;
        --prune-stale) PRUNE_STALE=1 ;;
        -h | --help) sed -n '2,24p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'; exit 0 ;;
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
# rather than named, because the folders that ship already spell it a different
# way each and a new folder is free to spell it its own.
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

# The identity a folder's memories are filed under, read from `memory` because that is
# the block every checkout agrees on. `plugins.config.everos-memory` may name it a
# second time, and what that copy does depends on the folder's vendored raven: some
# stamp stored messages from the slice and fall back to a shared `default` without it,
# newer ones ignore it entirely. Its presence is therefore not a defect and is not
# reported. A disagreement between the two is, because on the forks that stamp from the
# slice the agent stores under one id and recalls under the other -- silently, which is
# what makes every written memory unrecallable. A folder declaring no everos backend is
# reported and not failed: EverOS is optional, and a machine without one still installs.
everos_of() {
    python3 - "$1" <<'PYEOF'
import json, sys
from pathlib import Path
folder = Path(sys.argv[1])
try:
    cfg = json.loads((folder / "config.json").read_text(encoding="utf-8"))
except (OSError, ValueError) as exc:
    print(f"unreadable ({exc.__class__.__name__})")
    sys.exit(0)
memory = cfg.get("memory") or {}
plugin = ((cfg.get("plugins") or {}).get("config") or {}).get("everos-memory") or {}
# Every key here is optional and the schema fills all three: backend defaults to
# everos and both ids to "default". Saying nothing therefore does not mean "no
# memory", it means the host assistant's track -- so absent and null have to be
# told apart, and it is the resolved value, never the written one, that is
# reported. Only an explicit null (or another backend) switches everos off.
backend = memory.get("backend", "everos")
if backend != "everos":
    print(f"none - backend is {backend or 'null'}, so this folder keeps no everos memory")
    sys.exit(0)
user = memory.get("userId") or "default"
agent = memory.get("agentId") or "default"
print(f"{user}/{agent} via {plugin.get('base_url') or 'unrecorded'}")
shared = [label for label, value in (("userId", user), ("agentId", agent)) if value == "default"]
if shared:
    print(f"WARN memory names no {', '.join(shared)}, so this folder files under the shared 'default' track alongside the host assistant")
for label, resolved, slice_key in (("userId", user, "user_id"), ("agentId", agent, "agent_id")):
    if slice_key in plugin and plugin[slice_key] != resolved:
        print(f"WARN memory.{label} resolves to {resolved!r} but everos-memory.{slice_key} is {plugin[slice_key]!r}; stores and recalls would split")
PYEOF
}

if [ "$SYNC" = 1 ] && [ "$DRY_RUN" = 0 ] && ! command -v uv > /dev/null; then
    echo "error: no \`uv\` on PATH; it builds the checkouts' venvs (or pass --no-sync)" >&2
    exit 1
fi

ready=()
not_built=()
newly_built=()
# What `uv sync` changed, from the lines it writes for each package it touched.
# Read from a captured stream rather than left to scroll past: uv writes to
# stderr, which the `> /dev/null` below never suppressed, so a multi-folder run
# already printed its chatter interleaved and attributed to nothing.
delta_of() {
    awk '
        /^[[:space:]]*\+ / { added++ }
        /^[[:space:]]*- /  { removed++ }
        /^[[:space:]]*~ /  { updated++ }
        END {
            n = 0
            if (added)   { parts[n++] = added " added" }
            if (removed) { parts[n++] = removed " removed" }
            if (updated) { parts[n++] = updated " updated" }
            if (n == 0) { print "no change"; exit }
            out = parts[0]
            for (i = 1; i < n; i++) { out = out ", " parts[i] }
            print out
        }
    ' "$1"
}
extra_of() {
    # `ppt` for `raven-ppt`, when the checkout declares an optional-dependency
    # group by that name: the folder name without its `raven-` prefix, the same
    # derivation prefix_of uses for the .env variables. Nothing here enumerates
    # the folders, so a new one that needs an extra gets it by being named for it.
    #
    # Printed empty when the group does not exist. `uv sync --extra` on an
    # undeclared extra is an error, so guessing would turn a working install into
    # a failed one -- which is why this reads the section rather than the file.
    local folder="$1" checkout="$2" extra="${1#raven-}"
    [ -f "$checkout/pyproject.toml" ] || return 0
    awk -v want="$extra" '
        /^\[project\.optional-dependencies\]/ { inside = 1; next }
        /^\[/ { inside = 0 }
        inside && $0 ~ "^[[:space:]]*" want "[[:space:]]*=" { found = 1 }
        END { exit !found }
    ' "$checkout/pyproject.toml" && printf '%s' "$extra"
    return 0
}

# The interpreter that can import raven, resolved exactly as each folder's
# `install.py` resolves it: `$RAVEN_PYTHON`, else the shebang of `raven` on PATH.
# Failing here is the first-install case - no raven yet - and the caller turns
# that into a skipped step rather than an error.
host_interpreter() {
    if [ -n "${RAVEN_PYTHON:-}" ]; then
        printf '%s' "$RAVEN_PYTHON"
        return 0
    fi
    local script shebang
    script="$(command -v raven 2> /dev/null || true)"
    [ -n "$script" ] || return 1
    shebang="$(sed -n '1s/^#!//p' "$script" 2> /dev/null || true)"
    [ -n "$shebang" ] || return 1
    printf '%s' "$shebang"
}

# Runs under the host raven's interpreter, reading one JSON payload on stdin and
# writing the report on stdout. Kept in one `-c` string for the reason install.py
# gives for its own writer: a temp file would need cleaning up on every error path.
STALE_READER='
import json
import os
import sys
from pathlib import Path

from raven.config.loader import get_config_path, read_raw_or_raise
from raven.config.schema import ThirdPartyAcpSubagentConfig, ThirdPartyCliSubagentConfig
from raven.config.update_subagents import remove_agent

payload = json.load(sys.stdin)
path = get_config_path()
if not path.exists():
    sys.exit(0)

raw = read_raw_or_raise(path)
stored = raw.get("subagents") or {}
# All three spellings, because the key was renamed twice and a backup taken from
# the wrong one would be an empty list on a config that has already migrated.
rows = stored.get("agents") or stored.get("thirdParty") or stored.get("third_party") or []

# `{PYTHON}` resolves against this interpreter for the same reason
# vendored_agents._resolved_python does: the discovered row is materialized inside
# the host raven, which is the process this snippet runs in. Resolving it any other
# way would report a difference discovery does not see.
def as_row(entry):
    # The row an entry becomes on the table, or the entry as written when the
    # schema refuses it -- one bad row must not sink the report. Both sides go
    # through this, because comparing a manifest against a stored row directly
    # reports fields neither side can ever agree on: the schema drops
    # recommendedLlm (it annotates the installer and is not a config field), so
    # every row was listed as differing in it on every run, forever.
    # The `kind` the manifest declares picks the schema, the same way
    # vendored_agents.discover_vendored_rows does. Validating an acp entry as cli
    # is refused on the `kind` literal, so it fell back to the raw entry -- which
    # is exactly the "reports fields neither side can agree on" case above, and it
    # listed recommendedLlm and a literal-placeholder cwd for every acp folder.
    model = ThirdPartyAcpSubagentConfig if entry.get("kind") == "acp" else ThirdPartyCliSubagentConfig
    try:
        return model.model_validate(entry).model_dump(by_alias=True)
    except Exception:
        return entry

python = os.environ.get("SUBAGENT_PYTHON", "").strip() or sys.executable
wanted = {}
for folder, manifest in payload["folders"].items():
    entry = dict(manifest)
    # `cwd` too: it carries the placeholders on an acp manifest, and leaving it
    # unresolved reported the literal `{SUBAGENT_DIR}` as a difference from the
    # the real path in the stored row. Same three fields as _PLACEHOLDER_FIELDS.
    for field in ("command", "resumeCommand", "cwd"):
        template = entry.get(field)
        if template:
            entry[field] = str(template).replace("{SUBAGENT_DIR}", folder).replace("{PYTHON}", python)
    name = entry.get("name")
    if name:
        wanted[name] = as_row(entry)

colliding = [r for r in rows if isinstance(r, dict) and r.get("name") in wanted]
if not colliding:
    sys.exit(0)

out = ["== stale config rows (each one outranks the manifest of the folder it names)"]
for row in colliding:
    name = row["name"]
    # The manifest as declared, not as discovery would resolve it: discovery
    # computes enabled from whether the folder can start, so comparing against
    # that would report a difference for every folder whose venv is not built.
    stored = as_row(row)
    differs = sorted(k for k, v in wanted[name].items() if stored.get(k) != v)
    detail = "differs in: " + ", ".join(differs) if differs else "matches, and still outranks the manifest"
    out.append("   {:<16} {}".format(name, detail))
    if row.get("enabled") is False:
        out.append("   {:<16} ! enabled=false -- removing it would re-enable this agent".format(""))

mode = payload["mode"]
if mode == "report":
    out.append("   pass --prune-stale to delete them (the previous list is backed up first)")
    print("\n".join(out))
    sys.exit(0)
if mode == "dry-run":
    out.append("   --dry-run: --prune-stale would delete them")
    print("\n".join(out))
    sys.exit(0)

backup = Path(payload["backup"])
try:
    # Opened at 0600 rather than written and then chmodded: an openai row among
    # these carries its own api key, and write-then-chmod leaves that key on disk
    # world-readable for the length of the write. O_CREAT sets the mode only when
    # it creates, and this path can pre-exist (two runs in one second), so the
    # fchmod is what makes the mode true either way.
    fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(rows, handle, indent=2, ensure_ascii=False)
except OSError as exc:
    # Before any deletion, and with its own message. A backup that cannot be
    # written has to stop the prune rather than be found missing after it, and the
    # calling script reports a bare failure as "could not read the host config" -
    # which would send the reader to look at the wrong file. Exit 3 says the
    # reason is already on stderr.
    sys.stderr.write("cannot write the backup at {} ({}); the config is untouched\n".format(backup, exc))
    sys.exit(3)

out.append("   backed up {} row(s) to {}".format(len(rows), backup))
# Printed before the deletions rather than with the tally after them. A remove
# that raises partway through leaves the config half-pruned, and the one thing
# the reader needs then is the path to the backup - held to the end, that is
# exactly the line that would be lost.
print("\n".join(out), flush=True)
for row in colliding:
    remove_agent(row["name"], config_path=path)
print("   deleted {} row(s); restart raven (or the gateway) to pick up the discovered ones".format(len(colliding)))
'

stale_payload() {
    local mode="$1" backup="$2"
    shift 2
    python3 - "$mode" "$backup" "$@" <<'PYEOF'
import json
import sys
from pathlib import Path

mode, backup, folders = sys.argv[1], sys.argv[2], sys.argv[3:]
manifests = {}
for folder in folders:
    try:
        manifests[folder] = json.loads((Path(folder) / "subagent.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        continue
print(json.dumps({"folders": manifests, "mode": mode, "backup": backup}))
PYEOF
}

# Report the config rows that shadow a folder, and on --prune-stale delete them.
#
# Reporting is unconditional and deleting is not, because a row here is the user's
# config. Without the flag this function reads that file and writes nothing, which
# is what keeps a plain run safe to hand to anyone; the flag is the whole consent,
# and it is also the only path on which this script touches a file outside its own
# tree. The ordering that moved registration out of here - a first install runs
# this before a configured raven exists - is why that consent cannot be assumed.
stale_step() {
    local interpreter mode dirs=()
    if ! interpreter="$(host_interpreter)"; then
        return 0
    fi
    for folder in "${FOLDERS[@]}"; do
        [ -f "$HERE/$folder/subagent.json" ] && dirs+=("$HERE/$folder")
    done
    [ ${#dirs[@]} -gt 0 ] || return 0

    mode=report
    if [ "$PRUNE_STALE" = 1 ]; then
        if [ "$DRY_RUN" = 1 ]; then mode=dry-run; else mode=prune; fi
    fi

    local status=0
    # Unquoted on purpose: the shebang may be an `env` line, which is two words.
    # shellcheck disable=SC2086
    stale_payload "$mode" "$HERE/subagents-backup-$(date +%Y%m%d-%H%M%S).json" "${dirs[@]}" \
        | $interpreter -c "$STALE_READER" || status=$?
    # 3 means the reader already said what went wrong, on stderr. Repeating the
    # generic line over it would name the config as the thing that failed when the
    # backup was.
    if [ "$status" != 0 ] && [ "$status" != 3 ]; then
        echo "== stale config rows: could not read the host config through $interpreter"
    fi
    # Only when the deletion was actually asked for. Reporting is advisory - a
    # machine with no host raven to read is the ordinary first install, not a
    # failure - but `--prune-stale` names an action, and a script that gets exit 0
    # after it did not happen has been told the opposite of the truth.
    if [ "$status" != 0 ] && [ "$mode" = prune ]; then
        PRUNE_FAILED=1
    fi
}

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

    extra="$(extra_of "$folder" "$checkout")"

    was_built=0
    if [ -x "$checkout/.venv/bin/raven" ]; then
        was_built=1
        echo "   venv: present in $(basename "$checkout")"
    elif [ "$DRY_RUN" = 1 ] || [ "$SYNC" = 0 ]; then
        echo "   venv: MISSING in $(basename "$checkout") - the agent cannot run until \`uv sync\` builds it"
    fi
    if [ "$SYNC" = 1 ] && [ "$DRY_RUN" = 0 ]; then
        sync_args=(sync)
        if [ -n "$extra" ]; then
            sync_args+=(--extra "$extra")
        fi
        echo "   venv: uv ${sync_args[*]} in $(basename "$checkout")"
        sync_log="$(mktemp)"
        if ! (cd "$checkout" && uv "${sync_args[@]}" > /dev/null 2> "$sync_log"); then
            echo "   uv sync failed"
            sed 's/^/      /' "$sync_log"
            rm -f "$sync_log"
            failed+=("$folder")
            continue
        fi
        echo "   venv: $(delta_of "$sync_log")"
        rm -f "$sync_log"
        if [ "$was_built" = 0 ] && [ -x "$checkout/.venv/bin/raven" ]; then
            newly_built+=("$folder")
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

    everos="$(everos_of "$dir")"
    echo "   everos memory: $(printf '%s' "$everos" | head -n1)"
    printf '%s\n' "$everos" | sed -n 's/^WARN /   ! /p'
    # A built venv is what makes the folder offerable: onboarding declines to
    # register one that cannot start.
    if [ -x "$checkout/.venv/bin/raven" ]; then
        ready+=("$folder")
    else
        not_built+=("$folder")
    fi
done

stale_step

echo "== summary"
[ ${#ready[@]} -gt 0 ] && echo "   ready:       ${ready[*]}"
[ ${#newly_built[@]} -gt 0 ] && echo "   newly built: ${newly_built[*]}"
[ ${#not_built[@]} -gt 0 ] && echo "   not built:   ${not_built[*]}"
[ ${#failed[@]} -gt 0 ] && echo "   failed:      ${failed[*]}"
if [ ${#ready[@]} -gt 0 ] && [ "$DRY_RUN" = 0 ]; then
    echo "   now run \`raven\` - onboarding asks about each of these and registers the ones you take up"
fi
[ ${#failed[@]} -eq 0 ] && [ "$PRUNE_FAILED" = 0 ]
