# Code planning mechanism

The current runtime facts determine whether this mechanism is loaded. A profile name alone is insufficient.

## Ownership and interaction

The code-flow hook binds TodoStore to the actual session. The todo tool reads a detached list or writes a complete replacement; it persists before returning a receipt. Items marked completed are the agent's reports. They are not proof of task verification.

The participant restores the saved revision into the transcript only when that revision is no longer visible. It does not create a second task plan. Tool calls and restored model input refer to the same store.

## Modification boundary

A Curator PlanningStrategy has its own binding and checkpoint unless the host explicitly supplies a shared dependency. Adding it does not disable todo or CodeParticipant. Inspect the current code-flow configuration and source before selecting an existing plugin switch: a plugin-wide disable affects more than planning. Do not import global stores as an undeclared dependency or overwrite session files as a substitute for supported operations.

## Evidence

Use the registered tool.todo and actual code-flow hook sources, and search their supporting package in the supplied source snapshot. Inspect execution observations separately to establish whether the tool or restore path ran.
