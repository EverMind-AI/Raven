# Playbooks

A Playbook stores a reusable orchestration for a family of tasks. Use one when
the workflow should be reviewed, shared, and run again with different inputs.
It stores either a graph or instructions for assembling a graph; execution
still uses Raven's ordinary DAG machinery.

## Choose the right capability

| Capability | Use it for | What it does not provide |
| --- | --- | --- |
| Skill | Reusable instructions, scripts, and references | A stored multi-agent execution graph |
| `run_subagent_dag` | A graph composed for the current task | A named library entry for reuse |
| Playbook | A repeatable review, research, or other multi-step workflow | A scheduler or durable workflow service |
| Evolver | Benchmark-driven experiments on harness changes | Execution of a user's routine workflow |

For example, a release-review Playbook can inspect compatibility and tests in
parallel, then synthesize their outputs. An incident-review Playbook can
collect evidence before drafting a report. Start with read-only workflows;
sending messages or changing infrastructure adds separate permission concerns.

See [DAG orchestration](orchestration.md) for scheduling and failure handling,
and [Skills, memory, and extensions](skills-and-extensions.md) for the wider map.

## Inspect and create

```bash
raven playbook list
raven playbook create brief-review \
  --input "Review a supplied brief for gaps, then summarize actionable feedback. Do not edit files."
raven playbook get brief-review
raven playbook validate brief-review
```

`create` uses a configured model; `--from FILE` can supply workflow notes
instead of, or alongside, `--input`. Inspect the generated parameters and
steps before running: generation does not promise the exact example below.
`get` prints the file to stdout and its source path to stderr.

**Newly created Playbooks are usable immediately**, despite older CLI help
describing them as disabled on creation. Use `raven playbook disable brief-review`
to withhold one during review. For review before discovery, author the file
outside the library, validate it by path, and install it only when ready.

The library has a packaged, read-only layer at `raven/playbook/builtin/` and a
writable user layer. The packaged layer may contain no entries. The default
user root is `config.workspace_path / "playbooks"` (the configured Agent home);
`playbooks.dir` overrides it. A user file with a built-in's name shadows the
built-in. Library changes and the disabled list are read live.

## A complete DAG Playbook

Save the following as `brief-review/playbook.md` under your chosen authoring
directory. If you already generated that name, review and edit its file instead
of creating a second definition.

````markdown
---
name: brief-review
description: Review a brief for gaps and summarize actionable feedback
---

# Brief review

Inspect the supplied brief, then produce a prioritized checklist.

```yaml playbook-spec
version: 1
taskSummary: Review a brief
mode: dag
confirm: true
triggers:
  keywords: [brief review, review brief]
params:
  brief:
    type: string
    required: true
    description: The brief to inspect
nodes:
  - id: inspect
    subagent: raven
    nodeSummary: Identify gaps
    skills: []
    mcps: []
    promptTemplate: "Find gaps in this brief: {{ params.brief }}. Do not edit files or contact external services."
  - id: summarize
    subagent: raven
    nodeSummary: Prioritize feedback
    dependsOn: [inspect]
    skills: []
    mcps: []
    promptTemplate: "Turn these findings into a prioritized checklist: {{ inspect.output }}. Do not edit files or contact external services."
```
````

`raven` is the generic built-in agent. To use Raven-Code or another specialized
agent, substitute a registered name and verify its setup through
[Agent Integrations](agent-integrations.md). A stored Playbook uses the
underlying agent registry, not temporary worker labels advertised for an
ad-hoc graph.

```bash
raven playbook validate ./brief-review/playbook.md
```

After review, place the directory under the user library root. Then:

```bash
raven playbook run brief-review 'brief=Launch a read-only documentation preview for internal reviewers.'
```

The example's instructions and empty Skill/MCP selections are not a read-only
sandbox. Ordinary tools still follow the agent's permissions.

The file has three regions: a two-field frontmatter (`name` and `description`),
human-readable prose, and exactly one `yaml playbook-spec` block. The name
matches the directory and uses lowercase letters, digits, and hyphens.
`taskSummary` is the dispatched task title, distinct from the description
used for discovery. Unknown contract fields are errors.

## DAG mode and prompt mode

`mode: dag` requires non-empty `nodes` and no `prompts`. The saved graph is
filled and dispatched through `SubAgentDagTool`. Independent nodes can run in
parallel; references to another node require a dependency.

`mode: prompt` requires `prompts` and no graph. For example, replace the
example's `mode` and `nodes` fields with this fragment:

```yaml
mode: prompt
prompts: |
  Build a read-only review graph for this brief: {{ params.brief }}.
  Use registered agents to inspect clarity and feasibility independently.
  Add a final synthesis node depending on both inspections.
  Do not edit files or contact external services.
```

In a conversation, `load_playbook` returns this filled guidance; the caller
composes and submits `run_subagent_dag`. The CLI instead uses its configured
model to compose the graph, with a limited repair attempt. Both paths use the
ordinary DAG contract. Prompt mode is assembly guidance, not a runtime
branching language; inspect the submitted graph's options, including approval.

## Parameters and blank fields

Declare inputs in `params`. Supported declarations are `string`, `integer`,
`number`, `boolean`, `enum`, `path`, and `secret`. Ordinary parameter references
belong directly in `promptTemplate` or `prompts`, not in node `inputs`.
Both `{{ params.brief }}` and the dollar-brace parameter form are supported.

The current resolver substitutes text and detects missing required values; it
does not enforce runtime numeric, enum-membership, or path-access constraints
just because a type was declared. Validate sensitive inputs in the consuming
tool and apply filesystem/tool policy separately.

`fills` completes fields the author left blank. Required blanks are
`subagent`, `nodeSummary`, and `promptTemplate`; optional fillable fields are
`skills`, `mcps`, and `instance`. Already specified values cannot be changed.
An explicit empty list is a fixed choice, not a blank.

For a variant that deliberately leaves `inspect.promptTemplate` empty:

```bash
raven playbook run brief-review 'brief=Review the launch plan.' \
  --fill 'inspect.promptTemplate=List unclear assumptions in the launch plan; do not edit files.'
```

This command is rejected against the complete example above, which already
specifies that prompt. CLI `validate` checks completeness and reports such
required blanks even though the runtime can load the template and ask for
fills. CLI `--fill` supplies string fields; use structured tool arguments for
list-valued Skill/MCP fills.

## Agents, skills, and carried MCP servers

Node fields use the [DAG contract](orchestration.md#node-contract-and-data-flow).
Omitting `skills` or `mcps` preserves that agent's/default selection; `[]`
requests none. Non-empty lists request a specific selection, subject to backend
support. A shared `instance` needs a stateful agent and dependency-ordered
nodes targeting the same agent. Do not expect isolated working copies from
parallelism alone.

A DAG Playbook can carry `mcpServers` definitions, using the host MCP config
schema. Its definition takes precedence over a same-named host server for
that run. The following is a fragment to add to a DAG Playbook; replace the
example endpoint and add `mcps: [docs-api]` to the consuming node:

```yaml
params:
  API_TOKEN:
    type: secret
    description: Credential for the documentation service
mcpServers:
  docs-api:
    url: https://mcp.example.invalid/mcp
    headers:
      Authorization: "Bearer {{ params.API_TOKEN }}"
```

Merge the fragment with existing parameters; it is not a complete Playbook.
Review carried commands, URLs, and requested tools as executable configuration.
The loader drops unusable carried definitions with a warning. Prompt-mode
carried definitions are also dropped: the later caller-composed graph cannot
receive them. Use host-configured MCPs in prompt mode instead.

Missing servers, credentials, or unsupported injection can degrade the run
without blocking the whole workflow. Read capability notices and verify the
tools actually reached the agent before trusting an answer that needs them.

## Store credentials locally

For the fragment above, store a secret without placing it in shell history:

```bash
raven playbook secret set brief-review API_TOKEN
```

Omitting `--value` prompts without echo. The file is stored with mode `0600`
under `<credentials>/playbooks/brief-review/params.json`; this is a local file,
not an encrypted vault. The parameter must be declared as `type: secret` and
cannot have a default. Only carried MCP `env`/`headers` references receive its
value. References in prompts are withheld and logged.

Do not supply secrets through chat, ordinary parameters, or CLI `K=V`.
This protects the substitution path, not every possible response from an
external tool; a server receiving a credential is still trusted with it.

For a carried server declared with `auth: oauth`:

```bash
raven playbook auth brief-review docs-api
```

This requires an OAuth server definition, not the bearer-header example above.
OAuth tokens live under `<credentials>/playbooks/brief-review/mcp/`, separate
from host-server credentials. Use `raven playbook secret clear brief-review API_TOKEN`
to remove the stored parameter.

## Discovery, approval, and execution

`triggers.keywords` influences which descriptions are shown when the library
is large. Keywords never automatically run a Playbook. The model explicitly
calls `load_playbook`, and enabled names remain reachable even when retrieval
does not select their full description.

CLI `run` waits synchronously; conversational DAG loads normally dispatch in
the background. Missing ordinary parameters or required blanks prevent
dispatch and return named gaps. A prompt-mode load returns guidance instead
of dispatching by itself.

`confirm` defaults to true on a Playbook and is passed to the graph when the
executor dispatches it. **Without an ask channel, the current DAG implementation
logs a notice and runs unconfirmed; the explicit CLI path does not wire one.**
With an ask channel, refusal or delivery failure stops dispatch. This is not
an unattended security gate. See [Permissions and security](permissions.md).

```bash
raven playbook disable brief-review
raven playbook enable brief-review
```

Disable removes the entry from the model-facing library. Explicit CLI `run`
can still execute it. To delete a user entry, `raven playbook delete brief-review`
asks for confirmation and removes its directory. There is no Playbook undo.
Deleting an override reveals the built-in of that name and clears its disabled
state; built-ins themselves can only be disabled.

## Troubleshooting and operating safely

| Symptom | Check |
| --- | --- |
| Entry missing from the model's tools | Disabled list, parse errors, registered agents, and whether this agent has Playbook tools |
| Validation says an agent is unregistered | Use the current underlying roster; registration is separate from installation/readiness |
| Run reports missing values | Supply ordinary `params` and required blanks; store secrets locally |
| Fill is refused | The field is fixed, the node id is wrong, or the field is not fillable |
| MCP tools absent | Host/carried definition, credentials, connection state, and backend injection support |
| A run returned but work is incomplete | Inspect node results and verdicts; a receipt is not proof of task success |
| Host restarted during a graph | Inspect recorded outputs and side effects before submitting remaining work as a fresh graph |

Validate and review the file before installation, test with scoped credentials,
and inspect a small read-only run before allowing mutations. Keep workflow
definitions under version control, not credentials or transcripts. DAG history
recovery does not automatically resume execution, and a repeat run can repeat
external side effects.
