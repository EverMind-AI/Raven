# ClaudeCodeScanner Design Spec

## Context

Raven cold-start import Layer 1 is done (types, Scanner Protocol, ImportState,
store metadata). This spec covers the first Scanner implementation:
**ClaudeCodeScanner**, which discovers and reads Claude Code's local conversation
history and memory files for import into EverOS.

Approach: EverMe-compatible with targeted fixes — follow EverMe's proven parsing
logic, fix known deficiencies (isMeta filtering), adapt to Raven's local-only
architecture (no redaction, paragraph-based memory splitting, content truncation
at 10K chars).

## File Structure

```
raven/importer/scanners/__init__.py           # re-export ClaudeCodeScanner
raven/importer/scanners/claude_code.py        # full implementation
tests/test_importer_claude_code_scanner.py    # tests
```

Update `raven/importer/__init__.py` to re-export ClaudeCodeScanner.

## scan() -- Discovery

Scans `~/.claude/` (constructor-injectable for tests). Returns all importable
units tagged with SourceKind.

### Sources

| Source | kind | source_key | file_paths |
|---|---|---|---|
| `~/.claude/CLAUDE.md` | MEMORY_FILE | `global-claude-md` | (CLAUDE.md,) |
| `~/.claude/projects/{proj}/memory/*.md` | MEMORY_FILE | `{proj}-memory` | (all .md in dir) |
| `~/.claude/projects/{proj}/*.jsonl` | CONVERSATION | session UUID (filename) | (the .jsonl,) |

### Filters

- Active session: `time.time() - mtime < 300` (5 min) -> skip JSONL
- Memory file > 1MB -> skip
- Subagent directories -> not scanned (only project-root-level .jsonl)
- `~/.claude/` missing -> empty list, no error

## read(CONVERSATION) -- JSONL Parsing

### Event Filtering

| Condition | Action |
|---|---|
| No `message` dict | Skip (attachment, system, UI events) |
| `isMeta: true` | Skip (system injection, EverMe bug fix) |
| `isCompactSummary: true` | Skip (compaction artifact) |
| `isApiErrorMessage: true` | Skip (synthetic error) |
| `message.role` not user/assistant | Skip |

### Message Extraction

**role=user, content is string:**
One ImportMessage(role="user").

**role=user, content is list:**
Extract {type:"tool_result"} blocks -> each one ImportMessage(role="tool").
Extract {type:"text"} blocks -> one ImportMessage(role="user").
Order: tool_result first, then text (EverMe-compatible).

**role=assistant, content is string:**
One ImportMessage(role="assistant").

**role=assistant, content is list:**
Collect {type:"text"} blocks (skip "thinking"/"redacted_thinking") -> content.
Collect {type:"tool_use"} blocks -> tool_calls tuple in OpenAI function calling
format: `{id, type:"function", function:{name, arguments: json.dumps(input)}}`.
Verified: matches EverOS ToolCall Pydantic model exactly.

### Content Truncation

All ImportMessage content, regardless of role: if > 10,000 chars, truncate to
10,000 + "...". No head/tail strategy, simple head truncation.

Does NOT apply to tool_calls arguments (EverMe-compatible: EverMe also does not
truncate tool_call arguments).

### Timestamp

Parse `event["timestamp"]` (ISO 8601) -> ms epoch. Every conversation event has
a timestamp (verified: 6,053/6,053 across 20 sessions), no fallback needed.

### Session Construction

- app_id = "claude_code"
- project_id = project directory name (e.g. "-Users-admin-Documents-GitHub-Raven")
- session_id = "import-claude_code-{source_key}"
- sender_id = "user" for user/tool roles, "assistant" for assistant role
- tool_call_id on role="tool" messages, linking back to the assistant's
  tool_calls[].id (Anthropic toolu_ IDs, passed through verbatim)

## read(MEMORY_FILE) -- Markdown Parsing

### Message Structure (all memory files)

Three-level boundary signals so EverOS can segment content clearly:

1. **Session preamble** -- first message announces what is coming and how many
   files. E.g. "These are my memory files from Claude Code for the X project,
   16 files in total."
2. **Per-file**: intro (with filename) -> paragraphs -> file-end marker.
   E.g. "That is all the content from architecture.md."
3. **Session epilogue** -- final message confirms all files are done.

### Global CLAUDE.md

- Frontmatter parsed (stripped if present, though current files have none)
- Split by paragraph (empty line separator), each paragraph one ImportMessage
- Intro (language follows content):
  - CJK: "zhe shi wo zai Claude Code zhong she ding de quan ju pian hao he gui ze."
  - EN: "These are my global preferences and rules set in Claude Code."
- File-end marker: "That is all the content from CLAUDE.md."
- session_id = "import-claude_code-global", project_id = "global"

### Project memory/*.md

**MEMORY.md is processed first** (index file -- establishes global context
before detail files).

Per file:
1. Parse YAML frontmatter (pyyaml, already a dependency) -> name, metadata.type
2. Generate intro ImportMessage (language follows content detection, includes
   filename for cross-file reference resolution):

| Condition | Chinese | English |
|---|---|---|
| MEMORY.MD | yi xia shi xiang mu ji yi zong lan wen jian MEMORY.md | Here is the project memory overview file named MEMORY.md |
| type=reference | yi xia shi guan yu {name} de xiang mu zhi shi, wen jian {filename} | Here is project knowledge about {name}, file named {filename} |
| type=feedback | yi xia shi wo dui AI xie zuo de pian hao -- {name}, wen jian {filename} | Here is my preference for AI collaboration -- {name}, file named {filename} |
| type=project | yi xia shi guan yu {name} de xiang mu bi ji, wen jian {filename} | Here is a project note about {name}, file named {filename} |
| Other/no fm | yi xia shi guan yu {name} de bi ji, wen jian {filename} | Here is a note about {name}, file named {filename} |

3. Body (after frontmatter) split by paragraph, each one ImportMessage
4. File-end marker: "{filename} de nei rong dao ci jie shu." /
   "That is all the content from {filename}."
5. All files in one project -> one ImportSession (wrapped in preamble + epilogue)
6. Timestamp = file mtime (ms epoch)

Language detection: check first 200 chars for CJK characters (regex `[one-ideo-range]`).

## Error Handling

| Scenario | Handling |
|---|---|
| `~/.claude/` missing | scan() returns [], no error |
| Project dir unreadable | Skip, log warning, continue |
| JSONL line parse error | Skip line, log debug, continue |
| Memory file > 1MB | Skip at scan, log info |
| YAML parse failure | Treat as no frontmatter, use filename for intro |
| Memory file IO error | Skip file, log warning, continue |
| Empty content | Do not produce ImportMessage |
| UTF-8 decode error | `errors="replace"` |

Core principle: single file failure never aborts the overall scan/read.

## Async Strategy

All file I/O via `asyncio.to_thread` -- consistent with codebase pattern. No
aiofiles dependency.

## EverMe Compatibility Matrix

| Aspect | EverMe | Raven | Delta |
|---|---|---|---|
| Role resolution | 3-level fallback | message.role only | Simplified, all events have it |
| isMeta filter | No | Yes | Bug fix |
| isCompactSummary filter | No | Yes | New |
| isApiErrorMessage filter | No | Yes | New |
| Redaction | Yes | No | All local |
| Content truncation | 8K rune head/tail | 10K char head-only | Wider, simpler |
| tool_calls truncation | No | No | Aligned |
| Memory file splitting | 8K char boundary | Paragraph-based | More natural |
| Memory intro language | N/A (no intros) | Follows content, includes filename | New |
| Memory file boundaries | N/A | Preamble + per-file end + epilogue | New |
| Memory file order | N/A | MEMORY.md first (index), rest alphabetical | New |
| Frontmatter parsing | N/A | Yes (pyyaml), type includes "project" | New |
| tool_call_id linkage | N/A (truncated) | Preserved verbatim from JSONL | New |
| Session ID format | import-claude-code-{id} | Same (import-claude_code-{id}) | Aligned |
| Timestamp fallback | mtime + line_num | Not needed | All events have ts |
| Subagent files | Included (recursive) | Excluded | Simpler, main session has full context |
| Symlink handling | MD skips, JSONL follows | No special handling | Rare in ~/.claude/ |

## Verification

```bash
uv run python -c "from raven.importer.scanners import ClaudeCodeScanner; print('OK')"
uv run pytest tests/test_importer_claude_code_scanner.py -x -v
uv run pytest tests/test_importer_types.py tests/test_importer_state.py -x
uv run pytest --ignore=tests/integration -x
```
