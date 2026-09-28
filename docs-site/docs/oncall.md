# Manage long-running work with Oncall

Model training, solver runs, and parameter sweeps often need repeated status
checks after submission, decisions about whether to continue or adjust the
work, and a final report backed by measured results.

This guide shows how to use Raven-Oncall to manage that workflow within a
declared budget and allowed actions: prepare a machine, delegate the task,
follow its progress, stop when needed, and verify the outcome. Start with
the bounded local-job example below before entrusting a longer run.

**The initial delegation returns after the first turn, not when the job
finishes.** A DAG node returning from Oncall is therefore not proof that a
remote artifact or final report is ready.

## When to use Oncall

When a job needs multiple rounds of observation or adjustment, Raven-Oncall
can execute it, inspect outputs, decide the next step within the declared
budget and allowed actions, and report the measured outcome.

A **campaign** records that goal, target machine, execution setup, baseline,
budget, trials, and follow-up state. The agent's scheduler can arrange another
look; a scheduled look is not the job itself.

## Prepare the agent and machine

Check Raven-Oncall through [Agent Integrations](agent-integrations.md), including
its model credentials. Keep the host and agent runtime/scheduler available
for follow-up work. Closing a browser is different from stopping the resident
runtime.

The host manages a machine registry:

```bash
raven ops connection list
raven ops connection add
raven ops connection doctor
```

`add` is interactive and contacts the selected local/SSH machine by default.
It records its identity, access parameters, installed software, working paths,
budget unit, and concurrency. A non-interactive local example, after replacing
the executable and project paths:

```bash
raven ops connection add --id local-lab --name "Local lab" \
  --transport local --software "Python at /absolute/path/to/python" \
  --budget-unit minute --concurrency 1 --path /absolute/path/to/project \
  --non-interactive
```

The registry is the path selected by `RAVEN_CONNECTIONS`. Otherwise a
sub-agent reads the one in the raven home (`RAVEN_HOME`, or `~/.raven`) that the
host hands it, and any other instance -- the host itself, including one started
with `--config` -- reads the `connections.json` beside its config when there is
one, else the home. A first registration lands in the home. Sub-agents run on a
rendered config in a state directory of their own and inherit the home, so
they read the owner's registry rather than a copy of it. SSH registration can
also write a managed alias in `~/.ssh/config`; the key path is recorded, not
the private key bytes. Do not put keys or passwords in a task message.

An agent can register a machine too. The `ops_connection_add` tool (on in
raven-oncall and raven-code through `tools.connectionAdd`; off elsewhere) takes
what the owner said in conversation -- this computer or another, what they
call it, an address if another -- fills a port, user or key they left out from
their own ssh config, reaches the machine, and only then writes the row. When
the registry lists no machine that fits, the agent asks the owner rather than
looking for a way in with a raw ssh command.

`doctor` validates registry usability; it is not a fresh execution test of
every remote environment. `--skip-probe` on `add` skips contacting the machine
and explicitly leaves connectivity unverified. The Oncall agent, not the
host's DAG registry, decides how to run a campaign on that machine.

## Give a bounded work order

Start with a disposable local job and provide the following information:

| Field | Example |
| --- | --- |
| Machine and directory | Registered “Local lab”; a scratch project directory |
| Goal | Run the supplied benchmark and report whether the target was reached |
| Command and environment | Exact existing script, interpreter, and required inputs |
| Baseline and metric | Previous measured value, units, and whether higher/lower is better |
| Resource budget | Total machine minutes and allowed concurrency |
| Allowed actions | Read outputs and retry only the listed parameter changes |
| Stop/escalation conditions | Stop on budget exhaustion; ask before changing dependencies |
| Delivery | Return evidence, output locations, and remaining uncertainty |

Example request:

> Delegate this to Raven-Oncall on the registered Local lab machine. Use the
> scratch project and existing smoke script I attached. Inspect and declare
> the setup before submitting. Allow at most ten machine minutes and one job
> at a time. Do not install packages or alter the baseline. Watch the result,
> stop if the budget is exhausted, and report the measured outcome and logs.

These are user requirements to translate into a campaign, not literal CLI
flags. Inspect the declared setup: a budget mentioned only in prose is not
evidence that a configured meter will enforce it.

## What the agent does

```text
Declare goal/setup -> submit or observe -> inspect status and outputs
    -> schedule another look / adjust allowed parameters / ask the owner
    -> stop or finish -> deliver the evidence
```

Relevant **model tools inside Raven-Oncall**, not host shell subcommands:

| Tool | Role |
| --- | --- |
| `ops_declare` | Record the campaign's target, setup, baseline, and budget |
| `ops_submit` | Submit a trial under the declared setup and checks |
| `ops_tune_status` / `ops_outputs` | Inspect campaign state and output evidence |
| `ops_check_later` | Schedule the next observation |
| `ops_campaigns` / `ops_note` | Find campaigns and record decisions |
| `ops_ask_owner` | Evaluate a request for human intervention |
| `ops_kill` / `ops_finish` | Stop work or close the campaign with a report |

The next-look key is the campaign: a new scheduled look replaces the pending
one rather than adding unlimited polling jobs. If scheduling is unavailable
or has no usable route, the tool reports that manual checking is needed.
Do not interpret that notice as a successful unattended watch.

## Budgets and approval boundaries

Campaigns can meter compute, wall-clock watch time, or observation count.
Units and concurrency matter: ten GPU-minutes is not necessarily ten elapsed
minutes. A campaign with no declared budget has no total-budget stop.
These meters do not cap model-provider bills or every external cloud charge.

Oncall's declaration, execution checks, and escalation policies are distinct
from host [tool permissions](permissions.md). In particular, an allowed
`ops_ask_owner` returns text for the model to deliver using messaging; it does
not prove the owner received it. General messaging remains a separate tool.

The shipped Oncall config does not enable a sandbox by default and does not
confine all execution to the project directory. Use scoped machine accounts,
scratch paths, and an isolated environment before enabling unattended writes.
Host approval does not imply a separate human review of every child command.

## Follow up, stop, and verify

Use [instance collaboration](agent-collaboration.md) to ask the same agent
for campaign status, the pending observation, and the latest output evidence.
When requesting a stop, name the campaign and ask it to check running jobs,
cancel further observation, and report what actually stopped.

Closing a UI, canceling a host turn, or forgetting an instance is not a remote
job kill. After a runtime interruption, inspect the saved ledger, remote
processes, and scheduling route before resubmitting. Persisted campaign files
do not guarantee every transport automatically resumes its watch.

Accept a final report only when it names the goal, baseline where applicable,
actual measurements, termination reason, and retrievable output locations.
A “done” message without evidence is not a benchmark result.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Agent cannot select a machine | Registry id/name, access fields, and software paths |
| Submission refused | Declared setup, resource availability, budget, and immutable baseline checks |
| First reply arrives but the job continues | Expected first-turn behavior; inspect the campaign |
| No later update | Resident runtime, scheduler, wake route, and delivery channel |
| Budget seems different from elapsed time | Meter, unit, concurrency, and recorded consumption |
| Restart left uncertain state | Read ledger and remote status before retrying or killing anything |

Implementation evidence lives in `agents/raven-oncall/plugins/oncall-flow/`,
`raven/ops/connections.py`, `raven/ops/connection_add.py`,
`raven/agent/tools/connection_add.py`, and `raven/cli/ops_connection_commands.py`.
