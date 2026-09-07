# Sub-agent mode tiers - design

Date: 2026-09-03
Status: proposed

## Goal

One session-scoped tier - `medium`, `high`, `max` - that raven applies to the
sub-agents it dispatches. Settable two ways, both landing in the same place:

- over ACP, with the stable schema's `session/set_mode`;
- in the terminal, with `/mode` typed in the main conversation.

Raven's own behaviour is identical in every tier. The tier moves what raven asks
of its sub-agents, nothing else.

## Why this is needed

Every part of the delivery pipe is already built and none of it can fire, because
no agent declares a mode this side can name.

A sub-agent's modes are measured from its own ACP handshake, offered to the
dispatching model as a `mode` argument, resolved through one function, and
re-sent on every route into the agent's session. What is missing is a shared
vocabulary and a place for a person to state a standing choice: today the only
standing choice is per instance (`subagents.instance.set_mode`), and the only
other input is the model's own per-dispatch pick -- which this change withdraws,
for the reason recorded under Risks: the model is the one party that cannot know
what the operator chose.

Four of the five agents under `subagents/` carry no `acp` key in their
`config.json` at all. What keeps their menus empty is their own vendored Raven
trees, which predate the catalogue and so default `acp.modes` to `{}`:
`AgentMeta.modes` is empty for each, the `mode` property is absent from the spawn
schema, and the resolver returns `None` every time. The tier is therefore inert
for them on merge and starts working per agent, as each adopts the vocabulary -
which is the intended sequencing, not an accident of it.

The fifth, `raven-research`, is not silent: its own vendored `run.py` already
writes `acp.modes` into its rendered config from a `fast` baseline plus the
`deep` and `ultra` overlays (`subagents/raven-research/run.py:394-396`), so its
menu is non-empty today. Its `fast` is a different vocabulary from ours, and
with the ladder's own cheapest rung renamed to `medium` the two share no word
at all, so the resolver returns `None` for it on the strength of the plain
no-shared-rung rule - the same rule that handles any other agent with an empty
or unrelated menu, not a special case carved out for this one.

The absent key matters for how the first four adopt it. A missing `acp` block
takes the schema default, so a fork picks up `medium` / `high` / `max` the moment
its vendored tree merges trunk, with no config edit anywhere.

## What was measured

Against `origin/refactor/raven_v0_2_0` at `9bc06091`.

### The delivery pipe that already exists

| Step | Where |
|---|---|
| A sub-agent's modes are read from its session response | `raven/acp_client/capabilities.py:428` `_read_session_modes`, re-recorded by `:451` `relearn_session_modes` |
| They become the agent's advertised menu | `raven/agent/subagent/backends/__init__.py:135` -> `AgentMeta.modes`, read back by `raven/agent/subagent/manager.py:944` `agent_modes` |
| The menu is offered to the dispatching model | `raven/agent/subagent/spawn_tool.py:195` - one enum over every agent's ids, per-agent text in the description. Withdrawn by this change |
| One resolver serves every dispatch lane | `raven/agent/subagent/manager.py:958` `resolve_mode`: `requested` > the instance's standing override > `None`. `requested` is withdrawn too |
| A person's standing per-instance choice | `raven/agent/subagent/manager.py:988` `set_instance_mode`, held in raven's memory keyed by `(session_key, agent, handle)`, never persisted |
| Its RPC and its terminal surface | `raven/rpc/methods/instances.py:632` `instances_set_mode`; `ui-tui/src/app/slash/commands/core.ts:264` `/mode` |
| Delivery to the sub-agent | `raven/acp_client/acp_agent.py:1239` `_set_mode` sends `session/set_mode` on every route into the session, and is never fatal |

The mode is authoritative in raven and projected onto the sub-agent. It is re-sent
every turn rather than once because the agent holds it in memory keyed by session
id: it survives an engine eviction but not a process restart, and the pool
relaunches that process whenever the launch key changes.

### Raven's own ACP mode surface

`raven/acp/modes.py` serves the spec's `SessionModeState`; `raven/acp/methods.py:555`
`_session_set_mode` answers the method; `:595` `_apply_mode` hands the chosen
profile to the loop as `SessionPolicy` (`raven/agent/loop/wiring.py:160`
`set_session_policy`, `raven/agent/loop/_shared.py:293`). `AcpModeConfig` and
`AcpConfig` are at `raven/config/schema.py:630` and `:649`; `modes` defaults to
`{}`, which leaves the surface off.

### Pydantic and config write-back

`raven/config/loader.py:474` `save_config` writes `model_dump(exclude_defaults=True)`,
with a docstring naming a real incident (`contextWindowTokens: 65536`) as the
reason it refuses to freeze current defaults into a user's file.

Measured on pydantic 2.12.5, with a `default_factory` returning three modes:

| Case | Result |
|---|---|
| Untouched config, written back | `{}` - the built-in modes do not materialise on disk |
| Config declares its own catalogue | That catalogue only; the built-in is replaced wholesale |
| Config file has no `acp.modes` key | The three built-in modes |
| Config file has `"modes": {}` | Zero modes - an explicit opt-out of the whole surface |

### The terminal command that already exists

`/mode` is registered in `core.ts` and applies only inside a direct sub-agent
chat. In the main conversation it refuses, at `core.ts:273`:

```
/mode applies to a sub-agent chat -- /instance to enter one
```

`RESET_WORDS` (`core.ts:57`) reserves `reset` / `clear` / `default` for dropping an
override.

## Scope

In:

- three built-in modes on raven's own ACP surface, inert for raven itself;
- a session tier reachable from `session/set_mode` and from `/mode` in the main
  conversation;
- a new layer in `resolve_mode` that applies the tier to a dispatch, clamped to
  what the target agent offers.

Out, deliberately:

- changing what a tier does to raven's own effort. Every mode leaves
  `maxToolIterations` inherited and `overlay` empty;
- teaching the agents under `subagents/` the vocabulary. All five vendor a Raven
  fork, so four of them inherit the catalogue by merging trunk and need no config
  change; `raven-research` writes `acp.modes` and `acp.defaultMode` into its
  rendered config at launch (`subagents/raven-research/run.py:394-396`, from
  `modes/deep.json` and `modes/ultra.json`), so it keeps its own vocabulary until
  someone renames those;
- a permanent status-bar element for the current tier;
- the legacy `/fast` and `/reasoning` commands (`session.ts`, both
  `supported: false`), which are unrelated and untouched.

## Design

### 1. The catalogue is a schema default, not a fallback branch

`AcpConfig.modes` gains a `default_factory` returning the three modes in ladder
order, and `default_mode` defaults to `"high"`.

```python
def _builtin_modes() -> dict[str, AcpModeConfig]:
    return {
        "medium": AcpModeConfig(name="Medium", description=_TIER_TEXTS["medium"]),
        "high": AcpModeConfig(name="High", description=_TIER_TEXTS["high"]),
        "max": AcpModeConfig(name="Max", description=_TIER_TEXTS["max"]),
    }
```

One source of truth: `build_session_modes` keeps reading `config.acp.modes` and
needs no built-in branch of its own. A deployment that declares its own catalogue
replaces this one; a deployment that writes `"modes": {}` turns the surface off,
which is the migration path for anyone who wants the pre-change wire.

`default_mode = "high"` is a value, not a constraint. **No `default_mode`
validator may be added.** `SessionModes.__init__` already degrades an unknown
default to the first declared mode, and that degradation becomes load-bearing the
moment this field stops defaulting to `None`: a config that declares its own
catalogue and names no default would be failed at startup by a value it never
chose, and the failure would be ours, not the operator's.

No shipped config does that today - `raven-research`, the only product declaring a
catalogue, writes `acp.defaultMode` alongside it every time (`run.py:396`). The
prohibition is against building the trap, not a report of one being sprung.

The ladder used for clamping is a separate trunk constant, declared beside the
catalogue in `raven/config/schema.py`:

```python
TIER_LADDER = ("medium", "high", "max")
```

Ordering is a trunk fact, not configuration. The clamp that reads it lives in a
new `raven/agent/subagent/mode_tiers.py`, which imports the ladder from config -
the direction `manager.py` already imports in. The two are independent: an operator
who renames the modes gets a catalogue outside the ladder, which the clamp declines
to act on rather than guesses at (rule 1 below).

### 2. The session tier: no new store, two entry points

The tier is already stored. `_apply_mode` writes the chosen profile to the loop as
`SessionPolicy`, keyed by session key, in memory, not persisted. So:

> the session tier is `loop.session_policy(session_key).mode`.

- **ACP**: no change. `session/set_mode` is served, and section 1 gives it three
  values to accept.
- **TUI**: a new RPC `session.set_mode` in `raven/rpc/methods/session.py`, declared
  in `rpc-schema/openrpc.json` and surfaced to the terminal as a generated type, which
  resolves the id against the same `SessionModes` the ACP path uses and routes
  through the same `SessionModes.set`, so the two entry points cannot accept
  different words or log the same event two ways. Its reply mirrors
  `instances_set_mode`: `{mode, availableModes}`, told apart by which fields are
  present rather than by a sentinel id.

**A session that has never set one still has a tier.** `_apply_mode` runs at the
top of every ACP turn (`raven/acp/methods.py:680`), so an ACP session carries the
catalogue's default from its first turn. Nothing else does: `set_session_policy`
has exactly one caller, and a turn arriving over `turn.send` - the terminal, the
gateway, every IM channel - leaves `SessionPolicy.mode` an empty string. Rather
than teach each surface to stamp a policy, the tier is read as

```python
policy.mode or session_modes.default
```

so an unset session resolves to the same `high` an ACP session starts on. One
default, every surface, and no surface has to remember to write it.

`/mode` becomes context-sensitive rather than gaining a sibling command:

| Where it is typed | What it does |
|---|---|
| Inside a direct sub-agent chat | Unchanged: `subagents.instance.set_mode` |
| In the main conversation | The session tier, via `session.set_mode` |

This fills the branch that refuses today; it does not overload a live one. The
reading is one sentence in both cases - `/mode` sets the tier of whoever you are
talking to - and in the main conversation that is raven, whose tier is the tier of
the sub-agents it dispatches. `RESET_WORDS` keeps its meaning: back to
`defaultMode`.

A switch lands on the session's next turn. A turn reads its policy once, at its
start; nothing running is interrupted.

### 3. Dispatch: one new layer, and a total clamp

`resolve_mode` becomes:

```
the instance's standing override
  > clamp(session tier or catalogue default, what this agent offers)   <- new
  > None (the agent's own default)
```

The tier sits below the override because the override is the narrower statement,
and about a conversation the operator can see. Putting the tier on top would
silently drag a per-instance override back whenever the agent also offered the
session's tier, which would regress a shipped feature.

**The per-dispatch pick is withdrawn rather than ranked.** The first draft kept
`requested` above both, on the reasoning that a caller naming a mode for one
dispatch says the most. That reasoning does not survive asking *who* the caller
is: it is the model composing a `spawn`, and it is the only party with no way to
know what the operator set. A mode it named would have outranked the person who
chose one. So the schema property, its per-agent enum, the refusal that validated
it, `manager.spawn`'s `mode` argument and the `mode` field on the origin record
all go, and `resolve_mode` loses the parameter. What remains are two statements
a person makes: the session's tier, and a standing override on a named
instance.

**The `instance` short-circuit must move.** `resolve_mode` returns `None` today as
soon as `instance` is falsy, before consulting anything. A spawn carrying no handle
is the common case and exactly what the tier is for, so the new layer runs whether
or not an instance was named.

The clamp is total - every input has a defined answer:

```python
def clamp_tier(tier: str, offered: Iterable[str]) -> str | None:
    """The nearest rung this agent has, preferring the cheaper one."""
    if tier not in TIER_LADDER:            # 1. not our vocabulary: do not act
        return None
    have = [t for t in TIER_LADDER if t in set(offered)]
    if not have:                           # 2. no shared rung: the agent's default
        return None
    if tier in have:                       # 3. exact
        return tier
    below = [t for t in have if TIER_LADDER.index(t) < TIER_LADDER.index(tier)]
    return below[-1] if below else have[0]  # 4. highest below, else the lowest
```

Rule 1 keeps a deployment on its own vocabulary out of the ladder machinery: with
raven on `deep`, `{medium}` is not "the nearest rung", it is an unrelated word
that happens to be shared. Rule 4's second half covers the other direction -
raven on `medium` against an agent offering `{high, max}` sends `high`, the
cheapest thing that agent has.

The clamped value goes to the existing `_set_mode`, inheriting its three
properties unchanged: sent on every route in, never fatal, and re-asserted each
turn.

### 4. Wording and observability

Each mode's description is rendered on its own in a picker, so the caveat repeats
per entry rather than sitting in a header nothing renders:

```
The least effort a sub-agent is asked for.        (medium)
The middle rung, and where every session starts.  (high)
The most effort a sub-agent is asked for.         (max)
```

Two log lines, both de-duplicated per `(agent, tier)` following the
`_STALE_SNAPSHOT_SEEN` precedent at `backends/__init__.py:156`, so a busy session
does not print one per dispatch:

- `sub-agent X: tier 'max' not offered; running at 'high'`
- `sub-agent X: offers no tier from medium/high/max; running on its own default`

The switch itself is already logged by `SessionModes.set`, which both entry points
route through.

Current tier is reported by `/mode` with no argument, and nowhere else. Not the
status bar - the terminal's layout tests are sensitive to width, and the value is
available on demand. Not `/status` either: that handler shells out to the CLI
(`raven status`, `raven/rpc/methods/slash_routing.py:196`) with no session key in
the call, so a per-session value cannot reach it without threading one through
`cli_dispatch` into a CLI command - disproportionate to putting a line on screen.

## Testing

One existing test states the contract this change moves, and is rewritten rather
than adjusted to pass. `tests/test_acp_modes.py:21`
`test_no_declared_modes_means_no_surface` builds `build_session_modes(Config())`
and asserts the surface is absent. "Nothing declared" no longer means "no modes";
the test's intent - an empty catalogue serves no surface - survives by constructing
the empty catalogue explicitly.

| File | Action | Covers |
|---|---|---|
| `tests/test_acp_modes.py` | extend | the built-in catalogue on a session response; `set_mode` accepting the three; an unknown id answered with invalid-params naming `availableModes`; a declared catalogue replacing the built-in; `"modes": {}` leaving the surface absent |
| `tests/test_subagent_mode_resolution.py` | new | `clamp_tier` as a table over all four rules and their edges; the `resolve_mode` precedence chain, including the no-instance path that short-circuits today |
| `tests/test_rpc_session.py` | new | `session.set_mode`: report, set, reset; unknown id refused; reply shape |
| `tests/test_rpc_instances.py` | keep green | proof the per-instance override did not regress |
| `ui-tui/src/__tests__/instanceModeCommand.test.ts` | extend | the main-conversation branch, with the direct-chat cases passing unchanged |

**Two generated artefacts gate this change**, both checked by `make` and neither
by the merge-request pipeline, which runs only the Python unit suite:

| Source of truth | Regenerate | Drift check |
|---|---|---|
| `rpc-schema/openrpc.json` (160 methods; `session.set_mode` is added beside `subagents.instance.set_mode`) | `npm run gen:rpc --prefix ui-tui` -> `ui-tui/src/rpc/generated.ts` | `npm run lint:rpc` (`Makefile:62`) |
| `i18n/messages.json` at the repository root - not under `ui-tui/` | `npm run gen:i18n --prefix ui-tui` -> `ui-tui/src/i18n/messages.generated.ts` | `npm run lint:i18n` (`Makefile:63`) |

Only the first is actually touched here. `/mode` has no entry in the terminal
catalogue today and its output is inline English; adding a Chinese alias is
optional and would pull in the second gate.

The terminal suite is run serially - it flakes above ~100 files under default
worker parallelism.

## Follow-ups

Not part of this change, and each unblocked by it:

- four of the five forks under `subagents/` inherit the catalogue by merging
  trunk, and can give the tiers real weight by declaring their own `acp.modes`
  with the same three ids and non-empty `maxToolIterations` / `overlay`;
- `raven-research` moves from `fast` / `deep` / `ultra` onto the shared vocabulary;
- whether a partly-adopted ladder deserves better than clamping was re-asked
  during the pre-submit sweep, because MR !472 turns out to give `raven-code` a
  real partial set (`low` / `max`). Half of it is now answered -- see the Risks
  entry below -- and the rest stands: the log lines in section 4 exist so the
  evidence is there when more agents advertise partial menus.

## Risks

- **One sentence per rung, and the scope stated once per surface.** The first draft gave
  every rung "Sub-agents run at their `<tier>` tier. Raven's own effort is the same in
  every mode." -- a first half restating the name the row already shows, and a second half
  identical on all three, so a picker printed one fact three times and the scope once more.
  Raised by zwwu0215 from building the web control. Each row now says only what
  distinguishes it, which for the built-in rungs is position and nothing else: they carry
  no per-tier behaviour beyond their order and the per-agent clamp, so inventing a
  difference in copy would have been worse than repeating a true sentence. The scope
  belongs to whichever surface draws the control -- the RPC method summary states it for a
  schema consumer, `/mode` states it once for the terminal, and the web states it in its
  own translated footer. The ACP session response has no field for it, which is a protocol
  limitation rather than something to solve by repeating the sentence three times.

- **Raven translates its own rows and no one else's.** Also raised by zwwu0215: the
  descriptions were f-strings with no message id, so a zh reader got English under a
  translated heading. They are message ids now (`raven.i18n.t`, English source), resolved
  where the catalogue is built rather than where it is declared -- a pydantic
  `default_factory` runs before an entrance calls `set_language`, so translating at
  declaration would bake in English. A *declared* catalogue is passed through untouched:
  its words are its author's, in whatever language they wrote, and running them through
  the catalog would look up an id nobody registered while implying we own them. The
  asymmetry is deliberate and is what `AcpConfig.uses_builtin_modes` exists to draw.

- **A foreign menu that shares a ladder word is clamped DOWN, but never climbed
  towards.** The clamp declines a menu outright only when it shares no rung with
  the ladder, so an agent reusing one of our words -- a verbosity menu of
  `low` / `medium` / `high`, or a severity menu of `high` / `critical` -- is still
  clamped onto that word rather than left on its own default: `max` against
  `('low','medium','high')` answers `high`. What changed during the sweep is the
  other direction. Raising the effort, the move left when nothing on the menu sits
  at or below the tier, now requires every rung on offer to be one the ladder can
  rank. MR !472's `raven-code` catalogue (`low` / `max`) is why: `low` is illegible
  from here, so the cheapest *visible* rung is `max`, and a `medium` session was
  resolving onto the agent's dearest setting. On a control whose purpose is capping
  effort that is the wrong direction to guess in, so an unrankable menu falls
  through to the agent's own default instead. The code moved first here and this
  entry follows it: the defect was found by reproduction during the sweep, not
  designed. Renaming the cheapest rung from `fast` to
  `medium` removed the one case that was actually integrated (`raven-research`, which
  advertises `fast` / `deep` / `ultra` and now shares nothing), but it did not remove
  the class, and `medium` / `high` are more commonly reused scale words than the names
  they replaced -- `ui-tui/src/components/appChrome.tsx:297` already treats the literal
  `medium` as a default for an unrelated reasoning-effort knob.
  A subset guard (decline any menu that is not a subset of the ladder) closes this, and
  was written and then removed, because it has the larger cost: it also refuses an agent
  that supports all three of our tiers PLUS one of its own, which is the shape to expect
  as the fleet converges on this vocabulary. The trade is deliberate and the cheaper
  side of it was chosen; revisit it if a real agent turns up advertising a generic
  scale that overlaps ours.


- **The wire changes for every deployment.** Session responses gain a `modes`
  object and `session/set_mode` stops answering method-not-found. It is additive -
  a client that ignores `modes` is unaffected - and `"modes": {}` is an explicit
  opt-out.
- **A tier is a claim about sub-agents made on raven's own mode surface.** An
  upstream client reads three effort modes and gets three sub-agent tiers. The
  descriptions say so in every entry; there is no way to say it structurally, because
  the spec has one mode surface per agent.
- **`default_mode = "high"` applies to a fleet that has not opted in.** It is inert
  until an agent advertises a ladder rung, at which point every dispatch to that
  agent moves off the agent author's default. This is the intended control, but it
  arrives with the agent's adoption rather than with this change, so the two are
  worth landing close together.
