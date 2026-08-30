# Subagent enable switch, install grouping, and remembered test verdicts

Date: 2026-08-06
Status: approved, not yet implemented
Branch: `feat/subagents_preset_config` (continues the probe/test work)

## Problem

Three gaps, all on the `/subagents` page.

**There is no way to take a subagent off the roster without deleting it.** Every entry
in `subagents.thirdParty` is advertised to the dispatching model through
`format_agent_listing`, so the only way to stop `spawn` and `run_subagent_dag`
offering an agent is to remove its configuration -- losing the name, the key, and any
command edits with it.

**Configuring a preset costs a form round-trip even when nothing needs filling in.**
`claude_code`, `codex`, `openclaw` and `hermes` need no input at all: their commands
are complete as shipped. The user still has to open the pane and press Save.

**The page cannot report the failure that matters most for a cli agent.** The free
probe resolves `argv[0]` on the login-shell PATH, which answers "installed" and
nothing else. The failures actually hit on this machine -- `openclaw`'s
`ProviderAuthError` for a model with no credential, its hard node-version gate -- live
entirely past `which`, and the explicit test that does catch them throws its verdict
away the moment the pane changes.

## Goals

1. A per-agent switch that decides whether the model is offered that agent.
2. Split the presets by whether they are usable at all, so the switch is only offered
   where it can mean something.
3. Remember a test verdict long enough to be useful, without letting a stale one lie.

## Non-goals

- No change to how a subagent runs once dispatched.
- No re-testing on a schedule. A verdict is recorded only when the user asks for one.
- No new status beyond the existing four (`ready` / `attention` / `missing` /
  `unknown`). A remembered failure is rendered by precedence, not by a fifth status.

## Decisions

Settled with the user before this document:

| Fork | Decision |
|---|---|
| What does the switch mean for an unconfigured preset? | **The switch *is* the configuration.** On writes the preset into `subagents.thirdParty` with its shipped defaults and `enabled: true`; off keeps the entry (name, key, command edits survive) with `enabled: false`. |
| How long does "installed but failed its test" live? | **Persisted, and self-invalidating.** A verdict is stored on disk and discarded as soon as the configuration it was measured against changes. |

One restriction the user did not ask for and approved on review: **an Uninstalled row's
switch cannot be turned on** -- turning an already-enabled row back off is never blocked,
whatever its install group -- so an agent that cannot work cannot be switched on for the
first time. See "Enabled is intent" below for why the alternative is worse.

A correction to a mockup shown during that conversation: it placed a keyless
MiroThinker under Installed with a "needs key" note. The user's wording -- Installed
corresponds to *installed* for local agents and *key configured* for API agents --
puts it under Uninstalled instead. This document follows the wording.

## Enabled is intent, not readiness

`enabled` records what the user wants. It is never derived from a probe, and the
runtime never consults a probe when building its roster.

That matters because the alternative is worse in a specific way: if the roster filtered
on readiness, a cli agent whose PATH entry disappeared mid-session -- a `nvm use`, a
reinstall -- would silently vanish from the model's options, and the model would plan
around a roster that shrank underneath it. That is worse even though the actual failure
mode for a spawn naming such an agent is not a clean error:
`SubagentManager._resolve_backend` falls back to Raven's own in-process loop backend for
any name outside its third-party registry, so the spawn is quietly served by Raven
itself rather than erroring. The spawn tool's `agent` enum omits disabled names, so the
dispatching model should not choose one from the enum in the first place; the case this
argument is really about is a config edit that disables an agent mid-session, and there
a roster that silently lost an entry is worse than the same call landing, unexpectedly,
on Raven's own loop. The same argument rules out making the roster depend on a network
call.

The consequence is that a user could enable an agent that cannot work. The guard for
that belongs in the UI, not the runtime: **an Uninstalled row's switch blocks only the
off -> on transition.** An already-enabled row stays switchable off no matter its install
group -- disabling the whole row would bring back the "delete it to take it off the
roster" problem this switch exists to solve. To make such an agent usable in the first
place, you open its pane and fix the cause -- paste MiroThinker's key, or point
`openclaw`'s command at wherever it actually lives -- and Save, which writes it
configured and enabled. That keeps the form flow for exactly the agents that need input
and removes it for the four that do not.

## Architecture

### The `enabled` field

`enabled: bool = True` on both `ThirdPartyCliSubagentConfig` and
`ThirdPartyOpenAISubagentConfig`, wire alias `enabled`.

Defaulting to `True` is what makes this backward compatible: every entry already in a
user's `config.json` keeps being advertised exactly as before, and only an explicit
`false` removes one.

### One filter, at the two consumers

The filter is a single helper in `raven/agent/subagent/backends/__init__.py`, beside
`third_party_agent_meta` and `format_agent_listing` -- the module that already owns how
a config is presented to the model:

```python
def enabled_third_party(configs: Sequence[Any]) -> list[Any]:
    """The subset of third-party configs the model may dispatch to.

    ``enabled`` is the user's intent and is read here rather than derived from a
    probe: a roster that depended on a PATH lookup or a network call would let an
    agent silently vanish from the model's options mid-session, which is worse
    than a spawn that fails with a clear error.
    """
```

It is called by `SubagentManager.add_third_party_subagent`
(`raven/agent/subagent/manager.py:128`) and
`SubAgentDagTool.add_third_party_subagent` (`raven/agent/subagent_dag/tool.py:120`).

Filtering *inside the consumers* rather than at the call sites is deliberate. Five
paths hand a config list to those setters -- `raven/cli/agent_commands.py:344`,
`raven/cli/gateway_commands.py:260`, `raven/cli/tui_commands.py:440`,
`AgentLoop`'s own construction, and `AgentLoop.apply_third_party_subagents`
(`raven/agent/loop/main.py:1372`) -- so filtering at the boundary would be five places
to keep in step, and the sixth would be added without the filter.

`raven.subagents.list` keeps returning **every** entry, disabled included: the page has
to render what it can switch back on.

### Remembered verdicts

New module `raven/agent/subagent/test_state.py`, shaped like the existing
`instances.py`: one JSON file beside the config, written atomically, tolerant of a
missing or malformed file.

```python
_FILENAME = "subagent_test_state.json"


@dataclass(frozen=True)
class LastTest:
    ok: bool
    detail: str
    tested_at_ms: int


class TestStateStore:
    def record(self, cfg: Any, source: str, *, ok: bool, detail: str, tested_at_ms: int) -> None: ...
    def load(self, entries: Sequence[tuple[Any, str]]) -> dict[str, LastTest]: ...
```

`record` takes the **config object**, not a precomputed hash: fingerprinting lives
inside this module so `record` and `load` cannot drift onto different field sets, which
is the one failure that would make invalidation silently stop working. `tested_at_ms` is
passed in rather than read from the clock inside the store, so a test can pin it.

Stored as a list rather than a keyed object, for the reason `instances.py` gives for the
same choice: a subagent name is an arbitrary user string, and a list needs no key
escaping.

```json
{
  "version": 1,
  "verdicts": [
    {
      "source": "config",
      "name": "Coder",
      "ok": false,
      "detail": "CLI agent 'Coder' exited 1: ProviderAuthError",
      "fingerprint": "9f2a1c7b4e0d8a63",
      "testedAtMs": 1780000000000
    }
  ]
}
```

### Invalidation by fingerprint, not by a write hook

Each verdict carries a hash of the entry's **execution-relevant** fields. `load`
returns a verdict only when the fingerprint still matches the current config, so a
stale one is simply absent.

| kind | fields in the fingerprint |
|---|---|
| cli | `command`, `resume_command`, `id_source`, `session_id_pattern`, `output_pattern`, `transcript_format`, `cwd`, `env`, `timeout` |
| openai | `base_url`, `model`, `api_key` |

`name`, `description`, `preset` and `enabled` are deliberately **excluded** from the
digest: none of them changes whether the agent runs, so re-describing an agent or
switching it off and on must not discard a verdict that is still true.

**A rename does lose the verdict**, and that is a consequence of the *key*, not the
digest: a record is keyed `source:name`, because that is how the page looks one up, so
a renamed agent finds nothing under its new name. Accepted rather than worked around --
renaming is rare, re-testing is one click, and keying on anything more stable would
mean inventing an identity for these entries that nothing else in the system has.

Fingerprinting rather than hooking the write path is what makes this correct for a
hand-edited `config.json` too -- an edit no UI hook would ever see still invalidates.
`api_key` is included so that fixing a rejected key clears the old verdict; only the
digest is stored, never the key.

Implementation: `hashlib.sha256` over `json.dumps(fields, sort_keys=True)`, truncated to
16 hex chars. Truncation is fine here because this is a change-detector, not a security
boundary -- a collision would at worst show one stale verdict.

### Where the merge happens

`probe.py` stays free of file I/O. `ProbeResult` gains one field:

```python
    last_test: LastTest | None = None
```

and `probe_all` gains an optional `verdicts: Mapping[str, LastTest] | None` parameter,
keyed `f"{source}:{name}"`, which it attaches to the matching results. The RPC handler
in `methods_config.py` is the only thing that touches the store: it loads the verdicts,
passes them to `probe_all`, and records a new one after `run_test` returns.

That keeps three responsibilities separate and independently testable: `probe.py`
answers "is this usable", `test_state.py` owns persistence, `methods_config.py` wires
them together.

### Wire additions

`ProbeResult.to_wire()` grows `lastTest`, either `null` or
`{"ok": bool, "detail": str, "testedAtMs": int}`.

No new RPC methods and no new REST routes: `raven.subagents.probe` carries the
verdicts, and `raven.subagents.test` records them as a side effect of the call the UI
already makes. The `enabled` toggle rides the existing whole-list
`PUT /raven/subagents`.

## The page

### Groups

| Group | Rule |
|---|---|
| **Installed** | a preset, and either (cli whose `argv[0]` resolves on the login-shell PATH) or (openai with a non-empty `apiKey`) |
| **Uninstalled** | every other preset: not on PATH, no key, or a command `shlex` cannot parse |
| **Custom** | hand-written agents (`preset == null`), plus the two "add custom" rows |

The cli rule reads the probe status (`ready` means on PATH). The openai rule reads
`apiKey` **from the entry itself** -- the configured entry when there is one, otherwise
the preset payload -- and **not** from the probe's detail text. That distinction is not
cosmetic: the probe only short-circuits on a blank key when `source == "preset"`, so a
*configured* openai entry with a blank key is actually sent and comes back `attention
"api key not set or rejected"`. Grouping on that string could not tell "no key" from
"key rejected", which are different groups and different user actions. `apiKey` is
present on the payload the page already holds, so no extra call is needed.

A probe `unknown` (a blank or unparseable command) lands in Uninstalled -- practically
it is "not usable", which is what the group means.

The old **Configured** group is removed. A configured preset now appears in
Installed/Uninstalled with its switch on, which says strictly more than group
membership did.

Until the first probe lands there is no cli install status, so **all** presets render
under the single existing `subagent-sidebar.groupPresets` heading and split into the two
groups only once probes arrive. Splitting on partial information would make rows jump
between groups as results land.

### The switch

`SidebarMenuAction asChild` hosting the existing `Switch`, as a **sibling** of
`SidebarMenuButton` inside the `relative` `SidebarMenuItem`.

Both halves of that are load-bearing. Nesting an interactive control inside
`SidebarMenuButton` -- itself a `<button>` -- is invalid HTML and swallows the click;
as a sibling, no event reaches the row, so the switch needs no `stopPropagation`.
And `SidebarMenuAction` is what gives the row its right-hand clearance:
`sidebarMenuButtonVariants` applies `pr-8` via
`group-has-data-[sidebar=menu-action]/menu-item`, so a hand-rolled positioned `<div>`
would let a long agent name run underneath the switch.

`SidebarMenuAction` ships `aspect-square w-5`, which would squash a switch to 20x20, so
the call site overrides with `w-auto aspect-auto` and drops the action's hover
background (the switch has its own).

Disabled in two situations, each with its own title so the switch never reads as
inertly broken:

- the row is in Uninstalled and not already enabled -- blocks only the off -> on
  transition; an already-enabled row stays switchable off regardless of its install
  group. The title names the cause, either "not installed on this machine" or "no api
  key configured", from two new i18n keys rather than one generic string
- a whole-list save is already in flight -- toggling writes the entire
  `subagents.thirdParty` list, so two overlapping toggles would race and the later
  write would carry a stale copy of the earlier one. Disabling every switch while
  `saving` is true is the smallest correct fix; the alternative (queueing writes) buys
  nothing at this list size.

### Status line precedence

One line per row, most actionable first:

1. `not installed` -- probe `missing`, or `unknown` for a blank/unparseable command
2. **`test failed`** -- a remembered verdict with `ok: false`
3. probe `attention` -- also covers a keyless openai entry: there is no separate
   "needs key" status label, an openai probe returns `attention` ("api key not set")
   for a missing key exactly as it does for a rejected one; the Installed/Uninstalled
   group is what actually distinguishes the two, not the status text
4. `ready`

A remembered failure outranks a green probe on purpose: "`which` found it, and it still
cannot authenticate" is precisely the case the probe alone gets wrong, and it is the
whole reason verdicts are persisted.

The dot colour follows the same precedence, from the one status-to-colour map that
already serves both the dot and the pane line.

The pane shows the verdict with its **age** ("test failed, 2 hours ago"), so a stale
result is never read as a fresh one. Age is rendered from `testedAtMs`; the wire
carries the timestamp, not a pre-formatted string, so the page can keep it current
without a round trip.

## Consequences worth stating

- **Toggling is a config write**, hot-applied through the same
  `PUT /raven/subagents` path the form uses, so flipping a switch changes what the
  model can dispatch to on its next turn.
- **Disabling everything leaves `run_subagent_dag` with an empty roster.** Consistent
  with today's behaviour for an empty config, but reachable by a switch now rather than
  only by deleting entries.
- **A DAG that names a disabled agent fails its capability pre-check**, with the same
  "unknown agent" path an unconfigured name already takes. No new error handling.

## Tests

Extend `tests/test_subagent_probe.py` and add `tests/test_subagent_test_state.py`.

`enabled` filtering:
- an entry with `enabled: false` is absent from `SubagentManager.list_third_party_agents`
  and from the DAG tool's roster, while `enabled: true` and omitted are both present
- `format_agent_listing` output for a mixed list names only the enabled agents -- this
  is the assertion that actually protects the tool description the model reads
- a config with every entry disabled leaves the DAG tool with an empty roster rather
  than raising
- the default is `True`, so an entry that never mentions `enabled` still runs

`test_state.py`:
- `record` then `load` round-trips a verdict
- a verdict whose fingerprint no longer matches the config is **not** returned
- re-describing an agent or toggling `enabled` **preserves** the verdict; changing
  `command` (cli) or `apiKey` (openai) discards it
- a **rename** does not find the verdict, because the record is keyed by name -- pinned
  as intended behaviour so nobody later "fixes" it into a silent stale-verdict bug
- `source` is part of the key, so a preset verdict is not served to a configured entry
  of the same name
- a missing file, a malformed file, and an unwritable directory all degrade to "no
  verdicts" without raising -- the page must not break because this file is bad
- the stored payload contains no api key, only the digest

merge path:
- `probe_all` with a verdict mapping attaches `last_test` to the matching result only
- `ProbeResult.to_wire()` emits `lastTest: null` when there is none
- `raven.subagents.test` records a verdict that a following `raven.subagents.probe`
  returns

Frontend gate is unchanged (`pnpm -C frontend lint`, `pnpm -C frontend build`); there is
no JS unit-test runner in this repo.

## Rollout

The gateway holds the RPC table and `presets.py` in memory, so none of this reaches the
browser until it is restarted -- the same wall the previous two pieces of work hit.
Browser verification is a step that needs the user's live stack restarted.

## Risks

| Risk | Mitigation |
|---|---|
| A user disables an agent and forgets, then wonders why the model never uses it | The switch is visible on every row, and the roster is what the tool description shows |
| A persisted verdict is stale in a way the fingerprint cannot see (the CLI itself was upgraded, config unchanged) | The pane shows the verdict's age, and re-testing is one click |
| `enabled: false` on an agent with a running instance | Out of scope by design: `enabled` gates the roster handed to `spawn`/`run_subagent_dag`, not an in-flight run, which continues to completion |
| The new file grows without bound | One verdict per `source:name`; `record` replaces rather than appends, but `load` never writes -- it only declines to return a row whose fingerprint no longer matches, so a deleted or renamed agent's stale row stays on disk indefinitely, bounded by how many distinct `source:name` keys have ever been tested |
