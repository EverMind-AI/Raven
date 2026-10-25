# Agent Plugin Packages

Raven reads the portable package format of
[Agent Plugins 1.0](https://agent-plugins.org/specification): a directory
whose root holds a `plugin.json`, with optional `skills/` and `mcp.json`
components. Several ecosystems publish packages in this format -- for
example the `agentic-awesome-skills` repository ships thirty of them.

This is **not** the same thing as a Raven plugin. A Raven plugin
(`raven-plugin.toml`) ships Python factories that run inside the host
process; an Agent Plugin Package is data only: skills, and MCP servers in a
later phase. Nothing from a package is ever imported as code.

## What a package looks like

```text
my-package/
├── plugin.json          # required, at the package root
└── skills/
    └── my-skill/
        ├── SKILL.md     # required in every skill directory
        ├── scripts/     # optional
        └── references/  # optional
```

`plugin.json` is validated against the official Agent Plugins manifest
schema. A package that names an Agent Plugins version Raven does not
implement is refused, and so is a manifest that breaks the schema; an
unknown field is reported and ignored.

## Pointing Raven at a package

```json
{
  "agentPlugins": {
    "packages": ["/opt/agent-plugins"]
  }
}
```

Each entry is either a package root (a directory holding `plugin.json`) or
a directory whose immediate children are package roots. Relative paths
resolve against the working directory. `agentPlugins.disabled` names
packages to leave out.

## The commands

```bash
raven agent-plugins list                 # the packages Raven can see
raven agent-plugins inspect /opt/my-package   # validate one, import nothing
raven agent-plugins import /opt/my-package    # import its skills
raven agent-plugins check                 # report a copy that went stale
raven agent-plugins remove my-package     # delete the imported skills
```

Every command takes `--json` for scripting.

## What an import does

The package's skills are copied into the workspace skill tree, under
`<workspace>/skills/<package name>/`. One directory per package means the
skill pool mints one source label per package rather than one per skill, so
two packages may each ship a skill called `postgres-best-practices` without
either shadowing the other.

The copy is recorded in the package ledger (`<home>/agent-plugins.json`).
That record is what makes `remove` safe: only the directories the ledger
claims are ever deleted. `check` compares the ledger's fingerprint against
the package on disk, so a package that has been updated since the import is
reported as stale.

The imported skills are ordinary workspace skills afterwards: the file
watcher picks them up, the router can retrieve them, and their scripts run
through the ordinary `exec` tool under the ordinary workspace fence.

## What a package cannot do

- **It cannot inject itself into every turn.** The `always` flag is dropped
  from the imported copy. A package's skills stay retrievable, which is
  where the router puts them when they are relevant.
- **It cannot run outside the operator's blocklist.** A skill whose name is
  on `skillForge.blocklist` is not imported, and a skill whose body points
  at another product's home directory (`~/.openclaw` and friends) is
  refused.
- **It cannot execute code on import.** Raven copies files; the scripts
  inside a skill run only when the model decides to run them, through
  `exec`.
- **It cannot overwrite your own skills.** A target directory that exists
  and is not owned by the ledger is left alone, and the import says so.

## MCP servers a package ships

A package may carry an `mcp.json` naming MCP servers. Raven reads it,
validates every entry and reports what it found; it starts nothing until
you name the package:

```json
{
  "agentPlugins": {
    "packages": ["/opt/agent-plugins"],
    "mcpPackages": ["my-package"]
  }
}
```

That opt-in is per package because a server entry is a process on your
machine. The servers a package contributes are named
`<package>__<server>`, so they never collide with your own
`tools.mcpServers` entries.

A package's servers run with the package root as the working directory
(unless the entry names another one), and with `PLUGIN_ROOT` and
`PLUGIN_DATA` in their environment: the first is the package directory,
the second is a directory Raven keeps for that package across updates
(`<home>/plugin-data/<package>/`). The format's two placeholders,
`${PLUGIN_ROOT}` and `${PLUGIN_DATA}`, are expanded in a server's
arguments, environment and working directory.

One bad server entry does not disable the package: the entry is skipped
and reported, and the rest still run. A package whose `mcp.json` declares
a different Agent Plugins version than its `plugin.json`, or whose file is
not a valid MCP configuration, has MCP disabled while its skills keep
working.

## Current limits

- Raven does not download packages. A package is a directory you already
  have; there is no registry client and no update channel.
- The host-native formats of other tools (`.claude-plugin/`, `.codex-plugin/`)
  are not read yet.
- A package's stdio server that names a working directory cannot run under
  a sandboxed executor: the sandbox interface cannot set one, so the server
  is refused rather than launched in the wrong place.
