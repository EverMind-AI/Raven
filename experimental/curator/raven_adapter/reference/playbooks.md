# Playbooks

A playbook is a reusable procedure the main agent owns: it names the sub-agents a task needs, what each is asked to do and in which order. Raven loads playbooks at startup from the agent home's `playbooks/` folder and offers them to the main agent in its tool table; the model decides, like any other tool call, whether a request warrants one.

## File

One directory per playbook: `playbooks/<name>/playbook.md`, where `<name>` matches `^[a-z0-9][a-z0-9-]*$`. The file has three parts:

1. frontmatter with `name` and `description` (at most 200 characters; the model reads it to decide when to use the playbook);
2. a human-readable body;
3. exactly one fenced block tagged `yaml playbook-spec` holding the specification, with camelCase keys.

A specification that fails validation is refused at load with a warning and never offered; the host checks after binding that every authored playbook was loaded.

## Specification

`PlaybookSpec` fields: `name`, `description`, `taskSummary`, `version`, `mode` (`dag` or `prompt`), `confirm` (default true), `triggers.keywords`, `params` (each with `type`, `required`, `default`, `enum`, `description`), `mcpServers`, and either `nodes` (dag mode) or `prompts` (prompt mode).

A node (`DagNodeSpec`): `id`, `subagent` (a registered sub-agent name such as `Raven`, `Raven-Design`, `Raven-Research`), `nodeSummary`, `promptTemplate` (may reference `{{ params.X }}` and `{{ <node id>.output }}`), `dependsOn`, `inputs`, `instance` (a named stateful instance shared by nodes), `skills` and `mcps` (null keeps the agent's default, an empty list gives none, a list narrows to those names).

## Running

- The main agent calls `load_playbook` with the playbook `name`, its `params`, and optional `fills` for fields left blank. Missing required params return a gap list and nothing runs; the model asks the user and calls again.
- dag mode runs the nodes as a sub-agent graph in the background, like `spawn`: the tool returns a receipt at once and the graph's result returns to the conversation later as a new turn. The worker waits for it before the exchange ends.
- prompt mode returns assembly guidance; the model then composes and runs the graph itself with `run_subagent_dag`.
- `confirm: true` asks the user to approve the graph where an ask channel exists; without one the graph runs unconfirmed.
- Node progress is recorded as `dag.progress` rows (`dag_run_started`, `dag_node_updated` with a status, `dag_run_completed`).

## What a node can be configured with

- Built-in `Raven` nodes run in-process unless Raven is a managed child: their `promptTemplate` and their `skills`, chosen by name from the agent home's `skills/` folder, both apply. A managed `Raven` child is rebound to its own ACP host and is configured like the external agents below.
- External agents such as `Raven-Design` (which routes .pptx work to `Raven-PPT`) run in their own process: they take the node's `promptTemplate` only; injected skills are ignored with a notice. Anything they must follow, such as a template file path or a house style, has to be in the prompt, or in an upstream node's output that the prompt references.
- The main agent's own Harness (strategies, participants, prompts, skills) governs when and with which params the playbook is loaded; the playbook governs what happens inside the run.
