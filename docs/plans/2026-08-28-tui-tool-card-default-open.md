# TUI Tool Card Default-Open with a Row Cap Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A tool call's card opens by default instead of folded, shows at most N rows of its result, and hands the rest to a click that expands it further or folds it back.

**Architecture:** The row cap and the two-state fold store already exist; the work is (a) flipping `defaultOpen`, (b) teaching the height estimator to count an open card, since it currently assumes every settled stretch of work is exactly one row, and (c) a third fold level, which is blocked on there being anything left to reveal.

**Tech Stack:** TypeScript, React + hermes-ink (`ui-tui/packages/hermes-ink`), nanostores, vitest.

**Status:** Not started. Task 0 gates Task 3 and must be answered first.

## Global Constraints

Copied from AGENTS.md, which governs every task here:

- **Comments:** Do not add a comment unless the logic is non-obvious, there is a hidden constraint, or it explains *why*. Every new file needs a module docstring. All comments in English. Match the density of surrounding code -- `episodeView.tsx` and `virtualHeights.ts` both comment heavily on *why*, so a non-obvious choice gets a comment and a mechanical line does not.
- **Tests:** `ui-tui` uses `npx vitest run`. Extend the existing `src/__tests__/episodeView.test.tsx`, `messages.test.ts` and `virtualHeights.test.ts`; do not create parallel per-phase files. Colour and geometry assertions need the `renderSync` + `TerminalScreen` harness (see `src/__tests__/activityRowInk.test.tsx`), because `ink-testing-library` brings its own upstream `ink` and does not exercise this fork's renderer.
- **Commits:** Conventional Commits, `<type>(<scope>): <subject>`, header <= 100 chars, entire message ASCII English, `Co-authored-by: Claude (<real session model id>) <noreply@anthropic.com>` trailer.
- **Commit authorization:** AGENTS.md 3.4 -- **never commit unprompted.** A plan saying "commit per task" is not authorization.
- **Branch:** AGENTS.md 2.2 -- confirm the base with the user before cutting.
- **Build:** `ui-tui/dist` is not rebuilt on launch. Run `npm run build` before testing anything by hand, and `npm run build --prefix packages/hermes-ink` first if the fork changed.

## What already exists

Verified against the tree, not assumed:

| Piece | Where | State |
|---|---|---|
| Row cap | `TOOL_PREVIEW_ROWS = 5`, `ui-tui/src/domain/episodeSummary.ts:383` | Exists. Its own comment says the view and the estimator both read it, and a mismatch reserves the wrong number of rows. |
| "Rows the preview occupies" | `foldedPreviewRows()`, `episodeSummary.ts:409` | Exists, already returns `TOOL_PREVIEW_ROWS + 1` when there is a `+N` row. `virtualHeights.ts` already imports it -- but only to build a cache key, not to sum height. |
| Fold state | `$folds` in `ui-tui/src/app/foldStore.ts` | Two sets per scope, `open` and `closed`, keyed `call:<toolCallId>` / `seg:<firstToolCallId>`. Keyed on transport ids on purpose: a row rebuilt mid-turn must not lose the reader's decision. |
| Per-fold default | `isFoldOpen(scope, key, defaultOpen)`, `foldStore.ts:37` | Holding `closed` ids as well as `open` ones is what keeps a default from overriding the reader. Flipping a default is safe by construction. |
| Where the default is set | `defaultOpen={hasPanel}`, `episodeView.tsx:869` | Today a card opens itself only when it carries a DAG or spawn panel. |
| Estimator taking open state | `estimatedMsgHeight(..., { dagOpen, spawnOverrides })`, `virtualHeights.ts:167` | An established pattern for threading fold state from `useMainApp.ts:354` into the estimator. Reuse it. |
| The `+N` row | `episodeView.tsx:532` and the `output` prop below it | Just a string appended to the array. It inherits the block's `onClick`, which **collapses the whole card** -- there is no third level today. |

Not the same thing, easy to confuse: `DetailsMode` (`'hidden' | 'collapsed' | 'expanded'`) and the `/details` command gate *section visibility* on the older `ToolTrail` path (`messageLine.tsx:131`). They do not feed `episodeView`'s per-card folds.

## The blocker for "expand further"

`ui-tui/src/app/turnController.ts:875`:

```ts
et.resultPreview = toolResultPreview(error || summary || '') || undefined
//                 ^ clips to TOOL_RESULT_PREVIEW_CHARS = 200  (lib/text.ts:188)
```

The RPC `tool.complete` carries only a `result_preview` string (plus `diff` / `file_change`). **The client never holds a tool's full output.** So a third fold level can reveal at most the tail of a 200-character string -- and at typical terminal widths `TOOL_PREVIEW_ROWS = 5` already fits most of 200 characters, which means the `+N` row rarely appears at all today.

## Tasks

### Task 0: Measure the incoming preview before building Task 3

Gates Task 3. Do not design a third fold level before this is answered.

- [ ] Temporarily log `(error || summary || '').length` beside `turnController.ts:875`.
- [ ] Run a tool whose output is long (`cat` a few hundred lines).
- [ ] Read the number off `~/.raven/logs/tui.log`.

Outcomes:

- **Gateway sends much more than 200 chars** -- raising `TOOL_RESULT_PREVIEW_CHARS` gives real content to expand into, and Task 3 stays a client-only change.
- **Gateway also caps it short** -- Task 3 needs a protocol change (a `tool.output` fetch, or a larger preview field in `tool.complete`). That is backend work and belongs in its own plan.

Note: `~/.raven/logs/tui.log` does not record RPC frames today, which is why this needs a temporary log line rather than a grep.

### Task 1: Teach the estimator to count an open card

Do this **before** Task 2. Flipping the default first leaves the transcript with stale cells.

**Files:**
- Edit: `ui-tui/src/lib/virtualHeights.ts`
- Edit: `ui-tui/src/app/useMainApp.ts` (thread the fold state in, as `dagOpen` already is)
- Test: `ui-tui/src/__tests__/virtualHeights.test.ts`

**Why it is the real cost.** `virtualHeights.ts:230` counts a settled stretch of work as `h++` -- exactly one row -- and the comment above it states the assumption and the failure mode:

> A committed message is never live and never opened at first paint, so every stretch of work is exactly one row [...] counting no row for that is what keeps the estimate low, and a low estimate is the stale-cell symptom.

An open card is one row plus the argument row plus `foldedPreviewRows(tool)` plus the two padding rows the card now carries (`paddingTop` on `ActivityRow`, `paddingBottom` on `DetailBlock`).

- [ ] Read the fold scope the same way `episodeView` does (`turnFoldScope`), and pass the open sets into `estimatedMsgHeight` alongside `dagOpen`.
- [ ] Sum an open card's rows from `foldedPreviewRows`, so the cap stays single-sourced.
- [ ] Account for the card's two padding rows.
- [ ] Extend `virtualHeights.test.ts` with an open-card case; the existing cases pin the folded arithmetic and must keep passing.
- [ ] Check the height consumers that are not the virtualizer: scroll-position restore, jump-to-bottom, and `fitTraceTail` (the trace box measures with `dense`).

### Task 2: Open by default

**Files:**
- Edit: `ui-tui/src/components/episodeView.tsx`
- Test: `ui-tui/src/__tests__/episodeView.test.tsx`

- [ ] Decide the predicate (see the recommendation below) and replace `defaultOpen={hasPanel}` at `episodeView.tsx:869`.
- [ ] Confirm a reader's explicit collapse still wins -- `foldStore` holds `closed` ids for exactly this, so it should need no change. Add a test that pins it.
- [ ] Tune `TOOL_PREVIEW_ROWS` if 5 is the wrong number now that cards open unasked. One value, read by both the view and the estimator.

### Task 3: A third level -- capped, then full

Blocked on Task 0.

**Files:**
- Edit: `ui-tui/src/components/episodeView.tsx`
- Test: `ui-tui/src/__tests__/episodeView.test.tsx`

- [ ] Add a `full:<toolCallId>` key namespace beside the existing `call:<toolCallId>`. The two-state store needs no change -- a third state is a second key, not a third set.
- [ ] Lift the `+N` row out of the `output` string array (`episodeView.tsx:532`) into a row of its own with its own handler, so clicking it expands rather than collapsing the card.
- [ ] Give the fully-open level its own ceiling. `previewLines()` has no upper bound of its own; it is bounded today only by the 200-character clip, and raising that clip removes the accidental ceiling along with it.
- [ ] Mirror the third level in the estimator (Task 1's work, extended).

## Recommendation

Ship Task 1 + Task 2 first. Together they deliver "opens by default, capped at N rows, click to fold back" -- the fold-back click already works. Leave Task 3 until Task 0 says whether it is a client change or a protocol change.

On Task 2's predicate: prefer a condition over a bare `true`. A card that failed, or carries a diff, or has a short result is worth opening unasked; a long successful result is what made folding the default in the first place. The estimator work in Task 1 is identical either way, so the choice costs nothing extra and keeps the transcript scannable.

## Risk

`episodeView.tsx` had uncommitted changes from other in-flight work when this plan was written (a shared ground for a call row and its detail block, and a past-tense reasoning label). Task 1 and Task 2 touch the same file. Confirm the state of that work before starting.
