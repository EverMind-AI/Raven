# Agent Plugin Packages: reading the portable Agent Plugins 1.0 format in Raven

Date: 2026-10-25. Status: design proposed; nothing implemented.

## Problem

Agent Plugins 1.0 (`https://agent-plugins.org/specification`) is a portable
package format for agent components: a directory whose root holds a
`plugin.json` manifest, optional `skills/` directories, and an optional
`mcp.json`. It is published and used -- ten hosts are listed as compatible
clients (VS Code, Cursor, Copilot, ChatGPT/Codex, Kiro, Hermes, OpenClaw,
Grok Bot, NanoClaw, OpenHands), and repositories such as
`sickn33/agentic-awesome-skills` ship thirty packages in it (368 skills
across them, all of which pass the format's manifest and skill rules).

Raven cannot read that format today. Two unrelated things in this repo are
called "plugin", and neither is it:

1. **The kernel plugin system** (`raven/plugins/`). A plugin is a Python
   package that ships `raven-plugin.toml`
   (`raven/plugins/manifest.py:249`), declares one of seven contribution
   kinds -- `memory_backends`, `tools`, `hooks`, `services`, `tool_gates`,
   `session_observers`, `onboard` (`raven/plugins/manifest.py:231-246`) --
   and its factories are imported into the host process. The code is
   trusted and runs in-process (`docs-site/docs/building-plugin.md:4-6`).
   There is **no `skills` kind and no `mcp` kind**; a manifest declaring
   either loads and has the contribution silently dropped, because the
   model ignores unknown fields (`raven/plugins/manifest.py:52`).
2. **PlugHub** (`raven/market/`). A curated catalog whose entries are pure
   data contributing an MCP server or a SkillHub skill; installing one
   writes a stanza into `tools.mcpServers` in `~/.raven/config.json` and
   records a ledger (`raven/market/install.py:186-233`,
   `raven/market/ledger.py:59-67`). A `python` kind is refused outright
   (`raven/market/install.py:213`).

Neither reads a directory that someone else authored. So a person who wants
the skills from a portable package in Raven has to copy roughly twenty skill
directories by hand into `<workspace>/skills/`. The result has no package
identity, no version, no record of which skill came from where, no report of
the skills that did not fit, and no way to tell that the copy is stale.

## Goals

- **G1** Read a package's root `plugin.json` and decide conformance against
  the Agent Plugins 1.0.0 manifest rules, with the specification's own
  failure boundaries: an unknown top-level field is reported and ignored,
  every other violation rejects the package.
- **G2** Discover the package's skills exactly as the specification defines
  them (`skills/`, immediate children only, `SKILL.md` present), validate
  each against the Agent Skills rules, skip the invalid ones, and report
  every skip with its reason.
- **G3** Make those skills usable in Raven: present in the skill pool under
  a per-package source label, findable by the model, and runnable through
  the existing `exec` path without a new execution route.
- **G4** Keep the package outside Raven's trust boundary: no module from the
  package is imported, no directory outside the workspace becomes writable
  because of it, and a package cannot place instructions into every turn.
- **G5** Let the operator see and steer it: list, inspect, import, remove,
  disable per package, and a report of what was skipped and why.
- **G6** Keep the format open: a package that declares an Agent Plugins
  version Raven does not implement is rejected with a clear reason, never
  half-loaded, and Raven never fetches a schema while loading a plugin
  (spec §5.2).
- **G7** (Phase 2) Load the package's `mcp.json` servers under Raven's MCP
  rules, with an explicit per-package opt-in.

## Non-goals

- **Not a conformant Agent Plugins client submission.** Phase 1 is
  skills-only, which the specification allows (§11.2: "a skills-only client
  can conform without supporting MCP servers"). Being listed under
  `agent-plugins.org/compatible-clients` is a later, separate step.
- **Not a registry client.** No download, no signing, no directory API, no
  update channel. A package is a directory the operator already has.
- **Not an adapter for the host-native formats.** `.claude-plugin/` and
  `.codex-plugin/` packages (what `agentic-awesome-skills` ships for Claude
  Code and Codex) are a different format with different semantics; Phase 3
  may add them, Phase 1 does not.
- **No change to `raven-plugin.toml`.** The kernel plugin model stays as it
  is. The two systems keep separate manifests, separate discovery and
  separate trust stories.
- **Not a sandbox for package skills.** Package skills run their scripts
  through the ordinary `exec` tool, with the ordinary workspace fence and
  the ordinary approval gate.
- **No vendored copy of the specification prose.** The two JSON schemas are
  vendored (they are the machine-readable contract); the prose is cited.

## Terms

**Agent Plugin Package** (added to `CONTEXT.md` with this change). A
directory on disk whose root holds a `plugin.json` that validates against
the Agent Plugins 1.0.0 manifest schema, carrying optional `skills/` and
`mcp.json` components. It is data only: it contributes no Python, no hooks
and no tools to the host.
_Avoid_: "portable plugin", "third-party plugin", "plugin package"
unqualified, "Agent Plugin" unqualified.

**Package import.** Copying a package's validated skills into the workspace
skill tree under the package's own source label, and recording the copy in
the package ledger.

**Package source label.** The skill-pool source label minted for a package:
the `name` in the package's manifest. One label per package, so two
packages may each ship a skill called `postgres-best-practices` without
either shadowing the other.

**Package ledger.** The file that records which packages are imported, from
which path, at which version, and what the installed copy looked like.

## Why an import, not a mount

Raven's `exec` fence is `[cwd, workspace]`: the tool resolves the workspace
and the extra allowed directories and refuses a command that reaches outside
them (`raven/agent/tools/shell.py:433`, `:582`; the main loop passes the
workspace as the only extra directory, `raven/agent/loop/wiring.py:1249`).

A package mounted outside the workspace therefore yields skills whose
scripts the agent cannot run -- the model is handed an absolute path
(`raven/memory_engine/skill_forge/catalog.py:73-75` rewrites references to
absolute paths) and `exec` refuses it. Widening the fence to a third-party
directory is a boundary change, and it is not one to make by default.

The workspace skill tree is also the only layer the file watcher covers
(`raven/memory_engine/skill_forge/catalog.py:265-269`) and the only layer
whose skills can execute anything. So Phase 1 copies, and the copy is
recorded, owned and removable. The precedent is the existing importer:
`install_skills` copies Claude Code and Hermes skills into
`<workspace>/skills/<platform>/<name>/` (`raven/importer/skills/installer.py:53-152`).

The cost is real and named: a copy can go stale. The ledger's fingerprint
detects that, and `import` re-runs are how it is refreshed.

## Design

### 1. Module layout

```text
raven/agent_plugins/
  __init__.py     public surface
  manifest.py     AgentPluginManifest, AgentPluginMcpConfig
  package.py      package discovery, resolution, containment
  skills.py       Agent Skills validation and the import
  ledger.py       the package ledger
  mcp.py          Phase 2
schemas/agent-plugins/1.0.0/
  plugin.schema.json    vendored, unmodified
  mcp.schema.json       vendored, unmodified
  NOTICE.md             provenance and licence of the vendored files
```

The two schemas are the machine-readable contract, so they are vendored
verbatim with a `NOTICE.md` naming the upstream source and its Apache-2.0
licence, the same way `agentic-awesome-skills` does it. They are not
generated from our models (`schemas/raven-plugin.schema.json` is, by
`scripts/export_agent_schemas.py`; that script writes exactly two files and
does not own this directory).

No new runtime dependency: `pydantic` and the standard library are enough.
`jsonschema` stays a dev dependency and is used by the cross-check test
(§Testing).

### 2. Manifest validation

`AgentPluginManifest` is a pydantic model that mirrors the official
schema (`schemas/agent-plugins/1.0.0/plugin.schema.json`):

| Field | Rule |
|---|---|
| `$schema` | `Literal["https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"]`; required |
| `name` | required; 1-64 chars; `^(?!.*(?:--\|\.\.))[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?$` (spec §5.5) |
| `version` | string; Semantic Versioning recommended (§10.2) |
| `description`, `homepage`, `repository`, `license` | strings, validated by type only (§5.4) |
| `author` | object with `name`, `email`, `url` only -- extra keys make the manifest invalid |
| `keywords` | array of strings |
| `extensions` | object whose members are objects; namespaces Raven does not implement are ignored without validating their contents (§8.1) |

Two behaviours do not fit a plain model, so they are coded rather than
declared:

- **Unknown top-level fields are non-fatal** (§5.2: "clients MUST report
  and ignore each unknown field and MUST continue loading the plugin"). The
  model accepts extras, collects their names, and the loader reports them.
  The `author` object stays closed, because the official schema closes it
  and the specification makes such a violation fatal.
- **An unsupported `$schema` is a rejection, not a partial load** (§5.2:
  a client that does not support the declared version MUST reject the
  plugin). Raven recognises exactly the versions it implements and never
  retrieves a schema while loading.

Everything else that violates the schema is fatal for the package: no
skills and no MCP servers are taken from it, and the reason is reported.

`tests/test_agent_plugins_manifest.py` validates our model against the
vendored official schema with `jsonschema` over a corpus of manifests
(valid, unknown-field, bad name, wrong author shape, missing field), so the
two cannot drift apart.

### 3. Discovery and containment

Configuration (new top-level section, §Configuration):

```json
"agentPlugins": {
  "packages": ["/opt/agent-plugins", "/opt/single-package"],
  "disabled": ["agentic-bundle-example"],
  "mcpPackages": []
}
```

Each `packages` entry is either a package root (a directory holding
`plugin.json`) or a directory whose immediate children are package roots.
Discovery reads manifests only and never imports anything from a package.

Containment follows spec §4.1. The package root is resolved once
(`Path.resolve()`), and every path the loader touches must satisfy
`is_relative_to(root)` after resolution. A symlink or junction that
resolves outside the root is refused for that component -- the skill is
skipped, the server entry is invalid -- while the rest of the package still
loads. A `plugin.json` that does not resolve inside its own root rejects
the package (§4.1).

### 4. Skills: discovery, validation, import

**Discovery** (§6.1, §7.1). `skills/` absent is not an error. `skills/`
present but not a directory invalidates that component type only; other
component types still load (§6.2). Otherwise each immediate child
directory holding a regular file named exactly `SKILL.md` is one skill.
The specification forbids searching deeper (`"Clients MUST NOT recursively
search deeper descendants"`), so the loader reads one level, not a glob.
Note that Raven's own registry walks recursively
(`raven/memory_engine/skill_local/registry.py:395-440`), which is why the
package's skills are read here and handed over, not merely pointed at.

**Validation** (Agent Skills specification). A skill must carry frontmatter
whose `name` is 1-64 characters of lowercase letters, digits and hyphens,
with no leading, trailing or consecutive hyphen, and equal to its
directory name; and whose `description` is 1-1024 characters. A skill that
fails is skipped and reported; the package's other skills still load
(§7.1).

Raven's frontmatter reader is reused as is:
`raven/utils/text.py:parse_frontmatter` (a `yaml.SafeLoader` subclass that
refuses aliases and caps nesting). No YAML dependency is added, and no
second parser is introduced.

**Import.** The target is `<workspace>/skills/<package name>/<skill name>/`.
The first directory level below a layer root is the skill's source label
(`raven/memory_engine/skill_local/registry.py:395-440`), so one directory
per package mints one label per package rather than one per skill. This
matters: the thirty `agentic-awesome-skills` bundles ship 368 skills with
only 289 distinct names, and a single shared label would let 79 of them
shadow each other.

The import copies each skill directory whole (`scripts/`, `references/`,
`assets/` included) with `shutil.copytree`, the way the existing importer
does (`raven/importer/skills/installer.py:116`).

Two collisions are handled rather than papered over, at two levels:

- The **package directory** (`<workspace>/skills/<package>/`). If it exists
  and the ledger does not own it, the whole import is refused. Merging a
  package into a directory that belongs to something else -- the operator's
  own skill of the same name, most likely -- is never what anyone meant,
  and the refusal says so instead of quietly merging.
- A **skill directory** inside a package directory the ledger owns. If it
  exists and the ledger does not claim that skill, it is skipped and
  reported; the import continues with the remaining skills. The ledger
  records the skills it installed, so a directory the operator put there by
  hand is not the ledger's to replace. This is the importer's "an existing
  on-disk target is never overwritten" rule
  (`raven/importer/skills/installer.py:181-211`), at skill granularity.

A skill whose frontmatter name is already claimed inside the same package
is skipped and reported: the pool keys a skill as `(source, frontmatter
name)` (`raven/memory_engine/skill_local/registry.py:246`), so a second
skill under any directory name would be the invisible one.

After a copy the pool is invalidated for the affected source
(`LocalSkillCatalog.invalidate_skill_cache`, `raven/memory_engine/skill_forge/catalog.py:222-239`),
so the skills are visible on the next turn.

### 5. Keeping a package out of the trust boundary

Four rules, all of them about a package's instructions rather than its
bytes:

1. **No unconditional injection.** A package skill's `always: true` -- and
   the `metadata.<ns>.always` form the registry honours
   (`raven/memory_engine/skill_local/registry.py:552-561`) -- is dropped
   when the copy is written. A third-party package must not be able to put
   text into every turn; its skills stay retrievable, which is where the
   router puts them when they are relevant. The copy is ours, so the
   frontmatter we write is the frontmatter the pool reads.
2. **The operator blocklist applies.** A skill whose name is on
   `skillForge.blocklist` is not imported
   (`raven/skill_hub/policy.py:191-210`). The list is normalised the way
   the Hub path normalises it (`normalize_blocklist`), so the match is
   case-insensitive in both directions.
3. **The foreign-home lint applies.** A skill body pointing at another
   product's dot-directory (`~/.openclaw` and friends) is refused, the same
   rule the Skill Hub path applies
   (`raven/skill_hub/policy.py:74-108`). Only `~/.raven` is ours.
4. **No new execution route.** A package skill's scripts are run by the
   ordinary `exec` tool, under the ordinary workspace fence and the ordinary
   approval gate. Raven never executes a skill script itself
   (`raven/agent/tools/skill_hub.py:303-317` returns a path; the model calls
   `exec`).

### 6. MCP servers (Phase 2)

The format defines `mcp.json` at the package root with a closed top level
(`$schema`, `mcpServers`) and three server variants (§7.2).

Phase 2 needs work Raven does not have today, and the design records it
rather than hiding it:

| Requirement (spec) | What Raven has | Work |
|---|---|---|
| `cwd` for a stdio server, defaulting to the package root (§7.2.1) | `MCPServerConfig` has no `cwd` (`raven/config/schema.py:1271-1292`); `StdioServerParameters` gets command/args/env only (`raven/mcp/client.py:176-234`); `SandboxExecutor.start_process` has no `cwd` (`raven/sandbox/interfaces.py:87-102`) | add `cwd` to the config, pass it to the SDK, and extend the sandbox interface with an optional `cwd` |
| `${PLUGIN_ROOT}` / `${PLUGIN_DATA}` expansion in `args`, `env`, `cwd` (§9.2) | no `${...}` expansion exists; the only expansion is `${params.X}` in playbooks (`raven/playbook/params.py:24`) | a single, non-recursive expansion in the loader, applied to exactly those three fields |
| `PLUGIN_ROOT` / `PLUGIN_DATA` in the subprocess environment, set last (§9.1) | the SDK overlays `env` over `os.environ` (`raven/mcp/client.py:200`) | compose the environment explicitly (base + configured overlay + the two reserved variables) |
| a per-instance persistent data directory (§9.1) | no such concept | `<RAVEN_HOME>/plugin-data/<package>/` |
| remote URL rules: absolute, no userinfo or fragment, https unless the host is exactly `localhost` or a loopback IP (§7.2.1) | `raven/security/urls.py:require_https` already encodes that rule | reuse it |
| an invalid server entry must not take the others down (§7.2.2) | the manager already isolates per-server failures | keep the isolation boundary at the loader |

**Trust.** A package's `mcp.json` starts a process on the operator's
machine, which is arbitrary code execution from a third-party directory.
Phase 2 therefore requires an explicit per-package opt-in
(`agentPlugins.mcpPackages`). By default a package's `mcp.json` is parsed,
validated and reported, and no server is started. The opt-in is per
package, not global.

**Namespacing.** A package's server names are qualified with the package
name (`<package>__<server>`) so they cannot collide with the host's own
`tools.mcpServers` entries, and so a sub-agent row's `mcps` list
(`raven/agent/subagent/mcp_grant.py:364-448`) can name them precisely. The
separator is `__` because the manifest's name rules forbid an underscore in
a package name, so the first occurrence is unambiguous.

**Where they live.** The package's servers are composed at boot next to the
host's `tools.mcpServers` (`raven/config/live.py:465-483`), not written into
`~/.raven/config.json`: the config file stays the operator's own, and the
package's servers come and go with the package. They are also kept apart
inside the loop (`_package_mcp_servers`): a live answer about the host's own
servers must not drop them, so every path to the manager merges the host's
set with the package layer.

**What Phase 2 shipped.** `raven/agent_plugins/mcp.py` reads the file and
composes the surviving entries; `MCPServerConfig` gained `cwd`, which the
stdio transport passes to the SDK; the loop merges the package layer into
every MCP apply; the data directory is
`<RAVEN_HOME>/plugin-data/<package>/`, created before a server starts;
`raven agent-plugins list` and `inspect` report the servers and the
problems without starting anything. A package's stdio server whose `cwd`
is set cannot run under a sandboxed executor (the interface has no `cwd`),
so that combination is refused with a clear reason rather than silently
launched in the wrong directory.

The rows above are kept as the record of what the phase had to add; the
table describes the state before Phase 2 landed.

### 7. The package ledger

`~/.raven/agent-plugins.json` (path added to `raven/config/paths.py`):

```json
{
  "version": 1,
  "packages": {
    "agentic-bundle-essentials": {
      "version": "20.0.0",
      "source": "/opt/agent-plugins/agentic-bundle-essentials",
      "imported_at": "2026-10-25T12:00:00Z",
      "skills": ["api-design", "lint-and-validate"],
      "fingerprint": "sha256:..."
    }
  }
}
```

The fingerprint covers the package's skill files (relative path, size and
content hash), so `raven agent-plugins check` can say whether the installed
copy is stale without re-reading every file into memory. The ledger is what
makes removal safe: `remove` deletes only the directories the ledger says
it owns.

### 8. Configuration

A new top-level section in `raven/config/schema.py`:

| Key | Meaning |
|---|---|
| `agentPlugins.packages` | paths to package roots, or to directories of package roots |
| `agentPlugins.disabled` | package names that are not imported |
| `agentPlugins.mcpPackages` | package names whose `mcp.json` servers may start (Phase 2) |

Phase 1 writes nothing into the user's config: `import` copies files and
records them in the ledger. The config section only declares where packages
live and which are off.

### 9. CLI

A new command group, `raven agent-plugins`, in
`raven/cli/agent_plugin_commands.py`:

| Command | What it does |
|---|---|
| `list` | every known package: name, version, source path, imported skills, skipped skills with reasons, MCP servers (Phase 2), status |
| `inspect <path>` | validate a package and report, touching nothing |
| `import <path>` | validate and import; reports what was installed and what was skipped |
| `remove <name>` | delete the ledger-owned directories of one package |
| `check` | compare the ledger's fingerprints against the packages on disk |

Every command takes `--json` for scripting. `raven plugins` keeps its
current meaning (the kernel plugin list) and is not extended.

### 10. Contract surface

Phase 1 adds no entry to `raven/contracts/`: nothing new is contributed to
the runtime, and the loader is an ordinary internal package with the CLI as
its surface. No RPC method and no web panel in Phase 1; the settings page
can grow one later if the operator asks for it.

## Testing

| File | Covers |
|---|---|
| `tests/test_agent_plugins_manifest.py` | our model against the vendored official schema via `jsonschema`; unknown-field tolerance; unsupported `$schema` rejection; the §5.5 name rules; the closed `author` object |
| `tests/test_agent_plugins_package.py` | package-root vs directory-of-packages discovery; containment (a symlink escaping the root, a `../` path); missing `skills/`; `skills/` that is not a directory |
| `tests/test_agent_plugins_skills.py` | Agent Skills validation cases; the import; name collisions inside a package; a target owned by someone else; `always` neutralisation; the blocklist; the foreign-home lint; the fingerprint |
| `tests/test_cli_agent_plugin_commands.py` | the CLI commands, per the naming rule in `AGENTS.md` §5.1 |
| `tests/integration/test_agent_plugins_e2e.py` | a fixture package imported into a temporary workspace: the pool lists its skills under the package label, the prompt carries one, `remove` cleans up |

## Delivery

- **Phase 1** (this design): manifest validation, package discovery,
  containment, skill validation, the import, the ledger, the CLI.
- **Phase 2**: `mcp.json` -- `cwd` on the MCP config and the sandbox
  interface, `${PLUGIN_ROOT}`/`${PLUGIN_DATA}` expansion, the package data
  directory, the per-package opt-in, namespaced server names.
- **Phase 3** (optional): adapters for the host-native formats
  (`.claude-plugin/`, `.codex-plugin/`) so that a repository's root plugin
  is readable too, and a directory client.

## Compatibility and risks

- **The workspace is written to.** An import adds
  `<workspace>/skills/<package>/` directories. The operator can delete
  them; `remove` does it and updates the ledger. The fingerprint lets
  `check` say when the copy is stale.
- **Third-party instructions reach the model.** That is what a skill is.
  The four rules in §5 are the mitigation, and they are not a substitute for
  reading what one installs.
- **A package cannot clobber a user's own skill.** A target that exists and
  is not ledger-owned is never overwritten; the import reports the
  collision.
- **The package name is a safe directory name.** The manifest's `name` is
  constrained to lowercase letters, digits, hyphens and single periods, so
  it cannot carry a path separator or a traversal.
- **No new runtime dependency.** `pydantic` and the standard library; the
  schema cross-check uses the dev-only `jsonschema`.
- **The specification will move.** `$schema` is pinned to a canonical
  identifier and Raven never fetches a schema at load time, so a new
  specification release is a deliberate change to the pinned constant, not a
  surprise at runtime.
- **Windows specifics.** The copy preserves bytes as they are; long paths
  and the copy of a large tree are the practical limits, and both are the
  operator's to notice through `import`'s report.
- **The two plugin systems stay separate.** `raven plugins` continues to
  list kernel plugins; `raven agent-plugins` lists portable packages.
  The only shared thing is the word, which is why the domain term exists.

## Open questions for the maintainer

1. **Import (copy) or mount (read-only)?** The design argues for the copy,
   because the `exec` fence is `[cwd, workspace]` and a mounted package's
   scripts would be unreachable. A mount would need `exec`'s roots widened
   to a third-party directory.
2. **Ledger location.** `~/.raven/agent-plugins.json` (survives a workspace
   reset) or under the workspace (travels with the skills it describes)?
   The design picks the home.
3. **May a package skill be `always`?** The design says no, and drops the
   flag on copy. An operator who trusts a package enough may want it.
4. **MCP trust granularity.** The design proposes a per-package opt-in
   list; a single global switch would be simpler and coarser.
5. **Should a package with no `skills/` be importable at all?** The design
   parses and reports it but imports nothing until Phase 2 gives its MCP
   servers a home.
