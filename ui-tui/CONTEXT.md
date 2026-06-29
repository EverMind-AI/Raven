# TUI

> **Status: review baseline (2026-06-28).** Under team review via this PR (owner @sheng.zhao).
> Pending: StatusRulePane → Status Bar correction + candidate additions (Turn Cycle,
> Streaming Segment, RPC Client, Composer, Slash Command System, …).

The terminal front-end (`ui-tui/`, React/Ink). Renders the chat transcript and overlays;
talks to the Runtime only via TUI-RPC. Single-session per client in v0.1.

## Language

**Overlay**:
A modal layer over the chat view, tracked in overlay state and driven by keyboard
(Agents Overlay, Confirm Overlay, FPS Overlay).

**MessageLine**:
The UI element rendering one transcript row in the chat view.
_Avoid_: "chat stream" for the UI — chat stream is the data feed it renders

**StatusRulePane**:
The bottom status rule of the layout.

**Agents Overlay**:
The overlay showing the subagent tree (`SubagentNode` hierarchy with subtree
token/cost aggregates); opened with `/agents`, including for past turns by history index.

**Confirm Overlay**:
The countdown overlay a destructive Confirm Round-Trip presents; the answer resolves
the paused turn.

**Theme**:
The named color/glyph token set all components draw from.

**Current Session**:
The session the TUI is bound to — switching session means rebinding the client to a
different Runtime session key.
