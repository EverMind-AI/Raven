"""Search tools: grep (content search) and find (file lookup).

Both run host-side and reuse ``_FsTool``'s workspace/allowed_dir resolution so
they share the exact same path boundary as read_file/write_file/list_dir — never
the SandboxExecutor (avoids shuttling large result sets across a VM edge).

``grep`` prefers the ``rg`` (ripgrep) binary when present on PATH for speed and
.gitignore awareness, and falls back to a pure-Python scan otherwise so raven
keeps working with zero hard binary dependency.
"""

import asyncio
import fnmatch
import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

from raven.agent.tools.filesystem import _FsTool

# Noise directories skipped by the pure-Python fallback / find. ripgrep handles
# its own ignore logic via .gitignore, so this only gates the fallback path.
_IGNORE_DIRS = {
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    "dist",
    "build",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".coverage",
    "htmlcov",
}

# Pseudo / system filesystem roots that must never be tree-walked. A model that
# runs `grep <pat> /` (or find over /) would otherwise traverse the entire host
# — including slow network mounts under /proc, /sys, or /mnt — and hang the whole
# run indefinitely (observed: a 47-min wedge in disk-sleep on a shared mount).
# Searches must name a real subtree, not a system root.
_DENY_TRAVERSAL_ROOTS = {Path(p) for p in ("/", "/proc", "/sys", "/dev", "/run", "/boot")}
# Wall-clock cap on the pure-Python os.walk fallback so an allowed-but-huge tree
# still cannot hang the loop. ripgrep already has its own _RG_TIMEOUT.
_WALK_DEADLINE_S = 20.0


def _denied_traversal_root(base: Path) -> bool:
    """True if ``base`` resolves to a system root that must not be tree-walked."""
    try:
        resolved = base.resolve()
    except OSError:
        return False
    # Any filesystem/drive root is its own parent — catches POSIX "/" and the
    # Windows drive/UNC roots ("C:\\", "\\\\server\\share") that the POSIX-only
    # _DENY_TRAVERSAL_ROOTS set misses (a search at C:\ would otherwise walk the
    # whole drive).
    if resolved.parent == resolved:
        return True
    return resolved in _DENY_TRAVERSAL_ROOTS


# ---------------------------------------------------------------------------
# grep
# ---------------------------------------------------------------------------


class GrepTool(_FsTool):
    """Search file contents by regex, ripgrep-backed with a pure-Python fallback."""

    _MAX_CHARS = 30_000
    _DEFAULT_LIMIT = 100
    _RG_TIMEOUT = 30

    @property
    def name(self) -> str:
        return "grep"

    @property
    def description(self) -> str:
        return (
            "Search file contents by regular expression. Prefer this over running "
            "grep/rg through exec — results are paginated, capped, and .gitignore-aware. "
            "output_mode 'content' returns matching lines with path:line numbers, "
            "'files_with_matches' lists only file paths, 'count' shows match counts per file. "
            "Use glob to restrict to file types (e.g. '*.py')."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression to search for"},
                "path": {
                    "type": "string",
                    "description": "File or directory to search in (default: workspace root)",
                },
                "glob": {
                    "type": "string",
                    "description": "Only search files matching this glob (e.g. '*.py', '*.{ts,tsx}')",
                },
                "output_mode": {
                    "type": "string",
                    "enum": ["content", "files_with_matches", "count"],
                    "description": "Output format (default: content)",
                },
                "case_insensitive": {
                    "type": "boolean",
                    "description": "Case-insensitive matching (default false)",
                },
                "context": {
                    "type": "integer",
                    "description": "Lines of context before and after each match, content mode only (default 0)",
                    "minimum": 0,
                    "maximum": 20,
                },
                "limit": {
                    "type": "integer",
                    "description": "Max matching lines (content) or files (other modes) to return (default 100)",
                    "minimum": 1,
                },
            },
            "required": ["pattern"],
        }

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        glob: str | None = None,
        output_mode: str = "content",
        case_insensitive: bool = False,
        context: int = 0,
        limit: int | None = None,
        **kwargs: Any,
    ) -> str:
        cap = limit or self._DEFAULT_LIMIT
        try:
            re.compile(pattern)
        except re.error as e:
            return f"Error: invalid regular expression: {e}"
        try:
            base = self._resolve(path)
        except PermissionError as e:
            return f"Error: {e}"
        if not base.exists():
            return f"Error: path not found: {path}"
        if base.is_dir() and _denied_traversal_root(base):
            return (
                f"Error: refusing to search '{path}' — it resolves to a system root "
                f"({base.resolve()}). Searching the whole filesystem hangs the agent. "
                "Specify a narrower directory (e.g. the workspace or a project subtree)."
            )

        rg = shutil.which("rg")
        try:
            if rg:
                return await self._run_rg(rg, pattern, base, glob, output_mode, case_insensitive, context, cap)
            return self._run_python(pattern, base, glob, output_mode, case_insensitive, context, cap)
        except Exception as e:
            return f"Error running grep: {e}"

    # ── ripgrep backend ─────────────────────────────────────────────────

    async def _run_rg(
        self,
        rg: str,
        pattern: str,
        base: Path,
        glob: str | None,
        output_mode: str,
        case_insensitive: bool,
        context: int,
        cap: int,
    ) -> str:
        args = [rg, "--color=never"]
        if case_insensitive:
            args.append("-i")
        if glob:
            args += ["-g", glob]
        # rg only skips noise dirs when a .gitignore says so; add explicit excludes
        # so it matches the pure-Python fallback regardless of repo state. These come
        # after any user glob so the excludes win on last-match-wins ordering.
        for d in _IGNORE_DIRS:
            args += ["-g", f"!{d}"]

        if output_mode == "files_with_matches":
            args.append("-l")
        elif output_mode == "count":
            args.append("-c")
        else:
            args += ["--line-number", "--no-heading", "--with-filename"]
            if context:
                args += ["-C", str(context)]
        # Force forward-slash separators in rg's output paths on every platform
        # so results read the same on Windows as POSIX (only affects path fields,
        # not matched content).
        args += ["--path-separator", "/"]
        args += ["-e", pattern, "--", str(base)]

        proc = await asyncio.create_subprocess_exec(
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=self._RG_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            return f"Error: grep timed out after {self._RG_TIMEOUT}s"

        # rg exits 1 when there are no matches — that is a normal empty result.
        if proc.returncode not in (0, 1):
            return f"Error running rg: {err.decode('utf-8', 'replace').strip()}"

        text = out.decode("utf-8", "replace")
        # Make paths relative to the search root for compact, readable output.
        # rg emits forward-slash paths (--path-separator above); match that when
        # stripping the search-root prefix so the strip works on Windows too.
        base_str = str(base).replace(os.sep, "/")
        text = text.replace(base_str + "/", "").replace(base_str, base.name or ".")
        lines = [ln for ln in text.splitlines() if ln]
        if not lines:
            return "No matches found."

        unit = "matching lines" if output_mode == "content" else "files"
        return self._format_lines(lines, cap, unit)

    # ── pure-Python fallback ────────────────────────────────────────────

    def _run_python(
        self,
        pattern: str,
        base: Path,
        glob: str | None,
        output_mode: str,
        case_insensitive: bool,
        context: int,
        cap: int,
    ) -> str:
        flags = re.IGNORECASE if case_insensitive else 0
        rx = re.compile(pattern, flags)
        files = self._iter_files(base, glob)

        content_lines: list[str] = []
        match_files: list[str] = []
        counts: list[tuple[str, int]] = []

        for fp in files:
            rel = self._relpath(fp, base)
            try:
                raw = fp.read_bytes()
            except OSError:
                continue
            if b"\x00" in raw[:8192]:  # skip binary files
                continue
            text_lines = raw.decode("utf-8", "replace").splitlines()

            hits = [i for i, line in enumerate(text_lines) if rx.search(line)]
            if not hits:
                continue

            if output_mode == "files_with_matches":
                match_files.append(rel)
            elif output_mode == "count":
                counts.append((rel, len(hits)))
            else:
                self._collect_content(content_lines, rel, text_lines, hits, context)

        if output_mode == "files_with_matches":
            return self._format_lines(match_files, cap, "files") if match_files else "No matches found."
        if output_mode == "count":
            rendered = [f"{rel}:{n}" for rel, n in counts]
            return self._format_lines(rendered, cap, "files") if rendered else "No matches found."
        return self._format_lines(content_lines, cap, "matching lines") if content_lines else "No matches found."

    @staticmethod
    def _collect_content(
        out: list[str],
        rel: str,
        text_lines: list[str],
        hits: list[int],
        context: int,
    ) -> None:
        emitted: set[int] = set()
        for h in hits:
            lo = max(0, h - context)
            hi = min(len(text_lines), h + context + 1)
            for i in range(lo, hi):
                if i in emitted:
                    continue
                emitted.add(i)
                sep = ":" if i == h or context == 0 else "-"
                out.append(f"{rel}{sep}{i + 1}{sep}{text_lines[i]}")

    def _iter_files(self, base: Path, glob: str | None):
        if base.is_file():
            yield base
            return
        deadline = time.monotonic() + _WALK_DEADLINE_S
        for root, dirs, names in os.walk(base):
            if time.monotonic() > deadline:
                # Stop rather than hang on an unexpectedly huge / slow tree.
                break
            dirs[:] = [d for d in dirs if d not in _IGNORE_DIRS]
            for n in sorted(names):
                if glob and not fnmatch.fnmatch(n, glob):
                    continue
                yield Path(root) / n

    @staticmethod
    def _relpath(fp: Path, base: Path) -> str:
        try:
            return fp.relative_to(base if base.is_dir() else base.parent).as_posix()
        except ValueError:
            return fp.as_posix()

    def _format_lines(self, lines: list[str], cap: int, unit: str) -> str:
        total = len(lines)
        shown = lines[:cap]
        result = "\n".join(shown)
        notes = []
        if total > cap:
            notes.append(
                f"showing first {cap} of {total} {unit} — {total - cap} more not shown; "
                "this is a PARTIAL result, do not treat it as the complete set or count "
                "from it. Use output_mode='count' for exact totals, or a narrower pattern/glob."
            )
        if len(result) > self._MAX_CHARS:
            result = result[: self._MAX_CHARS]
            notes.append(
                f"output truncated to {self._MAX_CHARS} chars — narrow the pattern/glob or "
                "use output_mode='count' to get exact totals instead of eyeballing this view"
            )
        if notes:
            result += f"\n\n(⚠️ {'; '.join(notes)})"
        return result


# ---------------------------------------------------------------------------
# find
# ---------------------------------------------------------------------------


class FindTool(_FsTool):
    """Find files by glob pattern, sorted by recency. Pure-Python (pathlib)."""

    _DEFAULT_LIMIT = 1000

    @property
    def name(self) -> str:
        return "find"

    @property
    def description(self) -> str:
        return (
            "Find files by glob pattern (e.g. '*.py', 'src/**/*.ts'). Prefer this over "
            "running find/ls through exec. Returns paths relative to the search root, "
            "most-recently-modified first. Noise directories (.git, node_modules, etc.) "
            "are skipped."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "pattern": {
                    "type": "string",
                    "description": "Glob pattern, e.g. '*.py' or 'src/**/*.ts'",
                },
                "path": {
                    "type": "string",
                    "description": "Directory to search in (default: workspace root)",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum results to return (default 1000)",
                    "minimum": 1,
                },
            },
            "required": ["pattern"],
        }

    async def execute(
        self,
        pattern: str,
        path: str = ".",
        limit: int | None = None,
        **kwargs: Any,
    ) -> str:
        cap = limit or self._DEFAULT_LIMIT
        try:
            base = self._resolve(path)
        except PermissionError as e:
            return f"Error: {e}"
        if not base.exists():
            return f"Error: path not found: {path}"
        if not base.is_dir():
            return f"Error: not a directory: {path}"
        if _denied_traversal_root(base):
            return (
                f"Error: refusing to search '{path}' — it resolves to a system root "
                f"({base.resolve()}). Specify a narrower directory."
            )

        # A path-bearing pattern globs literally; a bare pattern matches basenames
        # recursively (fd-style), so 'foo.py' finds it at any depth.
        glob_expr = pattern if "/" in pattern else f"**/{pattern}"
        try:
            matches = [
                p for p in base.glob(glob_expr) if not any(part in _IGNORE_DIRS for part in p.relative_to(base).parts)
            ]
        except (ValueError, OSError) as e:
            return f"Error running find: {e}"
        if not matches:
            return "No files found matching pattern."

        matches.sort(key=lambda p: self._mtime(p), reverse=True)
        total = len(matches)
        shown = matches[:cap]
        lines = [
            f"{p.relative_to(base).as_posix()}/" if p.is_dir() else p.relative_to(base).as_posix()
            for p in shown
        ]
        result = "\n".join(lines)
        if total > cap:
            result += f"\n\n(showing first {cap} of {total} results)"
        return result

    @staticmethod
    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
