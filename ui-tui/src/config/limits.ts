export const LARGE_PASTE = { chars: 8000, lines: 80 }

export const LIVE_RENDER_MAX_CHARS = 16_000
export const LIVE_RENDER_MAX_LINES = 240

// Live reasoning is an append-only buffer that the renderer re-reads on every
// streamed update, so it has to be bounded by what a reader can use rather than
// by how long the model thinks. Both accumulators the renderer reads -- the
// turn's whole reasoning and the running segment's -- are trimmed to the same
// window; the episode's own copy gets the smaller one because a turn holds many.
export const REASONING_MAX_CHARS = 80_000
export const REASONING_KEEP_CHARS = 60_000
export const EPISODE_REASONING_MAX_CHARS = 20_000
export const EPISODE_REASONING_KEEP_CHARS = 16_000

// History-render bounds for messages outside FULL_RENDER_TAIL. Each rendered
// line ≈ 1 Yoga/Text node + inline spans, so this is the dominant lever on
// cold-mount cost during PageUp catch-up. 16 lines × 25 mounted ≈ 400 nodes
// — comfortably inside the 16ms per-frame budget. User pages back to
// recognize, not to read; full re-render once it falls inside the tail.
export const HISTORY_RENDER_MAX_CHARS = 800
export const HISTORY_RENDER_MAX_LINES = 16
export const FULL_RENDER_TAIL_ITEMS = 8

export const LONG_MSG = 300
export const MAX_HISTORY = 800
export const THINKING_COT_MAX = 160

// Rows per wheel event (pre-accel). 1 keeps Ink's DECSTBM fast path live
// (each scroll < viewport-1) and produces smooth motion. wheelAccel.ts
// ramps this on sustained scrolls.
export const WHEEL_SCROLL_STEP = 1

// How often the instance on screen is re-read while it is answering. Its steps
// live only in the runtime's in-flight activity, so this is the one thing that
// makes them appear before the turn ends. Fast enough to read as live, slow
// enough that a folded read of one conversation is not a per-frame cost.
export const DIRECT_STEP_POLL_MS = 400

// One `dag.node` per running-or-expanded node per tick, so a little slower than
// the direct-chat poll, which is one call however many instances exist.
export const DAG_NODE_POLL_MS = 500

// Rows of trace inside an expanded node's box. Blank-padded when the trace is
// shorter, so the box's height never depends on its content.
//
// A node still working gets the small window: it is re-read twice a second, and
// a tall box redrawing that often shoves everything under it. A node that has
// stopped will never move again, so it gets a window worth opening -- the box
// cannot scroll, so its height is the only thing that decides how much of a
// finished trace a reader can see without `/dag`.
export const DAG_TRACE_ROWS = 8
export const DAG_TRACE_ROWS_SETTLED = 20

// The box's outer height: its rows plus a header, a footer, and the two border
// rows. `height` on a bordered Box is the outer height -- the fork sets a Yoga
// border -- so this is what the height model adds and what the Box takes.
export const DAG_TRACE_BOX_ROWS = DAG_TRACE_ROWS + 4
export const DAG_TRACE_BOX_ROWS_SETTLED = DAG_TRACE_ROWS_SETTLED + 4

// How many trailing wire rows the trace box will consider when deciding what
// fits. The fold can collapse an un-narrated run of tool calls into one row, so
// the height it measures does not grow with the slice and the search cannot
// rely on overflowing to stop. Past this many rows the fold has certainly
// collapsed them into summaries, and another row cannot add a line a reader
// could tell apart.
export const DAG_TRACE_FIT_MAX_ROWS = 64

// `max_output_chars` for the trace read. A finished node's output arrives as the
// last message of the trace, so this bounds the answer the box can show; matches
// what `/dag <node>` already asks for.
export const DAG_TRACE_OUTPUT_CHARS = 4000

// Consecutive failed `dag.node` reads before a node is given up on. A run dir
// that is merely busy does not throw; one that throws this many times in a row
// has been pruned, and without a cap a node whose pinned status is frozen at
// `running` would be re-read for the rest of the session.
export const DAG_TRACE_READ_FAILURE_CAP = 5
