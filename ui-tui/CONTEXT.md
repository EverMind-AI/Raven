# TUI

> **Status: review baseline (2026-06-28).** Under team review via this PR (owner @sheng.zhao).
> Pending: candidate additions (Turn Cycle, Streaming Segment, Subagent Tree, RPC Client,
> ChatStream, Composer, Slash Command System, …) — owner @sheng.zhao to select.

The terminal front-end (`ui-tui/`, React/Ink). Renders the chat transcript and overlays;
talks to the Runtime only via the RPC protocol. Single-session per client in v0.1.

## Language

**Overlay**:
A modal layer over the chat view, tracked in `overlayStore` and driven by keyboard. Kinds
split into RPC-driven (Confirm, Approval, Clarify, Sudo, Secret) and user-toggled (Agents,
Model Picker, Picker, Pager) overlays; the FPS counter is a separate component, not an
overlay-store kind.

**MessageLine**:
The UI element rendering one transcript row in the chat view.
_Avoid_: "chat stream" for the UI — chat stream is the data feed it renders

**Episode**:
One model call within a turn, opened by an `episode.start` event: its reasoning, its
narration, and the tools it called. A turn is a list of episodes plus the final answer.
_Avoid_: "turn" for a single call — a turn holds many episodes.

**Segment**:
What `EpisodeView` renders a turn as: an alternating stream of `talk` (one episode's
reasoning and narration) and `work`. Episodes are the wire model; segments are the
reading model.

**Work Segment**:
Every call made between two things the model said — so it spans episode boundaries.
Folded it is one row ("listed .raven, read TOOLS.md, ran 4 commands (2.4s)"); opened, one
row per call; a call opens further into its Detail Block. A single-call segment skips the
middle depth, since its folded row already names the call.
_Avoid_: "run"/"tool group" — both were earlier, narrower constructs that this replaces.

**Detail Block**:
A call's full argument and its output, rendered on a filled background (a `▏` rule below
256 colors). The only place the raw command, path, or URL appears; rows above it carry
short labels only.

**Activity Row**:
A dim row naming machine work, with an inline duration and no fold glyph. Expandability is
a property of the activity column, not marked per row; a failure is shown by coloring the
row red, not by a marker.

**Status Bar**:
The status rule at the top or bottom of the layout, rendered by the `StatusRule` component;
placement is set by `StatusBarMode` (`top` | `bottom` | `off`).
_Avoid_: "StatusRulePane" — the exported component is `StatusRule`, there is no "Pane".

**Agents Overlay**:
The overlay showing the subagent tree (`SubagentNode` hierarchy with subtree
token/cost aggregates); opened with `/agents`, including for past turns by history index.

**Subagents Overlay**:
The overlay for configuring third-party sub-agents - listing them by whether they can
actually run, adding one from a preset, enabling, testing and deleting; opened with
`/subagents`. It edits `~/.raven/config.json` and hot-applies the result, so it changes
what the model may dispatch to. Not to be confused with the Agents Overlay, which shows
live delegation state and writes nothing.

**Confirm Overlay**:
The countdown overlay a destructive Confirm Round-Trip presents; the answer resolves
the paused turn.

**Theme**:
The named color/glyph token set all components draw from.

**Current Session**:
The session the TUI is bound to — switching session means rebinding the client to a
different Runtime session key.
