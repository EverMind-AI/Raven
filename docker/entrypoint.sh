#!/usr/bin/env sh
# Container entrypoint: nginx in front, the Raven engine behind it.
#
#   docker compose up                                 -> run (default)
#   docker compose exec raven docker-entrypoint.sh signin
#                                                     -> print a fresh sign-in URL
#   docker compose run --rm raven provider set anthropic --api-key sk-...
#                                                     -> any raven CLI command
#
# `run` and `signin` are dispatch keywords; anything else is handed to the
# `raven` CLI verbatim.

set -eu

# Defaults mirror the image's ENV. Repeated here so `set -u` cannot turn a
# variable someone dropped from .env into an unreadable failure.
PAGE_PORT="${RAVEN_PAGE_PORT:-18792}"
WEB_PORT="${RAVEN_WEB_PORT:-${PAGE_PORT}}"
AUTO_LOGIN="${RAVEN_AUTO_LOGIN:-1}"
RAVEN_HOME="${RAVEN_HOME:-/data/.raven}"
RAVEN_DIST_ROOT="${RAVEN_DIST_ROOT:-/app/ui-web/dist}"
RAVEN_PAGE_PORT="${PAGE_PORT}"
export RAVEN_HOME RAVEN_DIST_ROOT RAVEN_PAGE_PORT
BASE="http://127.0.0.1:${PAGE_PORT}"

log() { printf 'raven-docker: %s\n' "$*"; }

# The mark, on the container's first line of output. Quoted heredoc so the
# shell reads the art as bytes -- nothing in it is a shell metacharacter today,
# and this keeps that true if the drawing is ever redrawn. Lines carry no
# trailing spaces: the repo strips those on commit, and they render the same.
banner() {
    cat <<'RAVEN_MARK'
░█████████
░██     ░██
░██     ░██  ░██████   ░██    ░██  ░███████  ░████████
░█████████        ░██  ░██    ░██ ░██    ░██ ░██    ░██
░██   ░██    ░███████   ░██  ░██  ░█████████ ░██    ░██
░██    ░██  ░██   ░██    ░██░██   ░██        ░██    ░██
░██     ░██  ░█████░██    ░███     ░███████  ░██    ░██
RAVEN_MARK
    printf '\n'
}

# The engine writes {port, token, pid} here once it is listening; the token is
# what mints a sign-in nonce. Generated per boot rather than shipped in .env,
# so no deployment shares a secret with the repository.
serve_state() {
    python - "$1" <<'PY'
import json, os, sys
path = os.path.join(os.environ.get("RAVEN_HOME", ""), "serve.json")
try:
    with open(path, encoding="utf-8") as fh:
        print(json.load(fh).get(sys.argv[1], ""))
except (OSError, ValueError):
    print("")
PY
}

# 0 listening, 1 timed out, 2 the engine is gone.
wait_for_engine() {
    # 120 s: a first boot builds the agent loop, the plugin stack and the
    # memory store before it listens.
    i=0
    while [ "$i" -lt 240 ]; do
        # Asked before the probe. A child that died on its first line answers
        # nothing for the whole two minutes otherwise, and the container spends
        # them looking like a slow start instead of a failed one -- `top` shows
        # nginx, this shell and a `sleep`, and no engine.
        if ! kill -0 "${engine}" 2>/dev/null; then
            return 2
        fi
        if curl -fsS -m 2 "${BASE}/health" >/dev/null 2>&1; then
            return 0
        fi
        i=$((i + 1))
        sleep 0.5
    done
    return 1
}

sign_in_url() {
    token="$(serve_state token)"
    if [ -z "$token" ]; then
        log "no serve.json yet -- is the engine running?"
        return 1
    fi
    nonce="$(curl -fsS -m 5 -X POST -H "X-Raven-Token: ${token}" "${BASE}/auth/nonce" 2>/dev/null \
        | python -c 'import json,sys; print(json.load(sys.stdin).get("nonce",""))' 2>/dev/null || true)"
    if [ -z "$nonce" ]; then
        log "could not mint a sign-in nonce"
        return 1
    fi
    printf 'http://localhost:%s/auth#%s\n' "${WEB_PORT}" "${nonce}"
}

announce() {
    url="$(sign_in_url || true)"
    printf '\n'
    log "the page is on http://localhost:${WEB_PORT}"
    if [ -n "${url:-}" ]; then
        log "sign in once with this one-time link (replace localhost if you are remote):"
        printf '\n    %s\n\n' "$url"
        log "for another link: docker compose exec raven docker-entrypoint.sh signin"
    fi
}

# A provider named in the environment is written into config on every boot:
# the container is declarative, so .env is the source of truth whenever it says
# anything. Leave RAVEN_PROVIDER empty and the browser wizard owns the config.
seed_provider() {
    if [ -z "${RAVEN_PROVIDER:-}" ] \
        || { [ -z "${RAVEN_API_KEY:-}" ] && [ -z "${RAVEN_API_BASE:-}" ]; }; then
        return 0
    fi
    log "configuring provider '${RAVEN_PROVIDER}' from the environment"
    set -- provider set "${RAVEN_PROVIDER}"
    if [ -n "${RAVEN_API_KEY:-}" ]; then
        set -- "$@" --api-key "${RAVEN_API_KEY}"
    fi
    if [ -n "${RAVEN_API_BASE:-}" ]; then
        set -- "$@" --api-base "${RAVEN_API_BASE}"
    fi
    raven "$@"
}

start_nginx() {
    RAVEN_AUTO_LOGIN_COOKIE=""
    if [ "${AUTO_LOGIN}" = "1" ] || [ "${AUTO_LOGIN}" = "true" ]; then
        cookie="$(serve_state cookie)"
        if [ -z "${cookie}" ]; then
            log "the engine did not publish its browser session cookie"
            return 1
        fi
        RAVEN_AUTO_LOGIN_COOKIE="raven_session_${PAGE_PORT}=${cookie}; Path=/; Max-Age=2592000; HttpOnly; SameSite=Strict"
    fi
    export RAVEN_AUTO_LOGIN_COOKIE
    mkdir -p /etc/nginx/conf.d
    # Only these values: nginx's own $variables must survive substitution.
    envsubst '${RAVEN_DIST_ROOT} ${RAVEN_PAGE_PORT} ${RAVEN_AUTO_LOGIN_COOKIE}' \
        < /etc/nginx/templates/raven.conf.template \
        > /etc/nginx/conf.d/raven.conf
    nginx -t
    nginx
    log "nginx serving ${RAVEN_DIST_ROOT} on :80, proxying ${BASE}"
}

run() {
    # Printed here rather than at the top of the file: `signin` and the CLI
    # passthrough are read by people and by scripts, and a banner ahead of a
    # sign-in URL or a `raven status` is output nobody asked for.
    banner

    # The engine must land on exactly this port or nginx proxies into nothing;
    # without strict mode it probes forward when the port looks taken.
    RAVEN_SERVE_PORT_STRICT=1
    export RAVEN_SERVE_PORT_STRICT
    mkdir -p "${RAVEN_HOME}"

    seed_provider

    # Starts with no provider configured and warns; the page's Settings >
    # Models is how one gets added, and the next turn picks it up.
    raven gateway --page-port "${PAGE_PORT}" &
    engine=$!
    trap 'log "stopping"; kill -TERM "${engine}" 2>/dev/null || true' TERM INT

    boot=0
    wait_for_engine || boot=$?
    if [ "$boot" -eq 0 ]; then
        start_nginx
        announce
    elif [ "$boot" -eq 2 ]; then
        log "the engine exited during start-up -- its error is above this line"
    else
        log "the engine did not answer ${BASE}/health within 120s; see the log above"
    fi

    # `wait` returns the moment a trap fires, with the child still shutting
    # down -- a turn in flight and the memory store both want to finish. So the
    # status is taken from the first wait and the exit is held until the process
    # is actually gone.
    status=0
    wait "${engine}" || status=$?
    while kill -0 "${engine}" 2>/dev/null; do
        sleep 0.2
    done

    # Last, so the page keeps answering while the engine winds down, and the
    # container never lingers serving a socket that is no longer there.
    nginx -s quit 2>/dev/null || true
    return "${status}"
}

case "${1:-run}" in
    run)
        shift 2>/dev/null || true
        run
        ;;
    signin)
        sign_in_url
        ;;
    *)
        exec raven "$@"
        ;;
esac
