# Context Map

> **Status: review baseline (2026-06-28).** The domain glossary (this file + `CONTEXT.md`
> + `ui-tui/CONTEXT.md`) is under team review via this PR — owners refine their assigned
> terms by branching off this PR branch and merging refinements back; the branch merges to
> `main` when review completes.

## Contexts

- [Raven Runtime](./CONTEXT.md) — the Python agent runtime: channels, spine, agent loop, engines, providers
- [TUI](./ui-tui/CONTEXT.md) — the terminal frontend (`ui-tui/`, React/Ink); talks to the Runtime only via TUI-RPC

## Relationships

- **TUI ↔ Runtime**: communicate exclusively over the TUI-RPC protocol (`raven/tui_rpc/`); the TUI never imports Runtime internals
- **bridge/ (WhatsApp TS)**: part of the Runtime context's channel boundary, not a separate context

## Terms under review

A full glossary↔code gap scan (2026-06-28) is under team review. Key open items:

- **Bus → Spine** — the `CONTEXT.md` "Bus" cluster (Message Bus / Event Bus) is being
  rewritten as a **Spine** term; `raven/bus/` was replaced by `raven/spine/`.
- **Proposed missing Runtime terms** — Spine, Agent Loop, Turn Runner, Plugin, Skill Hub,
  Agent Hook, Subagent, Routing Profile. (**Consolidator** landed — now defined in `CONTEXT.md` → Memory.)
- **TUI side** — one correction (StatusRulePane → Status Bar) + candidate additions
  (Turn Cycle, Streaming Segment, RPC Client, Composer, Slash Command System, …).

Each owner reviews their assigned terms and merges refinements via the PR. ~30 terms were
confirmed still accurate and need no change.
