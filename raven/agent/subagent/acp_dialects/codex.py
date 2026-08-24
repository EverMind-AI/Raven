"""codex-acp: reading eleven codex tools out of five ACP ``kind`` values.

The adapter reports a call three ways -- the spec's ``kind`` enum, a title
written for a human, and discriminators in ``rawInput`` and ``_meta`` that no
other adapter sends -- and none of the three is codex's model-facing tool name.
Two of those names survive anyway, because codex runs those tools as shell
commands and the command string is on the frame: ``apply_patch`` is argv[0] of a
real command, and an MCP call names its server and tool in ``rawInput``.

Measured on v1.1.14. See ``docs/specs/2026-08-23-codex-acp-tool-parsing-design.md``
for the frame captures every row here is read from.
"""

from __future__ import annotations

import re
import shlex
from typing import Any

from raven.agent.subagent.acp_dialects.base import AcpDialect, ToolCall, ToolResult, _dict

# Codex's own item types (`codex-rs/protocol/src/items.rs`), which the adapter
# flattens into five ACP `kind` values on the way out. Recovering them is what
# lets a row say `webSearch` instead of `search`.
_MCP = "mcpToolCall"

# Three item types all arrive as `kind: "read"` and are separated only by the
# title the adapter wrote. Pinned to 1.1.14: a rephrased title degrades to
# `commandExecution.read`, which is the honest reading of a `kind: "read"` with
# no other marker, rather than a wrong one.
_VIEW_IMAGE_TITLE = "View Image"
_LIST_FILES_TITLE = "List files"
_IMAGE_GEN_TITLE = "Image generation"


def _argv0(command: str) -> str:
    """The program a command runs, for the one case where it is the tool's name.

    `apply_patch` is a real command whose argv[0] is literally that
    (`codex-rs/apply-patch/src/invocation.rs`); the patch body arrives on stdin.
    """
    try:
        parts = shlex.split(command)
    except ValueError:
        parts = command.split()
    return parts[0] if parts else ""


def _web_search_subject(raw: dict[str, Any]) -> str:
    """The queries, chosen the way the adapter's own title formatter chooses them.

    Reimplemented rather than lifted off the title, because the title is
    prefixed (``Web search: ``) and a prefix inside a subject reads as part of
    the value.
    """
    action = _dict(raw.get("action"))
    kind = action.get("type")
    if kind == "openPage":
        url = action.get("url")
        return url if isinstance(url, str) else ""
    if kind == "findInPage":
        pattern = action.get("pattern")
        return pattern if isinstance(pattern, str) else ""
    queries = [q for q in (action.get("queries") or []) if isinstance(q, str) and q]
    if queries:
        return ", ".join(queries)
    for candidate in (action.get("query"), raw.get("query")):
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return ""


def _file_change_paths(content: Any) -> str:
    """The paths a native ``fileChange`` frame's diff blocks touched.

    Each entry may be missing, not a dict, or carry no ``path`` -- this shape
    never fired in either capture, so nothing about it is guaranteed here.
    Joined the same way ``subject_from_result`` joins multiple patch targets,
    so the two read consistently.
    """
    if not isinstance(content, list):
        return ""
    paths: list[str] = []
    for entry in content:
        if not isinstance(entry, dict) or entry.get("type") != "diff":
            continue
        path = entry.get("path")
        if isinstance(path, str) and path.strip():
            paths.append(path.strip())
    return ", ".join(dict.fromkeys(paths))


# codex's own patch envelope (`codex-rs/apply-patch`). The body reaches the
# command on stdin, so the file it touched is in the output and nowhere else.
_PATCH_TARGET = re.compile(r"^\*\*\* (?:Update|Add|Delete) File: (?P<path>.+?)\s*$", re.MULTILINE)

# Terminal output arrives CRLF-terminated, and a C0 control character reaches
# the transcript as garbage. Tab and newline are the two that carry meaning.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x0c\x0e-\x1f]")


def _clean(text: str) -> str:
    return _CONTROL.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


class CodexDialect(AcpDialect):
    key = "codex-acp"

    plan_tool_name = "update_plan"
    """codex's own name for the tool behind a plan frame (``plan_tool.rs``)."""

    def tool_name(self, update: dict[str, Any]) -> str:
        raw = _dict(update.get("rawInput"))
        meta = _dict(update.get("_meta"))
        codex_meta = _dict(meta.get("codex"))
        title = update.get("title") if isinstance(update.get("title"), str) else ""
        kind = update.get("kind")

        command = raw.get("command")
        if isinstance(command, str) and _argv0(command) == "apply_patch":
            return "apply_patch"
        if meta.get("is_mcp_tool_call"):
            server, tool = raw.get("server"), raw.get("tool")
            if isinstance(server, str) and isinstance(tool, str):
                return f"mcp.{server}.{tool}"
            return _MCP
        if kind == "execute" and "arguments" in raw and "command" not in raw:
            # A dynamic tool's real name is the title; the adapter puts it there
            # and nowhere else.
            return title or "dynamicToolCall"
        if raw.get("type") == "webSearch":
            return "webSearch"
        # Unmeasured: neither capture reached these three branches. Written from
        # the adapter's `createCollabAgentToolCallUpdate`, `createSubAgentActivityUpdate`
        # and `createContextCompactionStartUpdate`.
        if codex_meta.get("collaboration"):
            return "collabAgentToolCall"
        if codex_meta.get("subagent"):
            return "subAgentActivity"
        if meta.get("contextCompaction"):
            return "contextCompaction"
        if kind == "other" and title.startswith(_IMAGE_GEN_TITLE):
            return "imageGeneration"
        if kind == "read":
            if title.startswith(_VIEW_IMAGE_TITLE):
                return "imageView"
            return "commandExecution.listFiles" if title.startswith(_LIST_FILES_TITLE) else "commandExecution.read"
        if kind == "edit":
            # Unmeasured: neither capture reached this branch, because codex ran
            # `apply_patch` as a command both times. Written from the adapter's
            # `createFileChangeUpdate`; the first real frame is the confirmation.
            return "fileChange"
        if kind == "search":
            return "commandExecution.search"
        if kind == "execute":
            return "commandExecution"
        return super().tool_name(update)

    def names_call(self, update: dict[str, Any]) -> bool:
        """A codex discriminator names the call even when ``kind`` is absent."""
        if super().names_call(update):
            return True
        raw = _dict(update.get("rawInput"))
        meta = _dict(update.get("_meta"))
        codex_meta = _dict(meta.get("codex"))
        return bool(
            raw.get("type") == "webSearch"
            or meta.get("is_mcp_tool_call")
            or codex_meta.get("collaboration")
            or codex_meta.get("subagent")
            or meta.get("contextCompaction")
        )

    def result(self, update: dict[str, Any]) -> ToolResult:
        ok = update.get("status") == "completed"
        raw = update.get("rawOutput")
        if isinstance(raw, dict):
            formatted = raw.get("formatted_output")
            exit_code = raw.get("exit_code")
            if isinstance(exit_code, int) and exit_code != 0:
                ok = False
            if isinstance(formatted, str):
                # An empty-but-present formatted_output is the real answer for a
                # command that printed nothing, so the exit code is what says so.
                return ToolResult(text=_clean(formatted) or f"(no output, exit {exit_code})", ok=ok)

        text = super().result(update).text
        return ToolResult(text=_clean(text), ok=ok)

    def subject_from_result(self, update: dict[str, Any], *, name: str | None = None) -> str | None:
        """A subject codex reports only once the call has finished.

        Two tools do this. ``apply_patch`` names the files it changed in its own
        envelope, and ``imageGeneration`` names where it wrote the image. Only
        ``savedPath`` is taken from the latter, never ``revisedPrompt``: the
        back-fill stores what it finds under ``path``, and a prompt keyed as a
        path is the mislabel this dialect exists to prevent.

        Gated on ``name`` before any of that content matching happens, because
        the completed frame carries none of ``tool_name``'s own discriminators --
        nothing on it would otherwise stop a ``commandExecution.search`` whose
        output happens to contain a patch-envelope-shaped line from being read
        as one. The caller supplies ``name`` from the call's opening frame; an
        unnamed call is the safe default and returns ``None``.
        """
        if name not in {"apply_patch", "imageGeneration"}:
            return None
        raw = update.get("rawOutput")
        if not isinstance(raw, dict):
            return None
        formatted = raw.get("formatted_output")
        if isinstance(formatted, str):
            targets = list(dict.fromkeys(_PATCH_TARGET.findall(formatted)))
            if targets:
                return ", ".join(targets)
        saved = raw.get("savedPath")
        return saved.strip() if isinstance(saved, str) and saved.strip() else None

    def permission_command(self, params: dict[str, Any]) -> str | None:
        """The command as codex parsed it, not as bash received it.

        ``toolCall.rawInput.command`` is the argument to ``bash -lc`` and arrives
        wrapped in its own quotes. ``_meta.codex.params.commandActions`` holds the
        same command already parsed (codex's ``ParsedCommand``), which is what a
        row should show.
        """
        actions = _dict(_dict(_dict(params.get("_meta")).get("codex")).get("params")).get("commandActions")
        if isinstance(actions, list):
            for action in actions:
                command = _dict(action).get("command")
                if isinstance(command, str) and command.strip():
                    return command.strip()
        command = super().permission_command(params)
        return command.strip('"').strip() if command else None

    def subject_field(self, update: dict[str, Any]) -> tuple[str, str]:
        """The subject to show beside the verb, and the key that names it.

        A pair rather than a string because one name can carry either of two
        things: a ``commandExecution.read`` shows the command when the
        permission frame recovered one and the path when it did not, and storing
        a path under ``command`` would mislabel it for every later reader.
        """
        name = self.tool_name(update)
        raw = _dict(update.get("rawInput"))
        meta_codex = _dict(_dict(update.get("_meta")).get("codex"))

        if name == "webSearch":
            return "query", _web_search_subject(raw)
        if name == "collabAgentToolCall":
            prompt = raw.get("prompt")
            return "prompt", prompt.strip() if isinstance(prompt, str) else ""
        if name == "subAgentActivity":
            path = _dict(meta_codex.get("subagent")).get("path")
            leaf = path.rstrip("/").rsplit("/", 1)[-1] if isinstance(path, str) and path else ""
            return "argument", leaf
        if name == "contextCompaction":
            return "argument", ""
        if name == "fileChange":
            # Shape from the adapter's `createFileChangeUpdate` and its diff-block
            # builders (`createAddFileContent` / `createUpdateFileContent` /
            # `createDeleteFileContent`), not from a frame capture. Returns "path"
            # even when empty, so a change with no recoverable path still routes to
            # a truthful key instead of falling to the generic tail below.
            return "path", _file_change_paths(update.get("content"))
        # An MCP or dynamic call is the only shape carrying `arguments`; a
        # dynamic one is named by its title, so the name cannot be matched on.
        if "arguments" in raw:
            arguments = raw.get("arguments")
            first = next(
                (v.strip() for v in _dict(arguments).values() if isinstance(v, str) and v.strip()),
                "",
            )
            return "argument", first
        if name == "apply_patch":
            # Filled from the patch envelope in the completed frame; the opening
            # frame's only "command" is the tool's own name.
            return "path", ""
        if name == "imageGeneration":
            # Filled from the completed frame's savedPath, never from rawInput's
            # own prompt: that field is prose, and this empty placeholder is what
            # keeps the generic tail below from keying it as a path instead.
            return "path", ""

        command = raw.get("command")
        if isinstance(command, str) and command.strip():
            return "command", command.strip()
        path = raw.get("path")
        if isinstance(path, str) and path.strip():
            return "path", path.strip()
        located = super().argument(update)
        return ("path" if located else "argument"), located

    def argument(self, update: dict[str, Any]) -> str:
        return self.subject_field(update)[1]

    def call(self, update: dict[str, Any]) -> ToolCall:
        """The adapter's call with the subject promoted to the front of its input.

        Insertion order is load-bearing at the read boundary, which takes the
        first string value it finds. Building the dict here rather than adding
        a ``tool_vocabulary.ARGUMENT_KEY`` row is what keeps a promotion keyed on
        a raven name from relabelling a codex field.
        """
        base = super().call(update)
        key, subject = self.subject_field(update)
        # A title that only repeats the verb says nothing, and `ToolCall.subject`
        # falls back to it: codex titles an `apply_patch` "apply_patch", which
        # rendered as `apply_patch apply_patch` until its result named the file.
        title = "" if base.title == base.name else base.title
        if not subject:
            return ToolCall(id=base.id, name=base.name, argument="", title=title, raw_input=base.raw_input)
        merged: dict[str, Any] = {key: subject}
        for name, value in base.raw_input.items():
            if name != key:
                merged[name] = value
        return ToolCall(id=base.id, name=base.name, argument=subject, title=title, raw_input=merged)


__all__ = ["CodexDialect"]
