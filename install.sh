#!/bin/sh
# Raven one-line installer (macOS / Linux).
#
#   Remote (website):   curl -fsSL https://raven.evermind.ai/install.sh | sh
#   Local (dev clone):  git clone ... && cd raven && ./install.sh
#
# A piped run always installs the published release wheel, even from inside a
# clone. Set RAVEN_LOCAL_SRC=<dir> to force an editable install of a checkout.
# Set RAVEN_MINIMAL=1 to skip the chromium download and the LibreOffice offer;
# the wheel install itself is unchanged.
#
# Goal: a clean machine ends up able to run `raven` / `raven tui` from any
# directory with no manual steps. The script is idempotent -- it detects what
# is already present and only fills the gaps:
#   1. uv            (Python toolchain + package manager)
#   2. Node.js >= 22 (TUI runtime; installed privately if the system lacks it)
#   3. raven         (installed as a global uv tool -> ~/.local/bin/raven)
#   4. chromium      (browser-tool runtime; downloaded by playwright)
#   5. LibreOffice   (deck preview; installed on macOS, offered on Linux)
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

# --- 2b. build the web assets a source checkout does not carry -------------
# `ui-tui/dist/entry.js` (the TUI bundle) and `ui-web/dist/index.html` (the page
# `raven web` serves) are both gitignored build artifacts. A release wheel
# carries them; an editable install of a checkout gets neither, so without this
# a clone install has no TUI and no page. Both must exist before first run.
build_web_assets() {
  src="$1"
  need_tui=0
  need_page=0
  [ -f "$src/ui-tui/dist/entry.js" ] || need_tui=1
  [ -f "$src/ui-web/dist/index.html" ] || need_page=1
  [ "$need_tui" = 1 ] || [ "$need_page" = 1 ] || return 0

  # One probe for both builds. ensure_node may have provisioned a private
  # runtime that never reaches PATH, so look there before giving up.
  node_bin="$(command -v node || true)"
  [ -n "$node_bin" ] || node_bin="$(private_node_bin || true)"
  node_dir=""
  blocker=""
  if [ -z "$node_bin" ] || [ ! -x "$node_bin" ]; then
    blocker="No usable node found"
  elif ! PATH="$(dirname "$node_bin"):$PATH" command -v npm >/dev/null 2>&1; then
    # npm ships alongside node, but verify explicitly before relying on it.
    blocker="Found node but not npm"
  else
    node_dir="$(dirname "$node_bin")"
  fi

  if [ "$need_tui" = 1 ]; then
    if [ -n "$blocker" ]; then
      warn "$blocker; skipping TUI build; raven tui may not work"
    else
      info "Building the TUI bundle (ui-tui/dist/entry.js)..."
      ( cd "$src/ui-tui" && PATH="$node_dir:$PATH" npm ci && PATH="$node_dir:$PATH" npm run build )
    fi
  fi

  if [ "$need_page" = 1 ]; then
    if [ -n "$blocker" ]; then
      warn "$blocker; skipping the served-page build; raven web will not start"
    else
      info "Building the served page (ui-web/dist/index.html)..."
      # Warned rather than propagated, unlike the bundle above: bare `raven`
      # opens the TUI, so a machine that cannot build the page still gets the
      # surface this script exists to deliver.
      build_page "$src" "$node_dir" \
        || warn "The served page did not build (see above); raven web will not start"
    fi
  fi
}

# Vite emits ui-web/.modern/modern.iife.js, then ui-web/build.py inlines it with the
# page sources and the shared i18n catalogue into the single-file dist.
#
# Kept to one `&&` chain on purpose: `set -e` does not apply inside a function
# whose caller guards it with `||`, so a second statement would run even after
# npm ci had failed. Python comes from uv, which is already a hard requirement
# here, rather than from a bare `python3` -- on a machine without the Command
# Line Tools that name is a stub macOS answers with an install prompt.
build_page() {
  ( cd "$1/ui-web" && PATH="$2:$PATH" npm ci && PATH="$2:$PATH" npm run build ) \
    && uv run --no-project python "$1/ui-web/build.py"
}

# --- 3. install raven ------------------------------------------------------
# True when $1 holds a raven source checkout (its own pyproject, not a dep's).
is_raven_source() {
  [ -f "$1/pyproject.toml" ] && grep -q '^name = "raven"' "$1/pyproject.toml" 2>/dev/null
}

install_raven() {
  # Local mode: run from a raven source checkout -> editable install of the
  # working tree (what a developer wants). Otherwise install the release wheel.
  #
  # "$0" names a real file only when this script runs as a file
  # (./install.sh). Piped through `curl ... | sh` the script arrives on stdin,
  # "$0" is "sh" and dirname "$0" is "." -- taking that as the source dir turns
  # a one-line install started from inside a clone into a silent editable
  # install of that working tree, whatever it happens to contain. So local mode
  # requires "$0" to be a file; RAVEN_LOCAL_SRC is the explicit opt-in for a
  # piped run.
  script_dir=""
  if [ -n "${RAVEN_LOCAL_SRC:-}" ]; then
    script_dir="$(CDPATH= cd -- "$RAVEN_LOCAL_SRC" 2>/dev/null && pwd || true)"
    [ -n "$script_dir" ] || die "RAVEN_LOCAL_SRC is not a directory: $RAVEN_LOCAL_SRC"
    is_raven_source "$script_dir" || die "RAVEN_LOCAL_SRC is not a raven source checkout: $script_dir"
  elif [ -f "$0" ]; then
    script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
    is_raven_source "$script_dir" || script_dir=""
  fi
  if [ -n "$script_dir" ]; then
    info "Local raven source detected; editable install: $script_dir"
    build_web_assets "$script_dir"
    # Pin to the locked dependency set so an install matches what we test.
    constraints="$(mktemp)"
    uv export --directory "$script_dir" --frozen --all-extras --no-hashes --no-emit-workspace -o "$constraints"
    # Install all channel adapters by default. If the umbrella extra fails to
    # resolve/build on this platform, fall back to base raven so one broken
    # channel SDK cannot block the whole install.
    # The default config names the everos memory backend, which ships as its
    # own distribution beside the wheel -- carry it, and degrade loudly (the
    # host boots memoryless and `raven doctor` says why) if it cannot build.
    memory_dir="$script_dir/plugins-dist/everos-memory"
    # Raven-Design and Raven-PPT keep their harness in a distribution too, and
    # the roster gates on it: discovery reads the `engine` block in each
    # agents/<product>/subagent.json and disables the row when that package is
    # not importable where raven runs. Installing raven without them therefore
    # yields two agents that are listed and cannot be dispatched to, with the
    # remedy stated nowhere the installer ran.
    design_dir="$script_dir/plugins-dist/design-engine"
    ppt_dir="$script_dir/plugins-dist/ppt-engine"
    # tool_install <target> [<plugin dir>...]. The directories ride in the
    # positional parameters rather than in one string: sh has no arrays, and a
    # checkout path holding a space must stay one argv entry -- encoding the
    # option/path pairs into a scalar splits every one of them and ends the
    # ladder at bare raven with no plugin installed. The loop rotates the list
    # in place, taking each directory off the front and appending its option
    # pair to the back, so what remains is exactly the pairs.
    tool_install() {
      spec="$1"
      shift
      remaining=$#
      while [ "$remaining" -gt 0 ]; do
        set -- "$@" --with-editable "$1"
        shift
        remaining=$((remaining - 1))
      done
      uv tool install --force -c "$constraints" "$@" -e "$spec"
    }
    # Four rungs, dropping one capability each: the engines carry native
    # builds (PDF, raster, plotting) that a platform can refuse on its own, so
    # they fall before the memory plugin rather than taking it down with them.
    if ! tool_install "$script_dir[channels]" "$memory_dir" "$design_dir" "$ppt_dir"; then
      warn "Channel dependencies failed to install; retrying with base raven. Some channels stay unavailable (see: raven channels list)."
      if ! tool_install "$script_dir" "$memory_dir" "$design_dir" "$ppt_dir"; then
        warn "A product engine failed to build; Raven-Design and Raven-PPT stay disabled (raven doctor explains)."
        if ! tool_install "$script_dir" "$memory_dir"; then
          warn "EverOS memory plugin failed to install; long-term memory stays off (raven doctor explains)."
          tool_install "$script_dir"
        fi
      fi
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
      release_json="$(curl -fsSL "https://api.github.com/repos/EverMind-AI/Raven/releases/latest" 2>/dev/null)"
      wheel_url="$(printf '%s' "$release_json" | grep -oE 'https://[^"]*/raven-[^"]*\.whl' | head -n1)"
      # The everos memory plugin ships as a sibling wheel from the same
      # release; older releases carry none, and its absence only means the
      # default memory backend degrades loudly at boot.
      everos_url="$(printf '%s' "$release_json" | grep -oE 'https://[^"]*/everos_memory-[^"]*\.whl' | head -n1)"
      design_url="$(printf '%s' "$release_json" | grep -oE 'https://[^"]*/design_engine-[^"]*\.whl' | head -n1)"
      ppt_url="$(printf '%s' "$release_json" | grep -oE 'https://[^"]*/ppt_engine-[^"]*\.whl' | head -n1)"
    fi
    if [ -z "$wheel_url" ]; then
      # The GitHub API caps unauthenticated callers at 60 requests/hour per IP, so a
      # shared egress can exhaust it. The release page carries no API quota: its
      # redirect names the latest stable tag, and the wheel URL is derived from it.
      warn "GitHub API returned no release wheel; falling back to the release page."
      tag="$(curl -fsS -o /dev/null -w '%{redirect_url}' \
        "https://github.com/EverMind-AI/Raven/releases/latest")" || tag=""
      # Same shape the CLI and install.ps1 enforce: the redirect must land on this
      # repository's tag page, and the version must be exactly three numeric fields
      # with no leading zeros.
      case "$tag" in
        https://github.com/EverMind-AI/Raven/releases/tag/v*) version="${tag##*/}" ;;
        *) version="" ;;
      esac
      version="${version#v}"
      case "$version" in
        *.*.*.*) version="" ;;
        *.*.*) ;;
        *) version="" ;;
      esac
      if [ -n "$version" ]; then
        v_rest="${version#*.}"
        for field in "${version%%.*}" "${v_rest%%.*}" "${v_rest#*.}"; do
          case "$field" in
            ""|*[!0-9]*|0[0-9]*) version="" ;;
          esac
        done
      fi
      if [ -n "$version" ]; then
        wheel_url="https://github.com/EverMind-AI/Raven/releases/download/v${version}/raven-${version}-py3-none-any.whl"
      fi
    fi
    [ -n "$wheel_url" ] || die "Could not resolve the latest raven release wheel from GitHub. Retry later, or set RAVEN_WHEEL_URL to a wheel URL."
    # Derive the locked-constraints URL from the wheel URL (same release dir) so
    # the constraints always match the wheel being installed, including when
    # RAVEN_WHEEL_URL pins an older wheel. Missing asset / download failure ->
    # install without pinning rather than fail.
    constraints_url="${RAVEN_CONSTRAINTS_URL:-}"
    if [ -z "$constraints_url" ]; then
      case "$wheel_url" in
        *.whl) constraints_url="${wheel_url%/*}/raven-constraints.txt" ;;
      esac
    fi
    c_args=""
    if [ -n "$constraints_url" ]; then
      constraints="$(mktemp)"
      if curl -fsSL "$constraints_url" -o "$constraints" 2>/dev/null; then
        c_args="-c $constraints"
      else
        warn "Could not download locked constraints; installing without version pinning."
      fi
    fi
    info "  installing $wheel_url"
    # shellcheck disable=SC2086  # $c_args is an intentional word-split option pair.
    p_args=""
    if [ -n "${everos_url:-}" ]; then
      # No spaces in the requirement: unquoted expansion must yield exactly
      # "--with" plus one argument, or uv rejects the extra words.
      p_args="--with everos-memory@$everos_url"
      info "  with memory plugin $everos_url"
    else
      warn "This release carries no EverOS memory plugin wheel; long-term memory stays off (raven doctor explains)."
    fi
    # Same shape for the product engines the roster gates Raven-Design and
    # Raven-PPT on. Absent wheels are a warning, not a failure: the release
    # still installs, and the two rows stay disabled the way discovery
    # already reports them.
    # Kept in their own variable, not appended to the memory plugin's: the
    # ladder below drops the engines one rung before the memory plugin, and a
    # shared scalar would take everos-memory down with the first engine that
    # cannot resolve or build.
    e_args=""
    if [ -n "${design_url:-}" ]; then
      e_args="$e_args --with design-engine@$design_url"
      info "  with design engine $design_url"
    else
      warn "This release carries no design-engine wheel; Raven-Design stays disabled (raven doctor explains)."
    fi
    if [ -n "${ppt_url:-}" ]; then
      e_args="$e_args --with ppt-engine@$ppt_url"
      info "  with deck engine $ppt_url"
    else
      warn "This release carries no ppt-engine wheel; Raven-PPT stays disabled (raven doctor explains)."
    fi
    # shellcheck disable=SC2086  # $c_args / $p_args / $e_args are intentional word-split option pairs.
    if ! uv tool install --force $c_args $p_args $e_args "raven[channels] @ $wheel_url"; then
      warn "Channel dependencies failed to install; retrying with base raven. Some channels stay unavailable (see: raven channels list)."
      if ! uv tool install --force $c_args $p_args $e_args "$wheel_url"; then
        warn "A product engine failed to install; Raven-Design and Raven-PPT stay disabled (raven doctor explains)."
        if ! uv tool install --force $c_args $p_args "$wheel_url"; then
          warn "EverOS memory plugin failed to install; long-term memory stays off (raven doctor explains)."
          uv tool install --force $c_args "$wheel_url"
        fi
      fi
    fi
  fi
  # Ensure ~/.local/bin (uv tool bin dir) is on PATH for future shells.
  uv tool update-shell || true
  ok "raven installed"
}

# --- 4. optional capabilities: browser + LibreOffice -------------------------
# Both installs are best-effort: raven itself is already installed by the time
# they run, so a failed download or a declined offer must never abort a
# completed install. RAVEN_MINIMAL skips both.

install_browser() {
  # The browser tool drives chromium through the playwright library inside the
  # raven tool venv, so both the probe and the download must use that venv's
  # python -- the system python knows nothing about this install.
  py="$(uv tool dir 2>/dev/null || true)/raven/bin/python"
  if [ ! -x "$py" ]; then
    warn "raven tool venv python not found; skipping the chromium download."
    return 0
  fi
  # A pinned RAVEN_WHEEL_URL and the release-page fallback install no engine
  # wheels, so playwright can be absent even after a green install.
  if ! "$py" -c "import playwright" 2>/dev/null; then
    warn "This install carries no browser library (a pinned wheel URL or the release-page fallback installs no engines); the browser tool stays off."
    return 0
  fi
  info "Downloading chromium for the browser tool..."
  # On Linux chromium may additionally need system libraries; playwright prints
  # the exact sudo command for them (--with-deps). We never run sudo ourselves.
  "$py" -m playwright install chromium \
    || warn "Chromium download failed; the browser tool stays off. Retry later with: $py -m playwright install chromium"
}

install_office() {
  # soffice and libreoffice are the two launcher names the runtime resolves
  # (raven/utils/office.py); either one means deck preview already works.
  have soffice && return 0
  have libreoffice && return 0
  case "$NODE_OS" in
    darwin)
      if have brew; then
        info "Installing LibreOffice (deck preview)..."
        # A cask needs no sudo, so install directly rather than prompting.
        brew install --cask libreoffice \
          || warn "LibreOffice install failed; deck preview stays off. Retry later with: brew install --cask libreoffice"
      else
        warn "LibreOffice not found; deck preview stays off. Install it later with: brew install --cask libreoffice"
      fi
      ;;
    linux)
      if ! have apt-get; then
        warn "LibreOffice not found; deck preview stays off. Install it with your system package manager (package: libreoffice)."
        return 0
      fi
      # Installing needs sudo, so ask first -- and under `curl | sh` stdin is
      # the script itself, so the answer must come from the terminal. The
      # /dev/tty node can exist yet be unopenable (CI, cron, docker -t
      # without -i), so probe by opening it rather than stat-ing it; no
      # openable terminal means skip cleanly, never hang on the read.
      if ! { : < /dev/tty; } 2>/dev/null || ! have sudo; then
        warn "LibreOffice not found; deck preview stays off. Install it later with: sudo apt-get install -y libreoffice"
        return 0
      fi
      # Default yes: for the deck lane this is the one dependency that matters
      # (the whole render-truth capability is soffice being present), and the
      # macOS path already installs it without asking. sudo's own password
      # prompt still stands between Enter and any change.
      printf 'Install LibreOffice for deck preview (needs sudo)? Without it a deck still builds, but no page is ever rendered, measured or checked. [Y/n] '
      answer=""
      read -r answer < /dev/tty || answer=""
      case "$answer" in
        n|N|[nN][oO])
          warn "Skipping LibreOffice; deck preview stays off. Install it later with: sudo apt-get install -y libreoffice"
          ;;
        *)
          # sudo's password prompt also reads stdin: give it the tty too.
          # shellcheck disable=SC2024  # input redirect on purpose; opening /dev/tty needs no elevation.
          sudo apt-get install -y libreoffice < /dev/tty \
            || warn "LibreOffice install failed; deck preview stays off. Retry later with: sudo apt-get install -y libreoffice"
          ;;
      esac
      ;;
  esac
}

# --- 5. capability summary ---------------------------------------------------
# One mouth for what actually landed: `raven doctor --install-summary` reads
# only what is importable/installed, needs no config, and always exits 0. The
# raven shim lands in `uv tool dir --bin`, which this shell's PATH may not
# carry yet, so invoke it by absolute path. Purely informational -- the caller
# guards the whole call so it can never fail a completed install.
print_capability_summary() {
  bin="$(uv tool dir --bin 2>/dev/null || true)/raven"
  [ -x "$bin" ] || bin="$HOME/.local/bin/raven"
  [ -x "$bin" ] || return 0
  printf '\n'
  info "Capabilities:"
  "$bin" doctor --install-summary \
    || warn "capability summary unavailable (raven doctor failed)"
}

# --- main ------------------------------------------------------------------
main() {
  have curl || die "curl is required; please install it first"
  # Read before installing so the closing hint can tell a first run from an
  # upgrade; the install itself never writes config.json (the wizard does).
  if [ -f "$RAVEN_HOME/config.json" ]; then
    had_config=1
  else
    had_config=0
  fi
  detect_platform
  ensure_uv
  ensure_node
  install_raven

  # The summary is not gated: a minimal install still sees what it skipped.
  [ -n "${RAVEN_MINIMAL:-}" ] || install_browser
  [ -n "${RAVEN_MINIMAL:-}" ] || install_office
  print_capability_summary || true

  printf '\n'
  if [ "$had_config" = 1 ]; then
    ok "Raven updated. Your config in $RAVEN_HOME is unchanged."
    printf '\n    \033[1mraven\033[0m    # continue where you left off\n\n'
    printf '  tip: next time you can upgrade in place with \033[1mraven upgrade\033[0m\n\n'
  else
    ok "All set! Open a new terminal (or source your shell profile), then run:"
    printf '\n    \033[1mraven\033[0m    # sets you up on first run, then opens the TUI\n\n'
  fi
  if ! printf '%s' "$PATH" | grep -q "$HOME/.local/bin"; then
    warn "Your current PATH does not include ~/.local/bin yet -- open a new terminal, or run: export PATH=\"\$HOME/.local/bin:\$PATH\""
  fi
}

main "$@"
