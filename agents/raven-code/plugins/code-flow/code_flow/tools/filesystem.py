"""The fork's file-tool face over trunk's file tools.

Each class here subclasses the trunk tool it replaces and changes three things:
the model-facing schema (the fork's parameter spelling and its conduct-bearing
descriptions), the accepted spellings (``cast_params`` maps the host's
``path`` / ``old_text`` / ``new_text`` onto the fork's names, so a call in
either convention runs), and two pieces of conduct the fork measured its way
to -- a read ledger that refuses to edit content unseen by the current session, and
a post-write Python syntax verdict in the same tool result.

Everything else (path resolution against the bound working directory, the
unified diff the UI renders, ``FileChange`` on the result, image reads) is
trunk's, inherited unchanged.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Any

from code_flow.tools.read_state import Owner, ReadLedger, current_ledger
from raven.agent.tools import filesystem as trunk
from raven.contracts.tool import FileChange, ToolResult


def python_syntax_note(fp: Path, text: str) -> str:
    """Post-write syntax verdict for ``.py`` files, appended to the result.

    Deterministic and in-band: a write or edit that leaves the file
    unparseable is told so in the same tool result, costing zero extra
    rounds. Syntax only -- a line that crashes at runtime still needs tests.
    """
    if fp.suffix != ".py":
        return ""
    try:
        ast.parse(text)
    except SyntaxError as exc:
        location = f"line {exc.lineno}" if exc.lineno else "unknown line"
        return (
            f"\nWARNING: {fp.name} now has a Python syntax error at {location}: "
            f"{exc.msg}. The file cannot even be parsed; fix it before moving on."
        )
    except Exception:  # noqa: BLE001 - a verdict the parser cannot give is no verdict
        return ""
    return ""


def _append_note(result: str | ToolResult, note: str) -> str | ToolResult:
    if not note:
        return result
    if isinstance(result, ToolResult):
        result.model_text = f"{result.model_text}{note}"
        return result
    return f"{result}{note}"


class _Aliased:
    """``cast_params`` that maps the host's spelling onto the fork's.

    Runs before schema validation, so a call spelled either way validates
    against the fork schema and reaches ``execute`` under one name. When a
    call carries both spellings the fork's wins and the legacy key is dropped.
    """

    ALIASES: dict[str, str] = {}

    def __init__(
        self, *args: Any, ledger: ReadLedger | None = None, read_owner: Owner | None = None, **kwargs: Any
    ) -> None:
        super().__init__(*args, **kwargs)
        self._explicit_ledger = ledger
        self._read_owner = read_owner

    @property
    def _ledger(self) -> ReadLedger | None:
        return self._explicit_ledger if self._explicit_ledger is not None else current_ledger(self._read_owner)

    def cast_params(self, params: dict[str, Any]) -> dict[str, Any]:
        out = dict(params)
        for legacy, canonical in self.ALIASES.items():
            if legacy not in out:
                continue
            value = out.pop(legacy)
            out.setdefault(canonical, value)
        return out


class ReadFileTool(_Aliased, trunk.ReadFileTool):
    ALIASES = {"path": "file_path"}

    #: One line may spend this many characters before it is cut, the fork's
    #: budget. Trunk numbers a line whole, so a minified bundle or a base64
    #: blob on one line overruns the whole-result budget by itself: the result
    #: comes back with NO body and a note telling the model to continue from
    #: the same offset, which it can do forever. Cutting the line keeps the
    #: read useful and makes the description's promise true.
    _MAX_LINE_CHARS = 2_000

    @property
    def description(self) -> str:
        return (
            "Read the contents of a file. Returns numbered lines formatted as "
            "'12| text' -- the 'N| ' prefix is added by this tool and is NOT part "
            "of the file. Use offset and limit to paginate through large files. "
            f"Lines longer than {self._MAX_LINE_CHARS} chars are truncated with an "
            "explicit marker; use grep to search inside such files. "
            "Do not assume a path exists: locate it with glob or list_dir instead of guessing, "
            "and if a read fails, find the real path rather than retrying a guess."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        base = super().parameters
        props = dict(base["properties"])
        props = {"file_path": {"type": "string", "description": "The file path to read"}, **props}
        props.pop("path", None)
        return {"type": "object", "properties": props, "required": ["file_path"]}

    async def execute(self, file_path: str = "", offset: int = 1, limit: int | None = None, **kwargs: Any):  # type: ignore[override]
        if not file_path:
            return "Error: missing required parameter 'file_path'."
        result = await super().execute(path=file_path, offset=offset, limit=limit, **kwargs)
        if _failed(result):
            return result
        # Recorded only now, and only for a file that answers a stat: a read
        # that failed must not license an edit of content nobody has seen.
        try:
            fp = self._resolve(file_path)
        except Exception:  # noqa: BLE001 - the read itself reported the path problem
            return _cut_long_lines(result, self._MAX_LINE_CHARS)
        if (ledger := self._ledger) is not None:
            ledger.note(fp)
        if _bodyless(result):
            # One line longer than the whole-result budget leaves trunk's read
            # with nothing to show and a note pointing at the same offset, so
            # the cut has to happen before that budget is spent, not after.
            taken = self._read_cut(fp, offset, limit)
            if taken is not None:
                return taken
        return _cut_long_lines(result, self._MAX_LINE_CHARS)

    def _read_cut(self, fp: Path, offset: int, limit: int | None) -> str | None:
        """Trunk's own numbering, with every line cut to the line budget first.

        Returns ``None`` when the file cannot be read as text -- the caller
        then keeps whatever trunk answered rather than inventing something.
        """
        try:
            all_lines = fp.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return None
        total = len(all_lines)
        if total == 0:
            return None
        start = max(offset, 1) - 1
        if start >= total:
            return None
        end = min(start + (limit or self._DEFAULT_LIMIT), total)
        numbered = []
        for index, line in enumerate(all_lines[start:end]):
            if len(line) > self._MAX_LINE_CHARS:
                line = line[: self._MAX_LINE_CHARS] + f"... (line truncated to {self._MAX_LINE_CHARS} chars)"
            numbered.append(f"{start + index + 1}| {line}")
        body, chars, kept = [], 0, 0
        for line in numbered:
            chars += len(line) + 1
            if chars > self._MAX_CHARS and kept:
                break
            body.append(line)
            kept += 1
        shown_end = start + kept
        text = "\n".join(body)
        if shown_end < total:
            text += f"\n\n(Showing lines {start + 1}-{shown_end} of {total}. Use offset={shown_end + 1} to continue.)"
        else:
            text += f"\n\n(End of file — {total} lines total)"
        return text


class WriteFileTool(_Aliased, trunk.WriteFileTool):
    ALIASES = {"path": "file_path"}

    def __init__(self, *args: Any, syntax_note: bool = True, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._syntax_note = syntax_note

    @property
    def description(self) -> str:
        return (
            "Write content to a file at the given path. Creates parent directories if needed. "
            "ALWAYS prefer editing existing files in the codebase: before creating a new file, "
            "check whether an existing file (an entry point, a stub, a TODO) already owns that "
            "responsibility, and NEVER write new files unless the task requires it. "
            "If the target file already exists, read it with read_file before overwriting. "
            "mode='overwrite' replaces the entire file; mode='append' adds content at the end. "
            "Appending to an existing file does not count as reading its previous content. "
            "After replacing a symbol that is used elsewhere, check its other call "
            "sites, overrides, and sibling branches for the same required change."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        base = super().parameters
        props = dict(base["properties"])
        props = {"file_path": {"type": "string", "description": "The file path to write to"}, **props}
        props.pop("path", None)
        return {"type": "object", "properties": props, "required": ["file_path", "content"]}

    async def execute(self, file_path: str = "", content: str | None = None, mode: str = "overwrite", **kwargs: Any):  # type: ignore[override]
        if not file_path or content is None:
            return "Error: missing required parameter(s): write_file needs file_path and content."
        try:
            fp = self._resolve(file_path)
        except (OSError, ValueError) as exc:
            return f"Error: {exc}"
        ledger = self._ledger
        known = ledger is not None and ledger.status(fp) == ReadLedger.OK
        authored = mode == "overwrite" or not fp.exists()
        result = await super().execute(path=file_path, content=content, mode=mode, **kwargs)
        if _failed(result):
            return result
        try:
            fp = self._resolve(file_path)
        except Exception:  # noqa: BLE001 - the write itself reported the path problem
            return result
        # Only a full write or a previously current record covers the old
        # content. Appending an unseen tail must not license editing the head.
        if ledger is not None:
            if authored or known:
                ledger.note(fp)
            else:
                ledger.forget(fp)
        if not self._syntax_note:
            return result
        try:
            text = fp.read_text(encoding="utf-8", errors="replace") if mode == "append" else content
        except Exception:  # noqa: BLE001 - no verdict beats a wrong one
            return result
        return _append_note(result, python_syntax_note(fp, text))


def _respell(result: str | ToolResult) -> str | ToolResult:
    """Trunk's messages name its own parameters; the model was shown the fork's.

    Only the first line is respelled. Trunk's own messages put their sentence
    there and any quoted file content on the lines after it (the not-found
    report prints the closest matching excerpt), and a blanket replacement
    rewrote the FILE's text: a file that really contains ``old_text`` was
    shown to the model as containing ``old_string``, so the model copied an
    excerpt that never matched anything.
    """
    swaps = (("old_text", "old_string"), ("new_text", "new_string"))

    def respelled(text: str) -> str:
        head, sep, tail = text.partition("\n")
        for legacy, spelled in swaps:
            head = head.replace(legacy, spelled)
        return head + sep + tail

    if isinstance(result, ToolResult):
        result.model_text = respelled(result.model_text)
        return result
    return respelled(str(result))


def _cut_long_lines(result: str | ToolResult, budget: int) -> str | ToolResult:
    """Cut any numbered line past ``budget`` characters, marking the cut."""

    def cut(text: str) -> str:
        if not any(len(line) > budget for line in text.split("\n")):
            return text
        out = []
        for line in text.split("\n"):
            if len(line) > budget:
                line = line[:budget] + f"... (line truncated to {budget} chars)"
            out.append(line)
        return "\n".join(out)

    if isinstance(result, ToolResult):
        result.model_text = cut(result.model_text)
        return result
    return cut(str(result))


def _bodyless(result: str | ToolResult) -> bool:
    """A read that answered with a note and no numbered line at all."""
    text = result.model_text if isinstance(result, ToolResult) else str(result)
    return "| " not in text and "(Showing lines" in text


def _failed(result: str | ToolResult) -> bool:
    text = result.model_text if isinstance(result, ToolResult) else str(result)
    return text.lstrip().startswith("Error")


class EditFileTool(_Aliased, trunk.EditFileTool):
    ALIASES = {"path": "file_path", "old_text": "old_string", "new_text": "new_string"}

    def __init__(
        self,
        *args: Any,
        syntax_note: bool = True,
        require_read: bool = True,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._syntax_note = syntax_note
        self._require_read = require_read

    @property
    def description(self) -> str:
        prerequisite = (
            "You must first read the file with read_file or write its complete content with write_file "
            "in this session of this Raven-Code instance. Unread or externally changed files are rejected. "
            "Appending to unseen existing content does not satisfy this check. "
            if self._require_read
            else "Read the file before editing so the replacement matches its current content. "
        )
        return (
            "Edit a file by replacing old_string with new_string. "
            + prerequisite
            + "old_string must be the file's actual content -- never include the "
            "'N| ' line-number prefix that read_file output adds. "
            "Without occurrence, supports minor whitespace/line-ending differences. "
            "If old_string matches several places, either set replace_all=true to "
            "change every one, or occurrence=<n> to change only the nth non-overlapping exact match "
            "(1-based, in file order, with CRLF normalized to LF). These options cannot be combined. "
            "After changing a symbol that is used elsewhere, check its other call "
            "sites, overrides, and sibling branches for the same required change."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        base = super().parameters
        props = dict(base["properties"])
        for legacy in ("path", "old_text", "new_text"):
            props.pop(legacy, None)
        props = {
            "file_path": {"type": "string", "description": "The file path to edit"},
            "old_string": {"type": "string", "description": "The text to find and replace"},
            "new_string": {"type": "string", "description": "The text to replace with"},
            **props,
            "occurrence": {
                "type": "integer",
                "description": (
                    "Replace only the Nth non-overlapping exact match (1-based, in file order, "
                    "with CRLF normalized to LF). Cannot be combined with replace_all."
                ),
                "minimum": 1,
            },
        }
        return {"type": "object", "properties": props, "required": ["file_path", "old_string", "new_string"]}

    async def execute(  # type: ignore[override]
        self,
        file_path: str = "",
        old_string: str | None = None,
        new_string: str | None = None,
        replace_all: bool = False,
        occurrence: int | None = None,
        **kwargs: Any,
    ):
        if not file_path or old_string is None or new_string is None:
            return "Error: missing required parameter(s): edit_file needs file_path, old_string and new_string."
        if replace_all and occurrence is not None:
            return "Error: replace_all and occurrence are mutually exclusive; pass one or the other."
        if not old_string:
            return "Error: old_string is empty. To create or overwrite a file use write_file instead."
        try:
            fp = self._resolve(file_path)
        except Exception as exc:  # noqa: BLE001 - the same wording trunk's tool gives
            return f"Error: {exc}"
        if not fp.exists():
            return f"Error: File not found: {file_path}. To create a new file use write_file instead."
        ledger = self._ledger
        if self._require_read:
            if ledger is None:
                return "Error: edit_file has no active Raven-Code session. Bind the session before editing."
            seen = ledger.status(fp)
            if seen == ReadLedger.UNREAD:
                return (
                    f"Error: you have not read {file_path} in this session -- editing unread content "
                    "is rejected. Read the file with read_file first, then edit the text you actually saw."
                )
            if seen == ReadLedger.STALE:
                return (
                    f"Error: {file_path} changed since you last read it, so the text you are editing "
                    "may no longer be there. Read it again, then edit the content you actually saw."
                )
        if occurrence is not None and occurrence < 1:
            return "Error: occurrence must be 1 or greater (it counts matches in file order)."
        if occurrence is not None:
            result = self._edit_occurrence(fp, file_path, old_string, new_string, occurrence)
        else:
            result = _respell(
                await super().execute(
                    path=file_path, old_text=old_string, new_text=new_string, replace_all=replace_all, **kwargs
                )
            )
        if not isinstance(result, ToolResult) or result.file_change is None:
            return result
        # The edit is the newest thing anyone has seen of this file, so the
        # ledger follows it -- otherwise the tool's own write would read back
        # as someone else's change and the next edit would be refused as stale.
        if ledger is not None:
            ledger.note(fp)
        if not self._syntax_note:
            return result
        try:
            after = fp.read_text(encoding="utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            return result
        return _append_note(result, python_syntax_note(fp, after))

    @staticmethod
    def _edit_occurrence(fp: Path, shown: str, old: str, new: str, occurrence: int) -> str | ToolResult:
        """Replace exactly the Nth exact match; trunk's fuzzy matching stays
        with the default path, since 'the Nth of several' needs one unambiguous
        counting rule."""
        try:
            raw = fp.read_bytes()
            uses_crlf = b"\r\n" in raw
            content = raw.decode("utf-8").replace("\r\n", "\n")
        except UnicodeDecodeError:
            return f"Error: {shown} is not a UTF-8 text file; edit_file cannot edit it."
        except OSError as exc:
            return f"Error: {exc}"
        old = old.replace("\r\n", "\n")
        new = new.replace("\r\n", "\n")
        count = content.count(old)
        if count == 0:
            return f"Error: old_string not found in {shown}. Verify the file content with read_file."
        if occurrence > count:
            return (
                f"Error: occurrence={occurrence} but old_string matches only {count} "
                f"place(s) in {shown}. Use read_file to see the current content."
            )
        start = 0
        for _ in range(occurrence):
            index = content.find(old, start)
            start = index + len(old)
        after = content[:index] + new + content[index + len(old) :]
        # Written back in the file's own line ending, the way trunk's edit
        # path does it: reading through ``read_text`` and writing back through
        # ``write_text`` rewrote every line of a CRLF file, so a one-line edit
        # arrived as a whole-file diff.
        fp.write_bytes((after.replace("\n", "\r\n") if uses_crlf else after).encode("utf-8"))
        line = content.count("\n", 0, index) + 1
        return ToolResult(
            model_text=f"Successfully edited {fp} (occurrence {occurrence} of {count}, line {line}).",
            diff=trunk._unified(content, after, str(fp)),
            file_change=FileChange(path=str(fp), after=after, before=content),
        )


class ListDirTool(trunk.ListDirTool):
    @property
    def description(self) -> str:
        return (
            "List the contents of a directory. "
            "For a first look at a repository, use recursive=true -- a flat listing "
            "hides everything inside subdirectories. "
            "Do not assume a directory exists; list its parent first when unsure. "
            "Common noise directories (.git, node_modules, __pycache__, etc.) are auto-ignored."
        )


__all__ = [
    "EditFileTool",
    "ListDirTool",
    "ReadFileTool",
    "ReadLedger",
    "WriteFileTool",
    "python_syntax_note",
]
