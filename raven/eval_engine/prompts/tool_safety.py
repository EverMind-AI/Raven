"""Tool-safety audit prompt.

Stub scaffold for an LLM-driven tool audit. The default tool-audit
behavior is a deterministic deny-list check inside
``ToolAuditHook.before_execute_tools``; a future expansion can call
this prompt when the deny-list is inconclusive.
"""

TOOL_SAFETY_PROMPT = """You are auditing whether an AI assistant should be allowed to invoke a tool.

Tool: {tool_name}
Arguments:
{tool_arguments}

Context (last user message): {user_goal}

Answer with ONE word on a single line:
- "allow"   — the call is consistent with the user's intent and not destructive.
- "deny"    — the call is unsafe, off-topic, or violates a policy.
- "unknown" — insufficient context.

Your one-word verdict:"""


__all__ = ["TOOL_SAFETY_PROMPT"]
