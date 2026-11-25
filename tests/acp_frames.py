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

# A Bash call carrying the model's own description of it. `_meta.claudeCode`
# and `rawInput` hold the same string; the adapter keeps it out of ACP's
# `title`, which clients use as the shell-command preview.
CLAUDE_BASH_WITH_DESCRIPTION: dict[str, Any] = {
    "sessionUpdate": "tool_call",
    "toolCallId": "toolu_019KgriiAGn9yJvTzSP6doNu",
    "kind": "execute",
    "status": "pending",
    "title": "cd /repo/ui-tui && ls i18n",
    "rawInput": {"command": "cd /repo/ui-tui && ls i18n", "description": "Locate messages.json and i18n dirs"},
    "_meta": {"claudeCode": {"toolName": "Bash", "title": "Locate messages.json and i18n dirs"}},
}

# A completing frame that names the tool but repeats no `kind`. 146 of the 350
# captured tool frames look like this, which is why `names_call` may not gate on
# `kind` alone for this adapter.
CLAUDE_UPDATE_WITHOUT_KIND: dict[str, Any] = {
    "sessionUpdate": "tool_call_update",
    "toolCallId": "toolu_019KgriiAGn9yJvTzSP6doNu",
    "_meta": {
        "claudeCode": {
            "toolName": "Bash",
            "toolResponse": {"stdout": "messages.json\n", "stderr": "", "interrupted": False},
        }
    },
}

# claude-agent-acp 0.108.0, live probe 2026-11-24, sonnet low. `t` is seconds from the
# prompt send; method "answer" marks the prompt answer. A background command plus a
# Monitor: the answer comes at 35.71, the last idle at 110.67.
CLAUDE_SETTLE_BASH_MONITOR: list[tuple[float, str, dict[str, Any]]] = [
    (
        0.1,
        "_claude/sdkMessage",
        {"message": {"type": "system", "subtype": "session_state_changed", "state": "running"}},
    ),
    (
        20.84,
        "session/update",
        {
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "toolu_bdrk_01OyaNUzu7Rn81a11VoGGBMS",
                "status": "pending",
                "_meta": {"claudeCode": {"toolName": "Bash"}},
            }
        },
    ),
    (
        21.68,
        "session/update",
        {
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "toolu_bdrk_01ytd8bxxvU5pHnU61LlDtH6",
                "status": "pending",
                "_meta": {"claudeCode": {"toolName": "Monitor"}},
            }
        },
    ),
    (
        21.76,
        "_claude/sdkMessage",
        {
            "message": {
                "type": "system",
                "subtype": "background_tasks_changed",
                "tasks": [
                    {"task_id": "beces0koi", "task_type": "local_bash", "description": "Sleep 40s then print marker"}
                ],
            }
        },
    ),
    (
        21.76,
        "_claude/sdkMessage",
        {
            "message": {
                "type": "system",
                "subtype": "task_started",
                "task_id": "beces0koi",
                "description": "Sleep 40s then print marker",
                "task_type": "local_bash",
                "is_backgrounded": True,
                "tool_use_id": "toolu_bdrk_01OyaNUzu7Rn81a11VoGGBMS",
            }
        },
    ),
    (
        22.67,
        "_claude/sdkMessage",
        {
            "message": {
                "type": "system",
                "subtype": "background_tasks_changed",
                "tasks": [
                    {"task_id": "beces0koi", "task_type": "local_bash", "description": "Sleep 40s then print marker"},
                    {"task_id": "bfydif3t5", "task_type": "local_bash", "description": "new lines in watch.log"},
                ],
            }
        },
    ),
    (
        22.67,
        "_claude/sdkMessage",
        {
            "message": {
                "type": "system",
                "subtype": "task_started",
                "task_id": "bfydif3t5",
                "description": "new lines in watch.log",
                "task_type": "local_bash",
                "is_backgrounded": True,
                "tool_use_id": "toolu_bdrk_01ytd8bxxvU5pHnU61LlDtH6",
            }
        },
    ),
    (
        29.03,
        "session/update",
        {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "started"}}},
    ),
    (
        35.7,
        "_claude/sdkMessage",
        {
            "message": {
                "type": "result",
                "subtype": "success",
                "origin": {"kind": "human"},
                "usage": {
                    "input_tokens": 4,
                    "output_tokens": 249,
                    "cache_read_input_tokens": 81924,
                    "cache_creation_input_tokens": 82458,
                },
            }
        },
    ),
    (
        35.71,
        "answer",
        {
            "stopReason": "end_turn",
            "usage": {
                "inputTokens": 4,
                "outputTokens": 249,
                "cachedReadTokens": 81924,
                "cachedWriteTokens": 82458,
                "totalTokens": 164635,
            },
        },
    ),
    (35.71, "_claude/sdkMessage", {"message": {"type": "system", "subtype": "session_state_changed", "state": "idle"}}),
    (
        45.02,
        "_claude/sdkMessage",
        {"message": {"type": "system", "subtype": "session_state_changed", "state": "running"}},
    ),
    (
        56.2,
        "session/update",
        {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "event"}}},
    ),
    (
        56.79,
        "session/update",
        {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": ": PING_1"}}},
    ),
    (61.8, "_claude/sdkMessage", {"message": {"type": "system", "subtype": "task_updated", "task_id": "beces0koi"}}),
    (
        61.8,
        "_claude/sdkMessage",
        {
            "message": {
                "type": "system",
                "subtype": "task_notification",
                "task_id": "beces0koi",
                "status": "completed",
                "tool_use_id": "toolu_bdrk_01OyaNUzu7Rn81a11VoGGBMS",
            }
        },
    ),
    (
        61.8,
        "_claude/sdkMessage",
        {
            "message": {
                "type": "system",
                "subtype": "background_tasks_changed",
                "tasks": [{"task_id": "bfydif3t5", "task_type": "local_bash", "description": "new lines in watch.log"}],
            }
        },
    ),
    (
        62.5,
        "_claude/sdkMessage",
        {
            "message": {
                "type": "result",
                "subtype": "success",
                "origin": {"kind": "task-notification"},
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 8,
                    "cache_read_input_tokens": 82461,
                    "cache_creation_input_tokens": 1427,
                },
            }
        },
    ),
    (
        76.33,
        "session/update",
        {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "event: PING_2"}}},
    ),
    (
        81.21,
        "_claude/sdkMessage",
        {
            "message": {
                "type": "result",
                "subtype": "success",
                "origin": {"kind": "task-notification"},
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 8,
                    "cache_read_input_tokens": 82699,
                    "cache_creation_input_tokens": 2846,
                },
            }
        },
    ),
    (
        93.2,
        "session/update",
        {
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "toolu_bdrk_01vSw3dvWSP2A6TDKd3jpK5n",
                "status": "pending",
                "_meta": {"claudeCode": {"toolName": "Bash"}},
            }
        },
    ),
    (
        106.98,
        "session/update",
        {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "final: BG_DONE"}}},
    ),
    (
        110.67,
        "_claude/sdkMessage",
        {
            "message": {
                "type": "result",
                "subtype": "success",
                "origin": {"kind": "task-notification"},
                "usage": {
                    "input_tokens": 4,
                    "output_tokens": 169,
                    "cache_read_input_tokens": 172105,
                    "cache_creation_input_tokens": 4018,
                },
            }
        },
    ),
    (
        110.67,
        "_claude/sdkMessage",
        {"message": {"type": "system", "subtype": "session_state_changed", "state": "idle"}},
    ),
]

# Same probe: a background subagent. The answer comes at 115.91, idle at 116.11.
CLAUDE_SETTLE_SUBAGENT: list[tuple[float, str, dict[str, Any]]] = [
    (
        0.02,
        "_claude/sdkMessage",
        {"message": {"type": "system", "subtype": "session_state_changed", "state": "running"}},
    ),
    (
        20.59,
        "session/update",
        {
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "toolu_bdrk_01V8S28wS1kkREyMr6BHuQpq",
                "status": "pending",
                "_meta": {"claudeCode": {"toolName": "Agent"}},
            }
        },
    ),
    (
        30.05,
        "_claude/sdkMessage",
        {
            "message": {
                "type": "system",
                "subtype": "background_tasks_changed",
                "tasks": [
                    {"task_id": "a5d473181da590312", "task_type": "local_agent", "description": "Run sleep and echo"}
                ],
            }
        },
    ),
    (
        30.06,
        "_claude/sdkMessage",
        {
            "message": {
                "type": "system",
                "subtype": "task_started",
                "is_backgrounded": True,
                "task_type": "local_agent",
                "description": "Run sleep and echo",
                "tool_use_id": "toolu_bdrk_01V8S28wS1kkREyMr6BHuQpq",
                "task_id": "a5d473181da590312",
            }
        },
    ),
    (
        37.1,
        "session/update",
        {
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "toolu_bdrk_01XvMKRFcdgaMN5fCqnqZfLr",
                "status": "pending",
                "_meta": {"claudeCode": {"toolName": "Bash"}},
            }
        },
    ),
    (
        37.86,
        "session/update",
        {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "started"}}},
    ),
    (
        41.92,
        "_claude/sdkMessage",
        {
            "message": {
                "type": "result",
                "subtype": "success",
                "origin": {"kind": "human"},
                "usage": {
                    "input_tokens": 4,
                    "output_tokens": 173,
                    "cache_read_input_tokens": 34051,
                    "cache_creation_input_tokens": 34614,
                },
            }
        },
    ),
    (
        42.72,
        "session/update",
        {
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "toolu_bdrk_01WfYPK48cg3csjkUQ4yGRAS",
                "status": "pending",
                "_meta": {"claudeCode": {"toolName": "Bash"}},
            }
        },
    ),
    (
        42.8,
        "_claude/sdkMessage",
        {
            "message": {
                "type": "system",
                "subtype": "background_tasks_changed",
                "tasks": [
                    {"task_id": "a5d473181da590312", "task_type": "local_agent", "description": "Run sleep and echo"},
                    {
                        "task_id": "bq1rfnpzt",
                        "task_type": "local_bash",
                        "description": "Wait 30 seconds then print marker",
                    },
                ],
            }
        },
    ),
    (
        42.8,
        "_claude/sdkMessage",
        {
            "message": {
                "type": "system",
                "subtype": "task_started",
                "is_backgrounded": True,
                "task_type": "local_bash",
                "description": "Wait 30 seconds then print marker",
                "tool_use_id": "toolu_bdrk_01WfYPK48cg3csjkUQ4yGRAS",
                "task_id": "bq1rfnpzt",
            }
        },
    ),
    (
        48.77,
        "session/update",
        {
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "toolu_bdrk_01Tf7r6ugL4SzAboVzSpVYe4",
                "status": "pending",
                "_meta": {"claudeCode": {"toolName": "ToolSearch"}},
            }
        },
    ),
    (
        58.84,
        "session/update",
        {
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "toolu_bdrk_01VzZ5mEF1LfDbfHs34bAJ8L",
                "status": "pending",
                "_meta": {"claudeCode": {"toolName": "Bash"}},
            }
        },
    ),
    (
        67.5,
        "_claude/sdkMessage",
        {
            "message": {
                "type": "system",
                "subtype": "task_started",
                "is_backgrounded": False,
                "task_type": "local_bash",
                "description": "Wait for background output and print it",
                "tool_use_id": "toolu_bdrk_01VzZ5mEF1LfDbfHs34bAJ8L",
                "task_id": "b8kod0ygk",
            }
        },
    ),
    (72.8, "_claude/sdkMessage", {"message": {"type": "system", "subtype": "task_updated", "task_id": "bq1rfnpzt"}}),
    (
        72.8,
        "_claude/sdkMessage",
        {
            "message": {
                "type": "system",
                "subtype": "task_notification",
                "tool_use_id": "toolu_bdrk_01WfYPK48cg3csjkUQ4yGRAS",
                "status": "completed",
                "task_id": "bq1rfnpzt",
            }
        },
    ),
    (
        72.8,
        "_claude/sdkMessage",
        {
            "message": {
                "type": "system",
                "subtype": "background_tasks_changed",
                "tasks": [
                    {"task_id": "a5d473181da590312", "task_type": "local_agent", "description": "Run sleep and echo"}
                ],
            }
        },
    ),
    (
        73.55,
        "_claude/sdkMessage",
        {
            "message": {
                "type": "system",
                "subtype": "task_notification",
                "tool_use_id": "toolu_bdrk_01VzZ5mEF1LfDbfHs34bAJ8L",
                "status": "completed",
                "task_id": "b8kod0ygk",
            }
        },
    ),
    (
        79.54,
        "session/update",
        {
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": "toolu_bdrk_012eNW1JFbMkaG9fbQzxoupq",
                "status": "pending",
                "_meta": {"claudeCode": {"toolName": "SubagentHandback"}},
            }
        },
    ),
    (
        88.37,
        "_claude/sdkMessage",
        {"message": {"type": "system", "subtype": "task_updated", "task_id": "a5d473181da590312"}},
    ),
    (
        88.37,
        "_claude/sdkMessage",
        {
            "message": {
                "type": "system",
                "subtype": "task_notification",
                "tool_use_id": "toolu_bdrk_01V8S28wS1kkREyMr6BHuQpq",
                "status": "completed",
                "task_id": "a5d473181da590312",
            }
        },
    ),
    (88.37, "_claude/sdkMessage", {"message": {"type": "system", "subtype": "background_tasks_changed", "tasks": []}}),
    (
        100.01,
        "session/update",
        {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "final: SUB_DON"}}},
    ),
    (
        100.01,
        "session/update",
        {"update": {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "E"}}},
    ),
    (
        115.91,
        "_claude/sdkMessage",
        {
            "message": {
                "type": "result",
                "subtype": "success",
                "origin": {"kind": "peer"},
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 11,
                    "cache_read_input_tokens": 34617,
                    "cache_creation_input_tokens": 2969,
                },
            }
        },
    ),
    (
        115.91,
        "answer",
        {
            "stopReason": "end_turn",
            "usage": {
                "inputTokens": 4,
                "outputTokens": 173,
                "cachedReadTokens": 34051,
                "cachedWriteTokens": 34614,
                "totalTokens": 68842,
            },
        },
    ),
    (
        116.11,
        "_claude/sdkMessage",
        {
            "message": {
                "type": "result",
                "subtype": "success",
                "origin": {"kind": "task-notification"},
                "usage": {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                },
            }
        },
    ),
    (
        116.11,
        "_claude/sdkMessage",
        {"message": {"type": "system", "subtype": "session_state_changed", "state": "idle"}},
    ),
]
