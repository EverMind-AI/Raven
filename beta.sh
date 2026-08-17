#!/bin/sh
# Raven beta installer (macOS / Linux).
#
# Testers run one line. The copy they run is not this one: `make beta` fills in
# the project and token below and uploads the result next to the wheels, so the
# URL that carries the credential also serves the script that remembers it.
#
#   curl -fsSL https://raven-beta:TOKEN@gitlab.com/api/v4/projects/ID/packages/generic/raven/latest/beta.sh | sh
#
# It resolves the newest published build, records the channel in
# ~/.raven/beta.json so the app can offer later updates by itself, and then
# hands the actual install to the released install.sh -- which already knows
# how to put uv, Node and raven on a clean machine, and takes the wheel to
# install as RAVEN_WHEEL_URL.
#
# POSIX sh on purpose (runs under dash/ash, not just bash).
set -eu

# --- config ----------------------------------------------------------------
# `make beta` rewrites these two lines. Left as placeholders in git so the
# repository never carries the credential.
BETA_PROJECT="${RAVEN_BETA_PROJECT:-__RAVEN_BETA_PROJECT__}"
BETA_TOKEN="${RAVEN_BETA_TOKEN:-__RAVEN_BETA_TOKEN__}"
BETA_USER="${RAVEN_BETA_USER:-raven-beta}"

INSTALL_SH="${RAVEN_INSTALL_SH:-https://raw.githubusercontent.com/EverMind-AI/Raven/refs/heads/main/install.sh}"
RAVEN_HOME="${RAVEN_HOME:-${HOME:?HOME is required, or set RAVEN_HOME explicitly}/.raven}"

# --- pretty output ---------------------------------------------------------
info()  { printf '\033[1;34m>\033[0m %s\n' "$1"; }
ok()    { printf '\033[1;32m+\033[0m %s\n' "$1"; }
die()   { printf '\033[1;31mx\033[0m %s\n' "$1" >&2; exit 1; }

case "$BETA_PROJECT" in
  __RAVEN_BETA_*) die "This is the template copy of beta.sh. Run the one-line command you were given instead." ;;
esac

command -v curl >/dev/null 2>&1 || die "curl is required; please install it first"

API="https://gitlab.com/api/v4/projects/$BETA_PROJECT/packages/generic/raven"

# --- 1. what is the newest build ------------------------------------------
info "Checking the beta channel..."
pointer="$(curl -fsSL -H "DEPLOY-TOKEN: $BETA_TOKEN" "$API/latest/latest.json" 2>/dev/null)" \
  || die "Could not reach the beta channel. Check your network, or ask for a fresh install command."

# One field, read without a JSON parser: `"version": "0.1.12b3"` -> `0.1.12b3`.
version="$(printf '%s' "$pointer" | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
[ -n "$version" ] || die "The beta channel returned something unreadable: $pointer"
ok "Newest beta build: $version"

# --- 2. remember the channel so the app can update itself later ------------
# 0600 from creation rather than a chmod afterwards: the token must never be
# world-readable, not even for the moment between the two calls.
mkdir -p "$RAVEN_HOME"
state="$RAVEN_HOME/beta.json"
(
  umask 077
  cat > "$state" <<EOF
{"project": "$BETA_PROJECT", "username": "$BETA_USER", "token": "$BETA_TOKEN"}
EOF
)
ok "Beta channel recorded in $state"

# --- 3. hand off to the released installer ---------------------------------
# Credentials ride in the URL because that is the one auth style uv carries.
wheel="https://$BETA_USER:$BETA_TOKEN@gitlab.com/api/v4/projects/$BETA_PROJECT/packages/generic/raven/$version/raven-$version-py3-none-any.whl"

info "Installing raven $version..."
script="$(mktemp)"
trap 'rm -f "$script"' EXIT
curl -fsSL "$INSTALL_SH" -o "$script" || die "Could not download the installer from $INSTALL_SH"
RAVEN_WHEEL_URL="$wheel" sh "$script"

printf '\n'
ok "Beta channel active. Raven will offer later beta builds by itself."
