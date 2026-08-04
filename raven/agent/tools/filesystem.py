"""File system tools: read, write, edit, list."""

import difflib
from pathlib import Path
from typing import Any

from raven.agent.tools.base import Tool


def _resolve_path(path: str, workspace: Path | None = None, allowed_dir: Path | None = None) -> Path:
    """Resolve path against workspace (if relative) and enforce directory restriction."""
    p = Path(path).expanduser()
    if not p.is_absolute() and workspace:
        p = workspace / p
    resolved = p.resolve()
    if allowed_dir:
        try:
            resolved.relative_to(allowed_dir.resolve())
        except ValueError:
            raise PermissionError(f"Path {path} is outside allowed directory {allowed_dir}")
    return resolved


class FileReadTracker:
    """Which files the agent has seen this session, and at what mtime.

    Backs the read-before-edit rule: editing a file the model has never read
    means it is guessing at content (the not-found retry loops observed in
    eval), and editing one that changed since the last read means the edit is
    based on stale content. read_file / write_file / a successful edit_file
    all count as having seen the file.
    """

    def __init__(self) -> None:
        self._seen: dict[str, int] = {}

    def record(self, fp: Path) -> None:
        try:
            self._seen[str(fp)] = fp.stat().st_mtime_ns
        except OSError:
            pass

    def status(self, fp: Path) -> str:
        """ "unread" (never seen), "stale" (changed since seen), or "ok"."""
        recorded = self._seen.get(str(fp))
        if recorded is None:
            return "unread"
        try:
            current = fp.stat().st_mtime_ns
        except OSError:
            return "ok"
        return "ok" if current == recorded else "stale"


class _FsTool(Tool):
    """Shared base for filesystem tools — common init and path resolution."""

    def __init__(
        self,
        workspace: Path | None = None,
        allowed_dir: Path | None = None,
        tracker: FileReadTracker | None = None,
    ):
        self._workspace = workspace
        self._allowed_dir = allowed_dir
        self._tracker = tracker

    def _resolve(self, path: str) -> Path:
        return _resolve_path(path, self._workspace, self._allowed_dir)


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


class ReadFileTool(_FsTool):
    """Read file contents with optional line-based pagination."""

    # Per-call context budget. 128k chars (~40k tokens) let a couple of big
    # reads consume most of a long task's window; models page through the rest.
    _MAX_CHARS = 48_000
    _DEFAULT_LIMIT = 2000
    _MAX_LINE_CHARS = 2_000

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Read the contents of a file. Returns numbered lines formatted as "
            "'12| text' — the 'N| ' prefix is added by this tool and is NOT part "
            "of the file. Use offset and limit to paginate through large files. "
            f"Lines longer than {self._MAX_LINE_CHARS} chars are truncated; use "
            "grep to search inside such files."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to read"},
                "offset": {
                    "type": "integer",
                    "description": "Line number to start reading from (1-indexed, default 1)",
                    "minimum": 1,
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to read (default 2000)",
                    "minimum": 1,
                },
            },
            "required": ["path"],
        }

    async def execute(self, path: str, offset: int = 1, limit: int | None = None, **kwargs: Any) -> str:
        try:
            fp = self._resolve(path)
            if not fp.exists():
                return f"Error: File not found: {path}. Check the location with list_dir or find before retrying."
            if not fp.is_file():
                return f"Error: Not a file: {path}. Use list_dir to view a directory."

            all_lines = fp.read_text(encoding="utf-8").splitlines()
            total = len(all_lines)

            if offset < 1:
                offset = 1
            if total == 0:
                return f"(Empty file: {path})"
            if offset > total:
                return f"Error: offset {offset} is beyond end of file ({total} lines)"

            start = offset - 1
            end = min(start + (limit or self._DEFAULT_LIMIT), total)
            numbered = []
            for i, line in enumerate(all_lines[start:end]):
                if len(line) > self._MAX_LINE_CHARS:
                    line = line[: self._MAX_LINE_CHARS] + f"... (line truncated to {self._MAX_LINE_CHARS} chars)"
                numbered.append(f"{start + i + 1}| {line}")
            result = "\n".join(numbered)

            if len(result) > self._MAX_CHARS:
                trimmed, chars = [], 0
                for line in numbered:
                    chars += len(line) + 1
                    if chars > self._MAX_CHARS and trimmed:
                        break
                    trimmed.append(line)
                end = start + len(trimmed)
                result = "\n".join(trimmed)

            if end < total:
                result += f"\n\n(Showing lines {offset}-{end} of {total}. Use offset={end + 1} to continue.)"
            else:
                result += f"\n\n(End of file — {total} lines total)"
            if self._tracker is not None:
                self._tracker.record(fp)
            return result
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error reading file: {e}"


# ---------------------------------------------------------------------------
# write_file
# ---------------------------------------------------------------------------


class WriteFileTool(_FsTool):
    """Write content to a file."""

    @property
    def name(self) -> str:
        return "write_file"

    @property
    def description(self) -> str:
        return "Write content to a file at the given path. Creates parent directories if needed."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to write to"},
                "content": {"type": "string", "description": "The content to write"},
            },
            "required": ["path", "content"],
        }

    async def execute(self, path: str, content: str, **kwargs: Any) -> str:
        try:
            fp = self._resolve(path)
            existed = fp.exists()
            old_size = fp.stat().st_size if existed else 0
            fp.parent.mkdir(parents=True, exist_ok=True)
            data = content.encode("utf-8")
            fp.write_bytes(data)
            if self._tracker is not None:
                self._tracker.record(fp)
            # "Created" vs "Overwrote" tells the model whether it just replaced
            # something that already existed - the signal that catches both
            # accidental clobbers and rewrite-the-same-file loops.
            if existed:
                return f"Overwrote existing file {fp} (was {old_size} bytes, now {len(data)} bytes)"
            return f"Created new file {fp} ({len(data)} bytes)"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error writing file: {e}"


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------


def _find_matches(content: str, old_text: str) -> list[tuple[int, str]]:
    """Locate old_text in content: exact first, then line-trimmed sliding window.

    Both inputs should use LF line endings (caller normalises CRLF).
    Returns non-overlapping ``(char_offset, matched_fragment)`` pairs in file
    order — offsets rather than fragments alone, so the caller can replace a
    specific occurrence and report the line of each one.
    """
    if old_text in content:
        matches = []
        start = 0
        while (i := content.find(old_text, start)) != -1:
            matches.append((i, old_text))
            start = i + max(len(old_text), 1)
        return matches

    old_lines = old_text.splitlines()
    if not old_lines:
        return []
    stripped_old = [line.strip() for line in old_lines]
    content_lines = content.splitlines()
    line_offsets = []
    pos = 0
    for line in content_lines:
        line_offsets.append(pos)
        pos += len(line) + 1

    matches = []
    next_free = 0
    for i in range(len(content_lines) - len(stripped_old) + 1):
        if i < next_free:
            continue
        window = content_lines[i : i + len(stripped_old)]
        if [line.strip() for line in window] == stripped_old:
            matches.append((line_offsets[i], "\n".join(window)))
            next_free = i + len(stripped_old)
    return matches


def _line_of(content: str, offset: int) -> int:
    return content.count("\n", 0, offset) + 1


class EditFileTool(_FsTool):
    """Edit a file by replacing text with fallback matching."""

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Edit a file by replacing old_text with new_text. "
            "You must have read the file (read_file) earlier in the session — "
            "editing an unread file is rejected. "
            "old_text must be the file's actual content — never include the "
            "'N| ' line-number prefix that read_file output adds. "
            "Supports minor whitespace/line-ending differences. "
            "If old_text matches several places, either set replace_all=true to "
            "change every one, or occurrence=<n> to change only the nth."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to edit"},
                "old_text": {"type": "string", "description": "The text to find and replace"},
                "new_text": {"type": "string", "description": "The text to replace with"},
                "replace_all": {
                    "type": "boolean",
                    "description": "Replace all occurrences (default false)",
                },
                "occurrence": {
                    "type": "integer",
                    "description": (
                        "When old_text matches multiple places, replace only the Nth match (1-based, in file order)"
                    ),
                    "minimum": 1,
                },
            },
            "required": ["path", "old_text", "new_text"],
        }

    _MAX_LISTED_MATCHES = 8

    async def execute(
        self,
        path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
        occurrence: int | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            fp = self._resolve(path)
            if not fp.exists():
                return f"Error: File not found: {path}. To create a new file use write_file instead."
            if replace_all and occurrence is not None:
                return "Error: replace_all and occurrence are mutually exclusive; pass one or the other."
            if not old_text:
                return "Error: old_text is empty. To create or overwrite a file use write_file instead."
            if self._tracker is not None:
                seen = self._tracker.status(fp)
                if seen == "unread":
                    return (
                        f"Error: you have not read {path} in this session — editing unread content "
                        "means guessing at it. Read the file with read_file first (or replace it "
                        "wholesale with write_file)."
                    )
                if seen == "stale":
                    return (
                        f"Error: {path} has changed since you last read it (another command may have "
                        "modified it). Read it again with read_file before editing."
                    )

            raw = fp.read_bytes()
            uses_crlf = b"\r\n" in raw
            content = raw.decode("utf-8").replace("\r\n", "\n")
            matches = _find_matches(content, old_text.replace("\r\n", "\n"))
            count = len(matches)

            if count == 0:
                return self._not_found_msg(old_text, content, path)
            if occurrence is not None and occurrence > count:
                return (
                    f"Error: occurrence={occurrence} but old_text matches only {count} "
                    f"location(s) in {path}:\n{self._match_lines(content, matches)}"
                )
            if count > 1 and not replace_all and occurrence is None:
                return (
                    f"Error: old_text matches {count} locations in {path}:\n"
                    f"{self._match_lines(content, matches)}\n"
                    "Add surrounding context to old_text to make it unique, pass "
                    "occurrence=<n> to target one of the matches above, or set "
                    "replace_all=true to change every one."
                )

            norm_new = new_text.replace("\r\n", "\n")
            targets = matches if replace_all else [matches[(occurrence or 1) - 1]]
            new_content = content
            for off, frag in sorted(targets, reverse=True):
                new_content = new_content[:off] + norm_new + new_content[off + len(frag) :]
            written = new_content.replace("\n", "\r\n") if uses_crlf else new_content

            fp.write_bytes(written.encode("utf-8"))
            if self._tracker is not None:
                self._tracker.record(fp)
            if replace_all and count > 1:
                return f"Successfully edited {fp} ({count} occurrences replaced)"
            # Echo the edited region back so a bad edit (wrong indentation, an
            # extra deleted line) surfaces now instead of at the next run.
            # Offsets live in the LF-normalized space, so the snippet must be
            # taken there too — indexing the CRLF re-expansion shifts the
            # window and echoes untouched code as if the edit missed.
            line = _line_of(content, targets[0][0])
            snippet = self._after_snippet(new_content, targets[0][0], len(norm_new))
            return f"Successfully edited {fp} (line {line}). New content:\n{snippet}"
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error editing file: {e}"

    _SNIPPET_CONTEXT_LINES = 2
    _SNIPPET_MAX_LINES = 12

    @classmethod
    def _after_snippet(cls, new_content: str, offset: int, new_len: int) -> str:
        """Numbered lines around the replaced region, taken from the new content."""
        lines = new_content.splitlines()
        if not lines:
            return "(file is now empty)"
        last_char = min(offset + max(new_len - 1, 0), len(new_content) - 1) if new_content else 0
        first_line = _line_of(new_content, min(offset, len(new_content) - 1)) - 1
        last_line = _line_of(new_content, last_char) - 1
        lo = max(0, first_line - cls._SNIPPET_CONTEXT_LINES)
        hi = min(len(lines), last_line + cls._SNIPPET_CONTEXT_LINES + 1)
        clipped = hi - lo > cls._SNIPPET_MAX_LINES
        if clipped:
            hi = lo + cls._SNIPPET_MAX_LINES
        out = [f"{i + 1}| {lines[i][:200]}" for i in range(lo, hi)]
        if clipped:
            out.append("...")
        return "\n".join(out)

    @classmethod
    def _match_lines(cls, content: str, matches: list[tuple[int, str]]) -> str:
        listed = [
            f"  {i}. line {_line_of(content, off)}: {frag.splitlines()[0][:100]}"
            for i, (off, frag) in enumerate(matches[: cls._MAX_LISTED_MATCHES], 1)
        ]
        if len(matches) > cls._MAX_LISTED_MATCHES:
            listed.append(f"  ... and {len(matches) - cls._MAX_LISTED_MATCHES} more")
        return "\n".join(listed)

    @staticmethod
    def _not_found_msg(old_text: str, content: str, path: str) -> str:
        lines = content.splitlines(keepends=True)
        old_lines = old_text.splitlines(keepends=True)
        window = len(old_lines)

        best_ratio, best_start = 0.0, 0
        for i in range(max(1, len(lines) - window + 1)):
            ratio = difflib.SequenceMatcher(None, old_lines, lines[i : i + window]).ratio()
            if ratio > best_ratio:
                best_ratio, best_start = ratio, i

        if best_ratio > 0.5:
            diff = "\n".join(
                difflib.unified_diff(
                    old_lines,
                    lines[best_start : best_start + window],
                    fromfile="old_text (provided)",
                    tofile=f"{path} (actual, line {best_start + 1})",
                    lineterm="",
                )
            )
            return f"Error: old_text not found in {path}.\nBest match ({best_ratio:.0%} similar) at line {best_start + 1}:\n{diff}"
        return f"Error: old_text not found in {path}. No similar text found. Verify the file content."


# ---------------------------------------------------------------------------
# list_dir
# ---------------------------------------------------------------------------


class ListDirTool(_FsTool):
    """List directory contents with optional recursion."""

    _DEFAULT_MAX = 200
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

    @property
    def name(self) -> str:
        return "list_dir"

    @property
    def description(self) -> str:
        return (
            "List the contents of a directory. "
            "Set recursive=true to explore nested structure. "
            "Common noise directories (.git, node_modules, __pycache__, etc.) are auto-ignored."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The directory path to list"},
                "recursive": {
                    "type": "boolean",
                    "description": "Recursively list all files (default false)",
                },
                "max_entries": {
                    "type": "integer",
                    "description": "Maximum entries to return (default 200)",
                    "minimum": 1,
                },
            },
            "required": ["path"],
        }

    async def execute(
        self,
        path: str,
        recursive: bool = False,
        max_entries: int | None = None,
        **kwargs: Any,
    ) -> str:
        try:
            dp = self._resolve(path)
            if not dp.exists():
                return f"Error: Directory not found: {path}. Check the parent with list_dir or locate it with find."
            if not dp.is_dir():
                return f"Error: Not a directory: {path}. Use read_file to view a file."

            cap = max_entries or self._DEFAULT_MAX
            items: list[str] = []
            total = 0

            if recursive:
                for item in sorted(dp.rglob("*")):
                    if any(p in self._IGNORE_DIRS for p in item.parts):
                        continue
                    total += 1
                    if len(items) < cap:
                        rel = item.relative_to(dp)
                        items.append(f"{rel}/" if item.is_dir() else str(rel))
            else:
                for item in sorted(dp.iterdir()):
                    if item.name in self._IGNORE_DIRS:
                        continue
                    total += 1
                    if len(items) < cap:
                        pfx = "📁 " if item.is_dir() else "📄 "
                        items.append(f"{pfx}{item.name}")

            if not items and total == 0:
                return f"Directory {path} is empty"

            result = "\n".join(items)
            if total > cap:
                result += f"\n\n(truncated, showing first {cap} of {total} entries)"
            return result
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error listing directory: {e}"
