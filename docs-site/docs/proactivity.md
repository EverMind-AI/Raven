# Proactivity Guide

For users and operators who want Raven to offer reminders and follow-up work
without a new request each time. This page covers enabling, configuring,
observing, and stopping **Sentinel**. Developers should use
[Proactivity Design and Implementation](proactivity-design.md) for the
decision pipeline and execution contracts.

## Choose the right mechanism

The Proactive Engine includes Sentinel and the time-driven Cron and Heartbeat
services. They have different purposes and independent lifecycles:

| Mechanism | Use it when | What starts the work |
| --- | --- | --- |
| Sentinel | Raven should decide whether a contextual reminder or follow-up is useful | A periodic evaluation of memory, recent conversations, and derived signals |
| Cron | You want an explicitly scheduled task or reminder | A user-created schedule |
| Heartbeat | You maintain periodic tasks in `HEARTBEAT.md` | Its own timer or an early wake request |

Sentinel is off by default (`sentinel.enabled=false`). Enabling it does not
enable task discovery automatically; disabling it does not disable Cron or
Heartbeat. Use [Command Reference](commands.md) for their separate commands.

Sentinel can stay silent, send a standalone reminder, append a reminder to a
later reply, wait for a session to become idle, or spawn a background task.
These are model decisions, not guaranteed schedules: use Cron when the timing
must be explicit, and do not rely on Sentinel as the sole alert for a critical
deadline.

## Before enabling

- Configure a working model provider; the Planner normally inherits
  `agents.defaults.model`. See [Quick Start](quick-start.md).
- Configure the messaging channel that should receive reminders and verify that
  ordinary messages work there.
- Review which agent-home memory and session data the configured model provider
  may receive. Sentinel uses these as planning context.
- Review tool permissions, workspace restrictions, and subagent backends.
  Enabling Sentinel in the gateway also wires background task spawning; it is
  not a reminders-only switch.

The steps below use `raven gateway`, which assembles and runs the Sentinel
stack. A standalone `raven sentinel` command does not start a background
service.

## Enable and configure

From a source checkout, inspect the saved configuration:

```bash
uv run raven sentinel status
```

Merge the following fragment into your existing `config.json` (normally
`~/.raven/config.json`); do not replace your provider or channel settings.
Use JSON, not YAML. Replace `telegram:123456789` with your own enabled channel
and recipient:

```json
{
  "sentinel": {
    "enabled": true,
    "tick_interval_seconds": 1800,
    "task_discovery_targets": ["telegram:123456789"],
    "nudge_policy": {
      "max_nudges_per_hour": 1,
      "max_nudges_per_day": 4,
      "quiet_hours": [23, 7],
      "high_priority_bypasses_limits": false
    }
  }
}
```

This example uses lower quotas than the defaults and disables the
high-priority bypass. Then start the gateway, or restart the gateway process
you already run:

```bash
uv run raven gateway
```

For a deployment whose targets and limits are already configured, the switch
also has a CLI:

```bash
uv run raven sentinel enable
```

The switch and quota commands update the saved configuration; restart the
gateway for them to take effect. Run configuration commands with the same
`RAVEN_HOME` as the gateway. `sentinel status` reports saved settings, not
proof that a running process has reloaded them.

### Delivery targets

Despite its name, `sentinel.task_discovery_targets` also supplies destinations
for plain nudges aimed at the internal `sentinel:direct` target, such as
daily-plan reminders. It is used even when task discovery is disabled.

- `"channel:chat_id"`: a specific recipient; preferable when a channel has
  multiple conversations.
- `"channel"`: resolve the most recent recipient in that channel at fire time.
- `"*"`: expand to enabled gateway channels and resolve their recent recipients.

An empty list leaves the daily discovery batch without a destination. A nudge
can still target a concrete session, but internal-target nudges need a
resolvable destination. A literal `tui` target is not automatically forwarded
by a gateway that has no matching outlet. Start with one explicit recipient
before choosing broadcast.

### Frequency and quiet hours

Common settings under `sentinel`:

| Setting | Default | Meaning |
| --- | --- | --- |
| `tick_interval_seconds` | `1800` | Evaluation interval in seconds; minimum `60` |
| `evaluator_model` | `null` | Inherit the main model, or select a Planner model |
| `nudge_policy.max_nudges_per_hour` | `3` | Base hourly quota; adaptive and weekend factors can change the effective limit |
| `nudge_policy.max_nudges_per_day` | `10` | Daily policy cap; high priority cannot bypass it |
| `nudge_policy.quiet_hours` | `[23, 7]` | Quiet window in the runtime host's local time |
| `nudge_policy.high_priority_bypasses_limits` | `true` | Allow high priority to bypass quiet hours and the hourly quota, subject to policy checks |
| `nudge_policy.min_interval_seconds` | `300` | Minimum interval for the same session |
| `inject_enabled` / `defer_enabled` | `true` / `true` | Wire reply-appended and delayed reminders; disabling one leaves its decisions unexecuted |

To change the two quotas without editing JSON:

```bash
uv run raven sentinel config set --max-nudges-per-hour 1 --max-nudges-per-day 4
```

Restart afterward. These are Sentinel policy limits, not account-wide billing
caps. User-scheduled Cron jobs bypass the policy check; when the shared ledger
is wired, their fires still consume counters seen by Sentinel.

The policy is not a strict send-time barrier for queued messages. Injects are
checked and charged when queued; deferred reminders are checked at registration
but currently lack a send-time recheck and quota-recording callback. To keep
Sentinel nudges out of quiet windows, also disable inject, defer, and the
high-priority bypass, or keep Sentinel off. This does not silence Cron or
already-running tasks.
See [Policy boundaries](proactivity-design.md#policy-boundaries).

## Optional task discovery

Task discovery proposes a numbered menu of tasks. It is separately opt-in.
Merge these fields into the same `sentinel` object, retaining your delivery
targets:

```json
{
  "task_discovery_enabled": true,
  "task_discovery_time": "08:00",
  "task_discovery_require_confirm": true
}
```

The daily batch runs on a Sentinel tick after the configured local time, not
at an exact minute. Targets default to an empty list, so enabling discovery
alone does not deliver a menu. By default a menu has at most four options and
expires after 60 minutes.

Reply with the option number or `/pick N` in the same conversation, then
answer the confirmation if requested. A selected option can start agent work,
invoke a tool, spawn a subagent, or confirm a learned routine. The confirmation
setting applies to menu choices; it is not an approval step for every
Planner-generated `spawn_agent` decision.

## Inspect and troubleshoot

These commands inspect saved settings or persisted state:

```bash
uv run raven sentinel status
uv run raven sentinel nudges
uv run raven sentinel decisions
uv run raven sentinel routines
uv run raven sentinel attention
uv run raven sentinel behaviors
```

To inspect one planning decision when Sentinel is enabled:

```bash
uv run raven sentinel tick --dry-run
```

Dry-run disables nudge execution and task discovery, but still runs planning
and state maintenance: it may call the model and refresh derived state. It is
not a read-only check or a free preview. The CLI's `--live` mode uses a
headless sink for plain nudges; it does not verify delivery to a real channel.

| Symptom | Check |
| --- | --- |
| Saved settings say enabled, but nothing runs | Restart the gateway and check its startup output; confirm it uses the same configuration home |
| A tick returns `skip` | Often normal: quiet hours, unchanged context, or no useful action; inspect `reason` and `route` |
| Decision exists, but no message arrives | Check the recipient, enabled channel, policy denial, queue expiry, and `no_delivery_target` / `degraded:...` result |
| No task menu appears | Check the separate discovery switch, non-empty targets, local time, quota, and menu expiry |
| No routines or behaviors appear | Routine learning needs enough parseable history; behavior extraction is separately off by default |
| Reminders are too frequent | Lower quotas, review high-priority bypass, and inspect Cron separately |

Replying `/dismiss` after a recent nudge in the same session records dismissal
and starts a cooldown. An ordinary reply is recorded as neutral, not
automatically as acceptance. Neither is a global stop command.

## Costs and safety limits

A tick that reaches the Planner makes a model request; provider retries can
add requests. Fast paths can skip that request. Task discovery, optional daily
analysis, routine validation, behavior extraction, and spawned tasks can add
their own model or tool costs. A 30-minute interval is not a total cost cap.

The Planner may receive memory, recent conversation excerpts, selected
`attention.md` sections, and folded behavior events. Runtime state and feedback
persist across restarts; see [State and feedback](proactivity-design.md#state-and-feedback)
for the files and their roles.

A spawned task uses the selected subagent backend. The built-in `raven-loop`
backend has an iteration cap and omits messaging and recursive-spawn tools.
However, `tools.restrict_to_workspace` defaults to `false`, and ProactiveSpawn
does not force it on or add an overall task timeout. External ACP and CLI
agents run as host processes. Review [Sandbox](sandbox.md) for what isolation
does and does not cover.

## Disable Sentinel

```bash
uv run raven sentinel disable
```

Restart the gateway to stop its Sentinel stack. The command alone does not
stop a currently running stack, cancel an already-dispatched task, remove
state, or disable Cron and Heartbeat. If you need an immediate stop, stop the
running gateway and inspect any remaining external agent processes separately.
Persisted queues are not cleared by disabling; inspect them before re-enabling.
