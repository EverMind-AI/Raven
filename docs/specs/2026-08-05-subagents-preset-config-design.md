# Subagents preset configuration - design

Status: proposed
Date: 2026-08-05
Scope: `ui-webui/frontend` (the `/subagents` page), `raven/agent/subagent`, `raven/config/schema.py`

## 1. Problem

`/subagents` is the only place to configure the third-party agents Raven can
dispatch to via `spawn(agent=<name>)`. Today it is a single 667-line page holding
one flat form of 20+ fields, including `idSource`, `transcriptFormat`, and two
raw regex inputs. Built-in presets exist in `raven/agent/subagent/presets.py`
(`claude_code`, `codex`, `mirothinker`) but they are reachable only through an
"Add from preset" dropdown that pre-fills that same flat form, so the user still
has to understand every field to finish. Naming is inconsistent across the UI:
`Sub-Agents`, `Sub-Agent`, `sub-agent`, and `prototype` all appear.

Two agents the team wants as presets, OpenClaw and Hermes Agent, have no preset
at all.

## 2. Goals

1. One spelling, `Subagent`/`Subagents`, in every user-visible English string.
2. Model the page on `/credential`: presets always visible in a grouped left
   list, and a right-hand form that asks only for what the selected agent
   actually needs.
3. Ship five working presets: Claude Code, Codex, OpenClaw, Hermes Agent,
   MiroThinker. "Working" means dispatchable with no field edits on a normal
   install.
4. Keep two custom escape hatches, CLI and OpenAI-compatible, each addable any
   number of times.

Non-goals: renaming i18n key namespaces, renaming the `pages/subagent/`
directory, and any change to the `spawn` / `run_subagent_dag` tool surface.

## 3. Verified CLI behaviour

Everything in this section was measured on this host on 2026-08-05, not read off
documentation. Where documentation and observed behaviour disagree, the observed
behaviour is recorded and the disagreement noted, because two of these
disagreements change the design.

### 3.1 Hermes Agent

| Invocation | Result |
|---|---|
| `hermes -z '<p>' --usage-file <f>` | Creates session; `<f>` contains `session_id`; `message_count` 2 |
| `hermes -r <id> -z '<p>'` | Does **not** resume: answers with no prior context, opens a **new** session; target session stays at 2 messages |
| `hermes -z '<p>' -r <id>` | Same as above, so it is not a flag-order problem |
| `hermes chat -q '<p>' --resume <id>` | **Resumes**: target session goes 2 -> 4 messages |
| `hermes chat --resume <unknown-uuid>` | Exits 1, `Session not found`; does not create |

Consequences:

- `-z` cannot participate in session continuation, so the `--usage-file`
  route to the session id is not usable for a stateful agent even though the
  file really does carry `session_id`.
- A caller-supplied id is rejected, so `idSource` must be `derived`, not
  `provisioned`.
- `hermes chat -Q -q '<p>'` (quiet) is the clean shape. Observed, including on a
  run that made tool calls:
  - stdout: the final answer only (`7`)
  - stderr: `session_id: 20260805_093449_6486bf`
  - resume adds one stderr line, `Resumed session <id> (...)`, and still puts the
    answer alone on stdout
- `--usage-file`'s own help text lists only cost/token/model/api_calls fields; it
  does in fact also write `session_id`. Not relied on either way.

Sessions live in `~/.hermes/state.db` (sqlite, `sessions` and `messages`
tables), not in `~/.hermes/sessions/`, which stays empty.

### 3.2 OpenClaw

| Invocation | Result |
|---|---|
| `openclaw agent --local --session-id <uuid> -m '<p>'` | Accepts the caller's uuid as the session id |
| Same command, same uuid, four turns | Stateful: `messageCount` 0 -> 2 -> 6 -> 8, content probe answered correctly |
| `--json` | stdout is pure JSON; diagnostics go to stderr |
| plain output (no `--json`) | ANSI-coloured `[plugins] loading ...` / `[provider-transport-fetch] ...` lines are interleaved **on stdout**, and `--verbose off` does not suppress them |
| `--model <id>` for an id absent from the agent's configured models | Rejected: `Model override "..." is not allowed for agent "main"` |

Consequences:

- `idSource` is `provisioned`, and create and resume are the *same* command,
  because the session is addressed by the id the caller supplies.
- The documented claim that "plain output writes only the final assistant text to
  stdout, diagnostics use stderr" does not hold here, so `transcriptFormat: text`
  is not usable. `--json` is.
- The reply is at `payloads[0].text` (also `meta.finalAssistantVisibleText`), and
  `meta.agentMeta.sessionId` echoes the session id.

## 4. Backend changes

Three, all in `raven/agent/subagent`. Each one exists because a preset cannot
work without it.

### 4.1 Read the session id from stderr as well as stdout

`CliAgentBackend._attempt` searches only stdout for `session_id_pattern`
(`backends/cli_agent.py:261`). Hermes puts the id on stderr. Change the derived-id
branch to search stdout, then stderr.

This is a fix rather than a new capability: the class docstring says raven "reads
it back out of the transcript", and `_attempt` already treats stderr as part of
the transcript when it builds `combined` (`cli_agent.py:269`).

Because `combined` appends stderr, a preset whose stderr is non-empty would
otherwise return the reply with a trailing `session_id: ...` line. The Hermes
preset therefore carries an `outputPattern` that selects stdout only; see 5.2.

### 4.2 An `openclaw_json` transcript format

Add `parse_openclaw_json(stdout)` to `backends/transcript.py`, returning
`(session_id, reply)` from `meta.agentMeta.sessionId` and the first `payloads[]`
entry carrying text, falling back to `meta.finalAssistantVisibleText` for a run that
produced no payload. Extend the `transcript_format` literal in
`raven/config/schema.py` plus the dispatch in `_attempt`.

Unlike the two JSONL parsers this one is all-or-nothing: it requires stdout to be
entirely one JSON document, so a future openclaw that prefixes a stray stdout line
degrades to returning the raw blob as the reply rather than partially parsing.

A regex `outputPattern` over the JSON was rejected: JSON string escaping means a
multi-line reply comes back carrying literal `\n` sequences, and agent replies
are usually multi-line. A parser per CLI is also what `codex_jsonl` and
`claude_stream_json` already do, so this stays symmetric.

### 4.3 Spawn subagents under the login shell's environment

`_exec` builds `env = {**os.environ, **self.env}` (`cli_agent.py:131`), so the
child inherits raven's own process environment. Measured against the running
gateway (pid 453395):

- `PATH` contains `/usr/local/bin` at position 15 but
  `<miniconda>/bin` at position 6, so `node` resolves to v25.8.2. OpenClaw
  requires `>=22.22.3 <23`, `>=24.15.0 <25`, or `>=25.9.0` and hard-exits
  otherwise, so `openclaw --version` fails under raven's environment and
  succeeds under a login shell's, where `node` resolves to `/usr/local/bin/node`
  v22.23.2. The version gate has no environment-variable override.
- `NODE_OPTIONS=--max-old-space-size=12800` and `CLAUDE_CODE_SSE_PORT=12917`
  leak in. The latter is injected by the editor session; handing it to a spawned
  `claude` would point that child at this session's SSE port.
- Four secret-bearing variables (a code-server password hash, a JWT public key,
  two token paths) are handed to every third-party CLI.

Change: capture the user's shell environment once per process
(`bash -lic 'env -0'`, cached; measured at 34 variables on this host) and use it as
the base, so a subagent sees what the user would see typing the command in a fresh
terminal. Per-agent `env` still layers on top, so anything the login shell lacks
can be pinned from the UI. If the capture fails or times out, fall back to
`os.environ` and log once, so a machine with an unusual login shell degrades to
today's behaviour instead of failing to spawn.

The capture must be given a **minimal base** rather than inheriting raven's
environment. Running `bash -lc` with no `env=` was tried first and only gets half
the job done: the profile *overlays* the inherited environment, so `PATH` is
rewritten (which does fix the node resolution) while the editor-injected variables
this exists to drop survive untouched - `CLAUDE_CODE_SSE_PORT` was verified still
present in the result. The base is therefore `HOME`, `USER`, `LOGNAME`, `SHELL`,
`TERM`, `LANG`, `LC_ALL` copied from `os.environ` when set, plus a bootstrap `PATH`
good enough to resolve `env` before the profile replaces it. `HOME` is what lets
bash find the profile at all.

The shell needs **both** `-l` and `-i` (`bash -lic`), and each flag was added for a
measured reason.

`-i` is needed because most distro `~/.bashrc` files open with a
`[ -z "$PS1" ] && return` guard, and this host's is one: everything below it -
including the `http_proxy` / `https_proxy` / `no_proxy` exports at lines 104-108 and
the version-manager init blocks - is skipped by a non-interactive shell. Measured:
`bash -lc` with the minimal base captured 14 variables with no proxy settings, and a
real dispatch then failed with `HTTP 403: This model is not available in your
region.`

`-l` is needed because `-i` alone sources only `/etc/bash.bashrc` and `~/.bashrc` -
never `/etc/profile`, `~/.bash_profile`, or `~/.profile`, which is where bun, nvm and
cargo write their `PATH` lines. Measured: `bash -ic` captured 28 variables and
dropped `BUN_INSTALL` and `~/.bun/bin` from `PATH` (exported at
`~/.bash_profile:4`), which would make every spawn of an agent CLI installed that way
fail with `FileNotFoundError` - and the fallback would not rescue it, because the
capture *succeeded*, just impoverished. `bash -lic` captured 34, carried the proxy
settings and the bun path, and still showed neither `CLAUDE_CODE_SSE_PORT` nor
`NODE_OPTIONS`.

The trade is deliberate: a login-interactive shell runs more of the user's profile,
which is the whole point of capturing it.

`bash -ic` on a non-tty writes `cannot set terminal process group` and `no job
control in this shell` to stderr. Only stdout is parsed, so this is inert, but the
failure-path log must not let that noise crowd out a real diagnostic.

Two consequences of the minimal base, both accepted: a profile that depends on an
inherited variable behaves differently under the capture than in the user's own
terminal, and a provider key injected into `os.environ` at runtime is no longer
visible to a CLI subagent. The latter is tolerable because the CLI agents in the
preset set carry their own credentials (`claude` and `codex` use their own login,
`openclaw` its own auth store), and anything genuinely needed can be pinned in the
per-agent `env` field.

Because `login_shell_env()` is synchronous and `_exec` is async, the call site
wraps it in `asyncio.to_thread`: a profile that runs `nvm` or `conda init` costs
seconds, and a blocking call there would stall every gateway channel on the first
spawn.

Accepted cost: a variable injected ad hoc into raven's own launch
(`FOO=bar raven gateway`) is no longer visible to subagents. This is the intended
change, not a regression.

## 5. Presets

`raven/agent/subagent/presets.py` gains two entries and keeps three. `timeout`
stays unset on all five (`test_presets_have_no_timeout`).

### 5.1 openclaw

```
kind: cli                 idSource: provisioned
command:       openclaw agent --local --json --session-id {agent_id} -m {prompt}
resumeCommand: openclaw agent --local --json --session-id {agent_id} -m {prompt}
transcriptFormat: openclaw_json
```

Create and resume are deliberately identical.

### 5.2 hermes

```
kind: cli                 idSource: derived
command:       hermes --yolo chat -Q -q {prompt}
resumeCommand: hermes --yolo chat -Q -q {prompt} --resume {agent_id}
transcriptFormat: text
sessionIdPattern: session_id:\s*(\S+)
outputPattern: (?s)\A(.*?)\s*\Z
```

`--yolo` is required for the same reason `claude` needs `--permission-mode auto`
and `codex` needs `-a never`: a headless run must not block on an approval
prompt. `outputPattern` exists only to keep the stderr line out of the reply
(4.1), and carries a comment in `presets.py` saying so.

### 5.3 Preset caveats to surface

Both go in the preset `description` and in the page copy, because neither is
fixable from Raven:

- OpenClaw needs a supported Node on the login shell's `PATH`. 4.3 fixes the
  common case; a machine whose login shell also resolves an unsupported `node`
  needs a `PATH` entry in the per-agent `env` field.
- OpenClaw's `main` agent injects `~/.openclaw/workspace/{IDENTITY,SOUL,BOOTSTRAP,
  HEARTBEAT}.md`, and on a brand-new session spends the first turn on that
  bootstrap persona instead of the prompt: turn 1 of a fresh session answered
  "Hey. I just came online. Who am I? Who are you?", while turns 3 and 4 answered
  the prompt correctly. For one-shot dispatch this means the create call can
  return a greeting rather than a result. The clean fix is a dedicated agent id
  with an empty workspace (`openclaw agents add`), which is preparation outside
  Raven and so cannot ship inside a preset.

## 6. Frontend

### 6.1 Naming

Every user-visible English string becomes `Subagent`/`Subagents`, and the
"prototype" wording goes: `common.subagents`, `subagent-sidebar.{kicker,title,
subtitle,empty,newTitle,editTitle,selectHint,saved,nameTaken}`,
`tool.{callSubagent,subagentDag,subagentDagTitle_*}`,
`tool.summary.{subagent_*,dag_*}`, `messageBubble.hintSource.subagent_response`,
`subagent-monitor.empty`, plus the hardcoded `Failed to load sub-agents` in
`hooks/useRavenSubagents.ts:27`. `zh.json` has no English residue; `子代理`
stays. i18n keys and the directory name are untouched.

### 6.2 Page structure

Left list, `/credential`'s shape:

| Group | Contents |
|---|---|
| Configured (with count) | every entry in `subagents.thirdParty` |
| Presets | the built-ins not yet configured |
| Custom | two "new" rows: custom CLI, custom OpenAI-compatible |

`name` is the primary key, so a preset occupies its own name and an entry whose
name matches a preset name *is* that preset, configured. Custom entries may not
take a preset name - on creation *or* on rename - refused on save with a dedicated
`presetNameReserved` message. That is what makes presets single-instance without new
backend state.

Preset mode is tracked by an explicit `presetName` flag, never inferred from the
typed name: the Name input's placeholder is itself a preset name, so matching on it
flipped the pane mid-typing and hid the field the user was editing. The reservation
guard therefore keys on `presetName === null` (a genuinely custom entry) rather than
on the name alone - keying on the name refused every preset save, since a preset's
entry name is necessarily a preset name.

### 6.3 Form

Preset form: `name` and `kind` fixed; header shows icon, label, kind badge,
stateful badge, and the preset's description. Only what the preset needs is
outside the disclosure:

- `claude_code`, `codex`, `openclaw`, `hermes`: nothing required. Optional
  working directory, environment variables, timeout, "can read local files".
- `mirothinker`: API key required; base URL and model pre-filled.

"Required" here means save is blocked: today `canSubmit` for an openai-kind entry
checks only `baseUrl` and `model`, so a `mirothinker` row can be saved with the
empty `apiKey` the preset ships and then fails at dispatch. The rule becomes: an
openai-kind entry needs a non-empty key, unless one is already stored for that
name, which is the existing "leave blank to keep the stored key" case.

`command`, `resumeCommand`, `idSource`, `transcriptFormat`, and both regexes sit
in an `Advanced` disclosure, pre-filled and editable. They are editable rather
than hidden because a preset command assumes the CLI is on `PATH` with default
flags, and this page is the only place to correct a non-standard install; hiding
them would make a broken preset unfixable from the UI.

Custom form: today's full form for the chosen kind, `name` editable, same
`Advanced` grouping.

### 6.4 Files

`pages/subagent/index.tsx` splits into `index.tsx` (shell and left list),
`catalog.ts` (display metadata by preset name: label key, icon, which fields stay
outside `Advanced`), `form.ts` (`FormState`, `toForm`, `toEntry`, env helpers,
validation; no JSX), and `SubagentForm.tsx`. A new `SubagentIcon` mirrors
`ProviderIcon`.

A backend preset with no `catalog.ts` entry still renders, with its name as the
label and a neutral icon, so adding a preset in Python can never break the page.
This is the contract `ProviderIcon` already has with the provider registry.

## 7. Verification

- `uv run pytest tests/test_subagent_third_party.py tests/test_web_rpc_config.py tests/test_update_subagents.py -x`
- `test_presets_are_valid_and_complete` grows to the five-name set plus field
  assertions for the two new presets.
- New tests: session id recovered from stderr; `parse_openclaw_json` on a
  recorded payload; the login-shell env base, including the fallback path.
- `pnpm -C frontend lint` (0 errors), `pnpm -C frontend build`, `make lint`,
  `make check-large-files`.
- End to end: `./start_webapp.sh`, configure each of the five presets from
  `/subagents`, dispatch to each, and confirm `~/.raven/config.json`.

## 8. Risks

- 4.3 changes the environment of every existing configured CLI subagent, not
  just the new presets. Rollback is the one-line revert to `os.environ`.
- 4.1 changes derived-id extraction for every CLI preset. `codex` is unaffected
  because it takes its id from the JSONL parser before the regex branch runs.
- OpenClaw's bootstrap-persona turn (5.3) is not solved by this change, only
  documented.
- The Hermes `outputPattern` (5.2) selects stdout, so a run that exits 0 with
  empty stdout returns an empty reply instead of falling back to `combined`,
  which would at least have carried the stderr diagnostics. Accepted: the
  alternative leaks the `session_id` line into every normal reply. The stderr is
  still in the gateway log.

## 9. Out of scope, worth recording

`.hermes/` appeared untracked in the repo root during this work and is not
covered by `.gitignore`. It must never reach `main`. Not changed here.
