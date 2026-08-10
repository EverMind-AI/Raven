# TUI `/subagents` command - design

**Status:** implemented
**Date:** 2026-08-06
**Branch:** `feat/subagents_preset_config`

## Goal

Give the TUI a `/subagents` command that configures third-party sub-agents from
the built-in presets, so a user in `raven tui` can see what is installed, add a
preset, name it, enable or disable it, and confirm it actually answers - without
leaving the TUI for the web UI or hand-editing `~/.raven/config.json`.

## Why this is needed

`ui-webui`'s `/subagents` page is currently the only editor. The TUI reads the
roster once at construction (`raven/cli/tui_commands.py:440`) and offers nothing
to change it; the CLI has no `raven subagents` command either, even though
`raven/config/update_subagents.py`'s own docstring says "Every entry point (CLI,
future WebUI ...) must go through here". A TUI-only user cannot configure
sub-agents at all.

## Scope

**In:** list configured agents with install/test status; add from preset (with a
name and description); enable/disable; delete; free probe; explicit test.

**Out:** creating or deep-editing a *custom* agent (12+ cli fields / 7 openai
fields). The overlay points those at the web UI or `config.json`. Also out: a
`raven subagents` CLI command - the overlay is native, so the CLI is not on the
critical path for this feature.

## Decisions

| # | Decision | Rationale |
|---|---|---|
| D1 | Native Ink overlay, not a CLI handoff | `/setup` suspends Ink to shell out; that flashes out of the TUI. `/skills` and `/model` are native overlays, and this is the same shape as `/skills`. |
| D2 | A preset's editable fields are **name + description** only (plus `apiKey` for openai kind) | Everything else comes from the preset template, which is already correct and verified. |
| D3 | Free probe on open; explicit test opt-in, async and cancellable | A cli test dispatches the real agent: it spends quota and may run 120s. It must never be implicit and must never wedge the UI. |
| D4 | Secret entry copies `modelPicker.tsx`'s masking exactly | A second masking style in the same app is a defect surface. |
| D5 | New `subagents.*` TUI-RPC namespace over the shared config/probe core | `config.set` is a scalar key/value RPC (`{key, value}` + `applied`/`previous`); a list of discriminated unions is not that shape. |
| D6 | The install group is computed server-side | The web UI computes it client-side in `catalog.ts`; a second copy in TS is how the two surfaces drift. |

## Architecture

Handlers are thin adapters over the same core the web RPC already uses. No
sub-agent semantics are reimplemented for the TUI:

| Core module | What it provides |
|---|---|
| `raven/config/update_subagents.py` | validated atomic read-modify-write; rejects duplicate names |
| `raven/agent/subagent/presets.py` | `third_party_subagent_presets()` / `third_party_subagent_preset(name)` |
| `raven/agent/subagent/probe.py` | `probe_all(entries, verdicts=)`, `probe_one(cfg, source=, path=)`, `run_test(cfg, source=)`, `TEST_TIMEOUT_SECONDS = 120` |
| `raven/agent/subagent/test_state.py` | `TestStateStore().load(entries)` / `.record(cfg, source, ok=, detail=, tested_at_ms=)` |

```
/subagents (slash)  ->  subagentsHub overlay  ->  TUI-RPC subagents.*
                                                      |
                        update_subagents / presets / probe / test_state
                                                      |
                        ~/.raven/config.json  +  agent_loop.apply_third_party_subagents()
```

### RPC surface

Eight methods in a new `raven/tui_rpc/methods/subagents.py` exposing
`register_subagents_methods(dispatcher, *, agent_loop_factory=None)`, wired into
`register_aligned_methods` beside `register_config_methods` (the umbrella already
forwards `agent_loop_factory`, so new helpers are picked up without registration
drift).

| Method | Params | Result |
|---|---|---|
| `subagents.list` | - | `{rows: SubagentRow[]}` - one call fills the overlay |
| `subagents.add` | `preset`, `name?`, `description?`, `api_key?` | `{added: true, name}` |
| `subagents.update` | `name`, `new_name?`, `description?`, `api_key?` | `{updated: true, name}` |
| `subagents.remove` | `name` | `{removed: bool}` |
| `subagents.toggle` | `name`, `enabled` | `{enabled: bool}` |
| `subagents.probe` | - | `{rows: SubagentRow[]}` |
| `subagents.test` | `name`, `source`, `timeout_s?` | `{ok, detail, elapsed_ms, reply?}` |
| `subagents.test_cancel` | `name` | `{cancelled: bool}` |

`SubagentRow` carries what the overlay renders and nothing more: `name`,
`preset`, `kind`, `description`, `enabled`, `configured`, `group`
(`installed` | `uninstalled`), `probe_status`, `probe_detail`, `has_api_key`,
and `last_test` (`{ok, detail, tested_at_ms}` or null). `apiKey` is **never**
returned - only the boolean `has_api_key`.

`subagents.test` sets its own timeout budget. The 30s default is
`cli.dispatch`-specific (`_DEFAULT_TIMEOUT_S` in `methods/cli_dispatch.py`), not a
global client timeout, so no `timeout_s` plumbing is needed elsewhere. A blocked
120s test still leaves the overlay responsive because the TS side awaits a
promise rather than blocking a thread.

Contract-first, per the existing pipeline: `ui-tui/rpc-schema/openrpc.json` is
the source of truth (each method gets `name` / `summary` / `params` / `result` /
`errors`, as `skill.pin` does), `npm run gen:rpc` emits
`ui-tui/src/rpc/generated.ts`, and `npm run lint:rpc` is the drift gate. Python
adds eight `METHODS` entries to `raven/tui_rpc/models.py` with a
`Params`/`Result` pair each.

### Hot-apply

Every mutating handler calls the live loop's
`apply_third_party_subagents(configs)` (`raven/agent/loop/main.py:1374`), which
updates the spawn registry *and* the DAG tool's executors, registering the DAG
tool on the first non-empty apply. The roster inside the `spawn` and
`run_subagent_dag` tool descriptions therefore changes mid-session with no TUI
restart - the same seam the web RPC uses at `methods_config.py:83`.

### Concurrency

The gateway (web UI) and the TUI are separate processes writing the same whole
`thirdParty` list. Every mutation is read-modify-write through
`update_subagents` (which re-reads the file), and the overlay's cached rows are
never the write basis. Without this, a TUI toggle silently reverts a web UI edit
made seconds earlier.

## Overlay

`ui-tui/src/components/subagentsHub.tsx`, modeled on `skillsHub.tsx` (313 lines,
same shape: RPC on mount, `useInput` keyboard, staged views, error line). Mounted
in `appOverlays.tsx` beside `SkillsHub`, keyed `subagentsHub` in
`overlayStore.ts` as a **user-toggled** overlay so it survives turn boundaries
(the store's reset preserves `agents` / `modelPicker` / `skillsHub`; it must
preserve this too).

```
┌ Subagents ─────────────────────────────────────┐
│ INSTALLED                                      │
│  ● Coder          claude_code   [on ]  ok 2h   │
│  ● Writer         codex         [on ]  ⠿ 12s   │
│  ○ General Agent  hermes        [off] fail 1d  │
│ NOT INSTALLED                                  │
│  ○ Guard          openclaw      [off] missing  │
│ AVAILABLE PRESETS                              │
│  + opencode       not configured               │
│                                                │
│ enter add/edit · space toggle · t test         │
│ esc cancel test · d delete · r re-probe · q    │
│ custom agents: web UI /subagents or config.json│
└────────────────────────────────────────────────┘
```

Stages: `list` -> `form` (add or edit) -> `confirm-delete`.

The form is two fields (three for openai kind), Tab to move:

```
┌ Add OpenCode ──────────────────────────────────┐
│ Saved to ~/.raven/config.json · Tab switches   │
│                                                │
│ ▸ Name:                                        │
│   opencode▎                                    │
│   Description:                                 │
│   OpenCode CLI - open-source coding agent…     │
└────────────────────────────────────────────────┘
```

`description` is load-bearing, not cosmetic: `format_agent_listing` renders it
into the `spawn` and `run_subagent_dag` tool descriptions, so it is what the
dispatching model reads when choosing an agent. It defaults to the preset's
shipped text, and clearing it reverts to that default rather than going blank -
the rule `toEntry` already applies in the web UI.

### Secret entry

Copied from `modelPicker.tsx`, cited so an implementer matches rather than
reinvents:

| Behaviour | Source |
|---|---|
| `'•'.repeat(Math.min(len, 40))`, `(empty)` when blank | `modelPicker.tsx:547,578` |
| caret `▎`, suppressed while saving | `modelPicker.tsx:553` |
| `Tab` cycles fields; focused `accent`, unfocused `muted`, `▸` marker | `modelPicker.tsx:176,571-572` |
| a "Saved to <path>" line under the title | `modelPicker.tsx:562` |
| key field omitted when the kind needs no key | `modelPicker.tsx:545` (`auth_type === 'local'`); here: cli presets |

Two deliberate departures:

1. **Storage path.** The model picker writes LLM keys to `~/.raven/.env`; a
   sub-agent's key is a field on its config entry
   (`subagents.thirdParty[].apiKey`). Only the input logic is shared - relocating
   storage would break the schema and the web UI's contract. The header line
   states the real path.
2. **A stored key is never echoed back, not even masked.** The field opens blank
   and blank-on-submit means "keep the stored key" (the web UI's `hasStoredKey`
   rule). Pre-filling 40 bullets invites correcting a key nobody can read, and
   silently overwriting it.

## Slash command

In `ui-tui/src/app/slash/commands/ops.ts` beside `/skills`: bare `/subagents`
opens the overlay via `patchOverlayState({ subagentsHub: true })`; with an
argument it runs non-interactively, mirroring `/skills pin <x>`:

```
/subagents                      open the overlay
/subagents add <preset> [name]  add from preset
/subagents on|off <name>        toggle
/subagents test <name>          run the real test
```

## Error handling

| Case | Behaviour |
|---|---|
| probe failure | never raises (existing `probe.py` invariant); the row shows `unknown` |
| schema validation error | overlay error line; nothing written (`update_subagents` validates before writing) |
| duplicate name | `ValueError` from the core, surfaced verbatim; nothing written |
| malformed `subagents` section on disk | explicit error message, not an empty list |
| test failure | the `detail` string, persisted via `TestStateStore.record` so it survives closing the overlay |
| openai preset added with no key | row renders `key required`; the entry is still added, and `enabled` stays off until it can work |
| no live agent loop (demo runner) | mutations still write config; hot-apply is skipped, not an error |

## Testing

**Python** - `tests/test_tui_rpc_subagents.py` (matches the `test_tui_rpc_*.py`
convention):

- `subagents.list` shape; `apiKey` absent and `has_api_key` correct
- add / update / remove / toggle round-trip against a `tmp_path` config
- duplicate name rejected, nothing written
- group computation for cli (probe) and openai (key present) rows
- `subagents.test` records a verdict; `test_cancel` kills a running test
- hot-apply: `apply_third_party_subagents` called on every mutation, skipped
  cleanly when no loop is present

**TypeScript** - vitest in `ui-tui` (`src/__tests__/`):

- slash parse for all four forms (following `createSlashHandler.test.ts`)
- overlay keyboard handling and stage transitions (following
  `modelPicker.test.tsx`)
- the key field renders bullets, never the stored value

**Gates:** `uv run pytest`, `uv run --extra dev ruff check raven tests scripts`
+ `ruff format --check`, and in `ui-tui`: `npm run type-check`, `npm run lint`,
`npm test`, `npm run lint:rpc`.

## Risks

| Risk | Mitigation |
|---|---|
| A 120s test holds an RPC slot | own timeout budget + `test_cancel`; the overlay stays interactive |
| Roster changes mid-session alter tool descriptions between turns | already true of the web UI's hot-apply; the same seam and the same behaviour |
| `openrpc.json` and `models.py` drift | `npm run lint:rpc` in the gate list |
| Group rule duplicated between web UI and TUI | computed server-side (D6); the web UI's client-side copy in `catalog.ts` is left alone for now and noted as a follow-up |

## Follow-ups (not this change)

- `raven subagents` CLI command - the gap `update_subagents.py`'s docstring
  already names.
- Migrate the web UI to the server-computed group so `catalog.ts`'s
  `installGroupOf` stops being a second source of truth.
- Config-layer name normalisation (strip / reject blank / reject duplicates), so
  every consumer can accept whatever config blessed.
