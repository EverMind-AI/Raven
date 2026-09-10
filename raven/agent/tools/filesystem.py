"""File system tools: read, write, edit, list."""

import difflib
import mimetypes
import os
import re
from pathlib import Path
from typing import Any

from raven.agent import workdir
from raven.contracts.tool import FileChange, Tool, ToolResult
from raven.utils.images import detect_image_mime

_DIFF_MAX_LINES = 400


def _write_text_bytes(content: str) -> bytes:
    """The bytes ``Path.write_text`` would leave on disk for ``content``.

    It writes through a text layer with ``newline=None``, which translates
    every ``\n`` to ``os.linesep`` -- so on a CRLF platform this is not the
    plain UTF-8 encoding of the string.
    """
    return content.replace("\n", os.linesep).encode("utf-8")


def _unified(before: str, after: str, name: str) -> str | None:
    """Unified diff of one write, or None when there is nothing useful to show.

    A UI cannot reconstruct this later: by the time the call is reported, the
    content it replaced is already overwritten. A rewrite too large to render is
    dropped whole rather than truncated -- half a diff reads as a smaller change
    than the one that happened.
    """
    if before == after:
        return None
    out = list(
        difflib.unified_diff(
            before.splitlines(),
            after.splitlines(),
            fromfile=name,
            tofile=name,
            lineterm="",
        )
    )
    if not out or len(out) > _DIFF_MAX_LINES:
        return None
    return "\n".join(out)


def resolve_path(
    path: str,
    workspace: Path | None = None,
    allowed_dirs: tuple[Path, ...] = (),
) -> Path:
    """Resolve path against workspace (if relative) and enforce the allowed roots."""
    p = Path(path).expanduser()
    if not p.is_absolute() and workspace:
        p = workspace / p
    resolved = p.resolve()
    if allowed_dirs:
        for allowed in allowed_dirs:
            try:
                resolved.relative_to(Path(allowed).resolve())
                return resolved
            except ValueError:
                continue
        roots = ", ".join(str(d) for d in allowed_dirs)
        raise PermissionError(f"Path {path} is outside allowed directories {roots}")
    return resolved


def _with_current_root(allowed_dirs: tuple[Path, ...], bound: Path | None) -> tuple[Path, ...]:
    """Add the turn's *live* working-directory binding to the fence, when the fence is on.

    Tools are constructed once for the loop's whole lifetime, before any turn's
    working directory exists, so ``allowed_dirs`` can only ever carry the static
    roots (agent home). The per-turn root reaches the fence here, at resolve
    time, the same way ``ExecTool`` folds its per-call ``cwd`` into its roots.

    ``bound`` must be the raw result of ``workdir.current()`` (or ``None`` for
    a tool that does not follow the ambient binding at all) -- never a value
    that already fell back to the tool's own ``workspace``. Folding in that
    fallback would silently widen the fence to a root the operator never put
    in ``allowed_dirs``. An empty ``allowed_dirs`` must stay empty regardless
    -- that is the "fence disabled" signal ``resolve_path`` checks for.
    """
    if not allowed_dirs or bound is None:
        return allowed_dirs
    return (bound, *allowed_dirs)


_URL_SCHEME_RE = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.\-]*)://")
_HOST_LIKE_RE = re.compile(r"^[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+/")


def _missing(path: str) -> str:
    """The refusal for a path that is not there, naming the tool that can reach it.

    A URL resolves to a path that cannot exist, and "File not found" describes the
    wrong problem: the file is not missing, the argument belongs to another tool.
    Reported as a missing file it reads as a misspelling, so the caller tries the
    same URL again with the workspace prefixed, percent-decoded, a directory up --
    none of which can work, and the search that produced the URL stalls there.

    Only the message changes: this answers inside the branch that has already found
    nothing on disk, so a path that does resolve never reaches it.
    """
    raw = path.strip()
    if match := _URL_SCHEME_RE.match(raw):
        if match.group("scheme").lower() == "file":
            return f"Error: {path} is a file:// URL; read_file takes a plain path -- try {raw[7:] or '/'}"
        return (
            f"Error: {path} is a URL, not a path on this machine. read_file only reads the "
            "filesystem; fetch it with web_fetch, which returns the page as text."
        )
    if _HOST_LIKE_RE.match(raw):
        return (
            f"Error: File not found: {path} -- which reads as a URL rather than a path. "
            "If it is one, fetch it with web_fetch; read_file only reads the filesystem."
        )
    return f"Error: File not found: {path}"


class _FsTool(Tool):
    """Shared base for filesystem tools — common init and path resolution."""

    def __init__(
        self,
        workspace: Path | None = None,
        allowed_dirs: tuple[Path, ...] = (),
        *,
        follow_binding: bool = True,
    ):
        self._workspace = workspace
        self._allowed_dirs = allowed_dirs
        # A sub-agent run is a background asyncio task that can outlive the turn
        # that spawned it, since SubagentManager.spawn captures the workspace at
        # spawn time; its tools must fence on the directory captured for that
        # run, not on whatever the ambient ContextVar happens to hold when the
        # task finally executes. The main loop's tools keep following the live
        # binding as normal.
        self._follow_binding = follow_binding

    def _resolve(self, path: str) -> Path:
        bound = workdir.current() if self._follow_binding else None
        current_root = bound or self._workspace
        return resolve_path(path, current_root, _with_current_root(self._allowed_dirs, bound))


# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------


class ReadFileTool(_FsTool):
    """Read file contents with optional line-based pagination."""

    _MAX_CHARS = 128_000
    _DEFAULT_LIMIT = 2000

    @property
    def name(self) -> str:
        return "read_file"

    @property
    def description(self) -> str:
        return (
            "Read the contents of a file. Text files return numbered lines — use offset and limit to "
            "paginate through large ones. Image files (PNG, JPEG, GIF, WebP, and other common formats) "
            "return the picture itself, downscaled if needed."
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

    # Sniffed from the first bytes, not the extension — a .txt that is really a
    # PNG must not be decoded as UTF-8, and vice versa.
    _MAGIC_SNIFF_BYTES = 32

    def _read_image(self, fp: Path, mime: str) -> ToolResult:
        """Image branch: preprocess, then hand back text *and* an image block.

        ``model_text`` is deliberately self-sufficient. Providers that cannot put
        an image in a tool result never see ``blocks``, and the same text is what
        lands in session history, so it carries the metadata and the path rather
        than pointing at a picture that may not be there.
        """
        from raven.agent.tools import media

        payload, out_mime, meta = media.prepare_image(fp.read_bytes(), mime)
        summary = media.describe_image(fp, meta)
        return ToolResult(
            model_text=summary,
            display_text=f"{fp.name} ({meta['width']}x{meta['height']}, ~{meta['tokens']} tok)",
            blocks=[
                media.text_block(summary),
                media.image_block(media.to_data_uri(payload, out_mime)),
            ],
        )

    async def execute(self, path: str, offset: int = 1, limit: int | None = None, **kwargs: Any) -> str | ToolResult:
        try:
            fp = self._resolve(path)
            if not fp.exists():
                return _missing(path)
            if not fp.is_file():
                return f"Error: Not a file: {path}"

            with fp.open("rb") as fh:
                head = fh.read(self._MAGIC_SNIFF_BYTES)
            sniffed = detect_image_mime(head)
            mime = sniffed
            if mime is None:
                guessed = mimetypes.guess_type(fp.name)[0]
                # Magic bytes only cover the four formats every target inlines, so
                # a raster format Pillow can convert (BMP/TIFF/ICO) reaches this
                # branch as an extension guess. A guess can be wrong both ways --
                # .svg is XML with no Pillow decoder, a .png stub may hold text --
                # so it is trusted provisionally and a decode failure falls back
                # to the text path below rather than refusing to read the file.
                if guessed and guessed.startswith("image/"):
                    mime = guessed
            if mime is not None:
                from raven.agent.tools.media import ImageTooLargeError

                try:
                    return self._read_image(fp, mime)
                except ImageTooLargeError as e:
                    return f"Error: {e}"
                except Exception as e:
                    if sniffed is not None:
                        return f"Error decoding image {path}: {e}"
                    # An extension-only guess that would not decode: fall through.

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
            numbered = [f"{start + i + 1}| {line}" for i, line in enumerate(all_lines[start:end])]
            result = "\n".join(numbered)

            if len(result) > self._MAX_CHARS:
                trimmed, chars = [], 0
                for line in numbered:
                    chars += len(line) + 1
                    if chars > self._MAX_CHARS:
                        break
                    trimmed.append(line)
                end = start + len(trimmed)
                result = "\n".join(trimmed)

            if end < total:
                result += f"\n\n(Showing lines {offset}-{end} of {total}. Use offset={end + 1} to continue.)"
            else:
                result += f"\n\n(End of file — {total} lines total)"
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
        return (
            "Write content to a file at the given path. Creates parent directories if needed. "
            "Use mode=append to add to a file rather than replace it."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "The file path to write to"},
                "content": {"type": "string", "description": "The content to write"},
                "mode": {
                    "type": "string",
                    "enum": ["overwrite", "append"],
                    "description": (
                        "append adds to the end of the file; overwrite (default) replaces it. "
                        "Use append to continue a file you have already started -- adding to "
                        "one written earlier, or building up long content across several calls."
                    ),
                },
            },
            "required": ["path", "content"],
        }

    @property
    def truncation_hint(self) -> str:
        return (
            "Arguments cut off by that limit are discarded whole rather than partly saved, "
            "so write the content across several calls: mode=overwrite to start a file, "
            "mode=append to continue one you have already begun."
        )

    @property
    def incomplete_hint(self) -> str:
        return (
            "arguments cut off that way are discarded whole rather than partly saved, so "
            "send the content across several calls -- mode=overwrite to start a file, "
            "mode=append to continue one you have already begun."
        )

    async def execute(self, path: str, content: str, mode: str = "overwrite", **kwargs: Any) -> str:
        if mode not in ("overwrite", "append"):
            return f"Error: unknown mode '{mode}' for write_file. Use 'overwrite' or 'append'."
        # An empty append is refused rather than treated as a no-op: it is what
        # a call cut off before its content field looks like, and the one thing
        # it must never silently become is an overwrite.
        if mode == "append" and not content:
            return "Error: write_file with mode=append needs content; refusing to append nothing."
        try:
            fp = self._resolve(path)
            # Read before writing: a whole-file write carries no record of what
            # it replaced, so a panel handed only the arguments draws every line
            # of an overwrite as an addition.
            before = ""
            # Three states, not two, and the third is why this is a separate
            # flag: absent, present and readable, present and not decodable as
            # text. Only the first is a new file, and reporting the third as one
            # would tell a client every line is an addition to a file that was
            # already there.
            previous: str | None = None
            if fp.is_file():
                try:
                    before = previous = fp.read_text(encoding="utf-8")
                except (UnicodeDecodeError, OSError):
                    before = ""
                    previous = None
                    unreadable = True
                else:
                    unreadable = False
            else:
                unreadable = False
            fp.parent.mkdir(parents=True, exist_ok=True)
            if mode == "append":
                with fp.open("a", encoding="utf-8") as handle:
                    handle.write(content)
                return f"Successfully appended {len(content)} bytes to {fp}"
            # The bytes decide, with no text gate in front of them. Text
            # equality is wrong in both directions here: ``read_text`` folds
            # CRLF to LF, so a CRLF file looks equal to the LF content that
            # would replace it, and content carrying CRLF never looks equal to
            # the file already holding exactly those bytes.
            would_write = _write_text_bytes(content)
            if fp.is_file():
                try:
                    unchanged = fp.read_bytes() == would_write
                except OSError:
                    # Reading to decide whether to write must not become a
                    # precondition for writing: a POSIX mode-0200 file is
                    # writable and unreadable, and overwriting one worked
                    # before this check existed. An unreadable file is simply
                    # not a no-op.
                    unchanged = False
                if unchanged:
                    # A byte count for a rewrite that changed nothing reads as
                    # progress, and ``model_text`` is the whole account of the
                    # call the caller gets: a loop shrinking a file can rewrite
                    # the same bytes several times before anything says
                    # otherwise. Nothing is written, so there is no diff or
                    # file_change to carry either.
                    return ToolResult(
                        f"File unchanged: {fp} already holds exactly these "
                        f"{len(would_write)} bytes, so nothing was written.",
                    )
            fp.write_text(content, encoding="utf-8")
            return ToolResult(
                f"Successfully wrote {len(content)} bytes to {fp}",
                diff=_unified(before, content, str(fp)),
                # Beside the rendered diff, not instead of it: the unified form is
                # what a text surface shows, and this is what a surface with its
                # own diff view needs. Both come from strings already in hand, so
                # neither costs a second read. Withheld entirely for a file that
                # existed and could not be read, because there is no ``before``
                # to give and every way of faking one misinforms the reader.
                file_change=None if unreadable else FileChange(path=str(fp), after=content, before=previous),
            )
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error writing file: {e}"


# ---------------------------------------------------------------------------
# edit_file
# ---------------------------------------------------------------------------


def _find_match(content: str, old_text: str) -> tuple[str | None, int]:
    """Locate old_text in content: exact first, then line-trimmed sliding window.

    Both inputs should use LF line endings (caller normalises CRLF).
    Returns (matched_fragment, count) or (None, 0).
    """
    if old_text in content:
        return old_text, content.count(old_text)

    old_lines = old_text.splitlines()
    if not old_lines:
        return None, 0
    stripped_old = [line.strip() for line in old_lines]
    content_lines = content.splitlines()

    candidates = []
    for i in range(len(content_lines) - len(stripped_old) + 1):
        window = content_lines[i : i + len(stripped_old)]
        if [line.strip() for line in window] == stripped_old:
            candidates.append("\n".join(window))

    if candidates:
        return candidates[0], len(candidates)
    return None, 0


class EditFileTool(_FsTool):
    """Edit a file by replacing text with fallback matching."""

    @property
    def name(self) -> str:
        return "edit_file"

    @property
    def description(self) -> str:
        return (
            "Edit a file by replacing old_text with new_text. "
            "Supports minor whitespace/line-ending differences. "
            "Set replace_all=true to replace every occurrence."
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
            },
            "required": ["path", "old_text", "new_text"],
        }

    async def execute(
        self,
        path: str,
        old_text: str,
        new_text: str,
        replace_all: bool = False,
        **kwargs: Any,
    ) -> str:
        try:
            fp = self._resolve(path)
            if not fp.exists():
                return f"Error: File not found: {path}"

            raw = fp.read_bytes()
            uses_crlf = b"\r\n" in raw
            content = raw.decode("utf-8").replace("\r\n", "\n")
            match, count = _find_match(content, old_text.replace("\r\n", "\n"))

            if match is None:
                return self._not_found_msg(old_text, content, path)
            if count > 1 and not replace_all:
                return (
                    f"Warning: old_text appears {count} times. "
                    "Provide more context to make it unique, or set replace_all=true."
                )

            norm_new = new_text.replace("\r\n", "\n")
            new_content = content.replace(match, norm_new) if replace_all else content.replace(match, norm_new, 1)
            if uses_crlf:
                new_content = new_content.replace("\n", "\r\n")

            fp.write_bytes(new_content.encode("utf-8"))
            normalised = new_content.replace("\r\n", "\n")
            return ToolResult(
                f"Successfully edited {fp}",
                # Compared line-for-line rather than passing the two snippets:
                # `replace_all` can change several places at once, and the
                # arguments alone do not say where.
                diff=_unified(content, normalised, str(fp)),
                # The whole file both ways. An edit's arguments carry only the
                # replaced fragment, so a surface handed those would render a
                # fragment as though it were the file.
                file_change=FileChange(path=str(fp), after=normalised, before=content),
            )
        except PermissionError as e:
            return f"Error: {e}"
        except Exception as e:
            return f"Error editing file: {e}"

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
                return f"Error: Directory not found: {path}"
            if not dp.is_dir():
                return f"Error: Not a directory: {path}"

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
