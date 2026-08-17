"""Task-completion judge prompt.

Asks the judge to classify a turn as ``completed`` / ``failed`` /
``unknown`` based on the user's original goal and the agent's final
response. Deliberately short so the haiku-class judge model can return
a single-word answer quickly and cheaply.
"""

TASK_COMPLETION_PROMPT = """You are evaluating whether an AI assistant completed the user's task.

User asked:
\"\"\"
{user_goal}
\"\"\"

Assistant's final response:
\"\"\"
{final_response}
\"\"\"

Answer with ONE word on a single line:
- "completed" — the assistant addressed the user's request and the turn ended cleanly.
- "failed"    — the assistant explicitly errored, refused, or missed the objective.
- "unknown"   — the turn is ambiguous (mid-conversation, clarification asked, etc).

Your one-word verdict:"""


__all__ = ["TASK_COMPLETION_PROMPT"]
