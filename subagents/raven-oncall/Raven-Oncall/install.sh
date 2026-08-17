#!/bin/sh
# Raven one-line installer (macOS / Linux).
#
#   Remote (website):   curl -fsSL https://raven.evermind.ai/install.sh | sh
#   Local (dev clone):  git clone ... && cd raven && ./install.sh
#
# Goal: a clean machine ends up able to run `raven` / `raven tui` from any
# directory with no manual steps. The script is idempotent -- it detects what
# is already present and only fills the gaps:
#   1. uv            (Python toolchain + package manager)
#   2. Node.js >= 22 (TUI runtime; installed privately if the system lacks it)
#   3. raven         (installed as a global uv tool -> ~/.local/bin/raven)
#
# POSIX sh on purpose (runs under dash/ash, not just bash).
set -eu

# --- config ---------------------------------------------------------------
MIN_NODE_MAJOR=22
RAVEN_HOME="${RAVEN_HOME:-${HOME:?HOME is required, or set RAVEN_HOME explicitly}/.raven}"
NODE_RUNTIME_DIR="$RAVEN_HOME/runtime"

# --- pretty output ---------------------------------------------------------
info()  { printf '\033[1;34m>\033[0m %s\n' "$1"; }
ok()    { printf '\033[1;32m+\033[0m %s\n' "$1"; }
warn()  { printf '\033[1;33m!\033[0m %s\n' "$1" >&2; }
die()   { printf '\033[1;31mx\033[0m %s\n' "$1" >&2; exit 1; }
have()  { command -v "$1" >/dev/null 2>&1; }

# --- 0. platform detection -------------------------------------------------
detect_platform() {
  os="$(uname -s)"
  arch="$(uname -m)"
  case "$os" in
    Darwin) NODE_OS="darwin" ;;
    Linux)  NODE_OS="linux" ;;
    *) die "Unsupported OS: $os (only macOS / Linux; on Windows use install.ps1)" ;;
  esac
  case "$arch" in
    arm64|aarch64) NODE_ARCH="arm64" ;;
    x86_64|amd64)  NODE_ARCH="x64" ;;
    *) die "Unsupported architecture: $arch" ;;
  esac
}

# --- 1. ensure uv ----------------------------------------------------------
ensure_uv() {
  if have uv; then
    ok "uv already installed ($(uv --version))"
    return
  fi
  info "uv not found, installing..."
  curl -fsSL https://astral.sh/uv/install.sh | sh
  # uv installs to ~/.local/bin (or $XDG_BIN_HOME) -- make it visible for the
  # rest of this script even before the shell profile is re-sourced.
  export PATH="$HOME/.local/bin:$PATH"
  have uv || die "uv still unavailable after install; check PATH (expected in ~/.local/bin)"
  ok "uv installed"
}

# --- 2. ensure Node >= 22 --------------------------------------------------
# Returns 0 if a usable system node is found.
system_node_ok() {
  have node || return 1
  v="$(node --version 2>/dev/null | sed 's/^v//; s/\..*//')"
  [ -n "$v" ] && [ "$v" -ge "$MIN_NODE_MAJOR" ] 2>/dev/null
}

# Resolve the latest v22 LTS version string (e.g. v22.20.0) from nodejs.org,
# without requiring jq/python. Falls back to a pinned version if the index
# can't be reached.
latest_node_v22() {
  idx="$(curl -fsSL https://nodejs.org/dist/index.json 2>/dev/null || true)"
  ver="$(printf '%s' "$idx" | tr ',' '\n' | grep -o '"version":"v22\.[0-9.]*"' \
         | head -n1 | sed 's/.*"v/v/; s/"$//')"
  [ -n "$ver" ] && printf '%s' "$ver" || printf 'v22.20.0'
}

# Print the path to a Raven-provisioned private node binary (first match), or
# return non-zero if none. Iterating the glob avoids passing multiple words to
# `[ -x ... ]` (which errors) when several versioned dirs linger.
private_node_bin() {
  for n in "$NODE_RUNTIME_DIR"/node-v22*/bin/node; do
    [ -x "$n" ] || continue
    # Actually run it -- a half-extracted / corrupt binary is +x but won't run,
    # and must NOT be mistaken for a ready runtime (else we'd never re-download).
    "$n" --version >/dev/null 2>&1 && { printf '%s' "$n"; return 0; }
  done
  return 1
}

ensure_node() {
  if system_node_ok; then
    ok "Node.js already meets requirement ($(node --version))"
    return
  fi
  # Already provisioned privately by a previous run?
  if pn="$(private_node_bin)"; then
    ok "Raven private Node already present ($pn)"
    return
  fi

  info "Node.js >= $MIN_NODE_MAJOR not found; downloading a private runtime (does not touch the system)..."
  ver="$(latest_node_v22)"
  pkg="node-${ver}-${NODE_OS}-${NODE_ARCH}"
  url="https://nodejs.org/dist/${ver}/${pkg}.tar.gz"
  mkdir -p "$NODE_RUNTIME_DIR"
  tmp="$(mktemp -d)"
  info "  $url"
  curl -fsSL "$url" -o "$tmp/node.tar.gz" || die "Node download failed: $url"

  # Supply-chain integrity: verify the tarball against the official
  # SHASUMS256.txt before extracting/executing it. Node publishes this file
  # next to every release.
  if curl -fsSL "https://nodejs.org/dist/${ver}/SHASUMS256.txt" -o "$tmp/SHASUMS256.txt" 2>/dev/null; then
    expected="$(awk -v f="${pkg}.tar.gz" '$2==f {print $1}' "$tmp/SHASUMS256.txt")"
    if [ -n "$expected" ]; then
      if have shasum; then
        actual="$(shasum -a 256 "$tmp/node.tar.gz" | awk '{print $1}')"
      elif have sha256sum; then
        actual="$(sha256sum "$tmp/node.tar.gz" | awk '{print $1}')"
      else
        actual=""; warn "shasum/sha256sum not found; skipping verification"
      fi
      if [ -n "$actual" ] && [ "$actual" != "$expected" ]; then
        rm -rf "$tmp"
        die "Node checksum mismatch (expected $expected, got $actual)"
      fi
      [ -n "$actual" ] && ok "Node tarball SHA256 verified"
    else
      warn "SHASUMS256.txt did not list ${pkg}.tar.gz; skipping verification"
    fi
  else
    warn "Could not fetch SHASUMS256.txt; skipping integrity check"
  fi

  tar -xzf "$tmp/node.tar.gz" -C "$NODE_RUNTIME_DIR"
  rm -rf "$tmp"
  [ -x "$NODE_RUNTIME_DIR/$pkg/bin/node" ] || die "Node executable not found after extraction"
  # Run it once now: catches a libc mismatch (e.g. glibc tarball on Alpine/musl)
  # at install time instead of letting `raven tui` fail later on the user's box.
  "$NODE_RUNTIME_DIR/$pkg/bin/node" --version >/dev/null 2>&1 \
    || die "Downloaded Node cannot run on this machine (possible libc mismatch, e.g. Alpine/musl). Install Node >= ${MIN_NODE_MAJOR} via your system package manager."
  ok "Node private runtime ready: $NODE_RUNTIME_DIR/$pkg"
  # raven's find_node() globs ~/.raven/runtime/node-*/bin/node automatically,
  # so no PATH change is needed for `raven tui` to find it.
}

# --- 3. install raven ------------------------------------------------------
install_raven() {
  # Local mode: run from a raven source checkout -> editable install of the
  # working tree (what a developer wants). Otherwise install from git.
  script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
  if [ -f "$script_dir/pyproject.toml" ] && grep -q '^name = "raven"' "$script_dir/pyproject.toml" 2>/dev/null; then
    info "Local raven source detected; editable install: $script_dir"
    # The TUI bundle must exist before first run. In a dev checkout it isn't
    # committed, so build it now if Node is available.
    if [ ! -f "$script_dir/ui-tui/dist/entry.js" ]; then
      node_bin="$(command -v node || true)"
      [ -n "$node_bin" ] || node_bin="$(private_node_bin || true)"
      if [ -n "$node_bin" ] && [ -x "$node_bin" ]; then
        node_dir="$(dirname "$node_bin")"
        # npm ships alongside node, but verify explicitly before relying on it.
        if PATH="$node_dir:$PATH" command -v npm >/dev/null 2>&1; then
          info "Building the TUI bundle (ui-tui/dist/entry.js)..."
          ( cd "$script_dir/ui-tui" && PATH="$node_dir:$PATH" npm ci && PATH="$node_dir:$PATH" npm run build )
        else
          warn "Found node but not npm; skipping TUI build; raven tui may not work"
        fi
      else
        warn "No usable node found; skipping TUI build; raven tui may not work"
      fi
    fi
    # Install all channel adapters by default. If the umbrella extra fails to
    # resolve/build on this platform, fall back to base raven so one broken
    # channel SDK cannot block the whole install.
    if ! uv tool install --force -e "$script_dir[channels]"; then
      warn "Channel dependencies failed to install; installed base raven only. Some channels stay unavailable (see: raven channels list)."
      uv tool install --force -e "$script_dir"
    fi
  else
    # Remote mode: install the latest published release wheel, which bundles
    # the prebuilt ui-tui/dist/entry.js (built by CI). We deliberately do NOT
    # install from git here -- the TUI bundle is a gitignored build artifact,
    # so a git install would yield a raven whose `raven tui` cannot start.
    # Override RAVEN_WHEEL_URL to pin a specific wheel.
    wheel_url="${RAVEN_WHEEL_URL:-}"
    if [ -z "$wheel_url" ]; then
      info "Resolving the latest raven release from GitHub..."
      wheel_url="$(curl -fsSL "https://api.github.com/repos/EverMind-AI/raven/releases/latest" 2>/dev/null \
        | grep -oE 'https://[^"]*/raven-[^"]*\.whl' | head -n1)"
    fi
    [ -n "$wheel_url" ] || die "Could not resolve the latest raven release wheel from GitHub (check network, or set RAVEN_WHEEL_URL to a wheel URL)."
    info "  installing $wheel_url"
    if ! uv tool install --force "raven[channels] @ $wheel_url"; then
      warn "Channel dependencies failed to install; installed base raven only. Some channels stay unavailable (see: raven channels list)."
      uv tool install --force "$wheel_url"
    fi
  fi
  # Ensure ~/.local/bin (uv tool bin dir) is on PATH for future shells.
  uv tool update-shell || true
  ok "raven installed"
}

# --- main ------------------------------------------------------------------
main() {
  have curl || die "curl is required; please install it first"
  detect_platform
  ensure_uv
  ensure_node
  install_raven

  printf '\n'
  ok "All set! Open a new terminal (or source your shell profile), then run:"
  printf '\n    \033[1mraven\033[0m            # enter the TUI\n'
  printf '    \033[1mraven agent\033[0m -m "hello"\n\n'
  if ! printf '%s' "$PATH" | grep -q "$HOME/.local/bin"; then
    warn "Your current PATH does not include ~/.local/bin yet -- open a new terminal, or run: export PATH=\"\$HOME/.local/bin:\$PATH\""
  fi
}

main "$@"
