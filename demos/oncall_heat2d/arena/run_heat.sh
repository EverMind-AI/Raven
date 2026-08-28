#!/bin/bash
# The job body. Runs with cwd = the trial's own directory; the configuration for
# this run is that directory's config.json. Absent keys fall back to the values
# below, which are the ones this case has always been run with.
#
# No `set -e`: every step prints a marker instead, so a failure says where.

CFG=config.json
get_cfg() {
  [ -f "$CFG" ] || return 1
  sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([0-9A-Za-z._eE+-]*\)\"\{0,1\}.*/\1/p" "$CFG" | head -1
}

NX=$(get_cfg nx)
ALPHA=$(get_cfg alpha)
DT=$(get_cfg dt)
TEND=$(get_cfg t_end)

: "${ALPHA:=1.0}"; : "${DT:=5e-6}"; : "${TEND:=0.05}"

if [ -z "$NX" ]; then
  echo "[job] FATAL config names no nx, and there is no default for it: how fine the"
  echo "[job] grid is is the thing this case is run to decide."
  exit 2
fi

echo "[job] start $(date -u +%H:%M:%S) in $(pwd)"
echo "[job] config nx=$NX alpha=$ALPHA dt=$DT t_end=$TEND"

# The physical source directory, not the invoked path: the process backend
# stages a case by filling each trial directory with symlinks back to it, so
# `dirname "$0"` is the trial directory and anything resolved relative to it
# lands under the ops workspace instead of beside the case.
SRC="$0"
while [ -L "$SRC" ]; do
  LINK=$(readlink "$SRC")
  case "$LINK" in
    /*) SRC="$LINK" ;;
    *) SRC="$(dirname "$SRC")/$LINK" ;;
  esac
done
ARENA=$(cd "$(dirname "$SRC")" && pwd -P)

# The interpreter is resolved, not assumed: the on-call agent runs this through
# a child that rebuilds its login-shell PATH, so the repo venv `uv sync` filled
# is not inherited and a bare `python3` is whatever the machine happens to have.
# Preference order is explicit override, then the repo venv beside this demo,
# then ambient -- and each candidate has to actually import numpy, because the
# one that cannot is the failure this ordering exists to avoid.
has_numpy() { [ -x "$1" ] && "$1" -c "import numpy" 2>/dev/null; }
PY_BIN=""
for cand in "$HEAT2D_PYTHON" "$ARENA/../../../.venv/bin/python" "$(command -v python3)"; do
  [ -n "$cand" ] || continue
  if has_numpy "$cand"; then PY_BIN="$cand"; break; fi
done
if [ -z "$PY_BIN" ]; then
  echo "[job] FATAL no python3 with numpy: tried HEAT2D_PYTHON, the repo venv"
  echo "[job] ($ARENA/../../../.venv), and this shell's python3."
  echo "[job] Run 'uv sync' at the repo root, or set HEAT2D_PYTHON to an"
  echo "[job] interpreter that has numpy."
  printf '{"status":"failed","rc":3,"error":"no python3 with numpy"}\n' > result.json
  exit 3
fi

echo "[job] interpreter $PY_BIN"
"$PY_BIN" "$ARENA/heat2d.py" "$NX" "$ALPHA" "$DT" "$TEND"
rc=$?
echo "[job] python exited rc=$rc"
[ -f result.json ] || printf '{"status":"failed","rc":%d,"error":"no result.json written"}\n' "$rc" > result.json
exit $rc
