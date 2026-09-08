# Permission face: the `machine` parameter on trunk exec

Date: 2026-09-03
Status: for sign-off by the v0.2.0 lead (migration acceptance discipline, gate 1)
Companion plan: docs/plans/2026-09-03-exec-machine-and-background.md

## What changes

The trunk `exec` tool gains an optional `machine` parameter: a connection id
from the owner's machine registry (`raven/ops/connections.py`). A call naming
one runs its command ON that machine over the row's transport (ssh, or local),
with the address, port, key path and account resolved below the model. The
parameter is only advertised while the registry has at least one row.

With it, the oncall plugin's `ops_exec` retires: the machine face was that
tool only because a plugin contributes new tools rather than patching trunk
exec's schema, and its docstring carried the open ruling ("the same-name
shadow is an open ruling"). This lands the face where the fork originally had
it and closes the ruling. The product guide (TOOLS_ONCALL.md) reverts to byte
parity with the vendored twin's -- the respelling exception list is gone.

## Who gains what

Every agent served by trunk raven -- the host main loop included -- can now
run a capped command on a registered machine by naming its id.

This channels an existing power; it does not widen the face. The host main
loop already reaches any machine by typing `ssh -p <port> root@<ip>` into
plain exec: measured 2026-09-03 on a live field run, every remote look went
out that way, with the raw address carried by the task statement because
nothing else could address the machine at all. What the parameter changes is
where the address lives (the registry, not the prompt), what happens at the
cap (the kill reaches both ends via a remote `timeout` wrapper; a raw ssh
kill orphaned a training process at 100% GPU, measured 2026-09-02), and what
the channel refuses (detaching commands, pointed at the on-call job runner).

The boundary stays deployment-side, as the migration acceptance documented
for the plugin backends: registry membership is the authorization; a machine
not in `connections.json` cannot be named.

## What the channel enforces

- per-command cap of 60 seconds, applied on the remote end where the
  `timeout` binary exists (local transports are capped by the runner itself:
  macOS ships no GNU timeout);
- refusal of `nohup` / `tmux` / `screen` / trailing `&` and the other
  detaching shapes, with the refusal naming ops_submit -- on a registered
  machine, work that outlives the call belongs to the governed job runner
  (budget, dedup, occupancy, result synthesis);
- an unknown id answers with the registry listing, so the next call can be
  right without a second round trip.

## Deliberate scope cuts against the fork's channel

- Connection ids only: the fork's `machine` also accepted a campaign name and
  resolved it through the campaign's meta. Campaigns are the oncall product's
  store; trunk cannot and should not read it. A campaign's meta names its
  connection, so the id is always one read away.
- No wait-clipping: the fork clipped a leading `sleep N` and pointed at
  ops_check_later. That discipline references an oncall tool and belongs to
  the oncall flow (a future hook axis if measurement warrants); the parked
  helpers retired with `ops_exec`, and the fork keeps the reference
  implementation.

## Rollback

Revert the branch commits. The plugin manifest row, the `ops_exec` module and
its tests restore with them; nothing outside this repository changes.
