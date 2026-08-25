# TUI

> **Status: review baseline (2026-06-28).** Under team review via this PR (owner @sheng.zhao).
> Pending: candidate additions (Turn Cycle, Streaming Segment, Subagent Tree, RPC Client,
> ChatStream, Composer, Slash Command System, …) — owner @sheng.zhao to select.

The terminal front-end (`ui-tui/`, React/Ink). Renders the chat transcript and overlays;
talks to the Runtime only via the RPC protocol. Single-session per client in v0.1.

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
Model Picker, Picker, Pager, New Instance Picker) overlays; the FPS counter is a separate
component, not an overlay-store kind.

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
Folded it is one row ("listed .raven, read TOOLS.md, ran 4 commands (2.4s)"), plus a DAG
Panel under any `run_subagent_dag` call it holds; opened, one row per call; a call opens
further into its Detail Block. A single-call segment skips the middle depth, since its
folded row already names the call.
_Avoid_: "run"/"tool group" — both were earlier, narrower constructs that this replaces.

**Codex Verb Rule**:
An entry in `CODEX_VERBS` (`ui-tui/src/domain/codexTools.ts`) giving one codex tool its
folding style. The verb is codex's own name verbatim — only `unit` and `style` are the
TUI's. Needed because codex rows deliberately keep codex's vocabulary instead of Raven's,
so `OVERRIDES` cannot match them. Claude Verb Rule is the same construct for a claude_code
row.

**Claude Verb Rule**:
An entry in `CLAUDE_VERBS` (`ui-tui/src/domain/claudeCodeTools.ts`) giving one Claude Code
tool its folding style. The verb is Claude Code's own name verbatim — only `unit` and
`style` are the TUI's. Needed because a claude_code row deliberately keeps Claude Code's
vocabulary instead of Raven's, so `OVERRIDES` cannot match it either.

**Detail Block**:
A call's full argument and its output, rendered on a filled background (a `▏` rule below
256 colors). The only place the raw command, path, or URL appears; rows above it carry
short labels only.

**Activity Row**:
A dim row naming machine work, with an inline duration and no fold glyph. Expandability is
a property of the activity column, not marked per row; a failure is shown by coloring the
row red, not by a marker.

**Prompt Block** (`ui-tui/src/components/messageLine.tsx`):
The person's own message, drawn on a filled background with the prompt chevron in its
gutter. A wrapped prompt stays one rectangle rather than one per line. Two padding rows
sit inside the fill, drawn at every color tier so `estimatedMsgHeight` can reserve a row
count without reading the terminal's capability; the fill itself is skipped below 256
colors, where nothing sits between black and brightBlack, and the chevron carries the row
alone.

**Cover** (`ui-tui/src/components/branding.tsx`):
The opening screen — wordmark above the session panel — which lives in the transcript as
its first row rather than as a view of its own, so the late `session.info` event has a row
to patch itself onto. It stands until the conversation starts; startup notices and slash
output are not a turn and leave it up.
_Avoid_: "banner" for the whole thing — the banner is only the wordmark inside it.

**Status Bar**:
The status rule at the top or bottom of the layout, rendered by the `StatusRule` component;
placement is set by `StatusBarMode` (`top` | `bottom` | `off`).
_Avoid_: "StatusRulePane" — the exported component is `StatusRule`, there is no "Pane".

**Agents Overlay**:
The overlay showing the subagent tree (`SubagentNode` hierarchy with subtree
token/cost aggregates) merged with the Live Agents rows; opened with `/agents` or Ctrl+T,
including for past turns by history index. A running row's detail pane polls that run's
own transcript (`subagent.context` for a spawn, `dag.node` for a graph node — the same
message shape by design) and redraws it while the run works.

**Live Agents** (`ui-tui/src/app/liveAgentsStore.ts`):
The session's delegated runs — spawns and dag nodes — folded from `subagent.status` and
`dag.*` events, reconciled against `subagent.list` on the boundaries events cannot cover
(cold start, reconnect, a missed terminal frame). Deliberately not turn-scoped: a
background spawn outlives the turn that made it, and `$turnState.subagents` is cleared at
every turn end. Feeds the Status Bar's ⚡ HUD, the Live Agents Strip, and the Agents
Overlay's live view.
_Avoid_: "running agents" — finished rows linger for a retention window so a just-ended
run is still inspectable.

**Live Agents Strip** (`ui-tui/src/components/liveAgentsStrip.tsx`):
The rows under the status rule, one per *active* delegated run (running or queued, spawns
and dag nodes alike), with a ticking elapsed time. Clicking a row opens the Agents Overlay
straight into that run's detail (`agentsFocusId`, consumed once), where its transcript
streams as it works. Hidden entirely while nothing is active — a live monitor, not a
history; finished runs are the Agents Overlay's business.
_Avoid_: confusing it with Instance Chips — a chip addresses an *instance* for Direct
Chat (talking), a strip row watches a *run* (working).

**Subagents Overlay**:
The overlay for configuring third-party sub-agents - listing them by whether they can
actually run, adding one from a preset, enabling, testing and deleting; opened with
`/subagents`. A row with no binary on the login shell PATH is collected behind a single
not-installed entry at the foot of the roster, which opens a list of its own. An un-added
preset there is read-only, since there is nothing to configure until the binary exists; a
configured agent whose binary went missing keeps every action, so a broken one can still
be edited or removed. Every kind that launches a command is filed that way - `cli` and
`acp` alike - while an openai row is placed on whether it was saved, so a preset needing
just an api key stays directly addable. The one `builtin` row answers to no action at all:
it is this process, so there is nothing to test or delete, and its switch belongs to the
package rather than to config - it renders `[core]` where the others carry one, and fills
its status column with its description, having no probe to report. It edits
`~/.raven/config.json` and hot-applies the result, so it changes what the model may
dispatch to. Not to be confused with the Agents Overlay, which shows live delegation state
and writes nothing.

**Direct Chat** (`ui-tui/src/app/directChatStore.ts`):
The mode in which the chat view is taken over by one sub-agent instance's own
conversation: same composer, that instance's transcript, Esc to return. Tracked in
`directChatStore`'s `active` field; `null` means the main Raven conversation. A direct-chat
turn is `turn.send` with a `target`, and is never written to the session transcript - the
main agent learns of it only through the Handoff Block. Its events are routed by their own
`target`, not by which view is on screen, because Esc leaves without stopping the turn. The
reply arrives as `token.delta` either way: an instance whose transport supports Reply
Streaming fills the view as it answers, one whose transport does not lands in a single
frame at the end, and the view treats both identically. Each instance runs on its own lane,
so several can be answering at once and you can talk to one while another writes; what is
refused is a *second* prompt to the instance already mid-reply, which would serialise on that
instance's handle anyway. A sub-agent's turn is not cancellable: Ctrl+C means the main
agent's turn, as it always did.
_Avoid_: "sub-agent session" - that is the CLI-side session a handle resumes, not this view.

**New Instance Picker** (`ui-tui/src/components/newInstancePicker.tsx`):
The `/new-instance` overlay: pick a sub-agent, get a fresh instance of it, and land in its
Direct Chat. Lists only agents that are enabled and stateful, since those are the only ones
a direct chat can address, and shows how many instances of each are already open. Opened
with no argument; `/new-instance <agent>` skips it and creates directly. Distinct from the
Subagents Overlay, which configures *which* sub-agents exist rather than instantiating one.
_Avoid_: "add agent" - nothing is added to the roster; an agent that already exists gets
another instance.

**Instance Chip** (`ui-tui/src/components/instanceChips.tsx`):
One entry in the strip above the composer, naming a sub-agent instance this session has
used and switching to its Direct Chat when selected. The first chip is always the way back
to the main agent, and it and the active chip are the two the strip never truncates away.
Two independent marks: the chip you are *on* is painted in the theme's accent, and a chip
whose instance is *replying* carries a bullet - the second can be any chip, including one
you are not looking at. Ordered by when each instance first appeared and never resorted, so
the accent never slides sideways while several instances answer at once. Drawn from the
instance registry, re-read after any event that could have moved it; a *failed* re-read
leaves the strip as it was rather than emptying it, since a quiet rpc reports "the call
failed" and "there is nothing" the same way.
_Avoid_: "agent chip" - a chip is one *instance* of an agent, and one agent can have
several.

**DAG Panel** (`ui-tui/src/components/dagPanel.tsx`):
One `run_subagent_dag` run drawn under the tool row that started it: a dependency
graph of boxed nodes, one column per depth, then one detail row per node. Fed
either by live `dag.*` frames during the turn or, on resume, by one `dag.get`
snapshot fetched per run and folded onto the same tool call -- both paths land
on `tool.dag`, so a resumed session shows the graph too, just never the
frame-by-frame replay a live turn drew. The Work Segment holding this call draws
the panel by default, without a click, and folding that segment back by hand
still leaves the panel drawn -- only the summary row folds. Clicking a row, or
the node's box in the graph, opens that node's **Trace box**; both carry the
same key, so they cannot disagree about what is open.

**Stream line**: the single row under a running DAG node's row, carrying the
last line's worth of what its sub-agent has produced, refreshed while it works.
Not a step ticker: it is a character tail, so it moves.
_Avoid_: "log line", "tail row".

**Trace box**: the fixed-height bordered block a DAG node row expands into,
holding the node's conversation trace drawn by the transcript's own renderer.
Replaces the stream line rather than joining it, and is the same height whatever
the trace's length.
_Avoid_: "detail panel", "node output".

**Ordinal** (`DagPanel`, `/dag`):
The short number (`1..N`) printed inside each graph box and at the head of the
matching detail row -- what ties the two together, and what `/dag 3` takes so a
node is reachable without a mouse. Assigned in submitted order, not by depth.
_Avoid_: "handle" -- that is the **instance handle** (`a2-d0bd29`), which the same
row already prints as `(subagent@instance)`; one row shows both, so the words
cannot be shared.

**Confirm Overlay**:
The countdown overlay a destructive Confirm Round-Trip presents; the answer resolves
the paused turn.

**Theme**:
The named color/glyph token set all components draw from.

**Current Session**:
The session the TUI is bound to — switching session means rebinding the client to a
different Runtime session key.
