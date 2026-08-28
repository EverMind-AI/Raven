# Subagent availability: automatic probe, explicit test, and honest capability limits

Date: 2026-08-06
Status: approved, not yet implemented
Branch: `feat/subagents_preset_config` (continues the preset-config work)

## Problem

The Subagents page tells the user what a subagent is *configured* as, and nothing
about whether it *works*. Every failure is discovered later, inside a real task,
as a spawn that fails obscurely:

- `claude` / `codex` / `openclaw` / `hermes` are ordinary executables that may
  simply not be installed. Nothing on the page says so.
- `openclaw`'s first real dispatch failed with `ProviderAuthError` because its
  default model had no credential, and again on a hard node-version gate. Both
  are invisible until something runs.
- A `mirothinker` entry with a stale key or a mistyped model name looks identical
  to a working one.

Two configuration fields are worse than unhelpful: they are advertised and then
silently ignored.

- `ThirdPartyOpenAISubagentConfig.system_prompt` is sent to mirothinker, which
  drops it. Verified by controlled experiment: the same instruction obeyed in the
  `user` role is ignored in the `system` role.
- `reads_local_files` on an openai config is rendered into the spawn and
  `run_subagent_dag` tool descriptions as a `local-files` tag
  (`raven/agent/subagent/backends/__init__.py:78`), which tells the dispatching
  model it may hand that agent a path. `OpenAIApiBackend.run` posts one text
  message; no protocol path exists for the remote to open a path. The tag is a
  lie the model acts on.

## Goals

1. Show, without being asked and without cost, whether each subagent is usable.
2. Offer a per-agent Test that reaches a real verdict, with the cost of reaching
   it made explicit.
3. Stop advertising the two capabilities that cannot be delivered.

## Non-goals

- No cost or token accounting for the test.
- No periodic / background re-probing. One probe per page load, plus a manual
  refresh.
- No change to how a subagent runs during a real task.

## Decisions

Three forks were settled with the user before this document:

| Fork | Decision |
|---|---|
| How deep does Test go? | **cli: a real dispatch. openai: the free probe only, never a completion.** The user's cost caveat was attached to the API case alone, and every cli failure we have actually hit lives *after* `which`. |
| Which layer refuses the two limits? | **Split.** `reads_local_files` is protocol-level, so `schema.py` rejects it for every openai agent. `system_prompt` is provider-specific, so `catalog.ts` hides it for mirothinker only. |
| How wide is the automatic probe? | **Configured agents and presets both**, so the Presets group shows what is installed before the user commits to configuring it. |

## Ground truth measured before designing

All four CLIs resolve on the login-shell PATH on this host and answer
`--version` in well under a second: `claude` 2.1.222, `codex` 0.146.1, `openclaw`
2026.7.1-2, `hermes` (OpenAI SDK 2.24.0). `codex` lives in the conda bin dir,
which is on the login-shell PATH and **not** on raven's own -- see
"Probing uses the login-shell PATH" below.

`GET https://api.miromind.ai/v1/models` is a far better probe than a ping:

| request | result |
|---|---|
| valid key | `200` in 0.84s, **and the body lists the models** (`mirothinker-1-7-deepresearch`, `mirothinker-1-7-deepresearch-mini`) |
| bad key | `401 {"error":"invalid api key"}` |
| no key | `401` |
| `GET /v1` (base URL itself) | `404` |

Two consequences: one free call separates *unreachable* / *bad key* / *wrong
model name* / *ready*, and the base URL alone is a useless target -- the probe
must ask for `/models`.

## Architecture

One new module, `raven/agent/subagent/probe.py`, holds the entire answer to "is
this subagent usable?". The RPC layer gets no logic, so the behaviour is unit
testable without a gateway, and the CLI could reuse it later.

```
raven/agent/subagent/probe.py
    probe_one(cfg, *, source, path=None) -> ProbeResult      # free, no side effects
    probe_all(entries)                   -> list[ProbeResult]
    run_test(cfg, *, source)             -> TestResult       # explicit
        |
        +-- cli    -> shutil.which(...)  then CliAgentBackend.run(PROBE_PROMPT)
        +-- openai -> GET {base_url}/models
```

### Result types

Frozen dataclasses with a `to_wire()` that emits camelCase, matching how
`AgentMeta` is an internal `NamedTuple` while the config wire is camelCase.

```python
ProbeStatus = Literal["ready", "attention", "missing", "unknown"]

@dataclass(frozen=True)
class ProbeResult:
    name: str
    source: Literal["config", "preset"]
    kind: Literal["cli", "openai"]
    status: ProbeStatus
    detail: str        # one human-readable line, always populated
    target: str        # cli: the resolved absolute path when found, else the bare
                       # argv[0] as written, so a `missing` result still names what
                       # was looked for. openai: the base URL. "" when unknown.
    elapsed_ms: int

@dataclass(frozen=True)
class TestResult:
    name: str
    source: Literal["config", "preset"]
    kind: Literal["cli", "openai"] | None  # None only for the unknown-name failure,
                                           # which has no config to read a kind from
    ok: bool
    detail: str        # any exception from the run, truncated to 2000 chars
    reply: str | None  # the agent's answer, truncated to 2000 chars; None for openai
    elapsed_ms: int
```

`source` is part of the identity, not decoration: a configured agent and a preset
can share a name (a preset that keeps its own default name is both), so the
frontend keys its map on `f"{source}:{name}"`.

### One status vocabulary for both kinds

The UI must have a single renderer, so the four statuses are kind-agnostic and
each kind maps its own findings onto them.

| status | cli | openai |
|---|---|---|
| `ready` | `argv[0]` resolves on the login-shell PATH; `target` is the absolute path | `200` and the configured model appears in `data[]` |
| `attention` | (not produced) | `401`/`403`; or `200` but the model is absent from the list; or `404` (no `/models` on this endpoint, so key and model are unverifiable); or a preset with no key |
| `missing` | not on PATH | DNS / connect / timeout failure |
| `unknown` | `command` blank, unparseable by `shlex`, or `argv[0]` is itself a `{...}` placeholder | no `base_url` |

`attention` is deliberately distinct from `missing`: "the endpoint answered, but
something about the configuration is wrong" is a different user action from "it
could not be reached at all".

### Neither function ever raises

Both return a result on every path, following the established `hub_test`
contract (`{ok, detail}`, documented as "never raises"). One unreachable endpoint
must not blank the page, and `probe_all` therefore needs no exception plumbing --
each probe is self-guarding.

## Probing uses the login-shell PATH

`_probe_cli` must resolve against `login_shell_env()["PATH"]`, not
`os.environ["PATH"]`.

That is not a detail. `CliAgentBackend._exec` builds the child's environment from
`login_shell_env()` (`cli_agent.py:139-140`), so the login-shell PATH is the PATH
a spawn actually searches. Raven's own PATH is a different set: `codex` lives in
the conda bin dir, so a probe reading `os.environ` would report the agent
"missing" while real dispatches work -- the page would be confidently wrong about
its own runtime.

`probe_all` captures the PATH **once**, in a thread, and passes it into each cli
probe. `login_shell_env()` shells out to `bash -lic 'env -0'` and can block for
real seconds on a first call (it caches afterwards), so a per-agent call would
serialise the batch behind that.

## Automatic probe, per kind

### cli

```
command blank                       -> unknown  "command is empty"
shlex.split raises ValueError       -> unknown  "command cannot be parsed: <e>"
argv empty                          -> unknown  "command is empty"
"{" in argv[0]                      -> unknown  "the command's first token is a placeholder"
shutil.which(argv[0], path=PATH)    -> ready    target = resolved absolute path
otherwise                           -> missing  "<exe> is not on the login shell PATH"
```

No subprocess is spawned. The probe is a filesystem lookup, so probing every
configured agent and every preset on page load costs nothing measurable.

**Documented limitation:** the probe reports on `argv[0]`. For a wrapper command
such as `sh -c '...'` it truthfully reports on `sh`, which says nothing about the
agent inside. The Test button is what covers that case, and the hint text says
so rather than leaving the user to infer a stronger claim than the check makes.

### openai

```
base_url blank                        -> unknown
api_key blank AND source == "preset"  -> attention "api key not set"   (no request sent)
otherwise: GET {base_url}/models
    200, model in data[] ids          -> ready
    200, model absent                 -> attention "reachable, but model <m> is not
                                         in its list (<n> available)"
    200, body has no data[] list      -> ready     "reachable; key accepted; model
                                         list unavailable, so the model name is
                                         unverified"
    401 / 403                         -> attention "api key rejected (HTTP <s>)"
                                         or "...not set or rejected" when blank
    404                               -> attention "reachable, but this endpoint has
                                         no /models (HTTP 404); key and model are
                                         unverified"
    other non-200                     -> attention "HTTP <s>: <body snippet>"
    ClientError / TimeoutError / OSError -> missing "unreachable: <e>"
```

Timeouts: `aiohttp.ClientTimeout(total=10, connect=5)`. A probe is a page-load
cost, so it is bounded tightly -- unlike a real dispatch, which is deliberately
unbounded.

**A blank key is only short-circuited for a preset.** A preset is a template, so
firing a request that is certain to 401 tells the user nothing. A *configured*
entry with a blank key still gets the request, because a keyless endpoint is
legitimate (a local vLLM needs no key) and short-circuiting would report a
working agent as broken. Its 401, if it comes, reads "not set or rejected".

### `trust_env=True` is required

The probe uses `aiohttp` with `trust_env=True`, exactly like
`OpenAIApiBackend.run` (`openai_api.py:78-83`). Without it, a host whose only
egress to the provider is a proxy would report `missing` for an endpoint real
dispatches reach without trouble -- and mirothinker answers an unexpected origin
with `451`, not a connection error, so the misreport would not even look like a
proxy problem. `tests/test_subagent_third_party.py::test_openai_backend_honors_env_proxy`
already pins this for the backend; the probe needs the same guarantee.

## Explicit test

### cli: a real dispatch

1. `probe_one`. Anything other than `ready` returns `ok=False` with the probe's
   own detail, and **no process is spawned** -- there is nothing to execute.
2. Build the real backend via `build_third_party_backend(cfg, ...)` and run
   `PROBE_PROMPT = "Reply with exactly: PONG"` once.
3. `ok = bool(reply.strip())`.

Going through the real `CliAgentBackend` is the point: the test then exercises
argv construction, the login-shell environment, the transcript parser, and the
CLI's own auth -- the layers where every failure we have actually hit lives.

**The verdict does not assert the reply contains `PONG`.** Asserting content
would flake on an agent that answers with a preamble. Exit 0 plus a non-empty
reply is the verdict, and the reply is shown so the user judges it. An empty
reply is itself a meaningful signal: that is exactly the symptom of openclaw
answering its workspace bootstrap instead of the task.

Three consequences, each stated in the UI rather than hidden:

- **It runs the agent for real and consumes that agent's own quota.** The button
  carries this warning.
- **It is capped at 120s** (`min(cfg.timeout or 120, 120)`), even though
  `timeout` defaults to `None` (no automatic limit). A genuinely slow agent can
  therefore report a test timeout while working fine for real tasks. 120s is
  generous for a one-word answer; an unbounded test button would be a hang.
- **A stateful agent's test creates one real session in that CLI's own store.**
  Unavoidable with a real dispatch. Raven's registry, however, stays clean.

### Registry and workspace isolation

`build_third_party_backend` gains two keyword-only overrides, `registry` and
`timeout`, applied to both branches for symmetry (only the cli branch has a
registry). The probe passes `InstanceRegistry(path=<tmpdir>/probe.json)`, so the
handle binding a stateful create commits lands in a throwaway file and never
touches `~/.raven/subagent_instances.json`.

Extending the factory rather than constructing `CliAgentBackend` directly in
`probe.py` is deliberate: a duplicated field list would drift the moment a field
is added to the cli config, and the test would then silently exercise a slightly
different command than a real spawn.

`workspace` is a fresh `TemporaryDirectory` (used as cwd unless `cfg.cwd` is
set), which keeps the test out of the user's workspace and, for openclaw,
guarantees the clean workspace its preset description calls for.

### openai: the free probe, reported in full

`run_test` on an openai config runs the same `/models` call and returns
`reply=None`. It never sends a completion. The result line states that no
conversation was started, so "Test passed" cannot be misread as "a real request
succeeded".

## Test is by saved name, never by client-supplied command

`raven.subagents.test` takes `{name, source}` and looks the entry up in the live
config or in `presets.py`. It does **not** accept a command from the browser.

`PUT /raven/subagents` already lets a client persist an arbitrary command that
the runtime will later execute, so this is not a new capability. But a
`POST /test` carrying its own command would execute one **immediately, with
nothing persisted** -- no config entry, no record of what ran -- which is a wider
hole than the config plane on a service whose authentication story is still open.

Cost of the decision: the user must save before testing. The button is disabled
only while the pane is a *new* agent (`editingName === ''`), which has nothing
saved to test. For an existing agent the button stays enabled and its hint says
it tests **the saved configuration**, so unsaved edits are visibly not covered.

No dirty tracking: the form has none today, and inventing it would mean diffing a
`toEntry` result whose `apiKey` is deliberately absent when unchanged -- a
comparison that reports "dirty" on a pane the user never touched. Stating what
the button tests is honest and costs no state.

Presets are exempt from the whole concern, being defined in Python rather than
supplied by the client.

Unknown name (either source) returns `ok=False` with a "no such subagent" detail
rather than raising, so a stale page cannot produce an error toast with no
context.

## Wire surface

RPC, in `raven/web_rpc/methods_config.py`:

| method | params | returns |
|---|---|---|
| `raven.subagents.probe` | none | `{"results": [ProbeResult, ...]}` |
| `raven.subagents.test` | `{name, source}` | `{"result": TestResult}` |

REST, in `ui-webui/service/raven_config_routes.py`, following the thin-proxy
shape of every other route there:

| route | method |
|---|---|
| `GET /raven/subagents/probe` | `raven.subagents.probe` |
| `POST /raven/subagents/test` | `raven.subagents.test` |

Both live in the gateway process, which is the only one holding the live config
and the login-shell environment.

`raven.subagents.probe` reads config entries as
`SubagentsConfig(third_party=get_third_party_subagents()).third_party`, the same
round-trip `_set` already performs at `methods_config.py:77`. That reads from
disk, so a save is reflected by the next probe with nothing to invalidate.
`get_third_party_subagents` does not redact `api_key` (it round-trips raw values
by design), so the probe sees the real key.

Presets come from `third_party_subagent_presets()`, and all entries are gathered
concurrently with `asyncio.gather`.

## The two capability limits

### `reads_local_files` is refused for every openai agent, in the schema

A new `model_validator` on `ThirdPartyOpenAISubagentConfig` rejects
`reads_local_files=True`, an exact parallel to the existing
`_reject_declared_stateful`. Both enforce the same principle: a declaration no
mechanism can deliver must be refused, not advertised.

The current docstring invites `true` for an endpoint "served from this host,
which can therefore read the run directory". That invitation is withdrawn: being
on localhost does not help, because `OpenAIApiBackend.run` posts a single text
message and there is no channel through which a path could be opened. The
docstring is rewritten to say so.

The default stays `False`, so no existing config breaks -- only an entry that
explicitly opted into `true` now fails validation, which is the point: it was
being rendered into the roster as a `local-files` tag the dispatching model
acts on.

The checkbox is removed from the openai branch of the form. It stays on the cli
branch, where it is real: a CLI agent running in a container or on a remote host
genuinely cannot see this filesystem.

### `system_prompt` is hidden for mirothinker, in the catalog

`PresetDisplay` gains `unsupported?: UnsupportedField[]`, a union narrowed to the
one member the form actually checks (`'systemPrompt'`) rather than a bare
`string[]`, so a typo or an unhandled field name fails to compile instead of
silently doing nothing. mirothinker declares `['systemPrompt']`, and
`SubagentForm` skips that field when it appears there.

Not enforced in `schema.py`, and the reason matters: this is a property of one
provider's server, not of the openai protocol. Keying config validation on a
preset name would make `raven/config/schema.py` depend on `presets.py` for
*rules* -- it already imports it for provenance name checks, but a rule keyed to
`preset == "mirothinker"` puts a vendor quirk in the config layer, where the next
provider quirk would follow it. A hand-edited config can still carry a
`systemPrompt` for mirothinker; it will be ignored, as it is today.

## Frontend

- `useRavenSubagents` gains `probes: Record<string, ProbeResult>` and
  `reprobe()`. Probes are fetched **after** agents and presets resolve, in a
  separate effect, so a slow endpoint never delays the list render. Keyed
  `` `${source}:${name}` ``.
- `reprobe()` runs on exactly two triggers: after a successful `save` (so fixing
  a key or a model name updates the status without a page reload), and from a
  refresh icon button on the pane's status line. Nothing polls.
- New `ui-webui/frontend/src/components/SubagentStatus.tsx` renders one
  `ProbeResult` two ways from a single status-to-colour map: a bare dot with an
  accessible label for a sidebar row, and a full line with `detail` and `target`
  for the pane. One component, so the two places can never disagree.
- `SubagentForm` gains the Test button, its pending state, and a result area
  showing `detail`, `elapsedMs`, and (cli) the truncated `reply`. The
  quota warning sits next to the cli button; the "no conversation was started"
  note sits in the openai result.
- `catalog.ts` gains `unsupported`, and `PRESET_DISPLAY.mirothinker` declares
  `systemPrompt`.
- i18n: new keys in both locales, added with targeted edits only.

Cost of opening the page: zero subprocesses, and exactly one HTTP request per
*configured* openai agent. Unconfigured openai presets send nothing.

## Tests

New `tests/test_subagent_probe.py`, per AGENTS.md section 5.1. HTTP cases use the
`aiohttp.web`-app-on-a-free-port pattern already established in
`tests/test_subagent_third_party.py::test_openai_backend_honors_env_proxy`, so no
new mocking dependency is added.

cli probe:
- executable on PATH -> `ready`, `target` is the resolved absolute path
- executable absent -> `missing`
- blank command, unbalanced quote, `{prompt}` as `argv[0]` -> `unknown`, each
  with its own detail
- resolution uses the PATH from `login_shell_env`, **not** `os.environ`: with a
  binary reachable only via the patched login-shell PATH, the probe finds it
- no agent process is spawned by a probe

openai probe:
- `200` with the model in `data[]` -> `ready`
- `200` with the model absent -> `attention`
- `200` with a body carrying no `data[]` -> `ready`, detail says the model is
  unverified
- `401` -> `attention`
- `404` -> `attention`, detail says key and model are unverified
- closed port -> `missing`
- blank key on a **preset** -> `attention` and the stub server records **zero**
  requests
- blank key on a **config** entry -> the request *is* sent

test:
- cli, not installed -> `ok=False` and no process spawned
- cli, success -> `ok=True`, `reply` carries the stub's output
- cli, stateful create -> the real `subagent_instances.json` path is untouched
  (asserted against the autouse isolated-registry fixture's path)
- openai -> `reply is None`, and the stub records no `/chat/completions` request
- unknown name -> `ok=False`, does not raise

schema:
- `kind="openai"` with `reads_local_files=True` -> `ValidationError`
- `False` and omitted both validate
- `kind="cli"` with `reads_local_files=True` still validates

presets:
- every preset probes without raising and yields a `status` in the vocabulary

Frontend gate is unchanged (`pnpm -C frontend lint`, `pnpm -C frontend build`);
there is no JS unit-test runner in this repo.

## Rollout

The gateway process holds `presets.py` and the RPC table in memory, so **none of
this appears in the browser until the gateway is restarted** -- the same wall the
preset-provenance work hit. Browser verification is therefore a step that needs
the user's live stack restarted, not something the implementation can self-verify.

## Risks

| Risk | Mitigation |
|---|---|
| The cli test spends the user's quota | Explicit warning next to the button; never automatic |
| A slow agent reports a false test timeout | 120s cap documented in the hint text |
| `argv[0]` is a wrapper, so "installed" is weaker than it reads | Hint text states the probe checks the command's executable; Test covers the rest |
| A stateful test leaves a session in the CLI's store | Documented; raven's own registry is isolated to a temp file |
| An existing config with `readsLocalFiles: true` on an openai agent now fails validation | Intended -- it was advertising a capability to the model that cannot be delivered. Default is `False`, so only an explicit opt-in is affected; the error message names the fix |
