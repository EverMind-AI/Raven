# TUI interrupt: the missing out-of-band escape hatch

Status: not started. Written alongside the fix for the long-thinking freeze
(`ui-tui/src/lib/text.ts`, `turnController.ts`, `raven/rpc/server.py`), which
removed the freeze that exposed this but not the structural gap underneath it.

## Why

A user reported that a long thinking phase sometimes wedged the TUI and that
Ctrl+C then did nothing at all. The freeze had a concrete cause and it is
fixed. The second half of the report does not have a cause of its own -- it is
what the current interrupt design does whenever the main thread is busy for any
reason, and the next bug that overruns a frame budget will produce it again.

Ctrl+C has three possible escape hatches. All three sit on the same failure
domain, and two of them are switched off:

| Tier | Mechanism | State | Survives a blocked main thread |
|---|---|---|---|
| Kernel | termios `ISIG` raises SIGINT | off: raw mode clears it (`hermes-ink/src/ink/components/App.tsx`, `handleSetRawMode`) | yes, if it were on |
| Framework | Ink's built-in `exitOnCtrlC` | off: `ui-tui/src/entry.tsx` passes `false` | no |
| Application | the Ctrl+C ladder in `useInputHandlers.ts` | on, and the only one | no |

With `ISIG` cleared the terminal never generates a signal, so Ctrl+C arrives as
a plain `0x03` byte on stdin. Reading that byte, parsing it and running the
ladder all happen on the thread that is busy. The byte waits in the tty buffer
until the thread comes back, and if it does not come back, nothing happens.

Two consequences are worth stating separately because each is easy to miss.

**Registering a JS signal handler removes the last kernel-level backstop.**
`ui-tui/src/lib/gracefulExit.ts` installs `process.on('SIGINT', ...)`. Node
delivers signals through libuv's loop, so a JS handler needs a healthy loop to
run at all; without one, the default disposition -- terminate the process, no
JS involved -- is what would have fired. The handler trades a backstop that
always works for one that works only when the process is already fine. Today
this is moot, because raw mode means the signal never arrives: the forwarder in
`raven/cli/tui_commands.py` (`run_subprocess`, `run_subprocess_with_rpc`) is
unreachable from the keyboard for the same reason.

**The existing anti-wedge machinery is in the same domain.** `chatStream.ts`
arms a `DEFAULT_WATCHDOG_MS` timer so a turn that never produces a terminal
event cannot wedge the UI. It is a `setTimeout` on the blocked thread. It
covers a silent server; it cannot cover a busy client.

The ladder's semantics are not the problem and should not change. Ctrl+C is not
an exit key, the first press cancels the turn, the second resets locally, and
`decideCtrlC` is the tested statement of that. The problem is that "stop this
turn" and "get out of a process that is not responding" are different concerns
sharing one key, one code path and one failure domain.

## Shape

Two independent tiers. The first is the actual invariant; the second is what
holds when the first is violated by a bug.

### Tier 0: no task on the main thread outruns a frame

The rendering path's cost must be a function of the viewport, not of how long
the session has run. This is the tier the accompanying fix restored, and it is
the only tier that prevents the symptom rather than surviving it. The loop-lag
monitor (`ui-tui/src/lib/loopLag.ts`) exists to say when it has been violated:
each stalled tick appends a line to `$RAVEN_HOME/tui-stalls.log`.

### Tier 1: an escape that does not run on the blocked thread

Nothing inside a Node process can preempt its own main thread -- there is no
public API to terminate JS execution from a worker, and a JS signal handler is
scheduled on the loop it is trying to rescue. The escape has to be another
process. Raven already has one: the Python parent that spawns the TUI and owns
its own asyncio loop.

**Path A -- restore `ISIG`, escalate from the parent.** The only option that
gives the user a key.

1. After Ink enables raw mode, flip `ISIG` back on for the tty (termios is
   per-bit; the rest of raw mode stays). Set `NOFLSH` so the signal does not
   discard input the user was mid-paste on.
2. Ctrl+C then reaches the whole foreground process group: the TUI child and
   the Python parent both get SIGINT.
3. The child runs the existing ladder from its SIGINT handler. Behaviour is
   unchanged whenever the loop is healthy.
4. The parent counts the signals and arms a grace timer. If the child has
   neither acknowledged nor exited by the Nth signal, the parent kills it,
   restores the terminal modes and prints why. The parent's loop is idle, so
   this works when the child's is not.

Costs, none of which should be discovered during implementation:

- The tty line discipline consumes the `0x03`, so the app never sees the
  keystroke. The ladder has to move into the SIGINT handler; `decideCtrlC`
  itself is unaffected, only its caller.
- Ctrl+C stops being distinguishable from any other route to SIGINT.
- Terminals that report keys through the kitty keyboard protocol may deliver
  ctrl+c as a `CSI u` sequence rather than `0x03`, in which case the line
  discipline never sees it and no signal is raised. This repo pushes
  `CSI >1u` (`hermes-ink/src/ink/termio/csi.ts`), whose disambiguate-only level
  should still send `0x03` -- but that must be measured, per terminal, not
  assumed. Minimum matrix: Terminal.app, iTerm2, Ghostty, WezTerm, kitty,
  VS Code, tmux over ssh.

**Path B -- parent-side liveness, automatic recovery only.** The parent is the
RPC server and already writes to the child. Once `send_frame` waits on the
transport (the accompanying fix), a child that has stopped reading is directly
observable: the wait stops returning, and `transport.get_write_buffer_size()`
says by how much. The parent can log the stall, and could degrade the session
(force `details_mode: hidden`, say) to let the child catch up.

Path B cannot replace Path A. With `ISIG` off no key produces a signal for
anyone, and the parent cannot read stdin because the child holds it in raw
mode. Path B recovers on its own or not at all; only Path A hands the user
something to press.

## Recommendation

Path A, with Path B's liveness logging as the thing that tells us whether the
escalation ever fires in practice. Not bundled with the freeze fix: it changes
termios and signal semantics for every session, its risk surface is the
terminal matrix rather than the code, and it has to be revertable on its own.

## Verification

- The terminal matrix above, each entry answering one question: does Ctrl+C
  raise SIGINT with `ISIG` restored under Ink's raw mode?
- A test that wedges the child deliberately (a synchronous sleep behind a debug
  RPC method) and asserts the parent escalates and restores the terminal.
- `tests/tui/autotest/tests/test_e2e_ctrl_c.py` keeps covering the healthy-loop
  ladder, which must not change.
