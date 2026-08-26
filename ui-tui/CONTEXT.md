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

**Turn Artifact Shelf**:
The block a closed turn ends with, naming the files it changed and delivered ("Files
changed this turn"). Its boundary is a user row -- typed, or one the runtime opened -- a
system row, or the end of the rows; the shape of an assistant row is not one, since a row
carrying text and no tool call is as much an agent between two steps as the end of a turn.
A live read (`openTurn`) withholds the trailing shelf, matching the main agent, which
appends its own once the turn completes and shows none while it runs.
_Avoid_: "artifact panel"/"delivery list" -- one block holds both halves, and it belongs to
a turn rather than to the session.

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
including for past turns by history index. A run's detail pane shows its conversation in
one of two ways. A run bound to a sub-agent instance (`SubagentProgress.instance`, the
stateful spawn's handle) draws that instance's Direct Chat there — the same store rows,
folds and polling as the chat view — and, when the registry row is `resumable`, a composer
under the pane that sends to that instance (`sendDirect`) -- or, while the instance is
mid-turn, steers it (`subagents.instance.steer`: the words are merged into the running turn
and read before its next step, and the run announces them itself as a user row marked
`steer`; the fold draws that row as a steer episode -- one indented line inside the turn,
placed after the paragraph it cut into -- rather than as a new turn; `no_turn` falls back
to a send, `unsupported` hands the text back to the draft). Tab moves the keys
between the composer and the pane, and Esc with a draft clears it before it goes back to
the list. Any
other run (a stateless spawn, a graph node) polls its own per-step transcript
(`subagent.context` for a spawn, `dag.node` for a graph node — the same message shape by
design), read-only, and redraws it while the run works.

**Live Agents** (`ui-tui/src/app/liveAgentsStore.ts`):
The session's delegated runs — spawns and dag nodes — folded from `subagent.status` and
`dag.*` events, reconciled against `subagent.list` on the boundaries events cannot cover
(cold start, reconnect, a missed terminal frame). Deliberately not turn-scoped: a
background spawn outlives the turn that made it, and `$turnState.subagents` is cleared at
every turn end. Cleared when the session on screen changes, since that is what the runs
belong to. Feeds the Status Bar's ⚡ HUD, the Live Agents Strip, and the Agents Overlay's
live view.
_Avoid_: "running agents" — finished rows linger for a retention window so a just-ended
run is still inspectable.

**Live DAG Run** (`ui-tui/src/app/liveAgentsStore.ts`):
One `run_subagent_dag` graph at graph granularity, folded from the same `dag.*` events as
the node rows: its goal line, when it started and ended, and every node's last known
status. Held beside the rows rather than reduced out of them because the rows are pruned
once they settle, and a tally read off a pruned list would shrink back down as the graph
aged out. Kept for the session, which is what lets a finished graph keep a line on the
Live Agents Strip. Seeded from `subagent.list` only for a graph the disk still shows
working, on the same terms as a spawn row; a node the snapshot stops naming is cancelled,
so a graph that died with its gateway stops reading as live.
_Avoid_: the `DagRunSummary` of `domain/dagRun.ts` — that is the count block one
`dag.run_completed` frame carried, for the DAG Panel.

**Live Agents Strip** (`ui-tui/src/components/liveAgentsStrip.tsx`):
What the session has delegated, under the status rule, in two layers. A **graph line** per
Live DAG Run — `dag <goal> - x/y done - n running - n queued - n failed - elapsed`, zero
counts dropped — carries the run's whole tally and *stays* once the run is over, its
elapsed time frozen at its own end. Under each, an **agent line** per active node of that
graph, then the loose spawns: running or queued only, with a ticking elapsed time, and each
one leaves as it settles. Clicking an agent line opens the Agents Overlay straight into
that run's detail (`agentsFocusId`, consumed once), where its transcript streams as it
works; clicking a graph line opens the overlay itself, a graph being no single transcript.
Hidden entirely only when the session has delegated nothing at all.
_Avoid_: reading it as a history of everything delegated — the graph lines are capped at
the newest few and the agent layer holds only what is in flight; the full record is the
Agents Overlay's business.

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
instance's handle anyway -- from the Agents Overlay's composer such words are *steered*
into the running turn instead. A sub-agent's turn is not cancellable: Ctrl+C means the main
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
One entry in the ordered list of sub-agent instances this session has used, drawn as
`[label]` in one row above the composer (`InstanceChips`; a running instance carries a
bullet, the active one is painted in `accent` and bold, a click switches to it), and the unit
Shift+Left / Shift+Right cycles through to switch Direct Chat. The row is for the instances a
person can talk to (`/new-instance`, a stateful spawn's handle); a dag run is shown on the
Live Agents Strip instead. The first chip is always the way back to the main agent, and it
and the active one are the two a width fit never truncates away. Ordered by when each instance first
appeared and never resorted, so one press keeps landing on the same instance while several
answer at once. Drawn from the instance registry, re-read after any event that could have
moved it; a *failed* re-read leaves the list as it was rather than emptying it, since a quiet
rpc reports "the call failed" and "there is nothing" the same way. Where the instances are
*seen* is the Agents Overlay and the Live Agents Strip.
_Avoid_: "agent chip" - a chip is one *instance* of an agent, and one agent can have
several.

**DAG Panel** (`ui-tui/src/components/dagPanel.tsx`):
One `run_subagent_dag` run as a framed card in the transcript: a header naming
the call, the size of the graph, the run id, the tally in status glyphs
(`1✓ 1● 2○`) and the run's elapsed time; then the dependency graph of boxed
nodes, one column per depth, each box's label a fixed space in from its own left
border so the ordinals, glyphs and agent names read as columns; then two lines per
node -- its ordinal, its own name, the agent that ran it and what it cost on the
first, and the line it was dispatched with turned in under the name on the second,
where its unmet dependencies and its error go too -- the turn-in mark standing in
the ordinal's column so the text below starts exactly where the node's name does; then a hint naming both ways
into a node. A node with nothing to say on the second line does not draw one. The card carries the call, so the transcript
row for a dag call is suppressed (`WorkSegment`): the row and the header said the
same sentence twice. The summary is the one part of a row with no width of its
own: it takes what its own line leaves once the error and the dependency list --
served first -- have taken theirs, and is dropped outright rather than shown as
three words. It is on its own line because it is a sentence and the rest of the
row is columns: sharing one line, a CJK summary at two cells a character pushed
the agent and elapsed columns so far right that neither could be read down. Nothing
here relies on ink's `truncate-end`, which is a no-op on a Text holding nested
Texts, and every line of this panel is nested Texts: each is cut in cells by the
panel, or it wraps and walks out through the frame. Same for the header, which
gives up its run id, then its elapsed time, to keep the call and the tally. A running node
hangs nothing under its row -- the live character tail that used to sit there cost
a row per running node and moved several times a second; its trace is what the row
opens into, and `dag.node` is polled only for a node someone has opened. Fed either by
live `dag.*` frames during the turn or, on resume, by one `dag.get` snapshot
fetched per run and folded onto the same tool call -- both paths land on
`tool.dag`, so a resumed session shows the graph too, just never the
frame-by-frame replay a live turn drew. The Work Segment holding this call draws
the panel by default, without a click, and folding that segment back by hand
still leaves the panel drawn -- only the summary row folds. Clicking a row, or
the node's box in the graph, opens that node's **Trace box**; both carry the
same key, so they cannot disagree about what is open.
_Avoid_: reading the elapsed columns as billed time -- they are wall clock, off
the runner's own `started_at` / `ended_at`, and a node that shares a stateful
instance spends some of that waiting its turn.

**Trace box**: the fixed-height bordered block a DAG node row expands into,
holding the tail of the node's conversation trace drawn by the transcript's own
renderer, under the node's id and the line it was dispatched with. The only thing
a node row hangs, and the same height whatever the trace's length -- short while
the node still works, since it is re-read twice a second and a tall box redrawing
that often shoves the transcript under it, and taller once the node has stopped,
since it cannot scroll and its height is then the only thing deciding how much is
readable without `/dag`. Nothing oversized may be handed to it: this fork does
not truncate a child that overflows a fixed height, it squeezes the column --
dropping scattered lines and painting the last over the footer -- so `fitTraceTail`
cuts a too-tall message down from its head and marks the cut with a leading `…`.
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
