---
name: multi-agent-coding
description: Use when the user needs a coding task done - code written, changed, fixed, tested or reviewed - through its follow-ups, and when a coding agent reports its quota or usage limit used up.
metadata: {"raven":{"emoji":"💻","always":true,"inject":"description","requires":{"tools":["spawn"]}}}
---

# Coding with sub-agents

## 1. A coding agent for the work

Coding work goes to a coding agent on the roster: the one the user named, otherwise one
the `## Delegation` section says owns coding, otherwise one whose description says it
writes or edits code. When a Worker Table is bound, `subagent` takes worker labels
instead: pick the worker whose brief covers the coding work. When none does, tell the user
and ask how to go on rather than writing the code yourself or sending it to an agent
outside the enum.

## 2. One instance per task

An instance is one conversation with one agent, named by the `instance` handle.

- A new task gets a new instance, never an earlier task's handle, which would carry that
  task's context into it. That holds for a task built on a finished one, such as docs for
  a finished feature: it starts fresh, with the earlier task's output file (section 3).
- A follow-up on an unfinished task - a correction, a bug in what it did, "keep going" -
  goes to that task's instance: same `subagent`, same `instance`, a new `node_id`. The
  agent keeps its context, so the prompt carries the new request in the user's words.
- Send a follow-up only once the instance's running dispatch has returned: sent sooner, it
  can start a separate conversation without that context.

## 3. Results by file

When a task needs what another produced, put that task's output file in its prompt -
`{{ <node_id>.output_path }}` of its last dispatch - instead of retelling the result. The
file is the agent's report, not the code, so also say where the code is (section 4).

## 4. A worktree per task in a shared project

Local sub-agents share this session's working directory, where nothing locks a file, and
giving each agent its own files there does not isolate them: tests run against the other's
half-made edits, and one agent's `git stash` or `checkout` wipes the other's work. So when
a coding task will run beside another in the same project - dispatched together, or while
the other is running or unfinished - its prompt tells the agent to make its own worktree,
on a branch you name for the task (`fix/login-session-expiry`), so the work can be found
even if the agent stops before reporting:

> Create a git worktree of your own for this task on a new branch `<branch>`, outside the
> project or in a directory git ignores. Work only there, and end your report with its
> path and branch. If the project is not a git repository, do not make it one: work in
> place on only this task's files, and say so.

- Every later prompt for the task - a follow-up, a takeover (section 5) - names its
  worktree: the branch, and the path from the report. A task built on its code, such as
  docs for it, works there too once the task has finished.
- A worktree starts from the last commit, without the checkout's uncommitted changes.
  When the request depends on them, ask the user before dispatching.
- When the work is done, tell the user which branch holds it. Merging the branch or
  removing the worktree is work for the task's instance, once the user asks.

## 5. Out of quota: another coding agent

When a coding agent's result says its usage limit, quota, credits or balance is used up -
often with a 402 or 429 status or a reset time, in whatever words or language - send the
task to another coding agent right away.

- Pick one that does not draw on the same account: agents that run through one platform
  share its quota, and a spent platform credit fails them all.
- When a Worker Table is bound, the takeover goes to another worker whose brief covers it
  and that runs on a different agent, by the same account rule.
- Use a new `node_id` and a new `instance`. The old instance's context does not carry
  over, so the prompt carries what the task needs: the user's requests on it, the worktree's
  branch and any path a report gave (the worktree may hold the first agent's unfinished
  changes), and the output files of the task's finished dispatches. From then on, the new
  instance is the task's instance.
- Tell the user which agent ran out (and when it resets, if it said) and which took over.
  Do not send the task back to the one that ran out; when no coding agent is left, ask the
  user how to go on.
- In a graph, answer the node's report with `replan`, putting its work on another coding
  agent under a new id. `continue` would send it back to the agent that ran out, and the
  user need not be asked.

Raven's own limits refuse every agent alike, so follow the refusal instead of switching
agents: a `spawn` or graph refused because this session hit its spawn rate limit or its
dispatch rate limit, because delegation is paused, or because the host is shutting down.
