#!/bin/sh
# Raven Desktop bootstrap (portable).
#
# Ensures the resident gateway is up and prints the one-time authenticated page
# URL on stdout. Intended to be called by the native macOS app; it deliberately
# does NOT open a browser -- BROWSER=/usr/bin/true keeps `raven web` from doing
# so, because the app itself is the window.
#
# No absolute user paths: works for any account / any uv tool location.

set -u
RAVEN_HOME="${RAVEN_HOME:-$HOME/.raven}"
export BROWSER=/usr/bin/true

# Finder-launched apps get a minimal PATH. Make sure ~/.local/bin is visible.
export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin${PATH:+:$PATH}"

# Resolve the `raven` CLI. Prefer PATH, then the conventional uv location.
if command -v raven >/dev/null 2>&1; then
    RAVEN="$(command -v raven)"
else
    RAVEN="$HOME/.local/bin/raven"
fi

# Resolve the Python interpreter that owns the raven install. The CLI is a
# script whose shebang points at that interpreter, so we read it rather than
# hard-coding a uv tool path.
PY=""
if [ -f "$RAVEN" ]; then
    shebang="$(head -1 "$RAVEN")"
    case "$shebang" in
        '#!'*) cand="${shebang#\#!}"; [ -x "$cand" ] && PY="$cand" ;;
    esac
fi
[ -n "$PY" ] || PY="$(command -v python3 || true)"

attach() {
    [ -n "$PY" ] || return 0
    "$PY" -c 'from raven.cli.serve_commands import _attached_url; print(_attached_url() or "")' 2>/dev/null
}

# Read the live page port from the gateway's own state file (never hard-code it).
page_url() {
    url="$(attach)"
    [ -n "$url" ] && printf '%s' "$url"
}

url="$(page_url)"
if [ -z "$url" ] && [ -x "$RAVEN" ]; then
    # No live gateway (or a stale record after an unexpected exit).
    "$RAVEN" web --stop >/dev/null 2>&1 || true
    "$RAVEN" web >/dev/null 2>&1 || true
    i=0
    while [ "$i" -lt 60 ]; do
        url="$(page_url)"
        [ -n "$url" ] && break
        sleep 0.5
        i=$((i + 1))
    done
fi
printf '%s' "$url"
