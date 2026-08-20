# TUI

> **Status: review baseline (2026-06-28).** Under team review via this PR (owner @sheng.zhao).
> Pending: candidate additions (Turn Cycle, Streaming Segment, Subagent Tree, RPC Client,
> ChatStream, Composer, Slash Command System, …) — owner @sheng.zhao to select.

The terminal front-end (`ui-tui/`, React/Ink). Renders the chat transcript and overlays;
talks to the Runtime only via TUI-RPC. Single-session per client in v0.1.

## Language

**Model scope** (TUI):
Which conversations a `/model` switch reaches. Plain `/model <name>` is
session-scoped: it moves this conversation only and does not touch the
configured default, so a new session still starts where it always did.
`/model <name> --default` changes that default instead, leaving conversations
that already chose their own model alone -- but it does move the ones that
never chose, including, usually, the conversation that asked. Which of the two
happened is the server's answer (`applies_to_session`), not something the scope
implies, and it is what decides whether the status bar repaints. The picker
shows the scope it will use.
_Avoid_: "global model switch" -- that was the pre-session behaviour.

**Overlay**:
A modal layer over the chat view, tracked in `overlayStore` and driven by keyboard. Kinds
split into RPC-driven (Confirm, Approval, Clarify, Sudo, Secret) and user-toggled (Agents,
Model Picker, Picker, Pager) overlays; the FPS counter is a separate component, not an
overlay-store kind.

**MessageLine**:
The UI element rendering one transcript row in the chat view.
_Avoid_: "chat stream" for the UI — chat stream is the data feed it renders

**Status Bar**:
The status rule at the top or bottom of the layout, rendered by the `StatusRule` component;
placement is set by `StatusBarMode` (`top` | `bottom` | `off`).
_Avoid_: "StatusRulePane" — the exported component is `StatusRule`, there is no "Pane".

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
