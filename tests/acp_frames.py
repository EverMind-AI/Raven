"""Frames copied verbatim from ACP captures, for tests to read instead of invent.

Sources: a codex-acp 1.1.14 direct chat
(`traces/logs/acp-frames/2026-08-23/Writer-092353638638.jsonl`) and a probe run
driving a four-step task to force a plan, a patch and two shell commands. Fields
none of the readers touch are trimmed; nothing is rephrased.
"""

from __future__ import annotations

from typing import Any

CODEX_WEBSEARCH_OPEN: dict[str, Any] = {
    "sessionUpdate": "tool_call",
    "toolCallId": "exec-02ba4d28",
    "kind": "search",
    "title": "Web search",
    "status": "in_progress",
    "rawInput": {"type": "webSearch", "id": "exec-02ba4d28", "query": "", "action": None},
}

CODEX_WEBSEARCH_DONE: dict[str, Any] = {
    "sessionUpdate": "tool_call_update",
    "toolCallId": "exec-02ba4d28",
    "title": "Web search: site:developers.openai.com/codex Codex overview coding agent capabilities",
    "status": "completed",
    "rawInput": {
        "type": "webSearch",
        "id": "exec-02ba4d28",
        "query": "site:developers.openai.com/codex Codex overview coding agent capabilities ...",
        "action": {
            "type": "search",
            "query": None,
            "queries": [
                "site:developers.openai.com/codex Codex overview coding agent capabilities",
                "site:developers.openai.com/codex models GPT-5 Codex",
            ],
        },
    },
}

CODEX_READ_OPEN: dict[str, Any] = {
    "sessionUpdate": "tool_call",
    "toolCallId": "exec-9694bddc",
    "status": "in_progress",
    "kind": "read",
    "title": "Read file '/tmp/codexprobe/ws/calc.py'",
    "locations": [{"path": "/tmp/codexprobe/ws/calc.py"}],
}

CODEX_READ_DONE: dict[str, Any] = {
    "sessionUpdate": "tool_call_update",
    "toolCallId": "exec-9694bddc",
    "status": "completed",
    "rawOutput": {"formatted_output": "def add(a, b):\n    return a - b\n", "exit_code": 0},
}

CODEX_READ_PERMISSION: dict[str, Any] = {
    "sessionId": "01a02e1a",
    "toolCall": {
        "toolCallId": "exec-9694bddc",
        "kind": "execute",
        "status": "pending",
        "rawInput": {"command": "\"sed -n '1,200p' calc.py\"", "cwd": "/tmp/codexprobe/ws"},
    },
    "options": [
        {"optionId": "allow_always", "name": "Allow for Session", "kind": "allow_always"},
        {"optionId": "reject_once", "name": "Reject", "kind": "reject_once"},
    ],
    "_meta": {
        "codex": {
            "params": {
                "command": "/bin/bash -lc \"sed -n '1,200p' calc.py\"",
                "cwd": "/tmp/codexprobe/ws",
                "commandActions": [
                    {
                        "type": "read",
                        "command": "sed -n '1,200p' calc.py",
                        "name": "calc.py",
                        "path": "/tmp/codexprobe/ws/calc.py",
                    }
                ],
            }
        }
    },
}

CODEX_PATCH_OPEN: dict[str, Any] = {
    "sessionUpdate": "tool_call",
    "toolCallId": "exec-9a186209",
    "status": "in_progress",
    "kind": "execute",
    "title": "apply_patch",
    "content": [{"type": "terminal", "terminalId": "exec-9a186209"}],
    "rawInput": {"command": "apply_patch", "cwd": "/tmp/codexprobe/ws"},
    "_meta": {"terminal_info": {"cwd": "/tmp/codexprobe/ws", "terminal_id": "exec-9a186209"}},
}

CODEX_PATCH_DONE: dict[str, Any] = {
    "sessionUpdate": "tool_call_update",
    "toolCallId": "exec-9a186209",
    "status": "completed",
    "rawOutput": {
        "formatted_output": (
            "*** Begin Patch\r\n*** Update File: calc.py\r\n@@\r\n def add(a, b):\r\n"
            "-    return a - b\r\n+    return a + b\r\n*** End Patch\r\n"
            "Success. Updated the following files:\r\nM calc.py\r\n"
        ),
        "exit_code": 0,
    },
    "_meta": {"terminal_exit": {"exit_code": 0, "signal": None, "terminal_id": "exec-9a186209"}},
}

CODEX_EXEC_OPEN: dict[str, Any] = {
    "sessionUpdate": "tool_call",
    "toolCallId": "exec-569fa7ee",
    "status": "in_progress",
    "kind": "execute",
    "title": 'python3 -c "import calc; print(calc.add(2,3))"',
    "rawInput": {"command": 'python3 -c "import calc; print(calc.add(2,3))"', "cwd": "/tmp/codexprobe/ws"},
}

CODEX_PLAN_FIRST: dict[str, Any] = {
    "sessionUpdate": "plan",
    "entries": [
        {"status": "in_progress", "content": "Show the contents of calc.py with a shell command", "priority": "medium"},
        {"status": "pending", "content": "Fix add() using the patch tool", "priority": "medium"},
    ],
}

CODEX_PLAN_SECOND: dict[str, Any] = {
    "sessionUpdate": "plan",
    "entries": [
        {"status": "completed", "content": "Show the contents of calc.py with a shell command", "priority": "medium"},
        {"status": "in_progress", "content": "Fix add() using the patch tool", "priority": "medium"},
    ],
}

# claude-agent-acp 0.66.0. Every one of its 132 captured updates that carried a
# `rawInput` also carried a `kind`, which is why the Task 1 guard cannot change
# how this adapter reads.
CLAUDE_EXEC_UPDATE: dict[str, Any] = {
    "sessionUpdate": "tool_call_update",
    "toolCallId": "toolu_01",
    "kind": "execute",
    "status": "completed",
    "rawInput": {"command": "pwd"},
    "rawOutput": "/root\n",
}
