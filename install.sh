#!/bin/sh
# Raven one-line installer (macOS / Linux).
#
#   Remote (website):   curl -fsSL https://raven.evermind.ai/install.sh | sh
#   Local (dev clone):  git clone ... && cd raven && ./install.sh
#
# A piped run always installs the published release wheel, even from inside a
# clone. Set RAVEN_LOCAL_SRC=<dir> to force an editable install of a checkout.
# Set RAVEN_MINIMAL=1 to skip the resource download, chromium and the LibreOffice offer;
# the wheel install itself is unchanged. Set RAVEN_NO_LAUNCH=1 to skip the
# closing `raven web` (CI, Dockerfiles), so the script returns. A download whose
# host cannot be reached at all is fetched from a mirror in mainland China
# instead; set RAVEN_NO_MIRROR=1 to keep every download on its default host.
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

# Where a download goes when its default host cannot be reached at all. From a
# mainland-China line with no proxy, GitHub and huggingface.co time out more
# often than they answer, and astral.sh, PyPI, nodejs.org or the npm registry
# can fail the same way. Only a connection that never completed moves a
# download (reachable() below); an HTTP error is the host's answer and stays
# final. A host the user chose is never replaced, and RAVEN_NO_MIRROR=1 keeps
# every download on its default host. raven/utils/mirrors.py names the same
# hosts for the runtime; a test holds the copies equal.
NODE_DIST="https://nodejs.org/dist"
NODE_MIRROR="https://cdn.npmmirror.com/binaries/node"
PYPI_INDEX="https://pypi.org/simple/"
PYPI_MIRROR="https://pypi.tuna.tsinghua.edu.cn/simple"
PYTHON_DOWNLOADS="https://releases.astral.sh/"
PYTHON_MIRROR="https://registry.npmmirror.com/-/binary/python-build-standalone"
NPM_REGISTRY="https://registry.npmjs.org/"
NPM_MIRROR="https://registry.npmmirror.com"
PLAYWRIGHT_MIRROR="https://cdn.npmmirror.com/binaries/playwright"
# uv's installer, and where it downloads uv from besides releases.astral.sh
# (PYTHON_DOWNLOADS): GitHub's release files. Asked only to tell why the
# installer left no uv (uv_unreachable).
UV_INSTALLER="https://astral.sh/uv/install.sh"
UV_GITHUB_RELEASE="https://github.com/astral-sh/uv/releases/latest/download/uv-installer.sh"
# Where playwright downloads Chromium from, in its own order (playwright_answers).
PLAYWRIGHT_HOSTS="https://cdn.playwright.dev/dbazure/download/playwright https://playwright.download.prss.microsoft.com/dbazure/download/playwright"
# The variables a fallback exported, unset again before the page starts: they
# are for this install's own downloads, and the runtime decides for itself.
MIRROR_EXPORTS=""
# Whether the npm registry answers at all: "" until it was asked, then 1 or 0.
NPM_ANSWERS=""
# The host Node comes from, once chosen (choose_node_dist).
NODE_FROM=""

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

# Read the way raven's own opt-outs read it, so this script and the
# `raven resources` it starts agree on what a value means.
mirrors_allowed() {
  case "$(printf '%s' "${RAVEN_NO_MIRROR:-}" | tr -d '[:space:]' | tr '[:upper:]' '[:lower:]')" in
    1|true|yes|on) return 1 ;;
  esac
  return 0
}

# Whether curl's exit code $1 says no connection completed: the name did not
# resolve, no route led to the host, the connection -- or a proxy's -- was
# refused, reset or timed out, or TLS never got going. $2 is what a proxy
# answered the request for a tunnel with (curl's %{http_connect}, 000 for none):
# a 5xx says the proxy could not reach the host either, while any other refusal
# is final, as the host's own HTTP error is -- the mirror would be asked through
# the same proxy, and would only route around its rules. The rule
# raven/utils/mirrors.py states in full; a test runs every copy of it against
# the same hosts and proxies.
connection_failed() {
  case "${2:-000}" in
    000|2??) ;;
    5??) return 0 ;;
    *) return 1 ;;
  esac
  case "$1" in
    6|7|28|35|52|56|97) return 0 ;;
  esac
  return 1
}

# False only when no connection to $1, or to where it redirects, completed.
# Any HTTP answer, an error status included, is the host being there, and so is
# a certificate that fails verification: whatever re-signs this host's traffic
# re-signs a mirror's too.
reachable() {
  code=0
  tunnel="$(curl -sS -o /dev/null --connect-timeout 5 --max-time 10 -IL -w '%{http_connect}' "$1" 2>/dev/null)" || code=$?
  ! connection_failed "$code" "$tunnel"
}

# use_mirror <variable> <value>: export it for the rest of this install, and
# remember it so the page does not start with it.
use_mirror() {
  eval "export $1=\"\$2\""
  MIRROR_EXPORTS="$MIRROR_EXPORTS $1"
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
  # Fetched, then run, rather than piped: curl's exit code tells a host that
  # answered with an error from one that could not be reached.
  uv_fetch=0
  uv_installer="$(mktemp "${TMPDIR:-/tmp}/raven-uv-installer.XXXXXX")" || die "Could not create a file for uv's installer."
  uv_tunnel="$(curl -fsSL --connect-timeout 15 -w '%{http_connect}' "$UV_INSTALLER" -o "$uv_installer")" || uv_fetch=$?
  if [ "$uv_fetch" -eq 0 ]; then sh "$uv_installer" || true; fi
  rm -f "$uv_installer"
  # uv installs to ~/.local/bin (or $XDG_BIN_HOME) -- make it visible for the
  # rest of this script even before the shell profile is re-sourced.
  export PATH="$HOME/.local/bin:$PATH"
  if ! have uv && mirrors_allowed && ! uv_host_chosen && uv_unreachable "$uv_fetch" "$uv_tunnel"; then install_uv_from_mirror || true; fi
  have uv || die "uv could not be installed, or is not where it was put; check PATH (expected in ~/.local/bin)"
  ok "uv installed"
}

# Whether the user chose where uv's installer downloads uv from. It reads these
# four, in this order, before its own two hosts, and the host chosen keeps its
# say: its answer, or its silence, is final, and the mirror never replaces it.
uv_host_chosen() {
  [ -n "${UV_DOWNLOAD_URL:-}${INSTALLER_DOWNLOAD_URL:-}${UV_INSTALLER_GHE_BASE_URL:-}${UV_INSTALLER_GITHUB_BASE_URL:-}" ]
}

# Whether uv's installer failed for want of a connection, the one failure a
# mirror stands in for: astral.sh could not be reached for the installer ($1 is
# curl's exit code fetching it, $2 what a proxy answered the tunnel with), or
# the installer ran and neither host it downloads uv from could be reached. Any
# answer -- an HTTP error, a certificate that fails verification, a failure
# inside the installer -- is final, and installing uv from another host would
# hide it.
uv_unreachable() {
  if [ "$1" -ne 0 ]; then
    connection_failed "$1" "${2:-000}"
    return
  fi
  ! reachable "$PYTHON_DOWNLOADS" && ! reachable "$UV_GITHUB_RELEASE"
}

# The absolute URL of the link $2 on the page at $1, which ends in /.
resolve_href() {
  case "$2" in
    http://*|https://*) printf '%s' "$2"; return 0 ;;
    /*) printf '%s%s' "$(printf '%s' "$1" | sed 's|^\([a-z]*://[^/]*\).*|\1|')" "$2"; return 0 ;;
  esac
  href_base="${1%/}"
  href_rest="$2"
  while :; do
    case "$href_rest" in
      ../*) href_base="${href_base%/*}"; href_rest="${href_rest#../}" ;;
      ./*) href_rest="${href_rest#./}" ;;
      *) break ;;
    esac
  done
  printf '%s/%s' "$href_base" "$href_rest"
}

# uv publishes the binary its installer fetches to PyPI too, as a wheel, and a
# wheel is a zip. When uv's own installer could not reach its hosts
# (uv_unreachable), uv and uvx are unpacked from the newest wheel for this
# platform on the indexes the user named for uv, asked in uv's order
# (uv_named_indexes), or on the PyPI mirror when they named none, and checked
# against the sha256 the index publishes beside the link. An index named in a
# uv.toml this cannot read keeps the mirror away all the same.
install_uv_from_mirror() {
  case "$NODE_OS-$NODE_ARCH" in
    linux-x64)
      uv_tag="manylinux_2_17_x86_64"
      if ldd --version 2>&1 | grep -qi musl; then uv_tag="musllinux_1_1_x86_64"; fi
      ;;
    linux-arm64) uv_tag="manylinux_2_17_aarch64" ;;
    darwin-x64) uv_tag="macosx_10_12_x86_64" ;;
    darwin-arm64) uv_tag="macosx_11_0_arm64" ;;
    *) return 1 ;;
  esac
  uv_indexes="$(uv_named_indexes)"
  if [ -z "$uv_indexes" ]; then
    if uv_index_chosen; then
      warn "uv's installer could not reach its hosts, and the index a uv.toml names could not be read here, so uv does not come from the mirror; set UV_DEFAULT_INDEX to that index to fetch uv from it."
      return 1
    fi
    uv_indexes="$PYPI_MIRROR"
  fi
  uv_work="$(mktemp -d)" || return 1
  uv_href=""
  set -f
  for uv_index in $uv_indexes; do
    uv_page="${uv_index%/}/uv/"
    warn "uv's installer could not reach its hosts; fetching uv from $uv_page instead (RAVEN_NO_MIRROR=1 turns this off)."
    # The newest release, compared field by field: an index lists files in
    # name order, where 0.9.9 sorts after 0.13.4.
    uv_href="$(
      curl -fsSL --connect-timeout 15 "$uv_page" 2>/dev/null | grep -o 'href="[^"]*"' | sed 's/^href="//; s/"$//' \
        | grep "uv-[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*-py3-none-[^/#]*$uv_tag[^/#]*\.whl" \
        | sed 's|^\(.*uv-\([0-9][0-9.]*\)-py3-none-.*\)$|\2 \1|' \
        | sort -t. -k1,1n -k2,2n -k3,3n | tail -n 1 | cut -d' ' -f2-
    )" || uv_href=""
    [ -z "$uv_href" ] || break
  done
  set +f
  case "$uv_href" in
    *'#sha256='*) uv_sha="${uv_href##*#sha256=}" ;;
    *) uv_sha="" ;;
  esac
  uv_href="${uv_href%%#*}"
  if [ -z "$uv_href" ] \
    || ! curl -fsSL --connect-timeout 15 -o "$uv_work/uv.whl" "$(resolve_href "$uv_page" "$uv_href")" \
    || { [ -n "$uv_sha" ] && [ "$(sha256_of "$uv_work/uv.whl")" != "$uv_sha" ]; }; then
    rm -rf "$uv_work"
    return 1
  fi
  if have unzip; then
    unzip -q -o "$uv_work/uv.whl" -d "$uv_work/x" >/dev/null 2>&1 || { rm -rf "$uv_work"; return 1; }
  elif have python3; then
    python3 -m zipfile -e "$uv_work/uv.whl" "$uv_work/x" || { rm -rf "$uv_work"; return 1; }
  else
    rm -rf "$uv_work"
    return 1
  fi
  mkdir -p "$HOME/.local/bin"
  for exe in uv uvx; do
    found="$(find "$uv_work/x" -path "*.data/scripts/$exe" -type f | head -n 1)"
    if [ -n "$found" ]; then cp "$found" "$HOME/.local/bin/$exe" && chmod 755 "$HOME/.local/bin/$exe"; fi
  done
  rm -rf "$uv_work"
  [ -x "$HOME/.local/bin/uv" ]
}

# --- 2. ensure Node >= 22 --------------------------------------------------
# Returns 0 if a usable system node is found.
system_node_ok() {
  have node || return 1
  v="$(node --version 2>/dev/null | sed 's/^v//; s/\..*//')"
  [ -n "$v" ] && [ "$v" -ge "$MIN_NODE_MAJOR" ] 2>/dev/null
}

# Where Node comes from: nodejs.org, or its mirror when nodejs.org cannot be
# reached at all. Sets NODE_FROM, once, so a host that does not answer is waited
# on once -- unless the caller is a subshell, which keeps its choice to itself.
choose_node_dist() {
  [ -z "$NODE_FROM" ] || return 0
  NODE_FROM="$NODE_DIST"
  if mirrors_allowed && ! reachable "$NODE_DIST/index.json"; then
    warn "nodejs.org could not be reached; downloading Node from $NODE_MIRROR instead (RAVEN_NO_MIRROR=1 turns this off)."
    NODE_FROM="$NODE_MIRROR"
  fi
}

# Resolve the latest v22 LTS version string (e.g. v22.20.0) from the index on
# host $1, without requiring jq/python. Falls back to a pinned version if the
# index can't be reached.
latest_node_v22() {
  idx="$(curl -fsSL "$1/index.json" 2>/dev/null || true)"
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
  choose_node_dist
  ver="$(latest_node_v22 "$NODE_FROM")"
  pkg="node-${ver}-${NODE_OS}-${NODE_ARCH}"
  url="$NODE_FROM/${ver}/${pkg}.tar.gz"
  mkdir -p "$NODE_RUNTIME_DIR"
  tmp="$(mktemp -d)"
  info "  $url"
  curl -fsSL "$url" -o "$tmp/node.tar.gz" || die "Node download failed: $url"

  # Verify the tarball against the SHASUMS256.txt published beside it before
  # extracting/executing it: nodejs.org's, or the mirror's copy when the tarball
  # came from the mirror, so the check catches a corrupt or truncated download.
  # A tarball that cannot be verified is treated like one that fails
  # verification: it is never extracted.
  node_hint="Install Node.js >= ${MIN_NODE_MAJOR} with npm yourself and re-run; the installer then uses it instead of downloading one."
  curl -fsSL "$NODE_FROM/${ver}/SHASUMS256.txt" -o "$tmp/SHASUMS256.txt" 2>/dev/null \
    || { rm -rf "$tmp"; die "Could not fetch SHASUMS256.txt for Node ${ver}, so the download cannot be verified. $node_hint"; }
  expected="$(awk -v f="${pkg}.tar.gz" '$2==f {print $1}' "$tmp/SHASUMS256.txt")"
  [ -n "$expected" ] \
    || { rm -rf "$tmp"; die "SHASUMS256.txt for Node ${ver} does not list ${pkg}.tar.gz, so the download cannot be verified. $node_hint"; }
  actual="$(sha256_of "$tmp/node.tar.gz")"
  [ -n "$actual" ] \
    || { rm -rf "$tmp"; die "Neither sha256sum nor shasum is available to verify the Node download. $node_hint"; }
  if [ "$actual" != "$expected" ]; then
    rm -rf "$tmp"
    die "Node checksum mismatch (expected $expected, got $actual)"
  fi
  ok "Node tarball SHA256 verified"

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

# Whether the npm registry answers at all. One probe for the install: both
# builds read the same registry, and a probe is a request to a host that may
# not answer.
npm_registry_answers() {
  if [ -z "$NPM_ANSWERS" ]; then
    if reachable "$NPM_REGISTRY"; then NPM_ANSWERS=1; else NPM_ANSWERS=0; fi
  fi
  return 0
}

# `npm ci` reads every package from the registry. One that cannot be reached at
# all is swapped for its mirror, unless the user named a registry: in the
# environment, or in npm's own configuration, which npm reads out of the
# directory it runs in -- a project names its own registry in a .npmrc beside
# it. The question is therefore asked in the directory the install runs in,
# and each build's answer is its own: the two builds may name different
# registries, and neither may reach the other.
#
# $1 the node bin directory, $2 the directory the install runs in. The
# registry to install from is printed; nothing is printed when the one npm
# reads stands.
use_npm_mirror() {
  mirrors_allowed || return 0
  [ -z "${npm_config_registry:-}${NPM_CONFIG_REGISTRY:-}" ] || return 0
  # An npm that cannot say which registry it reads may be reading one the user
  # chose, so only its own word for the default lets the mirror in.
  configured="$(cd "$2" && PATH="$1:$PATH" npm config get registry 2>/dev/null)" || return 0
  case "$configured" in
    "$NPM_REGISTRY"|"${NPM_REGISTRY%/}") ;;
    *) return 0 ;;
  esac
  npm_registry_answers
  if [ "$NPM_ANSWERS" = 1 ]; then return 0; fi
  warn "registry.npmjs.org could not be reached; installing packages from $NPM_MIRROR instead (RAVEN_NO_MIRROR=1 turns this off)."
  printf '%s' "$NPM_MIRROR"
}

# Run a command in a build directory with the registry that build installs
# from, and only for that command: the two builds may name different
# registries, and neither may reach the other.
#
# $1 the registry (empty when there is none to swap), $2 the node bin
# directory, $3 the build directory, the rest the command.
npm_run() {
  npm_registry_arg="$1"
  npm_node_bin="$2"
  npm_build_dir="$3"
  shift 3
  if [ -n "$npm_registry_arg" ]; then
    ( cd "$npm_build_dir" && PATH="$npm_node_bin:$PATH" npm_config_registry="$npm_registry_arg" "$@" )
  else
    ( cd "$npm_build_dir" && PATH="$npm_node_bin:$PATH" "$@" )
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
  if [ -z "$blocker" ]; then npm_registry_answers; fi

  if [ "$need_tui" = 1 ]; then
    if [ -n "$blocker" ]; then
      warn "$blocker; skipping TUI build; raven tui may not work"
    else
      info "Building the TUI bundle (ui-tui/dist/entry.js)..."
      npm_registry="$(use_npm_mirror "$node_dir" "$src/ui-tui")"
      npm_run "$npm_registry" "$node_dir" "$src/ui-tui" npm ci \
        && npm_run "$npm_registry" "$node_dir" "$src/ui-tui" npm run build
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
  npm_registry="$(use_npm_mirror "$2" "$1/ui-web")"
  npm_run "$npm_registry" "$2" "$1/ui-web" npm ci \
    && npm_run "$npm_registry" "$2" "$1/ui-web" npm run build \
    && uv run --no-project python "$1/ui-web/build.py"
}

# --- 3. install raven ------------------------------------------------------
# True when $1 holds a raven source checkout (its own pyproject, not a dep's).
is_raven_source() {
  [ -f "$1/pyproject.toml" ] && grep -q '^name = "raven"' "$1/pyproject.toml" 2>/dev/null
}

# The uv.toml files uv reads, one per line: the one UV_CONFIG_FILE names, or
# the user's, then the machine's -- each of XDG_CONFIG_DIRS, then /etc.
uv_config_files() {
  if [ -n "${UV_CONFIG_FILE:-}" ]; then
    printf '%s\n' "$UV_CONFIG_FILE"
    return
  fi
  uv_ifs="$IFS"
  IFS=:
  set -f
  set -- ${XDG_CONFIG_HOME:-$HOME/.config} ${XDG_CONFIG_DIRS:-/etc/xdg} /etc
  set +f
  IFS="$uv_ifs"
  for uv_root in "$@"; do
    printf '%s/uv/uv.toml\n' "$uv_root"
  done
}

# A uv.toml as uv reads it: a byte-order mark a Windows editor may have
# written is not part of the first key, and uv reads past it. Read as bytes,
# so the mark is three bytes and not one character whatever the locale says.
uv_config_text() {
  LC_ALL=C awk '
    NR == 1 && substr($0, 1, 3) == "\357\273\277" { $0 = substr($0, 4) }
    { print }
  ' "$1"
}

# Whether a uv.toml uv reads -- the user's, or the machine's -- has a line
# matching the extended regular expression $1. A key may be quoted, as TOML
# allows: "index-url" = ... is the same key as index-url = ...
uv_config_has() {
  uv_config_files | {
    while IFS= read -r uv_file; do
      [ -f "$uv_file" ] && uv_config_text "$uv_file" | grep -Eq "$1" && exit 0
    done
    exit 1
  }
}

# The address of the default index a uv.toml uv reads names: its index-url,
# or the url of the index marked default = true, written as a table or inline.
# The first file that names one decides, as it does for uv. A key may be
# quoted, as TOML allows.
uv_config_index() {
  uv_config_files | while IFS= read -r uv_file; do
    [ -f "$uv_file" ] || continue
    uv_found="$(LC_ALL=C awk '
      function value(text, q) {
        sub(/^[^=]*=[ \t]*/, "", text)
        q = substr(text, 1, 1)
        if (q != "\"" && q != sq) return ""
        text = substr(text, 2)
        return index(text, q) ? substr(text, 1, index(text, q) - 1) : ""
      }
      BEGIN { sq = sprintf("%c", 39) }
      NR == 1 && substr($0, 1, 3) == "\357\273\277" { $0 = substr($0, 4) }
      /^[ \t]*\[/ { table = ($0 ~ /^[ \t]*\[\[[ \t]*"?index"?[ \t]*\]\]/); url = ""; dflt = 0; next }
      /^[ \t]*"?index-url"?[ \t]*=/ { u = value($0); if (u != "") { print u; exit } }
      /[{,][ \t]*"?default"?[ \t]*=[ \t]*true/ && match($0, /[{,][ \t]*"?url"?[ \t]*=/) {
        u = value(substr($0, RSTART + 1)); if (u != "") { print u; exit }
      }
      table && /^[ \t]*"?url"?[ \t]*=/ { url = value($0) }
      table && /^[ \t]*"?default"?[ \t]*=[ \t]*true/ { dflt = 1 }
      table && dflt && url != "" { print url; exit }
    ' "$uv_file")"
    if [ -n "$uv_found" ]; then
      printf '%s\n' "$uv_found"
      break
    fi
  done
}

uv_index_chosen() {
  [ -n "${UV_DEFAULT_INDEX:-}${UV_INDEX_URL:-}${UV_INDEX:-}" ] && return 0
  uv_config_has '^[[:space:]]*"?index-url"?[[:space:]]*=' && return 0
  # A default index is marked default = true, in an [[index]] table or inline.
  uv_config_has '(^|[{,[:space:]]*)"?default"?[[:space:]]*=[[:space:]]*true'
}

# The indexes the user named for uv, one per line, in the order uv asks them:
# each of UV_INDEX's, then the default -- UV_DEFAULT_INDEX or UV_INDEX_URL, or
# else the one a uv.toml names.
uv_named_indexes() {
  set -f
  for uv_entry in ${UV_INDEX:-}; do
    printf '%s\n' "$uv_entry" | sed 's/^[A-Za-z0-9._-]*=//'
  done
  set +f
  if [ -n "${UV_DEFAULT_INDEX:-}" ]; then
    printf '%s\n' "$UV_DEFAULT_INDEX"
  elif [ -n "${UV_INDEX_URL:-}" ]; then
    printf '%s\n' "$UV_INDEX_URL"
  else
    uv_config_index
  fi
}

# uv resolves raven's dependencies from PyPI, and fetches a Python when the
# machine has none it can use. Either host that cannot be reached at all is
# swapped for its mirror -- unless the user named one -- for this install's uv.
use_package_mirrors() {
  mirrors_allowed || return 0
  if ! uv_index_chosen && ! reachable "$PYPI_INDEX"; then
    warn "pypi.org could not be reached; installing packages from $PYPI_MIRROR instead (RAVEN_NO_MIRROR=1 turns this off)."
    use_mirror UV_DEFAULT_INDEX "$PYPI_MIRROR"
  fi
  # A Python source the user named counts in each form uv reads: a mirror it is
  # given outranks a copy of releases.astral.sh, and fails outright beside a
  # list of downloads that live elsewhere.
  if [ -z "${UV_PYTHON_INSTALL_MIRROR:-}${UV_PYTHON_DOWNLOADS_JSON_URL:-}${UV_ASTRAL_MIRROR_URL:-}" ] \
    && ! uv_config_has '^[[:space:]]*"?python-(install-mirror|downloads-json-url)"?[[:space:]]*=' \
    && ! reachable "$PYTHON_DOWNLOADS"; then
    warn "releases.astral.sh could not be reached; fetching any Python uv needs from $PYTHON_MIRROR instead (RAVEN_NO_MIRROR=1 turns this off)."
    use_mirror UV_PYTHON_INSTALL_MIRROR "$PYTHON_MIRROR"
  fi
}

install_raven() {
  use_package_mirrors
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
    # The default config names the everos memory backend, and a user can point
    # memory at the hosted EverOS instead; each ships as its own distribution
    # beside the wheel -- carry both, and degrade loudly (the host boots
    # memoryless and `raven doctor` says why) if they cannot build.
    memory_dir="$script_dir/plugins-dist/everos-memory"
    cloud_dir="$script_dir/plugins-dist/everos-cloud-memory"
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
    # they fall before the memory plugins rather than taking them down too.
    if ! tool_install "$script_dir[channels]" "$memory_dir" "$cloud_dir" "$design_dir" "$ppt_dir"; then
      warn "Channel dependencies failed to install; retrying with base raven. Some channels stay unavailable (see: raven channels list)."
      if ! tool_install "$script_dir" "$memory_dir" "$cloud_dir" "$design_dir" "$ppt_dir"; then
        warn "A product engine failed to build; Raven-Design and Raven-PPT stay disabled (raven doctor explains)."
        if ! tool_install "$script_dir" "$memory_dir" "$cloud_dir"; then
          warn "The EverOS memory plugins failed to install; long-term memory stays off (raven doctor explains)."
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
      grep -E '^(everos-memory|everos-cloud-memory) ' "$plugins" > "$memory_only" || true
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

# --- 4. optional capabilities: resources + browser + LibreOffice -------------
# Every install here is best-effort: raven itself is already installed by the
# time they run, so a failed download or a declined offer must never abort a
# completed install. RAVEN_MINIMAL skips them all.

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
  if "$py" -m playwright install chromium; then return 0; fi
  # Playwright tries its own hosts in turn and reports only that none of them
  # delivered, so the mirror gets one retry after the whole download failed --
  # unless the user named a download host, or one of those hosts answered.
  if mirrors_allowed && [ -z "${PLAYWRIGHT_DOWNLOAD_HOST:-}${PLAYWRIGHT_CHROMIUM_DOWNLOAD_HOST:-}" ] \
    && ! playwright_answers; then
    warn "Chromium could not be downloaded; trying $PLAYWRIGHT_MIRROR instead (RAVEN_NO_MIRROR=1 turns this off)."
    if PLAYWRIGHT_DOWNLOAD_HOST="$PLAYWRIGHT_MIRROR" "$py" -m playwright install chromium; then return 0; fi
  fi
  warn "Chromium download failed; the browser tool stays off. Retry later with: $py -m playwright install chromium"
}

# Whether a host playwright downloads Chromium from answers. One that answered
# had its say -- an HTTP error, a certificate that fails verification -- and
# the mirror stands in only for hosts that cannot be reached.
playwright_answers() {
  for playwright_host in $PLAYWRIGHT_HOSTS; do
    reachable "$playwright_host" && return 0
  done
  return 1
}

# The word dictionary and the deepdoc vision models: ~110 MB that cannot live
# in git (AGENTS.md section 7 caps a file at 1 MiB) and so arrive here. The
# Makefile and the Dockerfile fetch them for the two installs that have a
# checkout or a build context; this is the third, and without it a one-line
# install segments Chinese per character and cannot parse a PDF at all.
#
# Through the tool venv's python, like install_browser above: the fetcher is
# part of the installed package now, so this one call works whether the install
# was a wheel or an editable checkout. The system python knows neither.
#
# `--optional` is the whole contract: every step reports its own failure and
# exits 0, so an unreachable host or a proxy leaves a working raven that says
# what is absent when asked rather than an install that stopped halfway.
#
# Both halves do run here. The deepdoc fetch needs huggingface-hub, which is
# not a declared dependency but arrives under litellm, which is -- so it is
# there on every install. If litellm ever drops it the fetch does not break the
# install, it reports itself missing and the models stay unfetched.
install_resources() {
  py="$(uv tool dir 2>/dev/null || true)/raven/bin/python"
  if [ ! -x "$py" ]; then
    warn "raven tool venv python not found; skipping the resource download."
    return 0
  fi
  info "Downloading the word dictionary and parser models..."
  "$py" -m raven.resources --optional \
    || warn "Resource download failed; raven still runs. Retry later with: raven resources"
}

# macOS without Homebrew: the release dmg, pinned the way the cask pins it --
# one version, one digest per build. The stable directory drops a release once
# the next one ships and the archive keeps it byte for byte, so the archive is
# the fallback rather than the only source. Verified before it is mounted.
LO_VERSION="26.8.0"
LO_BUILD="26.8.0.3"
LO_SHA256_ARM64="8858d8058da4f862f47559486814e65efc27294da67c5e4bb56b006b1ee59f89"
LO_SHA256_X64="2dcbce4894e01bc1ecd594658e2cbda70ff7bfcd0b310f35d38887797172d09e"
# TUNA's copy of libreoffice.org's download tree. The digest is pinned, so a
# third source cannot change what is installed.
LO_MIRROR="https://mirrors.tuna.tsinghua.edu.cn/libreoffice/libreoffice"

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
  lo_path="stable/$LO_VERSION/mac/$lo_dir/LibreOffice_${LO_VERSION}_MacOS_$lo_arch.dmg"
  set -- \
    "https://download.documentfoundation.org/libreoffice/$lo_path" \
    "https://downloadarchive.documentfoundation.org/libreoffice/old/$LO_BUILD/mac/$lo_dir/LibreOffice_${LO_BUILD}_MacOS_$lo_arch.dmg"
  if mirrors_allowed; then set -- "$@" "$LO_MIRROR/$lo_path"; fi
  for url in "$@"; do
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
# The same file through jsDelivr, for when GitHub does not deliver. Pinned by
# size and digest below, so a second source cannot change what is installed.
HAN_FONT_MIRROR_URL="https://cdn.jsdelivr.net/gh/notofonts/noto-cjk@Sans2.004/Sans/SubsetOTF/SC/NotoSansSC-Regular.otf"
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
  set -- "$HAN_FONT_URL"
  if mirrors_allowed; then set -- "$@" "$HAN_FONT_MIRROR_URL"; fi
  for url in "$@"; do
    # Size and digest both, before the file is put in place: a truncated OTF
    # still parses and draws nothing, which is the failure this step exists for.
    if curl -fsSL --max-time 120 -o "$part" "$url" \
      && [ "$(wc -c < "$part" | tr -d ' ')" = "$HAN_FONT_BYTES" ] \
      && [ "$(sha256_of "$part")" = "$HAN_FONT_SHA256" ] \
      && mv -f "$part" "$dir/$HAN_FONT_NAME"; then
      have fc-cache && fc-cache -f "$dir" >/dev/null 2>&1
      ok "Chinese font installed to $dir/$HAN_FONT_NAME"
      return 0
    fi
  done
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

  [ -n "${RAVEN_MINIMAL:-}" ] || install_resources
  [ -n "${RAVEN_MINIMAL:-}" ] || install_browser
  [ -n "${RAVEN_MINIMAL:-}" ] || install_office
  [ -n "${RAVEN_MINIMAL:-}" ] || install_cjk_fonts

  # Before the launch, not after: the page holds this terminal until Ctrl-C,
  # and `uv tool update-shell` only reaches future shells.
  case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) warn "Your current PATH does not include ~/.local/bin yet -- open a new terminal, or run: export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
  esac

  # A mirror served this install's downloads only: the page starts with the
  # user's own environment, and the runtime falls back for itself.
  for name in $MIRROR_EXPORTS; do unset "$name"; done

  [ -n "${RAVEN_NO_LAUNCH:-}" ] || launch_web
}

main "$@"
