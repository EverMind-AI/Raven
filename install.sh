#!/bin/sh
# Raven one-line installer (macOS / Linux).
#
#   Remote (website):   curl -fsSL https://raven.evermind.ai/install.sh | sh
#   Local (dev clone):  git clone ... && cd raven && ./install.sh
#
# A piped run always installs the published release wheel, even from inside a
# clone. Set RAVEN_LOCAL_SRC=<dir> to force an editable install of a checkout.
# Set RAVEN_MINIMAL=1 to skip the chromium download and the LibreOffice offer;
# the wheel install itself is unchanged. Set RAVEN_NO_LAUNCH=1 to skip the
# closing `raven web` (CI, Dockerfiles), so the script returns.
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
# It then ends in the product: `raven web` opens the page in a browser and holds
# this terminal, so the install finishes on something running rather than on a
# hint to go and start it.
#
# Probe rule: this script is served from main and installs the latest release,
# which can predate a subcommand main already knows about. Every `raven <sub>`
# call below is preceded by `raven <sub> --help`; when the probe fails, the
# script finishes on the one command every release has.
#
# POSIX sh on purpose (runs under dash/ash, not just bash).
set -eu

# --- config ---------------------------------------------------------------
MIN_NODE_MAJOR=22
RAVEN_HOME="${RAVEN_HOME:-${HOME:?HOME is required, or set RAVEN_HOME explicitly}/.raven}"
NODE_RUNTIME_DIR="$RAVEN_HOME/runtime"

# uv does not byte-compile by default, so the first process to import a module
# compiles it. For raven that process is the memory service the first session
# starts, and it imports the serving stack -- measured at 22s on a fresh
# install against 2s once compiled, which overruns the readiness budget and
# costs that session its long-term memory. Paid here instead, where a wait is
# what the user is already watching.
export UV_COMPILE_BYTECODE=1

# --- pretty output ---------------------------------------------------------
info()  { printf '\033[1;34m>\033[0m %s\n' "$1"; }
ok()    { printf '\033[1;32m+\033[0m %s\n' "$1"; }
warn()  { printf '\033[1;33m!\033[0m %s\n' "$1" >&2; }
die()   { printf '\033[1;31mx\033[0m %s\n' "$1" >&2; exit 1; }
have()  { command -v "$1" >/dev/null 2>&1; }
sha256_of() {
  if have sha256sum; then sha256sum "$1" | cut -d' ' -f1
  elif have shasum; then shasum -a 256 "$1" | cut -d' ' -f1
  else printf ''; fi
}

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
  provision_private_node
}

# Download, verify and extract a private Node runtime into $NODE_RUNTIME_DIR.
# Split out of ensure_node because build_web_assets needs it on a second path:
# a system node packaged without npm satisfies ensure_node and leaves the build
# with no npm to call. Every failure here is fatal, so a caller that must not
# die on a failed download runs this in a subshell.
provision_private_node() {
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
# a clone install has no TUI and no page. Both must exist, and both must be no
# older than the sources they were built from, before first run.
#
# Missing is not the only reason to build. The install is editable, so Python
# tracks the checkout with no further help -- but these two are compiled, and
# nothing relinks them. Built once and then only ever checked for existence,
# they keep serving whatever the tree held at first install while every `.py`
# beside them moves on, which reads as a frontend that ignores your edits.
#
# True when the artifact is missing, or any source under the named directories
# is newer than it. mtime is the right question: git stamps every file it
# rewrites with the time it wrote it, so a pull or a branch switch that touched
# the frontend sorts after the artifact and one that did not leaves it alone.
is_stale() {
  artifact="$1"
  shift
  [ -f "$artifact" ] || return 0
  for dir in "$@"; do
    [ -d "$dir" ] || [ -f "$dir" ] || continue
    # node_modules is rewritten by this script's own `npm ci`, which would
    # leave the artifact permanently stale; dist and .modern hold build output,
    # the artifact among it.
    newer="$(
      find "$dir" \
        \( -name node_modules -o -name dist -o -name .modern \) -prune -o \
        -type f -newer "$artifact" -print 2>/dev/null | head -n 1
    )"
    [ -z "$newer" ] || return 0
  done
  return 1
}

# Resolve a node bin directory that also carries npm. Sets `node_dir` (empty
# when there is none) and `blocker` (the reason, empty on success).
#
# ensure_node may have provisioned a private runtime that never reaches PATH,
# so look there before giving up. npm ships alongside node, but verify it
# explicitly rather than assume: Debian and Ubuntu package the two separately,
# and a system node >= 22 satisfies ensure_node, so on those a build would find
# node and no npm with no private runtime ever fetched. The official tarball
# carries npm beside node, so fetch one at that point rather than skip both
# builds on a machine one download away from running them.
#
# That fetch is subshelled because provisioning is fatal and this caller must
# not be: the system node still runs `raven tui`, so a download that fails here
# is a skipped build, not a failed install.
resolve_node_dir() {
  node_dir=""
  blocker=""
  node_bin="$(command -v node || true)"
  [ -n "$node_bin" ] || node_bin="$(private_node_bin || true)"
  if [ -z "$node_bin" ] || [ ! -x "$node_bin" ]; then
    blocker="No usable node found"
    return
  fi
  if PATH="$(dirname "$node_bin"):$PATH" command -v npm >/dev/null 2>&1; then
    node_dir="$(dirname "$node_bin")"
    return
  fi
  npm_node="$(private_node_bin || true)"
  if [ -z "$npm_node" ]; then
    info "Found node but not npm; fetching a private Node runtime that carries both..."
    if ( provision_private_node ); then npm_node="$(private_node_bin || true)"; fi
  fi
  if [ -n "$npm_node" ] && PATH="$(dirname "$npm_node"):$PATH" command -v npm >/dev/null 2>&1; then
    node_dir="$(dirname "$npm_node")"
  else
    blocker="Found node but not npm"
  fi
}

build_web_assets() {
  src="$1"
  need_tui=0
  need_page=0
  if is_stale "$src/ui-tui/dist/entry.js" "$src/ui-tui"; then need_tui=1; fi
  # The page inlines the shared catalogue (ui-web/build.py reads i18n/messages.json),
  # so a catalogue-only change is a page change.
  if is_stale "$src/ui-web/dist/index.html" "$src/ui-web" "$src/i18n"; then need_page=1; fi
  [ "$need_tui" = 1 ] || [ "$need_page" = 1 ] || return 0

  # One probe for both builds.
  resolve_node_dir

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
    uv export -q --directory "$script_dir" --frozen --all-extras --no-hashes --no-emit-workspace -o "$constraints"
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
    #
    # One discovery step, no GitHub API: the release page redirect names the
    # latest stable tag, and everything else is derived from it, because the
    # wheel, the locked constraints and the plugin list sit in one release
    # directory. The API caps unauthenticated callers at 60 requests/hour per
    # IP, which a shared egress exhausts, and its JSON was only ever grepped
    # for file names.
    wheel_url="${RAVEN_WHEEL_URL:-}"
    if [ -z "$wheel_url" ]; then
      info "Resolving the latest raven release from GitHub..."
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
    release_dir="${wheel_url%/*}"
    # The locked constraints from the same release directory, so they always
    # match the wheel being installed, including when RAVEN_WHEEL_URL pins an
    # older wheel. Missing asset / download failure -> install without pinning
    # rather than fail.
    constraints="$(mktemp)"
    c_args=""
    if curl -fsSL "${RAVEN_CONSTRAINTS_URL:-$release_dir/raven-constraints.txt}" -o "$constraints" 2>/dev/null; then
      c_args="-c $constraints"
    else
      warn "Could not download locked constraints; installing without version pinning."
    fi
    # The plugin list: the wheels this release ships beside raven, one
    # `name @ url` line each, written by the release workflow from what it
    # built. What a complete install is made of lives there, not here, and
    # `raven upgrade` installs from the same file. A release without one
    # (0.1.13 and older) installs raven alone, as it always did.
    plugins="$(mktemp)"
    memory_only="$(mktemp)"
    p_args=""
    m_args=""
    if curl -fsSL "$release_dir/raven-plugins.txt" -o "$plugins" 2>/dev/null && grep -q '[^[:space:]]' "$plugins"; then
      info "  with the release's plugins:"
      sed 's/^/    /' "$plugins"
      # Both option pairs expand unquoted below and must stay two words each;
      # mktemp paths carry no spaces.
      p_args="--with-requirements $plugins"
      grep '^everos-memory ' "$plugins" > "$memory_only" || true
      if [ -s "$memory_only" ] && ! cmp -s "$plugins" "$memory_only"; then
        m_args="--with-requirements $memory_only"
      fi
    else
      warn "This release carries no plugin list; long-term memory, Raven-Design and Raven-PPT stay off (raven doctor explains)."
    fi
    info "  installing $wheel_url"
    # shellcheck disable=SC2086  # $c_args and $1 are intentional word-split option pairs.
    install_rung() {
      uv tool install --force $c_args $1 "$2"
    }
    # Two independent things can fail: the channel extras, and the plugins
    # (the engines' native builds first, the memory plugin after). A failed
    # attempt does not say which, so the rungs walk both axes and stop at the
    # first that lands -- the largest install this machine can build -- and
    # warn about exactly what that rung lacks:
    #   1 channels + all plugins      4 base + memory plugin
    #   2 base + all plugins          5 channels, no plugins
    #   3 channels + memory plugin    6 base, no plugins
    lost_channels="Channel dependencies failed to install; some channels stay unavailable (see: raven channels list)."
    lost_engines="A product engine failed to install; Raven-Design and Raven-PPT stay disabled (raven doctor explains)."
    lost_plugins="No plugin could be installed; long-term memory, Raven-Design and Raven-PPT stay off (raven doctor explains)."
    if install_rung "$p_args" "raven[channels] @ $wheel_url"; then
      :
    elif install_rung "$p_args" "$wheel_url"; then
      warn "$lost_channels"
    elif [ -n "$m_args" ] && install_rung "$m_args" "raven[channels] @ $wheel_url"; then
      warn "$lost_engines"
    elif [ -n "$m_args" ] && install_rung "$m_args" "$wheel_url"; then
      warn "$lost_engines"
      warn "$lost_channels"
    elif [ -n "$p_args" ] && install_rung "" "raven[channels] @ $wheel_url"; then
      warn "$lost_plugins"
    elif [ -n "$p_args" ] && install_rung "" "$wheel_url"; then
      warn "$lost_plugins"
      warn "$lost_channels"
    else
      die "Raven install failed."
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

# macOS without Homebrew: the release dmg, pinned the way the cask pins it --
# one version, one digest per build. The stable directory drops a release once
# the next one ships and the archive keeps it byte for byte, so the archive is
# the fallback rather than the only source. Verified before it is mounted.
LO_VERSION="26.8.0"
LO_BUILD="26.8.0.3"
LO_SHA256_ARM64="8858d8058da4f862f47559486814e65efc27294da67c5e4bb56b006b1ee59f89"
LO_SHA256_X64="2dcbce4894e01bc1ecd594658e2cbda70ff7bfcd0b310f35d38887797172d09e"

# Where a Mac's apps live; overridable so a test can install into a directory
# of its own. raven's own lookup (raven/utils/office.py) checks the same two.
MACOS_APPS="${RAVEN_MACOS_APPS:-/Applications}"

# The cask's own trick: a two-line launcher rather than a symlink, because
# soffice finds the rest of its bundle from the path it was started by. On PATH
# because the model checks a deck by running `soffice` itself.
write_soffice_launcher() {
  launcher_dir="$HOME/.local/bin"
  mkdir -p "$launcher_dir" \
    && printf '#!/bin/sh\nexec "%s/Contents/MacOS/soffice" "$@"\n' "$1" > "$launcher_dir/soffice" \
    && chmod +x "$launcher_dir/soffice" \
    && ok "LibreOffice launcher: $launcher_dir/soffice"
}

install_libreoffice_dmg() {
  case "$NODE_ARCH" in
    arm64) lo_dir=aarch64; lo_arch=aarch64; lo_sha="$LO_SHA256_ARM64" ;;
    x64) lo_dir=x86_64; lo_arch=x86-64; lo_sha="$LO_SHA256_X64" ;;
    *) return 1 ;;
  esac
  # /Applications takes an admin's write without sudo; anyone else gets their
  # own Applications folder.
  apps="$MACOS_APPS"
  [ -w "$apps" ] || apps="$HOME/Applications"
  work="$(mktemp -d "${TMPDIR:-/tmp}/raven-libreoffice.XXXXXX")" || return 1
  dmg="$work/LibreOffice.dmg"
  info "Downloading LibreOffice $LO_VERSION for deck preview (about 300 MB)..."
  fetched=""
  for url in \
    "https://download.documentfoundation.org/libreoffice/stable/$LO_VERSION/mac/$lo_dir/LibreOffice_${LO_VERSION}_MacOS_$lo_arch.dmg" \
    "https://downloadarchive.documentfoundation.org/libreoffice/old/$LO_BUILD/mac/$lo_dir/LibreOffice_${LO_BUILD}_MacOS_$lo_arch.dmg"; do
    if curl -fsSL --retry 2 --max-time 1800 -o "$dmg" "$url" && [ "$(sha256_of "$dmg")" = "$lo_sha" ]; then
      fetched=1
      break
    fi
  done
  if [ -z "$fetched" ]; then
    rm -rf "$work"
    return 1
  fi
  mkdir -p "$work/mnt" "$apps" || { rm -rf "$work"; return 1; }
  hdiutil attach -nobrowse -readonly -noverify -noautoopen -quiet -mountpoint "$work/mnt" "$dmg" \
    || { rm -rf "$work"; return 1; }
  copied=0
  ditto "$work/mnt/LibreOffice.app" "$apps/LibreOffice.app" || copied=1
  hdiutil detach -quiet "$work/mnt" || hdiutil detach -quiet -force "$work/mnt" || true
  rm -rf "$work"
  [ "$copied" = 0 ] || { rm -rf "$apps/LibreOffice.app"; return 1; }
  ok "LibreOffice $LO_VERSION installed to $apps/LibreOffice.app"
  write_soffice_launcher "$apps/LibreOffice.app"
}

install_office() {
  # soffice and libreoffice are the two launcher names the runtime resolves
  # (raven/utils/office.py); either one means deck preview already works.
  have soffice && return 0
  have libreoffice && return 0
  case "$NODE_OS" in
    darwin)
      # An app already in an Applications folder (the libreoffice.org dmg, or
      # an earlier run of this script) only lacks a launcher on PATH. Never
      # install a second copy over it.
      for app in "$MACOS_APPS/LibreOffice.app" "$HOME/Applications/LibreOffice.app"; do
        if [ -x "$app/Contents/MacOS/soffice" ]; then
          write_soffice_launcher "$app" \
            || warn "Could not write ~/.local/bin/soffice; raven still finds $app, but a plain soffice command will not."
          return 0
        fi
      done
      # A cask needs no sudo, so install directly rather than prompting.
      if have brew; then
        info "Installing LibreOffice (deck preview)..."
        brew install --cask libreoffice && return 0
        warn "brew could not install LibreOffice; fetching it from libreoffice.org instead."
      fi
      install_libreoffice_dmg \
        || warn "LibreOffice install failed; deck preview stays off. Install it later from https://www.libreoffice.org/download/ or with: brew install --cask libreoffice"
      ;;
    linux)
      if ! have apt-get; then
        warn "LibreOffice not found; deck preview stays off. Install it with your system package manager (packages: libreoffice fonts-noto-cjk)."
        return 0
      fi
      # Installing needs sudo, so ask first -- and under `curl | sh` stdin is
      # the script itself, so the answer must come from the terminal. The
      # /dev/tty node can exist yet be unopenable (CI, cron, docker -t
      # without -i), so probe by opening it rather than stat-ing it; no
      # openable terminal means skip cleanly, never hang on the read.
      if ! { : < /dev/tty; } 2>/dev/null || ! have sudo; then
        warn "LibreOffice not found; deck preview stays off. Install it later with: sudo apt-get install -y libreoffice fonts-noto-cjk"
        return 0
      fi
      # Default yes: for the deck lane this is the one dependency that matters
      # (the whole render-truth capability is soffice being present), and the
      # macOS path already installs it without asking. sudo's own password
      # prompt still stands between Enter and any change.
      printf 'Install LibreOffice and a Chinese font for deck preview (needs sudo)? Without them a deck still builds, but no page is ever rendered, measured or checked. [Y/n] '
      # A failed read is not an Enter: Ctrl-D, or a tty that closed after the
      # gate passed, must decline -- only a deliberate empty Enter accepts.
      answer=""
      read -r answer < /dev/tty || {
        warn "Skipping LibreOffice (no answer read); deck preview stays off. Install it later with: sudo apt-get install -y libreoffice fonts-noto-cjk"
        return 0
      }
      case "$answer" in
        n|N|[nN][oO])
          warn "Skipping LibreOffice; deck preview stays off. Install it later with: sudo apt-get install -y libreoffice fonts-noto-cjk"
          ;;
        *)
          # sudo's password prompt also reads stdin: give it the tty too.
          # shellcheck disable=SC2024  # input redirect on purpose; opening /dev/tty needs no elevation.
          sudo apt-get install -y libreoffice fonts-noto-cjk < /dev/tty \
            || warn "LibreOffice install failed; deck preview stays off. Retry later with: sudo apt-get install -y libreoffice fonts-noto-cjk"
          ;;
      esac
      ;;
  esac
}

# --- 4b. Chinese on a rendered page -----------------------------------------
# A .pptx names its fonts and carries none, so a deck's Chinese is drawn with
# whatever this machine's LibreOffice can reach. With nothing, the conversion
# still succeeds and every Han glyph comes out as a box -- in the preview panel,
# in the delivery thumbnail, and in the page the model renders to check its work.
#
# Linux: LibreOffice's apt package brings no CJK face. The offer above installs
# fonts-noto-cjk alongside it; otherwise a pinned Noto Sans SC goes into the
# user's font directory, which fontconfig reads without being told. The pin is
# the Simplified Chinese subset, Regular weight only: bold is synthesized, and
# Traditional Chinese or Japanese glyphs outside the subset still draw as boxes.
# Enough for zh-CN decks; fonts-noto-cjk is the full answer.
#
# macOS needs nothing here. The system already ships Han faces; LibreOffice's
# macOS build just cannot see them when it renders headless, and raven links
# them into the profile of every conversion it runs, and into the default one
# the model's own soffice uses (raven/utils/office.py) -- no file outside the
# user's home, no password.

HAN_FONT_URL="https://raw.githubusercontent.com/notofonts/noto-cjk/Sans2.004/Sans/SubsetOTF/SC/NotoSansSC-Regular.otf"
HAN_FONT_SHA256="faa6c9df652116dde789d351359f3d7e5d2285a2b2a1f04a2d7244df706d5ea9"
HAN_FONT_BYTES="8331336"
HAN_FONT_NAME="NotoSansSC-Regular.otf"

# LibreOffice reads the same fontconfig as fc-list here, so its answer holds;
# the pinned file is checked by name for a host with the library but no fc-list.
linux_has_han_face() {
  [ -n "$(fc-list :lang=zh family 2>/dev/null)" ] && return 0
  [ -f "${XDG_DATA_HOME:-$HOME/.local/share}/fonts/$HAN_FONT_NAME" ]
}

install_linux_cjk_font() {
  dir="${XDG_DATA_HOME:-$HOME/.local/share}/fonts"
  hint="Install one with your system package manager (package: fonts-noto-cjk)."
  mkdir -p "$dir" || { warn "Could not create $dir; Chinese pages in a deck will render as boxes. $hint"; return 1; }
  part="$dir/.$HAN_FONT_NAME.$$.part"
  info "Downloading a Chinese font for deck preview..."
  # Size and digest both, before the file is put in place: a truncated OTF
  # still parses and draws nothing, which is the failure this step exists for.
  if curl -fsSL --max-time 120 -o "$part" "$HAN_FONT_URL" \
    && [ "$(wc -c < "$part" | tr -d ' ')" = "$HAN_FONT_BYTES" ] \
    && { actual="$(sha256_of "$part")"; [ -z "$actual" ] || [ "$actual" = "$HAN_FONT_SHA256" ]; } \
    && mv -f "$part" "$dir/$HAN_FONT_NAME"; then
    have fc-cache && fc-cache -f "$dir" >/dev/null 2>&1
    ok "Chinese font installed to $dir/$HAN_FONT_NAME"
    return 0
  fi
  rm -f "$part"
  warn "The Chinese font download failed; Chinese pages in a deck will render as boxes. $hint"
  return 1
}

install_cjk_fonts() {
  [ "$NODE_OS" = linux ] || return 0
  # No LibreOffice (the offer declined, a distro without apt): nothing renders,
  # so the face would be 8 MB nobody reads.
  have soffice || have libreoffice || return 0
  linux_has_han_face && return 0
  install_linux_cjk_font || return 0
  # Previews and gallery covers cached before this were drawn without a Han
  # face, and they are keyed by the deck's own stamp, so nothing else would
  # ever replace them.
  rm -rf "$RAVEN_HOME/cache/pdf-preview" "$RAVEN_HOME/cache/deck-template-covers"
}


# --- 5. launch -------------------------------------------------------------
# The install ends on a running page. `--stop` first, because a gateway an
# earlier install left resident would be attached to instead of the build that
# just landed; `--foreground` then holds this terminal on a fresh one and opens
# the browser on it, so Ctrl-C here means what it says. The raven shim lands in
# `uv tool dir --bin`, which this shell's PATH may not carry yet, so invoke it
# by absolute path.
launch_web() {
  bin="$(uv tool dir --bin 2>/dev/null || true)/raven"
  [ -x "$bin" ] || bin="$HOME/.local/bin/raven"
  [ -x "$bin" ] || {
    warn "raven is not where this script looked for it; open a new terminal and run: raven"
    return 0
  }
  # The release this script just installed may predate `raven web` (0.1.13
  # does). Ask before calling, and end on the command every release has.
  if ! "$bin" web --help >/dev/null 2>&1; then
    printf '\n'
    ok "Raven installed. Open a new terminal (or source your shell profile), then run: raven"
    return 0
  fi
  printf '\n'
  ok "Starting Raven -- your browser will open in a moment. Ctrl-C here stops it."
  printf '\n'
  # `raven web` starts its engine as `python -m raven`, which puts the working
  # directory first on sys.path: run from inside a source checkout, a release
  # without the `-P` guard comes up on that checkout's raven. The guard is on
  # main; the published release this script installs may predate it.
  cd "${HOME:-/}" 2>/dev/null || cd /
  "$bin" web --stop >/dev/null 2>&1 || warn "could not stop a previous gateway; continuing"
  # The page's exit code is not the install's: Ctrl-C is how a foreground page
  # ends, and the install above it already succeeded.
  "$bin" web --foreground || warn "the page ended with exit code $?; start it again with: raven web"
}

# --- main ------------------------------------------------------------------
main() {
  have curl || die "curl is required; please install it first"
  detect_platform
  ensure_uv
  ensure_node
  install_raven

  [ -n "${RAVEN_MINIMAL:-}" ] || install_browser
  [ -n "${RAVEN_MINIMAL:-}" ] || install_office
  [ -n "${RAVEN_MINIMAL:-}" ] || install_cjk_fonts

  # Before the launch, not after: the page holds this terminal until Ctrl-C,
  # and `uv tool update-shell` only reaches future shells.
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) warn "Your current PATH does not include ~/.local/bin yet -- open a new terminal, or run: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
  esac

  [ -n "${RAVEN_NO_LAUNCH:-}" ] || launch_web
}

main "$@"
