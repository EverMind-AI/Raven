# Tool Usage Notes

Tool signatures are provided automatically via function calling.
This file documents non-obvious constraints and usage patterns.

## exec — Safety Limits

- Commands have a configurable timeout (default 60s)
- Dangerous commands are blocked (rm -rf, format, dd, shutdown, etc.)
- Output is truncated at 10,000 characters
- `restrictToWorkspace` config can limit file access to the workspace

## cron — Scheduled Reminders

- Please refer to cron skill for usage.

## Running experiments / jobs on a remote machine — ops_declare then ops_submit

When a task requires running experiments, tuning, sweeping, or optimizing on a
remote host, dev machine, or simulation/compute platform (e.g. "tune BM25 on the
dev machine", HPC/GPU jobs, benchmark runs), drive it through `ops_submit`:
submit one round of config(s), let it wake you when results are due, read the
ledger with `ops_tune_status`, and choose the next round from the results.

**Start by calling `ops_campaigns`, or `ops_tune_status` with no arguments.** If a
campaign is already set up for this task it holds what the request leaves out:
which machine the work runs on, the starting config, the compute budget, the
starting value to beat, and the operating policy for this kind of run. Reading it
first is how you find all of that. If none of the campaigns listed is the task
you were given, say so with `ops_ask_owner` — naming one binds this window to it,
so taking the nearest is worse than asking.

**Often there is no campaign yet, and declaring one is the normal path.** The owner
writes their code and their case on the machine, then hands you the path and what
they want to know. `ops_declare` records the experiment — machine, case, the one
command that runs it once, the target, the starting point, the budget — and runs
nothing, so a mistake there costs no compute. `ops_submit` then submits round after
round against it. Work the declaration out rather than ask for it: the machine from
what the task needs, the command and the starting values from the case's own entry
script, the target from what the owner asked to know. A parameter the script gives
no default to is usually the one the experiment is searching over. Only the budget
has no answer in the case — if they did not say, do not invent one; report what a
round costs once you know.

**`ops_connections` lists the machines this instance can run on** — the owner's
own name for each, what it is, and what it has installed. You never need a host,
a port, a user or a key: those belong to the connection and the tools use them
for you. A task statement that names no machine is normal.

**`ops_exec` runs a command on one of those machines.** Use it to look at anything
remote: list a directory, read or grep a file, tail a log, check a size or a hash.
It takes a `campaign` when one exists, or a `connection` id when none does yet —
looking at a machine is how you learn what a campaign should run, so it does not
wait on one. The plain `exec` tool runs on THIS computer instead, which is why a
remote path looks missing there. Looking before you spend compute is cheap and
expected — reading the case, the solver script and the inputs before the first
submit costs seconds, and has repeatedly been what separated a useful first round
from a wasted one.

**A campaign has to be ended, and `ops_finish` is how.** It files the result and
closes the campaign in one call. Until it is called, the campaign is open, and
the wake timer keeps waking you to a round of work that is already done —
measured across two runs: both reported everything the reader needed, in prose,
and both went on being woken until the re-arm cap stopped them. Saying you are
finished is not finishing. Use `outcome='failed'` when it is not going to
succeed; if there is nothing to report a number for, say why in
`no_data_reason`. If you need the OWNER to choose rather than to be told, that
is `ops_ask_owner`, and it ends nothing.

Do NOT reproduce the remote computation locally: do not use `exec` to run the
experiment yourself, do not ssh to the host to hunt for paths or ports, and do
not pull a domain skill (`use_skill`) to compute the result in this process.
Those bypass the durable, round-by-round Ops loop, and there is nothing to guess
at: `ops_exec` already reaches that machine for looking, and `ops_submit` is what
starts work there. A command that would outlive its call is refused by `ops_exec`
for exactly that reason. A skill or reference is
fine only as background for CHOOSING configs — never for executing the
experiment. The job runs on the remote host through `ops_submit`.
