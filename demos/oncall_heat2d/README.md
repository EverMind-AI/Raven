# oncall_heat2d — the on-call agent, end to end, on nothing but your laptop

Ten minutes, zero infrastructure. This demo shows the full dispatch chain that
makes raven's on-call capability reproducible without anyone's dev machine:

  1. You paste a task naming a solver case, a budget, and a shared machine.
  2. The host recognises it as **work to run and watch** and steers itself to
     spawn the on-call agent instead of hand-running trials.
  3. The on-call agent starts, reads the machine registry before anything
     else, and finds it **empty** -- so its first move is to hand you the list
     of what a machine needs, rather than to look for a way onto one.
  4. You register your laptop (`raven ops connection add --transport local`,
     which probes the machine and records what it found). A remote machine
     needs the address and key, which nothing but you can supply.
  5. The on-call agent runs a real campaign: multiple trials, a ledger, budget
     metering, mid-run readings, and a conclusion with the best configuration.

No CalculiX, no GROMACS, no remote box. The case is a 2D heat equation in
~70 lines of numpy.

## Why this case

The solver (`arena/heat2d.py`) integrates the heat equation on the unit square
with explicit forward Euler, from an initial condition whose exact solution is
known -- so the end-time L2 error is a hard number, not an impression. One run
takes 1-3 seconds, so a 25-minute budget buys a real parameter search.

It also carries the on-call lesson in miniature. Forward Euler is stable only
while `alpha*dt/dx^2 <= 0.25`; at the default `dt=5e-6` that means `nx <= 224`.
Past that the solution blows up to `nan` -- **and the script still reports
`"status": "succeeded"` with exit code 0** (`l2_error` becomes `null`; the
truth survives only in `l2_error_raw`). "Exit code 0 does not mean the result
is usable" is the sentence every on-call task turns on, and this case lets you
watch the agent earn it. The naive move -- "finer grid, smaller error" -- walks
straight into the trap; the correct move is to shrink `dt` along with `dx`, or
to recognise `nx ~ 224` as the ceiling for the default step.

## Prerequisites

- `uv sync` from the repo root -- this installs the NumPy the case needs, and
  the job script prefers that venv over whatever `python3` the machine has
  (set `HEAT2D_PYTHON` to override; with no NumPy anywhere it fails with rc 3
  rather than pretending)
- The on-call sub-agent installed: `bash subagents/install.sh` (builds
  `subagents/raven-oncall`'s venv), and its `.env` carrying `ONCALL_API_KEY`
  (see `subagents/raven-oncall/.env.example`)
- A provider key in your own raven config (the demo copies it into a
  demo-local home)

## Run it

```bash
demos/oncall_heat2d/run_demo.sh
```

The script launches the TUI with `RAVEN_HOME` pointed at a demo-local home:
your provider config is copied in, the machine registry is deliberately left
empty, and your real `~/.raven` is not touched. Then paste the task from
[`task.md`](task.md) and answer the one question it comes back with.

## What success looks like

The shape below is the chain this demo exercises; trial values vary run to
run, and the cold-start half has not been re-recorded live since the host
stopped gating the spawn on the registry.

```
Tool call: read_file(arena/run_heat.sh)          <- looks at the case
  ... tool result gains a line steering to the on-call specialist
Tool call: spawn(subagent="Raven-Oncall", ...)
  -> "Subagent [...] started"
  ... the on-call agent's first call is ops_connections, which answers
      "No machine is set up in this instance ... Hand this to the owner as
       it stands. Ask them to run `raven ops connection add`" and lists the
       seven things that command asks for
$ raven ops connection add --transport local        <- you, once
  -> probed the machine, wrote the registry row
  ... the on-call agent declares a campaign, submits trials, takes readings,
      and concludes with the best stable configuration and its L2 error
```

A good final answer names a configuration near the stability ceiling (e.g.
`nx=201, dt=5e-6` with L2 around `5e-6`, or a finer grid with a smaller `dt`)
and treats any `nan` trial as a failed reading, not a candidate.

## What's in this directory

```
demos/oncall_heat2d/
├── README.md            <- you are here
├── task.md              <- the statement to paste (EN + ZH) and how to answer
├── run_demo.sh          <- TUI in a demo-local home with an empty registry
├── arena/
│   ├── heat2d.py        <- the solver: heat equation, exact solution, result.json
│   └── run_heat.sh      <- the job body: reads config.json, runs one trial
└── .raven-demo-home/    <- created by run_demo.sh (not committed)
```
