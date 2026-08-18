#!/usr/bin/env bash
# One-click launcher for the Raven web app (ui-webui layout).
#
# Brings up four pieces:
#   1. Redis          -> docker container "ravenx-redis" on :6379
#   2. Raven gateway  -> `raven gateway` + web WS channel on :8765
#   3. Agent service  -> FastAPI (uvicorn) on :8000   [raven's own env, dir service/]
#   4. Web UI         -> Vite dev server on :5173      (pnpm --filter frontend dev)
#
# The chat's MAIN AGENT is the persistent `raven gateway` web channel
# (RavenGatewayAgent): the service connects to the gateway's WebSocket JSON-RPC
# endpoint and streams its spine events. This is the only supported mode. The Web
# UI resolves the agent service automatically from SERVICE_PORT (no server-URL
# prompt); override it in Settings - Preferences.
#
# Usage:
#   ./start_webapp.sh            # auto-clear any prior instance, then start
#                                # everything & stream logs; Ctrl+C stops all
#   ./start_webapp.sh stop       # stop gateway + service + frontend
#   ./start_webapp.sh restart    # stop then start (non-blocking)
#   ./start_webapp.sh status     # show what is currently up
#
# start/restart force-restart the gateway (killing an orphan on :8765 that no
# pid file tracks), so it always runs the current code — edits under raven/,
# such as the gateway's own RPC handlers, take effect on restart. That also
# bounces the IM channels living in the same process; KEEP_GATEWAY=1 reuses the
# running gateway untouched instead, at the cost of it serving stale code.
#
# Redis is a shared docker container, so it is left running on stop.
set -euo pipefail

# ---- config --------------------------------------------------------------
# This launcher lives one level above the web app; the app itself is in ui-webui/.
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/ui-webui" && pwd)"
# The gateway, the CLI and the agent service all run on one interpreter: the uv
# tool env holding the editable `raven`. start_service() locates it from the
# `raven` entrypoint; SERVICE_PYTHON overrides it.
REDIS_CONTAINER="ravenx-redis"
SERVICE_PORT=8000      # python agent service (uvicorn), see service/main.py
FRONTEND_PORT=5173     # vite dev server, see frontend/
# The chat always runs through a persistent `raven gateway` web channel; the
# service has no other backend (see service/main.py).
#
# The gateway's web channel (gateway.web in ~/.raven/config.json). The script
# writes host/port into that config and reads authToken back out, so these vars
# stay the single source of truth for both sides of the WebSocket.
GATEWAY_WS_HOST="${RAVEN_GATEWAY_WS_HOST:-127.0.0.1}"
GATEWAY_WS_PORT="${RAVEN_GATEWAY_WS_PORT:-8765}"
RAVEN_GATEWAY_WS_URL="${RAVEN_GATEWAY_WS_URL:-ws://$GATEWAY_WS_HOST:$GATEWAY_WS_PORT/ws}"
RAVEN_GATEWAY_WS_TOKEN="${RAVEN_GATEWAY_WS_TOKEN:-}"
RAVEN_CONFIG="${RAVEN_CONFIG:-$HOME/.raven/config.json}"
# start/restart force-restart the gateway so it always runs the current code.
# KEEP_GATEWAY=1 opts out: reuse whatever already listens on :$GATEWAY_WS_PORT
# without bouncing it (for a hand-run, long-lived gateway also serving the IM
# channels) at the cost of not picking up edits under raven/.
KEEP_GATEWAY="${KEEP_GATEWAY:-0}"
# Seconds to wait for a process to die after each signal before escalating.
# Sized for the gateway's teardown (cancel detached subagents, close MCP, stop
# the memory backend); fast processes exit on the first poll, so it costs
# nothing for the service/frontend.
STOP_GRACE_SECS="${STOP_GRACE_SECS:-15}"
LOG_DIR="$REPO_ROOT/.webapp_logs"
GATEWAY_PID_FILE="$LOG_DIR/gateway.pid"
SERVICE_PID_FILE="$LOG_DIR/service.pid"
FRONTEND_PID_FILE="$LOG_DIR/frontend.pid"
# Brand logos are two DIFFERENT images, not copies of each other:
#   - sidebar (top-left, transparent bg) -> src, imported by AppSidebar
#   - favicon (browser tab, white bg)     -> public/, referenced by index.html
LOGO_SIDEBAR="$REPO_ROOT/frontend/src/assets/images/raven_logo.svg"
LOGO_FAVICON="$REPO_ROOT/frontend/public/favicon.svg"

# UTF-8 so Chinese in logs / prompts renders correctly.
export LANG="${LANG:-C.UTF-8}"
export LC_ALL="${LC_ALL:-C.UTF-8}"
export PYTHONUTF8=1

mkdir -p "$LOG_DIR"

# ---- helpers -------------------------------------------------------------
port_in_use() {
  local p="$1"
  if command -v ss >/dev/null 2>&1; then
    ss -ltn 2>/dev/null | grep -qE "[:.]$p\b"
  else
    (exec 3<>"/dev/tcp/127.0.0.1/$p") 2>/dev/null && { exec 3>&-; return 0; } || return 1
  fi
}

# PIDs of whatever is listening on a TCP port (best-effort, deduped).
# Wrapped in `{ ...; } || true` so an empty match (grep exit 1 under
# `pipefail`) yields no output instead of aborting the script via `set -e`.
pids_on_port() {
  local p="$1"
  { if command -v ss >/dev/null 2>&1; then
      ss -ltnpH 2>/dev/null | grep -E "[:.]$p\b" \
        | grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u
    elif command -v fuser >/dev/null 2>&1; then
      fuser "$p/tcp" 2>/dev/null | tr -s ' ' '\n' | grep -E '^[0-9]+$' | sort -u
    fi; } || true
}

# Forcefully free a port: TERM the holders, wait, then escalate to KILL.
# Re-resolves PIDs each round so we catch children that outlive the leader.
free_port() {
  local p="$1" sig=TERM pids
  for round in 1 2; do
    pids="$(pids_on_port "$p")" || true
    [[ -n "$pids" ]] || return 0
    echo "[port $p] SIG$sig -> $(echo $pids | tr '\n' ' ')"
    kill -"$sig" $pids 2>/dev/null || true
    for _ in $(seq 1 8); do sleep 1; port_in_use "$p" || return 0; done
    sig=KILL
  done
  port_in_use "$p" && echo "[port $p] WARNING: still in use after SIGKILL" || true
}

# A zombie keeps answering `kill -0` until its parent reaps it, and `restart`
# stops the very children this shell backgrounded — so treat state Z as dead,
# otherwise every stop would burn its full grace period waiting on a corpse.
# The `.*) ` strip skips comm, which may itself contain spaces.
pid_alive() {
  local pid="$1" state
  kill -0 "$pid" 2>/dev/null || return 1
  state="$(sed 's/.*) //' "/proc/$pid/stat" 2>/dev/null | cut -d' ' -f1)"
  [[ -z "$state" || "$state" != Z ]]
}

wait_pid_gone() {
  local pid="$1" secs="$2" i
  for ((i = 0; i < secs; i++)); do
    pid_alive "$pid" || return 0
    sleep 1
  done
  ! pid_alive "$pid"
}

# True only for a `raven gateway` process: an argv element naming the raven
# entrypoint plus a bare `gateway` subcommand. Guards every kill aimed at
# :$GATEWAY_WS_PORT so an unrelated listener squatting there is reported instead
# of killed. Matching the raw cmdline as one string would false-positive on the
# agent service, whose env block carries RAVEN_GATEWAY_WS_URL.
is_raven_gateway() {
  local pid="$1" args=() a saw_raven=0 saw_gateway=0
  [[ -r "/proc/$pid/cmdline" ]] || return 1
  mapfile -d '' -t args <"/proc/$pid/cmdline" 2>/dev/null || return 1
  for a in "${args[@]}"; do
    [[ "$a" == raven || "$a" == */raven ]] && saw_raven=1
    [[ "$a" == gateway ]] && saw_gateway=1
  done
  (( saw_raven && saw_gateway ))
}

ensure_redis() {
  if ! command -v docker >/dev/null 2>&1; then
    echo "[redis] docker not found — make sure Redis is reachable on :6379 yourself" >&2
    return 0
  fi
  if docker ps --format '{{.Names}}' | grep -qx "$REDIS_CONTAINER"; then
    echo "[redis] already running ($REDIS_CONTAINER)"
  elif docker ps -a --format '{{.Names}}' | grep -qx "$REDIS_CONTAINER"; then
    echo "[redis] starting existing container $REDIS_CONTAINER"
    docker start "$REDIS_CONTAINER" >/dev/null
  else
    echo "[redis] creating container $REDIS_CONTAINER"
    docker run -d --name "$REDIS_CONTAINER" -p 6379:6379 redis:7 >/dev/null
  fi
}

# A root <svg> with width/height but no viewBox does not scale: rendered at a
# small CSS size (the sidebar uses size-8 = 32px) only the top-left 32px sliver
# shows, so the logo looks blank. Inject viewBox="0 0 W H" derived from the
# width/height attrs so CSS sizing works. Idempotent: skips if viewBox exists.
fix_svg_viewbox() {
  local f="$1"
  [[ -f "$f" ]] || return 0
  grep -qE '<svg[^>]*viewBox=' "$f" && return 0
  local tag w h
  tag="$(grep -oE '<svg[^>]*>' "$f" | head -1)"
  w="$(printf '%s' "$tag" | grep -oE 'width="[0-9.]+"'  | head -1 | grep -oE '[0-9.]+')"
  h="$(printf '%s' "$tag" | grep -oE 'height="[0-9.]+"' | head -1 | grep -oE '[0-9.]+')"
  if [[ -z "$w" || -z "$h" ]]; then
    echo "[logo] WARNING: $(basename "$f") has no viewBox and no numeric width/height — cannot auto-fix" >&2
    return 0
  fi
  echo "[logo] $(basename "$f") missing viewBox — injecting viewBox=\"0 0 $w $h\""
  # 0,/<svg/ limits the substitution to the first tag even when the whole SVG
  # is a single line (GNU sed).
  sed -i "0,/<svg/s|<svg|<svg viewBox=\"0 0 $w $h\"|" "$f"
}

# Generate the favicon from the single source-of-truth logo: the transparent
# sidebar SVG plus a rounded white rectangle drawn behind it (favicons need an
# opaque background to read well against dark browser chrome). Regenerated each
# startup so editing only the sidebar logo keeps the favicon in sync.
FAVICON_BG="${FAVICON_BG:-#ffffff}"     # rounded-rect background color
FAVICON_RADIUS_PCT="${FAVICON_RADIUS_PCT:-24}"  # corner radius as % of canvas
build_favicon() {
  [[ -f "$LOGO_SIDEBAR" ]] || { echo "[favicon] sidebar logo missing — skip" >&2; return 0; }
  if ! command -v python3 >/dev/null 2>&1; then
    echo "[favicon] python3 not found — cannot generate favicon; skipping" >&2
    return 0
  fi
  FAVICON_BG="$FAVICON_BG" FAVICON_RADIUS_PCT="$FAVICON_RADIUS_PCT" \
    python3 - "$LOGO_SIDEBAR" "$LOGO_FAVICON" <<'PY'
import sys, re, os
src, dst = sys.argv[1], sys.argv[2]
bg = os.environ.get("FAVICON_BG", "#ffffff")
pct = float(os.environ.get("FAVICON_RADIUS_PCT", "24"))
s = open(src, encoding="utf-8").read()
m = re.search(r"<svg\b[^>]*>", s)
if not m:
    sys.exit("[favicon] no <svg> tag in source")
tag = m.group(0)
vb = re.search(r'viewBox="\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*"', tag)
if vb:
    x, y, w, h = (float(g) for g in vb.groups())
else:
    x = y = 0.0
    w = float((re.search(r'width="([\d.]+)"', tag) or ["", "1254"])[1] if re.search(r'width="([\d.]+)"', tag) else 1254)
    h = float((re.search(r'height="([\d.]+)"', tag) or ["", "1254"])[1] if re.search(r'height="([\d.]+)"', tag) else 1254)
r = round(min(w, h) * pct / 100.0)
rect = f'<rect x="{x:g}" y="{y:g}" width="{w:g}" height="{h:g}" rx="{r}" ry="{r}" fill="{bg}"/>'
out = s[:m.end()] + rect + s[m.end():]
open(dst, "w", encoding="utf-8").write(out)
print(f"[favicon] generated {os.path.basename(dst)} = transparent logo + {bg} rounded bg (rx={r})")
PY
}

# Prepare brand logos: fix the sidebar SVG's viewBox (a freshly-exported file
# often drops it), then (re)generate the favicon from it.
ensure_logo() {
  fix_svg_viewbox "$LOGO_SIDEBAR"
  build_favicon
}

# Install node deps once (pnpm workspace hoists to the root node_modules).
ensure_deps() {
  command -v pnpm >/dev/null 2>&1 || return 0
  if [[ ! -d "$REPO_ROOT/node_modules" \
     || ! -d "$REPO_ROOT/frontend/node_modules" ]]; then
    echo "[deps] installing node deps (pnpm install) ..."
    (cd "$REPO_ROOT" && pnpm install)
  fi
}

# Turn on the gateway's web channel in raven's own config and align it with
# GATEWAY_WS_HOST/PORT. `raven gateway` only hosts the WS endpoint when
# gateway.web.enabled is true, and there is no env override for it — the config
# file is the only switch. Rewrites in place (keeping a .bak) only when a value
# actually differs, and echoes the effective authToken back so the service can
# authenticate with the same secret.
sync_gateway_web_config() {
  local out
  out="$(RAVEN_CONFIG="$RAVEN_CONFIG" WEB_HOST="$GATEWAY_WS_HOST" WEB_PORT="$GATEWAY_WS_PORT" \
    python3 - <<'PY'
import json, os, shutil, sys
from pathlib import Path

path = Path(os.environ["RAVEN_CONFIG"])
host, port = os.environ["WEB_HOST"], int(os.environ["WEB_PORT"])
if path.exists():
    try:
        cfg = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        # Never overwrite a config we failed to parse: that would replace the
        # user's whole file with just this block.
        print(f"error=cannot parse {path}: {exc}", file=sys.stderr)
        sys.exit(1)
else:
    cfg = {}

web = cfg.setdefault("gateway", {}).setdefault("web", {})
before = dict(web)
web["enabled"] = True
web["host"] = host
web["port"] = port
if before != web:
    if path.exists():
        shutil.copy2(path, path.with_suffix(path.suffix + ".bak"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"changed=1 detail=gateway.web -> enabled=true {host}:{port}")
else:
    print("changed=0")
print("token=" + (web.get("authToken") or web.get("auth_token") or ""))
PY
  )" || { echo "[gateway] FATAL: could not update $RAVEN_CONFIG" >&2; exit 1; }
  while IFS= read -r line; do
    case "$line" in
      changed=1*) echo "[gateway] ${line#changed=1 detail=} (in $RAVEN_CONFIG, backup .bak)" ;;
      changed=0)  echo "[gateway] config ok: gateway.web enabled on $GATEWAY_WS_HOST:$GATEWAY_WS_PORT" ;;
      token=*)    if [[ -z "$RAVEN_GATEWAY_WS_TOKEN" ]]; then RAVEN_GATEWAY_WS_TOKEN="${line#token=}"; fi ;;
    esac
  done <<<"$out"
}

start_gateway() {
  if ! command -v raven >/dev/null 2>&1; then
    echo "[gateway] FATAL: 'raven' is not on PATH, and the chat cannot run" >&2
    echo "          without the gateway. Install it with" >&2
    echo "          uv tool install --editable <repo> --force" >&2
    exit 1
  fi
  sync_gateway_web_config
  # Never silently reuse a listener here: stop_gateway ran first, so anything
  # still on the port is a gateway that refused to die or a foreign process.
  # Reusing it is what let a day-old gateway keep serving after a restart.
  if port_in_use "$GATEWAY_WS_PORT"; then
    echo "[gateway] FATAL: :$GATEWAY_WS_PORT still held after the stop attempt:" >&2
    for pid in $(pids_on_port "$GATEWAY_WS_PORT"); do
      echo "          pid $pid: $(ps -o cmd= -p "$pid" 2>/dev/null)" >&2
    done
    echo "          refusing to start a second gateway. Free the port, or pass" >&2
    echo "          KEEP_GATEWAY=1 to reuse the running one as-is." >&2
    exit 1
  fi
  echo "[gateway] starting raven gateway, web channel on :$GATEWAY_WS_PORT   (log: $LOG_DIR/gateway.log)"
  setsid bash -c "exec raven gateway --config '$RAVEN_CONFIG'" \
    >"$LOG_DIR/gateway.log" 2>&1 &
  echo $! >"$GATEWAY_PID_FILE"
}

start_service() {
  if port_in_use "$SERVICE_PORT"; then
    echo "[service] :$SERVICE_PORT already in use — leaving it as is"
    return 0
  fi
  echo "[service] main agent = raven gateway web channel  ($RAVEN_GATEWAY_WS_URL)"
  echo "[service] starting agent service on :$SERVICE_PORT   (log: $LOG_DIR/service.log)"
  # The service runs on the same interpreter as the gateway: the uv tool env that
  # `raven` is installed into, which carries the service's dependencies too (see
  # service/requirements.txt). Located from the `raven` entrypoint rather than
  # hardcoded, so it follows a move of UV_TOOL_DIR. Override with
  # SERVICE_PYTHON=/abs/path/to/python.
  local svc_py=""
  if [[ -n "${SERVICE_PYTHON:-}" ]] && "${SERVICE_PYTHON}" -c "import uvicorn" >/dev/null 2>&1; then
    svc_py="$SERVICE_PYTHON"; echo "[service] python: $svc_py (SERVICE_PYTHON)"
  else
    local raven_bin cand
    raven_bin="$(command -v raven || true)"
    if [[ -n "$raven_bin" ]]; then
      cand="$(dirname "$(readlink -f "$raven_bin")")/python"
      if [[ -x "$cand" ]] && "$cand" -c "import uvicorn" >/dev/null 2>&1; then
        svc_py="$cand"; echo "[service] python: $svc_py (raven tool env)"
      fi
    fi
    if [[ -z "$svc_py" ]]; then
      echo "[service] FATAL: raven's interpreter lacks the service deps (agentscope+uvicorn)." >&2
      echo "          Install them into it, from the repo root:" >&2
      echo "            uv tool install --force --editable . \\" >&2
      echo "              --with-requirements ui-webui/service/requirements-dev.txt" >&2
      echo "          Or set SERVICE_PYTHON=/abs/path/to/python and retry." >&2
      exit 1
    fi
  fi
  # `agentscope` lives in service/ alongside main.py, so the interpreter finds it
  # via sys.path[0] (the script's own directory) -- which precedes site-packages,
  # so the in-repo copy wins over any agentscope installed in the environment.
  local svc_env="RAVEN_GATEWAY_WS_URL='$RAVEN_GATEWAY_WS_URL' RAVEN_GATEWAY_WS_TOKEN='$RAVEN_GATEWAY_WS_TOKEN' PYTHONUTF8=1"
  setsid bash -c "cd '$REPO_ROOT/service' && exec env $svc_env '$svc_py' main.py" \
    >"$LOG_DIR/service.log" 2>&1 &
  echo $! >"$SERVICE_PID_FILE"
}


start_frontend() {
  command -v pnpm >/dev/null 2>&1 || { echo "[frontend] pnpm not found — skipping" >&2; return 0; }
  if port_in_use "$FRONTEND_PORT"; then
    echo "[frontend] :$FRONTEND_PORT already in use — leaving it as is"
    return 0
  fi
  echo "[frontend] starting Vite on :$FRONTEND_PORT   (log: $LOG_DIR/frontend.log)"
  # VITE_SERVICE_PORT is baked into the bundle at dev-server start: the UI
  # derives the service URL from it instead of asking the user, so a changed
  # SERVICE_PORT here needs no browser-side re-entry.
  setsid bash -c "cd '$REPO_ROOT' && VITE_SERVICE_PORT='$SERVICE_PORT' exec pnpm --filter frontend dev" \
    >"$LOG_DIR/frontend.log" 2>&1 &
  echo $! >"$FRONTEND_PID_FILE"
}

wait_for_port() {
  local p="$1" name="$2" tries="${3:-30}"
  printf '[%s] waiting for :%s ' "$name" "$p"
  while (( tries-- > 0 )); do
    if port_in_use "$p"; then echo "-> up"; return 0; fi
    printf '.'; sleep 1
  done
  echo " -> not up yet (check $LOG_DIR/$name.log)"
}

# Stop a pid-file-tracked process group, escalating through <signals> and
# waiting for the process to actually die between steps. The pid file is kept
# when the process outlives even the last signal: deleting it would strand a
# live process with no handle for the next run to find, which is how the gateway
# ends up orphaned to init and serving day-old code.
stop_pid_file() {
  local f="$1" name="$2" signals="${3:-TERM KILL}"
  [[ -f "$f" ]] || return 0
  local pid sig; pid="$(cat "$f")"
  if ! pid_alive "$pid"; then rm -f "$f"; return 0; fi
  for sig in $signals; do
    echo "[$name] stopping (pid group $pid, SIG$sig)"
    kill -"$sig" -"$pid" 2>/dev/null || kill -"$sig" "$pid" 2>/dev/null || true
    if wait_pid_gone "$pid" "$STOP_GRACE_SECS"; then rm -f "$f"; return 0; fi
  done
  echo "[$name] WARNING: pid $pid survived $signals — keeping $f" >&2
  return 1
}

# Force-stop the gateway so the next start runs current code. Two paths, because
# the pid file is not always there: a prior run that failed to kill it leaves an
# orphan reparented to init, and reusing that orphan is exactly how the web app
# serves a stale gateway. SIGINT first — the gateway only runs its teardown
# (cancel detached subagents, close MCP, flush the memory backend) on
# KeyboardInterrupt, and SIGTERM skips that path entirely.
stop_gateway() {
  stop_pid_file "$GATEWAY_PID_FILE" gateway "INT TERM KILL" || true
  port_in_use "$GATEWAY_WS_PORT" || return 0
  local pids=() foreign=() pid sig i
  for pid in $(pids_on_port "$GATEWAY_WS_PORT"); do
    if is_raven_gateway "$pid"; then pids+=("$pid"); else foreign+=("$pid"); fi
  done
  for pid in ${foreign[@]+"${foreign[@]}"}; do
    echo "[gateway] WARNING: :$GATEWAY_WS_PORT held by non-gateway pid $pid" \
         "($(ps -o cmd= -p "$pid" 2>/dev/null)) — not killing it" >&2
  done
  (( ${#pids[@]} )) || return 0
  echo "[gateway] adopting orphaned gateway (pid ${pids[*]}, no pid file) — restarting it"
  for sig in INT TERM KILL; do
    kill -"$sig" "${pids[@]}" 2>/dev/null || true
    for ((i = 0; i < STOP_GRACE_SECS; i++)); do
      port_in_use "$GATEWAY_WS_PORT" || return 0
      sleep 1
    done
    echo "[gateway] pid ${pids[*]} still holding :$GATEWAY_WS_PORT after SIG$sig" >&2
  done
  echo "[gateway] WARNING: :$GATEWAY_WS_PORT still in use after SIGKILL" >&2
}

# Free every port/process this app uses: the pid-file process groups plus
# whatever holds the service/frontend ports. This also clears stale or
# externally-started holders the pid files don't know about — notably an
# orphaned :5173 Vite left by a crashed prior run, the classic cause of
# EADDRINUSE on the next start. Redis is left running.
free_all_ports() {
  # The gateway is force-stopped too (pid file, else the orphan on the port) so
  # start/restart always brings up one running current code. That bounces the IM
  # channels sharing the process; KEEP_GATEWAY=1 keeps it untouched instead.
  if [[ "$KEEP_GATEWAY" == "1" ]]; then
    echo "[gateway] KEEP_GATEWAY=1 — leaving :$GATEWAY_WS_PORT alone"
  else
    stop_gateway
  fi
  stop_pid_file "$SERVICE_PID_FILE"  service  || true
  stop_pid_file "$FRONTEND_PID_FILE" frontend || true
  free_port "$SERVICE_PORT"
  free_port "$FRONTEND_PORT"
}

stop_all() {
  free_all_ports
  echo "stopped (Redis container left running)."
}

status() {
  echo "redis    : $(docker ps --format '{{.Names}} {{.Status}}' 2>/dev/null | grep "$REDIS_CONTAINER" || echo 'not running')"
  echo "gateway  : $(port_in_use "$GATEWAY_WS_PORT" && echo "UP on :$GATEWAY_WS_PORT (web channel)" || echo 'down')"
  echo "service  : $(port_in_use "$SERVICE_PORT"  && echo "UP on :$SERVICE_PORT"  || echo 'down')"
  echo "frontend : $(port_in_use "$FRONTEND_PORT"  && echo "UP on :$FRONTEND_PORT" || echo 'down')"
}

# Bring up all pieces and wait for the key ports; no log tailing so this is
# safe to call from non-interactive flows (e.g. restart).
bringup() {
  ensure_redis
  ensure_logo
  ensure_deps
  if [[ "$KEEP_GATEWAY" == "1" ]] && port_in_use "$GATEWAY_WS_PORT"; then
    echo "[gateway] KEEP_GATEWAY=1 — reusing the gateway on :$GATEWAY_WS_PORT without restarting it"
    echo "[gateway] WARNING: it may be running stale code; edits under raven/ will NOT take effect" >&2
  else
    start_gateway
    # The service dials the WS lazily, but coming up second keeps the first
    # chat turn from racing a gateway that is still loading its config.
    wait_for_port "$GATEWAY_WS_PORT" gateway 60
  fi
  start_service
  start_frontend
  wait_for_port "$SERVICE_PORT"  service 40
  wait_for_port "$FRONTEND_PORT" frontend
  cat <<EOF

──────────────────────────────────────────────────────────────
  Web UI  : http://localhost:$FRONTEND_PORT
  Service : http://localhost:$SERVICE_PORT   (auto-configured in the UI)
  Gateway : $RAVEN_GATEWAY_WS_URL
  VSCode remote: forward ports $FRONTEND_PORT and $SERVICE_PORT to your local machine.
──────────────────────────────────────────────────────────────
EOF
}

start_all() {
  trap 'echo; echo "shutting down..."; stop_all; exit 0' INT TERM
  # Auto-clear any already-running instance first — stale pid files or port
  # holders on :$SERVICE_PORT / :$FRONTEND_PORT — so `start`
  # always yields a fresh app instead of failing with EADDRINUSE.
  echo "[preflight] clearing any previous instance ..."
  free_all_ports
  bringup
  echo
  echo "Tailing logs — press Ctrl+C to stop gateway + service + frontend (Redis stays up)."
  echo
  touch "$LOG_DIR/gateway.log"
  tail -n +1 -F "$LOG_DIR/gateway.log" "$LOG_DIR/service.log" "$LOG_DIR/frontend.log"
}

# Force-restart everything: stop (freeing all ports), then bring back up.
# Non-blocking — returns after startup.
restart_all() {
  echo "== force-restarting gateway + service + frontend =="
  stop_all
  sleep 1
  bringup
  echo
  status
}

# ---- entrypoint ----------------------------------------------------------
case "${1:-start}" in
  start)   start_all ;;
  stop)    stop_all ;;
  restart) restart_all ;;
  status)  status ;;
  *) echo "usage: $0 [start|stop|restart|status]" >&2; exit 1 ;;
esac
