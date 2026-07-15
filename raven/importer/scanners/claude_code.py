"""ClaudeCodeScanner -- discover and read Claude Code local data."""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path
from typing import Any

from loguru import logger

from raven.importer.types import (
    ImportMessage,
    ImportSession,
    Platform,
    ScanResult,
    SourceKind,
)
from raven.utils.text import is_cjk, parse_frontmatter, parse_iso_ts_ms

_ACTIVE_THRESHOLD_S = 300
_MAX_MEMORY_FILE_BYTES = 1_048_576
_CONTENT_TRUNCATE_LIMIT = 10_000
_APP_ID = "claude_code"

_SKIP_CONTENT_TYPES = frozenset({"thinking", "redacted_thinking"})

_INTRO_TEMPLATES: dict[str | None, tuple[str, str]] = {
    "MEMORY.MD": (
        "以下是项目记忆总览文件 {filename}",
        "Here is the project memory overview file named {filename}",
    ),
    "reference": (
        "以下是关于 {name} 的项目知识，文件 {filename}",
        "Here is project knowledge about {name}, file named {filename}",
    ),
    "feedback": (
        "以下是我对 AI 协作的偏好——{name}，文件 {filename}",
        "Here is my preference for AI collaboration -- {name}, file named {filename}",
    ),
    "project": (
        "以下是关于 {name} 的项目笔记，文件 {filename}",
        "Here is a project note about {name}, file named {filename}",
    ),
    None: (
        "以下是关于 {name} 的笔记，文件 {filename}",
        "Here is a note about {name}, file named {filename}",
    ),
}

_FILE_END_TEMPLATES = (
    "{filename} 的内容到此结束。",
    "That is all the content from {filename}.",
)

_GLOBAL_INTRO = (
    "这是我在 Claude Code 中设定的全局偏好和规则。",
    "These are my global preferences and rules set in Claude Code.",
)

_SESSION_PREAMBLE = (
    "这是我在 Claude Code 中 {proj} 项目的记忆文件，共 {count} 个。",
    "These are my memory files from Claude Code for the {proj} project, {count} files in total.",
)

_SESSION_EPILOGUE = (
    "以上是 {proj} 项目的全部 {count} 个记忆文件。",
    "End of all {count} memory files for the {proj} project.",
)


# ---------------------------------------------------------------------------
# Helpers -- content extraction
# ---------------------------------------------------------------------------


def _truncate(text: str) -> str:
    if len(text) <= _CONTENT_TRUNCATE_LIMIT:
        return text
    return text[:_CONTENT_TRUNCATE_LIMIT] + "..."


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        if block.get("type") in _SKIP_CONTENT_TYPES:
            continue
        if block.get("type") == "text":
            t = block.get("text", "")
            if t:
                parts.append(t)
    return "\n\n".join(parts)


def _tool_calls_from_content(content: list[dict[str, Any]], ts: int) -> tuple[dict[str, Any], ...] | None:
    calls: list[dict[str, Any]] = []
    for i, block in enumerate(content):
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        call_id = block.get("id") or f"claude_tool_{ts}_{i}"
        raw_input = block.get("input", {})
        calls.append(
            {
                "id": call_id,
                "type": "function",
                "function": {
                    "name": block.get("name", "unknown"),
                    "arguments": json.dumps(raw_input) if not isinstance(raw_input, str) else raw_input,
                },
            }
        )
    return tuple(calls) if calls else None


def _tool_results_from_content(content: list[dict[str, Any]], ts: int, sender: str) -> list[ImportMessage]:
    msgs: list[ImportMessage] = []
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        tool_use_id = block.get("tool_use_id") or block.get("toolCallId") or block.get("tool_call_id")
        if not tool_use_id:
            logger.debug("tool_result block without tool_use_id, dropped")
            continue
        inner = block.get("content", "")
        if isinstance(inner, list):
            inner = _text_from_content(inner)
        elif not isinstance(inner, str):
            inner = json.dumps(inner)
        if not inner:
            inner = "(empty tool result)"
        msgs.append(
            ImportMessage(
                role="tool",
                content=_truncate(inner),
                timestamp=ts,
                sender_id=sender,
                tool_call_id=tool_use_id,
            )
        )
    return msgs


# ---------------------------------------------------------------------------
# Helpers -- memory file parsing
# ---------------------------------------------------------------------------


def _make_intro(filename: str, fm: dict[str, Any], cjk: bool) -> str:
    meta = fm.get("metadata", {}) if isinstance(fm.get("metadata"), dict) else {}
    name = fm.get("name") or Path(filename).stem

    key = filename.upper() if filename.upper() == "MEMORY.MD" else meta.get("type")
    cjk_tpl, en_tpl = _INTRO_TEMPLATES.get(key, _INTRO_TEMPLATES[None])
    tpl = cjk_tpl if cjk else en_tpl
    return tpl.format(name=name, filename=filename)


def _make_file_end(filename: str, cjk: bool) -> str:
    tpl = _FILE_END_TEMPLATES[0] if cjk else _FILE_END_TEMPLATES[1]
    return tpl.format(filename=filename)


def _split_paragraphs(text: str) -> list[str]:
    """Split text by blank lines, discard empty paragraphs."""
    paragraphs = re.split(r"\n\s*\n", text)
    return [p.strip() for p in paragraphs if p.strip()]


def _build_file_messages(intro: str, body: str, file_end: str, mtime_ms: int) -> list[ImportMessage]:
    paragraphs = _split_paragraphs(body)
    messages = [
        ImportMessage(role="user", content=intro, timestamp=mtime_ms, sender_id="user"),
    ]
    for i, para in enumerate(paragraphs):
        messages.append(
            ImportMessage(
                role="user",
                content=para,
                timestamp=mtime_ms + i + 1,
                sender_id="user",
            )
        )
    messages.append(
        ImportMessage(
            role="user",
            content=file_end,
            timestamp=mtime_ms + len(paragraphs) + 1,
            sender_id="user",
        )
    )
    return messages


def _memory_files_sorted(paths: tuple[Path, ...]) -> list[Path]:
    """Sort memory files with MEMORY.md first (index), rest alphabetical."""
    index = [p for p in paths if p.name.upper() == "MEMORY.MD"]
    rest = sorted(p for p in paths if p.name.upper() != "MEMORY.MD")
    return index + rest


def _project_dir_from_path(file_path: Path, projects_dir: Path) -> str:
    try:
        rel = file_path.relative_to(projects_dir)
        return rel.parts[0] if rel.parts else "unknown"
    except ValueError:
        return "unknown"


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------


class ClaudeCodeScanner:
    """Discovers and reads Claude Code local data for cold-start import."""

    platform = Platform.CLAUDE_CODE

    def __init__(self, claude_dir: Path | None = None) -> None:
        base = claude_dir or (Path.home() / ".claude")
        self._claude_dir = base
        self._projects_dir = base / "projects"

    async def scan(self) -> list[ScanResult]:
        return await asyncio.to_thread(self._scan_sync)

    async def read(self, result: ScanResult) -> ImportSession:
        if result.kind == SourceKind.CONVERSATION:
            return await asyncio.to_thread(self._read_conversation, result)
        return await asyncio.to_thread(self._read_memory, result)

    # -- scan ---------------------------------------------------------------

    def _scan_sync(self) -> list[ScanResult]:
        results: list[ScanResult] = []
        self._scan_global_md(results)
        self._scan_projects(results)
        return results

    def _scan_global_md(self, out: list[ScanResult]) -> None:
        gmd = self._claude_dir / "CLAUDE.md"
        if not gmd.is_file():
            return
        try:
            st = gmd.stat()
        except OSError:
            return
        out.append(
            ScanResult(
                source_key="global-claude-md",
                platform=Platform.CLAUDE_CODE,
                kind=SourceKind.MEMORY_FILE,
                file_paths=(gmd,),
                estimated_size=st.st_size,
                mtime=st.st_mtime,
            )
        )

    def _scan_projects(self, out: list[ScanResult]) -> None:
        if not self._projects_dir.is_dir():
            return
        now = time.time()
        try:
            proj_dirs = sorted(self._projects_dir.iterdir())
        except OSError:
            return
        for proj in proj_dirs:
            if not proj.is_dir():
                continue
            self._scan_project_memory(proj, out)
            self._scan_project_sessions(proj, out, now)

    def _scan_project_memory(self, proj: Path, out: list[ScanResult]) -> None:
        mem_dir = proj / "memory"
        if not mem_dir.is_dir():
            return
        md_files: list[Path] = []
        total_size = 0
        max_mtime = 0.0
        try:
            for f in sorted(mem_dir.iterdir()):
                if not f.is_file() or f.suffix.lower() != ".md":
                    continue
                try:
                    st = f.stat()
                except OSError:
                    continue
                if st.st_size > _MAX_MEMORY_FILE_BYTES:
                    logger.info("Skipping oversized memory file: {} ({} bytes)", f, st.st_size)
                    continue
                md_files.append(f)
                total_size += st.st_size
                max_mtime = max(max_mtime, st.st_mtime)
        except OSError:
            return
        if not md_files:
            return
        out.append(
            ScanResult(
                source_key=f"{proj.name}-memory",
                platform=Platform.CLAUDE_CODE,
                kind=SourceKind.MEMORY_FILE,
                file_paths=tuple(md_files),
                estimated_size=total_size,
                mtime=max_mtime,
            )
        )

    def _scan_project_sessions(self, proj: Path, out: list[ScanResult], now: float) -> None:
        try:
            entries = sorted(proj.iterdir())
        except OSError:
            return
        for f in entries:
            if not f.is_file() or f.suffix.lower() != ".jsonl":
                continue
            try:
                st = f.stat()
            except OSError:
                continue
            if now - st.st_mtime < _ACTIVE_THRESHOLD_S:
                logger.debug("Skipping active session: {}", f.name)
                continue
            out.append(
                ScanResult(
                    source_key=f.stem,
                    platform=Platform.CLAUDE_CODE,
                    kind=SourceKind.CONVERSATION,
                    file_paths=(f,),
                    estimated_size=st.st_size,
                    mtime=st.st_mtime,
                )
            )

    # -- read: conversation -------------------------------------------------

    def _read_conversation(self, result: ScanResult) -> ImportSession:
        path = result.file_paths[0]
        proj_id = _project_dir_from_path(path, self._projects_dir)

        messages: list[ImportMessage] = []
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line_num, raw_line in enumerate(fh, start=1):
                raw_line = raw_line.strip()
                if not raw_line:
                    continue
                try:
                    ev = json.loads(raw_line)
                except json.JSONDecodeError:
                    logger.debug("Bad JSON at {}:{}, skipped", path.name, line_num)
                    continue
                self._extract_event(ev, messages)

        return ImportSession(
            app_id=_APP_ID,
            project_id=proj_id,
            session_id=f"import-{_APP_ID}-{result.source_key}",
            messages=tuple(messages),
        )

    def _extract_event(
        self,
        ev: dict[str, Any],
        out: list[ImportMessage],
    ) -> None:
        if ev.get("isMeta") is True:
            return
        if ev.get("isCompactSummary") is True:
            return
        if ev.get("isApiErrorMessage") is True:
            return

        msg = ev.get("message")
        if not isinstance(msg, dict):
            return
        role = msg.get("role")
        if role not in ("user", "assistant"):
            return

        content = msg.get("content")
        if content is None:
            return

        ts = parse_iso_ts_ms(ev.get("timestamp"))
        if ts is None:
            return
        sender = "user" if role == "user" else "assistant"

        if isinstance(content, str):
            text = content.strip()
            if text:
                out.append(
                    ImportMessage(
                        role=role,
                        content=_truncate(text),
                        timestamp=ts,
                        sender_id=sender,
                    )
                )
            return

        if not isinstance(content, list):
            return

        if role == "user":
            for tr in _tool_results_from_content(content, ts, sender):
                out.append(tr)
            text = _text_from_content(content)
            if text:
                out.append(ImportMessage(role="user", content=_truncate(text), timestamp=ts, sender_id=sender))
        else:
            text = _text_from_content(content)
            tool_calls = _tool_calls_from_content(content, ts)
            if text or tool_calls:
                out.append(
                    ImportMessage(
                        role="assistant",
                        content=_truncate(text),
                        timestamp=ts,
                        sender_id=sender,
                        tool_calls=tool_calls,
                    )
                )

    # -- read: memory -------------------------------------------------------

    def _read_memory(self, result: ScanResult) -> ImportSession:
        if result.source_key == "global-claude-md":
            return self._read_global_md(result)
        return self._read_project_memory(result)

    def _read_global_md(self, result: ScanResult) -> ImportSession:
        path = result.file_paths[0]
        text = path.read_text(encoding="utf-8", errors="replace")
        _, body = parse_frontmatter(text)

        if not body.strip():
            return ImportSession(
                app_id=_APP_ID,
                project_id="global",
                session_id=f"import-{_APP_ID}-global",
            )

        mtime_ms = int(result.mtime * 1000)
        cjk = is_cjk(body)
        intro = _GLOBAL_INTRO[0] if cjk else _GLOBAL_INTRO[1]
        file_end = _make_file_end("CLAUDE.md", cjk)
        file_msgs = _build_file_messages(intro, body, file_end, mtime_ms)

        return ImportSession(
            app_id=_APP_ID,
            project_id="global",
            session_id=f"import-{_APP_ID}-global",
            messages=tuple(file_msgs),
        )

    def _read_project_memory(self, result: ScanResult) -> ImportSession:
        proj_name = result.source_key.removesuffix("-memory")
        base_mtime_ms = int(result.mtime * 1000)

        paths = _memory_files_sorted(result.file_paths)
        readable_count = sum(1 for p in paths if p.is_file())

        first_body = ""
        for p in paths:
            try:
                t = p.read_text(encoding="utf-8", errors="replace")
                _, b = parse_frontmatter(t)
                if b.strip():
                    first_body = b
                    break
            except OSError:
                continue
        cjk = is_cjk(first_body)

        preamble_tpl = _SESSION_PREAMBLE[0] if cjk else _SESSION_PREAMBLE[1]
        epilogue_tpl = _SESSION_EPILOGUE[0] if cjk else _SESSION_EPILOGUE[1]

        messages: list[ImportMessage] = [
            ImportMessage(
                role="user",
                content=preamble_tpl.format(proj=proj_name, count=readable_count),
                timestamp=base_mtime_ms,
                sender_id="user",
            ),
        ]

        ts_offset = 1
        for path in paths:
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                logger.warning("Cannot read memory file: {}", path)
                continue

            fm, body = parse_frontmatter(text)
            if not body.strip():
                continue

            try:
                file_mtime_ms = int(path.stat().st_mtime * 1000)
            except OSError:
                file_mtime_ms = base_mtime_ms

            file_cjk = is_cjk(body)
            intro = _make_intro(path.name, fm, file_cjk)
            file_end = _make_file_end(path.name, file_cjk)
            file_msgs = _build_file_messages(intro, body, file_end, file_mtime_ms)
            messages.extend(file_msgs)
            ts_offset += len(file_msgs)

        messages.append(
            ImportMessage(
                role="user",
                content=epilogue_tpl.format(proj=proj_name, count=readable_count),
                timestamp=base_mtime_ms + ts_offset,
                sender_id="user",
            ),
        )

        return ImportSession(
            app_id=_APP_ID,
            project_id=proj_name,
            session_id=f"import-{_APP_ID}-mem-{proj_name}",
            messages=tuple(messages),
        )


__all__ = ["ClaudeCodeScanner"]
