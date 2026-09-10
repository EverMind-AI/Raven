# Changelog

All notable changes to Raven are documented here.

## Unreleased

### Added

- Two dark templates join the bundled catalogue, cut from user uploads: a
  near-black circuit-board deck for product launches and a green aurora deck
  for trend reports. Hidden vendor pages are gone, vendor marks are stripped,
  and the roles each one lacked are borrowed from the light templates onto its
  own dark masters. The template-selection prompt no longer refuses a dark
  style outright: it takes one when the request asks for dark, black, night or
  a technology register.

### Changed

- The Raven-PPT launcher sizes the run's context window from the endpoint
  that will serve the model instead of a shipped number capped by the host
  catalog: OpenRouter is asked for the providers serving the model and the
  smallest window among those the request may reach wins (an `only` list or
  an `order` with fallbacks off fences the request; an `order` with fallbacks
  on does not, so every serving endpoint counts), a vLLM-style server answers
  with its `max_model_len`. The shipped glm-5.3-flash row now fences the run
  to Z.AI, DeepInfra and Novita (`allow_fallbacks: false`), sized at their
  1,048,576 and never routed to the 262,144-token fp8 host; a call finding all
  three unavailable fails over the retry ladder instead of to that host. When the endpoint does not answer, LiteLLM's table
  and then the host catalog are asked, but a catalog may only lower the number
  in `config.json`, never raise it. `PPT_CONTEXT_WINDOW` pins it by hand.

### Added

- Raven-PPT runs with the host's context compaction on (`compaction.enabled`,
  `triggerRatio` 0.85, every other threshold the host's default) and the
  ppt-engine plugin appends a deck ledger under each compaction summary: the
  user's own messages and `ask_user` answers verbatim (journaled as the running
  turn sees them, since the filed record ends with the previous turn), and the deck's state as
  the tools left it on disk (brief, template, sources, figure catalogue, outline
  pages, the reader's open findings, published and refused records). Replayed on
  a recorded 34-page deck session, the host's summary alone lost the page limit,
  the language and style requirements, both `ask_user` answers and the source
  path; the ledger carries them.

- Raven-PPT declares the host's three effort tiers: `medium` runs the author at
  low reasoning effort and caps a deck at ten whole-deck builds and three
  readings, `high` (the default) keeps the effort and the caps, `max` is the
  uncapped run the product shipped with. At the build cap the deck is delivered
  as it stands and the reply names what would have held it back; at the reading
  cap the second reader stops. The tier reaches the deck tools through
  `.deck-mode.json` in the session's deck folder, written by the plugin hook
  from the session's mode overlay.

### Fixed

- Four kinds of ppt-engine finding that six audited deck runs showed to churn
  without changing the deck are no longer repeated: findings about a page the
  runner stood in for (besides the failure itself), `repeated_layout` on
  adjacent pages the outline gives one prototype or layout, `prototype_kept`
  after its first report per page, and the whitespace gates when the brief
  asks for air. The second reader's `type`, `alignment` and `listed` entries
  stay in the reading's own reply instead of the ledger, a draft shorter than
  the outline is not held to the outline's claims, and a reading whose quoted
  headline is not on the page it names is dropped.

### Removed

- **The vendored subagents tree.** The five product forks under `subagents/`
  leave the repository: the products live under `agents/` as thin launchers on
  the installed raven, a wheel carries that tree and copies it out to the raven
  home on first use, and the design/ppt engines ship as their own wheels. A
  stored roster row written against the old tree migrates on load (config
  floor six): paths re-aim at `agents/`, a fork-venv interpreter becomes the
  running one, and a fork-era row that cannot be re-aimed is left as written
  with a notice to re-run onboarding. The forks' record stays reachable as byte
  snapshots under `tests/fixtures/vendored_fork/` and as full trees in git
  history; leftover copies under `<raven home>/subagents` can be deleted.
  The research product reclaims the fork's display name: registered
  `Raven-Research-NG` rows rename to `Raven-Research` on load (config floor
  seven); if a fork-era row still holds the name, an advisory repeats until
  that row is removed and the rename completes on a later load. The machine
  id `raven-research-ng` (state root, ACP home, everos identity, the
  `RESEARCH_NG_*` variables) is unchanged. References stored during the
  unreleased pilot window (instance bindings, direct chats under the
  transition name) are deliberately not carried: no shipped artifact knows
  that name, and addressing one answers with the explicit not-configured
  refusal.

### Added

- A slide deck opens in the WebUI file viewer as a PDF. Clicking a `.pptx`
  asks the file route for `render=pdf`: the deck's own published `<stem>.pdf`
  is served when it is beside the deck and current, otherwise LibreOffice
  renders one into a cache under raven's state directory keyed by path, size
  and mtime, one render at a time per deck. The viewer says it is rendering
  until the frame loads, offers the PDF in a tab and the deck itself as a
  download, and falls back to the open-with note with the gateway's words when
  the host has no LibreOffice, the render times out, or nothing is produced.
- The research agent's three modes are three stop rules rather than three sizes
  of one budget. `medium` may answer a settled general-knowledge question
  without searching: the first model call has the web tools withheld and a
  `request_research` tool offered, a plain draft is put to an independent judge,
  and an accepted one ships saying no sources were consulted (`plainFirst`,
  medium and high). `high` terminates on the reviewer, with three revisions and an
  evidence round on a rejection. `max` adds an evidence floor: a draft resting on
  fewer than 18 readable pages from 8 sites is sent back to research, at most
  twice (`evidenceFloor`). The reviewer asks for its verdict as a forced tool
  call, runs one uninterrupted attempt with 16384 tokens, and records why it
  failed open. The sufficiency judge can release a turn from the search listing
  before any page is opened (`sufficiency.judgeListing`). The product label
  moves to `dr@3.7-filetools-askuser-derive-numeric-cite-rank-tiers-plain-high`.

- `web_search` and `web_fetch` route through a vendor the deployment picks:
  Serper, AnySearch, SerpApi, Tavily, Exa, Brave Search or Firecrawl for
  search, Jina Reader, AnySearch, Tavily, Exa or Firecrawl for pages. Keys are
  held once per vendor under `tools.web.providers.<vendor>.apiKey`, so a
  vendor that serves both tools is pasted once; the pre-vendor
  `tools.web.search.apiKey` and `tools.web.jinaApiKey` still count. The
  onboarding wizard's web step asks for the vendor before the key (also
  `--search-provider` / `--fetch-provider` / `--search-api-key` /
  `--fetch-api-key`), the settings page and `settings.set` accept the new
  keys, `raven doctor` names the selected vendor's slot, and `~/.raven/env`
  mirrors every vendor key. A page reader selected without its key falls back
  to Jina, and the log says so. The vendored research checkout's launcher
  inherits the host's vendor choice and keys when its own config names none and
  the key resolves for it; the in-repo research plugin keeps its own separate
  search key.

### Changed

- The research agent's sufficiency judge no longer passes a transport timeout
  the provider rejects; before this every judge call failed open before it was
  sent, so `medium` never actually stopped early.

- The ppt engine (`plugins-dist/ppt-engine`) carries the fork's layout work:
  eight bundled templates instead of twelve (four dropped for looks), a curated
  set of reference pages any deck may borrow across templates through
  `adapt(prs, prototype(bundled('<template>'), N), ...)`, `add_unit` /
  `remove_unit` / `clone_shape` / `swap_icon` for growing, shrinking and
  re-drawing a template's repeated units, fill in reading order with the
  numbered-tile rule, and measurements that read a tinted card as a card
  (`is_filled`) and report a body nothing divides (`undivided_body`). A build
  that succeeds still hands its stderr back as warnings, and a shape placed at
  more than twice its box's proportion warns instead of refusing. `backdrop`
  lays a generated picture behind everything on a page, cover-cropped and
  washed to an `alpha` of its own, and `layout_pictures` reaches the
  photographs a template keeps on its layouts -- which `ppt_template` now
  lists and a new `layout_picture` warning names, since `pictures={...}` on a
  cloned page never touched them.
- A card's plane is now told apart from its ground by colour difference rather
  than luminance ratio, and pushed towards the accent until it is (the beige
  template's cards sat at a dE of 7 on its own ground and were cards only to the
  file); the soft tile is deepened so it still leads the plane. A picture frame
  with its own outline is filled with `cover` instead of shrunk inside it. A
  tinted-header table's emphasised row takes a tint a step deeper than the
  header's, and every mark a table says as a glyph is set in one symbol face at
  1.3x the copy, so a scale's full and half steps come out one size. The image
  tool asks for a photographic manner for any subject with a face in the world
  and the template's manner only for a concept.
- The deck engine's replies to the author are slimmer: `ppt_template` states a
  reply budget instead of letting the host cut a five-page reference in half
  (pages that do not fit are named in `pages_not_read`), folds a run of
  custom-drawn decoration into one line (a Bauhaus contents page went from
  12,800 to 4,000 characters), states the imports once per reply and a run's
  type on one line; `ppt_build` repeats a page's plan only when that page has
  something to fix.
- A model call that fails with a retryable error after the provider's own
  seconds-long ladder now waits out a second, longer one
  (`agents.defaults.llmErrorRetryDelays`, default 15/30/60s) before the turn
  is given up; a body that is not JSON and a wording no bucket names are both
  retried instead of ending the turn, and an image the endpoint refuses for its
  size -- or a text-only model refuses at all -- is taken out of the
  conversation and the call asked again. The streaming path, which every ACP
  hosting uses, waits out the same ladder after its one reconnect instead of
  failing the turn on the second error. An unattended run may also ask, with
  `agents.defaults.llmRetryAfterOutput`, for a streamed call that failed after
  it had produced output to be asked again (the deck launcher does); off, the
  turn fails as before.
- The deck engine's second reader is bounded: one reading stops at 240 s and
  names the pages it did not get to, a deck's readings together stop at 15 min,
  a reading is recorded at the version of the pixels it read (the build's own
  render of the page) rather than the code that drew it, so a revision re-reads
  only the pages whose render changed, the reader answers in one call with room
  for a reasoning model's thinking (no doubled-budget retry), and it reads the
  build's renders instead of rendering the deck again. Measured on a 20-page
  run, 18 whole-deck builds had each re-read the pages the last revision
  touched, 136 of the run's 327 minutes. The deck launcher keeps a configured
  context window that is under the catalog's ceiling as a cap.
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
  boundaries are machine-enforced: a kernel closed to drive-by edits
  (`raven/spine`), the papers
  (`raven/contracts`, every interface a shelf implements), the assembly root
  (`raven/core`, where config becomes a running agent through one door,
  `build_runtime`), the shelves (channels, plugins, providers, memory, ...),
  and the entrances (`cli`, `rpc`, `acp`). Eight import-linter contracts run
  in CI: inner layers never import an entrance (with no allowlisted
  exceptions), the twelve channel adapters are mutually independent, the
  kernel imports nothing else at module level, with no exception named at
  all, the cargo under `raven/agent` never
  imports the loop shell it is consumed by, the runtime never imports the
  repo-level `evolver/` tool that drives it, it never imports the
  `agents/` product definitions built on top of it, and the surfaces law
  holds: the served surfaces (`rpc`, `acp`) never import the launcher and
  rpc never imports acp -- what a served surface needs from the cli arrives
  by registration, and acp reaches rpc only through the bootstrap facade. The papers hold shapes only (machinery such as provider retry
  and tool-argument validation lives with the code that runs it, and a ledger
  test keeps it there). `CONTEXT.md` records every package's seat.
- The EverOS memory backend leaves the wheel: it is its own distribution
  (`plugins-dist/everos-memory`), discovered through the `raven.plugins`
  entry-point group like any third-party plugin. The default configuration is
  unchanged; an install without the plugin keeps booting and says loudly that
  the configured backend is missing.
- The ACP client family (the client, the `acp_agent` sub-agent backend, the
  `acp_dialects` translators) moves from `raven/agent` to the top-level
  `raven/acp_client` package -- an address change only; `raven/acp` remains
  the server side.
- The playbook entry tools (`create_playbook`, `load_playbook`) are bundled
  plugin cargo now (`raven/plugins/bundled/playbook`) and bind the
  loop-assembled runtime late; `playbooks.enabled: false` behaves exactly as
  before.
- A running gateway's composition is sealed per generation: `RavenRuntime` is
  frozen, and a composition change is the next generation through the swap
  path (`raven gateway reload`), never an in-place mutation.
- `raven/agent/subagent/test_state.py` is `probe_state.py`: a production
  module was sitting on pytest's collection pattern.
- **The second read of every module.** Each package was read against a
  nine-item checklist (module docstring, prose in the present tense, no
  cross-package reach into a private name, no import of a surface, no symbol
  without a reader, no public seam without a test, glossary terms, no CJK
  outside the catalog, test naming) and each finding verified independently:
  617 findings, 423 confirmed. What landed from it: every cross-package reach
  into a private name became a public seam (`SessionManager.session_path`,
  `update_providers.oauth_credentials_present`, `schema.section_has_credentials`,
  `ImportState.path`, `EverOSBackend.state`, `BehaviorsExtractor.extract_session`,
  `AgentLoop.notify_turn_complete`, `dag_graph.REQUIRED_NON_BLANK`); symbols with
  no reader are gone (two `raven.auth` placeholders, two MCP inventory views,
  three ops helpers, the playbook trigger expansion the generator supersedes, an
  fd-level terminal redirect, an ISO timestamp helper, a duplicated JSON parser,
  an unread `memory_dir`, an ignored `llm_provider` parameter, three unfilled
  `SkillMeta` fields, a method alias, a no-op validator, two dead config paths);
  three untested seams got tests (the message splitter three channel adapters
  share, the WhatsApp bridge's progress hook, and the control-plane client behind
  `raven gateway status|reload|stop`, driven against a real control plane); five
  channel adapters that wrote their description after a statement -- so the class
  had no docstring at all -- have one; the memory store's lock is named for what
  it is (portable, not fcntl, not a no-op on Windows); and three generated
  benchmark reports, a 97 KB routing sample and two PLAN.md files left the
  package. Throughout, prose that narrated the change which produced a rule now
  states the rule: no design-doc sections, ticket ids, commit hashes, phase codes
  or incident retellings.
- The last two organs get instance sockets on the door: `build_runtime` takes
  `context_engine=` (riding `EngineWiring`, where that organ's config already
  rides) and `executor=` (beside `sandbox_config`, its config twin), and the
  shell binds a handed instance instead of building its own. With provider,
  session, routing, pool, memory and token_wise, all seven decision points are
  now substitutable through the one door, and `tests/test_core_runtime_swap.py`
  pins each one.
- **The audit's second pass.** The confirmations above were written before
  those passes landed, so the remainder was re-read against the tip and verified
  again: 32 were already fixed, 3 were not defects, 67 stood. What that pass
  found and fixed, beyond more of the same: `dry_query` in the repo-level
  `evolver/` tree raised `TypeError` on every call, because a parameter removal
  a few commits earlier had grepped `raven/` and `tests/` and the evolver is
  neither -- it now has tests, so a signature change in `raven/` that breaks it
  fails the suite. `MemoryStore` had two copies of the same compare-and-set
  write; there is one, and the live caller is on it. `connect_mcp_servers` had
  no caller and eight tests pinning its error policy while the connection
  manager's own policy went unpinned; the function is gone and the tests drive
  the live path (one of them had been passing against a handshake that never
  completed). Four cross-package seams that only one caller crossed got tests,
  and so did the personalizer, which nothing had constructed.
- Two vocabularies the glossary had already ruled out are gone from the code.
  `credential_kind` and the `CRED_*` constants are `auth_shape` and `SHAPE_*`
  (CONTEXT.md gains the **Auth Shape** entry that separates what the wizard asks
  for from what makes a connection valid), and the rates ladder no longer calls
  itself pricing, which names the arithmetic on top of it.
- **User-visible:** compaction stops calling itself archiving, because
  `session.archive` is a different feature -- it hides a session and moves no
  message. `session.compress` now answers `compacted N messages` where it said
  `archived N messages`, and a `/new` that cannot fold the tail says "Memory
  consolidation failed". The wire fields are unchanged: `session.compress` still
  answers `removed`, and `session.archive` still takes `archived`.
- The tripwire that guards the surface the vendored product installers import
  named four symbols while those trees reach for eight. It reads the surface out
  of `subagents/*/install.py`, `install.sh` and the README now, so a rename that
  would break a downstream install fails here instead of there.
- The span vocabulary moves out of the kernel to `raven/observability/`.
  Deciding what a raven span *means* -- that an LLM span carries its routing
  backend, that a usage block prices out at a number -- needs the provider
  registry and the token ledger, so the kernel was reaching into two shelves for
  it. The machinery it keeps: context, suppression, the `instrument` decorator,
  the store. Instrumented call sites pass their extractor as an argument, so
  nothing moved but eighteen imports.
- `RAVEN_HOME` and the config path resolve in `raven/home.py`, a kernel module
  that imports nothing: the kernel has to find its own settings, and a core that
  needed the config shelf to locate `config.json` would not be the closure the
  raven-core wheel claims. `config/loader.py` re-exports the three names, so
  every caller reads them where it always did. With that, **all five
  import-linter contracts carry no exceptions at all** -- the last three
  `ignore_imports` entries in the tree are gone.
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
- The evolver moves out of the `raven` package to the repo-level `evolver/`
  tool (`python -m evolver run --config <yaml>`): it drives raven as a
  library, ships in no wheel, and a fifth import-linter contract keeps the
  runtime from importing it back.
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
- A docs-governance pass caught the living records up with the tree: the
  source-language rule and its exemption zones are written down in
  `AGENTS.md`, the repo-layout table covers every package it names as a
  commit scope, and the term canon (`CONTEXT.md`) records the full plugin
  kind roster, compaction, and the product path vocabulary. Stale design
  docs under `docs/` are stamped as dated records and the report assets
  (rendered HTML, SVG diagrams, a market-comparison note) are removed.
- A comment-rectification pass brought in-tree comments back to the
  comment rules: English only, why over what, no edit markers.

### Added

- A top-level `agents/` directory holds product definitions built on the
  installed runtime, A/B-able against the frozen vendored `subagents/`. The
  first product, Raven-Research (transition name Raven-Research-NG), rebuilds
  the vendored research agent's
  whole flow as a plugin (`agents/raven-research/plugins/research-flow`) on
  public seams alone; the sixth import-linter contract keeps the runtime from
  importing the products back.
- Plugins can contribute agent hooks (`[[plugin.contributes.hooks]]`) beside
  memory backends and tools; a config may name extra plugin roots
  (`plugins.dirs`); a contributed tool that needs what only the assembled
  loop owns declares `bind_runtime(handles)` and receives the frozen
  `RuntimeHandles` grants. A factory may decline by returning `None`, a
  binder by raising `BindDeclinedError` -- both leave the built-in serving.
- The agent-hook surface is at version 3: `ctx.session_history` is populated
  at every phase (the iteration phases included, post-consolidation), the
  iteration context carries the turn's `max_iterations` and
  `context_window_tokens` -- the cap the loop actually enforces and the
  active binding's window, so a budget-shaped hook needs no config mirror --
  `CompositeHook` chains every child's diagnostic `notes`, and a hook's
  `observers` stash is filed at persist time, after the send fire.
- The generation freeze law is written where its machine already stood:
  FREEZE seals member identity, and exactly three declared doors reconcile a
  member's data plane mid-generation after the durable truth is written --
  the agents table (`apply_agents`), the MCP server set (`apply_mcp_config`)
  and the default binding (`set_default_binding`). A door-roster guard pins
  who may spell those reaches (the `getattr`/`hasattr` string forms
  included), the plugin market holds the loop through the new `McpHost`
  paper instead of `Any`, and the Generation glossary body now carries the
  identity-vs-interior distinction its own carve-out already implied.
- ACP session modes: `acp.modes` in a product's config becomes a client's
  mode picker (`session/set_mode`), each mode a per-session overlay over the
  base configuration. The mode entry alone carries `maxToolIterations`; the
  overlay ships no copy, since the loop hands its hooks the enforced cap
  directly (`ctx.max_iterations`).
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
  are reachable too), and `@nodes/<node_id>...` addresses this session's own node artifacts
  by path -- which is what reaches a node's own prompt file, since the short
  `{{ <node_id>.output }}` form cannot. The second root stops at that directory rather than
  at agent home: agent home also holds user memory, installed skills, and every *other*
  conversation's transcript and sub-agent history, which `workdir.py` already keeps off the
  agent's file surface, and a DAG graph is LLM-authored and auto-run.
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
- Three more products join `agents/`: `raven-oncall`, `raven-code` and
  `raven-ppt` rebuild their vendored twins on public seams (flow plugins
  `oncall-flow` and `code-flow`; ppt ships no product-local plugins), each
  accepted by transport-face equivalence against the frozen `subagents/`
  tree and recorded in a migration closure record (oncall-closure-0901,
  code-closure-0902, ppt-closure-0902).
- Plugins can contribute three more kinds beside memory backends, tools and
  hooks: background services (`[[plugin.contributes.services]]`), per-call
  tool gates (`tool_gates`) and session observers (`session_observers`).
  The papers are `raven/contracts/services.py`,
  `raven/contracts/tool_gate.py` and `raven/contracts/session_events.py`;
  `CONTRACTS_VERSION` moves to 5.
- The ppt deck-building engine ships as its own distribution
  (`plugins-dist/ppt-engine`, plugin id `ppt-engine`): eleven deck tools
  and a staging hook on the `raven.plugins` entry-point group, with the
  deck skill corpus and templates as package data.

### Fixed

- The research agent's `high` mode may answer a settled general-knowledge
  question without searching, the way `medium` already could: its overlay no
  longer turns the plain-first door off, so a question such as an emperor's
  given name is not sent through search, fetch and a 360-second reviewer wait
  by the host's default tier. `max` keeps researching every question. The
  research model's reasoning effort now follows the tier as well: `medium`
  runs at medium, `high` and `max` at high.
  The plain-first judge is asked once more when its reply names the wrong keys
  (`{"correct": true}` for `plain_ok` and `sound`), which on the default model
  sent about one settled question in ten into research for a parsing miss.
- A sub-agent's result relay into a page session runs on the page spine's lane
  for that session, behind whatever the page is running there, instead of on
  the gateway spine beside it: the two used to run at once, drawing the same
  tool calls twice and delivering the result twice. The watch-work judgement
  the loop pays on a turn's first look is no longer paid on a relay or a
  sentinel notice (the runtime speaking, not the owner), and is cut after 60
  seconds when the model does not answer; one relay turn spent 1229 seconds in
  it.
- A sub-agent instance the user is chatting with is reported to the main agent
  as answering, with the time it began answering and a line saying it is the
  user's conversation, instead of as an instance with no turns yet -- which the
  main agent read as free and dispatched its own task onto. A spawn that
  reaches an instance mid-answer stays `pending` until the instance is free,
  and no longer takes over the instance's live view: the direct chat's steps
  stay on screen, and the spawn's own appear once it runs.
- When reasoning runs to the model's output ceiling and the loop feeds it back
  for the model to continue, the continuation's opening clause -- the tail of
  the cut thought -- no longer reaches the reader as the head of the answer.
- A shell command the safety guard refuses is refused as a command, not as the
  task: the tool result no longer says "stop this operation immediately", which
  an unattended agent read as the whole job and ended a deck build on.
- `read_file` handed a URL says so and names `web_fetch`, instead of a "File
  not found" that reads as a misspelling and sends the caller retrying the
  same URL with prefixes.
- The deck engine's material scan reads every path in one prose match, so two
  documents joined by an ideographic comma both arrive instead of the second
  being dropped without a word.
- The deck launcher (`agents/raven-ppt`) writes the host's or the environment's
  HTTPS proxy into `tools.web.proxy`, since the engine's fetch deliberately
  ignores the process environment; it disables `ask_user`, which an
  unattended build cannot answer; and on the own-key branch against OpenRouter
  it hands the same key to the image generator (`tools.media.image.apiKey`,
  or `PPT_IMAGE_API_KEY`), so a deck can generate its backdrops.
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
- A product engine served over ACP homed itself inside the host's Agent
  home, so every dispatch to it failed before it started -- a raven engine
  refuses a working directory that contains its own home -- while
  capability probing still passed. Product engine homes now live under the
  raven data directory (`product_acp_home` in
  `raven/config/product_render.py`), checked against the configured host
  Agent home, with a per-product `*_ACP_HOME` override that wins outright.

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

- `spawn` now takes a required `node_id`, and it is the same namespace a
  `run_subagent_dag` node id lives in. A later task of either kind names a finished one
  with `{{ <node_id>.output }}`, `{{ <node_id>.output_path }}` or an
  `inputs {"node": <node_id>}` entry, so a spawn can read a graph's node and a graph can
  read a spawn's. Every call must now supply one -- raven no longer mints an id -- and a
  duplicate is refused before dispatch, naming what holds it. An id whose task failed, was
  skipped, was cancelled or is still running is refused with which of those it was, rather
  than resolving to whatever partial file is on disk.
- A spawn's record moved out of `subagents/spawn/<call_id>/` into the flat
  `subagents/nodes/` root the DAG already used, as `<node_id>.prompt.md`,
  `<node_id>.out.md`, `<node_id>.error.md`, `<node_id>.meta.json` and
  `<node_id>.transcript.jsonl`. The `spawn/` tree is gone and is neither migrated nor
  read; records written under it stay on disk and stay readable by absolute path, but no
  listing draws them and no id addresses them. The `Record:` line in a completion
  announcement names the call's output file now, not a directory. Direct chats are
  unaffected and keep a directory per call.
- On the wire, `call_id` keeps its name and changes its value: it carries the node id the
  model chose rather than a minted timestamp. It is no longer time-ordered, so the
  `subagent.list` ordering reads the registry's own clock instead of sorting on the id.
- A `run_subagent_dag` node id must now be unique across the whole conversation, not
  just within its own graph, and a graph that reuses one an earlier run took is refused
  before any node is dispatched. That is what makes an id an address: a later graph reads
  an earlier run's node with `{{ <id>.output }}` and no `depends_on`. Graphs that reused
  a generic id (`plan`, `step1`) across runs in one conversation used to run and now need
  a fresh id each time; the refusal says which run holds the id and offers referencing it
  instead.
- `@runs/<run_id>/...` no longer addresses anything: node artifacts moved to a flat,
  id-keyed layout, and the address is the node id, `@nodes/<node_id>...`, which also
  reaches a node's own prompt file -- something the short `{{ <id>.output }}` form
  cannot. A reference still spelled `@runs/...` is not specially rejected; it falls
  through to a literal workdir-relative path, which normally does not exist, so it
  fails the same not-found check as any other bad reference.
- No migration ships for the registry: `nodes.json` starts empty and any
  `mas_dag/index.json` written by an earlier build is ignored. Every node id used in a
  conversation before this change becomes free again -- a conversation that used
  `plan` last week can reuse it, and `{{ plan.output }}` then names the new one, not
  the old. The old node was never going to be id-addressable after this change either
  way, but the reuse itself is a visible behaviour change. Its artifacts still exist
  and are still readable, by absolute path under the session's sub-agent history; with
  `@runs/` retired they need the full path, not a prefix.
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
