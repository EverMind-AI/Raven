#!/bin/sh
# Raven one-line installer (macOS / Linux).
#
#   Remote (website):   curl -fsSL https://raven.evermind.ai/install.sh | sh
#   Local (dev clone):  git clone … && cd raven && ./install.sh
#
# Goal: a clean machine ends up able to run `raven` / `raven tui` from any
# directory with no manual steps. The script is idempotent — it detects what
# is already present and only fills the gaps:
#   1. uv            (Python toolchain + package manager)
#   2. Node.js >= 22 (TUI runtime; installed privately if the system lacks it)
#   3. raven         (installed as a global uv tool -> ~/.local/bin/raven)
#
# POSIX sh on purpose (runs under dash/ash, not just bash).
set -eu

# --- config ---------------------------------------------------------------
REPO_URL="${RAVEN_REPO_URL:-git+https://github.com/EverMind-AI/raven.git}"
MIN_NODE_MAJOR=22
RAVEN_HOME="${RAVEN_HOME:-${HOME:?需要 HOME 环境变量，或显式设置 RAVEN_HOME}/.raven}"
NODE_RUNTIME_DIR="$RAVEN_HOME/runtime"

# --- pretty output ---------------------------------------------------------
info()  { printf '\033[1;34m▶\033[0m %s\n' "$1"; }
ok()    { printf '\033[1;32m✓\033[0m %s\n' "$1"; }
warn()  { printf '\033[1;33m!\033[0m %s\n' "$1" >&2; }
die()   { printf '\033[1;31m✗\033[0m %s\n' "$1" >&2; exit 1; }
have()  { command -v "$1" >/dev/null 2>&1; }

# --- 0. platform detection -------------------------------------------------
detect_platform() {
  os="$(uname -s)"
  arch="$(uname -m)"
  case "$os" in
    Darwin) NODE_OS="darwin" ;;
    Linux)  NODE_OS="linux" ;;
    *) die "不支持的操作系统：$os（仅支持 macOS / Linux；Windows 请用 install.ps1）" ;;
  esac
  case "$arch" in
    arm64|aarch64) NODE_ARCH="arm64" ;;
    x86_64|amd64)  NODE_ARCH="x64" ;;
    *) die "不支持的架构：$arch" ;;
  esac
}

# --- 1. ensure uv ----------------------------------------------------------
ensure_uv() {
  if have uv; then
    ok "uv 已安装（$(uv --version)）"
    return
  fi
  info "未找到 uv，正在安装…"
  curl -fsSL https://astral.sh/uv/install.sh | sh
  # uv installs to ~/.local/bin (or $XDG_BIN_HOME) — make it visible for the
  # rest of this script even before the shell profile is re-sourced.
  export PATH="$HOME/.local/bin:$PATH"
  have uv || die "uv 安装后仍不可用，请检查 PATH（期望在 ~/.local/bin）"
  ok "uv 安装完成"
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
    # Actually run it — a half-extracted / corrupt binary is +x but won't run,
    # and must NOT be mistaken for a ready runtime (else we'd never re-download).
    "$n" --version >/dev/null 2>&1 && { printf '%s' "$n"; return 0; }
  done
  return 1
}

ensure_node() {
  if system_node_ok; then
    ok "Node.js 已满足要求（$(node --version)）"
    return
  fi
  # Already provisioned privately by a previous run?
  if pn="$(private_node_bin)"; then
    ok "已存在 Raven 私有 Node（$pn）"
    return
  fi

  info "未找到 Node.js >= $MIN_NODE_MAJOR，正在下载私有运行时（不污染系统）…"
  ver="$(latest_node_v22)"
  pkg="node-${ver}-${NODE_OS}-${NODE_ARCH}"
  url="https://nodejs.org/dist/${ver}/${pkg}.tar.gz"
  mkdir -p "$NODE_RUNTIME_DIR"
  tmp="$(mktemp -d)"
  info "  $url"
  curl -fsSL "$url" -o "$tmp/node.tar.gz" || die "Node 下载失败：$url"

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
        actual=""; warn "未找到 shasum/sha256sum，跳过校验"
      fi
      if [ -n "$actual" ] && [ "$actual" != "$expected" ]; then
        rm -rf "$tmp"
        die "Node 校验失败：SHA256 不匹配（期望 $expected，实际 $actual）"
      fi
      [ -n "$actual" ] && ok "Node tarball SHA256 校验通过"
    else
      warn "SHASUMS256.txt 未列出 ${pkg}.tar.gz，跳过校验"
    fi
  else
    warn "无法获取 SHASUMS256.txt，跳过完整性校验"
  fi

  tar -xzf "$tmp/node.tar.gz" -C "$NODE_RUNTIME_DIR"
  rm -rf "$tmp"
  [ -x "$NODE_RUNTIME_DIR/$pkg/bin/node" ] || die "Node 解压后未找到可执行文件"
  # Run it once now: catches a libc mismatch (e.g. glibc tarball on Alpine/musl)
  # at install time instead of letting `raven tui` fail later on the user's box.
  "$NODE_RUNTIME_DIR/$pkg/bin/node" --version >/dev/null 2>&1 \
    || die "下载的 Node 无法在本机运行（可能 libc 不匹配，如 Alpine/musl）。请改用系统包管理器安装 Node >= ${MIN_NODE_MAJOR}。"
  ok "Node 私有运行时就绪：$NODE_RUNTIME_DIR/$pkg"
  # raven's find_node() globs ~/.raven/runtime/node-*/bin/node automatically,
  # so no PATH change is needed for `raven tui` to find it.
}

# --- 3. install raven ------------------------------------------------------
install_raven() {
  # Local mode: run from a raven source checkout -> editable install of the
  # working tree (what a developer wants). Otherwise install from git.
  script_dir="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
  if [ -f "$script_dir/pyproject.toml" ] && grep -q '^name = "raven"' "$script_dir/pyproject.toml" 2>/dev/null; then
    info "检测到本地 raven 源码，使用 editable 安装：$script_dir"
    # The TUI bundle must exist before first run. In a dev checkout it isn't
    # committed, so build it now if Node is available.
    if [ ! -f "$script_dir/ui-tui/dist/entry.js" ]; then
      node_bin="$(command -v node || true)"
      [ -n "$node_bin" ] || node_bin="$(private_node_bin || true)"
      if [ -n "$node_bin" ] && [ -x "$node_bin" ]; then
        node_dir="$(dirname "$node_bin")"
        # npm ships alongside node, but verify explicitly before relying on it.
        if PATH="$node_dir:$PATH" command -v npm >/dev/null 2>&1; then
          info "构建 TUI 产物（ui-tui/dist/entry.js）…"
          ( cd "$script_dir/ui-tui" && PATH="$node_dir:$PATH" npm ci && PATH="$node_dir:$PATH" npm run build )
        else
          warn "找到 node 但未找到 npm，跳过 TUI 产物构建；raven tui 可能不可用"
        fi
      else
        warn "未找到可用 node，跳过 TUI 产物构建；raven tui 可能不可用"
      fi
    fi
    uv tool install --force -e "$script_dir"
  else
    info "从 GitHub 安装 raven：$REPO_URL"
    uv tool install --force "$REPO_URL"
  fi
  # Ensure ~/.local/bin (uv tool bin dir) is on PATH for future shells.
  uv tool update-shell || true
  ok "raven 安装完成"
}

# --- main ------------------------------------------------------------------
main() {
  have curl || die "需要 curl，请先安装"
  detect_platform
  ensure_uv
  ensure_node
  install_raven

  printf '\n'
  ok "全部就绪！打开一个新终端（或 source 你的 shell 配置），然后运行："
  printf '\n    \033[1mraven\033[0m            # 进入 TUI\n'
  printf '    \033[1mraven agent\033[0m -m "你好"\n\n'
  if ! printf '%s' "$PATH" | grep -q "$HOME/.local/bin"; then
    warn "当前 shell 的 PATH 还没包含 ~/.local/bin —— 重开终端或运行：export PATH=\"\$HOME/.local/bin:\$PATH\""
  fi
}

main "$@"
