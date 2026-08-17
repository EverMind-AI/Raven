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

## Running experiments / jobs on a remote machine — use ops_submit

When a task requires running experiments, tuning, sweeping, or optimizing on a
remote host, dev machine, or simulation/compute platform (e.g. "tune BM25 on the
dev machine", HPC/GPU jobs, benchmark runs), drive it through `ops_submit`:
submit one round of config(s), let it wake you when results are due, read the
ledger with `ops_tune_status`, and choose the next round from the results.

**Start by calling `ops_tune_status` with no arguments.** A campaign is usually
already set up for you, and it holds what the request leaves out: the host and
port, the starting config, the compute budget, the starting value to beat, and
the operating policy for this kind of run. Reading it first is how you find all
of that. If the reply says there is no campaign, then say so — do not go looking
for the machine yourself.

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
Those bypass the durable, round-by-round Ops loop, and the connection details
you would be guessing at are already in the campaign. A skill or reference is
fine only as background for CHOOSING configs — never for executing the
experiment. The job runs on the remote host through `ops_submit`.
