# Changelog

All notable changes to Raven are documented here.

## Unreleased

### Changed

- `setup.status` now reads the config file `RAVEN_HOME` points at, like every
  other reader; it used to answer for `~/.raven/config.json` regardless.
- `raven channels *` and the gateway no longer warn that `channels.sendProgress`
  / `channels.sendToolHints` "is not a table": section-wide settings are not
  channels whose cargo failed to parse.
- The bundled EverOS plugin declares the keys the onboarding wizard records
  (`root`, `owned`, `agent_id`, `user_id`), so a boot no longer warns about
  each of them; an invalid type now fails loudly at activation instead.
- A context builder that runs after the prefix is assembled (the Curator)
  now degrades like the others: its segment is dropped and named, the turn
  runs on. Chat channels render that one notice as its own short line, so an
  answer produced without long-term memory says so.
- `raven sentinel tick` builds the same Sentinel stack the gateway runs
  (shared state store, pending decisions, routines), so a CLI tick reads and
  writes the quotas a live gateway would instead of a private copy.
- **Architecture, v0.2.0.** The runtime is now layered by binding time and the
  boundaries are machine-enforced: a frozen kernel (`raven/spine`), the papers
  (`raven/contracts`, every interface a shelf implements), the assembly root
  (`raven/core`, where config becomes a running agent through one door,
  `build_runtime`), the shelves (channels, plugins, providers, memory, ...),
  and the entrances (`cli`, `rpc`, `acp`). Five import-linter contracts run
  in CI: inner layers never import an entrance (with no allowlisted
  exceptions), the twelve channel adapters are mutually independent, the
  kernel imports nothing else at module level (three lazy tracing reads of the
  host are named and may only shrink), the cargo under `raven/agent` never
  imports the loop shell it is consumed by, and the runtime never imports the
  repo-level `evolver/` tool that drives it. The papers hold shapes only (machinery such as provider retry
  and tool-argument validation lives with the code that runs it, and a ledger
  test keeps it there). `CONTEXT.md` records every package's seat.
- Audit pass six: the context engine's own prose describes the engine that
  is there -- one assembler over segment builders (not three lanes and a
  retired ContextBuilder), the third router source numbered third, the
  Curator's working state rendered by its own builder.
- Audit pass five: five channel adapters wrote their description after a
  statement, so it was an expression and the class had no docstring; the
  adapters' retry comments name the delivery hub that does the retrying;
  three empty `TYPE_CHECKING` guards, a commented-out table column and a
  comment about another module's error contract are gone; the benchmark
  cache, the sandbox error and the usage tracker say what they do.
- Audit pass four: eight more symbols with no reader are gone -- the fd-level
  terminal redirect, the standalone LLM trigger expansion (the generator's own
  tool call and the shared guard remain), an ISO timestamp helper, two MCP
  inventory views, the recorded-server stop path, a duplicated JSON parser, a
  method alias, and a 97 KB routing sample no code reads.
- `raven.i18n` is seated as an inner cross-cutting leaf: it may not import a
  surface, and the kernel may not import it. The glossary records what it is
  and what `zh_lexicon` is not.
- The sentinel planner's and the daily planner's system prompts are
  per-language templates (`raven/templates/prompts/{en,zh}/`) loaded by
  `raven.i18n.prompt`; their context blocks render through `t()`. An `en`
  user's sentinel used to be prompted in Chinese.
- Security: the three URL policies (egress fetch, market trust, browser
  navigation) read one address vocabulary, `raven/security/hosts.py`
  (canonical and browser-legacy spellings, UTS-46 mapping, IPv4 embedded in
  IPv6, the names that mean this machine); the market's public-address check
  and the browser's link-local check see through every spelling now.
- Security: a base URL or redirect host the WeChat login exchange hands back
  is adopted only over https and within the configured operator's domain;
  a Discord `resume_gateway_url` outside the configured gateway's operator
  is ignored in favour of the configured gateway. Both carry the bot token.
- Audit pass over eval_engine, knowledge, trajectory and config: the names
  other packages reached under an underscore are public
  (`SessionManager.session_path`, `update_providers.oauth_credentials_present`,
  `schema.section_has_credentials`), unread symbols are gone
  (`section_key`, the tool-safety prompt stub,
  two dead config paths, a no-op validator), and docstrings describe the
  present shape instead of the change that produced it.
- The evolver moves out of the `raven` package to the repo-level `evolver/`
  tool (`python -m evolver run --config <yaml>`): it drives raven as a
  library, ships in no wheel, and a fifth import-linter contract keeps the
  runtime from importing it back.
- Audit pass three: the `raven.auth` placeholders (`capability_token`,
  `managed_settings`), the MCP OAuth `set_callback_base` hook, three ops
  connection helpers and an unread tool table are removed with their tests;
  two PLAN.md files leave the package.
- Security: a market catalogue entry may name a remote MCP server only at a
  public https address (a local or private one is refused before it is
  written to the config); a redirect hop whose host does not resolve is
  refused instead of waved through; an input image the model names by local
  path honours `tools.restrictToWorkspace` like every file read.
- The sentinel's replies and menus, the TUI launcher's errors, the WeChat
  quote marker and the importers' preambles render in the user's language
  through `raven.i18n` (English by default, Chinese when `language` is
  `zh`); every `raven` command sets the language from the saved config.
- The attention.md daily fire plan section is headed `## Today's fire plan`;
  files written with the Chinese heading keep parsing through the legacy
  alias table, which now lives with the other Chinese language data in
  `raven/i18n/zh_lexicon.py` (cue words, punctuation classes, date counters).
- `raven.i18n` translates user-facing text by its English source (`t("Back")`),
  with the Chinese catalog in `raven/i18n/zh.py`; the onboarding wizard and
  the CLI screens that carried `(en, zh)` pairs speak through it, and a test
  keeps Chinese literals out of every other module (the files still carrying
  them are listed, and the list only shrinks).
- `AgentLoop` takes its five wiring bundles and nothing else: the flat
  keyword shim that folded legacy names into them (used by tests only) is
  gone, and the tests construct the bundles they mean.
- `raven.memory_engine` is a package face: the store, the consolidator, the
  skill catalog and router, the attention and behaviors parsers resolve as
  names on the package (lazily), and nothing outside the engine imports its
  submodules; a test keeps it so.
- Modules that only type against the provider shapes (`LLMProvider`,
  `LLMResponse`, `ToolCallRequest`, ...) import them from the paper,
  `raven.contracts.llm_provider`; `raven.providers.base` is imported by the
  adapters and fakes that subclass its machinery.
- `settings.everos_set` is the RPC method's name; `settings.everosSet` stays
  registered and declared (marked deprecated in the OpenRPC document) until
  the TUI reads the new one. The `-32012` error class is `NotSupportedError`;
  its wire message keeps the `not_supported_in_v01` spelling for the same
  reason.
- `RpcServer` takes the connected socket and nothing else: the POSIX pipe
  path (`request_fd` / `notify_fd`, kept for a demo runner that no longer
  exists) is gone with the workaround comments it needed, and the tests
  hand the accepted connection over the way `raven tui` does.
- The three asking capabilities (`QuestionResponder`, `ApprovalResponder`,
  `Asker`) are papers in `raven/contracts/asking.py`; the tools and the ACP
  client type against them from there.
- Config version floor 4 retires three legacy leaves in the file instead of
  in the schema: `skillForge.skillsDir` becomes the first `skillForge.localDirs`
  entry, `skillForge.massLibraryDb` and `context.engine` are removed, each
  with a notice on the first load. The model validators and the
  DeprecationWarning that used to paper over them are gone.
- The nine terminal-dialect RPC methods that had handlers but no contract
  entry (`clipboard.paste`, `command.dispatch`, `delegation.status/pause`,
  `input.detect_drop`, `session.interrupt`, `shell.exec`, `skills.manage`,
  `subagent.interrupt`) are declared in `rpc-schema/openrpc.json` and
  `METHOD_MODELS`; the registration guard's inherited allowlist is empty.
- The Eval Engine has a config table (`evalEngine`, off by default) and
  `build_runtime` mounts its three hooks when it is on; before, the engine
  and its stack builder existed but nothing assembled them.
- Config-slice admission (`admit_slice`, `dispense_channel_config`) lives in
  `raven/config/admission.py`: the door validates config against a cargo
  declaration, which is config vocabulary, so the channel commands, the
  gateway manager, the trajectory redactor and the plugin registry reach it
  without importing the assembly root.
- **Generations.** The gateway rebuilds its runtime from config and swaps it in
  at the loop's turn boundary instead of restarting: build the candidate first,
  stop the serving generation, dispose it in a pinned order. Channels, cron,
  the sentinel and the control plane survive the swap.
- **Channel config lives with the adapter.** Each adapter's `spec.py` declares
  its fields, defaults, secrecy and nesting; the twelve central per-channel
  config classes are gone, `channels.<name>` sections are dynamic, and
  `raven channels set/show` validate through the same admission door the
  gateway dispenses through. Existing config files load unchanged.
- The gateway's web channel is retired and its endpoint becomes the gateway
  **control plane** (`ws://127.0.0.1:<port>/ws`, loopback, per-boot token
  published in the gateway lock; for this one release the lock also carries the
  older `web_*` spelling of that address, so a reader from the previous release
  still finds it): six methods only -- `gateway.channels.live`,
  `.qr`, `.start`, `gateway.status`, `gateway.reload`, `gateway.shutdown`.
  `raven gateway reload|status|stop` drive it from the CLI; `reload` rebuilds the
  runtime from config and swaps it in without a restart (SIGHUP stays as the
  POSIX alias of `reload --force`). The `gateway.web` config table is removed on load with a
  notice: proactive replies and heartbeat output that `web.enabled: true` used to
  send to the web channel (which had no clients) now reach your IM channels and
  the page. The control port no longer serves deliverable downloads; those routes
  stay on the page transport behind its session guard.

### Added

- `spawn` now records every sub-agent call on disk, the way `run_subagent_dag`
  already recorded every node: one directory per call under
  `<agent home>/sessions/<group>/<chat_id>/subagents/spawn/`, holding
  the prompt, the result (or the error), and a small `meta.json`. Previously a
  `spawn` left no file at all -- its reply went straight into the conversation
  and nothing else -- so the two ways of delegating are now equally
  inspectable. Failed, aborted and cancelled calls are recorded too, and the
  prompt is written before dispatch, so a call that never returns still shows
  what was asked. The directory is append-only: there is no expiry or size cap,
  and reclaiming it means deleting the session, which removes its metadata
  directory too. DAG runs dominate the volume, because a node's rendered prompt
  inlines its dependencies' full output.
- A `run_subagent_dag` graph can read an earlier run's output from the same
  conversation. A completed node is named by its id alone (`{{ <id>.output }}` /
  `{{ <id>.output_path }}`, or an `inputs` entry of the form `{"node": "<id>"}`), needing no
  `depends_on` -- there is nothing left to order. `depends_on` may name it anyway, which
  records the dependency without ordering anything. Previously nothing carried across runs:
  the outputs sit under the session's metadata directory, which no working directory can
  be aimed at, so no relative path reached them. A node with no output to read --
  failed, skipped, cancelled, or in a run still in flight -- keeps its id but is
  refused, naming which case it is. Node ids are unique per conversation for this to
  work (see Breaking Changes).
- A DAG file reference (`{{ ref: }}` / `{{ ref_path: }}` / an `inputs` `{"file": ...}`)
  resolves inside two roots now, the session working directory and this conversation's
  sub-agent history (`<session_dir>/subagents/`, so the `spawn` records beside the DAG runs
  are reachable too), and `@runs/<run_id>/...` addresses the run history by path -- which is
  what reaches a file that is not a node output, or a run recorded before node ids were
  indexed. The second root stops at that directory rather than at agent home: agent home
  also holds user memory, installed skills, and every *other* conversation's transcript and
  sub-agent history, which `workdir.py` already keeps off the agent's file surface, and a
  DAG graph is LLM-authored and auto-run.
  Every `_path` form is now checked to exist before its sub-agent is dispatched.
- A DAG run stopped by `/stop` or a gateway shutdown now records each unfinished node by
  the stage it was in: one still running when the stop arrived is recorded `cancelled`, and
  one still pending stays `skipped`, in the session index on the way out. Those routes
  cancel the run task, which skipped the finalizer, so the ids the run had claimed stayed
  readable as "still being written" forever -- a later graph could then neither reuse them
  nor read them, and the two refusals it got contradicted each other. The id-reuse refusal
  also stops advising a reference to a node that has no output to reference.
- Sessions started by `raven tui` / `raven agent` record the directory they were
  launched in as `Session.metadata["project_dir"]`. The group directory name is
  a lossy slug, so this is what identifies the project.
- A DAG node stopped mid-flight is now recorded `cancelled` rather than `skipped`, in the
  session index, the manifest, and both the TUI and web frontends, and it stays on the
  instance list, where a `skipped` node is filtered out on the grounds that it has no
  transcript, no cost, and no clock -- untrue of a node that ran.
- Cancelling a sub-agent on the ACP transport now actually stops the turn on the agent:
  raven sends the protocol's `session/cancel` and waits up to 5 seconds for
  `stopReason: "cancelled"`. Previously it only abandoned its own wait, so the agent ran
  the turn to completion and answered a request nobody was listening for -- and for a
  stateful agent the half-finished turn stayed in its session. If the wait expires the
  session binding is dropped, so the next dispatch opens a fresh session instead of
  colliding with a turn that is still running.
- The gateway now closes the ACP connection pool on the way out. Those adapter servers
  are launched with `start_new_session=True` and receive none of the gateway's signals,
  so every gateway exit used to orphan them.
- The wheel now carries the sub-agent tree, so a `pip` / `uv tool install` has the
  four agents without a source checkout. `subagents/` is force-included file by file
  from git's index rather than as a directory: hatchling applies no exclude config to
  a force-included path, and a built working copy holds each fork's filled-in `.env`
  next to its source. That adds 67 MiB to the download and 106 MiB unpacked, half of
  it the PPT templates. On first run the tree is copied -- not moved -- to
  `<raven home>/subagents`, once per raven version, where an upgrade cannot take the
  built venvs with it. The copy under site-packages stays, because the installer owns
  it and it is what resolution falls back to when the copy-out fails, so an install
  holds the tree twice: about 212 MiB across the two. There is no expiry or size cap,
  and deleting the raven home copy reclaims half; the rest goes when raven itself is
  uninstalled. An editable install is skipped -- it reads the tree beside the package
  already. This replaces
  the beta-only path, where `make beta` staged the tree and injected a force-include
  line into a temporary `pyproject.toml` so the agents reached testers and no one
  else; both mechanisms at once made every wheel build fail on a duplicate archive
  path, and the tree now reaches stable releases and `git+` installs as well.
- Setup now offers the sub-agents that ship with raven. Step 6 of the wizard
  lists each folder under `subagents/` and asks whether to run it on the model it is
  tuned for, on this raven's LLM, or not at all, then writes the roster entries. The
  tuned model leads the menu because it is the one a key of its own buys: inheritance
  copies the host's `agents.defaults.model` too, so a folder without a key stops running
  the model it was built around. All three are tuned for models served through
  OpenRouter, so a raven that already has an OpenRouter key is not asked for a second
  copy of it - and that reuse reads `providers.openrouter` alone, never a key parked in
  `custom`, which belongs to whichever private gateway that section names. It replaces the
  deep_research step, which is unchanged and still reachable through
  `raven deep-research enable`. Previously `subagents/install.sh` did the registering,
  which could not work on a first install: it runs before `~/.raven/config.json` exists,
  read that file to decide whether an agent had an LLM to fall back on, and so declined
  to register every folder on exactly the machines that had just been set up. It now
  builds the venvs and stops there, and its `--config` flag is gone with the write it
  fed. A folder whose venv is not built is not offered: a name in the roster that fails
  the moment the model picks it is worse than an absent one.

- A delegated run's record now keeps the tool name the transport itself used, and the
  mapping into Raven's own names (`exec`, `read_file`, ...) happens when those rows go to
  a client rather than when they are written. A record that stored `exec` could never be
  read back for whether the agent ran a shell command or wrote a todo list, and the record
  is what a later reader has. Every front end keeps the one vocabulary its verb table is
  keyed by, so nothing a user sees changes, with two deliberate exceptions: a
  claude-agent-acp `Grep` carrying both a pattern and a path scope used to arrive with the
  pattern destroyed and now arrives whole, and a claude tool outside the twelve the old
  table listed -- `TodoWrite`, `ExitPlanMode`, `MultiEdit`, `SlashCommand`, every `mcp__*`
  tool -- used to be reported as `tool_call` or as `exec` and now arrives under its own
  name. The second cannot be mapped back, because the coarse `kind` it was derived from is
  not stored; a todo-list write labelled `exec` was wrong, and an unlisted name is rendered
  from the name itself.

- An `openai`-backed sub-agent's conversation now holds the middle of its run, not just the
  question and the answer. An endpoint that reports its own steps -- a deep-research model
  sending `reasoning_steps` -- has those searches, fetches and thoughts written into the
  instance log as tool calls and their results, and its token cost recorded, where before
  the backend published none of it and a call left two rows and no cost. Measured on one
  captured research call: two rows became nine. Each step keeps the endpoint's own name for
  what it did and its own argument keys, so `fetch_url_content` is not reported as Raven's
  `web_fetch` -- it fetches and then runs an extraction, which is a different tool. Buffered
  and streamed responses differ in shape, a streamed thought arriving as token fragments,
  and both produce the same rows; a streamed run republishes on every step, so a panel
  watching it sees the run progress rather than only its result.

- `spawn` now takes a `prompt_template` and the same placeholder grammar `run_subagent_dag`
  already had: `{{ ref:<path> }}` / `{{ ref_path:<path> }}` inline a file's contents or its
  path, and an `inputs` mapping fills `{{ inputs.<key> }}` / `{{ inputs.<key>.path }}`. Paths
  resolve inside the same two roots as a DAG node's -- the working directory and this
  conversation's sub-agent history (`<session_dir>/subagents/`) -- so an earlier spawn's own
  `Record:` directory is reachable without restating its output, and the `_path` forms are
  refused for a sub-agent the roster tags `[no-local-files]`. The parameter was `task`, a
  literal string with no such grammar; renamed to `prompt_template` (see Breaking Changes).
  The completion announcement always shows the template as written, never the rendered prompt;
  the `Record:` line beside it only names where the rendered version, with any `{{ ref: }}`
  file inlined, is written on disk.
- A prompt template's `{{ ... }}` text that matches none of the six placeholder shapes
  (`ref`, `ref_path`, `input`/`inputs.<key>`, `input_path`, `output`, `output_path`) is
  ordinary text now, on both `spawn` and `run_subagent_dag`. A stray `{{ }}` in an example, or
  a note that happens to use double braces, used to be refused outright as `unrecognized
  placeholder '...'`; the grammar now only claims a body actually shaped like one of the six,
  and leaves everything else untouched.

### Fixed

- An `inputs.<key>` placeholder naming a key the template's `inputs` never defined rendered
  the literal text `None` into the sub-agent's prompt -- indistinguishable from a real answer
  -- instead of being refused as the typo it almost always is. Fixed on both
  `run_subagent_dag` and `spawn`; an explicitly null input (`{"key": null}`) is refused the
  same way, since it carries no more of a usable value than an absent one.
- A file's contents reached a sub-agent's prompt unfenced whenever the file sat outside the
  sub-agent history root -- which is most files, and includes a repository someone checked
  out into the working directory. The fence now follows the kind of reference instead of the
  directory the file was found in: a `{{ ref: }}` or file-shaped `inputs.<key>` read is
  wrapped as `file` wherever it was read from, and another node's output as `subagent`. A
  literal `inputs.<key>` string stays verbatim, because the author typed it into the call,
  and so does every `_path` form, which carries a path rather than any text to fence.
- A `spawn` call refused before dispatch -- a stateless sub-agent given an `instance` handle,
  an empty `prompt_template`, or a rejected file reference -- could leave a previous call's
  completion metadata sitting uncollected if that call's `instance` handle was never picked up
  (no tool-event sink listening on the channel), and have a later, unrelated call report the
  stale handle as its own. The handle is now cleared at the top of every call, ahead of every
  refusal.

### Breaking Changes

- An `inputs` key that no placeholder in the `prompt_template` references now refuses the
  call, on both `spawn` and `run_subagent_dag`, before any file is read or any node is
  dispatched. Material reaches a sub-agent only where a placeholder puts it -- nothing is
  prepended and nothing is added around a substituted value -- so a declared key with no
  placeholder was material that silently did not arrive, and the run still read as
  finished. A call that passed an unused key has to drop it or reference it. An `inputs`
  entry that is an object naming neither a file nor a node is refused for the same reason:
  it used to render as its own Python repr.

- `raven onboard --skip-deep-research` is now `--skip-subagents`, because step 6 is the
  sub-agent step. Typer rejects an unknown option, so a script or CI job passing the old
  name exits 2 with `No such option` rather than skipping anything.

- A `run_subagent_dag` node id must now be unique across the whole conversation, not
  just within its own graph, and a graph that reuses one an earlier run took is refused
  before any node is dispatched. That is what makes an id an address: a later graph reads
  an earlier run's node with `{{ <id>.output }}` and no `depends_on`. Graphs that reused
  a generic id (`plan`, `step1`) across runs in one conversation used to run and now need
  a fresh id each time; the refusal says which run holds the id and offers referencing it
  instead.
- DAG run directories moved from `<workdir>/.ravenx_dag/<run_id>/` to
  `<agent home>/sessions/<group>/<chat_id>/subagents/mas_dag/<run_id>/`,
  and the per-session `index.json` moved with them. The history is keyed on the
  session now rather than on whichever directory the run happened to use, so
  repointing a session's working directory no longer orphans the runs already
  recorded. Existing `.ravenx_dag/` directories are neither migrated nor read;
  move them by hand to keep them visible in the UI.
- `raven agent -w <dir>` no longer starts an isolated agent instance rooted at
  `<dir>`. `-w`/`--workspace` now sets the working directory a turn reads and
  writes files in (defaulting to the process launch directory); use the new
  `--home <dir>` for the old "isolated instance" meaning (agent home: memory,
  skills, transcripts). A script that passed an absolute path to `-w` for
  isolation will keep running but silently share the default agent home
  instead. One that passed a relative path (`-w myproject`, resolved against
  the current directory) now fails outright: a working directory must be
  absolute, so the run exits with a parameter error. Both cases are fixed by
  switching the flag to `--home`.
- `raven tui` and `raven agent` now default their working directory to the
  process launch directory instead of agent home.
- `raven gateway` now gives each *channel* its own working directory instead of
  running every conversation in the agent-home root. Set it per channel with
  `channels.<name>.workspace`,
  configured alongside that channel's credentials; unset means
  `~/.raven/tmp/<channel>`. Existing files already at the agent-home root are
  left in place. A single session can still be pinned elsewhere from the web
  UI, taking effect on the next turn.
- Session transcripts on `raven tui` and `raven agent` moved from
  `sessions/<channel>/<id>.jsonl` to `sessions/<project slug>/<id>.jsonl`,
  where the slug is the launch directory flattened the way Claude Code names
  `~/.claude/projects/` entries -- so a project's conversations stay together
  instead of piling up under `cli/` and `tui/`. Gateway transcripts keep the
  channel as their directory. Sessions written before this keep their existing
  file and still resume by id; nothing is moved.
- Each session now also has a metadata directory beside its transcript,
  `sessions/<group>/<id>/`, holding the sub-agent call history (`subagents/`).
- Shadow-git checkpoints now follow the working directory instead of always
  snapshotting agent home, so launching the agent inside a real project
  directory checkpoints that project. On `raven gateway` the same rule gives
  every channel its own shadow repository inside its own working directory,
  replacing the single one that used to cover all of agent home (session
  transcripts and user memory included). Because the working directory is now
  wherever you launched from, checkpointing refuses a root that is your home
  directory or an ancestor of it -- snapshotting a whole home every turn copies
  whatever you keep there into a repository that retains history, and an
  interrupted turn edits a project rather than a home. Run from a project
  directory, or set `runtime.checkpoint.policy` to `never`. Private keys are
  excluded everywhere else: `*.key` and `*.pem` never matched `id_ed25519`.
- `raven agent --continue` and the TUI's resume-most-recent now look only at the
  project you launched in, instead of the newest session on that channel
  anywhere. Resuming across projects also corrupted what it resumed, by
  appending this project's turns to a transcript filed under the other one.
  Sessions written before project grouping carry no project attribution and
  stay reachable from any directory. Cron and sentinel delivery are unchanged:
  they still forward to whichever session is live, wherever it started.
- A working directory may no longer be agent home itself, nor any directory
  containing it, on the CLI flag or the per-session override. Working at agent
  home puts `user_memory/` and `skills/` one relative path away from every
  write the agent makes; working above it (`~/.raven`, `~`) additionally puts
  `config.json` and `oauth/` inside the per-turn shadow-git snapshot. Use a
  subdirectory.
- `spawn`'s `task` parameter is renamed `prompt_template`, matching the field name
  `run_subagent_dag` already used for a node's own prompt -- a literal string still works
  exactly as before, and the field additionally accepts the placeholder grammar described
  above. `task` still works from a call that bypasses the tool registry (a direct or
  programmatic call, including this repo's own tests): the registry rejects a model call
  missing a schema's `required` parameter before `spawn` runs, so the old spelling only ever
  rescues a caller that skips that validation, and a call sending both spellings has
  `prompt_template` win. The TUI's and the web UI's transcript views read `prompt_template`
  first and fall back to `task`, so a transcript recorded before this rename keeps labeling
  its delegation rows correctly.

## v0.1.0 - Public Preview - 2026-06-30

Raven is now available as an Apache-2.0 open-source project.

This is the first public preview release: stable enough for developers to try,
inspect, and build around, while the CLI surface, plugin contracts, and runtime
internals may still evolve before v1.0.

### Highlights

- AI-native command line agent built for terminal-first workflows.
- Memory-first runtime with context assembly, routing, and session management.
- Proactive execution path for scheduled, event-driven, and background work.
- Native TUI and bridge packages aligned under the v0.1.0 release line.
- Skill and plugin foundations for extensible agent behavior.
- Provider routing and fallback paths for multiple model backends.
- Public repository hygiene: CI, commit linting, pre-commit checks, issue
  templates, security policy, contribution guide, and Apache-2.0 licensing.

### Installation

```bash
curl -fsSL https://raven.evermind.ai/install.sh | bash
```

### Release Status

- Version: `0.1.0`
- Tag: `v0.1.0`
- Stability: public preview
- License: Apache-2.0

### Notes

- Raven is not yet API stable.
- The public install endpoint and package distribution flow should be verified
  before publishing the GitHub Release.
- Future releases should use semantic versioning: patch releases for fixes,
  minor releases for new capabilities, and v1.0 once the public CLI and plugin
  contracts are stable.
