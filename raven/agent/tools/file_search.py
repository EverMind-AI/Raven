"""Search tools: grep (content search) and find (file lookup).

Both run host-side and reuse ``_FsTool``'s workspace/allowed_dirs resolution so
they share the exact same path boundary as read_file/write_file/list_dir — never
the SandboxExecutor (avoids shuttling large result sets across a VM edge).

``grep`` prefers the ``rg`` binary installed by ripgrep-bin for speed and
.gitignore awareness, then a system rg on PATH. If neither is available, it
falls back to a pure-Python scan and reports when that scan is incomplete.

Every tree walk here goes through ``tree_walk``: noise directories are pruned
as the walk goes, a wall-clock deadline turns a huge tree into a partial
result that says so, and the walk runs in a worker thread so the event loop
keeps serving every other session meanwhile.
"""

import asyncio
import fnmatch
import os
import re
import shutil
import sysconfig
from pathlib import Path
from typing import Any

from raven.agent.tools import tree_walk
from raven.agent.tools.filesystem import _FsTool

# Pseudo / system filesystem roots that must never be tree-walked. A model that
# runs `grep <pat> /` (or find over /) would otherwise traverse the entire host
# — including slow network mounts under /proc, /sys, or /mnt — and hang the whole
# run indefinitely (observed: a 47-min wedge in disk-sleep on a shared mount).
# Searches must name a real subtree, not a system root.
_DENY_TRAVERSAL_ROOTS = {Path(p) for p in ("/", "/proc", "/sys", "/dev", "/run", "/boot")}


def _resolve_rg() -> str | None:
    """Find the installed binary even when the environment's scripts path is absent."""
    bundled = Path(sysconfig.get_path("scripts")) / ("rg.exe" if os.name == "nt" else "rg")
    if bundled.is_file() and os.access(bundled, os.X_OK):
        return str(bundled)
    return shutil.which("rg")


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

        rg = _resolve_rg()
        try:
            if rg:
                return await self._run_rg(rg, pattern, base, glob, output_mode, case_insensitive, context, cap)
            return await asyncio.to_thread(
                self._run_python, pattern, base, glob, output_mode, case_insensitive, context, cap
            )
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
        for d in sorted(tree_walk.IGNORE_DIRS):
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

        incomplete = False
        try:
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
        except TimeoutError:
            incomplete = True

        if output_mode == "files_with_matches":
            lines, unit = match_files, "files"
        elif output_mode == "count":
            lines, unit = [f"{rel}:{n}" for rel, n in counts], "files"
        else:
            lines, unit = content_lines, "matching lines"
        result = self._format_lines(lines, cap, unit) if lines else ""
        if incomplete:
            warning = (
                f"Warning: search incomplete; the Python fallback exceeded its {tree_walk.WALK_DEADLINE_S:g}s "
                "traversal budget. Results and counts may be partial; absence of a match is not conclusive. "
                "Narrow the search path or glob and try again."
            )
            return f"{warning}\n\n{result}" if result else warning
        return result or "No matches found."

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
        for root, _dirs, names in tree_walk.walk(base):
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

# ``Path.glob`` folds case on Windows only; ``fnmatch`` decides the same way.
_CASE_FLAGS = 0 if os.path.normcase("Aa") == "Aa" else re.IGNORECASE


def _compile_pattern(pattern: str) -> tuple[re.Pattern[str] | None, ...]:
    """One matcher per path component of ``pattern``; ``None`` stands for ``**``.

    Follows what ``Path.glob`` accepted, which is what ``find`` used to call:
    a component is an ``fnmatch`` pattern that cannot cross a slash, and a
    component that is exactly ``**`` spans any number of components including
    none, so ``**/x`` also matches a top-level ``x`` and ``src/**`` matches
    ``src`` itself and everything beneath it. ``..`` is refused rather than
    followed, since a pattern must not reach outside the directory the fence
    resolved.
    """
    if pattern.startswith("/"):
        raise ValueError("pattern must be relative to path")
    segments = [s for s in pattern.split("/") if s not in ("", ".")]
    if not segments:
        raise ValueError("empty pattern")
    if ".." in segments:
        raise ValueError("pattern must not contain '..'")
    compiled: list[re.Pattern[str] | None] = []
    for segment in segments:
        if segment == "**":
            if not compiled or compiled[-1] is not None:
                compiled.append(None)
        elif "**" in segment:
            raise ValueError("'**' can only be an entire path component")
        else:
            compiled.append(re.compile(fnmatch.translate(segment), _CASE_FLAGS))
    return tuple(compiled)


def _matches(components: list[str], segments: tuple[re.Pattern[str] | None, ...], i: int = 0, j: int = 0) -> bool:
    """Whether the path ``components`` satisfy the compiled ``segments`` from ``i`` and ``j`` on."""
    while j < len(segments):
        segment = segments[j]
        if segment is None:
            if j == len(segments) - 1:
                return True
            return any(_matches(components, segments, k, j + 1) for k in range(i, len(components) + 1))
        if i >= len(components) or not segment.match(components[i]):
            return False
        i += 1
        j += 1
    return i == len(components)


def _partial_clause() -> str:
    return (
        f"PARTIAL result: the search hit its {tree_walk.WALK_DEADLINE_S:g}s traversal budget "
        "before finishing, so absence of a match is not conclusive -- narrow the path or pattern"
    )


class FindTool(_FsTool):
    """Find files by glob pattern, sorted by recency, over the shared bounded walk."""

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
        glob_expr = f"**/{pattern}" if pattern and "/" not in pattern else pattern
        try:
            segments = _compile_pattern(glob_expr)
        except (ValueError, re.error) as e:
            return f"Error running find: {e}"
        matches, incomplete = await asyncio.to_thread(self._search, base, segments)
        if not matches and not incomplete:
            return "No files found matching pattern."

        matches.sort(key=lambda m: m[0], reverse=True)
        total = len(matches)
        shown = matches[:cap]
        result = "\n".join(rel for _, rel in shown) or "No files found matching pattern."
        notes = []
        if total > cap:
            notes.append(f"showing first {cap} of {total} results")
        if incomplete:
            notes.append(_partial_clause())
        if notes:
            result += f"\n\n({'; '.join(notes)})"
        return result

    def _search(self, base: Path, segments: tuple[re.Pattern[str] | None, ...]) -> tuple[list[tuple[float, str]], bool]:
        """Collect ``(mtime, display path)`` for every entry under ``base`` the pattern matches.

        Runs in a worker thread. An entry is matched on its path components
        relative to ``base``, and a matched directory is shown with a trailing
        slash. The second value says whether the walk hit its deadline, in
        which case the list is what was found before it did.
        """
        matches: list[tuple[float, str]] = []
        base_parts = len(base.parts)
        try:
            for root, dirs, names in tree_walk.walk(base):
                parents = list(Path(root).parts[base_parts:])
                prefix = "/".join(parents) + "/" if parents else ""
                for name, is_dir in [(d, True) for d in dirs] + [(n, False) for n in names]:
                    if _matches([*parents, name], segments):
                        matches.append((self._mtime(Path(root, name)), f"{prefix}{name}{'/' if is_dir else ''}"))
        except TimeoutError:
            return matches, True
        return matches, False

    @staticmethod
    def _mtime(p: Path) -> float:
        try:
            return p.stat().st_mtime
        except OSError:
            return 0.0
